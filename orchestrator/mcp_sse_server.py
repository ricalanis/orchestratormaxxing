#!/usr/bin/env python3
"""
Hermes Orchestrator MCP Server — SSE HTTP transport
===================================================
A thin HTTP/SSE wrapper around the EXISTING stdio MCP server (``mcp_server.py``).

Everything that decides *what the tools are and do* — the tool schemas
(``TOOLS``), the handlers (``TOOL_HANDLERS``), the least-authority scope model
(``PRIVILEGED_TOOLS`` / ``_resolve_scope`` / ``_scope_allows``) and the JSON-RPC
dispatch (``handle_request``) — is imported verbatim from ``mcp_server``. This
file adds ONLY the transport: it speaks the MCP "SSE transport" so remote clients
on the tailnet can connect over HTTP instead of spawning a stdio subprocess.

MCP SSE transport (the shape ``claude mcp add --transport sse <url>`` expects):
  1. Client opens ``GET /sse`` → server replies with an SSE stream and, as its
     first event, an ``endpoint`` event whose data is the URL to POST to
     (``/messages?session_id=<id>``).
  2. Client POSTs JSON-RPC requests to that endpoint URL.
  3. Server processes each request with the SAME ``handle_request`` as stdio and
     delivers the JSON-RPC response back over the SSE stream as a ``message``
     event. The POST itself just gets ``202 Accepted``.

Scope model is identical to stdio: it's resolved once at import time from
``HERMES_MCP_SCOPE`` (+ ``HERMES_MCP_TOKEN`` / the privileged-token file). Run
this process with those env vars to grant the privileged surface — otherwise it
serves the safe default scope, exactly like the stdio server.

Bind: 127.0.0.1:5556 (loopback). Exposed to the tailnet via
``tailscale serve --bg 5556`` (TLS + tailnet ACLs), never a raw public bind.

Run:
    .venv/bin/python mcp_sse_server.py
    # or: .venv/bin/uvicorn mcp_sse_server:app --host 127.0.0.1 --port 5556

Register with a client:
    claude mcp add --transport sse hermes-orchestrator https://<host>/sse
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from html import escape as _html_escape

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse, StreamingResponse, PlainTextResponse, HTMLResponse,
)
import uvicorn

# Import the tool surface + protocol core from the stdio server UNCHANGED. This
# is the whole point: one definition of the tools, two transports. Importing the
# module also runs its schema-ensure bootstrap and resolves ACTIVE_SCOPE from the
# environment — same as launching the stdio server.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_server import (  # noqa: E402
    TOOLS,
    TOOL_HANDLERS,
    PRIVILEGED_TOOLS,
    ACTIVE_SCOPE,
    _resolve_scope,
    _scope_allows,
    handle_request,
)

BIND = os.environ.get("HERMES_MCP_SSE_BIND", "127.0.0.1")
PORT = int(os.environ.get("HERMES_MCP_SSE_PORT", "5556"))

# Server identity + the PUBLIC URL clients connect to. The process binds to
# loopback (127.0.0.1:5556) but is exposed to the internet via `tailscale
# funnel` on 8556 — the /install configurator hands out that public URL, not
# the loopback bind. Override with HERMES_MCP_PUBLIC_URL when the funnel moves.
SERVER_VERSION = "2.0.0"
PUBLIC_URL = os.environ.get(
    "HERMES_MCP_PUBLIC_URL", "http://127.0.0.1:5556"
).rstrip("/")

# Tool-surface version counter. Bumped whenever the exposed tool set changes so
# clients (Claude Cowork especially) can be told to re-fetch tools/list via a
# `notifications/tools/list_changed`. Starts at 1; POST /api/tools/refresh
# increments it and broadcasts the notification to every open stream.
_TOOL_VERSION = 1

# Where clients POST their JSON-RPC messages. Kept relative so it works behind
# `tailscale serve` (TLS terminates upstream; the path is preserved).
MESSAGES_PATH = "/messages"

# One SSE stream ⇄ one message queue, keyed by the session id we mint on connect.
# The GET /sse generator drains its queue to the client; POST /messages finds the
# queue by session_id and enqueues the JSON-RPC response the client is waiting on.
_SESSIONS: dict[str, asyncio.Queue] = {}

# SSE keepalive: if no message is ready within this many seconds, emit a comment
# line so proxies (and `tailscale serve`) don't time out an idle stream.
_KEEPALIVE_SECS = 15.0

# --- Per-client rate limiting -------------------------------------------------
# Sliding 60s window per CLIENT (the tailnet peer). Behind `tailscale serve`
# the loopback bind sees every peer as 127.0.0.1, so the client key prefers
# X-Forwarded-For (which tailscale serve sets to the tailnet IP); a direct
# connection falls back to the socket peer. The limit follows the process's
# scope — a privileged server (Hermes' own) gets the higher ceiling:
#   default scope     60 req/min   (HERMES_MCP_RATE_LIMIT)
#   privileged scope 120 req/min   (HERMES_MCP_RATE_LIMIT_PRIVILEGED)
# Costing is BATCH-AWARE: a JSON-RPC batch of N counts N, so batching can't
# sidestep the limit. GET /sse connects count 1 (connection-churn guard);
# /health is never limited (liveness must stay observable).
_RATE_WINDOW = 60.0
_RATE_LIMIT_DEFAULT = int(os.environ.get("HERMES_MCP_RATE_LIMIT", "60"))
_RATE_LIMIT_PRIV = int(os.environ.get("HERMES_MCP_RATE_LIMIT_PRIVILEGED", "120"))
_RATE_LIMIT = _RATE_LIMIT_PRIV if ACTIVE_SCOPE == "privileged" else _RATE_LIMIT_DEFAULT
# El webhook de WhatsApp llega en ráfagas de historial (se midieron ~5/segundo).
# Sigue acotado — una cubeta sin techo es una fuga de memoria esperando — pero con
# margen para que un backfill no lo tumbe.
_WEBHOOK_RATE_LIMIT = int(os.environ.get("HERMES_WEBHOOK_RATE_LIMIT", "3000"))
_RATE_BUCKETS: dict[str, deque] = {}
# Monitoring counters. Exposed both via /health (federated by the dashboard)
# and directly at /metrics (Prometheus text). Latency is measured for the
# short /messages POST only — /sse is a long-lived stream, so timing it would
# poison the histogram with stream duration.
_METRICS = {
    "requests_total": 0,            # rate-limiter-admitted (both endpoints, batch-aware)
    "rate_limit_rejections_total": 0,
    "auth_failures_total": 0,       # 401s from the Bearer gate
    "sse_connections_total": 0,     # /sse streams opened
    "message_requests_total": 0,    # /messages POSTs handled
    "http_requests_total": 0,       # POST /mcp (streamable-http) requests handled
}
_LAT_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_LAT_HIST = {"buckets": [0] * (len(_LAT_BUCKETS_S) + 1), "sum": 0.0, "count": 0}


def _record_message_latency(seconds: float) -> None:
    h = _LAT_HIST
    h["sum"] += seconds
    h["count"] += 1
    for i, le in enumerate(_LAT_BUCKETS_S):
        if seconds <= le:
            h["buckets"][i] += 1
            break
    else:
        h["buckets"][-1] += 1

# --- Security: Bearer auth + Origin allowlist (P0 hardening) -------------------
# TOKEN: env HERMES_MCP_SSE_TOKEN, else ~/.config/orchestratormaxxing/mcp-sse-token.
# Configured → /sse and /messages REQUIRE `Authorization: Bearer <token>`
# (401 + WWW-Authenticate otherwise). Not configured → DEV MODE: requests
# pass, but startup logs a loud warning. /health stays open (liveness).
_TOKEN_FILE = Path.home() / ".config" / "orchestratormaxxing" / "mcp-sse-token"


def _configured_token() -> str:
    tok = os.environ.get("HERMES_MCP_SSE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return _TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def _auth_reject(request: Request):
    """None when authorized (or dev mode); a 401 response otherwise.

    Two credential channels, both checked in constant time:
      • ``Authorization: Bearer <token>`` — Claude Code / Claude Desktop / any
        client that can set headers.
      • ``?token=<token>`` query param — Claude Cowork's custom connectors are
        URL-only and cannot send custom headers, so the token rides the URL.
        (Over ``tailscale funnel`` the URL is TLS-encrypted end-to-end, so the
        query string is not exposed on the wire; keep the URL itself secret.)
    """
    token = _configured_token()
    if not token:
        return None  # dev mode
    supplied = request.headers.get("authorization", "")
    if supplied.startswith("Bearer ") and secrets.compare_digest(supplied[7:].strip(), token):
        return None
    qtok = request.query_params.get("token", "").strip()
    if qtok and secrets.compare_digest(qtok, token):
        return None
    _METRICS["auth_failures_total"] += 1
    return JSONResponse(
        {"error": "unauthorized — Bearer token required"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


# ORIGIN allowlist (shared by CORS and the per-request Origin check). A
# present-but-unlisted Origin is a browser from somewhere it shouldn't be →
# 403. An ABSENT Origin is a non-browser MCP client (claude mcp add) → allow.
_DEFAULT_ORIGINS = [
    "http://localhost:5555",
    "http://127.0.0.1:5555",
    "https://claude.ai",
    "https://www.anthropic.com",
]
def _compute_allowed_origins() -> list[str]:
    """Defaults + HERMES_MCP_CORS_ORIGINS (comma-separated): extend, never replace."""
    return _DEFAULT_ORIGINS + [
        o.strip()
        for o in os.environ.get("HERMES_MCP_CORS_ORIGINS", "").split(",")
        if o.strip()
    ]


ALLOWED_ORIGINS = _compute_allowed_origins()


def _origin_reject(request: Request):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"error": f"origin '{origin}' not allowed"}, status_code=403)
    return None


def _security_reject(request: Request):
    """Combined gate for /sse and /messages: auth first, then origin."""
    return _auth_reject(request) or _origin_reject(request)


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(request: Request, cost: int = 1, limit: int = None, bucket_key: str = None):
    """Charge `cost` requests to this client's window. Returns None when
    allowed, or a 429 JSONResponse (with Retry-After) when over the limit.
    Runs entirely on the event loop — no lock needed.

    `limit`/`bucket_key` dan cubeta propia a un endpoint. Existe porque compartir
    una cubeta acopla dos cosas que no tienen nada que ver: un emisor que manda
    en ráfaga (el sync de WhatsApp vaciando meses de historial) agota el
    presupuesto y el 429 resultante puede callar su webhook para siempre — se
    midió exactamente eso el 2026-08-04, y el síntoma es indistinguible de
    «nadie te escribió».
    """
    key = (bucket_key + "|" if bucket_key else "") + _client_key(request)
    tope = _RATE_LIMIT if limit is None else limit
    now = time.monotonic()
    bucket = _RATE_BUCKETS.setdefault(key, deque())
    while bucket and now - bucket[0] > _RATE_WINDOW:
        bucket.popleft()
    if len(bucket) + cost > tope:
        _METRICS["rate_limit_rejections_total"] += 1
        retry = max(1, int(_RATE_WINDOW - (now - bucket[0]))) if bucket else 1
        return JSONResponse(
            {"error": "rate limit exceeded",
             "limit": tope, "window_seconds": int(_RATE_WINDOW),
             "retry_after": retry},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    bucket.extend([now] * cost)
    _METRICS["requests_total"] += cost
    # Opportunistic GC: drop other clients' empty buckets so the dict can't
    # grow unboundedly across many short-lived peers.
    if len(_RATE_BUCKETS) > 64:
        for k in [k for k, b in _RATE_BUCKETS.items() if not b and k != key]:
            _RATE_BUCKETS.pop(k, None)
    return None


app = FastAPI(title="hermes-orchestrator-mcp-sse")

# CORS locked to the tailnet dashboard + localhost (P0-2) — extend via
# HERMES_MCP_CORS_ORIGINS (comma-separated). Browser policy only; the hard
# gates are Bearer auth + the per-request Origin check + the scope model.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)


def _sse_event(data: str, event: str | None = None) -> str:
    """Frame one SSE event. `data` is sent as a single `data:` line (our payloads
    are compact single-line JSON, so no multi-line splitting is needed)."""
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {data}\n\n"


# --- Graceful shutdown --------------------------------------------------------
# On SIGTERM (uvicorn runs the shutdown lifecycle) we DRAIN in-flight streams
# instead of dropping them: each active queue gets the _CLOSE sentinel, the
# generator emits a final `close` event and stops, and we wait up to
# HERMES_MCP_SSE_DRAIN_TIMEOUT (default 10s) for the sessions to unwind — so a
# container redeploy ends streams cleanly rather than mid-message.
_CLOSE = object()          # drain sentinel: emit a close event, then stop
_SHUTTING_DOWN = False
DRAIN_TIMEOUT = float(os.environ.get("HERMES_MCP_SSE_DRAIN_TIMEOUT", "10"))


async def _event_stream(session_id: str, queue: asyncio.Queue, is_disconnected,
                        send_endpoint: bool = True):
    """The SSE body: (optional) endpoint handshake, then relay queued messages
    until the client leaves (is_disconnected), an explicit None sentinel, or
    _CLOSE (graceful drain → a final `close` event). Removes itself from
    _SESSIONS on exit no matter the cause.

    ``send_endpoint`` frames the MCP *SSE transport* handshake (GET /sse) whose
    first event tells the client where to POST. The *streamable-http* transport
    (GET /mcp) opens the same relay WITHOUT that event — there the client already
    knows to POST to /mcp, and the stream carries only server→client
    notifications (e.g. tools/list_changed)."""
    try:
        if send_endpoint:
            yield _sse_event(f"{MESSAGES_PATH}?session_id={session_id}", event="endpoint")
        while True:
            if await is_disconnected():
                break
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"  # SSE comment — ignored by clients
                continue
            if payload is _CLOSE:
                yield _sse_event("server shutting down", event="close")
                break
            if payload is None:  # explicit shutdown sentinel
                break
            yield _sse_event(payload, event="message")
    finally:
        _SESSIONS.pop(session_id, None)


async def drain_sse_connections(timeout: float = DRAIN_TIMEOUT) -> int:
    """Signal every active stream to close, then wait up to `timeout`s for
    them to unwind. Returns how many were active when the drain began.
    Idempotent and safe with zero sessions."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True
    active = len(_SESSIONS)
    for q in list(_SESSIONS.values()):
        try:
            q.put_nowait(_CLOSE)
        except Exception:
            pass
    waited = 0.0
    while _SESSIONS and waited < timeout:
        await asyncio.sleep(0.05)
        waited += 0.05
    return active


