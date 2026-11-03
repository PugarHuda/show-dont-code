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
PROCESS_TIMEOUT = 3600
MAX_SECONDS = 300           # the reviewer agent watches up to 300 s, and cost scales with length
DAILY_UPLOADS = int(os.getenv("DAILY_UPLOADS", "15"))  # ponytail: global daily cap; per-user quotas if abused
DAILY_IMPORTS = int(os.getenv("DAILY_IMPORTS", "25"))  # protocols.io imports: text only, much cheaper than a video

app = modal.App("show-dont-code")
vol = modal.Volume.from_name("sdc-runs", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install_from_requirements("requirements.txt")
    .pip_install("PyLabRobot==0.2.2")  # second-robot check; kept out of requirements.txt so Vercel stays small
    .pip_install("mcp==2.3.0")  # /mcp for other agents; own step: it lifts jsonschema past opentrons' pin (see app.py)
    .pip_install("pypdfium2==4.30.0")  # PDF pages -> images for a photographed/scanned protocol import
    .env({"OT_API_CONFIG_DIR": "/tmp/opentrons", "PYTHONIOENCODING": "utf-8", "UPLOADS": "1",
          "REVIEW_ON_EDIT": "0", "LIVE_CALL_LIMIT": "120", "PUBLIC_URL": "https://show-dont-code.vercel.app"})
    .add_local_python_source("sdc")
    .add_local_file("app.py", "/root/app.py")
    .add_local_dir("web", "/root/web")
)
secret = modal.Secret.from_name("show-dont-code")
baseten = modal.Secret.from_name("show-dont-code-baseten")  # hackathon Baseten key (muse-spark-open-1026 only)
backup = modal.Secret.from_name("show-dont-code-fireworks")  # Fireworks key for the reviewer's backup host
protocolsio = modal.Secret.from_name("show-dont-code-protocolsio")  # protocols.io client token: public search


# Opentrons' own robot-server (the software on a Flex, v10.1.0) in Flex simulation mode. Its own image: it pins
# fastapi 0.100 and pydantic 2.11.7, and it isn't on PyPI, so it comes from Opentrons' repo at the released tag.
OT_TAG = "v10.1.0"
flex_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .pip_install("opentrons==10.1.0", "anyio==4.9.0", "fastapi==0.100.0", "python-dotenv==1.0.1",
                 "python-multipart==0.0.28", "pydantic==2.11.7", "pydantic-settings==2.4.0", "uvicorn==0.27.0.post1",
                 "wsproto==1.2.0", "sqlalchemy==1.4.51", "paho-mqtt==1.6.1", "aiohttp>=3.12.14,<4", "Pyro5>=5.16,<6",
                 "zipstream-ng==1.9.3", "pyserial==3.5", "numpy>=1.24.5,<2", "python-can==4.2.2", "setuptools>=69.1.1,<82")
    .run_commands(f"git clone -q --depth 1 --branch {OT_TAG} --filter=blob:none --sparse "
                  "https://github.com/Opentrons/opentrons /ot",
                  "cd /ot && git sparse-checkout set --no-cone /robot-server/robot_server/ /robot-server/simulators/ "
                  "/server-utils/server_utils/ /hardware/opentrons_hardware/")
    .env({"PYTHONPATH": "/ot/robot-server:/ot/server-utils:/ot/hardware", "OT_API_CONFIG_DIR": "/tmp/opentrons",
          # what robot-server's own `make dev-flex` sets (robot-server/dev-flex.env)
          "ENABLE_VIRTUAL_SMOOTHIE": "true", "OT_ROBOT_SERVER_persistence_directory": "automatically_make_temporary",
          "OT_ROBOT_SERVER_simulator_configuration_file_path": "/ot/robot-server/simulators/test-flex.json",
          "OT_API_FF_enableOT3HardwareController": "true", "DEV_ROBOT_NAME": "show-dont-code-flex",
          "OT_API_FF_enableHardwareSubprocess": "false", "OT_API_FF_enableProtocolSubprocess": "false"})
    .add_local_file("scripts/send_to_flex.py", "/root/send_to_flex.py")
)


