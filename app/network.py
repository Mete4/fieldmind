"""Network state for the demo: online = manual toggle AND real connectivity
check (cached CONNECTIVITY_CACHE_S). cloud_ready additionally needs an
ANTHROPIC_API_KEY. The manual toggle is persisted in the metrics table so
every process (API, dashboard, scripts) sees the same state."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

import httpx

from app import config as C
from app import db
from app.schemas import NetworkState, now_iso

_lock = threading.Lock()
_last_check: tuple[float, bool] = (0.0, False)
_listeners: list[Callable[[bool], None]] = []


def manual_online() -> bool:
    return bool(db.get_metric("manual_online", True))


def set_manual_online(value: bool) -> NetworkState:
    before = state()
    db.set_metric("manual_online", bool(value))
    after = state(force=True)
    if after.online and not before.online:
        for cb in list(_listeners):
            try:
                cb(True)
            except Exception:  # noqa: BLE001
                pass
    return after


def on_online(cb: Callable[[bool], None]) -> None:
    _listeners.append(cb)


def connectivity(force: bool = False) -> bool:
    """Real reachability of the cloud endpoint (HEAD, 2 s timeout). Any HTTP
    answer counts as reachable; DNS/connect/timeout errors count as offline."""
    global _last_check
    with _lock:
        ts, ok = _last_check
        if not force and time.time() - ts < C.CONNECTIVITY_CACHE_S:
            return ok
    try:
        r = httpx.head(C.CONNECTIVITY_CHECK_URL, timeout=C.CONNECTIVITY_TIMEOUT_S, follow_redirects=False)
        ok = r.status_code < 600
    except Exception:  # noqa: BLE001
        ok = False
    with _lock:
        _last_check = (time.time(), ok)
    return ok


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def state(force: bool = False) -> NetworkState:
    m = manual_online()
    c = connectivity(force) if m else connectivity(force)   # always measured, shown even when toggled off
    s = NetworkState(manual_online=m, connectivity=c, online=bool(m and c), checked_at=now_iso(), queue_size=db.queue_size())
    return s


def cloud_ready() -> tuple[bool, str]:
    s = state()
    if not s.manual_online:
        return False, "demo toggle is OFFLINE"
    if not s.connectivity:
        return False, "no connectivity to the cloud endpoint"
    if not has_api_key():
        return False, "ANTHROPIC_API_KEY not set"
    return True, "online"
