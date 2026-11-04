"""Opentrons analysis -> a Protocol Designer file (JSON protocol, schema 8): the protocol opens as editable steps in
Opentrons' own no-code editor (designer.opentrons.com), and the Opentrons App runs the same file.

Built from analysis.json, not from our steps: those are the commands Opentrons itself produced for protocol.py, so the
file's `commands` are exactly what was analysed, and every Protocol Designer form below is read off them (one
"transfer" per tip, the thermocycler program, the final hold). A scientist who never opens the Python can change a
volume or a temperature there and export again.

  python -m sdc.designer            # self-check on a sample run's analysis
"""
import json
import uuid
from datetime import datetime

from opentrons_shared_data.labware import load_definition

PD_VERSION = "8.6.0"  # the designerApplication version the forms below follow (fixtures/protocol/8/doItAllV8.json)
TC_TYPE = "thermocyclerModuleType"
TRASH_CUTOUT = {"movableTrashA3": "cutoutA3", "movableTrashD3": "cutoutD3", "movableTrashA1": "cutoutA1"}
LIQUID_FORM = {  # the fixture's moveLiquid fields; we set the ones a scientist would read, the rest are PD defaults
    "aspirate_airGap_checkbox": False, "aspirate_airGap_volume": "5", "aspirate_delay_checkbox": False,
    "aspirate_delay_seconds": "1", "aspirate_flowRate": None, "aspirate_mix_checkbox": False, "aspirate_mix_times": None,
    "aspirate_mix_volume": None, "aspirate_mmFromBottom": None, "aspirate_position_reference": "well-bottom",
    "aspirate_retract_delay_seconds": None, "aspirate_retract_mmFromBottom": 2, "aspirate_retract_speed": 100,
    "aspirate_retract_x_position": None, "aspirate_retract_y_position": None, "aspirate_retract_position_reference": "well-top",
    "aspirate_submerge_delay_seconds": None, "aspirate_submerge_speed": 100, "aspirate_submerge_mmFromBottom": 2,
    "aspirate_submerge_x_position": None, "aspirate_submerge_y_position": None, "aspirate_submerge_position_reference": "well-top",
    "aspirate_touchTip_checkbox": False, "aspirate_touchTip_mmFromTop": None, "aspirate_touchTip_speed": 300,
    "aspirate_touchTip_mmFromEdge": 0, "aspirate_wellOrder_first": "t2b", "aspirate_wellOrder_second": "l2r",
    "aspirate_wells_grouped": False, "aspirate_x_position": 0, "aspirate_y_position": 0, "blowout_checkbox": False,
    "blowout_flowRate": None, "blowout_location": None, "changeTip": "always", "conditioning_checkbox": False,
    "conditioning_volume": None, "dispense_airGap_checkbox": False, "dispense_airGap_volume": "5",
    "dispense_delay_checkbox": False, "dispense_delay_seconds": "1", "dispense_flowRate": None, "dispense_mix_checkbox": False,
    "dispense_mix_times": None, "dispense_mix_volume": None, "dispense_mmFromBottom": None,
    "dispense_position_reference": "well-bottom", "dispense_retract_delay_seconds": None, "dispense_retract_mmFromBottom": 2,
    "dispense_retract_speed": 100, "dispense_retract_x_position": None, "dispense_retract_y_position": None,
    "dispense_retract_position_reference": "well-top", "dispense_submerge_delay_seconds": None, "dispense_submerge_speed": 100,
    "dispense_submerge_mmFromBottom": 2, "dispense_submerge_x_position": None, "dispense_submerge_y_position": None,
    "dispense_submerge_position_reference": "well-top", "dispense_touchTip_checkbox": False, "dispense_touchTip_mmFromTop": None,
    "dispense_touchTip_speed": 300, "dispense_touchTip_mmFromEdge": 0, "dispense_wellOrder_first": "t2b",
    "dispense_wellOrder_second": "l2r", "dispense_x_position": 0, "dispense_y_position": 0, "disposalVolume_checkbox": False,
    "disposalVolume_volume": None, "liquidClassesSupported": False, "liquidClass": "none", "nozzles": None, "path": "single",
    "preWetTip": False, "pushOut_checkbox": False, "pushOut_volume": None, "stepType": "moveLiquid", "stepDetails": "",
    "dispense_touchTip_mmfromTop": None}
