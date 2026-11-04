"""Stage 1 on open weights: Muse Spark Open (Baseten) watches the video instead of Muse Spark 1.3 through the Meta
Model API. Same prompt, same hints, same strict step schema, same scorer; a lab could run this tier on its own
hardware, so the video never leaves the building.

The video is the one the reviewer agent already sends to Baseten (review.mp4: 2 fps, 480 px; Baseten's Muse Spark
Open takes at most 300 s and samples up to 96 frames), so Spark saw more frames than this model does.

Usage: python scripts/open_bench.py pcr_1 pcr_2 pcr_3 pcr_4 pcr_5 miniprep_7
Caches steps_raw_open.json per run (one call each); prints the comparison table for the README.
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdc import agent, evaluate, meta, pipeline  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out"


def fmt(e):
    return f"{e['recall']:.2f} / {e['precision']:.2f} / {e['mean_tiou']:.2f}"


def video(d):
    v = d / "review.mp4"
    if not v.exists():  # the same encoding as the pipeline's review.mp4, kept under Baseten's 300 s
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(d / "spark.mp4"), "-t", str(agent.MAX_VIDEO_S - 1),
                        "-vf", "fps=2,scale=480:-2", "-an", str(v)], check=True)
    return v


def extract(d, hint):
    b64 = base64.b64encode(video(d).read_bytes()).decode()
    c = agent.client(os.getenv("BASETEN_HACKATHON_KEY"))
    for attempt in range(1, 4):  # it sometimes stops after its reasoning with no answer; those tries are logged
        t = time.time()
        r = c.chat.completions.create(
            model=agent.HACKATHON, max_tokens=16000,
            messages=[{"role": "user", "content": [{"type": "text", "text": meta.PROMPT + hint},
                                                   {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}]}],
            response_format={"type": "json_schema", "json_schema": {"name": "steps", "schema": meta.STEP_SCHEMA, "strict": True}})
        if r.choices[0].message.content:
            return json.loads(r.choices[0].message.content)["steps"], round(time.time() - t, 1), attempt
        print(f"  ({d.name}: try {attempt} ended with no answer, finish_reason={r.choices[0].finish_reason})", file=sys.stderr)
    raise RuntimeError(f"{d.name}: no answer in 3 tries")


rows = []
print("| Video | Muse Spark 1.3 (API), stage 1: recall / precision / tIoU | Muse Spark Open (open weights, Baseten), stage 1 |")
print("|---|---|---|")
for run in sys.argv[1:]:
    d = OUT / run
    cache = d / "steps_raw_open.json"
    if not cache.exists():
        load = lambda f: json.loads((d / f).read_text(encoding="utf-8")) if (d / f).exists() else {}  # noqa: E731
        hint = pipeline.sheet_hint(load("sheet.json")) + pipeline.speech_hint(load("speech.json") or [])
        steps, secs, tries = extract(d, hint)
        cache.write_text(json.dumps(steps, indent=1), encoding="utf-8")
        print(f"  ({run}: {len(steps)} steps in {secs} s, answer on try {tries})", file=sys.stderr)
    gt = evaluate.load_gt(d / "gt.csv")
    spark = evaluate.score(gt, json.loads((d / "steps_raw.json").read_text(encoding="utf-8")))
    open_ = evaluate.score(gt, json.loads(cache.read_text(encoding="utf-8")))
    print(f"| {run} | {fmt(spark)} | {fmt(open_)} |", flush=True)
    rows.append((run, spark, open_))
test = [r for r in rows if r[0].startswith("pcr_") and r[0] != "pcr_1"]
if test:
    m = lambda i, k: sum(r[i][k] for r in test) / len(test)  # noqa: E731
    print(f"| **PCR test mean (n={len(test)})** | {m(1, 'recall'):.2f} / {m(1, 'precision'):.2f} | "
          f"{m(2, 'recall'):.2f} / {m(2, 'precision'):.2f} |")
