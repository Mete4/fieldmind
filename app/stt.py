"""Speech-to-text on the GPU, timed. Backend order for WHISPER_BACKEND=auto:
  1. faster  : faster-whisper (CTranslate2) on CUDA. Not possible on aarch64
               (the wheel is CPU-only), kept for x86 workstations.
  2. transformers : openai/whisper-large-v3 in float16 on torch CUDA (the PGX path).
  3. whispercpp   : whisper.cpp CUDA binary built under ~/.local/share/fieldmind.
  4. faster_cpu   : faster-whisper int8 on the 20 ARM cores (last resort).
transcribe() returns text + latency_ms + audio_s + rtf and logs route edge_stt."""
from __future__ import annotations

import io
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, Union

import numpy as np

from app import config as C
from app import db
from app.schemas import RequestLog

SAMPLE_RATE = 16000
AudioSource = Union[str, Path, bytes]


# ----------------------------------------------------------------- audio decode
def load_audio(source: AudioSource) -> tuple[np.ndarray, float]:
    """Decode wav/mp3/flac/m4a/ogg/webm to mono float32 16 kHz with PyAV
    (bundled ffmpeg). Returns (samples, duration_s)."""
    import av

    container = av.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else str(source))
    stream = next(s for s in container.streams if s.type == "audio")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    for frame in container.decode(stream):
        for out in resampler.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    for out in resampler.resample(None):          # flush
        chunks.append(out.to_ndarray().reshape(-1))
    container.close()
    audio = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)
    return audio, len(audio) / SAMPLE_RATE


# ----------------------------------------------------------------- backends
class _Backend:
    name = "none"
    model_name = ""
    device = "none"

    def transcribe(self, audio: np.ndarray) -> str:  # pragma: no cover
        raise NotImplementedError


class FasterWhisperBackend(_Backend):
    def __init__(self, device: str) -> None:
        from faster_whisper import WhisperModel

        self.name = "faster" if device == "cuda" else "faster_cpu"
        self.device = device
        self.model_name = f"faster-whisper {C.WHISPER_MODEL}"
        kw = {"compute_type": C.WHISPER_COMPUTE_TYPE} if device == "cuda" else {"compute_type": "int8", "cpu_threads": 16}
        self.model = WhisperModel(C.WHISPER_MODEL, device=device, **kw)

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self.model.transcribe(audio, language=C.WHISPER_LANGUAGE, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


class TransformersBackend(_Backend):
    def __init__(self) -> None:
        import torch
        from transformers import pipeline

        if not torch.cuda.is_available():
            raise RuntimeError("torch.cuda.is_available() is False")
        self.name, self.device = "transformers", torch.cuda.get_device_name(0)
        self.model_name = C.WHISPER_HF_MODEL
        self.pipe = pipeline("automatic-speech-recognition", model=C.WHISPER_HF_MODEL, dtype=torch.float16,
                             device="cuda:0", model_kwargs={"attn_implementation": "sdpa"})
        # vocabulary prompt: real place names + radio call signs, so Whisper spells them right
        self.prompt_ids = None
        try:
            import json

            names = [e["name"] for e in json.loads(C.GAZETTEER_PATH.read_text()) if e["type"] in ("school", "hospital", "major_road", "neighborhood", "park", "fire_station")]
            streets = ["Pentz Road", "Wagstaff Road", "Bille Road", "Clark Road", "Pearson Road", "Neal Road", "Elliott Road", "Edgewood Lane", "Oliver Road", "Skyway", "Maxwell Drive"]
            vocab = "Butte ECC, Engine 81, Battalion 3, Medic 41, Paradise PD 7. " + ", ".join(dict.fromkeys(streets + names[:40])) + "."
            self.prompt_ids = self.pipe.tokenizer.get_prompt_ids(vocab, return_tensors="pt").to("cuda:0")
        except Exception:  # noqa: BLE001
            self.prompt_ids = None

    def transcribe(self, audio: np.ndarray) -> str:
        gk = {"language": C.WHISPER_LANGUAGE, "task": "transcribe"}
        if self.prompt_ids is not None:
            gk["prompt_ids"] = self.prompt_ids
        try:
            out = self.pipe({"raw": audio, "sampling_rate": SAMPLE_RATE}, chunk_length_s=30, batch_size=8, generate_kwargs=gk, return_timestamps=False)
        except Exception:  # noqa: BLE001  (prompt unsupported by this transformers version)
            gk.pop("prompt_ids", None)
            self.prompt_ids = None
            out = self.pipe({"raw": audio, "sampling_rate": SAMPLE_RATE}, chunk_length_s=30, batch_size=8, generate_kwargs=gk, return_timestamps=False)
        return str(out.get("text", "")).strip()


class WhisperCppBackend(_Backend):
    def __init__(self) -> None:
        self.bin = C.WHISPER_CPP_DIR / "build/bin/whisper-cli"
        if not (self.bin.exists() and C.WHISPER_CPP_MODEL.exists()):
            raise RuntimeError("whisper.cpp binary or model missing")
        self.name, self.device, self.model_name = "whispercpp", "cuda (whisper.cpp)", C.WHISPER_CPP_MODEL.name

    def transcribe(self, audio: np.ndarray) -> str:
        import soundfile as sf

        with tempfile.TemporaryDirectory(dir=C.LOG_DIR) as tmp:
            wav = Path(tmp) / "in.wav"
            sf.write(wav, audio, SAMPLE_RATE, subtype="PCM_16")
            r = subprocess.run([str(self.bin), "-m", str(C.WHISPER_CPP_MODEL), "-f", str(wav), "-l", C.WHISPER_LANGUAGE,
                                "-nt", "-np", "-otxt", "-of", str(Path(tmp) / "out")], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-300:])
            return (Path(tmp) / "out.txt").read_text().strip()


