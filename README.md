# Show, Don't Code

Film yourself doing a lab protocol once by hand, and get an Opentrons robot protocol that runs it.

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026), Push the Frontier theme. All code in
this repository was written during the hackathon. The git history starts on Oct 27, 2026.

## How it works

1. **Muse Spark** (Meta Model API) watches the video and lists every protocol step: action, reagent, source,
   destination, volume and timestamps, as strict JSON.
2. **SAM 3.1** tracks the pipette tip across the video, so each step comes with visual evidence. The UI draws
   the tracked tip over the video.
3. A **deterministic code generator** turns the steps into an Opentrons Flex protocol (Protocol API 2.20). The
   model decides *what* happened; plain code decides *how* the robot does it, so model output never becomes
   executable code. Steps a robot can't do (centrifuge, invert) become `ctx.pause()` hand-offs. "Set in DNA
   engine" becomes the Thermocycler module.
4. The protocol runs in the **official Opentrons simulator**. Anything the video can't show, like a volume
   nobody said out loud, goes back to the scientist as a question before any code is produced.
5. A **reviewer agent on Muse Spark Open** (Meta open weights, served by Baseten) cross-checks the extracted
   steps against the bench protocol sheet (which Muse Spark reads from full-resolution stills) and the simulator.
   It flags what a careful lab member would catch: reagents missing from the sheet, one reagent drawn from two
   tubes, reactions that would get an addition twice, and volumes that cannot add up. Every finding becomes a
   question for the scientist. The agent never edits the protocol itself.
6. The **review UI** plays the human video next to the robot deck, step by step. It asks one volume question
   per reagent and source tube, not one per transfer.

## Run it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv openai meta-sam-parser opentrons fastapi uvicorn python-multipart python-dotenv remotezip
printf "MODEL_API_KEY=...
BASETEN_API_KEY=...
" > .env
.venv/Scripts/python scripts/fetch_biovl2.py                              # annotations + pcr_1..4 videos only
.venv/Scripts/python -m sdc.pipeline data/BioVL2/videos/pcr/pcr_1.mp4     # Muse Spark + SAM 3.1 + simulator
.venv/Scripts/python app.py                                               # http://127.0.0.1:8000
```

`--from-gt <csv>` replays the human annotations through the same pipeline without calling the API. It
produces an oracle baseline and lets you test the UI.

Self-checks: `python -m sdc.protocol` and `python -m sdc.evaluate`.

## Evaluation

Predicted steps are matched to BioVL2's human step annotations by temporal IoU and content. We report step
recall, precision, mean tIoU and ordering. In BioVL2 the same protocol is performed in different orders (for
example, Template before or after Premix), so the model has to watch the video. Reciting the protocol isn't enough.

## Data and attribution

Videos and step annotations come from the **BioVL2 dataset** (Nishimura et al., Kyoto University),
licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
[zenodo.org/records/21495188](https://zenodo.org/records/21495188). The data is not redistributed here.
`scripts/fetch_biovl2.py` downloads only the files needed from Zenodo. Videos are re-encoded (downscaled)
for model input and display.
