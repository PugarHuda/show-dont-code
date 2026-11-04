"""What the reviewer agent (Muse Spark Open) did on each run, from the agent_log saved in result.json: tool calls,
simulator tries and passes, findings by severity, one-click fixes, simulator-checked volumes. Nothing is re-run.

Usage: python scripts/agent_stats.py [runs dir, default demo/]
"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "demo")
print("| Run | Host | Saw video | Frames looked at | Simulator tries (passed) | Findings: block / check / info | "
      "One-click fixes | Simulator-checked volumes |")
print("|---|---|---|---|---|---|---|---|")
tot = {"look": 0, "sim": 0, "ok": 0, "fix": 0, "vol": 0, "runs": 0}
for f in sorted(root.glob("*/result.json")):
    r = json.loads(f.read_text(encoding="utf-8"))
    log, rv = r.get("agent_log") or [], r.get("review") or []
    if not (log or rv):
        continue
    looks = sum(e["tool"] == "look_at_moment" and "ok" in e.get("result", {}) for e in log)
    sims = [e for e in log if e["tool"] == "simulate_with_volumes"]
    ok = sum(bool((e.get("result") or {}).get("ran")) for e in sims)
    sev = {k: sum(i.get("severity") == k for i in rv) for k in ("block", "check", "info")}
    fixes = sum((i.get("fix") or {}).get("kind", "none") != "none" for i in rv)
    vols = sum(bool(v.get("sim_checked")) for v in r.get("suggested_volumes") or [])
    host = (r.get("review_model") or "").split(":")[0] or "–"  # runs from before the host was recorded
    print(f"| {f.parent.name} | {host} | {'yes' if r.get('review_saw_video') else 'no'} | {looks} | {len(sims)} ({ok}) | "
          f"{sev['block']} / {sev['check']} / {sev['info']} | {fixes} | {vols} |")
    for k, v in (("look", looks), ("sim", len(sims)), ("ok", ok), ("fix", fixes), ("vol", vols), ("runs", 1)):
        tot[k] += v
print(f"| **All {tot['runs']} runs** | | | {tot['look']} | {tot['sim']} ({tot['ok']}) | | {tot['fix']} | {tot['vol']} |")
