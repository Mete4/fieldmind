"""Transcript -> structured incident, fully on the edge:
local LLM (Ollama structured output, JSON schema) -> geocode -> hazard zones
-> priority. Every model call is timed and logged; invalid JSON is retried
once and then replaced by a safe fallback record. Never raises."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import ollama

from app import config as C
from app import db, hazards
from app.geocode import get_geocoder
from app.schemas import INCIDENT_TYPES, Extraction, Incident, RequestLog

SYSTEM_PROMPT = """You are the triage assistant in a disaster-response command center for {town}.
You read ONE emergency radio transmission or text report and return ONLY a JSON object with these fields:
- location_text: the most specific place in the report (intersection, street, landmark, facility, neighborhood), copied as said; null if no place is given.
- type: one of fire, collapse, medical, road_blocked, trapped, other. Use trapped for people stuck, missing, cut off or needing rescue; medical for injuries or patients; road_blocked for impassable roads, debris, stalled vehicles or downed lines.
- severity: integer 1-5. 5 = lives in immediate danger or major structure/infrastructure loss; 4 = serious injuries, spreading fire or rising water threatening homes; 3 = significant but contained; 2 = minor; 1 = informational.
- people_count: number of people affected, trapped, injured or unaccounted for if stated (digits), else null. Never guess.
- needs: 1-4 items from: {needs}.
- department: FD (fire), PD (police/sheriff), EMS (medic/ambulance) or other, from the reporting unit.
- unit: the radio call sign of the unit reporting (e.g. "Engine 81", "Medic 3", "Paradise PD 12"); "" if none.
- structure_part: the part of a building involved ("east wing", "gym", "second floor"), else null.
- fire_direction: compass direction the fire comes FROM, one of N, NE, E, SE, S, SW, W, NW; null if not stated.
- fire_size: small, medium or large if stated or clearly implied (a spot fire is small, a fully involved structure or a running fire front is large); else null.
- details: one or two sentences with every fact that fits no other field (exposures, access, hazards, resources on scene, timing).
- summary: one line, at most 20 words, for a priority list.
Never invent details that are not in the report."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "location_text": {"type": ["string", "null"]},
        "type": {"type": "string", "enum": list(INCIDENT_TYPES)},
        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
        "people_count": {"type": ["integer", "null"]},
        "needs": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "department": {"type": "string", "enum": ["FD", "PD", "EMS", "other"]},
        "unit": {"type": "string"},
        "structure_part": {"type": ["string", "null"]},
        "fire_direction": {"type": ["string", "null"], "enum": ["N", "NE", "E", "SE", "S", "SW", "W", "NW", None]},
        "fire_size": {"type": ["string", "null"], "enum": ["small", "medium", "large", None]},
        "details": {"type": "string"},
    },
    "required": ["location_text", "type", "severity", "people_count", "needs", "summary",
                 "department", "unit", "structure_part", "fire_direction", "fire_size", "details"],
}

_client: Optional[ollama.Client] = None
_model: Optional[str] = None
_lock = threading.Lock()


def client() -> ollama.Client:
    global _client
    with _lock:
        if _client is None:
            _client = ollama.Client(host=C.OLLAMA_HOST, timeout=C.LLM_TIMEOUT_S)
        return _client


def available_models() -> list[str]:
    try:
        return [m.model for m in client().list().models]
    except Exception:  # noqa: BLE001
        return []


def resolve_model(force: Optional[str] = None) -> str:
    """LLM_MODEL from config/env, or the first pulled model in LLM_CANDIDATES."""
    global _model
    if force:
        return force
    if _model:
        return _model
    if C.LLM_MODEL and C.LLM_MODEL != "auto":
        _model = C.LLM_MODEL
        return _model
    avail = available_models()
    for cand in C.ORGANIZER_CANDIDATES:        # the organizer: first candidate that is pulled
        if cand in avail:
            _model = cand
            return _model
    for cand in C.LLM_CANDIDATES:
        if any(a == cand or a.split(":")[0] == cand.split(":")[0] and a.startswith(cand) for a in avail):
            _model = cand
            return _model
    _model = next((a for a in avail if "vl" not in a and "embed" not in a), C.LLM_CANDIDATES[-1])
    return _model


