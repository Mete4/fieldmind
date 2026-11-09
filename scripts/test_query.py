#!/usr/bin/env python3
"""Section 4 test: the three required operator questions through app/query.py."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402
from app.query import answer  # noqa: E402

QUESTIONS = sys.argv[1:] or ["Which incidents have people trapped inside?",
                             "What did FD units report about XYZ High School in the last 30 minutes?",
                             "List large fires coming from the north with more than 50 people."]
db.init_db()
for q in QUESTIONS:
    r = answer(q)
    print(f"\nQ: {q}\nmodel {r['model']} | {r['latency_ms']} ms | attempts {r['attempts']} | error: {r['error']}\nSQL: {r['sql']}")
    print(f"rows: {len(r['rows'])} | report_ids {r['report_ids'][:5]} | incident_ids {r['incident_ids'][:5]}")
    for row in r["rows"][:4]:
        print("  ", json.dumps(row)[:160])
    print(f"ANSWER: {r['answer']}")
