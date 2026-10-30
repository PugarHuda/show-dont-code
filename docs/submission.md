# Submission kit (draft): Show, Don't Code

Numbers below come from `README.md` → Evaluation. Re-check them before submitting if the pipeline changes.

## Form fields

**Team name:** Show, Don't Code
**Track:** Indie Developer
**Problem statement:** Push the Frontier

**Project description**
> Thousands of labs own liquid-handling robots that sit idle, because programming them means writing Python and
> most scientists don't. Show, Don't Code turns one first-person video of a scientist doing a protocol by hand into
> an Opentrons Flex protocol that passes the official simulator. Muse Spark watches the video and finds every step;
> a second pass looks closely at each aspiration moment to tell which tube the liquid came from. SAM 3.1 tracks
> tubes the scientist clicks, and Muse Spark rejects the tracker's jumps. Muse Voice Transcribe turns narration into
> evidence. A reviewer agent on Muse Spark Open watches the same video, runs the simulator as a tool to work out a
> volume plan that hits the bench sheet's total, and asks the scientist about anything it can't verify. Code
> generation is deterministic, so model output never becomes executable code. On held-out videos from the public
> BioVL2 dataset, step recall is 0.79 and precision 0.69 on PCR (n=4 held-out videos), and 0.28 / 0.42 on a
> protocol it was never developed on (plasmid miniprep).

**Public GitHub repository:** https://github.com/PugarHuda/show-dont-code (make public before submitting)
**3-minute demo video:** (YouTube link)
**Live demo:** https://show-dont-code.vercel.app (landing) and https://show-dont-code.vercel.app/app (seven sample runs).
Uploading your own video needs the access code; put it in the description field of the form, never in this
repo (the code lives in `out/upload_code.txt`, which is gitignored).

**Features to name in the form:** upload a video and watch each stage run (Modal backend); click-to-track any tube
(SAM 3.1 + Muse Spark verification); reviewer agent that runs the simulator as a tool (Muse Spark Open on
Baseten); one question per reagent, pre-filled with a simulator-checked plan; robot replay of the simulator's
own command log on the deck; downloadable `protocol.py` with Opentrons liquid definitions; three independent checks
(Opentrons simulator, official `opentrons analyze`, PyLabRobot Hamilton STARlet simulator with volume tracking);
Methods / Autoprotocol / PyLabRobot exports; reproducibility receipt (SHA-256 of protocol, video and model outputs) anchored in Bitcoin with OpenTimestamps;
answer by voice (Muse Voice Transcribe + Muse Spark); reviewer agent with a `look_at_moment` tool that proposes one-click fixes (rename reagent, set volume,
skip invented steps; applied only when the scientist clicks); per-step SAM 3.1 tip evidence; thermal cycling program read off the bench sheet and exposed as Opentrons runtime
parameters (editable in the Opentrons App); Opentrons Flex liquid classes per reagent (viscous premix, volatile
ethanol, aqueous), also runtime parameters; liquid presence detection switch (Flex pipette sensor probes each tube);
STARlet export uses Opentrons' liquid class flow rates; the whole run as an RO-Crate (Process Run Crate, passes rocrate-validator);
thermocycler time from Opentrons' analysis at the module's rated ramp rates; an MCP server so other agents can read runs and protocols as tools; strict nonce CSP; reviewer fails over from Baseten to Fireworks; Playwright QA suite with axe-core accessibility checks in light and dark mode, CSP, MCP, layout-shift and caption and edit-copy checks (130/130 against production); Lighthouse 100/100/100, app performance 94 with zero layout shift; transcribed speech shown as video captions; the protocol opens as editable no-code steps in Opentrons Protocol Designer, verified by importing it into the live editor and analysing the editor's own export; the same steps also run on a Hamilton STARlet and a Tecan Freedom EVO 150 in PyLabRobot's simulator, and visitors can re-run those simulations in their own browser (Pyodide); a phone can film the protocol straight into the upload dialog; receipts are timestamped in Bitcoin (OpenTimestamps); printable QR tube labels (Avery L7160 sheet, or ZPL for a Zebra thermal printer, previewed in the browser by a WebAssembly ZPL renderer) that the phone camera scans to load the tube rack, each scan lighting that tube's well on the deck; film one reaction and run up to a plate of it, through an Opentrons "Reactions" runtime parameter verified by opentrons analyze at the deck's maximum.

