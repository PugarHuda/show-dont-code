"""Review UI server: human video next to the generated robot protocol.  Run: python app.py"""
import json
import os
import re
import shutil
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

if os.getenv("VERCEL") == "1":  # opentrons writes its config under $HOME at import; only /tmp is writable
    os.environ.setdefault("OT_API_CONFIG_DIR", "/tmp/opentrons")
    os.environ["HOME"] = "/tmp"

from sdc import pipeline  # noqa: E402  (must follow the env setup above)

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Show, Don't Code")

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


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/runs")
def runs():
    return sorted(p.parent.name for p in pipeline.OUT.glob("*/result.json"))


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


@app.post("/api/runs/{name}/answers")
def answer(name: str, answers: dict[str, float] = Body(...)):
    d = _run_dir(name)
    old = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    if any(v <= 0 or v > 1000 for v in answers.values()):
        raise HTTPException(422, "volumes must be in (0, 1000] µL")
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    if any(not k.isdigit() or int(k) >= len(steps) for k in answers):
        raise HTTPException(422, "unknown step")
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
    global live_calls_left
    if live_calls_left <= 0:
        raise HTTPException(429, "live model calls for this demo are used up; try again later")
    live_calls_left -= 1
    frame = round(t * pipeline.SAM_FPS)
    fid = json.loads((d / "sam_file.json").read_text())
    track = {str(k): v for k, v in meta.track_point(fid, frame, x, y, (640, 360)).items()}
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    uses = meta.steps_using(d / "sam.mp4", track, steps, pipeline.SAM_FPS, frame)
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
