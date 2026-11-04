"""Send a run's .eln straight into the user's own eLabFTW (API v2 import), as an experiment visible to its owner only.

eLabFTW sends no CORS headers, so the browser can't call it: the app's server forwards the file with the user's API
key, which is used for this one request and never stored or logged. Because the server connects to an address the user
typed, the address must be https on port 443 and resolve only to public IPs, and the connection is pinned to the IP that
was checked (no DNS rebinding) with redirects refused.

  python -m sdc.elab                       self-check of the address guard (no network beyond DNS)
  python -m sdc.elab <eln> <url> <keyfile> import a file for real
"""
import ipaddress
import json
import re
import socket
import urllib.parse
import uuid

import certifi
import urllib3

TIMEOUT = urllib3.Timeout(connect=10, read=120)


class Refused(ValueError):
    """The address or key can't be used; the message says why, for the user."""


MAX_REPLY = 1 << 20  # eLabFTW's answers are small JSON; a server that streams more is cut off
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _public(addr):
    """A public unicast IP, also judged by any IPv4 address inside an IPv6 one (::ffff:a.b.c.d, ::a.b.c.d, 6to4,
    NAT64 64:ff9b::a.b.c.d): Python's is_global alone accepts 64:ff9b::a9fe:a9fe, which is 169.254.169.254."""
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.version == 6:
        if int(ip) >> 32 == 0:  # ::a.b.c.d, deprecated IPv4-compatible: never a real server
            return False
        inner = ip.ipv4_mapped or ip.sixtofour or (ipaddress.ip_address(int(ip) & 0xFFFFFFFF) if ip in NAT64 else None)
        if inner is not None:
            return inner.is_global
    return ip.is_global and not ip.is_multicast


def _read(r):
    """The reply body, at most MAX_REPLY bytes."""
    data = r.read(MAX_REPLY + 1, decode_content=True)
    r.release_conn()
    if len(data) > MAX_REPLY:
        raise RuntimeError("eLabFTW's reply was larger than expected")
    return data


def target(url):
    """(hostname, checked IP, base path) for an eLabFTW address, or Refused."""
    u = urllib.parse.urlsplit((url or "").strip())
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.port not in (None, 443):
        raise Refused("use the https address of your eLabFTW, e.g. https://elab.example.org")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise Refused(f"{u.hostname} does not resolve") from None
    if not addrs or not all(_public(a) for a in addrs):
        raise Refused("that address is not on the public internet")  # loopback, private, link-local, metadata IPs
    return u.hostname, sorted(addrs)[0], u.path.rstrip("/")


def _pool(host, ip):
    return urllib3.HTTPSConnectionPool(ip, 443, server_hostname=host, assert_hostname=host, cert_reqs="CERT_REQUIRED",
                                       ca_certs=certifi.where(), timeout=TIMEOUT, retries=False)


def check(url, key):
    """(host, ip, base) when the key looks like an API key and the address passes target(), else Refused."""
    if not re.fullmatch(r"[\x21-\x7e]{8,200}", key or ""):
        raise Refused("paste the API key from eLabFTW (Settings, API keys, with write access)")
    return target(url)


def push(eln, filename, title, url, key):
    """Import the .eln as one experiment (owner-only read and write). Returns {"id", "url"} of the new entry."""
    host, ip, base = check(url, key)
    pool = _pool(host, ip)
    head = {"Host": host, "Authorization": key, "User-Agent": "show-dont-code/1.0"}
    r = pool.request("POST", f"{base}/api/v2/import", headers=head, redirect=False, preload_content=False, fields={
        "file": (filename, eln, "application/zip"), "entity_type": "experiments",
        "canread_base": "10", "canwrite_base": "10"})
    body = _read(r)
    if r.status in (401, 403):
        raise Refused("eLabFTW refused the API key: it needs write access")
    if r.status != 201:
        raise RuntimeError(f"eLabFTW answered {r.status} to the import: {body[:200].decode('utf-8', 'replace')}")
    # eLabFTW's search misses a whole title with punctuation: search its last word (the run name), match the title exactly
    q = urllib.parse.urlencode({"q": title.rsplit(" ", 1)[-1], "limit": 20, "order": "id", "sort": "desc"})
    r = pool.request("GET", f"{base}/api/v2/experiments?{q}", headers=head, redirect=False, preload_content=False)
    body = _read(r)
    found = [e for e in (json.loads(body) if r.status == 200 else []) if isinstance(e, dict) and e.get("title") == title]
    if not found:
        raise RuntimeError("eLabFTW accepted the file but the new experiment wasn't found")
    eid = found[0]["id"]
    return {"id": eid, "url": f"https://{host}{base}/experiments.php?mode=view&id={eid}"}


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) == 4:
        f = Path(sys.argv[1])
        title = f"Show, Don't Code run {f.stem.removeprefix('show-dont-code-')}"
        print(push(f.read_bytes(), f.name, title, sys.argv[2], Path(sys.argv[3]).read_text().strip()))
        sys.exit()
    for bad in ("http://demo.elabftw.net", "https://127.0.0.1", "https://localhost", "https://10.0.0.5",
                "https://169.254.169.254", "https://[::1]", "https://user:pw@demo.elabftw.net",
                "https://demo.elabftw.net:8443", "ftp://demo.elabftw.net", "",
                f"https://{uuid.uuid4().hex}.invalid", "https://[64:ff9b::a9fe:a9fe]", "https://[::127.0.0.1]",
                "https://[::ffff:10.0.0.1]", "https://[2002:a00:1::]", "https://0.0.0.0", "https://[ff02::1]"):
        try:
            target(bad)
            raise AssertionError(f"{bad!r} must be refused")
        except Refused:
            pass
    assert target("https://demo.elabftw.net/")[0] == "demo.elabftw.net"
    assert _public("8.8.8.8") and _public("2606:4700:4700::1111") and _public("64:ff9b::808:808")  # NAT64 of 8.8.8.8
    for key in ("", "short", "has space in it 123", "x" * 201, "line\nbreak123"):
        try:
            push(b"", "a.eln", "t", "https://demo.elabftw.net", key)
            raise AssertionError(f"key {key!r} must be refused")
        except Refused:
            pass
    print("elab self-check OK (17 addresses and 5 keys refused before any request)")