@app.function(image=flex_image, timeout=1800, cpu=2.0, memory=4096, max_containers=2)
def flex_run(source: str, params: dict | None = None) -> dict:
    """Upload a protocol to a fresh robot-server in Flex simulation, exactly as the Opentrons App would, let the robot
    analyse it, then press play and run it to the end (hand-off pauses resumed). Returns what the robot reported."""
    import urllib.request

    import send_to_flex

    t0 = time.time()
    # robot-server quietly becomes an OT-2 if the Flex hardware stack can't import; fail loudly instead
    probe = subprocess.run(["python", "-c", "from opentrons_hardware.drivers.can_bus import CanDriver"],
                           capture_output=True, text=True)
    if probe.returncode:
        raise RuntimeError("the Flex hardware stack doesn't import: " + probe.stderr[-1200:])
    srv = subprocess.Popen(["python", "-m", "uvicorn", "robot_server.app:app", "--host", "127.0.0.1", "--port", "31950",
                            "--ws", "wsproto"], cwd="/ot/robot-server", stdout=open("/tmp/rs.log", "wb"),
                           stderr=subprocess.STDOUT)
    tail = lambda: open("/tmp/rs.log", errors="replace").read()[-1500:]
    try:
        for _ in range(180):
            try:
                with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:31950/health",
                                                                   headers=send_to_flex.HEAD), timeout=2):
                    break
            except OSError:
                if srv.poll() is not None:
                    raise RuntimeError("robot-server exited: " + tail())
                time.sleep(1)
        log = []
        out = send_to_flex.send("127.0.0.1", source, params or {}, start=True, log=log.append, resume_pauses=True, deck=True)
        run = send_to_flex._call("127.0.0.1", "GET", f"/runs/{out['run_id']}")["data"]
        cmds = send_to_flex._call("127.0.0.1", "GET", f"/runs/{out['run_id']}/commands?pageLength=1")["meta"]
        out.update(robot_software=OT_TAG, log=log, commands=cmds["totalLength"], started_at=run.get("startedAt"),
                   completed_at=run.get("completedAt"), seconds=round(time.time() - t0, 1))
        return out
    except SystemExit as e:  # send_to_flex reports a robot refusal this way
        return {"status": "rejected", "error": str(e)[:600], "robot_software": OT_TAG}
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}\n--- robot-server log ---\n{tail()}") from None
    finally:
        srv.terminate()


@app.local_entrypoint()
def try_flex(run: str = "pcr_1-gt", reactions: int = 0):
    """modal run modal_app.py::try_flex --run pcr_1-gt --reactions 2"""
    import urllib.request

    src = urllib.request.urlopen(f"https://pugarhuda--show-dont-code-web.us-east.modal.run/api/runs/{run}/protocol.py",
                                 timeout=120).read().decode()
    print(json.dumps(flex_run.remote(src, {"reactions": reactions} if reactions else {}), indent=1))


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret, baseten, backup, protocolsio], timeout=PROCESS_TIMEOUT, cpu=2.0,
              memory=4096)
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


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret, baseten, backup, protocolsio], timeout=PROCESS_TIMEOUT)
def process_text(run_id: str, link: str):
    """A published protocol (protocols.io) through the same pipeline as a video, minus tracking."""
    from sdc import pipeline

    pipeline.OUT = Path(RUNS)
    pipeline.on_progress = vol.commit
    d = Path(RUNS) / run_id
    try:
        pipeline.run_text(link, run_id)
    except Exception as e:  # the UI shows this stage as failed; details stay in the Modal logs
        print(f"import {run_id} failed: {type(e).__name__}: {e}")
        pipeline.progress(d, "failed", "error")
    vol.commit()


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret, baseten, backup, protocolsio], timeout=PROCESS_TIMEOUT)
def process_document(run_id: str):
    """Photos or PDF pages of a protocol (already re-encoded to <run>/doc/page_N.jpg) through the text pipeline."""
    from sdc import pipeline

    pipeline.OUT = Path(RUNS)
    pipeline.on_progress = vol.commit
    d = Path(RUNS) / run_id
    try:
        pipeline.run_document(run_id)
    except Exception as e:  # the UI shows this stage as failed; details stay in the Modal logs
        print(f"document {run_id} failed: {type(e).__name__}: {e}")
        pipeline.progress(d, "failed", "error")
    vol.commit()


