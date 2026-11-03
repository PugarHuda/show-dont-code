"""Ontology terms for steps and equipment, used by the ARC and Bioschemas exports. Only exact matches: each IRI was
looked up in EBI OLS, and `python -m sdc.terms` checks every one there again (label and not obsolete). Actions with no
exact term (a pipetted transfer, a thermocycling step, an inversion) stay plain text rather than get a near miss.
"""
OBO = "http://purl.obolibrary.org/obo/"

DEVICE = ("device", "OBI", "OBI_0000968")  # the ARC column the equipment terms sit in
ACTION = {"mix": ("mixing", "CHMO", "CHMO_0001685"), "centrifuge": ("centrifugation", "OBI", "OBI_0302886")}
EQUIPMENT = {  # which instrument a step's action needs
    "transfer": ("liquid handler", "OBI", "OBI_0400112"), "mix": ("liquid handler", "OBI", "OBI_0400112"),
    "thermocycle": ("thermal cycler", "OBI", "OBI_0400116"), "centrifuge": ("centrifuge", "OBI", "OBI_0400106"),
    "vortex": ("vortexer", "OBI", "OBI_0400118"),
}


def iri(term):
    return OBO + term[2]


def equipment(steps):
    """The instruments the steps need, each once, in step order."""
    seen = {}
    for s in steps:
        t = EQUIPMENT.get(s.get("action"))
        if t:
            seen.setdefault(t[2], t)
    return list(seen.values())


if __name__ == "__main__":  # every term still resolves in OLS with the same label, and is not obsolete
    import json
    import urllib.request

    for label, onto, code in {DEVICE, *ACTION.values(), *EQUIPMENT.values()}:
        url = f"https://www.ebi.ac.uk/ols4/api/terms?iri={OBO}{code}"
        with urllib.request.urlopen(url, timeout=30) as r:
            hits = json.load(r)["_embedded"]["terms"]
        assert any(h["label"].lower() == label and not h.get("is_obsolete") for h in hits), (code, [h["label"] for h in hits])
    assert [t[0] for t in equipment([{"action": "transfer"}, {"action": "mix"}, {"action": "thermocycle"}])] == \
        ["liquid handler", "thermal cycler"]
    print(f"terms OK ({len({DEVICE, *ACTION.values(), *EQUIPMENT.values()})} IRIs confirmed in EBI OLS)")
