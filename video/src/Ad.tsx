// 1080x1080 cut for X: a hook in the first seconds, one big claim per beat over a cropped, readable part of the live
// recording, and a call to action. Narration and timings come from ad.json via `python make_voice.py ad`.
import {
  AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile, useCurrentFrame,
} from "remotion";
import adJson from "../public/ad_scenes.json";
import { Deck, FPS, RED, fontFamily } from "./Demo";

type Line = { from: number; to: number; text: string };
type Beat = { id: string; kind: "rec" | "card" | "stat" | "cta"; head: string; say: string; voice_s: number; lines: Line[];
  clip?: string; from?: number; crop?: [number, number, number, number]; credit?: boolean };
const beats = adJson as Beat[];
const PAD = 0.35;
const beatFrames = (b: Beat) => Math.round((b.voice_s + PAD) * FPS);
export const adFrames = () => beats.reduce((a, b) => a + beatFrames(b), 0);

const Caption = ({ beat }: { beat: Beat }) => {
  const t = useCurrentFrame() / FPS;
  const cur = beat.lines.find((l) => t >= l.from - 0.05 && t < l.to);
  if (!cur) return null;
  return (
    <div style={{ position: "absolute", left: 40, right: 40, bottom: 44, display: "flex", justifyContent: "center" }}>
      <div style={{ background: "#fff", color: "#000", fontFamily, fontWeight: 700, fontSize: 36, lineHeight: 1.2,
        padding: "10px 22px", borderRadius: 14, textAlign: "center" }}>{cur.text}</div>
    </div>
  );
};

const Head = ({ text, top = 70, size = 76 }: { text: string; top?: number; size?: number }) => {
  const frame = useCurrentFrame();
  const s = spring({ frame, fps: FPS, config: { damping: 16, stiffness: 160 } });
  return (
    <div style={{ position: "absolute", left: 50, right: 50, top, color: "#fff", fontFamily, fontWeight: 900, fontSize: size,
      lineHeight: 1.0, letterSpacing: "-0.03em", whiteSpace: "pre-line", transform: `translateY(${(1 - s) * 30}px)`, opacity: s }}>{text}</div>
  );
};

const Rec = ({ beat }: { beat: Beat }) => {
  const frame = useCurrentFrame();
  const [x, y, w, h] = beat.crop!;
  const boxW = 980, boxH = Math.min(600, Math.round((boxW * h) / w));
  const zoom = interpolate(frame, [0, beatFrames(beat)], [1, 1.06]);  // slow push-in keeps a static UI alive
  const k = (boxW / w) * zoom;
  return (
    <AbsoluteFill style={{ background: "#000" }}>
      <Head text={beat.head} />
      <div style={{ position: "absolute", left: 50, top: 290, width: boxW, height: boxH, borderRadius: 22, overflow: "hidden",
        border: `3px solid ${RED}` }}>
        <OffthreadVideo src={staticFile(`clips/${beat.clip}.mp4`)} startFrom={Math.round((beat.from ?? 0) * FPS)} muted
          style={{ position: "absolute", width: 1920 * k, height: 1080 * k, left: -x * k - (boxW * (zoom - 1)) / 2,
            top: -y * k - (boxH * (zoom - 1)) / 2, maxWidth: "none" }} />
      </div>
      {beat.credit && <div style={{ position: "absolute", left: 52, top: 296 + boxH, color: "#8a8a8a", fontFamily, fontSize: 18 }}>
        Lab video: BioVL2 (Kyoto University), CC BY 4.0</div>}
      <Caption beat={beat} />
    </AbsoluteFill>
  );
};

