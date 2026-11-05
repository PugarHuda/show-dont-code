"""Structured steps -> Opentrons Flex protocol, plus simulation.

Codegen is deterministic on purpose: the model decides *what* happened, this file
decides *how* a robot does it, so a bad generation can never produce unsafe code.
"""
import io
import json
import os

from opentrons.simulate import simulate as _simulate

ROBOT_ACTIONS = {"transfer", "mix", "thermocycle"}
TUBE_RACK = "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap"
PCR_PLATE = "opentrons_96_wellplate_200ul_pcr_full_skirt"
RACK_WELLS = [f"{r}{c}" for c in range(1, 7) for r in "ABCD"]  # column-major, like a human fills a rack
SPARE_SLOTS = ["B2", "B3", "C3", "D2", "D3", "A2"]  # free deck slots: A1/B1 thermocycler, A3 trash, C1/C2/D1 used
DEAD_UL, TUBE_UL = 30.0, 1500.0  # 1.5 mL snap-cap tube: unreachable bottom volume, capacity
# Thermocycler GEN2 block, docs.opentrons.com/thermocycler/product-specs: max 4.25 °C/s heating, 2.0 °C/s cooling
TC_HEAT, TC_COOL, AMBIENT_C = 4.25, 2.0, 25.0
# the two robots the same steps can target. OT-2: opentrons 10.x only runs Flex protocols, so OT-2 files are checked
# with opentrons 9.0.0 in its own environment (OT2_PYTHON); no liquid classes and no liquid sensor on an OT-2
FLEX = {"name": "Flex", "api": "2.24", "small_ul": 50, "small": ("p50", "flex_1channel_50", "opentrons_flex_96_tiprack_50ul"),
        "big": ("p1000", "flex_1channel_1000", "opentrons_flex_96_tiprack_1000ul"), "rack": "C2", "big_tips": "C1",
        "small_tips": "D1", "spare": SPARE_SLOTS, "flex": True}
OT2 = {"name": "OT-2", "api": "2.20", "small_ul": 20, "small": ("p20", "p20_single_gen2", "opentrons_96_tiprack_20ul"),
       "big": ("p300", "p300_single_gen2", "opentrons_96_tiprack_300ul"), "rack": "5", "big_tips": "4",
       "small_tips": "1", "spare": ["2", "3", "6", "9"], "flex": False}  # thermocycler takes 7, 8, 10, 11; trash 12
ROBOTS = {"Flex": FLEX, "OT-2": OT2}
LIQUID_COLORS = ["#2f63d8", "#d9480f", "#0f7a5c", "#9c36b5", "#e8590c", "#1098ad", "#5c940d", "#c2255c"]


def load_ul(drawn):
    """What to fill a source tube with: 10 % overage plus the tube's dead volume, capped at the tube."""
    return min(round(drawn * 1.1 + DEAD_UL, 1), TUBE_UL)


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

# Opentrons' built-in Flex liquid classes (API 2.24): flow rates, delays, air gaps and blowout tuned per pipette and tip
LIQUID_CLASSES = {"water": "Aqueous (water)", "glycerol_50": "Viscous (50% glycerol)", "ethanol_80": "Volatile (80% ethanol)"}
VISCOUS = ("premix", "master mix", "mastermix", "polymerase", "enzyme", "glycerol", "taq", "phusion", "primestar", "2x", "2 x")
VOLATILE = ("ethanol", "etoh", "isopropanol", "propanol", "methanol", "acetone")


def liquid_class(reagent):
    """(class, why) from the reagent's name; the lab can change it in the Opentrons App (runtime parameter)."""
    name = " ".join(str(reagent or "").lower().split())
    for cls, words in (("ethanol_80", VOLATILE), ("glycerol_50", VISCOUS)):
        hit = next((w for w in words if w in name), None)
        if hit:
            return cls, f"name contains '{hit}'"
    return "water", "no viscous or volatile keyword in the name"


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
                      "reason": "the cycle count isn't stated"})
    return q


def layout(steps):
    """Deck placement for every source and destination name. Needs no volumes, so the UI can show the deck
    while questions are still open."""
    sources, dests, _ = _plan(steps)
    return {n: f"{r}:{w}" for n, (r, w) in {**sources, **dests}.items()}


