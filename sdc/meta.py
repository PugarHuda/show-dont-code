"""Meta Model API calls: Muse Spark (video -> steps) and SAM 3.1 (video -> tracked boxes)."""
import asyncio
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
- Name reagents exactly as written on tube labels, the paper protocol on the bench, or as spoken aloud.
- Use the same destination name for every step that targets the same tube.
- Only give volume_ul if you can see it on the pipette dial, read it on the protocol sheet, or hear it; else null.
- Steps a liquid handler cannot do (centrifuge, vortex, invert, placing tubes in a machine) still get listed.
- Placing tubes into a PCR machine / thermal cycler / "DNA engine" is action "thermocycle".
- Ignore idle time, adjusting gloves, and looking around."""


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
