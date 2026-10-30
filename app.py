"""Review UI server: human video next to the generated robot protocol.  Run: python app.py"""
import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

try:  # the MCP SDK needs jsonschema>=4.20 while opentrons 10.1 pins <4.18 (it runs fine on 4.26: the codegen self-check
    # analyses and simulates with it), so modal_app.py installs it in its own step; Vercel only serves the pages
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:
    MCPServer = None

if os.getenv("VERCEL") == "1":  # opentrons writes its config under $HOME at import; only /tmp is writable
    os.environ.setdefault("OT_API_CONFIG_DIR", "/tmp/opentrons")
    os.environ["HOME"] = "/tmp"

from sdc import pipeline, protocol  # noqa: E402  (must follow the env setup above)

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
# a public link must not drain API credits: a daily budget of live model calls (voice, click-to-track), stored next
# to the runs so it survives restarts (on Modal that is the shared Volume) and resets at 00:00 UTC
LIVE_CALL_LIMIT = int(os.getenv("LIVE_CALL_LIMIT", "1000000"))
_calls_lock = threading.Lock()


def live_calls_left():
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        used = json.loads((pipeline.OUT / ".live_calls.json").read_text())
    except (OSError, ValueError):
        used = {}
    return LIVE_CALL_LIMIT - (used.get("used", 0) if used.get("day") == today else 0), today


def spend_live_call():
    with _calls_lock:
        left, today = live_calls_left()
        if left <= 0:
            raise HTTPException(429, "today's live model calls for this demo are used up; they reset at 00:00 UTC")
        (pipeline.OUT / ".live_calls.json").write_text(json.dumps({"day": today, "used": LIVE_CALL_LIMIT - left + 1}))


def _run_dir(name):
    if not re.fullmatch(r"[\w.-]+", name) or not (pipeline.OUT / name / "result.json").exists():
        raise HTTPException(404, "unknown run")
    return pipeline.OUT / name


PRIVATE = ("u-", "c-")  # uploads and edit copies: unlisted, reachable only through their unguessable link
_edit_locks = defaultdict(threading.Lock)  # ponytail: one web container (max_containers=1); a volume lock if that grows


COPY_DAYS, MAX_COPIES = 7, int(os.getenv("MAX_COPIES", "1000"))  # ~430 KB each: at most ~430 MB of copies on disk


def _make_room_for_copy():
    """Edit copies are unauthenticated, so they are bounded without ever refusing an edit: copies untouched for a week
    go, and past MAX_COPIES the least recently edited go first. A copy being edited (its lock held) is never deleted."""
    def idle(c):  # every edit rewrites result.json
        try:
            return time.time() - (c / "result.json").stat().st_mtime
        except OSError:  # half-made (its maker holds the lock) or already gone
            return 0.0
    copies = sorted(pipeline.OUT.glob("c-*"), key=idle, reverse=True)
    excess = len(copies) - MAX_COPIES + 1
    for i, c in enumerate(copies):
        if i < excess or idle(c) > COPY_DAYS * 86400:
            lock = _edit_locks[c.name]
            if lock.acquire(blocking=False):
                try:
                    shutil.rmtree(c, ignore_errors=True)
                finally:
                    lock.release()


@contextmanager
def _editable(name):
    """The run an edit may write to. Sample runs are shared by every visitor, so an edit never lands on one: it goes to a
    private copy (c-*, as unguessable as an upload) whose name the response carries as `run`; a rejected edit leaves
    no copy behind. Writes to one run are serialized, since each is a read-modify-write of result.json."""
    d = _run_dir(name)
    copy = not name.startswith(PRIVATE)
    if copy:
        _make_room_for_copy()
        d = pipeline.OUT / ("c-" + secrets.token_urlsafe(12))
    with _edit_locks[d.name]:  # held from before the copy exists, so eviction can't take it half-made
        try:
            if copy:
                shutil.copytree(pipeline.OUT / name, d, ignore=shutil.ignore_patterns(*pipeline.SHARED))
                (d / "copy_of").write_text(name)
            elif not (d / "result.json").exists():  # evicted while this edit waited for the lock
                raise HTTPException(404, "this edited copy expired; open the sample again to make a new one")
            yield d
        except BaseException:
            if copy:
                shutil.rmtree(d, ignore_errors=True)
            raise


