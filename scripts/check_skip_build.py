"""Exit 0 when a Skip ahead build database is complete: every scenario call is stored and no
extraction failed. Prints the counts."""
import json
import sqlite3
import sys
from pathlib import Path

db_path = sys.argv[1]
expected = sum(len(json.loads(Path(p).read_text())["calls"]) for p in sys.argv[2:])
con = sqlite3.connect(db_path)
call_count = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
failed_count = con.execute(
    "SELECT COUNT(*) FROM reports WHERE review_reasons LIKE '%extraction failed%'").fetchone()[0]
con.close()
print(f"{call_count} of {expected} calls stored, {failed_count} failed extractions")
sys.exit(0 if call_count >= expected and failed_count == 0 else 1)