def _broadcast(payload: str) -> int:
    """Enqueue one already-serialized JSON-RPC string onto EVERY open stream
    (both /sse and streamable-http /mcp share _SESSIONS). Returns how many
    streams it reached. Used for server-initiated notifications."""
    n = 0
    for q in list(_SESSIONS.values()):
        try:
            q.put_nowait(payload)
            n += 1
        except Exception:
            pass
    return n


def _notify_tools_list_changed() -> int:
    """Fan a `notifications/tools/list_changed` out to all connected clients so
    they re-fetch tools/list. Per MCP, a notification has no `id`."""
    return _broadcast(json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}))


class DrainingServer(uvicorn.Server):
    """uvicorn Server that DRAINS in-flight SSE streams on the first
    SIGTERM/SIGINT, THEN stops. Draining here (not in a lifespan shutdown
    handler) is essential: uvicorn waits for open connections BEFORE running
    lifespan shutdown, but those streams only close once the drain signals
    them — doing it in handle_exit breaks that deadlock. A second signal
    forces immediate exit (uvicorn's normal behavior)."""
    def handle_exit(self, sig, frame):
        if _SHUTTING_DOWN:
            return super().handle_exit(sig, frame)  # second signal → force
        asyncio.get_event_loop().create_task(self._drain_then_exit(sig, frame))

    async def _drain_then_exit(self, sig, frame):
        n = await drain_sse_connections()
        print(f"MCP SSE: drained {n} in-flight stream(s), exiting", file=sys.stderr)
        super().handle_exit(sig, frame)