MIX_FORM = {  # Protocol Designer's mix fields (fixtures/protocol/8/mix_8_0_0.json)
    "changeTip": "always", "mix_wellOrder_first": "t2b", "mix_wellOrder_second": "l2r", "blowout_checkbox": False,
    "blowout_location": None, "aspirate_flowRate": None, "dispense_flowRate": None, "aspirate_delay_checkbox": False,
    "aspirate_delay_seconds": "1", "dispense_delay_checkbox": False, "dispense_delay_seconds": "1",
    "mix_touchTip_checkbox": False, "mix_touchTip_mmFromBottom": None, "nozzles": None, "mix_x_position": 0,
    "mix_y_position": 0, "blowout_z_offset": 0, "blowout_flowRate": None, "stepType": "mix", "stepDetails": ""}
TC_FORM = {"blockIsActive": False, "blockIsActiveHold": False, "blockTargetTemp": None, "blockTargetTempHold": None,
           "lidIsActive": False, "lidIsActiveHold": False, "lidOpen": False, "lidOpenHold": None, "lidTargetTemp": None,
           "lidTargetTempHold": None, "orderedProfileItems": [], "profileItemsById": {}, "profileTargetLidTemp": None,
           "profileVolume": None, "stepType": "thermocycler", "stepName": "thermocycler", "stepDetails": ""}


def _num(x):
    return f"{x:g}"


def _uid(*parts):  # stable ids: the same analysis always gives the same file (and the same hash in the receipt)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "sdc:" + ":".join(map(str, parts))))


