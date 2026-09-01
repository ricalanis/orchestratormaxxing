"""
Hermes Orchestrator Dashboard — FastAPI backend.
Binds loopback by default (DASHBOARD_BIND); reachable beyond the machine only via a fronting layer such as `tailscale serve` — never a public bind.
"""
import os
import sys
import subprocess
import json
import time
import uuid
import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import re
import threading
import traceback

from . import db
from . import agent_status
from . import sessions
from . import sprints
from . import usage
from . import providers as _providers
from . import object_graph as graph
from . import loop
from . import orchestration as orch
from . import coordinators
from . import memory as memory_view
from . import graph_memory as gmem
from . import semantica_client
from . import lakehouse_client as lakehouse
from . import identity
from . import canvas
from . import brief
from . import dispatch
from . import task_planning
from . import threads as thread_registry
from . import attachments as attachment_hub
from . import pulse
from . import capacity
from . import plan
from . import day_review
from . import context as entity_context
from . import comments as task_comments

# The rest of the package — imported here (not above) because these modules were
# historically introduced alongside their own ensure_schema() call. The schema
# work has since moved into the migration runner; only the imports stay.
from . import governance
from . import strategy
from . import crm
from . import crm_proposals
from . import commercial_proposals
from . import growth
from . import cadence
from . import fireflies
from . import readiness
from . import health as _health_mod
from . import reflection as _reflection_mod
from . import weekly_reflection as _weekly_reflection_mod
from . import friday_prep as _friday_prep_mod
from . import growth_radar as _growth_radar_mod
from . import okrs as _okrs_mod
from . import consulting_time as _consulting_time_mod

# --- the schema floor (m00) -------------------------------------------------
# ONE entrypoint, called by EVERY process that opens the kanban DB. This app and
# mcp_server.py used to keep separate hand-maintained ensure_schema() lists and
# they drifted — the MCP server's was a strict subset, so a DB bootstrapped by it
# came up without the fireflies / nurture / events / task-flag / comments /
# consulting-time tables or the P3 indexes. runner.run() is the shared chain
# (legacy ensures + the versioned orch_migrations ledger). Idempotent.
from .migrations import runner as _migration_runner
_migration_runner.run()

import time
HISTORY_FILE = Path(__file__).parent / "prompt_history.json"

def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except:
            return {}
    return {}

def save_history(data):
    try:
        HISTORY_FILE.write_text(json.dumps(data, indent=2))
    except:
        pass

PROMPT_HISTORY = load_history()

def add_to_prompt_history(host, session_name, text):
    key = f"{host}:{session_name}"
    if key not in PROMPT_HISTORY:
        PROMPT_HISTORY[key] = []
    PROMPT_HISTORY[key].insert(0, {
        "ts": int(time.time()),
        "text": text[:200]
    })
    PROMPT_HISTORY[key] = PROMPT_HISTORY[key][:8]
    save_history(PROMPT_HISTORY)

def get_prompt_history(host, session_name):
    key = f"{host}:{session_name}"
    return PROMPT_HISTORY.get(key, [])
def enrich_tasks_with_sessions(tasks, sessions_data):
    """Link tasks to their Claude Code sessions.

    HARD link first: if `task.session_id` is set (an agent claimed the task and
    recorded its run), match that exact session — authoritative. SOFT fallback:
    match an agent task to a session by project path (best-effort, legacy).
    """
    claude_sessions = sessions_data.get("claude_code", [])
    by_id = {str(s.get("session_id", "")): s for s in claude_sessions}
    by_project = {str(s.get("project", "")).lower(): s for s in claude_sessions}

    def link(session, hard):
        return {
            "session_id": session.get("session_id"),
            "host": session.get("host", "local"),
            "last_active": session.get("last_active"),
            "status": session.get("status"),
            "hard": hard,
        }

    for task in tasks:
        # Hard link: the task's recorded session (works for human + agent tasks).
        sid = getattr(task, "session_id", None)
        if sid and sid in by_id:
            task.session_link = link(by_id[sid], True)
            continue
        if task.assignee_type == "human":
            continue
        needle = str(task.assignee or "").lower()
        if not needle:
            continue
        session = by_project.get(needle) or next(
            (s for s in claude_sessions if needle in str(s.get("project", "")).lower()),
            None,
        )
        if session:
            task.session_link = link(session, False)
    return tasks


def _parse_json_list(v):
    """task_ledger.files_modified / risks are JSON-encoded lists (sometimes '[]',
    sometimes null). Decode defensively to a plain list."""
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        import json as _json
        p = _json.loads(v)
        return p if isinstance(p, list) else ([p] if p else [])
    except Exception:
        return [str(v)] if str(v).strip() else []


def attach_ledger_digest(tasks):
    """For tasks awaiting review, attach a compact ledger digest so the Review
    card can show files-touched / risks counts + the latest summary at a glance
    (the full ledger stays lazy — it's only fetched when the drawer opens). Cheap:
    one query per review task, and there are only a handful at a time."""
    for task in tasks:
        if getattr(task, "status", None) != "review":
            continue
        try:
            rows = db.get_task_ledger(task.id, limit=1)
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0]
        files = _parse_json_list(row.get("files_modified"))
        risks = _parse_json_list(row.get("risks"))
        task.ledger_digest = {
            "files": files[:12],
            "files_count": len(files),
            "risks": [str(r) for r in risks[:6]],
            "risks_count": len(risks),
            "summary": row.get("summary"),
            "passed": bool(row.get("passed")),
        }
    return tasks

# --- Config ---
TAILSCALE_IP = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "3000"))

BASE_DIR = Path(__file__).resolve().parent


# --- Lifespan handler (replaces deprecated @app.on_event("startup")) ---
# Schema initialization happens at import time (module-level ensure_schema()
# calls), so there's no heavy startup work here yet. The lifespan context
# manager is the modern FastAPI API for startup/shutdown hooks — add future
# background-task setup or resource cleanup inside this function.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    yield
    # ── shutdown ──


app = FastAPI(title="Hermes Orchestrator Dashboard", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Uniform 500 envelope for any *unhandled* error across all routes —
    previously these fell through to FastAPI's bare 500 with no JSON body, so a
    scriptable client got an opaque failure. HTTPException keeps its own handler
    (deliberate 4xx/5xx raises are untouched); this only catches the unexpected."""
    sys.stderr.write(
        f"[unhandled] {request.method} {request.url.path}: "
        f"{type(exc).__name__}: {exc}\n")
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": str(exc),
                 "path": request.url.path},
    )


class CachedStaticFiles(StaticFiles):
    """StaticFiles that stamps Cache-Control on every response.

    JS/CSS change between deploys but are immutable within a deploy —
    ``max-age=3600`` lets the browser reuse them on repeat visits within
    a session without revalidating, while the HTML pages stay no-store
    so the dashboard always gets the latest markup.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response


app.mount("/static", CachedStaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Jinja2 custom filter for timestamps
import datetime
def _timestamp_filter(unix_ts):
    if not unix_ts:
        return ""
    dt = datetime.datetime.fromtimestamp(unix_ts)
    return dt.strftime("%H:%M")

templates.env.filters["timestamp"] = _timestamp_filter

# Sprint date filter
def _sprint_date_filter(unix_ts):
    if not unix_ts:
        return ""
    dt = datetime.datetime.fromtimestamp(unix_ts)
    return dt.strftime("%b %d")

templates.env.filters["sprint_date"] = _sprint_date_filter

# --- Dashboard auth token (defined early — needed by template global + middleware) ---
# Token source (first match wins):
#   1. env HERMES_DASHBOARD_TOKEN
#   2. file ~/.config/orchestratormaxxing/dashboard-token
# Not configured → DEV MODE: no auth (stderr warning, same as the MCP SSE gate).
_DASH_TOKEN_FILE = Path.home() / ".config" / "orchestratormaxxing" / "dashboard-token"


def _configured_dashboard_token() -> str | None:
    # An explicitly-empty env var means "disable auth" (test/CI mode).
    if "HERMES_DASHBOARD_TOKEN" in os.environ:
        tok = os.environ["HERMES_DASHBOARD_TOKEN"].strip()
        return tok or None  # "" → None → dev mode
    try:
        return _DASH_TOKEN_FILE.read_text().strip() or None
    except Exception:
        return None


_DASH_TOKEN = _configured_dashboard_token()

# Inject the dashboard token into all templates so browser JS can send it
# on mutating fetch calls. Empty string in dev mode (no token configured).
templates.env.globals["DASHBOARD_TOKEN"] = _DASH_TOKEN or ""
# Tenant UI config (payday-cycle account regex, per-tenant project styling):
# VALUES live in the service environment, never in the template.
def _tenant_ui_json() -> str:
    try:
        styles = json.loads(os.environ.get("HERMES_PROJECT_STYLES_JSON", "") or "{}")
        if not isinstance(styles, dict):
            styles = {}
    except ValueError:
        styles = {}
    return json.dumps({
        "payday_1020_re": os.environ.get("HERMES_PAYDAY_1020_ACCOUNTS_RE", ""),
        "project_styles": styles,
    })
templates.env.globals["TENANT_UI_JSON"] = _tenant_ui_json()


# --- Request-body size limit ----------------------------------------------
# Pure ASGI middleware (not BaseHTTPMiddleware): the reject happens from the
# Content-Length header alone, BEFORE any handler — or any middleware inward
# of this one — reads a byte of body. Bodies without a Content-Length
# (chunked) get 411: this is a JSON API with no legitimate streaming uploads,
# and demanding a length is what makes the pre-handler guarantee airtight.
# Registered BEFORE the logging middleware in file order, which makes it the
# INNER layer — so 413/411 rejections still show up in the request log and
# /metrics like any other response.
MAX_BODY_BYTES = int(os.environ.get("DASHBOARD_MAX_BODY_BYTES", str(1024 * 1024)))  # 1MB


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = None):
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else MAX_BODY_BYTES

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        cl = headers.get("content-length")
        body = None
        if cl is None:
            if headers.get("transfer-encoding", "").lower() == "chunked":
                body = {"detail": "Length Required — chunked bodies are not accepted",
                        "limit_bytes": self.max_bytes}
                status = 411
        else:
            try:
                length = int(cl)
            except ValueError:
                body, status = {"detail": "invalid Content-Length"}, 400
            else:
                if length > self.max_bytes:
                    body = {"detail": "request body too large",
                            "limit_bytes": self.max_bytes, "got_bytes": length}
                    status = 413
        if body is None:
            return await self.app(scope, receive, send)
        payload = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})


app.add_middleware(BodySizeLimitMiddleware)

# --- GZip compression ---------------------------------------------------------
# Compress all responses > 1 KB. The big win is index.html (628 KB → ~143 KB
# on the wire) and the static JS/CSS assets. The @app.middleware("http")
# logger sits INSIDE this layer, so it still sees the original status code and
# timing — only the wire bytes change.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# --- Bearer auth on mutating endpoints ----------------------------------------
# Every POST/PATCH/DELETE/PUT to /api/* requires `Authorization: Bearer <token>`.
# GETs and page loads are exempt — the dashboard is read-only for browsers
# without a token, and the operator's browser gets the token injected via a
# Jinja2 global (DASHBOARD_TOKEN) so its JS fetch calls carry it automatically.
# Token + helpers defined earlier (before templates) so the global can use it.
_MUTATING_METHODS = frozenset(("POST", "PATCH", "DELETE", "PUT"))
_SENSITIVE_GET_PATHS = frozenset(("/api/personal/okrs",))
# Familias de rutas cuyos GET llevan habla textual de clientes. Se comparan por
# SEGMENTO (igual o seguido de "/"), no con un startswith a secas: ese
# sobre-capturaría un futuro `/api/suggestions-export`, que es otra ruta.
# `/api/personal/cogload` va por PREFIJO, no por ruta exacta: la familia incluye
# `/weekly`, y todo GET de carga cognitiva es estado personal de salud — nunca
# público. Por segmento, así que un futuro `/api/personal/cogload-export` no
# queda cubierto por accidente y tiene que declararse a propósito.
_SENSITIVE_GET_PREFIXES = ("/api/suggestions", "/api/objectives", "/api/whatsapp",
                           "/api/personal/cogload")


def _is_sensitive_get(path: str) -> bool:
    if path in _SENSITIVE_GET_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in _SENSITIVE_GET_PREFIXES)


