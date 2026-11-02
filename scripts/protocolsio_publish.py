"""Put a run on protocols.io as a protocol other labs can read, fork and cite: one protocols.io step per step found in
the video (what, which reagent, how much, the video seconds), with links to the Opentrons Flex protocol.py it became.

  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt              # private draft in your account
  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt --doi        # + DOI, still private (prepublish)
  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt --publish    # public, permanent

Token: protocols.io/developers (client access token: your own private content). API: apidoc.protocols.io
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

UA = "show-dont-code/1.0 (+https://github.com/PugarHuda/show-dont-code)"  # Cloudflare refuses Python's default (1010)
API = "https://pugarhuda--show-dont-code-web.us-east.modal.run"
SITE = "https://show-dont-code.vercel.app"
PIO = "https://www.protocols.io/api"
ROBOT = {"transfer", "mix", "thermocycle"}


def _p(method, path, token, body=None, form=None):
    data, headers = None, {"Authorization": f"Bearer {token}", "User-Agent": UA}
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    elif form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(PIO + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"protocols.io said {e.code} to {method} {path}: {e.read().decode()[:500]}") from None
    if out.get("status_code", 0) != 0:
        raise SystemExit(f"protocols.io refused {method} {path}: {json.dumps(out)[:500]}")
    return out


def step_text(s):
    """One protocols.io step: what happened, with the numbers the robot uses and where to see it in the video."""
    bits = [" ".join(str(s["description"]).split())]
    if s["action"] == "transfer" and s.get("volume_ul"):
        bits.append(f"{s['volume_ul']:g} µL of {s.get('reagent') or 'reagent'}"
                    + (f" into {s['destination']}" if s.get("destination") else ""))
    if s["action"] == "thermocycle" and s.get("cycles"):
        bits.append(f"{s['cycles']} cycles")
    bits.append(f"Video {float(s['start']):.0f}-{float(s['end']):.0f} s."
                + ("" if s["action"] in ROBOT else " Done by hand: the robot pauses here."))
    return " · ".join(bits)


def publish(run, token, doi=False, public=False, api=API, log=print):
    with urllib.request.urlopen(f"{api}/api/runs/{run}", timeout=180) as r:
        R = json.load(r)
    if not (R.get("protocol") and R.get("sim_ok")):
        raise SystemExit(f"{run} has no simulator-passing protocol yet: answer its open questions first")
    steps = [s for s in R["steps"] if s["action"] != "skipped"]
    guid = uuid.uuid4().hex.upper()
    p = _p("POST", f"/v3/protocols/{guid}", token, body={"type_id": 1})["protocol"]
    pid = p.get("id") or guid
    links = (f"{SITE}/app?run={run}", f"{api}/api/runs/{run}/protocol.py", f"{api}/api/runs/{run}/ro-crate.zip")
    _p("PUT", f"/v4/protocols/{pid}", token, body={
        "title": f"{'PCR setup' if run.startswith('pcr') else 'Plasmid miniprep' if run.startswith('miniprep') else run}"
                 f" recovered from a first-person video ({run}, Show, Don't Code)",
        "description": (f"Steps recovered from a first-person video of this protocol done by hand (BioVL2, CC BY 4.0), "
                        f"and the Opentrons Flex protocol they became. Watch each step next to the robot: {links[0]} · "
                        f"Opentrons Flex protocol.py (passes the Opentrons simulator, opentrons analyze and a run on "
                        f"Opentrons' robot-server): {links[1]} · Everything, with hashes (RO-Crate): {links[2]}")})
    prev, body = None, []
    for s in steps:
        g = uuid.uuid4().hex.upper()
        body.append({"guid": g, "previous_guid": prev, "step": step_text(s),
                     "section": "Robot" if s["action"] in ROBOT else "By hand"})
        prev = g
    _p("POST", f"/v4/protocols/{pid}/steps", token, body={"steps": body})
    p["uri"] = _p("GET", f"/v4/protocols/{pid}", token)["payload"]["uri"]  # the URI follows the title set above
    log(f"protocol {pid} created with {len(body)} steps (private)")
    out = {"id": pid, "guid": guid, "steps": len(body), "url": f"https://www.protocols.io/edit/{p['uri']}"}
    if doi or public:
        r = _p("POST", f"/v3/protocols/{p['uri']}/publish", token, body={} if public else {"prepublish": 1})
        out["doi"] = (r.get("protocol") or {}).get("doi") or r.get("doi")
        out["public"] = public
        log(f"DOI {out['doi']} ({'public' if public else 'reserved, still private'})")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run")
    ap.add_argument("--doi", action="store_true", help="get a DOI but keep the protocol private (prepublish)")
    ap.add_argument("--publish", action="store_true", help="make it public (permanent)")
    ap.add_argument("--api", default=API)
    a = ap.parse_args()
    token = os.getenv("PROTOCOLSIO_TOKEN") or sys.exit("set PROTOCOLSIO_TOKEN")
    print(json.dumps(publish(a.run, token, a.doi, a.publish, a.api), indent=1))


if __name__ == "__main__":
    main()
