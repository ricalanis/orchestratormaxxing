"""Honest outbox saga behind ``POST /api/tasks/{id}/plan``."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import db


PLANNERS = ("fable", "opus1m", "sol")
_TIMEOUT = 45


def _now() -> int:
    return int(time.time())


def _error(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "error": message}


def _task_plan_bin() -> str:
    return (os.environ.get("TASK_PLAN_BIN") or shutil.which("task-plan")
            or str(Path.home() / ".local" / "bin" / "task-plan"))


def _run_cli(argv: list[str], timeout: int = _TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive process seam
        return 1, "", str(exc)


def _has_table(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_plan_requests'"
    ).fetchone() is not None


def _reply(row: dict, *, idempotent: bool = False) -> dict:
    return {
        "status": "ok",
        "request_id": row["id"],
        "task_id": row["task_id"],
        "planner": row["planner"],
        "state": row["state"],
        "session": row["session"],
        "attach_hint": row["attach_hint"],
        "folder": row["folder"],
        "note": row["note"],
        "exit_code": row["exit_code"],
        "idempotent": idempotent,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _update(request_id: str, **fields) -> dict:
    cols = ", ".join(f"{name} = ?" for name in fields)
    conn = db.get_conn()
    try:
        conn.execute(
            f"UPDATE task_plan_requests SET {cols}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), request_id),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM task_plan_requests WHERE id = ?", (request_id,)
        ).fetchone())
    finally:
        conn.close()


def plan_task(task_id: str, planner: str, request_id: str | None = None) -> dict:
    planner = (planner or "").strip().lower()
    if planner not in PLANNERS:
        return _error("unknown_planner", f"planner must be one of {list(PLANNERS)}")

    conn = db.get_conn()
    try:
        if not _has_table(conn):
            return _error("planning_outbox_missing", "m29_task_plan_requests has not run")
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            return _error("not_found", "task not found")
        rid = (request_id or "").strip() or f"plan_{uuid.uuid4().hex[:10]}"
        now = _now()
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_plan_requests "
            "(id, task_id, planner, state, created_at, updated_at) "
            "VALUES (?,?,?,'requested',?,?)",
            (rid, task_id, planner, now, now),
        )
        conn.commit()
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM task_plan_requests WHERE id = ?", (rid,)
            ).fetchone()
            if row is None:
                return _error("request_conflict", "planning request id was not readable")
            stored = dict(row)
            if stored["task_id"] != task_id or stored["planner"] != planner:
                return _error("request_conflict", "planning request id belongs to another request")
            return _reply(stored, idempotent=True)
    finally:
        conn.close()

    argv = [_task_plan_bin(), task_id, "--planner", planner, "--json"]
    code, stdout, stderr = _run_cli(argv)
    if code != 0:
        detail = (stderr or stdout or "task-plan failed without output").strip()[-2000:]
        return _reply(_update(rid, state="spawn_failed", exit_code=code, note=detail))

    try:
        payload = json.loads(stdout)
        session = str(payload["session"])
        attach_hint = str(payload["attach_hint"])
        folder = str(payload["folder"])
        if payload.get("planner") != planner or not session:
            raise ValueError("planner/session mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        note = f"task-plan returned invalid JSON: {exc}; output={stdout[-1000:]}"
        return _reply(_update(rid, state="spawn_failed", exit_code=1, note=note))

    # `-F <task-id>` asks c/g to register this link through the API. Stamp it
    # here too: this request is already inside that API, so the hard link cannot
    # be lost merely because the launcher's best-effort callback raced a restart.
    link_note = None
    conn = db.get_conn()
    try:
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session, task_id))
        conn.commit()
    except Exception as exc:  # session creation still happened; keep the toast truthful
        link_note = f"session created; task link failed: {exc}"
    finally:
        conn.close()

    return _reply(_update(
        rid,
        state="created",
        session=session,
        attach_hint=attach_hint,
        folder=folder,
        exit_code=0,
        note=link_note,
    ))