class MutatingAuthMiddleware:
    """ASGI middleware: 401 + WWW-Authenticate on mutating /api/* calls without
    a valid Bearer token. GETs, page routes, /healthz, /metrics, and /static
    pass through unconditionally. Runs as an INNER layer (registered before the
    http logger) so 401s are logged and counted in metrics like any other
    response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        path = scope.get("path", "")
        # Gate all API writes plus explicitly-sensitive personal reads.
        protected = ((method in _MUTATING_METHODS and path.startswith("/api/"))
                     or (method == "GET" and _is_sensitive_get(path)))
        if not protected:
            return await self.app(scope, receive, send)
        # Test-mode bypass: the pytest suite (conftest sets TESTING=1) exercises
        # mutating endpoints via Starlette's TestClient without minting a Bearer
        # token. Read per-request so the dedicated auth regression guard
        # (test_auth_middleware) can clear TESTING to assert real enforcement.
        # Never set in production, so the gate below stays authoritative there.
        if os.environ.get("TESTING"):
            return await self.app(scope, receive, send)
        # No token configured → dev mode (allow, with one-time stderr warning).
        if not _DASH_TOKEN:
            return await self.app(scope, receive, send)
        # Extract Authorization header from raw ASGI scope.
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        supplied = headers.get("authorization", "")
        if supplied.startswith("Bearer ") and secrets.compare_digest(
            supplied[7:].strip(), _DASH_TOKEN
        ):
            return await self.app(scope, receive, send)
        # Reject — 401 + WWW-Authenticate (RFC 6750 §3).
        payload = json.dumps(
            {"detail": "unauthorized — Bearer token required for mutating requests"}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"www-authenticate", b'Bearer realm="hermes-dashboard"'),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


if not _DASH_TOKEN:
    print("hermes-dashboard: DEV MODE — no DASHBOARD_TOKEN configured, mutating "
          "endpoints are OPEN. Set HERMES_DASHBOARD_TOKEN or "
          "~/.config/orchestratormaxxing/dashboard-token before exposing beyond localhost.",
          file=sys.stderr)

app.add_middleware(MutatingAuthMiddleware)


# --- API request logging (JSONL, daily files) ---------------------------------
# Every /api* and /healthz request is appended to orchestrator/logs/
# api-YYYYMMDD.log as one JSON line: ts, method, path, status, ms, client.
# Rotation is BY FILENAME — the date is in the name, so midnight naturally
# starts a new file and yesterday's is closed on the next write (no
# TimedRotatingFileHandler state to corrupt on restart). The write is a
# line-buffered append (microseconds); pages/static are not API traffic and
# are skipped.
API_LOG_DIR = BASE_DIR.parent / "logs"
_API_LOG_LOCK = threading.Lock()
_API_LOG = {"day": None, "fh": None}


def _api_log_write(record: dict) -> None:
    day = time.strftime("%Y%m%d")
    with _API_LOG_LOCK:
        if _API_LOG["day"] != day:
            if _API_LOG["fh"]:
                try:
                    _API_LOG["fh"].close()
                except Exception:
                    pass
            API_LOG_DIR.mkdir(exist_ok=True)
            _API_LOG["fh"] = open(API_LOG_DIR / f"api-{day}.log", "a", buffering=1)
            _API_LOG["day"] = day
        _API_LOG["fh"].write(json.dumps(record, separators=(",", ":")) + "\n")


# --- Prometheus metrics (in-process, fed by the same middleware) ---------------
# Counters keyed by (method, normalized path, status) + one global latency
# histogram. Path labels are NORMALIZED — entity ids (t_xxx, init_xxx, UUIDs,
# numbers) collapse to {id} so label cardinality stays bounded no matter how
# many tasks/deals exist.
_METRICS_LOCK = threading.Lock()
_REQ_COUNTS: dict = {}
_LAT_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_LAT_HIST = {"buckets": [0] * (len(_LAT_BUCKETS_S) + 1), "sum": 0.0, "count": 0}

# Prefixed ids match on SHAPE (prefix_suffix), not hex-ness — a 404 probe like
# /api/tasks/t_zzzznope must collapse too, or garbage ids mint unbounded series.
_ID_SEG_RE = re.compile(
    r"^(?:t|init|deal|acct|proj|spr|epic|cyc|run|ev)_[0-9a-z]{4,}$"
    r"|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    r"|^[0-9a-f]{32}$|^\d+$", re.IGNORECASE)
# Hard cardinality cap: past this many series, NEW label combos collapse into
# a single overflow series (existing ones keep counting) — a scanner spraying
# random paths can't grow the scrape unboundedly.
_REQ_COUNTS_MAX = 500


def _metrics_path(path: str) -> str:
    return "/".join("{id}" if _ID_SEG_RE.match(seg) else seg
                    for seg in path.split("/"))


def _metrics_record(method: str, path: str, status: int, seconds: float) -> None:
    key = (method, _metrics_path(path), status)
    with _METRICS_LOCK:
        if key not in _REQ_COUNTS and len(_REQ_COUNTS) >= _REQ_COUNTS_MAX:
            key = (method, "{other}", status)
        _REQ_COUNTS[key] = _REQ_COUNTS.get(key, 0) + 1
        h = _LAT_HIST
        h["sum"] += seconds
        h["count"] += 1
        for i, le in enumerate(_LAT_BUCKETS_S):
            if seconds <= le:
                h["buckets"][i] += 1
                break
        else:
            h["buckets"][-1] += 1


_API_ERR = {"day": None, "fh": None}


def _api_error_write(record: dict) -> None:
    """Structured uncaught-exception record → logs/api-errors-YYYYMMDD.log.
    Same filename-rotation scheme as the request log; the correlation id
    (request_id) links a record here to its line in the request log and to
    the X-Request-ID the client received."""
    day = time.strftime("%Y%m%d")
    with _API_LOG_LOCK:
        if _API_ERR["day"] != day:
            if _API_ERR["fh"]:
                try:
                    _API_ERR["fh"].close()
                except Exception:
                    pass
            API_LOG_DIR.mkdir(exist_ok=True)
            _API_ERR["fh"] = open(API_LOG_DIR / f"api-errors-{day}.log", "a", buffering=1)
            _API_ERR["day"] = day
        _API_ERR["fh"].write(json.dumps(record, separators=(",", ":")) + "\n")


def _log_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def api_request_logger(request: Request, call_next):
    path = request.url.path
    if not (path.startswith("/api") or path == "/healthz"):
        return await call_next(request)
    # Correlation id: honor a caller-supplied X-Request-ID (so a client's
    # trace continues through us), else mint one. Returned on EVERY API
    # response and stamped on the request-log line; an uncaught exception is
    # additionally written to logs/api-errors-*.log with the full traceback
    # under the same id — the header is the handle for finding the traceback.
    rid = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:16]
    start = time.perf_counter()
    status = 500
    response = None
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as exc:
        try:
            _api_error_write({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "request_id": rid,
                "method": request.method,
                "path": path + (f"?{request.url.query}" if request.url.query else ""),
                "client": _log_client_ip(request),
                "exception": type(exc).__name__,
                "message": str(exc)[:500],
                "traceback": traceback.format_exc(limit=25),
            })
        except Exception:
            pass
        response = JSONResponse(
            {"detail": "internal server error", "request_id": rid},
            status_code=500,
        )
    finally:
        try:
            _metrics_record(request.method, path, status, time.perf_counter() - start)
            _api_log_write({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "method": request.method,
                "path": path + (f"?{request.url.query}" if request.url.query else ""),
                "status": status,
                "ms": round((time.perf_counter() - start) * 1000, 1),
                "client": _log_client_ip(request),
                "rid": rid,
            })
        except Exception:
            pass  # logging must never break a request
    response.headers["X-Request-ID"] = rid
    return response


# --- Models ---
class TaskCreate(BaseModel):
    title: str
    body: Optional[str] = None
    assignee: str = "ricardo"
    priority: int = 0
    project_id: Optional[str] = None
    sprint_id: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    contract_cmd: Optional[str] = None
    practice_text: Optional[str] = None
    practice_host: str = "orchestrator"
    run_context: Optional[dict] = None
    created_by: str = "ricardo"
    workspace: str = "scratch"
    due_date: Optional[str] = None
    # Journey fase 1, step 4 (ruling 5): the FIRST of exactly three writers that
    # may set `tasks.deal_id`. Optional, and applied as a sidecar after the
    # hermes-CLI create — the same idiom as `project_id` above, for the same
    # reason (the CLI has no flag for it and gaining one is fase 2).
    deal_id: Optional[str] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    from_status: Optional[str] = None
    assignee: Optional[str] = None
    # Verb audit Tier 2: the card itself is editable (audited task_updated event).
    title: Optional[str] = None
    body: Optional[str] = None
    priority: Optional[int] = None
    due_date: Optional[str] = None
    # P0-7 (§6): per-task opt-out of server-side cycle auto-commit.
    auto_cycle: Optional[bool] = None
    # P1-3 (§8): direct initiative attribution. "" / null clears it.
    initiative_id: Optional[str] = None
    clear_initiative: Optional[bool] = None
    # Backlog Phase 1: schedule a task into an ISO week ("2026-W28"), or clear it
    # (clear_scheduled_week) to drop it back to the Backlog. assign_active_cycle
    # commits the task to the current active cycle in the same PATCH ("This week").
    scheduled_week: Optional[str] = None
    clear_scheduled_week: Optional[bool] = None
    assign_active_cycle: Optional[bool] = None
    # DECLARED IN ORDER TO BE REFUSED (ruling 5). Pydantic ignores unknown fields
    # by default, so leaving `deal_id` off this model would make the generic
    # patch answer 200 to a body that set it and write nothing — the worst of
    # the three options, because the caller believes the link landed. Declaring
    # it is what lets `api_update_task` answer a typed 400 pointing at
    # PATCH /api/tasks/{id}/deal.
    deal_id: Optional[str] = None


class CommentCreate(BaseModel):
    # The drawer posts {text, author}; accept `body` as an alias so either works.
    text: Optional[str] = None
    body: Optional[str] = None
    author: str = "ricardo"


def _or_http(res):
    """Translate a module-layer error dict into a real HTTP error.

    The dashboard modules return {"status": "error", "error": msg} rather than
    raising (they're also called by MCP handlers, which want dicts). At the
    HTTP edge that convention must become a status code — a client checking
    codes must never see 200 on failure. 'not found' → 404; a state-conflict
    refusal (e.g. "refusing to delete a completed cycle") → 409; else → 400.
    """
    if isinstance(res, dict) and res.get("status") == "error":
        msg = str(res.get("error") or res.get("detail") or "operation failed")
        low = msg.lower()
        if "not found" in low:
            code = 404
        elif low.startswith("refusing") or "already exists" in low:
            code = 409
        else:
            code = 400
        raise HTTPException(code, msg)
    return res


# --- Pages ---
# (Main route and /agents are defined below after the sessions API)


# --- API ---
@app.get("/api/tasks")
def api_tasks(offset: int = 0, limit: int = 0, backlog: bool = False):
    # limit defaults to 0 = firehose: return EVERY task, unpaginated. The board
    # never hides awaiting-review / blocked work behind a "Load More" (PRD
    # principle #1). Pagination stays available for callers that pass a limit.
    all_tasks = db.get_all_tasks()
    # Backlog Phase 1: truly-unscheduled tasks = no sprint AND no scheduled week.
    if backlog:
        all_tasks = [t for t in all_tasks if not t.sprint_id and not t.scheduled_week]
    all_tasks = enrich_tasks_with_sessions(all_tasks, sessions.get_all_sessions())
    all_tasks = attach_ledger_digest(all_tasks)
    total = len(all_tasks)
    if limit == 0:                       # limit=0 → firehose: every task, unpaginated
        paginated = all_tasks
    else:
        paginated = all_tasks[offset:offset + limit] if offset >= 0 else all_tasks[:limit]
    return {
        "tasks": [t.to_dict() for t in paginated],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(paginated)) < total if limit else False,
    }


@app.get("/api/search")
def api_search(q: str = "", limit: int = 8):
    """Global omnibar search (P2-20) across tasks, projects, deals, accounts,
    contacts, sessions, and memory.

    Read-only; small case-insensitive substring match over the in-memory/cheap
    listings. Each hit carries {type, id, title, subtitle, tab, entity} so the
    frontend can route to the right tab / open the right drawer on Enter.

    Spec §4 ("Search is the real navigation"): for an ADHD operator, typing a
    name is recall-free and beats remembering which tab owns a thing — so the
    delivery and client nouns have to be typeable, not just the work items.
    Every block is individually try/except'd: one unavailable table degrades
    that block to zero hits, it never 500s the omnibar."""
    query = (q or "").strip().lower()
    if not query:
        return {"query": q, "results": []}
    per_type = max(1, min(limit, 20))
    results: list[dict] = []

    # Tasks — title/id substring.
    try:
        for t in db.get_all_tasks():
            d = t.to_dict()
            title = str(d.get("title") or "")
            if query in title.lower() or query in str(d.get("id") or "").lower():
                proj = d.get("project_id") or ""
                results.append({
                    "type": "task", "id": d.get("id"),
                    "title": title or d.get("id"),
                    "subtitle": f"{d.get('status', '')} · {proj}".strip(" ·"),
                    "tab": "board", "entity": f"task:{d.get('id')}",
                })
                if sum(1 for r in results if r["type"] == "task") >= per_type:
                    break
    except Exception:
        pass

    # Projects — name/slug substring. The delivery noun (spec §1); its drawer is
    # already reachable, so a hit is one Enter away from the project context.
    try:
        for p in sprints.list_projects():
            name = str(p.get("name") or "")
            slug = str(p.get("slug") or "")
            if query in name.lower() or query in slug.lower():
                tasks_n = p.get("task_count")
                results.append({
                    "type": "project", "id": p.get("id"),
                    "title": name or slug or p.get("id"),
                    "subtitle": " · ".join(x for x in (
                        slug, f"{tasks_n} tasks" if tasks_n is not None else "",
                        str(p.get("status") or "")) if x),
                    "tab": "projects", "entity": f"project:{p.get('id')}",
                })
                if sum(1 for r in results if r["type"] == "project") >= per_type:
                    break
    except Exception:
        pass

    # Deals — name/account substring.
    try:
        for dl in crm.list_deals():
            name = str(dl.get("name") or dl.get("title") or "")
            acct = str(dl.get("account_name") or "")
            if query in name.lower() or query in acct.lower():
                results.append({
                    "type": "deal", "id": dl.get("id"),
                    "title": name or acct or dl.get("id"),
                    "subtitle": f"{dl.get('stage', '')} · {acct}".strip(" ·"),
                    "tab": "crm", "entity": f"deal:{dl.get('id')}",
                })
                if sum(1 for r in results if r["type"] == "deal") >= per_type:
                    break
    except Exception:
        pass

    # Accounts — name substring. `accounts` has 32 rows and every deal is linked
    # to one; it simply had no way in (spec §4).
    try:
        for a in crm.list_accounts():
            name = str(a.get("name") or "")
            if query in name.lower() or query in str(a.get("id") or "").lower():
                results.append({
                    "type": "account", "id": a.get("id"),
                    "title": name or a.get("id"),
                    "subtitle": " · ".join(x for x in (
                        str(a.get("domain") or ""),
                        f"{a.get('deals')} deals" if a.get("deals") is not None else "",
                        f"{a.get('contacts')} contacts" if a.get("contacts") is not None else "",
                    ) if x),
                    "tab": "crm", "entity": f"account:{a.get('id')}",
                })
                if sum(1 for r in results if r["type"] == "account") >= per_type:
                    break
    except Exception:
        pass

    # Contacts — name/email substring. There is no `contact` entity type in the
    # drawer (context.ENTITY_TYPES), so a hit opens the contact's ACCOUNT: the
    # nearest addressable context, where the contact is listed. A `contact:` id
    # would be dropped silently by openEntity and read as a dead row.
    try:
        for c in crm.list_contacts():
            name = str(c.get("name") or "")
            email = str(c.get("email") or "")
            if query in name.lower() or query in email.lower():
                acct = c.get("account_id")
                results.append({
                    "type": "contact", "id": c.get("id"),
                    "title": name or email or c.get("id"),
                    "subtitle": " · ".join(x for x in (
                        str(c.get("account_name") or ""), str(c.get("role") or ""), email) if x),
                    "tab": "crm", "entity": f"account:{acct}" if acct else None,
                })
                if sum(1 for r in results if r["type"] == "contact") >= per_type:
                    break
    except Exception:
        pass

    # Sessions — id/tag substring.
    try:
        sess = sessions.get_all_sessions()
        pool = (sess.get("claude_code") or []) + (sess.get("opencode") or [])
        for s in pool:
            sid = str(s.get("session_id") or "")
            tag = str(s.get("tag") or "")
            if query in sid.lower() or query in tag.lower():
                results.append({
                    "type": "session", "id": sid,
                    "title": tag or sid,
                    "subtitle": f"{s.get('status', '')} · {s.get('host', 'local')}".strip(" ·"),
                    "tab": "sessions", "entity": None,
                })
                if sum(1 for r in results if r["type"] == "session") >= per_type:
                    break
    except Exception:
        pass

    # Memory — entry content substring.
    try:
        mem = memory_view.build()
        for e in (mem.get("agent") or []) + (mem.get("user") or []):
            content = str(e.get("content") or "")
            if query in content.lower():
                snippet = content.strip().split("\n", 1)[0][:80]
                results.append({
                    "type": "memory", "id": f"{e.get('source')}:{e.get('idx')}",
                    "title": snippet or "(memory)",
                    "subtitle": f"{e.get('source', '')} memory · {e.get('category', '')}".strip(" ·"),
                    "tab": "memory", "entity": None,
                })
                if sum(1 for r in results if r["type"] == "memory") >= per_type:
                    break
    except Exception:
        pass

    return {"query": q, "results": results}


@app.get("/api/archive")
def api_archive(group: str = "day"):
    """The graveyard: completed tasks bucketed by completion date (all assignees)."""
    if group not in ("day", "week", "month"):
        group = "day"
    return db.get_archive(group)


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {
        "task": task.to_dict(),
        "comments": db.get_task_comments(task_id),
        "events": db.get_task_events(task_id),
        "links": db.get_task_links(task_id),
    }


@app.get("/api/context/{entity_type}/{entity_id}")
async def api_entity_context(entity_type: str, entity_id: str):
    """P0-3: one-call context for the entity drawer — ancestors + entity +
    children for any of task | project | initiative | deal | session | account.
    Replaces N chatty client round-trips to reconstruct a breadcrumb (§3)."""
    try:
        ctx = await asyncio.to_thread(entity_context.build_context, entity_type, entity_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if ctx is None:
        raise HTTPException(404, f"{entity_type} '{entity_id}' not found")
    return ctx


@app.get("/api/tasks/{task_id}/runs")
def api_task_runs(task_id: str):
    """Execution history: every run attempt with step/outcome/error."""
    return {"task_id": task_id, "runs": db.get_task_runs(task_id)}


@app.get("/api/tasks/{task_id}/history")
def api_task_history(task_id: str):
    """Full status/audit trail for a task (oldest → newest)."""
    return {"task_id": task_id, "history": db.get_task_history(task_id)}


@app.get("/api/tasks/{task_id}/comments")
def api_list_comments(task_id: str):
    """Comments for one task (oldest → newest) — the drawer's Comments section."""
    return {"task_id": task_id, "comments": db.get_task_comments(task_id)}


@app.post("/api/tasks/{task_id}/comments")
async def api_add_comment(task_id: str, comment: CommentCreate):
    """Append a comment ({text|body, author}). 404 if the task is unknown, 400 if empty."""
    res = await asyncio.to_thread(task_comments.add_comment, task_id, comment.text or comment.body, comment.author)
    return _or_http(res)


@app.delete("/api/comments/{comment_id}")
async def api_delete_comment(comment_id: int):
    """Delete one comment by id. 404 if it doesn't exist."""
    res = await asyncio.to_thread(task_comments.delete_comment, comment_id)
    return _or_http(res)


@app.post("/api/tasks")
async def api_create_task(task: TaskCreate):
    """Create a task and (in one round-trip) link it to a project.

    `hermes kanban create` has no --project flag, so project/sprint are set via
    the sidecar (sprints.assign_task_*) right after create. We pass --json to get
    the new task id back reliably instead of scraping stdout."""
    if task.due_date is not None:
        _, due_error = sprints.normalize_due_date(task.due_date)
        if due_error:
            raise HTTPException(400, due_error)
    if task.sprint_id:
        sprint_check = await asyncio.to_thread(
            sprints.validate_sprint_target, task.sprint_id)
        if sprint_check.get("status") == "error":
            code = 404 if sprint_check.get("reason") == "not_found" else 409
            raise HTTPException(code, sprint_check["error"])
    envelope_parts = (task.contract_cmd, task.practice_text, task.run_context)
    if any(part is not None for part in envelope_parts) and not all(
            part is not None for part in envelope_parts):
        raise HTTPException(400, "contract_cmd, practice_text and run_context must be supplied together")

    # Fold acceptance criteria into the body under a heading (no separate column yet).
    body = task.body or ""
    if task.acceptance_criteria:
        body = (body + "\n\n## Acceptance\n" + task.acceptance_criteria).strip()

    cmd = [db.hermes_bin(), "kanban", "create", task.title, "--json",
           "--assignee", task.assignee, "--created-by", task.created_by,
           "--workspace", task.workspace]
    if body:
        cmd += ["--body", body]
    if task.priority:
        cmd += ["--priority", str(task.priority)]

    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise HTTPException(500, f"hermes kanban create failed: {result.stderr.strip() or result.stdout.strip()}")

    # Parse the task id out of the JSON create output.
    task_id = None
    try:
        payload = json.loads(result.stdout.strip())
        task_id = payload.get("id") or payload.get("task_id") or (payload.get("task") or {}).get("id")
    except Exception:
        # Fallback: scrape a t_xxx token from stdout.
        import re as _re
        m = _re.search(r"\bt_[0-9a-f]+\b", result.stdout)
        task_id = m.group(0) if m else None

    warnings = []
    project_assigned = False
    # Phase 1 (item 4): a task always lands in SOME project. Resolve the operator's
    # explicit choice, else the Inbox floor (the identity trigger also enforces
    # this for any writer, but resolving here keeps the id in the response).
    target_project = await asyncio.to_thread(identity.resolve_create_project, task.project_id, session_key=None)
    if task_id:
        try:
            await asyncio.to_thread(sprints.assign_task_project, task_id, target_project)
            project_assigned = True
        except Exception as e:
            warnings.append(f"created but project link failed: {e}")
    elif task.project_id:
        warnings.append("created but could not parse task id to link project")

    sprint_assigned = None if not task.sprint_id else False
    if task_id and task.sprint_id:
        try:
            sprint_res = await asyncio.to_thread(
                sprints.assign_task_sprint, task_id, task.sprint_id)
            if sprint_res.get("status") == "error":
                warnings.append(
                    f"sprint link failed: {sprint_res.get('error', 'assignment failed')}")
            else:
                sprint_assigned = True
        except Exception as e:
            warnings.append(f"sprint link failed: {e}")

    envelope_status = None
    if task_id and task.contract_cmd is not None:
        contract_res = await asyncio.to_thread(
            governance.set_contract, task_id, task.contract_cmd)
        if contract_res.get("status") != "ok":
            warnings.append(f"contract setup failed: {contract_res.get('error')}")
        else:
            envelope_res = await asyncio.to_thread(
                governance.set_run_envelope, task_id, task.practice_text,
                task.practice_host, task.run_context)
            envelope_status = envelope_res.get("status")
            if envelope_status != "ready":
                warnings.append(f"run envelope is {envelope_status}: {envelope_res.get('reason')}")

    # The commercial lineage, if the caller named one. A REFUSED link is a
    # warning, not a 500: the task itself was really created (the CLI already
    # committed it), so failing the request would tell the caller nothing was
    # written when something was. The refusal is still surfaced — a silently
    # dropped deal_id is the quiet lie this whole layer refuses.
    deal_linked = False
    if task_id and task.deal_id:
        res = await asyncio.to_thread(crm.link_task_deal, task_id, task.deal_id)
        if res.get("status") == "error":
            warnings.append(f"deal link failed: {res['error']}")
        else:
            deal_linked = True
    elif task.deal_id:
        warnings.append("created but could not parse task id to link deal")

    due_date_set = None
    if task.due_date is not None:
        due_date_set = False
        if task_id:
            due_res = await asyncio.to_thread(
                sprints.update_task_fields, task_id, due_date=task.due_date)
            if due_res.get("status") == "error":
                warnings.append(f"due date failed: {due_res['error']}")
            else:
                due_date_set = True
        else:
            warnings.append("created but could not parse task id to set due date")

    return {
        "status": "created",
        "task_id": task_id,
        "project_assigned": project_assigned,
        "sprint_assigned": sprint_assigned,
        "deal_linked": deal_linked,
        "due_date_set": due_date_set,
        "envelope_status": envelope_status,
        "warnings": warnings,
    }


@app.get("/api/tasks/{task_id}/run-envelope")
def api_get_run_envelope(task_id: str):
    envelope = governance.get_run_envelope(task_id)
    if envelope is None:
        raise HTTPException(404, "run envelope not found")
    return envelope


@app.post("/api/tasks/{task_id}/run-envelope")
def api_set_run_envelope(task_id: str, body: dict):
    required = ("contract_cmd", "practice_text", "host", "context")
    missing = [key for key in required if key not in body]
    if missing:
        raise HTTPException(400, f"missing fields: {', '.join(missing)}")
    contract = governance.set_contract(task_id, body["contract_cmd"])
    if contract.get("status") != "ok":
        return _or_http(contract)
    return _or_http(governance.set_run_envelope(
        task_id, body["practice_text"], body["host"], body["context"]))

@app.get("/api/sessions/{host}/{session_name}/history")
async def get_session_history(host: str, session_name: str):
    history = await asyncio.to_thread(get_prompt_history, host, session_name)
    return {"history": history}


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: str, update: TaskUpdate):
    """Move/update a task. The human board owns task status directly via the
    sidecar (sprints.set_task_status): `hermes kanban` verbs only transition
    between hermes-native statuses and `complete` even exits 0 while silently
    refusing our synthetic `in_progress`, so verbs can't reliably move a board
    card. Assignee still goes through the kanban CLI.

    `deal_id` is REFUSED here (ruling 5) — the commercial lineage has its own
    named route, `PATCH /api/tasks/{task_id}/deal`. Refused FIRST, before any
    other field is applied, so a body that mixes a legal edit with an illegal
    one writes nothing at all rather than half of what it asked for."""
    if update.deal_id is not None:
        raise HTTPException(
            400, "deal_id is not editable here — use PATCH "
                 "/api/tasks/{task_id}/deal (the named lineage writer, ruling 5)")
    # This endpoint commits several independent sidecars. Refuse a bad deadline
    # before any status/assignee writer can create a partial mixed-field PATCH.
    if update.due_date is not None:
        _, due_error = sprints.normalize_due_date(update.due_date)
        if due_error:
            raise HTTPException(400, due_error)
    if update.status:
        res = sprints.set_task_status(task_id, update.status)
        if res.get("status") == "error":
            raise HTTPException(400, res["error"])
    if update.assignee:
        # Sidecar reassignment (dispatch to an agent / reclaim to me) — reliable
        # for any name and logs the handoff to the audit trail.
        sprints.set_task_assignee(task_id, update.assignee)
    if update.auto_cycle is not None:
        # P0-7: toggle the per-task cycle auto-commit opt-out.
        sprints.set_auto_cycle(task_id, update.auto_cycle)
    if update.initiative_id is not None or update.clear_initiative:
        # P1-3: direct initiative attribution (or clear).
        res = graph.set_task_initiative(task_id, None if update.clear_initiative else update.initiative_id)
        if res.get("error"):
            raise HTTPException(404 if "not found" in res["error"] else 400, res["error"])
    if any(v is not None for v in (update.title, update.body, update.priority, update.due_date)):
        res = sprints.update_task_fields(task_id, title=update.title, body=update.body,
                                         priority=update.priority, due_date=update.due_date)
        if res.get("status") == "error":
            raise HTTPException(404, res["error"])
    # Backlog Phase 1: schedule into / out of an ISO week.
    if update.scheduled_week is not None or update.clear_scheduled_week:
        res = sprints.set_scheduled_week(
            task_id, None if update.clear_scheduled_week else update.scheduled_week)
        if res.get("status") == "error":
            raise HTTPException(404, res["error"])
    # "This week" commits the task to the current active cycle (no-op if none exists).
    if update.assign_active_cycle:
        active = sprints.get_active_sprint()
        if active:
            res = sprints.assign_task_sprint(task_id, active["id"])
            if res.get("status") == "error":
                raise HTTPException(400, res["error"])
    return {"status": "updated"}


@app.post("/api/tasks/{task_id}/accept")
def api_accept_task(task_id: str):
    """Operator accepts an agent's completion — the human gate (PRD §7). Stamps
    reviewed_at so the card leaves the Inbox and settles into the Fleet's Done."""
    res = sprints.accept_task(task_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: str):
    """Guarded hard delete: refuses accepted work; tombstone event kept."""
    res = sprints.delete_task(task_id)
    if res.get("status") == "error":
        raise HTTPException(404 if "not found" in res["error"] else 409, res["error"])
    return res


