# Show, Don't Code

[![checks](https://github.com/PugarHuda/show-dont-code/actions/workflows/checks.yml/badge.svg)](https://github.com/PugarHuda/show-dont-code/actions/workflows/checks.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23876762.svg)](https://doi.org/10.5281/zenodo.23876762)

Film yourself doing a lab protocol once by hand, and get an Opentrons robot protocol that runs it.

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026), Push the Frontier theme. All code in
this repository was written during the hackathon. The git history starts on Oct 27, 2026.

- **Live app:** https://show-dont-code.vercel.app (landing) and https://show-dont-code.vercel.app/app (eight samples,
  no login: seven filmed BioVL2 videos and one published protocols.io protocol)
- **Demo video (2:58):** https://youtu.be/s_g8SAHBRDU, recorded live from production with Playwright, narrated and
  assembled in Remotion (`video/`); a 53 s square cut for X is the `Ad` composition (`npm run render:ad --prefix video`)
- **Dataset:** the pcr_1-gt run as an RO-Crate on Zenodo, [doi:10.5281/zenodo.23876762](https://doi.org/10.5281/zenodo.23876762);
  its protocol on protocols.io: https://www.protocols.io/view/pcr-setup-recovered-from-a-first-person-video-pcr-krdncv25f
  (submitted for publication Nov 2, in protocols.io's review)
- **MCP server:** https://pugarhuda--show-dont-code-web.us-east.modal.run/mcp
- **REST API (OpenAPI):** https://pugarhuda--show-dont-code-web.us-east.modal.run/docs, every endpoint the app uses,
  with its schema at `/openapi.json`, so a lab's own tools can call it directly (runs, protocol.py, exports, receipt,
  RO-Crate, .eln, robot-server runs, imports)
- **Cite it:** `CITATION.cff` (GitHub's "Cite this repository"); the pcr_1-gt run is a dataset with its own DOI

## Why

There are 3,522 public PCR protocols on protocols.io (API search, Nov 3 2026), and about 100 ready-to-run protocols for
the Opentrons Flex 18 months after its launch ([Opentrons, Jan 2025](https://www.businesswire.com/news/home/20250128131412/en)).
Everything else has to be programmed, and scripting liquid handlers "remains a persistent bottleneck requiring
substantial programming expertise" ([bioRxiv, 2025](https://www.biorxiv.org/content/10.1101/2025.09.30.679666v1.full)).
Show, Don't Code starts from what a lab already has: the protocol they do by hand (a video), or the one they published.

## How it works

Five Meta models, each doing the part it is best at:

| Model | Job |
|---|---|
| **Muse Spark 1.3** (Meta Model API) | Stage 1: watches the whole video and finds every step with timestamps (strict JSON). Reads the printed protocol sheet on the bench from full-resolution stills. Stage 2: looks closely at the aspiration moment of every transfer, groups transfers by physical source tube, and names each group. |
| **SAM 3.1** (Meta Model API) | Tracks the pipette tip. **Click-to-track**: the scientist clicks a tube in the video, and a point prompt follows it through the whole recording. |
| **Muse Voice Transcribe** (Meta Model API) | Turns anything said aloud ("fifty microlitres of primer") into timestamped evidence for Muse Spark, with reagent names as keyword bias. |
| **Muse Image** (Meta Model API) | Draws the **bench card**: each filmed step's own video frame redrawn as a clean training-manual illustration (an edit of the real frame, told to keep every object and draw no text), which Muse Spark then compares with the frame and sends back if anything was added, removed or moved. |
| **Muse Spark Open** (open weights, served by Baseten) | Reviewer agent. Watches the video, checks the steps against it and the bench sheet, and **runs the Opentrons simulator as a tool** to work out a volume plan that hits the sheet's per-reaction total. Its third tool, `published_volumes`, reads how public protocols.io protocols with the same kit dose it (the volume lines, verbatim, with DOIs); on pcr_3 it cited "PrimeStar Mix (2X): 25 uL in 50 uL" (doi:10.17504/protocols.io.81wgbrknnlpk/v2) before its plan passed the simulator. |

Then:

1. A **deterministic code generator** turns the steps into an Opentrons Flex protocol (Protocol API 2.24). The
   models decide *what* happened; plain code decides *how* the robot does it, so model output never becomes
   executable code. Steps a robot can't do (centrifuge, invert) become `ctx.pause()` hand-offs. "Set in DNA
   engine" becomes the Thermocycler module.
2. The protocol runs in the **official Opentrons simulator**. Anything the video can't show goes back to the
   scientist as a question first. Volumes are asked once per reagent and source tube, pre-filled with the
   agent's simulator-verified plan.
3. **SAM finds, Spark verifies.** SAM 3.1 sometimes jumps to a similar-looking object (another cap of the same
   colour) with high confidence. When a tube is clicked, Muse Spark compares every tracked sighting with the
   clicked reference, rejects the jumps, and reports which steps draw from or fill that tube. The scientist names
   it once and the name is applied everywhere.
4. The **review UI** plays the human video next to the robot deck, step by step.
5. **Muse Image draws, Muse Spark checks.** The bench card turns the footage into a printable illustrated guide for
   training the next person at the bench: one drawing per step, made from that step's own frame (`sdc/illustrate.py`).
   Muse Spark compares every drawing with its frame and has it redrawn if an object was added, removed or moved, or
   text was drawn in (on `pcr_1-gt`, two of six first drawings were sent back). Every word and number under a drawing
   comes from the protocol, never from the image model. `card.json` keeps each frame's and drawing's SHA-256 and the
   checker's verdict; it is in the RO-Crate as its own action, and the MCP tool `get_bench_card` serves it.

## What you get out of a run

- `protocol.py` for the **Opentrons Flex**, with every reagent declared as a liquid (`define_liquid` / `load_liquid`,
  volume = what the transfers draw + 10 % + the tube's dead volume), so the Opentrons App shows the liquid setup.
  Tip racks scale with the number of transfers, and more than 24 tubes get a second (third...) tube rack in a free
  deck slot. Every step is announced with `ctx.comment("Step 3 · video 64-99 s: ...")`, so the Opentrons App's run
  timeline points back to the moment in the video; the in-app robot replay uses the same lines to move the human
  video along with the robot.
- Every transfer uses an **Opentrons Flex liquid class** (`transfer_with_liquid_class`, API 2.24): enzyme mixes and
  premixes are pipetted as *viscous* (slower flow, delays, the tuned air gap and blowout for glycerol), ethanol as
  *volatile*, everything else as *aqueous*. The class is picked from the reagent's name, and each reagent's class is a
  runtime parameter, so the lab switches it in the Opentrons App if the name guessed wrong.
- **Liquid presence detection** is one switch at run setup ("Check tubes for liquid", off by default because it adds
  seconds per draw): when on, the Flex pipette's pressure sensor probes each source tube with the tip it is about to
  aspirate with (`require_liquid_presence`), and an empty tube stops the run instead of dispensing air. The self-check
  turns it on and counts the `liquidProbe` commands in Opentrons' analysis.
- The **Hamilton STARlet** translation moves each reagent at the flow rates Opentrons' own liquid class definition gives
  that volume (PyLabRobot's STAR backend otherwise pipettes everything as water), so the premix is drawn slowly on both
  robots.
- The **thermal cycling program** comes off the bench sheet when one is written there: Muse Spark turns the sheet's
  notes into a structured program (`profile.json`; on `pcr_4` it reads "94C 5min, 94C 30s / 55C 30s x32 / 72C, 72C
  5min"). A temperature written without a time gets 30 s and says so; nothing legible means a stated default. Every
  temperature, time and the cycle count are **Opentrons runtime parameters**, so the lab changes them in the
  Opentrons App at run setup without touching code. `methods.md` and `autoprotocol.json` use the same program.
- **Film one reaction, run a plate.** A protocol that ends on the thermocycler gets a **Reactions** runtime parameter:
  the lab picks a number on the robot's touchscreen, and every transfer and mix repeats into the next PCR plate wells,
  the source tubes are filled for all of them, and only as many tip racks as that number needs are loaded. The maximum
  comes from the deck itself: plate wells, a 1.5 mL tube holding every reaction's draw (+10 % and dead volume; 53 for
  `pcr_1-gt`, bound by 25 µL of premix each), and free slots for tip racks. The protocol self-check and the e2e suite
  run `opentrons analyze --rtp-values` at that maximum and require one plate well per copy, one tip per copy of each
  step, and tubes filled for all of them. The Reactions box next to the labels, `?reactions=N` on `labels.svg` and
  `labels.zpl`, and MCP `get_tube_labels(name, reactions)` give the fill volumes for that number. Typing a number there
  also runs Opentrons' analysis on the server with Reactions set to it (`/api/runs/<run>/reactions/<n>`, cached per
  protocol version): the deck marks the plate wells the copies go into and draws the extra tip racks in the slots the
  analysis loaded them, and "Before you run" switches to that number's volumes. The PyLabRobot,
  Autoprotocol and Protocol Designer exports stay at the one filmed reaction.
- **Checks by three vendors' robot software** for every confirmed protocol: the Opentrons simulator,
  Opentrons' own protocol analysis (`opentrons analyze`, what the App runs on import, saved as `analysis.json`), and
  translations for a **Hamilton STARlet** and a **Tecan Freedom EVO 150** (LiHa arm, DiTi tips, the DiTi carrier's
  waste slot), each run in **PyLabRobot**'s simulator with volume tracking on, so over-drawing a tube fails there too.
  The e2e suite runs the downloaded Tecan file and requires as many pipetting actions as the STARlet's.
- **Re-run those simulations in your own browser**, without trusting our server's "passed": one click loads
  CPython compiled to WebAssembly ([Pyodide](https://pyodide.org) 0.27.7), installs the same PyLabRobot 0.2.2 from
  PyPI and executes the downloaded `pylabrobot.py` or `tecan-evo.py` on the page, then compares the action count
  with the server's. The e2e suite also serves it a copy with the premix stock cut to 5 µL and requires the browser
  run to fail with PyLabRobot's `TooLittleLiquidError`.
- `methods.md` (a methods section for a notebook or paper), `autoprotocol.json` (vendor-neutral), `pylabrobot.py`
  (STARlet) and `tecan-evo.py`.
- **Load the tube rack by scanning labels.** `labels.svg` is a sheet of tube labels sized for Avery L7160 / J8160
  (3 x 7 on A4). Each label carries the tube's name, its deck slot and well, how much to fill it with (what the
  protocol draws, plus 10 % and the tube's dead volume, the same number `protocol.py` loads), and a QR code (made
  with [segno](https://github.com/heuer/segno)) linking to `/app?run=...&tube=...`. Any phone camera app opens that
  link on the tube's place in the rack. In the app, "Check the tube rack with your camera" reads labels live
  ([jsQR](https://github.com/cozmo/jsQR) 1.4.0, pinned with subresource integrity): each tube scanned lights its well
  on the deck, and the count says when the rack is loaded the way `protocol.py` expects. A mis-loaded rack is the
  usual way a correct protocol still ruins a run. The e2e suite photographs a label from the real sheet, feeds it to
  Chromium's fake camera, and requires the scan to light the right well; it also opens a label's link directly and
  checks that a label from another run's rack is refused.
- **Or print them on a Zebra.** Most labs print tube labels on a thermal printer, not an A4 sheet. `labels.zpl` is the
  same set in ZPL II for 2 x 1 inch labels at 203 dpi: send it to the printer as is (`lp -o raw`, or Zebra Setup
  Utilities). Model text in it is hex-escaped (`^FH`), so a reagent named `^XZ~JR` prints as text instead of resetting
  the printer. "Preview the Zebra labels" draws the file the way the printer will, in the browser:
  [zebrash](https://github.com/ingridhq/zebrash), a Go ZPL renderer compiled to WebAssembly
  ([zpl-renderer-js](https://github.com/Fabrizz/zpl-renderer-js) 4.0.0, pinned; module and wasm both loaded with subresource
  integrity), so labels never go to an online render service. The e2e suite renders every label that way and reads each
  QR code back with jsQR. Both label files are in the RO-Crate, and MCP `get_tube_labels` gives an agent each tube's
  slot, well, fill volume and label link.
- **Opentrons Protocol Designer, no code.** `protocol-designer.json` opens in Opentrons' own visual editor
  ([designer.opentrons.com](https://designer.opentrons.com/), Import): every transfer, mix and thermocycler step
  arrives as an editable form named after its moment in the video ("Add Primer1", "Step 2, video 64-99 s"), with the
  deck, liquids, tip heights and the cycling program filled in. It is built from Opentrons' own analysis of
  `protocol.py` (`sdc/designer.py`), not re-derived from the steps, and checked three ways: `opentrons analyze` runs
  the file with the same aspirate/dispense volumes and the same thermocycler time as `protocol.py`; the e2e suite
  imports it into the live Protocol Designer (every step builds a valid robot state); and the Python that Protocol
  Designer exports from it passes `opentrons analyze` with the same volumes. A scientist who never reads Python
  can change a volume or a temperature there and send it to the robot.
- A **reproducibility receipt**: SHA-256 of the protocol, the video and every model output, the model IDs, and the
  scientist's answers. Ground-truth replays say no model read them.
- A **Bitcoin timestamp** on that receipt ([OpenTimestamps](https://opentimestamps.org), `receipt.ots`): the receipt's
  SHA-256 goes to four public calendars (only the hash leaves, behind a random nonce); once their Bitcoin
  transaction confirms, the proof upgrades on a later visit and the server checks it against the block header
  (mempool.space, Blockstream as fallback). That makes it independently checkable that this protocol, video and
  every model output existed no later than that block: `ots verify -d <stamped_sha256> receipt.ots`. Each edited
  copy gets its own stamp for its own answers.
- **Everything as an RO-Crate** (`ro-crate.zip`, [Process Run Crate 0.5](https://w3id.org/ro/wfrun/process/0.5)): the
  protocol, the exports, every model output and the Opentrons analysis, each pinned by SHA-256, plus JSON-LD saying
  which tool (Muse Spark, SAM 3.1, the reviewer on whichever host ran it, the code generator at the hash of its own
  source, Opentrons) made which file from which inputs. WorkflowHub and other RO-Crate readers open it; the e2e suite
  runs it through `rocrate-validator` (all REQUIRED checks pass; the one RECOMMENDED gap is a `publisher`, which a
  hackathon entry doesn't have). The video stays out of the zip and is pinned by its hash.
- **Thermocycler time**: the minimum the block needs for the program, summed over the `thermocycler/*` commands in
  Opentrons' analysis (every hold, plus every ramp at the Thermocycler GEN2's rated 4.25 °C/s heating and 2.0 °C/s
  cooling, [spec](https://docs.opentrons.com/thermocycler/product-specs/)). 35 cycles of 98/55/72 °C come to 31 min.
  Opentrons' own `opentrons_simulate -e` reports 0 min for Flex protocols, which is why it is computed here.
- **Straight to the robot**: `python scripts/send_to_flex.py <flex-ip> pcr_1-gt --reactions 8` uploads the protocol to a
  Flex on your network over its HTTP API (as the Opentrons App does), waits for the robot's own analysis with that
  Reactions value and creates the run on its touchscreen; `--start` also presses play, `--configure-deck` writes the
  thermocycler into the robot's deck configuration (cutouts A1+B1).
- **A run on Opentrons' own robot software**, one click under "Before you run": the backend starts Opentrons'
  robot-server v10.1.0 (the software inside a Flex, built from Opentrons' repo at that tag) in Flex simulation on
  Modal, and sends the protocol through the same script with the chosen number of reactions. The robot analyses it,
  takes the thermocycler into its deck configuration, plays it to the end with hand-off pauses resumed, and reports
  its run status and command count (pcr_1-gt, two reactions: 240 commands, "succeeded").
- **Start from a published protocol instead of a video**: paste a protocols.io link or DOI (in the upload dialog, no
  access code). Muse Spark reads the protocol's text into the same steps a video yields (one reaction, the smallest
  size given; volumes only when stated, so ranges and "water to volume" become questions), reads the cycling program,
  and the run goes through the same codegen, simulator, analysis, PyLabRobot and reviewer agent. Only public protocols
  are read. Sample: `protocolsio_q5`, from NEB's Q5 High-Fidelity 2X Master Mix protocol
  ([doi:10.17504/protocols.io.7cwhixe](https://doi.org/10.17504/protocols.io.7cwhixe)); answered with the reviewer's
  plan it passes the simulator, `opentrons analyze` (145 commands) and both PyLabRobot robots. Also checked live with
  [Colony PCR](https://doi.org/10.17504/protocols.io.gzbbx2n).
- **Published protocols with the same kit**: each run searches protocols.io's public API for the brand and kit
  words in its reagent names (e.g. "PrimeSTAR", from "DNA polymerase (Prime STAR MAX)") and its protocol type, ranks
  the results by shared reagent words and lists them with their DOIs, so the filmed volumes can be checked against
  how other labs wrote the same kit up (also MCP `get_related_protocols`). **Volumes cross-checked against them**
  (`sdc/crosscheck.py`): Muse Spark reads up to three of those protocols and gives the volume each uses for this run's
  reagents with a verbatim quote; plain code keeps only quotes that are really in the protocol's text and compares each
  reagent's share of its reaction. On pcr_1-gt, a published PrimeSTAR protocol uses "PrimeStar Mix (2X) 25 uL" in a
  50 uL reaction, the same 50 % share as the filmed premix; its primers (1 uL) differ from the filmed 1.5 uL, and the
  app says so (MCP `crosscheck_volumes`).
- **Citable and forkable**: `scripts/zenodo_deposit.py <run>` deposits the run's RO-Crate on Zenodo with metadata
  from its receipt (CC BY 4.0, derived from BioVL2's DOI) and reserves a DOI; `scripts/protocolsio_publish.py <run>`
  puts it on protocols.io as one step per step found in the video (volume, reagent, video seconds, robot or by hand),
  linked to protocol.py. pcr_1-gt is published on Zenodo:
  [doi:10.5281/zenodo.23876762](https://doi.org/10.5281/zenodo.23876762).
- **A lab notebook entry** (`run.eln`, the ELN Consortium's file format: RO-Crate 1.2 in a zip, ELN version
  1.2+20260923) that eLabFTW, RSpace, Kadi4Mat, SampleDB and other electronic lab notebooks import as one experiment:
  the steps with volumes and video times, the checks the protocol passed and the bench card in the entry's body, every
  file of the run attached with its SHA-256, and each reviewer-agent finding as a comment on the entry
  (`sdc/eln.py`). It passes `rocrate-validator`'s RO-Crate 1.2 profile, and `python scripts/elabftw_roundtrip.py`
  imports it into the public eLabFTW demo (6.0.9) and checks the entry: on Nov 2 pcr_1-gt arrived as experiment with
  its steps table, checks, licence, tags and all 16 files, the bench card drawings shown as images.
- **A photo or PDF of a protocol instead of a video** (`POST /api/import/document`, the upload dialog): a printed
  sheet, a notebook page or a paper's methods, up to 6 pages. The file's type is checked by its bytes, PDF pages are
  rendered and every page is re-encoded as a plain JPEG before Muse Spark transcribes them (illegible parts are marked,
  never guessed); then the same step reader, codegen, simulator, analysis and reviewer agent as a protocols.io import.
  The sample `pdf_colony_pcr` is protocols.io's own 3-page PDF of a public Colony PCR protocol (CC BY 4.0); on Nov 3 the
  same PDF and a single page photo ran live on production in about 2 minutes each. A visitor's document gets no licence,
  like a video upload; the RO-Crate and .eln cite it by the SHA-256 of its pages.
- **An MCP server** at `https://pugarhuda--show-dont-code-web.us-east.modal.run/mcp` (Streamable HTTP), so another
  agent can use the runs as tools: `list_runs`, `get_run` (steps with video seconds, open questions, simulator and
  analysis results), `get_protocol`, `get_protocol_designer` (the same protocol as no-code editor steps), `get_tube_labels` (what goes where on the tube rack, and how much), `run_on_flex_robot_server` (the protocol run end to end on Opentrons' robot-server, the robot's own report back), `get_related_protocols`, `crosscheck_volumes` (this run's volumes next to published ones, with quotes), `get_bench_card` (the illustrated steps with the checker's verdict), `get_receipt`; `get_run` also reports the STARlet and Tecan EVO simulations. No tool runs code an agent sends (a
  protocol is Python) or spends model credits; robot-server runs are only of protocols this server generated, cached, and capped per day. Add it to Claude Code with
  `claude mcp add --transport http show-dont-code <url>/mcp`.
- **Edits never change a shared sample.** The first answer, tube name or accepted reviewer fix on a sample run
  goes to a private copy (`c-…`, as unguessable as an upload link) that the page switches to and keeps in the URL,
  so one judge's answers don't become the next judge's demo. A copy holds only the run's JSON (~430 KB) and reads
  the footage and SAM frames from its sample; a rejected edit leaves no copy, writes to one run are serialized, and copies expire after 7 days untouched; past 1000 (~430 MB) the least recently edited go first, never one being edited.

The scientist can also **answer by voice** (browser recording → Muse Voice Transcribe with reagent-name keyword
bias → Muse Spark maps the words to the open questions → the form is pre-filled, never submitted), and the reviewer
agent can **look at any second of the video itself** (`look_at_moment`, up to 6 frames) before it decides. Each
finding can carry **one proposed fix** (rename a reagent, set a volume, skip steps that never happened); the
server validates it against the steps, and nothing changes until the scientist clicks Apply. Every step shows its
**SAM 3.1 evidence** (how many frames the pipette tip was tracked in), and steps with none are flagged to the agent.
The fixes are not counted in the evaluation table: accepting every proposed fix blindly leaves recall and precision
unchanged on all five test videos (on `pcr_2` the duplicates it skips sit inside groups that already match), so
their value is in the protocol the robot runs, not in the benchmark.

Baseten keys are tied to one model id: the hackathon key (`BASETEN_HACKATHON_KEY`, Oct 29) serves only
`meta-models/muse-spark-open-1026` and answers 403 for the bare id, while the earlier key serves only
`meta-models/muse-spark-open` (`BASETEN_API_KEY`, override with `OPEN_MODEL`) and answers 404 for `-1026`. The reviewer
tries them in that order. If both fail, the review moves to the same model on
**Fireworks** (`FIREWORKS_API_KEY`) and stays there for the rest of the conversation. Fireworks' Muse Spark Open
rejects video input ("does not support video inputs yet", Oct 28), so on that host the agent works from the frames it
requests with `look_at_moment`; the result records which host ran (`review_model`) and whether it saw the video.

## Hosting

The landing page and app HTML are on Vercel; the API, uploads and the pipeline worker run on Modal
(`modal_app.py`), with all runs on one Modal Volume. Live model calls from the page (voice answers, click-to-track) share a daily budget stored on the Volume, so a
restart doesn't reset it and it refills at 00:00 UTC. Uploads need an access code (fail closed), are trimmed to
5 minutes, size-capped while streaming, and private to whoever holds the link. Instead of picking a file, a phone
can film the protocol in the upload dialog: the back camera is recorded in the browser (MediaRecorder, MP4 where
the browser records it, else WebM) and sent as the upload; a codec MP4 can't hold as is (VP8) is re-encoded to
H.264 on the server. The e2e records with Chromium's fake camera, runs the server's ffmpeg step on the clip, and
sends it to the real endpoint with a wrong code. Pages are served under a strict
Content-Security-Policy (scripts run only with a per-response nonce, plus the pinned Pyodide release and WebAssembly for the in-browser re-run, the pinned QR reader and the pinned ZPL renderer; no inline handlers, no `eval`, no framing),
with `nosniff`, a referrer policy, and a permissions policy that allows only the page's own microphone (voice answers)
and camera (recording, label scanning). The Vercel front end holds only a read-only demo bundle, so its `/api/*` and
`/mcp` redirect (307) to the Modal backend: one source of truth for receipts, timestamp proofs and edits.

## Testing

`python tests/e2e.py` drives a real Chrome through the landing page, every sample run, confirm → simulate →
replay, every download (the receipt hash must equal the SHA-256 of `protocol.py`) and the security edges
(bad answers, path traversal, script in the URL, wrong upload code, non-audio voice), edit copies (answers
and fixes on a sample land in a private copy, the sample is unchanged, the copy's link reopens it), the
Protocol Designer round trip on designer.opentrons.com (import, then its own export through `opentrons analyze`), the CSP (headers, every
script carries the nonce, nothing the pages need is blocked), and the MCP server through the official MCP client
(`get_protocol` must be byte for byte the downloaded file), the bench card (every drawing served, the receipt hashes
`card.json`, a shared sample can't be redrawn, Enter on a drawing seeks the video), the `.eln` (one root folder, ELN
1.2+20260923, every file attached and hashed, the body's steps and checks) and the volume cross-check (quotes present,
the premix share agrees, a half-confirmed run is refused): 173/173 against production on Nov 2. A live upload, the way a judge would
try it (Nov 3: the first 100 s of a BioVL2 PCR video with the access code), ran every stage on production in 3 min 11 s,
from the bench sheet to the reviewer agent, and its bench card was drawn live in 105 s. That upload also exposed an agent gap: with no legible bench sheet and
unnamed tubes, the reviewer returned an empty review without calling a tool. It is now told, and reminded in its final
turn, to look at unnamed reagents' tubes and to report volumes the video can't settle; on the same upload it made 8
tool calls and reported 4 specific findings (which seconds it looked at, the orange-cap 15 mL tube whose label is too
blurry, the pipette display that is never legible). `BASE=<url>` runs it
against a deployment; `VOICE=1` also speaks answers through a fake microphone. Self-checks:
`python -m sdc.protocol`, `python -m sdc.designer`, `python -m sdc.stamp`, `python -m sdc.evaluate`, `python -m sdc.export`, `python -m sdc.agent`.
Accessibility runs inside the same suite: axe-core checks the landing page and the app in light and dark mode
(no serious or critical violations); Lighthouse 12 in a clean headless Chrome (Nov 2, from Indonesia) scores both
pages 100 for accessibility, best practices and SEO; performance is 100 desktop / 88 mobile on the landing page and
93 desktop / 84 mobile on the app (a sample's poster and video come from the CDN, the run data from Modal us-east). Layout shift is measured while runs load at phone and desktop widths and must stay under 0.1 (it was 0.47
on phones before panels were revealed together and the video got a reserved 16:9 box; now 0, app performance 75 → 94).
Speech that Muse Voice Transcribe heard in a video is served as WebVTT captions under the video (WCAG 1.2.2); the
e2e loads the cues in the browser and checks that a silent video gets no empty track. `modal run scripts/failover_drill.py` runs a real review on Modal with a broken Baseten key: on Oct 28 Baseten
answered 401, the review moved to Fireworks and finished there (726 s, frames only, simulator called twice).

## Evaluation

Predicted steps are matched to BioVL2's human step annotations by temporal IoU and content, after grouping
back-to-back transfers of one reagent into one protocol step (the annotations are per reagent; the robot needs
one transfer per tube). The same protocol is performed in different orders across videos (Template before or
after Premix), so reciting a protocol does not score; the model has to watch. Non-transfer steps (centrifuge,
invert, incubate) match on the action itself rather than on wording.

Prompts were developed on `pcr_1` only. Every other video is held out and was run once per pipeline version.
`miniprep_7` is a different protocol entirely (plasmid miniprep: lysis, neutralisation, spin columns).
Regenerate this table with `python scripts/eval_table.py pcr_1 pcr_2 pcr_3 pcr_4 pcr_5 miniprep_7`.

| Video | Stage 1 only: recall / precision / tIoU | **Stage 1 + 2**: recall / precision / tIoU |
|---|---|---|
| pcr_1 (dev) | 1.00 / 1.00 / 0.87 | 1.00 / 1.00 / 0.87 |
| pcr_2 (test) | 0.83 / 0.50 / 0.51 | 0.83 / 0.62 / 0.72 |
| pcr_3 (test) | 0.67 / 0.67 / 0.65 | 1.00 / 1.00 / 0.67 |
| pcr_4 (test) | 0.33 / 0.25 / 0.52 | 0.67 / 0.57 / 0.56 |
| pcr_5 (test) | 0.67 / 0.50 / 0.57 | 0.67 / 0.57 / 0.68 |
| **PCR test mean (n=4)** | 0.62 / 0.48 | **0.79 / 0.69** |
| miniprep_7 (test, unseen protocol) | 0.28 / 0.42 / 0.82 | 0.28 / 0.42 / 0.82 |

Evaluator change made after seeing test data, disclosed: `pcr_5` wrote water as "H2O", which the synonym list
did not know; it was added. Its "PrimeTime mix" was *not* accepted as the PrimeSTAR premix, because it is a
different product (a hallucinated name).

Why Muse Spark watches and not Muse Wonder: on Oct 29 we ran stage 1 with Meta's newer Muse Wonder 1.0 on the same
10 fps video file with the same prompt, hints and scorer (`python scripts/wonder_bench.py pcr_1 pcr_2 pcr_3 pcr_4 pcr_5 miniprep_7`).

| Video | Muse Spark 1.3, stage 1: recall / precision / tIoU | Muse Wonder 1.0, stage 1 |
|---|---|---|
| pcr_1 (dev) | 1.00 / 1.00 / 0.87 | 0.17 / 0.17 / 0.60 |
| pcr_2 | 0.83 / 0.50 / 0.51 | 0.33 / 0.22 / 0.78 |
| pcr_3 | 0.67 / 0.67 / 0.65 | 0.17 / 0.14 / 0.19 |
| pcr_4 | 0.33 / 0.25 / 0.52 | 0.83 / 0.45 / 0.60 |
| pcr_5 | 0.67 / 0.50 / 0.57 | 0.33 / 0.33 / 0.65 |
| **PCR test mean (n=4)** | **0.62 / 0.48** | 0.42 / 0.29 |
| miniprep_7 | 0.28 / 0.42 / 0.82 | 0.22 / 0.36 / 0.52 |

Wonder spent about 32k reasoning tokens per video and usually found the events, but named reagents from the order
on the bench sheet or a textbook PCR rather than from the tube in hand (on `pcr_1` it laid the sheet's order over
evenly spaced 7-second slots; without the sheet hint it put the premix second, where the primers really go: 0.33).
It did beat Spark on `pcr_4`, the video Spark handles worst. One run per video, prompts tuned on Spark, so this
favours Spark; it is why the pipeline keeps Spark for watching.

Known failures:
- `pcr_4`: stage 1 invents a "master mix" preparation that never happens. Stage 2 renames events but cannot
  delete them, so the reviewer agent and the scientist are the last line of defence.
- `pcr_5`: the structure is right (2 tubes, thermocycler on the exact seconds), but the model pattern-matches to
  qPCR: it invents a "probe" and calls the premix "PrimeTime mix", and the primer labels shift by one group.
- `miniprep_7`: the timing is right where steps match (tIoU 0.82, the centrifuge step lands on the exact
  seconds), but no reagent name is readable, so "Add 200 µl of Resuspension Solution" comes out as "transfer
  liquid from source microtube", and 3-second "invert 4 times" steps are missed. Naming the tubes by
  click-to-track is the intended fix for this.

## Run it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements.txt remotezip PyLabRobot==0.2.2 playwright roc-validator mcp==2.3.0
printf 'MODEL_API_KEY=...\nBASETEN_API_KEY=...\n' > .env
.venv/Scripts/python scripts/fetch_biovl2.py                              # annotations + pcr_1..4 videos only
.venv/Scripts/python -m sdc.pipeline data/BioVL2/videos/pcr/pcr_1.mp4     # all models + simulator
.venv/Scripts/python app.py                                               # http://127.0.0.1:8000
```

Every model call is cached as JSON in `out/<run>/`, so re-runs and UI edits never re-bill the API.
`--from-gt <csv>` replays the human annotations through the same pipeline without calling any model.


## Data and attribution

The code is open source under the [MIT License](LICENSE). The lab videos and everything derived from them (sample
runs, bench card drawings, the Zenodo RO-Crate) keep BioVL2's CC BY 4.0 licence, as below.

Videos and step annotations come from the **BioVL2 dataset** (Nishimura et al., Kyoto University),
licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
[zenodo.org/records/21495188](https://zenodo.org/records/21495188). The data is not redistributed here.
`scripts/fetch_biovl2.py` downloads only the files needed from Zenodo. Videos are re-encoded (downscaled)
for model input and display.
