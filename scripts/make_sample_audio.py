#!/usr/bin/env python3
"""ONE-TIME, online: synthesize the simulated radio reports in
data/samples/reports.txt to data/samples/audio/<n>.mp3 with Microsoft Edge
neural voices (free, via the edge-tts package), so the audio channel can be
demoed without recording. Replace any file with a real recording if you like.
Existing files are kept unless --force."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402
from app.samples import load_reports  # noqa: E402

VOICES = ["en-US-GuyNeural", "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-RogerNeural",
          "en-US-SteffanNeural", "en-US-JennyNeural", "en-US-MichelleNeural", "en-US-AriaNeural"]


async def synth(text: str, voice: str, out: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice, rate="+10%").save(str(out))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out_dir = C.SAMPLE_DIR / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = load_reports()
    done = skipped = failed = 0
    for i, r in enumerate(reports):
        out = out_dir / f"{r['id']}.mp3"
        if out.exists() and not a.force:
            skipped += 1
            continue
        voice = VOICES[i % len(VOICES)]
        text = r["text"].replace("[static]", "...").replace("[unintelligible]", "...")
        try:
            asyncio.run(synth(text, voice, out))
            done += 1
            print(f"  {out.name:<8} {voice:<26} {out.stat().st_size / 1024:6.0f} KB  {r['channel']}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  {out.name}: FAILED {e}")
    print(f"synthesized {done}, kept {skipped}, failed {failed} -> {out_dir.relative_to(ROOT)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