@app.post("/api/projects/{project_id}/archive")
def api_archive_project(project_id: str):
    res = sprints.archive_project(project_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/agents")
async def api_register_agent(body: dict):
    res = await asyncio.to_thread(graph.register_agent, body.get("name", ""), kind=body.get("kind"),
                               host=body.get("host"), skills=body.get("skills"),
                               notes=body.get("notes"))
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/tasks/bulk-accept")
def api_bulk_accept(body: dict):
    """The sanctioned mass human-gate: each id routes through accept_task
    (done+reviewed+verification row) — never raw SQL. all_review=true resolves
    the id set SERVER-side (every review-status task), so UI callers never
    re-filter the task firehose."""
    ids = body.get("task_ids") or []
    if body.get("all_review"):
        conn = db.get_conn()
        try:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE status = 'review'").fetchall()]
        finally:
            conn.close()
    return sprints.bulk_accept(ids)


@app.post("/api/tasks/{task_id}/reject")
def api_reject_task(task_id: str, body: dict = None):
    """Operator rejects a task — the negative human gate. Sets status='rejected'
    and stores the optional reason (JSON {reason: 'optional text'})."""
    body = body or {}
    res = sprints.reject_task(task_id, reason=body.get("reason", ""))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- The Canvas / Today (design Phase 3) ---
# The Today tab is a COMPOSITION of server-side queries (canvas.get_day_plan):
# the same surface the MCP get_day_plan verb serves to Hermes, so the dashboard
# and the rituals can never disagree about what "today" contains.

@app.get("/api/day-plan")
def api_day_plan(date: Optional[str] = None, candidates: bool = False):
    """The Today canvas: do / review / needs-you / later / overdue zones
    (+ the standup's candidate plan with ?candidates=true)."""
    res = canvas.get_day_plan(date, include_candidates=candidates)
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/day-review")
def api_day_review(date: Optional[str] = None):
    """Hour-by-hour evidence timeline for one local calendar day."""
    try:
        return day_review.get_day_review(date or "today")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/day-review/raw")
def api_day_review_raw(date: Optional[str] = None):
    """Unmerged collector evidence, without inferred gap blocks."""
    try:
        return day_review.get_day_review(date or "today", raw=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/day-plan/candidates")
def api_plan_candidates(date: Optional[str] = None):
    """The standup's candidate plan: overdue + carry-overs + cycle tasks."""
    res = canvas.plan_candidates(date)
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/day-plan/wrap")
def api_wrap_day(body: dict = None):
    """The 19:00 wrap-up write: carried_over events + the day's digest."""
    res = canvas.wrap_day((body or {}).get("date"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/day-plan")
def api_plan_day(body: dict):
    """Commit a day's plan (the morning confirm): {task_ids: [...], date?}."""
    res = canvas.plan_day(body.get("task_ids") or [], date=body.get("date"),
                          replace=body.get("replace", True))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


# --- The 3x-daily ritual (consolidation spec §3) ---
# ONE deterministic composer (dashboard/brief.py), ONE renderer, persisted per
# (date, slot). No LLM anywhere in this path (red line 4): every number comes
# from a query or an existing server-side composition, so the brief can never
# narrate momentum that did not happen. The cron scripts curl these routes and
# send the rendered text themselves — the CRON NEVER WRITES STATE; this process
# does every write.

@app.post("/api/brief/{slot}")
def api_brief_compose(slot: str, date: Optional[str] = None, force: bool = False):
    """Compose (or return the already-stored) brief for a slot.

    Idempotent per (date, slot): a re-fired cron gets `already_composed: true`
    and the SAME payload back, never a second composition or a second post.

    `?force=1` is the RECOVERY route — it recomposes and OVERWRITES the stored
    row (fresh payload, fresh `created_at`, `sent_at` cleared). It exists
    because a brief composed against an incomplete schema was otherwise frozen
    for the whole day: on 2026-07-29 the morning brief was composed 11 minutes
    before m02_spine added `deals.project_id`, so four won deals worth $194,500
    were missing from every surface that reads the stored payload and the only
    repair was hand-written SQL. Not for the cron scripts — they must stay
    idempotent, or an at-least-once retry posts a second Telegram message."""
    try:
        return brief.get_or_compose(slot, date, force=force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/brief/{slot}/sent")
def api_brief_sent(slot: str, date: Optional[str] = None):
    """Delivery callback: stamp `sent_at` once the transport confirmed."""
    try:
        res = brief.mark_sent(slot, date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/brief/latest")
def api_brief_latest():
    """The most recently composed brief — the web mirror of the Telegram text."""
    res = brief.latest()
    if res is None:
        raise HTTPException(404, "no brief composed yet")
    return res


@app.patch("/api/tasks/{task_id}/plan")
def api_plan_task(task_id: str, body: dict):
    """Plan/unplan one task: {planned_for?|clear?, plan_order?, due_date?}."""
    res = canvas.plan_task(
        task_id, planned_for=body.get("planned_for"),
        plan_order=body.get("plan_order"), due_date=body.get("due_date"),
        clear_plan=bool(body.get("clear")))
    if res.get("status") == "error":
        raise HTTPException(400 if "task not found" not in res["error"] else 404, res["error"])
    return res


# --- PRD Phase 3: the push/pull loop (agent-side + operator dispatch) ---
# These HTTP routes are the internal-MCP mirror of loop.py: the same core the
# `hermes-orchestrator` MCP server calls, exposed so the operator UI can drive
# dispatch and so the loop is testable end-to-end over HTTP.

@app.get("/api/pool")
def api_pool(agent: Optional[str] = None, skills: Optional[str] = None):
    """What's claimable right now: the open pool + (if agent given) its queue."""
    return loop.list_pool(agent=agent, skills=skills)


@app.post("/api/tasks/{task_id}/claim")
def api_claim(task_id: str, body: dict):
    """Agent pulls a task Pool→Working (atomic). Returns task + acceptance + workspace."""
    agent = body.get("agent")
    if not agent:
        raise HTTPException(400, "agent required")
    res = loop.claim_task(task_id, agent, session_id=body.get("session_id"))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/claim-next")
def api_claim_next(body: dict):
    """Claim the highest-priority claimable task for an agent (races-safe)."""
    agent = body.get("agent")
    if not agent:
        raise HTTPException(400, "agent required")
    return loop.claim_next(agent, skills=body.get("skills"))


@app.post("/api/tasks/{task_id}/progress")
def api_progress(task_id: str, body: dict):
    """Live progress push: sets progress_note/pct + heartbeat (+ optional
    step advance: plan → code → validate)."""
    res = loop.report_progress(task_id, body.get("note", ""), pct=body.get("pct"),
                               agent=body.get("agent"), step=body.get("step"))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/tasks/{task_id}/heartbeat")
def api_heartbeat(task_id: str, body: dict = None):
    # item 4: pass the caller's agent id so the ownership guard can verify the
    # claim holder (a task under a live claim only accepts its owner's beat).
    res = loop.heartbeat(task_id, agent=(body or {}).get("agent"))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/tasks/{task_id}/result")
def api_result(task_id: str, body: dict):
    """Agent reports a result → the §7 auto-accept-vs-escalate rule routes it."""
    res = loop.report_result(
        task_id, body.get("result", ""), passed=body.get("passed", True),
        artifacts=body.get("artifacts"), agent=body.get("agent"))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/tasks/{task_id}/blocked")
def api_blocked(task_id: str, body: dict):
    """Agent reports blocked → always escalates to the operator's Inbox."""
    res = loop.report_blocked(task_id, body.get("reason", ""), agent=body.get("agent"))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/discoveries")
def api_discovery(body: dict):
    """Agent-found "this needs you" → new task straight into the operator Inbox."""
    if not body.get("title"):
        raise HTTPException(400, "title required")
    res = loop.escalate_discovery(
        body["title"], body.get("body", ""), reason=body.get("reason", ""),
        related_task=body.get("related_task"), agent=body.get("agent"))
    if res.get("status") == "error":
        raise HTTPException(500, res["error"])
    return res


@app.post("/api/tasks/{task_id}/pool")
def api_set_pool(task_id: str, body: dict):
    """Operator dispatch: put a task into (or pull it from) the open pool."""
    res = loop.set_pool(task_id, bool(body.get("pool", True)))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/tasks/{task_id}/dispatch")
def api_dispatch_task(task_id: str, body: dict = None):
    """Honest dispatch — one verb, one saga, and the answer is what happened.

    Body: {executor_kind: hermes|codex|claude, executor_target?, dispatch_id?,
    thread_id?}. `dispatch_id` is the idempotency key: the same one twice
    returns the stored outbox row and fires no second side effect.

    `spawn_failed` / `send_failed` come back as **200 with that state**, not as
    an HTTP error: the saga ran, the outbox row is the truth, and the toast
    renders it verbatim. Turning a partial dispatch into a 500 would hide which
    side effects actually landed — the exact dishonesty this replaces (spec §2:
    `dispatchTo()` wrote a flag and always said "Dispatched").

    HUMAN-ORIGIN GUARD (ruling 2): phase-1 dispatch is always human-initiated —
    the click IS the approval, which is why no ASK-queue mechanics are needed
    yet. Enforced structurally, not by prose: this is a mutating /api/* POST, so
    `MutatingAuthMiddleware` demands the dashboard Bearer token, and dispatch is
    deliberately absent from mcp_server.py. Do not add MCP parity for it."""
    body = body or {}
    res = dispatch.dispatch_task(
        task_id,
        executor_kind=body.get("executor_kind"),
        executor_target=body.get("executor_target"),
        dispatch_id=body.get("dispatch_id"),
        thread_id=body.get("thread_id"),
    )
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 400, res["error"])
    return res


@app.get("/api/tasks/{task_id}/dispatches")
def api_task_dispatches(task_id: str):
    """The task's dispatch history — proof a dispatch is an event that happened,
    not a flag that got set."""
    return {"dispatches": dispatch.list_dispatches(task_id)}


@app.post("/api/tasks/{task_id}/plan")
def api_plan_task(task_id: str, body: dict = None):
    """Human-origin planning launch; the final outbox row is the response."""
    body = body or {}
    res = task_planning.plan_task(
        task_id,
        planner=body.get("planner"),
        request_id=body.get("request_id"),
    )
    if res.get("status") == "error":
        code = 404 if res.get("code") == "not_found" else 400
        raise HTTPException(code, res["error"])
    return res


# --- Telegram thread registry (spec §2) ---------------------------------------
# The table behind `dispatch._resolve_thread`. These two routes are the Agents
# threads panel: see where work is announced, and fix a binding without a SQL
# client. There is deliberately NO create/delete — the registry is hand-seeded
# (m02_spine) and a thread is never auto-created for a project, so the list
# cannot grow with the backlog.

@app.get("/api/threads")
async def api_threads():
    """The whole registry, active first, each row carrying `archived` and the
    bound project's name. Archived rows ship (the panel shows history) but the
    flag is what keeps them out of pickers."""
    return await asyncio.to_thread(thread_registry.list_threads)


@app.patch("/api/threads/{thread_id}")
async def api_update_thread(thread_id: str, body: dict = None):
    """Edit one thread: {name?, role?, project_id?, status?}. PATCH semantics —
    only supplied keys are written, and an explicit `"project_id": null` clears
    the binding. `role` is checked against the schema's 5-value CHECK and
    `project_id` against a live project, so a bad edit is a typed 400 here
    instead of an IntegrityError (or, worse, a binding that silently routes
    every dispatch to the Hoy fallback forever)."""
    res = await asyncio.to_thread(thread_registry.update_thread, thread_id, body or {})
    if isinstance(res, dict) and res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 400, res["error"])
    return res


@app.patch("/api/tasks/{task_id}/pin-bottom")
def api_pin_bottom(task_id: str, body: dict):
    """Park a task at the bottom of its kanban column (pinned=true) or restore it.
    Purely positional — the task keeps its status, unlike 'blocked'."""
    res = db.set_pinned_bottom(task_id, bool(body.get("pinned", True)))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/tasks/{task_id}/autonomy")
def api_set_autonomy(task_id: str, body: dict):
    """Operator sets task autonomy: 'auto' (auto-accept eligible) | 'dispatch'."""
    res = loop.set_autonomy(task_id, body.get("autonomy", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/agent-status")
async def api_agent_status():
    return await asyncio.to_thread(agent_status.get_agent_status)


@app.get("/api/coordinators")
def api_coordinators():
    """Derived Coordinators view — Code / Research / Commercial sub-agents, each
    with the tasks it's handling, last activity, and a green/yellow/red signal.
    Pure function of the live task rows (no new backend state)."""
    tasks = [t.to_dict() for t in db.get_all_tasks()]
    return coordinators.build(tasks)


# --- Sugerencias: el gate humano del loop de digestión -----------------------
# Los GET viven detrás del bearer (ver `_SENSITIVE_GET_PREFIXES`): llevan citas
# textuales de clientes. Accept/dismiss/edit son POST/PATCH, así que
# `MutatingAuthMiddleware` ya los cubre — y deliberadamente NO existen como
# verbos MCP: el gate humano es estructural, no una regla de prompt.

@app.get("/api/suggestions")
async def api_list_suggestions(status: str = "open", limit: int = 100):
    from .digestion import list_suggestions
    return await asyncio.to_thread(list_suggestions, status, limit)


@app.get("/api/objectives")
async def api_list_objectives(status: str = None, entity_kind: str = None,
                              entity_id: str = None, limit: int = 100):
    from .digestion import list_objectives
    return await asyncio.to_thread(list_objectives, status, entity_kind, entity_id, limit)


@app.post("/api/suggestions/{sid}/accept")
async def api_accept_suggestion(sid: str, body: dict = None):
    from .digestion import accept_suggestion
    return _or_http(await asyncio.to_thread(accept_suggestion, sid, body or None))


@app.post("/api/suggestions/{sid}/dismiss")
async def api_dismiss_suggestion(sid: str):
    from .digestion import dismiss_suggestion
    return _or_http(await asyncio.to_thread(dismiss_suggestion, sid))


@app.patch("/api/suggestions/{sid}")
async def api_edit_suggestion(sid: str, body: dict):
    from .digestion import edit_suggestion
    return _or_http(await asyncio.to_thread(
        edit_suggestion, sid, body.get("title"), body.get("project_id"), body.get("due")))


@app.get("/api/whatsapp/review")
async def api_whatsapp_review():
    """Los carriles a revisar. Lleva forma (cuánto, cuándo, qué tanto contestas)
    y contra-evidencia; **nunca** texto de un mensaje."""
    from .whatsapp_review import review
    return await asyncio.to_thread(review)


@app.post("/api/whatsapp/stage")
async def api_whatsapp_stage(body: dict):
    """Fija un lote por REGLA y devuelve qué contiene. No autoriza nada."""
    from .whatsapp_review import stage
    return _or_http(await asyncio.to_thread(stage, body.get("carril", "")))


@app.post("/api/whatsapp/stage/{batch_id}/quitar")
async def api_whatsapp_unstage(batch_id: str, body: dict):
    from .whatsapp_review import unstage
    return _or_http(await asyncio.to_thread(unstage, batch_id, body.get("jid", "")))


@app.post("/api/whatsapp/commit")
async def api_whatsapp_commit(body: dict):
    """Autoriza el lote fijado en el servidor. El cuerpo lleva un identificador,
    no una lista: lo que se enseñó es lo que se autoriza."""
    from .whatsapp_review import commit
    return _or_http(await asyncio.to_thread(commit, body.get("batch_id", "")))


@app.post("/api/whatsapp/chats/{jid}/decidir")
async def api_whatsapp_decide(jid: str, body: dict):
    from .whatsapp_review import decide
    return _or_http(await asyncio.to_thread(decide, jid, bool(body.get("allowed"))))


@app.post("/api/whatsapp/barrido")
async def api_whatsapp_sweep():
    """Saca del tracker todo lo que sigue sin decidir. Deniega; nunca autoriza —
    no existe el verbo gemelo, y es a propósito."""
    from .whatsapp_review import sweep_pending
    return _or_http(await asyncio.to_thread(sweep_pending))


@app.post("/api/whatsapp/chats/{jid}/backfill")
async def api_whatsapp_backfill(jid: str, body: dict = None):
    """Baja el historial ANTERIOR al permiso. Sin `confirmar` solo previsualiza:
    devuelve cuántos mensajes y de qué fechas, sin leer nada."""
    from .whatsapp_review import backfill
    b = body or {}
    return _or_http(await asyncio.to_thread(
        backfill, jid, int(b.get("dias", 30)), bool(b.get("confirmar"))))


@app.get("/api/whatsapp/chats")
async def api_whatsapp_chats(estado: str = "permitidos", q: str = "", limit: int = 60):
    """Un lado u otro de la decisión, buscable. `estado=denegados` es lo que hace
    reversible un «no»: sin poder encontrar ese chat entre mil, reconsiderarlo
    deja de ser posible."""
    from .whatsapp_review import listed_chats
    return await asyncio.to_thread(listed_chats, estado == "permitidos", q, limit)


@app.get("/api/whatsapp/permitidos")
async def api_whatsapp_allowed():
    """Lo que hoy está autorizado. Una lista de permisos que no se puede ver
    completa es una que nadie revoca."""
    from .whatsapp_review import allowed_chats
    return await asyncio.to_thread(allowed_chats)


@app.get("/api/capture/status")
async def api_capture_status():
    """Salud del loop en números — sin títulos ni citas, a propósito."""
    from .digestion import capture_status
    return await asyncio.to_thread(capture_status)


@app.get("/api/memory")
async def api_memory():
    """Hermes's structured memory stores (agent memory + user profile),
    categorized by content, with capacity stats. Pure read of the on-disk
    memory files — no new backend state."""
    return await asyncio.to_thread(memory_view.build)


@app.patch("/api/memory")
async def api_memory_update(body: dict):
    """Edit a memory entry in place (Memory tab inline editor). Addressed by
    {source: agent|user, index, content}."""
    res = await asyncio.to_thread(memory_view.update_entry,
        body.get("source", "agent"), body.get("index", -1), body.get("content", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.delete("/api/memory")
async def api_memory_delete(source: str = "agent", index: int = -1):
    """Delete a memory entry (Memory tab). Backs up the store first."""
    res = await asyncio.to_thread(memory_view.delete_entry, source, index)
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/graph")
async def api_graph(request: Request, q: Optional[str] = None,
                    node: Optional[str] = None, hops: int = 2,
                    type: Optional[str] = None,
                    include_archived: bool = False):
    """Knowledge-graph memory query surface.
      ?stats            → node/edge counts by type
      ?q=<query>        → nodes matching label (+ their 1-hop neighbors)
      ?node=<id>&hops=N → expand a subgraph N hops out
      (no params)       → the whole graph (capped) for the visualization
      ?include_archived=true → include archived nodes (default: excluded)
    """
    store = gmem.get_store()
    if request.query_params.get("stats") is not None:
        return await asyncio.to_thread(store.stats)
    if node:
        return await asyncio.to_thread(store.expand, node, hops=max(0, min(hops, 4)))
    if q:
        matches = await asyncio.to_thread(store.search, q, type=type, include_archived=include_archived)
        # Include the immediate neighborhood so a search reads as a subgraph.
        nodes = {n["id"]: n for n in matches}
        edges = {}
        for m in matches:
            for nb in store.neighbors(m["id"]):
                nodes.setdefault(nb["id"], {k: v for k, v in nb.items() if k != "edge"})
                e = nb["edge"]
                edges[e["id"]] = {"id": e["id"], "src": e["src"], "dst": e["dst"], "type": e["type"]}
        return {"query": q, "matches": [m["id"] for m in matches],
                "nodes": list(nodes.values()), "edges": list(edges.values())}
    return await asyncio.to_thread(store.all_graph, include_archived=include_archived)


@app.get("/api/recall")
async def api_recall(q: str, project_id: Optional[str] = None,
                     task_id: Optional[str] = None, k: int = 8):
    """Phase 5 — THE unified memory read: graph search → authoritative source →
    {fact, source, ref, staleness}. Semantica is shadow enrichment only: the
    canonical result is always returned even when the projection is down."""
    bounded_k = max(1, min(k, 25))
    canonical, semantic = await asyncio.gather(
        asyncio.to_thread(gmem.recall, q, project_id=project_id, task_id=task_id, k=bounded_k),
        asyncio.to_thread(semantica_client.query, q, min(bounded_k, 10))
        if semantica_client.enabled()
        else asyncio.sleep(0, result={"status": "disabled"}),
    )
    # Byte-shape preservation is the rollback contract: a disabled or failed
    # projection returns the canonical payload without even a diagnostic key.
    if semantic.get("status") == "ok":
        canonical["semantic_context"] = semantic
    return canonical


@app.get("/api/graph/rebuild")
async def api_graph_rebuild():
    """Re-ingest the graph from all sources (tasks, git, notes, MEMORY.md).
    Also archives stale nodes past their TTL (GLM review fix #23)."""
    def _rebuild():
        store = gmem.get_store()
        archived = gmem.archive_stale(store=store)
        summary = gmem.ingest_all(store, rebuild=True)
        summary["archived_stale"] = archived
        # The export is privacy-filtered before it reaches the mounted input
        # directory. Projection failure cannot roll back or taint canonical data.
        try:
            from semantica_service import export_source
            output = Path(os.environ.get(
                "SEMANTICA_INPUT",
                str(Path.home() / ".local/share/orchestratormaxxing/semantica-host/input/source.json"),
            ))
            summary["semantica_export"] = export_source(output)
            summary["semantica"] = semantica_client.rebuild()
        except Exception as exc:
            summary["semantica"] = {"status": "fallback", "reason": type(exc).__name__}
        return summary
    return await asyncio.to_thread(_rebuild)


@app.get("/api/semantica/status")
async def api_semantica_status():
    """Read-only projection health; never changes Hermes's canonical graph."""
    if not semantica_client.enabled():
        return {"status": "disabled"}
    return await asyncio.to_thread(semantica_client.health)


@app.get("/api/memory/metabolism")
async def api_memory_metabolism():
    """Memory metabolism metrics (arXiv:2604.12034): the digestive system view.
    Returns 24h counts: inputs_processed, facts_distilled, memories_evicted,
    decay_triggered, total_active, total_archived."""
    return await asyncio.to_thread(gmem.get_metabolism_stats)


@app.get("/api/memory/contradiction")
async def api_memory_contradiction(new_fact: str, existing: list[str] = Query(default=[])):
    """Check if a new fact contradicts existing facts (MemClaw pattern).
    Pass existing as repeated query params: ?new_fact=...&existing=fact1&existing=fact2
    Caps input length to prevent abuse."""
    if len(new_fact) > 2000:
        raise HTTPException(400, "new_fact too long (max 2000 chars)")
    if sum(len(f) for f in existing) > 10000:
        raise HTTPException(400, "existing facts too long (max 10000 chars total)")
    return await asyncio.to_thread(gmem.contradiction_check, new_fact, existing)


@app.get("/api/graph/related")
async def api_graph_related(q: str, limit: int = 5):
    """A-MEM neighbour search: graph nodes related to a query by label
    word-overlap. Parity twin of the MCP `find_related` tool — the read the
    UI/API needs to surface existing facts a new write might evolve/supersede."""
    return {"related": await asyncio.to_thread(gmem.find_related, q, limit=max(1, min(limit, 25)))}


@app.post("/api/graph/evolve")
async def api_graph_evolve(body: dict):
    """A-MEM memory evolution: merge properties into a graph node and re-stamp
    last_verified. Parity twin of the MCP `evolve_node` tool. Mutating —
    Bearer-gated by the auth middleware like every other write."""
    node_id = (body or {}).get("node_id")
    props = (body or {}).get("properties") or {}
    if not node_id:
        raise HTTPException(400, "node_id required")
    if not isinstance(props, dict):
        raise HTTPException(400, "properties must be an object")
    ok = await asyncio.to_thread(gmem.evolve_node, node_id, props)
    return {"evolved": bool(ok), "node_id": node_id}


@app.get("/api/lakehouse/overview")
async def api_lakehouse_overview():
    """Lakehouse tab: key metrics + data freshness, fetched from the standalone
    lakehouse over MCP (client only — no import, no shared DB). Runs in a worker
    thread so the blocking MCP call doesn't stall the event loop."""
    return await asyncio.to_thread(lakehouse.overview)


@app.get("/api/lakehouse/lineage")
async def api_lakehouse_lineage():
    """Lakehouse tab: flattened source→target lineage edges across key metrics."""
    return await asyncio.to_thread(lakehouse.lineage)


@app.get("/api/lakehouse/ask")
async def api_lakehouse_ask(q: str):
    """Lakehouse tab search box → ask_lakehouse (constrained NL→metric) over MCP."""
    return await asyncio.to_thread(lakehouse.ask, q)


@app.get("/api/mcp/manifest")
async def api_mcp_manifest():
    """Self-describing connection manifest for external Claude Code fleets.
    Advertises the transport, the two least-authority scopes, and which tools
    live in each — so a fleet can discover how to connect (PRD §8). The tool
    lists are sourced from mcp_server.py (single source of truth), never
    re-declared here, so they can't drift from what's actually enforced."""
    default_tools, privileged_tools = [], []
    try:
        import sys as _sys
        _orch_dir = str(Path(__file__).resolve().parent.parent)
        if _orch_dir not in _sys.path:
            _sys.path.insert(0, _orch_dir)
        import mcp_server as _mcp
        priv = _mcp.PRIVILEGED_TOOLS
        for t in _mcp.TOOLS:
            entry = {"name": t["name"], "description": t.get("description", "")}
            (privileged_tools if t["name"] in priv else default_tools).append(entry)
    except Exception as e:  # pragma: no cover — manifest degrades, never 500s
        return {"error": f"mcp_server introspection failed: {e}"}

    server_path = str(Path(__file__).resolve().parent.parent / "mcp_server.py")
    return {
        "server": "hermes-orchestrator",
        "version": "2.0.0",
        "positioning": "A shared brain, work queue, and human approval loop for your Claude Code fleet.",
        "transport": {
            "type": "stdio",
            "note": "Spawned as a subprocess by the MCP client (Claude Code / Desktop / Cursor).",
            "connect_command": f"claude mcp add hermes-orchestrator -- python3 {server_path}",
            "connector_script": str(Path(__file__).resolve().parent.parent / "connect-fleet.sh"),
            # The one address (dashboard/db.py::dashboard_url) — a manifest that
            # hardcoded its own copy is a fourth default waiting to drift.
            "tailnet_dashboard": db.dashboard_url(),
        },
        "scopes": {
            "default": {
                "grant": "automatic — what every external fleet gets",
                "capabilities": ["orient (read)", "pull (claim)", "report", "declare"],
                "tools": [t["name"] for t in default_tools],
            },
            "privileged": {
                "grant": "operator-only — HERMES_MCP_SCOPE=privileged + matching HERMES_MCP_TOKEN (constant-time compare)",
                "capabilities": ["dispatch", "edit roadmap/sprints/projects", "change trust grade"],
                "tools": [t["name"] for t in privileged_tools],
            },
        },
        "safety": [
            "Least authority: default token cannot restructure the plan.",
            "Every agent action is audited (status_changed / result / escalation events).",
            "No load-bearing auto-commit — agents propose and report.",
            "The trust dial is operator-only; an agent can never raise its own trust grade.",
            "Dashboard mutating endpoints (POST/PATCH/DELETE/PUT) require Bearer auth — HERMES_DASHBOARD_TOKEN or ~/.config/orchestratormaxxing/dashboard-token.",
        ],
        "tool_detail": {"default": default_tools, "privileged": privileged_tools},
    }


@app.get("/api/activity")
def api_activity(limit: int = 30):
    return db.get_recent_activity(limit)


@app.get("/api/stats")
def api_stats():
    return db.get_stats()


_PROCESS_STARTED = time.time()


@app.get("/api/health")
async def api_health():
    return {
        "status": "ok",
        "tailscale_ip": TAILSCALE_IP,
        "kanban_db": str(db.KANBAN_DB),
        "kanban_db_exists": db.KANBAN_DB.exists(),
        "uptime_seconds": round(time.time() - _PROCESS_STARTED, 1),
    }


@app.get("/api/errors/recent")
async def api_recent_errors(hours: float = 24):
    """Recent uncaught-exception records from logs/api-errors-*.log (the
    X-Request-ID error store), summarized for status surfaces: count in the
    window + the last 10 records (tracebacks trimmed — the request_id is the
    handle back to the full record in the file)."""
    if hours <= 0 or hours > 24 * 14:
        raise HTTPException(400, "hours must be in (0, 336]")

    def _read():
        import datetime as _dt
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
        records = []
        today = _dt.date.today()
        for d in (today - _dt.timedelta(days=1), today):
            f = API_LOG_DIR / f"api-errors-{d.strftime('%Y%m%d')}.log"
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    ts = _dt.datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S%z")
                except Exception:
                    continue
                if ts >= cutoff:
                    records.append({k: rec.get(k) for k in
                                    ("ts", "request_id", "method", "path",
                                     "exception", "message")})
        return {"hours": hours, "count": len(records), "errors": records[-10:]}

    return await asyncio.to_thread(_read)


_COGLOAD_CHECK_CACHE: dict = {"val": None, "at": 0.0}
_COGLOAD_CHECK_TTL = 30.0


def _healthz_checks() -> tuple[dict, list]:
    """Downstream dependency checks for /healthz. Returns (checks, degraded)
    — degraded is the list of FAILING gating check names; [] = healthy.

    GATING (drive 200 vs 503): kanban DB readable; sessions-cache pipeline not
    flatlined (a populated cache older than 10× its TTL means the sweeper/
    refresh path is dead — a cold cache right after boot is fine).
    INFORMATIONAL (reported, never gate): remote SSH hosts (a sleeping laptop
    is normal) and the MCP SSE server (optional; may not be armed).
    """
    checks: dict = {}
    degraded: list = []

    # -- kanban DB (gating) --
    try:
        conn = db.get_conn()
        conn.execute("SELECT 1 FROM tasks LIMIT 1")
        conn.close()
        checks["kanban_db"] = {"ok": True, "path": str(db.KANBAN_DB)}
    except Exception as e:
        checks["kanban_db"] = {"ok": False, "path": str(db.KANBAN_DB), "error": str(e)[:120]}
        degraded.append("kanban_db")

    # -- sessions cache (gating on flatline only) --
    cache = sessions._SESSIONS_CACHE
    ttl = sessions.SESSIONS_CACHE_TTL
    if cache["data"] is None:
        checks["sessions_cache"] = {"ok": True, "state": "cold", "ttl_seconds": ttl}
    else:
        age = round(time.time() - cache["ts"], 1)
        flatlined = age > ttl * 10
        cc = cache["data"].get("claude_code", [])
        checks["sessions_cache"] = {
            "ok": not flatlined,
            "state": "flatlined" if flatlined else ("fresh" if age < ttl else "refreshing"),
            "age_seconds": age, "ttl_seconds": ttl,
            "sessions": len(cc),
            "tmux_attached": sum(1 for x in cc if x.get("tmux_attached")),
        }
        if flatlined:
            degraded.append("sessions_cache")

    # -- SSH probe pool (informational) --
    hosts = {}
    try:
        online = {h["host"] for h in sessions._online_remote_hosts()}
        for h in sessions.REMOTE_HOSTS:
            hosts[h["host"]] = "online" if h["host"] in online else "offline"
    except Exception as e:
        hosts = {"error": str(e)[:120]}
    checks["ssh_probe_pool"] = {"ok": True, "hosts": hosts,
                                "note": "offline hosts are normal (laptops sleep); never gates"}

    # -- dual-store consistency: tasks.sprint_id ⇔ task_sprints (P2-1, §2.3) --
    # A data-integrity signal, NOT a dependency outage: report drift (ok=false +
    # counts + sample ids) so monitors can alert, but keep it OUT of `degraded` so
    # it never 503s the dashboard (the service is up; the ledger just drifted).
    try:
        drift = sprints.sprint_ledger_drift()
        drift["note"] = ("sprint_id ⇔ task_sprints invariant (§2.3); drift is a "
                         "data-integrity signal — reported, never gates 200/503")
        checks["sprint_ledger"] = drift
    except Exception as e:
        checks["sprint_ledger"] = {"ok": True, "state": "uncheckable", "error": str(e)[:120]}

    # -- MCP SSE server (informational) --
    sse_port = os.environ.get("HERMES_MCP_SSE_PORT", "5556")
    try:
        import urllib.request as _url
        with _url.urlopen(f"http://127.0.0.1:{sse_port}/health", timeout=2) as r:
            h = json.loads(r.read())
        checks["mcp_sse"] = {"ok": True, "state": "up", "scope": h.get("scope"),
                             "tools_visible": h.get("tools_visible"),
                             "rate_limit_per_min": h.get("rate_limit_per_min")}
    except Exception:
        checks["mcp_sse"] = {"ok": True, "state": "unreachable", "port": sse_port,
                             "note": "optional transport; not armed ≠ unhealthy"}

    # -- cogload collector (ADVISORY: reported, never gates) --
    # CACHED. This block forks systemctl and reads the digest store, and
    # _healthz_checks runs on every /healthz AND every /api/ops-status hit —
    # which the Health tab polls. Uncached it made the page never reach
    # networkidle. The collector's own design log makes this same point about
    # live_session_type() (659x saving by not forking loginctl in a loop): an
    # observer that costs real resources changes the thing it observes.
    # Why this exists: on 2026-08-16 the collector went blind and the nightly
    # unit failed three nights running, and this endpoint reported
    # {"status":"ok","degraded":[]} the entire time — cogload had no
    # representation here at all, so the only place the fault was visible was
    # a tab you had to think to open. Advisory, not gating: a personal
    # collector must never 503 the dashboard (same posture as sprint_ledger).
    _cc = _COGLOAD_CHECK_CACHE
    if _cc["at"] and (time.time() - _cc["at"]) < _COGLOAD_CHECK_TTL:
        checks["cogload"] = _cc["val"]
        return checks, degraded
    try:
        from dashboard import cogload as _cg
        st = _cg.live_status() or {}
        cog: dict = {
            "ok": bool(st.get("ok")),
            "state": st.get("last_status") or "unknown",
            "session_type": st.get("session_type"),
        }
        # Freshness: a digest that stopped folding is as blinding as a dead
        # collector, and it fails silently.
        try:
            rows = _cg.load_digest_days() or []
            newest = max((r.get("day") or "") for r in rows) if rows else ""
            cog["newest_digest_day"] = newest or None
            if newest:
                from datetime import date as _d
                y, m, dd = (int(x) for x in newest.split("-"))
                cog["digest_stale"] = (_d.today() - _d(y, m, dd)).days > 2
                if cog["digest_stale"]:
                    cog["ok"] = False
            else:
                cog["digest_stale"] = True
                cog["ok"] = False
        except Exception as e:
            cog["digest_error"] = str(e)[:80]
        # The nightly unit's own verdict. Linux/systemd only; absence is not
        # a failure, it is simply not measurable here.
        try:
            import subprocess as _sp
            r = _sp.run(["systemctl", "--user", "show",
                         "cogload-nightly.service", "-p", "Result", "--value"],
                        capture_output=True, text=True, timeout=2)
            res = (r.stdout or "").strip()
            if res:
                cog["nightly_result"] = res
                if res != "success":
                    cog["ok"] = False
        except Exception:
            pass
        checks["cogload"] = cog
    except Exception as e:
        # Unmeasurable is not healthy — it is unknown, and it says so.
        checks["cogload"] = {"ok": False, "state": f"unreadable: {str(e)[:80]}"}
    _cc["val"], _cc["at"] = checks["cogload"], time.time()

    return checks, degraded


@app.get("/metrics")
async def metrics():
    """Prometheus exposition endpoint. Exposes the middleware-fed request
    counters and latency histogram, plus the MCP SSE server's counters
    (federated here because its bind is loopback-only — the dashboard is the
    tailnet-reachable scrape point). Text format, version 0.0.4."""
    lines = [
        "# HELP hermes_api_requests_total API requests by method, path, status.",
        "# TYPE hermes_api_requests_total counter",
    ]
    with _METRICS_LOCK:
        for (method, path, status), n in sorted(_REQ_COUNTS.items()):
            lines.append(
                f'hermes_api_requests_total{{method="{method}",path="{path}",status="{status}"}} {n}')
        h = dict(_LAT_HIST)
        buckets = list(h["buckets"])
    lines += [
        "# HELP hermes_api_request_duration_seconds API request latency.",
        "# TYPE hermes_api_request_duration_seconds histogram",
    ]
    cum = 0
    for i, le in enumerate(_LAT_BUCKETS_S):
        cum += buckets[i]
        lines.append(f'hermes_api_request_duration_seconds_bucket{{le="{le}"}} {cum}')
    cum += buckets[-1]
    lines.append(f'hermes_api_request_duration_seconds_bucket{{le="+Inf"}} {cum}')
    lines.append(f'hermes_api_request_duration_seconds_sum {h["sum"]:.6f}')
    lines.append(f'hermes_api_request_duration_seconds_count {h["count"]}')

    # Session gauges — read from the CACHE only (a scrape must never trigger
    # SSH probes); absent while the cache is cold.
    cache = sessions._SESSIONS_CACHE
    if cache["data"]:
        cc = cache["data"].get("claude_code", [])
        by_status: dict = {}
        for sess in cc:
            st = sess.get("status") or "unknown"
            by_status[st] = by_status.get(st, 0) + 1
        lines += [
            "# HELP hermes_sessions Claude Code sessions by status (from the sessions cache).",
            "# TYPE hermes_sessions gauge",
        ]
        for st, n in sorted(by_status.items()):
            lines.append(f'hermes_sessions{{status="{st}"}} {n}')
        lines += [
            "# HELP hermes_sessions_tmux_attached Sessions with a live terminal attached.",
            "# TYPE hermes_sessions_tmux_attached gauge",
            f'hermes_sessions_tmux_attached {sum(1 for x in cc if x.get("tmux_attached"))}',
        ]
        # "Stuck" detector: the max idle age among sessions HOLDING A LIVE
        # TERMINAL. Transcript-only sessions idle for days are normal (a
        # transcript is just a file); a held terminal idle for an hour is a
        # session someone forgot — the alert target.
        now_ts = int(time.time())
        attached_idle = [max(0, now_ts - (x.get("modified") or now_ts))
                         for x in cc if x.get("tmux_attached")]
        lines += [
            "# HELP hermes_sessions_attached_max_idle_seconds Max idle age among live-terminal sessions.",
            "# TYPE hermes_sessions_attached_max_idle_seconds gauge",
            f'hermes_sessions_attached_max_idle_seconds {max(attached_idle) if attached_idle else 0}',
        ]

    # Gating /healthz checks as 0/1 gauges — the CHEAP subset only (one
    # SELECT + pure memory; the full /healthz also shells out to tailscale,
    # which has no place on a 15s scrape path).
    db_ok = 1
    try:
        def _db_ping():
            _c = db.get_conn(); _c.execute("SELECT 1 FROM tasks LIMIT 1"); _c.close()
        await asyncio.to_thread(_db_ping)
    except Exception:
        db_ok = 0
    cache_ok = 1
    if cache["data"] is not None and (time.time() - cache["ts"]) > sessions.SESSIONS_CACHE_TTL * 10:
        cache_ok = 0
    lines += [
        "# HELP hermes_healthz_check_ok Gating /healthz dependency state (1=ok, 0=down).",
        "# TYPE hermes_healthz_check_ok gauge",
        f'hermes_healthz_check_ok{{check="kanban_db"}} {db_ok}',
        f'hermes_healthz_check_ok{{check="sessions_cache"}} {cache_ok}',
    ]

    # Federated MCP SSE counters (absent when the transport isn't running —
    # an absent series is Prometheus-idiomatic for "target down").
    def _sse_counters():
        import urllib.request as _url
        port = os.environ.get("HERMES_MCP_SSE_PORT", "5556")
        with _url.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            return json.loads(r.read())
    try:
        sse = await asyncio.to_thread(_sse_counters)
        lines += [
            "# HELP hermes_mcp_sse_requests_total MCP SSE requests admitted by the rate limiter.",
            "# TYPE hermes_mcp_sse_requests_total counter",
            f'hermes_mcp_sse_requests_total {sse.get("requests_total", 0)}',
            "# HELP hermes_mcp_sse_rate_limit_rejections_total MCP SSE requests rejected (429).",
            "# TYPE hermes_mcp_sse_rate_limit_rejections_total counter",
            f'hermes_mcp_sse_rate_limit_rejections_total {sse.get("rate_limit_rejections_total", 0)}',
            f'hermes_mcp_sse_active_sessions {sse.get("active_sessions", 0)}',
        ]
    except Exception:
        pass

    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/ops-status")
async def api_ops_status():
    """Aggregated operational snapshot — the same document orchestrator
    status --json assembles, composed SERVER-SIDE so the Health tab and any
    external consumer read one endpoint instead of four."""
    checks, degraded = await asyncio.to_thread(_healthz_checks)
    # Advisory failures: reported, never gating. Derived FROM `checks` rather
    # than maintained by hand, so a future advisory check cannot be added and
    # silently left out of the top-level verdict.
    attention = sorted(
        name for name, c in checks.items()
        if isinstance(c, dict) and c.get("ok") is False and name not in degraded
    )
    sess = await asyncio.to_thread(sessions.get_all_sessions)
    errs = await api_recent_errors(hours=24)
    cc = sess.get("claude_code", [])
    by_status: dict = {}
    for x in cc:
        st = x.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "ts": int(time.time()),
        # THREE-valued, deliberately. Binary ok/degraded is how three failed
        # nightlies and three blind days hid behind a green tick: an advisory
        # check that never enters `degraded` could not move the top-level
        # status at all, so "ok" meant "nothing GATING is broken" while
        # reading as "nothing is broken".
        "status": ("degraded" if degraded
                   else "attention" if attention
                   else "ok"),
        "degraded": degraded,
        "attention": attention,
        "uptime_seconds": round(time.time() - _PROCESS_STARTED, 1),
        "sessions": {
            "total": len(cc),
            "by_status": by_status,
            "idle": by_status.get("idle", 0),
            "tmux_attached": sum(1 for x in cc if x.get("tmux_attached")),
        },
        "auto_tags": {
            "stale": sum(1 for x in cc if x.get("tag") == "stale"),
            "needs_attention": sum(1 for x in cc if x.get("tag") == "needs-attention"),
        },
        "errors_24h": errs,
        "checks": checks,
    }


@app.get("/healthz")
async def healthz():
    """Monitoring endpoint: downstream dependency status. 200 when the gating
    dependencies hold, 503 when one is down — probes can alert on the code
    alone (condition-first: the body is for the human who follows up)."""
    checks, degraded = await asyncio.to_thread(_healthz_checks)
    return JSONResponse(
        {"status": "degraded" if degraded else "ok",
         "degraded": degraded,          # failing dep names — [] when healthy
         "checks": checks},
        status_code=503 if degraded else 200,
    )


# --- Sessions API ---

@app.get("/api/sessions")
def api_sessions():
    """Get all active sessions across local + remote hosts. `_cache` carries the
    snapshot's freshness (age / fresh / refreshing) for the UI staleness dot —
    the scan is serve-stale-while-revalidate, so a response may be cached."""
    data = sessions.get_all_sessions()
    return {**data, "_cache": sessions.cache_meta()}


@app.get("/api/sessions/{host}/{session_name}/output")
def api_session_output(host: str, session_name: str, lines: int = 50):
    """Get recent output from a session: a live terminal capture or a parsed
    Claude Code transcript (kind tells the UI which to render)."""
    # Reachable from chat clients via the MCP proxy — clamp here too so no
    # caller can request an unbounded capture.
    lines = max(1, min(500, lines))
    view = sessions.get_session_view(host, session_name, lines)
    return {
        "host": host,
        "session": session_name,
        "kind": view["kind"],
        "output": view["output"],
        "messages": view["messages"],
    }


@app.post("/api/sessions/{host}/{session_name}/send")
def api_session_send(host: str, session_name: str, body: dict):
    """Send a prompt to an interactive session."""
    text = body.get("text", "")
    if not text:
        raise HTTPException(400, "Missing 'text' field")
    result = sessions.send_to_session(host, session_name, text)
    if result.get("status") == "sent":
        add_to_prompt_history(host, session_name, text)
    return result


@app.post("/api/sessions/{host}/{session_name}/resend-last")
async def api_session_resend_last(host: str, session_name: str):
    """Re-send the LAST recorded instruction to an idle session — the
    supervisor's manual nudge, automated (CLI: orchestrator sessions
    --revive). History is keyed by the name the original send used, so try
    the given name first, then its resolved tmux target (covers UUID-vs-
    tmux-name key drift). Deliberately does NOT re-append to history: the
    queue keeps the instruction, not the nudges."""
    history = get_prompt_history(host, session_name)
    key_used = session_name
    if not history:
        target = await asyncio.to_thread(sessions.resolve_tmux_target, host, session_name)
        if target and target != session_name:
            history = get_prompt_history(host, target)
            key_used = target
    if not history:
        raise HTTPException(404, f"no recorded instruction for '{session_name}' — "
                                 "send one from the dashboard first")
    text = history[0]["text"]
    result = await asyncio.to_thread(sessions.send_to_session, host, session_name, text)
    if result.get("status") != "sent":
        raise HTTPException(404 if "transcript only" in str(result.get("error", "")) else 400,
                            result.get("error", "send failed"))
    return {"status": "resent", "host": host, "session": result.get("session"),
            "text": text, "recorded_at": history[0]["ts"], "history_key": key_used}


@app.post("/api/sessions/{host}/{session_name}/revive")
def api_session_revive(host: str, session_name: str):
    """Revive a session on its origin machine."""
    return _or_http(sessions.revive_session(host, session_name))


@app.post("/api/sessions/create")
async def api_session_create(body: dict):
    """Spin up a fresh Claude Code tmux session from the UI (+ New Session).
    host ∈ {local, ricalaniscloud}; name is normalized to the claude-* convention."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Missing 'name' field")
    host = body.get("host") or "local"
    return await asyncio.to_thread(
        sessions.create_session, host, name, body.get("cwd") or None)


@app.post("/api/sessions/prune-transcripts")
async def api_prune_transcripts(body: dict = None):
    """Hide transcript-only sessions idle > hours (default 48) from the
    listing. Display hygiene only — files untouched; a pruned session that
    wakes up auto-unhides."""
    hours = float((body or {}).get("hours", 48))
    if hours <= 0:
        raise HTTPException(400, "hours must be > 0")
    return await asyncio.to_thread(sessions.prune_transcript_sessions, hours)


@app.post("/api/sessions/{host}/{session_name}/kill")
def api_session_kill(host: str, session_name: str):
    """Kill a live tmux session (resolver-backed: a transcript UUID resolves
    to its terminal first). Best-effort semantics from the module: already-
    gone is 'gone', not an error — idempotent cleanup."""
    return _or_http(sessions.kill_session(host, session_name))


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, tab: str = "today"):
    """Main dashboard. The page is FULLY JS-rendered (every tab fetches its
    own API); the old server-side context (tasks/sessions/stats/usage) had
    ZERO Jinja references and cost 3.5s of SSH probes + full-table scans per
    page load — verified dead and removed. First paint is now instant."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"active_tab": tab},
        # The page is a live dashboard whose inline JS changes between deploys;
        # never let a browser serve a stale cached copy.
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    """Agent tasks tab — delegates to index with tab=agent-tasks."""
    return await index(request, tab="agent-tasks")


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request):
    """Sessions tab — delegates to index with tab=sessions."""
    return await index(request, tab="sessions")


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    """Usage tab — delegates to index with tab=usage."""
    return await index(request, tab="usage")


# --- Review queue (Little's law on the human gate) ---

@app.get("/api/review-queue")
async def api_review_queue(days: int = 7):
    """Little's-Law instrumentation for the human review gate. Read-only by
    contract (byte-identical DB test); pinned metric definitions live in
    dashboard/review_queue.py."""
    from dashboard import review_queue
    return review_queue.review_queue_summary(days=days)


# --- Governance: contract coverage (K10 v1) ---

@app.get("/api/governance/contract-coverage")
async def api_contract_coverage():
    """Contract-adequacy + verification-provenance ratios (read-only; the
    pinned definitions live on governance.contract_coverage)."""
    return governance.contract_coverage()


@app.get("/api/governance/envelope-coverage")
async def api_envelope_coverage():
    """M1 adoption + C2 typed-brake countermetric; read-only."""
    return governance.envelope_coverage()


# --- Usage API ---

@app.get("/api/usage")
async def api_usage(limit: int = 100):
    """Return per-provider usage (Claude Max + Ollama Cloud) + the unified
    cross-provider roll-up. Backward-compatible: data.claude and data.ollama
    are still at the top level; data.unified has the combined totals."""
    summary = usage.get_usage_summary(limit=limit)
    summary["unified"] = _providers.get_unified_summary()
    return summary


@app.get("/api/usage/providers")
async def api_usage_providers():
    """Unified cross-provider usage summary — the transversal view.

    Returns combined token totals, estimated cost, and a per-provider
    comparison (tokens + % share + cost) across all registered providers.
    Also includes the full per-provider breakdown under .providers.
    """
    return _providers.get_unified_summary()


@app.post("/api/usage/ollama-completion")
async def api_log_ollama_completion(body: dict):
    """Log a chat completion usage event from Ollama Cloud."""
    model = body.get("model", "unknown")
    usage_blob = body.get("usage", {})
    metadata = body.get("metadata", {})
    return usage.log_ollama_completion(model, usage_blob, metadata)


@app.post("/api/usage/refresh-ollama")
async def api_refresh_ollama():
    """Re-scrape ollama.com/settings on demand and return the fresh usage.
    Runs scrape_ollama_usage.py as a subprocess (it drives the gstack browse
    daemon), then invalidates the usage cache so the returned numbers reflect
    the new scrape. Manual counterpart to the 30-min cron
    (~/.hermes/scripts/ollama-usage-refresh.sh). Scraper exit codes:
    0 = ok · 2 = not logged in · 3 = logged in but couldn't parse."""
    script = Path(__file__).parent / "scrape_ollama_usage.py"
    py = os.environ.get("HERMES_PYTHON") or sys.executable
    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [py, str(script)], capture_output=True, text=True, timeout=90,
            ),
        )
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"ok": False, "error": "Scraper timed out (browse daemon slow or unreachable)."},
            status_code=504,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    usage.invalidate_ollama_cache()
    fresh = usage.get_usage_summary()
    ok = proc.returncode == 0
    result = {"ok": ok, "returncode": proc.returncode, "usage": fresh}
    if not ok:
        result["error"] = (proc.stderr or proc.stdout or "").strip()[:500]
        result["hint"] = {
            2: "Not logged in — run `browse connect` and log into ollama.com, then retry.",
            3: "Logged in but couldn't parse the page — check raw_text in the usage store.",
        }.get(proc.returncode)
    return result


@app.post("/api/usage/refresh-claude")
async def api_refresh_claude():
    """Force a cache-bust + re-fetch of the live Claude Max limits (Claude Code's
    usage API, OAuth token from ~/.claude/.credentials.json). Unlike Ollama there's
    no scraper — the token is refreshed by *running Claude Code*; this drops the
    90s live-limits cache and re-hits the API so the card reflects the current
    token state immediately. Reports live_available=false (with a token-expired
    hint) when the API is still unreachable."""
    usage.invalidate_claude_cache()
    fresh = await asyncio.get_event_loop().run_in_executor(None, usage.get_usage_summary)
    limits = (fresh.get("claude") or {}).get("limits") or {}
    live_ok = limits.get("source") == "live"
    result = {"ok": True, "live_available": live_ok, "usage": fresh}
    if not live_ok:
        st = limits.get("live_http_status")
        result["stale"] = True
        result["live_http_status"] = st
        result["hint"] = ("Token expired — run Claude Code to refresh."
                          if limits.get("token_expired")
                          else "Live usage API unavailable — showing estimates.")
    return result


@app.get("/api/projects")
def api_projects():
    return sprints.list_projects()


@app.post("/api/projects")
def api_create_project(body: dict):
    name = body.get("name", "")
    slug = body.get("slug", name.lower().replace(" ", "-"))
    desc = body.get("description", "")
    color = body.get("color", "#3b82f6")
    icon = body.get("icon", "📦")
    if not name:
        raise HTTPException(400, "Missing 'name'")
    return sprints.create_project(name, slug, desc, color, icon)


@app.get("/api/projects/load")
def api_projects_load():
    """Carga de la cartera: horas declaradas contra horas de entrega medidas.

    Va ANTES de las rutas `/{project_id}/...` a propósito — `load` es un
    sustantivo fijo, y registrarlo después dejaría su suerte a merced del
    orden de resolución. Lectura pura: no escribe nada."""
    return capacity.project_load()


@app.get("/api/projects/plan")
def api_projects_plan():
    return plan.week_plan()


@app.put("/api/projects/plan/cell")
def api_projects_plan_cell(body: dict):
    res = plan.set_cell(
        body.get("project_id"), body.get("iso_week"), body.get("hours")
    )
    if res.get("status") == "error" and str(res.get("error", "")).startswith(
            "la semana en curso"):
        raise HTTPException(409, res["error"])
    return _or_http(res)


@app.post("/api/projects/plan/apply")
def api_projects_plan_apply(body: dict):
    return _or_http(plan.apply_overdue(body.get("project_id")))


@app.put("/api/projects/plan/weeks")
def api_projects_plan_weeks(body: dict):
    return _or_http(plan.set_weeks_ahead(body.get("weeks_ahead")))


@app.get("/api/projects/{project_id}/detail")
def api_project_detail(project_id: str):
    """Project detail modal (Phase E): project + tasks-by-column + stats +
    initiatives + active-cycle id. Accepts an id or slug. Read-only."""
    return _or_http(sprints.get_project_detail(project_id))


@app.post("/api/projects/{project_id}/delivered")
def api_project_mark_delivered(project_id: str):
    """Conversion verb 3/3 — "Mark delivered": the delivery leg's terminal
    state. Sets projects.status='delivered' + delivered_at through the single
    lifecycle writer and events one `project_delivered` row per won deal it
    covers. It does NOT touch `deals.stage` (ruling 2): a won deal stays won
    forever, and "delivered" as a READ derives from deals.project_id →
    projects.status. The response names those deals in `delivered_deals`.

    Conversion verbs are HUMAN-ONLY by design (spec red line 11): an agent may
    create, comment on, progress and complete tasks unattended, but it only ever
    PROPOSES a conversion into the brief — Ricardo taps. That is why this verb
    lives here, in the dashboard API, and is deliberately absent from
    mcp_server.py; the absence is the guard, so do not add MCP parity for it.

    Idempotent — a project already at 'delivered' returns `already_delivered`
    with an empty `delivered_deals` and writes nothing."""
    res = crm.mark_project_delivered(project_id)
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 400, res["error"])
    return res


@app.patch("/api/projects/{project_id}")
def api_update_project(project_id: str, body: dict):
    """Inline-edit a project — rename, recolor, fix slug, update description,
    move it along its lifecycle. PATCH semantics: only supplied fields are
    written. Archived projects must be unarchived first.

    `status` is NOT one of the editable fields: it goes through
    `sprints.set_project_status`, the single writer (ruling 8), which is what
    turns an unknown value into a 400 here instead of an arbitrary string in the
    column. The two halves are applied in order — the descriptive edit first, so
    a rejected status never lands a half-edit — and either one alone is a valid
    request."""
    status = body.get("status")
    fields = {k: body.get(k)
              for k in ("name", "slug", "description", "color", "icon",
                        "weekly_hours", "kind", "tier")}
    res = None
    if any(v is not None for v in fields.values()):
        res = sprints.update_project(project_id, **fields)
        if isinstance(res, dict) and res.get("status") == "error":
            return _or_http(res)
    if status is not None:
        try:
            written = sprints.update_project_status(project_id, status, via="api_patch")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if written is None:
            raise HTTPException(404, f"project '{project_id}' not found")
        if res is None:
            res = {"status": "updated", "project_id": project_id}
        res["project_status"] = written
    if res is None:
        # Raised here rather than delegated to update_project's own refusal:
        # that message lists only the fields IT owns, and `status` is now a
        # legitimate thing to PATCH.
        raise HTTPException(
            400, "nothing to update (name/slug/description/color/icon/status)")
    return _or_http(res)


# --- Attachments + the five-facet project hub (journey F3.5) ------------------
# The routes the four host skills call: Claude, the Codex plugin, Hermes and
# OpenCode all end a deep planning session the same way — write the plan into
# ~/dev/planning under the project slug, then POST it here. Everything else the
# hub shows (Fireflies meetings, projects.repo_path, tasks) is DERIVED, so those
# facets fill in with no writes at all.

# `_or_http`'s substring heuristic is deliberately not used here: it maps any
# message containing "not found" to 404, which would answer 404 for a POST whose
# *body* names a node that does not exist — a validation failure of the request,
# not a missing endpoint. The codes are explicit instead.
_ATTACHMENT_STATUS = {"not_found": 404}


def _attachment_or_http(res):
    if isinstance(res, dict) and res.get("status") == "error":
        raise HTTPException(_ATTACHMENT_STATUS.get(res.get("code"), 400), res["error"])
    return res


@app.post("/api/attachments")
async def api_create_attachment(body: dict):
    """Register a pointer on a node: {node_kind, node_id, kind, title, url?,
    path?, source_agent?}.

    UPSERT semantics — re-posting the same target on the same node refreshes
    `title`/`source_agent`/`updated_at` and answers `created: false` with the
    original id. That is the whole point: four hosts run the same skill, a
    planning repo gets re-synced, and none of that may grow the drawer.

    Every refusal is a typed 400 naming the legal vocabulary (`node_kind`,
    `kind`), an empty title, a pointer with neither url nor path, or a node that
    does not exist — because the CHECKs and the missing FK would otherwise
    surface as a 500 (or, for the node, as a silent success that renders an
    empty facet forever)."""
    body = body or {}
    res = await asyncio.to_thread(
        attachment_hub.add_attachment,
        body.get("node_kind"), body.get("node_id"), body.get("kind"),
        body.get("title"), body.get("url"), body.get("path"),
        body.get("source_agent"))
    return _attachment_or_http(res)


@app.get("/api/attachments")
async def api_list_attachments(node_kind: str, node_id: str):
    """Everything attached to one node, flat and grouped by kind. `by_kind`
    always carries all four keys, so an empty facet is an empty list."""
    res = await asyncio.to_thread(attachment_hub.list_for, node_kind, node_id)
    return _attachment_or_http(res)


@app.delete("/api/attachments/{attachment_id}")
async def api_delete_attachment(attachment_id: str):
    """Unregister one pointer. A missing row is 404, never a silent 200 — the
    caller must not believe it removed something still on the board."""
    res = await asyncio.to_thread(attachment_hub.remove, attachment_id)
    return _attachment_or_http(res)


@app.get("/api/projects/{project_id}/hub")
async def api_project_hub(project_id: str):
    """The five facets of one project: conversations · resources · code · plans
    · tasks. Accepts an id or a slug. Read-only.

    Two facets are unions of registered pointers with facts the system already
    had — conversations with the Fireflies meetings of this project's deals
    (`deals.project_id`), code with `projects.repo_path` — and `tasks` is a
    COUNT over the real table, never attachment rows: the work has its own
    writers and a second copy here would be a second answer."""
    res = await asyncio.to_thread(attachment_hub.list_project_hub, project_id)
    return _attachment_or_http(res)


# --- The journey pulse (fase 1 step 7 + ADICIÓN 9) ---------------------------
# One reference in, the whole client cycle out — task-first, grouped by journey
# stage. The composer is `dashboard/pulse.py`; this stays a wrapper on purpose
# (the MCP verb calls the same composer in-process, so the two frontends cannot
# drift into two different pulses).

_PULSE_STATUS = {"not_found": 404, "ambiguous": 400}


@app.get("/api/journey/pulse")
async def api_journey_pulse(ref: str = Query(..., description="account, deal or project — id, slug or name")):
    """¿Dónde está <cliente>? — deals + delivering project + OPEN tasks typed by
    journey stage (contacto → … → cobranza) + what is planned today + the last
    human touches + the attachment counts.

    Refusals are typed and carry their evidence: `ambiguous` answers 400 with
    the candidate list (the caller re-asks with an exact ref — the resolution
    never guesses, ruling 1) and `not_found` answers 404. Read-only."""
    res = await asyncio.to_thread(pulse.compose, ref)
    if isinstance(res, dict) and res.get("status") == "error":
        code = res.get("code", "error")
        raise HTTPException(_PULSE_STATUS.get(code, 400),
                            {k: v for k, v in res.items() if k != "status"})
    return res


@app.get("/api/journey/horizon")
async def api_journey_horizon():
    """La línea de horizonte — the six numbers Ricardo's six questions reduce to,
    each with the deep link that answers it.

    `{clientes_activos, oportunidades_trabadas, proyectos_vivos, delegando,
    entregables, hoy_pendientes}`, every one carrying `{count, label, hint,
    target}` plus the fixed `order` the line renders in. Composed by
    `pulse.horizon()` — this stays a wrapper for the same reason the pulse does:
    one composer, so the web line and any agent reading it cannot drift.

    Read-only. Has no refusal branch: the horizon always answers (a zero is an
    answer), which is why the Today poll can call it unguarded."""
    return await asyncio.to_thread(pulse.horizon)


@app.get("/api/inbox/count")
def api_inbox_count():
    """How many tasks sit untriaged — the floor that reached 44 orphans while
    being visible only to someone reading SQL. `identity.inbox_count()` has
    existed since phase 1 item 4 and nothing served it; the chip that renders it
    needs the project id too (that is how a task is known to BE untriaged)."""
    return {"project_id": identity.inbox_id(), "count": identity.inbox_count()}


@app.get("/api/sprints")
def api_sprints(project_id: str = None):
    return sprints.list_sprints(project_id)


@app.get("/api/sprints/slots")
def api_sprint_slots():
    """Exact global-cycle slots for the unified Board: current, W+1, W+2."""
    return sprints.get_board_cycle_slots()


@app.get("/api/sprints/{sprint_id}/tasks")
def api_sprint_tasks(sprint_id: str):
    if sprints.get_sprint(sprint_id) is None:
        raise HTTPException(404, f"cycle '{sprint_id}' not found")
    return {"sprint_id": sprint_id, "tasks": sprints.get_sprint_tasks(sprint_id)}


@app.post("/api/sprints")
def api_create_sprint(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    project_id = body.get("project_id")
    name = body.get("name", "")
    goal = body.get("goal", "")
    start_date, end_date = body.get("start_date"), body.get("end_date")
    # Validate the optional window up front — otherwise a stray string reaches
    # create_cycle → _week_window(date.fromtimestamp(str)) → an opaque 500.
    for label, val in (("start_date", start_date), ("end_date", end_date)):
        if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float))):
            raise HTTPException(400, f"'{label}' must be a unix timestamp (number), not {type(val).__name__}")
    # Defense-in-depth for the New-cycle form's inline check: a custom range must
    # end on/after it starts.
    if start_date is not None and end_date is not None and end_date < start_date:
        raise HTTPException(400, "end_date must be on or after start_date")
    if project_id and name:
        # A project sprint's FK must resolve, else the INSERT 500s on the FK.
        if not db.resolve_project(project_id):
            raise HTTPException(404, f"project '{project_id}' not found")
        return _or_http(sprints.create_sprint(project_id, name, goal))
    # No project = a cross-project weekly CYCLE (Phase 3 item 5). start_date lets
    # the calendar create a SPECIFIC future week (create_cycle Mon-snaps it).
    return _or_http(sprints.create_cycle(name=name or None, goal=goal,
                                         start_date=start_date, end_date=end_date))


