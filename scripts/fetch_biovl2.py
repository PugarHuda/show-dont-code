"""Fetch selected files from the BioVL2 zip on Zenodo (CC BY 4.0) via HTTP range requests.

Downloads each member's compressed bytes in small resumable chunks (the connection drops on long streams),
then inflates locally.

Usage: python scripts/fetch_biovl2.py [glob ...]   (default: all CSVs + pcr_1..4 videos)
"""
import fnmatch
import struct
import sys
import time
import zlib
from pathlib import Path

import requests
from remotezip import RemoteZip

URL = "https://zenodo.org/records/21495188/files/BioVL2.zip?download=1"
DEFAULT = ["BioVL2/protocols/*", "BioVL2/videos/pcr/pcr_[1-4].mp4"]
CHUNK = 8 << 20


def get_range(start, end):
    """Bytes [start, end] inclusive, retrying dropped connections."""
    for attempt in range(8):
        try:
            r = requests.get(URL, headers={"Range": f"bytes={start}-{end}"}, timeout=60)
            if r.status_code == 206 and len(r.content) == end - start + 1:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"range {start}-{end} failed after retries")


def fetch(info, dest):
    hdr = get_range(info.header_offset, info.header_offset + 29)
    name_len, extra_len = struct.unpack("<HH", hdr[26:30])
    data_start = info.header_offset + 30 + name_len + extra_len
    part = dest.with_suffix(dest.suffix + ".part")  # raw compressed bytes, resumable
    have = part.stat().st_size if part.exists() else 0
    with open(part, "ab") as f:
        for off in range(have, info.compress_size, CHUNK):
            f.write(get_range(data_start + off, data_start + min(off + CHUNK, info.compress_size) - 1))
            print(f"\r  {min(off + CHUNK, info.compress_size) / info.compress_size:6.1%}", end="", flush=True)
    print()
    inflate = zlib.decompressobj(-15) if info.compress_type == 8 else None
    crc = 0
    with open(part, "rb") as src, open(dest, "wb") as out:
        while block := src.read(CHUNK):
            data = inflate.decompress(block) if inflate else block
            crc = zlib.crc32(data, crc)
            out.write(data)
        if inflate:
            out.write(tail := inflate.flush())
            crc = zlib.crc32(tail, crc)
    if crc != info.CRC or dest.stat().st_size != info.file_size:
        dest.unlink()
        raise RuntimeError(f"{info.filename}: CRC/size mismatch, delete {part} and retry")
    part.unlink()


patterns = sys.argv[1:] or DEFAULT
out = Path(__file__).resolve().parent.parent / "data"
with RemoteZip(URL) as z:
    members = [i for i in z.infolist() if not i.is_dir() and any(fnmatch.fnmatch(i.filename, p) for p in patterns)]
for info in members:
    dest = out / info.filename
    if dest.exists() and dest.stat().st_size == info.file_size:
        continue
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    print(f"{info.file_size / 1e6:8.1f}MB {info.filename}", flush=True)
    fetch(info, dest)
