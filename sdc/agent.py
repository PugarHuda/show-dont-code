"""Protocol reviewer agent on Muse Spark Open (Meta open weights, served by Baseten).

It reads what the vision models extracted, what the bench sheet says, and what the simulator did, and flags
anything a scientist should check before a robot touches real reagents. It never edits steps itself; every
finding becomes a question in the UI.
"""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
MODEL = os.getenv("OPEN_MODEL", "meta-models/muse-spark-open")

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issues"],
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
        }
    },
}

SYSTEM = """You review liquid-handling protocols that were extracted automatically from a video of a scientist
working by hand, before they run on an Opentrons robot. Find problems a careful lab member would catch:
- a reagent listed on the bench sheet that never appears in the steps (or the reverse)
- the same reagent drawn from different source tubes, or different reagents from the same tube
- reaction tubes that do not all receive every reagent, or receive one twice
- volumes that are missing, implausible for the pipette, or do not add up to the sheet's total
- a step order that differs from the sheet in a way that matters chemically (e.g. enzyme before water)
- simulator errors, explained in plain words
Only report real problems visible in the data. severity "block" = robot must not run until answered.
Answer with JSON only."""


def client():
    key = os.getenv("BASETEN_API_KEY")
    if not key:
        raise RuntimeError("BASETEN_API_KEY missing: put it in .env")
    return OpenAI(base_url="https://inference.baseten.co/v1", api_key=key)


def review(steps, sheet, simulator):
    """simulator: 'passed', 'failed: <error>', or 'not run yet: waiting for scientist answers'."""
    brief = [{k: s.get(k) for k in ("start", "end", "action", "reagent", "source", "destination", "volume_ul",
                                    "volume_evidence")} | {"i": i} for i, s in enumerate(steps)]
    user = json.dumps({"steps": brief, "bench_sheet": sheet, "simulator": simulator}, ensure_ascii=False)
    r = client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": "review", "schema": REVIEW_SCHEMA, "strict": True}},
        max_tokens=16000,
    )
    choice = r.choices[0]
    if not choice.message.content:
        raise RuntimeError(f"reviewer returned no content (finish_reason={choice.finish_reason})")
    issues = json.loads(choice.message.content)["issues"]
    # model output is untrusted: keep only indices that exist
    for it in issues:
        it["steps"] = [i for i in it["steps"] if isinstance(i, int) and 0 <= i < len(steps)]
    return issues
