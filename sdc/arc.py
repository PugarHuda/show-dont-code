"""ARC export (Annotated Research Context, nfdi4plants/DataPLANT): the run as an ISA investigation with one assay whose
table has a row per robot step (source tube -> Opentrons Flex action -> volume or cycles -> destination), written with
the official arctrl library, plus the run's files: protocol.py and the methods under the assay's protocols/, the
receipt and Opentrons' analysis under its dataset/. Zipped, it opens in ARCitect and the DataHUB.

  python -m sdc.arc        (self-check: write an ARC, read it back with arctrl, compare)
"""
import io
import tempfile
import zipfile
from pathlib import Path

from sdc import terms

VOLUME = ("volume", "PATO", "http://purl.obolibrary.org/obo/PATO_0000918")
MICROLITRE = ("microliter", "UO", "http://purl.obolibrary.org/obo/UO_0000101")
CYCLES = ("number of cycles", "", "")
ROBOT = {"transfer", "mix", "thermocycle"}


def investigation(name, title, steps, author=True, description=""):
    from arctrl import (ArcAssay, ArcInvestigation, ArcTable, CompositeCell, CompositeHeader, IOType,
                        OntologyAnnotation, Person)

    inv = ArcInvestigation.init(f"show-dont-code-{name}")
    inv.Title = title
    inv.Description = description or "An Opentrons Flex protocol made by Show, Don't Code, with the checks it passed."
    if author:
        inv.Contacts.append(Person.create(first_name="Pugar Huda", last_name="Mantoro"))
    vol, ul, cyc = OntologyAnnotation(*VOLUME), OntologyAnnotation(*MICROLITRE), OntologyAnnotation(*CYCLES)
    rows = [s for s in steps if s.get("action") in ROBOT]
    t = ArcTable.init("Opentrons Flex steps")
    t.AddColumn(CompositeHeader.input(IOType.source()),
                [CompositeCell.create_free_text(s.get("source") or s.get("reagent") or "") for s in rows])
    t.AddColumn(CompositeHeader.protocol_ref(),
                [CompositeCell.create_free_text(f"Opentrons Flex {s['action']}") for s in rows])

    def term(t):  # an ontology term from sdc.terms, or an empty term where no exact one exists
        return OntologyAnnotation(t[0], t[1], terms.iri(t)) if t else OntologyAnnotation("")

    t.AddColumn(CompositeHeader.protocol_type(), [CompositeCell.create_term(term(terms.ACTION.get(s["action"]))) for s in rows])
    t.AddColumn(CompositeHeader.component(term(terms.DEVICE)),
                [CompositeCell.create_term(term(terms.EQUIPMENT.get(s["action"]))) for s in rows])
    t.AddColumn(CompositeHeader.parameter(OntologyAnnotation("reagent")),
                [CompositeCell.create_term(OntologyAnnotation(s.get("reagent") or "")) for s in rows])  # terms, by ISA rules
    t.AddColumn(CompositeHeader.parameter(vol),
                [CompositeCell.create_unitized(f"{s['volume_ul']:g}", ul) if s.get("volume_ul")
                 else CompositeCell.create_unitized("", ul) for s in rows])
    t.AddColumn(CompositeHeader.parameter(cyc),
                [CompositeCell.create_term(OntologyAnnotation(str(s["cycles"]) if s.get("cycles") else "")) for s in rows])
    t.AddColumn(CompositeHeader.output(IOType.sample()),
                [CompositeCell.create_free_text(s.get("destination") or ("thermocycler plate" if s["action"] ==
                                                                        "thermocycle" else "")) for s in rows])
    assay = ArcAssay.init("liquid_handling")
    assay.MeasurementType = OntologyAnnotation("liquid handling")
    assay.TechnologyPlatform = OntologyAnnotation("Opentrons Flex")  # a product: no ontology term; its type is the column above
    assay.AddTable(t)
    inv.AddAssay(assay)
    return inv


def build(name, title, steps, files, author=True, licence=None):
    """files: {filename: bytes} of the run. Returns the ARC as zip bytes (one root folder)."""
    from arctrl import ARC

    arc = ARC.from_arc_investigation(investigation(name, title, steps, author))
    if licence:
        arc.SetLicenseFulltext(licence)
    root = f"show-dont-code-{name}"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / root
        arc.Write(str(d))
        assay = d / "assays" / "liquid_handling"
        for f, b in files.items():  # code and methods are the assay's protocols; results and analyses its dataset
            sub = "protocols" if f in ("protocol.py", "methods.md", "protocol-designer.json") else "dataset"
            (assay / sub / f).write_bytes(b)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(Path(tmp)).as_posix())
    return buf.getvalue()


if __name__ == "__main__":  # write, read back with arctrl, compare
    from arctrl import ARC

    steps = [{"action": "transfer", "reagent": "water", "source": "source tube 1", "volume_ul": 20.0,
              "destination": "tube 1"},
             {"action": "transfer", "reagent": "premix", "volume_ul": 25.0, "destination": "tube 1"},
             {"action": "centrifuge"},
             {"action": "thermocycle", "cycles": 35}]
    data = build("selfcheck", "Self-check", steps, {"protocol.py": b"x", "receipt.json": b"{}"}, licence="CC BY 4.0")
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        root = Path(tmp) / "show-dont-code-selfcheck"
        assert (root / "assays/liquid_handling/protocols/protocol.py").read_bytes() == b"x"
        assert (root / "assays/liquid_handling/dataset/receipt.json").exists()
        t = ARC.load(str(root)).GetAssay("liquid_handling").Tables[0]
        assert t.RowCount == 3, t.RowCount  # the centrifuge is a hand-off, not a robot step
        heads = [str(h) for h in t.Headers]
        assert heads[0] == "Input [Source Name]" and heads[-1] == "Output [Sample Name]", heads
        kinds = [str(t.GetCellAt(heads.index("Component [device]"), r)) for r in range(3)]
        assert "liquid handler" in kinds[0] and "thermal cycler" in kinds[2], kinds
        assert str(t.GetCellAt(0, 0)).find("source tube 1") >= 0 and "20" in str(t.GetCellAt(heads.index("Parameter [volume]"), 0)), heads
    print("arc self-check OK")
