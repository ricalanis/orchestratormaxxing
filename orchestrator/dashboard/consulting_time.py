"""
Hermes Orchestrator Dashboard — Consultant Time Ledger (MVP).

Actual consulting time per canonical project (+ optional task). Two capture
modes: manual rows (date + minutes) and one globally-exclusive persistent
timer (server-timed stop). Summaries are local-date scoped
(America/Monterrey). See knowledge/hermes-project-workspace-prd.md §6–7.

The domain raises ``ConsultingTimeError(status_code, message)``; the HTTP
edge in api.py translates that into the matching 4xx.
"""
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import db

# America/Monterrey local zone for work_date derivation + summary boundaries.
try:
    from zoneinfo import ZoneInfo
    _MTY = ZoneInfo("America/Monterrey")
except Exception:  # pragma: no cover — zoneinfo needs tzdata on some builds
    _MTY = timezone(timedelta(hours=-6))

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConsultingTimeError(Exception):
    """Domain error carrying the HTTP status code the API edge should return."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


# ─── Schema ─────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    """Idempotent install of consulting_time_entries + the global active-timer
    partial unique index. Safe every boot."""
    conn = db.get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consulting_time_entries ("
            " id               TEXT PRIMARY KEY,"
            " project_id       TEXT NOT NULL,"
            " task_id          TEXT,"
            " work_date        TEXT,"
            " description      TEXT,"
            " billable         INTEGER NOT NULL DEFAULT 1,"
            " source           TEXT NOT NULL,"          # manual | timer
            " started_at       INTEGER,"                 # UTC epoch; null for manual
            " ended_at         INTEGER,"                 # UTC epoch; null while running
            " duration_seconds INTEGER,"                 # null while timer runs
            " created_at       INTEGER NOT NULL"
            ")"
        )
        # One active timer globally: among rows where source='timer' AND
        # ended_at IS NULL, the `source` column (always 'timer') must be unique
        # — i.e. at most one such row can exist.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_consulting_time_active_timer "
            "ON consulting_time_entries(source) "
            "WHERE source = 'timer' AND ended_at IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _local_today() -> str:
    return datetime.now(_MTY).date().isoformat()


def _validate_date(value: Optional[str]) -> str:
    value = (value or "").strip()
    if not _DATE_RE.match(value):
        raise ConsultingTimeError(400, "work_date debe ser YYYY-MM-DD")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ConsultingTimeError(400, "work_date inválida")
    if d > date.fromisoformat(_local_today()):
        raise ConsultingTimeError(400, "work_date no puede ser futura")
    return value


def _validate_minutes(minutes) -> int:
    # Reject bool (isinstance(True, int) is True) and non-ints (float, str).
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ConsultingTimeError(400, "minutes must be an integer 1..1440")
    if minutes < 1 or minutes > 1440:
        raise ConsultingTimeError(400, "minutes must be 1..1440")
    return minutes


def _validate_project(conn: sqlite3.Connection, project_id: str) -> None:
    if not conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)
    ).fetchone():
        raise ConsultingTimeError(404, f"project not found: {project_id}")


def _validate_task(conn: sqlite3.Connection, project_id: str,
                   task_id: Optional[str]) -> None:
    if task_id is None or task_id == "":
        return
    row = conn.execute(
        "SELECT project_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        raise ConsultingTimeError(404, f"task not found: {task_id}")
    if row["project_id"] != project_id:
        raise ConsultingTimeError(
            400, f"task {task_id} does not belong to project {project_id}")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _new_id() -> str:
    return f"cte_{uuid.uuid4().hex[:12]}"


# ─── Manual entries ─────────────────────────────────────────────────────────

def create_manual(project_id: str, work_date: str, minutes,
                   description: str, task_id: Optional[str] = None,
                   billable: bool = True) -> dict:
    """Insert a completed manual entry. Validates project/task/minutes/date."""
    work_date = _validate_date(work_date)
    mins = _validate_minutes(minutes)
    desc = (description or "").strip()
    if not desc:
        raise ConsultingTimeError(400, "description is required")

    conn = db.get_conn()
    try:
        _validate_project(conn, project_id)
        _validate_task(conn, project_id, task_id)
        entry_id = _new_id()
        now = int(time.time())
        conn.execute(
            "INSERT INTO consulting_time_entries "
            "(id, project_id, task_id, work_date, description, billable, "
            " source, started_at, ended_at, duration_seconds, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, project_id, task_id, work_date, desc,
             1 if billable else 0, "manual", None, None, mins * 60, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM consulting_time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


# ─── Timer ───────────────────────────────────────────────────────────────────

def start_timer(project_id: str, description: Optional[str] = None,
                task_id: Optional[str] = None, billable: bool = True,
                now: Optional[int] = None) -> dict:
    """Start the globally-exclusive timer. 409 if one is already running."""
    if now is None:
        now = int(time.time())
    desc = (description or "").strip() or None

    conn = db.get_conn()
    try:
        _validate_project(conn, project_id)
        _validate_task(conn, project_id, task_id)
        entry_id = _new_id()
        work_date = _local_today()
        try:
            conn.execute(
                "INSERT INTO consulting_time_entries "
                "(id, project_id, task_id, work_date, description, billable, "
                " source, started_at, ended_at, duration_seconds, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, project_id, task_id, work_date, desc,
                 1 if billable else 0, "timer", now, None, None, now),
            )
        except sqlite3.IntegrityError:
            # The partial unique index uq_consulting_time_active_timer enforces
            # one active timer globally — a second start violates it.
            raise ConsultingTimeError(409, "a timer is already running")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM consulting_time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def stop_timer(entry_id: str, now: Optional[int] = None) -> dict:
    """Stop the active timer using server time. Minimum one second."""
    if now is None:
        now = int(time.time())
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM consulting_time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            raise ConsultingTimeError(404, f"entry not found: {entry_id}")
        if row["source"] != "timer" or row["ended_at"] is not None:
            raise ConsultingTimeError(409, "timer is not active")
        duration = max(now - row["started_at"], 1)
        conn.execute(
            "UPDATE consulting_time_entries SET ended_at = ?, duration_seconds = ? "
            "WHERE id = ?",
            (now, duration, entry_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM consulting_time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_active_timer() -> Optional[dict]:
    """The one globally-active timer row, or None."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM consulting_time_entries "
            "WHERE source = 'timer' AND ended_at IS NULL"
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


