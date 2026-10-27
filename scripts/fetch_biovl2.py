"""Fetch selected files from the BioVL2 zip on Zenodo (CC BY 4.0) via HTTP range requests.

Usage: python scripts/fetch_biovl2.py [glob ...]   (default: all CSVs + pcr_1..4 videos)
"""
import fnmatch
import sys
from pathlib import Path

from remotezip import RemoteZip

URL = "https://zenodo.org/records/21495188/files/BioVL2.zip?download=1"
DEFAULT = ["BioVL2/protocols/*", "BioVL2/videos/pcr/pcr_[1-4].mp4"]

patterns = sys.argv[1:] or DEFAULT
out = Path(__file__).resolve().parent.parent / "data"
with RemoteZip(URL) as z:
    for info in z.infolist():
        if info.is_dir() or not any(fnmatch.fnmatch(info.filename, p) for p in patterns):
            continue
        dest = out / info.filename
        if dest.exists() and dest.stat().st_size == info.file_size:
            continue  # ponytail: size check only, no per-file hash in the zip listing
        print(f"{info.file_size / 1e6:8.1f}MB {info.filename}", flush=True)
        z.extract(info, out)
