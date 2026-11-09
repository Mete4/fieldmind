"""Network state = the real connectivity check only (cached CONNECTIVITY_CACHE_S).
cloud_ready additionally needs an ANTHROPIC_API_KEY. Listeners fire when the
link comes back so the queued situation request runs."""
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
_prev_online: bool | None = None
_listeners: list[Callable[[bool], None]] = []


def on_online(cb: Callable[[bool], None]) -> None:
    _listeners.append(cb)


def connectivity(force: bool = False) -> bool:
    """Real reachability of the cloud endpoint (HEAD, 2 s timeout). Any HTTP
    answer counts as reachable; DNS/connect/timeout errors count as offline."""
    global _last_check, _prev_online
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
        came_back = ok and _prev_online is False
        _prev_online = ok
    if came_back:
        for cb in list(_listeners):
            try:
                cb(True)
            except Exception:  # noqa: BLE001
                pass
    return ok


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def state(force: bool = False) -> NetworkState:
    c = connectivity(force)
    return NetworkState(manual_online=True, connectivity=c, online=c, checked_at=now_iso(), queue_size=db.queue_size())


def cloud_ready() -> tuple[bool, str]:
    if not connectivity():
        return False, "no connectivity to the cloud endpoint"
    if not has_api_key():
        return False, "ANTHROPIC_API_KEY not set"
    return True, "online"
