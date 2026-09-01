"""Honest dispatch — the one verb behind `POST /api/tasks/{id}/dispatch`.

Consolidation spec §2 ("Dispatch — the honesty rule"). What this replaces:
`dispatchTo()` → `set_task_assignee()` → `UPDATE tasks SET assignee`. Nothing
spawned, nothing was notified, the screen was identical afterwards, and Ricardo
pressed it 111 times. **A control that lies about its effect teaches distrust of
every other control on the page** — so this module's whole contract is that the
state it reports is the state that actually happened.

**The saga (ruling 4).** Dispatch has three side effects on three different
systems (the kanban DB via the hermes CLI, a detached OS process, a Telegram
message) and no distributed transaction exists across them. So it is written as
an outbox: a `task_dispatches` row is created FIRST in state `requested`, the
side effects run in order, and the row is then updated to what ACTUALLY
happened — `delivered` · `spawn_failed` · `send_failed`. The response is that
final row and the toast renders exactly it. A dispatch whose Telegram message
failed says so; it never renders "Sent" over a failure.

  * **Idempotency key.** `dispatch_id` is the caller-stable id (a double-tap, a
    retried fetch and a re-fired cron all carry the same one). The insert is
    `INSERT OR IGNORE`; a second call returns the STORED row and runs no side
    effect twice.
  * **The dashboard NEVER writes `task_runs`.** That table is Hermes-owned: the
    in-gateway dispatcher (60s) creates the run row when it claims the task.
    A dashboard-written run row would be a second writer inventing runs that no
    worker ever executed — the same lie in a different table. Codex's exit code
    and stdout tail therefore land on the OUTBOX row, which is ours.
  * **Human-origin guard (ruling 2).** Phase-1 dispatch is ALWAYS human-initiated
    from the web UI — the click IS the approval, which is why no ASK-queue
    mechanics are needed yet. The guard is structural, not a comment: the route
    is a mutating `/api/*` POST (so `MutatingAuthMiddleware` demands the
    dashboard Bearer token) and dispatch is deliberately absent from
    `mcp_server.py`. Do not add MCP parity for it.

**The three destinations, and why they are not symmetric** (taught by the verb
and the icon, never by documentation — dialogs get click-through blindness):

  * `▶ hermes (runs now)` — `kanban assign <id> default` + a validated
    transition to `ready`; the gateway's dispatcher spawns it. Phase 1 targets
    `default` only: it is the sole profile on disk, which is why a live dry-run
    skipped 100% of ready tasks as `skipped_nonspawnable`. New profiles are a
    Hermes-side change (phase 3).
  * `▶ codex (runs now)` — `codex exec -C <workspace> -s workspace-write`,
    detached. The only fully-autonomous onward path.
  * `✋ claude (brief & notify)` — composes the brief and posts it. **Never
    spawns anything, ever** (red line 10): human-in-the-loop is policy here, not
    an engineering gap.

**Every CLI/OS crossing goes through the two seams `_run_cli` and `_spawn`.**
That is what lets the contract pin the EXACT argv of every verb without a test
ever touching the real kanban, spawning a real Codex, or sending Ricardo a real
Telegram message — and it is why a wrong flag is a red test rather than a
silent no-op in production.
"""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import db

# The three onward destinations. `human` is not a dispatch — handing a task back
# to Ricardo is `reclaim`, a different verb with no side effects.
EXECUTOR_KINDS = ("hermes", "codex", "claude")

# Phase 1 targets the only Hermes profile that exists on disk.
HERMES_TARGET = "default"

# The unbound-project fallback (ruling 10): a task whose project carries no
# bound thread is announced in the ritual thread, and the response says so, so the toast can
# tell Ricardo where it actually went.
HOY_THREAD_NAMES = ("Hoy", "📅 Hoy")
HOY_THREAD_STATION = "ritual"

# Only used when the thread registry carries no chat_id for a row (it always
# does in production — m02_spine seeds it). Never a substitute for the registry.
DEFAULT_CHAT_ID = os.environ.get("HERMES_DEFAULT_CHAT_ID", "")

# How much of a detached Codex run's output the outbox row keeps.
STDOUT_TAIL_CHARS = 2000