@app.get("/media/{name}")
def media(name: str):  # local preview only; on Vercel public/media is served by the CDN before this runs
    path = ROOT / "public" / "media" / name
    if not re.fullmatch(r"[\w.-]+", name) or not path.exists():
        raise HTTPException(404, "no such file")
    return FileResponse(path)


def _page(path, head=""):
    """An HTML page under a strict CSP: only scripts carrying this response's nonce run, so injected markup can't."""
    api = os.getenv("API_BASE", "")  # hosted: the API lives on Modal; empty = same origin (local, Modal itself)
    if not re.fullmatch(r"(https://[\w.-]+)?", api):
        raise HTTPException(500, "API_BASE must be an https origin")
    nonce = secrets.token_urlsafe(16)
    html = path.read_text(encoding="utf-8").replace("</head>", head + "</head>", 1).replace("<script>", f'<script nonce="{nonce}">')
    # Pyodide (CPython in WebAssembly, pinned release) re-runs the PyLabRobot simulations in the visitor's browser
    pyo = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/"
    jsqr = "https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js"  # reads tube labels from the camera (pinned + SRI)
    zplr = "https://cdn.jsdelivr.net/npm/zpl-renderer-js@4.0.0/dist/"  # draws the Zebra labels in the browser (zebrash)
    csp = (f"default-src 'self'; script-src 'nonce-{nonce}' 'wasm-unsafe-eval' {pyo} {jsqr} {zplr}index.external.esm.js; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           f"font-src https://fonts.gstatic.com; img-src 'self' data: blob: {api}; media-src 'self' blob: {api}; "
           f"connect-src 'self' {api} {pyo} {zplr}zebrash.wasm https://pypi.org https://files.pythonhosted.org; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return HTMLResponse(html, headers={"Content-Security-Policy": " ".join(csp.split())})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    api = os.getenv("API_BASE", "")
    if api and request.url.path.startswith(("/api/", "/mcp")):
        # the front end (Vercel) only holds a read-only demo bundle: no footage to hash, nowhere to keep a timestamp
        # proof. Its /api answered with receipts that left the video out and re-stamped forever; send callers to the
        # backend instead (307 keeps the method and body)
        from fastapi.responses import RedirectResponse

        q = f"?{request.url.query}" if request.url.query else ""
        resp = RedirectResponse(api + request.url.path + q, 307)
    else:
        resp = await call_next(request)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
                         "Permissions-Policy": "camera=(self), geolocation=(), microphone=(self)"})  # voice answers, recording
    return resp


@app.get("/")
def landing():
    page = ROOT / "web" / "landing.html"
    return _page(page if page.exists() else ROOT / "web" / "index.html")


@app.get("/app")
def index():
    api = os.getenv("API_BASE", "")
    uploads = "true" if api or os.getenv("UPLOADS") == "1" else "false"  # only the Modal backend can take uploads
    return _page(ROOT / "web" / "index.html", f'<script>window.API_BASE = "{api}"; window.UPLOADS = {uploads};</script>\n')


@app.get("/api/runs")
def runs():
    # uploads (u-*) are private: reachable only through the link their uploader got
    return sorted(p.parent.name for p in pipeline.OUT.glob("*/result.json") if not p.parent.name.startswith(PRIVATE))