@app.get("/health")
async def health():
    """Liveness + a peek at the active scope and how many tools it exposes."""
    visible = sum(1 for t in TOOLS if _scope_allows(t["name"]))
    return {
        "status": "ok",
        "server": "hermes-orchestrator",
        "version": SERVER_VERSION,
        # Two live transports off one tool surface: the legacy MCP SSE transport
        # (/sse) and the streamable-http transport (/mcp). `transport` kept for
        # backward compatibility with anything scraping the old field.
        "transport": "sse",
        "transports": ["sse", "http"],
        "scope": ACTIVE_SCOPE,
        "tools_visible": visible,
        "tools_total": len(TOOLS),
        "tool_version": _TOOL_VERSION,
        "active_sessions": len(_SESSIONS),
        "rate_limit_per_min": _RATE_LIMIT,
        "auth": "bearer" if _configured_token() else "DEV-MODE-OPEN",
        "requests_total": _METRICS["requests_total"],
        "message_requests_total": _METRICS["message_requests_total"],
        "http_requests_total": _METRICS["http_requests_total"],
        "rate_limit_rejections_total": _METRICS["rate_limit_rejections_total"],
    }


@app.get("/metrics")
async def metrics():
    """Prometheus exposition — scraped directly by the monitoring stack.
    Unauthenticated + un-rate-limited (monitoring must always reach it), and
    excluded from its own counters. Latency covers /messages only (a /sse
    stream is long-lived — timing it would poison the histogram)."""
    m = _METRICS
    lines = [
        "# HELP hermes_mcp_sse_requests_total Rate-limiter-admitted requests (both endpoints).",
        "# TYPE hermes_mcp_sse_requests_total counter",
        f'hermes_mcp_sse_requests_total {m["requests_total"]}',
        "# HELP hermes_mcp_sse_message_requests_total /messages POSTs handled.",
        "# TYPE hermes_mcp_sse_message_requests_total counter",
        f'hermes_mcp_sse_message_requests_total {m["message_requests_total"]}',
        "# HELP hermes_mcp_sse_http_requests_total POST /mcp (streamable-http) requests handled.",
        "# TYPE hermes_mcp_sse_http_requests_total counter",
        f'hermes_mcp_sse_http_requests_total {m["http_requests_total"]}',
        "# HELP hermes_mcp_sse_sse_connections_total SSE streams opened.",
        "# TYPE hermes_mcp_sse_sse_connections_total counter",
        f'hermes_mcp_sse_sse_connections_total {m["sse_connections_total"]}',
        "# HELP hermes_mcp_sse_active_connections Currently-open SSE streams.",
        "# TYPE hermes_mcp_sse_active_connections gauge",
        f'hermes_mcp_sse_active_connections {len(_SESSIONS)}',
        "# HELP hermes_mcp_sse_auth_failures_total 401s from the Bearer gate.",
        "# TYPE hermes_mcp_sse_auth_failures_total counter",
        f'hermes_mcp_sse_auth_failures_total {m["auth_failures_total"]}',
        "# HELP hermes_mcp_sse_rate_limit_rejections_total 429s from the rate limiter.",
        "# TYPE hermes_mcp_sse_rate_limit_rejections_total counter",
        f'hermes_mcp_sse_rate_limit_rejections_total {m["rate_limit_rejections_total"]}',
        "# HELP hermes_mcp_sse_message_duration_seconds /messages handling latency.",
        "# TYPE hermes_mcp_sse_message_duration_seconds histogram",
    ]
    cum = 0
    for i, le in enumerate(_LAT_BUCKETS_S):
        cum += _LAT_HIST["buckets"][i]
        lines.append(f'hermes_mcp_sse_message_duration_seconds_bucket{{le="{le}"}} {cum}')
    cum += _LAT_HIST["buckets"][-1]
    lines.append(f'hermes_mcp_sse_message_duration_seconds_bucket{{le="+Inf"}} {cum}')
    lines.append(f'hermes_mcp_sse_message_duration_seconds_sum {_LAT_HIST["sum"]:.6f}')
    lines.append(f'hermes_mcp_sse_message_duration_seconds_count {_LAT_HIST["count"]}')
    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/sse")
