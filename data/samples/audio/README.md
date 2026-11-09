# Sample audio (optional)

Record each report in `../reports.txt` as a radio-style voice clip and save it
here with the report number as the file name: `1.wav`, `2.wav`, ... `15.wav`
(`.mp3`, `.m4a`, `.flac` and `.ogg` work too). Any sample rate is fine; mono
16 kHz is ideal. Read them like real radio traffic: unit ID first, a little
noise and hesitation is good, reports 12-15 should sound garbled or vague.

Nothing in the demo requires these files: `scripts/simulate.py`, the Streamlit
"Demo mode" button and `scripts/benchmark.py` all fall back to the text of
`reports.txt` when an audio file is missing. When a file exists, it is sent to
`POST /ingest/audio` and transcribed on the GPU by Whisper large-v3 instead.

Note: the `<n>.mp3` files present here were generated once (online) by
`scripts/make_sample_audio.py` with Microsoft Edge neural voices as stand-ins
for real recordings. Overwrite them with your own recordings any time.