@app.get("/api/runs/{name}")
def result(name: str):
    d = _run_dir(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    r["run"] = name  # after an edit to a sample run: the private copy it went to
    tracks = d / "tracks_tip.json"
    r["tracks"] = json.loads(tracks.read_text()) if tracks.exists() else {}
    r["sam_fps"] = pipeline.SAM_FPS
    if r.get("analysis") and "cycling_s" not in r["analysis"] and (d / "analysis.json").exists():  # runs analysed earlier
        r["analysis"]["cycling_s"] = protocol.block_time(json.loads((d / "analysis.json").read_text())["commands"])
    r["tubes"] = _tubes(d)
    r["can_track"] = (d / "sam_file.json").exists()
    r["captions"] = len(_speech(d))
    r["max_reactions"] = protocol.max_reactions(r["steps"]) if r.get("protocol") else 1
    return r


@app.get("/api/runs/{name}/video")
def video(name: str):
    d = _run_dir(name)
    video = pipeline.asset(d, "video.mp4")
    if ON_VERCEL:  # public/ is served by the CDN and is not inside the function bundle
        return RedirectResponse(f"/runs/{video.parent.name}/video.mp4")
    return FileResponse(video, media_type="video/mp4")


def _speech(d):
    f = d / "speech.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def vtt(turns):
    """WebVTT for the <track> under the video. Model text is escaped: in a cue, '<' starts markup, '-->' a timing."""
    def ts(sec):
        return f"{int(sec // 3600):02}:{int(sec % 3600 // 60):02}:{sec % 60:06.3f}"

    out = ["WEBVTT", ""]
    for t in turns:
        text = " ".join(str(t.get("text", "")).split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if text:
            start = float(t["start"])
            out += [f"{ts(start)} --> {ts(max(float(t['end']), start + 2))}", text, ""]  # at least 2 s on screen
    return "\n".join(out)


@app.get("/api/runs/{name}/captions.vtt")
def captions(name: str):
    """Captions of the speech in the video, as Muse Voice Transcribe heard it (WCAG 1.2.2)."""
    return PlainTextResponse(vtt(_speech(_run_dir(name))), media_type="text/vtt")


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


def _app_link(name, request=None):
    """Where a tube label's QR code sends the phone: the app at this run (on Modal: the Vercel front end)."""
    site = os.getenv("PUBLIC_URL") or (str(request.base_url).rstrip("/") if request else "")
    return f"{site}/app?run={name}"


def _reactions(steps, reactions):
    top = protocol.max_reactions(steps)
    if not 1 <= reactions <= top:
        raise HTTPException(422, f"reactions must be 1 to {top} for this protocol")
    return reactions


@app.get("/api/runs/{name}/labels.svg")
def labels_file(name: str, request: Request, reactions: int = 1):
    """Tube labels to print on an Avery L7160 sheet. Each QR code opens the app at this run and tube, and the app's
    camera check ticks the tube off and shows its deck slot, so the rack is loaded the way protocol.py expects."""
    from fastapi.responses import Response

    from sdc import export

    steps = _confirmed_steps(name)
    svg, _ = export.tube_labels(steps, _app_link(name, request), _reactions(steps, reactions))
    return Response(svg, media_type="image/svg+xml", headers={  # served from the API origin: no script may run in it
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "Content-Disposition": f'inline; filename="{name}-tube-labels.svg"'})


@app.get("/api/runs/{name}/labels.zpl")
def labels_zpl(name: str, request: Request, reactions: int = 1):
    """The same tube labels for a Zebra thermal printer (ZPL II, 2 x 1 inch labels, 203 dpi): send the file to the
    printer as is (lp -o raw, or Zebra Setup Utilities' "send file")."""
    from sdc import export

    steps = _confirmed_steps(name)
    zpl, _ = export.tube_labels_zpl(steps, _app_link(name, request), _reactions(steps, reactions))
    return PlainTextResponse(zpl, headers={"Content-Disposition": f'attachment; filename="{name}-tube-labels.zpl"'})


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


@app.get("/api/runs/{name}/tecan-evo.py")
def tecan_file(name: str):
    from sdc import export

    return PlainTextResponse(export.pylabrobot_script(_confirmed_steps(name), "evo150"),
                             headers={"Content-Disposition": f'attachment; filename="{name}-tecan-evo.py"'})


@app.get("/api/runs/{name}/analysis.json")
def analysis_file(name: str):
    f = _run_dir(name) / "analysis.json"
    if not f.exists():
        raise HTTPException(409, "no analysis yet: the protocol has to pass the simulator first")
    return FileResponse(f, media_type="application/json", filename=f"{name}-opentrons-analysis.json")


_rx_locks = defaultdict(threading.Lock)  # one analysis per (protocol version, n); others wait and read its cache
_rx_slots = threading.Semaphore(2)  # opentrons analyze is a ~5 s subprocess: at most two at once, whatever arrives


@app.get("/api/runs/{name}/reactions/{n}")
def reactions_analysis(name: str, n: int):
    """Opentrons' own analysis of protocol.py with its Reactions parameter set to n, as the Opentrons App runs it after
    run setup: whether it passes, what to load into each tube, which plate wells get a copy, tips and tip racks.
    Cached per protocol version and n, failures included (at most max_reactions analyses per protocol)."""
    import hashlib
    import tempfile

    d = _run_dir(name)
    steps = _confirmed_steps(name)
    _reactions(steps, n)
    src = json.loads((d / "result.json").read_text(encoding="utf-8"))["protocol"]
    cache = d / "reactions" / f"{hashlib.sha256(src.encode()).hexdigest()[:16]}-{n}.json"
    with _rx_locks[str(cache)]:
        if cache.exists():  # made while this request waited for the lock
            return json.loads(cache.read_text(encoding="utf-8"))
        with _rx_slots, tempfile.TemporaryDirectory() as tmp:
            ok, an = protocol.analyze(src, Path(tmp) / "a.json", rtp={"reactions": n} if n > 1 else None)
            full = json.loads((Path(tmp) / "a.json").read_text(encoding="utf-8")) if (Path(tmp) / "a.json").exists() else {}
        out = {"reactions": n, "ok": ok, "errors": an.get("errors") or ([an["error"]] if "error" in an else []),
               "liquid_setup": an.get("liquid_setup", []), "commands": an.get("commands"), "cycling_s": an.get("cycling_s"),
               **protocol.plate_summary(full)}
        cache.parent.mkdir(exist_ok=True)  # a failure is cached too: asking again can't start another subprocess
        cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def _designer(d, name):
    from sdc import designer

    f = d / "analysis.json"
    if not f.exists():
        raise HTTPException(409, "no analysis yet: the protocol has to pass the simulator first")
    return designer.protocol_designer(json.loads(f.read_text(encoding="utf-8")), f"Show, Don't Code: {name}")


@app.get("/api/runs/{name}/protocol-designer.json")
def designer_file(name: str):
    """The protocol as editable steps for Opentrons Protocol Designer (designer.opentrons.com, Import): change a volume
    or the cycling program without code, and the Opentrons App runs the same file."""
    return JSONResponse(_designer(_run_dir(name), name),
                        headers={"Content-Disposition": f'attachment; filename="{name}-protocol-designer.json"'})


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
    files = {f: _sha256(pipeline.asset(d, f).read_bytes()) for f in ("video.mp4", "steps_raw.json", "steps_stage2.json",
                                                                       "tracks_tip.json", "analysis.json") if pipeline.asset(d, f).exists()}
    answers = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    rec = {
        "run": name,
        "protocol_sha256": _sha256(r["protocol"].encode("utf-8")),
        "files_sha256": files,
        "simulator": {"passed": r["sim_ok"], "robot_actions": len(r["sim_log"])},
        "opentrons_analysis": {"passed": r.get("analysis_ok"), "commands": (r.get("analysis") or {}).get("commands")},
        # a ground-truth replay never ran the models on its video, so don't claim it did
        "models": {"steps_and_sheet": meta.SPARK, "tracking": "sam-3.1", "speech": "muse-voice-transcribe-1.0",
                   "reviewer": r.get("review_model") or f"baseten:{agent.MODEL}"} if (d / "steps_raw.json").exists()
        else {"note": "steps replayed from BioVL2 human annotations; no model read this video"},
        "human_input": {"answers": answers, "named_tubes": [t["name"] for t in _tubes(d) if t.get("name")]},
        "reviewer_findings": len(r.get("review") or []),
        "verify": "sha256 of each file must match; the protocol hash covers protocol.py byte for byte (UTF-8)",
    }
    # anchor the receipt in Bitcoin (OpenTimestamps): proof that these hashes existed no later than that block
    digest = _sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    ts = {"stamped_sha256": digest, "how": "sha256 of this receipt without 'timestamp', as JSON with sorted keys, "
          "',' and ':' separators, ASCII escapes", "proof": f"/api/runs/{name}/receipt.ots",
          "verify": f"ots verify -d {digest} receipt.ots"}
    try:
        from sdc import stamp

        s = stamp.ensure(d / "stamps" / f"{digest}.ots", bytes.fromhex(digest))
        ts.update(pending_calendars=s["calendars"], bitcoin=s["bitcoin"])
    except Exception as e:  # noqa: BLE001 - the calendars or the explorer are down: the receipt still stands
        ts = {"stamped_sha256": digest, "error": f"OpenTimestamps unavailable right now ({type(e).__name__})"}
    rec["timestamp"] = ts
    return rec


@app.get("/api/runs/{name}/receipt.ots")
def receipt_ots(name: str):
    """The OpenTimestamps proof for the current receipt (binary .ots, verifiable with the standard `ots` client)."""
    from fastapi.responses import Response

    ts = receipt(name)["timestamp"]
    if "error" in ts:
        raise HTTPException(503, ts["error"])
    return Response((_run_dir(name) / "stamps" / f"{ts['stamped_sha256']}.ots").read_bytes(),
                    media_type="application/vnd.opentimestamps.ots",
                    headers={"Content-Disposition": f'attachment; filename="{name}-receipt.ots"'})


@app.get("/api/runs/{name}/ro-crate.zip")
def ro_crate(name: str, request: Request):
    """The whole run as an RO-Crate (Process Run Crate): files, hashes, and which tool made which file from what."""
    from fastapi.responses import Response

    from sdc import crate, export

    rec = receipt(name)
    d, steps = _run_dir(name), _confirmed_steps(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    title = f"Show, Don't Code run {name}"
    gen = {"protocol.py": r["protocol"].encode("utf-8"), "methods.md": export.methods_markdown(steps, title).encode("utf-8"),
           "autoprotocol.json": json.dumps(export.autoprotocol(steps), indent=1).encode("utf-8"),
           "pylabrobot.py": export.pylabrobot_script(steps).encode("utf-8"),
           "tecan-evo.py": export.pylabrobot_script(steps, "evo150").encode("utf-8"),
           "receipt.json": json.dumps(rec, indent=1).encode("utf-8"),
           "labels.svg": export.tube_labels(steps, _app_link(name, request))[0].encode("utf-8"),
           "labels.zpl": export.tube_labels_zpl(steps, _app_link(name, request))[0].encode("utf-8")}
    if "error" not in rec["timestamp"]:
        gen["receipt.ots"] = (d / "stamps" / f"{rec['timestamp']['stamped_sha256']}.ots").read_bytes()
    if (d / "analysis.json").exists():
        gen["protocol-designer.json"] = json.dumps(_designer(d, name), indent=1).encode("utf-8")
    if "review" in r and "note" not in rec["models"]:
        gen["review.json"] = json.dumps({k: r.get(k) for k in ("review", "agent_log", "review_model", "review_saw_video",
                                                              "review_host_failures")}, indent=1).encode("utf-8")
    return Response(crate.build(d, name, gen, rec["models"]), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}-ro-crate.zip"'})


@app.post("/api/runs/{name}/voice")
async def voice(name: str, request: Request):
    """Spoken answers: Muse Voice Transcribe hears them, Muse Spark maps them to the open questions.
    Returns pre-fill values only; the scientist still presses Confirm."""
    import subprocess
    import tempfile

    from sdc import meta

    d = _run_dir(name)
    if not os.getenv("MODEL_API_KEY"):
        raise HTTPException(503, "live model calls are not configured on this deployment")
    if live_calls_left()[0] <= 0:  # refuse before reading the upload; the call is only counted once audio is valid
        raise HTTPException(429, "today's live model calls for this demo are used up; they reset at 00:00 UTC")
    body = b""
    async for chunk in request.stream():  # a short voice note; refuse anything long before reading it all
        body += chunk
        if len(body) > 5 * 1024 * 1024:
            raise HTTPException(413, "keep voice answers under a minute")
    if len(body) < 1000:
        raise HTTPException(422, "no audio arrived; check the microphone permission")
    spend_live_call()
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
    with _editable(name) as d:
        _apply_fix(d, k)
    return result(d.name)


def _apply_fix(d, k):
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


@app.post("/api/runs/{name}/answers")
def answer(name: str, answers: dict[str, float] = Body(...)):
    with _editable(name) as d:
        _answer(d, answers)
    return result(d.name)


def _answer(d, answers):
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


def _tubes(d):
    f = d / "tubes.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


@app.post("/api/runs/{name}/tubes")
def identify_tube(name: str, t: float = Body(...), x: float = Body(...), y: float = Body(...)):
    """Scientist clicked a tube at time t, (x, y) in the 640x360 SAM frame: track it, then ground its uses."""
    if not (_run_dir(name) / "sam_file.json").exists():
        raise HTTPException(409, "this run has no SAM video (ground-truth replays can't track)")
    if not (0 <= x < 640 and 0 <= y < 360 and t >= 0):
        raise HTTPException(422, "click outside the frame")
    if not os.getenv("MODEL_API_KEY"):
        raise HTTPException(503, "live model calls are not configured on this deployment")
    spend_live_call()
    with _editable(name) as d:
        _track_tube(d, name, t, x, y)
    return result(d.name)


def _track_tube(d, name, t, x, y):
    from sdc import meta

    frame = round(t * pipeline.SAM_FPS)
    fid = json.loads((d / "sam_file.json").read_text())
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    try:
        track = {str(k): v for k, v in meta.track_point(fid, frame, x, y, (640, 360)).items()}
        uses = meta.steps_using(pipeline.asset(d, "sam_frames"), track, steps, pipeline.SAM_FPS, frame)
    except Exception as e:  # details go to the server log only; provider messages can carry ids and internals
        log.exception("tube tracking failed for run %s", name)
        busy = "RateLimit" in type(e).__name__ or "429" in str(e)
        raise HTTPException(502, "the model provider is busy; try again in a minute" if busy
                            else "model call failed; please try again later")
    tubes = _tubes(d)
    tubes.append({"id": len(tubes), "t": t, "x": x, "y": y, "name": None, "track": track, **uses})
    (d / "tubes.json").write_text(json.dumps(tubes), encoding="utf-8")


@app.post("/api/runs/{name}/tubes/{tube_id}/name")
def name_tube(name: str, tube_id: int, label: str = Body(..., embed=True)):
    """Apply the scientist's name for a tube to every step SAM + Muse Spark tied to it."""
    with _editable(name) as d:
        _name_tube(d, tube_id, label)
    return result(d.name)


def _name_tube(d, tube_id, label):
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


# ---- MCP: other agents (Claude, Llama, any MCP client) read runs over Streamable HTTP at /mcp -----------------
if MCPServer:
    # Read-only on purpose: no tool runs code an agent sends (an Opentrons protocol is Python) or spends model credits.
    mcp = MCPServer("show-dont-code", instructions=(
        "Robot protocols recovered from first-person lab videos. list_runs, then get_run for the steps found in the video "
        "and the checks they passed, get_protocol for the Opentrons Flex Python file, get_protocol_designer for the same "
        "protocol as editable no-code steps, get_tube_labels for loading the tube rack, get_receipt for the hashes."))

    def _http(fn, *args):  # an agent should read "unknown run", not a bare "Error executing tool"
        try:
            return fn(*args)
        except HTTPException as e:
            raise ToolError(e.detail) from None

    @mcp.tool()
    def list_runs() -> list[str]:
        """Names of the sample runs (videos already turned into protocols)."""
        return runs()

    @mcp.tool()
    def get_run(name: str) -> dict:
        """The steps found in the video (with video seconds), the open questions, and the Opentrons simulator and protocol
        analysis results, including the deck setup, runtime parameters and the thermocycler's minimum time in seconds."""
        r = _http(result, name)
        keep = ("action", "description", "reagent", "volume_ul", "source", "destination", "start", "end", "cycles")
        return {"run": name, "steps": [{k: s[k] for k in keep if s.get(k) is not None} for s in r.get("steps", [])],
                "questions": r.get("questions", []), "simulator_ok": r.get("sim_ok"), "robot_actions": len(r.get("sim_log", [])),
                "analysis_ok": r.get("analysis_ok"), "analysis": r.get("analysis"), "hamilton_starlet_ok": r.get("plr_ok"),
                "tecan_evo_ok": r.get("evo_ok")}

    @mcp.tool()
    def get_protocol(name: str) -> str:
        """The generated Opentrons Flex protocol (Python, API 2.24), ready to import into the Opentrons App."""
        r = _http(result, name)
        if not r.get("protocol"):
            raise ToolError("no protocol yet: the run still has open questions (see get_run)")
        return r["protocol"]

    @mcp.tool()
    def get_protocol_designer(name: str) -> dict:
        """The protocol as an Opentrons Protocol Designer file: import it at designer.opentrons.com to edit each step
        (transfers, mixes, the thermocycler program) without code. The Opentrons App can also run it as is."""
        return _http(lambda n: _designer(_run_dir(n), n), name)

    @mcp.tool()
    def get_tube_labels(name: str, reactions: int = 1) -> dict:
        """The tubes to load on the tube rack before the run: each tube's deck slot and well, how many µL to fill a source
        tube with (what the robot draws + 10 % + dead volume) for `reactions` copies of the filmed reaction (protocol.py's
        Reactions parameter, up to max_reactions), and the link its label's QR code opens. Links to the printable
        labels: an Avery L7160 sheet (SVG) and a Zebra thermal printer file (ZPL, 2 x 1 in)."""
        from sdc import export

        steps = _http(_confirmed_steps, name)
        link = _app_link(name)
        n = _http(_reactions, steps, reactions)
        files, q = f"{os.getenv('PUBLIC_URL', '')}/api/runs/{name}", f"?reactions={n}" if n > 1 else ""
        return {"run": name, "reactions": n, "max_reactions": protocol.max_reactions(steps),
                "tubes": [{**t, "qr": f"{link}&tube={quote(t['tube'])}"} for t in export.label_tubes(steps, n)],
                "labels_svg": f"{files}/labels.svg{q}", "labels_zpl": f"{files}/labels.zpl{q}"}

    @mcp.tool()
    def get_receipt(name: str) -> dict:
        """Reproducibility receipt: SHA-256 of the protocol, the video and every model output, and which models ran,
        anchored in Bitcoin with OpenTimestamps ('timestamp': pending calendars, or the confirming block)."""
        return _http(receipt, name)

    app.mount("/", mcp.streamable_http_app(  # mounted last, so every route above matches first
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)))  # public, read-only, no cookies
    app.router.lifespan_context = lambda _app: mcp.session_manager.run()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