**Describe your use of Meta products**
> - Muse Spark 1.3 (Meta Model API, Files + Responses API, strict JSON schema): video → timestamped protocol steps;
>   OCR of the printed protocol sheet from full-resolution stills; a second pass that groups every transfer by its
>   physical source tube from aspiration stills; and identity verification of SAM tracks.
> - SAM 3.1 (Meta Model API): text-prompted pipette tracking, and point-prompted click-to-track of any tube with
>   propagation in both directions, parsed with meta-sam-parser.
> - Muse Voice Transcribe: timestamped speech turns with reagent-name keyword biasing, fed to Muse Spark as evidence
>   (Spark 1.3's own audio understanding is limited).
> - Muse Spark Open (open weights on Baseten): a reviewer agent that watches the video, calls the Opentrons simulator
>   as a tool to find a volume plan whose per-tube totals match the sheet, and returns structured findings.

**Why are Meta's models important to your solution?**
> The task needs long-video understanding, small-text reading, precise object tracking and agentic tool use, all on
> the same footage. Muse Spark's 1M-token context takes a whole 5-minute protocol video in one call, and SAM 3.1 is
> the only piece that can follow one specific tube through a head-mounted camera's motion. Neither is enough alone:
> SAM jumps to look-alike caps with 0.95 confidence, and Spark mislabels reagents when it only sees the whole video.
> Combining them (SAM finds, Spark verifies; Spark segments, Spark-on-stills identifies) is what made held-out
> held-out PCR precision go from 0.48 to 0.69. Running the reviewer on open weights means a lab could keep it on-premise.

**Feedback on Meta products** (honest, short)
> - The `muse-voice-transcribe` keyword limit (30 characters) isn't in the docs; it returns a 400.
> - Reasoning shares the output budget: with a low max_output_tokens the structured output came back empty.
> - SAM 3.1 point tracking is impressive, but identity drifts to similar objects with high confidence. A per-frame
>   "same object" score would help.

**Feedback on partner technologies**
> Baseten: Muse Spark Open with video input and tool calling worked first try over the OpenAI SDK. The
> Each key is bound to one model id: the hackathon key serves only `meta-models/muse-spark-open-1026` (403 on the
> bare id), the earlier key only `meta-models/muse-spark-open` (404 on `-1026`), so the app keeps both as failover hosts.

## Demo video (3:00)

| Time | Picture | Voice-over |
|---|---|---|
| 0:00–0:15 | Idle Opentrons robot photo → scientist pipetting by hand (BioVL2 clip) | "Labs buy robots that sit idle, because programming them means writing Python. What if you could just show it once?" |
| 0:15–0:35 | Terminal: `python -m sdc.pipeline pcr_3.mp4`, stages printing (sped up) | "This is a real lab video. One command: Muse Spark watches all four minutes and finds every pipetting step." |
| 0:35–1:05 | UI: video plays, steps highlight in sync, deck wells light up (source orange, destination green) | "Each step lands on the robot deck. Water into four tubes, then primer, template, premix, then the thermocycler." |
| 1:05–1:35 | Click a tube → SAM mask follows it → reviewer text "tracker jump, ignore" | "Not sure which tube is which? Click it. SAM 3.1 follows it through the whole video, and Muse Spark checks every sighting. Here it catches SAM jumping to a look-alike cap." |
| 1:35–2:05 | Reviewer agent panel: tool runs, totals 12.5 µL, 0.25 µL warning, 35 cycles from the sheet | "A reviewer agent on Muse Spark Open watches the same video, runs the robot simulator itself, and works out volumes that add up to the sheet's 12.5 microlitres. It flagged a 0.25 µL step the robot can't pipette, and found 35 cycles on a handwritten note." |
| 2:05–2:25 | Confirm → "simulator passed · 106 robot actions" → download protocol.py → code scroll | "Confirm, and you get a real Opentrons protocol that passes the official simulator." |
| 2:25–2:45 | Eval table, held-out numbers | "On PCR videos we never tuned on, it finds 79% of the protocol steps; on a protocol it was never built for, far fewer, and we show that too. Every model call is cached and every number is reproducible." |
| 2:45–3:00 | Title card + URL | "Show, Don't Code. Built in nine days on Muse Spark, SAM 3.1, Muse Voice Transcribe and Muse Spark Open." |

Recording notes: record the UI at 1080p from the live Vercel link; trim model wait times and say so on screen
("sped up"); show the BioVL2 credit on screen at the first lab clip.

## X post (for Most Viral)

> I filmed nothing. A Kyoto lab filmed themselves doing PCR by hand. My app watched it and wrote the robot code. 🧪🤖
>
> Muse Spark finds every pipetting step, SAM 3.1 tracks the tubes, and an agent on Muse Spark Open runs the
> Opentrons simulator itself until the volumes add up.
>
> 79% of PCR steps on videos it never saw. Try it: show-dont-code.vercel.app
> #MetaAIHackathon @AIatMeta

(Attach a 30–45 s cut: hand pipetting on the left, robot deck lighting up on the right.)
