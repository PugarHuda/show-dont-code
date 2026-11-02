import { Composition } from "remotion";
import { Demo, FPS, totalFrames } from "./Demo";

export const Root = () => (
  <Composition id="Demo" component={Demo} durationInFrames={totalFrames()} fps={FPS} width={1920} height={1080} />
);
