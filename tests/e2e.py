"""End-to-end QA in a real Chrome via Playwright: landing, every sample run, the confirm->simulate->replay flow,
every download (the receipt's hash must match protocol.py byte for byte), and the security edges.

  python tests/e2e.py                                   # against http://127.0.0.1:8000 (python app.py)
  BASE=https://show-dont-code.vercel.app python tests/e2e.py
  VOICE=1 ... python tests/e2e.py                       # also speak answers through a fake microphone (costs API)
  FLEX=127.0.0.1 ... python tests/e2e.py                # also send pcr_1-gt to a robot-server and run it (scripts/send_to_flex.py)

Nothing here costs model calls unless VOICE=1. Its edits land in private copies (c-*); the shared sample runs never change.
"""
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from concurrent.futures import ThreadPoolExecutor

from mcp import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sdc import designer, export, protocol, stamp  # noqa: E402  (the repo root, for the Protocol Designer round trip)
from playwright.sync_api import sync_playwright


async def mcp_session(url):
    """What an outside agent sees over MCP: tool names, a run's protocol, and an unknown run's error."""
    async with Client(url) as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        proto = (await c.call_tool("get_protocol", {"name": "pcr_1-gt"})).content[0].text
        run = json.loads((await c.call_tool("get_run", {"name": "pcr_1-gt"})).content[0].text)
        bad = await c.call_tool("get_run", {"name": "no-such-run"})
        pd = json.loads((await c.call_tool("get_protocol_designer", {"name": "pcr_1-gt"})).content[0].text)
        lab = json.loads((await c.call_tool("get_tube_labels", {"name": "pcr_1-gt"})).content[0].text)
        lab8 = json.loads((await c.call_tool("get_tube_labels", {"name": "pcr_1-gt", "reactions": 8})).content[0].text)
        return tools, proto, run, (bad.is_error, bad.content[0].text), pd, (lab, lab8)

BASE = os.getenv("BASE", "http://127.0.0.1:8000").rstrip("/")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  -- ' + str(detail) if detail and not cond else ''}", flush=True)


