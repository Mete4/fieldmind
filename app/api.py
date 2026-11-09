"""FieldMind edge API (FastAPI, async). Blocking model work runs in worker
threads so parallel radio channels really overlap. Start with:
  .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import config as C
from app import db, hazards, metrics, network, pipeline, router
from app.samples import load_reports


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    hazards.get_index()
    router.start_background_sync()

    def warm_stt() -> None:   # load Whisper on the GPU at startup so the first clip is fast
        try:
            from app.stt import get_engine

            get_engine().load()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=warm_stt, daemon=True).start()
    yield
    router.stop_background_sync()


app = FastAPI(title="FieldMind edge API", version="0.1", lifespan=lifespan)


class NetworkToggle(BaseModel):
    online: bool


class TextIn(BaseModel):
    text: str
    channel: str = "text"


def _ingest_text_sync(text: str, channel: str, input_type: str = "text", source_file: Optional[str] = None, stt: Optional[dict] = None) -> dict:
    metrics.inflight[input_type] += 1
    try:
        report, inc, action = pipeline.ingest_text(text, channel=channel, input_type=input_type, source_file=source_file, stt=stt)
        decision = None
        if inc is not None:
            inc, decision = router.route_incident(inc)
        return {"report": report.model_dump(), "incident": inc.model_dump() if inc else None, "link": action, "routing": decision}
    finally:
        metrics.inflight[input_type] -= 1


@app.get("/health")
async def health():
    return {"ok": True, "town": C.TOWN_NAME, "banner": C.SCENARIO_BANNER, "network": network.state().model_dump(),
            "cloud_ready": network.cloud_ready(), "models_loaded": metrics.models_loaded()}


@app.post("/ingest/text")
async def ingest_text(body: Optional[TextIn] = None, text: Optional[str] = Form(None), channel: Optional[str] = Form(None)):
    t = body.text if body else text
    ch = (body.channel if body else channel) or "text"
    if not t or not t.strip():
        raise HTTPException(400, "text is required")
    return await asyncio.to_thread(_ingest_text_sync, t, ch)


@app.post("/ingest/audio")
async def ingest_audio(file: UploadFile = File(...), channel: str = Form("audio")):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")

    def work() -> dict:
        metrics.inflight["audio"] += 1
        try:
            report, inc, action, stt = pipeline.ingest_audio(data, channel=channel, source_file=file.filename)
            decision = None
            if inc is not None:
                inc, decision = router.route_incident(inc)
            return {"report": report.model_dump(), "incident": inc.model_dump() if inc else None, "link": action,
                    "routing": decision, "stt": {k: stt.get(k) for k in ("text", "audio_s", "latency_ms", "rtf", "backend", "error")}}
        finally:
            metrics.inflight["audio"] -= 1

    return await asyncio.to_thread(work)


@app.get("/incidents")
async def incidents(order: str = "priority", limit: int = 500):
    return [i.model_dump() for i in await asyncio.to_thread(db.list_incidents, limit, order)]


@app.get("/incidents/{incident_id}")
async def incident(incident_id: str):
    inc = await asyncio.to_thread(db.get_incident, incident_id)
    if inc is None:
        raise HTTPException(404, "no such incident")
    return {"incident": inc.model_dump(), "reports": [r.model_dump() for r in db.list_reports(incident_id=incident_id)]}


@app.get("/reports")
async def reports(limit: int = 500, incident_id: Optional[str] = None):
    return [r.model_dump() for r in await asyncio.to_thread(db.list_reports, limit, incident_id)]


@app.get("/hazards")
async def hazards_list():
    return {"layers": hazards.get_index().layer_summary(), "polygons_indexed": hazards.get_index().polygon_count,
            "zone_styles": {k: {"label": v[0], "color": v[1]} for k, v in C.ZONE_STYLES.items()}}


@app.get("/network")
async def network_get():
    s = network.state()
    ready, reason = network.cloud_ready()
    return {**s.model_dump(), "cloud_ready": ready, "cloud_reason": reason, "last_drain": router.last_drain}


@app.post("/network")
async def network_set(body: NetworkToggle):
    s = await asyncio.to_thread(network.set_manual_online, body.online)
    ready, reason = network.cloud_ready()
    return {**s.model_dump(), "cloud_ready": ready, "cloud_reason": reason}


@app.get("/queue")
async def queue():
    items = db.pending_queue(100)
    return {"size": len(items), "items": items, "last_drain": router.last_drain}


@app.post("/queue/drain")
async def queue_drain():
    return await asyncio.to_thread(router.drain_queue)


@app.post("/summary")
async def summary(force_edge: bool = False):
    return await asyncio.to_thread(router.make_summary, force_edge)


@app.get("/summary")
async def summary_latest():
    return db.latest_summary() or {}


@app.get("/metrics")
async def metrics_get():
    return await asyncio.to_thread(metrics.snapshot)


@app.get("/samples")
async def samples():
    return load_reports()


@app.post("/reset")
async def reset():
    db.reset_db()
    return {"ok": True}
