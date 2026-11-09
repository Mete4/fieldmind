"""Skip ahead 30 minutes: replaces the live data with the state that
scripts/build_skip_ahead.sh made with the real pipeline, and shifts its times
so that the batch calls spread over the last 30 minutes and the newest is now."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config as C

SKIP_AHEAD_PATH = C.SAMPLE_DIR / "skip_ahead.sqlite"
SKIP_SECONDS = 30 * 60
TIME_COLUMNS = ("ts", "timestamp", "last_ts", "answered_ts", "created_ts", "done_ts", "checked_at")


def _parse(value: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def is_skip_ahead_ready() -> bool:
    return SKIP_AHEAD_PATH.exists()


def load_skip_ahead(now: datetime | None = None) -> int:
    """Replaces every table of the live database with the skip-ahead state. Returns the incident count."""
    now = now or datetime.now(timezone.utc)
    con = sqlite3.connect(C.DB_PATH, timeout=30)
    try:
        con.execute("ATTACH DATABASE ? AS snap", (str(SKIP_AHEAD_PATH),))
        tables = [r[0] for r in con.execute("SELECT name FROM snap.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        live = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
        starts = [t for (v,) in con.execute("SELECT ts FROM snap.reports WHERE input_type='text'") if (t := _parse(v))]
        ends = [t for tab in ("reports", "alerts", "open_questions") if tab in tables
                for (v,) in con.execute(f"SELECT ts FROM snap.{tab}") if (t := _parse(v))]
        batch_start = min(starts) if starts else min(ends)
        end = max(ends)
        stretch = SKIP_SECONDS / max((end - batch_start).total_seconds(), 1.0)

        def shift(t: datetime) -> datetime:
            if t >= batch_start:
                return now - (end - t) * stretch
            return now - timedelta(seconds=SKIP_SECONDS) - (batch_start - t)

        with con:
            for tab in tables:
                if tab not in live:
                    continue
                snap_cols = [r[1] for r in con.execute(f"PRAGMA snap.table_info({tab})")]
                live_cols = {r[1] for r in con.execute(f"PRAGMA main.table_info({tab})")}
                cols = [c for c in snap_cols if c in live_cols]
                col_list = ", ".join(cols)
                con.execute(f"DELETE FROM main.{tab}")
                con.execute(f"INSERT INTO main.{tab} ({col_list}) SELECT {col_list} FROM snap.{tab}")
                for col in (c for c in cols if c in TIME_COLUMNS):
                    rows = con.execute(f"SELECT rowid, {col} FROM main.{tab} WHERE {col} IS NOT NULL").fetchall()
                    for rowid, value in rows:
                        t = _parse(value)
                        if t:
                            con.execute(f"UPDATE main.{tab} SET {col}=? WHERE rowid=?",
                                        (shift(t).isoformat(timespec="seconds"), rowid))
                if "data" in cols:   # the console reads the stored record, so its times move too
                    for rowid, value in con.execute(f"SELECT rowid, data FROM main.{tab} WHERE data IS NOT NULL").fetchall():
                        try:
                            record = json.loads(value)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        for key in TIME_COLUMNS:
                            t = _parse(record.get(key)) if isinstance(record.get(key), str) else None
                            if t:
                                record[key] = shift(t).isoformat(timespec="seconds")
                        con.execute(f"UPDATE main.{tab} SET data=? WHERE rowid=?", (json.dumps(record), rowid))
        count = con.execute("SELECT COUNT(*) FROM main.incidents").fetchone()[0]
    finally:
        con.close()
    return count
