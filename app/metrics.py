"""Hardware + pipeline metrics for GET /metrics and the Hardware tab.
Every number is measured: psutil, nvidia-smi, Ollama /api/ps, the SQLite
request log. Nothing is estimated except the cloud cost (tokens x list price)."""
from __future__ import annotations

import datetime as dt
import platform
import subprocess
import threading
import time
from typing import Any

import psutil

from app import config as C
from app import cloud, db, network

_gpu_cache: tuple[float, dict] = (0.0, {})
_lock = threading.Lock()
inflight = {"audio": 0, "text": 0}


def device_info() -> dict:
    try:
        product = open("/sys/class/dmi/id/product_name").read().strip()
    except Exception:  # noqa: BLE001
        product = "unknown"
    return {"label": C.DEVICE_LABEL, "product": product, "arch": platform.machine(), "cpu_count": psutil.cpu_count(),
            "kernel": platform.release(), "memory_total_gib": round(psutil.virtual_memory().total / 2**30, 1)}


def gpu_info() -> dict:
    global _gpu_cache
    with _lock:
        ts, cached = _gpu_cache
        if time.time() - ts < 2.0:
            return cached
    info: dict[str, Any] = {}
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,driver_version",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout.strip()
        name, util, temp, power, drv = [x.strip() for x in out.split(",")][:5]

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None

        info = {"name": name, "utilization_pct": num(util), "temperature_c": num(temp), "power_w": num(power), "driver": drv,
                "memory_note": "unified memory: the GPU shares the 128 GB system pool (nvidia-smi reports no VRAM)"}
    except Exception as e:  # noqa: BLE001
        info = {"error": str(e)[:80]}
    with _lock:
        _gpu_cache = (time.time(), info)
    return info


def memory_info() -> dict:
    vm = psutil.virtual_memory()
    return {"total_gib": round(vm.total / 2**30, 1), "used_gib": round((vm.total - vm.available) / 2**30, 1),
            "available_gib": round(vm.available / 2**30, 1), "percent": vm.percent, "cpu_percent": psutil.cpu_percent(interval=None)}


def models_loaded() -> list[dict]:
    try:
        import ollama

        ps = ollama.Client(host=C.OLLAMA_HOST, timeout=3).ps()
        return [{"model": m.model, "size_gib": round(m.size / 2**30, 1)} for m in ps.models]
    except Exception as e:  # noqa: BLE001
        return [{"error": f"ollama unreachable: {type(e).__name__}"}]


def pipeline_stats() -> dict:
    stats = db.request_stats()
    with db.connect() as c:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).isoformat(timespec="seconds")
        per_min = c.execute("SELECT COUNT(*) FROM reports WHERE ts >= ?", (since,)).fetchone()[0]
        tps = c.execute("SELECT AVG(json_extract(data,'$.tokens_per_s')) FROM incidents WHERE json_extract(data,'$.tokens_per_s') > 0").fetchone()[0]
        rtf = c.execute("SELECT AVG(json_extract(data,'$.stt_rtf')), COUNT(*) FROM reports WHERE json_extract(data,'$.stt_rtf') > 0").fetchone()
    edge_routes = [r for r in stats if r.startswith("edge")]
    cloud_routes = [r for r in stats if r.startswith("cloud")]
    cloud_in = sum(stats[r]["tokens_in"] for r in cloud_routes)
    cloud_out = sum(stats[r]["tokens_out"] for r in cloud_routes)

    def avg(routes: list[str]):
        tot = sum(stats[r]["count"] for r in routes)
        return int(sum(stats[r]["avg_ms"] * stats[r]["count"] for r in routes) / tot) if tot else None

    return {"reports_total": db.report_count(), "incidents_total": db.incident_count(), "reports_per_min": per_min,
            "active_streams": dict(inflight), "llm_tokens_per_s_avg": round(tps, 1) if tps else None,
            "whisper_rtf_avg": round(rtf[0], 3) if rtf and rtf[0] else None, "audio_reports": rtf[1] if rtf else 0,
            "routes": stats, "edge_avg_latency_ms": avg(edge_routes), "cloud_avg_latency_ms": avg(cloud_routes),
            "cloud_calls": sum(stats[r]["count"] for r in cloud_routes),
            "cloud_tokens_in": cloud_in, "cloud_tokens_out": cloud_out,
            "cloud_cost_usd_est": round(cloud.cost_usd(cloud_in, cloud_out), 4),
            "bytes_sent_off_device": sum(stats[r]["bytes_out"] for r in stats),
            "queue_size": db.queue_size()}


def snapshot() -> dict:
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "device": device_info(), "gpu": gpu_info(), "memory": memory_info(),
            "models_loaded": models_loaded(), "models_choice": db.get_metric("models", {}), "stt": db.get_metric("stt_backend", {}),
            "network": network.state().model_dump(), "cloud_ready": network.cloud_ready(), "pipeline": pipeline_stats()}
