"""Copy finished runs into the shape the hosted demo needs (not committed; BioVL2 media stays out of git).

  demo/<run>/            JSON results + sam_frames/ (read by the function, copied to /tmp at start)
  public/runs/<run>/     video.mp4 served by the CDN (Vercel responses are capped at 4.5 MB)

Usage: python scripts/export_demo.py pcr_1 pcr_2 ...
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNCTION_FILES = ["result.json", "source.json", "answers.json", "tubes.json", "tracks_tip.json", "sheet.json", "gt.csv",
                  "sam_file.json", "profile.json", "analysis.json",
                  # model outputs: the receipt and RO-Crate cite them, and steps_raw.json is what proves a model ran
                  "steps_raw.json", "steps_stage2.json", "speech.json"]  # videos stay out: the bundle must fit Vercel's 250 MB standard size

for run in sys.argv[1:]:
    src = ROOT / "out" / run
    if not (src / "result.json").exists():
        sys.exit(f"{run}: no result.json in {src}")
    dst = ROOT / "demo" / run
    dst.mkdir(parents=True, exist_ok=True)
    for f in FUNCTION_FILES:
        if (src / f).exists():
            shutil.copy2(src / f, dst / f)
    if (src / "sam_frames").exists():
        shutil.copytree(src / "sam_frames", dst / "sam_frames", dirs_exist_ok=True)
    if (src / "video.mp4").exists():  # runs read from a published protocol have no video
        pub = ROOT / "public" / "runs" / run
        pub.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "video.mp4", pub / "video.mp4")
    print(f"{run}: exported")
