#!/usr/bin/env python3
"""Write simulated radio traffic for a scenario with a model, then (optionally)
voice it with a local TTS. Run ONCE while online, before the scenario plays;
this is not part of the system under test.

  .venv/bin/python scripts/generate_radio.py --scenario "Fire at Paradise High School. The east wing burns..." --id phs
  .venv/bin/python scripts/generate_radio.py --nist "<a NIST Camp Fire timeline event typed by a person>" --id nist1
  options: --calls 14  --local (use the local 32B instead of the cloud model when no ANTHROPIC_API_KEY)  --no-audio

Output: data/samples/radio/<id>.json = [{offset_s, channel, department, unit, text}, ...]
        data/samples/radio/<id>/<n>.wav  (Piper voice per unit + light radio noise)
The writer model is different from the extraction model (qwen2.5:14b) so extraction scores stay honest."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402
from app.redact import redact  # noqa: E402

RADIO_DIR = C.SAMPLE_DIR / "radio"
WRITER_SYSTEM = """You write realistic simulated emergency radio traffic for a training exercise in {town} during a wind-driven wildfire.
Output ONLY a JSON array of {n} objects, ordered by time: {{"offset_s": <seconds from scenario start, 0-{dur}>, "channel": "<FD|PD|EMS tactical channel name>",
"department": "<FD|PD|EMS>", "unit": "<call sign, e.g. Engine 81, Truck 82, Battalion 3, Medic 41, Paradise PD 7, Dispatch>", "text": "<one radio transmission, 20-60 words>"}}.
Rules: at least 3 departments (FD, PD, EMS); real radio style ("Engine 81 to Butte Command, ..."); use ONLY these real place names from the town gazetteer: {places}.
Several transmissions must give specific facts for a dispatcher to extract: the part of a structure involved, the direction the fire comes from, its size (small/medium/large),
how many people are affected, resources needed. Two or three transmissions must be unclear, garbled, or contradict an earlier one (a unit corrects a count or a direction).
Spread the calls over the whole duration with several units talking within seconds of each other. No real people's names, no phone numbers."""


def places_list(k: int = 60) -> list[str]:
    gaz = json.loads(C.GAZETTEER_PATH.read_text())
    pri = [e["name"] for e in gaz if e["type"] in ("school", "hospital", "fire_station", "police", "park", "major_road", "neighborhood", "civic", "landmark")]
    streets = [e["name"] for e in gaz if e["type"] == "street"]
    random.seed(7)
    return pri[:40] + random.sample(streets, min(k - min(40, len(pri)), len(streets)))


def write_calls(scenario: str, n: int, duration: int, use_local: bool) -> tuple[list[dict], str]:
    system = WRITER_SYSTEM.format(town=C.TOWN_NAME, n=n, dur=duration, places="; ".join(places_list()))
    user = f"Scenario (written by the exercise designer):\n{scenario}\n\nWrite the {n} radio transmissions as the JSON array."
    if not use_local and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        model = C.ANTHROPIC_MODEL
        r = anthropic.Anthropic(timeout=120).messages.create(model=model, max_tokens=4000, system=system, messages=[{"role": "user", "content": user}])
        raw = "".join(getattr(b, "text", "") for b in r.content)
    else:
        import ollama

        model = json.loads(C.MODELS_JSON.read_text()).get("summary_llm", "qwen2.5:32b") if C.MODELS_JSON.exists() else "qwen2.5:32b"
        r = ollama.Client(host=C.OLLAMA_HOST, timeout=600).chat(model=model, format="json", keep_alive=C.OLLAMA_KEEP_ALIVE,
                                                                 messages=[{"role": "system", "content": system + "\nReturn {\"calls\": [...]}."},
                                                                           {"role": "user", "content": user}],
                                                                 options={"temperature": 0.8, "num_ctx": 8192, "num_predict": 3500})
        raw = r.message.content
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
    data = json.loads(raw[raw.find("[" if raw.lstrip().startswith("[") else "{"):])
    calls = data.get("calls") if isinstance(data, dict) else data
    out = []
    for c in calls:
        text, _ = redact(str(c.get("text", "")).strip())
        if not text:
            continue
        out.append({"offset_s": float(c.get("offset_s", 0) or 0), "channel": str(c.get("channel", "TAC 1")), "department": str(c.get("department", "FD")).upper()[:5],
                    "unit": str(c.get("unit", "Unit")), "text": text})
    out.sort(key=lambda c: c["offset_s"])
    return out, model


# ----------------------------------------------------------------- TTS (Piper, local)
VOICE_DIR = Path.home() / ".local/share/fieldmind/piper-voices"
VOICES = ["en_US-lessac-medium", "en_US-ryan-medium", "en_US-joe-medium", "en_US-amy-medium", "en_US-kusal-medium", "en_US-arctic-medium"]


def _voice_files() -> list[Path]:
    return sorted(VOICE_DIR.glob("*.onnx"))


def ensure_voices(n: int) -> list[Path]:
    import subprocess

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    for name in VOICES[:n]:
        if not (VOICE_DIR / f"{name}.onnx").exists():
            subprocess.run([sys.executable, "-m", "piper.download_voices", name, "--data-dir", str(VOICE_DIR)], capture_output=True, text=True, timeout=600)
    return _voice_files()


