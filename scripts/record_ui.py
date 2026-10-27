"""Record the review UI playing a run (real app, real data) for the landing page and demo video.

Usage: python scripts/record_ui.py pcr_3 31 75 out/rec/pcr3_water_primer   (run, start s, end s, output stem)
Needs the local server (python app.py) on :8000.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

run, start, end, stem = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), Path(sys.argv[4])
stem.parent.mkdir(parents=True, exist_ok=True)
tmp = stem.parent / "_raw"
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=str(tmp),
                              record_video_size={"width": 1600, "height": 900})
    t_rec = time.time()  # recording starts with the context; everything before playback gets trimmed
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:8000/app?run={run}")
    page.wait_for_function("document.getElementById('video')?.readyState >= 1", timeout=60000)
    page.evaluate(f"""() => new Promise(r => {{ const v = document.getElementById('video');
        v.addEventListener('seeked', r, {{once: true}}); v.currentTime = {start}; }})""")
    time.sleep(1.5)
    t0 = time.time()
    page.evaluate("() => document.getElementById('video').play()")
    time.sleep(end - start)
    page.evaluate("() => document.getElementById('video').pause()")
    raw = page.video.path()
    ctx.close()
    browser.close()
lead = t0 - t_rec
subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{lead:.2f}", "-i", str(raw), "-c:v", "libx264", "-crf", "20",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", f"{stem}.mp4"], check=True)
shutil.rmtree(tmp, ignore_errors=True)
print(f"{stem}.mp4")