async def sse(request: Request):
    """Open the SSE stream. First event is `endpoint` (where to POST); after that
    we relay whatever POST /messages enqueues for this session, plus keepalives."""
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    rejected = _security_reject(request)
    if rejected is not None:
        return rejected
    if _SHUTTING_DOWN:
        return JSONResponse({"error": "server shutting down"}, status_code=503)
    _METRICS["sse_connections_total"] += 1
    session_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _SESSIONS[session_id] = queue
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable proxy buffering so events flush live
    }
    return StreamingResponse(
        _event_stream(session_id, queue, request.is_disconnected),
        media_type="text/event-stream", headers=headers)


@app.post(MESSAGES_PATH)
async def messages(request: Request):
    """Receive a JSON-RPC request (or batch), dispatch it through the SAME
    handle_request as stdio, and push each response onto the session's SSE queue.
    The HTTP response is just an ack — the real answer arrives over the stream."""
    # Rate check FIRST — invalid-session and bad-JSON spam must be charged
    # too, or a client could hammer the 404/400 paths for free. One unit is
    # charged on entry; a batch tops up the remainder after parsing.
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    rejected = _security_reject(request)
    if rejected is not None:
        return rejected

    _msg_t0 = time.perf_counter()
    _METRICS["message_requests_total"] += 1
    session_id = request.query_params.get("session_id")
    queue = _SESSIONS.get(session_id) if session_id else None
    if queue is None:
        _record_message_latency(time.perf_counter() - _msg_t0)
        return JSONResponse(
            {"error": "unknown or expired session_id — (re)connect to GET /sse first"},
            status_code=404,
        )

    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)

    # Accept a single request or a JSON-RPC batch (batch of N costs N total).
    reqs = body if isinstance(body, list) else [body]
    if len(reqs) > 1:
        limited = _rate_limited(request, cost=len(reqs) - 1)
        if limited is not None:
            return limited
    for req in reqs:
        if not isinstance(req, dict):
            continue
        # handle_request runs the tool synchronously; keep the event loop free by
        # offloading it to a thread (tool handlers touch sqlite / subprocess).
        response = await asyncio.to_thread(handle_request, req)
        if response is not None:  # notifications (e.g. `initialized`) yield None
            await queue.put(json.dumps(response))

    _record_message_latency(time.perf_counter() - _msg_t0)
    return PlainTextResponse("", status_code=202)