# Task statuses the hermes gateway can actually pick up, and how each one is
# moved to `ready`. Anything else is refused BEFORE a CLI call (dispatching a
# done task is not a transport failure — it is a category error).
_HERMES_READY = "ready"
_HERMES_PROMOTE = ("todo", "blocked")     # dependency-checked; refuses unsatisfied parents
_HERMES_UNBLOCK = ("scheduled",)

_SUBPROCESS_TIMEOUT = 30


def _now() -> int:
    return int(time.time())


def _gen_id() -> str:
    return f"disp_{uuid.uuid4().hex[:8]}"


def _error(code: str, message: str) -> dict:
    """Typed error, same shape as crm's: `code` is the machine-readable branch
    (route and UI both switch on it), `error` the human string."""
    return {"status": "error", "code": code, "error": message}


def _dashboard_url() -> str:
    """Delegates — `db.dashboard_url()` is the single source (see its docstring).

    Kept as a named function rather than inlined at the two call sites because
    `brief.py` deliberately reads THIS one ("both read `dispatch._dashboard_url()`")
    and a test repoints it."""
    return db.dashboard_url()


def _codex_bin() -> str:
    """Same absolute-resolution rule as db.hermes_bin(): a systemd --user unit's
    default PATH lacks ~/.local/bin, so a bare "codex" works in a dev shell and
    FileNotFoundErrors after a reboot."""
    return shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")


# --------------------------------------------------------------- the seams
#
# Two functions, and they are the ONLY places this module leaves the process.
# The contract monkeypatches them to pin argv exactly; production keeps the real
# ones. Keep every subprocess call inside them.

