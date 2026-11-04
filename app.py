"""Review UI server: human video next to the generated robot protocol.  Run: python app.py"""
import asyncio
import json
import logging
import math
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

from sdc import pipeline, protocol, terms  # noqa: E402  (must follow the env setup above)

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Show, Don't Code", version="1.0.0", description=(
    "Lab protocols filmed once by hand (or read from protocols.io, a photo or a PDF) turned into Opentrons Flex robot "
    "protocols. Read endpoints are open; uploads need the access code; live model calls have a daily budget. "
    "Code: https://github.com/PugarHuda/show-dont-code · MCP server at /mcp."))
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


def spend_live_call(n=1):
    with _calls_lock:
        left, today = live_calls_left()
        if left < n:
            raise HTTPException(429, "today's live model calls for this demo are used up; they reset at 00:00 UTC")
        (pipeline.OUT / ".live_calls.json").write_text(json.dumps({"day": today, "used": LIVE_CALL_LIMIT - left + n}))


def _run_dir(name):
    if not re.fullmatch(r"[\w.-]{1,100}", name) or not (pipeline.OUT / name / "result.json").exists():
        raise HTTPException(404, "unknown run")
    return pipeline.OUT / name


PRIVATE = ("u-", "c-", "p-")  # uploads, edit copies and protocols.io imports: reachable only through their unguessable link
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


@app.get("/sw.js")
def service_worker():  # at the root so its scope covers /app; no-cache so a new version is picked up at once
    return FileResponse(ROOT / "web" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(ROOT / "web" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icon-{size}.png")
def icon(size: int):
    if size not in (192, 512):
        raise HTTPException(404, "no such icon")
    return FileResponse(ROOT / "web" / f"icon-{size}.png", media_type="image/png")


@app.get("/media/{name}")
def media(name: str):  # local preview only; on Vercel public/media is served by the CDN before this runs
    path = ROOT / "public" / "media" / name
    if not re.fullmatch(r"[\w.-]{1,100}", name) or not path.exists():
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
           f"connect-src 'self' {api} {pyo} {zplr}zebrash.wasm https://pypi.org https://files.pythonhosted.org; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return HTMLResponse(html, headers={"Content-Security-Policy": " ".join(csp.split())})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method == "HEAD":  # uptime and link checkers ask with HEAD: answer as GET (the server drops the body)
        request.scope["method"] = "GET"
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
                         "Strict-Transport-Security": "max-age=31536000",
                         "Permissions-Policy": "camera=(self), geolocation=(), microphone=(self)"})  # voice answers, recording
    return resp


@app.get("/")
def landing():
    page = ROOT / "web" / "landing.html"
    return _page(page if page.exists() else ROOT / "web" / "index.html")


def lab_protocol(name):
    """Bioschemas LabProtocol (0.8-DRAFT) JSON-LD for a shared run, so search engines and FAIR crawlers can read the
    protocol from the page: steps, reagents, equipment, source and licence. None for a private run or no run."""
    if not re.fullmatch(r"[\w.-]{1,100}", name or "") or name.startswith(PRIVATE):
        return None
    f = pipeline.OUT / name / "result.json"
    if not f.exists():
        return None
    r = json.loads(f.read_text(encoding="utf-8"))
    src = pipeline.OUT / name / "source.json"
    source = json.loads(src.read_text(encoding="utf-8")) if src.exists() else None
    site = os.getenv("PUBLIC_URL", "https://show-dont-code.vercel.app")
    steps = [s for s in r["steps"] if s.get("action") != "skipped"]
    based = ({"@id": f"https://doi.org/{source['doi']}"} if source and source.get("doi") else
             None if source else {"@id": "https://zenodo.org/records/21495188"})
    out = {
        "@context": {"@vocab": "https://schema.org/", "dct": "http://purl.org/dc/terms/"},
        "@type": "LabProtocol", "@id": f"{site}/app?run={name}", "url": f"{site}/app?run={name}",
        "dct:conformsTo": {"@id": "https://bioschemas.org/profiles/LabProtocol/0.8-DRAFT"},
        "name": f"Opentrons Flex protocol: {name}",
        "description": ("Steps recovered " + ("from a protocol document" if source else "from a first-person video of the "
                        "protocol done by hand") + ", turned into an Opentrons Flex protocol by Show, Don't Code."),
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "labEquipment": ["Opentrons Flex"] + [{"@type": "DefinedTerm", "name": t[0], "termCode": t[2].replace("_", ":"),
                                               "url": terms.iri(t)} for t in terms.equipment(steps)],
        "reagentUsed": sorted({s["reagent"] for s in steps if s.get("reagent")}),
        "step": [{"@type": "HowToStep", "position": i + 1, "text": " ".join(str(x) for x in (
            s.get("description") or s["action"], f"{s['volume_ul']} µL" if s.get("volume_ul") else "") if x),
            **({"additionalType": terms.iri(terms.ACTION[s["action"]])} if s["action"] in terms.ACTION else {})}
            for i, s in enumerate(steps)],
        "isPartOf": {"@id": "https://github.com/PugarHuda/show-dont-code"},
    }
    if based:
        out["isBasedOn"] = based
    return out