def radio_fx(wav_in: Path, wav_out: Path, seed: int, department: str = "FD") -> None:
    """Scanner-recording feel: band-pass 300-3000 Hz, light compression and clipping, key-up click,
    squelch tail, a low noise floor and (FD) a little pump/wind rumble."""
    import numpy as np
    import soundfile as sf

    x, sr = sf.read(wav_in, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    rng = np.random.default_rng(seed)
    # band-pass in the frequency domain
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    spec[(f < 300) | (f > 3000)] *= 0.03
    spec[(f >= 300) & (f < 450)] *= 0.5
    y = np.fft.irfft(spec, n=len(x)).astype(np.float32)
    # light compression + a little clipping (hot mic)
    y = y / (np.max(np.abs(y)) + 1e-6)
    y = np.sign(y) * (np.abs(y) ** 0.7)
    y = np.clip(y * 1.35, -0.85, 0.85)
    # noise floor + (FD) pump/wind rumble
    noise = rng.normal(0, 0.006, len(y)).astype(np.float32)
    if department == "FD":
        t = np.arange(len(y)) / sr
        rumble = 0.012 * np.sin(2 * np.pi * 38 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.7 * t)) + rng.normal(0, 0.01, len(y)) * 0.5
        rs = np.fft.rfft(rumble.astype(np.float32)); rs[f > 400] *= 0.1
        noise = noise + np.fft.irfft(rs, n=len(y)).astype(np.float32)
    y = 0.9 * y + noise
    # key-up click, squelch tail
    click = rng.normal(0, 0.3, int(0.012 * sr)).astype(np.float32) * np.linspace(1, 0, int(0.012 * sr))
    gap = np.zeros(int(0.06 * sr), dtype=np.float32)
    tail = rng.normal(0, 0.18, int(0.09 * sr)).astype(np.float32) * np.linspace(1, 0, int(0.09 * sr)) ** 0.5
    y = np.concatenate([click, gap, y, gap[: int(0.02 * sr)], tail]).astype(np.float32)
    sf.write(wav_out, y, sr, subtype="PCM_16")


def voice_calls(calls: list[dict], out_dir: Path) -> int:
    import wave

    from piper import PiperVoice

    out_dir.mkdir(parents=True, exist_ok=True)
    units = sorted({c["unit"] for c in calls})
    files = ensure_voices(min(len(units), len(VOICES)))
    if not files:
        print("no Piper voices available, skipping audio")
        return 0
    voices = {}
    done = 0
    for i, c in enumerate(calls, 1):
        vf = files[units.index(c["unit"]) % len(files)]
        if vf not in voices:
            voices[vf] = PiperVoice.load(str(vf))
        raw = out_dir / f"{i}.raw.wav"
        from piper import SynthesisConfig

        with wave.open(str(raw), "wb") as w:
            voices[vf].synthesize_wav(c["text"].replace("[static]", "...").replace("[unintelligible]", "..."), w,
                                      syn_config=SynthesisConfig(length_scale=0.88, noise_scale=0.6, noise_w_scale=0.7))
        radio_fx(raw, out_dir / f"{i}.wav", seed=i, department=str(c.get("department", "FD")).upper())
        raw.unlink()
        c["audio"] = f"{out_dir.name}/{i}.wav"
        done += 1
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scenario", help="short scenario text written by a person")
    g.add_argument("--nist", help="a NIST Camp Fire timeline event, typed in")
    ap.add_argument("--id", required=True, help="scenario id (file name)")
    ap.add_argument("--calls", type=int, default=14)
    ap.add_argument("--duration", type=int, default=240, help="seconds the traffic spans")
    ap.add_argument("--local", action="store_true", help="write with the local 32B instead of the cloud model")
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args()
    scenario = a.scenario or f"Reconstructed from the NIST Camp Fire timeline (Paradise, 8 Nov 2018): {a.nist}"
    t0 = time.time()
    calls, model = write_calls(scenario, a.calls, a.duration, a.local)
    print(f"{len(calls)} calls written by {model} in {time.time() - t0:.0f}s; departments {sorted({c['department'] for c in calls})}; units {sorted({c['unit'] for c in calls})}")
    n_audio = 0
    if not a.no_audio:
        try:
            t1 = time.time()
            n_audio = voice_calls(calls, RADIO_DIR / a.id)
            print(f"{n_audio} clips voiced with Piper in {time.time() - t1:.0f}s")
        except Exception as e:  # noqa: BLE001
            print(f"TTS failed ({type(e).__name__}: {e}); text only")
    RADIO_DIR.mkdir(parents=True, exist_ok=True)
    (RADIO_DIR / f"{a.id}.json").write_text(json.dumps({"id": a.id, "scenario": scenario, "writer_model": model, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                        "calls": calls}, indent=1, ensure_ascii=False))
    for c in calls:
        print(f"  t+{c['offset_s']:>5.0f}s [{c['department']:<3} {c['channel']:<10}] {c['unit']:<16} {c['text'][:90]}")
    print(f"wrote data/samples/radio/{a.id}.json ({n_audio} audio clips)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
