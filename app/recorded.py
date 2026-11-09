"""Replay of the recorded cloud response (data/samples/radio/cloud_recorded_camp.json).
NOT a live API call: the dashboard labels it "Recorded cloud response". Every
item carries a match block {type, location_text}; we geocode the place and
attach the nearest real incident of that type (else any type) within 700 m, so
the operator sees real IDs such as FIRE-001, or the place name when nothing matches."""
from __future__ import annotations

import json
from typing import Optional

from app import config as C
from app import db
from app.geocode import get_geocoder
from app.pipeline import haversine_m

PATH = C.SAMPLE_DIR / "radio" / "cloud_recorded_camp.json"


def _match(m: Optional[dict], incidents: list) -> dict:
    if not m:
        return {"label": None, "incident_id": None, "place": None}
    place = m.get("location_text") or ""
    g = None
    try:
        g = get_geocoder().geocode(place)
    except Exception:  # noqa: BLE001
        pass
    if not g:
        return {"label": None, "incident_id": None, "place": place}
    best, best_d = None, None
    for same_type in (True, False):
        for inc in incidents:
            if inc.location_unknown or (same_type and inc.type != m.get("type") and m.get("type") not in inc.types):
                continue
            d = haversine_m(g["lat"], g["lon"], inc.lat, inc.lon)
            if d <= 700 and (best_d is None or d < best_d):
                best, best_d = inc, d
        if best:
            break
    if best:
        return {"label": best.label or best.id, "incident_id": best.id, "place": g["match"], "distance_m": round(best_d)}
    return {"label": None, "incident_id": None, "place": g["match"]}


def replay() -> dict:
    if not PATH.exists():
        return {"ok": False, "reason": f"{PATH.name} missing"}
    raw = json.loads(PATH.read_text())
    incidents = db.list_incidents(limit=500)
    newest = max((i.last_ts for i in incidents), default="")

    def items(key: str) -> list[dict]:
        out = []
        for it in raw.get(key, []) or []:
            ref = _match(it.get("match"), incidents)
            out.append({**{k: v for k, v in it.items() if k != "match"}, "ref": ref, "match": it.get("match")})
        return out

    payload = {"recorded": True, "label": raw.get("label", "Recorded cloud response"), "model": raw.get("model"), "note": raw.get("note"),
               "situation": raw.get("situation", ""), "priorities": items("priorities"), "suggestions": items("suggestions"),
               "contradictions": items("contradictions"), "open_questions": items("open_questions"), "resolved": items("resolved"),
               "data_as_of": newest}
    # write suggestions / questions to the operator tables (source cloud) so Accept/Reject work
    sit = {"situation": payload["situation"],
           "suggestions": [{"text": f"{(s['ref']['label'] or s['ref']['place'] or '')}: {s['text']}", "reason": s.get("reason", ""),
                            "report_ids": []} for s in payload["suggestions"]],
           "open_questions": [{"text": q["text"], "incident_id": q["ref"]["incident_id"] or ""} for q in payload["open_questions"]],
           "data_as_of": newest}
    sid = db.save_situation("cloud", f"{raw.get('model')} (recorded)", 0, newest, {**sit, "recorded": True, "label": payload["label"]}, len(incidents), "recorded")
    payload["situation_id"] = sid
    payload["db_suggestions"] = db.list_suggestions()
    return {"ok": True, **payload}