def protocol_designer(analysis, title):
    """The Protocol Designer file for one analysed protocol, as a dict ready for json.dumps."""
    cmds = analysis["commands"]
    # Protocol Designer only opens an imported file that has a creation time (ms); the analysis time keeps it reproducible
    created = round(datetime.fromisoformat(analysis["createdAt"]).timestamp() * 1000)
    lw_ids, mod_ids, defs, labware, modules, pipettes, tipracks = {}, {}, {}, {}, {}, {}, {}
    deck = {"labwareLocationUpdate": {}, "moduleLocationUpdate": {}, "pipetteLocationUpdate": {}, "trashBinLocationUpdate": {},
            "wasteChuteLocationUpdate": {}, "stagingAreaLocationUpdate": {}, "gripperLocationUpdate": {},
            "stepType": "manualIntervention", "id": "__INITIAL_DECK_SETUP_STEP__"}
    for c in cmds:
        p, r = c["params"], c.get("result") or {}
        if c["commandType"] == "loadModule":
            mod_ids[r["moduleId"]] = f"{r['moduleId']}:{TC_TYPE}" if p["model"].startswith("thermocycler") else r["moduleId"]
            modules[mod_ids[r["moduleId"]]] = {"model": p["model"]}
            deck["moduleLocationUpdate"][mod_ids[r["moduleId"]]] = p["location"]["slotName"]
        elif c["commandType"] == "loadLabware":
            uri = f"{p['namespace']}/{p['loadName']}/{p['version']}"
            defs[uri] = load_definition(p["loadName"], p["version"])
            lw_ids[r["labwareId"]] = f"{r['labwareId']}:{uri}"
            labware[lw_ids[r["labwareId"]]] = {"displayName": p.get("displayName") or defs[uri]["metadata"]["displayName"],
                                               "labwareDefURI": uri}
            loc = p["location"]
            deck["labwareLocationUpdate"][lw_ids[r["labwareId"]]] = mod_ids[loc["moduleId"]] if "moduleId" in loc else loc["slotName"]
        elif c["commandType"] == "loadPipette":
            pipettes[r["pipetteId"]] = {"pipetteName": p["pipetteName"]}
            deck["pipetteLocationUpdate"][r["pipetteId"]] = p["mount"]
        elif c["commandType"] == "pickUpTip":
            tipracks.setdefault(p["pipetteId"], set()).add(labware[lw_ids[p["labwareId"]]]["labwareDefURI"])
        elif c["commandType"] == "moveToAddressableAreaForDropTip":
            trash = f"{_uid(title, 'trash')}:trashBin"
            deck["trashBinLocationUpdate"][trash] = TRASH_CUTOUT[p["addressableAreaName"]]

    racks = sorted({v["labwareDefURI"] for v in labware.values() if "tiprack" in v["labwareDefURI"]})
    for pid, pip in pipettes.items():  # a pipette that never picks a tip still needs its rack named in the editor
        if pid not in tipracks:
            vol = pip["pipetteName"].split("_")[0][1:]
            tipracks[pid] = {r for r in racks if f"_{vol}ul" in r} or set(racks[:1])
    liquids = {str(i): {"displayName": q["displayName"], "description": q.get("description") or "", "displayColor": q["displayColor"]}
               for i, q in enumerate(analysis["liquids"])}
    liquid_key = {q["id"]: str(i) for i, q in enumerate(analysis["liquids"])}
    ingred_locations = {}
    for c in cmds:
        if c["commandType"] == "loadLiquid":
            wells = ingred_locations.setdefault(lw_ids[c["params"]["labwareId"]], {})
            for w, v in c["params"]["volumeByWell"].items():
                wells[w] = {liquid_key[c["params"]["liquidId"]]: {"volume": v}}

    forms, order, label, tip, tc, profile = {}, [], ("", ""), None, None, None

    def add(form):
        form["id"] = _uid(title, len(order))
        forms[form["id"]] = form
        order.append(form["id"])
        return form

    for c in cmds:
        kind, p = c["commandType"], c["params"]
        if kind == "comment":  # "Step 3 · video 106-130 s: Add Primer2" -> step name + details in the editor
            head, _, what = p["message"].partition(": ")
            label = (what or head, head.replace(" · ", ", "))  # the designer shows a middle dot as mojibake
        elif kind == "pickUpTip":
            tip = {"pipette": p["pipetteId"], "rack": labware[lw_ids[p["labwareId"]]]["labwareDefURI"], "at": None, "moves": []}
        elif tip and kind in ("moveToWell", "aspirate", "dispense"):  # where the tip went, in mm above the well bottom
            depth = defs[labware[lw_ids[p["labwareId"]]]["labwareDefURI"]]["wells"][p["wellName"]]["depth"]
            loc = p.get("wellLocation") or {}
            z = loc.get("offset", {}).get("z", 0) + (depth if loc.get("origin", "top") == "top" else 0)
            tip["at"] = (lw_ids[p["labwareId"]], p["wellName"], round(max(z, 0.5), 1))
            if kind != "moveToWell":
                tip["moves"].append([kind, tip["at"], p["volume"], p.get("flowRate"), None])
        elif tip and kind in ("aspirateInPlace", "dispenseInPlace"):
            tip["moves"].append([kind.replace("InPlace", ""), tip["at"], p["volume"], p.get("flowRate"), None])
        elif tip and kind == "waitForDuration" and tip["moves"]:  # the liquid class's pause after a draw or dispense
            tip["moves"][-1][4] = p["seconds"]
        elif kind == "dropTipInPlace" and tip:
            asps = [m for m in tip["moves"] if m[0] == "aspirate"]
            disps = [m for m in tip["moves"] if m[0] == "dispense"]
            name = {"stepName": label[0][:60] or ("mix" if len(asps) > 1 else "transfer"), "stepDetails": label[1]}
            if len(asps) > 1 and len({m[1][:2] for m in tip["moves"]}) == 1:  # up and down in one well: a mix
                add({**MIX_FORM, **name, "pipette": tip["pipette"], "tipRack": tip["rack"], "labware": asps[0][1][0],
                     "wells": [asps[0][1][1]], "volume": _num(asps[0][2]), "times": str(len(asps)),
                     "mix_mmFromBottom": asps[0][1][2], "dropTip_location": trash})
            elif asps and disps:
                asp, disp = max(asps, key=lambda m: m[2]), max(disps, key=lambda m: m[2])  # not the air gap's dispense
                add({**LIQUID_FORM, **name, "pipette": tip["pipette"], "tipRack": tip["rack"], "volume": _num(asp[2]),
                     "aspirate_labware": asp[1][0], "aspirate_wells": [asp[1][1]], "aspirate_mmFromBottom": asp[1][2],
                     "dispense_labware": disp[1][0], "dispense_wells": [disp[1][1]], "dispense_mmFromBottom": disp[1][2],
                     "dropTip_location": trash,
                     # the Opentrons liquid class's rates and pauses, so an edit in the designer keeps the premix slow
                     "aspirate_flowRate": asp[3] and round(asp[3], 2), "dispense_flowRate": disp[3] and round(disp[3], 2),
                     "aspirate_delay_checkbox": bool(asp[4]), "aspirate_delay_seconds": _num(asp[4] or 1),
                     "dispense_delay_checkbox": bool(disp[4]), "dispense_delay_seconds": _num(disp[4] or 1)})
            tip = None
        elif kind == "thermocycler/setTargetLidTemperature":
            tc = p["celsius"]
        elif kind == "thermocycler/runExtendedProfile":
            if profile is None:  # consecutive profiles (initial denaturation, then the cycles) are one editor step
                profile = add({**json.loads(json.dumps(TC_FORM)), "moduleId": mod_ids[p["moduleId"]], "thermocyclerFormType": "thermocyclerProfile",
                               "lidIsActive": tc is not None, "lidTargetTemp": tc, "profileTargetLidTemp": _num(tc or 105),
                               "profileVolume": _num(p.get("blockMaxVolumeUl") or 25), "stepName": "PCR program"})
            for i, el in enumerate(p["profileElements"]):
                steps = [{"type": "profileStep", "id": _uid(title, profile["id"], len(profile["orderedProfileItems"]), i, j),
                          "title": "", "temperature": _num(s["celsius"]), "durationMinutes": str(int(s["holdSeconds"] // 60)),
                          "durationSeconds": _num(s["holdSeconds"] % 60)} for j, s in enumerate(el["steps"])]
                if el["repetitions"] > 1:
                    items = [{"type": "profileCycle", "id": _uid(title, profile["id"], "cycle", len(profile["orderedProfileItems"])),
                              "repetitions": str(el["repetitions"]), "steps": steps}]
                else:
                    items = steps
                for it in items:
                    profile["orderedProfileItems"].append(it["id"])
                    profile["profileItemsById"][it["id"]] = it
        elif kind == "thermocycler/setTargetBlockTemperature" and profile:  # the hold after the program is part of
            # the profile step (Protocol Designer shows it as the step right after the program)
            profile.update(blockIsActiveHold=True, blockTargetTempHold=_num(p["celsius"]), lidIsActiveHold=False, lidOpenHold=False)
            profile = None
        elif kind == "thermocycler/setTargetBlockTemperature":
            add({**json.loads(json.dumps(TC_FORM)), "moduleId": mod_ids[p["moduleId"]], "thermocyclerFormType": "thermocyclerState",
                 "blockIsActive": True, "blockTargetTemp": _num(p["celsius"]), "stepName": f"hold at {_num(p['celsius'])} °C"})
            profile = None
        elif kind == "thermocycler/openLid":  # Protocol Designer starts with the lid closed, like the module powers up
            last = forms[order[-1]] if order else {}
            add({**json.loads(json.dumps(TC_FORM)), "moduleId": mod_ids[p["moduleId"]], "thermocyclerFormType": "thermocyclerState", "lidOpen": True,
                 "blockIsActive": last.get("blockIsActive", False), "blockTargetTemp": last.get("blockTargetTemp"),
                 "stepName": "open lid"})
            profile = None

    return {
        "$otSharedSchema": "#/protocol/schemas/8", "schemaVersion": 8,
        "metadata": {"protocolName": title, "author": "Show, Don't Code", "description":
                     "Recovered from a first-person video of the protocol done by hand. Every value stays editable here.",
                     "created": created, "lastModified": created, "category": None, "subcategory": None, "tags": []},
        "designerApplication": {"name": "opentrons/protocol-designer", "version": PD_VERSION, "data": {
            "_internalAppBuildDate": "", "pipetteTiprackAssignments": {k: sorted(v) for k, v in tipracks.items()},
            "dismissedWarnings": {"form": [], "timeline": []},
            "ingredients": {k: {**v, "liquidGroupId": k, "liquidClass": None} for k, v in liquids.items()},
            "ingredLocations": ingred_locations, "savedStepForms": {deck["id"]: deck, **forms}, "orderedStepIds": order,
            "pipettes": pipettes, "modules": modules, "labware": labware}},
        "robot": {"model": "OT-3 Standard", "deckId": "ot3_standard"},
        "labwareDefinitionSchemaId": "opentronsLabwareSchemaV2", "labwareDefinitions": defs,
        "liquidSchemaId": "opentronsLiquidSchemaV1", "liquids": liquids,
        "commandSchemaId": "opentronsCommandSchemaV14",
        "commands": [_rewrite(c, lw_ids, mod_ids, liquid_key) for c in cmds],
        "commandAnnotationSchemaId": "opentronsCommandAnnotationSchemaV1", "commandAnnotations": [],
    }


def _rewrite(c, lw_ids, mod_ids, liquid_key):
    """An analysed command as a file command, with the file's ids (Protocol Designer names labware 'id:uri' and
    modules 'id:type'); a load command also names the id it creates, which analysis only reports in its result."""
    out = {k: c[k] for k in ("commandType", "key") if k in c}
    p = out["params"] = json.loads(json.dumps(c["params"]))
    for k, table in (("labwareId", lw_ids), ("moduleId", mod_ids), ("liquidId", liquid_key)):
        if p.get(k) in table:
            p[k] = table[p[k]]
    if isinstance(p.get("location"), dict) and p["location"].get("moduleId") in mod_ids:
        p["location"]["moduleId"] = mod_ids[p["location"]["moduleId"]]
    made = c.get("result") or {}
    if c["commandType"] == "loadLabware":
        p["labwareId"] = lw_ids[made["labwareId"]]
    elif c["commandType"] == "loadModule":
        p["moduleId"] = mod_ids[made["moduleId"]]
    elif c["commandType"] == "loadPipette":
        p["pipetteId"] = made["pipetteId"]
    return out


def moved(commands):
    """(kind, µL) of every aspirate and dispense, in order: what two files must agree on to do the same liquid handling."""
    return [(c["commandType"].replace("InPlace", ""), c["params"]["volume"]) for c in commands
            if c["commandType"] in ("aspirate", "aspirateInPlace", "dispense", "dispenseInPlace")]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from . import protocol

    demo = [{"start": 30, "end": 45, "action": "transfer", "description": "Add sterile water", "reagent": "water",
             "volume_ul": 20, "destination": "PCR tube"},
            {"start": 64, "end": 99, "action": "transfer", "description": "Add Primer1", "reagent": "Primer1", "volume_ul": 1.5,
             "destination": "PCR tube"},
            {"start": 239, "end": 250, "action": "mix", "description": "Pipette to mix", "volume_ul": 20, "destination": "PCR tube"},
            {"start": 260, "end": 266, "action": "thermocycle", "description": "Set in DNA engine", "cycles": 35}]
    src, _ = protocol.generate(demo)
    tmp = Path(tempfile.mkdtemp())
    ok, an = protocol.analyze(src, tmp / "a.json")
    assert ok, an
    a = json.loads((tmp / "a.json").read_text(encoding="utf-8"))
    pd = protocol_designer(a, "self-check")
    data = pd["designerApplication"]["data"]
    forms = [data["savedStepForms"][i] for i in data["orderedStepIds"]]
    assert [f["stepType"] for f in forms] == ["thermocycler", "moveLiquid", "moveLiquid", "mix", "thermocycler", "thermocycler"], forms
    assert [f.get("volume") for f in forms[1:4]] == ["20", "1.5", "20"] and forms[3]["times"] == "3", forms[1:4]
    assert forms[1]["stepName"] == "Add sterile water" and forms[1]["stepDetails"] == "Step 1, video 30-45 s", forms[1]
    prof = forms[4]
    cycle = next(prof["profileItemsById"][i] for i in prof["orderedProfileItems"] if prof["profileItemsById"][i]["type"] == "profileCycle")
    assert cycle["repetitions"] == "35" and prof["blockTargetTempHold"] == "4" and forms[5]["lidOpen"], prof
    assert all(pd["designerApplication"]["data"]["pipetteTiprackAssignments"].values())
    rates = [c["params"]["flowRate"] for c in a["commands"] if c["commandType"] in ("aspirate", "aspirateInPlace")]
    assert forms[1]["aspirate_flowRate"] in [round(r, 2) for r in rates] and forms[1]["dispense_flowRate"] and forms[1]["aspirate_delay_checkbox"] in (True, False), forms[1]
    # Opentrons runs the file itself: same liquid handling and the same thermocycler time as protocol.py
    (tmp / "pd.json").write_text(json.dumps(pd), encoding="utf-8")
    ok2, an2 = protocol.analyze(json.dumps(pd), tmp / "b.json", "protocol.json")
    assert ok2, an2
    b = json.loads((tmp / "b.json").read_text(encoding="utf-8"))
    assert moved(b["commands"]) == moved(a["commands"]) and an2["cycling_s"] == an["cycling_s"], (moved(b["commands"]), an2)
    print(f"{len(forms)} editor steps, {an2['commands']} commands analysed by Opentrons, same volumes and "
          f"{an2['cycling_s']} s of cycling as protocol.py: OK")
