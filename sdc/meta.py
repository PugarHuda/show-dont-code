"""Meta Model API calls: Muse Spark (video -> steps) and SAM 3.1 (video -> tracked boxes)."""
import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from meta_sam_parser import parse_responses_stream, video_segmentation_format
from openai import AsyncOpenAI, OpenAI

load_dotenv()
BASE_URL = "https://api.meta.ai/v1"
SPARK = os.getenv("SPARK_MODEL", "muse-spark-1.3")

ACTIONS = ["transfer", "mix", "thermocycle", "centrifuge", "vortex", "invert", "incubate", "discard", "other"]
STEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end", "action", "description", "reagent", "source", "destination",
                             "volume_ul", "volume_evidence", "confidence"],
                "properties": {
                    "start": {"type": "number", "description": "seconds from video start"},
                    "end": {"type": "number"},
                    "action": {"type": "string", "enum": ACTIONS},
                    "description": {"type": "string", "description": "imperative, like a protocol line"},
                    "reagent": {"type": ["string", "null"]},
                    "source": {"type": ["string", "null"], "description": "tube/container liquid is taken from"},
                    "destination": {"type": ["string", "null"], "description": "tube/container liquid goes into"},
                    "volume_ul": {"type": ["number", "null"]},
                    "volume_evidence": {"type": "string", "enum": ["spoken", "pipette_dial", "label", "none"]},
                    "confidence": {"type": "number", "description": "0-1, how sure the step happened as described"},
                },
            },
        }
    },
}

PROMPT = """You are watching a first-person video of a scientist performing a wet-lab protocol by hand.
List every protocol step in the order it happens, with start/end times in seconds.
- One liquid transfer (aspirate from a source, dispense into a destination) is one "transfer" step.
  If the same reagent goes into several tubes, that is one transfer step per tube.
- Identify each reagent: read tube labels and the printed/handwritten protocol sheet on the bench, and listen
  to speech. Typical PCR setup reagents are water, primers, template DNA and a polymerase premix/master mix.
  Never write a generic name like "stock solution" when a specific one is visible or inferable from the sheet;
  if it truly cannot be identified, use the cap colour and position (e.g. "orange-cap tube, front rack").
- Destination tubes are physical objects: count how many distinct reaction tubes are being filled and name
  them "tube 1", "tube 2", ... by their left-to-right position. Reuse the same name every time that tube is
  filled again. Do not invent a new tube for every transfer.
- Only give volume_ul if you can see it on the pipette dial, read it on the protocol sheet, or hear it; else null.
- Steps a liquid handler cannot do (centrifuge, vortex, invert, placing tubes in a machine) still get listed.
- Placing tubes into a PCR machine / thermal cycler / "DNA engine" is action "thermocycle".
- Ignore idle time, adjusting gloves, and looking around."""


SHEET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["found", "reagents", "notes"],
    "properties": {
        "found": {"type": "boolean", "description": "a written protocol is legible in the images"},
        "reagents": {
            "type": "array",
            "description": "the recipe the scientist is following, in the order written",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "volume_ul"],
                "properties": {"name": {"type": "string"}, "volume_ul": {"type": ["number", "null"]}},
            },
        },
        "notes": {"type": "string", "description": "handwritten notes relevant to the run, e.g. number of samples"},
    },
}


def _key():
    key = os.getenv("MODEL_API_KEY")
    if not key:
        raise RuntimeError("MODEL_API_KEY missing: put it in .env")
    return key


def client():
    return OpenAI(base_url=BASE_URL, api_key=_key())


def shrink(src, dst, fps=None, width=640):
    """Re-encode for upload: smaller, H.264, faststart (SAM needs the index up front). Keeps audio."""
    dst = Path(dst)
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={width}:-2" + (f",fps={fps}" if fps else "")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf, "-c:v", "libx264", "-crf", "28",
                    "-preset", "veryfast", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst)],
                   check=True)
    return dst


def upload(path):
    with open(path, "rb") as f:
        return client().files.create(file=f, purpose="user_data").id


def frames(src, times, dst_dir):
    """Full-resolution JPEG stills: small print on a bench sheet is unreadable in the downscaled video."""
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for t in times:
        p = dst_dir / f"frame_{t:06.1f}.jpg"
        if not p.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(src), "-frames:v", "1",
                            "-q:v", "2", str(p)], check=True)
        out.append(p)
    return out


def read_sheet(images):
    """Transcribe the paper protocol lying on the bench (reagent recipe + volumes) from video stills."""
    content = [{"type": "input_text", "text": (
        "These are stills from a head-mounted camera at a lab bench. A printed or handwritten protocol may be "
        "lying on the bench. Transcribe the reaction recipe the scientist is following: each reagent and its "
        "volume per reaction in µL, in the order written. If several recipes are printed, pick the one that is "
        "highlighted, annotated, or matches the tubes on the bench, and say which in notes. Do not guess "
        "numbers you cannot read: use null.")}]
    for p in images:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"})
    r = client().responses.create(
        model=SPARK,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "sheet", "schema": SHEET_SCHEMA, "strict": True}},
    )
    return json.loads(r.output_text)


def extract_steps(file_id, extra=""):
    r = client().responses.create(
        model=SPARK,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": PROMPT + extra},
            {"type": "input_file", "file_id": file_id},
        ]}],
        text={"format": {"type": "json_schema", "name": "steps", "schema": STEP_SCHEMA, "strict": True}},
    )
    return json.loads(r.output_text)["steps"]


async def _track(file_id, phrase):
    c = AsyncOpenAI(base_url=BASE_URL, api_key=_key())
    stream = await c.responses.create(
        model="sam-3.1",
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": phrase},
            {"type": "input_video", "file_id": file_id},
        ]}],
        metadata={"include_confidence": "true"},
        stream=True,
    )
    parsed = parse_responses_stream(stream, video_segmentation_format())
    async for _ in parsed:  # drain; the parser accumulates records
        pass
    return await parsed.final_result()


def track(file_id, phrase):
    """{frame_index: [(object_id, left, top, right, bottom, confidence)]} for one noun phrase."""
    result = asyncio.run(_track(file_id, phrase))
    frames = {}
    for r in result.records:
        if r.kind == "box" and r.frame is not None:
            frames.setdefault(r.frame.frame_index, []).append(
                (r.object_id, r.left, r.top, r.right, r.bottom, r.confidence))
    return frames
