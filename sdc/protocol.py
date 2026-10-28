"""Structured steps -> Opentrons Flex protocol, plus simulation.

Codegen is deterministic on purpose: the model decides *what* happened, this file
decides *how* a robot does it, so a bad generation can never produce unsafe code.
"""
import io
import json

from opentrons.simulate import simulate as _simulate

ROBOT_ACTIONS = {"transfer", "mix", "thermocycle"}
TUBE_RACK = "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap"
PCR_PLATE = "opentrons_96_wellplate_200ul_pcr_full_skirt"
RACK_WELLS = [f"{r}{c}" for c in range(1, 7) for r in "ABCD"]  # column-major, like a human fills a rack
SPARE_SLOTS = ["B2", "B3", "C3", "D2", "D3", "A2"]  # free deck slots: A1/B1 thermocycler, A3 trash, C1/C2/D1 used
DEAD_UL, TUBE_UL = 30.0, 1500.0  # 1.5 mL snap-cap tube: unreachable bottom volume, capacity
LIQUID_COLORS = ["#2f63d8", "#d9480f", "#0f7a5c", "#9c36b5", "#e8590c", "#1098ad", "#5c940d", "#c2255c"]


class NeedsInput(Exception):
    """Steps are missing values only the scientist can give (e.g. a volume)."""

    def __init__(self, questions):
        super().__init__(f"{len(questions)} step(s) need confirmation")
        self.questions = questions


MIN_UL = 1.0  # smallest volume of the Flex 1-channel 50 µL pipette; the simulator does not enforce it


# used only when the bench sheet shows no cycling program; every value stays editable in the Opentrons App
DEFAULT_PROFILE = {"initial": [{"c": 98.0, "s": 30}],
                   "cycle": [{"c": 98.0, "s": 10}, {"c": 55.0, "s": 5}, {"c": 72.0, "s": 5}],
                   "final": [], "source": "default 3-step program (none legible on the bench sheet)"}


UNWRITTEN_S = 30  # a step whose temperature is on the sheet but whose time is not


def thermal(step):
    """The cycling program of a thermocycle step: the one Muse Spark read off the bench sheet, else the default.
    Model numbers are untrusted: kept only inside what the Flex Thermocycler can do."""
    p = step.get("profile") or {}

    guessed = []

    def clean(xs, n):
        out = []
        for x in (xs or [])[:n]:
            try:
                c = float(x["c"])
                sec = UNWRITTEN_S if x.get("s") is None else int(x["s"])
            except (KeyError, TypeError, ValueError):
                continue
            if x.get("s") is None and 4 <= c <= 99:
                guessed.append(f"{c:g} °C")
            if 4 <= c <= 99 and 1 <= sec <= 3600:
                out.append({"c": c, "s": sec})
        return out

    cycle = clean(p.get("cycle"), 3)
    if not cycle:
        return dict(DEFAULT_PROFILE)
    initial, final = clean(p.get("initial"), 2), clean(p.get("final"), 2)
    note = f"; no time written for {', '.join(guessed)}, {UNWRITTEN_S} s used" if guessed else ""
    return {"initial": initial, "cycle": cycle, "final": final, "source": "bench sheet, read by Muse Spark" + note}


def missing_inputs(steps):
    q = []
    for i, s in enumerate(steps):
        if s["action"] in ("transfer", "mix"):
            v = s.get("volume_ul")
            if not v:
                q.append({"step": i, "field": "volume_ul", "description": s["description"], "reason": ""})
            elif v < MIN_UL:
                q.append({"step": i, "field": "volume_ul", "description": s["description"],
                          "reason": f"{v:g} µL is below the robot pipette's {MIN_UL:g} µL minimum: dilute the stock "
                                    f"and enter the new volume"})
        elif s["action"] == "thermocycle" and not s.get("cycles"):
            q.append({"step": i, "field": "cycles", "description": s["description"],
                      "reason": "the video can't show the cycling program"})
    return q


def layout(steps):
    """Deck placement for every source and destination name. Needs no volumes, so the UI can show the deck
    while questions are still open."""
    sources, dests, _ = _plan(steps)
    return {n: f"{r}:{w}" for n, (r, w) in {**sources, **dests}.items()}


