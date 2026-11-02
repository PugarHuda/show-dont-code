import { loadFont } from "@remotion/google-fonts/SchibstedGrotesk";
import {
  AbsoluteFill, Audio, OffthreadVideo, Sequence, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig,
} from "remotion";
import scenesJson from "../public/scenes.json";
import clipsJson from "../public/clips.json";

const { fontFamily } = loadFont("normal", { weights: ["400", "700", "900"] });

export const FPS = 30;
const PAD = 0.7; // seconds of quiet after each line before the next scene
const RED = "#d4231b";

type Word = { t: number; d: number; w: string };
type Callout = { at: number; text: string };
type Line = { from: number; to: number; text: string };
type Scene = { id: string; kind: "card" | "rec"; say: string; words: Word[]; lines: Line[]; voice_s: number; callouts: Callout[] };
const scenes = scenesJson as Scene[];
const clips = clipsJson as Record<string, number>;

const sceneSeconds = (s: Scene) => s.voice_s + PAD;
export const totalFrames = () => Math.round(scenes.reduce((a, s) => a + sceneSeconds(s), 0) * FPS);

const Subtitles = ({ scene }: { scene: Scene }) => {
  const t = useCurrentFrame() / FPS;
  const cur = scene.lines.find((l) => t >= l.from - 0.05 && t < l.to);
  if (!cur) return null;
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "center", paddingBottom: 54 }}>
      <div style={{ background: "rgba(0,0,0,.82)", color: "#fff", fontFamily, fontWeight: 700, fontSize: 40, lineHeight: 1.25,
        padding: "12px 26px", borderRadius: 14, maxWidth: 1500, textAlign: "center" }}>{cur.text}</div>
    </AbsoluteFill>
  );
};

