#!/usr/bin/env python3
"""Play a generated scenario against the API: every call is sent at its
offset_s, channels run at the same time (asyncio). Audio clips go to
POST /ingest/audio (Whisper on the GPU); calls without audio, or with
--text, go to POST /ingest/text.

  .venv/bin/python scripts/play_radio.py data/samples/radio/phs.json [--speed 4] [--text]"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402


async def send(client: httpx.AsyncClient, call: dict, base: Path, use_text: bool, started: float, results: list) -> None:
    audio = base / call["audio"] if call.get("audio") else None
    channel = f"{call['department']} {call['channel']}".strip()
    t0 = time.perf_counter()
    try:
        if audio and audio.exists() and not use_text:
            with open(audio, "rb") as f:
                r = await client.post(f"{C.API_URL}/ingest/audio", files={"file": (str(audio.relative_to(C.SAMPLE_DIR / "radio")), f, "audio/wav")}, data={"channel": channel})
        else:
            r = await client.post(f"{C.API_URL}/ingest/text", json={"text": call["text"], "channel": channel})
        d = r.json()
        inc = d.get("incident") or {}
        stt = d.get("stt") or {}
        line = (f"t+{time.time() - started:5.0f}s {call['unit']:<16} -> {d.get('link', '?'):<6} {inc.get('type', '?'):<12} sev {inc.get('severity', '?')} "
                f"ppl {inc.get('people_count')} dir {inc.get('fire_direction')} size {inc.get('fire_size')} | {int((time.perf_counter() - t0) * 1000)} ms"
                + (f" | stt rtf {stt.get('rtf')}" if stt else ""))
        results.append(d)
    except Exception as e:  # noqa: BLE001
        line = f"t+{time.time() - started:5.0f}s {call['unit']:<16} FAILED {type(e).__name__}: {e}"
    print(line, flush=True)


async def stream_call(call: dict, base: Path, speed: float, started: float, results: list) -> None:
    """Section 6: push the clip as 0.5 s PCM16 chunks over the websocket at (compressed) real time."""
    import websockets

    from app.stt import load_audio

    audio, dur = load_audio(base / call["audio"])
    pcm = (audio * 32767).astype("int16").tobytes()
    channel = f"{call['department']} {call['channel']}".strip().replace(" ", "_")
    chunk = int(0.5 * 16000) * 2
    t0 = time.perf_counter()
    try:
        src = str((base / call["audio"]).relative_to(C.SAMPLE_DIR / "radio"))
        async with websockets.connect(f"{C.API_URL.replace('http', 'ws')}/ws/radio/{channel}?source={src}", max_size=None) as ws:
            async def reader() -> None:
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("done"):
                        return
                    inc = d.get("incident") or {}
                    print(f"t+{time.time() - started:5.0f}s {call['unit']:<16} seg {d.get('segment')} ({d.get('audio_s')}s, cut {d.get('cut')}) stt {d.get('latency_ms')} ms rtf {d.get('rtf')} "
                          f"-> {d.get('link', '-'):<6} {inc.get('type', '?')} sev {inc.get('severity', '?')} ppl {inc.get('people_count')} | {str(d.get('text', ''))[:70]}", flush=True)
                    results.append(d)

            rtask = asyncio.create_task(reader())
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i + chunk])
                await asyncio.sleep(0.5 / speed)
            await ws.send("END")
            await asyncio.wait_for(rtask, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"t+{time.time() - started:5.0f}s {call['unit']:<16} STREAM FAILED {type(e).__name__}: {e}", flush=True)


async def run(path: Path, speed: float, use_text: bool, stream: bool = False) -> None:
    data = json.loads(path.read_text())
    calls = data["calls"]
    base = path.parent
    print(f"playing {len(calls)} calls from {path.name} at {speed}x ({'text' if use_text else 'audio where available'})")
    started = time.time()
    results: list = []
    async with httpx.AsyncClient(timeout=180) as client:
        async def at(call: dict) -> None:
            await asyncio.sleep(call["offset_s"] / speed)
            if stream and call.get("audio") and (base / call["audio"]).exists() and not use_text:
                await stream_call(call, base, speed, started, results)
            else:
                await send(client, call, base, use_text, started, results)

        await asyncio.gather(*(at(c) for c in calls))
    incs = {(r.get("incident") or {}).get("id") for r in results if r.get("incident")}
    merged = sum(1 for r in results if r.get("link") == "merged")
    print(f"done in {time.time() - started:.0f}s: {len(results)} calls -> {len(incs)} incidents ({merged} merged)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario_json")
    ap.add_argument("--speed", type=float, default=1.0, help="time compression, e.g. 4 = four times faster")
    ap.add_argument("--text", action="store_true", help="send text even when audio exists")
    ap.add_argument("--stream", action="store_true", help="stream audio in 0.5 s chunks over the websocket (VAD-cut live transcription)")
    a = ap.parse_args()
    asyncio.run(run(Path(a.scenario_json), a.speed, a.text, a.stream))
    return 0


if __name__ == "__main__":
    sys.exit(main())