@app.get("/app")
def index(run: str = ""):
    api = os.getenv("API_BASE", "")
    uploads = "true" if api or os.getenv("UPLOADS") == "1" else "false"  # only the Modal backend can take uploads
    pre = f'<link rel="preconnect" href="{api}" crossorigin>\n' if api else ""  # the run's data comes from there first
    ld = lab_protocol(run)
    if ld:  # data, not a script: CSP doesn't run it. "</" is escaped so no text in it can close the tag
        pre += '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>\n"
    return _page(ROOT / "web" / "index.html", pre + f'<script>window.API_BASE = "{api}"; window.UPLOADS = {uploads};</script>\n')


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
    r["copy_of"] = (d / "copy_of").read_text().strip() if (d / "copy_of").exists() else None
    r["bench_card"] = card_state(d)  # the app asks for the card only when there is one: no 404 in the console
    src = d / "source.json"  # a run started from a published protocol instead of a video
    r["source"] = {k: v for k, v in json.loads(src.read_text(encoding="utf-8")).items() if k != "text"} if src.exists() else None
    return r


@app.get("/api/runs/{name}/video")
def video(name: str):
    d = _run_dir(name)
    video = pipeline.asset(d, "video.mp4")
    if not video.exists():  # a protocols.io or document import has no footage
        raise HTTPException(404, "this run has no video")
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
    if not r.get("sim_ok"):  # a file the Opentrons simulator rejects would be rejected by the robot too
        raise HTTPException(409, "this protocol failed the Opentrons simulator; see the simulator log")
    return PlainTextResponse(r["protocol"], headers={"Content-Disposition": f'attachment; filename="{name}.py"'})


