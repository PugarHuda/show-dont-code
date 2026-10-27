"""Print the README evaluation table from cached runs, so every published number is reproducible.

Usage: python scripts/eval_table.py pcr_1 pcr_2 pcr_3 pcr_4 ...   (first argument = dev video, rest = test)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdc import evaluate, pipeline  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out"


def fmt(e):
    return f"{e['recall']:.2f} / {e['precision']:.2f} / {e['mean_tiou']:.2f}"


runs = sys.argv[1:]
print("| Video | Stage 1 only: recall / precision / tIoU | **Stage 1 + 2**: recall / precision / tIoU | volume accuracy |")
print("|---|---|---|---|")
test = []
for k, run in enumerate(runs):
    d = OUT / run
    gt = evaluate.load_gt(d / "gt.csv")
    s1 = evaluate.score(gt, json.loads((d / "steps_raw.json").read_text(encoding="utf-8")))
    s2_file = d / "steps_stage2.json"
    sheet = json.loads((d / "sheet.json").read_text(encoding="utf-8")) if (d / "sheet.json").exists() else {}
    # fully automatic output only: stage 2 + volumes read off the bench sheet, never the scientist's answers
    s2 = evaluate.score(gt, pipeline.apply_sheet(json.loads(s2_file.read_text(encoding="utf-8")), sheet)) \
        if s2_file.exists() else s1
    va = s2["volume_accuracy"]
    vol = f"{va:.2f} ({s2['volumes_checked']} stated)" if va is not None else "n/a"
    print(f"| {run} ({'dev' if k == 0 else 'test'}) | {fmt(s1)} | {fmt(s2)} | {vol} |")
    if k:
        test.append((s1, s2))
if test:
    mean = lambda xs, key: sum(x[key] for x in xs) / len(xs)  # noqa: E731
    s1s, s2s = [t[0] for t in test], [t[1] for t in test]
    print(f"| **test mean (n={len(test)})** | {mean(s1s, 'recall'):.2f} / {mean(s1s, 'precision'):.2f} | "
          f"**{mean(s2s, 'recall'):.2f} / {mean(s2s, 'precision'):.2f}** | |")