def _run_cli(argv: list, timeout: int = _SUBPROCESS_TIMEOUT) -> tuple:
    """Run a CLI synchronously and RETURN ITS EXIT CODE. (returncode, stdout,
    stderr). Failures are converted to a code, never raised: the saga's job is
    to record what happened, and an exception escaping here would abandon an
    outbox row in `requested` while the side effect may well have landed."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as e:
        return 127, "", f"{argv[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(argv[:3])}"
    except Exception as e:                                   # pragma: no cover
        return 1, "", str(e)


def _spawn(argv: list):
    """Start a DETACHED process and return it. `start_new_session=True` puts it
    in its own process group so the dashboard restarting (or being killed) never
    takes a running Codex with it."""
    return subprocess.Popen(
        argv, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _start_reaper(dispatch_id: str, proc) -> None:
    """Daemon thread that waits for the detached run and records its outcome on
    the OUTBOX row. A seam of its own so the contract can run the reaper
    synchronously and assert where it wrote."""
    t = threading.Thread(target=_reap, args=(dispatch_id, proc), daemon=True,
                         name=f"codex-reap-{dispatch_id}")
    t.start()


def _reap(dispatch_id: str, proc) -> None:
    """Wait, then stamp exit_code + stdout_tail on the outbox row.

    Deliberately does NOT touch `state`: the state is the DELIVERY saga's answer
    ("did the dispatch leave the building"), and a Codex run that exits 1 an hour
    later did not un-deliver it. It also never writes `task_runs` — that table is
    Hermes-owned (ruling 4)."""
    try:
        out, _ = proc.communicate()
    except Exception as e:                                   # pragma: no cover
        out = f"[reaper] {e}"
    code = getattr(proc, "returncode", None)
    tail = (out or "")[-STDOUT_TAIL_CHARS:]
    try:
        _update_outbox(dispatch_id, exit_code=code, stdout_tail=tail)
    except Exception:                                        # pragma: no cover
        pass


# ------------------------------------------------------------- the outbox

def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _update_outbox(dispatch_id: str, **fields) -> Optional[dict]:
    """Patch an outbox row and return it. `updated_at` always moves."""
    if not fields:
        return _get_outbox(dispatch_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_conn()
    try:
        conn.execute(f"UPDATE task_dispatches SET {cols}, updated_at = ? WHERE id = ?",
                     (*fields.values(), _now(), dispatch_id))
        conn.commit()
        row = conn.execute("SELECT * FROM task_dispatches WHERE id = ?",
                           (dispatch_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_outbox(dispatch_id: str) -> Optional[dict]:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM task_dispatches WHERE id = ?",
                           (dispatch_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_dispatches(task_id: str, limit: int = 20) -> list:
    """The task's dispatch history — what the drawer reads to show that a
    dispatch is a thing that happened, not a flag that got set."""
    conn = db.get_conn()
    try:
        if not _has_table(conn, "task_dispatches"):
            return []
        return [dict(r) for r in conn.execute(
            "SELECT * FROM task_dispatches WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (task_id, limit)).fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------ thread resolution
#
# "Ricardo never picks a thread" (spec §2). The destination is derived; the
# picker is the escape hatch, not the path. Required decisions per dispatch: 0.

def _thread_row(row) -> dict:
    return {"thread_id": row["thread_id"], "name": row["name"],
            "chat_id": str(row["chat_id"] or DEFAULT_CHAT_ID)}


def _thread_of(conn, thread_id) -> dict:
    """The registry row for a thread id that is already RECORDED (the stored
    outbox row's). Used on the idempotent path so a replay names the thread the
    dispatch actually went to, not the one this call would have picked."""
    if thread_id is None or not _has_table(conn, "threads"):
        return {"thread_id": thread_id, "name": None, "chat_id": DEFAULT_CHAT_ID,
                "fallback": False, "source": "stored"}
    row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        return {"thread_id": thread_id, "name": None, "chat_id": DEFAULT_CHAT_ID,
                "fallback": False, "source": "stored"}
    return {**_thread_row(row), "fallback": False, "source": "stored"}


def _resolve_thread(conn, task: dict, explicit=None) -> dict:
    """explicit → the task's project binding (active) → Hoy. `fallback` is True
    when the task's project had no bound thread, so the response can say
    "no thread bound — went to Hoy" instead of implying a binding exists."""
    if not _has_table(conn, "threads"):
        return {"thread_id": None, "name": None, "chat_id": DEFAULT_CHAT_ID,
                "fallback": False, "source": "no_registry"}
    if explicit not in (None, ""):
        try:
            wanted = int(explicit)
        except (TypeError, ValueError):
            return _error("unknown_thread", f"thread '{explicit}' is not a thread id")
        row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (wanted,)).fetchone()
        if row is None:
            # Routing a message somewhere unverified is exactly the failure mode
            # this step exists to kill, so an unknown thread is refused, never
            # silently redirected.
            return _error("unknown_thread", f"thread {wanted} is not in the registry")
        return {**_thread_row(row), "fallback": False, "source": "explicit"}

    project_id = task.get("project_id")
    if project_id:
        row = conn.execute(
            "SELECT * FROM threads WHERE project_id = ? AND status = 'active' "
            "ORDER BY thread_id LIMIT 1", (project_id,)).fetchone()
        if row is not None:
            return {**_thread_row(row), "fallback": False, "source": "project"}

    thread_cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "station" in thread_cols:
        # m12 deliberately made the ritual role stable while keeping the
        # display name editable. Routing by the old literal `Hoy` silently
        # stopped working as soon as that migration renamed it to `📅 Hoy`.
        row = conn.execute(
            "SELECT * FROM threads WHERE station = ? AND status = 'active' "
            "ORDER BY thread_id LIMIT 1", (HOY_THREAD_STATION,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM threads WHERE name IN (?, ?) AND status = 'active' "
            "ORDER BY thread_id LIMIT 1", HOY_THREAD_NAMES).fetchone()
    if row is not None:
        return {**_thread_row(row), "fallback": True, "source": "hoy"}
    return {"thread_id": None, "name": None, "chat_id": DEFAULT_CHAT_ID,
            "fallback": True, "source": "none"}


# ------------------------------------------------------------- the brief

_ACCEPTANCE_RE = re.compile(r"^#{1,6}\s*acceptance\b", re.IGNORECASE)


def acceptance_of(body: Optional[str], max_lines: int = 3) -> list:
    """The task's acceptance criteria, if it wrote any. A `## Acceptance`
    section wins; otherwise the first lines of the body. Never invented — an
    empty body yields an empty list and the brief simply omits the block."""
    lines = [l.rstrip() for l in (body or "").splitlines()]
    out, capturing = [], False
    for line in lines:
        if _ACCEPTANCE_RE.match(line.strip()):
            capturing = True
            continue
        if capturing:
            if line.strip().startswith("#"):
                break
            if line.strip():
                out.append(line.strip())
            if len(out) >= max_lines:
                break
    if out:
        return out
    return [l.strip() for l in lines if l.strip()][:max_lines]


def task_link(task_id: str) -> str:
    """The deep link every brief line carries: tap → the drawer opens on the
    exact object → tap the action. Two taps from Telegram to a state change."""
    return f"{_dashboard_url()}/?entity=task:{task_id}"


_HEADERS = {
    "hermes": "▶ Hermes (runs now)",
    "codex": "▶ Codex (runs now)",
    "claude": "✋ Claude (brief & notify)",
}


def compose_brief(task: dict, kind: str, workspace: Optional[str] = None) -> str:
    """The message posted to the thread. Same asymmetry the buttons teach:
    ▶ runs now vs ✋ brief & notify. Deterministic — title, the task's own
    acceptance text, and the link. No narrator (red line 4)."""
    title = (task.get("title") or task["id"]).strip()
    out = [f"{_HEADERS[kind]} · {title}"]
    for line in acceptance_of(task.get("body")):
        out.append(f"• {line}")
    if kind == "codex" and workspace:
        out.append(f"📂 {workspace}")
    if kind == "claude":
        # Paste-ready: the human's job here is to paste, not to reconstruct the
        # request from a notification.
        out.append("📋 Paste into a Claude session:")
        out.append(f"    {title} — {task_link(task['id'])}")
    out.append(f"🔗 {task_link(task['id'])}")
    return "\n".join(out)


# ------------------------------------------------------------- the branches
#
# Each returns {"ok": bool, "steps": [...], "code": ..., "note": ...}. `steps`
# is what actually succeeded — it becomes the outbox note, so a partial dispatch
# reads as a partial dispatch.

def _dispatch_hermes(task: dict, thread: dict) -> dict:
    hermes, task_id = db.hermes_bin(), task["id"]
    status = (task.get("status") or "").strip().lower()

    # Refuse BEFORE any CLI call: a done/archived task is not a failed dispatch,
    # it is a request that should never have been made.
    if status == _HERMES_READY:
        transition = None
    elif status in _HERMES_PROMOTE:
        # `promote` is dependency-checked and refuses unsatisfied parents — that
        # refusal is a feature, so it is invoked WITHOUT --force.
        transition = [hermes, "kanban", "promote", task_id]
    elif status in _HERMES_UNBLOCK:
        transition = [hermes, "kanban", "unblock", task_id]
    else:
        return {"ok": False, "code": "not_dispatchable", "steps": [],
                "note": f"a task with status '{status}' cannot be handed to the "
                        f"hermes gateway (needs ready/todo/blocked/scheduled)"}

    steps = []
    # POSITIONAL profile — `kanban assign <task_id> <profile>`, verified against
    # hermes_cli/kanban.py's parser. `--assignee` is not a flag this verb has.
    code, out, err = _run_cli([hermes, "kanban", "assign", task_id, HERMES_TARGET])
    if code != 0:
        return {"ok": False, "code": "assign_failed", "steps": steps,
                "note": f"kanban assign exited {code}: {(err or out).strip()[-300:]}"}
    steps.append(f"assign:{HERMES_TARGET}")

    if transition:
        code, out, err = _run_cli(transition)
        if code != 0:
            return {"ok": False, "code": "not_ready", "steps": steps,
                    "note": f"kanban {transition[2]} exited {code}: "
                            f"{(err or out).strip()[-300:]}"}
        steps.append(transition[2])

    # Scoped to THIS task and THIS thread — a reply to a request, not a
    # completion firehose (spec §2). Best-effort: the dispatch itself has already
    # landed (the task is assigned and ready, the gateway will spawn it), so a
    # failed subscription is recorded, not promoted into a failed dispatch.
    if thread.get("thread_id"):
        code, out, err = _run_cli([
            hermes, "kanban", "notify-subscribe", task_id,
            "--platform", "telegram",
            "--chat-id", str(thread["chat_id"]),
            "--thread-id", str(thread["thread_id"])])
        steps.append("notify-subscribe" if code == 0
                     else f"notify-subscribe-failed:{code}")
    return {"ok": True, "steps": steps, "code": None, "note": None}


def _resolve_workspace(task: dict) -> Optional[str]:
    """task.workspace_path → the project's repo_path → nothing. Both must be
    directories that EXIST: `codex exec -C <missing>` fails after the outbox row
    already claims a spawn, so the check is done here, before spawning."""
    candidate = (task.get("workspace_path") or "").strip()
    if candidate and Path(candidate).is_dir():
        return str(Path(candidate))
    project_id = task.get("project_id")
    if project_id:
        conn = db.get_conn()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
            if "repo_path" in cols:
                row = conn.execute("SELECT repo_path FROM projects WHERE id = ?",
                                   (project_id,)).fetchone()
                repo = (row["repo_path"] if row else None) or ""
                if repo.strip() and Path(repo.strip()).is_dir():
                    return str(Path(repo.strip()))
        finally:
            conn.close()
    return None


def _dispatch_codex(task: dict, dispatch_id: str) -> dict:
    workspace = _resolve_workspace(task)
    if not workspace:
        return {"ok": False, "code": "no_workspace", "steps": [],
                "note": "no runnable workspace: set the task's workspace_path or "
                        "the project's repo_path"}
    prompt = compose_brief(task, "codex", workspace)
    argv = [_codex_bin(), "exec", "-C", workspace, "-s", "workspace-write", prompt]
    try:
        proc = _spawn(argv)
    except Exception as e:
        return {"ok": False, "code": "spawn_error", "steps": [],
                "note": f"could not spawn codex: {e}"}
    _start_reaper(dispatch_id, proc)
    return {"ok": True, "steps": ["spawn:codex"], "code": None, "note": None,
            "executor_target": workspace}


def _dispatch_claude(task: dict) -> dict:
    """No spawn. Ever. (Red line 10 — "never automate Claude Code onward
    delegation".) The whole branch is the absence of a spawn plus the brief the
    send step posts; if this function ever grows a `_spawn` call, the contract
    for it goes red."""
    return {"ok": True, "steps": ["brief"], "code": None, "note": None}


def _send_brief(thread: dict, text: str) -> dict:
    if not thread.get("thread_id"):
        return {"ok": False, "code": "no_thread",
                "note": "no thread to announce into (registry has no active Hoy)"}
    argv = [db.hermes_bin(), "send", "--to",
            f"telegram:{thread['chat_id']}:{thread['thread_id']}", "-q", text]
    code, out, err = _run_cli(argv)
    if code != 0:
        return {"ok": False, "code": "send_failed",
                "note": f"hermes send exited {code}: {(err or out).strip()[-300:]}"}
    return {"ok": True, "code": None, "note": None}


# ------------------------------------------------------------------ the saga

def _reply(row: dict, thread: dict, idempotent: bool = False,
           code: Optional[str] = None) -> dict:
    """The response the toast renders — the FINAL outbox row plus the thread's
    human name. Honest by construction: there is no field here that the saga did
    not observe, and `state` is read back out of the row, never assumed."""
    return {
        "status": "ok",
        "dispatch_id": row["id"],
        "task_id": row["task_id"],
        "executor_kind": row["executor_kind"],
        "executor_target": row["executor_target"],
        "state": row["state"],
        "note": row["note"],
        "exit_code": row["exit_code"],
        "thread_id": row["thread_id"],
        "thread_name": thread.get("name"),
        "thread_fallback": bool(thread.get("fallback")),
        "thread_source": thread.get("source"),
        "code": code,
        "idempotent": idempotent,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def dispatch_task(task_id: str, executor_kind: str,
                  executor_target: Optional[str] = None,
                  dispatch_id: Optional[str] = None,
                  thread_id=None) -> dict:
    """Dispatch a task to one of the three destinations, as an outbox saga.

    Returns the final outbox row + the thread's name. `spawn_failed` and
    `send_failed` are SUCCESSFUL returns of this function — the saga ran and
    this is what happened; only a malformed request (unknown task, unknown
    executor, unknown thread) is a typed error.
    """
    kind = (executor_kind or "").strip().lower()
    if kind not in EXECUTOR_KINDS:
        return _error("unknown_executor",
                      f"executor_kind must be one of {list(EXECUTOR_KINDS)}")
    target = (executor_target or "").strip() or None
    if kind == "hermes" and target not in (None, HERMES_TARGET):
        # Phase 1 targets `default` only; anything else would assign a task to a
        # profile that does not exist on disk and skip forever as
        # `skipped_nonspawnable`. Refusing is honest; assigning is not.
        return _error("unsupported_target",
                      f"phase 1 dispatches to the '{HERMES_TARGET}' hermes profile only")
    if kind == "hermes":
        target = HERMES_TARGET
    if kind == "claude":
        target = target or "claude-code"

    conn = db.get_conn()
    try:
        if not _has_table(conn, "task_dispatches"):
            return _error("spine_missing",
                          "task_dispatches is missing — the m02_spine migration has not run")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return _error("not_found", "task not found")
        task = dict(row)

        thread = _resolve_thread(conn, task, thread_id)
        if thread.get("status") == "error":
            return thread

        # --- 1. the outbox row, BEFORE any side effect ---------------------
        did = (dispatch_id or "").strip() or _gen_id()
        now = _now()
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_dispatches "
            "(id, task_id, executor_kind, executor_target, state, thread_id, "
            " note, created_at, updated_at) VALUES (?,?,?,?,'requested',?,?,?,?)",
            (did, task_id, kind, target, thread["thread_id"], None, now, now))
        if cur.rowcount == 0:
            # Same idempotency key → the stored row, and NOT a second set of side
            # effects. A double-tap must not send two Telegram messages or spawn
            # two Codex runs.
            conn.commit()
            stored = dict(conn.execute("SELECT * FROM task_dispatches WHERE id = ?",
                                       (did,)).fetchone())
            # Name the thread the STORED dispatch went to. Re-using this call's
            # freshly-resolved thread would let a replay with a different
            # thread_id toast a destination the message never reached.
            return _reply(stored, _thread_of(conn, stored["thread_id"]), idempotent=True)

        # --- 2. the executor columns (spec §1: assignee stays derived) -----
        conn.execute(
            "UPDATE tasks SET executor_kind = ?, executor_target = ?, thread_id = ? "
            "WHERE id = ?", (kind, target, thread["thread_id"], task_id))
        conn.commit()
    finally:
        conn.close()

    # --- 3. the side effects, in order --------------------------------------
    if kind == "hermes":
        branch = _dispatch_hermes(task, thread)
    elif kind == "codex":
        branch = _dispatch_codex(task, did)
    else:
        branch = _dispatch_claude(task)

    steps = list(branch.get("steps") or [])
    if branch.get("executor_target"):
        target = branch["executor_target"]
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tasks SET executor_target = ? WHERE id = ?",
                         (target, task_id))
            conn.commit()
        finally:
            conn.close()

    if not branch["ok"]:
        # The side effect the destination is FOR did not happen, so no brief is
        # sent: announcing a dispatch that did not occur is precisely the lie
        # this module exists to remove. The human is looking at the toast.
        note = "; ".join([*steps, branch.get("note") or branch.get("code") or "failed"])
        row = _update_outbox(did, state="spawn_failed", note=note[:2000],
                             executor_target=target)
        return _reply(row, thread, code=branch.get("code"))

    # --- 4. announce it into the thread -------------------------------------
    sent = _send_brief(thread, compose_brief(task, kind, target if kind == "codex" else None))
    if not sent["ok"]:
        # The work IS running (hermes assigned + ready, or Codex spawned). Only
        # the notification failed, and the note records exactly which side
        # effects landed — so the state is `send_failed`, never `spawn_failed`.
        note = "; ".join([*steps, sent.get("note") or "send failed"])
        row = _update_outbox(did, state="send_failed", note=note[:2000],
                             executor_target=target)
        return _reply(row, thread, code=sent.get("code"))

    note = "; ".join([*steps, "send"]) or None
    row = _update_outbox(did, state="delivered", note=(note or "")[:2000] or None,
                         executor_target=target)
    return _reply(row, thread)
