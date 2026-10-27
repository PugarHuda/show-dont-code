"""Protocol reviewer agent on Muse Spark Open (Meta open weights, served by Baseten).

It watches the video, reads what the vision models extracted and what the bench sheet says, and can run the
Opentrons simulator itself through a tool, trying volume plans until one adds up. It never edits steps or runs a
robot: every finding becomes a question and every volume it works out is a pre-filled suggestion the scientist
confirms.
"""
import base64
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
MODEL = os.getenv("OPEN_MODEL", "meta-models/muse-spark-open")
MAX_VIDEO_S = 300  # Baseten Muse Spark Open limit per video (it samples at most 96 frames)
MAX_TOOL_TURNS = 5

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issues", "suggested_volumes"],
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["steps", "severity", "finding", "question"],
                "properties": {
                    "steps": {"type": "array", "items": {"type": "integer"}, "description": "0-based step indices"},
                    "severity": {"type": "string", "enum": ["block", "check", "info"]},
                    "finding": {"type": "string"},
                    "question": {"type": "string", "description": "one question the scientist can answer quickly"},
                },
            },
        },
        "suggested_volumes": {
            "type": "array",
            "description": "per-transfer volumes you verified with the simulator; empty if you could not",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["reagent", "volume_ul", "why"],
                "properties": {"reagent": {"type": "string"}, "volume_ul": {"type": "number"}, "why": {"type": "string"}},
            },
        },
    },
}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "simulate_with_volumes",
        "description": ("Fill in per-transfer volumes for reagents that have none, generate the Opentrons Flex "
                        "protocol and run it in the official simulator. Returns whether it ran, the error if not, "
                        "and the total volume each reaction tube ends up with."),
        "parameters": {
            "type": "object",
            "properties": {"volumes_ul": {
                "type": "object", "additionalProperties": {"type": "number"},
                "description": "µL per single transfer, keyed by reagent name exactly as written in the steps"}},
            "required": ["volumes_ul"],
        },
    },
}]

SYSTEM = """You review liquid-handling protocols that were extracted automatically from a video of a scientist
working by hand, before they run on an Opentrons robot. Find problems a careful lab member would catch:
- a reagent listed on the bench sheet that never appears in the steps (or the reverse)
- the same reagent drawn from different source tubes, or different reagents from the same tube
- reaction tubes that do not all receive every reagent, or receive one twice
- volumes that are missing, implausible for the pipette, or do not add up to the sheet's total
- a step order that differs from the sheet in a way that matters chemically (e.g. enzyme before water)
- simulator errors, explained in plain words
When the video is attached, it is the ground truth: check the steps against it (step timestamps are seconds
from the start). Flag a step you cannot see happen, an action you see that is missing from the steps, and
a wrong tube or reagent. Say what you saw and at what time.

If volumes are missing, work out a plan: use the sheet's per-reaction total and standard recipes for the kit
you see, call simulate_with_volumes, read the per-tube totals, and adjust until the simulator passes and every
tube reaches the sheet's total. Only suggest volumes you verified this way, and then list EVERY reagent whose
volume is missing in suggested_volumes, not just the ones you changed. Your suggestions are shown to the
scientist as editable defaults; they decide.
Only report real problems visible in the data or video. severity "block" = robot must not run until answered."""


def client():
    key = os.getenv("BASETEN_API_KEY")
    if not key:
        raise RuntimeError("BASETEN_API_KEY missing: put it in .env")
    return OpenAI(base_url="https://inference.baseten.co/v1", api_key=key)


def review(steps, sheet, simulator, video=None, simulate=None):
    """simulator: status string. simulate: callable(volumes_ul) -> dict, exposed to the model as a tool.

    Returns {"issues", "suggested_volumes", "tool_log"}.
    """
    brief = [{k: s.get(k) for k in ("start", "end", "action", "reagent", "source", "destination", "volume_ul",
                                    "volume_evidence")} | {"i": i} for i, s in enumerate(steps)]
    content = [{"type": "text", "text": json.dumps({"steps": brief, "bench_sheet": sheet, "simulator": simulator},
                                                    ensure_ascii=False)}]
    if video:
        b64 = base64.b64encode(Path(video).read_bytes()).decode()
        content.append({"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}})
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
    log = []
    for _ in range(MAX_TOOL_TURNS if simulate else 0):
        m = client().chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, max_tokens=16000).choices[0].message
        if not m.tool_calls:
            break
        messages.append({"role": "assistant", "content": m.content or "",
                         "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                out = simulate(args.get("volumes_ul") or {}) if tc.function.name == "simulate_with_volumes" \
                    else {"error": f"unknown tool {tc.function.name}"}
            except (ValueError, TypeError) as e:  # malformed arguments from the model
                args, out = {}, {"error": f"bad arguments: {e}"}
            log.append({"tool": tc.function.name, "args": args, "result": out})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out)})
    messages.append({"role": "user", "content": "Give your final review now."})
    r = client().chat.completions.create(
        model=MODEL, messages=messages, max_tokens=16000,
        response_format={"type": "json_schema", "json_schema": {"name": "review", "schema": REVIEW_SCHEMA, "strict": True}},
    )
    choice = r.choices[0]
    if not choice.message.content:
        raise RuntimeError(f"reviewer returned no content (finish_reason={choice.finish_reason})")
    out = json.loads(choice.message.content)
    # model output is untrusted: keep only indices and reagents that exist, volumes a pipette can do
    for it in out["issues"]:
        it["steps"] = [i for i in it["steps"] if isinstance(i, int) and 0 <= i < len(steps)]
    reagents = {s.get("reagent") for s in steps}
    out["suggested_volumes"] = [v for v in out["suggested_volumes"]
                                if v["reagent"] in reagents and 0 < v["volume_ul"] <= 1000]
    # an incomplete final answer falls back to the last plan the simulator actually accepted
    missing = {s.get("reagent") for s in steps if s["action"] == "transfer" and not s.get("volume_ul")}
    passed = [c for c in log if c["result"].get("ran")]
    if passed and not missing <= {v["reagent"] for v in out["suggested_volumes"]}:
        plan = passed[-1]["args"]["volumes_ul"]
        out["suggested_volumes"] = [{"reagent": k, "volume_ul": float(v), "why": "last simulator-verified plan"}
                                    for k, v in plan.items() if k in reagents and 0 < v <= 1000]
    out["tool_log"] = log
    return out
