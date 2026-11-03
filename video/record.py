"""Record each scene of the demo video from the live site with Playwright: real clicks, real results, one clip per
scene (video/public/clips/<id>.mp4). A visible cursor and a red spotlight show where to look.

  python video/record.py                 # every recorded scene in script.json
  python video/record.py robot labels    # just these
"""
import asyncio
import html
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
SITE = "https://show-dont-code.vercel.app"
API = "https://pugarhuda--show-dont-code-web.us-east.modal.run"
W, H = 1920, 1080  # output size
VW, VH = 1536, 864  # pages laid out at 1536 px and scaled up to 1080p in ffmpeg: text 25% bigger in the video

# a cursor you can see, and a spotlight around the thing the voice is talking about
OVERLAY = """
(() => {
  const css = document.createElement('style');
  css.textContent = `.__cur{position:fixed;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;background:rgba(212,35,27,.28);
    border:3px solid #d4231b;z-index:2147483647;pointer-events:none;transition:transform .15s;left:-50px;top:-50px}
    .__cur.down{transform:scale(.7)}
    .__spot{position:fixed;border:5px solid #d4231b;border-radius:16px;z-index:2147483646;pointer-events:none;
    transition:all .35s ease}`;  /* an outline only: dimming the page made whole scenes look dark */
  const add = () => { document.head.appendChild(css); const c = document.createElement('div'); c.className = '__cur'; document.body.appendChild(c);
    addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', () => c.classList.add('down'), true); addEventListener('mouseup', () => c.classList.remove('down'), true); };
  document.readyState === 'loading' ? addEventListener('DOMContentLoaded', add) : add();
  window.__spot = (sel) => {
    document.querySelectorAll('.__spot').forEach(e => e.remove());
    const el = typeof sel === 'string' ? document.querySelector(sel) : sel; if (!el) return false;
    const r = el.getBoundingClientRect(), d = document.createElement('div'); d.className = '__spot';
    Object.assign(d.style, {left: r.left - 10 + 'px', top: r.top - 10 + 'px', width: r.width + 20 + 'px', height: r.height + 20 + 'px'});
    document.body.appendChild(d); return true;
  };
  window.__unspot = () => document.querySelectorAll('.__spot').forEach(e => e.remove());
  addEventListener('scroll', () => window.__unspot && window.__unspot(), true);
})();
"""


class Scene:
    def __init__(self, page):
        self.page = page
        self.cuts = []  # (start, end) wall-clock spans of pure waiting, dropped from the clip and said on screen

    def _cut(self, t1, t2):
        if t2 - t1 > 1.2:  # keep a beat on each side so the change is visible
            self.cuts.append((t1 + 0.4, t2 - 0.3))

    def nav(self, url, **kw):
        """A later page in the same scene: its loading time is cut."""
        t1 = time.time()
        self.page.goto(url, **kw)
        self._cut(t1, time.time())

    def go(self, url, ready):
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_function(ready, timeout=90000)

    def show(self, sel, block="center"):
        """Scroll the element into view, let it settle, then spotlight it."""
        self.page.evaluate("([s, b]) => document.querySelector(s)?.scrollIntoView({block: b, behavior: 'smooth'})", [sel, block])
        time.sleep(1.0)
        self.page.evaluate("s => __spot(s)", sel)

    def point(self, sel, click=False):
        """Move the visible cursor onto the element, then click that exact element. Scroll and let the layout settle
        first: a click at stale coordinates once hit the button above the intended one."""
        loc = self.page.locator(sel).first
        loc.scroll_into_view_if_needed()
        time.sleep(0.6)
        box = loc.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=28)
        time.sleep(0.3)
        if click:
            loc.click()

    def wait(self, js, timeout=240000):
        t1 = time.time()
        self.page.wait_for_function(js, timeout=timeout)
        self._cut(t1, time.time())


def intro(s):
    """The hook: the scientist's own video on the left, the robot replaying the protocol written from it on the right."""
    s.go(SITE + "/app?run=pcr_1-gt", "() => document.getElementById('video')?.readyState >= 2 && !document.getElementById('replaybox').hidden")
    s.page.evaluate("() => scrollTo(0, 0)")
    time.sleep(0.8)
    s.page.locator("#replay").click()
    s.restart()
    time.sleep(13)


