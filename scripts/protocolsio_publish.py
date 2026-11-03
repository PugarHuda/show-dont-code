"""Put a run on protocols.io as a full protocol other labs can read, fork and cite: abstract, reaction composition,
materials and equipment, tube setup, warnings, and sectioned steps (deck setup, run setup, the robot's additions with the
video seconds each came from, thermocycling, after the run), all from the run's own data, with links to protocol.py.

  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt              # private draft in your account
  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt --update ID  # rewrite an existing draft
  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt --doi        # + DOI, still private (prepublish)
  PROTOCOLSIO_TOKEN=... python scripts/protocolsio_publish.py pcr_1-gt --publish    # public, permanent
  python scripts/protocolsio_publish.py pcr_1-gt --preview                          # print the content, send nothing

Token: protocols.io/developers (client access token: your own private content). API: apidoc.protocols.io
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

UA = "show-dont-code/1.0 (+https://github.com/PugarHuda/show-dont-code)"  # Cloudflare refuses Python's default (1010)
API = "https://pugarhuda--show-dont-code-web.us-east.modal.run"
SITE = "https://show-dont-code.vercel.app"
PIO = "https://www.protocols.io/api"


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


def _defaults(src):
    """protocol.py's runtime-parameter defaults (cycling program) and its Reactions ceiling, read from the file."""
    out = {m[1]: float(m[2]) for m in re.finditer(r"variable_name='(\w+)'[^\n]*?default=([\d.]+)", src)}
    top = re.search(r"variable_name='reactions'[^\n]*?maximum=(\d+)", src)
    out["max_reactions"] = int(top[1]) if top else 1
    return out


def _said(s):
    return f"video {float(s['start']):.0f}–{float(s['end']):.0f} s: \"{' '.join(str(s['description']).split())}\""


