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
> generation is deterministic, so model output never becomes executable code. Muse Image turns the same footage into a
> printable bench card for training: each step's own video frame redrawn as a manual illustration, which Muse Spark
> checks against the frame and sends back if anything was added or moved; every word on the card comes from the
> protocol. A lab notebook entry (.eln, the ELN Consortium format) imports the whole run into eLabFTW, RSpace or
> Kadi4Mat as one experiment; verified by importing it into the public eLabFTW demo. The volumes are cross-checked against
> published protocols.io protocols with the same kit: Muse Spark reads them, and only volumes quoted verbatim are kept. No video? Paste a published protocols.io
> protocol: Muse Spark reads its text into the same steps, and it goes through the same codegen and checks, so the
> protocols labs already publish become robot runs too. Every protocol is run end to end on Opentrons' own
> robot-server (the software inside a Flex), and every command it ran matched its own analysis (240 of 240). On held-out videos from the public
> BioVL2 dataset, step recall is 0.79 and precision 0.69 on PCR (n=4 held-out videos), and 0.28 / 0.42 on a
> protocol it was never developed on (plasmid miniprep).

**Public GitHub repository:** https://github.com/PugarHuda/show-dont-code
**Dataset DOI (pcr_1-gt RO-Crate on Zenodo):** https://doi.org/10.5281/zenodo.23876762
**3-minute demo video:** https://youtu.be/zZE0nk85MxE (2:57, unlisted)
**Bonus prizes this is eligible for:** Best Build on Meta Open Weight Models (the reviewer agent runs on Muse Spark Open,
served by Baseten as `meta-models/muse-spark-open-1026`, failing over to Fireworks) and Best Agentic Use-Case (the agent
watches the video, calls the Opentrons simulator and `look_at_moment` as tools until the volumes add up, and proposes fixes
the scientist applies with one click). Best Creative Build (Muse Image draws the bench card from the scientist's own
footage; Muse Spark checks every drawing against its frame and has it redrawn when it isn't faithful).
**Live demo:** https://show-dont-code.vercel.app (landing) and https://show-dont-code.vercel.app/app (eight sample runs: seven filmed BioVL2 videos and one published protocols.io protocol).
Uploading your own video needs the access code; put it in the description field of the form, never in this
repo (the code lives in `out/upload_code.txt`, which is gitignored).

**Features to name in the form:** upload a video and watch each stage run (Modal backend); click-to-track any tube
(SAM 3.1 + Muse Spark verification); reviewer agent that runs the simulator as a tool (Muse Spark Open on
Baseten); one question per reagent, pre-filled with the reviewer's plan (each value marked when the simulator ran it); robot replay of the simulator's
own command log on the deck; downloadable `protocol.py` with Opentrons liquid definitions; checks by three vendors'
robot software (Opentrons simulator, official `opentrons analyze`, a full run on Opentrons' own robot-server v10.1.0 in Flex
simulation with the robot's analysis, deck configuration and play-to-the-end, and PyLabRobot Hamilton STARlet and Tecan EVO
simulators with volume tracking);
Methods / Autoprotocol / PyLabRobot exports; reproducibility receipt (SHA-256 of protocol, video and model outputs) anchored in Bitcoin with OpenTimestamps;
answer by voice (Muse Voice Transcribe + Muse Spark); reviewer agent with a `look_at_moment` tool that proposes one-click fixes (rename reagent, set volume,
skip invented steps; applied only when the scientist clicks); per-step SAM 3.1 tip evidence; thermal cycling program read off the bench sheet and exposed as Opentrons runtime
parameters (editable in the Opentrons App); Opentrons Flex liquid classes per reagent (viscous premix, volatile
ethanol, aqueous), also runtime parameters; liquid presence detection switch (Flex pipette sensor probes each tube);
STARlet export uses Opentrons' liquid class flow rates; the whole run as an RO-Crate (Process Run Crate, passes rocrate-validator);
thermocycler time from Opentrons' analysis at the module's rated ramp rates; an MCP server so other agents can read runs and protocols as tools; strict nonce CSP; reviewer fails over from Baseten to Fireworks; Playwright QA suite with axe-core accessibility checks in light and dark mode, CSP, MCP, layout-shift, caption, edit-copy, validation and robot-server checks (172/172 against production); Lighthouse 100/100/100 (accessibility, best practices, SEO) on both pages, performance 100/93 desktop, CLS under 0.01; transcribed speech shown as video captions; the protocol opens as editable no-code steps in Opentrons Protocol Designer, verified by importing it into the live editor and analysing the editor's own export; the same steps also run on a Hamilton STARlet and a Tecan Freedom EVO 150 in PyLabRobot's simulator, and visitors can re-run those simulations in their own browser (Pyodide); a phone can film the protocol straight into the upload dialog; receipts are timestamped in Bitcoin (OpenTimestamps); printable QR tube labels (Avery L7160 sheet, or ZPL for a Zebra thermal printer, previewed in the browser by a WebAssembly ZPL renderer) that the phone camera scans to load the tube rack, each scan lighting that tube's well on the deck; film one reaction and run up to a plate of it, through an Opentrons "Reactions" runtime parameter verified by opentrons analyze at the deck's maximum; an illustrated bench card (Muse Image redraws each step's own frame, Muse Spark checks every drawing against it and sends unfaithful ones back: 108 drawings across the 7 filmed samples, 33 redrawn, 100 passed), printable and in the RO-Crate; a lab notebook entry (.eln, ELN Consortium 1.2+20260923 on RO-Crate 1.2) that eLabFTW, RSpace and Kadi4Mat import as one experiment, verified by importing it into the public eLabFTW demo (scripts/elabftw_roundtrip.py); volumes cross-checked against published protocols.io protocols with the same kit (Muse Spark reads them; only quotes really in their text are kept; on pcr_1-gt the PrimeSTAR premix share matches a published protocol's 25 uL in 50 uL).

**Describe your use of Meta products**
> - Muse Spark 1.3 (Meta Model API, Files + Responses API, strict JSON schema): video → timestamped protocol steps;
>   OCR of the printed protocol sheet from full-resolution stills; a second pass that groups every transfer by its
>   physical source tube from aspiration stills; identity verification of SAM tracks; checking every Muse Image
>   drawing against its video frame; reading a published protocols.io protocol's text into steps; and reading the
>   volumes related published protocols use for the same kit, each with a verbatim quote the code checks.
> - SAM 3.1 (Meta Model API): text-prompted pipette tracking, and point-prompted click-to-track of any tube with
>   propagation in both directions, parsed with meta-sam-parser.
> - Muse Voice Transcribe: timestamped speech turns with reagent-name keyword biasing, fed to Muse Spark as evidence
>   (Spark 1.3's own audio understanding is limited).
> - Muse Image (Meta Model API, /v1/images/edits): the bench card. Each step's video frame is edited into a clean
>   training-manual illustration (keep every object, draw no text); Muse Spark compares each drawing with its frame
>   and the step is redrawn if anything was added, removed or moved.
> - Muse Spark Open (open weights on Baseten): a reviewer agent that watches the video, calls the Opentrons simulator
>   as a tool to find a volume plan whose per-tube totals match the sheet, and returns structured findings.

**Why are Meta's models important to your solution?**
> The task needs long-video understanding, small-text reading, precise object tracking and agentic tool use, all on
> the same footage. Muse Spark's 1M-token context takes a whole 5-minute protocol video in one call, and SAM 3.1 is
> the only piece that can follow one specific tube through a head-mounted camera's motion. Neither is enough alone:
> SAM jumps to look-alike caps with 0.95 confidence, and Spark mislabels reagents when it only sees the whole video.
> Combining them (SAM finds, Spark verifies; Spark segments, Spark-on-stills identifies) is what made held-out
> PCR precision go from 0.48 to 0.69. Running the reviewer on open weights means a lab could keep it on-premise.

**Feedback on Meta products** (honest, short)
> - The `muse-voice-transcribe` keyword limit (30 characters) isn't in the docs; it returns a 400.
> - Reasoning shares the output budget: with a low max_output_tokens the structured output came back empty.
> - SAM 3.1 point tracking is impressive, but identity drifts to similar objects with high confidence. A per-frame
>   "same object" score would help.

**Feedback on partner technologies**
> Baseten: Muse Spark Open with video input and tool calling worked first try over the OpenAI SDK.
> Each key is bound to one model id: the hackathon key serves only `meta-models/muse-spark-open-1026` (403 on the
> bare id), the earlier key only `meta-models/muse-spark-open` (404 on `-1026`), so the app keeps both as failover hosts.

## Demo video (2:57)

https://youtu.be/zZE0nk85MxE. Every scene is recorded live from production with Playwright (`video/record.py`),
narrated by a neural AI voice with word-timed subtitles (`video/make_voice.py`, edge-tts) and assembled in Remotion
(`video/src`); waits are sped up and marked on screen. The narration and the result callouts are in `video/script.json`;
rebuild with `python video/make_voice.py && python video/record.py && npm run render --prefix video`.

## X post (for Most Viral)

The 46 s square ad is `video/out/show-dont-code-ad.mp4` (Remotion composition `Ad`, narration in `video/ad.json`;
rebuild with `python video/make_voice.py ad && npm run render:ad --prefix video`). The post text is long-form (X
Premium) and names every model; paste the post's link into the submission's X field once it is live.