# --- Streamable-HTTP transport (/mcp) ----------------------------------------
# The modern MCP transport that supersedes SSE. Unlike the SSE transport (open a
# stream, then POST to a session endpoint, answers come back over the stream),
# streamable-http answers a POST *inline*: the client POSTs a JSON-RPC request
# (or batch) to /mcp and reads the JSON-RPC response straight out of the HTTP
# response body — no session handshake needed. This is what Claude Cowork's
# custom connectors speak, and it pairs with ?token= auth since Cowork can't set
# a Bearer header. An optional GET /mcp opens a server→client notification
# stream (tools/list_changed), for clients that want push updates.

@app.post("/")
@app.post("/mcp")
async def mcp_http(request: Request):
    """Streamable-HTTP: dispatch a JSON-RPC request (or batch) through the SAME
    handle_request as stdio/SSE and return the JSON-RPC response(s) inline.
    A single request → the single response object; a batch → an array (with
    notifications, which produce no response, omitted). A request that is purely
    a notification (no id, no response) → 202 with an empty body."""
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    rejected = _security_reject(request)
    if rejected is not None:
        return rejected

    _msg_t0 = time.perf_counter()
    _METRICS["http_requests_total"] += 1

    try:
        body = await request.json()
    except Exception as e:
        _record_message_latency(time.perf_counter() - _msg_t0)
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": f"Parse error: {e}"}},
            status_code=400,
        )

    is_batch = isinstance(body, list)
    reqs = body if is_batch else [body]
    # Batch-aware rate limiting, same rule as /messages: a batch of N costs N.
    if len(reqs) > 1:
        limited = _rate_limited(request, cost=len(reqs) - 1)
        if limited is not None:
            _record_message_latency(time.perf_counter() - _msg_t0)
            return limited

    responses = []
    for req in reqs:
        if not isinstance(req, dict):
            continue
        # Tool handlers touch sqlite/subprocess — run off the event loop.
        response = await asyncio.to_thread(handle_request, req)
        if response is not None:  # notifications (e.g. `initialized`) yield None
            responses.append(response)

    _record_message_latency(time.perf_counter() - _msg_t0)

    if is_batch:
        # Empty array would be an invalid JSON-RPC batch response; if everything
        # was a notification, ack with 202 instead.
        if not responses:
            return PlainTextResponse("", status_code=202)
        return JSONResponse(responses)
    if not responses:  # single notification → nothing to return
        return PlainTextResponse("", status_code=202)
    return JSONResponse(responses[0])


