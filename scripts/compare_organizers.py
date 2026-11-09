"""Run one organizer model on every scenario call and compare its fields with the fields that
the Skip ahead state stored. Writes only to its own database (FIELDMIND_DB is set before import).

  .venv/bin/python scripts/compare_organizers.py qwen3.6:35b-a3b"""
import json
import os
import sqlite3
import sys
from pathlib import Path

os.environ["FIELDMIND_DB"] = str(Path("data/organizer_compare.sqlite").resolve())
sys.path.insert(0, ".")
from app import db, extract  # noqa: E402

model = sys.argv[1]
db.init_db()
reference = sqlite3.connect("data/samples/skip_ahead.sqlite")
reference_by_text = {}
for text, type_, severity, people, place in reference.execute(
        "SELECT text, type, severity, people_count, location_text FROM reports"):
    reference_by_text[(text or "").strip()[:80]] = (type_, severity, people, place)

calls = []
for name in ("camp_live.json", "camp_batch.json"):
    calls += json.loads(Path("data/samples/radio", name).read_text())["calls"]

latencies_ms, failed_count, type_match_count, severity_match_count, compared_count = [], 0, 0, 0, 0
for call in calls:
    ext, meta = extract.llm_extract(call["text"], model=model)
    latencies_ms.append(meta["latency_ms"])
    failed_count += bool(meta["fallback"])
    ref = reference_by_text.get(call["text"].strip()[:80])
    mark = ""
    if ref:
        compared_count += 1
        type_match_count += ext.type == ref[0]
        severity_match_count += ext.severity == ref[1]
        if ext.type != ref[0] or ext.severity != ref[1]:
            mark = f"  | stored: {ref[0]} sev {ref[1]} ppl {ref[2]} @ {ref[3]}"
    print(f"{meta['latency_ms']:6d} ms {call['unit']:<16} {ext.type:<12} sev {ext.severity} "
          f"ppl {ext.people_count} @ {ext.location_text}{' FAILED' if meta['fallback'] else ''}{mark}")

latencies_ms.sort()
print(f"\n{model}: {len(calls)} calls, {failed_count} failed, "
      f"median {latencies_ms[len(latencies_ms) // 2]} ms, max {latencies_ms[-1]} ms; "
      f"same type as stored {type_match_count}/{compared_count}, same severity {severity_match_count}/{compared_count}")