@app.get("/api/velocity")
def api_velocity():
    """Per-cycle committed vs velocity (accepted human-intent tasks) — the
    cycle_velocity VIEW, verbatim."""
    return {"cycles": sprints.get_velocity()}


@app.get("/api/cycle/active/board")
def api_cycle_board(sprint_id: str = None):
    """The Cycle tab (Phase A): the active cycle (or `sprint_id`) + its committed
    tasks grouped into board columns, velocity, a completed_at-derived burndown,
    days-left, and the icebox. Composed server-side (sprints.get_cycle_board) so
    the tab reads one query surface, never the /api/tasks firehose."""
    # An EXPLICIT sprint_id that doesn't resolve is a 404 — not a silent empty
    # board (which conflates "that cycle is gone" with "no active cycle").
    if sprint_id and sprints.get_sprint(sprint_id) is None:
        raise HTTPException(404, f"cycle '{sprint_id}' not found")
    return sprints.get_cycle_board(sprint_id)


@app.get("/api/cycle-board")
def api_cycle_board_alias(sprint_id: str = None):
    """Alias for /api/cycle/active/board — the active cycle (or `sprint_id`)
    grouped into board columns. Matches the MCP `get_cycle_board` verb's path so
    external callers of that name resolve instead of 404-ing."""
    if sprint_id and sprints.get_sprint(sprint_id) is None:
        raise HTTPException(404, f"cycle '{sprint_id}' not found")
    return sprints.get_cycle_board(sprint_id)


