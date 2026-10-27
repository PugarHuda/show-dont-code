"""video -> steps (Muse Spark) -> tip evidence (SAM 3.1) -> Opentrons protocol -> simulator.

Every stage is cached as JSON in out/<run>/ so re-runs and UI edits never re-bill the API.

Usage:
  python -m sdc.pipeline data/BioVL2/videos/pcr/pcr_1.mp4          # real run (needs MODEL_API_KEY)
  python -m sdc.pipeline --from-gt data/BioVL2/protocols/pcr/pcr_1.csv data/BioVL2/videos/pcr/pcr_1.mp4
"""
import argparse
import json
import re
from pathlib import Path

from . import evaluate, protocol

OUT = Path(__file__).resolve().parent.parent / "out"
SAM_FPS = 2  # ponytail: 2 fps is enough to see a tip enter a tube; raise if steps are shorter than ~1 s


def _cached(path, fn):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = fn()
    path.write_text(json.dumps(value, indent=1), encoding="utf-8")
    return value


def steps_from_gt(csv_path):
    """Turn BioVL2 sentences into steps. 'Add X' becomes a transfer of X; everything else stays manual/other."""
    steps = []
    for g in evaluate.load_gt(csv_path):
        s = g["sentence"]
        m = re.match(r"(?i)add (?:(\d+(?:\.\d+)?)\s*[μu]l (?:of )?)?(.+)", s)
        step = {"start": g["start"], "end": g["end"], "description": s, "reagent": None, "source": None,
                "destination": None, "volume_ul": None, "volume_evidence": "none", "confidence": 1.0}
        if m:
            step.update(action="transfer", reagent=m.group(2).strip(), destination="sample tube",
                        volume_ul=float(m.group(1)) if m.group(1) else None,
                        volume_evidence="label" if m.group(1) else "none")
        elif re.search(r"(?i)dna engine|thermal cycler|pcr machine", s):
            step["action"] = "thermocycle"
        elif re.search(r"(?i)centrifuge", s):
            step["action"] = "centrifuge"
        else:
            step["action"] = "other"
        steps.append(step)
    return steps


def tip_evidence(tracks, steps, fps=SAM_FPS):
    """Attach SAM evidence to each step: how many sampled frames show the pipette tip inside the step window."""
    for s in steps:
        frames = [f for f in tracks if s["start"] <= int(f) / fps <= s["end"]]
        s["tip_frames"] = len(frames)
        s["tip_seen"] = bool(frames) or s["action"] not in ("transfer", "mix")
    return steps


def build(run_dir, steps, answers=None):
    """Apply scientist answers, generate the protocol, simulate. Returns the result dict (also saved)."""
    run_dir = Path(run_dir)
    steps = [dict(s) for s in steps]
    for i, v in (answers or {}).items():
        steps[int(i)]["volume_ul"] = float(v)
        steps[int(i)]["volume_evidence"] = "confirmed"
    result = {"steps": steps, "questions": [], "protocol": None, "layout": {}, "sim_ok": False, "sim_log": []}
    try:
        result["protocol"], result["layout"] = protocol.generate(steps)
        result["sim_ok"], result["sim_log"] = protocol.simulate(result["protocol"])
    except protocol.NeedsInput as e:
        result["questions"] = e.questions
    gt = run_dir / "gt.csv"
    if gt.exists():
        result["eval"] = evaluate.score(evaluate.load_gt(gt), steps)
    (run_dir / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    (run_dir / "answers.json").write_text(json.dumps(answers or {}), encoding="utf-8")
    return result


def run(video, name=None, gt_csv=None, from_gt=False):
    video = Path(video)
    name = name or (video.stem + ("-gt" if from_gt else ""))
    run_dir = OUT / name
    run_dir.mkdir(parents=True, exist_ok=True)
    gt_csv = gt_csv or next(video.parents[2].glob(f"protocols/*/{video.stem}.csv"), None)
    if gt_csv:
        (run_dir / "gt.csv").write_bytes(Path(gt_csv).read_bytes())

    from . import meta  # local import: ffmpeg shrink lives there; API key only needed below

    meta.shrink(video, run_dir / "video.mp4", fps=30)
    if from_gt:
        steps = steps_from_gt(gt_csv)
    else:
        spark_video = meta.shrink(video, run_dir / "spark.mp4", fps=10)
        fid = _cached(run_dir / "spark_file.json", lambda: meta.upload(spark_video))
        steps = _cached(run_dir / "steps_raw.json", lambda: meta.extract_steps(fid))
        sam_video = meta.shrink(video, run_dir / "sam.mp4", fps=SAM_FPS)
        sam_fid = _cached(run_dir / "sam_file.json", lambda: meta.upload(sam_video))
        tracks = _cached(run_dir / "tracks_tip.json", lambda: meta.track(sam_fid, "pipette tip"))
        steps = tip_evidence(tracks, steps)
    answers_file = run_dir / "answers.json"
    answers = json.loads(answers_file.read_text()) if answers_file.exists() else {}
    return build(run_dir, steps, answers)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--from-gt", metavar="CSV", help="replay ground-truth steps instead of calling the models")
    ap.add_argument("--name")
    a = ap.parse_args()
    r = run(a.video, a.name, gt_csv=a.from_gt, from_gt=bool(a.from_gt))
    print(json.dumps({k: r[k] for k in ("questions", "sim_ok", "layout")}, indent=1))
    if "eval" in r:
        print({k: v for k, v in r["eval"].items() if k != "matches"})