def content(run, R, api=API):
    """(fields, [(section, step text)]) for protocols.io, from the run's steps, deck layout, Opentrons' liquid setup and
    protocol.py's parameters. Nothing here is typed in by hand for a particular run."""
    steps = [s for s in R["steps"] if s["action"] != "skipped"]
    src, d = R["protocol"], _defaults(R["protocol"])
    adds = [s for s in steps if s["action"] == "transfer" and s.get("volume_ul")]
    total = sum(s["volume_ul"] for s in adds)
    layout, setup = R.get("layout") or {}, (R.get("analysis") or {}).get("liquid_setup") or []
    py, crate = f"{api}/api/runs/{run}/protocol.py", f"{api}/api/runs/{run}/ro-crate.zip"
    tc = [s for s in steps if s["action"] == "thermocycle"]
    program = []
    if "initial1_c" in d:
        program.append(f"initial denaturation {d['initial1_c']:g} °C for {d['initial1_s']:g} s")
    if "cycle1_c" in d:
        program.append(f"{d.get('cycles', 1):g} cycles of {d['cycle1_c']:g} °C {d['cycle1_s']:g} s, "
                       f"{d['cycle2_c']:g} °C {d['cycle2_s']:g} s, {d['cycle3_c']:g} °C {d['cycle3_s']:g} s")
    program.append("then 4 °C with the lid open (heated lid at 105 °C while cycling)")
    composition = "\n".join(f"- {s['reagent']}: {s['volume_ul']:g} µL" for s in adds) + f"\n- Total: {total:g} µL"
    fields = {
        "description": (
            f"Assembly of a {total:g} µL PCR ({', '.join(s['reagent'] for s in adds)}) on an Opentrons Flex with the "
            f"Thermocycler Module, for 1 to {d['max_reactions']} reactions in a 96-well PCR plate, followed by "
            "thermocycling on the deck. The steps and volumes were recovered from a first-person video of this reaction "
            "set up by hand (BioVL2 dataset, CC BY 4.0). They were turned into an Opentrons Flex protocol (Python API "
            "2.24) that passes the Opentrons simulator, opentrons analyze at every reaction count, and a run on "
            "Opentrons' robot-server. Each robot step below names the video seconds it came from, so it can be checked "
            f"against the original side by side: {SITE}/app?run={run}"),
        "guidelines": (
            f"Reaction composition, per reaction, as pipetted in the video:\n{composition}\n\n"
            "The video shows volumes, not stock concentrations. For example, with 10 µM primer stocks, 1.5 µL of each "
            f"in {total:g} µL gives 0.3 µM of each primer. Check this against your polymerase premix's "
            "recommendations.\n\n"
            "The robot uses a fresh tip for every reagent in every reaction. Reaction n goes into plate well n in column "
            "order (A1, B1 ... H1, A2 ...). The premix is pipetted with the viscous (50% glycerol) liquid class and "
            "everything else as aqueous; each reagent's liquid class can be changed in the Opentrons App.\n\n"
            f"Opentrons Flex protocol: {py}\nAll files with their SHA-256 hashes (RO-Crate): {crate}"),
        "materials_text": (
            "Reagents:\n" + "\n".join(f"- {s['reagent']}" for s in adds) +
            "\n\nEquipment:\n"
            "- Opentrons Flex with a Flex 1-Channel 50 µL pipette (left mount) and a Flex 1-Channel 1000 µL pipette "
            "(right mount)\n"
            "- Opentrons Flex Thermocycler Module GEN2 (slots A1 and B1) with an Opentrons 96-well 200 µL full-skirt "
            "PCR plate\n"
            "- Opentrons 24 tube rack with 1.5 mL Eppendorf Safe-Lock snap-cap tubes (slot C2)\n"
            "- Opentrons Flex 96 tip racks: 50 µL (slot D1, plus B2 and B3 above 96 reactions) and 1000 µL (slot C1)\n"
            "- Trash bin (slot A3)\n"
            "- Opentrons App (desktop)\n"
            "- Microcentrifuge"),
        "before_start": (
            "Thaw the reagents on ice, mix them gently and spin them down. Don't vortex the polymerase premix.\n\n"
            "Fill the 1.5 mL tubes in the 24 tube rack as follows for 1 reaction. Each volume includes a 10% excess plus "
            "30 µL the pipette cannot reach. For more reactions, the Opentrons App lists the volumes for the count you "
            "set:\n" + "\n".join(f"- {x['well']}: {x['liquid']}, {x['volume_ul']:g} µL" for x in setup)),
        "warning": (
            "Review every step against the video before running on hardware.\n\n" +
            ("The cycling program was not legible in the video. The protocol uses editable defaults: 98 °C "
             "denaturation, 55 °C annealing, 72 °C extension, 35 cycles. Set the annealing temperature for your primers "
             "and the extension time for your amplicon length; cycles, temperatures and times are runtime parameters "
             "in the Opentrons App.\n\n" if "none legible on the bench sheet" in src else "") +
            "The filmed protocol does not mix after the last addition. If your premix needs it, mix by pipetting or "
            "flicking and spin the plate down before cycling."),
    }
    sec = [
        ("Deck setup", "Put the PCR plate in the Thermocycler Module (slots A1 and B1, lid open), the 24 tube rack in "
         "C2, the 50 µL tip rack in D1 (also B2 and B3 above 96 reactions), the 1000 µL tip rack in C1 and the trash bin "
         "in A3."),
        ("Deck setup", "Load the reagent tubes into the rack as listed under Before start (" +
         ", ".join(f"{x['liquid']} in {x['well']}" for x in setup) + ") and open the caps."),
        ("Run setup", f"Import protocol.py ({py}) into the Opentrons App. Set Reactions (1 to {d['max_reactions']}) and "
         "check the cycling parameters. Optionally turn on 'Check tubes for liquid': the pipette probes each tube "
         "before drawing and stops on an empty one."),
        ("Run setup", "Run Labware Position Check, then start the run."),
    ]
    for s in adds:
        tube = layout.get(s["reagent"], "").split(":")[-1]
        sec.append(("Reaction assembly (robot)",
                    f"The 50 µL pipette transfers {s['volume_ul']:g} µL of {s['reagent']}"
                    + (f" from tube {tube}" if tube else "") + f" into each reaction well, with a new tip per well "
                    f"({_said(s)})."))
    for s in steps:
        if s["action"] not in ("transfer", "thermocycle"):
            sec.append(("By hand", f"{' '.join(str(s['description']).split())}: the robot pauses for you here "
                        f"({_said(s)})."))
    for s in tc:
        sec.append(("Thermocycling (robot)", "The Thermocycler Module closes its lid and runs " + "; ".join(program) +
                    f" ({_said(s)})."))
    sec.append(("After the run", "Take the plate out, spin it briefly and keep it at 4 °C until analysis, for example "
                "agarose gel electrophoresis of 5 µL per reaction."))
    return fields, sec


