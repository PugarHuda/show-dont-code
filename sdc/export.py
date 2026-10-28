"""The same confirmed steps, exported for people and for other robots.

- methods_markdown: a methods section a scientist can paste into a notebook or paper
- autoprotocol: vendor-neutral Autoprotocol JSON (pipette + thermocycle instructions)
- pylabrobot_script / run_pylabrobot: a Hamilton STARlet script for PyLabRobot, executed in PyLabRobot's simulator
  with volume tracking on, so over-drawing a tube or a tip fails there too

Everything reads model text only as data: names land in string literals or markdown text, never in code.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .protocol import DEAD_UL, LIQUID_CLASSES, _plan, liquid_class, thermal

WELLS_96 = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"]


def _clean(text, n=80):
    return " ".join(str(text or "").split())[:n]


def _needs(steps):
    """Per source: reagent name, total µL drawn, number of transfers."""
    need = {}
    for s in steps:
        if s["action"] == "transfer":
            key = s.get("source") or s.get("reagent") or "reagent"
            name, total, n = need.get(key, (s.get("reagent") or key, 0.0, 0))
            need[key] = (name, total + float(s["volume_ul"]), n + 1)
    return need


def methods_markdown(steps, title="Protocol"):
    need = _needs(steps)
    lines = [f"# {_clean(title)}", "", "Recovered from a first-person video by Show, Don't Code and confirmed by the scientist.",
             "Times in brackets point to the moment in the source video.", "", "## Materials", "",
             "| Reagent | Prepare at least | Used for | Pipetted as |", "|---|---|---|---|"]
    for name, total, n in need.values():
        cls, _ = liquid_class(name)
        lines.append(f"| {_clean(name)} | {total * 1.1 + DEAD_UL:.1f} µL | {n} transfer{'s' * (n != 1)}, {total:g} µL in total "
                     f"| {LIQUID_CLASSES[cls].lower()} |")
    lines += ["", "## Procedure", ""]
    for i, s in enumerate(steps, 1):
        when = f"[{float(s['start']):.0f}–{float(s['end']):.0f} s]"
        if s["action"] == "transfer":
            text = (f"Transfer {float(s['volume_ul']):g} µL of {_clean(s.get('reagent') or s.get('source'))} "
                    f"into {_clean(s.get('destination') or 'the sample tube')}.")
        elif s["action"] == "mix":
            text = f"Mix {_clean(s.get('destination') or 'the sample tube')} by pipetting {float(s['volume_ul']):g} µL up and down 3 times."
        elif s["action"] == "thermocycle":
            p = thermal(s)
            fmt = lambda xs: ", ".join(f"{x['c']:g} °C {x['s']} s" for x in xs)  # noqa: E731
            text = (f"Place the tubes in the thermal cycler: " + (f"{fmt(p['initial'])}; " if p["initial"] else "")
                    + f"{int(s['cycles'])} cycles of {fmt(p['cycle'])}"
                    + (f"; then {fmt(p['final'])}" if p["final"] else "") + f"; hold at 4 °C ({p['source']}).")
        else:
            text = _clean(s["description"], 200).rstrip(".") + "."
        lines.append(f"{i}. {text} {when}")
    return "\n".join(lines) + "\n"


def autoprotocol(steps):
    """Autoprotocol JSON: refs for the reagent tubes and the reaction plate, one pipette instruction per transfer."""
    sources, dests, uses_tc = _plan(steps)
    refs = {"reagents": {"new": "96-deep", "discard": True}, "reactions": {"new": "96-pcr", "store": {"where": "cold_4"}}}
    # Autoprotocol addresses wells by container/index; reagent tubes become wells of a deep-well plate here
    # one index per tube, so tubes from different racks never share a well
    src_idx = {k: f"reagents/{i}" for i, k in enumerate(sources)}
    dst_idx = {k: f"reactions/{WELLS_96.index(w[1]) if uses_tc else i}" for i, (k, w) in enumerate(dests.items())}
    instructions = []
    for s in steps:
        if s["action"] == "transfer":
            src = src_idx[s.get("source") or s.get("reagent") or "reagent"]
            dst = dst_idx[s.get("destination") or "sample tube"]
            instructions.append({"op": "pipette", "groups": [{"transfer": [
                {"from": src, "to": dst, "volume": f"{float(s['volume_ul']):g}:microliter"}]}]})
        elif s["action"] == "thermocycle":
            instructions.append({"op": "seal", "object": "reactions", "type": "ultra-clear"})
            p = thermal(s)  # the same program protocol.py runs, so every export agrees
            steps_of = lambda xs: [{"temperature": f"{x['c']:g}:celsius", "duration": f"{x['s']}:second"} for x in xs]  # noqa: E731
            groups = ([{"cycles": 1, "steps": steps_of(p["initial"])}] if p["initial"] else []) +                 [{"cycles": int(s["cycles"]), "steps": steps_of(p["cycle"])}] +                 ([{"cycles": 1, "steps": steps_of(p["final"])}] if p["final"] else [])
            instructions.append({"op": "thermocycle", "object": "reactions", "volume": "50:microliter", "groups": groups})
    return {"refs": refs, "instructions": instructions}


def pylabrobot_script(steps):
    """A Hamilton STARlet translation for PyLabRobot. Reagent tubes sit in a 2 mL deep-well plate and reactions in a
    96-well plate on one plate carrier, because tube carriers differ from lab to lab."""
    sources, dests, _ = _plan(steps)
    need = _needs(steps)
    liquid = [s for s in steps if s["action"] in ("transfer", "mix")]
    big = any(float(s["volume_ul"]) > 50 for s in liquid)
    tip = "hamilton_96_tiprack_1000uL_filter" if big else "hamilton_96_tiprack_50uL_filter"
    racks = max(1, -(-len(liquid) // 96))
    if racks > 5:
        raise ValueError(f"{len(liquid)} transfers need {racks} tip racks; one STARlet tip carrier holds 5")
    src_well = {k: WELLS_96[i] for i, k in enumerate(sources)}
    dst_well = {k: WELLS_96[i] for i, k in enumerate(dests)}
    L = ['"""Generated by Show, Don\'t Code: Hamilton STARlet via PyLabRobot. Simulated with the chatterbox backend."""',
         "import asyncio", "from pylabrobot.liquid_handling import LiquidHandler",
         "from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend",
         "from pylabrobot.resources import (STARLetDeck, TIP_CAR_480_A00, PLT_CAR_L5AC_A00, cor_96_wellplate_2mL_Vb,",
         f"                                  cor_96_wellplate_360uL_Fb, set_volume_tracking, {tip})", "",
         "async def main(backend=None):",
         "    set_volume_tracking(True)  # over-drawing a tube or a tip is an error, not a silent pass",
         "    lh = LiquidHandler(backend=backend or LiquidHandlerChatterboxBackend(num_channels=8), deck=STARLetDeck())",
         "    await lh.setup()",
         "    tips = TIP_CAR_480_A00(name='tips')"]
    L += [f"    tips[{i}] = {tip}(name='tips_{i + 1}')" for i in range(racks)]
    L += ["    plates = PLT_CAR_L5AC_A00(name='plates')",
          "    plates[0] = cor_96_wellplate_2mL_Vb(name='reagents')", "    plates[1] = cor_96_wellplate_360uL_Fb(name='reactions')",
          "    lh.deck.assign_child_resource(tips, rails=3)", "    lh.deck.assign_child_resource(plates, rails=9)",
          "    reagents, reactions = lh.deck.get_resource('reagents'), lh.deck.get_resource('reactions')",
          f"    tip_spots = [spot for i in range({racks}) for spot in lh.deck.get_resource(f'tips_{{i + 1}}').get_all_items()]"]
    for key, (name, total, _) in need.items():
        L.append(f"    reagents[{src_well[key]!r}][0].tracker.set_volume({total * 1.1 + DEAD_UL:.1f})  # {_clean(name)!r}")
    n = 0
    for i, s in enumerate(steps):
        L.append(f"    # step {i + 1} [{float(s['start']):.0f}-{float(s['end']):.0f}s]: {_clean(s['description'], 120)}")
        if s["action"] == "transfer":
            v = float(s["volume_ul"])
            src, dst = src_well[s.get("source") or s.get("reagent") or "reagent"], dst_well[s.get("destination") or "sample tube"]
            L += [f"    await lh.pick_up_tips([tip_spots[{n}]])", f"    await lh.aspirate(reagents[{src!r}], vols=[{v}])",
                  f"    await lh.dispense(reactions[{dst!r}], vols=[{v}])", "    await lh.discard_tips()"]
            n += 1
        elif s["action"] == "mix":
            v, dst = float(s["volume_ul"]), dst_well[s.get("destination") or "sample tube"]
            L += [f"    await lh.pick_up_tips([tip_spots[{n}]])"] + [
                f"    await lh.aspirate(reactions[{dst!r}], vols=[{v}])\n    await lh.dispense(reactions[{dst!r}], vols=[{v}])"] * 3 + [
                "    await lh.discard_tips()"]
            n += 1
        else:
            L.append(f"    print({('Hand-off to the scientist: ' + _clean(s['description'], 120))!r})")
    L += ["    await lh.stop()", "", "if __name__ == '__main__':", "    asyncio.run(main())", ""]
    return "\n".join(L)


