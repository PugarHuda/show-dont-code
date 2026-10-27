"""Review UI server: human video next to the generated robot protocol.  Run: python app.py"""
import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

if os.getenv("VERCEL") == "1":  # opentrons writes its config under $HOME at import; only /tmp is writable
    os.environ.setdefault("OT_API_CONFIG_DIR", "/tmp/opentrons")
    os.environ["HOME"] = "/tmp"

from sdc import pipeline  # noqa: E402  (must follow the env setup above)

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Show, Don't Code")
log = logging.getLogger("show-dont-code")

ON_VERCEL = os.getenv("VERCEL") == "1"
if ON_VERCEL:  # the bundle is read-only: work on a /tmp copy of the exported demo runs (scripts/export_demo.py)
    if not Path("/tmp/out").exists():
        shutil.copytree(ROOT / "demo", "/tmp/out")
    pipeline.OUT = Path("/tmp/out")
# re-running the reviewer agent takes minutes; on the hosted demo, edits keep the last review
REVIEW_ON_EDIT = os.getenv("REVIEW_ON_EDIT", "0" if ON_VERCEL else "1") == "1"
# ponytail: per-instance counter so a public link can't drain API credits; use a shared store if abused
live_calls_left = int(os.getenv("LIVE_CALL_LIMIT", "1000000"))


def _run_dir(name):
    if not re.fullmatch(r"[\w.-]+", name) or not (pipeline.OUT / name / "result.json").exists():
        raise HTTPException(404, "unknown run")
    return pipeline.OUT / name


@app.get("/media/{name}")
def media(name: str):  # local preview only; on Vercel public/media is served by the CDN before this runs
    path = ROOT / "public" / "media" / name
    if not re.fullmatch(r"[\w.-]+", name) or not path.exists():
        raise HTTPException(404, "no such file")
    return FileResponse(path)


@app.get("/")
def landing():
    page = ROOT / "web" / "landing.html"
    return FileResponse(page if page.exists() else ROOT / "web" / "index.html")


@app.get("/app")
def index():
    page = ROOT / "web" / "index.html"
    api = os.getenv("API_BASE", "")  # hosted: the API lives on Modal; empty = same origin (local, Modal itself)
    if not re.fullmatch(r"(https://[\w.-]+)?", api):
        raise HTTPException(500, "API_BASE must be an https origin")
    uploads = "true" if api or os.getenv("UPLOADS") == "1" else "false"  # only the Modal backend can take uploads
    html = page.read_text(encoding="utf-8").replace(
        "</head>", f'<script>window.API_BASE = "{api}"; window.UPLOADS = {uploads};</script>\n</head>', 1)
    return HTMLResponse(html)


@app.get("/api/runs")
def runs():
    # uploads (u-*) are private: reachable only through the link their uploader got
    return sorted(p.parent.name for p in pipeline.OUT.glob("*/result.json") if not p.parent.name.startswith("u-"))