def _plan(steps):
    uses_tc = any(s["action"] == "thermocycle" for s in steps)
    sources, dests = {}, {}
    for s in steps:
        if s["action"] == "transfer":
            sources.setdefault(s.get("source") or s.get("reagent") or "reagent", None)
        if s["action"] in ("transfer", "mix"):
            dests.setdefault(s.get("destination") or "sample tube", None)
    if len(sources) > len(RACK_WELLS):
        raise ValueError(f"{len(sources)} reagents do not fit one 24-tube rack")  # ponytail: add a second rack when needed
    for name, well in zip(sources, RACK_WELLS):
        sources[name] = ("reagents", well)
    for i, name in enumerate(dests):
        dests[name] = ("dest", f"{'ABCDEFGH'[i % 8]}{i // 8 + 1}") if uses_tc else ("reagents", RACK_WELLS[len(sources) + i])
    return sources, dests, uses_tc


def generate(steps, title="Show, Don't Code protocol"):
    """Return (python_source, layout). layout maps each reagent/tube name to a deck location."""
    questions = missing_inputs(steps)
    if questions:
        raise NeedsInput(questions)
    sources, dests, uses_tc = _plan(steps)

    liquid_steps = [s for s in steps if s["action"] in ("transfer", "mix")]
    n50 = sum(1 for s in liquid_steps if float(s["volume_ul"]) <= 50)  # one fresh tip per transfer / mix
    n1000 = len(liquid_steps) - n50
    small = n50 > 0
    free = iter(SPARE_SLOTS)  # extra tip racks go to empty slots; a run with >96 transfers needs more than one

    def racks(name, count, first_slot):
        slots = [first_slot] + [next(free) for _ in range(max(1, -(-count // 96)) - 1)]
        return [f"ctx.load_labware({name!r}, {slot!r})" for slot in slots]

    L = [
        "# Generated by Show, Don't Code from a video of a human performing the protocol.",
        "# Review every step against the side-by-side video before running on hardware.",
        f"metadata = {{'protocolName': {title!r}}}",
        "requirements = {'robotType': 'Flex', 'apiLevel': '2.20'}",
        "",
    ]
    # thermal programs become Opentrons runtime parameters: the lab adjusts them in the App at run setup, no code
    tcs = [(i, thermal(s)) for i, s in enumerate(steps) if s["action"] == "thermocycle"]
    if tcs:
        L.append("def add_parameters(parameters):")
    for j, (i, prof) in enumerate(tcs):
        pre = f"pcr{j + 1}_" if len(tcs) > 1 else ""
        tag = f"PCR {j + 1} " if len(tcs) > 1 else ""
        L.append(f"    # step {i + 1} cycling program: {prof['source']}")
        L.append(f"    parameters.add_int(variable_name={pre + 'cycles'!r}, display_name={tag + 'Cycles'!r}, "
                 f"description='Number of cycles', default={int(steps[i]['cycles'])}, minimum=1, maximum=99)")
        for stage in ("initial", "cycle", "final"):
            for k, st in enumerate(prof[stage]):
                name = f"{tag}{stage.title()} {k + 1}"
                L.append(f"    parameters.add_float(variable_name={f'{pre}{stage}{k + 1}_c'!r}, display_name={name + ' temp'!r}, "
                         f"default={st['c']}, minimum=4.0, maximum=99.0, unit='C')")
                L.append(f"    parameters.add_int(variable_name={f'{pre}{stage}{k + 1}_s'!r}, display_name={name + ' time'!r}, "
                         f"default={st['s']}, minimum=1, maximum=3600, unit='s')")
    if tcs:
        L.append("")
    L += [
        "def run(ctx):",
        "    ctx.load_trash_bin('A3')",
        f"    reagents = ctx.load_labware({TUBE_RACK!r}, 'C2', 'reagent tubes')",
    ]
    if uses_tc:
        L += [
            "    tc = ctx.load_module('thermocyclerModuleV2')",
            "    tc.open_lid()",
            f"    dest = tc.load_labware({PCR_PLATE!r})",
        ]
    L += [
        f"    tips1000 = [{', '.join(racks('opentrons_flex_96_tiprack_1000ul', n1000, 'C1'))}]",
        "    p1000 = ctx.load_instrument('flex_1channel_1000', 'right', tip_racks=tips1000)",
    ]
    if small:
        L += [
            f"    tips50 = [{', '.join(racks('opentrons_flex_96_tiprack_50ul', n50, 'D1'))}]",
            "    p50 = ctx.load_instrument('flex_1channel_50', 'left', tip_racks=tips50)",
        ]
    # liquids: the Opentrons App shows these on its deck map and in Liquid Setup before anyone presses run
    need = {}
    for s in steps:
        if s["action"] == "transfer":
            key = s.get("source") or s.get("reagent") or "reagent"
            reagent, total = need.get(key, (s.get("reagent") or key, 0.0))
            need[key] = (reagent, total + float(s["volume_ul"]))
    for n, (key, (reagent, total)) in enumerate(need.items()):
        name = " ".join(str(reagent).split())[:60]  # model text only ever lands inside a string literal
        load = min(round(total * 1.1 + DEAD_UL, 1), TUBE_UL)  # 10 % overage plus the tube's dead volume
        L += [f"    liquid{n} = ctx.define_liquid({name!r}, description={f'{total:g} uL drawn over the run'!r}, "
              f"display_color={LIQUID_COLORS[n % len(LIQUID_COLORS)]!r})",
              f"    reagents[{sources[key][1]!r}].load_liquid(liquid{n}, {load})"]

    def loc(place):
        rack, well = place
        return f"{rack}[{well!r}]"

    for i, s in enumerate(steps):
        # model output never reaches the source raw: one-line comment, numbers forced to float
        desc = " ".join(str(s["description"]).split())
        L.append(f"    # step {i + 1} [{float(s['start']):.0f}-{float(s['end']):.0f}s]: {desc}")
        a = s["action"]
        if a == "transfer":
            v = float(s["volume_ul"])
            pip = "p50" if v <= 50 else "p1000"
            src = loc(sources[s.get("source") or s.get("reagent") or "reagent"])
            dst = loc(dests[s.get("destination") or "sample tube"])
            L.append(f"    {pip}.transfer({v}, {src}, {dst}, new_tip='always')")
        elif a == "mix":
            v = float(s["volume_ul"])
            pip = "p50" if v <= 50 else "p1000"
            dst = loc(dests[s.get("destination") or "sample tube"])
            L += [f"    {pip}.pick_up_tip()", f"    {pip}.mix(3, {v}, {dst})", f"    {pip}.drop_tip()"]
        elif a == "thermocycle":
            j = [t[0] for t in tcs].index(i)
            pre = f"pcr{j + 1}_" if len(tcs) > 1 else ""
            prof = tcs[j][1]

            def stage(name, reps):
                return (f"    tc.execute_profile(steps=[" + ", ".join(
                    f"{{'temperature': ctx.params.{pre}{name}{k + 1}_c, 'hold_time_seconds': ctx.params.{pre}{name}{k + 1}_s}}"
                    for k in range(len(prof[name]))) + f"], repetitions={reps}, block_max_volume=50)")
            L += ["    tc.close_lid()", "    tc.set_lid_temperature(105)"]
            L += [stage("initial", 1)] if prof["initial"] else []
            L += [stage("cycle", f"ctx.params.{pre}cycles")]
            L += [stage("final", 1)] if prof["final"] else []
            L += ["    tc.set_block_temperature(4)", "    tc.open_lid()"]
        elif a == "skipped":  # the scientist accepted that this step never happened
            L.append("    # skipped: did not happen in the video (reviewed)")
        else:
            L.append(f"    ctx.pause({('Manual step: ' + desc)!r})")
    layout = {n: f"{r}:{w}" for n, (r, w) in {**sources, **dests}.items()}
    return "\n".join(L) + "\n", layout


def analyze(source, json_path):
    """Opentrons' own protocol analysis (what the Opentrons App runs on import). Writes the full JSON to json_path
    and returns (ok, summary) where summary lists what to load into which tube before the run."""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        proto = Path(tmp) / "protocol.py"
        proto.write_text(source, encoding="utf-8")
        p = subprocess.run([sys.executable, "-m", "opentrons.cli", "analyze", "--json-output", str(json_path), str(proto)],
                           capture_output=True, text=True, timeout=240)
    if p.returncode != 0 or not Path(json_path).exists():
        return False, {"error": (p.stderr or p.stdout)[-300:]}
    a = json.loads(Path(json_path).read_text(encoding="utf-8"))
    liquids = {q["id"]: q for q in a.get("liquids", [])}
    labware = {lw["id"]: lw for lw in a.get("labware", [])}
    setup = []
    for c in a.get("commands", []):
        if c.get("commandType") == "loadLiquid":
            q, lw = liquids.get(c["params"]["liquidId"], {}), labware.get(c["params"]["labwareId"], {})
            for well, vol in c["params"]["volumeByWell"].items():
                setup.append({"liquid": q.get("displayName"), "color": q.get("displayColor"),
                              "labware": lw.get("displayName") or lw.get("loadName"), "well": well, "volume_ul": vol})
    return a.get("result") == "ok" and not a.get("errors"), {
        "result": a.get("result"), "errors": [e.get("detail", str(e))[:200] for e in a.get("errors", [])],
        "commands": len(a.get("commands", [])), "liquid_setup": setup,
        # runtime parameters as the Opentrons App will offer them at run setup
        "parameters": [{"name": q.get("displayName"), "value": q.get("default"), "unit": q.get("suffix", "")}
                       for q in a.get("runTimeParameters", [])]}


def simulate(source):
    """Run the official Opentrons simulator. Returns (ok, log_lines_or_error)."""
    f = io.StringIO(source)
    f.name = "protocol.py"
    try:
        runlog, _ = _simulate(f, file_name="protocol.py")
    except Exception as e:  # simulator raises arbitrary errors for bad protocols; surface them to the agent
        return False, [f"{type(e).__name__}: {e}"]
    return True, [entry["payload"]["text"] for entry in runlog]


if __name__ == "__main__":
    demo = [
        {"start": 30, "end": 45, "action": "transfer", "description": "Add sterile water", "reagent": "water", "volume_ul": 20, "destination": "PCR tube"},
        {"start": 64, "end": 99, "action": "transfer", "description": "Add Primer1", "reagent": "Primer1", "volume_ul": 1, "destination": "PCR tube"},
        {"start": 190, "end": 238, "action": "transfer", "description": "Add premix", "reagent": "PrimeSTAR Max Premix", "volume_ul": 25, "destination": "PCR tube"},
        {"start": 239, "end": 250, "action": "mix", "description": "Pipette to mix", "volume_ul": 20, "destination": "PCR tube"},
        {"start": 251, "end": 255, "action": "centrifuge", "description": "Spin down"},
        {"start": 260, "end": 266, "action": "thermocycle", "description": "Set in DNA engine", "cycles": 35},
    ]
    for bad, field in (({**demo[0], "volume_ul": None}, "volume_ul"), ({**demo[0], "volume_ul": 0.25}, "volume_ul"),
                       ({**demo[5], "cycles": None}, "cycles")):
        try:
            generate([bad])
            raise AssertionError(f"{bad} must raise")
        except NeedsInput as e:
            assert e.questions[0]["field"] == field, e.questions
    evil, _ = generate([{**demo[4], "description": "x\nimport os; os.system('calc')"}])
    assert "\nimport os" not in evil, "description must stay inside the comment/string"
    src, layout = generate(demo)
    ok, log = simulate(src)
    assert ok, log
    assert any("Primer1" not in l and "Transferring 1.0" in l for l in log), log
    assert layout["Primer1"] == "reagents:B1", layout
    assert "default=35" in src and "repetitions=ctx.params.cycles" in src and "default=98.0" in src, src
    # a program read off the sheet replaces the default; out-of-range model numbers are dropped, not run
    sheet = {**demo[5], "profile": {"initial": [{"c": 94, "s": 300}], "cycle": [{"c": 94, "s": 30}, {"c": 55, "s": 30},
             {"c": 72, "s": 60}, {"c": 50, "s": 1}], "final": [{"c": 72, "s": 300}, {"c": 150, "s": 5}]}}
    prof = thermal(sheet)
    assert len(prof["cycle"]) == 3 and prof["final"] == [{"c": 72.0, "s": 300}] and prof["source"].startswith("bench"), prof
    src2, _ = generate(demo[:5] + [sheet])
    ok, log2 = simulate(src2)
    assert ok, log2
    assert "variable_name='final1_c'" in src2 and "default=94.0" in src2 and "default=98.0" not in src2, src2
    assert thermal({"profile": {"cycle": [{"c": "x", "s": 5}]}})["source"].startswith("default")
    p = thermal({"profile": {"cycle": [{"c": 94, "s": 30}, {"c": 72, "s": None}]}})
    assert p["cycle"][1] == {"c": 72.0, "s": 30} and "no time written for 72 °C" in p["source"], p
    print(json.dumps(layout), f"\n{len(log)} simulator lines OK")