@app.get("/api/cycles/calendar")
def api_cycle_calendar(weeks_back: int = 2, weeks_fwd: int = 5):
    """The Cycle calendar strip (Phase B): one cell per ISO week (Mon-Sun) with
    the cycle that starts that week, its progress, and project-color dots. Empty
    future weeks return cycle_id=None for the 'Plan' affordance."""
    if weeks_back < 0 or weeks_fwd < 0:
        raise HTTPException(400, "weeks_back and weeks_fwd must be >= 0")
    if weeks_back + weeks_fwd > 104:
        raise HTTPException(400, "requested window too large (max 104 weeks)")
    return sprints.get_calendar(weeks_back, weeks_fwd)


@app.post("/api/cycles/roll")
def api_roll_cycle():
    """Manually trigger the weekly auto-roll (the sweeper also does this)."""
    return sprints.roll_cycle()


@app.post("/api/sprints/finish")
def api_finish_sprint():
    """The 🏁 Finish Sprint flow: archive accepted/rejected work, roll the
    unfinished pile into the next cycle, close the active one, activate next, and
    guarantee a +2 planning slot. Distinct from the auto roll_cycle sweeper."""
    return _or_http(sprints.finish_sprint())


@app.post("/api/sprints/{sprint_id}/start")
def api_start_sprint(sprint_id: str):
    return _or_http(sprints.start_sprint(sprint_id))


@app.post("/api/sprints/{sprint_id}/close")
def api_close_sprint(sprint_id: str, body: dict = None):
    next_sprint = body.get("next_sprint_id") if body else None
    return _or_http(sprints.close_sprint(sprint_id, next_sprint))


@app.delete("/api/sprints/{sprint_id}")
def api_delete_cycle(sprint_id: str):
    """Guarded delete of a planning/active cycle (removes a mistaken/empty one).
    Refuses a completed cycle; returns its committed tasks to the icebox."""
    return _or_http(sprints.delete_cycle(sprint_id))


@app.post("/api/sprints/reconcile")
def api_reconcile_sprint_ledger():
    """Repair sprint ledger drift — fix both forward orphans (missing
    task_sprints rows) and reverse orphans (stale open rows). Idempotent.
    Bearer-gated like all mutating endpoints."""
    return _or_http(sprints.reconcile_sprint_ledger())


@app.patch("/api/tasks/{task_id}/sprint")
def api_assign_sprint(task_id: str, body: dict):
    sprint_id = body.get("sprint_id")  # None = move to icebox
    return _or_http(sprints.assign_task_sprint(task_id, sprint_id))


@app.post("/api/cycles/{sprint_id}/commit")
def api_bulk_commit(sprint_id: str, body: dict):
    """Commit several tasks to a cycle at once (Phase F multi-select). Each id
    routes through assign_task_sprint (ledger per task). sprint_id='icebox'
    pulls them all to the icebox (sprint_id=None)."""
    ids = body.get("task_ids") or []
    if not isinstance(ids, list):
        raise HTTPException(400, "'task_ids' must be a list")
    target = None if sprint_id == "icebox" else sprint_id
    if target and sprints.get_sprint(target) is None:
        raise HTTPException(404, f"cycle '{target}' not found")
    return sprints.bulk_assign_sprint(ids, target)


@app.post("/api/cycles/reorder")
def api_cycle_reorder(body: dict):
    """Persist a manual drag-reorder of a cycle board's cards. Body:
    {sprint_id, order:[task_id,…]} — the new full top-to-bottom order; each task
    gets board_order = its index (get_cycle_board reads it back)."""
    sprint_id = (body or {}).get("sprint_id")
    order = (body or {}).get("order")
    if not sprint_id:
        raise HTTPException(400, "missing 'sprint_id'")
    if not isinstance(order, list):
        raise HTTPException(400, "'order' must be a list of task ids")
    return _or_http(sprints.reorder_cycle_tasks(sprint_id, order))


@app.patch("/api/tasks/{task_id}/project")
def api_assign_project(task_id: str, body: dict):
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(400, "Missing 'project_id'")
    return sprints.assign_task_project(task_id, project_id)


