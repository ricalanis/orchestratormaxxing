"""Fail-open read client for Hermes's disposable Semantica projection."""
from __future__ import annotations

import json
import http.client
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("SEMANTICA_URL", "http://127.0.0.1:8765")
SOCKET_PATH = os.environ.get(
    "SEMANTICA_SOCKET",
    str(os.path.expanduser("~/.local/share/orchestratormaxxing/semantica-host/run/api.sock")),
)
TIMEOUT = float(os.environ.get("SEMANTICA_CLIENT_TIMEOUT", "0.75"))
MAX_QUERY = 512
MAX_K = 10
MAX_RESPONSE_BYTES = 8 * 1024
MAX_FRESHNESS_SECONDS = 15 * 60
_LOCK = threading.Lock()
_FAILURES = 0
_OPEN_UNTIL = 0.0


def enabled() -> bool:
    return os.environ.get("HERMES_SEMANTICA_ENABLED", "0").lower() in {"1", "true", "yes", "on"}


def _request(path: str, method: str = "GET", timeout: float = TIMEOUT) -> dict:
    if os.path.exists(SOCKET_PATH):
        conn = http.client.HTTPConnection("localhost", timeout=timeout)
        conn.sock = socket.socket(socket.AF_UNIX)
        conn.sock.settimeout(timeout)
        conn.sock.connect(SOCKET_PATH)
        conn.request(method, path)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Semantica response exceeds 8 KiB")
        value = json.loads(raw)
    else:
        req = urllib.request.Request(BASE_URL + path, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Semantica response exceeds 8 KiB")
            value = json.loads(raw)
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise RuntimeError("invalid Semantica response")
    return value


def _record(ok: bool) -> None:
    global _FAILURES, _OPEN_UNTIL
    with _LOCK:
        if ok:
            _FAILURES = 0
            _OPEN_UNTIL = 0.0
        else:
            _FAILURES += 1
            if _FAILURES >= 3:
                _OPEN_UNTIL = time.monotonic() + 30.0


def query(text: str, k: int = 8) -> dict:
    """Return semantic context or an explicit, non-throwing fallback marker."""
    if len(text) > MAX_QUERY:
        return {"status": "fallback", "reason": "query_limit"}
    with _LOCK:
        if time.monotonic() < _OPEN_UNTIL:
            return {"status": "fallback", "reason": "circuit_open"}
    try:
        value = _request("/query?q=" + urllib.parse.quote(text) + "&k=" + str(max(1, min(k, MAX_K))))
        built_at = int(value.get("built_at") or 0)
        if not built_at or time.time() - built_at > MAX_FRESHNESS_SECONDS:
            _record(False)
            return {"status": "fallback", "reason": "stale"}
        _record(True)
        return value
    except Exception:  # projection failure must never fail canonical recall
        _record(False)
        return {"status": "fallback", "reason": "unavailable"}


def health() -> dict:
    try:
        return _request("/healthz")
    except Exception:
        return {"status": "fallback", "reason": "unavailable"}


def rebuild(timeout: float = 30.0) -> dict:
    try:
        return _request("/rebuild", method="POST", timeout=timeout)
    except Exception:
        return {"status": "fallback", "reason": "unavailable"}
