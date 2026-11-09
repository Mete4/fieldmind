"""Operator questions in plain language -> ONE guarded DuckDB SELECT over the
SQLite store (attached READ_ONLY) with Flock LLM functions (llm_filter,
llm_complete running on local Ollama models) -> rows + a short answer.
The operator always gets the SQL and the report/incident IDs behind it."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import duckdb

from app import config as C
from app import db
from app.extract import client as ollama_client
from app.extract import resolve_model
from app.schemas import RequestLog

_lock = threading.Lock()
_con: Optional[duckdb.DuckDBPyConnection] = None
_models: dict[str, str] = {}
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|pragma|export|import|call|install|load|set|grant)\b", re.I)

SCHEMA_DOC = """Tables (DuckDB SQL, SQLite attached as schema fm, read-only):

fm.reports  -- one row per raw radio call / text, newest first by ts
  id TEXT, ts TEXT (ISO UTC, e.g. 2026-11-09T01:10:22+00:00), channel TEXT, input_type TEXT ('audio'|'text'),
  department TEXT ('FD'|'PD'|'EMS'|'other'), unit TEXT (call sign), type TEXT ('fire'|'collapse'|'medical'|'road_blocked'|'trapped'|'other'),
  severity INTEGER 1-5, people_count INTEGER, location_text TEXT, needs_review INTEGER 0/1, review_reasons TEXT (comma list: 'address not found','no location','unclear audio','question in call','contradicts FIRE-001','extraction failed'), structure_part TEXT, fire_direction TEXT ('N'..'NW'),
  fire_size TEXT ('small'|'medium'|'large'), details TEXT, text TEXT (the transcript), status TEXT, incident_id TEXT (link to fm.incidents.id)

fm.incidents -- one row per incident, aggregated from its linked reports
  id TEXT (internal), label TEXT (human id such as FIRE-001, MED-002, TRAP-001, ROAD-001; always select it), timestamp TEXT (first report), last_ts TEXT (latest report), type TEXT (same vocabulary), severity INTEGER 1-5 (max),
  needs_review INTEGER 0/1, review_reasons TEXT (comma list), priority_score REAL, in_hazard_zone INTEGER 0/1, hazard_zones TEXT (comma list, e.g. 'fire_very_high'),
  status TEXT ('local'|'queued'|'cloud_refined'), report_count INTEGER, location_text TEXT, geocode_match TEXT (place name), lat REAL, lon REAL,
  people_count INTEGER (max), needs TEXT (comma list), summary TEXT, transcript TEXT (latest report), department TEXT (comma list, e.g. 'FD,EMS'),
  unit TEXT (comma list), structure_part TEXT, fire_direction TEXT, fire_size TEXT, details TEXT (all reports, newest first), report_ids TEXT (comma list)

fm.units -- unit status board
  id TEXT, name TEXT (e.g. 'Engine 81'), department TEXT, status TEXT ('available'|'assigned'|'out_of_service'), incident_id TEXT (fm.incidents.id when assigned)

LLM functions (local models, slow: ~1 s per row; ALWAYS put normal SQL filters first so few rows reach them; use them only on text columns text/details/summary/transcript):
  llm_filter({'model_name': 'local'}, {'prompt': 'yes/no question about {t}', 'context_columns': [{'data': <text column>, 'name': 't'}]})  -> BOOLEAN
  llm_complete({'model_name': 'local'}, {'prompt': 'instruction mentioning {t}', 'context_columns': [{'data': <text column>, 'name': 't'}]}) -> VARCHAR
