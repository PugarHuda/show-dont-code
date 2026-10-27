# Show, Don't Code

Film yourself doing a lab protocol once by hand, and get an Opentrons robot protocol that runs it.

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026), Push the Frontier theme. All code in
this repository was written during the hackathon. The git history starts on Oct 27, 2026.

## How it works

Four Meta models, each doing the part it is best at:

| Model | Job |
|---|---|
| **Muse Spark 1.3** (Meta Model API) | Stage 1: watches the whole video and finds every step with timestamps (strict JSON). Reads the printed protocol sheet on the bench from full-resolution stills. Stage 2: looks closely at the aspiration moment of every transfer, groups transfers by physical source tube, and names each group. |
| **SAM 3.1** (Meta Model API) | Tracks the pipette tip. **Click-to-track**: the scientist clicks a tube in the video, and a point prompt follows it through the whole recording. |
| **Muse Voice Transcribe** (Meta Model API) | Turns anything said aloud ("fifty microlitres of primer") into timestamped evidence for Muse Spark, with reagent names as keyword bias. |
| **Muse Spark Open** (open weights, served by Baseten) | Reviewer agent. Watches the video, checks the steps against it and the bench sheet, and **runs the Opentrons simulator as a tool** to work out a volume plan that hits the sheet's per-reaction total. |

Then:

1. A **deterministic code generator** turns the steps into an Opentrons Flex protocol (Protocol API 2.20). The
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

## Evaluation

Predicted steps are matched to BioVL2's human step annotations by temporal IoU and content, after grouping
back-to-back transfers of one reagent into one protocol step (the annotations are per reagent; the robot needs
one transfer per tube). The same protocol is performed in different orders across videos (Template before or
after Premix), so reciting a protocol does not score; the model has to watch.

Prompts were developed on `pcr_1` only. `pcr_2`–`pcr_4` are held out and were each run once per pipeline version.

| Video | Stage 1 only: recall / precision / tIoU | **Stage 1 + 2**: recall / precision / tIoU |
|---|---|---|
| pcr_1 (dev) | 1.00 / 1.00 / 0.87 | 1.00 / 1.00 / 0.87 |
| pcr_2 (test) | 0.83 / 0.50 / 0.51 | 0.83 / 0.62 / 0.72 |
| pcr_3 (test) | 0.67 / 0.67 / 0.65 | 1.00 / 1.00 / 0.67 |
| pcr_4 (test) | 0.33 / 0.25 / 0.52 | 0.67 / 0.57 / 0.56 |
| **test mean** | 0.61 / 0.47 | **0.83 / 0.73** |

Known failure: in `pcr_4`, stage 1 invents a "master mix" preparation that never happens. Stage 2 renames
events but cannot delete them, so the reviewer agent and the scientist are the last line of defence.

## Run it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv openai meta-sam-parser opentrons fastapi uvicorn python-multipart python-dotenv remotezip opencv-python-headless requests
printf 'MODEL_API_KEY=...\nBASETEN_API_KEY=...\n' > .env
.venv/Scripts/python scripts/fetch_biovl2.py                              # annotations + pcr_1..4 videos only
.venv/Scripts/python -m sdc.pipeline data/BioVL2/videos/pcr/pcr_1.mp4     # all models + simulator
.venv/Scripts/python app.py                                               # http://127.0.0.1:8000
```

Every model call is cached as JSON in `out/<run>/`, so re-runs and UI edits never re-bill the API.
`--from-gt <csv>` replays the human annotations through the same pipeline without calling any model.

Self-checks: `python -m sdc.protocol` and `python -m sdc.evaluate`.

## Data and attribution

Videos and step annotations come from the **BioVL2 dataset** (Nishimura et al., Kyoto University),
licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
[zenodo.org/records/21495188](https://zenodo.org/records/21495188). The data is not redistributed here.
`scripts/fetch_biovl2.py` downloads only the files needed from Zenodo. Videos are re-encoded (downscaled)
for model input and display.
