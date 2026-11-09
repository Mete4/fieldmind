#!/usr/bin/env python3
"""Phase 2 STT test: load Whisper on the GPU (timed), transcribe one clip or
all sample clips (timed, real-time factor), and compare with the source text.

  .venv/bin/python scripts/test_stt.py                       # data/samples/audio/1.mp3
  .venv/bin/python scripts/test_stt.py path/to/clip.wav
  .venv/bin/python scripts/test_stt.py --all                 # every <n>.mp3/.wav in data/samples/audio
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402
from app import db  # noqa: E402
from app.samples import load_reports  # noqa: E402
from app.stt import get_engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", nargs="?", default=str(C.SAMPLE_DIR / "audio" / "1.mp3"))
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    db.init_db()
    eng = get_engine()
    print(f"FieldMind STT test | backend preference: {C.WHISPER_BACKEND} | model {C.WHISPER_MODEL}")
    t0 = time.perf_counter()
    b = eng.load()
    print(f"loaded backend '{b.name}' ({b.model_name}) on {b.device} in {time.perf_counter() - t0:.1f}s; skipped: {eng.errors}\n")
    if a.all:
        from rapidfuzz import fuzz

        src = {r["id"]: r["text"] for r in load_reports()}
        clips = sorted((p for p in (C.SAMPLE_DIR / "audio").iterdir() if p.suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg") and p.stem.isdigit()),
                       key=lambda p: int(p.stem))
    else:
        src, clips = {}, [Path(a.clip)]
    rtfs, sims = [], []
    print("clip      audio_s  latency_ms   RTF   match%  transcript")
    for p in clips:
        r = eng.transcribe(p)
        if r["error"]:
            print(f"{p.name:<9} ERROR {r['error']}")
            continue
        rid = int(p.stem) if p.stem.isdigit() else None
        sim = fuzz.ratio(r["text"].lower(), src[rid].lower()) if rid in src else None
        rtfs.append(r["rtf"])
        if sim is not None:
            sims.append(sim)
        print(f"{p.name:<9} {r['audio_s']:7.1f} {r['latency_ms']:11d} {r['rtf']:6.3f} {('%5.0f' % sim) if sim is not None else '    -'}   {r['text'][:110]}")
    if rtfs:
        print(f"\n{len(rtfs)} clips | mean RTF {sum(rtfs) / len(rtfs):.3f} (1.0 = real time; lower is faster) | "
              f"total audio {sum(r for r in [0]) :.0f}" if False else
              f"\n{len(rtfs)} clips | mean RTF {sum(rtfs) / len(rtfs):.3f} (1.0 = real time; lower is faster)"
              + (f" | mean text match {sum(sims) / len(sims):.0f}% vs reports.txt" if sims else ""))
    print("backend info:", eng.info())
    return 0 if rtfs else 1


if __name__ == "__main__":
    sys.exit(main())