# ─── Project ledger + delete ─────────────────────────────────────────────────

def get_project_ledger(project_id: str, today: Optional[str] = None,
                       limit: int = 50) -> dict:
    """Project-scoped completed entries + today/week/month summaries.

    Summary boundaries use local dates (America/Monterrey). Only completed rows
    (duration_seconds IS NOT NULL) are counted — an active timer is excluded.
    """
    conn = db.get_conn()
    try:
        _validate_project(conn, project_id)
        today_str = (today or "").strip() or _local_today()
        try:
            today_date = date.fromisoformat(today_str)
        except ValueError:
            today_str = _local_today()
            today_date = date.fromisoformat(today_str)

        # ISO week: Monday–Sunday containing today.
        monday = today_date - timedelta(days=today_date.weekday())
        sunday = monday + timedelta(days=6)
        week_start = monday.isoformat()
        week_end = sunday.isoformat()

        # Calendar month containing today.
        month_start = f"{today_date.year:04d}-{today_date.month:02d}-01"
        if today_date.month == 12:
            month_end = f"{today_date.year:04d}-12-31"
        else:
            month_end = (
                date(today_date.year, today_date.month + 1, 1) - timedelta(days=1)
            ).isoformat()

        rows = conn.execute(
            "SELECT * FROM consulting_time_entries "
            "WHERE project_id = ? AND duration_seconds IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        entries = [_row_to_dict(r) for r in rows]

        today_seconds = sum(
            r["duration_seconds"] for r in rows if r["work_date"] == today_str
        )
        week_seconds = sum(
            r["duration_seconds"] for r in rows
            if week_start <= r["work_date"] <= week_end
        )
        month_seconds = sum(
            r["duration_seconds"] for r in rows
            if month_start <= r["work_date"] <= month_end
        )

        return {
            "summary": {
                "today_seconds": today_seconds,
                "week_seconds": week_seconds,
                "month_seconds": month_seconds,
            },
            "entries": entries,
        }
    finally:
        conn.close()


def delete_entry(entry_id: str) -> dict:
    """Delete a completed mistake or discard an active timer. 404 if missing."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM consulting_time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            raise ConsultingTimeError(404, f"entry not found: {entry_id}")
        conn.execute(
            "DELETE FROM consulting_time_entries WHERE id = ?", (entry_id,)
        )
        conn.commit()
        return {"id": entry_id}
    finally:
        conn.close()