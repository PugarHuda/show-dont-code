"""video -> steps (Muse Spark) -> tip evidence (SAM 3.1) -> Opentrons protocol -> simulator.

Every stage is cached as JSON in out/<run>/ so re-runs and UI edits never re-bill the API.

Usage:
  python -m sdc.pipeline data/BioVL2/videos/pcr/pcr_1.mp4          # real run (needs MODEL_API_KEY)
  python -m sdc.pipeline --from-gt data/BioVL2/protocols/pcr/pcr_1.csv data/BioVL2/videos/pcr/pcr_1.mp4
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from . import evaluate, protocol

OUT = Path(__file__).resolve().parent.parent / "out"
SAM_FPS = 2  # ponytail: 2 fps is enough to see a tip enter a tube; raise if steps are shorter than ~1 s


on_progress = None  # optional callback after each progress write (the hosted worker commits its volume here)


def progress(run_dir, stage, state):
    """Append a stage event to <run>/progress.json so a UI can show the run as it happens."""
    f = Path(run_dir) / "progress.json"
    events = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    events.append({"stage": stage, "state": state, "t": round(time.time(), 1)})
    f.write_text(json.dumps(events), encoding="utf-8")
    print(f"  {stage} {'...' if state == 'running' else f'({state})'}", flush=True)
    if on_progress:
        on_progress()


def _cached(path, fn, label=None):
    path = Path(path)
    if path.exists():
        if label:
            progress(path.parent, label, "cached")
        return json.loads(path.read_text(encoding="utf-8"))
    if label:
        progress(path.parent, label, "running")
    value = fn()
    path.write_text(json.dumps(value, indent=1), encoding="utf-8")
    if label:
        progress(path.parent, label, "done")
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


def sheet_hint(sheet):
    if not sheet.get("found") or not sheet["reagents"]:
        return ""
    names = "; ".join(r["name"] for r in sheet["reagents"])
    # a hint, not a constraint: a misread sheet must not force wrong names onto correctly seen transfers
    return (f"\n\nA quick OCR of the protocol sheet on the bench suggests these reagents (may be incomplete or "
            f"misread, and NOT in the order they are added): {names}. Prefer these names when they fit what "
            f"you see; water is often omitted from such lists. Each distinct source tube is a distinct reagent.")


def speech_hint(speech):
    """Muse Voice Transcribe turns, timestamped. Spark 1.3 hears audio poorly, so we hand it the words."""
    if not speech:
        return ""
    lines = "\n".join(f"[{t['start']:.0f}-{t['end']:.0f}s] {t['text']}" for t in speech)
    return f"\n\nSpeech heard in the video (any language; volumes or reagent names said aloud are evidence):\n{lines}"


def apply_sheet(steps, sheet):
    """Fill volumes the sheet states for a reagent the model named. Sheet volumes beat model guesses."""
    vols = {tuple(evaluate._norm(r["name"])): r["volume_ul"] for r in sheet.get("reagents", []) if r["volume_ul"]}
    for s in steps:
        v = vols.get(tuple(evaluate._norm(s.get("reagent"))))
        if v and s["action"] == "transfer":
            s["volume_ul"], s["volume_evidence"] = v, "protocol_sheet"
    return steps


def simulate_with(steps, volumes_ul):
    """Tool for the reviewer agent: try a per-reagent volume plan in the real simulator, report per-tube totals."""
    trial, missing = [dict(s) for s in steps], set()
    for s in trial:
        if s["action"] == "thermocycle" and not s.get("cycles"):
            s["cycles"] = 30  # the tool checks volumes; the real cycle count is the scientist's answer
        if s["action"] in ("transfer", "mix") and not s.get("volume_ul"):
            v = volumes_ul.get(s.get("reagent") or "")
            if isinstance(v, (int, float)) and 0 < v <= 1000:
                s["volume_ul"] = float(v)
            else:
                missing.add(str(s.get("reagent") or s["description"]))
    if missing:
        return {"ran": False, "missing_or_invalid_volume_for": sorted(missing)}
    totals = {}
    for s in trial:
        if s["action"] == "transfer":
            dest = s.get("destination") or "sample tube"
            totals[dest] = round(totals.get(dest, 0) + s["volume_ul"], 2)
    try:
        src, _ = protocol.generate(trial)
    except protocol.NeedsInput as e:  # e.g. a planned volume below the pipette minimum
        return {"ran": False, "rejected": [q["reason"] or q["field"] for q in e.questions]}
    ok, log = protocol.simulate(src)
    return {"ran": ok, "error": None if ok else log[-1], "robot_actions": len(log), "total_ul_per_tube": totals}


def _second_robot(steps):
    """Translate to PyLabRobot (Hamilton STARlet) and run it in PyLabRobot's simulator: an independent second check."""
    from . import export

    try:
        import pylabrobot  # noqa: F401  (optional dependency: present on the hosted backend and locally)
    except ImportError:
        return None, {"note": "PyLabRobot is not installed here"}
    try:
        return export.run_pylabrobot(export.pylabrobot_script(steps))
    except (ValueError, subprocess.TimeoutExpired) as e:
        return False, {"tail": [str(e)[:200]]}


def tip_evidence(tracks, steps, fps=SAM_FPS):
    """Attach SAM evidence to each step: how many sampled frames show the pipette tip inside the step window."""
    for s in steps:
        frames = [f for f in tracks if s["start"] <= int(f) / fps <= s["end"]]
        s["tip_frames"] = len(frames)
        s["tip_seen"] = bool(frames) or s["action"] not in ("transfer", "mix")
    return steps