def _source(name):
    """source.json of a protocols.io or document import (an edit copy reads its sample's), else None for a video."""
    f = pipeline.asset(_run_dir(name), "source.json")
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


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

    return PlainTextResponse(export.methods_markdown(_confirmed_steps(name), f"Show, Don't Code run {name}", _source(name)),
                             media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{name}-methods.md"'})


@app.get("/api/runs/{name}/protocol.ipynb")
def notebook_file(name: str, request: Request):
    """Jupyter notebook: methods, protocol.py and a cell that runs the Opentrons simulator on it."""
    from sdc import export

    steps = _confirmed_steps(name)
    src = protocol_file(name).body.decode("utf-8")  # same checks as protocol.py: confirmed and simulator-passing
    nb = export.notebook(steps, src, f"Show, Don't Code run {name}", _app_link(name, request), _source(name))
    return JSONResponse(nb, headers={"Content-Disposition": f'attachment; filename="{name}.ipynb"'})


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


@app.get("/api/runs/{name}/tecan.gwl")
def tecan_worklist_file(name: str, robot: str = "evo", aqueous: str = "", viscous: str = "", volatile: str = ""):
    """Tecan worklist for EVOware / FluentControl (robotools, volume-tracked). Liquid class names per installation."""
    from sdc import export

    if robot not in ("evo", "fluent"):
        raise HTTPException(422, "robot must be evo or fluent")
    classes = {"water": aqueous, "glycerol_50": viscous, "ethanol_80": volatile}
    if not all(export.CLASS_NAME.fullmatch(v) for v in classes.values() if v):
        raise HTTPException(422, "liquid class names: letters, digits, spaces and _ . ( ) + - only, up to 32")
    try:
        gwl = export.tecan_worklist(_confirmed_steps(name), robot, classes)
    except Exception as e:  # robotools' volume tracking: an over-filled well is refused, not written
        if "Volume" not in type(e).__name__:
            raise
        raise HTTPException(409, f"robotools refused the worklist: {e}")
    return PlainTextResponse(gwl, headers={"Content-Disposition": f'attachment; filename="{name}-{robot}.gwl"'})


@app.get("/api/runs/{name}/transfers.csv")
def transfers_csv_file(name: str):
    from sdc import export

    return PlainTextResponse(export.transfer_csv(_confirmed_steps(name)), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}-transfers.csv"'})


def _ot2(name):
    """The same confirmed steps for an OT-2 (P20/P300 GEN2, Thermocycler GEN2), and Opentrons' analysis of that file
    with opentrons 9.0.0 (10.x refuses OT-2 protocols), cached per file hash: at the default and at the most reactions."""
    steps = _confirmed_steps(name)
    src, _ = protocol.generate(steps, robot="OT-2")
    d = _run_dir(name) / "ot2_checks"
    f = d / f"{_sha256(src.encode('utf-8'))[:16]}.json"
    if f.exists():
        return src, json.loads(f.read_text(encoding="utf-8"))
    py = os.getenv("OT2_PYTHON")
    if not py or not Path(py).exists():
        raise HTTPException(503, "the OT-2 checker (opentrons 9.0.0) is not installed on this server")
    top = protocol.max_reactions(steps, protocol.OT2)
    checks = []
    d.mkdir(exist_ok=True)
    for rtp in ([None, {"reactions": top}] if top > 1 else [None]):
        tmp = d / f"{f.stem}.{secrets.token_hex(4)}.tmp"  # two requests at once must not share a file
        ok, summary = protocol.analyze(src, tmp, rtp=rtp, python=py)
        tmp.unlink(missing_ok=True)
        checks.append({"reactions": (rtp or {}).get("reactions", 1), "ok": ok, "errors": summary.get("errors") or
                       ([summary["error"]] if "error" in summary else []), "commands": summary.get("commands")})
    res = {"ok": all(c["ok"] for c in checks), "checker": "opentrons 9.0.0 analyze", "checks": checks}
    f.write_text(json.dumps(res), encoding="utf-8")
    return src, res


@app.get("/api/runs/{name}/ot2.py")
def ot2_file(name: str):
    """The protocol for an Opentrons OT-2, served only if Opentrons' own analysis (opentrons 9.0.0) passes it."""
    src, res = _ot2(name)
    if not res["ok"]:
        raise HTTPException(409, f"the OT-2 version failed Opentrons' analysis: {res['checks']}")
    return PlainTextResponse(src, headers={"Content-Disposition": f'attachment; filename="{name}-ot2.py"'})


@app.get("/api/runs/{name}/ot2-check.json")
def ot2_check(name: str):
    """What opentrons 9.0.0 analyze said about the OT-2 file."""
    return _ot2(name)[1]


@app.get("/api/runs/{name}/analysis.json")
def analysis_file(name: str):
    f = _analysis(_run_dir(name))
    if not f:
        raise HTTPException(409, "no analysis yet: the protocol has to pass the simulator first")
    return FileResponse(f, media_type="application/json", filename=f"{name}-opentrons-analysis.json")


_rx_locks = defaultdict(threading.Lock)  # one analysis per (protocol version, n); others wait and read its cache
_rx_slots = threading.Semaphore(2)  # opentrons analyze is a ~5 s subprocess: at most two at once, whatever arrives


@app.get("/api/runs/{name}/reactions/{n}")
def reactions_analysis(name: str, n: int):
    """Opentrons' own analysis of protocol.py with its Reactions parameter set to n, as the Opentrons App runs it after
    run setup: whether it passes, what to load into each tube, which plate wells get a copy, tips and tip racks.
    Cached per protocol version and n once the analyzer finishes, pass or fail (at most max_reactions per protocol)."""
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
        if full:  # the analyzer finished (passed or found protocol errors): the same answer every time, so keep it.
            # A crash or missing output may be transient and is not cached; the lock and the two slots still bound retries
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(json.dumps(out), encoding="utf-8")
    return out


_flex_slot = threading.Semaphore(1)  # each robot-server run is a ~50 s Modal container
FLEX_RUNS_PER_DAY = int(os.getenv("FLEX_RUNS_PER_DAY", "40"))  # ponytail: global daily cap; per-visitor if abused


def _spend_flex_run():
    with _calls_lock:
        f, today = pipeline.OUT / ".flex_runs.json", time.strftime("%Y-%m-%d", time.gmtime())
        c = json.loads(f.read_text()) if f.exists() else {}
        used = c.get("used", 0) if c.get("day") == today else 0
        if used >= FLEX_RUNS_PER_DAY:
            raise HTTPException(429, "today's robot-server runs for this demo are used up; cached runs still open")
        f.write_text(json.dumps({"day": today, "used": used + 1}))


@app.get("/api/runs/{name}/flex-run/{n}")
def flex_robot_run(name: str, n: int):
    """protocol.py run end to end on Opentrons' own robot-server (the software on a Flex) in Flex simulation, with
    Reactions = n: upload, the robot's analysis, deck configuration, play, hand-off pauses resumed. Cached per protocol
    version and n once the robot reports a finished or refused run."""
    import hashlib

    d = _run_dir(name)
    steps = _confirmed_steps(name)
    _reactions(steps, n)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    if not r.get("sim_ok"):
        raise HTTPException(409, "the protocol has to pass the simulator first")
    src = r["protocol"]
    cache = d / "flex_runs" / f"{hashlib.sha256(src.encode()).hexdigest()[:16]}-{n}.json"
    top = protocol.max_reactions(steps)
    # a shared sample's receipt cites its robot-server runs: visitors can't add one per reaction count (and use up
    # the day's cap), only the counts the app offers; a private run (upload, edit copy) takes any count
    if not name.startswith(PRIVATE) and not cache.exists() and n not in {1, 2, 8, 24, top}:
        raise HTTPException(422, f"on a shared sample, run 1, 2, 8, 24 or {top} reactions (or open your own copy)")
    with _rx_locks[str(cache)]:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        if not _flex_slot.acquire(blocking=False):  # answer at once rather than queue threads on the one container
            raise HTTPException(429, "the robot-server is busy with another run; try again in a minute")
        try:
            _spend_flex_run()  # only uncached runs count
            import modal

            out = modal.Function.from_name("show-dont-code", "flex_run").remote(src, {"reactions": n} if n > 1 else {})
        except HTTPException:
            raise
        except Exception as e:  # Modal unreachable or the container failed: say so, don't cache
            log.exception("robot-server run failed for %s", name)
            raise HTTPException(503, "the robot-server run didn't finish; please try again later")
        finally:
            _flex_slot.release()
        out["reactions"] = n
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
    return out


_card_jobs = {}  # run -> "drawing" | error text, for the one web container


def card_state(d):
    """"ready", "drawing", or None: what the app shows before asking for the card."""
    if pipeline.asset(d, "card.json").exists():
        return "ready"
    return "drawing" if _card_jobs.get(d.name) == "drawing" else None


@app.get("/api/runs/{name}/bench-card")
def bench_card(name: str):
    """The illustrated bench card: each filmed step drawn by Muse Image from its own video frame, with Muse Spark's
    verdict on whether the drawing kept the scene (card.json). 202 while it is being drawn, 404 before."""
    d = _run_dir(name)
    f = pipeline.asset(d, "card.json")
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    job = _card_jobs.get(d.name)
    if job == "drawing":
        return JSONResponse({"drawing": True}, status_code=202)
    if not job and not pipeline.asset(d, "video.mp4").exists():
        raise HTTPException(404, "this run has no video, so no bench card")
    raise HTTPException(503 if job else 404, job or "no bench card yet")


@app.get("/api/runs/{name}/bench-card/{i}.jpg")
def bench_card_image(name: str, i: int, w: int = 0):
    """A drawing at full size (1280 px, for print) or w=640 for screens: phones show it about 330 px wide."""
    d = _run_dir(name)
    f = pipeline.asset(d, "card") / f"step_{i}.jpg"
    if not f.exists() or not pipeline.asset(d, "card.json").exists():  # only a finished card is served
        raise HTTPException(404, "no drawing for this step")
    if w == 640:  # one fixed size, made once next to the drawing (or into the copy reading its sample's card)
        small = d / "card_640" / f.name
        if not small.exists():
            from PIL import Image

            small.parent.mkdir(exist_ok=True)
            img = Image.open(f).convert("RGB")
            tmp = small.with_name(f"{small.name}.{secrets.token_hex(4)}.tmp")  # a reader never sees half a JPEG
            img.resize((640, round(img.height * 640 / img.width))).save(tmp, "JPEG", quality=82)
            os.replace(tmp, small)
        f = small
    elif w:
        raise HTTPException(422, "w is 640 or left out")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/runs/{name}/bench-card", status_code=202)
def draw_bench_card(name: str):
    """Start drawing the bench card of an uploaded run (the samples come with theirs, and a shared sample is never
    changed by a visitor). One Muse Image edit and one Muse Spark check per filmed step, in the background; each step
    counts as a live model call. Poll GET until it answers 200."""
    from sdc import illustrate

    d = _run_dir(name)
    if not name.startswith("u-"):
        raise HTTPException(409, "the sample runs come with their bench cards; draw one for your own upload")
    if not pipeline.asset(d, "video.mp4").exists():
        raise HTTPException(409, "this run has no video to draw from")
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    todo = illustrate.filmed(steps)
    if not todo:
        raise HTTPException(409, "no filmed steps to illustrate")
    with _card_locks[d.name]:
        if card_state(d):
            return {"drawing": card_state(d) == "drawing"}
        spend_live_call(len(todo))
        _card_jobs[d.name] = "drawing"

    def work():
        try:
            illustrate.bench_card(d, pipeline.asset(d, "video.mp4"))
            _card_jobs.pop(d.name, None)
        except Exception:  # model refused or unreachable: nothing half-made is kept (illustrate writes card/ last)
            log.exception("bench card failed for %s", name)
            _card_jobs[d.name] = "Muse Image or Muse Spark didn't answer; please try again later"

    threading.Thread(target=work, daemon=True).start()
    return {"drawing": True, "steps": len(todo)}


_card_locks = defaultdict(threading.Lock)


def _run_title(d):
    """What names the protocol: a sample's BioVL2 name (miniprep_7) via its copy_of, or an import's title."""
    src = pipeline.asset(d, "source.json")
    title = json.loads(src.read_text(encoding="utf-8")).get("title") or "" if src.exists() else ""
    sample = (d / "copy_of").read_text().strip() if (d / "copy_of").exists() else d.name
    return f"{title} {'' if sample.startswith('u-') else sample}".strip()


@app.get("/api/runs/{name}/related")
def related_protocols(name: str):
    """Published protocols on protocols.io that use this run's reagents (brand and kit names), with their DOIs.
    Cached per run and search terms; a protocols.io outage is a 503, not an empty list."""
    from sdc import related

    d = _run_dir(name)
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    title = _run_title(d)
    words, kind = related.terms(steps, title)
    cache = d / "related.json"
    if cache.exists():
        c = json.loads(cache.read_text(encoding="utf-8"))
        if c.get("reagent_words") == words and c.get("kind") == kind:
            return c
    try:
        out = related.related(steps, title=title)
    except Exception:  # noqa: BLE001 - token missing, protocols.io down or changed
        log.exception("protocols.io search failed for %s", name)
        raise HTTPException(503, "protocols.io search is unavailable right now")
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


@app.get("/api/runs/{name}/related/crosscheck")
def related_crosscheck(name: str, compute: bool = False):
    """This run's volumes next to the volumes published protocols with the same kit use: Muse Spark reads up to three
    of the related protocols.io protocols, and only volumes with a quote that is really in the protocol are kept.
    Cached per run, kit words and volumes; computing it counts as one live model call per protocol read, so a plain
    GET only serves the cache ({"computed": false} when there is none) and ?compute=true, a click in the app, computes it."""
    from sdc import crosscheck, related

    d = _run_dir(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    steps = r["steps"]
    mine, _ = crosscheck.reaction(steps)
    if not r.get("protocol") or not mine:  # shares of a half-confirmed reaction would mean nothing
        raise HTTPException(409, "no protocol yet: answer the open questions first")
    key = related.terms(steps)[0] + [f"{k}={v}" for k, v in sorted(mine.items())]
    cache = pipeline.asset(d, "crosscheck.json")
    if cache.exists():
        c = json.loads(cache.read_text(encoding="utf-8"))
        if c.get("key") == key:
            return c
    if not compute:  # a page load or a crawler never spends model calls; the scientist asks for it
        return {"computed": False, "detail": "not computed yet for these volumes: ask for it with ?compute=true"}
    with _rx_locks[f"crosscheck-{d.name}"]:
        cache = d / "crosscheck.json"  # another request may have finished it while this one waited
        if cache.exists() and json.loads(cache.read_text(encoding="utf-8")).get("key") == key:
            return json.loads(cache.read_text(encoding="utf-8"))
        rel = related_protocols(name)
        spend_live_call(min(3, len([p for p in rel["protocols"] if p.get("doi")])) or 1)
        try:
            c = crosscheck.crosscheck(steps, rel)
        except Exception:  # noqa: BLE001 - Muse Spark or protocols.io unreachable
            log.exception("cross-check failed for %s", name)
            raise HTTPException(503, "the cross-check with published protocols didn't finish; please try again later")
        c["key"] = key
        (d / "crosscheck.json").write_text(json.dumps(c), encoding="utf-8")
    return c


def _analysis(d):
    """analysis.json of the run's current protocol, or None: a protocol that failed the simulator or still needs answers
    has none, and an edit copy that never rebuilt reads its sample's (same protocol)."""
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    f = pipeline.asset(d, "analysis.json")
    return f if r.get("protocol") and r.get("sim_ok") and r.get("analysis_ok") and f.exists() else None


def _designer(d, name):
    from sdc import designer

    f = _analysis(d)
    if not f:
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
    from sdc import meta

    d = _run_dir(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    if not r.get("protocol"):
        raise HTTPException(409, "no protocol yet: answer the open questions first")
    # every model output the protocol depends on: steps, bench sheet and cycling program, speech, tip and tube tracks
    files = {f: _sha256(pipeline.asset(d, f).read_bytes()) for f in (
        "video.mp4", "source.json", "steps_raw.json", "steps_stage2.json", "sheet.json", "profile.json", "speech.json",
        "tracks_tip.json", "tubes.json", "card.json") if pipeline.asset(d, f).exists()}
    if _analysis(d):  # Opentrons' analysis only of the protocol this receipt is for
        files["analysis.json"] = _sha256(_analysis(d).read_bytes())
    psha = _sha256(r["protocol"].encode("utf-8"))
    flex = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((d / "flex_runs").glob(f"{psha[:16]}-*.json"))]
    answers = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    rec = {
        "run": name,
        "protocol_sha256": psha,
        "files_sha256": files,
        "simulator": {"passed": r["sim_ok"], "robot_actions": len(r["sim_log"])},
        "opentrons_analysis": {"passed": r.get("analysis_ok"), "commands": (r.get("analysis") or {}).get("commands")},
        # this exact protocol.py run on Opentrons' robot-server (Flex simulation), if anyone pressed the button
        "opentrons_robot_server": [{k: f.get(k) for k in ("reactions", "robot_software", "analysis", "status", "commands",
                                                           "matches_analysis", "completed_at")} for f in flex],
        "pylabrobot": {"hamilton_starlet_passed": r.get("plr_ok"), "tecan_evo_passed": r.get("evo_ok")},
        # a ground-truth replay never ran the models on its video, so don't claim it did
        # a text run read a published protocol, not a video: no tracking or speech model ran, so none is claimed
        "models": ({"steps_and_cycling_program": meta.SPARK, "reviewer": r.get("review_model") or "not recorded for this run"}
                   if (d / "source.json").exists() else
                   {"steps_and_sheet": meta.SPARK, "tracking": "sam-3.1", "speech": "muse-voice-transcribe-1.0",
                    "reviewer": r.get("review_model") or "not recorded for this run"}) if (d / "steps_raw.json").exists()
        else {"note": "steps replayed from BioVL2 human annotations; no model read this video"},
        # the bench card is drawn from the video for people; the protocol doesn't depend on it, but its models ran
        "bench_card": ({k: json.loads(pipeline.asset(d, "card.json").read_text(encoding="utf-8"))[k] for k in
                        ("image_model", "check_model")} if pipeline.asset(d, "card.json").exists() else None),
        "source": {k: v for k, v in json.loads((d / "source.json").read_text(encoding="utf-8")).items() if k != "text"}
        if (d / "source.json").exists() else {"video_sha256": files.get("video.mp4")},
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


def _run_files(name, request):
    """Every file a run hands out (protocol, exports, receipt, robot-server reports, ...), shared by the RO-Crate and
    the .eln export so both carry exactly the same bytes."""
    from sdc import export

    rec = receipt(name)
    d, steps = _run_dir(name), _confirmed_steps(name)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    title = f"Show, Don't Code run {name}"
    gen = {"protocol.py": r["protocol"].encode("utf-8"), "methods.md": export.methods_markdown(steps, title, _source(name)).encode("utf-8"),
           "autoprotocol.json": json.dumps(export.autoprotocol(steps), indent=1).encode("utf-8"),
           "transfers.csv": export.transfer_csv(steps).encode("utf-8"),
           "receipt.json": json.dumps(rec, indent=1).encode("utf-8"),
           "labels.svg": export.tube_labels(steps, _app_link(name, request))[0].encode("utf-8"),
           "labels.zpl": export.tube_labels_zpl(steps, _app_link(name, request))[0].encode("utf-8")}
    for f, make in (("pylabrobot.py", lambda: export.pylabrobot_script(steps)),
                    ("tecan-evo.py", lambda: export.pylabrobot_script(steps, "evo150")),
                    ("tecan-evo.gwl", lambda: export.tecan_worklist(steps))):
        try:  # a run bigger than one tip carrier, or a well robotools refuses, has no such file; the rest still ships
            gen[f] = make().encode("utf-8")
        except Exception as e:  # noqa: BLE001 - ValueError, or robotools' VolumeOverflowError
            if not isinstance(e, ValueError) and "Volume" not in type(e).__name__:
                raise
            log.info("%s: no %s (%s)", name, f, e)
    if "error" not in rec["timestamp"]:
        gen["receipt.ots"] = (d / "stamps" / f"{rec['timestamp']['stamped_sha256']}.ots").read_bytes()
    psha = rec["protocol_sha256"][:16]
    for f in sorted((d / "flex_runs").glob(f"{psha}-*.json")):  # robot-server runs of this exact protocol.py
        gen[f"opentrons-robot-server-run-{f.stem.split('-')[-1]}-reactions.json"] = f.read_bytes()
    if _analysis(d):
        gen["protocol-designer.json"] = json.dumps(_designer(d, name), indent=1).encode("utf-8")
    if "review" in r and "note" not in rec["models"]:
        gen["review.json"] = json.dumps({k: r.get(k) for k in ("review", "agent_log", "review_model", "review_saw_video",
                                                              "review_host_failures")}, indent=1).encode("utf-8")
    return gen, rec, d, steps, r


_exports = {}  # (kind, run) -> (version, bytes): the last build of each export
_export_slots = threading.BoundedSemaphore(2)  # builds take seconds on the one web container: two at a time, the rest wait up to 45 s


def _export(name, request, kind, build):
    """An export (RO-Crate, .eln) is rebuilt only when what it contains changed: the run, its stamps, robot-server
    runs or bench card. Otherwise the bytes of the last build are served."""
    d = _run_dir(name)
    version = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (
        d / "result.json", d / "answers.json", d / "stamps", d / "flex_runs", pipeline.asset(d, "card.json"))) + (
        _app_link(name, request),)
    hit = _exports.get((kind, d.name))
    if hit and hit[0] == version:
        return hit[1]
    if not _export_slots.acquire(timeout=45):  # three quick clicks (crate, ARC, .eln) queue rather than fail
        raise HTTPException(429, "busy building other downloads; try again in a few seconds")
    try:
        body = build(*_run_files(name, request))
    finally:
        _export_slots.release()
    if len(_exports) > 64:  # ponytail: drop everything past 64 entries, an LRU if traffic ever needs it
        _exports.clear()
    _exports[(kind, d.name)] = (version, body)
    return body


@app.get("/api/runs/{name}/ro-crate.zip")
def ro_crate(name: str, request: Request):
    """The whole run as an RO-Crate (Process Run Crate): files, hashes, and which tool made which file from what."""
    from fastapi.responses import Response

    from sdc import crate

    body = _export(name, request, "crate", lambda gen, rec, d, steps, r: crate.build(d, name, gen, rec["models"]))
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}-ro-crate.zip"'})


@app.get("/api/runs/{name}/run.arc.zip")
def arc_file(name: str, request: Request):
    """The run as an ARC (Annotated Research Context, nfdi4plants): an ISA investigation whose assay table has a row
    per robot step, with protocol.py and the methods as the assay's protocols and the checks as its dataset. Opens in
    ARCitect and the DataHUB."""
    from fastapi.responses import Response

    from sdc import arc

    def build(gen, rec, d, steps, r):
        src = d / "source.json"
        source = json.loads(src.read_text(encoding="utf-8")) if src.exists() else None
        own = name.startswith(("u-", "c-"))  # a visitor's upload or edited copy: not the repo author's work
        free = not name.startswith("u-") and not (source and source.get("kind") == "document" and not source.get("license"))
        keep = ("protocol.py", "methods.md", "protocol-designer.json", "autoprotocol.json", "receipt.json", "analysis.json")
        return arc.build(name, f"Show, Don't Code run {name}", steps, {f: b for f, b in gen.items() if f in keep},
                         author=not own, licence="CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)" if free else None)

    body = _export(name, request, "arc", build)
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="show-dont-code-{name}.arc.zip"'})


@app.get("/api/runs/{name}/run.eln")
def eln_file(name: str, request: Request):
    """The run as one experiment in the ELN Consortium's .eln format (RO-Crate 1.2): import it into eLabFTW, RSpace,
    Kadi4Mat, SampleDB or another electronic lab notebook. Steps, checks and bench card in the entry's body, every
    file attached, each reviewer finding a comment."""
    from fastapi.responses import Response

    from sdc import eln

    def build(gen, rec, d, steps, r):
        card_f = pipeline.asset(d, "card.json")
        card = json.loads(card_f.read_text(encoding="utf-8")) if card_f.exists() else None
        imgs = {c["step"]: (pipeline.asset(d, "card") / c["image"]).read_bytes() for c in (card or {}).get("steps", [])
                if (pipeline.asset(d, "card") / c["image"]).exists()}
        src = d / "source.json"
        source = json.loads(src.read_text(encoding="utf-8")) if src.exists() else None
        return eln.build(name, f"Show, Don't Code run {name}", steps, gen, rec, source=source, card=card, card_images=imgs,
                         review=r.get("review") or [], app_url=_app_link(name, request),
                         created=eln._iso((d / "result.json").stat().st_ctime),
                         modified=eln._iso((d / "result.json").stat().st_mtime))

    body = _export(name, request, "eln", build)
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="show-dont-code-{name}.eln"'})


_elab_slots = threading.BoundedSemaphore(2)


@app.post("/api/runs/{name}/elabftw")
def send_to_elabftw(name: str, request: Request, url: str = Body(...), key: str = Body(...)):
    """Import the run's .eln into the user's own eLabFTW, as an experiment only its owner can read or edit. eLabFTW has
    no CORS, so this server forwards it; the key is used for this request only and never stored or logged."""
    from sdc import elab

    try:  # a bad address or key is refused before any .eln is built
        elab.check(url, key)
    except elab.Refused as e:
        raise HTTPException(400, str(e))
    eln = eln_file(name, request).body
    if not _elab_slots.acquire(timeout=30):
        raise HTTPException(429, "busy: try again in a minute")
    try:
        return elab.push(eln, f"show-dont-code-{name}.eln", f"Show, Don't Code run {name}", url, key)
    except elab.Refused as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 - the eLabFTW couldn't be reached or answered oddly: say so, without the key
        raise HTTPException(502, f"couldn't import into that eLabFTW: {type(e).__name__}: {str(e)[:200]}")
    finally:
        _elab_slots.release()


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
    spend_live_call()  # only now: junk bytes or a run with nothing to ask cost no model call
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
    _check_answers(_run_dir(name), answers)  # before _editable: a bad body must not make a copy or a rebuild
    with _editable(name) as d:
        _answer(d, answers)
    return result(d.name)


def _check_answers(d, answers):
    if not answers:
        raise HTTPException(422, "no answers given")
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    for k, v in answers.items():
        m = re.fullmatch(r"(\d+)(:cycles)?", k)
        if not m or int(m[1]) >= len(steps):
            raise HTTPException(422, f"unknown step {k!r}")
        if m[2] and not (1 <= v <= 99 and v == int(v)):
            raise HTTPException(422, "cycles must be a whole number from 1 to 99")
        if not m[2] and not protocol.MIN_UL <= v <= 1000:
            raise HTTPException(422, f"volumes must be {protocol.MIN_UL:g} to 1000 µL (the Flex pipettes' range)")
    return steps


def _answer(d, answers):
    old = json.loads((d / "answers.json").read_text()) if (d / "answers.json").exists() else {}
    steps = _check_answers(d, answers)
    with _rx_slots:  # simulator, opentrons analyze and two PyLabRobot runs: share the two slots with /reactions
        pipeline.build(d, steps, {**old, **answers}, review=REVIEW_ON_EDIT)


def _tubes(d):
    f = d / "tubes.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


@app.post("/api/runs/{name}/tubes")
def identify_tube(name: str, t: float = Body(...), x: float = Body(...), y: float = Body(...)):
    """Scientist clicked a tube at time t, (x, y) in the 640-px-wide SAM frame: track it, then ground its uses."""
    d = _run_dir(name)
    if not (d / "sam_file.json").exists():
        raise HTTPException(409, "this run has no SAM video (ground-truth replays can't track)")
    frame = pipeline.asset(d, "sam_frames") / f"{round(t * pipeline.SAM_FPS):05d}.jpg" if math.isfinite(t) and t >= 0 else None
    if not (frame and frame.exists()):  # checked before a live call is spent
        raise HTTPException(422, "no video frame at that time")
    from PIL import Image

    w, h = Image.open(frame).size  # portrait phone videos are 640 x 1138, not 640 x 360
    if not (0 <= x < w and 0 <= y < h):
        raise HTTPException(422, "click outside the frame")
    if not os.getenv("MODEL_API_KEY"):
        raise HTTPException(503, "live model calls are not configured on this deployment")
    spend_live_call()
    with _editable(name) as d:
        _track_tube(d, name, t, x, y, (w, h))
    return result(d.name)


def _track_tube(d, name, t, x, y, size):
    from sdc import meta

    frame = round(t * pipeline.SAM_FPS)
    fid = json.loads((d / "sam_file.json").read_text())
    steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
    try:
        track = {str(k): v for k, v in meta.track_point(fid, frame, x, y, size).items()}
        if not track.get(str(frame)):  # nothing to show Muse Spark: don't ask it about a tube that isn't outlined
            raise HTTPException(422, "SAM 3.1 found no object at that point; click the middle of the tube")
        uses = meta.steps_using(pipeline.asset(d, "sam_frames"), track, steps, pipeline.SAM_FPS, frame)
    except HTTPException:
        raise
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
    # Read-only on purpose: no tool runs code an agent sends (an Opentrons protocol is Python) Only crosscheck_volumes can spend model
    # calls: at most 3 per run and set of volumes, cached, inside the daily cap (spend_live_call).
    mcp = MCPServer("show-dont-code", instructions=(
        "Robot protocols recovered from first-person lab videos. list_runs, then get_run for the steps found in the video "
        "and the checks they passed, get_protocol for the Opentrons Flex Python file, get_protocol_designer for the same "
        "protocol as editable no-code steps, get_tube_labels for loading the tube rack, run_on_flex_robot_server to run the "
        "protocol on Opentrons' robot software, get_related_protocols and crosscheck_volumes for published protocols with "
        "the same kit and their volumes, get_bench_card for the illustrated steps, get_receipt for the hashes."))

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
    def run_on_flex_robot_server(name: str, reactions: int = 1) -> dict:
        """Run the run's protocol.py end to end on Opentrons' own robot-server (the software inside a Flex, v10.1.0) in
        Flex simulation, with the Reactions parameter set: the robot's analysis, deck configuration, play to the end.
        Returns the robot's report (status 'succeeded', command count). About a minute the first time, then cached.
        Runs only the protocol this server generated, never code an agent sends."""
        return _http(flex_robot_run, name, reactions)

    @mcp.tool()
    def get_related_protocols(name: str) -> dict:
        """Public protocols on protocols.io that use this run's reagents (brand and kit names) and protocol type, with
        DOIs and which reagent words each mentions: how other labs wrote up the same kit."""
        return _http(related_protocols, name)

    @mcp.tool()
    def get_bench_card(name: str) -> dict:
        """The illustrated bench card for training: each filmed step drawn by Muse Image from its own video frame (an
        edit of the real frame, no text drawn in), Muse Spark's check that the drawing kept every object, and the
        step's reagent and volume from the protocol. Image links are under /api/runs/<name>/bench-card/<step>.jpg."""
        card = _http(bench_card, name)
        if not isinstance(card, dict):  # 202: an upload's card is still being drawn
            raise ToolError("the bench card is still being drawn; try again in a minute")
        steps = _http(result, name)["steps"]
        base = f"{os.getenv('PUBLIC_URL', '')}/api/runs/{name}/bench-card"
        return {**card, "steps": [{**c, "image": f"{base}/{c['step']}.jpg", **{k: steps[c["step"]].get(k) for k in (
            "description", "reagent", "volume_ul", "destination")}} for c in card["steps"]
            if c["step"] < len(steps) and steps[c["step"]].get("action") != "skipped"]}

    @mcp.tool()
    def crosscheck_volumes(name: str) -> dict:
        """This run's per-reaction volumes next to those of published protocols.io protocols that use the same kit, read by
        Muse Spark: each match has the published volume, its verbatim quote (checked to be in the protocol's text), both
        reagents' share of the reaction and whether they agree within 20 %."""
        return _http(related_crosscheck, name, True)

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
