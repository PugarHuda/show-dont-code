"""Cross-check a run's volumes against published protocols that use the same kit: Muse Spark reads each protocols.io
protocol and finds the volume it uses for each of this run's reagents, with a verbatim quote; plain code then keeps
only quotes that really are in the protocol's text and compares each reagent's share of the reaction (so a 50 uL
reaction with 25 uL premix agrees with a 25 uL reaction with 12.5 uL).

  python -m sdc.crosscheck pcr_1-gt      (writes out/<run>/crosscheck.json)
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from . import meta, pio

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reaction_ul", "matches"],
    "properties": {
        "reaction_ul": {"type": ["number", "null"], "description": "total volume of one reaction in this protocol, uL"},
        "matches": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["ours", "theirs", "volume_ul", "quote"],
            "properties": {
                "ours": {"type": "string", "description": "the reagent name exactly as given in OUR list"},
                "theirs": {"type": "string", "description": "the same reagent as this protocol names it"},
                "volume_ul": {"type": ["number", "null"], "description": "uL per reaction in this protocol"},
                "quote": {"type": "string", "description": "verbatim text from the protocol stating that volume"}}}},
    },
}
AGREE = 0.2  # shares within 20 % of each other count as the same recipe


def reaction(steps):
    """(per-reagent uL, total uL) of the fullest reaction well: what one filmed reaction holds."""
    wells = {}
    for s in steps:
        if s.get("action") == "transfer" and s.get("volume_ul") and s.get("reagent"):
            w = wells.setdefault(s.get("destination") or "sample tube", {})
            w[s["reagent"]] = w.get(s["reagent"], 0) + float(s["volume_ul"])
    if not wells:
        return {}, 0
    well = max(wells.values(), key=lambda w: sum(w.values()))
    return well, sum(well.values())


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w.µ ]", " ", s.lower())).strip()


def read(ours, text):
    r = meta.client().responses.create(
        model=meta.SPARK, max_output_tokens=16000,  # reasoning shares this budget
        input=[{"role": "user", "content": [{"type": "input_text", "text": (
            "Below is a published lab protocol, then OUR list of reagents. For each of OUR reagents that this protocol "
            "also uses (same reagent, possibly another name, e.g. 'PrimeSTAR Max Premix' = '2x PrimeSTAR Max'), give the "
            "volume it uses per single reaction in microlitres and copy the exact sentence or table row that states it. "
            "Leave out reagents it doesn't use or gives no volume for. Also give the total volume of one reaction. "
            "Do not compute or guess a volume the text doesn't state.\n\nOUR reagents: " + "; ".join(ours) +
            "\n\nPROTOCOL:\n" + text[:24000])}]}],
        text={"format": {"type": "json_schema", "name": "volumes", "schema": SCHEMA, "strict": True}})
    if not r.output_text:
        raise RuntimeError(f"Muse Spark returned no text: status={r.status}")
    return json.loads(r.output_text)


def crosscheck(steps, related, top=3):
    """Compare with up to `top` related protocols that mention this run's kit words (else the top ones)."""
    mine, total = reaction(steps)
    if not mine:
        raise ValueError("no confirmed volumes to compare yet")
    picks = [p for p in related["protocols"] if p["matched"] and p.get("doi")][:top] or \
            [p for p in related["protocols"] if p.get("doi")][:top]
    def one(p):
        try:
            src = pio.fetch(p["doi"])
        except Exception as e:  # noqa: BLE001 - unpublished since the search, or protocols.io down
            return {"title": p["title"], "doi": p["doi"], "url": p["url"], "error": str(e)[:200], "rows": []}
        got = read(sorted(mine), src["text"])
        body, rows = _norm(src["text"]), []
        their_total = got["reaction_ul"] if got["reaction_ul"] and got["reaction_ul"] > 0 else None
        for m in got["matches"]:
            # model output is untrusted: an unknown reagent or a quote that isn't in the protocol is dropped
            if m["ours"] not in mine or not m["volume_ul"] or m["volume_ul"] <= 0 or not _norm(m["quote"])                     or _norm(m["quote"]) not in body:
                continue
            share_ours = mine[m["ours"]] / total
            share_theirs = m["volume_ul"] / their_total if their_total else None
            agree = None if share_theirs is None else abs(share_ours - share_theirs) <= AGREE * max(share_ours, share_theirs)
            rows.append({"reagent": m["ours"], "ours_ul": mine[m["ours"]], "theirs_name": m["theirs"][:80],
                         "theirs_ul": m["volume_ul"], "share_ours": round(share_ours, 3),
                         "share_theirs": None if share_theirs is None else round(share_theirs, 3),
                         "agree": agree, "quote": m["quote"][:300]})
        return {"title": p["title"], "doi": p["doi"], "url": p["url"], "reaction_ul": their_total, "rows": rows}

    with ThreadPoolExecutor(top) as pool:  # one Muse Spark read per protocol, side by side
        out = list(pool.map(one, picks))
    return {"model": meta.SPARK, "reaction_ul": total, "ours": mine, "protocols": out}


def _selfcheck():  # python -m sdc.crosscheck --self-check: the parts that need no network
    steps = [{"action": "transfer", "reagent": "premix", "volume_ul": 25.0, "destination": "tube 1"},
             {"action": "transfer", "reagent": "water", "volume_ul": 20.0, "destination": "tube 1"},
             {"action": "transfer", "reagent": "primer", "volume_ul": 1.0, "destination": "tube 2"},
             {"action": "mix", "reagent": None, "volume_ul": 10.0, "destination": "tube 1"}]
    mine, total = reaction(steps)
    assert mine == {"premix": 25.0, "water": 20.0} and total == 45.0, (mine, total)  # the fullest well, transfers only
    body = _norm("Component | Volume\nPrimeStar Mix (2X) | 25 uL\n")
    assert _norm("PrimeStar Mix (2X) | 25 uL") in body and _norm("PrimeStar Mix (2X) | 2.5 uL") not in body
    print("crosscheck self-check OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _selfcheck()
        sys.exit(0)
    from . import pipeline, related as rel

    for name in sys.argv[1:]:
        d = pipeline.OUT / name
        steps = json.loads((d / "result.json").read_text(encoding="utf-8"))["steps"]
        c = crosscheck(steps, rel.related(steps))
        c["key"] = rel.terms(steps)[0] + [f"{k}={v}" for k, v in sorted(c["ours"].items())]
        (d / "crosscheck.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
        for p in c["protocols"]:
            print(name, p["doi"], p.get("error") or [(r["reagent"], r["ours_ul"], r["theirs_ul"], r["agree"]) for r in p["rows"]])
