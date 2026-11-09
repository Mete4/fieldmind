"""Local-vs-cloud decisions, the cloud queue and its background sync, and
situation summaries.

Rules (section 8): every report is processed locally first and saved at once
(status local). If the resulting incident's confidence < CONFIDENCE_THRESHOLD:
  cloud ready  -> redact, refine in the cloud, update (source cloud, status cloud_refined)
  otherwise    -> cloud_queue (status queued)
Going online drains the queue in the background. Summaries: cloud when
online, else the local LLM, labelled "edge summary"."""
from __future__ import annotations

import json
import threading
import time

from app import config as C
from app import cloud, db, hazards, network
from app.extract import client as ollama_client
from app.extract import resolve_model
from app.geocode import get_geocoder
from app.redact import redact
from app.schemas import Incident, RequestLog


# ------------------------------------------------------------------ routing
def apply_refinement(inc: Incident, ext, notes: str, meta: dict) -> Incident:
    inc.type, inc.severity, inc.needs = ext.type, ext.severity, ext.needs or inc.needs
    if ext.people_count is not None:
        inc.people_count = ext.people_count
    inc.confidence = ext.confidence
    if ext.summary:
        inc.summary = ext.summary
    if ext.location_text and ext.location_text != inc.location_text:
        g = None
        try:
            g = get_geocoder().geocode(ext.location_text)
        except Exception:  # noqa: BLE001
            pass
        if g:
            inc.location_text, inc.lat, inc.lon = ext.location_text, g["lat"], g["lon"]
            inc.geocode_match, inc.geocode_score, inc.location_unknown = g["match"], g["score"], False
    inc.source, inc.status = "cloud", "cloud_refined"
    inc.cloud_latency_ms, inc.cloud_notes = meta["latency_ms"], notes
    hazards.annotate(inc)
    return inc


def route_incident(inc: Incident) -> tuple[Incident, str]:
    """Decide what happens after local processing. Returns (incident, decision)."""
    if inc.confidence >= C.CONFIDENCE_THRESHOLD:
        return inc, "local"
    if inc.status == "cloud_refined":
        return inc, "already_refined"
    ready, reason = network.cloud_ready()
    if ready:
        try:
            ext, notes, meta = cloud.refine_incident(inc)
            inc = apply_refinement(inc, ext, notes, meta)
            db.save_incident(inc)
            return inc, "cloud_refined"
        except Exception as e:  # noqa: BLE001
            reason = f"cloud call failed: {type(e).__name__}"
    inc.status = "queued"
    db.save_incident(inc)
    db.enqueue_cloud(inc.id)
    return inc, f"queued ({reason})"


# ------------------------------------------------------------------ queue drain
_drain_lock = threading.Lock()
_stop = threading.Event()
last_drain: dict = {"ts": None, "processed": 0, "failed": 0, "reason": ""}


def drain_queue(max_items: int = 50) -> dict:
    """Refine every queued incident while the cloud is ready. Safe to call often."""
    result = {"processed": 0, "failed": 0, "remaining": db.queue_size(), "reason": ""}
    if not _drain_lock.acquire(blocking=False):
        result["reason"] = "drain already running"
        return result
    try:
        ready, reason = network.cloud_ready()
        if not ready:
            result["reason"] = reason
            return result
        for item in db.pending_queue(max_items):
            inc = db.get_incident(item["incident_id"])
            if inc is None:
                db.mark_queue_item(item["id"], done=True)
                continue
            try:
                ext, notes, meta = cloud.refine_incident(inc)
                db.save_incident(apply_refinement(inc, ext, notes, meta))
                db.mark_queue_item(item["id"], done=True)
                result["processed"] += 1
            except Exception as e:  # noqa: BLE001
                db.mark_queue_item(item["id"], done=False, error=f"{type(e).__name__}: {str(e)[:120]}")
                result["failed"] += 1
                if item["attempts"] + 1 >= 5:          # give up, keep the local result
                    db.mark_queue_item(item["id"], done=True, error="gave up after 5 attempts")
                    db.update_incident(inc.id, status="local")
                if not network.connectivity(force=True):
                    result["reason"] = "lost connectivity mid-drain"
                    break
        result["remaining"] = db.queue_size()
        last_drain.update(ts=time.time(), processed=result["processed"], failed=result["failed"], reason=result["reason"])
        return result
    finally:
        _drain_lock.release()