@app.get("/")
@app.get("/mcp")
async def mcp_http_stream(request: Request):
    """Optional streamable-http server→client channel: an SSE stream carrying
    only notifications (e.g. tools/list_changed). No `endpoint` handshake — the
    client already knows to POST to /mcp. Same drain/keepalive semantics as
    /sse; shares the _SESSIONS registry so _broadcast reaches it too."""
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    rejected = _security_reject(request)
    if rejected is not None:
        return rejected
    if _SHUTTING_DOWN:
        return JSONResponse({"error": "server shutting down"}, status_code=503)
    session_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _SESSIONS[session_id] = queue
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _event_stream(session_id, queue, request.is_disconnected, send_endpoint=False),
        media_type="text/event-stream", headers=headers)


@app.post("/api/tools/refresh")
async def tools_refresh(request: Request):
    """Bump the tool-surface version and broadcast tools/list_changed to every
    open stream so clients (Cowork especially) re-fetch tools/list. Auth-gated
    like the transports — it mutates advertised state."""
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    rejected = _security_reject(request)
    if rejected is not None:
        return rejected
    global _TOOL_VERSION
    _TOOL_VERSION += 1
    reached = _notify_tools_list_changed()
    return {
        "ok": True,
        "tool_version": _TOOL_VERSION,
        "notified_streams": reached,
    }


# --- Install configurator (/install, /install.json) --------------------------
# A self-describing setup surface. Open (like /health) — it exposes NO secrets:
# the token is always the literal placeholder "YOUR_TOKEN" that the operator
# substitutes with their own Bearer token from ~/.config/orchestratormaxxing/
# mcp-sse-token. Handy to hit over the funnel to grab copy-paste config.
_TOKEN_PLACEHOLDER = "YOUR_TOKEN"