def landing(s):
    s.go(SITE + "/", "() => document.getElementById('deck')")
    time.sleep(1.2)
    s.page.evaluate("() => __spot('#deck')")
    # step through with the deck in view: the lit tube moves along the rack as the steps change
    for _ in range(4):
        time.sleep(1.4)
        s.page.evaluate("() => { document.getElementById('next').click(); __spot('#deck'); }")
    time.sleep(1.0)
    s.show(".carousel")
    s.point("#next", click=True)
    time.sleep(2.5)


def sync(s):
    s.go(SITE + "/app?run=pcr_3", "() => document.getElementById('video')?.readyState >= 1 && document.querySelectorAll('#steps li').length")
    s.page.evaluate("() => new Promise(r => { const v = document.getElementById('video'); v.addEventListener('seeked', r, {once: true}); v.currentTime = 31; })")
    time.sleep(1)
    s.page.evaluate("() => document.getElementById('video').play()")
    # the lab video buffers before it plays: start the clip once it is really moving, not on a frozen frame
    s.wait("() => document.getElementById('video').currentTime > 31.6", 120000)
    s.restart()
    time.sleep(2)
    s.page.evaluate("() => __spot('#video')")  # the steps list is below the fold here; point at what is on screen
    time.sleep(3)
    s.page.evaluate("() => __spot('#deck')")
    time.sleep(3.5)
    s.page.evaluate("() => __spot('#now')")
    time.sleep(3)
    s.page.evaluate("() => document.getElementById('video').pause()")


def tubes(s):
    s.go(SITE + "/app?run=pcr_3", "() => document.querySelectorAll('#tubes > *').length")
    t = s.page.evaluate("() => R.tubes[0].t")
    s.page.evaluate(f"() => new Promise(r => {{ const v = document.getElementById('video'); v.addEventListener('seeked', r, {{once: true}}); v.currentTime = {t}; }})")
    s.show("#pick", "start")
    s.point("#pick")
    time.sleep(1)
    s.page.evaluate("() => { window.scrollTo({top: 0, behavior: 'smooth'}); }")
    time.sleep(1)
    s.page.evaluate("() => __spot('#video')")
    s.page.evaluate("() => document.getElementById('video').play()")
    time.sleep(4)
    s.page.evaluate("() => document.getElementById('video').pause()")
    s.show("#tubes", "start")
    time.sleep(3)


def reviewer(s):
    s.go(SITE + "/app?run=pcr_3", "() => document.querySelectorAll('#issues li').length && document.querySelector('#qform .hint')")
    passed = s.page.evaluate("() => [...document.querySelectorAll('#issues li.info')].findIndex(li => /simulator passed/.test(li.textContent))")
    s.page.evaluate(f"() => document.querySelectorAll('#issues li.info')[{passed}].id = '__passed'")
    s.show("#__passed")
    time.sleep(5)
    s.show("#qform")
    time.sleep(6)


def confirm(s):
    s.go(SITE + "/app?run=pcr_3", "() => document.querySelector('#qform button[type=submit]')")
    # the reviewer pre-filled every volume; the cycle count comes from the bench sheet's 35
    s.page.evaluate("() => document.querySelectorAll('#qform input[type=number]').forEach(i => { if (!i.value && i.name.endsWith(':cycles')) i.value = 35; })")
    s.show("#qform")
    time.sleep(1.5)
    s.point("#qform button[type=submit]", click=True)
    s.wait("() => /passed/.test(document.getElementById('status').textContent)")
    time.sleep(1)
    s.page.evaluate("() => scrollTo({top: 0, behavior: 'smooth'})")
    time.sleep(0.8)
    s.page.evaluate("() => __spot('#status')")
    time.sleep(2.5)
    s.show("#analysisline")
    time.sleep(2.5)
    s.show("#setup")
    time.sleep(2.5)


def robot(s):
    s.go(SITE + "/app?run=pcr_1-gt", "() => !document.getElementById('flexrun').closest('section').hidden")
    s.page.fill("#rx", "2")
    s.show("#flexrun")
    s.point("#flexrun", click=True)
    s.wait("() => /ran it to the end|did not finish|didn't complete/.test(document.getElementById('flexline').textContent)", 400000)
    time.sleep(0.5)
    s.show("#flexline")
    time.sleep(5)


def browser(s):
    s.go(SITE + "/app?run=pcr_1-gt", "() => !document.getElementById('here-evo').hidden")
    s.show("#plrline")
    time.sleep(2)
    s.point("#here-evo", click=True)
    s.wait("() => /ran in this browser|Tecan Freedom EVO 150: (?!loading|running)/.test(document.getElementById('plrhere').textContent)", 300000)
    s.show("#plrhere")
    time.sleep(4)