def fetch(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        page = b.new_page(viewport={"width": 1440, "height": 900})
        errors, bad = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: r.status >= 400 and "favicon" not in r.url and bad.append(f"{r.status} {r.url}"))
        # anything the Content-Security-Policy blocks (a script, font, video or API call it forgot) lands here
        page.add_init_script("document.addEventListener('securitypolicyviolation', "
                             "e => (window.__csp = window.__csp || []).push(e.violatedDirective + ' ' + e.blockedURI))")
        csp_seen = []

        # ---- landing --------------------------------------------------------------------------------
        page.goto(BASE + "/")
        page.wait_for_timeout(2500)
        check("landing: headline", "Pipette it once" in page.inner_text("h1"))
        csp_seen += page.evaluate("() => window.__csp || []")
        for path in ("/", "/app"):
            with urllib.request.urlopen(BASE + path, timeout=60) as r:
                h = r.headers
            csp = h.get("Content-Security-Policy", "")
            check(f"security: {path} has a nonce-only script CSP and nosniff",
                  "script-src 'nonce-" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
                  and "frame-ancestors 'none'" in csp and h.get("X-Content-Type-Options") == "nosniff", csp)
        with urllib.request.urlopen(BASE + "/app", timeout=60) as r:
            html = r.read().decode()
        check("security: no script runs without the nonce", html.count("<script") == html.count("<script nonce="), html.count("<script"))
        check("landing: hero video plays", page.evaluate("() => { const v = document.getElementById('hero'); return !v.paused && v.readyState >= 2; }"))
        page.click("#toggle")
        check("landing: pause control works", page.evaluate("() => document.getElementById('hero').paused"))
        check("landing: every image/video has a text alternative",
              page.evaluate("() => [...document.querySelectorAll('img,video')].every(e => e.getAttribute('alt') !== null || e.getAttribute('aria-label'))"))
        og = page.evaluate("() => ({img: document.querySelector('meta[property=\"og:image\"]')?.content, card: document.querySelector('meta[name=\"twitter:card\"]')?.content})")
        check("landing: X/OG card uses an absolute 1200x630 image", (og["img"] or "").startswith("https://") and og["card"] == "summary_large_image", og)
        st, _ = fetch(og["img"] or BASE + "/media/og.jpg")
        check("landing: the card image is reachable", st == 200, st)
        check("landing: every button and link has an accessible name",
              page.evaluate("() => [...document.querySelectorAll('a,button')].every(e => (e.innerText || e.getAttribute('aria-label') || '').trim())"))
        mobile = b.new_page(viewport={"width": 360, "height": 780})
        mobile.goto(BASE + "/")
        mobile.wait_for_timeout(2000)
        check("landing: no horizontal scroll at 360 px", mobile.evaluate("() => document.documentElement.scrollWidth <= innerWidth"))
        mobile.goto(BASE + "/app?run=pcr_3")
        mobile.wait_for_function("() => document.querySelectorAll('#run option').length > 0", timeout=60000)
        check("app: no horizontal scroll at 360 px", mobile.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1"))
        mobile.goto(BASE + "/app?run=pcr_1-gt")
        mobile.wait_for_selector("#setupbox", state="visible", timeout=60000)
        small = mobile.evaluate("""() => [...document.querySelectorAll('button, select, input, .files a, [role=button]')]
          .filter(e => { const b = e.getBoundingClientRect(); return b.width && b.height && (b.height < 24 || b.width < 24); })
          .map(e => e.id || e.textContent.trim().slice(0, 20))""")
        check("app: every tap target is at least 24 px at 360 px (WCAG 2.2)", not small, small)
        # layout stability: panels and the video must not push content around while a run loads (CLS, Core Web Vital)
        for vw, vh in ((412, 823), (1440, 900)):
            lp = b.new_page(viewport={"width": vw, "height": vh})
            lp.add_init_script("window.__cls = 0; new PerformanceObserver((l) => { for (const e of l.getEntries()) "
                               "if (!e.hadRecentInput) window.__cls += e.value; }).observe({type: 'layout-shift', buffered: true})")
            for run in ("pcr_3", "pcr_1-gt"):
                lp.goto(f"{BASE}/app?run={run}")
                lp.wait_for_function("() => document.querySelectorAll('#steps li').length > 0", timeout=60000)
                lp.wait_for_timeout(2500)
                cls = lp.evaluate("() => window.__cls")
                check(f"perf: layout shift while {run} loads at {vw} px under 0.1 (Core Web Vitals 'good')", cls < 0.1, round(cls, 3))
            lp.close()
        mobile.close()

        # ---- every sample run ----------------------------------------------------------------------
        page.goto(BASE + "/app")
        page.wait_for_function("() => document.querySelectorAll('#run option').length > 0", timeout=60000)
        runs = page.eval_on_selector_all("#run option", "o => o.map(x => x.value)")
        check("app: lists the sample runs", len(runs) >= 6, runs)
        check("app: private uploads are not listed", not any(r.startswith("u-") for r in runs), runs)
        for run in runs:
            page.select_option("#run", run)
            page.wait_for_function("() => document.getElementById('status').textContent !== 'loading…'", timeout=60000)
            page.wait_for_timeout(1200)
            n_steps = len(page.query_selector_all("#steps li"))
            src = page.evaluate("() => document.getElementById('video').currentSrc")
            wells = len(page.query_selector_all("#deck .well"))
            check(f"run {run}: steps, video, deck", n_steps > 0 and src and wells > 0, (n_steps, src[-40:], wells))
        page.goto(BASE + "/app?run=pcr_3")
        page.wait_for_function("() => document.querySelectorAll('#steps li').length > 0", timeout=60000)
        page.wait_for_function("() => document.getElementById('video').readyState >= 1", timeout=60000)
        labels = page.eval_on_selector_all("#run option", "o => o.map(x => x.textContent)")
        check("app: runs have readable labels", any("PCR setup" in x for x in labels) and any("human annotations" in x for x in labels), labels)
        page.focus("#steps li:nth-child(3)")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        check("app: steps work from the keyboard", page.evaluate("() => document.getElementById('video').currentTime") > 30)
        sam_tags = page.eval_on_selector_all("#steps small.sam", "xs => xs.length")
        check("app: steps show SAM 3.1 tip evidence", sam_tags > 0, sam_tags)
        page.click("#steps li:nth-child(5)")
        page.wait_for_timeout(1500)
        t = page.evaluate("() => document.getElementById('video').currentTime")
        check("app: clicking a step seeks the video", t > 50, t)
        page.evaluate("() => document.getElementById('video').pause()")
        lit = page.evaluate("() => document.querySelectorAll('#deck .well.src, #deck .well.dst').length")
        check("app: deck lights the step's source and destination", lit >= 1, lit)

        # ---- confirm -> simulator -> replay -> downloads (pcr_1-gt, idempotent answers) ----------------
        api = page.evaluate("() => window.API_BASE || ''") or BASE
        answers = {"0": 20, "1": 1.5, "2": 1.5, "3": 2, "4": 25, "5:cycles": 35}
        st, body = fetch(f"{api}/api/runs/pcr_1-gt/answers", "POST", json.dumps(answers).encode(), {"Content-Type": "application/json"})
        res = json.loads(body) if st == 200 else {}
        check("api: answers -> simulator passes", st == 200 and res.get("sim_ok"), (st, body[:120]))
        check("api: Opentrons analysis passes", res.get("analysis_ok") is True, res.get("analysis", {}).get("errors"))
        check("api: PyLabRobot STARlet simulation passes", res.get("plr_ok") is True, res.get("plr"))
        check("api: PyLabRobot Tecan Freedom EVO 150 simulation passes, same aspirate/dispense count as the STARlet",
              res.get("evo_ok") is True and res.get("evo", {}).get("actions") == res.get("plr", {}).get("actions"), res.get("evo"))
        page.goto(BASE + "/app?run=pcr_1-gt")
        page.wait_for_selector("#replay", state="visible", timeout=60000)
        page.click("#replay")
        page.wait_for_timeout(3000)
        cap = page.inner_text("#replaycap")
        check("app: robot replay runs the simulator's commands", "Robot command" in cap, cap)
        step = page.inner_text("#replaystep")
        check("app: replay follows the protocol's ctx.comment steps in the video", step.startswith("Step ") and "video" in step, step)
        check("app: replay moved the human video to that step", page.evaluate("() => document.getElementById('video').currentTime") > 0)
        page.click("#replay")
        check("app: liquid setup lists what to load", len(page.query_selector_all("#setup tr")) >= 3)
        st, proto = fetch(f"{api}/api/runs/pcr_1-gt/protocol.py")
        check("download: protocol.py", st == 200 and b"def run(ctx)" in proto and b"load_liquid" in proto)
        check("protocol: thermal program is Opentrons runtime parameters",
              b"def add_parameters(parameters)" in proto and b"repetitions=ctx.params.cycles" in proto)
        check("app: runtime parameters listed before the run", len(page.query_selector_all("#params tr")) >= 3,
              len(page.query_selector_all("#params tr")))
        st, an = fetch(f"{api}/api/runs/pcr_1-gt/analysis.json")
        check("analysis: Opentrons App sees the runtime parameters", st == 200 and b'"variableName":"cycles"' in an.replace(b" ", b""))
        st, rec = fetch(f"{api}/api/runs/pcr_1-gt/receipt.json")
        rec = json.loads(rec) if st == 200 else {}
        check("download: receipt hash matches protocol.py byte for byte",
              rec.get("protocol_sha256") == hashlib.sha256(proto).hexdigest(), rec.get("protocol_sha256"))
        check("receipt: replay runs do not claim models read them", "note" in rec.get("models", {}), rec.get("models"))
        ts = rec.get("timestamp", {})
        body = {k: v for k, v in rec.items() if k != "timestamp"}
        check("receipt: OpenTimestamps digest recomputes from the receipt itself",
              ts.get("stamped_sha256") == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), ts)
        st, ots = fetch(f"{api}/api/runs/pcr_1-gt/receipt.ots")
        try:  # parse the proof here and check it independently: digest inside it, and its calendars or Bitcoin block
            o = stamp.status(ots) if st == 200 else {}
        except Exception as e:  # noqa: BLE001
            o = {"error": repr(e)}
        check("download: receipt.ots is a valid OpenTimestamps proof for that digest, on public calendars or in a Bitcoin block",
              o.get("digest") == ts.get("stamped_sha256") and (o.get("bitcoin") or len(o.get("calendars", [])) >= 2)
              and o.get("bitcoin") == ts.get("bitcoin"), (st, o))
        check("app: the Bitcoin timestamp is shown next to the receipt",
              page.wait_for_function("() => /OpenTimestamps/.test(document.getElementById('otsline').textContent)", timeout=60000)
              and page.is_visible("#f-ots"), page.inner_text("#otsline")[:120])
        check("protocol: every transfer uses an Opentrons liquid class, premix pipetted as viscous",
              b".transfer(" not in proto and b"transfer_with_liquid_class(" in proto and b"default='glycerol_50'" in proto)
        check("app: liquid classes listed as runtime parameters", "Viscous (50% glycerol)" in page.inner_text("#params"))
        cyc = re.search(r"thermocycler needs at least (?:(\d+) h )?(\d+) min", page.inner_text("#analysisline"))
        check("app: thermocycler time from the analysed commands (35 cycles 98/55/72 -> about 31 min)",
              cyc and 25 <= int(cyc[1] or 0) * 60 + int(cyc[2]) <= 40, page.inner_text("#analysisline")[-200:])
        check("protocol: pipette sensor liquid check is a runtime switch, off by default",
              b"variable_name='detect_liquid'" in proto and b"require_liquid_presence(" in proto)
        check("analysis: the App offers the liquid check", b'"variableName":"detect_liquid"' in an.replace(b" ", b"") if st == 200 else False)
        st, z = fetch(f"{api}/api/runs/pcr_1-gt/ro-crate.zip")
        crate = zipfile.ZipFile(io.BytesIO(z)) if st == 200 else None
        meta = json.loads(crate.read("ro-crate-metadata.json")) if crate else {"@graph": []}
        ents = {e["@id"]: e for e in meta["@graph"]}
        check("download: RO-Crate pins protocol.py by SHA-256",
              ents.get("protocol.py", {}).get("sha256") == hashlib.sha256(proto).hexdigest() == hashlib.sha256(crate.read("protocol.py")).hexdigest(),
              (st, list(ents)[:6]))
        check("RO-Crate: declares Process Run Crate and the actions that made each file",
              ents.get("./", {}).get("conformsTo", {}).get("@id") == "https://w3id.org/ro/wfrun/process/0.5"
              and {"#generate", "#analyze"} <= set(ents))
        check("RO-Crate: carries the timestamp proof and the action that made it",
              ents.get("receipt.ots", {}).get("sha256") == hashlib.sha256(ots).hexdigest()
              and ents.get("#timestamp", {}).get("result", {}).get("@id") == "receipt.ots", ents.get("#timestamp"))
        try:
            from rocrate_validator import models as rv_models, services as rv
        except ImportError:  # dev-only dependency
            rv = None
        if rv and crate:
            logging.getLogger("rdflib").setLevel(logging.ERROR)  # Windows temp paths make it warn about file:// URIs
            with tempfile.TemporaryDirectory() as tmp:
                crate.extractall(tmp)
                v = rv.validate(rv.ValidationSettings(rocrate_uri=tmp, profile_identifier="process-run-crate",
                                                      requirement_severity=rv_models.Severity.REQUIRED, skip_availability_check=True))
                check("RO-Crate: passes rocrate-validator (process-run-crate, REQUIRED)", v.passed(),
                      [i.message[:90] for i in v.get_issues()][:3])
        for path, needle in (("analysis.json", b'"liquids"'), ("methods.md", b"## Procedure"), ("autoprotocol.json", b'"thermocycle"'),
                             ("pylabrobot.py", b"set_volume_tracking(True)"), ("pylabrobot.py", b"# glycerol_50: Opentrons liquid class rates")):
            st, data = fetch(f"{api}/api/runs/pcr_1-gt/{path}")
            check(f"download: {path}", st == 200 and needle in data, (st, data[:80]))
        st, _ = fetch(f"{api}/api/runs/pcr_3/protocol.py")
        check("download: protocol refused while questions are open", st == 409, st)

        st, evo = fetch(f"{api}/api/runs/pcr_1-gt/tecan-evo.py")
        ok, elog = export.run_pylabrobot(evo.decode("utf-8")) if st == 200 else (False, {})
        st, plr = fetch(f"{api}/api/runs/pcr_1-gt/pylabrobot.py")
        ok2, plog = export.run_pylabrobot(plr.decode("utf-8")) if st == 200 else (False, {})
        check("download: tecan-evo.py runs in PyLabRobot here, EVO deck and LiHa tips, as many pipetting actions as the STARlet",
              ok and ok2 and b"EVO150Deck()" in evo and b"_LiHa" in evo and elog["actions"] == plog["actions"] > 0, (elog, plog.get("actions")))
        check("RO-Crate: carries the Tecan EVO translation", crate and "tecan-evo.py" in crate.namelist())
        check("RO-Crate: carries the tube labels (Avery SVG and Zebra ZPL), made by the code generator",
              crate and {"labels.svg", "labels.zpl"} <= set(crate.namelist())
              and {"labels.svg", "labels.zpl"} <= {r["@id"] for r in ents.get("#generate", {}).get("result", [])})
        page.goto(BASE + "/app?run=pcr_1-gt")
        page.wait_for_function("() => /Tecan Freedom EVO 150/.test(document.getElementById('plrline').textContent)", timeout=60000)
        check("app: says the protocol also ran on a Hamilton STARlet and a Tecan EVO",
              "Hamilton STARlet" in page.inner_text("#plrline") and page.is_visible("#f-evo"), page.inner_text("#plrline"))

        # ---- the same simulations re-run in the visitor's browser (Pyodide: CPython in WebAssembly) ------------
        done = "() => !document.getElementById('here-evo').disabled && /: (?!loading|running)/.test(document.getElementById('plrhere').textContent)"
        page.click("#here-evo")
        page.wait_for_function(done, timeout=300000)
        here = re.search(r"no errors: (\d+) aspirate/dispense actions \(the server counted (\d+)\)", page.inner_text("#plrhere"))
        check("browser: tecan-evo.py re-runs in Pyodide with PyLabRobot from PyPI, as many actions as the server counted",
              here and here[1] == here[2] == str(elog["actions"]), page.inner_text("#plrhere")[:200])
        # a tube that holds less than the protocol draws: the in-browser run must fail, not echo the server's verdict
        page.route("**/pcr_1-gt/pylabrobot.py", lambda r: r.fulfill(
            body=plr.decode("utf-8").replace("tracker.set_volume(57.5)", "tracker.set_volume(5.0)"), content_type="text/x-python"))
        page.click("#here-plr")
        page.wait_for_function(done, timeout=300000)
        check("browser: the in-browser run catches an over-drawn tube (premix stock cut to 5 µL)",
              "no errors" not in page.inner_text("#plrhere") and re.search(r"(?i)not enough|volume", page.inner_text("#plrhere")),
              page.inner_text("#plrhere")[-200:])
        page.unroute("**/pcr_1-gt/pylabrobot.py")

        # ---- Opentrons Protocol Designer: the same protocol as editable no-code steps ------------------------
        st, pdf = fetch(f"{api}/api/runs/pcr_1-gt/protocol-designer.json")
        pdj = json.loads(pdf) if st == 200 else {}
        forms = [pdj["designerApplication"]["data"]["savedStepForms"][i] for i in pdj["designerApplication"]["data"]["orderedStepIds"]] if pdj else []
        xfers = [f for f in forms if f["stepType"] == "moveLiquid"]
        check("protocol designer: one editable transfer per pipetting step, named after its moment in the video",
              len(xfers) >= 3 and all("video" in f["stepDetails"] for f in xfers), [(f["stepName"], f["stepDetails"]) for f in xfers][:3])
        check("RO-Crate: carries the Protocol Designer file and the action that made it",
              crate and "protocol-designer.json" in crate.namelist() and "#designer" in ents)
        an_cmds = json.loads(an)["commands"] if an else []
        with tempfile.TemporaryDirectory() as tmp:
            ok, pda = protocol.analyze(pdf.decode("utf-8"), os.path.join(tmp, "pd.json"), "protocol.json")
            pd_cmds = json.loads(open(os.path.join(tmp, "pd.json"), encoding="utf-8").read())["commands"] if ok else []
        check("protocol designer: Opentrons analyses the file with the same volumes and cycling time as protocol.py",
              ok and designer.moved(pd_cmds) == designer.moved(an_cmds) and pda["cycling_s"] == protocol.block_time(an_cmds), pda.get("errors"))
        # film one reaction, run a plate: the Reactions runtime parameter, analysed by Opentrons at the deck's maximum
        top = json.loads(fetch(f"{api}/api/runs/pcr_1-gt")[1])["max_reactions"]
        with tempfile.TemporaryDirectory() as tmp:
            ok, rx = protocol.analyze(proto.decode("utf-8"), os.path.join(tmp, "rx.json"), rtp={"reactions": top})
            rxa = json.loads(open(os.path.join(tmp, "rx.json"), encoding="utf-8").read()) if ok else {"commands": [], "labware": []}
        plate = {lw["id"] for lw in rxa["labware"] if lw["loadName"] == protocol.PCR_PLATE}
        rx_wells = {c["params"]["wellName"] for c in rxa["commands"] if c["params"].get("labwareId") in plate
                    and c["commandType"] in ("dispense", "dispenseInPlace", "moveToWell")}
        one_tips = sum(c["commandType"] == "pickUpTip" for c in an_cmds)
        check("reactions: protocol.py asks for Reactions at run setup; Opentrons analyses the most the deck allows, "
              "each copy in its own plate well, with tubes filled for all of them",
              top > 1 and ok and len(rx_wells) == top and sum(c["commandType"] == "pickUpTip" for c in rxa["commands"]) == one_tips * top
              and any(q["name"] == "Reactions" for q in rx.get("parameters", []))
              and all(q["volume_ul"] > 50 for q in rx["liquid_setup"]), (top, len(rx_wells), rx.get("errors")))
        st8, svg8 = fetch(f"{api}/api/runs/pcr_1-gt/labels.svg?reactions=8")
        stbad, _ = fetch(f"{api}/api/runs/pcr_1-gt/labels.svg?reactions={top + 1}")
        check("reactions: labels for 8 reactions say how much to fill for 8, and more than the deck allows is refused",
              st8 == 200 and "(8 × " in svg8.decode() and stbad == 422, (st8, stbad))
        # the real editor: import, every step builds a robot state, then export (Protocol Designer 9 writes Python)
        pdp = b.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        warns = []
        pdp.on("console", lambda m: "No robot state" in m.text and warns.append(m.text))
        pdp.on("dialog", lambda d: d.accept())
        try:
            pdp.goto("https://designer.opentrons.com/", wait_until="networkidle", timeout=120000)
            with tempfile.TemporaryDirectory() as tmp:
                f = os.path.join(tmp, "pcr_1-gt-protocol-designer.json")
                open(f, "wb").write(pdf)
                pdp.set_input_files("input[type=file] >> nth=0", f)
                pdp.get_by_text("Your protocol was made in an older version").wait_for(timeout=30000)
                next(x for x in pdp.query_selector_all("button") if x.inner_text().strip() == "Import" and x.bounding_box()["y"] > 100).click()
                pdp.get_by_role("button", name="Export protocol").wait_for(timeout=60000)
                pdp.goto("https://designer.opentrons.com/#/designer")  # the timeline (a toast can cover "Edit protocol")
                pdp.get_by_text(xfers[0]["stepName"], exact=False).first.wait_for(timeout=30000)
                timeline = pdp.inner_text("body")
                check("protocol designer (designer.opentrons.com): imports every step with a valid robot state",
                      not warns and all(x["stepName"].lower() in timeline.lower() for x in xfers), (warns[:1], timeline[:200]))
                pdp.goto("https://designer.opentrons.com/#/overview")
                if pdp.get_by_role("button", name="Confirm").count():  # the editor's analytics notice, in a throwaway browser
                    pdp.get_by_role("button", name="Confirm").click()
                pdp.get_by_role("button", name="Export protocol").click()
                with pdp.expect_download(timeout=30000) as dl:
                    [x for x in pdp.query_selector_all("button") if x.is_visible() and x.inner_text().strip() in ("Export", "Continue with export", "Export protocol")][-1].click()
                out = os.path.join(tmp, "exported.py")
                dl.value.save_as(out)
                ok, ex = protocol.analyze(open(out, encoding="utf-8").read(), os.path.join(tmp, "ex.json"))
                ex_cmds = json.loads(open(os.path.join(tmp, "ex.json"), encoding="utf-8").read())["commands"] if ok else []
                vols = lambda cs: [v for k, v in designer.moved(cs) if k == "aspirate" and v > 0.5]  # not air gaps
                check("protocol designer: its own export runs in Opentrons analysis with the same volumes",
                      ok and vols(ex_cmds) == vols(an_cmds), (ex.get("errors"), vols(ex_cmds), vols(an_cmds)))
        except Exception as e:  # a third-party site: report what broke, keep the rest of the suite running
            check("protocol designer (designer.opentrons.com): round trip", False, repr(e)[:200])
        pdp.close()

        # ---- reviewer agent: one-click fixes ------------------------------------------------------------
        fix_runs = []
        for run in ("pcr_4", "pcr_5", "pcr_2", "pcr_3", "miniprep_7", "pcr_1"):
            st, body = fetch(f"{api}/api/runs/{run}")
            if st == 200 and any((i.get("fix") or {}).get("kind", "none") != "none" for i in json.loads(body).get("review") or []):
                fix_runs.append(run)
        check("reviewer: at least one run carries a one-click fix", fix_runs, fix_runs)
        if fix_runs:
            page.goto(f"{BASE}/app?run={fix_runs[0]}")
            page.wait_for_function("() => document.querySelectorAll('#issues li').length > 0", timeout=60000)
            check("app: fix buttons render", len(page.query_selector_all("#issues button.fix, #issues em.applied")) > 0)
        st, _ = fetch(f"{api}/api/runs/pcr_1-gt/review/999/apply", "POST")
        check("api: applying a fix that doesn't exist -> 404", st == 404, st)
        if fix_runs:  # on a shared sample, so this also proves the fix lands in a private copy
            before = json.loads(fetch(f"{api}/api/runs/{fix_runs[0]}")[1])
            k = next(i for i, it in enumerate(before["review"]) if it["fix"]["kind"] != "none" and not it.get("applied"))
            fix = before["review"][k]["fix"]
            st, body = fetch(f"{api}/api/runs/{fix_runs[0]}/review/{k}/apply", "POST")
            after = json.loads(body) if st == 200 else {}
            copy = after.get("run", "")
            changed = st == 200 and all(
                (fix["kind"] == "skip_steps" and after["steps"][i]["action"] == "skipped")
                or (fix["kind"] == "set_reagent" and after["steps"][i]["reagent"] == fix["value"])
                or (fix["kind"] == "set_volume" and after["steps"][i]["volume_ul"] == float(fix["value"])) for i in fix["steps"])
            check(f"api: applying a {fix['kind']} fix changes exactly its steps", changed and after["review"][k].get("applied"), (st, body[:120]))
            check("api: the fix went to a private copy, the sample is unchanged",
                  re.fullmatch(r"c-[\w-]{16}", copy) and json.loads(fetch(f"{api}/api/runs/{fix_runs[0]}")[1])["review"] == before["review"], copy)
            st, _ = fetch(f"{api}/api/runs/{copy}/review/{k}/apply", "POST")
            check("api: applying the same fix twice -> 409", st == 409, st)

        # ---- edits from the page: a visitor's answers never change the shared sample -------------------
        sample = json.loads(fetch(f"{api}/api/runs/pcr_1")[1])
        page.goto(BASE + "/app?run=pcr_1")
        page.wait_for_selector("#qform input", timeout=60000)
        check("app: the question box says answers go to a private copy", page.is_visible("#copynote"))
        for inp in page.query_selector_all("#qform input"):
            inp.fill("30" if ":cycles" in (inp.get_attribute("name") or "") else "7.5")
        page.click("#qform button[type=submit]")
        page.wait_for_function("() => /[?&]run=c-/.test(location.search)", timeout=180000)
        copy = re.search(r"run=(c-[\w-]{16})", page.url)
        copy = copy and copy[1]
        check("app: confirming on a sample opens a private copy with its own link", copy and page.input_value("#run") == copy, page.url)
        check("app: the copy's protocol is complete", page.wait_for_function("() => !document.getElementById('dl').disabled", timeout=60000))
        check("api: the shared sample keeps its open questions", json.loads(fetch(f"{api}/api/runs/pcr_1")[1])["questions"] == sample["questions"])
        check("api: copies stay out of the run list", copy not in json.loads(fetch(f"{api}/api/runs")[1]))
        rc = json.loads(fetch(f"{api}/api/runs/{copy}/receipt.json")[1])
        check("api: the copy shares the sample's footage (same video hash, no 13 MB duplicate)",
              rc["files_sha256"].get("video.mp4") == hashlib.sha256(fetch(f"{api}/api/runs/pcr_1/video")[1]).hexdigest())
        check("api: the edited copy gets its own Bitcoin timestamp, for its own answers",
              rc.get("timestamp", {}).get("stamped_sha256") not in (None, ts.get("stamped_sha256"))
              and rc["timestamp"].get("pending_calendars") and rc["human_input"]["answers"], rc.get("timestamp"))
        cz = zipfile.ZipFile(io.BytesIO(fetch(f"{api}/api/runs/{copy}/ro-crate.zip")[1]))
        cents = {e["@id"]: e for e in json.loads(cz.read("ro-crate-metadata.json"))["@graph"]}
        check("RO-Crate: an edited copy is not credited to the author, but still cites BioVL2",
              "author" not in cents["./"] and all("agent" not in e for e in cents.values())
              and cents["./"].get("isBasedOn"), cents["./"])
        page.goto(page.url)
        page.wait_for_selector("#steps li", timeout=60000)
        check("app: the copy's link reopens it after a reload", page.input_value("#run") == copy and page.is_hidden("#qbox"))

        # ---- input validation and security edges ----------------------------------------------------
        for bad_answers, why in (({"0": -5}, "negative volume"), ({"99": 5}, "unknown step"), ({"5:cycles": 2.5}, "fractional cycles"),
                                 ({"0; rm": 5}, "malformed key")):
            st, _ = fetch(f"{api}/api/runs/pcr_1-gt/answers", "POST", json.dumps(bad_answers).encode(), {"Content-Type": "application/json"})
            check(f"api: rejects {why}", st == 422, st)
        for name in ("..%2F..%2Fetc", "pcr_1%00", "nope"):
            st, _ = fetch(f"{api}/api/runs/{name}")
            check(f"api: unknown run {name} -> 404", st == 404, st)
        st, _ = fetch(f"{api}/api/upload", "POST", b"x" * 2000, {"X-Upload-Code": "wrong"})
        # 403 from the upload route itself; a 404 here once meant the route was shadowed and uploads were down
        hosted = not BASE.startswith("http://127.0.0.1")  # the upload routes exist only on the Modal backend
        check("api: upload with a wrong code is refused", st == 403 if hosted else st in (403, 404, 405), st)
        if hosted:
            st, body = fetch(f"{api}/api/runs/nope/progress")
            check("api: the upload progress route answers (its own 404, not the catch-all's)",
                  st == 404 and body.startswith(b'{"detail":"unknown run"'), (st, body[:80]))
        st, _ = fetch(f"{api}/api/runs/pcr_3/voice", "POST", os.urandom(5000))
        check("api: voice refuses non-audio", st in (415, 503, 404, 405), st)
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.goto(BASE + '/app?run=u-%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E')
        page.wait_for_function("() => document.querySelectorAll('#run option').length > 0", timeout=60000)
        page.wait_for_timeout(1500)
        check("app: script in ?run= does not execute", not dialogs, dialogs)

        # ---- upload dialog -------------------------------------------------------------------------
        if page.is_visible("#upbtn"):
            page.click("#upbtn")
            check("upload: dialog opens", page.is_visible("#updlg"))
            page.keyboard.press("Escape")
            check("upload: Escape closes it", not page.is_visible("#updlg"))

        # ---- record with the camera (Chromium's fake camera and microphone) -----------------------
        policy = urllib.request.urlopen(BASE + "/app", timeout=60).headers.get("Permissions-Policy", "")
        check("headers: the page may use its own camera, nothing else may", "camera=(self)" in policy, policy)
        cb = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        cp = cb.new_context(permissions=["camera", "microphone"]).new_page()
        cp.goto(BASE + "/app?run=pcr_1-gt")
        cp.wait_for_selector("#steps li", timeout=60000)
        if cp.is_visible("#upbtn"):
            cp.click("#upbtn")
            cp.click("#recbtn")
            cp.wait_for_timeout(7000)
            check("record: live camera preview with a running clock", cp.is_visible("#recview") and cp.inner_text("#rectime").startswith("0:0"),
                  cp.inner_text("#rectime"))
            cp.click("#recbtn")
            cp.wait_for_function("() => document.getElementById('upform').video.files.length === 1", timeout=15000)
            clip = base64.b64decode(cp.evaluate("""async () => { const f = document.getElementById('upform').video.files[0];
                const b = new Uint8Array(await f.arrayBuffer()); let s = ''; for (const x of b) s += String.fromCharCode(x); return btoa(s); }"""))
            with tempfile.TemporaryDirectory() as tmp:  # the upload endpoint's own ffmpeg step, on the recorded file
                raw, mp4 = os.path.join(tmp, "raw"), os.path.join(tmp, "input.mp4")
                open(raw, "wb").write(clip)
                cut = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-t", "300", "-c", "copy", mp4], capture_output=True)
                dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp4],
                                     capture_output=True, text=True).stdout.strip()
            check("record: the recording becomes the upload file, and the server's remux reads it (>= 5 s)",
                  cut.returncode == 0 and float(dur or 0) >= 5, (len(clip), dur, cut.stderr[:120]))
            cp.fill("#upform input[name=code]", "wrong-code")
            cp.click("#upgo")
            cp.wait_for_function("() => /access code/.test(document.getElementById('upmsg').textContent)", timeout=60000)
            check("record: the recorded clip goes through the real upload endpoint (wrong code refused, nothing stored)",
                  "access code isn't right" in cp.inner_text("#upmsg"), cp.inner_text("#upmsg"))
        cb.close()

        # ---- tube labels: print, stick on, scan each tube into the rack ------------------------------
        if hosted:  # the front end's /api answered from a read-only bundle (no footage, re-stamped forever) until Oct 29
            with urllib.request.urlopen(BASE + "/api/runs/pcr_1-gt/receipt.json", timeout=120) as r:
                landed, rr = r.url, json.load(r)
            check("api: the front end's /api goes to the backend, whose receipt hashes the footage",
                  landed.startswith(api) and "video.mp4" in rr["files_sha256"], (landed, list(rr["files_sha256"])))
        req = urllib.request.urlopen(f"{api}/api/runs/pcr_1-gt/labels.svg", timeout=120)
        svg, lh = req.read().decode(), req.headers
        layout = json.loads(fetch(f"{api}/api/runs/pcr_1-gt")[1])["layout"]
        rack = [n for n, loc in layout.items() if loc.startswith("reagents")]
        check("labels: one QR label per tube in the rack, served as an inert SVG",
              lh.get_content_type() == "image/svg+xml" and "default-src 'none'" in lh.get("Content-Security-Policy", "")
              and svg.count("<path") == len(rack) and "<script" not in svg, (len(rack), svg.count("<path")))
        sheet = os.path.abspath("out/rec")
        os.makedirs(sheet, exist_ok=True)
        lb = p.chromium.launch()
        lp = lb.new_page(device_scale_factor=4, viewport={"width": 800, "height": 1130})
        lp.goto(f"{api}/api/runs/pcr_1-gt/labels.svg")
        mm = 96 / 25.4  # the second label on the sheet (Avery L7160: 3 across), photographed as a camera would see it
        lp.screenshot(path=f"{sheet}/label.png", clip={"x": (7.2 + 66.04) * mm, "y": 15.15 * mm, "width": 63.5 * mm, "height": 38.1 * mm})
        lb.close()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", f"{sheet}/label.png", "-t", "4", "-r", "10", "-vf",
                        "scale=600:-2,pad=640:480:(ow-iw)/2:(oh-ih)/2:white", "-pix_fmt", "yuv420p", f"{sheet}/label.y4m"], check=True)
        sb = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                     f"--use-file-for-fake-video-capture={sheet}/label.y4m"])
        sp = sb.new_context(permissions=["camera"]).new_page()
        sp.add_init_script("document.addEventListener('securitypolicyviolation', "
                           "e => (window.__csp = window.__csp || []).push(e.violatedDirective + ' ' + e.blockedURI))")
        sp.goto(BASE + "/app?run=pcr_1-gt")
        sp.wait_for_selector("#scanbtn", state="visible", timeout=60000)
        sp.click("#scanbtn")
        sp.wait_for_function("() => /tubes checked/.test(document.getElementById('scanmsg').textContent)", timeout=60000)
        read = sp.evaluate("() => scan.last")  # what jsQR decoded off the camera frame
        tube = rack[1]
        well = layout[tube].split(":")[1]
        check("labels: the camera reads a printed label (jsQR) and the deck ticks off that tube's well",
              read.startswith(BASE + "/app?run=pcr_1-gt&tube=") and f"well {well}" in sp.inner_text("#scanmsg")
              and sp.evaluate("() => [...document.querySelectorAll('#deck .well.ok')].map(w => w.dataset.w)") == [well],
              (read, sp.inner_text("#scanmsg")))
        sp.goto(BASE + f"/app?run=pcr_1-gt&tube={urllib.request.quote(rack[0])}")  # the label's link, opened by a phone's camera app
        sp.wait_for_function("() => /tubes checked/.test(document.getElementById('scanmsg').textContent)", timeout=60000)
        check("labels: a label's link opens the app on that tube's place in the rack",
              layout[rack[0]].split(":")[1] in sp.inner_text("#scanmsg") and sp.evaluate("() => document.querySelectorAll('#deck .well.here').length") == 1,
              sp.inner_text("#scanmsg"))
        sp.evaluate("tube => onLabel(`https://elsewhere.example/app?run=pcr_3&tube=${tube}`)", "not a tube here")
        check("labels: a label from another run's rack is refused by name", "isn't a tube in this run's rack" in sp.inner_text("#scanmsg"),
              sp.inner_text("#scanmsg"))
        sp.goto(BASE + "/app?run=pcr_1-gt")
        sp.wait_for_selector("#rx", state="visible", timeout=60000)
        sp.fill("#rx", "8")
        check("reactions: the Reactions box points both label files at 8 reactions",
              sp.get_attribute("#f-zpl", "href").endswith("/labels.zpl?reactions=8")
              and sp.get_attribute("#f-labels", "href").endswith("/labels.svg?reactions=8") and "Reactions" in sp.inner_text("#rxnote"),
              sp.get_attribute("#f-zpl", "href"))
        sp.wait_for_function("() => /passed|failed|Couldn't/.test(document.getElementById('rxline').textContent)", timeout=180000)
        rx_used = sp.evaluate("() => [...document.querySelectorAll('#deck [data-lw=dest].used')].map(w => w.dataset.w)")
        water = next((r for r in sp.inner_text("#setup").splitlines() if "water" in r.lower()), "")
        check("reactions: Opentrons analyses the chosen 8 on the server; the deck marks 8 plate wells and the setup table "
              "shows the volumes for 8", "passed" in sp.inner_text("#rxline") and rx_used == [f"{r}1" for r in "ABCDEFGH"]
              and "206 µL" in water, (sp.inner_text("#rxline"), rx_used, water))
        st_rx, rxj = fetch(f"{api}/api/runs/pcr_1-gt/reactions/8")
        rxj = json.loads(rxj) if st_rx == 200 else {}
        check("reactions: the per-number analysis endpoint answers from its cache with tips, wells and tip-rack slots, "
              "and refuses a number the deck can't hold",
              rxj.get("ok") and len(rxj["plate_wells"]) == 8 and rxj["tips"] % 8 == 0 and len(rxj["tip_rack_slots"]) == rxj["tip_racks"]
              and fetch(f"{api}/api/runs/pcr_1-gt/reactions/999")[0] == 422, str(rxj)[:200])
        req = urllib.request.urlopen(f"{api}/api/runs/pcr_1-gt/labels.zpl", timeout=120)
        zpl = req.read().decode()
        check("labels: a Zebra ZPL file, one ^XA..^XZ label per tube in the rack",
              zpl.count("^XA") == zpl.count("^XZ") == len(rack) and "attachment" in req.headers.get("Content-Disposition", ""), zpl[:120])
        sp.goto(BASE + "/app?run=pcr_1-gt")
        sp.wait_for_selector("#zplbtn", state="visible", timeout=60000)
        sp.click("#zplbtn")  # zebrash in WebAssembly draws the file the way the printer will
        sp.wait_for_selector("#zplview img", timeout=120000)
        zqr = sp.evaluate("""async () => { const jsQR = await jsqr(), out = [];
          for (const img of document.querySelectorAll('#zplview img')) { await img.decode();
            const c = Object.assign(document.createElement('canvas'), { width: img.naturalWidth, height: img.naturalHeight });
            const x = c.getContext('2d'); x.drawImage(img, 0, 0);
            out.push([c.width, c.height, jsQR(x.getImageData(0, 0, c.width, c.height).data, c.width, c.height)?.data]); }
          return out; }""")
        check("labels: the Zebra labels render in the browser at 2 x 1 in, 203 dpi, and every QR code reads back as its tube's link",
              all(abs(w - 406) <= 2 and abs(h - 203) <= 2 for w, h, _ in zqr)
              and sorted(q for _, _, q in zqr) == sorted(f"{BASE}/app?run=pcr_1-gt&tube={urllib.request.quote(t)}" for t in rack), zqr)
        check("labels: no CSP violation (pinned jsQR with integrity, zebrash wasm checked by hash)", not sp.evaluate("() => window.__csp || []"), sp.evaluate("() => window.__csp || []"))
        sb.close()

        # ---- optional: spoken answers through a fake microphone --------------------------------------
        if os.getenv("VOICE") == "1" and os.path.exists("out/rec/voice_test.wav"):
            vb = p.chromium.launch(channel="chrome", args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                                           f"--use-file-for-fake-audio-capture={os.path.abspath('out/rec/voice_test.wav')}"])
            vp = vb.new_context(permissions=["microphone"]).new_page()
            vp.goto(BASE + "/app?run=pcr_3")
            vp.wait_for_selector("#voicebtn", state="visible", timeout=60000)
            vp.click("#voicebtn"); vp.wait_for_timeout(15000); vp.click("#voicebtn")
            # wait for the outcome message, not the button: onstop disables it asynchronously
            vp.wait_for_function("() => /^(Heard|Nothing|The |send|that|keep|live)/.test(document.getElementById('voicemsg').textContent)", timeout=180000)
            filled = vp.eval_on_selector_all("#qform input.heard", "xs => xs.length")
            check("voice: spoken answers fill the form", filled >= 3, vp.inner_text("#voicemsg"))
            vb.close()

        # ---- accessibility: axe-core (the engine behind Lighthouse's audit) in both colour schemes ------------
        axe_src = urllib.request.urlopen("https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js", timeout=60).read().decode()
        for scheme in ("light", "dark"):
            ab = p.chromium.launch()
            ap = ab.new_page(color_scheme=scheme, bypass_csp=True)  # axe is injected inline
            for path, ready in (("/", "h1"), ("/app?run=pcr_1-gt", "#steps li")):
                ap.goto(BASE + path)
                ap.wait_for_selector(ready, timeout=60000)
                ap.wait_for_timeout(1500)
                ap.add_script_tag(content=axe_src)
                viol = ap.evaluate("""async () => (await axe.run(document, {resultTypes: ['violations']})).violations
                    .filter(v => ['serious', 'critical'].includes(v.impact)).map(v => v.id + ': ' + v.nodes[0].html.slice(0, 80))""")
                check(f"a11y: axe-core finds no serious violations on {path} ({scheme})", not viol, viol[:3])
            ab.close()

        # ---- MCP: the same runs as tools for other agents ----------------------------------------------
        # ---- captions: the speech Muse Voice Transcribe heard, as a WebVTT track under the video ----------
        st, vtt = fetch(f"{api}/api/runs/pcr_1/captions.vtt")
        check("captions: WebVTT with timed cues", st == 200 and vtt.startswith(b"WEBVTT") and b" --> " in vtt, vtt[:80])
        page.goto(BASE + "/app?run=pcr_1")
        page.wait_for_function("() => document.querySelector('#video track')?.track.cues?.length > 0", timeout=60000)
        cues = page.evaluate("() => { const t = document.querySelector('#video track').track; return [t.mode, t.cues.length]; }")
        check("captions: the app loads them as a showing captions track", cues[0] == "showing" and cues[1] >= 1, cues)
        page.goto(BASE + "/app?run=pcr_2")
        page.wait_for_function("() => document.querySelectorAll('#steps li').length > 0", timeout=60000)
        check("captions: no empty track when nobody spoke", page.evaluate("() => !document.querySelector('#video track')"))
        tools, mproto, mrun, (err, msg), mpd, (mlab, mlab8) = ThreadPoolExecutor(1).submit(asyncio.run, mcp_session(api + "/mcp")).result()  # Playwright owns this thread's loop
        check("mcp: read-only tools listed", tools == ["list_runs", "get_run", "get_protocol", "get_protocol_designer",
                                                     "get_tube_labels", "get_receipt"], tools)
        check("mcp: get_tube_labels gives each rack tube's well, fill volume and label link",
              sorted(t["tube"] for t in mlab["tubes"]) == sorted(rack)
              and all(t["well"] == layout[t["tube"]].split(":")[1] and (t["fill_ul"] > t["drawn_ul"] > 0 or not t["source"])
                      and t["qr"].endswith(f"/app?run=pcr_1-gt&tube={urllib.request.quote(t['tube'])}") for t in mlab["tubes"])
              and mlab["labels_zpl"].endswith("/labels.zpl"), str(mlab)[:200])
        check("mcp: get_tube_labels scales the fill volumes to the reactions asked for",
              mlab8["reactions"] == 8 and mlab8["max_reactions"] == top and mlab8["labels_zpl"].endswith("?reactions=8")
              and all(b["fill_ul"] > a["fill_ul"] for a, b in zip(mlab["tubes"], mlab8["tubes"]) if a["source"]), str(mlab8)[:200])
        check("mcp: get_protocol_designer is the downloaded Protocol Designer file", mpd == pdj, str(mpd)[:120])
        check("mcp: get_protocol is byte for byte the downloaded protocol.py", mproto.encode() == proto)
        check("mcp: get_run carries the steps and the thermocycler time",
              len(mrun["steps"]) > 3 and mrun["simulator_ok"] and mrun["analysis"]["cycling_s"] > 1500, str(mrun)[:200])
        check("mcp: an unknown run is a readable tool error", err and "unknown run" in msg, msg)
        if os.getenv("FLEX"):  # ---- a robot-server (a real Flex, or Opentrons' robot-server in Flex simulation) ----
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
            import send_to_flex
            r = send_to_flex.send(os.environ["FLEX"], proto.decode(), {"reactions": 2}, start=True, log=lambda *_: None, resume_pauses=True)
            check("flex: the robot's own analysis passes with Reactions = 2 and the run succeeds",
                  r["analysis"] == "ok" and r["status"] == "succeeded", r)
        check("no uncaught page errors", not errors, (errors[:3], bad[:5]))
        csp_seen += page.evaluate("() => window.__csp || []")
        check("security: the CSP blocked nothing the pages need", not csp_seen, csp_seen[:3])
        check("no failed requests during normal use", not [x for x in bad if "/answers" not in x and "/voice" not in x
                                                           and "/upload" not in x and "protocol.py" not in x], bad[:5])
        b.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