def _build_backend(name: str) -> _Backend:
    if name == "faster":
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("CTranslate2 has no CUDA device (aarch64 wheel is CPU-only)")
        return FasterWhisperBackend("cuda")
    if name == "transformers":
        return TransformersBackend()
    if name == "whispercpp":
        return WhisperCppBackend()
    if name == "faster_cpu":
        return FasterWhisperBackend("cpu")
    raise ValueError(f"unknown STT backend {name}")


# ----------------------------------------------------------------- engine
class STTEngine:
    ORDER = ("faster", "transformers", "whispercpp", "faster_cpu")

    def __init__(self) -> None:
        self.backend: Optional[_Backend] = None
        self.load_ms = 0
        self.errors: dict[str, str] = {}
        self.recent_rtf: deque[float] = deque(maxlen=50)
        self._sem = threading.Semaphore(C.STT_MAX_CONCURRENT)
        self._load_lock = threading.Lock()

    def load(self) -> _Backend:
        with self._load_lock:
            if self.backend is not None:
                return self.backend
            t0 = time.perf_counter()
            order = self.ORDER if C.WHISPER_BACKEND == "auto" else (C.WHISPER_BACKEND,)
            for name in order:
                try:
                    self.backend = _build_backend(name)
                    break
                except Exception as e:  # noqa: BLE001
                    self.errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
            self.load_ms = int((time.perf_counter() - t0) * 1000)
            if self.backend is None:
                raise RuntimeError(f"no STT backend could be loaded: {self.errors}")
            db.set_metric("stt_backend", self.info())
            return self.backend

    def info(self) -> dict:
        b = self.backend
        return {"backend": b.name if b else None, "model": b.model_name if b else None, "device": b.device if b else None,
                "load_ms": self.load_ms, "errors": self.errors,
                "avg_rtf": round(sum(self.recent_rtf) / len(self.recent_rtf), 3) if self.recent_rtf else None}

    def transcribe_array(self, audio: np.ndarray) -> str:
        """Transcribe decoded 16 kHz mono float32 audio under the GPU semaphore (thread-safe entry)."""
        b = self.load()
        with self._sem:
            return b.transcribe(audio)

    def transcribe(self, source: AudioSource, channel: str = "audio") -> dict:
        """Never raises: on failure returns text='' and error set."""
        t0 = time.perf_counter()
        result = {"text": "", "audio_s": 0.0, "latency_ms": 0, "rtf": None, "backend": None, "model": None, "error": None}
        try:
            audio, dur = load_audio(source)
            result["audio_s"] = round(dur, 2)
            b = self.load()
            result["backend"], result["model"] = b.name, b.model_name
            with self._sem:
                text = b.transcribe(audio)
            result["text"] = text
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        result["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        if result["audio_s"]:
            result["rtf"] = round(result["latency_ms"] / 1000 / result["audio_s"], 3)
            if result["error"] is None:
                self.recent_rtf.append(result["rtf"])
        db.log_request(RequestLog(route="edge_stt", model=result["model"] or "", latency_ms=result["latency_ms"],
                                  tokens_out=len(result["text"].split()), bytes_out=0, ok=result["error"] is None,
                                  error=result["error"]))
        return result


_engine: Optional[STTEngine] = None
_lock = threading.Lock()


def get_engine() -> STTEngine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = STTEngine()
        return _engine


def transcribe(source: AudioSource, channel: str = "audio") -> dict:
    return get_engine().transcribe(source, channel)
