"""The orchestrator: watches every new or updated incident and tells the
operator what to look at. Rules first (fast), then the organizer model writes
one short line about why. One alert per incident; another only when the
incident gets worse (higher severity, more people, newly trapped, a new part
of a structure involved)."""
from __future__ import annotations

import re
import threading
import time

from app import config as C
from app import db
from app.extract import client as ollama_client
from app.extract import resolve_model
from app.schemas import Incident, RequestLog

KEY_PLACES = re.compile(r"(?i)school|hospital|adventist health|clinic|care center|nursing")
EVAC_ROUTES = re.compile(r"(?i)skyway|clark road|pentz road|neal road|pearson road|highway 191")
BURNOVER = re.compile(r"(?i)burn ?over|overrun by fire|fire across the road")
ESCALATION_WORDS = re.compile(r"(?i)now involved|has extended|spreading|jumped|second alarm|third alarm|more (?:people|victims)")


def assess(inc: Incident) -> tuple[bool, list[str], tuple]:
    reasons: list[str] = []
    if inc.severity >= 4:
        reasons.append(f"severity {inc.severity}")
    if (inc.people_count or 0) >= 10:
        reasons.append(f"{inc.people_count} people")
    if inc.type == "trapped":
        reasons.append("people trapped")
    place = f"{inc.geocode_match or ''} {inc.location_text or ''}"
    if KEY_PLACES.search(place):
        reasons.append("school or hospital")
    text = f"{inc.transcript} {inc.details}"
    if BURNOVER.search(text):
        reasons.append("burnover")
    if inc.type == "road_blocked" and EVAC_ROUTES.search(place):
        reasons.append("evacuation route blocked")
    score = (inc.severity, inc.people_count or 0, int(inc.type == "trapped"), int(bool(inc.structure_part)), int(bool(BURNOVER.search(text))))
    return bool(reasons), reasons, score


def headline(inc: Incident) -> str:
    what = f"{(inc.fire_size or '').capitalize()} {inc.type.replace('_', ' ')}".strip() if inc.type == "fire" else inc.type.replace("_", " ").capitalize()
    where = inc.geocode_match or inc.location_text or "unknown location"
    people = f"{inc.people_count} people" if inc.people_count else "people unknown"
    return f"{inc.label} \u00b7 {what} at {where} \u00b7 {people} \u00b7 look into this incident"


def _why(inc: Incident, reasons: list[str]) -> tuple[str, str, int]:
    model = resolve_model()
    t0 = time.perf_counter()
    try:
        import ollama

        c = ollama.Client(host=C.OLLAMA_HOST, timeout=25)
        r = c.chat(model=model, keep_alive=C.OLLAMA_KEEP_ALIVE, think=C.LLM_THINK if model.startswith("qwen3") else None,
                   messages=[{"role": "system", "content": "You are the dispatcher's assistant. In ONE sentence of at most 18 words, say why the operator "
                                                           "should look at this incident now. No preamble."},
                             {"role": "user", "content": f"{inc.label}: {inc.type}, severity {inc.severity}, people {inc.people_count}, at {inc.geocode_match}, "
                                                         f"fire from {inc.fire_direction} size {inc.fire_size}, part {inc.structure_part}. Rule hits: {', '.join(reasons)}. "
                                                         f"Latest report: {inc.transcript[:300]}"}],
                   options={"temperature": 0.2, "num_ctx": 2048, "num_predict": 40})
        ms = int((time.perf_counter() - t0) * 1000)
        db.log_request(RequestLog(route="edge_alert", model=model, latency_ms=ms, tokens_in=int(r.prompt_eval_count or 0), tokens_out=int(r.eval_count or 0), ok=True, incident_id=inc.id))
        return (r.message.content or "").strip().split("\n")[0][:220], model, ms
    except Exception as e:  # noqa: BLE001
        return f"Rules: {', '.join(reasons)} ({type(e).__name__})", model, int((time.perf_counter() - t0) * 1000)


_eval_lock = threading.Lock()


def evaluate(inc: Incident, action: str) -> dict | None:
    """Called after every save. Returns the alert dict or None."""
    important, reasons, score = assess(inc)
    if not important:
        return None
    with _eval_lock:
        return _evaluate_locked(inc, reasons, score)


def _evaluate_locked(inc: Incident, reasons: list[str], score: tuple) -> dict | None:
    prev = db.last_alert_score(inc.id)
    if prev is not None:
        if tuple(score) <= tuple(prev):
            return None
        trigger = "escalation"
    else:
        trigger = "new"
    why, model, ms = _why(inc, reasons)
    level = "critical" if inc.severity >= 5 or inc.type == "trapped" or (inc.people_count or 0) >= 50 else "high"
    return db.add_alert(inc.id, inc.label, level, headline(inc), why, reasons, trigger, score, model, ms)


def evaluate_async(inc: Incident, action: str) -> None:
    threading.Thread(target=lambda: evaluate(inc, action), daemon=True).start()
