"""FieldMind edge API (FastAPI, async). Blocking model work runs in worker
threads so parallel radio channels really overlap. Start with:
  .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
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


class TextIn(BaseModel):
    text: str
    channel: str = "text"


class QuestionIn(BaseModel):
    question: str


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


@app.websocket("/ws/radio/{channel}")
async def ws_radio(ws: WebSocket, channel: str):
    """Live radio channel: binary frames of PCM16 little-endian 16 kHz mono (any chunk size,
    0.5 s recommended), text 'END' to close. Each speech segment (cut at pauses by VAD) is
    transcribed as soon as it ends and becomes a report; one JSON message per segment."""
    import numpy as np

    from app.stt_stream import StreamSession

    await ws.accept()
    loop = asyncio.get_running_loop()
    outq: asyncio.Queue = asyncio.Queue()

    def on_segment(seg: dict) -> None:
        if seg.get("text"):
            try:
                metrics.inflight["audio"] += 1
                report, inc, action = pipeline.ingest_text(seg["text"], channel=channel, input_type="audio", source_file="live-stream",
                                                           stt={"backend": seg.get("backend"), "latency_ms": seg["latency_ms"], "rtf": seg["rtf"], "audio_s": seg["audio_s"]})
                seg.update(report_id=report.id, incident_id=inc.id if inc else None, link=action,
                           incident={k: inc.model_dump().get(k) for k in ("type", "severity", "people_count", "fire_direction", "fire_size", "geocode_match")} if inc else None)
            except Exception as e:  # noqa: BLE001
                seg["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            finally:
                metrics.inflight["audio"] -= 1
        loop.call_soon_threadsafe(outq.put_nowait, seg)

    session = StreamSession(channel, on_segment)

    async def pump() -> None:
        while True:
            msg = await outq.get()
            await ws.send_json(msg)

    pumper = asyncio.create_task(pump())
    try:
        while True:
            m = await ws.receive()
            if m.get("bytes"):
                pcm = np.frombuffer(m["bytes"], dtype=np.int16).astype(np.float32) / 32768.0
                await asyncio.to_thread(session.feed, pcm)
            elif m.get("text") == "END" or m.get("type") == "websocket.disconnect":
                break
        await asyncio.to_thread(session.flush)
        for _ in range(400):                     # wait for in-flight transcriptions + ingest (max ~2 min)
            if session.pending() <= 0:
                break
            await asyncio.sleep(0.3)
        while not outq.empty():                  # let the pump flush queued segment messages
            await asyncio.sleep(0.05)
        await ws.send_json({"done": True, "stats": session.stats, "segments": session.segments})
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


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
    st = network.state()
    ready, reason = network.cloud_ready()
    return {**st.model_dump(), "cloud_ready": ready, "cloud_reason": reason, "last_situation": router.last_run}


@app.get("/queue")
async def queue():
    item = db.pending_situation()
    return {"size": 1 if item else 0, "pending_situation": item, "last_situation": router.last_run}


@app.get("/situation")
async def situation_latest():
    return db.latest_situation() or {}


@app.post("/situation")
async def situation_update():
    return await asyncio.to_thread(router.run_situation, "operator")


class DecisionIn(BaseModel):
    action: str


class AssignIn(BaseModel):
    incident_id: Optional[str] = None
    status: str = "assigned"


class AnswerIn(BaseModel):
    answer: str


class InfoRequestIn(BaseModel):
    incident_id: str
    unit_id: str
    what_to_find: str


@app.get("/suggestions")
async def suggestions(status: Optional[str] = None):
    return db.list_suggestions(status)


@app.post("/suggestions/{sid}")
async def suggestion_decide(sid: int, body: DecisionIn):
    if body.action not in ("accept", "reject"):
        raise HTTPException(400, "action must be accept or reject")
    row = db.decide_suggestion(sid, body.action)
    if row is None:
        raise HTTPException(404, "no such suggestion")
    return row


@app.get("/units")
async def units():
    return db.list_units()


@app.post("/units/{unit_id}/assign")
async def unit_assign(unit_id: str, body: AssignIn):
    if body.status not in ("assigned", "available", "out_of_service"):
        raise HTTPException(400, "bad status")
    row = db.assign_unit(unit_id, body.incident_id, body.status)
    if row is None:
        raise HTTPException(404, "no such unit")
    return row


@app.get("/questions")
async def questions(status: Optional[str] = None):
    return db.list_questions(status)


@app.post("/questions/{qid}/answer")
async def question_answer(qid: int, body: AnswerIn):
    row = db.answer_question(qid, body.answer)
    if row is None:
        raise HTTPException(404, "no such question")
    return row


@app.get("/info_requests")
async def info_requests():
    return db.list_info_requests()


@app.post("/info_requests")
async def info_request_create(body: InfoRequestIn):
    return db.create_info_request(body.incident_id, body.unit_id, body.what_to_find)


@app.post("/info_requests/{rid}/answer")
async def info_request_answer(rid: int, body: AnswerIn):
    row = db.answer_info_request(rid, body.answer)
    if row is None:
        raise HTTPException(404, "no such info request")
    return row


@app.get("/decisions")
async def decisions(limit: int = 100):
    return db.list_decisions(limit)


@app.get("/metrics")
async def metrics_get():
    return await asyncio.to_thread(metrics.snapshot)


@app.post("/query")
async def query_post(body: QuestionIn):
    if not body.question.strip():
        raise HTTPException(400, "question is required")
    from app import query

    return await asyncio.to_thread(query.answer, body.question)


@app.get("/samples")
async def samples():
    return load_reports()


@app.post("/reset")
async def reset():
    db.reset_db()
    return {"ok": True}
