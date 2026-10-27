"""Review UI server: human video next to the generated robot protocol.  Run: python app.py"""
import json
import re
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from sdc import pipeline

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Show, Don't Code")


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
    return r


@app.get("/api/runs/{name}/video")
def video(name: str):
    return FileResponse(_run_dir(name) / "video.mp4", media_type="video/mp4")


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
    pipeline.build(d, steps, {**old, **answers})
    return result(name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
