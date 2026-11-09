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
CREATE TABLE IF NOT EXISTS situations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, model TEXT, latency_ms INTEGER,
  data_as_of TEXT, situation TEXT, payload TEXT, incident_count INTEGER, trigger TEXT);
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, text TEXT, reason TEXT, report_ids TEXT,
  data_as_of TEXT, status TEXT DEFAULT 'open', decided_ts TEXT, situation_id INTEGER);
CREATE TABLE IF NOT EXISTS units (
  id TEXT PRIMARY KEY, name TEXT, department TEXT, status TEXT DEFAULT 'available', incident_id TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS open_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, incident_id TEXT, text TEXT, source TEXT, answer TEXT,
  status TEXT DEFAULT 'open', answered_ts TEXT, situation_id INTEGER);
CREATE TABLE IF NOT EXISTS info_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, incident_id TEXT, unit_id TEXT, what_to_find TEXT,
  status TEXT DEFAULT 'sent', answer TEXT, answered_ts TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, target_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, incident_id TEXT, label TEXT, level TEXT, headline TEXT, why TEXT,
  reasons TEXT, trigger TEXT, score TEXT, status TEXT DEFAULT 'new', model TEXT, latency_ms INTEGER);
"""

SEED_UNITS = [("E81", "Engine 81", "FD"), ("E82", "Engine 82", "FD"), ("E83", "Engine 83", "FD"), ("E35", "Engine 35", "FD"),
              ("T82", "Truck 82", "FD"), ("B3", "Battalion 3", "FD"), ("WT26", "Water Tender 26", "FD"),
              ("M41", "Medic 41", "EMS"), ("M42", "Medic 42", "EMS"), ("M43", "Medic 43", "EMS"),
              ("PD7", "Paradise PD 7", "PD"), ("PD12", "Paradise PD 12", "PD"), ("SO4", "Sheriff 4", "PD")]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(C.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# query-friendly columns (section 3/4); added with ALTER TABLE so data survives
REPORT_COLS = {"text": "TEXT", "department": "TEXT", "unit": "TEXT", "type": "TEXT", "severity": "INTEGER",
               "people_count": "INTEGER", "location_text": "TEXT", "confidence": "REAL", "structure_part": "TEXT",
               "fire_direction": "TEXT", "fire_size": "TEXT", "details": "TEXT", "source_file": "TEXT"}
INCIDENT_COLS = {"label": "TEXT", "types": "TEXT", "location_text": "TEXT", "geocode_match": "TEXT", "lat": "REAL", "lon": "REAL", "people_count": "INTEGER",
                 "needs": "TEXT", "hazard_zones": "TEXT", "summary": "TEXT", "transcript": "TEXT", "department": "TEXT",
                 "unit": "TEXT", "structure_part": "TEXT", "fire_direction": "TEXT", "fire_size": "TEXT", "details": "TEXT",
                 "report_ids": "TEXT"}


def _ensure_columns(c: sqlite3.Connection, table: str, cols: dict[str, str]) -> None:
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, typ in cols.items():
        if name not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def init_db() -> None:
    C.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        cols = {r[1] for r in c.execute("PRAGMA table_info(incidents)").fetchall()}
        if cols and "last_ts" not in cols:       # pre-split schema: demo data, rebuild
            c.execute("DROP TABLE incidents")
        c.executescript(SCHEMA)
        _ensure_columns(c, "reports", REPORT_COLS)
        _ensure_columns(c, "incidents", INCIDENT_COLS)
        _ensure_columns(c, "cloud_queue", {"kind": "TEXT", "payload": "TEXT"})
        if c.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0:
            for uid, name, dept in SEED_UNITS:
                c.execute("INSERT INTO units (id,name,department,status,updated_ts) VALUES (?,?,?,?,?)", (uid, name, dept, "available", now_iso()))


def _upsert(c: sqlite3.Connection, table: str, row: dict) -> None:
    cols = ", ".join(row)
    c.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def reset_db() -> None:
    """Demo reset: wipe every table (keeps the schema)."""
    with _write_lock, connect() as c:
        for t in ("reports", "incidents", "cloud_queue", "requests", "metrics", "summaries", "situations", "suggestions",
                  "open_questions", "info_requests", "decisions", "alerts"):
            c.execute(f"DELETE FROM {t}")
        c.execute("UPDATE units SET status='available', incident_id=NULL, updated_ts=?", (now_iso(),))


# ------------------------------------------------------------------ incidents
def save_incident(inc: Incident) -> Incident:
    row = {"id": inc.id, "label": inc.label, "types": ",".join(inc.types), "timestamp": inc.timestamp, "last_ts": inc.last_ts, "channel": inc.channel, "type": inc.type,
           "severity": inc.severity, "confidence": inc.confidence, "priority_score": inc.priority_score,
           "in_hazard_zone": int(inc.in_hazard_zone), "status": inc.status, "source": inc.source, "report_count": inc.report_count,
           "location_text": inc.location_text, "geocode_match": inc.geocode_match, "lat": inc.lat, "lon": inc.lon,
           "people_count": inc.people_count, "needs": ",".join(inc.needs), "hazard_zones": ",".join(inc.hazard_zones),
           "summary": inc.summary, "transcript": inc.transcript, "department": ",".join(inc.department), "unit": ",".join(inc.unit),
           "structure_part": inc.structure_part, "fire_direction": inc.fire_direction, "fire_size": inc.fire_size,
           "details": inc.details, "report_ids": ",".join(inc.report_ids), "data": inc.model_dump_json()}
    with _write_lock, connect() as c:
        _upsert(c, "incidents", row)
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
    row = {"id": rep.id, "ts": rep.ts, "channel": rep.channel, "input_type": rep.input_type, "status": rep.status,
           "incident_id": rep.incident_id, "link_action": rep.link_action, "text": rep.text, "department": rep.department,
           "unit": rep.unit, "type": rep.type, "severity": rep.severity, "people_count": rep.people_count,
           "location_text": rep.location_text, "confidence": rep.confidence, "structure_part": rep.structure_part,
           "fire_direction": rep.fire_direction, "fire_size": rep.fire_size, "details": rep.details,
           "source_file": rep.source_file, "data": rep.model_dump_json()}
    with _write_lock, connect() as c:
        _upsert(c, "reports", row)
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


# ------------------------------------------------------------------ situation queue (one slot) + results
def queue_situation(trigger: str) -> None:
    """Keep only the newest pending situation request."""
    with _write_lock, connect() as c:
        c.execute("DELETE FROM cloud_queue WHERE kind='situation' AND done=0")
        c.execute("INSERT INTO cloud_queue (incident_id, created_ts, kind, payload) VALUES ('', ?, 'situation', ?)", (now_iso(), trigger))


def pending_situation() -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM cloud_queue WHERE kind='situation' AND done=0 ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def save_situation(source: str, model: str, latency_ms: int, data_as_of: str, payload: dict, incident_count: int, trigger: str) -> int:
    """Store a situation result; open suggestions/questions from the same
    source are replaced by the new set, decided ones are kept."""
    with _write_lock, connect() as c:
        cur = c.execute("INSERT INTO situations (ts,source,model,latency_ms,data_as_of,situation,payload,incident_count,trigger) VALUES (?,?,?,?,?,?,?,?,?)",
                        (now_iso(), source, model, latency_ms, data_as_of, payload.get("situation", ""), json.dumps(payload), incident_count, trigger))
        sid = cur.lastrowid
        c.execute("DELETE FROM suggestions WHERE status='open' AND source=?", (source,))
        for s in payload.get("suggestions", []) or []:
            c.execute("INSERT INTO suggestions (ts,source,text,reason,report_ids,data_as_of,status,situation_id) VALUES (?,?,?,?,?,?,'open',?)",
                      (now_iso(), source, str(s.get("text", ""))[:500], str(s.get("reason", ""))[:500], ",".join(map(str, s.get("report_ids", []) or [])), data_as_of, sid))
        c.execute("DELETE FROM open_questions WHERE status='open' AND source=?", (source,))
        for q in payload.get("open_questions", []) or []:
            c.execute("INSERT INTO open_questions (ts,incident_id,text,source,status,situation_id) VALUES (?,?,?,?,'open',?)",
                      (now_iso(), str(q.get("incident_id") or ""), str(q.get("text", ""))[:500], source, sid))
        return sid


def latest_situation() -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM situations ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def list_suggestions(status: Optional[str] = None) -> list[dict]:
    return _rows("SELECT * FROM suggestions WHERE (? IS NULL OR status=?) ORDER BY id DESC", (status, status))


def decide_suggestion(sid: int, action: str) -> Optional[dict]:
    status = "accepted" if action == "accept" else "rejected"
    with _write_lock, connect() as c:
        c.execute("UPDATE suggestions SET status=?, decided_ts=? WHERE id=?", (status, now_iso(), sid))
        row = c.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if row:
        log_decision("suggestion_" + status, str(sid), row["text"])
    return dict(row) if row else None


def list_units() -> list[dict]:
    return _rows("SELECT * FROM units ORDER BY department, name")


def assign_unit(unit_id: str, incident_id: Optional[str], status: str = "assigned") -> Optional[dict]:
    with _write_lock, connect() as c:
        c.execute("UPDATE units SET status=?, incident_id=?, updated_ts=? WHERE id=?", (status, incident_id, now_iso(), unit_id))
        row = c.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    if row:
        log_decision("unit_" + status, unit_id, f"incident {incident_id or '-'}")
    return dict(row) if row else None


def list_questions(status: Optional[str] = None) -> list[dict]:
    return _rows("SELECT * FROM open_questions WHERE (? IS NULL OR status=?) ORDER BY id DESC", (status, status))


def answer_question(qid: int, answer: str) -> Optional[dict]:
    with _write_lock, connect() as c:
        c.execute("UPDATE open_questions SET answer=?, status='answered', answered_ts=? WHERE id=?", (answer, now_iso(), qid))
        row = c.execute("SELECT * FROM open_questions WHERE id=?", (qid,)).fetchone()
    if row:
        log_decision("question_answered", str(qid), answer[:200])
    return dict(row) if row else None


def create_info_request(incident_id: str, unit_id: str, what: str) -> dict:
    with _write_lock, connect() as c:
        cur = c.execute("INSERT INTO info_requests (ts,incident_id,unit_id,what_to_find,status) VALUES (?,?,?,?,'sent')", (now_iso(), incident_id, unit_id, what))
        row = c.execute("SELECT * FROM info_requests WHERE id=?", (cur.lastrowid,)).fetchone()
    log_decision("info_request_sent", str(row["id"]), f"{unit_id} -> {incident_id}: {what[:150]}")
    return dict(row)


def answer_info_request(rid: int, answer: str) -> Optional[dict]:
    with _write_lock, connect() as c:
        c.execute("UPDATE info_requests SET answer=?, status='answered', answered_ts=? WHERE id=?", (answer, now_iso(), rid))
        row = c.execute("SELECT * FROM info_requests WHERE id=?", (rid,)).fetchone()
    if row:
        log_decision("info_request_answered", str(rid), answer[:200])
    return dict(row) if row else None


def list_info_requests() -> list[dict]:
    return _rows("SELECT * FROM info_requests ORDER BY id DESC")


def log_decision(kind: str, target_id: str, detail: str) -> None:
    with _write_lock, connect() as c:
        c.execute("INSERT INTO decisions (ts,kind,target_id,detail) VALUES (?,?,?,?)", (now_iso(), kind, target_id, detail))


def list_decisions(limit: int = 100) -> list[dict]:
    return _rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------ human ids + alerts
def next_label(prefix: str) -> str:
    """FIRE-001, FIRE-002 ... per type prefix (call under the caller's lock)."""
    with connect() as c:
        rows = c.execute("SELECT label FROM incidents WHERE label LIKE ?", (f"{prefix}-%",)).fetchall()
    nums = [int(r[0].split("-")[-1]) for r in rows if r[0] and r[0].split("-")[-1].isdigit()]
    return f"{prefix}-{(max(nums) + 1 if nums else 1):03d}"


def last_alert_score(incident_id: str) -> Optional[tuple]:
    with connect() as c:
        row = c.execute("SELECT score FROM alerts WHERE incident_id=? ORDER BY id DESC LIMIT 1", (incident_id,)).fetchone()
    return tuple(json.loads(row[0])) if row and row[0] else None


def add_alert(incident_id: str, label: str, level: str, headline: str, why: str, reasons: list[str], trigger: str,
              score: tuple, model: str = "", latency_ms: int = 0) -> dict:
    with _write_lock, connect() as c:
        cur = c.execute("INSERT INTO alerts (ts,incident_id,label,level,headline,why,reasons,trigger,score,status,model,latency_ms) VALUES (?,?,?,?,?,?,?,?,?,'new',?,?)",
                        (now_iso(), incident_id, label, level, headline, why, ",".join(reasons), trigger, json.dumps(list(score)), model, latency_ms))
        row = c.execute("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_alerts(limit: int = 50) -> list[dict]:
    return _rows("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))


def mark_alert(alert_id: int, status: str = "seen") -> None:
    with _write_lock, connect() as c:
        c.execute("UPDATE alerts SET status=? WHERE id=?", (status, alert_id))