def run_pylabrobot(source, timeout=120):
    """Execute the generated script in a separate process with PyLabRobot's simulator. Returns (ok, last log lines)."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "plr_protocol.py"
        script.write_text(source, encoding="utf-8")
        p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=timeout,
                           env={"PYTHONIOENCODING": "utf-8", **__import__("os").environ})
    out = (p.stdout + p.stderr).strip().splitlines()
    actions = sum(1 for line in out if line.startswith(("Aspirating", "Dispensing")))
    return p.returncode == 0, {"actions": actions, "tail": out[-6:]}


if __name__ == "__main__":
    demo = [
        {"start": 30, "end": 45, "action": "transfer", "description": "Add water", "reagent": "water", "volume_ul": 20, "destination": "tube 1"},
        {"start": 50, "end": 60, "action": "transfer", "description": "Add primer", "reagent": "Primer \"1\"\nimport os", "volume_ul": 1.5, "destination": "tube 1"},
        {"start": 61, "end": 70, "action": "centrifuge", "description": "Spin down"},
        {"start": 80, "end": 90, "action": "thermocycle", "description": "Set in DNA engine", "cycles": 35},
    ]
    md = methods_markdown(demo, "Demo")
    assert "35 cycles" in md and "| water |" in md and "aqueous (water) |" in md, md
    ap = autoprotocol(demo)
    assert [i["op"] for i in ap["instructions"]] == ["pipette", "pipette", "seal", "thermocycle"], ap
    src = pylabrobot_script(demo)
    assert "\nimport os" not in src
    ok, log = run_pylabrobot(src)
    assert ok and log["actions"] == 4, log
    over = [{**demo[0], "volume_ul": 20}] * 2
    src2 = pylabrobot_script(over).replace("set_volume(74.0)", "set_volume(25.0)")
    assert run_pylabrobot(src2)[0] is False, "volume tracking must catch drawing 40 µL from a 25 µL tube"
    print("export self-check OK:", json.dumps(log))