Time: "last 30 minutes" = ts >= strftime(now() AT TIME ZONE 'UTC' - INTERVAL 30 MINUTE, '%Y-%m-%dT%H:%M:%S')
Text matching: use lower(col) LIKE '%high school%'. Department lists: department LIKE '%FD%'. Comma lists: hazard_zones LIKE '%fire_very_high%'."""

EXAMPLES = [
    ("Which incidents have people trapped inside?",
     "SELECT id, label, type, severity, people_count, geocode_match, structure_part, summary FROM fm.incidents "
     "WHERE type = 'trapped' OR (type IN ('fire','collapse') AND llm_filter({'model_name': 'local'}, "
     "{'prompt': 'Does this radio report say people are trapped inside or unable to get out? Answer yes or no. Report: {t}', "
     "'context_columns': [{'data': transcript, 'name': 't'}]})) ORDER BY priority_score DESC LIMIT 50"),
    ("What did FD units report about XYZ High School in the last 30 minutes?",
     "SELECT id, ts, unit, department, text, incident_id FROM fm.reports WHERE department = 'FD' "
     "AND ts >= strftime(now() AT TIME ZONE 'UTC' - INTERVAL 30 MINUTE, '%Y-%m-%dT%H:%M:%S') "
     "AND (lower(text) LIKE '%high school%' OR lower(location_text) LIKE '%high school%') ORDER BY ts DESC LIMIT 50"),
    ("List large fires coming from the north with more than 50 people.",
     "SELECT id, label, geocode_match, fire_size, fire_direction, people_count, severity, summary FROM fm.incidents "
     "WHERE type = 'fire' AND fire_size = 'large' AND fire_direction = 'N' AND people_count > 50 ORDER BY people_count DESC LIMIT 50"),
    ("Which incidents have people trapped and no unit assigned?",
     "SELECT i.id, i.label, i.type, i.severity, i.people_count, i.geocode_match FROM fm.incidents i "
     "WHERE i.type = 'trapped' AND NOT EXISTS (SELECT 1 FROM fm.units u WHERE u.incident_id = i.id AND u.status = 'assigned') "
     "ORDER BY i.priority_score DESC LIMIT 50"),
    ("Give me a one-line summary of each EMS report.",
     "SELECT id, ts, unit, llm_complete({'model_name': 'local'}, {'prompt': 'Summarize this radio report in one line of at most 12 words: {t}', "
     "'context_columns': [{'data': text, 'name': 't'}]}) AS one_line FROM fm.reports WHERE department = 'EMS' ORDER BY ts DESC LIMIT 20"),
]

SQL_SYSTEM = """You translate an emergency-operations question into ONE DuckDB SQL SELECT statement. Return only the SQL, no prose, no code fences.
Rules: a single SELECT (WITH ... SELECT is allowed); only the tables fm.reports, fm.incidents and fm.units; put ordinary WHERE filters (department, type,
severity, fire_size, fire_direction, people_count, time) BEFORE any llm_ function so that few rows reach the model; use llm_filter / llm_complete
only when the question needs reading the text; always include id columns (id, incident_id or report_ids) so the operator can trace rows; end with LIMIT 50 or less.
""" + SCHEMA_DOC + "\n\nExamples:\n" + "\n".join(f"Q: {q}\nSQL: {s}" for q, s in EXAMPLES)

ANSWER_SYSTEM = ("You are the operator's assistant in a disaster command center. Answer the question in at most 3 short sentences "
                 "using ONLY the rows given (JSON). Refer to incidents by their label (FIRE-001 etc.) when present. Mention counts, places and people numbers. If there are no rows, say so plainly.")


def _setup() -> duckdb.DuckDBPyConnection:
    global _con, _models
    if _con is not None:
        return _con
    con = duckdb.connect()
    for s in ("INSTALL sqlite", "LOAD sqlite", "INSTALL flock FROM community", "LOAD flock"):
        con.execute(s)
    con.execute("CREATE SECRET (TYPE OLLAMA, API_URL '%s')" % C.OLLAMA_HOST.replace("http://", "").replace("https://", ""))
    try:
        mj = json.loads(C.MODELS_JSON.read_text()) if C.MODELS_JSON.exists() else {}
    except Exception:  # noqa: BLE001
        mj = {}
    live = mj.get("live_llm") or resolve_model()
    big = mj.get("summary_llm") or live
    _models = {"local": live, "local32": big}
    for name, model in _models.items():
        con.execute("""CREATE MODEL('%s', '%s', 'ollama', {"tuple_format": "json", "max_batch_size": 8, "model_parameters": {"temperature": 0.1}})""" % (name, model))
    _con = con
    return con


def _attach(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("DETACH fm")
    except Exception:  # noqa: BLE001
        pass
    con.execute(f"ATTACH '{C.DB_PATH}' AS fm (TYPE sqlite, READ_ONLY)")


def guard_sql(sql: str) -> str:
    s = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql.strip(), flags=re.S).strip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("only one statement is allowed")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("only SELECT queries are allowed")
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", s)):
        raise ValueError("statement contains a forbidden keyword")
    if not re.search(r"\blimit\s+\d+", s, re.I):
        s += " LIMIT 50"
    return s


def _ollama(system: str, user: str, model: str, num_predict: int, route: str, timeout_s: float = 60) -> tuple[str, int]:
    t0 = time.perf_counter()
    import ollama

    c = ollama.Client(host=C.OLLAMA_HOST, timeout=timeout_s)
    r = c.chat(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
               keep_alive=C.OLLAMA_KEEP_ALIVE, options={"temperature": 0.0, "num_ctx": 8192, "num_predict": num_predict})
    ms = int((time.perf_counter() - t0) * 1000)
    db.log_request(RequestLog(route=route, model=model, latency_ms=ms, tokens_in=int(r.prompt_eval_count or 0), tokens_out=int(r.eval_count or 0), ok=True))
    return (r.message.content or "").strip(), ms


def _run_sql(con: duckdb.DuckDBPyConnection, sql: str, timeout_s: float = 60) -> tuple[list[str], list[tuple]]:
    result: dict = {}

    def work() -> None:
        try:
            cur = con.execute(sql)
            result["cols"] = [d[0] for d in cur.description]
            result["rows"] = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            result["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        try:
            con.interrupt()
        except Exception:  # noqa: BLE001
            pass
        raise TimeoutError(f"query exceeded {timeout_s:.0f} s")
    if "error" in result:
        raise result["error"]
    return result["cols"], result["rows"]


def _ids(sql: str, cols: list[str], rows: list[dict]) -> tuple[list[str], list[str]]:
    reports, incidents = [], []
    from_reports = "fm.reports" in sql.lower() and "fm.incidents" not in sql.lower()
    for r in rows:
        for c in cols:
            v = r.get(c)
            if not isinstance(v, str) or not v:
                continue
            if c == "id":
                (reports if from_reports else incidents).append(v)
            elif c == "incident_id":
                incidents.append(v)
            elif c == "report_ids":
                reports += [x for x in v.split(",") if x]
    return sorted(set(reports)), sorted(set(incidents))


def answer(question: str, model: Optional[str] = None) -> dict:
    """Never raises: errors come back in the 'error' field."""
    t0 = time.perf_counter()
    out = {"question": question, "answer": "", "sql": "", "rows": [], "columns": [], "report_ids": [], "incident_ids": [],
           "model": None, "latency_ms": 0, "attempts": 0, "error": None}
    with _lock:
        try:
            con = _setup()
            _attach(con)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"query engine unavailable: {type(e).__name__}: {str(e)[:160]}"
            out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            return out
        sql_model = model or _models.get("local32") or resolve_model()
        user = f"Q: {question}\nSQL:"
        cols, rows, sql, last_err = [], [], "", None
        for attempt in range(2):
            out["attempts"] = attempt + 1
            try:
                budget = 25 if attempt == 0 and sql_model == _models.get("local32") else 60
                raw, _ = _ollama(SQL_SYSTEM, user, sql_model, 300, "edge_query_sql", timeout_s=budget)
                sql = guard_sql(raw)
                out["sql"], out["model"] = sql, sql_model
                cols, rows = _run_sql(con, sql)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {str(e)[:300]}"
                sql_model = C.FALLBACK_MODEL if C.FALLBACK_MODEL in (_models.values()) or True else sql_model   # retry on the fast model
                user = f"Q: {question}\nYour previous SQL:\n{sql or raw if 'raw' in dir() else ''}\nfailed with: {last_err}\nReturn a corrected single SELECT.\nSQL:"
        if last_err:
            out["error"] = last_err
            out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            return out
    dict_rows = [{c: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v)) for c, v in zip(cols, r)} for r in rows]
    out["columns"], out["rows"] = cols, dict_rows
    out["report_ids"], out["incident_ids"] = _ids(sql, cols, dict_rows)
    try:
        preview = json.dumps(dict_rows[:25])[:3500]
        out["answer"], _ = _ollama(ANSWER_SYSTEM, f"Question: {question}\nRows ({len(dict_rows)} total, first {min(25, len(dict_rows))} shown):\n{preview}",
                                   _models.get("local") or resolve_model(), 200, "edge_query_answer")
    except Exception as e:  # noqa: BLE001
        out["answer"] = f"{len(dict_rows)} rows (answer model failed: {type(e).__name__})"
    out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return out