def _install_config() -> dict:
    visible = sum(1 for t in TOOLS if _scope_allows(t["name"]))
    connector_url = f"{PUBLIC_URL}/mcp?token={_TOKEN_PLACEHOLDER}"
    return {
        "server": "hermes-orchestrator",
        "version": SERVER_VERSION,
        "scope": ACTIVE_SCOPE,
        "tool_version": _TOOL_VERSION,
        "transports": {
            "sse": {"url": f"{PUBLIC_URL}/sse", "auth": "bearer"},
            "http": {"url": f"{PUBLIC_URL}/mcp", "auth": "bearer-or-query-param"},
        },
        "tools_count": visible,
        "tools_total": len(TOOLS),
        "cowork_setup": {
            "connector_url": connector_url,
            "instructions": (
                "Customize → Connectors → Add custom connector → paste the "
                f"connector_url, replacing {_TOKEN_PLACEHOLDER} with your Bearer "
                "token (from ~/.config/orchestratormaxxing/mcp-sse-token)."
            ),
        },
        "claude_code_setup": (
            f"claude mcp add --transport http hermes-orchestrator "
            f"{PUBLIC_URL}/mcp --header 'Authorization: Bearer {_TOKEN_PLACEHOLDER}'"
        ),
        "claude_desktop_setup": {
            "note": "Add to claude_desktop_config.json under mcpServers.",
            "config": {
                "hermes-orchestrator": {
                    "url": f"{PUBLIC_URL}/mcp",
                    "headers": {"Authorization": f"Bearer {_TOKEN_PLACEHOLDER}"},
                }
            },
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/install.json")
async def install_json():
    """Machine-readable connection config for every supported client."""
    return JSONResponse(_install_config())


@app.get("/install", response_class=HTMLResponse)
async def install_html():
    """Human-readable setup page — copy-paste config for Cowork, Claude Code,
    and Claude Desktop."""
    cfg = _install_config()
    e = lambda x: _html_escape(str(x))  # coerce ints (tool counts) before escaping
    connector_url = cfg["cowork_setup"]["connector_url"]
    cc = cfg["claude_code_setup"]
    desktop_json = json.dumps(
        {"mcpServers": cfg["claude_desktop_setup"]["config"]}, indent=2)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Orchestrator — MCP setup</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 780px; margin: 2.5rem auto; padding: 0 1.25rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: .25rem; }}
  .sub {{ opacity: .7; margin-top: 0; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  pre {{ background: rgba(127,127,127,.12); padding: .8rem 1rem; border-radius: 8px;
         overflow-x: auto; font-size: 13px; }}
  .pill {{ display: inline-block; background: rgba(127,127,127,.15); border-radius: 999px;
           padding: .1rem .6rem; font-size: 12px; margin-right: .35rem; }}
  .note {{ opacity: .7; font-size: 13px; }}
  a {{ color: inherit; }}
</style></head><body>
<h1>Hermes Orchestrator</h1>
<p class="sub">MCP server v{e(cfg["version"])} ·
  <span class="pill">{e(cfg["tools_count"])}/{e(cfg["tools_total"])} tools</span>
  <span class="pill">scope: {e(cfg["scope"])}</span>
  <span class="pill">transports: sse + http</span>
  <span class="pill">tool&nbsp;rev {e(cfg["tool_version"])}</span>
</p>
<p class="note">Replace <code>{e(_TOKEN_PLACEHOLDER)}</code> below with your Bearer token
  (<code>~/.config/orchestratormaxxing/mcp-sse-token</code>). The token is never shown on this page.</p>

<h2>Claude Cowork &amp; other custom connectors</h2>
<p>Cowork connectors are URL-only (no custom headers), so the token rides the URL as
  <code>?token=</code>. Customize → Connectors → Add custom connector → paste:</p>
<pre>{e(connector_url)}</pre>
<p class="note">{e(cfg["cowork_setup"]["instructions"])}</p>

<h2>Claude Code (CLI)</h2>
<pre>{e(cc)}</pre>

<h2>Claude Desktop</h2>
<p>Add to <code>claude_desktop_config.json</code>:</p>
<pre>{e(desktop_json)}</pre>

<h2>Endpoints</h2>
<pre>SSE   (bearer)                 {e(cfg["transports"]["sse"]["url"])}
HTTP  (bearer or ?token=)      {e(cfg["transports"]["http"]["url"])}
Health                         {e(PUBLIC_URL)}/health
Machine-readable config        {e(PUBLIC_URL)}/install.json</pre>
<p class="note">Last updated {e(cfg["last_updated"])}</p>
</body></html>"""
    return HTMLResponse(html)


def main():
    visible = sum(1 for t in TOOLS if _scope_allows(t["name"]))
    auth = "bearer-token" if _configured_token() else "DEV MODE"
    print(
        f"Hermes MCP server → http://{BIND}:{PORT}  "
        f"[sse: /sse · http: /mcp · install: /install] "
        f"(scope={ACTIVE_SCOPE}, {visible}/{len(TOOLS)} tools, auth={auth}, "
        f"public={PUBLIC_URL})",
        file=sys.stderr,
    )
    if not _configured_token():
        print(
            "WARNING: no HERMES_MCP_SSE_TOKEN (or ~/.config/orchestratormaxxing/"
            "mcp-sse-token) configured — running OPEN (dev mode). Set a token "
            "before exposing this beyond localhost.",
            file=sys.stderr,
        )

    server = DrainingServer(uvicorn.Config(app, host=BIND, port=PORT, log_level="info"))
    server.run()



# --------------------------------------------------------------------------
# Fireflies webhook — la única ruta pública que NO usa el bearer del MCP.
#
# Fireflies no puede mandar nuestro token, así que su credencial es la firma
# HMAC del cuerpo. Por eso esta ruta salta `_auth_reject` a propósito: la firma
# ES su autenticación, y verificarla ANTES de cualquier trabajo es lo que evita
# que un desconocido nos haga hacer fetches o escribir filas.
#
# Falla cerrada: sin secreto configurado responde 503, nunca "modo dev". El
# bearer del MCP puede permitirse un modo dev porque su default es una
# superficie de lectura segura; esta ruta escribe, y está en internet.
#
# El request hace lo MÍNIMO durable — un acuse — y devuelve 200. El fetch del
# transcript lo hace el tick, donde un fallo es reintentable y visible; hacerlo
# aquí (o en un BackgroundTask) perdería la junta sin dejar señal si el proceso
# muere después del 200.
# --------------------------------------------------------------------------

FIREFLIES_WEBHOOK_PATH = "/webhooks/fireflies"
_WEBHOOK_MAX_BODY = 64 * 1024


@app.post(FIREFLIES_WEBHOOK_PATH)
async def fireflies_webhook(request: Request):
    limited = _rate_limited(request)
    if limited is not None:
        return limited

    # Guardia de tamaño ANTES de leer un solo byte: leer y luego medir no es una
    # protección de memoria, es una medición post-mortem.
    declared = request.headers.get("content-length")
    if declared is None:
        return JSONResponse({"error": "length required"}, status_code=411)
    try:
        if int(declared) > _WEBHOOK_MAX_BODY:
            return JSONResponse({"error": "payload too large"}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "bad content-length"}, status_code=400)

    try:
        from dashboard import fireflies as _ff
        secret = _ff.webhook_secret()
    except Exception:
        secret = None
    if not secret:
        return JSONResponse({"error": "webhook secret not configured"}, status_code=503)

    raw = await request.body()
    presented = (request.headers.get("x-hub-signature")
                 or request.headers.get("X-Hub-Signature") or "")
    if presented.startswith("sha256="):
        presented = presented[7:]
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not presented or not secrets.compare_digest(presented, expected):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return JSONResponse({"error": "bad json"}, status_code=400)

    event_name = body.get("eventType") or body.get("event") or ""
    if event_name != "meeting.summarized":
        # `meeting.transcribed` llega ANTES de que exista el resumen, y el
        # resumen es toda nuestra evidencia. Actuar sobre él capturaría juntas
        # vacías.
        return JSONResponse({"ignored": True, "event": event_name})

    meeting_id = body.get("meetingId") or body.get("meeting_id")
    if not meeting_id:
        return JSONResponse({"error": "missing meetingId"}, status_code=400)

    try:
        from dashboard import digestion as _dg
        _dg.record_receipt(str(meeting_id), event_name=event_name)
    except Exception as e:
        return JSONResponse({"error": f"could not record receipt: {type(e).__name__}"},
                            status_code=500)
    return JSONResponse({"received": True})



# --------------------------------------------------------------------------
# Webhook de WhatsApp (wacli, localhost) — escribe SOLO un pulso.
#
# El cuerpo trae el mensaje completo, y a propósito NO se guarda: solo se anota
# "este chat tuvo movimiento a esta hora". Un chat que el operador nunca permita
# jamás deja texto en nuestra base, y eso no depende de recordar filtrarlo
# después — depende de nunca haberlo escrito.
#
# El contenido se lee del espejo de wacli cuando la ventana cierra y solo para
# chats permitidos (`whatsapp_chats.allowed = 1`, default-deny).
#
# Sin firma HMAC configurada NO falla cerrado como el de Fireflies: este endpoint
# solo es alcanzable desde localhost (wacli corre en la misma máquina y el bind
# es 127.0.0.1), así que su frontera es la máquina, no un secreto. Si el secreto
# existe, se verifica.
# --------------------------------------------------------------------------

WHATSAPP_WEBHOOK_PATH = "/webhooks/whatsapp"

# Dónde puede venir el jid del chat según cómo anide wacli su payload. Se busca
# en varias formas a propósito: el emisor es de otro proyecto y su envoltura
# cambió entre versiones, y adivinar mal aquí no falla ruidosamente — devuelve
# 200, no anota nada, y se ve exactamente igual que "nadie te escribió".
_RUTAS_JID = (
    ("chat_jid",), ("chatJid",), ("chat",), ("jid",),
    ("message", "chat_jid"), ("message", "chatJid"), ("message", "chat"),
    ("data", "chat_jid"), ("data", "chatJid"),
    ("data", "message", "chat_jid"), ("data", "message", "chatJid"),
    ("info", "chat"), ("info", "chat_jid"), ("key", "remoteJid"),
    ("message", "key", "remoteJid"), ("event", "chat_jid"),
)


def _extraer_jid(body) -> str:
    """El jid del chat, venga en la envoltura que venga. Cadena vacía si no está."""
    for ruta in _RUTAS_JID:
        v = body
        for paso in ruta:
            v = v.get(paso) if isinstance(v, dict) else None
            if v is None:
                break
        if isinstance(v, str) and "@" in v:
            return v
    return ""


@app.post(WHATSAPP_WEBHOOK_PATH)
async def whatsapp_webhook(request: Request):
    # Cubeta propia y holgada: este emisor manda en ráfaga cuando el sync vacía
    # historial, y es loopback + HMAC — limitarlo no protege de nada y su 429
    # puede callar el webhook durante toda la vida del proceso emisor.
    limited = _rate_limited(request, limit=_WEBHOOK_RATE_LIMIT, bucket_key="wa")
    if limited is not None:
        return limited

    client = (request.client.host if request.client else "") or ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "loopback only"}, status_code=403)

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > 2 * 1024 * 1024:
                return JSONResponse({"error": "payload too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "bad content-length"}, status_code=400)

    raw = await request.body()
    secret = os.environ.get("WACLI_WEBHOOK_SECRET", "").strip()
    if secret:
        presented = request.headers.get("x-wacli-signature", "")
        if presented.startswith("sha256="):
            presented = presented[7:]
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not presented or not secrets.compare_digest(presented, expected):
            return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return JSONResponse({"error": "bad json"}, status_code=400)

    jid = _extraer_jid(body)
    if not jid:
        # Un webhook que se ignora en silencio es indiagnosticable: wacli recibe
        # 200, no registra nada, y del otro lado no aparece ningún pulso — se ve
        # idéntico a "nadie te escribió". Se devuelven las LLAVES del JSON (nunca
        # los valores) para poder arreglar la forma sin sacar una sola palabra de
        # una conversación.
        forma = sorted(body.keys())[:12] if isinstance(body, dict) else [type(body).__name__]
        anidadas = {k: sorted(v.keys())[:12] for k, v in (body.items() if isinstance(body, dict) else [])
                    if isinstance(v, dict)}
        return JSONResponse({"ignored": True, "reason": "sin chat",
                             "llaves": forma, "anidadas": anidadas})

    try:
        from dashboard import whatsapp as _wa
        _wa.record_activity(str(jid))
    except Exception as e:
        return JSONResponse({"error": f"no se anotó: {type(e).__name__}"}, status_code=500)
    return JSONResponse({"received": True})


if __name__ == "__main__":
    main()
