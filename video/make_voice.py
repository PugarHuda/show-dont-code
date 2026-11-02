"""Narration for the demo video: one AI voice clip per scene (Microsoft Edge neural TTS) with word timings, which
the Remotion composition turns into subtitles. Writes public/voice/<id>.mp3 and public/scenes.json.

  python video/make_voice.py        (the demo: script.json -> public/scenes.json)
  python video/make_voice.py ad     (the X ad: ad.json -> public/ad_scenes.json, voice/ad_<id>.mp3)
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import edge_tts

HERE = Path(__file__).parent
VOICE, RATE = "en-US-AndrewMultilingualNeural", "+4%"


async def speak(text, mp3, tries=4):
    """The free Edge voice service sometimes answers with no audio; try again a few times before giving up."""
    for k in range(tries):
        try:
            return await _speak(text, mp3)
        except edge_tts.exceptions.NoAudioReceived:
            if k == tries - 1:
                raise
            await asyncio.sleep(2 + 3 * k)


async def _speak(text, mp3):
    words = []
    with open(mp3, "wb") as f:
        async for ch in edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary").stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append({"t": round(ch["offset"] / 1e7, 3), "d": round(ch["duration"] / 1e7, 3), "w": ch["text"]})
    return words


def subtitle_lines(text, words, end):
    """Subtitle lines from the script itself (it has the punctuation; TTS word events don't), timed by the TTS words:
    break after a comma or full stop, or every 8 words; a line starts when its first word is spoken."""
    toks, chunks, cur = text.split(), [], []
    for tok in toks:
        cur.append(tok)
        if len(cur) >= 8 or tok[-1] in ".,:;?!":
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    merged, carry = [], []
    for ch in chunks:  # no one- or two-word orphans: "end," joins the line before, "Show," the line after
        ch = carry + ch
        carry = []
        if merged and len(ch) <= 2 and len(merged[-1]) + len(ch) <= 11 and merged[-1][-1][-1] not in ".?!":
            merged[-1] = merged[-1] + ch
        elif len(ch) <= 2 and ch[-1][-1] not in ".?!" and ch is not chunks[-1]:
            carry = ch
        else:
            merged.append(ch)
    chunks = merged + ([carry] if carry else [])
    k, out = 0, []
    for ch in chunks:  # token index -> TTS word index, scaled when the counts differ ("SAM 3.1", "Don't")
        i = min(len(words) - 1, round(k * len(words) / len(toks)))
        out.append({"from": words[i]["t"], "text": " ".join(ch)})
        k += len(ch)
    for a, b in zip(out, out[1:] + [{"from": end}]):
        a["to"] = b["from"]
    return out


def seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout)


async def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    pre = f"{name}_" if name else ""
    scenes = json.loads((HERE / f"{name or 'script'}.json").read_text(encoding="utf-8"))
    (HERE / "public" / "voice").mkdir(parents=True, exist_ok=True)
    for s in scenes:
        mp3 = HERE / "public" / "voice" / f"{pre}{s['id']}.mp3"
        s["words"] = await speak(s["say"], mp3)
        s["voice_s"] = round(seconds(mp3), 2)
        s["lines"] = subtitle_lines(s["say"], s["words"], s["voice_s"])
        print(f"{s['id']:10} {s['voice_s']:5.1f} s  {len(s['words'])} words")
    (HERE / "public" / f"{pre}scenes.json").write_text(json.dumps(scenes, indent=1), encoding="utf-8")
    print(f"total {sum(s['voice_s'] for s in scenes):.1f} s of narration")


asyncio.run(main())