def labels(s):
    s.go(SITE + "/app?run=pcr_1-gt", "() => !document.getElementById('rxbox').hidden")
    s.show("#rxbox")
    s.point("#rx")
    s.page.fill("#rx", "8")
    s.page.dispatch_event("#rx", "input")
    s.wait("() => /passed|failed|Couldn't/.test(document.getElementById('rxline').textContent)")
    s.page.evaluate("() => __spot('#rxline')")
    time.sleep(2.2)
    s.point("#zplbtn", click=True)
    s.wait("() => document.querySelectorAll('#zplview img').length > 0", 120000)
    time.sleep(0.6)
    s.show("#zplview")
    time.sleep(3)


def receipt(s):
    s.go(SITE + "/app?run=pcr_1-gt", "() => !document.getElementById('otsline').hidden || document.getElementById('f-receipt').href")
    s.show("#otsline")
    time.sleep(3)
    s.nav("https://zenodo.org/records/23876762", wait_until="domcontentloaded")
    time.sleep(1.5)
    s.page.evaluate("() => { const b = [...document.querySelectorAll('button')].find(x => /Accept all cookies|Accept only essential/.test(x.textContent)); b && b.click(); }")
    time.sleep(2)
    # the run's .eln as eLabFTW showed it after import (scripts/elabftw_roundtrip.py on demo.elabftw.net, Nov 2)
    s.nav((HERE / "public" / "elab.html").resolve().as_uri())
    time.sleep(1.2)
    time.sleep(1.5)
    s.page.evaluate("() => scrollTo({top: document.body.scrollHeight * 0.4, behavior: 'smooth'})")
    time.sleep(3)


def card(s):
    s.go(SITE + "/app?run=pcr_1-gt", "() => [...document.querySelectorAll('#card img')].length && [...document.querySelectorAll('#card img')].every(i => (i.loading = 'eager', true))")
    s.wait("() => [...document.querySelectorAll('#card img')].every(i => i.complete && i.naturalWidth > 0)", 60000)
    s.show("#cardbox", "start")
    time.sleep(4)
    s.page.evaluate("() => __spot('#card li:nth-child(2)')")
    time.sleep(3)
    s.point("#card li:nth-child(2) button.seek", click=True)  # the drawing's own moment in the video
    time.sleep(3)


def crosscheck(s):
    s.go(SITE + "/app?run=pcr_1-gt", "() => document.querySelectorAll('#xcheck tr').length > 0")
    s.show("#xbox")
    time.sleep(3)
    s.page.evaluate("() => { const r = [...document.querySelectorAll('#xcheck tr')].find(t => /same share/.test(t.textContent)); r.id = '__same'; }")
    s.page.evaluate("() => __spot('#__same')")
    time.sleep(5)


def ecosystem(s):
    s.go(SITE + "/app?run=protocolsio_q5", "() => !document.getElementById('srcbox').hidden && document.querySelectorAll('#steps li').length")
    time.sleep(0.8)
    s.page.evaluate("() => __spot('#srcbox')")
    time.sleep(2.5)
    s.show("#steps", "start")
    time.sleep(2.5)
    s.nav((HERE / "public" / "mcp.html").resolve().as_uri())
    time.sleep(4.5)


def results(s):
    s.go(SITE + "/", "() => document.getElementById('results')")
    s.show(".scores")
    time.sleep(3.5)
    s.show("table.runs")
    time.sleep(4)


async def _mcp_calls():
    from mcp import Client

    async with Client(API + "/mcp") as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        rel = json.loads((await c.call_tool("get_related_protocols", {"name": "pcr_3"})).content[0].text)
        fl = json.loads((await c.call_tool("run_on_flex_robot_server", {"name": "pcr_1-gt", "reactions": 2})).content[0].text)
    return tools, rel, fl


