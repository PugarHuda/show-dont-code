"""Meta Model API calls: Muse Spark (video -> steps) and SAM 3.1 (video -> tracked boxes)."""
import asyncio
import base64
import io
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from meta_sam_parser import (FramePrompt, PromptObject, PromptPoint, build_object_prompt, parse_responses_stream,
                             video_segmentation_format)
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


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return float(out.strip())


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
        max_output_tokens=16000,  # reasoning shares this budget; too low returns empty JSON
        text={"format": {"type": "json_schema", "name": "sheet", "schema": SHEET_SCHEMA, "strict": True}},
    )
    return json.loads(r.output_text)


def transcribe(video, wav_path, keywords=None):
    """Muse Voice Transcribe on the video's audio track. Returns [{start, end, text}] speech turns."""
    import requests

    wav_path = Path(wav_path)
    if not wav_path.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-sample_fmt", "s16", str(wav_path)], check=True)
    req = {"mode": "ENDPOINTING", "model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"}
    keywords = [k for k in keywords or [] if len(k) <= 30]  # API rejects keywords over 30 characters
    if keywords:
        req["keywords"] = keywords
    with open(wav_path, "rb") as audio:
        r = requests.post("https://api.meta.ai/v1/asr/transcribe", headers={"Authorization": f"Bearer {_key()}"},
                          files={"request": (None, json.dumps(req), "application/json"),
                                 "audio": (wav_path.name, audio)}, timeout=300)
    r.raise_for_status()
    return [{"start": (t.get("startMs") or 0) / 1000, "end": (t.get("endMs") or 0) / 1000,
             "text": t.get("transcript", "")} for t in r.json().get("turns", []) if t.get("transcript")]


def extract_steps(file_id, extra=""):
    r = client().responses.create(
        model=SPARK,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": PROMPT + extra},
            {"type": "input_file", "file_id": file_id},
        ]}],
        max_output_tokens=16000,
        text={"format": {"type": "json_schema", "name": "steps", "schema": STEP_SCHEMA, "strict": True}},
    )
    return json.loads(r.output_text)["steps"]


async def _track(file_id, prompt, metadata):
    c = AsyncOpenAI(base_url=BASE_URL, api_key=_key())
    stream = await c.responses.create(
        model="sam-3.1",
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_video", "file_id": file_id},
        ]}],
        metadata={"include_confidence": "true", **metadata},
        stream=True,
    )
    parsed = parse_responses_stream(stream, video_segmentation_format())
    async for _ in parsed:  # drain; the parser accumulates records
        pass
    return await parsed.final_result()


def track_point(file_id, frame, x, y, size):
    """Track the object under one clicked point, forward and backward from that frame. size=(w, h) of the video."""
    prompt = build_object_prompt(size=size, objects=[PromptObject(id=1, prompts=[
        FramePrompt(frame=int(frame), points=[PromptPoint(x=int(x), y=int(y))])])])
    return track(file_id, prompt, {"propagation_direction": "both"})


SOURCES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events", "groups"],
    "properties": {
        "events": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["step", "source_tube", "group"],
            "properties": {
                "step": {"type": "integer"},
                "source_tube": {"type": "string", "description": "type, cap colour, label, where it sits/is held"},
                "group": {"type": "integer", "description": "same number = same physical source tube"},
            }}},
        "groups": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["group", "reagent", "evidence"],
            "properties": {
                "group": {"type": "integer"},
                "reagent": {"type": "string", "description": "name from the sheet if it fits, else a description"},
                "evidence": {"type": "string"},
            }}},
    },
}


def identify_sources(video, steps, sheet, still_dir):
    """Stage 2: look closely at each transfer's aspiration moment, group transfers by physical source tube, name groups.

    Stage 1 (extract_steps) finds *when* transfers happen reliably but often mislabels *what* is transferred;
    deciding the source per event from full-resolution stills, then naming whole groups, is far more consistent.
    """
    idx = [i for i, s in enumerate(steps) if s["action"] == "transfer"]
    if not idx:
        return steps
    times = []
    for i in idx:  # aspiration happens early in a transfer step
        s, e = steps[i]["start"], steps[i]["end"]
        times += [round(s + 0.15 * (e - s), 1), round(s + 0.4 * (e - s), 1)]
    stills = frames(video, times, still_dir)
    names = [r["name"] for r in sheet.get("reagents", [])] if sheet.get("found") else []
    content = [{"type": "input_text", "text": (
        "First-person video of a scientist pipetting. Below are two stills from the start of each liquid transfer "
        "(when the pipette draws liquid from its SOURCE tube). For every transfer, describe the source tube the "
        "pipette tip is in or has just left: tube type, cap colour, label, where it sits or which hand holds it. "
        "Then give the same group number to transfers that use the same physical source tube; when the source "
        "changes, use a new group. Consecutive transfers usually share a source. Finally name each group's "
        "reagent." + (f" The bench protocol sheet suggests these reagents (may be misread or incomplete; water is "
                      f"often left off): {'; '.join(names)}." if names else "")
        + " Do not invent reagents or preparation steps you cannot see. A reagent name is at most 6 words; if "
          "you cannot identify it, name it like 'unknown, orange-cap tube'.")}]
    for k, i in enumerate(idx):
        content.append({"type": "input_text", "text": f"Transfer step {i} ({steps[i]['start']:.0f}-{steps[i]['end']:.0f} s):"})
        for p in stills[2 * k: 2 * k + 2]:
            content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(
                _small_jpeg(p)).decode()})
    r = client().responses.create(
        model=SPARK, input=[{"role": "user", "content": content}], max_output_tokens=16000,
        text={"format": {"type": "json_schema", "name": "sources", "schema": SOURCES_SCHEMA, "strict": True}},
    )
    if not r.output_text:
        raise RuntimeError(f"Muse Spark returned no text: status={r.status} {r.incomplete_details}")
    out = json.loads(r.output_text)
    names_by_group = {g["group"]: g["reagent"] for g in out["groups"]}
    steps = [dict(s) for s in steps]
    for ev in out["events"]:
        i = ev["step"]
        if i in idx and ev["group"] in names_by_group:  # model output is untrusted
            steps[i].update(reagent=names_by_group[ev["group"]], source=f"source tube {ev['group'] + 1}",
                            source_desc=ev["source_tube"][:160])
    return steps


