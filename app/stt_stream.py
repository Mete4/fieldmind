"""Speech-to-text as the audio plays (section 6). A StreamSession takes
0.5 s PCM chunks, cuts the stream at pauses with voice activity detection
(Silero VAD on torch; energy fallback) and transcribes each speech segment
as soon as it ends, then feeds the text into the normal report pipeline."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np

from app import config as C
from app.stt import SAMPLE_RATE, get_engine

FRAME = 512                     # Silero works on 512-sample frames at 16 kHz (32 ms)
_vad_model = None
_vad_lock = threading.Lock()


def _vad():
    global _vad_model
    with _vad_lock:
        if _vad_model is None:
            try:
                import torch
                from silero_vad import load_silero_vad

                torch.set_num_threads(2)
                _vad_model = ("silero", load_silero_vad())
            except Exception as e:  # noqa: BLE001
                _vad_model = ("energy", str(e)[:80])
        return _vad_model


_vad_call = threading.Lock()      # the JIT model is not thread-safe: one frame at a time across sessions


def speech_prob(frame: np.ndarray) -> float:
    kind, model = _vad()
    if kind == "silero":
        import torch

        with _vad_call, torch.no_grad():
            return float(model(torch.from_numpy(np.ascontiguousarray(frame)), SAMPLE_RATE).item())
    rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-9)
    return 1.0 if rms > 0.015 else 0.0


class StreamSession:
    """Feed float32 16 kHz mono audio in any chunk size; segments are
    transcribed on a worker thread and passed to on_segment(dict)."""

    def __init__(self, channel: str, on_segment: Callable[[dict], None], silence_ms: int = 700, min_speech_ms: int = 500,
                 max_segment_s: float = 20.0, threshold: float = 0.4) -> None:
        self.channel, self.on_segment = channel, on_segment
        self.silence_frames = max(1, int(silence_ms / 1000 * SAMPLE_RATE / FRAME))
        self.min_speech = int(min_speech_ms / 1000 * SAMPLE_RATE)
        self.max_segment = int(max_segment_s * SAMPLE_RATE)
        self.threshold = threshold
        self._pending = np.zeros(0, dtype=np.float32)
        self._seg: list[np.ndarray] = []
        self._in_speech = False
        self._silence = 0
        self._seg_started_at = 0.0
        self.segments = 0
        self._lock = threading.Lock()
        self.stats = {"chunks": 0, "segments": 0, "delivered": 0, "speech_s": 0.0}

    def feed(self, audio: np.ndarray) -> None:
        self.stats["chunks"] += 1
        buf = np.concatenate([self._pending, audio.astype(np.float32)])
        n = (len(buf) // FRAME) * FRAME
        frames, self._pending = buf[:n], buf[n:]
        for i in range(0, n, FRAME):
            self._frame(frames[i:i + FRAME])

    def _frame(self, frame: np.ndarray) -> None:
        p = speech_prob(frame)
        if p >= self.threshold:
            if not self._in_speech:
                self._in_speech, self._seg_started_at = True, time.time()
            self._silence = 0
            self._seg.append(frame)
        elif self._in_speech:
            self._seg.append(frame)
            self._silence += 1
            if self._silence >= self.silence_frames:
                self._end_segment("pause")
        if self._in_speech and sum(len(x) for x in self._seg) >= self.max_segment:
            self._end_segment("max_length")

    def _end_segment(self, why: str) -> None:
        seg = np.concatenate(self._seg) if self._seg else np.zeros(0, dtype=np.float32)
        self._seg, self._in_speech, self._silence = [], False, 0
        if len(seg) < self.min_speech:
            return
        self.segments += 1
        idx, started = self.segments, self._seg_started_at
        threading.Thread(target=self._transcribe, args=(seg, idx, why, started), daemon=True).start()

    def _transcribe(self, seg: np.ndarray, idx: int, why: str, started: float) -> None:
        t0 = time.perf_counter()
        eng = get_engine()
        try:
            text = eng.transcribe_array(seg)
            err = None
        except Exception as e:  # noqa: BLE001
            text, err = "", f"{type(e).__name__}: {str(e)[:120]}"
        dur = len(seg) / SAMPLE_RATE
        ms = int((time.perf_counter() - t0) * 1000)
        self.stats["segments"] += 1
        self.stats["speech_s"] += dur
        with self._lock:
            try:
                self.on_segment({"channel": self.channel, "segment": idx, "cut": why, "audio_s": round(dur, 2), "latency_ms": ms,
                                 "rtf": round(ms / 1000 / dur, 3) if dur else None, "text": text, "error": err,
                                 "backend": eng.backend.name if eng.backend else None, "since_speech_start_s": round(time.time() - started, 2)})
            finally:
                self.stats["delivered"] += 1

    def pending(self) -> int:
        return self.segments - self.stats["delivered"]

    def flush(self) -> None:
        if self._in_speech:
            self._end_segment("end_of_stream")