@app.function(image=image, volumes={RUNS: vol}, secrets=[secret, baseten, backup, protocolsio], timeout=900, scaledown_window=1200, max_containers=1,
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
        # answers, tube names, timestamp proofs, robot-server runs, protocols.io matches: persist them and make them visible to other containers
        path = request.url.path
        if (request.method == "POST" or path.endswith(("/receipt.json", "/receipt.ots", "/ro-crate.zip", "/related"))
                or "/flex-run/" in path):
            await asyncio.to_thread(vol.commit)
        return response

    quota_lock = asyncio.Lock()  # one web container (max_containers=1), so this lock makes the quota atomic

    def take_quota(delta, kind="uploads", cap=None):
        counter = Path(RUNS) / f".{kind}-{time.strftime('%Y-%m-%d')}"
        used = int(counter.read_text()) if counter.exists() else 0
        if delta > 0 and used >= (cap or DAILY_UPLOADS):
            return False
        counter.write_text(str(max(0, used + delta)))
        vol.commit()
        return True

    @api.post("/api/import")
    async def import_protocol(request: Request):
        """Start a run from a published protocols.io protocol (link, URI or DOI) instead of a video. No access code:
        it costs two Muse Spark text calls and a review, so it has its own daily cap."""
        from sdc import pio

        import urllib.error

        try:
            link = str((await request.json()).get("link", ""))[:300]
            pio.ref(link)  # only protocols.io addresses; anything else is refused before any request goes out
            await asyncio.to_thread(pio.fetch, link)  # exists and is public, or a clear answer now rather than a failed run
        except urllib.error.HTTPError:
            raise HTTPException(422, "protocols.io has no public protocol at that address")
        except (ValueError, AttributeError, json.JSONDecodeError) as e:
            raise HTTPException(422, str(e) if isinstance(e, ValueError) else "send {\"link\": \"...\"}")
        async with quota_lock:
            if not await asyncio.to_thread(take_quota, +1, "imports", DAILY_IMPORTS):
                raise HTTPException(429, "today's protocol imports for this demo are used up; open the sample instead")
        run_id = "p-" + secrets.token_urlsafe(12)
        d = Path(RUNS) / run_id
        d.mkdir(parents=True)
        webapp.pipeline.progress(d, "protocol link received", "done")
        await asyncio.to_thread(vol.commit)
        await process_text.spawn.aio(run_id, link)
        return {"run": run_id}

    @api.post("/api/import/document")
    async def import_document(request: Request):
        """Start a run from a photo (JPEG, PNG, WebP) or a PDF of a protocol: a printed sheet, a notebook page, a
        paper's methods. Raw body, 15 MB, at most 6 pages; counted against the daily imports cap like protocols.io."""
        cap = 15 * 1024 * 1024
        if int(request.headers.get("content-length") or 0) > cap:
            raise HTTPException(413, "send one photo or PDF of up to 15 MB")
        chunks, size = [], 0
        async for chunk in request.stream():  # stop at the cap rather than buffer whatever is sent
            size += len(chunk)
            if size > cap:
                raise HTTPException(413, "send one photo or PDF of up to 15 MB")
            chunks.append(chunk)
        body = b"".join(chunks)
        if not body:
            raise HTTPException(413, "send one photo or PDF of up to 15 MB")
        kind = ("pdf" if body[:5] == b"%PDF-" else "jpeg" if body[:3] == bytes.fromhex("ffd8ff")
                else "png" if body[:8] == bytes.fromhex("89504e470d0a1a0a")
                else "webp" if body[:4] == b"RIFF" and body[8:12] == b"WEBP" else None)
        if not kind:  # by content, not by the name or type the browser claims
            raise HTTPException(422, "send a JPEG, PNG or WebP photo, or a PDF")

        try:
            jpegs = await asyncio.to_thread(webapp.pipeline.document_pages, body, kind)
        except Exception:  # a broken or hostile file: refuse it, nothing is kept
            raise HTTPException(422, "that file couldn't be read as a photo or PDF")
        async with quota_lock:
            if not await asyncio.to_thread(take_quota, +1, "imports", DAILY_IMPORTS):
                raise HTTPException(429, "today's protocol imports for this demo are used up; open the sample instead")
        run_id = "p-" + secrets.token_urlsafe(12)  # private like every import: the link is the only way in
        d = Path(RUNS) / run_id
        (d / "doc").mkdir(parents=True)
        for i, j in enumerate(jpegs, 1):
            (d / "doc" / f"page_{i}.jpg").write_bytes(j)
        webapp.pipeline.progress(d, f"{len(jpegs)} page{'s' if len(jpegs) > 1 else ''} received", "done")
        await asyncio.to_thread(vol.commit)
        await process_document.spawn.aio(run_id)
        return {"run": run_id, "pages": len(jpegs)}

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
            # only real video containers, by their magic bytes, and ffmpeg is told which demuxer to use: a text
            # playlist (HLS, concat) named .mp4 would otherwise make it open other files or URLs
            with open(d / "raw", "rb") as f:
                head = f.read(12)
            fmt = ("mov" if head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip") else
                   "matroska" if head[:4] == bytes.fromhex("1a45dfa3") else
                   "avi" if head[:4] == b"RIFF" and head[8:12] == b"AVI " else None)
            if not fmt:
                raise HTTPException(422, "that file isn't a video we can read; upload an MP4, MOV, WebM or AVI")
            src = ["-protocol_whitelist", "file", "-f", fmt, "-i", str(d / "raw")]
            # cut to the limit for real: container metadata can claim any duration
            # off the event loop, with hard timeouts: this is the only web container
            cut = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-y", *src, "-t",
                                          str(MAX_SECONDS), "-c", "copy", "-movflags", "+faststart", str(d / "input.mp4")],
                                          capture_output=True, timeout=120)
            if cut.returncode != 0:  # a codec MP4 can't hold as is (VP8 from a browser recording): re-encode instead
                cut = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-y", *src, "-t",
                                              str(MAX_SECONDS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                                              str(d / "input.mp4")], capture_output=True, timeout=600)
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
        events, ready = json.loads(f.read_text()), (Path(RUNS) / name / "result.json").exists()
        # a worker killed at its time limit (or out of memory) writes nothing: past the limit, say it stopped
        if not ready and events and time.time() - events[0]["t"] > PROCESS_TIMEOUT + 120 and \
                not any(e["state"] == "error" for e in events):
            events.append({"stage": "analysis stopped before finishing", "state": "error", "t": round(time.time(), 1)})
        return {"events": events, "ready": ready}

    # app.py mounts the MCP server at "/" as its last route; routes added here come after it and would never match
    # (upload and progress answered 404 until Oct 29), so the catch-all mount moves back to the end
    from starlette.routing import Mount

    catch_all = [r for r in api.router.routes if isinstance(r, Mount) and r.path == ""]
    api.router.routes[:] = [r for r in api.router.routes if r not in catch_all] + catch_all
    return api
