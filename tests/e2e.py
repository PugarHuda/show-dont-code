"""End-to-end QA in a real Chrome via Playwright: landing, every sample run, the confirm->simulate->replay flow,
every download (the receipt's hash must match protocol.py byte for byte), and the security edges.

  python tests/e2e.py                                   # against http://127.0.0.1:8000 (python app.py)
  BASE=https://show-dont-code.vercel.app python tests/e2e.py
  VOICE=1 ... python tests/e2e.py                       # also speak answers through a fake microphone (costs API)

Nothing here costs model calls unless VOICE=1. The one write it makes re-sends answers pcr_1-gt already has.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

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

        # ---- landing --------------------------------------------------------------------------------
        page.goto(BASE + "/")
        page.wait_for_timeout(2500)
        check("landing: headline", "Pipette it once" in page.inner_text("h1"))
        check("landing: hero video plays", page.evaluate("() => { const v = document.getElementById('hero'); return !v.paused && v.readyState >= 2; }"))
        page.click("#toggle")
        check("landing: pause control works", page.evaluate("() => document.getElementById('hero').paused"))
        check("landing: every image/video has a text alternative",
              page.evaluate("() => [...document.querySelectorAll('img,video')].every(e => e.getAttribute('alt') !== null || e.getAttribute('aria-label'))"))
        check("landing: every button and link has an accessible name",
              page.evaluate("() => [...document.querySelectorAll('a,button')].every(e => (e.innerText || e.getAttribute('aria-label') || '').trim())"))
        mobile = b.new_page(viewport={"width": 360, "height": 780})
        mobile.goto(BASE + "/")
        mobile.wait_for_timeout(2000)
        check("landing: no horizontal scroll at 360 px", mobile.evaluate("() => document.documentElement.scrollWidth <= innerWidth"))
        mobile.goto(BASE + "/app?run=pcr_3")
        mobile.wait_for_function("document.querySelectorAll('#run option').length > 0", timeout=60000)
        check("app: no horizontal scroll at 360 px", mobile.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1"))
        mobile.close()

        # ---- every sample run ----------------------------------------------------------------------
        page.goto(BASE + "/app")
        page.wait_for_function("document.querySelectorAll('#run option').length > 0", timeout=60000)
        runs = page.eval_on_selector_all("#run option", "o => o.map(x => x.value)")
        check("app: lists the sample runs", len(runs) >= 6, runs)
        check("app: private uploads are not listed", not any(r.startswith("u-") for r in runs), runs)
        for run in runs:
            page.select_option("#run", run)
            page.wait_for_function("document.getElementById('status').textContent !== 'loading…'", timeout=60000)
            page.wait_for_timeout(1200)
            n_steps = len(page.query_selector_all("#steps li"))
            src = page.evaluate("() => document.getElementById('video').currentSrc")
            wells = len(page.query_selector_all("#deck .well"))
            check(f"run {run}: steps, video, deck", n_steps > 0 and src and wells > 0, (n_steps, src[-40:], wells))
        page.goto(BASE + "/app?run=pcr_3")
        page.wait_for_function("document.querySelectorAll('#steps li').length > 0", timeout=60000)
        page.wait_for_function("document.getElementById('video').readyState >= 1", timeout=60000)
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
        page.goto(BASE + "/app?run=pcr_1-gt")
        page.wait_for_selector("#replay", state="visible", timeout=60000)
        page.click("#replay")
        page.wait_for_timeout(3000)
        cap = page.inner_text("#replaycap")
        check("app: robot replay runs the simulator's commands", "Robot command" in cap, cap)
        page.click("#replay")
        check("app: liquid setup lists what to load", len(page.query_selector_all("#setup tr")) >= 3)
        st, proto = fetch(f"{api}/api/runs/pcr_1-gt/protocol.py")
        check("download: protocol.py", st == 200 and b"def run(ctx)" in proto and b"load_liquid" in proto)
        st, rec = fetch(f"{api}/api/runs/pcr_1-gt/receipt.json")
        rec = json.loads(rec) if st == 200 else {}
        check("download: receipt hash matches protocol.py byte for byte",
              rec.get("protocol_sha256") == hashlib.sha256(proto).hexdigest(), rec.get("protocol_sha256"))
        check("receipt: replay runs do not claim models read them", "note" in rec.get("models", {}), rec.get("models"))
        for path, needle in (("analysis.json", b'"liquids"'), ("methods.md", b"## Procedure"), ("autoprotocol.json", b'"thermocycle"'),
                             ("pylabrobot.py", b"set_volume_tracking(True)")):
            st, data = fetch(f"{api}/api/runs/pcr_1-gt/{path}")
            check(f"download: {path}", st == 200 and needle in data, (st, data[:80]))
        st, _ = fetch(f"{api}/api/runs/pcr_3/protocol.py")
        check("download: protocol refused while questions are open", st == 409, st)

        # ---- input validation and security edges ----------------------------------------------------
        for bad_answers, why in (({"0": -5}, "negative volume"), ({"99": 5}, "unknown step"), ({"5:cycles": 2.5}, "fractional cycles"),
                                 ({"0; rm": 5}, "malformed key")):
            st, _ = fetch(f"{api}/api/runs/pcr_1-gt/answers", "POST", json.dumps(bad_answers).encode(), {"Content-Type": "application/json"})
            check(f"api: rejects {why}", st == 422, st)
        for name in ("..%2F..%2Fetc", "pcr_1%00", "nope"):
            st, _ = fetch(f"{api}/api/runs/{name}")
            check(f"api: unknown run {name} -> 404", st == 404, st)
        st, _ = fetch(f"{api}/api/upload", "POST", b"x" * 2000, {"X-Upload-Code": "wrong"})
        check("api: upload with a wrong code is refused", st in (403, 404, 405), st)
        st, _ = fetch(f"{api}/api/runs/pcr_3/voice", "POST", os.urandom(5000))
        check("api: voice refuses non-audio", st in (415, 503, 404, 405), st)
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.goto(BASE + '/app?run=u-%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E')
        page.wait_for_function("document.querySelectorAll('#run option').length > 0", timeout=60000)
        page.wait_for_timeout(1500)
        check("app: script in ?run= does not execute", not dialogs, dialogs)

        # ---- upload dialog -------------------------------------------------------------------------
        if page.is_visible("#upbtn"):
            page.click("#upbtn")
            check("upload: dialog opens", page.is_visible("#updlg"))
            page.keyboard.press("Escape")
            check("upload: Escape closes it", not page.is_visible("#updlg"))

        # ---- optional: spoken answers through a fake microphone --------------------------------------
        if os.getenv("VOICE") == "1" and os.path.exists("out/rec/voice_test.wav"):
            vb = p.chromium.launch(channel="chrome", args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                                           f"--use-file-for-fake-audio-capture={os.path.abspath('out/rec/voice_test.wav')}"])
            vp = vb.new_context(permissions=["microphone"]).new_page()
            vp.goto(BASE + "/app?run=pcr_3")
            vp.wait_for_selector("#voicebtn", state="visible", timeout=60000)
            vp.click("#voicebtn"); vp.wait_for_timeout(15000); vp.click("#voicebtn")
            # wait for the outcome message, not the button: onstop disables it asynchronously
            vp.wait_for_function("/^(Heard|Nothing|The |send|that|keep|live)/.test(document.getElementById('voicemsg').textContent)", timeout=180000)
            filled = vp.eval_on_selector_all("#qform input.heard", "xs => xs.length")
            check("voice: spoken answers fill the form", filled >= 3, vp.inner_text("#voicemsg"))
            vb.close()

        check("no uncaught page errors", not errors, errors[:3])
        check("no failed requests during normal use", not [x for x in bad if "/answers" not in x and "/voice" not in x
                                                           and "/upload" not in x and "protocol.py" not in x], bad[:5])
        b.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
