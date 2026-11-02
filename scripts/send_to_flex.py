"""Send a run's protocol.py straight to an Opentrons Flex over its HTTP API (what the Opentrons App does), with the
runtime parameters already set, so nobody has to carry a file to the robot.

  python scripts/send_to_flex.py 192.168.1.42 pcr_1-gt --reactions 8           # upload, robot analyses, run created
  python scripts/send_to_flex.py 192.168.1.42 pcr_1-gt --reactions 8 --start   # ...and press play

The robot must be on your network (port 31950). A browser on the https site can't reach it (mixed content), hence
this script. The run then appears on the robot's touchscreen and in the Opentrons App like any other.
Check it against Opentrons' own robot-server in Flex simulation mode: FLEX=127.0.0.1 python tests/e2e.py
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "https://pugarhuda--show-dont-code-web.us-east.modal.run"
HEAD = {"Opentrons-Version": "*"}


def _call(robot, method, path, body=None, files=None, timeout=60):
    """JSON (body) or multipart (files: {field: (filename, bytes)} plus body fields) request to the robot."""
    headers, data = dict(HEAD), None
    if files is not None:
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in (body or {}).items()]
        parts += [f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: text/x-python\r\n\r\n'.encode()
                  + blob + b"\r\n" for k, (fn, blob) in files.items()]
        data, headers["Content-Type"] = b"".join(parts) + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(f"http://{robot}:31950{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"robot said {e.code} to {method} {path}: {e.read().decode()[:400]}") from None


def _wait(fn, done, what, timeout):
    t0 = time.time()
    while True:
        v = fn()
        if done(v):
            return v
        if time.time() - t0 > timeout:
            raise SystemExit(f"gave up waiting for {what} after {timeout} s")
        time.sleep(1)


def configure_deck(robot, analysis, log=print):
    """What the touchscreen asks before a run: tell the robot which fixtures are installed. Adds the thermocycler the
    protocol loads (cutouts A1+B1) with the serial of the one the robot sees, keeping the rest of the deck as it is."""
    # ponytail: thermocycler only, the one module the generator loads; other modules need their own cutout fixtures
    if not any(c["commandType"] == "loadModule" and c["params"]["model"] == "thermocyclerModuleV2"
               for c in analysis.get("commands", [])):
        return
    tc = [m for m in _call(robot, "GET", "/modules")["data"] if m["moduleModel"] == "thermocyclerModuleV2"]
    if not tc:
        raise SystemExit("the protocol needs a Thermocycler GEN2 and the robot doesn't see one")
    deck = _call(robot, "GET", "/deck_configuration")["data"]["cutoutFixtures"]
    deck = [f for f in deck if f["cutoutId"] not in ("cutoutA1", "cutoutB1")]
    deck += [{"cutoutId": c, "cutoutFixtureId": f, "opentronsModuleSerialNumber": tc[0]["serialNumber"]}
             for c, f in (("cutoutA1", "thermocyclerModuleV2Rear"), ("cutoutB1", "thermocyclerModuleV2Front"))]
    _call(robot, "PUT", "/deck_configuration", body={"data": {"cutoutFixtures": deck}})
    log(f"deck configuration: Thermocycler {tc[0]['serialNumber']} in A1+B1")


def send(robot, source, params, start=False, log=print, resume_pauses=False, deck=False):
    """Upload `source` with runtime parameter values `params`, wait for the robot's analysis, create a run, optionally
    play it to the end. A pause (a hand-off such as the centrifuge) waits for someone to resume it on the robot, unless
    `resume_pauses` (for tests). `deck` writes the modules it loads into the robot's deck configuration. Returns {"protocol_id", "analysis", "run_id", "status"}."""
    health = _call(robot, "GET", "/health")
    log(f"robot {health.get('name')} ({health.get('robot_model')}, software {health.get('api_version')})")
    p = _call(robot, "POST", "/protocols", body={"runTimeParameterValues": json.dumps(params)},
              files={"files": ("protocol.py", source.encode("utf-8"))})["data"]
    pid = p["id"]
    # the robot analyses the file with these values itself, the same check the App shows before a run
    an = _wait(lambda: _call(robot, "GET", f"/protocols/{pid}/analyses")["data"][-1],
               lambda a: a.get("status") == "completed", "the robot's analysis", 300)
    if an.get("result") != "ok":
        raise SystemExit(f"the robot's analysis failed: {[e.get('detail') for e in an.get('errors', [])][:3]}")
    log(f"uploaded as {pid}; the robot's analysis passed with {params or 'the defaults'}")
    if deck:
        configure_deck(robot, _call(robot, "GET", f"/protocols/{pid}/analyses/{an['id']}")["data"], log)
    run = _call(robot, "POST", "/runs", body={"data": {"protocolId": pid, "runTimeParameterValues": params}})["data"]
    out = {"protocol_id": pid, "analysis": an.get("result"), "run_id": run["id"], "status": run["status"]}
    log(f"run {run['id']} created ({run['status']}); it is on the robot's touchscreen now")
    if start:
        _call(robot, "POST", f"/runs/{run['id']}/actions", body={"data": {"actionType": "play"}})
        log("started")
        def poll():
            r = _call(robot, "GET", f"/runs/{run['id']}")["data"]
            if r["status"] == "paused" and resume_pauses:
                _call(robot, "POST", f"/runs/{run['id']}/actions", body={"data": {"actionType": "play"}})
            return r
        end = _wait(poll, lambda r: r["status"] in ("succeeded", "failed", "stopped"), "the run to finish", 3600)
        out["status"] = end["status"]
        out["errors"] = [e.get("detail") for e in end.get("errors", [])][:3]
        log(f"run {end['status']}" + (f": {out['errors']}" if out["errors"] else ""))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("robot", help="the Flex's IP address or host name")
    ap.add_argument("run", help="a Show, Don't Code run name (e.g. pcr_1-gt), or a path to a protocol.py")
    ap.add_argument("--reactions", type=int, help="copies of the filmed reaction (the protocol's Reactions parameter)")
    ap.add_argument("--start", action="store_true", help="press play after creating the run")
    ap.add_argument("--configure-deck", action="store_true",
                    help="write the thermocycler (A1+B1) into the robot's deck configuration, as the touchscreen does")
    ap.add_argument("--api", default=API, help="the Show, Don't Code backend")
    a = ap.parse_args()
    if a.run.endswith(".py"):
        source = open(a.run, encoding="utf-8").read()
    else:
        with urllib.request.urlopen(f"{a.api}/api/runs/{a.run}/protocol.py", timeout=120) as r:
            source = r.read().decode("utf-8")
    params = {"reactions": a.reactions} if a.reactions else {}
    send(a.robot, source, params, a.start, deck=a.configure_deck)


if __name__ == "__main__":
    sys.exit(main())