def set_model(name: str) -> None:
    global _model
    _model = name


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def llm_extract(transcript: str, model: Optional[str] = None, incident_id: Optional[str] = None) -> tuple[Extraction, dict]:
    """Call the local LLM. Returns (Extraction, meta). On two failures returns
    the safe fallback (confidence FALLBACK_CONFIDENCE) instead of raising."""
    model = model or resolve_model()
    meta = {"model": model, "attempts": 0, "fallback": False, "tokens_in": 0, "tokens_out": 0,
            "tokens_per_s": None, "latency_ms": 0, "error": None}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(town=C.TOWN_NAME, needs=", ".join(C.NEEDS_VOCAB))},
        {"role": "user", "content": f"Report:\n{transcript.strip()}\n\nReturn the JSON object now."},
    ]
    t0 = time.perf_counter()
    last_err: Optional[str] = None
    for attempt in range(2):
        meta["attempts"] = attempt + 1
        try:
            fmt = EXTRACTION_SCHEMA if attempt == 0 else "json"
            if attempt == 1 and last_err and "Timeout" in last_err and C.FALLBACK_MODEL in available_models():
                model = C.FALLBACK_MODEL          # organizer too slow right now: step down
                meta["model"] = model
            c = ollama.Client(host=C.OLLAMA_HOST, timeout=C.ORGANIZER_TIMEOUT_S if attempt == 0 else C.LLM_TIMEOUT_S)
            r = c.chat(model=model, messages=messages, format=fmt, keep_alive=C.OLLAMA_KEEP_ALIVE, think=C.LLM_THINK if model.startswith("qwen3") else None,
                       options={"temperature": C.LLM_TEMPERATURE, "num_ctx": C.LLM_NUM_CTX, "num_predict": C.LLM_NUM_PREDICT})
            meta["tokens_in"] += int(getattr(r, "prompt_eval_count", 0) or 0)
            out = int(getattr(r, "eval_count", 0) or 0)
            meta["tokens_out"] += out
            ed = int(getattr(r, "eval_duration", 0) or 0)
            if ed and out:
                meta["tokens_per_s"] = round(out / (ed / 1e9), 1)
            ext = Extraction.model_validate(_parse_json(r.message.content or ""))
            meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            db.log_request(RequestLog(route="edge_llm", model=model, latency_ms=meta["latency_ms"], tokens_in=meta["tokens_in"],
                                      tokens_out=meta["tokens_out"], bytes_out=0, ok=True, incident_id=incident_id))
            return ext, meta
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            continue
    meta.update(fallback=True, error=last_err, latency_ms=int((time.perf_counter() - t0) * 1000))
    db.log_request(RequestLog(route="edge_llm", model=model, latency_ms=meta["latency_ms"], tokens_in=meta["tokens_in"],
                              tokens_out=meta["tokens_out"], ok=False, error=last_err, incident_id=incident_id))
    return Extraction(type="other", summary="Extraction failed, needs manual review"), meta


def process_text(transcript: str, channel: str = "text", input_type: str = "text",
                 model: Optional[str] = None) -> Incident:
    """Full edge pipeline for one report. Always returns an Incident."""
    t0 = time.perf_counter()
    inc = Incident(channel=channel, input_type=input_type, transcript=transcript.strip())
    ext, meta = llm_extract(transcript, model, incident_id=inc.id)
    inc.location_text = ext.location_text
    inc.type, inc.severity, inc.people_count = ext.type, ext.severity, ext.people_count
    inc.needs, inc.summary = ext.needs, ext.summary
    if meta["fallback"]:
        inc.review_reasons.append("extraction failed")
    inc.department, inc.unit = [ext.department], [ext.unit] if ext.unit else []
    inc.structure_part, inc.fire_direction, inc.fire_size, inc.details = ext.structure_part, ext.fire_direction, ext.fire_size, ext.details or ""
    inc.llm_model, inc.tokens_per_s = meta["model"], meta["tokens_per_s"]
    inc.extraction_attempts, inc.fallback = meta["attempts"], meta["fallback"]
    try:
        g = get_geocoder().geocode(ext.location_text)
    except Exception:  # noqa: BLE001
        g = None
    if g:
        inc.lat, inc.lon, inc.geocode_match, inc.geocode_score = g["lat"], g["lon"], g["match"], g["score"]
    else:
        inc.lat, inc.lon, inc.location_unknown = C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON, True
        inc.review_reasons.append("address not found" if ext.location_text else "no location")
    if re.search(r"\?|\b(can anybody confirm|can anyone confirm|confirm that|say again)\b", transcript, re.I):
        inc.review_reasons.append("question in call")
    inc.needs_review = bool(inc.review_reasons)
    hazards.annotate(inc)
    inc.edge_latency_ms = int((time.perf_counter() - t0) * 1000)
    return inc
