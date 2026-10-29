"""Stage 1 with Muse Wonder 1.0 instead of Muse Spark 1.3: same prompt, same video file, same hints, same scorer.

Usage: python scripts/wonder_bench.py pcr_1 pcr_2 pcr_3 pcr_4 pcr_5 miniprep_7
Caches steps_raw_wonder.json per run (one API call each); prints the comparison table for the README.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdc import evaluate, meta, pipeline  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out"
WONDER = "muse-wonder-1.0"


def fmt(e):
    return f"{e['recall']:.2f} / {e['precision']:.2f} / {e['mean_tiou']:.2f}"


rows = []
print(f"| Video | Muse Spark 1.3, stage 1: recall / precision / tIoU | Muse Wonder 1.0, stage 1 |")
print("|---|---|---|")
for run in sys.argv[1:]:
    d = OUT / run
    cache = d / "steps_raw_wonder.json"
    if not cache.exists():
        load = lambda f: json.loads((d / f).read_text(encoding="utf-8")) if (d / f).exists() else {}  # noqa: E731
        hint = pipeline.sheet_hint(load("sheet.json")) + pipeline.speech_hint(load("speech.json") or [])
        fid = meta.upload(d / "spark.mp4")  # the exact file Spark watched (10 fps, 640 px)
        cache.write_text(json.dumps(meta.extract_steps(fid, hint, model=WONDER, max_tokens=64000), indent=1), encoding="utf-8")
    gt = evaluate.load_gt(d / "gt.csv")
    spark = evaluate.score(gt, json.loads((d / "steps_raw.json").read_text(encoding="utf-8")))
    wonder = evaluate.score(gt, json.loads(cache.read_text(encoding="utf-8")))
    print(f"| {run} | {fmt(spark)} | {fmt(wonder)} |", flush=True)
    rows.append((run, spark, wonder))
test = [r for r in rows if r[0].startswith("pcr_") and r[0] != "pcr_1"]
if test:
    m = lambda i, k: sum(r[i][k] for r in test) / len(test)  # noqa: E731
    print(f"| **PCR test mean (n={len(test)})** | {m(1, 'recall'):.2f} / {m(1, 'precision'):.2f} | "
          f"{m(2, 'recall'):.2f} / {m(2, 'precision'):.2f} |")
