"""The same confirmed steps, exported for people and for other robots.

- methods_markdown: a methods section a scientist can paste into a notebook or paper
- autoprotocol: vendor-neutral Autoprotocol JSON (pipette + thermocycle instructions)
- pylabrobot_script / run_pylabrobot: a Hamilton STARlet script for PyLabRobot, executed in PyLabRobot's simulator
  with volume tracking on, so over-drawing a tube or a tip fails there too

Everything reads model text only as data: names land in string literals or markdown text, never in code.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .protocol import DEAD_UL, LIQUID_CLASSES, _plan, liquid_class, pipetting_error, thermal, well_volume

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


def flow_rates(cls, volume):
    """(aspirate, dispense) uL/s that Opentrons' own liquid class definition gives this volume on the Flex pipette the
    protocol uses, linear between its calibration points as on the robot. Lets the STARlet move the liquid alike."""
    from opentrons_shared_data.liquid_classes import load_definition

    pip, tips = ("flex_1channel_50", "tiprack_50ul") if volume <= 50 else ("flex_1channel_1000", "tiprack_1000ul")
    by_pip = next(p for p in load_definition(cls).byPipette if p.pipetteModel == pip)
    tip = next(t for t in by_pip.byTipType if t.tiprack.endswith(f"opentrons_flex_96_{tips}/1"))

    def interp(points):
        pts = sorted(points)
        if volume <= pts[0][0]:
            return pts[0][1]
        for (v0, r0), (v1, r1) in zip(pts, pts[1:]):
            if volume <= v1:
                return round(r0 + (r1 - r0) * (volume - v0) / (v1 - v0), 1)
        return pts[-1][1]

    return interp(tip.aspirate.flowRateByVolume), interp(tip.singleDispense.flowRateByVolume)


def _origin(source):
    """Where the steps came from, for people: the video, a published protocol, or a photographed/scanned document."""
    if not source:
        return ["Recovered from a first-person video by Show, Don't Code and confirmed by the scientist.",
                "Times in brackets point to the moment in the source video."]
    doi = f" (doi:{_clean(source['doi'])})" if source.get("doi") else ""
    what = "a photo or PDF of the protocol" if source.get("kind") == "document" else "the published protocol"
    return [f"Read from {what} “{_clean(source.get('title'), 120)}”{doi} by Show, Don't Code and confirmed by the "
            "scientist. Steps are numbered in the order the text gives them."]


def methods_markdown(steps, title="Protocol", source=None):
    need = _needs(steps)
    lines = [f"# {_clean(title)}", "", *_origin(source), "", "## Materials", "",
             "| Reagent | Prepare at least | Used for | Pipetted as |", "|---|---|---|---|"]
    for name, total, n in need.values():
        cls, _ = liquid_class(name)
        lines.append(f"| {_clean(name)} | {total * 1.1 + DEAD_UL:.1f} µL | {n} transfer{'s' * (n != 1)}, {total:g} µL in total "
                     f"| {LIQUID_CLASSES[cls].lower()} |")
    err = pipetting_error(steps)
    if err:
        lines += ["", "## Expected pipetting error (Opentrons Flex)", "",
                  "Each reagent's share of its reaction, with the error the Flex pipettes' published accuracy allows "
                  "(docs.opentrons.com/flex/system-description/pipettes/): worst-case systematic, then random (CV).", "",
                  "| Into | Reagent | µL | Share | Systematic | Random |", "|---|---|---|---|---|---|"]
        lines += [f"| {_clean(r['destination'])} | {_clean(r['reagent'])} | {r['ul']:g} | {r['share']:g} % | ±{r['share_d']:g} % "
                  f"| {r['share_cv']:g} % |" for r in err]
    lines += ["", "## Procedure", ""]
    for i, s in enumerate(steps, 1):
        when = "" if source else f" [{float(s['start']):.0f}–{float(s['end']):.0f} s]"  # a text has no video seconds
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
        lines.append(f"{i}. {text}{when}")
    return "\n".join(lines) + "\n"


def autoprotocol(steps):
    """Autoprotocol JSON: refs for the reagent tubes and the reaction plate, one pipette instruction per transfer or mix.
    Autoprotocol has no instruction for a person's hand-off (spin, vortex), so those are in methods.md only."""
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
        elif s["action"] == "mix":  # pipette up and down 3 times, as protocol.py does
            instructions.append({"op": "pipette", "groups": [{"mix": [
                {"well": dst_idx[s.get("destination") or "sample tube"], "volume": f"{float(s['volume_ul']):g}:microliter",
                 "repetitions": 3}]}]})
        elif s["action"] == "thermocycle":
            instructions.append({"op": "seal", "object": "reactions", "type": "ultra-clear"})
            p = thermal(s)  # the same program protocol.py runs, so every export agrees
            steps_of = lambda xs: [{"temperature": f"{x['c']:g}:celsius", "duration": f"{x['s']}:second"} for x in xs]  # noqa: E731
            groups = ([{"cycles": 1, "steps": steps_of(p["initial"])}] if p["initial"] else []) +                 [{"cycles": int(s["cycles"]), "steps": steps_of(p["cycle"])}] +                 ([{"cycles": 1, "steps": steps_of(p["final"])}] if p["final"] else [])
            instructions.append({"op": "thermocycle", "object": "reactions", "volume": f"{well_volume(steps)}:microliter", "groups": groups})
    return {"refs": refs, "instructions": instructions}


