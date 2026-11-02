import { Composition } from "remotion";
import { Ad, adFrames } from "./Ad";
import { Demo, FPS, totalFrames } from "./Demo";

export const Root = () => (
  <>
    <Composition id="Demo" component={Demo} durationInFrames={totalFrames()} fps={FPS} width={1920} height={1080} />
    <Composition id="Ad" component={Ad} durationInFrames={adFrames()} fps={FPS} width={1080} height={1080} />
  </>
);