@app.patch("/api/tasks/{task_id}/deal")
def api_link_task_deal(task_id: str, body: dict):
    """Link a task to the deal it exists for — writer 3 of 3 (ruling 5).

    A NAMED route, deliberately, and the generic `PATCH /api/tasks/{id}` above
    refuses `deal_id` outright (400 `deal_id_named_route`). The distinction is
    not ceremony: the generic patch is the body an agent assembles from a diff
    of what it thinks changed, and lineage is not a field — it is an assertion
    about why this work exists, which is worth one explicit call. It is also the
    difference between an audit trail with a `deal_linked` event and a column
    that quietly changed value.

    Mirrors `/api/tasks/{id}/project` exactly (same shape, same idiom, same
    place in the file) so the pair reads as one convention rather than two.
    """
    deal_id = body.get("deal_id")
    if not deal_id:
        raise HTTPException(400, "Missing 'deal_id'")
    return _or_http(crm.link_task_deal(task_id, deal_id))


# --- Object graph (Phase 2): epics, agents, task-session ---

# Epics were FOLDED INTO PROJECTS (spec §1, migration m03): 1 row, 0 tasks, no
# page. `tasks.epic_id` stays in the schema as **frozen audit** — still read for
# display, never written again — so what dies here is the write/list surface,
# not the column and not the readers.
#
# 410 Gone, not 404: the routes existed, the *concept* was retired. A 404 tells
# a caller "wrong URL" and invites a retry; a 410 with a typed body tells it the
# resource is intentionally gone and why. `mcp_server.py`'s four epic verbs
# return this exact payload — tests/test_mcp_api_parity.py gates the symmetry,
# so the two frontends cannot drift into "gone here, alive there".
EPICS_GONE = {
    "error": "epics_folded",
    "hint": "epics were folded into projects (m03); tasks.epic_id is frozen audit",
}


def _epics_gone() -> JSONResponse:
    return JSONResponse(EPICS_GONE, status_code=410)


@app.get("/api/epics")
def api_epics(project_id: str = None):
    return _epics_gone()


@app.post("/api/epics")
def api_create_epic(body: dict = None):
    return _epics_gone()


@app.patch("/api/epics/{epic_id}")
def api_update_epic(epic_id: str, body: dict = None):
    return _epics_gone()


@app.patch("/api/tasks/{task_id}/epic")
def api_assign_epic(task_id: str, body: dict = None):
    return _epics_gone()


@app.get("/api/tasks/{task_id}/links")
def api_task_links(task_id: str):
    """MCP-parity for get_task_links: this task's dependency edges resolved to
    titles/status so the drawer can render them as clickable rows. parents =
    tasks this one depends on; children = tasks that depend on this one."""
    raw = db.get_task_links(task_id)

    def _resolve(ids):
        out = []
        for tid in ids:
            t = db.get_task(tid)
            if t:
                out.append({"id": t.id, "title": t.title, "status": t.status})
        return out

    return {"task_id": task_id,
            "parents": _resolve(raw.get("parents", [])),
            "children": _resolve(raw.get("children", []))}


@app.post("/api/tasks/{task_id}/links")
def api_add_task_link(task_id: str, body: dict):
    """Create a dependency edge for this task. direction=depends_on (default):
    the given other_id is a PARENT this task depends on; direction=blocks: the
    other_id is a CHILD that depends on this task."""
    other = (body.get("other_id") or "").strip()
    if not other:
        raise HTTPException(400, "other_id is required")
    direction = body.get("direction", "depends_on")
    if direction == "blocks":
        res = db.add_task_link(task_id, other)          # this → other
    else:
        res = db.add_task_link(other, task_id)          # other → this (default)
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


@app.delete("/api/tasks/{task_id}/links")
def api_remove_task_link(task_id: str, other_id: str, direction: str = "depends_on"):
    if direction == "blocks":
        return db.remove_task_link(task_id, other_id)
    return db.remove_task_link(other_id, task_id)


@app.get("/api/agents")
def api_agents():
    """The agent registry: assignees graded by outcome history, marked online
    if they have a live session."""
    sess = sessions.get_all_sessions()
    online = set()
    for s in sess.get("claude_code", []):
        if s.get("status") in ("active", "recent"):
            for key in ("display_name", "host_machine", "project"):
                v = s.get(key)
                if v:
                    online.add(str(v).lower())
                    online.add(str(v).split("/")[-1].lower())
    return graph.get_agents(online)


@app.patch("/api/agents/{name}/trust")
async def api_set_trust(name: str, body: dict):
    return await asyncio.to_thread(graph.set_agent_trust, name, body.get("trust_grade"))


@app.get("/api/sessions/{host}/{session_name}/tasks")
async def api_session_tasks(host: str, session_name: str):
    """Tasks hard-linked to a session (Task↔Session)."""
    return {"session_id": session_name, "tasks": await asyncio.to_thread(graph.tasks_for_session, session_name)}


@app.patch("/api/tasks/{task_id}/session")
async def api_link_session(task_id: str, body: dict):
    return await asyncio.to_thread(graph.set_task_session, task_id, body.get("session_id"))


@app.get("/api/icebox")
def api_icebox(project_id: str = None):
    return sprints.get_icebox_tasks(project_id)


@app.get("/api/delivered")
def api_delivered(project_id: str = None):
    return sprints.get_delivered_sprints(project_id)


# --- Sprint Planning Page — DELETED (spec §4 "Delete — relocate first", #4).
# `/planning` + planning.html are gone. Its ONE unique capability, "+ Project"
# (the only project-creation UI in the product), was relocated into the Work
# workspace first (index.html #new-project-modal); everything else it rendered
# is a duplicate of the Cycle sub-view. The `/api/planning*` JSON endpoints
# above are untouched — they were never the page.


# --- Roadmap API ---
#
# Initiatives were FOLDED INTO PROJECTS (spec §1, migration m03): quarter, tier,
# why, success_check, health and confidence moved onto `projects`, so the
# roadmap is now a read over the PROJECT spine and `quarters` is what the UI
# renders. The `initiatives` rows are kept as READ-ONLY ARCHIVE — the entity
# drawer, the deal→initiative chip and the deal form still resolve historical
# links through them — but nothing can create or edit one any more.
#
# 410 Gone, not 404: the routes existed, the *concept* was retired. A 404 tells
# a caller "wrong URL" and invites a retry; a 410 with a typed body tells it the
# resource is intentionally gone and what replaced it. `mcp_server.py`'s two
# initiative write verbs (create_initiative, edit_roadmap) return this exact
# payload — tests/test_initiatives_410.py gates the symmetry, so the two
# frontends cannot drift into "gone here, alive there".
INITIATIVES_GONE = {
    "error": "initiatives_folded",
    "hint": "initiatives were folded into projects (m03); use projects + quarter",
}


def _initiatives_gone() -> JSONResponse:
    return JSONResponse(INITIATIVES_GONE, status_code=410)


@app.get("/api/roadmap")
def api_get_roadmap():
    """The quarterly roadmap.

    `quarters` — the LIVE surface: projects grouped by `projects.quarter` with
    a DERIVED per-project progress roll-up (unscheduled projects land in a
    trailing `quarter: null` group). This is what the Roadmap view renders.

    `initiatives` — the FROZEN archive, still carrying its derived progress so
    the initiative drawer and the deal chain keep reading history. Nothing
    writes it: POST/PATCH here answer 410.
    """
    inits = strategy.list_initiatives()
    slugs = {p["id"]: p["slug"] for p in sprints.list_projects()}
    for init in inits:
        init["project_slug"] = slugs.get(init.get("project_id", ""))
        prog = graph.initiative_progress(init)
        init["epic_count"] = prog["epic_count"]
        init["task_in_flight"] = prog["task_in_flight"]
        if prog["task_total"] > 0:
            init["progress"] = prog["progress"]
            init["derived"] = True
            init["progress_scope"] = prog["scope"]
            init["task_total"] = prog["task_total"]
            init["task_done"] = prog["task_done"]
            # P2-3: per-initiative burndown (accepted-done vs ideal over the quarter).
            init["burndown"] = graph.initiative_burndown(init)
        else:
            init["derived"] = False
    return {"quarters": strategy.projects_by_quarter(), "initiatives": inits}

def _current_quarter() -> str:
    t = time.localtime()
    return f"{t.tm_year}-Q{(t.tm_mon - 1) // 3 + 1}"


@app.post("/api/roadmap")
def api_create_initiative(body: dict = None):
    """RETIRED — initiatives were folded into projects (m03). Create a project
    and set its quarter/tier instead.

    Untyped body on purpose: 410 outranks 422. Validating a payload for a dead
    resource would answer the wrong question ("your fields are wrong" instead of
    "this concept no longer exists"), so the InitiativeCreate model died with
    the route it validated.
    """
    return _initiatives_gone()

@app.get("/api/roadmap/{initiative_id}/drilldown")
def api_roadmap_drilldown(initiative_id: str):
    """Initiative→Project→Cycle→Task tree (epics included when declared)."""
    init = strategy.get_initiative(initiative_id)
    if not init:
        raise HTTPException(404, "Initiative not found")
    return graph.initiative_drilldown(init)


@app.get("/api/roadmap/{initiative_id}/events")
def api_initiative_events(initiative_id: str):
    """The initiative's audit spine (mirrors task history)."""
    return {"initiative_id": initiative_id, "events": strategy.get_events(initiative_id)}

@app.patch("/api/roadmap/{initiative_id}")
def api_update_initiative(initiative_id: str, body: dict = None):
    """RETIRED — the whole initiative WRITE surface dies together.

    Killing create while leaving edit alive would keep the noun first-class
    (nine live rows an operator can still curate) and would split the API from
    mcp_server's `edit_roadmap` tombstone — exactly the "gone here, alive there"
    drift the parity test exists to catch. Edit the PROJECT instead
    (PATCH /api/projects/{id}); the initiative rows stay readable as archive.
    """
    return _initiatives_gone()


# --- CRM (Phase 6: the top of the spine) ---

@app.get("/api/crm/pipeline")
def api_crm_pipeline():
    """Deals by stage, each carrying its initiative's DERIVED progress."""
    return crm.pipeline()


@app.get("/api/pipeline")
def api_pipeline():
    """Alias for /api/crm/pipeline — deals grouped by stage. Matches the MCP
    `get_pipeline` verb's path so external callers of that name resolve instead
    of 404-ing."""
    return crm.pipeline()


@app.get("/api/crm/stale")
def api_crm_stale(days: int = 7, include_stalled: bool = True):
    """Deals idle >= N days on the touch clock (last_touch_date, not edits)."""
    return {"stale_deals": crm.detect_stale_deals(days, include_stalled=include_stalled)}


@app.post("/api/crm/decay")
def api_crm_decay(days_to_stalled: int = 30, days_to_lost: int = 90):
    """Auto-decay: move idle deals to stalled (30d) or lost (90d)."""
    return crm.auto_stale_decay(days_to_stalled, days_to_lost)


# --- CRM proposals (m27: the propose-only correction inbox) ---

@app.get("/api/crm/proposals")
def api_crm_proposals(status: str = "proposed"):
    """The correction inbox: proposed CRM updates awaiting the human gate.
    status='' lists every proposal regardless of state."""
    return {"proposals": crm_proposals.list_proposals(status=status or None)}


@app.post("/api/crm/proposals")
def api_crm_create_proposal(body: dict):
    """File one proposal (the Thursday session's manual/Gmail path)."""
    res = crm_proposals.create(
        body.get("deal_id", ""), body.get("kind", ""), body.get("payload") or {},
        body.get("evidence_kind", "manual"), body.get("evidence_ref", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/crm/proposals/derive")
def api_crm_derive_proposals():
    """Fireflies-cache sweep → touch proposals. Read-only on deals."""
    return crm_proposals.derive()


@app.post("/api/crm/proposals/{pid}/approve")
def api_crm_approve_proposal(pid: str, body: dict = None):
    """The human gate: apply once through the audited writers."""
    res = crm_proposals.approve(pid, via=(body or {}).get("via", "dashboard"))
    if res.get("status") == "error":
        raise HTTPException(409, res["error"])
    return res


@app.post("/api/crm/proposals/{pid}/dismiss")
def api_crm_dismiss_proposal(pid: str, body: dict = None):
    """Sticky rejection — the proposal never re-appears."""
    res = crm_proposals.dismiss(pid, via=(body or {}).get("via", "dashboard"))
    if res.get("status") == "error":
        raise HTTPException(409, res["error"])
    return res


# --- commercial proposal packets (m31; separate from the correction inbox) ---

@app.get("/api/crm/deals/{deal_id}/commercial-proposals")
def api_list_commercial_proposals(deal_id: str):
    return commercial_proposals.list_for_deal(deal_id)


@app.post("/api/crm/deals/{deal_id}/commercial-proposals")
def api_register_commercial_proposal(deal_id: str, body: dict):
    res = commercial_proposals.register_packet(
        deal_id, body.get("revision"), body.get("workspace_path", ""),
        body.get("manifest_path", ""), body.get("proposal_path", ""),
        body.get("prototype_path"),
        workspace_schema_version=body.get("workspace_schema_version", 1),
        evidence_manifest_path=body.get("evidence_manifest_path"),
        checker_report_path=body.get("checker_report_path"),
        quality_report_path=body.get("quality_report_path"))
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 409, res["error"])
    return res


@app.post("/api/crm/commercial-proposals/{packet_id}/verify")
def api_verify_commercial_proposal(packet_id: str, body: dict):
    res = commercial_proposals.verify_packet(
        packet_id, body.get("package_path", ""), body.get("manifest_sha256", ""),
        body.get("package_sha256", ""), body.get("verification_receipt", ""),
        receipt_sha256=body.get("receipt_sha256"),
        evidence_manifest_sha256=body.get("evidence_manifest_sha256"),
        checker_report_sha256=body.get("checker_report_sha256"),
        quality_report_sha256=body.get("quality_report_sha256"),
        quality_status=body.get("quality_status"))
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 409, res["error"])
    return res


@app.post("/api/crm/commercial-proposals/{packet_id}/send")
def api_record_commercial_proposal_send(packet_id: str, body: dict):
    """Human-only assertion that the exact verified packet left the building.

    Deliberately absent from MCP, alongside win/deliver: agents prepare and
    verify; Ricardo records the external, irreversible act.
    """
    body = body or {}
    res = commercial_proposals.record_send(
        packet_id, body.get("channel", ""), body.get("evidence_ref", ""),
        body.get("idempotency_key", ""), body.get("recipient"), body.get("sent_at"))
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 409, res["error"])
    return res


@app.get("/api/crm/accounts")
def api_crm_accounts():
    return {"accounts": crm.list_accounts()}


