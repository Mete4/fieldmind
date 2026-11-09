"""Ingest pipeline = the glue between raw REPORTS and triaged INCIDENTS.

  report (saved at once)  ->  edge extraction (LLM + geocode + hazards)
                          ->  LINK RULE  ->  new incident, or merged into one

Link rule (app/config.py): a report joins an existing incident when it has the
same type, lies within LINK_RADIUS_M metres of it, arrives within
LINK_WINDOW_MIN minutes of that incident's last report, and both locations
are known. Merging keeps max severity, max people_count, the union of needs,
the higher confidence, the latest transcript; priority is recomputed.
LINK_RADIUS_M = 0 turns merging off (one report = one incident)."""
from __future__ import annotations

import datetime as dt
import math
import threading
import time
from typing import Optional

from app import config as C
from app import db, hazards, orchestrator
from app.extract import process_text
from app.schemas import LABEL_PREFIX, TYPE_SEVERITY_ORDER, Incident, Report, now_iso

_link_lock = threading.Lock()      # serialize "find target + save" so parallel channels cannot double-open


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_link_target(cand: Incident) -> tuple[Optional[Incident], str]:
    """The nearest open incident the candidate may join, and why / why not."""
    if C.LINK_RADIUS_M <= 0:
        return None, "merging disabled"
    if cand.type == "medical" and (cand.people_count or 0) <= 2:
        pass                                       # a single patient still joins the event at the same place
    if cand.location_unknown:
        # no place in the call: join the incident this unit reported most recently (follow-up traffic)
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=C.LINK_WINDOW_MIN)).isoformat(timespec="seconds")
        unit = (cand.unit or [""])[0]
        if unit:
            for inc in db.candidates_for_link(None, since):
                if unit in inc.unit and inc.report_count < C.LINK_MAX_REPORTS:
                    return inc, f"no place given; follow-up from {unit}, which last reported {inc.label or inc.id}"
        return None, "location unknown, opened separately"
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=C.LINK_WINDOW_MIN)).isoformat(timespec="seconds")
    best, best_d = None, None
    for inc in db.candidates_for_link(cand.type if C.LINK_SAME_TYPE else None, since):
        if inc.location_unknown or inc.report_count >= C.LINK_MAX_REPORTS:
            continue
        d = haversine_m(cand.lat, cand.lon, inc.lat, inc.lon)
        if d <= C.LINK_RADIUS_M and (best_d is None or d < best_d):
            best, best_d = inc, d
    if best is None:
        return None, f"no open incident within {C.LINK_RADIUS_M} m / {C.LINK_WINDOW_MIN} min"
    return best, f"{best_d:.0f} m from {best.label or best.id} ({best.type}) reported {best.report_count}x"


_SIZE_RANK = {None: 0, "small": 1, "medium": 2, "large": 3}


def merge_into(inc: Incident, cand: Incident, report: Report) -> Incident:
    inc.severity = max(inc.severity, cand.severity)
    if cand.type not in inc.types:
        inc.types.append(cand.type)
    if TYPE_SEVERITY_ORDER.get(cand.type, 0) > TYPE_SEVERITY_ORDER.get(inc.type, 0):
        inc.type = cand.type                       # most severe type of its reports; label stays stable
    inc.department = inc.department + [d for d in cand.department if d not in inc.department]
    inc.unit = inc.unit + [u for u in cand.unit if u not in inc.unit]
    if _SIZE_RANK.get(cand.fire_size, 0) > _SIZE_RANK.get(inc.fire_size, 0):
        inc.fire_size = cand.fire_size
    inc.fire_direction = cand.fire_direction or inc.fire_direction
    inc.structure_part = cand.structure_part or inc.structure_part
    if cand.details:
        inc.details = f"[{report.channel}] {cand.details}\n{inc.details}".strip()
    if cand.people_count is not None:
        inc.people_count = max(inc.people_count or 0, cand.people_count)
    inc.needs = (inc.needs + [n for n in cand.needs if n not in inc.needs])[:8]
    if cand.confidence >= inc.confidence:
        inc.confidence = round(cand.confidence, 3)
        if cand.summary:
            inc.summary = cand.summary
    inc.transcript = cand.transcript
    inc.last_ts = now_iso()
    inc.report_count += 1
    inc.report_ids.append(report.id)
    if report.channel not in inc.channels:
        inc.channels.append(report.channel)
    hazards.annotate(inc)                      # recompute priority (location unchanged)
    return inc


def ingest_text(text: str, channel: str = "text", input_type: str = "text", source_file: Optional[str] = None,
                stt: Optional[dict] = None, model: Optional[str] = None) -> tuple[Report, Optional[Incident], str]:
    """Save the raw report, extract on the edge, apply the link rule, save the
    incident, link the report. Returns (report, incident, 'new'|'merged'|'failed')."""
    t0 = time.perf_counter()
    stt = stt or {}
    report = Report(channel=channel, input_type=input_type, text=text.strip(), source_file=source_file,
                    stt_backend=stt.get("backend"), stt_latency_ms=stt.get("latency_ms"), stt_rtf=stt.get("rtf"),
                    audio_s=stt.get("audio_s"))
    db.save_report(report)                     # on disk before any model runs
    inc: Optional[Incident] = None
    action = "failed"
    try:
        cand = process_text(text, channel=channel, input_type=input_type, model=model)
        cand.report_ids, cand.channels, cand.types = [report.id], [channel], [cand.type]
        if cand.details:
            cand.details = f"[{channel}] {cand.details}"
        report.department = cand.department[0] if cand.department else "other"
        report.unit = cand.unit[0] if cand.unit else ""
        report.type, report.severity, report.people_count = cand.type, cand.severity, cand.people_count
        report.location_text, report.confidence = cand.location_text, cand.confidence
        report.structure_part, report.fire_direction, report.fire_size = cand.structure_part, cand.fire_direction, cand.fire_size
        report.details = cand.details
        with _link_lock:
            target, reason = find_link_target(cand)
            if target is not None:
                inc, action = merge_into(target, cand, report), "merged"
            else:
                inc, action = cand, "new"
                inc.label = db.next_label(LABEL_PREFIX.get(inc.type, "OTHER"))
            db.save_incident(inc)
        orchestrator.evaluate_async(inc.model_copy(deep=True), action)
        report.status, report.incident_id, report.link_action, report.link_reason = "processed", inc.id, action, reason
    except Exception as e:  # noqa: BLE001
        report.status, report.error = "failed", f"{type(e).__name__}: {str(e)[:180]}"
    report.processing_ms = int((time.perf_counter() - t0) * 1000)
    db.save_report(report)
    return report, inc, action


def ingest_audio(source, channel: str = "audio", source_file: Optional[str] = None,
                 model: Optional[str] = None) -> tuple[Report, Optional[Incident], str, dict]:
    """Whisper on the GPU, then the text pipeline. Returns (report, incident, action, stt_result)."""
    from app.stt import transcribe   # lazy: loads torch/Whisper only when audio is used

    res = transcribe(source, channel=channel)
    if res["error"] or not res["text"].strip():
        report = Report(channel=channel, input_type="audio", text="", source_file=source_file, status="failed",
                        stt_backend=res.get("backend"), stt_latency_ms=res.get("latency_ms"), stt_rtf=res.get("rtf"),
                        audio_s=res.get("audio_s"), error=res["error"] or "empty transcript")
        db.save_report(report)
        return report, None, "failed", res
    report, inc, action = ingest_text(res["text"], channel=channel, input_type="audio", source_file=source_file, stt=res, model=model)
    return report, inc, action, res
