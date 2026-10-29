"""A run packaged as an RO-Crate (Process Run Crate 0.5): every file of the run, its SHA-256, and which tool made which
file from which inputs, as JSON-LD that WorkflowHub, Galaxy and any RO-Crate reader understand.

Validate a downloaded crate with: rocrate-validator validate -p process-run-crate <unzipped dir>
"""
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROFILE = "https://w3id.org/ro/wfrun/process/0.5"
BIOVL2 = "https://zenodo.org/records/21495188"
AUTHOR = "https://github.com/PugarHuda"
REPO, OPENTRONS, META = "https://github.com/PugarHuda/show-dont-code", "https://github.com/Opentrons/opentrons", "https://api.meta.ai/v1"
FORMATS = {".py": "text/x-python", ".json": "application/json", ".md": "text/markdown",
           ".ots": "application/vnd.opentimestamps.ots"}
OTS = "https://opentimestamps.org"
MODEL_OUTPUTS = {"steps_raw.json": "Steps found in the video (Muse Spark, stage 1)",
                 "steps_stage2.json": "Transfers grouped by source tube and named (Muse Spark, stage 2)",
                 "sheet.json": "Bench sheet read from full-resolution stills (Muse Spark)",
                 "profile.json": "Thermal cycling program read off the bench sheet (Muse Spark)",
                 "speech.json": "Speech in the video, timestamped (Muse Voice Transcribe)",
                 "tracks_tip.json": "Pipette tip tracked frame by frame (SAM 3.1)"}
NAMES = {"review.json": "Reviewer agent findings and its simulator tool calls", "answers.json": "The scientist's answers",
         "receipt.json": "Reproducibility receipt", "pylabrobot.py": "Hamilton STARlet translation (PyLabRobot)",
         "tecan-evo.py": "Tecan Freedom EVO 150 translation (PyLabRobot)", "analysis.json": "Opentrons protocol analysis",
         "protocol-designer.json": "The protocol as editable steps for Opentrons Protocol Designer (no code)",
         "receipt.ots": "OpenTimestamps proof anchoring the receipt in Bitcoin"}


