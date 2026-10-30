"""OpenTimestamps: anchor a SHA-256 in the Bitcoin blockchain through the public calendars (no account; only the
hash leaves the server). A fresh proof is "pending" until a calendar's Bitcoin transaction confirms (a few hours);
upgrade() then fetches the path to the block, and the Bitcoin block header (from an Esplora explorer API) is the
check that the proof is real: the proof's final commitment must equal that block's merkle root.

Verify a downloaded proof independently with:  pip install opentimestamps-client && ots verify -d <sha256> receipt.ots

Self-check (talks to the real calendars):  python -m sdc.stamp
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpAppend, OpSHA256
from opentimestamps.core.serialize import BytesDeserializationContext, BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

CALENDARS = ("https://a.pool.opentimestamps.org", "https://b.pool.opentimestamps.org",
             "https://a.pool.eternitywall.com", "https://ots.btc.catallaxy.com")
# the pools above hand out these calendars' URIs in pending attestations; upgrades only ever ask them
UPGRADE_HOSTS = (".opentimestamps.org", ".eternitywall.com", ".catallaxy.com")
ESPLORA = ("https://mempool.space/api", "https://blockstream.info/api")  # same API; some ISPs block one
_tried = {}  # proof path -> last upgrade attempt; calendars only change when Bitcoin mines, so ask at most every 5 min


def stamp(digest: bytes) -> bytes:
    """Submit to every calendar that answers; returns the serialized .ots proof (raises if none answered)."""
    ts = Timestamp(digest)
    tip = ts.ops.add(OpAppend(os.urandom(16))).ops.add(OpSHA256())  # nonce: the calendars never learn the digest
    errors = []
    for url in CALENDARS:
        try:
            tip.merge(RemoteCalendar(url).submit(tip.msg, timeout=10))
        except Exception as e:  # noqa: BLE001 - one calendar down is fine, the proof keeps the others
            errors.append(f"{url}: {type(e).__name__}")
    if not tip.attestations and not tip.ops:
        raise RuntimeError("no OpenTimestamps calendar answered: " + "; ".join(errors))
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    return ctx.getbytes()


def load(ots: bytes) -> DetachedTimestampFile:
    return DetachedTimestampFile.deserialize(BytesDeserializationContext(ots))


def _walk(ts):
    yield ts
    for sub in ts.ops.values():
        yield from _walk(sub)


def upgrade(ots: bytes) -> bytes:
    """Ask each pending calendar for the Bitcoin path; returns the proof, upgraded where a calendar had it."""
    f = load(ots)
    for node in list(_walk(f.timestamp)):
        for att in [a for a in node.attestations if isinstance(a, PendingAttestation)]:
            host = urllib.parse.urlsplit(att.uri)
            if host.scheme != "https" or not (host.hostname or "").endswith(UPGRADE_HOSTS):
                continue
            try:
                node.merge(RemoteCalendar(att.uri).get_timestamp(node.msg, timeout=10))
            except Exception:  # noqa: BLE001 - still pending (404) or calendar down: try again later
                pass
    ctx = BytesSerializationContext()
    f.serialize(ctx)
    return ctx.getbytes()


def _get(path):
    err = None
    for base in ESPLORA:
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, headers={"User-Agent": "show-dont-code"}),
                                        timeout=10) as r:
                return r.read().decode()
        except OSError as e:
            err = e
    raise err


def status(ots: bytes) -> dict:
    """{"digest", "calendars" (pending), "bitcoin": [{"height", "block", "time"}] checked against Esplora}."""
    f = load(ots)
    pending, blocks = [], []
    for msg, att in f.timestamp.all_attestations():
        if isinstance(att, PendingAttestation):
            pending.append(att.uri)
        elif isinstance(att, BitcoinBlockHeaderAttestation):
            block = _get(f"/block-height/{att.height}").strip()
            header = json.loads(_get(f"/block/{block}"))
            # Esplora prints the merkle root big-endian; the proof commits to it in Bitcoin's internal byte order
            if bytes.fromhex(header["merkle_root"])[::-1] != msg:
                raise ValueError(f"proof does not match Bitcoin block {att.height}")
            blocks.append({"height": att.height, "block": block, "time": header["timestamp"]})
    return {"digest": f.file_digest.hex(), "calendars": sorted(set(pending)), "bitcoin": blocks}


def _write(path, data: bytes):
    """Whole file or nothing: a receipt read while the proof is being written used to see a half-written file
    (BadMagicError). Write beside it, then rename over it (atomic on one filesystem)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure(path, digest: bytes) -> dict:
    """The proof for `digest` at `path`: stamped on first use, upgraded when due; returns status() plus "proof"."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, stamp(digest))
    elif time.time() - _tried.get(str(path), 0) > 300 and not status(path.read_bytes())["bitcoin"]:
        _tried[str(path)] = time.time()
        if time.time() - path.stat().st_mtime > 1800:  # a calendar commits to Bitcoin about once an hour
            _write(path, upgrade(path.read_bytes()))
    s = status(path.read_bytes())
    assert s["digest"] == digest.hex(), "stored proof is for another digest"
    return s


if __name__ == "__main__":
    import hashlib
    d = hashlib.sha256(b"show-dont-code self-check " + os.urandom(8)).digest()
    p = stamp(d)
    s = status(p)
    assert s["digest"] == d.hex() and s["calendars"] and not s["bitcoin"], s
    assert status(upgrade(p))["digest"] == d.hex()  # a fresh proof stays pending, and upgrading keeps it intact
    # the Bitcoin path, on the OpenTimestamps project's own example proof (block 358391, 2015)
    hw = urllib.request.urlopen("https://raw.githubusercontent.com/opentimestamps/opentimestamps-client/master/"
                                "examples/hello-world.txt.ots", timeout=10).read()
    b = status(hw)
    assert b["digest"] == hashlib.sha256(b"Hello World!\n").hexdigest() and b["bitcoin"][0]["height"] == 358391, b
    # and upgrade(): the project's example pending proof, which the alice calendar has since committed to Bitcoin
    inc = urllib.request.urlopen("https://raw.githubusercontent.com/opentimestamps/opentimestamps-client/master/"
                                 "examples/incomplete.txt.ots", timeout=10).read()
    assert not status(inc)["bitcoin"] and status(upgrade(inc))["bitcoin"][0]["height"] == 428648
    bad = bytearray(hw)
    bad[-40] ^= 1  # flip one bit on the path to the block: the merkle root no longer matches
    try:
        status(bytes(bad))
        raise AssertionError("tampered proof accepted")
    except ValueError:
        pass
    print(f"stamped on {len(s['calendars'])} calendars (pending Bitcoin); example proofs verified at blocks 358391 and 428648 (after upgrade): OK")