@app.post("/api/crm/accounts")
def api_crm_create_account(body: dict):
    res = crm.create_account(body.get("name", ""), body.get("domain", ""), body.get("notes", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/crm/contacts")
def api_crm_contacts(account_id: Optional[str] = None):
    """Verb audit Tier 1: contacts were create-only (no GET existed here either
    — the audit's 'API exists' was wrong on this one)."""
    return {"contacts": crm.list_contacts(account_id)}


@app.post("/api/crm/contacts")
def api_crm_create_contact(body: dict):
    res = crm.create_contact(
        body.get("account_id", ""), body.get("name", ""),
        email=body.get("email", ""), role=body.get("role", ""),
        notes=body.get("notes", ""), phone=body.get("phone", ""),
        whatsapp=body.get("whatsapp", ""), linkedin_url=body.get("linkedin_url", ""),
        source=body.get("source", ""), source_notes=body.get("source_notes", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/crm/accounts/{account_id}")
def api_crm_update_account(account_id: str, body: dict):
    """Inline-edit an account. `domain` is normalized to the bare host the
    Fireflies matcher compares against, and refused if it carries none."""
    res = crm.update_account(
        account_id, name=body.get("name"), domain=body.get("domain"),
        notes=body.get("notes"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/crm/contacts/{contact_id}")
def api_crm_update_contact(contact_id: str, body: dict):
    """Inline-edit a contact — mirrors the deal-edit modal pattern."""
    res = crm.update_contact(
        contact_id,
        name=body.get("name"), email=body.get("email"), role=body.get("role"),
        notes=body.get("notes"), phone=body.get("phone"),
        whatsapp=body.get("whatsapp"), linkedin_url=body.get("linkedin_url"),
        source=body.get("source"), source_notes=body.get("source_notes"),
        account_id=body.get("account_id"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/crm/deals")
def api_crm_list_deals(stage: Optional[str] = None):
    """MCP-parity for list_deals: a flat list of every deal (optionally filtered
    by stage), enriched with account/contact/initiative names — the source for
    the CRM tab's 'All Deals' table (the pipeline kanban groups the same rows by
    stage)."""
    return {"deals": crm.list_deals(stage=stage)}


@app.post("/api/crm/deals")
def api_crm_create_deal(body: dict):
    res = crm.create_deal(
        body.get("account_id", ""), body.get("title", ""),
        stage=body.get("stage", "lead"), value=body.get("value"),
        currency=body.get("currency", "MXN"), contact_id=body.get("contact_id"),
        initiative_id=body.get("initiative_id"), notes=body.get("notes", ""),
        source=body.get("source", ""),
        parent_deal_id=body.get("parent_deal_id"),
        recurrence_type=body.get("recurrence_type"),
        recurrence_interval=body.get("recurrence_interval"),
        product_id=body.get("product_id"),
        expected_close_date=body.get("expected_close_date"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    # Growth attributes (value ladder / loop / lead source) at birth, if given.
    if res.get("deal_id") and any(body.get(k) for k in
                                  ("value_ladder_stage", "growth_loop", "lead_source")):
        g = growth.update_deal_growth(
            res["deal_id"], value_ladder_stage=body.get("value_ladder_stage"),
            growth_loop=body.get("growth_loop"), lead_source=body.get("lead_source"))
        if g.get("status") == "error":
            raise HTTPException(400, g["error"])
    return res


@app.patch("/api/crm/deals/{deal_id}")
def api_crm_update_deal(deal_id: str, body: dict):
    # m17: the promised payment date moves ONLY through the audited verb —
    # a silent generic-PATCH write would erase the repromise trail that keeps
    # the month's standing honest. Loud 400 instead of the silent drop.
    if "expected_payment_date" in body or "expected_payment_date_original" in body:
        raise HTTPException(
            400, "expected_payment_date is audited — use "
                 "POST /api/crm/deals/{deal_id}/payment-promise")
    res = crm.update_deal(deal_id, stage=body.get("stage"), value=body.get("value"),
                          initiative_id=body.get("initiative_id"),
                          notes=body.get("notes"), title=body.get("title"),
                          account_id=body.get("account_id"),
                          clear_initiative=bool(body.get("clear_initiative")),
                          recurrence_type=body.get("recurrence_type"),
                          recurrence_interval=body.get("recurrence_interval"),
                          parent_deal_id=body.get("parent_deal_id"),
                          product_id=body.get("product_id"),
                          clear_product=bool(body.get("clear_product")),
                          expected_close_date=body.get("expected_close_date"),
                          lost_reason=body.get("lost_reason"),
                          lost_notes=body.get("lost_notes"),
                          payment_terms_days=body.get("payment_terms_days"),
                          expected_invoice_date=body.get("expected_invoice_date"),
                          paid_amount=body.get("paid_amount"))
    if res.get("status") == "error":
        raise HTTPException(404 if "not found" in res["error"] else 400, res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/deliver")
def api_crm_deliver_deal(deal_id: str, body: dict = None):
    """Conversion verb 2/3 — "Deliver this": join a WON deal to the project that
    delivers it (deals.project_id + projects.status/account_id + a
    `delivered_link` event).

    Conversion verbs are HUMAN-ONLY by design (spec red line 11): an agent may
    create, comment on, progress and complete tasks unattended, but it only ever
    PROPOSES a conversion into the brief — Ricardo taps. That is why this verb
    lives here, in the dashboard API, and is deliberately absent from
    mcp_server.py; the absence is the guard, so do not add MCP parity for it.

    Body: {project_id} to deliver into an existing project, or
    {new_project_name, repo_path?} to create one. `repo_path` registers an
    existing proposal workspace when available; it is not required and no
    directory is synthesized. Idempotent — a deal that already carries a
    project_id returns `already_delivered` with the existing link."""
    body = body or {}
    res = crm.deliver_deal(deal_id, project_id=body.get("project_id"),
                           new_project_name=body.get("new_project_name"),
                           repo_path=body.get("repo_path"))
    if res.get("status") == "error":
        raise HTTPException(
            404 if res.get("code") in ("not_found", "project_not_found") else 400,
            res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/invoiced")
def api_crm_mark_invoiced(deal_id: str, body: dict = None):
    """💵 Facturado — conversion verb 4/5 (directiva ADICIÓN 8).

    HUMAN-ONLY, exactly like deliver/mark-delivered: an agent may mint the
    "Facturar …" card, but only Ricardo asserts that an invoice was issued. That
    is why this lives here and is deliberately absent from `mcp_server.py` — the
    absence IS the guard, so do not add MCP parity for it.

    m17: optional body `{expected_payment_date}` — the drawer's prefilled
    "cobro esperado" confirmation rides the same tap; absent, the date derives
    from `payment_terms_days` when set. A manual date already on the deal is
    never overwritten.

    Idempotent (`already_deal_invoiced` with the existing stamp)."""
    body = body or {}
    res = crm.mark_deal_invoiced(
        deal_id, expected_payment_date=body.get("expected_payment_date"))
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 400,
                            res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/payment-promise")
def api_crm_payment_promise(deal_id: str, body: dict):
    """📅 Cobro esperado — the ONE audited write path for the payment plan.

    HUMAN-ONLY like every money verb, and deliberately absent from
    `mcp_server.py` — the absence IS the guard, do not add MCP parity. The
    date is a plan, so unlike 💵/✅ it is editable — but every movement logs
    an event (`payment_promised` / `payment_repromised {from,to,reason}`),
    a change requires a reason, and the plan freezes once the deal is paid
    (`already_paid`): plan vs. `paid_at` is the reconciliation record.

    Body: `{expected_payment_date: 'YYYY-MM-DD', reason?}` — reason is
    mandatory when moving an existing date."""
    res = crm.set_payment_promise(
        deal_id, body.get("expected_payment_date"), reason=body.get("reason"))
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 400,
                            res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/paid")
def api_crm_mark_paid(deal_id: str, body: dict = None):
    """✅ Pagado — conversion verb 5/5 (directiva ADICIÓN 8).

    Human-only for the same reason as the four before it. Requires
    `invoiced_at` (typed `not_invoiced` → 400): a payment against an invoice
    that was never issued is a missing tap, not a state.

    m19: optional body `{paid_amount}` — the deposit that actually landed,
    prefilled with `value` in the drawer; NULL keeps meaning "= value".

    Side effect: the deal's open cobranza/facturación cards are cancelled in the
    same call, so the board is right the instant the drawer closes rather than
    at the next reconcile."""
    body = body or {}
    res = crm.mark_deal_paid(deal_id, paid_amount=body.get("paid_amount"))
    if res.get("status") == "error":
        raise HTTPException(404 if res.get("code") == "not_found" else 400,
                            res["error"])
    return res


@app.get("/api/crm/cash-flow")
def api_crm_cash_flow(date: Optional[str] = None):
    """💰 The Today Cobro read: week / month standing / overdue / leaks /
    narrative, from `crm.cash_flow` — deterministic SQL + templates, zero LLM.

    `?date=YYYY-MM-DD` freezes the clock (contract injection, the
    `api_cadence_reconcile` pattern). READ-ONLY: the block it feeds never
    mutates money — the verbs live in the drawer."""
    res = crm.cash_flow(date=date)
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/cadence/reconcile")
def api_cadence_reconcile(body: dict = None):
    """Run ONE deterministic materializer pass (journey fase 1, step 5).

    Closes cadence cards whose reason to exist is gone and mints the ONE next
    card per deal — the next nurture touch, a 🚚 for a won orphan, or ADICIÓN 8's
    facturación / cobranza. No LLM anywhere in the path (red line 4); everything
    is SQL and arithmetic under `BEGIN IMMEDIATE`.

    Safe to call repeatedly: a second pass in the same minute closes nothing new
    and mints nothing new (every deal's one slot is occupied, and the storage
    engine refuses a second open cadence card regardless). `{"date": "YYYY-MM-DD"}`
    overrides the day, which is what the contract drives it with."""
    body = body or {}
    res = cadence.reconcile(date=body.get("date"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/crm/deals/orphan-won")
def api_crm_orphan_won_deals():
    """Won deals with no delivering project — LIVE, not the stored brief's copy.

    Today's ⚠️ block used to render this list out of `/api/brief/latest`, which
    freezes at composition time: a deal delivered at 10:00 stayed on the screen
    until the 13:30 slot recomposed, and a brief composed against a schema that
    predated `deals.project_id` showed an empty block all day. Same SQL as the
    composer (`brief.orphan_won_deals`), so the two channels cannot disagree —
    only the freshness differs, which is the point."""
    deals = brief.orphan_won_deals_now()
    return {"deals": deals, "count": len(deals)}


@app.get("/api/crm/loss-reasons")
def api_crm_loss_reasons(days: Optional[int] = None):
    """Win/loss breakdown of lost deals by category (count + value)."""
    return crm.loss_reasons(days)


@app.get("/api/crm/deals/{deal_id}/children")
def api_crm_deal_children(deal_id: str):
    """List all sub-deals (children) of a parent deal."""
    return {"children": crm.list_deal_children(deal_id)}


@app.post("/api/crm/deals/{deal_id}/children")
def api_crm_create_child(deal_id: str, body: dict):
    """Create a sub-deal under a parent deal. Inherits account_id from parent."""
    parent = crm.get_deal(deal_id)
    if not parent:
        raise HTTPException(404, "parent deal not found")
    res = crm.create_deal(
        parent["account_id"], body.get("title", ""),
        stage=body.get("stage", "lead"), value=body.get("value"),
        currency=body.get("currency", parent.get("currency", "MXN")),
        notes=body.get("notes", ""),
        parent_deal_id=deal_id,
        recurrence_type=body.get("recurrence_type"),
        recurrence_interval=body.get("recurrence_interval"),
        product_id=body.get("product_id"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/events")
def api_add_deal_event(deal_id: str, body: dict):
    """Log a free-form commercial interaction (call/meeting/email/note)."""
    res = crm.add_deal_event(deal_id, body.get("kind", ""), note=body.get("note", ""),
                             agent=body.get("agent"))
    if res.get("status") == "error":
        raise HTTPException(404 if "not found" in res["error"] else 400, res["error"])
    return res


@app.get("/api/crm/accounts/{account_id}/chain")
def api_account_chain(account_id: str):
    """The lateral view: account → contacts → deals (+ value totals)."""
    res = crm.account_chain(account_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/crm/deals/{deal_id}/drilldown")
def api_crm_drilldown(deal_id: str):
    """THE full spine: Deal→Initiative→Epics→Tasks→Runs→Commits."""
    res = crm.deal_drilldown(deal_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/score")
def api_crm_score(deal_id: str, body: dict):
    """Trigger lead-scoring recomputation for a single deal."""
    res = growth.score_deal(deal_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/crm/deals/{deal_id}/fireflies")
def api_crm_deal_fireflies(deal_id: str):
    """Cached Fireflies meeting signals for a deal."""
    return {"deal_id": deal_id, "meetings": crm.get_deal_fireflies(deal_id)}

@app.post("/api/crm/deals/{deal_id}/fireflies/fetch")
def api_crm_deal_fireflies_fetch(deal_id: str, limit: int = 25, since: str = None):
    """Fetch fresh Fireflies meeting signals for a deal and store them.
    `limit` widens the transcript window (default 25 ≈ the last ~12 days at
    Ricardo's meeting rate); `since` (YYYY-MM-DD) floors it. The response
    reports `scanned` so 0-matched-of-40 reads differently from 0-of-0."""
    from . import fireflies as _ff
    return _ff.fetch_and_store_for_deal(deal_id, limit=limit, since=since)


@app.get("/api/growth/lead-scores")
def api_growth_lead_scores():
    """All non-closed deals with their lead scores and category breakdowns."""
    deals = crm.list_deals()
    active = [d for d in deals if d.get("stage") not in crm._CLOSED and d.get("stage") not in crm._INACTIVE]
    return {
        "deals": [
            {
                "id": d["id"],
                "title": d["title"],
                "stage": d["stage"],
                "lead_score": d.get("lead_score"),
                "lead_score_details": d.get("lead_score_details"),
                "client_profile": d.get("client_profile"),
                "account_name": d.get("account_name"),
                "value": d.get("value"),
                "currency": d.get("currency"),
            }
            for d in active
        ]
    }


@app.post("/api/growth/score-all-leads")
def api_growth_score_all_leads():
    """Bulk re-score all active leads. Useful when ICP config changes and
    existing deals need their scores refreshed."""
    return growth.score_all_leads()


# --- CRM Growth System (value ladder · loops · scoring · touches · scorecard) ---

@app.patch("/api/crm/deals/{deal_id}/growth")
def api_crm_update_growth(deal_id: str, body: dict):
    """Set a deal's growth attributes (value_ladder_stage / growth_loop /
    lead_source / next_touch_date / product_id) — validated enums. Passing
    product_id auto-derives value_ladder_stage from the product's rung unless
    value_ladder_stage is also given."""
    res = growth.update_deal_growth(
        deal_id,
        value_ladder_stage=body.get("value_ladder_stage"),
        growth_loop=body.get("growth_loop"),
        lead_source=body.get("lead_source"),
        next_touch_date=body.get("next_touch_date"),
        expected_close_date=body.get("expected_close_date"),
        product_id=body.get("product_id"))
    if res.get("status") == "error":
        raise HTTPException(404 if "not found" in res["error"] else 400, res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/touch")
def api_crm_touch(deal_id: str, body: dict = None):
    """Cap. 4 touch tracking: +1 touch, stamp today, suggest next_touch (+7d)."""
    body = body or {}
    res = growth.record_touch(deal_id, note=body.get("note", ""),
                              kind=body.get("kind", "touch"),
                              next_in_days=int(body.get("next_in_days", 7)))
    if res.get("status") == "error":
        raise HTTPException(404 if "not found" in res["error"] else 400, res["error"])
    return res


@app.post("/api/crm/deals/{deal_id}/score")
def api_crm_score(deal_id: str, body: dict):
    """Upsert lead-scoring features → recompute deals.lead_score (0–100).
    Pass ?recompute=true (or any truthy body['recompute']) to trigger a
    full score_deal() using persisted features + latest Fireflies signals."""
    if body.get("recompute"):
        res = growth.score_deal(deal_id)
    else:
        res = growth.set_lead_features(
            deal_id, account_type=body.get("account_type", ""),
            source=body.get("source", ""), product_interest=body.get("product_interest", ""),
            engagement_score=int(body.get("engagement_score", 0) or 0),
            industry=body.get("industry", ""))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/crm/leads")
def api_crm_quick_add_lead(body: dict):
    """Phase 2 lead capture: one minimal form → account + contact + deal
    (value-ladder 'iman' / stage 'lead'), scored."""
    res = growth.quick_add_lead(
        name=body.get("name", ""), company=body.get("company", ""),
        source=body.get("source", ""), loop=body.get("loop", ""),
        notes=body.get("notes", ""),
        engagement_score=int(body.get("engagement_score", 0) or 0),
        industry=body.get("industry", ""), value=body.get("value"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.post("/api/crm/quick-add")
def api_crm_quick_add_contact(body: dict):
    """Growth Operating Framework — fastest capture: name + company → account +
    contact + lead-stage deal. Optional email/phone/whatsapp/linkedin/source."""
    res = crm.quick_add_contact(
        name=body.get("name", ""), company=body.get("company", ""),
        email=body.get("email", ""), phone=body.get("phone", ""),
        whatsapp=body.get("whatsapp", ""), linkedin_url=body.get("linkedin_url", ""),
        source=body.get("source", ""), notes=body.get("notes", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    # Apply loop + lead_source growth fields if provided.
    if res.get("deal_id") and (body.get("loop") or body.get("lead_source")):
        g = growth.update_deal_growth(
            res["deal_id"], growth_loop=body.get("loop"),
            lead_source=body.get("lead_source"))
        if g.get("status") == "error":
            raise HTTPException(400, g["error"])
    return res


@app.get("/api/readiness")
def api_readiness():
    """Readiness dashboard view: all active deals with fresh multi-dimensional
    readiness scores (buyer/product/market), bucketed nurture → qualified →
    sales_ready → hot, each with its next best action on the sprint ladder."""
    return readiness.readiness_overview()


@app.post("/api/crm/deals/{deal_id}/readiness")
def api_crm_readiness(deal_id: str, body: dict = None):
    """Compute + persist a deal's readiness score and dimension breakdown.
    Pass {"from_fireflies": true} to require stored Fireflies meetings and
    return the extracted meeting signals alongside the score."""
    body = body or {}
    if body.get("from_fireflies"):
        res = readiness.score_readiness_from_fireflies(deal_id)
    else:
        res = readiness.score_readiness(deal_id)
    if res.get("status") == "error":
        raise HTTPException(404 if "not found" in res["error"] else 400, res["error"])
    return res


@app.get("/api/pipeline-math")
def api_pipeline_math(revenue_goal: Optional[float] = None,
                            avg_ticket: Optional[float] = None):
    """Cap. 4 backward funnel: goal → clients → proposals → discovery → leads →
    touches, plus current pipeline coverage."""
    return growth.pipeline_math(revenue_goal=revenue_goal, avg_ticket=avg_ticket)


@app.get("/api/scorecard")
def api_scorecard(week: Optional[str] = None):
    """Cap. 6 weekly 5 (leads · touches · discovery · content · proposals),
    auto-derived from the week's events."""
    return growth.scorecard(week=week)


@app.get("/api/growth/loops")
def api_growth_loops():
    """Cap. 3: the three flywheels + per-loop leads / conversion / ratio."""
    return growth.growth_loops()


@app.get("/api/growth/content")
def api_growth_content(weeks: int = 8):
    """Content cadence (pieces per week + publishing streak) plus the raw
    `pieces` list for the content calendar."""
    return growth.content_cadence(weeks=weeks)


@app.post("/api/growth/content")
def api_growth_add_content(body: dict):
    """Create a content piece (content_pipeline calendar)."""
    res = growth.create_content_piece(
        title=body.get("title", ""), topic=body.get("topic", ""),
        channel=body.get("channel", ""),
        growth_loop=body.get("growth_loop", body.get("loop", "")),
        hook=body.get("hook", ""),
        publish_date=body.get("publish_date", body.get("published_at", "")),
        status=body.get("status", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/growth/content/{content_id}")
def api_growth_update_content(content_id: str, body: dict):
    res = growth.update_content_piece(content_id, body or {})
    if res.get("status") == "error":
        raise HTTPException(404 if res["error"] == "content piece not found" else 400,
                            res["error"])
    return res


@app.delete("/api/growth/content/{content_id}")
def api_growth_delete_content(content_id: str):
    res = growth.delete_content_piece(content_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- Speaking pipeline (Growth → talks as attraction-loop generators) ---
@app.get("/api/growth/speaking")
def api_growth_speaking():
    return growth.list_speaking()


@app.post("/api/growth/speaking")
def api_growth_create_speaking(body: dict):
    res = growth.create_speaking(
        title=body.get("title", ""), event_name=body.get("event_name", ""),
        event_date=body.get("event_date", ""), status=body.get("status", ""),
        attraction_loop_status=body.get("attraction_loop_status", ""),
        deal_id=body.get("deal_id", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/growth/speaking/{speaking_id}")
def api_growth_update_speaking(speaking_id: str, body: dict):
    res = growth.update_speaking(speaking_id, body or {})
    if res.get("status") == "error":
        raise HTTPException(404 if res["error"] == "speaking event not found" else 400,
                            res["error"])
    return res


@app.delete("/api/growth/speaking/{speaking_id}")
def api_growth_delete_speaking(speaking_id: str):
    res = growth.delete_speaking(speaking_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- Time-block calendar (Today → weekly role-block schedule) ---
@app.get("/api/growth/time-blocks")
def api_growth_time_blocks():
    """The weekly schedule of role-specialized blocks (seeds the 5 defaults on
    first call). Each block carries a derived `done` flag for the current week."""
    return growth.list_time_blocks()


@app.get("/api/growth/time-blocks/{day_of_week}/activities")
def api_growth_time_block_activities(day_of_week: int):
    """Tasks planned for the next occurrence of this weekday (0=Monday)."""
    if not 0 <= day_of_week <= 6:
        raise HTTPException(400, "day_of_week must be 0–6 (0=Mon)")
    try:
        return growth.time_block_activities(day_of_week)
    except Exception:
        return {"date": "", "day_of_week": day_of_week, "activities": []}


@app.post("/api/growth/time-blocks")
def api_growth_create_time_block(body: dict):
    res = growth.create_time_block(
        day_of_week=body.get("day_of_week"),
        start_time=body.get("start_time", ""), end_time=body.get("end_time", ""),
        role=body.get("role", ""), label=body.get("label", ""),
        active=body.get("active", True))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/growth/time-blocks/{block_id}")
def api_growth_update_time_block(block_id: str, body: dict):
    res = growth.update_time_block(block_id, body or {})
    if res.get("status") == "error":
        raise HTTPException(404 if res["error"] == "time block not found" else 400,
                            res["error"])
    return res


@app.delete("/api/growth/time-blocks/{block_id}")
def api_growth_delete_time_block(block_id: str):
    res = growth.delete_time_block(block_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- Conversion funnel over time (Today → weekly snapshots) ---
@app.get("/api/growth/funnel-trend")
def api_growth_funnel_trend(weeks: int = 12):
    """Last N weeks of lead→discovery→proposal→won conversion snapshots (seeds
    the first snapshot from current deals if none exist yet)."""
    return growth.funnel_trend(weeks=weeks)


@app.post("/api/growth/funnel-snapshot")
def api_growth_funnel_snapshot(body: Optional[dict] = None):
    """Manually capture a snapshot for the current (or given) week. Normally the
    Monday-9am timer does this; exposed for on-demand refresh."""
    week = (body or {}).get("week_start")
    return growth.snapshot_funnel(week_start=week)


@app.get("/api/growth/pipeline-temporal")
def api_growth_pipeline_temporal(months: int = 12):
    """Month-by-month pipeline flow: new deals, stage movements, won/lost counts
    and revenue closed. The temporal view — answers 'what happened each month?'"""
    return growth.pipeline_temporal(months=months)


@app.get("/api/growth/fireflies-analytics")
async def api_fireflies_analytics(limit: int = 10):
    """Behavioral coaching (playbook Cap. 6): talk-listen ratio over the last N
    meetings (default 10) from Fireflies, plus the coaching summary (avg over the
    last 5 non-solo meetings, gap to the 45% target, trend). Fail-soft — returns
    available:false rather than 500 when Fireflies isn't configured."""
    from . import fireflies
    return await asyncio.to_thread(fireflies.analytics, limit=limit)


@app.get("/api/growth/behavioral-coaching")
async def api_behavioral_coaching(limit: int = 10):
    """Behavioral coaching (playbook Cap. 6): the last N meetings (default 10)
    with talk%, filler words and longest monologue — each vs its target (talk
    ≤45%, fillers trending down, monologues <60s) with a trend + sparkline
    series, plus a rotating coaching tip aimed at the most off-target metric.
    Fail-soft — returns available:false rather than 500 without Fireflies."""
    from . import fireflies
    return await asyncio.to_thread(fireflies.coaching, limit=limit)


@app.get("/api/growth/icp")
def api_growth_icp():
    """The effective ICP config (DB → env fallback): industries · positioning ·
    target revenue · avg ticket · close rate. Drives lead scoring + pipeline math."""
    return growth.icp_config()


@app.patch("/api/growth/icp")
def api_growth_update_icp(body: dict):
    """Update ICP config from the Strategy → ICP Editor. Persists to icp_config."""
    res = growth.set_icp(body)
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


# --- Product catalog (Strategy → productized offers) ---
@app.get("/api/growth/products")
def api_growth_products():
    """All products (seeds the 3 default offers on first call of an empty table)."""
    return growth.list_products()


@app.post("/api/growth/products")
def api_growth_create_product(body: dict):
    res = growth.create_product(
        name=body.get("name", ""), description=body.get("description", ""),
        value_ladder_stage=body.get("value_ladder_stage", ""),
        fixed_price_mxn=body.get("fixed_price_mxn"),
        ficha_html=body.get("ficha_html", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/growth/products/{product_id}")
def api_growth_update_product(product_id: str, body: dict):
    res = growth.update_product(product_id, body)
    if res.get("status") == "error":
        raise HTTPException(404 if res["error"] == "product not found" else 400,
                            res["error"])
    return res


@app.delete("/api/growth/products/{product_id}")
def api_growth_delete_product(product_id: str):
    res = growth.delete_product(product_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- 90-Day Plan tracker (Strategy → playbook execution) ---
@app.get("/api/growth/plan-milestones")
def api_growth_plan_milestones():
    """All plan milestones grouped by phase + per-phase/overall progress
    (seeds the playbook's 3-phase plan on first call of an empty table)."""
    return growth.list_plan_milestones()


@app.patch("/api/growth/plan-milestones/{milestone_id}")
def api_growth_toggle_milestone(milestone_id: str, body: Optional[dict] = None):
    """Toggle a milestone's completed flag (or set it explicitly with
    {"completed": bool})."""
    completed = None
    if body and "completed" in body:
        completed = bool(body["completed"])
    res = growth.set_milestone_completed(milestone_id, completed)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/growth/pipeline-health")
def api_growth_pipeline_health():
    """Touch-cadence alert triage over active deals: red (overdue next touch) ·
    yellow (gone cold 7+ days) · blue (no next touch scheduled)."""
    return growth.pipeline_health()


@app.get("/api/growth/forecast")
def api_growth_forecast():
    """30/60/90-day revenue forecast from expected_close_date (explicit or auto-estimated from stage)."""
    return growth.forecast()


# --- CLTV:CAC unit economics (Growth → per lead source) ---
@app.get("/api/growth/cltv-cac")
def api_growth_cltv_cac():
    """CLTV (avg_ticket × repeat × lifespan) + CAC and CLTV:CAC ratio per lead
    source, with a green/yellow/red rating."""
    return growth.cltv_cac()


@app.get("/api/growth/acquisition-costs")
def api_growth_acquisition_costs():
    return growth.list_acquisition_costs()


@app.post("/api/growth/acquisition-costs")
def api_growth_add_acquisition_cost(body: dict):
    res = growth.add_acquisition_cost(
        source=body.get("source", ""), cost_mxn=body.get("cost_mxn"),
        month=body.get("month", ""), notes=body.get("notes", ""))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.delete("/api/growth/acquisition-costs/{cost_id}")
def api_growth_delete_acquisition_cost(cost_id: str):
    res = growth.delete_acquisition_cost(cost_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- Nurture sequences (per-deal Hook cadence) ---
@app.get("/api/growth/nurture/{deal_id}")
def api_growth_get_nurture(deal_id: str):
    """A deal's 5-touch nurture sequence + the next suggested touch date."""
    return growth.get_nurture(deal_id)


@app.get("/api/growth/cadence/{deal_id}")
def api_growth_cadence_status(deal_id: str):
    """Per-deal nurture cadence: steps, next due date, and compliance %."""
    res = crm.get_cadence_status(deal_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/growth/nurture/{deal_id}/generate")
def api_growth_generate_nurture(deal_id: str):
    """(Re)generate a 5-touch Hook sequence from the deal's name/source/stage."""
    res = growth.generate_nurture(deal_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/growth/monthly-view")
def api_growth_monthly_view(month: Optional[str] = None):
    """C9 — 8-block strategic monthly view (pipeline math, revenue mix, loops,
    channels, scorecard rollup, score accuracy, milestones)."""
    return growth.monthly_strategic_view(month)


@app.get("/api/growth/conversion-path/{deal_id}")
def api_growth_conversion_path(deal_id: str):
    """Where this deal sits on the value ladder and the measured probability of
    moving to the next rung."""
    res = growth.conversion_path_for_deal(deal_id)
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


@app.patch("/api/growth/nurture/{step_id}")
def api_growth_update_nurture(step_id: str, body: dict):
    """Update a nurture step's status (pending/sent/skipped)."""
    res = growth.set_nurture_status(step_id, (body or {}).get("status", ""))
    if res.get("status") == "error":
        raise HTTPException(404 if res["error"] == "step not found" else 400,
                            res["error"])
    return res


# =====================================================================
# Parallel orchestration (role → spec → work → ledger → guardrails)
# =====================================================================
# The role→spec→work→result→guardrail loop. All additive; see orchestration.py.

# --- (1) Sessions by role ---
@app.get("/api/session-meta")
def api_session_meta():
    """Every registered session's role/feature/policy (incl. tag) + role
    counts + a distinct-tag summary for filter UIs."""
    metas = orch.all_session_meta()
    tags: dict = {}
    for m in metas.values():
        t = m.get("tag")
        if t:
            tags[t] = tags.get(t, 0) + 1
    return {"sessions": metas, "role_summary": orch.role_summary(),
            "roles": list(orch.ROLES), "tags": tags}


@app.post("/api/session-meta")
def api_set_session_meta(body: dict):
    """Register/update a session's role, feature, and auto policies."""
    key = body.get("session_key")
    if not key:
        raise HTTPException(400, "session_key required")
    res = orch.set_session_role(
        key, role=body.get("role"), feature=body.get("feature"),
        project=body.get("project"), host=body.get("host", "local"),
        auto_compact=body.get("auto_compact"), auto_abort=body.get("auto_abort"),
        notes=body.get("notes"), tag=body.get("tag"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


# --- (2) Task ledger ---
@app.get("/api/ledger")
def api_ledger(limit: int = 50, task_id: Optional[str] = None,
                     session_key: Optional[str] = None):
    return {"ledger": orch.get_ledger(limit=limit, task_id=task_id, session_key=session_key)}


@app.get("/api/tasks/{task_id}/ledger")
def api_task_ledger(task_id: str):
    return {"task_id": task_id, "ledger": orch.get_ledger(limit=50, task_id=task_id)}


@app.post("/api/tasks/{task_id}/ledger")
def api_report_ledger(task_id: str, body: dict):
    """Agent reports a structured RESULT → ledger + task routing (§7 / auto-abort)."""
    return _or_http(orch.report_ledger(
        task_id, body.get("summary", ""), files_modified=body.get("files_modified"),
        risks=body.get("risks"), status=body.get("status", "passed"),
        agent=body.get("agent"), session_key=body.get("session_key"),
        role=body.get("role"), route=body.get("route", True)))


# --- (3) Session events / hooks notification ---
@app.post("/api/session-events")
def api_session_event(body: dict):
    """Hook target: a Claude Code session pushes a lifecycle event up.
    `Notification` → input_needed; `Stop` clears open asks for that session."""
    key = body.get("session_key") or body.get("session_id")
    if not key:
        raise HTTPException(400, "session_key/session_id required")
    kind = body.get("kind", "note")
    if kind == "stop":
        orch.resolve_inputs_for(key)
    res = orch.record_event(key, kind, body.get("payload") or
                            {k: v for k, v in body.items()
                             if k not in ("session_key", "session_id", "kind", "host")},
                            host=body.get("host"))
    return res


@app.get("/api/session-events")
def api_get_session_events(limit: int = 50, session_key: Optional[str] = None,
                                 unresolved_only: bool = False):
    return {"events": orch.get_events(limit=limit, session_key=session_key,
                                      unresolved_only=unresolved_only),
            "pending_input": orch.pending_input()}


@app.post("/api/session-events/{event_id}/resolve")
def api_resolve_event(event_id: int):
    return orch.resolve_event(event_id)


# --- (4) Auto-compact ---
@app.get("/api/compact-candidates")
def api_compact_candidates():
    """Live sessions whose context is large enough to warrant /compact."""
    return {"candidates": orch.compact_candidates(sessions.get_all_sessions())}


@app.post("/api/sessions/{host}/{session_name}/compact")
def api_compact(host: str, session_name: str):
    """Send /compact to a session (operator button)."""
    return _or_http(orch.compact_session(host, session_name, auto=False))


# --- (5) Shared spec ---
@app.get("/api/specs")
def api_specs():
    return {"specs": orch.list_specs()}


@app.get("/api/specs/{feature}")
def api_spec(feature: str, role: Optional[str] = None):
    """Full spec, or (with ?role=) only that role's slice."""
    if role:
        res = orch.spec_slice(feature, role)
        if res.get("status") == "error":
            raise HTTPException(404, res["error"])
        return res
    content = orch.read_spec(feature)
    if content is None:
        raise HTTPException(404, f"no spec for '{feature}'")
    return {"feature": feature, "content": content}


@app.put("/api/specs/{feature}")
def api_write_spec(feature: str, body: dict):
    """Operator writes the source-of-truth spec for a feature."""
    return orch.write_spec(feature, body.get("content", ""))


# --- Phase 4: runnable acceptance ---
@app.post("/api/tasks/{task_id}/run-contract")
def api_run_contract(task_id: str, body: dict = None):
    """VALIDATE: run the task's contract; passed = exit code. Writes the
    authoritative verification ledger row."""
    body = body or {}
    res = governance.run_contract(task_id, agent=body.get("agent"),
                                  session_key=body.get("session_key"))
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/tasks/{task_id}/contract")
def api_set_contract(task_id: str, body: dict):
    """Operator authors the runnable acceptance contract (Tier-0 spec gate)."""
    res = governance.set_contract(task_id, body.get("contract_cmd"))
    if res.get("status") == "error":
        raise HTTPException(404, res["error"])
    return res


# --- (6) Auto-abort (manual trigger; the sweeper does it autonomously) ---
@app.post("/api/tasks/{task_id}/abort")
def api_abort(task_id: str, body: dict = None):
    body = body or {}
    return _or_http(orch.abort_task(task_id, reason=body.get("reason", "manual abort"),
                          agent=body.get("agent"), kill=body.get("kill", True)))


@app.post("/api/tasks/{task_id}/fail")
def api_fail(task_id: str, body: dict = None):
    """Record a failed attempt (increments strikes; auto-aborts on the 3rd)."""
    body = body or {}
    return _or_http(orch.record_failed_attempt(task_id, error=body.get("error", ""),
                                     agent=body.get("agent"),
                                     session_key=body.get("session_key")))


# --- Sweeper (features 4 + 6 run autonomously) ---
@app.post("/api/orchestration/sweep")
def api_sweep():
    """Run one sweeper pass on demand (auto-compact + auto-abort)."""
    return orch.sweep()

# --- Orchestration page — DELETED (spec §4 "Delete — relocate first", #5).
# `/orchestration` + orchestration.html are gone. Its ONE unique capability, the
# pending-input attention queue, was relocated into Today first — and survived
# that block's OWN deletion for the same reason (journey fase 1, step 5): it now
# lives in `#today-input-zone` (💬 Esperan respuesta). Relocate, then delete, at
# every level. The sweeper endpoint above and every
# `/api/orchestration/*` verb stay — they were never the page.


# --- Personal Health API ---

@app.get("/api/health/today")
def health_today():
    """Today's health canvas: routines grouped by time_block with done status."""
    return _health_mod.get_today()


@app.get("/api/health/routines")
def health_routines():
    """All active health routines (ordered)."""
    return {"routines": _health_mod.get_routines()}


@app.post("/api/health/routines/{routine_id}/check")
def health_check(routine_id: int, note: str = ""):
    """Mark a routine done for today (idempotent)."""
    try:
        return _health_mod.check_routine(routine_id, note or None)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/health/routines/{routine_id}/uncheck")
def health_uncheck(routine_id: int):
    """Uncheck a routine for today."""
    try:
        return _health_mod.uncheck_routine(routine_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/health/config")
def health_config_get():
    return _health_mod.get_config()


@app.patch("/api/health/config")
def health_config_update(key: str, value: str):
    try:
        return _health_mod.update_config(key, value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health/plate")
def health_plate():
    """The Balanced plate reference data (from mi_plato_balanced artifact)."""
    return _health_mod.get_plate_data()


# --- Personal OKRs (sensitive check-ins) -------------------------------------

@app.get("/api/personal/okrs")
def personal_okrs_get(year: int = 2026, history_limit: int = 5):
    try:
        return _okrs_mod.get_okrs(year, history_limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/personal/okrs/check-ins")
def personal_okrs_checkin(body: dict):
    try:
        return _okrs_mod.save_checkin(body or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Cognitive-load API (cogload) ---
# Reads the behavioural store at ~/.local/share/cogload, which lives OUTSIDE the
# repo on purpose. All GETs here are bearer-gated via _SENSITIVE_GET_PREFIXES:
# this is personal health state, not dashboard furniture.

def _cogload_mod():
    # Function-local import: the module shells out to `cogload` and reads a
    # store that may not exist on a machine where the collector was never armed.
    # Importing at module scope would couple dashboard start-up to that.
    from dashboard import cogload as _c
    return _c


@app.get("/api/personal/cogload")
def personal_cogload_get(days: int = 35, weeks: int = 8, months: int = 6):
    """Daily rows + weekly/monthly aggregates + readiness + live capture health.

    Never raises on an empty store: a dashboard that 500s when the collector is
    off is strictly worse than one that says "sin datos" — and an error page is
    indistinguishable from a calm day, which is the failure this whole surface
    exists to prevent.
    """
    c = _cogload_mod()
    from datetime import date as _date, timedelta as _td
    try:
        all_days = c.load_digest_days() or []
        labels = c.load_labels() or []
    except Exception as e:  # a broken store must degrade loudly, not 500
        return {"status": {"available": False, "reason": f"store-unreadable: {e}"},
                "days": [], "weeks": [], "months": [], "readiness": {},
                "fleet": {"available": False,
                          "reason": f"store-unreadable: {e}"}}

    # Aggregates must see EVERY device, not just this box. The healthy Mac was
    # invisible to weekly/monthly/readiness and to the Sunday Telegram brief
    # because they all read the local store only, so a fully-captured day on
    # the laptop still reported "0 días válidos". _aggregate_period dedupes by
    # (date, host), so a re-digest supersedes and never doubles.
    try:
        _fleet_rows = c.load_fleet_days() or []
    except Exception:
        _fleet_rows = []
    corpus = list(all_days) + list(_fleet_rows)

    all_days.sort(key=lambda r: r.get("day") or "")
    recent = all_days[-max(1, days):]
    enriched = []
    for row in recent:
        try:
            enriched.append({**row, **c.capture_health(row)})
        except Exception as e:
            enriched.append({**row, "valid": False, "reason": f"health-error: {e}"})

    today = _date.today()
    week_ids, seen = [], set()
    for i in range(weeks):
        wid = c._iso_week(today - _td(days=7 * i))
        if wid not in seen:
            seen.add(wid)
            week_ids.append(wid)
    month_ids, seen_m = [], set()
    d = today
    for _ in range(months):
        mid = c._ym(d)
        if mid not in seen_m:
            seen_m.add(mid)
            month_ids.append(mid)
        d = d.replace(day=1) - _td(days=1)

    def _safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            return {"sufficient": False, "reason": f"error: {e}"}

    try:
        fleet_days = c.load_fleet_days() or []
        merged = c.person_merge(fleet_days) or {}
        person_days = sorted(
            merged.values(), key=lambda row: row.get("day") or ""
        )[-max(1, days):]
        fleet = {"devices": c.fleet_devices(), "person_days": person_days}
    except Exception as e:
        fleet = {"available": False, "reason": f"fleet-unreadable: {e}"}

    return {
        "status": _safe(c.live_status),
        "days": enriched,
        "weeks": [_safe(c.weekly, corpus, w, labels) for w in reversed(week_ids)],
        "months": [_safe(c.monthly, corpus, m, labels) for m in reversed(month_ids)],
        "readiness": _safe(c.readiness, corpus, labels),
        "fleet": fleet,
    }


@app.get("/api/personal/cogload/weekly")
def personal_cogload_weekly(format: str = "md"):
    """The Sunday brief text. Aggregates only — the module guarantees no label
    note text reaches this, and it is forwarded verbatim to Telegram."""
    c = _cogload_mod()
    from datetime import date as _date
    try:
        # Same fleet corpus as the tab: the brief must not tell a different
        # story from the screen it summarises.
        days = (c.load_digest_days() or []) + (c.load_fleet_days() or [])
        wk = c.weekly(days, c._iso_week(_date.today()), c.load_labels() or [])
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"cogload store unreadable: {e}")
    if format == "md":
        return PlainTextResponse(c.weekly_markdown(wk))
    return wk


@app.post("/api/personal/cogload/label")
def personal_cogload_label(body: dict):
    """Record a ground-truth label.

    The write goes THROUGH `cogload mark`, never by appending to labels.jsonl
    here: the tool owns the store's format, and a second writer would fork it.
    """
    body = body or {}

    def _score(name, required):
        v = body.get(name)
        if v is None:
            if required:
                raise HTTPException(status_code=400, detail=f"{name} is required")
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{name} must be an integer")
        if not 1 <= v <= 5:
            raise HTTPException(status_code=400, detail=f"{name} must be 1..5")
        return v

    # A day has TWO measurement moments, and the morning one carries no stress
    # obligation: requiring `stress` here made the morning check-in (ansiedad +
    # estres, no efectividad) unrepresentable from the dashboard, so the two
    # writers disagreed about what a reading even is. At least one measure is
    # required; which ones is up to the moment.
    slot = body.get("slot")
    if slot is not None and slot not in ("morning", "evening"):
        raise HTTPException(status_code=400, detail="slot must be 'morning' or 'evening'")
    measures = {name: _score(name, False)
                for name in ("anx", "anx_day", "stress", "eff", "tol")}
    given = {k: v for k, v in measures.items() if v is not None}
    if not given:
        raise HTTPException(status_code=400, detail="at least one measure is required")
    if "anx_day" in given and slot == "morning":
        raise HTTPException(status_code=400,
                            detail="anx_day is a whole-day recall; evening slot only")
    src = str(body.get("src") or "dashboard")[:32]
    note = str(body.get("note") or "")[:280]

    cmd = ["cogload", "mark", "--src", src]
    if slot:
        cmd += ["--slot", slot]
    for name, flag in (("anx", "--anx"), ("anx_day", "--anx-day"),
                       ("stress", "--stress"), ("eff", "--eff"), ("tol", "--tol")):
        if given.get(name) is not None:
            cmd += [flag, str(given[name])]
    if note:
        cmd += ["--note", note]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(status_code=503, detail=f"cogload unavailable: {e}")
    if r.returncode != 0:
        raise HTTPException(status_code=502,
                            detail=(r.stderr or r.stdout or "cogload mark failed").strip()[:300])
    return {"ok": True, "slot": slot, **given, "src": src}


# --- Daily Reflection API (Reflection–Action Loop — BRIEF-daily-reflection v2) ---
# Fixed paths only (no /{date} route), so there is no route-order footgun.

@app.get("/api/reflection")
def reflection_get(date: str = None):
    """One day's reflection (default today) — BRIEF v2 §6."""
    try:
        return _reflection_mod.get_reflection(date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Weekly 5-questions log + Friday brief (motor caliente, m28) ---

@app.get("/api/reflection/weekly")
def weekly_reflection_get(week: str = None):
    """The Friday 5-questions log for one ISO week (default: current)."""
    row = _weekly_reflection_mod.get_week(week)
    return {"week_reflection": row}


@app.post("/api/reflection/weekly")
def weekly_reflection_save(body: dict):
    """Save/upsert the week's answers (q_regale/q_declare/q_referido/
    q_propuesta/q_aprendi; optional 'week' YYYY-Www)."""
    week = body.pop("week", None) if isinstance(body, dict) else None
    res = _weekly_reflection_mod.save_week(body or {}, week=week)
    if res.get("status") == "error":
        raise HTTPException(400, res["error"])
    return res


@app.get("/api/reflection/weekly/history")
def weekly_reflection_history(n: int = 8):
    """Last N weeks of 5-questions answers, newest first — the personal
    scorecard trend."""
    return {"history": _weekly_reflection_mod.history(n)}


@app.get("/api/growth/radar")
def growth_radar_view():
    """The Motor Caliente radar: the commercial journey as rings (seguimiento →
    oportunidad → propuesta → proyectos), radius = the touch clock. Pure read;
    nothing moves inward except a real touch."""
    return _growth_radar_mod.compose()


@app.get("/api/friday-brief")
def friday_brief(format: str = None):
    """The Thursday pre-block brief: gates, tablero, stale deals, pending CRM
    proposals, generosity-touch candidates and the referral momento-alto.
    Pure read — composing it changes nothing. ?format=md renders the Telegram
    text (canonical deep links; read/nudge only)."""
    if format == "md":
        return PlainTextResponse(_friday_prep_mod.render_md())
    return _friday_prep_mod.compose()


@app.get("/api/reflection/history")
def reflection_history(days: int = 7):
    """Last N days of reflections, newest first (clamped 1–90)."""
    return {"days": days, "history": _reflection_mod.get_history(days)}


@app.get("/api/reflection/prefill")
def reflection_prefill(date: str = None):
    """Candidate wins + bounded timeline from the persisted day review.
    available=false (never an error) when the review hasn't run."""
    try:
        return _reflection_mod.prefill_from_day_review(date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _save_morning(body: dict):
    body = body or {}
    try:
        return _reflection_mod.save_morning(
            body.get("date") or _reflection_mod.today_str(),
            body.get("intentions") or [],
            source=body.get("source") or "dashboard",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reflection/morning")
def reflection_save_morning(body: dict):
    """Save 1-3 morning intentions. Idempotent upsert — re-saving replaces
    the JSON, never duplicates the row."""
    return _save_morning(body)


@app.put("/api/reflection/morning")
def reflection_edit_morning(body: dict):
    """Edit morning intentions (BRIEF v2 §6 — same body/semantics as POST;
    the upsert makes both verbs safe)."""
    return _save_morning(body)


@app.put("/api/reflection/morning/progress")
def reflection_morning_progress(body: dict):
    """Set one canonical morning goal's date-scoped completion state."""
    body = body or {}
    try:
        return _reflection_mod.set_morning_progress(
            body.get("date") or _reflection_mod.today_str(),
            body.get("index"),
            body.get("completed"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reflection/evening")
def reflection_save_evening(body: dict):
    """Save the Reflection–Action Loop: wins [{what,why}] (1-3),
    misses [{what,what_happened,why}] (0-2), adjustments [{action,when}] (0-2)."""
    body = body or {}
    try:
        return _reflection_mod.save_evening(
            body.get("date") or _reflection_mod.today_str(),
            body.get("wins") or [],
            body.get("misses") or [],
            body.get("adjustments") or [],
            source=body.get("source") or "dashboard",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reflection/generate-morning")
def reflection_generate_morning():
    """Morning cron endpoint: the deterministic Telegram prompt, or
    exists=true (→ the cron stays silent) when today is already saved."""
    today = _reflection_mod.today_str()
    if _reflection_mod.get_reflection(today).get("morning"):
        return {"exists": True, "prompt": None}
    return {"exists": False,
            "prompt": _reflection_mod.generate_morning_prompt(today)}


@app.post("/api/reflection/generate-evening")
def reflection_generate_evening(body: dict = None):
    """Evening cron endpoint (18:45): prompt composed from the REAL morning
    intentions + the persisted 18:30 day review; exists=true when already
    answered. A missing day review degrades to an honest 'no data' line."""
    today = _reflection_mod.today_str()
    cur = _reflection_mod.get_reflection(today)
    if cur.get("evening"):
        return {"exists": True, "prompt": None}
    day_review = (body or {}).get("day_review")
    if day_review is None:
        day_review = _reflection_mod.load_day_review(today)
    return {"exists": False,
            "prompt": _reflection_mod.generate_evening_prompt(
                day_review, cur.get("morning"))}


# --- Consultant Time Ledger API (PRD §7) ---
# Six routes over the consulting_time domain module. Domain errors carry
# their HTTP status code; everything else is a 200 with the row/ledger.

def _ct_err(exc: _consulting_time_mod.ConsultingTimeError):
    raise HTTPException(exc.status_code, str(exc))


@app.get("/api/consulting-time")
def consulting_time_list(project_id: str, today: Optional[str] = None,
                          limit: int = 50):
    """Selected-project completed entries + today/week/month summaries.
    project_id is required (422 when missing — FastAPI query-required)."""
    try:
        return _consulting_time_mod.get_project_ledger(project_id, today=today,
                                                       limit=limit)
    except _consulting_time_mod.ConsultingTimeError as exc:
        raise HTTPException(exc.status_code, str(exc))


@app.post("/api/consulting-time")
def consulting_time_create(body: dict):
    """Manual entry: project_id, work_date, minutes (1..1440), description,
    optional task_id, billable (default true)."""
    try:
        return _consulting_time_mod.create_manual(
            body.get("project_id") or "",
            body.get("work_date") or "",
            body.get("minutes"),
            body.get("description") or "",
            task_id=body.get("task_id"),
            billable=bool(body.get("billable", True)),
        )
    except _consulting_time_mod.ConsultingTimeError as exc:
        raise HTTPException(exc.status_code, str(exc))


@app.get("/api/consulting-time/active")
def consulting_time_active():
    """The one globally-active timer, or {active: null}."""
    return {"active": _consulting_time_mod.get_active_timer()}


@app.post("/api/consulting-time/timer/start")
def consulting_time_timer_start(body: dict):
    """Start the global timer: project_id, optional description/task_id/billable."""
    try:
        return _consulting_time_mod.start_timer(
            body.get("project_id") or "",
            description=body.get("description"),
            task_id=body.get("task_id"),
            billable=bool(body.get("billable", True)),
        )
    except _consulting_time_mod.ConsultingTimeError as exc:
        raise HTTPException(exc.status_code, str(exc))


@app.post("/api/consulting-time/{entry_id}/stop")
def consulting_time_timer_stop(entry_id: str):
    """Stop the active timer using server time only. Minimum one second."""
    try:
        return _consulting_time_mod.stop_timer(entry_id)
    except _consulting_time_mod.ConsultingTimeError as exc:
        raise HTTPException(exc.status_code, str(exc))


@app.delete("/api/consulting-time/{entry_id}")
def consulting_time_delete(entry_id: str):
    """Delete a completed mistake or discard an active timer (after UI confirm)."""
    try:
        return _consulting_time_mod.delete_entry(entry_id)
    except _consulting_time_mod.ConsultingTimeError as exc:
        raise HTTPException(exc.status_code, str(exc))


# --- Entry point ---
if __name__ == "__main__":
    import uvicorn
    print(f"🎮 Hermes Orchestrator Dashboard")
    print(f"   Binding to: http://{TAILSCALE_IP}:{PORT}")
    print(f"   Kanban DB: {db.KANBAN_DB}")
    print(f"   ⚠️  Intranet only — accessible only via Tailscale tailnet")
    uvicorn.run(app, host=TAILSCALE_IP, port=PORT, log_level="info")