def build(run_dir, steps, answers=None, review=True):
    """Apply scientist answers, generate the protocol, simulate. Returns the result dict (also saved).

    review=False keeps the previous reviewer output instead of re-running the agent (it takes minutes).
    """
    run_dir = Path(run_dir)
    prev_file = run_dir / "result.json"
    prev = json.loads(prev_file.read_text(encoding="utf-8")) if prev_file.exists() else {}
    steps = [dict(s) for s in steps]
    for key, v in (answers or {}).items():  # "7" -> volume of step 7, "20:cycles" -> cycle count of step 20
        i, _, field = key.partition(":")
        if field == "cycles":
            steps[int(i)]["cycles"] = int(v)
        else:
            steps[int(i)]["volume_ul"] = float(v)
            steps[int(i)]["volume_evidence"] = "confirmed"
    result = {"steps": steps, "questions": [], "protocol": None, "layout": {}, "sim_ok": False, "sim_log": []}
    try:
        result["protocol"], result["layout"] = protocol.generate(steps)
        result["sim_ok"], result["sim_log"] = protocol.simulate(result["protocol"])
        if result["sim_ok"]:  # the Opentrons App's own analysis: the file it would accept, and what to load where
            result["analysis_ok"], result["analysis"] = protocol.analyze(result["protocol"], run_dir / "analysis.json")
            result["plr_ok"], result["plr"] = _second_robot(steps)
    except protocol.NeedsInput as e:
        result["questions"], result["layout"] = e.questions, protocol.layout(steps)
    sim = ("not run yet: waiting for scientist answers" if result["questions"]
           else "passed" if result["sim_ok"] else f"failed: {result['sim_log'][-1:]}")
    sheet_file = run_dir / "sheet.json"
    sheet = json.loads(sheet_file.read_text(encoding="utf-8")) if sheet_file.exists() else {"found": False}
    keep = ("review", "suggested_volumes", "agent_log", "review_saw_video", "review_error")
    if not review:
        result.update({k: prev[k] for k in keep if k in prev})
    else:
        try:
            from . import agent

            review_video = run_dir / "review.mp4"
            frames = run_dir / "sam_frames"
            out = agent.review(steps, sheet, sim, review_video if review_video.exists() else None,
                               simulate=(lambda vols: simulate_with(steps, vols)) if result["questions"] else None,
                               frames_dir=frames if frames.exists() else None, fps=SAM_FPS)
            result["review"], result["suggested_volumes"], result["agent_log"] = (
                out["issues"], out["suggested_volumes"], out["tool_log"])
            result["review_saw_video"] = review_video.exists()
        except Exception as e:  # reviewer is advisory; a provider outage must not block protocol generation
            result["review"], result["review_error"] = [], f"{type(e).__name__}: {e}"[:300]
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
        length = meta.duration(video)  # uploads can be short: only sample stills that exist
        stills = meta.frames(video, [t for t in (2, 10, 20, 35, 50) if t < length - 0.5] or [0], run_dir / "stills")
        sheet = _cached(run_dir / "sheet.json", lambda: meta.read_sheet(stills), "Muse Spark reads the bench protocol sheet")
        spark_video = meta.shrink(video, run_dir / "spark.mp4", fps=10)
        fid = _cached(run_dir / "spark_file.json", lambda: meta.upload(spark_video), "upload video to Meta Model API")
        try:  # speech is an optional hint; a transcription failure must not stop the run
            speech = _cached(run_dir / "speech.json", lambda: meta.transcribe(
                video, run_dir / "audio.wav", keywords=[r["name"] for r in sheet.get("reagents", [])] or None), "Muse Voice Transcribe listens")
        except Exception as e:
            print(f"speech transcription skipped: {e}")
            speech = []
        steps = _cached(run_dir / "steps_raw.json",
                        lambda: meta.extract_steps(fid, sheet_hint(sheet) + speech_hint(speech)), "Muse Spark watches the video: steps + timestamps")
        steps = _cached(run_dir / "steps_stage2.json",
                        lambda: meta.identify_sources(video, steps, sheet, run_dir / "stills_src"), "Muse Spark stage 2: which tube did each transfer draw from")
        steps = apply_sheet(steps, sheet)
        sam_video = meta.shrink(video, run_dir / "sam.mp4", fps=SAM_FPS)
        sam_fid = _cached(run_dir / "sam_file.json", lambda: meta.upload(sam_video), "upload video for SAM 3.1")
        meta.sam_frames(sam_video, run_dir / "sam_frames")  # click-to-track reads these, no decoder needed
        tracks = _cached(run_dir / "tracks_tip.json", lambda: meta.track(sam_fid, "pipette tip"), "SAM 3.1 tracks the pipette tip")
        steps = tip_evidence(tracks, steps)
        from . import agent

        if length <= agent.MAX_VIDEO_S:  # longer videos: reviewer works from the steps alone
            meta.shrink(video, run_dir / "review.mp4", fps=2, width=480)
    answers_file = run_dir / "answers.json"
    answers = json.loads(answers_file.read_text()) if answers_file.exists() else {}
    progress(run_dir, "Opentrons simulator + Muse Spark Open reviewer agent", "running")
    result = build(run_dir, steps, answers)
    progress(run_dir, "finished", "done")
    return result


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
