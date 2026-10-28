"""Failover drill on Modal: same image, secrets and volume as production, Baseten key deliberately wrong.

The reviewer must move to Fireworks by itself. Reads a shared run, writes nothing. Run: modal run scripts/failover_drill.py
"""
import modal

app = modal.App("sdc-failover-drill")
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install_from_requirements("requirements.txt")
    .env({"OT_API_CONFIG_DIR": "/tmp/opentrons", "PYTHONIOENCODING": "utf-8"})
    .add_local_python_source("sdc")
)


@app.function(image=image, timeout=1800, volumes={"/runs": modal.Volume.from_name("sdc-runs")},
              secrets=[modal.Secret.from_name("show-dont-code"), modal.Secret.from_name("show-dont-code-fireworks")])
def drill(run="pcr_3"):
    import json, os, shutil, time
    from pathlib import Path
    os.environ["BASETEN_API_KEY"] = "invalid-key-failover-drill"   # Baseten will answer 401
    from sdc import agent, pipeline
    d = Path("/tmp") / run
    shutil.copytree(f"/runs/{run}", d)                               # read-only use of the shared run
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    sheet = json.loads((d / "sheet.json").read_text(encoding="utf-8")) if (d / "sheet.json").exists() else {}
    video = next((d / n for n in ("review.mp4", "video.mp4") if (d / n).exists()), None)
    t = time.time()
    out = agent.review(r["steps"], sheet, "passed", video,
                       simulate=lambda v: pipeline.simulate_with(r["steps"], v),
                       frames_dir=d / "sam_frames" if (d / "sam_frames").exists() else None, fps=pipeline.SAM_FPS)
    return {"seconds": round(time.time() - t), "model": out["model"], "host_failures": out["host_failures"],
            "saw_video": out["saw_video"], "findings": len(out["issues"]),
            "tools": [c["tool"] for c in out["tool_log"]]}


@app.local_entrypoint()
def main():
    print("DRILL", drill.remote())