// the real frame, wiped left to right into Muse Image's drawing of it
const Card = ({ beat }: { beat: Beat }) => {
  const frame = useCurrentFrame();
  const wipe = interpolate(frame, [0.6 * FPS, 2.4 * FPS], [0, 100], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const img = { position: "absolute" as const, inset: 0, width: "100%", height: "100%", objectFit: "cover" as const };
  return (
    <AbsoluteFill style={{ background: "#000" }}>
      <Head text={beat.head} />
      <div style={{ position: "absolute", left: 50, top: 290, width: 980, height: 551, borderRadius: 22, overflow: "hidden",
        border: `3px solid ${RED}`, background: "#fff" }}>
        <Img src={staticFile("card_frame.jpg")} style={img} />
        <Img src={staticFile("card_draw.jpg")} style={{ ...img, clipPath: `inset(0 ${100 - wipe}% 0 0)` }} />
        <div style={{ position: "absolute", top: 0, bottom: 0, left: `${wipe}%`, width: 4, background: RED, opacity: wipe > 0 && wipe < 100 ? 1 : 0 }} />
      </div>
      <div style={{ position: "absolute", left: 52, top: 852, color: "#8a8a8a", fontFamily, fontSize: 18 }}>
        Step 2 · 1.5 µL Primer1 · drawn from the frame at 1:18, checked by Muse Spark · BioVL2, CC BY 4.0</div>
      <Caption beat={beat} />
    </AbsoluteFill>
  );
};

const Stat = ({ beat }: { beat: Beat }) => {
  const frame = useCurrentFrame();
  const n = Math.round(interpolate(frame, [0, 0.9 * FPS], [0, 79], { extrapolateRight: "clamp" }));
  return (
    <AbsoluteFill style={{ background: "#000", color: "#fff", fontFamily, alignItems: "center", justifyContent: "center" }}>
      <div style={{ fontWeight: 900, fontSize: 330, lineHeight: 0.9, letterSpacing: "-0.05em", color: RED, marginTop: -120 }}>{n}%</div>
      <div style={{ fontWeight: 700, fontSize: 50, textAlign: "center", marginTop: 20, lineHeight: 1.1 }}>
        of PCR steps found,<br />on videos it never saw</div>
      <div style={{ fontSize: 24, color: "#8a8a8a", marginTop: 22 }}>held-out BioVL2 videos, n = 4 · precision 0.69</div>
      <Caption beat={beat} />
    </AbsoluteFill>
  );
};

const Cta = ({ beat }: { beat: Beat }) => {
  const frame = useCurrentFrame();
  const lit = interpolate(frame, [0, 1.2 * FPS], [0, 1], { extrapolateRight: "clamp" });
  const rise = spring({ frame: frame - 0.4 * FPS, fps: FPS, config: { damping: 20 } });
  return (
    <AbsoluteFill style={{ background: "#000", alignItems: "center", fontFamily }}>
      <div style={{ marginTop: 30, transform: "scale(.82)", transformOrigin: "top center" }}><Deck lit={lit} /></div>
      <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, height: 600 * rise, background: "#fff",
        borderRadius: "48px 48px 0 0", textAlign: "center", paddingTop: 44 }}>
        <div style={{ fontWeight: 900, fontSize: 104, lineHeight: 0.95, letterSpacing: "-0.035em" }}>Pipette it once.</div>
        <div style={{ fontWeight: 700, fontSize: 44, marginTop: 16 }}>Get the robot protocol.</div>
        <div style={{ marginTop: 34, display: "inline-block", background: RED, color: "#fff", borderRadius: 999,
          padding: "18px 48px", fontWeight: 700, fontSize: 40 }}>show-dont-code.vercel.app</div>
        <div style={{ marginTop: 22, fontSize: 24, color: "#5b5b5b" }}>
          Open source · Muse Spark · SAM 3.1 · Muse Image · Muse Spark Open</div>
      </div>
    </AbsoluteFill>
  );
};

export const Ad = () => {
  const frame = useCurrentFrame();
  const total = adFrames();
  let from = 0;
  return (
    <AbsoluteFill style={{ background: "#000" }}>
      {beats.map((b) => {
        const n = beatFrames(b);
        const seq = (
          <Sequence key={b.id} from={from} durationInFrames={n} name={b.id}>
            {b.kind === "rec" ? <Rec beat={b} /> : b.kind === "card" ? <Card beat={b} /> : b.kind === "stat" ? <Stat beat={b} /> : <Cta beat={b} />}
            <Audio src={staticFile(`voice/ad_${b.id}.mp3`)} />
          </Sequence>
        );
        from += n;
        return seq;
      })}
      {/* brand and progress, on every beat */}
      <div style={{ position: "absolute", left: 50, top: 22, color: "#fff", fontFamily, fontWeight: 900, fontSize: 26 }}>
        Show, Don't Code</div>
      <div style={{ position: "absolute", left: 0, top: 0, height: 8, width: `${(frame / total) * 100}%`, background: RED }} />
    </AbsoluteFill>
  );
};