@app.get("/api/runs/{name}")
def result(name: str):
    d = _run_dir(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    tracks = d / "tracks_tip.json"
    r["tracks"] = json.loads(tracks.read_text()) if tracks.exists() else {}
    r["sam_fps"] = pipeline.SAM_FPS
    r["tubes"] = _tubes(d)
    r["can_track"] = (d / "sam_file.json").exists()
    return r


@app.get("/api/runs/{name}/video")
def video(name: str):
    d = _run_dir(name)
    if ON_VERCEL:  # public/ is served by the CDN and is not inside the function bundle
        return RedirectResponse(f"/runs/{name}/video.mp4")
    return FileResponse(d / "video.mp4", media_type="video/mp4")


@app.get("/api/runs/{name}/protocol.py")
def protocol_file(name: str):
    r = json.loads((_run_dir(name) / "result.json").read_text(encoding="utf-8"))
    if not r["protocol"]:
        raise HTTPException(409, "protocol not generated yet: answer the open questions first")
    return PlainTextResponse(r["protocol"], headers={"Content-Disposition": f'attachment; filename="{name}.py"'})


def _confirmed_steps(name):
    r = json.loads((_run_dir(name) / "result.json").read_text(encoding="utf-8"))
    if not r.get("protocol"):
        raise HTTPException(409, "no protocol yet: answer the open questions first")
    return r["steps"]


@app.get("/api/runs/{name}/methods.md")
def methods_file(name: str):
    from sdc import export

    return PlainTextResponse(export.methods_markdown(_confirmed_steps(name), f"Show, Don't Code run {name}"),
                             media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{name}-methods.md"'})


@app.get("/api/runs/{name}/autoprotocol.json")
def autoprotocol_file(name: str):
    from sdc import export

    return JSONResponse(export.autoprotocol(_confirmed_steps(name)),
                        headers={"Content-Disposition": f'attachment; filename="{name}-autoprotocol.json"'})


@app.get("/api/runs/{name}/pylabrobot.py")
def pylabrobot_file(name: str):
    from sdc import export

    return PlainTextResponse(export.pylabrobot_script(_confirmed_steps(name)),
                             headers={"Content-Disposition": f'attachment; filename="{name}-pylabrobot.py"'})


@app.get("/api/runs/{name}/analysis.json")
def analysis_file(name: str):
    f = _run_dir(name) / "analysis.json"
    if not f.exists():
        raise HTTPException(409, "no analysis yet: the protocol has to pass the simulator first")
    return FileResponse(f, media_type="application/json", filename=f"{name}-opentrons-analysis.json")


def _sha256(data: bytes):
    import hashlib

    return hashlib.sha256(data).hexdigest()


@app.get("/api/runs/{name}/receipt.json")
def receipt(name: str):
    """Reproducibility receipt: hashes that tie the protocol to the footage it came from, the models that read it,
    and the human answers that completed it. Anyone with the files can recompute every hash."""
    from sdc import agent, meta

    d = _run_dir(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    if not r.get("protocol"):
        raise HTTPException(409, "no protocol yet: answer the open questions first")
    files = {f: _sha256((d / f).read_bytes()) for f in ("video.mp4", "steps_raw.json", "steps_stage2.json",
                                                         "tracks_tip.json", "analysis.json") if (d / f).exists()}
    answers = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    return {
        "run": name,
        "protocol_sha256": _sha256(r["protocol"].encode("utf-8")),
        "files_sha256": files,
        "simulator": {"passed": r["sim_ok"], "robot_actions": len(r["sim_log"])},
        "opentrons_analysis": {"passed": r.get("analysis_ok"), "commands": (r.get("analysis") or {}).get("commands")},
        # a ground-truth replay never ran the models on its video, so don't claim it did
        "models": {"steps_and_sheet": meta.SPARK, "tracking": "sam-3.1", "speech": "muse-voice-transcribe-1.0",
                   "reviewer": agent.MODEL} if (d / "steps_raw.json").exists()
        else {"note": "steps replayed from BioVL2 human annotations; no model read this video"},
        "human_input": {"answers": answers, "named_tubes": [t["name"] for t in _tubes(d) if t.get("name")]},
        "reviewer_findings": len(r.get("review") or []),
        "verify": "sha256 of each file must match; the protocol hash covers protocol.py byte for byte (UTF-8)",
    }


@app.post("/api/runs/{name}/voice")
async def voice(name: str, request: Request):
    """Spoken answers: Muse Voice Transcribe hears them, Muse Spark maps them to the open questions.
    Returns pre-fill values only; the scientist still presses Confirm."""
    import subprocess
    import tempfile

    from sdc import meta

    d = _run_dir(name)
    global live_calls_left
    if not os.getenv("MODEL_API_KEY"):
        raise HTTPException(503, "live model calls are not configured on this deployment")
    if live_calls_left <= 0:
        raise HTTPException(429, "live model calls for this demo are used up; try again later")
    body = b""
    async for chunk in request.stream():  # a short voice note; refuse anything long before reading it all
        body += chunk
        if len(body) > 5 * 1024 * 1024:
            raise HTTPException(413, "keep voice answers under a minute")
    if len(body) < 1000:
        raise HTTPException(422, "no audio arrived; check the microphone permission")
    live_calls_left -= 1
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    groups = {}
    for q in r["questions"]:
        s = r["steps"][q["step"]]
        if q["field"] == "cycles":
            groups[f"{q['step']}:cycles"] = "number of PCR cycles for the thermocycler"
        else:
            label = f"µL per transfer of {s.get('reagent') or s['description']}" + (f" (from {s['source']})" if s.get("source") else "")
            groups.setdefault(label, []).append(str(q["step"]))
    questions = [{"key": k, "asks": v} if isinstance(v, str) else {"key": ",".join(v), "asks": k} for k, v in groups.items()]
    if not questions:
        raise HTTPException(409, "there are no open questions to answer")
    # only containers a browser recorder produces, identified by magic bytes, and decoded with that format forced
    if body[:4] == b"\x1a\x45\xdf\xa3":
        fmt = "matroska"  # WebM (Chrome, Firefox)
    elif body[:4] == b"OggS":
        fmt = "ogg"
    elif body[4:8] == b"ftyp":
        fmt = "mov"  # MP4 audio (Safari)
    elif body[:4] == b"RIFF" and body[8:12] == b"WAVE":
        fmt = "wav"
    else:
        raise HTTPException(415, "send WebM, Ogg, MP4 or WAV audio")
    keywords = sorted({s.get("reagent") for s in r["steps"] if s.get("reagent")})[:20]
    with tempfile.TemporaryDirectory() as tmp:
        clip, wav = Path(tmp) / "voice.in", Path(tmp) / "voice.wav"
        clip.write_bytes(body)
        try:
            await asyncio.to_thread(subprocess.run, [
                "ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file", "-f", fmt, "-i", str(clip), "-t", "60",
                "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(wav)], check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            raise HTTPException(422, "that recording could not be read; try again")
        try:  # transcribe() skips its own ffmpeg step because the wav already exists
            turns = await asyncio.to_thread(meta.transcribe, clip, wav, keywords)
        except Exception:
            log.exception("voice transcription failed for run %s", name)
            raise HTTPException(502, "the speech model is unavailable; type the answers instead")
    transcript = " ".join(t["text"] for t in turns).strip()
    if not transcript:
        return {"transcript": "", "answers": []}
    try:
        answers = await asyncio.to_thread(meta.voice_answers, transcript, questions)
    except Exception:
        log.exception("voice mapping failed for run %s", name)
        raise HTTPException(502, "the model could not read the answers; type them instead")
    ok = [a for a in answers if (":cycles" in a["key"] and 1 <= a["value"] <= 99) or (":cycles" not in a["key"] and 0 < a["value"] <= 1000)]
    return {"transcript": transcript, "answers": ok}


@app.post("/api/runs/{name}/review/{k}/apply")
def apply_fix(name: str, k: int):
    """Accept the reviewer agent's proposed change for finding k. The agent proposes; only this click changes steps."""
    d = _run_dir(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    issues = r.get("review") or []
    if not 0 <= k < len(issues) or (issues[k].get("fix") or {}).get("kind", "none") == "none":
        raise HTTPException(404, "no fix to apply for that finding")
    if issues[k].get("applied"):
        raise HTTPException(409, "already applied")
    fix, steps = issues[k]["fix"], r["steps"]
    for i in fix["steps"]:  # indices were validated against these steps when the review was stored
        if fix["kind"] == "set_reagent":
            steps[i].update(reagent=fix["value"], reagent_by="reviewer agent, accepted")
        elif fix["kind"] == "set_volume":
            steps[i].update(volume_ul=float(fix["value"]), volume_evidence="reviewer agent, accepted")
        elif fix["kind"] == "skip_steps":
            steps[i].update(action="skipped", skipped_from=steps[i]["action"])
    answers = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    out = pipeline.build(d, steps, answers, review=False)  # keeps the review, so mark this finding done in it
    out["review"][k]["applied"] = True
    (d / "result.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return result(name)


@app.post("/api/runs/{name}/answers")
def answer(name: str, answers: dict[str, float] = Body(...)):
    d = _run_dir(name)
    old = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    for k, v in answers.items():
        m = re.fullmatch(r"(\d+)(:cycles)?", k)
        if not m or int(m[1]) >= len(steps):
            raise HTTPException(422, f"unknown step {k!r}")
        if m[2] and not (1 <= v <= 99 and v == int(v)):
            raise HTTPException(422, "cycles must be a whole number from 1 to 99")
        if not m[2] and not 0 < v <= 1000:
            raise HTTPException(422, "volumes must be in (0, 1000] µL")
    pipeline.build(d, steps, {**old, **answers}, review=REVIEW_ON_EDIT)
    return result(name)


def _tubes(d):
    f = d / "tubes.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


@app.post("/api/runs/{name}/tubes")
def identify_tube(name: str, t: float = Body(...), x: float = Body(...), y: float = Body(...)):
    """Scientist clicked a tube at time t, (x, y) in the 640x360 SAM frame: track it, then ground its uses."""
    from sdc import meta

    d = _run_dir(name)
    if not (d / "sam_file.json").exists():
        raise HTTPException(409, "this run has no SAM video (ground-truth replays can't track)")
    if not (0 <= x < 640 and 0 <= y < 360 and t >= 0):
        raise HTTPException(422, "click outside the frame")
    if not os.getenv("MODEL_API_KEY"):
        raise HTTPException(503, "live model calls are not configured on this deployment")
    global live_calls_left
    if live_calls_left <= 0:
        raise HTTPException(429, "live model calls for this demo are used up; try again later")
    live_calls_left -= 1
    frame = round(t * pipeline.SAM_FPS)
    fid = json.loads((d / "sam_file.json").read_text())
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    try:
        track = {str(k): v for k, v in meta.track_point(fid, frame, x, y, (640, 360)).items()}
        uses = meta.steps_using(d / "sam_frames", track, steps, pipeline.SAM_FPS, frame)
    except Exception as e:  # details go to the server log only; provider messages can carry ids and internals
        log.exception("tube tracking failed for run %s", name)
        busy = "RateLimit" in type(e).__name__ or "429" in str(e)
        raise HTTPException(502, "the model provider is busy; try again in a minute" if busy
                            else "model call failed; please try again later")
    tubes = _tubes(d)
    tubes.append({"id": len(tubes), "t": t, "x": x, "y": y, "name": None, "track": track, **uses})
    (d / "tubes.json").write_text(json.dumps(tubes), encoding="utf-8")
    return result(name)


@app.post("/api/runs/{name}/tubes/{tube_id}/name")
def name_tube(name: str, tube_id: int, label: str = Body(..., embed=True)):
    """Apply the scientist's name for a tube to every step SAM + Muse Spark tied to it."""
    d = _run_dir(name)
    label = " ".join(label.split())[:60]
    tubes = _tubes(d)
    if not label or not 0 <= tube_id < len(tubes):
        raise HTTPException(422, "bad tube or empty name")
    tube = tubes[tube_id]
    tube["name"] = label
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    for i in tube["source_steps"]:
        steps[i].update(reagent=label, source=label)
    for i in tube["destination_steps"]:
        steps[i]["destination"] = label
    (d / "tubes.json").write_text(json.dumps(tubes), encoding="utf-8")
    answers = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    pipeline.build(d, steps, answers, review=REVIEW_ON_EDIT)
    return result(name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