def _drain_loop() -> None:
    while not _stop.wait(C.QUEUE_DRAIN_INTERVAL_S):
        try:
            if db.queue_size():
                drain_queue()
        except Exception:  # noqa: BLE001
            pass


def start_background_sync() -> None:
    threading.Thread(target=_drain_loop, name="cloud-queue-drain", daemon=True).start()
    network.on_online(lambda _: threading.Thread(target=drain_queue, daemon=True).start())


def stop_background_sync() -> None:
    _stop.set()


# ------------------------------------------------------------------ summaries
def summary_context(limit: int = 25) -> tuple[str, int]:
    incs = db.list_incidents(limit=200)
    lines = []
    for i, inc in enumerate(sorted(incs, key=lambda x: -x.priority_score)[:limit], 1):
        text = inc.summary or inc.transcript[:140]
        lines.append(f"{i}. [{inc.type} sev {inc.severity} prio {inc.priority_score:g}] {text} | at {inc.geocode_match or 'unknown location'}"
                     f" | people {inc.people_count if inc.people_count is not None else '?'} | hazard zones: {', '.join(inc.hazard_zones) or 'none'}"
                     f" | needs {', '.join(inc.needs) or '-'} | reports {inc.report_count} | status {inc.status}")
    blocked = [inc for inc in incs if inc.type == "road_blocked"]
    in_zone = sum(1 for inc in incs if inc.in_hazard_zone)
    people = sum(inc.people_count or 0 for inc in incs)
    head = (f"{len(incs)} incidents, {in_zone} inside real hazard zones, {len(blocked)} blocked roads, "
            f"{people} people reported affected. Queue to cloud: {db.queue_size()}.\n")
    return head + "\n".join(lines), len(incs)


def _summary_model() -> str:
    try:
        m = json.loads(C.MODELS_JSON.read_text()).get("summary_llm") if C.MODELS_JSON.exists() else None
    except Exception:  # noqa: BLE001
        m = None
    return m or resolve_model()


def make_summary(force_edge: bool = False) -> dict:
    """Cloud summary when online, otherwise the local LLM. Never raises."""
    ctx, n = summary_context()
    ctx, _ = redact(ctx)
    ready, reason = network.cloud_ready()
    if ready and not force_edge:
        try:
            text, meta = cloud.situation_summary(ctx)
            db.save_summary("cloud", meta["model"], meta["latency_ms"], text, n)
            return {"text": text, "source": "cloud", "label": "Cloud summary", "model": meta["model"],
                    "latency_ms": meta["latency_ms"], "incidents": n}
        except Exception as e:  # noqa: BLE001
            reason = f"cloud failed: {type(e).__name__}"
    model = _summary_model()
    t0 = time.perf_counter()
    try:
        r = ollama_client().chat(model=model, keep_alive=C.OLLAMA_KEEP_ALIVE,
                                 messages=[{"role": "system", "content": cloud.SUMMARY_SYSTEM.format(town=C.TOWN_NAME, scenario=C.SCENARIO_NAME)},
                                           {"role": "user", "content": ctx}],
                                 options={"temperature": 0.2, "num_ctx": C.LLM_NUM_CTX, "num_predict": 320})
        text = r.message.content.strip()
        ms = int((time.perf_counter() - t0) * 1000)
        db.log_request(RequestLog(route="edge_summary", model=model, latency_ms=ms, tokens_in=int(r.prompt_eval_count or 0),
                                  tokens_out=int(r.eval_count or 0), ok=True))
        db.save_summary("edge", model, ms, text, n)
        return {"text": text, "source": "edge", "label": f"Edge summary ({reason})", "model": model, "latency_ms": ms, "incidents": n}
    except Exception as e:  # noqa: BLE001
        ms = int((time.perf_counter() - t0) * 1000)
        db.log_request(RequestLog(route="edge_summary", model=model, latency_ms=ms, ok=False, error=str(e)[:160]))
        return {"text": f"Summary unavailable: {type(e).__name__}", "source": "none", "label": "Summary failed",
                "model": model, "latency_ms": ms, "incidents": n}