def _when(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def build(run_dir, name, generated, models):
    """generated: {filename: bytes} made on request (protocol, exports, receipt). models: receipt["models"].
    Returns the crate as zip bytes."""
    d = Path(run_dir)
    files = dict(generated)
    for f in ("analysis.json", "answers.json", *MODEL_OUTPUTS):
        if (d / f).exists():
            files[f] = (d / f).read_bytes()
    from_gt = "note" in models
    from .pipeline import asset

    video = asset(d, "video.mp4")
    graph = [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"},
         "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}},
        {"@id": PROFILE, "@type": "CreativeWork", "name": "Process Run Crate", "version": "0.5"},
    ]
    entities = []
    for f, data in sorted(files.items()):
        entities.append({"@id": f, "@type": "File", "name": MODEL_OUTPUTS.get(f) or NAMES.get(f, f), "contentSize": str(len(data)),
                         "encodingFormat": FORMATS.get(Path(f).suffix, "application/octet-stream"),
                         "sha256": hashlib.sha256(data).hexdigest()})
    # the footage stays out of the crate (size, privacy of uploads); its hash pins exactly which video this was
    src = {"@id": "#source-video", "@type": "MediaObject", "name": "First-person video of the protocol done by hand",
           "encodingFormat": "video/mp4"}
    if video.exists():
        src |= {"sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "contentSize": str(video.stat().st_size)}
    edited = name.startswith(("u-", "c-"))  # uploads and visitors' edited copies: not the author's work
    if not name.startswith("u-"):  # sample runs and their edited copies come from BioVL2
        src |= {"isPartOf": {"@id": BIOVL2}, "license": {"@id": "https://creativecommons.org/licenses/by/4.0/"}}
        graph.append({"@id": BIOVL2, "@type": "CreativeWork", "name": "BioVL2 (Nishimura et al., Kyoto University)",
                      "license": {"@id": "https://creativecommons.org/licenses/by/4.0/"}})
        graph.append({"@id": "https://creativecommons.org/licenses/by/4.0/", "@type": "CreativeWork", "name": "CC BY 4.0"})
    entities.append(src)

    def app(id_, name_, url, version=None):
        e = {"@id": id_, "@type": "SoftwareApplication", "name": name_, "url": url}
        return e | ({"softwareVersion": version} if version else {})

    import opentrons

    # the code generator's version is the hash of its own source, so a crate pins the exact codegen that ran
    codegen = hashlib.sha256(b"".join((Path(__file__).parent / f).read_bytes() for f in ("protocol.py", "export.py")))
    tools = [app(REPO, "Show, Don't Code", REPO, f"codegen-{codegen.hexdigest()[:12]}"),
             app(OPENTRONS, "Opentrons protocol analysis and simulator", OPENTRONS, opentrons.__version__)]
    have = lambda *fs: [{"@id": f} for f in fs if f in files]  # noqa: E731
    actions = []
    if not from_gt:
        tools += [app(META + "#muse-spark", "Muse Spark", META, models.get("steps_and_sheet")),
                  app(META + "#sam", "SAM 3.1", META, models.get("tracking")),
                  app(META + "#voice", "Muse Voice Transcribe", META, models.get("speech"))]
        actions.append({"@id": "#read-video", "@type": "CreateAction", "name": "Read the video with Meta models",
                        "description": "Stage 1 and 2 step finding, bench sheet reading, tip tracking and speech",
                        "instrument": [{"@id": META + "#muse-spark"}, {"@id": META + "#sam"}, {"@id": META + "#voice"}],
                        "object": {"@id": "#source-video"},
                        "result": have(*MODEL_OUTPUTS), "endTime": _when(d / "steps_stage2.json")
                        if (d / "steps_stage2.json").exists() else None, "actionStatus": "http://schema.org/CompletedActionStatus"})
    actions.append({"@id": "#generate", "@type": "CreateAction",
                    "name": "Deterministic code generation from the confirmed steps and the scientist's answers",
                    "description": "Model text only lands in comments and string literals; numbers are validated",
                    "instrument": {"@id": REPO},
                    "object": ([{"@id": "#source-video"}] if from_gt else have(*MODEL_OUTPUTS)) + have("answers.json"),
                    "result": have("protocol.py", "methods.md", "autoprotocol.json", "pylabrobot.py", "tecan-evo.py"),
                    "endTime": _when(d / "result.json"), "actionStatus": "http://schema.org/CompletedActionStatus"})
    if "analysis.json" in files:
        actions.append({"@id": "#analyze", "@type": "CreateAction", "name": "opentrons analyze (what the Opentrons App runs on import)",
                        "description": "Protocol analysis of the generated file, same as on import into the Opentrons App",
                        "instrument": {"@id": OPENTRONS}, "object": have("protocol.py"), "result": have("analysis.json"),
                        "endTime": _when(d / "analysis.json"), "actionStatus": "http://schema.org/CompletedActionStatus"})
    if "protocol-designer.json" in files:
        actions.append({"@id": "#designer", "@type": "CreateAction", "name": "Analysed commands to Protocol Designer steps",
                        "description": "Every transfer, mix and thermocycler step read off Opentrons' own analysis",
                        "instrument": {"@id": REPO}, "object": have("analysis.json"), "result": have("protocol-designer.json"),
                        "actionStatus": "http://schema.org/CompletedActionStatus"})
    if "receipt.ots" in files:
        tools.append(app(OTS, "OpenTimestamps (Bitcoin-anchored timestamp)", OTS))
        actions.append({"@id": "#timestamp", "@type": "CreateAction", "name": "Timestamp the receipt in Bitcoin",
                        "description": "sha256 of the receipt (without its 'timestamp' field) sent to public "
                                       "OpenTimestamps calendars; their Bitcoin transactions anchor it to a block",
                        "instrument": {"@id": OTS}, "object": have("receipt.json"), "result": have("receipt.ots"),
                        "actionStatus": "http://schema.org/CompletedActionStatus"})
    rv = models.get("reviewer")
    if rv and "review.json" in files:  # which host ran the reviewer: Baseten, or Fireworks after a failover
        host = "https://fireworks.ai" if rv.startswith("fireworks") else "https://www.baseten.co"
        tools.append(app(host + "#muse-spark-open", "Muse Spark Open (reviewer agent)", host, rv))
        actions.append({"@id": "#review", "@type": "CreateAction", "name": "Reviewer agent checks the steps against the video",
                        "description": "Watches the video, may look at single moments, runs the Opentrons simulator as a tool",
                        "instrument": {"@id": host + "#muse-spark-open"},
                        "object": [{"@id": "#source-video"}] + have("steps_stage2.json", "sheet.json"),
                        "result": have("review.json"), "actionStatus": "http://schema.org/CompletedActionStatus"})
    if not edited:  # the sample runs were run by the author; uploads and edits stay anonymous
        graph += [{"@id": AUTHOR, "@type": "Person", "name": "Pugar Huda Mantoro", "affiliation": {"@id": "https://www.uii.ac.id"}},
                  {"@id": "https://www.uii.ac.id", "@type": "Organization", "name": "Universitas Islam Indonesia",
                   "url": "https://www.uii.ac.id"}]
        for a in actions:
            a["agent"] = {"@id": AUTHOR}
    for a in actions:
        if a.get("endTime") is None:
            a.pop("endTime", None)
    for a in actions:  # RO-Crate prefers single values to one-element lists
        for k in ("object", "result", "instrument"):
            if isinstance(a.get(k), list) and len(a[k]) == 1:
                a[k] = a[k][0]
    root = {"@id": "./", "@type": "Dataset", "name": f"Show, Don't Code run {name}",
            "description": "An Opentrons Flex protocol recovered from a first-person video of the protocol done by hand, "
                           "with every intermediate model output and the checks it passed.",
            "datePublished": _when(d / "result.json"), "conformsTo": {"@id": PROFILE},
            "hasPart": [{"@id": e["@id"]} for e in entities if e["@type"] == "File"],
            "mentions": [{"@id": a["@id"]} for a in actions]}
    if not name.startswith("u-"):
        root["isBasedOn"] = {"@id": BIOVL2}
    if not edited:
        root["license"] = {"@id": "https://creativecommons.org/licenses/by/4.0/"}
        root["author"] = {"@id": AUTHOR}
    meta = {"@context": ["https://w3id.org/ro/crate/1.1/context", {"sha256": "http://schema.org/sha256"}],
            "@graph": graph + [root] + entities + tools + actions}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ro-crate-metadata.json", json.dumps(meta, indent=1))
        for f, data in files.items():
            z.writestr(f, data)
    return buf.getvalue()