def _jpeg_bytes(img, quality=85):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _small_jpeg(path, width=960):
    """Full-res stills are ~1 MB; 960 px keeps tube labels legible at a quarter of the tokens."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)))
    return _jpeg_bytes(img)


USES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_steps", "destination_steps", "reasoning"],
    "properties": {
        "source_steps": {"type": "array", "items": {"type": "integer"}, "description": "pipette draws liquid FROM it"},
        "destination_steps": {"type": "array", "items": {"type": "integer"}, "description": "pipette dispenses INTO it"},
        "reasoning": {"type": "string"},
    },
}


def sam_frames(sam_video, frames_dir):
    """Every frame of the SAM copy as a JPEG named by SAM's frame index (no video decoding needed at serve time)."""
    frames_dir = Path(frames_dir)
    if not frames_dir.exists():
        frames_dir.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(sam_video), "-q:v", "4", "-start_number", "0",
                        str(frames_dir / "%05d.jpg")], check=True)
    return frames_dir


def _jpeg_with_box(frames_dir, frame, box, label):
    from PIL import Image, ImageDraw

    path = Path(frames_dir) / f"{int(frame):05d}.jpg"
    if not path.exists():
        return None
    img = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(img)
    _, l, t, r, b, *_ = box
    draw.rectangle([l - 3, t - 3, r + 3, b + 3], outline=(255, 0, 0), width=2)
    draw.text((8, 8), label, fill=(255, 255, 255))
    return base64.b64encode(_jpeg_bytes(img)).decode()


def steps_using(frames_dir, track_frames, steps, fps, click_frame):
    """SAM says where the tube is; Muse Spark checks it is the same tube and says when it is used."""
    ref = track_frames.get(str(click_frame)) or track_frames.get(click_frame)
    content = [{"type": "input_text", "text": (
        "The first image shows a tube outlined in red, selected by the scientist. Each later image is a moment "
        "from one pipetting step of the same first-person video, with the same tube outlined in red where a "
        "tracker found it. The tracker can jump to a different object that looks similar (e.g. another cap of "
        "the same colour), so first compare each outlined object with the reference: same size, shape, cap and "
        "label? If it is a different object, ignore that step. Then list the steps where the pipette aspirates "
        "FROM the reference tube (source_steps) and where it dispenses INTO it (destination_steps). A tube held "
        "in the hand next to the pipette tip is usually in use. Ignore steps where it only sits in the background.")}]
    if ref:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{_jpeg_with_box(frames_dir, click_frame, ref[0], 'selected tube')}"})
    for i, s in enumerate(steps):
        if s["action"] != "transfer":
            continue
        lo, hi = int(s["start"] * fps), int(s["end"] * fps)
        seen = [f for f in range(lo, hi + 1) if track_frames.get(str(f)) or track_frames.get(f)]
        if not seen:
            continue
        early = [f for f in seen if f <= lo + (hi - lo) * 0.6] or seen  # aspiration happens early in a step
        f = early[len(early) // 2]
        img = _jpeg_with_box(frames_dir, f, (track_frames.get(str(f)) or track_frames.get(f))[0], f"step {i}")
        if img:
            content.append({"type": "input_text", "text": f"Step {i} ({s['start']:.0f}-{s['end']:.0f} s): {s['description']}"})
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{img}"})
    if len(content) <= 2:
        return {"source_steps": [], "destination_steps": [], "reasoning": "the tracker never saw this tube during a transfer step"}
    r = client().responses.create(
        model=SPARK, input=[{"role": "user", "content": content}], max_output_tokens=32000, reasoning={"effort": "low"},  # many images; keep reasoning short
        text={"format": {"type": "json_schema", "name": "uses", "schema": USES_SCHEMA, "strict": True}},
    )
    if not r.output_text:
        raise RuntimeError(f"Muse Spark returned no text: status={r.status} {r.incomplete_details}")
    out = json.loads(r.output_text)
    for k in ("source_steps", "destination_steps"):  # model output is untrusted
        out[k] = [i for i in out[k] if isinstance(i, int) and 0 <= i < len(steps) and steps[i]["action"] == "transfer"]
    return out


def track(file_id, prompt, metadata=None):
    """{frame_index: [(object_id, left, top, right, bottom, confidence)]} for a noun phrase or an object prompt."""
    result = asyncio.run(_track(file_id, prompt, metadata or {}))
    frames = {}
    for r in result.records:
        if r.kind == "box" and r.frame is not None:
            frames.setdefault(r.frame.frame_index, []).append(
                (r.object_id, r.left, r.top, r.right, r.bottom, r.confidence))
    return frames
