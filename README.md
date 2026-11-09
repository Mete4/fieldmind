# FieldMind

**The edge listens to every channel. The cloud thinks over the whole fire. The operator decides.**

A hybrid AI command console for wildfire radio traffic, built at a hackathon on a Lenovo
ThinkStation PGX (NVIDIA GB10 Grace Blackwell, 128 GB unified memory). Simulated scenario on
real geography: Paradise, California, the 2018 Camp Fire, real OpenStreetMap streets and real
CAL FIRE hazard zones; the radio traffic is written for the exercise.

## Hybrid design

- **Edge (the PGX, every call, in seconds):** Whisper large-v3 on the GPU turns radio audio into
  text as it plays; a local LLM (qwen2.5) turns each call into fields (type, severity, people,
  part of the structure, fire direction and size, unit, department); calls are linked into
  incidents by place and time; an orchestrator raises alerts by rules; SQL questions run on
  DuckDB with Flock LLM functions over the same store. Audio never leaves the device.
- **Cloud (a large model, every few minutes or on request):** reads the whole picture over all
  incidents and returns priorities, suggestions, contradictions and open questions, each
  pointing to an incident ID. In the demo this is a recorded response, labelled as such.
- **The link:** the edge sends only the structured incident state (rows, a few KB, no audio);
  the cloud sends back items that point to incident IDs; the operator accepts or rejects each
  one, and every decision is logged. If the link drops, the edge continues and the cloud
  catches up when the link returns.

## Demo (about 3 minutes)

1. **Reset**: empty map of Paradise.
2. **Play radio**: the part-1 calls play (several channels at once), each transcribed on the
   GPU; pins appear; the banner shows FIRE-001 (Paradise High School, 123 in the gym).
3. **Open FIRE-001**: key facts, then the Engine 81 and Battalion 3 calls linked into one
   incident, with audio. An unclear call shows as an open question, not as a guess.
4. **Skip ahead 30 min**: 22 batch calls as text; the queue re-sorts; TRAP-001 (burnover,
   Edgewood Lane) tops the banner. "Ask the data": *Which incidents have people trapped and no
   unit assigned?* returns rows; the local model wrote the SQL.
5. **Ask cloud agent**: the recorded cloud response with priorities and suggestions linked to
   incident IDs; Accept writes to the decisions table.

## Run

    ./run.sh          # Ollama + models, tile server, API (8000), dashboard (8501), watchdog
    .venv/bin/python scripts/test_extract.py --reset     # pipeline test on the sample reports
    .venv/bin/python scripts/play_radio.py data/samples/radio/camp_live.json --stream

See `data/hazards/README.md` for data sources and licenses, `logs/changes_progress.md` for the
build log. Measured numbers live on the Hardware tab and in `~/agihouse/AGENT_SYNC.md`.
