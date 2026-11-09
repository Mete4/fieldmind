#!/usr/bin/env python3
"""Make Piper audio for the calls in an existing radio JSON (text is NOT
changed) and write the 'audio' field back.  .venv/bin/python scripts/tts_from_json.py data/samples/radio/camp_live.json"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from generate_radio import voice_calls  # noqa: E402

path = Path(sys.argv[1])
data = json.loads(path.read_text())
calls = data["calls"] if isinstance(data, dict) else data
t0 = time.time()
n = voice_calls(calls, path.parent / path.stem)
path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
print(f"{n} clips voiced in {time.time() - t0:.0f}s -> {path.parent / path.stem}/ ; audio field written to {path.name}")