def mcp_page():
    """A terminal-style page of what an agent gets from the live MCP server (real calls, made now)."""
    tools, rel, fl = asyncio.run(_mcp_calls())
    show = lambda o: html.escape(json.dumps(o, indent=1, ensure_ascii=False))  # noqa: E731
    lines = [f'<p class="c">$ claude mcp add --transport http show-dont-code {API}/mcp</p>',
             f'<p class="k">list_tools()</p><pre>{show(tools)}</pre>',
             '<p class="k">run_on_flex_robot_server(name="pcr_1-gt", reactions=2)</p>'
             f'<pre>{show({k: fl.get(k) for k in ("robot_software", "status", "commands", "matches_analysis")})}</pre>',
             '<p class="k">get_related_protocols(name="pcr_3")</p>'
             f'<pre>{show([{"title": p["title"][:60], "doi": p["doi"]} for p in rel["protocols"][:3]])}</pre>']
    (HERE / "public" / "mcp.html").write_text(
        "<!doctype html><meta charset=utf-8><style>body{margin:0;background:#000;color:#f2f2f2;font:22px/1.45 ui-monospace,Consolas,monospace;"
        "padding:60px 90px}p{margin:18px 0 6px}.c{color:#a6a6a6}.k{color:#ff8a80;font-weight:700}pre{margin:0;color:#f2f2f2}</style>"
        + "".join(lines), encoding="utf-8")


SCENES = {f.__name__: f for f in (intro, landing, sync, tubes, reviewer, confirm, robot, browser, labels, card, crosscheck,
                                     receipt, ecosystem, results)}


def record(name, min_s):
    out = HERE / "public" / "clips"
    raw = out / f"_raw_{name}"
    shutil.rmtree(raw, ignore_errors=True)
    if name == "receipt":
        (HERE / "public" / "elab.html").write_text('<body style="margin:0;background:#fff"><img src="elab_entry.png" '
                                                   'style="width:100%;display:block" alt="eLabFTW entry"></body>')
    if name == "ecosystem":
        mcp_page()  # before the browser opens: Playwright's sync API owns an event loop, asyncio.run can't nest in it
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        ctx = b.new_context(viewport={"width": VW, "height": VH}, record_video_dir=str(raw), record_video_size={"width": VW, "height": VH},
                            bypass_csp=True)
        ctx.add_init_script(OVERLAY)
        t_rec = time.time()
        page = ctx.new_page()
        page.goto("about:blank")
        s = Scene(page)
        t0 = None

        def mark():
            nonlocal t0
            t0 = time.time()
            return t0

        orig_go = s.go

        def go(url, ready):  # the clip starts once the first page is ready, not while it loads
            orig_go(url, ready)
            if t0 is None:
                time.sleep(0.6)
                mark()
        s.go = go
        s.restart = lambda: (time.sleep(0.2), mark())  # a scene can move the clip's start past its own loading
        SCENES[name](s)
        if t0 is None:
            mark()
        s.cuts = [(max(a, t0), b) for a, b in s.cuts if b > t0]  # waiting before the clip starts is not in it anyway
        cut_s = sum(b - a for a, b in s.cuts)
        left = min_s - (time.time() - t0 - cut_s)
        if left > 0:
            time.sleep(left)  # hold the last frame long enough for the narration
        video = page.video
        ctx.close()
        b.close()
        src = video.path()
    lead = t0 - t_rec
    keep = f"scale={W}:{H}:flags=lanczos"
    if s.cuts:  # times relative to the clip start (the -ss below)
        drop = "+".join(f"between(t,{a - t0:.2f},{b - t0:.2f})" for a, b in s.cuts)
        keep = f"fps=30,select='not({drop})',setpts=N/30/TB," + keep  # constant rate first: the recording's varies
    cuts = json.loads((out.parent / "cuts.json").read_text()) if (out.parent / "cuts.json").exists() else {}
    cuts[name] = round(cut_s, 1)
    (out.parent / "cuts.json").write_text(json.dumps(cuts, indent=1))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{lead:.2f}", "-i", str(src), "-an", "-vf", keep, "-c:v", "libx264", "-crf", "18",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(out / f"{name}.mp4")], check=True)
    shutil.rmtree(raw, ignore_errors=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out / f"{name}.mp4")],
                               capture_output=True, text=True).stdout)
    print(f"{name:10} {dur:6.1f} s", flush=True)
    return dur


if __name__ == "__main__":
    scenes = json.loads((HERE / "public" / "scenes.json").read_text(encoding="utf-8"))
    (HERE / "public" / "clips").mkdir(parents=True, exist_ok=True)
    want = sys.argv[1:] or [s["id"] for s in scenes if s["kind"] == "rec"]
    clips = json.loads((HERE / "public" / "clips.json").read_text()) if (HERE / "public" / "clips.json").exists() else {}
    for sc in scenes:
        if sc["id"] in want:
            clips[sc["id"]] = record(sc["id"], sc["voice_s"])
            (HERE / "public" / "clips.json").write_text(json.dumps(clips, indent=1))
