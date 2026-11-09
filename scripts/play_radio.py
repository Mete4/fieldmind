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
                r = await client.post(f"{C.API_URL}/ingest/audio", files={"file": (audio.name, f, "audio/wav")}, data={"channel": channel})
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


async def run(path: Path, speed: float, use_text: bool) -> None:
    data = json.loads(path.read_text())
    calls = data["calls"]
    base = path.parent
    print(f"playing {len(calls)} calls from {path.name} at {speed}x ({'text' if use_text else 'audio where available'})")
    started = time.time()
    results: list = []
    async with httpx.AsyncClient(timeout=180) as client:
        async def at(call: dict) -> None:
            await asyncio.sleep(call["offset_s"] / speed)
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
    a = ap.parse_args()
    asyncio.run(run(Path(a.scenario_json), a.speed, a.text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
