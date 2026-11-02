"""Import a run's .eln into a real eLabFTW and check what the notebook shows: the public eLabFTW demo
(demo.elabftw.net, reset every 24 h, one-click demo login) by default. The entry is created visible to its owner only.

  python scripts/elabftw_roundtrip.py [run] [api] [screenshot.png]     e.g. pcr_1-gt https://pugarhuda--show-dont-code-web.us-east.modal.run
"""
import re
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ELAB = "https://demo.elabftw.net"
run = sys.argv[1] if len(sys.argv) > 1 else "pcr_1-gt"
api = (sys.argv[2] if len(sys.argv) > 2 else "https://pugarhuda--show-dont-code-web.us-east.modal.run").rstrip("/")

eln = Path(tempfile.mkdtemp()) / f"show-dont-code-{run}.eln"
with urllib.request.urlopen(f"{api}/api/runs/{run}/run.eln", timeout=180) as r:
    eln.write_bytes(r.read())

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1400, "height": 1000})
    pg.goto(f"{ELAB}/login.php")
    pg.get_by_text("Login as User").first.click()
    pg.wait_for_load_state("networkidle")
    pg.goto(f"{ELAB}/profile.php?tab=3")
    pg.get_by_text("Import from file").click()
    pg.set_input_files("#importFileInput", str(eln))
    pg.check("#entityTypeRadioExperiments")
    pg.select_option("#importModalCanread", index=4)  # only owner
    pg.select_option("#importModalCanwrite", index=4)
    pg.click("#importFileBtn")
    time.sleep(12)
    pg.goto(f"{ELAB}/experiments.php?q={run}")
    pg.wait_for_load_state("networkidle")
    pg.get_by_text(f"Show, Don't Code run {run}").first.click()
    pg.wait_for_load_state("networkidle")
    want = len([n for n in zipfile.ZipFile(eln).namelist() if not n.endswith("ro-crate-metadata.json")])
    for _ in range(30):  # eLabFTW stores the attachments after creating the entry: wait until it lists every one
        m = re.search(r"ATTACHED FILES \((\d+)\)", pg.inner_text("body").upper())
        if m and int(m[1]) >= want:
            break
        time.sleep(4)
        pg.reload(wait_until="networkidle")
    body = pg.inner_text("body")
    m = re.search(r"ATTACHED FILES \((\d+)\)", body.upper())
    checks = {"steps table": "Steps" in body and "µL" in body, "simulator passed": "Opentrons simulator: passed" in body,
              "protocol.py attached": "protocol.py" in body, "receipt attached": "receipt.json" in body,
              "bench card attached": "bench-card-step-1.jpg" in body,
              f"all {want} files attached": bool(m) and int(m[1]) == want}
    print(pg.url)
    if len(sys.argv) > 3:  # a full-page screenshot of the entry, e.g. for the demo video
        pg.screenshot(path=sys.argv[3], full_page=True)
    for k, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  eLabFTW shows the {k}")
    b.close()
sys.exit(0 if all(checks.values()) else 1)