def _plan(steps, bot=FLEX):
    uses_tc = any(s["action"] == "thermocycle" for s in steps)
    sources, dests = {}, {}
    for s in steps:
        if s["action"] == "transfer":
            sources.setdefault(s.get("source") or s.get("reagent") or "reagent", None)
        if s["action"] in ("transfer", "mix"):
            dests.setdefault(s.get("destination") or "sample tube", None)
    # tubes fill rack "reagents" (C2), then "reagents2", "reagents3"... in the spare slots, 24 tubes each
    tubes = len(sources) + (0 if uses_tc else len(dests))
    n_racks = -(-tubes // len(RACK_WELLS))
    if n_racks - 1 > len(bot["spare"]):
        raise ValueError(f"{tubes} tubes need {n_racks} tube racks; the {bot['name']} deck has room for {len(bot['spare']) + 1}")
    if uses_tc and len(dests) > 96:
        raise ValueError(f"{len(dests)} reaction tubes do not fit the 96-well PCR plate")

    def tube(k):
        return ("reagents" if k < len(RACK_WELLS) else f"reagents{k // len(RACK_WELLS) + 1}", RACK_WELLS[k % len(RACK_WELLS)])
    for k, name in enumerate(sources):
        sources[name] = tube(k)
    for i, name in enumerate(dests):
        dests[name] = ("dest", f"{'ABCDEFGH'[i % 8]}{i // 8 + 1}") if uses_tc else tube(len(sources) + i)
    return sources, dests, uses_tc


def extra_racks(sources, dests, bot=FLEX):
    """Tube racks beyond the first, in load order, each with its deck slot."""
    names = sorted({r for r, _ in {**sources, **dests}.values() if r.startswith("reagents") and r != "reagents"},
                   key=lambda r: int(r[len("reagents"):]))
    return list(zip(names, bot["spare"]))


def _drawn(steps):
    """Source tube -> (reagent, total µL drawn for one reaction), in first-use order."""
    need = {}
    for s in steps:
        if s["action"] == "transfer":
            key = s.get("source") or s.get("reagent") or "reagent"
            reagent, total = need.get(key, (s.get("reagent") or key, 0.0))
            need[key] = (reagent, total + float(s["volume_ul"]))
    return need


def _tip_counts(steps, bot=FLEX):
    liquid = [s for s in steps if s["action"] in ("transfer", "mix")]
    n50 = sum(1 for s in liquid if float(s["volume_ul"]) <= bot["small_ul"])  # one fresh tip per transfer / mix
    return n50, len(liquid) - n50


def max_reactions(steps, bot=FLEX):
    """How many copies of the filmed reaction the deck can set up in the PCR plate: limited by the plate's wells,
    by a 1.5 mL tube holding every reaction's draw (+10 % and dead volume), and by deck slots for tip racks.
    1 when the protocol has no thermocycler plate (its destinations are named tubes in the rack)."""
    sources, dests, uses_tc = _plan(steps, bot)
    if not uses_tc or not dests:
        return 1
    n = 96 // len(dests)
    for _, total in _drawn(steps).values():
        if total > 0:
            n = min(n, int((TUBE_UL - DEAD_UL) / 1.1 / total))
    n50, n1000 = _tip_counts(steps, bot)
    free = len(bot["spare"]) - len(extra_racks(sources, dests, bot))
    while n > 1 and max(0, -(-n50 * n // 96) - 1) + max(0, -(-n1000 * n // 96) - 1) > free:
        n -= 1
    return max(n, 1)


def well_volume(steps):
    """The fullest reaction well (uL), so the thermocycler models the right thermal mass; its block takes 1-100 uL."""
    well = {}
    for s in steps:
        if s["action"] == "transfer" and s.get("volume_ul"):
            k = s.get("destination") or "sample tube"
            well[k] = well.get(k, 0) + float(s["volume_ul"])
    return max(1, min(100, round(max(well.values(), default=50))))


# Opentrons' published Flex 1-channel accuracy, docs.opentrons.com/flex/system-description/pipettes/:
# (volume uL, random error %CV, systematic error %D). p50 points use the 50 uL tips this protocol loads; p1000 points
# are with 50/200/1000 uL tips (Opentrons publishes no 1000 uL-tip figure below 1000 uL)
FLEX_SPEC = {"flex_1channel_50": [(1, 7.0, 8.0), (10, 0.5, 1.5), (50, 0.4, 1.25)],
             "flex_1channel_1000": [(50, 0.3, 0.5), (200, 0.15, 0.5), (1000, 0.15, 0.5)]}


def _spec(pipette, ul):
    """(%CV, %D) at ul, log-linear between Opentrons' published points; held at the ends outside them."""
    import math
    pts = FLEX_SPEC[pipette]
    if ul <= pts[0][0]:
        return pts[0][1:]
    for (v0, c0, d0), (v1, c1, d1) in zip(pts, pts[1:]):
        if ul <= v1:
            f = math.log(ul / v0) / math.log(v1 / v0)
            return c0 + f * (c1 - c0), d0 + f * (d1 - d0)
    return pts[-1][1:]


def pipetting_error(steps, bot=FLEX):
    """Expected error of every reagent's share of its reaction, from the Flex pipettes' published accuracy.
    Each transfer's volume is off by up to %D (systematic) with %CV scatter; a reagent's share a/V of its well
    moves with its own error and, the other way, with every other transfer into that well. Worst-case systematic
    errors add; random ones add in quadrature."""
    wells = {}
    for s in steps:
        if s["action"] != "transfer" or not s.get("volume_ul"):
            continue
        v = float(s["volume_ul"])
        pip = bot["small"][1] if v <= bot["small_ul"] else bot["big"][1]
        cv, d = _spec(pip, v)
        g = wells.setdefault(s.get("destination") or "sample tube", {}).setdefault(
            s.get("reagent") or s.get("source") or "reagent", {"ul": 0.0, "var": 0.0, "sys": 0.0, "pip": set(), "worst": 0.0})
        g["ul"] += v; g["var"] += (cv / 100 * v) ** 2; g["sys"] += d / 100 * v
        g["pip"].add(bot["small"][0] if v <= bot["small_ul"] else bot["big"][0]); g["worst"] = max(g["worst"], d)
    rows = []
    for dest, gs in wells.items():
        total = sum(g["ul"] for g in gs.values())
        for name, g in gs.items():
            sh = g["ul"] / total
            others_var = sum(h["var"] for k, h in gs.items() if k != name)
            others_sys = sum(h["sys"] for k, h in gs.items() if k != name)
            rows.append({"destination": dest, "reagent": name, "ul": round(g["ul"], 2), "share": round(sh * 100, 1),
                         "pipettes": sorted(g["pip"]), "transfer_d": round(g["worst"], 2),
                         "share_cv": round(100 * ((((1 - sh) ** 2) * g["var"] + sh ** 2 * others_var) ** 0.5) / g["ul"], 2),
                         "share_d": round(100 * ((1 - sh) * g["sys"] + sh * others_sys) / g["ul"], 2)})
    return rows


def generate(steps, title="Show, Don't Code protocol", robot="Flex"):
    """Return (python_source, layout). layout maps each reagent/tube name to a deck location. robot: "Flex" or "OT-2"."""
    bot = ROBOTS[robot]
    questions = missing_inputs(steps)
    if questions:
        raise NeedsInput(questions)
    sources, dests, uses_tc = _plan(steps, bot)

    n50, n1000 = _tip_counts(steps, bot)
    small = n50 > 0
    more = extra_racks(sources, dests, bot)
    free = iter(bot["spare"][len(more):])  # extra tip racks go to the slots tube racks left; >96 transfers need more
    top = max_reactions(steps, bot)  # >1: a "Reactions" runtime parameter repeats the filmed reaction down the plate
    R = "ctx.params.reactions" if top > 1 else None

    def racks(name, count, first_slot):
        """Tip racks for `count` tips per reaction. With a Reactions parameter, the slots for the most reactions are
        set aside here and the protocol loads only as many racks as the chosen number needs."""
        n = max(1, -(-count * top // 96))
        slots = [first_slot] + [slot for _, slot in zip(range(n - 1), free)]
        if len(slots) < n:
            raise ValueError(f"{count} transfers need {n} racks of {name}; no free deck slot left")
        if R and n > 1:
            return f"[ctx.load_labware({name!r}, s) for s in {slots!r}[:max(1, -(-{count} * {R} // 96))]]"
        return "[" + ", ".join(f"ctx.load_labware({name!r}, {slot!r})" for slot in slots) + "]"

    need = _drawn(steps)  # source tube -> (reagent, total uL drawn); one liquid and one liquid class each
    lc_of = {key: n for n, key in enumerate(need)} if bot["flex"] else {}

    L = [
        "# Generated by Show, Don't Code from a video of a human performing the protocol.",
        "# Review every step against the side-by-side video before running on hardware.",
        f"metadata = {{'protocolName': {title!r}}}",
        f"requirements = {{'robotType': {bot['name']!r}, 'apiLevel': {bot['api']!r}}}",
        "",
    ]
    # thermal programs become Opentrons runtime parameters: the lab adjusts them in the App at run setup, no code
    tcs = [(i, thermal(s)) for i, s in enumerate(steps) if s["action"] == "thermocycle"]
    if tcs or R or lc_of:
        L.append("def add_parameters(parameters):")
    if R:  # film one reaction, run a plate: the lab picks the number on the robot's touchscreen
        L.append("    parameters.add_int(variable_name='reactions', display_name='Reactions', description="
                 "'Copies of the filmed reaction, in the next PCR plate wells; tubes are filled for all of them', "
                 f"default=1, minimum=1, maximum={top})")
    if need and bot["flex"]:  # the Flex pipettes' pressure sensor: off by default because probing adds seconds to every draw
        L.append("    parameters.add_bool(variable_name='detect_liquid', display_name='Check tubes for liquid', "
                 "description='Probe each source tube with the pipette sensor before drawing; an empty tube stops the run', "
                 "default=False)")
    choices = [{"display_name": d, "value": v} for v, d in LIQUID_CLASSES.items()]
    for key, n in lc_of.items():
        cls, why = liquid_class(need[key][0])
        label = " ".join(str(need[key][0]).split())
        label = label if len(label) <= 30 else label[:29] + "…"  # App limit: 30 characters
        L.append(f"    parameters.add_str(variable_name='lc{n}', display_name={label!r}, "
                 f"description={('Liquid class (how the pipette handles it): ' + why)[:100]!r}, choices={choices!r}, default={cls!r})")
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
    if tcs or R or lc_of:
        L.append("")
    L += [
        "def run(ctx):",
    ] + (["    ctx.load_trash_bin('A3')"] if bot["flex"] else []) + [
        f"    reagents = ctx.load_labware({TUBE_RACK!r}, {bot['rack']!r}, 'reagent tubes')",
    ]
    L += [f"    {rack} = ctx.load_labware({TUBE_RACK!r}, {slot!r}, {'reagent tubes ' + rack[len('reagents'):]!r})"
          for rack, slot in more]
    if uses_tc:
        L += [
            "    tc = ctx.load_module('thermocyclerModuleV2')",
            "    tc.open_lid()",
            f"    dest = tc.load_labware({PCR_PLATE!r})",
        ]
    (big, big_model, big_rack), (sm, sm_model, sm_rack) = bot["big"], bot["small"]
    L += [
        f"    tips{big[1:]} = {racks(big_rack, n1000, bot['big_tips'])}",
        f"    {big} = ctx.load_instrument({big_model!r}, 'right', tip_racks=tips{big[1:]})",
    ]
    if small:
        L += [
            f"    tips{sm[1:]} = {racks(sm_rack, n50, bot['small_tips'])}",
            f"    {sm} = ctx.load_instrument({sm_model!r}, 'left', tip_racks=tips{sm[1:]})",
        ]
    # liquids: the Opentrons App shows these on its deck map and in Liquid Setup before anyone presses run
    for n, (key, (reagent, total)) in enumerate(need.items()):
        name = " ".join(str(reagent).split())[:60]  # model text only ever lands inside a string literal
        # same rule as load_ul(), for however many reactions the lab picked
        load = f"min(round({total} * {R} * 1.1 + {DEAD_UL}, 1), {TUBE_UL})" if R else load_ul(total)
        what = f"{total:g} uL drawn per reaction" if R else f"{total:g} uL drawn over the run"
        L += [f"    liquid{n} = ctx.define_liquid({name!r}, description={what!r}, "
              f"display_color={LIQUID_COLORS[n % len(LIQUID_COLORS)]!r})",
              f"    {sources[key][0]}[{sources[key][1]!r}].load_liquid(liquid{n}, {load})"]
        if key in lc_of:
            L.append(f"    lc{n} = ctx.get_liquid_class(ctx.params.lc{n})")

    dest_index = {name: i for i, name in enumerate(dests)}
    block_ul = well_volume(steps)

    def loc(place):
        rack, well = place
        return f"{rack}[{well!r}]"

    def where(name):  # a destination; with Reactions, reaction r's copy sits len(dests) wells further down the plate
        return f"dest.wells()[{dest_index[name]} + {len(dests)} * r]" if R else loc(dests[name])

    def each(lines):  # the filmed step, once per reaction
        return ([f"    for r in range({R}):"] + ["    " + x for x in lines]) if R else lines

    for i, s in enumerate(steps):
        # model output never reaches the source raw: one-line comment, numbers forced to float
        desc = " ".join(str(s["description"]).split())
        t0, t1 = float(s["start"]), float(s["end"])
        L.append(f"    # step {i + 1} [{t0:.0f}-{t1:.0f}s]: {desc}")
        # shown in the Opentrons App's run timeline, so whoever stands at the robot can find the moment in the video
        L.append(f"    ctx.comment({f'Step {i + 1} · video {t0:.0f}-{t1:.0f} s: {desc[:120]}'!r})")
        a = s["action"]
        if a == "transfer":
            v = float(s["volume_ul"])
            pip = sm if v <= bot["small_ul"] else big
            key = s.get("source") or s.get("reagent") or "reagent"
            src, dst = loc(sources[key]), where(s.get("destination") or "sample tube")
            if bot["flex"]:
                move = ["    if ctx.params.detect_liquid:", f"        {pip}.require_liquid_presence({src})",
                        f"    {pip}.transfer_with_liquid_class(liquid_class=lc{lc_of[key]}, volume={v}, source={src}, "
                        f"dest={dst}, new_tip='never')"]
            else:  # ponytail: OT-2 has no liquid classes, so default flow rates; slow them for a viscous premix if needed
                move = [f"    {pip}.transfer({v}, {src}, {dst}, new_tip='never')"]
            L += each([f"    {pip}.pick_up_tip()", *move, f"    {pip}.drop_tip()"])
        elif a == "mix":
            v = float(s["volume_ul"])
            pip = sm if v <= bot["small_ul"] else big
            dst = where(s.get("destination") or "sample tube")
            L += each([f"    {pip}.pick_up_tip()", f"    {pip}.mix(3, {v}, {dst})", f"    {pip}.drop_tip()"])
        elif a == "thermocycle":
            j = [t[0] for t in tcs].index(i)
            pre = f"pcr{j + 1}_" if len(tcs) > 1 else ""
            prof = tcs[j][1]

            def stage(name, reps):
                return (f"    tc.execute_profile(steps=[" + ", ".join(
                    f"{{'temperature': ctx.params.{pre}{name}{k + 1}_c, 'hold_time_seconds': ctx.params.{pre}{name}{k + 1}_s}}"
                    for k in range(len(prof[name]))) + f"], repetitions={reps}, block_max_volume={block_ul})")
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


def analyze(source, json_path, filename="protocol.py", rtp=None, python=None):
    """Opentrons' own protocol analysis (what the Opentrons App runs on import). Writes the full JSON to json_path
    and returns (ok, summary) where summary lists what to load into which tube before the run. rtp: runtime parameter
    values to analyse with instead of the defaults, as the App does after run setup. python: the interpreter whose
    opentrons package analyses it (OT-2 files need opentrons 9.x, see OT2)."""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        proto = Path(tmp) / filename  # .py, or .json for a Protocol Designer file
        proto.write_text(source, encoding="utf-8")
        extra = ["--rtp-values", json.dumps(rtp)] if rtp else []
        p = subprocess.run([python or sys.executable, "-m", "opentrons.cli", "analyze", "--json-output", str(json_path), *extra,
                            str(proto)],
                           capture_output=True, text=True, timeout=240, encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
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
        "commands": len(a.get("commands", [])), "liquid_setup": setup, "cycling_s": block_time(a.get("commands", [])),
        # runtime parameters as the Opentrons App will offer them at run setup
        "parameters": [{"name": q.get("displayName"), "unit": q.get("suffix", ""),
                        "value": next((c["displayName"] for c in q.get("choices") or [] if c.get("value") == q.get("default")),
                                      ("on" if q["default"] else "off") if q.get("type") == "bool" else q.get("default"))}
                       for q in a.get("runTimeParameters", [])]}


def plate_summary(a):
    """From an analysis: PCR plate wells the pipettes dispense into, tips picked up, tip racks loaded."""
    plate = {lw["id"] for lw in a.get("labware", []) if lw["loadName"] == PCR_PLATE}
    cmds = a.get("commands", [])
    wells = {c["params"]["wellName"] for c in cmds if c["commandType"] in ("dispense", "dispenseInPlace", "moveToWell")
             and c.get("params", {}).get("labwareId") in plate}
    return {"plate_wells": sorted(wells, key=lambda w: (int(w[1:]), w[0])),
            "tips": sum(c["commandType"] == "pickUpTip" for c in cmds),
            "tip_racks": sum(lw["loadName"].startswith("opentrons_flex_96_tiprack") for lw in a.get("labware", [])),
            # where the analysis put each tip rack, so the deck drawing matches the robot
            "tip_rack_slots": [{"slot": lw.get("location", {}).get("slotName"), "ul": 50 if "50ul" in lw["loadName"] else 1000}
                               for lw in a.get("labware", []) if lw["loadName"].startswith("opentrons_flex_96_tiprack")]}


def block_time(commands):
    """Seconds the thermocycler block needs for the analysed commands: every hold plus every ramp at the spec's max
    rates, so a lower bound (lid preheat not included). None when the protocol does not cycle."""
    t, temp, seen = 0.0, AMBIENT_C, False

    def go(c, hold):
        nonlocal t, temp
        t += (c - temp) / TC_HEAT if c > temp else (temp - c) / TC_COOL
        t += hold
        temp = c

    for c in commands:
        kind, p = c.get("commandType", ""), c.get("params", {})
        if kind == "thermocycler/runExtendedProfile":
            seen = True
            for el in p["profileElements"]:
                for _ in range(int(el.get("repetitions", 1)) if "repetitions" in el else 1):
                    for st in el.get("steps", [el]):
                        go(st["celsius"], st["holdSeconds"])
        elif kind == "thermocycler/runProfile":
            seen = True
            for st in p["profile"]:
                go(st["celsius"], st["holdSeconds"])
        elif kind == "thermocycler/setTargetBlockTemperature":
            go(p["celsius"], p.get("holdTimeSeconds") or 0)
    return round(t) if seen else None


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
    assert _spec("flex_1channel_50", 1) == (7.0, 8.0) and _spec("flex_1channel_50", 50) == (0.4, 1.25)
    assert abs(_spec("flex_1channel_50", 10 ** 0.5)[1] - 4.75) < 1e-9  # halfway between 1 and 10 uL in log volume
    pe = {r["reagent"]: r for r in pipetting_error(demo)}
    assert pe["Primer1"]["share_d"] > 7 > pe["PrimeSTAR Max Premix"]["share_d"], pe  # 1 uL is the weak transfer
    assert pe["water"]["pipettes"] == ["p50"] and abs(sum(r["share"] for r in pe.values()) - 100) < 0.2, pe
    src, layout = generate(demo)
    ok, log = simulate(src)
    assert ok, log
    assert any("Transferring 1.0 uL of Aqueous" in l for l in log), log
    assert any("Transferring 25.0 uL of Viscous liquid class" in l for l in log), log  # the premix is pipetted as viscous
    assert liquid_class("70% EtOH")[0] == "ethanol_80" and liquid_class("Primer1")[0] == "water"
    assert "display_name='PrimeSTAR Max Premix'" in src and "default='glycerol_50'" in src, src
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
    # 30 reagents: a second tube rack in a spare slot, every tube in the simulator log, tips still fit
    many = [{"start": i, "end": i + 1, "action": "transfer", "description": f"add r{i}", "reagent": f"r{i}",
             "source": f"r{i}", "volume_ul": 5, "destination": "PCR tube"} for i in range(30)] + [demo[5]]
    src3, lay3 = generate(many)
    ok, log3 = simulate(src3)
    assert ok, log3
    assert lay3["r29"] == "reagents2:B2" and "reagents2 = ctx.load_labware(" in src3 and "'B2'" in src3, lay3
    assert any("reagent tubes 2" in l for l in log3), log3[:5]
    assert "Step 2 · video 64-99 s: Add Primer1" in log, [l for l in log if l.startswith("Step")][:3]
    p = thermal({"profile": {"cycle": [{"c": 94, "s": 30}, {"c": 72, "s": None}]}})
    assert p["cycle"][1] == {"c": 72.0, "s": 30} and "no time written for 72 °C" in p["source"], p
    # liquid presence detection: a real liquidProbe before every draw when the lab switches it on
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        ok, _ = analyze(src.replace("default=False)", "default=True)", 1), Path(tmp) / "a.json")
        probes = sum(c["commandType"] == "liquidProbe" for c in json.loads((Path(tmp) / "a.json").read_text())["commands"])
    assert ok and probes == 3, probes  # 3 transfers in the demo
    # cycling time from the analysed commands: 25->98 then hold 30 s; 35 cycles of 98/55/72 (34 ramps back up 72->98);
    # 72->4 at the end
    ok, an = analyze(src, Path(tempfile.mkdtemp()) / "a.json")
    expect = 73 / 4.25 + 30 + 35 * (10 + 43 / 2 + 5 + 17 / 4.25 + 5) + 34 * 26 / 4.25 + 68 / 2
    assert ok and abs(an["cycling_s"] - expect) < 1, (an["cycling_s"], expect)
    # film one reaction, run a plate: analysed at the most reactions the deck allows, every copy in its own well
    top = max_reactions(demo)
    assert top == 53 and "variable_name='reactions'" in src and f"maximum={top}" in src, top  # premix 25 uL: tube-bound
    with tempfile.TemporaryDirectory() as tmp:
        ok, an = analyze(src, Path(tmp) / "a.json", rtp={"reactions": top})
        a = json.loads((Path(tmp) / "a.json").read_text())
    assert ok, an
    water = next(q for q in an["liquid_setup"] if q["liquid"] == "water")
    assert water["volume_ul"] == load_ul(20 * top), water  # filled for every reaction
    ps = plate_summary(a)
    racks = sum(lw["loadName"] == "opentrons_flex_96_tiprack_50ul" for lw in a["labware"])
    assert ps["tips"] == 4 * top and len(ps["plate_wells"]) == top and racks == -(-4 * top // 96), (ps, racks)
    assert ps["plate_wells"][:9] == ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1", "A2"], ps  # down the columns
    assert "reactions" not in generate([{**demo[0], "destination": "tube A"}])[0]  # no plate: no parameter
    # the same steps for an OT-2: its pipettes and slots, none of the Flex-only calls; Flex output unchanged by the switch
    ot2, _ = generate(demo, robot="OT-2")
    assert "'robotType': 'OT-2'" in ot2 and "p20.transfer(1.0," in ot2 and "p300.transfer(25.0," in ot2, ot2
    assert not any(x in ot2 for x in ("liquid_class", "load_trash_bin", "detect_liquid", "flex_")), ot2
    assert generate(demo)[0] == src
    import os
    if os.getenv("OT2_PYTHON"):  # opentrons 9.0.0 (10.x refuses OT-2 files)
        with tempfile.TemporaryDirectory() as tmp:
            top2 = max_reactions(demo, OT2)
            ok, an = analyze(ot2, Path(tmp) / "a.json", rtp={"reactions": top2}, python=os.environ["OT2_PYTHON"])
        assert ok, an
        print(f"OT-2: {top2} reactions analysed by opentrons 9.0.0")
    print(json.dumps(layout), f"\n{len(log)} simulator lines OK; {top} reactions analysed")
