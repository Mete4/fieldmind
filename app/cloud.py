"""Anthropic API calls, only ever with redacted text:
  refine_incident(): second opinion on a low-confidence extraction
  situation_summary(): cross-incident summary for the command staff
Every call is timed and logged with tokens and bytes sent off-device."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from app import config as C
from app import db
from app.redact import redact, redacted_bytes
from app.schemas import INCIDENT_TYPES, Extraction, Incident, RequestLog

_client = None


class CloudUnavailable(RuntimeError):
    pass


def client():
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise CloudUnavailable("ANTHROPIC_API_KEY not set")
        import anthropic

        _client = anthropic.Anthropic(timeout=C.ANTHROPIC_TIMEOUT_S, max_retries=1)
    return _client


def _json_from(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        return json.loads(text[s:e + 1])


def _call(route: str, system: str, user: str, model: str, max_tokens: int, incident_id: Optional[str] = None) -> tuple[str, dict]:
    t0 = time.perf_counter()
    bytes_out = redacted_bytes(system) + redacted_bytes(user)
    try:
        resp = client().messages.create(model=model, max_tokens=max_tokens, system=system,
                                        messages=[{"role": "user", "content": user}])
        text = "".join(getattr(b, "text", "") for b in resp.content)
        meta = {"latency_ms": int((time.perf_counter() - t0) * 1000), "tokens_in": resp.usage.input_tokens,
                "tokens_out": resp.usage.output_tokens, "bytes_out": bytes_out, "model": model}
        db.log_request(RequestLog(route=route, model=model, latency_ms=meta["latency_ms"], tokens_in=meta["tokens_in"],
                                  tokens_out=meta["tokens_out"], bytes_out=bytes_out, ok=True, incident_id=incident_id))
        return text, meta
    except Exception as e:  # noqa: BLE001
        db.log_request(RequestLog(route=route, model=model, latency_ms=int((time.perf_counter() - t0) * 1000),
                                  bytes_out=0, ok=False, error=f"{type(e).__name__}: {str(e)[:160]}", incident_id=incident_id))
        raise


REFINE_SYSTEM = """You are a senior emergency dispatcher in {town} reviewing an automated triage extraction from a radio report.
Correct it if needed and return ONLY a JSON object with: location_text (most specific place in the report or null),
type (one of {types}), severity (1-5; 5 = lives in immediate danger), people_count (integer or null, never guess),
needs (1-4 of: {needs}), confidence (0-1, your confidence in type+severity+location), summary (<= 20 words),
notes (<= 25 words: what you changed and why, or "confirmed"). Personal data in the report has been redacted; do not speculate about it."""


def refine_incident(inc: Incident) -> tuple[Extraction, str, dict]:
    """Returns (refined extraction, notes, meta). Raises on failure."""
    text, counts = redact(inc.transcript)
    current = {k: getattr(inc, k) for k in ("location_text", "type", "severity", "people_count", "needs", "confidence", "summary")}
    user = (f"Redacted report:\n{text}\n\nLocal extraction (confidence {inc.confidence}):\n{json.dumps(current)}\n"
            f"Local geocoder matched: {inc.geocode_match or 'nothing'} (score {inc.geocode_score}).\n"
            f"Real hazard zones at that point: {', '.join(inc.hazard_zones) or 'none'}.\nReturn the JSON object.")
    raw, meta = _call("cloud_refine", REFINE_SYSTEM.format(town=C.TOWN_NAME, types=", ".join(INCIDENT_TYPES), needs=", ".join(C.NEEDS_VOCAB)),
                      user, C.ANTHROPIC_MODEL, C.ANTHROPIC_MAX_TOKENS, inc.id)
    data = _json_from(raw)
    notes = str(data.pop("notes", "") or "")[:200]
    meta["redacted"] = counts
    return Extraction.model_validate(data), notes, meta


SUMMARY_SYSTEM = """You write the situation summary for the incident commander of {town} ({scenario}).
Use ONLY the incident list given. In at most 180 words and plain language: 1) overall picture, 2) incidents inside real
hazard zones, 3) blocked roads and access problems, 4) recommended priorities (top 3-5 with why). Mention counts of
people affected. Personal data has been redacted. Do not invent incidents."""


def situation_summary(context: str) -> tuple[str, dict]:
    text, meta = _call("cloud_summary", SUMMARY_SYSTEM.format(town=C.TOWN_NAME, scenario=C.SCENARIO_NAME), context,
                       C.ANTHROPIC_SUMMARY_MODEL, 700)
    return text.strip(), meta


SITUATION_SYSTEM = """You support the incident commander of {town} ({scenario}). You get the current incident list (personal data removed,
audio never left the device). Return ONLY a JSON object:
{{"situation": "<at most 150 words: overall picture, incidents inside the real fire hazard zones, blocked roads and access problems, people affected>",
  "suggestions": [{{"text": "<one concrete action>", "reason": "<why, citing the incident>", "report_ids": ["<report id>", ...]}}, ... 3 to 6 items, most urgent first],
  "open_questions": [{{"text": "<what the commander should find out>", "incident_id": "<incident id>"}}, ... 0 to 5 items]}}
The human operator decides; you only suggest. Use only the data given, never invent incidents, and copy ids exactly."""


def situation_and_suggestions(context: str, data_as_of: str) -> tuple[dict, dict]:
    """Returns (payload, meta). Raises on failure. Audio never leaves the device; the context is redacted text only."""
    raw, meta = _call("cloud_situation", SITUATION_SYSTEM.format(town=C.TOWN_NAME, scenario=C.SCENARIO_NAME),
                      context + "\nReturn the JSON object.", C.ANTHROPIC_SUMMARY_MODEL, 1500)
    payload = normalize_situation(_json_from(raw), data_as_of)
    return payload, meta


def normalize_situation(d: dict, data_as_of: str) -> dict:
    sugg = [{"text": str(s.get("text", "")).strip(), "reason": str(s.get("reason", "")).strip(),
             "report_ids": [str(x) for x in (s.get("report_ids") or []) if x]} for s in (d.get("suggestions") or []) if isinstance(s, dict) and s.get("text")]
    qs = [{"text": str(q.get("text", "")).strip(), "incident_id": str(q.get("incident_id") or "")} for q in (d.get("open_questions") or []) if isinstance(q, dict) and q.get("text")]
    return {"situation": str(d.get("situation", "")).strip(), "suggestions": sugg[:8], "open_questions": qs[:8], "data_as_of": data_as_of}


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * C.ANTHROPIC_PRICE_IN_PER_M + tokens_out / 1e6 * C.ANTHROPIC_PRICE_OUT_PER_M