const Callouts = ({ scene }: { scene: Scene }) => {
  const frame = useCurrentFrame();
  const len = sceneSeconds(scene) * FPS;
  return (
    <AbsoluteFill style={{ alignItems: "flex-end", justifyContent: "flex-start", padding: "48px 56px", gap: 14, flexDirection: "column" }}>
      {scene.callouts.map((c, i) => {
        const start = Math.round(c.at * len);
        if (frame < start) return null;
        const s = spring({ frame: frame - start, fps: FPS, config: { damping: 18, stiffness: 140 } });
        return (
          <div key={i} style={{ transform: `translateX(${(1 - s) * 60}px)`, opacity: s, background: "#000", color: "#fff", fontFamily,
            fontWeight: 900, fontSize: 34, padding: "14px 26px 14px 20px", borderRadius: 999, display: "flex", alignItems: "center", gap: 14,
            boxShadow: "0 10px 30px rgba(0,0,0,.35)" }}>
            <span style={{ width: 18, height: 18, borderRadius: 9, background: RED, display: "inline-block" }} />{c.text}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

const Chip = ({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) => (
  <div style={{ position: "absolute", background: "rgba(0,0,0,.78)", color: "#fff", fontFamily, fontWeight: 700, fontSize: 24,
    padding: "8px 18px", borderRadius: 999, ...style }}>{children}</div>
);

const Recording = ({ scene, index }: { scene: Scene; index: number }) => {
  const clip = clips[scene.id];
  const want = sceneSeconds(scene);
  // long waits (the robot-server, Pyodide loading, a rebuild) are played faster, and the video says so
  const rate = Math.max(1, clip / want);
  const frame = useCurrentFrame();
  const fade = interpolate(frame, [0, 8], [0, 1], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ background: "#000", opacity: fade }}>
      <OffthreadVideo src={staticFile(`clips/${scene.id}.mp4`)} playbackRate={rate} muted />
      <Chip style={{ left: 40, top: 40 }}>{index} / {scenes.filter((s) => s.kind === "rec").length} · live at show-dont-code.vercel.app</Chip>
      {rate > 1.25 && <Chip style={{ left: 40, top: 96, background: RED }}>sped up ×{rate.toFixed(1)}</Chip>}
      {scene.id === "sync" && <Chip style={{ left: 40, bottom: 140, fontSize: 20 }}>Video: BioVL2 (Nishimura et al., Kyoto University), CC BY 4.0</Chip>}
      <Callouts scene={scene} />
      <Subtitles scene={scene} />
    </AbsoluteFill>
  );
};

// the Flex deck drawn in white line, as on the landing page; the slot in use fills in white
const Deck = ({ lit }: { lit: number }) => {
  const slots = [[20, 16, 176, 180, "THERMOCYCLER"], [212, 16, 176, 86, "A2"], [404, 16, 176, 86, "TRASH"], [212, 110, 176, 86, "TIPS"],
    [404, 110, 176, 86, "TIPS"], [20, 204, 176, 86, "TIPS"], [212, 204, 176, 86, "TUBE RACK"], [404, 204, 176, 86, "C3"],
    [20, 298, 176, 86, "TIPS"], [212, 298, 176, 86, "D2"], [404, 298, 176, 86, "D3"]] as const;
  return (
    <svg viewBox="0 0 600 404" style={{ width: 760 }}>
      {slots.map(([x, y, w, h, label], i) => {
        const on = i === 6 && lit > 0.5, tc = i === 0 && lit > 0.5;
        const draw = Math.min(1, Math.max(0, lit * 3 - i * 0.18));
        return (
          <g key={i} opacity={draw}>
            <rect x={x} y={y} width={w} height={h} rx={10} fill={on ? "#fff" : "none"} stroke={tc ? RED : "#fff"} strokeWidth={tc ? 4 : 1.6}
              strokeDasharray={["A2", "C3", "D2", "D3"].includes(label) ? "4 5" : undefined} />
            <text x={x + w / 2} y={y + h / 2 + 4} textAnchor="middle" fill={on ? "#000" : "#fff"} style={{ fontFamily, fontWeight: 700, fontSize: 11, letterSpacing: ".08em" }}>{label}</text>
          </g>
        );
      })}
    </svg>
  );
};

const Card = ({ scene }: { scene: Scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const lit = interpolate(frame, [0, 1.6 * fps], [0, 1], { extrapolateRight: "clamp" });
  const rise = spring({ frame: frame - 0.6 * fps, fps, config: { damping: 20 } });
  const outro = scene.id === "outro";
  return (
    <AbsoluteFill style={{ background: "#000", alignItems: "center", justifyContent: "flex-start", fontFamily, color: "#fff" }}>
      <div style={{ marginTop: 70 }}><Deck lit={lit} /></div>
      <div style={{ position: "absolute", bottom: 0, left: 260, right: 260, height: 470 * rise, background: "#fff", borderRadius: "48px 48px 0 0",
        color: "#000", textAlign: "center", paddingTop: 48 }}>
        <div style={{ fontWeight: 900, fontSize: 108, lineHeight: 0.95, letterSpacing: "-0.035em" }}>
          {outro ? "Pipette it once." : "Show, Don't Code"}</div>
        <div style={{ fontWeight: 700, fontSize: 40, marginTop: 22 }}>
          {outro ? "Get the robot protocol." : "Film a lab protocol once. Get the robot protocol."}</div>
        {outro && <div style={{ marginTop: 34, display: "inline-block", background: RED, color: "#fff", borderRadius: 999, padding: "16px 44px",
          fontWeight: 700, fontSize: 34 }}>show-dont-code.vercel.app</div>}
        {outro && <div style={{ marginTop: 18, fontSize: 24, color: "#5b5b5b" }}>github.com/PugarHuda/show-dont-code · doi:10.5281/zenodo.23876762</div>}
        {!outro && <div style={{ marginTop: 26, fontSize: 26, color: "#5b5b5b" }}>Meta Global AI Developer Hackathon 2026</div>}
      </div>
      <Subtitles scene={scene} />
    </AbsoluteFill>
  );
};

export const Demo = () => {
  let from = 0, n = 0;
  return (
    <AbsoluteFill style={{ background: "#000" }}>
      {scenes.map((s) => {
        const frames = Math.round(sceneSeconds(s) * FPS);
        const seq = (
          <Sequence key={s.id} from={from} durationInFrames={frames} name={s.id}>
            {s.kind === "card" ? <Card scene={s} /> : <Recording scene={s} index={++n} />}
            <Audio src={staticFile(`voice/${s.id}.mp3`)} />
          </Sequence>
        );
        from += frames;
        return seq;
      })}
    </AbsoluteFill>
  );
};