def _title(run):
    kind = "PCR setup" if run.startswith("pcr") else "Plasmid miniprep" if run.startswith("miniprep") else run
    return f"{kind} on the Opentrons Flex, recovered from a first-person video ({run}, Show, Don't Code)"


def _run(run, api):
    with urllib.request.urlopen(f"{api}/api/runs/{run}", timeout=180) as r:
        R = json.load(r)
    if not (R.get("protocol") and R.get("sim_ok")):
        raise SystemExit(f"{run} has no simulator-passing protocol yet: answer its open questions first")
    return R


def publish(run, token, doi=False, public=False, api=API, log=print, update=None):
    R = _run(run, api)
    fields, sec = content(run, R, api)
    if update:
        pid, guid = update, None
    else:
        guid = uuid.uuid4().hex.upper()
        p = _p("POST", f"/v3/protocols/{guid}", token, body={"type_id": 1})["protocol"]
        pid = p.get("id") or guid
    _p("PUT", f"/v4/protocols/{pid}", token, body={"title": _title(run), **fields})
    old = (_p("GET", f"/v4/protocols/{pid}/steps", token).get("payload") or []) if update else []
    if old:  # replace the draft's steps wholesale
        _p("DELETE", f"/v4/protocols/{pid}/steps", token, body={"steps": [o["guid"] for o in old]})
    prev, body = None, []
    for section, text in sec:
        g = uuid.uuid4().hex.upper()
        body.append({"guid": g, "previous_guid": prev, "step": text, "section": section})
        prev = g
    _p("POST", f"/v4/protocols/{pid}/steps", token, body={"steps": body})
    uri = _p("GET", f"/v4/protocols/{pid}", token)["payload"]["uri"]  # the URI follows the title
    log(f"protocol {pid} {'rewritten' if update else 'created'} with {len(body)} steps (private)")
    out = {"id": pid, "guid": guid, "steps": len(body), "url": f"https://www.protocols.io/edit/{uri}"}
    if doi or public:
        r = _p("POST", f"/v3/protocols/{uri}/publish", token, body={} if public else {"prepublish": 1})
        out["doi"] = (r.get("protocol") or {}).get("doi") or r.get("doi")
        out["public"] = public
        log(f"DOI {out['doi']} ({'public' if public else 'reserved, still private'})")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run")
    ap.add_argument("--doi", action="store_true", help="get a DOI but keep the protocol private (prepublish)")
    ap.add_argument("--publish", action="store_true", help="make it public (permanent)")
    ap.add_argument("--update", type=int, help="rewrite this existing protocol id instead of creating one")
    ap.add_argument("--preview", action="store_true", help="print the content and send nothing")
    ap.add_argument("--api", default=API)
    a = ap.parse_args()
    if a.preview:
        fields, sec = content(a.run, _run(a.run, a.api), a.api)
        print(_title(a.run))
        for k, v in fields.items():
            print(f"\n=== {k}\n{v}")
        print("\n=== steps")
        for i, (section, text) in enumerate(sec, 1):
            print(f"{i}. [{section}] {text}")
        return
    token = os.getenv("PROTOCOLSIO_TOKEN") or sys.exit("set PROTOCOLSIO_TOKEN")
    print(json.dumps(publish(a.run, token, a.doi, a.publish, a.api, update=a.update), indent=1))


if __name__ == "__main__":
    main()
