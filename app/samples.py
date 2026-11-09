"""Reader for data/samples/reports.txt (simulated radio traffic).
Format: lines starting with "## <n> | <channel>" open a report; the following
lines up to the next "## " are its transcript. Lines starting with "# " are
comments."""
from __future__ import annotations

import re
from pathlib import Path

from app import config as C

_HEAD = re.compile(r"^##\s*(\d+)\s*\|\s*(.+?)\s*$")


def load_reports(path: Path | None = None) -> list[dict]:
    path = path or (C.SAMPLE_DIR / "reports.txt")
    if not path.exists():
        return []
    reports: list[dict] = []
    cur: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        m = _HEAD.match(line)
        if m:
            cur = {"id": int(m.group(1)), "channel": m.group(2), "text": ""}
            reports.append(cur)
            continue
        if line.startswith("# ") or line == "#" or cur is None:
            continue
        cur["text"] = (cur["text"] + " " + line.strip()).strip() if line.strip() else cur["text"]
    return [r for r in reports if r["text"]]
