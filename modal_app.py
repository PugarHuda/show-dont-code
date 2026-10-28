"""Hosted backend on Modal: the same FastAPI app as app.py, plus video upload and a long-running worker.

Vercel serves the landing page and the app HTML; the browser talks to this API (CORS) for everything else.
All runs live on one Modal Volume: samples, uploads, and the private copies an edit to a sample creates (app.py).

  modal deploy modal_app.py
  modal volume put sdc-runs out/pcr_3 /pcr_3        # seed demo runs
"""
import asyncio
import json
import os
import re
import secrets
import shutil
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
    .pip_install("PyLabRobot==0.2.2")  # second-robot check; kept out of requirements.txt so Vercel stays small
    .pip_install("mcp==2.3.0")  # /mcp for other agents; own step: it lifts jsonschema past opentrons' pin (see app.py)
    .env({"OT_API_CONFIG_DIR": "/tmp/opentrons", "PYTHONIOENCODING": "utf-8", "UPLOADS": "1",
          "REVIEW_ON_EDIT": "0", "LIVE_CALL_LIMIT": "30"})
    .add_local_python_source("sdc")
    .add_local_file("app.py", "/root/app.py")
    .add_local_dir("web", "/root/web")
)
secret = modal.Secret.from_name("show-dont-code")
backup = modal.Secret.from_name("show-dont-code-fireworks")  # Fireworks key for the reviewer's backup host


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret, backup], timeout=3600, cpu=2.0, memory=4096)
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


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret, backup], timeout=900, scaledown_window=1200, max_containers=1,
              min_containers=1)  # always warm for judges: no cold start on the first visit
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def web():
    from fastapi import HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware

    from sdc import pipeline

    pipeline.OUT = Path(RUNS)
    import app as webapp  # the same routes as local and Vercel

    api = webapp.app
    api.add_middleware(CORSMiddleware, allow_origins=["https://show-dont-code.vercel.app", "http://127.0.0.1:8000"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Upload-Code"])
    last_reload, inflight, idle = [0.0], [0], asyncio.Event()
    idle.set()

    @api.middleware("http")
    async def fresh_volume(request, call_next):
        # the worker writes from another container; pick up its commits at most every 2 s. A reload swaps the mount,
        # so a request reading a run at that moment can 404: reload only when no other request is in flight, and make
        # requests that arrive meanwhile wait for it (one event loop, so the check and the clear can't interleave)
        await idle.wait()
        inflight[0] += 1
        try:
            if request.method == "GET" and inflight[0] == 1 and time.time() - last_reload[0] > 2:
                idle.clear()
                try:
                    await asyncio.to_thread(vol.reload)
                except RuntimeError:  # reload refuses while files are open (a video still streaming); retried later
                    pass
                finally:
                    last_reload[0] = time.time()
                    idle.set()
            response = await call_next(request)
        finally:
            inflight[0] -= 1
        if request.method == "POST":  # answers, tube names: persist them and make them visible to other containers
            await asyncio.to_thread(vol.commit)
        return response

    quota_lock = asyncio.Lock()  # one web container (max_containers=1), so this lock makes the quota atomic

    def take_quota(delta):
        counter = Path(RUNS) / f".uploads-{time.strftime('%Y-%m-%d')}"
        used = int(counter.read_text()) if counter.exists() else 0
        if delta > 0 and used >= DAILY_UPLOADS:
            return False
        counter.write_text(str(max(0, used + delta)))
        vol.commit()
        return True

    @api.post("/api/upload")
    async def upload(request: Request):
        """Raw video body (not multipart), so size is enforced while streaming, before anything is spooled."""
        want = os.getenv("UPLOAD_CODE", "")
        if not want:  # fail closed: no code configured means uploads are off
            raise HTTPException(403, "uploads are switched off on this deployment")
        if not secrets.compare_digest(request.headers.get("x-upload-code", ""), want):
            raise HTTPException(403, "that access code isn't right")
        if int(request.headers.get("content-length") or 0) > MAX_BYTES:
            raise HTTPException(413, f"videos up to {MAX_BYTES >> 20} MB")
        async with quota_lock:  # reserve the slot first; refunded below if the upload is rejected
            if not await asyncio.to_thread(take_quota, +1):
                raise HTTPException(429, "today's upload quota for this demo is used up; try a sample run instead")
        run_id = "u-" + secrets.token_urlsafe(12)  # unguessable: the link is the only way to see an upload
        d = Path(RUNS) / run_id
        d.mkdir(parents=True)
        try:
            size = 0
            with open(d / "raw", "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, f"videos up to {MAX_BYTES >> 20} MB")
                    out.write(chunk)
            # cut to the limit for real: container metadata can claim any duration
            # off the event loop, with hard timeouts: this is the only web container
            cut = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-y", "-i", str(d / "raw"), "-t",
                                          str(MAX_SECONDS), "-c", "copy", "-movflags", "+faststart", str(d / "input.mp4")],
                                          capture_output=True, timeout=120)
            probe = await asyncio.to_thread(subprocess.run, ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                            "-of", "csv=p=0", str(d / "input.mp4")], capture_output=True, text=True, timeout=30)
            try:
                seconds = float(probe.stdout.strip()) if cut.returncode == 0 else 0.0
            except ValueError:
                seconds = 0.0
            if seconds < 5:
                raise HTTPException(422, "that file isn't a video we can read; upload an MP4 or MOV of at least 5 seconds")
        except (HTTPException, subprocess.TimeoutExpired) as e:
            shutil.rmtree(d, ignore_errors=True)
            async with quota_lock:
                await asyncio.to_thread(take_quota, -1)
            if isinstance(e, subprocess.TimeoutExpired):
                raise HTTPException(422, "that video took too long to read; try a standard MP4")
            raise
        (d / "raw").unlink()
        webapp.pipeline.progress(d, "upload received", "done")
        await asyncio.to_thread(vol.commit)
        await process.spawn.aio(run_id)
        return {"run": run_id, "seconds": round(seconds, 1), "trimmed": seconds >= MAX_SECONDS - 1}

    @api.get("/api/runs/{name}/progress")
    def run_progress(name: str):
        if not re.fullmatch(r"[\w.-]+", name):
            raise HTTPException(404, "unknown run")
        f = Path(RUNS) / name / "progress.json"
        if not f.exists():
            raise HTTPException(404, "unknown run")
        return {"events": json.loads(f.read_text()), "ready": (Path(RUNS) / name / "result.json").exists()}

    return api
