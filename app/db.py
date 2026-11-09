"""SQLite persistence: incidents, cloud_queue, requests (every model/API
call), metrics, summaries. Plain sqlite3 with WAL; one short-lived
connection per call so it is safe from threads and from the async API."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app import config as C
from app.schemas import Incident, Report, RequestLog, now_iso

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY, ts TEXT, channel TEXT, input_type TEXT, status TEXT,
  incident_id TEXT, link_action TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reports_incident ON reports(incident_id);
CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(ts DESC);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, timestamp TEXT, last_ts TEXT, channel TEXT, type TEXT, severity INTEGER,
  confidence REAL, priority_score REAL, in_hazard_zone INTEGER, status TEXT, source TEXT,
  report_count INTEGER, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_incidents_prio ON incidents(priority_score DESC, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_link ON incidents(type, last_ts DESC);
CREATE TABLE IF NOT EXISTS cloud_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, created_ts TEXT,
  attempts INTEGER DEFAULT 0, last_error TEXT, done INTEGER DEFAULT 0, done_ts TEXT);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, route TEXT, model TEXT, latency_ms INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, bytes_out INTEGER, ok INTEGER, error TEXT, incident_id TEXT);
CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, model TEXT, latency_ms INTEGER,
  text TEXT, incident_count INTEGER);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(C.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    C.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        cols = {r[1] for r in c.execute("PRAGMA table_info(incidents)").fetchall()}
        if cols and "last_ts" not in cols:       # pre-split schema: demo data, rebuild
            c.execute("DROP TABLE incidents")
        c.executescript(SCHEMA)


def reset_db() -> None:
    """Demo reset: wipe every table (keeps the schema)."""
    with _write_lock, connect() as c:
        for t in ("reports", "incidents", "cloud_queue", "requests", "metrics", "summaries"):
            c.execute(f"DELETE FROM {t}")


# ------------------------------------------------------------------ incidents
def save_incident(inc: Incident) -> Incident:
    with _write_lock, connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO incidents (id,timestamp,last_ts,channel,type,severity,confidence,priority_score,"
            "in_hazard_zone,status,source,report_count,data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (inc.id, inc.timestamp, inc.last_ts, inc.channel, inc.type, inc.severity, inc.confidence, inc.priority_score,
             int(inc.in_hazard_zone), inc.status, inc.source, inc.report_count, inc.model_dump_json()),
        )
    return inc


def candidates_for_link(incident_type: Optional[str], since_iso: str, limit: int = 200) -> list[Incident]:
    """Open incidents a new report could join: same type (if given) and a
    report within the time window. Distance is checked by the caller."""
    with connect() as c:
        if incident_type:
            rows = c.execute("SELECT data FROM incidents WHERE type=? AND last_ts>=? ORDER BY last_ts DESC LIMIT ?",
                             (incident_type, since_iso, limit)).fetchall()
        else:
            rows = c.execute("SELECT data FROM incidents WHERE last_ts>=? ORDER BY last_ts DESC LIMIT ?", (since_iso, limit)).fetchall()
    return [Incident.model_validate_json(r["data"]) for r in rows]


# ------------------------------------------------------------------ reports (raw inputs)
def save_report(rep: Report) -> Report:
    with _write_lock, connect() as c:
        c.execute("INSERT OR REPLACE INTO reports (id,ts,channel,input_type,status,incident_id,link_action,data) VALUES (?,?,?,?,?,?,?,?)",
                  (rep.id, rep.ts, rep.channel, rep.input_type, rep.status, rep.incident_id, rep.link_action, rep.model_dump_json()))
    return rep


def get_report(report_id: str) -> Optional[Report]:
    with connect() as c:
        row = c.execute("SELECT data FROM reports WHERE id=?", (report_id,)).fetchone()
    return Report.model_validate_json(row["data"]) if row else None


def list_reports(limit: int = 500, incident_id: Optional[str] = None) -> list[Report]:
    with connect() as c:
        if incident_id:
            rows = c.execute("SELECT data FROM reports WHERE incident_id=? ORDER BY ts LIMIT ?", (incident_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT data FROM reports ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [Report.model_validate_json(r["data"]) for r in rows]


def report_count() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]


def get_incident(incident_id: str) -> Optional[Incident]:
    with connect() as c:
        row = c.execute("SELECT data FROM incidents WHERE id=?", (incident_id,)).fetchone()
    return Incident.model_validate_json(row["data"]) if row else None


def update_incident(incident_id: str, **fields: Any) -> Optional[Incident]:
    inc = get_incident(incident_id)
    if inc is None:
        return None
    inc = inc.model_copy(update=fields)
    return save_incident(inc)


def list_incidents(limit: int = 500, order: str = "priority") -> list[Incident]:
    order_sql = "priority_score DESC, timestamp DESC" if order == "priority" else "timestamp DESC"
    with connect() as c:
        rows = c.execute(f"SELECT data FROM incidents ORDER BY {order_sql} LIMIT ?", (limit,)).fetchall()
    return [Incident.model_validate_json(r["data"]) for r in rows]


def incident_count() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]


# ------------------------------------------------------------------ cloud queue
def enqueue_cloud(incident_id: str) -> int:
    with _write_lock, connect() as c:
        row = c.execute("SELECT id FROM cloud_queue WHERE incident_id=? AND done=0", (incident_id,)).fetchone()
        if row:
            return row["id"]
        cur = c.execute("INSERT INTO cloud_queue (incident_id, created_ts) VALUES (?,?)", (incident_id, now_iso()))
        return cur.lastrowid


def pending_queue(limit: int = 100) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM cloud_queue WHERE done=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def queue_size() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM cloud_queue WHERE done=0").fetchone()[0]


def mark_queue_item(queue_id: int, done: bool, error: Optional[str] = None) -> None:
    with _write_lock, connect() as c:
        if done:
            c.execute("UPDATE cloud_queue SET done=1, done_ts=?, last_error=NULL WHERE id=?", (now_iso(), queue_id))
        else:
            c.execute("UPDATE cloud_queue SET attempts=attempts+1, last_error=? WHERE id=?", (error, queue_id))


# ------------------------------------------------------------------ request log + metrics
def log_request(entry: RequestLog) -> None:
    try:
        with _write_lock, connect() as c:
            c.execute(
                "INSERT INTO requests (ts,route,model,latency_ms,tokens_in,tokens_out,bytes_out,ok,error,incident_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (entry.ts, entry.route, entry.model, entry.latency_ms, entry.tokens_in, entry.tokens_out,
                 entry.bytes_out, int(entry.ok), entry.error, entry.incident_id),
            )
    except Exception:  # noqa: BLE001  logging must never break the pipeline
        pass


def request_stats() -> dict[str, dict]:
    """Per route: count, avg/p50/p95 latency, tokens, bytes sent off-device."""
    with connect() as c:
        rows = c.execute("SELECT route, latency_ms, tokens_in, tokens_out, bytes_out, ok FROM requests").fetchall()
    out: dict[str, dict] = {}
    by_route: dict[str, list[int]] = {}
    for r in rows:
        s = out.setdefault(r["route"], {"count": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "bytes_out": 0, "avg_ms": 0})
        s["count"] += 1
        s["errors"] += 0 if r["ok"] else 1
        s["tokens_in"] += r["tokens_in"] or 0
        s["tokens_out"] += r["tokens_out"] or 0
        s["bytes_out"] += r["bytes_out"] or 0
        by_route.setdefault(r["route"], []).append(r["latency_ms"] or 0)
    for route, lat in by_route.items():
        lat.sort()
        out[route]["avg_ms"] = int(sum(lat) / len(lat))
        out[route]["p50_ms"] = lat[len(lat) // 2]
        out[route]["p95_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    return out


def recent_requests(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_metric(key: str, value: Any) -> None:
    with _write_lock, connect() as c:
        c.execute("INSERT OR REPLACE INTO metrics (key,value,ts) VALUES (?,?,?)", (key, json.dumps(value), now_iso()))


def get_metric(key: str, default: Any = None) -> Any:
    with connect() as c:
        row = c.execute("SELECT value FROM metrics WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def all_metrics() -> dict[str, Any]:
    with connect() as c:
        rows = c.execute("SELECT key, value FROM metrics").fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}


# ------------------------------------------------------------------ summaries
def save_summary(source: str, model: str, latency_ms: int, text: str, incident_count: int) -> None:
    with _write_lock, connect() as c:
        c.execute("INSERT INTO summaries (ts,source,model,latency_ms,text,incident_count) VALUES (?,?,?,?,?,?)",
                  (now_iso(), source, model, latency_ms, text, incident_count))


def latest_summary() -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM summaries ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
