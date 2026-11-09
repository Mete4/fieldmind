"""Cloud role and queue (sections 7/8 of CHANGES_FOR_AGENT.md).

- Extraction is always local; nothing urgent waits for the cloud.
- A SITUATION request (situation + suggestions + open questions) runs in the
  cloud when the link and the key are there, every 60 s and on operator
  request. Offline, the local 32B writes the same JSON shape per hazard zone,
  labelled "edge summary". If the cloud is not ready, only the newest
  situation request is kept in the queue; it runs when the link comes back."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from app import config as C
from app import cloud, db, network
from app.extract import client as ollama_client
from app.extract import resolve_model
from app.redact import redact
from app.schemas import Incident, RequestLog

_stop = threading.Event()
_sit_lock = threading.Lock()
last_run: dict = {"ts": None, "source": None, "reason": "", "latency_ms": None}


def route_incident(inc: Incident) -> tuple[Incident, str]:
    """Kept for the API: every incident is final on the edge."""
    return inc, "local"


# ------------------------------------------------------------------ context
def situation_context(limit: int = 30) -> tuple[str, int, str]:
    incs = db.list_incidents(limit=200)
    newest = max((inc.last_ts for inc in incs), default=None) or ""
    by_zone: dict[str, list[Incident]] = {}
    for inc in incs:
        by_zone.setdefault(inc.hazard_zones[0] if inc.hazard_zones else "outside_mapped_hazard_zones", []).append(inc)
    lines = []
    for zone, items in by_zone.items():
        lines.append(f"\n## zone {zone} ({len(items)} incidents)")
        for inc in sorted(items, key=lambda x: -x.priority_score)[:limit]:
            lines.append(f"- incident {inc.id} | reports {','.join(inc.report_ids) or '-'} | {inc.type} sev {inc.severity} prio {inc.priority_score:g} | "
                         f"{inc.geocode_match or 'unknown location'}{' (location unknown)' if inc.location_unknown else ''} | people {inc.people_count if inc.people_count is not None else '?'} | "
                         f"units {','.join(inc.unit) or '-'} ({','.join(inc.department) or '-'}) | part {inc.structure_part or '-'} | fire from {inc.fire_direction or '?'} size {inc.fire_size or '?'} | "
                         f"needs {','.join(inc.needs) or '-'} | last {inc.last_ts[11:19]} | {inc.summary or inc.transcript[:120]} | details: {inc.details[:220]}")
    units = db.list_units()
    blocked = [inc for inc in incs if inc.type == "road_blocked"]
    head = (f"{len(incs)} incidents, {sum(1 for i in incs if i.in_hazard_zone)} inside CAL FIRE Very High/High fire hazard zones, "
            f"{len(blocked)} blocked roads, {sum(i.people_count or 0 for i in incs)} people reported affected. Newest report {newest}.\n"
            f"Units: " + "; ".join(f"{u['name']} ({u['department']}) {u['status']}{' on ' + u['incident_id'] if u['incident_id'] else ''}" for u in units))
    ctx, _ = redact(head + "\n".join(lines))
    return ctx, len(incs), newest


# ------------------------------------------------------------------ run
def _edge_situation(ctx: str, data_as_of: str) -> tuple[dict, str, int]:
    model = _summary_model()
    t0 = time.perf_counter()
    r = ollama_client().chat(model=model, keep_alive=C.OLLAMA_KEEP_ALIVE, format="json",
                             messages=[{"role": "system", "content": cloud.SITUATION_SYSTEM.format(town=C.TOWN_NAME, scenario=C.SCENARIO_NAME)
                                        + "\nWrite the situation as one short paragraph PER ZONE heading given in the input."},
                                       {"role": "user", "content": ctx + "\nReturn the JSON object."}],
                             options={"temperature": 0.2, "num_ctx": 8192, "num_predict": 900})
    ms = int((time.perf_counter() - t0) * 1000)
    db.log_request(RequestLog(route="edge_situation", model=model, latency_ms=ms, tokens_in=int(r.prompt_eval_count or 0), tokens_out=int(r.eval_count or 0), ok=True))
    raw = r.message.content or "{}"
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    return cloud.normalize_situation(d, data_as_of), model, ms


def run_situation(trigger: str = "operator") -> dict:
    """Cloud when ready, else edge (and the request stays queued for the cloud). Never raises."""
    if not _sit_lock.acquire(blocking=False):
        return {"ok": False, "reason": "situation request already running"}
    try:
        ctx, n, newest = situation_context()
        data_as_of = newest or db.now_iso() if hasattr(db, "now_iso") else newest
        ready, reason = network.cloud_ready()
        if ready:
            try:
                payload, meta = cloud.situation_and_suggestions(ctx, data_as_of)
                sid = db.save_situation("cloud", meta["model"], meta["latency_ms"], data_as_of, payload, n, trigger)
                item = db.pending_situation()
                if item:
                    db.mark_queue_item(item["id"], done=True)
                last_run.update(ts=time.time(), source="cloud", reason="", latency_ms=meta["latency_ms"])
                return {"ok": True, "id": sid, "source": "cloud", "label": "Cloud situation", "model": meta["model"], "latency_ms": meta["latency_ms"], **payload}
            except Exception as e:  # noqa: BLE001
                reason = f"cloud failed: {type(e).__name__}"
        db.queue_situation(trigger)             # newest request only; runs when the link is back
        try:
            payload, model, ms = _edge_situation(ctx, data_as_of)
            sid = db.save_situation("edge", model, ms, data_as_of, payload, n, trigger)
            last_run.update(ts=time.time(), source="edge", reason=reason, latency_ms=ms)
            return {"ok": True, "id": sid, "source": "edge", "label": f"Edge summary ({reason})", "model": model, "latency_ms": ms, **payload}
        except Exception as e:  # noqa: BLE001
            last_run.update(ts=time.time(), source="none", reason=f"{reason}; edge failed: {type(e).__name__}")
            return {"ok": False, "reason": last_run["reason"]}
    finally:
        _sit_lock.release()


def _loop() -> None:
    while not _stop.wait(C.SITUATION_INTERVAL_S):
        try:
            if db.report_count() == 0:
                continue
            ready, _ = network.cloud_ready()
            if ready or db.pending_situation():
                run_situation("timer" if ready else "queue")
        except Exception:  # noqa: BLE001
            pass


def start_background_sync() -> None:
    threading.Thread(target=_loop, name="situation-loop", daemon=True).start()
    network.on_online(lambda _: db.pending_situation() and threading.Thread(target=run_situation, args=("reconnect",), daemon=True).start())


def stop_background_sync() -> None:
    _stop.set()


def _summary_model() -> str:
    try:
        m = json.loads(C.MODELS_JSON.read_text()).get("summary_llm") if C.MODELS_JSON.exists() else None
    except Exception:  # noqa: BLE001
        m = None
    return m or resolve_model()
