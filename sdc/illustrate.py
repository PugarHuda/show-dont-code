"""Illustrated bench card: each robot step drawn by Muse Image from the video frame where it happens, then checked
against that frame by Muse Spark. The image model only redraws the scientist's own bench (an edit of the real frame,
never a scene from nothing) and is told to write no text; every number and name on the card comes from the protocol.

  python -m sdc.illustrate pcr_1-gt     (writes out/<run>/card/step_<i>.jpg and card.json)
"""
import base64
import hashlib
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from . import meta

IMAGE = "muse-image-1.0"
PROMPT = ("Redraw this exact first-person lab photo as a clean instructional illustration for a lab training manual: flat "
          "vector style, soft neutral colours, clear outlines, white background around the bench. Keep every object "
          "exactly where it is: the same hands, gloves, pipette, tubes, racks and their colours and count. Do not add, "
          "remove or move any object. Do not add any text, letters, numbers, labels or arrows.")
CHECK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["faithful", "differences"],
    "properties": {
        "faithful": {"type": "boolean", "description": "same objects, same places, same action; no added text"},
        "differences": {"type": "array", "items": {"type": "string"}, "description": "each object added, removed, moved "
                        "or changed, and any text or labels drawn in; empty when faithful"},
    },
}


def moment(step):
    """When to take the frame: a transfer while the tip is in the source tube (as identify_sources), else mid-step."""
    s, e = float(step["start"]), float(step["end"])
    return round(s + (0.4 if step.get("action") == "transfer" else 0.5) * (e - s), 1)


def _data_url(jpeg):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def draw(frame_jpeg):
    """Muse Image edit of the real frame -> PNG bytes."""
    r = requests.post(f"{meta.BASE_URL}/images/edits", headers={"Authorization": f"Bearer {meta._key()}"}, timeout=300,
                      json={"model": IMAGE, "prompt": PROMPT, "images": [{"image_url": _data_url(frame_jpeg)}],
                            "output_format": "png", "size": "1920x1080"})
    if not r.ok:
        raise RuntimeError(f"Muse Image refused: {r.status_code} {r.text[:200]}")
    return base64.b64decode(r.json()["data"][0]["b64_json"])


def check(frame_jpeg, drawing_jpeg):
    """Muse Spark compares the drawing with the frame it came from."""
    r = meta.client().responses.create(
        model=meta.SPARK, max_output_tokens=16000,  # reasoning shares this budget
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "Image 1 is a photo from a lab video. Image 2 should be the same scene redrawn "
             "as an illustration. Compare them object by object: hands, gloves, pipettes, tubes, tube racks, cap colours, "
             "and what the hands are doing. It is faithful only if nothing was added, removed or moved, the action is "
             "the same, and no text, numbers, labels or arrows were drawn in. Style changes are fine."},
            {"type": "input_image", "image_url": _data_url(frame_jpeg)},
            {"type": "input_image", "image_url": _data_url(drawing_jpeg)}]}],
        text={"format": {"type": "json_schema", "name": "check", "schema": CHECK_SCHEMA, "strict": True}})
    if not r.output_text:
        raise RuntimeError(f"Muse Spark returned no text: status={r.status}")
    return json.loads(r.output_text)


def _jpeg(png, width=1280):
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(png)).convert("RGB")
    img = img.resize((width, round(img.height * width / img.width)))
    return meta._jpeg_bytes(img, quality=86)


def filmed(steps):
    """(index, step) of every step that happens on camera; a step the scientist marked as never done is left out."""
    return [(i, s) for i, s in enumerate(steps)
            if s.get("start") is not None and s.get("end") is not None and s.get("action") != "skipped"]


def illustrate_step(video, run_dir, out_dir, i, step, tries=2):
    t = moment(step)
    frame = meta._small_jpeg(meta.frames(video, [t], Path(run_dir) / "stills_card")[0])
    rejected = []  # what Muse Spark found wrong with drawings it sent back
    for attempt in range(1, tries + 1):
        drawing = _jpeg(draw(frame))
        verdict = check(frame, drawing)
        if verdict["faithful"] or attempt == tries:
            break
        rejected.append([str(x)[:200] for x in verdict["differences"]][:8])
    (out_dir / f"step_{i}.jpg").write_bytes(drawing)
    return {"step": i, "t": t, "image": f"step_{i}.jpg", "frame_sha256": hashlib.sha256(frame).hexdigest(),
            "image_sha256": hashlib.sha256(drawing).hexdigest(), "attempts": attempt,
            "faithful": verdict["faithful"], "differences": [str(x)[:200] for x in verdict["differences"]][:8], "rejected": rejected}


def bench_card(run_dir, video=None):
    """Draw every filmed step (in parallel: ~30 s per image) and write card/ and card.json. All or nothing: drawings
    go to card.tmp/, which becomes card/ only when every step is drawn, and card.json (what marks a card as ready)
    is written last."""
    run_dir = Path(run_dir)
    video = video or run_dir / "video.mp4"
    steps = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["steps"]
    todo = filmed(steps)
    if not todo or not Path(video).exists():
        raise ValueError("no filmed steps to illustrate")
    tmp = run_dir / "card.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        with ThreadPoolExecutor(6) as pool:
            cards = list(pool.map(lambda a: illustrate_step(video, run_dir, tmp, *a), todo))
        shutil.rmtree(run_dir / "card", ignore_errors=True)
        tmp.rename(run_dir / "card")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    out = {"image_model": IMAGE, "check_model": meta.SPARK, "prompt": PROMPT,
           "frame": "frame_sha256 is of the 960-px JPEG of the video frame at t that both models were shown",
           "steps": cards}
    (run_dir / "card.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    from . import pipeline

    for name in sys.argv[1:]:
        c = bench_card(pipeline.OUT / name)
        print(name, [(s["step"], s["faithful"], s["attempts"]) for s in c["steps"]])
