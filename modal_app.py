"""Hosted backend on Modal: the same FastAPI app as app.py, plus video upload and a long-running worker.

Vercel serves the landing page and the app HTML; the browser talks to this API (CORS) for everything else.
All runs, demo and uploaded, live on one Modal Volume, so answers and named tubes persist across visitors.

  modal deploy modal_app.py
  modal volume put sdc-runs out/pcr_3 /pcr_3        # seed demo runs
"""
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import modal

RUNS = "/runs"
MAX_BYTES = 400 * 1024 * 1024
MAX_SECONDS = 300           # the reviewer agent watches up to 300 s, and cost scales with length
DAILY_UPLOADS = int(os.getenv("DAILY_UPLOADS", "15"))  # ponytail: global daily cap; per-user quotas if abused

app = modal.App("show-dont-code")
vol = modal.Volume.from_name("sdc-runs", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install_from_requirements("requirements.txt")
    .env({"OT_API_CONFIG_DIR": "/tmp/opentrons", "PYTHONIOENCODING": "utf-8", "UPLOADS": "1"})
    .add_local_python_source("sdc")
    .add_local_file("app.py", "/root/app.py")
    .add_local_dir("web", "/root/web")
)
secret = modal.Secret.from_name("show-dont-code")


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret], timeout=3600, cpu=2.0, memory=4096)
def process(run_id: str):
    """Run the whole pipeline on an uploaded video. Progress events are committed as they happen."""
    from sdc import pipeline

    pipeline.OUT = Path(RUNS)
    pipeline.on_progress = vol.commit
    d = Path(RUNS) / run_id
    try:
        pipeline.run(d / "input.mp4", name=run_id)
    except Exception as e:  # the UI shows this stage as failed; details stay in the Modal logs
        print(f"run {run_id} failed: {type(e).__name__}: {e}")
        pipeline.progress(d, "failed", "error")
    vol.commit()


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret], timeout=900, scaledown_window=1200)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def web():
    from fastapi import File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware

    from sdc import pipeline

    pipeline.OUT = Path(RUNS)
    import app as webapp  # the same routes as local and Vercel

    api = webapp.app
    api.add_middleware(CORSMiddleware, allow_origins=["https://show-dont-code.vercel.app", "http://127.0.0.1:8000"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
    last_reload = [0.0]

    @api.middleware("http")
    async def fresh_volume(request, call_next):
        # the worker writes from another container; pick up its commits at most every 2 s
        if request.method == "GET" and time.time() - last_reload[0] > 2:
            try:
                vol.reload()
            except RuntimeError:  # reload refuses while files are open; the next request will retry
                pass
            last_reload[0] = time.time()
        response = await call_next(request)
        if request.method == "POST":  # answers, tube names: persist them and make them visible to other containers
            vol.commit()
        return response

    @api.post("/api/upload")
    async def upload(file: UploadFile = File(...), code: str = Form("")):
        want = os.getenv("UPLOAD_CODE", "")
        if want and not secrets.compare_digest(code, want):
            raise HTTPException(403, "this demo needs an access code to upload")
        today = time.strftime("%Y-%m-%d")
        counter = Path(RUNS) / f".uploads-{today}"
        used = int(counter.read_text()) if counter.exists() else 0
        if used >= DAILY_UPLOADS:
            raise HTTPException(429, "today's upload quota for this demo is used up; try a sample run instead")
        run_id = f"u-{time.strftime('%m%d')}-{secrets.token_hex(3)}"
        d = Path(RUNS) / run_id
        d.mkdir(parents=True)
        size = 0
        with open(d / "input.mp4", "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    out.close()
                    subprocess.run(["rm", "-rf", str(d)])
                    raise HTTPException(413, f"videos up to {MAX_BYTES >> 20} MB")
                out.write(chunk)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(d / "input.mp4")], capture_output=True, text=True)
        try:
            seconds = float(probe.stdout.strip())
        except ValueError:
            subprocess.run(["rm", "-rf", str(d)])
            raise HTTPException(422, "that file isn't a video we can read; upload an MP4 or MOV")
        if seconds > MAX_SECONDS:
            subprocess.run(["rm", "-rf", str(d)])
            raise HTTPException(422, f"videos up to {MAX_SECONDS // 60} minutes; this one is {seconds / 60:.1f}")
        counter.write_text(str(used + 1))
        webapp.pipeline.progress(d, "upload received", "done")
        vol.commit()
        process.spawn(run_id)
        return {"run": run_id, "seconds": round(seconds, 1)}

    @api.get("/api/runs/{name}/progress")
    def run_progress(name: str):
        if not re.fullmatch(r"[\w.-]+", name):
            raise HTTPException(404, "unknown run")
        f = Path(RUNS) / name / "progress.json"
        if not f.exists():
            raise HTTPException(404, "unknown run")
        return {"events": json.loads(f.read_text()), "ready": (Path(RUNS) / name / "result.json").exists()}

    return api