ROBOTS = {"starlet": "Hamilton STARlet", "evo150": "Tecan Freedom EVO 150"}


def pylabrobot_script(steps, robot="starlet"):
    """A translation for PyLabRobot: a Hamilton STARlet, or a Tecan Freedom EVO 150 with its LiHa arm. Reagent tubes sit
    in a deep-well plate and reactions in a 96-well plate on one plate carrier, because tube carriers differ from lab to
    lab. Same steps, same Opentrons liquid-class flow rates, a different vendor's deck, tips and carriers."""
    sources, dests, _ = _plan(steps)
    need = _needs(steps)
    liquid = [s for s in steps if s["action"] in ("transfer", "mix")]
    top = max((float(s["volume_ul"]) for s in liquid), default=0)
    racks = max(1, -(-len(liquid) // 96))
    if robot == "evo150":  # DiTi carrier with a waste slot: 3 racks + the tip waste, as on the robot
        tip = "DiTi_1000ul_LiHa" if top > 200 else "DiTi_200ul_LiHa" if top > 50 else "DiTi_50ul_LiHa"
        if racks > 3:
            raise ValueError(f"{len(liquid)} transfers need {racks} tip racks; one EVO DiTi carrier holds 3")
        head = ["from pylabrobot.resources import set_volume_tracking, Trash",
                f"from pylabrobot.resources.tecan import EVO150Deck, DiTi_3Pos___Waste, MP_3Pos, DeepWell_96_Well, PCR_Plate_96_Well, {tip}"]
        deck, setup = "EVO150Deck()", (
            ["    tips = DiTi_3Pos___Waste(name='tips')"] + [f"    tips[{i}] = {tip}(name='tips_{i + 1}')" for i in range(racks)] +
            ["    waste = tips.sites[3]  # the carrier's DiTi waste position",
             "    tips[3] = Trash(name='trash', size_x=waste.get_size_x(), size_y=waste.get_size_y(), size_z=0)",
             "    plates = MP_3Pos(name='plates')",
             "    plates[0] = DeepWell_96_Well(name='reagents')", "    plates[1] = PCR_Plate_96_Well(name='reactions')",
             "    lh.deck.assign_child_resource(tips, rails=10)", "    lh.deck.assign_child_resource(plates, rails=16)"])
    else:
        tip = "hamilton_96_tiprack_1000uL_filter" if top > 50 else "hamilton_96_tiprack_50uL_filter"
        if racks > 5:
            raise ValueError(f"{len(liquid)} transfers need {racks} tip racks; one STARlet tip carrier holds 5")
        head = ["from pylabrobot.resources import (STARLetDeck, TIP_CAR_480_A00, PLT_CAR_L5AC_A00, cor_96_wellplate_2mL_Vb,",
                f"                                  cor_96_wellplate_360uL_Fb, set_volume_tracking, {tip})"]
        deck, setup = "STARLetDeck()", (
            ["    tips = TIP_CAR_480_A00(name='tips')"] + [f"    tips[{i}] = {tip}(name='tips_{i + 1}')" for i in range(racks)] +
            ["    plates = PLT_CAR_L5AC_A00(name='plates')",
             "    plates[0] = cor_96_wellplate_2mL_Vb(name='reagents')", "    plates[1] = cor_96_wellplate_360uL_Fb(name='reactions')",
             "    lh.deck.assign_child_resource(tips, rails=3)", "    lh.deck.assign_child_resource(plates, rails=9)"])
    src_well = {k: WELLS_96[i] for i, k in enumerate(sources)}
    dst_well = {k: WELLS_96[i] for i, k in enumerate(dests)}
    L = [f"\"\"\"Generated by Show, Don't Code: {ROBOTS[robot]} via PyLabRobot. Simulated with the chatterbox backend.\"\"\"",
         "import asyncio", "from pylabrobot.liquid_handling import LiquidHandler",
         "from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend", *head, "",
         "async def main(backend=None):",
         "    set_volume_tracking(True)  # over-drawing a tube or a tip is an error, not a silent pass",
         f"    lh = LiquidHandler(backend=backend or LiquidHandlerChatterboxBackend(num_channels=8), deck={deck})",
         "    await lh.setup()", *setup,
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
            cls, _ = liquid_class(s.get("reagent") or s.get("source"))
            asp, disp = flow_rates(cls, v)  # PyLabRobot's backends would otherwise pipette everything as water
            L += [f"    await lh.pick_up_tips([tip_spots[{n}]])",
                  f"    await lh.aspirate(reagents[{src!r}], vols=[{v}], flow_rates=[{asp}])  # {cls}: Opentrons liquid class rates",
                  f"    await lh.dispense(reactions[{dst!r}], vols=[{v}], flow_rates=[{disp}])", "    await lh.discard_tips()"]
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


# Avery L7160 / J8160 (A4, 3 x 7 labels of 63.5 x 38.1 mm): a sheet most lab printers already take
SHEET_W, SHEET_H, LABEL_W, LABEL_H, LEFT, TOP, PITCH_X, COLS, ROWS = 210, 297, 63.5, 38.1, 7.2, 15.15, 66.04, 3, 7


def label_tubes(steps, reactions=1):
    """The tubes on the tube racks, in label order (sources first): name, deck slot, well, and what goes in it, for
    `reactions` copies of the filmed reaction (protocol.py's Reactions parameter)."""
    from .protocol import extra_racks, load_ul

    sources, dests, _ = _plan(steps)
    slot_of = {"reagents": "C2", **dict(extra_racks(sources, dests))}
    need = _needs(steps)
    out = []
    for name, (rack, well) in {**sources, **dests}.items():
        if not rack.startswith("reagents"):
            continue
        drawn = need.get(name, (None, 0.0, 0))[1]
        out.append({"tube": name, "slot": slot_of[rack], "well": well, "source": name in sources, "reactions": reactions,
                    "fill_ul": load_ul(drawn * reactions) if name in sources else 0, "drawn_ul": drawn})
    return out


def _fill_lines(t):
    per = f"{t['reactions']} × {t['drawn_ul']:g}" if t.get("reactions", 1) > 1 else f"{t['drawn_ul']:g}"
    return (f"fill with {t['fill_ul']:g} µL", f"({per} µL drawn + 10 % + dead volume)") if t["source"] \
        else ("starts empty", "the robot fills it")


def tube_labels(steps, link, reactions=1):
    """Printable SVG labels, one per tube on the tube racks: a QR code that opens `link` + &tube=<name> (the app then
    shows where that tube goes on the deck), the tube's deck slot and well, and how much to fill a source tube with.
    Returns (svg, number of labels). Model text is escaped; it never becomes markup."""
    from urllib.parse import quote
    from xml.sax.saxutils import escape

    import segno

    tubes = label_tubes(steps, reactions)
    pages = max(1, -(-len(tubes) // (COLS * ROWS)))  # ponytail: >21 tubes stack sheets in one tall SVG
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{SHEET_W}mm" height="{SHEET_H * pages}mm" '
           f'viewBox="0 0 {SHEET_W} {SHEET_H * pages}" font-family="Helvetica, Arial, sans-serif">']
    for i, t in enumerate(tubes):
        page, k = divmod(i, COLS * ROWS)
        x, y = LEFT + (k % COLS) * PITCH_X, page * SHEET_H + TOP + (k // COLS) * LABEL_H
        qr = segno.make(f"{link}&tube={quote(t['tube'])}", error="m")
        size = 26.0  # ~0.7 mm modules for a ~90 character link: a phone camera reads them from a hand's length
        mod = size / qr.symbol_size(border=0)[0]
        qy = y + (LABEL_H - size) / 2
        path = "".join(f"M{x + 3 + c * mod:.3f} {qy + r * mod:.3f}h{mod:.3f}v{mod:.3f}h{-mod:.3f}z"
                       for r, row in enumerate(qr.matrix) for c, dark in enumerate(row) if dark)
        fill = _fill_lines(t)
        tx, room = x + size + 5, LABEL_W - size - 7
        lines = [(_clean(t["tube"], 40), 3.6, "bold"), (f"slot {t['slot']} · well {t['well']}", 3.2, "normal"),
                 (fill[0], 3.0, "normal"), (fill[1], 2.0, "normal")]
        out.append(f'<rect x="{x}" y="{y}" width="{LABEL_W}" height="{LABEL_H}" rx="2" fill="none" stroke="#ccc" stroke-width=".2"/>'
                   f'<path d="{path}" fill="#000"/>')
        for j, (txt, fs, fw) in enumerate(lines):  # squeeze a long name into the label instead of running off it
            fit = f' textLength="{room}" lengthAdjust="spacingAndGlyphs"' if len(txt) * fs * .56 > room else ""
            out.append(f'<text x="{tx:.2f}" y="{y + 10 + j * 6:.2f}" font-size="{fs}" font-weight="{fw}"{fit}>{escape(txt)}</text>')
    out.append("</svg>")
    return "".join(out), len(tubes)


def _zpl_field(text):
    """^FH hex escapes for ^ and ~ (command prefixes: model text can't inject ZPL commands), _ (the escape itself),
    control and non-ASCII characters (µ survives as UTF-8, ^CI28)."""
    return "".join(c if " " <= c <= "}" and c not in "^_\\" else "".join(f"_{b:02X}" for b in c.encode("utf-8"))
                   for c in text)


def tube_labels_zpl(steps, link, reactions=1):
    """The same labels for a Zebra thermal printer (ZPL II, 2 x 1 inch stock at 203 dpi, the common lab label size;
    one ^XA..^XZ format per tube). Returns (zpl, number of labels)."""
    from urllib.parse import quote

    tubes = label_tubes(steps, reactions)
    out = []
    for t in tubes:
        fill = _fill_lines(t)
        name = _clean(t["tube"], 40)
        # font 0 is ~0.55 h wide: one line at 28 dots if the name fits in 204, else two lines at 20 (then cut)
        h, n = (28, 1) if len(name) * 28 * .55 <= 204 else (20, 2)
        name = name[:int(204 / (h * .55)) * n]
        rows = [(name, h, n, 12), (f"slot {t['slot']} - well {t['well']}", 22, 1, 62), (fill[0], 22, 1, 94),
                (fill[1], 14, 2, 124)]
        out.append("^XA^CI28^PW406^LL203^LH0,0"
                   # QR model 2, magnification 4 (0.5 mm modules), error level M; ^BQ adds its own top offset
                   f"^FO8,0^BQN,2,4^FH^FDMA,{_zpl_field(link + '&tube=' + quote(t['tube']))}^FS"
                   + "".join(f"^FO196,{y}^A0N,{fh},{fh}^FB204,{n},0,L^FH^FD{_zpl_field(txt)}^FS" for txt, fh, n, y in rows)
                   + "^XZ")
    return "\n".join(out) + "\n", len(tubes)


def notebook(steps, source, title, link, origin=None):
    """A Jupyter notebook (nbformat 4, opens in JupyterLab, VS Code or Colab): the methods, protocol.py written to disk
    by a cell, and the Opentrons simulator run on it, so a lab re-checks the file without installing our app."""
    ids = iter(range(10))

    def cell(kind, text, **extra):  # nbformat 4.5: every cell has an id
        return {"id": f"sdc-{next(ids)}", "cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True), **extra}

    def code(text):
        return cell("code", text, execution_count=None, outputs=[])

    return {"nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                         "language_info": {"name": "python"}},
            "cells": [cell("markdown", methods_markdown(steps, title, origin) +
                           f"\n\n{'Source' if origin else 'Source video'}, step by step: {link}\n"),
                      code("%pip install -q opentrons==10.1.0"),
                      code("%%writefile protocol.py\n" + source),
                      code("from opentrons.simulate import simulate\n\n"
                           "with open('protocol.py') as f:\n"
                           "    log, _ = simulate(f, file_name='protocol.py')\n"
                           "print(len(log), 'simulator steps')\n"
                           "for entry in log:\n"
                           "    print(entry['payload']['text'])")]}


def run_pylabrobot(source, timeout=120):
    """Execute the generated script in a separate process with PyLabRobot's simulator. Returns (ok, last log lines)."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "plr_protocol.py"
        script.write_text(source, encoding="utf-8")
        p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",  # the child writes UTF-8 (µL); Windows' default reads cp1252
                           env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    out = (p.stdout + p.stderr).strip().splitlines()
    actions = sum(1 for line in out if line.startswith(("Aspirating", "Dispensing")))
    return p.returncode == 0, {"actions": actions, "tail": out[-6:]}


# Tecan liquid class names differ per installation: these are only defaults, renamed per request (?aqueous= etc.).
# EVOware/FluentControl stop on a class name they don't have instead of pipetting with a guessed one.
CLASS_NAME = re.compile(r"[A-Za-z0-9 _.()+-]{1,32}")
TECAN_CLASSES = {"water": "Water Free Single", "glycerol_50": "Glycerol Free Single", "ethanol_80": "Ethanol Free Single"}


def tecan_worklist(steps, robot="evo", classes=None):
    """A Tecan worklist (.gwl) for EVOware's or FluentControl's Worklist command, written by robotools, which tracks
    every well's volume: drawing more than a tube holds raises instead of writing the line. Labware labels on the
    worksheet must be 'reagents' (tubes, filled as the labels say) and 'reactions' (the 96-well PCR plate)."""
    import robotools

    cls_names = {**TECAN_CLASSES, **{k: v for k, v in (classes or {}).items() if v}}
    for k, v in cls_names.items():  # a ';' or line break would start a new worklist record: refuse, don't guess
        if not CLASS_NAME.fullmatch(v):
            raise ValueError(f"liquid class name {v[:40]!r}: letters, digits, spaces and _ . ( ) + - only, up to 32")
    sources, dests, _ = _plan(steps)
    need = _needs(steps)
    src_well = {k: f"{w[0]}{int(w[1:]):02d}" for k, w in zip(sources, WELLS_96)}  # robotools names wells A01
    dst_well = {k: f"{w[0]}{int(w[1:]):02d}" for k, w in zip(dests, WELLS_96)}
    reagents = robotools.Labware("reagents", 8, 12, min_volume=DEAD_UL, max_volume=2000)
    reactions = robotools.Labware("reactions", 8, 12, min_volume=0, max_volume=200)
    for key, (_, total, _) in need.items():  # what the labels and protocol.py fill: drawn + 10 % + dead volume
        reagents.add(src_well[key], round(total * 1.1 + DEAD_UL, 1))
    wl = (robotools.FluentWorklist if robot == "fluent" else robotools.EvoWorklist)(auto_split=True)
    wl.comment("Show, Don't Code: labware labels 'reagents' and 'reactions', liquid classes: " +
               ", ".join(sorted(set(cls_names.values()))))
    def note(text):  # a semicolon would end the GWL record early, so model text never carries one
        wl.comment(_clean(text, 120).replace(";", ","))

    for i, s in enumerate(steps):
        note(f"step {i + 1} [{float(s['start']):.0f}-{float(s['end']):.0f} s]: {s['description']}")
        if s["action"] == "transfer":
            cls, _ = liquid_class(s.get("reagent") or s.get("source"))
            wl.transfer(reagents, src_well[s.get("source") or s.get("reagent") or "reagent"], reactions,
                        dst_well[s.get("destination") or "sample tube"], float(s["volume_ul"]),
                        liquid_class=cls_names[cls], label=_clean(s.get("reagent"), 40))
        elif s["action"] == "mix":
            w, v = dst_well[s.get("destination") or "sample tube"], float(s["volume_ul"])
            for _ in range(3):
                wl.aspirate(reactions, w, v, liquid_class=cls_names["water"])
                wl.dispense(reactions, w, v, liquid_class=cls_names["water"])
            wl.wash()
        else:
            note("by hand: " + s["description"])
    return "\n".join(wl) + "\n"


def transfer_csv(steps):
    """One row per transfer: a generic pick list for Biomek, Hamilton or a spreadsheet; same wells as the worklist."""
    import csv
    import io

    sources, dests, _ = _plan(steps)
    src_well = {k: WELLS_96[i] for i, k in enumerate(sources)}
    dst_well = {k: WELLS_96[i] for i, k in enumerate(dests)}
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["step", "video_start_s", "video_end_s", "reagent", "source_labware", "source_well", "destination_labware",
                "destination_well", "volume_ul", "liquid_class"])
    for i, s in enumerate(steps):
        if s["action"] != "transfer":
            continue
        name = _clean(s.get("reagent"))
        w.writerow([i + 1, f"{float(s['start']):g}", f"{float(s['end']):g}", "'" + name if name[:1] in "=+-@" else name,
                    "reagents", src_well[s.get("source") or s.get("reagent") or "reagent"], "reactions",
                    dst_well[s.get("destination") or "sample tube"], f"{float(s['volume_ul']):g}",
                    liquid_class(s.get("reagent") or s.get("source"))[0]])
    return out.getvalue()


if __name__ == "__main__":
    demo = [
        {"start": 30, "end": 45, "action": "transfer", "description": "Add water", "reagent": "water", "volume_ul": 20, "destination": "tube 1"},
        {"start": 50, "end": 60, "action": "transfer", "description": "Add primer; then; mix", "reagent": "Primer \"1\"\nimport os", "volume_ul": 1.5, "destination": "tube 1"},
        {"start": 61, "end": 70, "action": "centrifuge", "description": "Spin down"},
        {"start": 80, "end": 90, "action": "thermocycle", "description": "Set in DNA engine", "cycles": 35},
    ]
    md = methods_markdown(demo, "Demo")
    doc = methods_markdown(demo, "Demo", {"kind": "document", "title": "Colony PCR"})
    assert "photo or PDF" in doc and "first-person video" not in doc and "[30–45 s]" not in doc and "[30–45 s]" in md, doc
    assert "## Expected pipetting error" in md and "\nimport os" not in md, md
    assert "35 cycles" in md and "| water |" in md and "aqueous (water) |" in md, md
    ap = autoprotocol(demo)
    assert [i["op"] for i in ap["instructions"]] == ["pipette", "pipette", "seal", "thermocycle"], ap
    mx = autoprotocol(demo[:1] + [{**demo[0], "action": "mix", "volume_ul": 15}])["instructions"][1]
    assert mx["groups"][0]["mix"][0] == {"well": "reactions/0", "volume": "15:microliter", "repetitions": 3}, mx
    src = pylabrobot_script(demo)
    assert "\nimport os" not in src
    assert flow_rates("glycerol_50", 25) != flow_rates("water", 25), "viscous must move slower than water"
    assert flow_rates("glycerol_50", 25)[1] < flow_rates("water", 25)[1]
    ok, log = run_pylabrobot(src)
    assert ok and log["actions"] == 4, log
    over = [{**demo[0], "volume_ul": 20}] * 2
    evo = pylabrobot_script(demo, "evo150")
    assert "EVO150Deck()" in evo and "DiTi_50ul_LiHa" in evo and "\nimport os" not in evo
    ok, elog = run_pylabrobot(evo)
    assert ok and elog["actions"] == 4 and any("trash" in line for line in elog["tail"]), elog
    assert run_pylabrobot(pylabrobot_script(over, "evo150").replace("set_volume(74.0)", "set_volume(25.0)"))[0] is False
    src2 = pylabrobot_script(over).replace("set_volume(74.0)", "set_volume(25.0)")
    assert run_pylabrobot(src2)[0] is False, "volume tracking must catch drawing 40 µL from a 25 µL tube"
    svg, n = tube_labels(demo, "https://example.org/app?run=demo")
    # the thermocycler takes tube 1 into its plate, so only the two source tubes get labels
    assert n == 2 and svg.count("<path") == 2 and "fill with 52 µL" in svg and "slot C2 · well B1" in svg, n
    svg, n = tube_labels(demo[:2] + [{**demo[0], "reagent": "<script>x</script>", "destination": "tube 2"}], "https://e.org/app?run=d")
    assert n == 5 and svg.count("starts empty") == 2 and "<script>" not in svg and "&lt;script&gt;" in svg, n
    zpl, n = tube_labels_zpl(demo[:2] + [{**demo[0], "reagent": "^XZ^XA~JR", "destination": "tube 2"}], "https://e.org/app?run=d")
    # one format per tube, and a reagent named like ZPL commands stays text (hex-escaped), never a command
    assert n == 5 and zpl.count("^XA") == 5 and zpl.count("^XZ") == 5 and "~" not in zpl, zpl
    assert "fill with 52 _C2_B5L" in zpl and "_5EXZ_5EXA_7EJR" in zpl, zpl
    svg, _ = tube_labels(demo, "https://example.org/app?run=demo", reactions=8)
    assert "fill with 206 µL" in svg and "(8 × 20 µL drawn" in svg, svg  # 8 x 20 x 1.1 + 30
    from sdc.protocol import generate, simulate
    flex, _ = generate(demo)
    nb = notebook(demo, flex, "Demo", "https://example.org/app?run=demo")
    write = "".join(nb["cells"][2]["source"])
    assert write == "%%writefile protocol.py\n" + flex, write[:80]  # the file the simulator cell runs, byte for byte
    assert simulate(write.split("\n", 1)[1])[0] and "35 cycles" in "".join(nb["cells"][0]["source"])
    gwl = tecan_worklist(demo)
    lines = gwl.splitlines()
    assert sum(x.startswith("A;") for x in lines) == 2 and sum(x.startswith("D;") for x in lines) == 2, gwl
    assert "A;reagents;;;1;;20.00;Water Free Single" in gwl and "\nimport os" not in gwl, gwl
    assert all(x.split(";")[0] in ("A", "D", "W", "W1", "C", "B", "F") for x in lines), gwl  # EVOware record types
    assert "C;" in tecan_worklist(demo, "fluent")
    for bad in ("x;D;reactions;;;1;;99", "Water\r\nA;reagents", "a" * 33):
        try:
            tecan_worklist(demo, classes={"water": bad})
            raise AssertionError(f"{bad!r} must be refused")
        except ValueError as e:
            assert "liquid class name" in str(e), e
    try:  # 300 µL into one 200 µL PCR well must not become a worklist
        tecan_worklist([{**demo[0], "volume_ul": 150}] * 2)
        raise AssertionError("robotools must refuse overfilling a well")
    except Exception as e:
        assert type(e).__name__ == "VolumeOverflowError", e
    csv_ = transfer_csv(demo + [{**demo[0], "reagent": "=HYPERLINK(1)", "destination": "tube 2"}])
    assert csv_.count("\n") == 4 and ",'=HYPERLINK(1)," in csv_, csv_
    tail = " ".join(log["tail"])  # the simulator's UTF-8 survives the pipe (Windows read it as cp1252: "ÂµL")
    assert "µL" in tail and "Â" not in tail, tail
    print("export self-check OK:", json.dumps(log))
