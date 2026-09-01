"""
Sprint and project management layer.
Sidecar tables on top of the existing Hermes kanban DB.
"""
import os
import sqlite3
import time
import uuid
import json
import datetime as _dt
import re
from pathlib import Path
from typing import Optional

_DUE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_due_date(value):
    """Return ``(normalized, error)`` for the shared task deadline field.

    ``None`` means omitted and ``""`` is the explicit clear sentinel.  A set
    value must already be an exact, real calendar date; silently trimming or
    accepting timestamps would let different writers store different clocks.
    """
    if value is None or value == "":
        return None, None
    if not isinstance(value, str) or not _DUE_DATE_RE.fullmatch(value):
        return None, "due_date must be YYYY-MM-DD (empty string clears)"
    try:
        return _dt.date.fromisoformat(value).isoformat(), None
    except ValueError:
        return None, "due_date must be a real calendar date in YYYY-MM-DD"

KANBAN_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
    else Path.home() / ".hermes" / "kanban.db"


def get_conn():
    # Same live-DB tripwire as dashboard.db.get_conn — sprints keeps its own
    # KANBAN_DB global, so it is a second, independent path to the real file.
    from dashboard.db import assert_not_live_db
    assert_not_live_db(KANBAN_DB)
    conn = sqlite3.connect(str(KANBAN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _week_window(ts: Optional[int] = None) -> tuple:
    """The Mon→Sun window (local time) containing `ts` (default now), as a
    (start, end) unix pair. LOCKED decision: a cycle is one ISO week, Monday
    start. start = Monday 00:00:00 local; end = Sunday 23:59:59 local. Every
    cycle constructor snaps to this so the weekly auto-roll stays aligned."""
    import datetime as _dtm
    d = _dtm.date.fromtimestamp(ts if ts is not None else int(time.time()))
    monday = d - _dtm.timedelta(days=d.weekday())
    start = int(time.mktime(monday.timetuple()))
    end = start + 7 * 24 * 3600 - 1
    return start, end


def _iso_week_str(ts: Optional[int] = None, offset_weeks: int = 0) -> str:
    """The ISO week label ("2026-W28") for the week containing `ts` (default
    now), shifted by `offset_weeks`. Snaps through _week_window so the label
    always agrees with the cycle constructors' Mon→Sun windows."""
    import datetime as _dtm
    base = (ts if ts is not None else int(time.time())) + offset_weeks * 7 * 86400
    ws, _ = _week_window(base)
    iso = _dtm.date.fromtimestamp(ws).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _cycle_slot_window(anchor_start: int, offset_weeks: int) -> tuple[int, int]:
    """Return the exact ISO-week window ``offset_weeks`` after an anchor.

    Calendar-date arithmetic is deliberate: adding a fixed number of seconds
    across a DST boundary can land outside local midnight.  A noon anchor is
    then snapped through the one canonical Mon→Sun window helper.
    """
    import datetime as _dtm
    anchor_day = _dtm.date.fromtimestamp(anchor_start)
    target_day = anchor_day + _dtm.timedelta(weeks=offset_weeks)
    target_noon = _dtm.datetime.combine(target_day, _dtm.time(hour=12))
    return _week_window(int(time.mktime(target_noon.timetuple())))


# --- Projects ---

def list_projects() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM projects WHERE archived_at IS NULL ORDER BY name").fetchall()
        projects = [dict(r) for r in rows]
        # Add task counts per project
        for p in projects:
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id = ?", (p["id"],)).fetchone()[0]
            p["task_count"] = count
        return projects
    finally:
        conn.close()


def get_sprint_tasks(sprint_id: str) -> list[dict]:
    """Tasks assigned to a sprint, grouped-friendly (by status then priority)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, status, priority, assignee, project_id, completed_at "
            "FROM tasks WHERE sprint_id = ? ORDER BY status, priority DESC",
            (sprint_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_project(project_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- the project lifecycle ------------------------------------------------
#
# The declared vocabulary, and the whole of it. m02_spine's docstring also named
# `delivering`; nothing ever wrote it, and a status no verb can produce is a
# value every reader has to branch on for nothing — so it is deliberately
# unshipped (decisions log). A project is planned, being worked, delivered, or
# archived.
PROJECT_STATUSES = ("planned", "active", "delivered", "archived")

# What a new project is: nothing has started yet. It used to be NULL, which is
# how 13 of 18 live rows came to carry no lifecycle at all (see m04).
DEFAULT_PROJECT_STATUS = "planned"


def _iso_now() -> str:
    """`projects.delivered_at` is a TEXT column (m02_spine), so it gets an
    ISO-8601 local timestamp with its offset — the same format `crm._iso_now`
    writes, because they write the same column."""
    import datetime as _dtm
    return _dtm.datetime.now().astimezone().isoformat(timespec="seconds")


def set_project_status(conn, project_id: str, status: str, *, via: str) -> Optional[dict]:
    """THE writer for `projects.status` (ruling 8) — single writer, caller's txn.

    It **receives** the connection instead of opening one. That is the entire
    point: a status change is almost never a fact on its own — it is the
    consequence of something else (a deal delivered, a project marked done, a
    cadence step) that is already mid-transaction. A writer with its own
    connection would commit the status change independently, so a caller that
    rolled back would leave a project claiming a delivery that never happened.
    It therefore does NOT commit either; the caller's commit is the one that
    counts.

    `via` names the caller (`api_patch`, `deliver_deal`, …). There is no
    `project_events` table in this schema — the audit tables are task_events /
    deal_events / initiative_events — so it is echoed in the result (and thus in
    the API response) rather than logged; when a project audit row does exist,
    this is the one place that has to learn to write it.

    Raises `ValueError` on a status outside `PROJECT_STATUSES` — fail closed,
    inside the caller's transaction, where an error dict could be ignored and
    committed. Returns None (writing nothing) when the project does not exist,
    so the caller can 404 rather than invent a row.

    `delivered_at` is stamped on →`delivered` and never rewritten: COALESCE, so
    a re-delivery keeps the day it was first delivered, and leaving `delivered`
    never clears the date (it is history, not current state). `archived_at`
    stays `archive_project`'s to write — this writer owns `status` and
    `delivered_at`, nothing else.
    """
    if status not in PROJECT_STATUSES:
        raise ValueError(
            f"unknown project status {status!r} — expected one of {list(PROJECT_STATUSES)}")
    row = conn.execute(
        "SELECT status, delivered_at FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    previous = row["status"] if isinstance(row, sqlite3.Row) else row[0]
    if status == "delivered":
        conn.execute(
            "UPDATE projects SET status = ?, delivered_at = COALESCE(delivered_at, ?) "
            "WHERE id = ?", (status, _iso_now(), project_id))
    else:
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
    delivered_at = conn.execute(
        "SELECT delivered_at FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
    return {"project_id": project_id, "status": status, "previous": previous,
            "delivered_at": delivered_at, "via": via, "changed": previous != status}


def update_project_status(project_id: str, status: str, *, via: str) -> Optional[dict]:
    """Transaction shell for callers that have no connection of their own (the
    PATCH route). All the SQL still lives in `set_project_status` — this only
    opens, commits and closes, so there is still exactly one writer."""
    conn = get_conn()
    try:
        res = set_project_status(conn, project_id, status, via=via)
        conn.commit()
        return res
    finally:
        conn.close()


def create_project(name: str, slug: str, description: str = "", color: str = "#3b82f6",
                   icon: str = "📦", repo_path: str | None = None) -> dict:
    conn = get_conn()
    try:
        pid = _gen_id("proj")
        now = int(time.time())
        # `status` is written at INSERT, not left to a later verb: a project
        # created without one is exactly how the column came to be NULL on most
        # of the table (m04 backfills the ones that predate this line).
        conn.execute(
            "INSERT INTO projects (id, slug, name, description, color, icon, created_at, status, repo_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, slug, name, description, color, icon, now, DEFAULT_PROJECT_STATUS, repo_path)
        )
        conn.commit()
        # NOTE: this "status" is the RESULT of the call, not the project's
        # lifecycle — a pre-existing wart in the response shape, left alone here
        # (its callers switch on it) and disambiguated by `project_status`.
        return {"id": pid, "slug": slug, "name": name, "status": "created",
                "project_status": DEFAULT_PROJECT_STATUS, "repo_path": repo_path}
    finally:
        conn.close()


MAX_WEEKLY_HOURS = 40.0

# Qué es el proyecto (decide si consume horas de ENTREGA) y qué tan comprometido
# está (decide el orden). Ambos cerrados a un conjunto: un valor libre aquí es
# cómo `tier`/`health` acabaron puestos en 5 de 23 filas y sin lector.
PROJECT_KINDS = ("product", "sales", "personal", "system", "self")
PROJECT_TIERS = ("commit", "bet", "explore")


def _coerce_weekly_hours(val):
    """`weekly_hours` es un presupuesto semanal declarado, y la validación vive
    en el ÚNICO escritor para que el MCP y el PATCH no puedan divergir.

    Devuelve (float, None) si es válido, o (None, mensaje) si no. Acepta 0
    —que significa *aparcado*, la manera reversible de soltar un proyecto— y
    rechaza el negativo y cualquier cosa por encima de una semana entera: 40 h
    ya está por encima del bloque de entrega declarado, así que un número mayor
    es un dedazo, no un compromiso. Un booleano NO es un número aquí aunque
    Python lo trate como tal."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None, "weekly_hours must be a number (0 = parked, null = unsized)"
    val = float(val)
    if val < 0:
        return None, "weekly_hours must be >= 0"
    if val > MAX_WEEKLY_HOURS:
        return None, f"weekly_hours must be <= {MAX_WEEKLY_HOURS:g}"
    return val, None


def update_project(project_id: str, name: str = None, slug: str = None,
                   description: str = None, color: str = None,
                   icon: str = None, weekly_hours=None, kind: str = None,
                   tier: str = None) -> dict:
    """Verb-audit gap: projects were create-only — no way to rename, recolor,
    or fix a slug. All fields optional (PATCH semantics); only supplied fields
    are written. Refuses to archive (that's archive_project's job)."""
    if kind is not None and kind not in PROJECT_KINDS:
        return {"status": "error",
                "error": f"kind must be one of {'/'.join(PROJECT_KINDS)}"}
    # `tier` necesita distinguir "no lo mandaste" de "quítalo", y el patrón
    # `is not None` del resto de campos no puede: bajo él, NULL sería
    # indistinguible de ausente y la jerarquía sólo podría subir, nunca
    # limpiarse. La cadena vacía es el "quítalo" explícito.
    clear_tier = tier == ""
    if clear_tier:
        tier = None
    elif tier is not None and tier not in PROJECT_TIERS:
        return {"status": "error",
                "error": f"tier must be one of {'/'.join(PROJECT_TIERS)}"}
    if weekly_hours is not None:
        weekly_hours, err = _coerce_weekly_hours(weekly_hours)
        if err:
            return {"status": "error", "error": err}
    sets, params = [], []
    for col, val in (("name", name), ("slug", slug),
                     ("description", description), ("color", color),
                     ("icon", icon), ("weekly_hours", weekly_hours),
                     ("kind", kind)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    # La marca viaja PEGADA al número, en el único escritor. Si existiera un
    # verbo para fecharla por separado, se podría fingir el ritual semanal sin
    # revisar un solo proyecto — y el panel diría "declarado esta semana" sobre
    # un reparto que nadie miró.
    if weekly_hours is not None:
        sets.append("weekly_hours_set_at = ?")
        params.append(int(time.time()))
    if tier is not None or clear_tier:
        sets.append("tier = ?")
        params.append(tier)
    if not sets:
        return {"status": "error",
                "error": "nothing to update (name/slug/description/color/icon/weekly_hours)"}
    conn = get_conn()
    try:
        cur = conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ? AND archived_at IS NULL",
            (*params, project_id))
        if cur.rowcount == 0:
            exists = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                                  (project_id,)).fetchone()
            if exists is None:
                return {"status": "error", "error": "project not found"}
            return {"status": "error", "error": "project is archived — unarchive before editing"}
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return {"status": "updated", "project": dict(row)}
    finally:
        conn.close()


def assign_task_project(task_id: str, project_id: str) -> dict:
    conn = get_conn()
    try:
        conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?", (project_id, task_id))
        conn.commit()
        return {"task_id": task_id, "project_id": project_id, "status": "assigned"}
    finally:
        conn.close()


# The dashboard's board columns. `hermes kanban` verbs only transition
# cleanly between hermes-native statuses (and `complete` even returns exit 0
# while silently refusing a foreign status like our synthetic `in_progress`).
# Human tasks are never driven by the hermes dispatcher, so the dashboard owns
# their status directly via this sidecar write — the same sanctioned exception
# already used for project/sprint assignment. Timestamps mirror the CLI's:
# started_at on →in_progress, completed_at on →done.
# Phase 3 (item 2): `review` is a REAL status — a finished-but-unaccepted
# completion (what used to be the invisible `done AND reviewed_at IS NULL`
# predicate). `done` now always means ACCEPTED.
# Phase 1 (m02_spine): `rejected` and `cancelled` are REAL statuses too — 14 live
# tasks carry them and this gate refused to move any of them, so the only way out
# of either was a hand-written SQL update. `rejected` already had a one-way ramp
# in (reject_task) and a resurrect path out (→backlog clears rejection_reason);
# what it lacked was a way for the board to name where a card already is.
_BOARD_STATUSES = {"backlog", "ready", "in_progress", "blocked", "review", "done",
                   "rejected", "cancelled"}


def _log_event(conn, task_id: str, kind: str, payload: dict) -> None:
    """Append a row to the shared task_events audit log (best-effort)."""
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, kind, json.dumps(payload), int(time.time())),
        )
    except Exception:
        pass


def set_task_assignee(task_id: str, assignee: str) -> dict:
    """Reassign a task via a sidecar write (reliable for any agent/human name,
    unlike the CLI which validates profiles). Logs a dispatched/reclaimed event
    so the handoff is in the audit trail. This is the manual stand-in for the
    MCP claim/dispatch loop (PRD Phase 3)."""
    conn = get_conn()
    try:
        prior = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        from_assignee = prior["assignee"] if prior else None
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id))
        human = {"ricardo", "user"}
        kind = "reclaimed" if assignee in human else ("dispatched" if from_assignee in human else "reassigned")
        _log_event(conn, task_id, kind, {"from": from_assignee, "to": assignee, "via": "dashboard"})
        conn.commit()
        return {"task_id": task_id, "assignee": assignee, "from": from_assignee, "result": kind}
    finally:
        conn.close()


def accept_task(task_id: str) -> dict:
    """Operator accepts an agent's completion (the human gate, PRD §7). Stamps
    reviewed_at so the task leaves the Inbox and settles into the Fleet's Done;
    logs an `accepted` event to the audit trail. Idempotent — accepting an
    already-reviewed task is a no-op that still returns the timestamp."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT status, reviewed_at, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        already = row["reviewed_at"]
        now = int(time.time())
        if already and row["status"] == "done":
            return {"task_id": task_id, "reviewed_at": already, "result": "already_reviewed"}
        # Phase 3 (item 2): accepting exits the `review` state — done = accepted.
        # Exactly-once (2026-08-09, arXiv:2608.03836 shape, observed live as
        # t_c7ab4210's double `accepted` event): the WHERE clause is a
        # compare-and-swap so concurrent accepts that both read
        # reviewed_at IS NULL produce ONE update/event; a reviewed task in a
        # non-done state (e.g. re-accept after reject) still legitimately lands.
        cur = conn.execute(
            "UPDATE tasks SET status = 'done', reviewed_at = COALESCE(reviewed_at, ?), "
            "completed_at = COALESCE(completed_at, ?) "
            "WHERE id = ? AND (reviewed_at IS NULL OR status != 'done')",
            (now, now, task_id),
        )
        won = cur.rowcount == 1
        if won:
            _log_event(conn, task_id, "accepted", {"from": row["status"], "via": "dashboard"})
        conn.commit()
        if not won:
            fresh = conn.execute(
                "SELECT reviewed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return {"task_id": task_id,
                    "reviewed_at": (fresh["reviewed_at"] if fresh else None) or already or now,
                    "result": "already_reviewed"}
        agent_task = row["assignee"] and row["assignee"] not in ("ricardo", "user")
    finally:
        conn.close()
    # Phase 4 (item 1): a manual operator accept of an AGENT task IS the human
    # verification — record it so "no done agent task without a verification
    # ledger row" (the ratchet) holds without forcing a contract run first.
    if agent_task:
        try:
            from . import governance, orchestration
            if governance.verification_row(task_id) is None:
                orchestration.append_ledger(
                    task_id, "operator manual accept (the human gate reviewed this work)",
                    status="passed", agent="ricardo", role="verification", passed=True)
        except Exception:
            pass
    return {"task_id": task_id, "reviewed_at": already or now, "result": "accepted"}


def reject_task(task_id: str, reason: str = "") -> dict:
    """Operator rejects a task — the negative counterpart of accept_task. Sets
    status='rejected' (a dashboard-owned terminal state, not a hermes-native or
    board status, so it's written directly via this sidecar), stores the optional
    reason, and logs a `rejected` event to the audit trail. Idempotent enough:
    re-rejecting just overwrites the reason and re-logs."""
    reason = (reason or "").strip()
    conn = get_conn()
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        from_status = row["status"]
        conn.execute(
            "UPDATE tasks SET status = 'rejected', rejection_reason = ? WHERE id = ?",
            (reason or None, task_id),
        )
        _log_event(conn, task_id, "rejected",
                   {"from": from_status, "reason": reason, "via": "dashboard"})
        conn.commit()
        return {"task_id": task_id, "status": "rejected", "from": from_status,
                "reason": reason, "result": "rejected"}
    finally:
        conn.close()


def set_task_status(task_id: str, status: str) -> dict:
    """Authoritatively set a human task's board status via a sidecar write.
    Stamps started_at on →in_progress and completed_at on →done, and clears the
    matching stamp when moving back out, so the board and any CLI reader agree.
    Appends a `status_changed` event so the transition is in the audit log."""
    if status not in _BOARD_STATUSES:
        return {"status": "error", "error": f"unknown board status '{status}'"}
    conn = get_conn()
    try:
        prior = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "task not found"}
        from_status = prior["status"]
        now = int(time.time())
        if status == "in_progress":
            conn.execute(
                "UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?), completed_at = NULL WHERE id = ?",
                (status, now, task_id),
            )
        elif status == "done":
            # A dashboard →done is operator-initiated, so it counts as reviewed
            # (the human is the one moving it). Agent completions arrive via the
            # hermes CLI, NOT this path, so they land with reviewed_at NULL and
            # surface in the operator's Inbox for acceptance.
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = COALESCE(completed_at, ?), "
                "reviewed_at = COALESCE(reviewed_at, ?) WHERE id = ?",
                (status, now, now, task_id),
            )
        elif status == "review":
            # Finished, awaiting the operator's accept: completed_at stamps (the
            # work IS finished) but reviewed_at stays NULL until accept_task.
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = COALESCE(completed_at, ?), "
                "reviewed_at = NULL WHERE id = ?",
                (status, now, task_id),
            )
        elif status == "backlog":
            # rejected → backlog is the sanctioned RESURRECT path (Phase 3
            # decision: rejected is resurrectable, not terminal). Clear the
            # rejection reason so the revived card starts clean.
            conn.execute(
                "UPDATE tasks SET status = ?, started_at = NULL, completed_at = NULL, "
                "rejection_reason = NULL WHERE id = ?",
                (status, task_id),
            )
        else:  # ready, blocked, rejected, cancelled
            # rejected/cancelled clear completed_at on purpose: the work did not
            # complete, and leaving the stamp would count it as delivery.
            # rejection_reason is reject_task's to write — this path only names
            # where the card is.
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = NULL WHERE id = ?",
                (status, task_id),
            )
        # --- the cadence loop closure (journey fase 1, step 5) ---------------
        # A commercial card moving to `done` means the touch HAPPENED, and until
        # now that fact died on the board: the nurture step stayed `pending`
        # forever, `sent_at` was never written (so compliance could not be
        # nonzero), touch_count never moved and next_touch_date kept whatever
        # flat +7d had been stamped on it.
        #
        # SAME TRANSACTION as the status write, by ruling 8's shape — the helper
        # receives this connection and never opens one. A commit that landed the
        # `done` but rolled back the step would leave a card the operator
        # finished and a cadence that still believes it is pending, i.e. the
        # card gets minted again tomorrow.
        #
        # Best-effort by design: a task move must never fail because the
        # commercial side-effect could not be written. A missed closure is
        # repaired by the next `reconcile` (the card is settled, so its slot is
        # free and the step's precondition check closes it).
        cadence_closed = None
        if status == "done":
            try:
                from . import cadence
                cadence_closed = cadence.complete_step_for_task(conn, task_id)
            except Exception:
                cadence_closed = None
        if from_status == "rejected" and status == "backlog":
            _log_event(conn, task_id, "resurrected", {"via": "dashboard"})
        if from_status != status:
            _log_event(conn, task_id, "status_changed",
                       {"from": from_status, "to": status, "via": "dashboard"})
        conn.commit()
        agent_task = None
        if status == "done":
            row2 = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
            agent_task = row2 and row2["assignee"] and row2["assignee"] not in ("ricardo", "user")
    finally:
        conn.close()
    # Phase-4 coherence: an operator →done on an AGENT task is a manual accept,
    # so it records the human gate as the verification row exactly like
    # accept_task — otherwise this path leaves 'done' unverified and the
    # verified-done ratchet trips (live incident: Hermes's bulk-accept).
    if status == "done" and agent_task:
        try:
            from . import governance, orchestration
            if governance.verification_row(task_id) is None:
                orchestration.append_ledger(
                    task_id, "operator moved to done (human gate via status write)",
                    status="passed", agent="ricardo", role="verification", passed=True)
        except Exception:
            pass
    # P0-7 (§6): a task entering in_progress auto-commits to the active cycle,
    # server-side. Best-effort + outside the status txn (its own connection), so
    # a commit hiccup never blocks the status move itself.
    committed = None
    if status == "in_progress":
        try:
            committed = auto_commit_to_active_cycle(task_id)
        except Exception:
            committed = None
    out = {"task_id": task_id, "status": status, "result": "updated", "from": from_status}
    if committed:
        out["auto_committed_cycle"] = committed
    if cadence_closed:
        out["cadence"] = cadence_closed
    return out


def auto_commit_to_active_cycle(task_id: str) -> Optional[str]:
    """P0-7 (§6, Fable R5): commit a task to the active cycle when it enters
    in_progress — UNLESS it opted out (auto_cycle=0) or is already committed.

    Server-side because fleet agents can't self-commit to a cycle (privileged),
    so the server closes the "agent work never reaches the cycle" hole. Default
    ON. Called AFTER the status write has committed (opens its own connection),
    so it never nests a transaction; idempotent — an already-committed task is
    left alone, and there being no active cycle is a no-op. Returns the cycle id
    it committed to, or None. The manual ＋Cycle affordance is unaffected."""
    conn = get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        sel = "sprint_id" + (", auto_cycle" if "auto_cycle" in cols else "")
        row = conn.execute(f"SELECT {sel} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["sprint_id"]:
            return None                      # gone, or already in a cycle
        auto = row["auto_cycle"] if ("auto_cycle" in cols and row["auto_cycle"] is not None) else 1
        if not auto:
            return None                      # opted out (auto_cycle = 0)
    finally:
        conn.close()
    active = get_active_sprint()
    if not active:
        return None                          # nothing to commit to yet
    res = assign_task_sprint(task_id, active["id"])
    return active["id"] if res.get("status") != "error" else None


def set_auto_cycle(task_id: str, enabled: bool) -> dict:
    """Per-task opt-out of P0-7 auto-commit (auto_cycle 1=on / 0=off). The setter
    behind the PATCH /tasks/{id} `auto_cycle` field."""
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE tasks SET auto_cycle = ? WHERE id = ?",
                           (1 if enabled else 0, task_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"status": "error", "error": "task not found"}
        return {"task_id": task_id, "auto_cycle": bool(enabled)}
    finally:
        conn.close()


def set_scheduled_week(task_id: str, week: Optional[str]) -> dict:
    """Backlog Phase 1: set (or clear) a task's planned ISO-week bucket
    (tasks.scheduled_week, e.g. "2026-W28"). `week=None` clears it → the task
    falls back to the Backlog lens (no sprint, no scheduled week). Audited with a
    task_updated event so a schedule change is in the trail.

    SPRINT SYNC: setting a scheduled_week to a FUTURE week (different from the
    active cycle's week) automatically pulls the task out of the current sprint
    via assign_task_sprint(task_id, None) — stamps the ledger row 'dropped' and
    clears sprint_id. This prevents the dual-store drift where a task appears
    in both the W28 cycle board (via task_sprints) AND the W29 scheduled bucket.
    Clearing scheduled_week does NOT re-commit (the task stays where it is)."""
    conn = get_conn()
    try:
        prior = conn.execute(
            "SELECT scheduled_week, sprint_id FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "task not found"}
        if prior["scheduled_week"] == week:
            return {"status": "unchanged", "task_id": task_id, "scheduled_week": week}
        conn.execute("UPDATE tasks SET scheduled_week = ? WHERE id = ?",
                     (week, task_id))
        _log_event(conn, task_id, "task_updated",
                   {"changed": {"scheduled_week": {"from": prior["scheduled_week"], "to": week}},
                    "via": "set_scheduled_week"})
        conn.commit()
    finally:
        conn.close()

    # Sprint sync: if the task is currently in a sprint and the new scheduled
    # week is different from the active cycle's week, pull it out of the sprint
    # (stamps the ledger row 'dropped', clears sprint_id). This prevents the
    # duplicate-appearance bug (task showing in both cycle board + scheduled list).
    if prior["sprint_id"] and week:
        active = get_active_sprint()
        if active:
            active_week = _sprint_to_iso_week(active)
            if active_week and active_week != week:
                assign_task_sprint(task_id, None)
                _log_sprint_sync(task_id, prior["sprint_id"], week)

    return {"status": "updated", "task_id": task_id, "scheduled_week": week}


def _sprint_to_iso_week(sprint: dict) -> Optional[str]:
    """Derive the ISO week string (e.g. "2026-W28") from a sprint's start_date."""
    if not sprint or not sprint.get("start_date"):
        return None
    try:
        from datetime import date
        d = date.fromtimestamp(sprint["start_date"])
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except Exception:
        return None


def _log_sprint_sync(task_id: str, old_sprint_id: str, new_week: str):
    """Log the automatic sprint pull triggered by scheduled_week change."""
    conn = get_conn()
    try:
        _log_event(conn, task_id, "sprint_synced",
                   {"from_sprint": old_sprint_id, "to_week": new_week,
                    "via": "set_scheduled_week", "reason": "scheduled_week changed"})
        conn.commit()
    finally:
        conn.close()


def sprint_ledger_drift() -> dict:
    """P2-1 (§2.3): consistency check for the dual store — tasks.sprint_id (HEAD)
    ⇔ task_sprints (the commit ledger). Both directions:

      forward  — a task whose sprint_id is set but has NO matching ledger row
                 at all (neither open nor closed) for that sprint. A task with
                 a closed (outcome IS NOT NULL) row is NOT an orphan — it was
                 committed and delivered, which is the normal lifecycle.
      reverse  — an OPEN ledger row whose task no longer points at that sprint
                 (a commit that was moved/pulled without stamping the old row) —
                 the 'vice versa' direction.

    Returns {ok, forward_orphans, reverse_orphans, drift, samples}. ok=True and
    drift=0 is a clean store. Read-only; cheap (two correlated NOT EXISTS)."""
    conn = get_conn()
    try:
        forward = [r[0] for r in conn.execute(
            "SELECT t.id FROM tasks t WHERE t.sprint_id IS NOT NULL AND NOT EXISTS ("
            " SELECT 1 FROM task_sprints ts WHERE ts.task_id = t.id "
            " AND ts.sprint_id = t.sprint_id)").fetchall()]
        reverse = [{"task_id": r[0], "sprint_id": r[1]} for r in conn.execute(
            "SELECT ts.task_id, ts.sprint_id FROM task_sprints ts WHERE ts.outcome IS NULL "
            "AND NOT EXISTS ("
            " SELECT 1 FROM tasks t WHERE t.id = ts.task_id AND t.sprint_id = ts.sprint_id)"
        ).fetchall()]
        return {
            "ok": not forward and not reverse,
            "forward_orphans": len(forward),
            "reverse_orphans": len(reverse),
            "drift": len(forward) + len(reverse),
            "sample_forward": forward[:5],
            "sample_reverse": reverse[:5],
        }
    finally:
        conn.close()


def reconcile_sprint_ledger() -> dict:
    """P2-1 repair: fix BOTH forward and reverse orphans in the dual store.
    Forward orphans: task has sprint_id but no matching ledger row → insert
    a 'delivered' row. Reverse orphans: open ledger row (outcome IS NULL)
    whose task no longer points at that sprint → close it with outcome='carried'
    (the task moved to a different sprint). Idempotent — re-running on a
    clean store is a no-op. Returns {forward_repaired, reverse_repaired,
    samples, drift_before, drift_after}. Read-then-write; safe at any time."""
    conn = get_conn()
    try:
        now = int(time.time())
        # --- Forward orphans: sprint_id set but no ledger row for that sprint ---
        forward = conn.execute(
            "SELECT t.id, t.sprint_id, t.completed_at FROM tasks t "
            "WHERE t.sprint_id IS NOT NULL AND NOT EXISTS ("
            " SELECT 1 FROM task_sprints ts WHERE ts.task_id = t.id "
            " AND ts.sprint_id = t.sprint_id)"
        ).fetchall()
        forward_repaired = []
        for row in forward:
            stamp = row["completed_at"] if row["completed_at"] else now
            # A forward orphan that is NOT done is an open commit (outcome=NULL),
            # not a delivered one.  Only stamp "delivered" for completed tasks.
            outcome = "delivered" if row["completed_at"] else None
            conn.execute(
                "INSERT INTO task_sprints (task_id, sprint_id, committed_at, outcome) "
                "VALUES (?, ?, ?, ?)",
                (row["id"], row["sprint_id"], stamp, outcome),
            )
            forward_repaired.append(row["id"])
        # --- Reverse orphans: open ledger row whose task points elsewhere ---
        reverse = conn.execute(
            "SELECT ts.task_id, ts.sprint_id FROM task_sprints ts "
            "WHERE ts.outcome IS NULL AND NOT EXISTS ("
            " SELECT 1 FROM tasks t WHERE t.id = ts.task_id "
            " AND t.sprint_id = ts.sprint_id)"
        ).fetchall()
        reverse_repaired = []
        for row in reverse:
            conn.execute(
                "UPDATE task_sprints SET outcome = 'carried' "
                "WHERE task_id = ? AND sprint_id = ? AND outcome IS NULL",
                (row["task_id"], row["sprint_id"]),
            )
            reverse_repaired.append(row["task_id"])
        conn.commit()
        # Verify — check both directions
        remaining_forward = conn.execute(
            "SELECT t.id FROM tasks t WHERE t.sprint_id IS NOT NULL AND NOT EXISTS ("
            " SELECT 1 FROM task_sprints ts WHERE ts.task_id = t.id "
            " AND ts.sprint_id = t.sprint_id)"
        ).fetchall()
        remaining_reverse = conn.execute(
            "SELECT ts.task_id FROM task_sprints ts WHERE ts.outcome IS NULL "
            "AND NOT EXISTS ("
            " SELECT 1 FROM tasks t WHERE t.id = ts.task_id "
            " AND t.sprint_id = ts.sprint_id)"
        ).fetchall()
        total_before = len(forward) + len(reverse)
        total_after = len(remaining_forward) + len(remaining_reverse)
        return {
            "forward_repaired": len(forward_repaired),
            "reverse_repaired": len(reverse_repaired),
            "forward_samples": forward_repaired[:5],
            "reverse_samples": reverse_repaired[:5],
            "drift_before": total_before,
            "drift_after": total_after,
        }
    finally:
        conn.close()


def update_task_fields(task_id: str, title: str = None, body: str = None,
                       priority: int = None, due_date: str = None,
                       project_id: str = None) -> dict:
    """Verb-audit gap: the card itself was uneditable — status could move but
    title/body/priority/due_date were frozen at create. Sidecar write with a
    task_updated event carrying exactly what changed (scope changes mid-task
    become auditable instead of buried in comments). `project_id` closes the
    triage gap — re-homing a task no longer needs raw SQL (FK-validated)."""
    due_supplied = due_date is not None
    normalized_due, due_error = normalize_due_date(due_date)
    if due_error:
        return {"status": "error", "error": due_error}
    conn = get_conn()
    try:
        prior = conn.execute(
            "SELECT title, body, priority, due_date, project_id FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "task not found"}
        if project_id is not None and project_id != prior["project_id"]:
            if not conn.execute("SELECT 1 FROM projects WHERE id = ?",
                                (project_id,)).fetchone():
                return {"status": "error", "error": f"project '{project_id}' not found"}
        sets, params, changed = [], [], {}
        for col, val in (("title", title), ("body", body),
                         ("priority", priority),
                         ("project_id", project_id)):
            if val is not None and val != prior[col]:
                sets.append(f"{col} = ?")
                params.append(val)
                changed[col] = {"from": prior[col] if col != "body" else f"{len(prior[col] or '')}ch",
                                "to": val if col != "body" else f"{len(val)}ch"}
        if due_supplied and normalized_due != prior["due_date"]:
            sets.append("due_date = ?")
            params.append(normalized_due)
            changed["due_date"] = {"from": prior["due_date"], "to": normalized_due}
        if not sets:
            return {"status": "unchanged", "task_id": task_id}
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*params, task_id))
        _log_event(conn, task_id, "task_updated", {"changed": changed, "via": "update_task"})
        conn.commit()
        return {"task_id": task_id, "status": "updated", "changed": list(changed)}
    finally:
        conn.close()


def bulk_accept(task_ids: list) -> dict:
    """The SANCTIONED bulk accept (complement to the verb audit — today's
    incident happened precisely because no such path existed and the raw
    write skipped the verification row). Routes every id through accept_task,
    so each accept stamps done+reviewed AND records the human-gate
    verification row. Returns per-id results; never raw SQL."""
    results, ok = {}, 0
    for tid in task_ids or []:
        r = accept_task(tid)
        results[tid] = r.get("result") or r.get("error")
        if r.get("result") in ("accepted", "already_reviewed"):
            ok += 1
    return {"status": "ok", "accepted": ok, "total": len(task_ids or []), "results": results}


def delete_task(task_id: str) -> dict:
    """Verb-audit gap: mistakes/duplicates/test tasks accumulated forever.
    Guarded HARD delete: refuses accepted work (done+reviewed is history —
    archive, don't erase). The task's events are KEPT (append-only spine; a
    final task_deleted event records the tombstone), sidecar rows cleaned."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT status, reviewed_at, title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        if row["status"] == "done" and row["reviewed_at"]:
            return {"status": "error",
                    "error": "refusing to delete accepted work — it's history "
                             "(the graveyard is a view, not a trash can)"}
        _log_event(conn, task_id, "task_deleted",
                   {"title": row["title"], "was_status": row["status"], "via": "delete_task"})
        conn.execute("DELETE FROM task_sprints WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
                     (task_id, task_id))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return {"status": "deleted", "task_id": task_id}
    finally:
        conn.close()


def archive_project(project_id: str) -> dict:
    """Verb-audit gap: dead projects accumulated. Stamps archived_at (the
    list already filters archived); open tasks are surfaced, not blocked —
    the operator sees what's being parked."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (int(time.time()), project_id))
        if cur.rowcount == 0:
            exists = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                                  (project_id,)).fetchone()
            conn.commit()
            if exists is None:
                return {"status": "error", "error": "project not found"}
            return {"status": "already_archived", "project_id": project_id}
        open_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status NOT IN ('done', 'rejected')",
            (project_id,)).fetchone()[0]
        conn.commit()
        return {"status": "archived", "project_id": project_id, "open_tasks": open_tasks}
    finally:
        conn.close()


# --- Sprints / Cycles ---
#
# Phase 3 (item 5) — the cycle model:
#   • A CYCLE is a weekly, cross-project (project_id nullable), auto-rolling
#     timebox — an orthogonal selection over the spine, never a hierarchy rung.
#   • `task_sprints` is repurposed as the append-only COMMIT-LEDGER:
#     {task_id, sprint_id, committed_at, outcome ∈ delivered|carried|dropped}.
#     This fixes the structurally-always-100% delivery_rate (membership used to
#     be overwritten, so a closed sprint only ever contained its survivors) and
#     makes closed cycles reconstructable.
#   • Velocity = ACCEPTED HUMAN-INTENT tasks (a VIEW, cycle_velocity): committed
#     tasks that are done+reviewed with origin ∈ (ricardo, hermes) — agent
#     bursts (origin agent/decomposed) can't inflate the number.
#   • Re-commit gate (Shape Up): closing a cycle stamps unfinished work
#     `carried`, but the next cycle starts EMPTY — carry-overs compete fresh at
#     the standup, they never auto-roll.

_HUMAN_INTENT_ORIGINS = ("ricardo", "hermes")


def ensure_cycle_schema() -> None:
    """Idempotent Phase-3 cycle migration, safe at startup:
    1. sprints.project_id NOT NULL → nullable (cross-project cycles). SQLite
       can't ALTER a constraint, so rebuild the table once (FK off, copy, swap).
    2. task_sprints += committed_at / outcome (the commit-ledger columns).
    3. cycle_velocity VIEW."""
    conn = get_conn()
    try:
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(sprints)").fetchall()}
        if notnull.get("project_id") == 1:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript("""
                BEGIN;
                CREATE TABLE sprints_new (
                    id          TEXT PRIMARY KEY,
                    project_id  TEXT REFERENCES projects(id),
                    name        TEXT NOT NULL,
                    goal        TEXT,
                    start_date  INTEGER NOT NULL,
                    end_date    INTEGER NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'planning',
                    closed_at   INTEGER,
                    created_at  INTEGER NOT NULL
                );
                INSERT INTO sprints_new SELECT id, project_id, name, goal, start_date,
                    end_date, status, closed_at, created_at FROM sprints;
                DROP TABLE sprints;
                ALTER TABLE sprints_new RENAME TO sprints;
                COMMIT;
            """)
            conn.execute("PRAGMA foreign_keys = ON")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(task_sprints)").fetchall()]
        if "committed_at" not in cols:
            conn.execute("ALTER TABLE task_sprints ADD COLUMN committed_at INTEGER")
            # Legacy rows predate the ledger; stamp them so every row is dated.
            conn.execute("UPDATE task_sprints SET committed_at = ? WHERE committed_at IS NULL",
                         (int(time.time()),))
        if "outcome" not in cols:
            conn.execute("ALTER TABLE task_sprints ADD COLUMN outcome TEXT")
        # Manual drag-reorder position on the Cycle board (per-cycle; a task is in
        # one cycle at a time). NULL until a card is dragged — such cards sort last
        # by the board query's COALESCE, then by priority/created.
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "board_order" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN board_order INTEGER")
        # P0-7 (§6): opt-out flag for server-side cycle auto-commit. Default 1
        # (auto-commit ON); set 0 to opt a task out. Existing rows inherit the
        # default → every task auto-commits until explicitly excluded.
        if "auto_cycle" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN auto_cycle INTEGER DEFAULT 1")
        # Finish-Sprint stamps closed-out work (done/rejected) with archived_at so it
        # drops off the active board while the completed sprint keeps it as history.
        if "archived_at" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN archived_at INTEGER")
        conn.execute("""
            CREATE VIEW IF NOT EXISTS cycle_velocity AS
            SELECT s.id   AS sprint_id,
                   s.name AS name,
                   s.status AS status,
                   s.start_date AS start_date,
                   s.end_date   AS end_date,
                   COUNT(ts.task_id) AS committed,
                   SUM(CASE WHEN t.status = 'done' AND t.reviewed_at IS NOT NULL
                            AND t.origin IN ('ricardo', 'hermes') THEN 1 ELSE 0 END) AS velocity
            FROM sprints s
            LEFT JOIN task_sprints ts ON ts.sprint_id = s.id
            LEFT JOIN tasks t ON t.id = ts.task_id
            GROUP BY s.id
        """)
        conn.commit()
    finally:
        conn.close()


def list_sprints(project_id: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    try:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM sprints WHERE project_id = ? ORDER BY start_date DESC", (project_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sprints ORDER BY start_date DESC").fetchall()
        sprints = [dict(r) for r in rows]
        # Add task counts
        for s in sprints:
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE sprint_id = ?", (s["id"],)).fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE sprint_id = ? AND status = 'done'", (s["id"],)
            ).fetchone()[0]
            s["task_count"] = count
            s["done_count"] = done
            s["progress_pct"] = int(done / count * 100) if count > 0 else 0
        return sprints
    finally:
        conn.close()


def get_sprint(sprint_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_sprint(project_id: str, name: str, goal: str = "",
                  start_date: int = None, end_date: int = None,
                  duration_weeks: int = 2) -> dict:
    conn = get_conn()
    try:
        sid = _gen_id("spr")
        now = int(time.time())
        if not start_date:
            start_date = now
        if not end_date:
            end_date = now + (duration_weeks * 7 * 24 * 3600)
        conn.execute(
            "INSERT INTO sprints (id, project_id, name, goal, start_date, end_date, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, project_id, name, goal, start_date, end_date, "planning", now)
        )
        conn.commit()
        return {"id": sid, "name": name, "status": "created"}
    finally:
        conn.close()


def create_cycle(name: Optional[str] = None, goal: str = "",
                 start_date: Optional[int] = None,
                 end_date: Optional[int] = None) -> dict:
    """Create a weekly, cross-project cycle (project_id NULL — a timebox over
    the whole spine, not one project). Named after the ISO week by default."""
    import datetime as _dtm
    now = int(time.time())
    # LOCKED: Mon→Sun. If no explicit window, snap to the week containing `now`
    # (or the given start). A caller may still pass an explicit start/end.
    if start_date and end_date:
        start, end = start_date, end_date
    else:
        start, end = _week_window(start_date or now)
    if not name:
        iso = _dtm.date.fromtimestamp(start).isocalendar()
        name = f"Cycle {iso[0]}-W{iso[1]:02d}"
    conn = get_conn()
    try:
        # Dedup — one cycle per week. If a non-completed cycle already starts in
        # this week's window, reuse it (makes planWeek/roll idempotent; a
        # double-plan or a pre-planned week the roll later adopts can't fork).
        ws, we = _week_window(start + 3 * 86400)
        prior = conn.execute(
            "SELECT id, name, start_date, end_date, status FROM sprints "
            "WHERE project_id IS NULL AND start_date >= ? AND start_date <= ? "
            "AND status != 'completed' "
            "ORDER BY start_date LIMIT 1", (ws, we)).fetchone()
        if prior:
            out = {"id": prior["id"], "name": prior["name"], "project_id": None,
                   "status": prior["status"], "start_date": prior["start_date"],
                   "end_date": prior["end_date"], "reused": True}
        else:
            sid = _gen_id("cyc")
            conn.execute(
                "INSERT INTO sprints (id, project_id, name, goal, start_date, end_date, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, None, name, goal, start, end, "planning", now),
            )
            conn.commit()
            out = {"id": sid, "name": name, "project_id": None, "status": "created",
                   "start_date": start, "end_date": end}
    finally:
        conn.close()
    # Cohesive sprint/week model: sprint creation is a SYNC POINT — every task
    # already scheduled for this cycle's ISO week auto-commits into it. Runs on
    # the reuse path too (idempotent), so adopting a pre-planned week also pulls
    # in tasks scheduled for it since.
    committed = auto_commit_scheduled(out["id"])
    if committed:
        out["auto_committed"] = committed
    return out


def get_active_cycle() -> Optional[dict]:
    """Return the active cross-project weekly cycle, never a client sprint."""
    now = int(time.time())
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM sprints WHERE project_id IS NULL AND status = 'active' "
            "AND start_date <= ? AND end_date >= ? "
            "ORDER BY start_date DESC LIMIT 1", (now, now)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM sprints WHERE project_id IS NULL AND status = 'active' "
                "ORDER BY start_date DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_following_cycle_slots(anchor: dict, ensure: bool = False) -> dict:
    """Resolve the exact W+1 and W+2 global-cycle slots after ``anchor``.

    Distant future cycles and project-specific sprints are intentionally
    invisible.  ``ensure`` creates only missing exact slots, outside any held
    connection, so ``create_cycle`` keeps ownership of commits and auto-commit.
    """
    if not anchor or not anchor.get("start_date"):
        raise ValueError("cycle slot resolution requires an anchor start_date")
    slots = {}
    for key, offset in (("next", 1), ("plus2", 2)):
        ws, we = _cycle_slot_window(anchor["start_date"], offset)
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM sprints WHERE project_id IS NULL "
                "AND status != 'completed' AND start_date >= ? AND start_date <= ? "
                "ORDER BY start_date, id LIMIT 1", (ws, we)).fetchone()
            cycle = dict(row) if row else None
        finally:
            conn.close()
        if cycle is None and ensure:
            created = create_cycle(start_date=ws, end_date=we)
            cycle = get_sprint(created["id"])
        iso = _dt.date.fromtimestamp(ws).isocalendar()
        slots[key] = {
            "offset": offset,
            "iso_week": f"{iso[0]}-W{iso[1]:02d}",
            "start_date": ws,
            "end_date": we,
            "cycle": cycle,
        }
    return slots


def get_board_cycle_slots() -> dict:
    """Read model for Board's current, W+1, and W+2 global-cycle slots."""
    anchor = get_active_cycle()
    if anchor is None:
        ws, we = _week_window()
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM sprints WHERE project_id IS NULL "
                "AND status != 'completed' AND start_date >= ? AND start_date <= ? "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, start_date, id "
                "LIMIT 1", (ws, we)).fetchone()
            anchor = dict(row) if row else None
        finally:
            conn.close()
    math_anchor = anchor or {"start_date": _week_window()[0]}
    slots = resolve_following_cycle_slots(math_anchor)
    return {"anchor": anchor, **slots}


def auto_commit_scheduled(sprint_id: str) -> list:
    """The sprint-creation sync point of the cohesive sprint/week model: commit
    every task whose scheduled_week matches this sprint's ISO week and that sits
    in NO sprint. Each move routes through assign_task_sprint (commit-ledger row
    per task) and logs a `cycle_auto_committed` event. Settled (done/rejected)
    and archived tasks are left alone. Idempotent — already-committed tasks
    don't match the sprint_id IS NULL filter. Returns the committed task ids."""
    week = _sprint_to_iso_week(get_sprint(sprint_id))
    if not week:
        return []
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE scheduled_week = ? AND sprint_id IS NULL "
            "AND status NOT IN ('done', 'rejected') AND archived_at IS NULL",
            (week,)).fetchall()
    finally:
        conn.close()
    committed = []
    for r in rows:
        if assign_task_sprint(r["id"], sprint_id).get("status") == "assigned":
            committed.append(r["id"])
    if committed:
        conn = get_conn()
        try:
            for tid in committed:
                _log_event(conn, tid, "cycle_auto_committed",
                           {"cycle": sprint_id, "week": week, "via": "sprint_creation_sync"})
            conn.commit()
        finally:
            conn.close()
    return committed


def start_sprint(sprint_id: str) -> dict:
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE sprints SET status = 'active' WHERE id = ?", (sprint_id,))
        conn.commit()
        if cur.rowcount != 1:
            return {"status": "error", "error": "sprint not found"}
        return {"id": sprint_id, "status": "active"}
    finally:
        conn.close()


def close_sprint(sprint_id: str, next_sprint_id: Optional[str] = None,
                 auto_create: bool = True) -> dict:
    """Close a cycle and stamp every open commit-ledger row's OUTCOME:
    delivered (done + accepted) | carried (unfinished — it will compete fresh
    at the next standup, not auto-roll) | dropped (already stamped mid-cycle).

    LOCKED decision — auto-create next on close: when no `next_sprint_id` is
    given (and `auto_create`), the NEXT week's cycle is created + started
    automatically, so a close is never a dead end. The re-commit gate still
    holds: carry-overs' pointer clears to the icebox (sprint_id NULL) and they
    compete fresh at the next standup — auto-create makes the *cycle*, it does
    not auto-roll the *tasks*. If a next cycle is explicitly named, unfinished
    tasks re-commit into it (an explicit operator hand-off, ledger rows there)."""
    conn = get_conn()
    try:
        now = int(time.time())
        row = conn.execute("SELECT end_date FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "sprint not found"}
        this_end = row["end_date"]
        cur = conn.execute(
            "UPDATE sprints SET status = 'completed', closed_at = ? WHERE id = ?",
            (now, sprint_id))
        if cur.rowcount != 1:
            conn.commit()
            return {"status": "error", "error": "sprint not found"}

        delivered = conn.execute(
            "UPDATE task_sprints SET outcome = 'delivered' WHERE sprint_id = ? "
            "AND outcome IS NULL AND task_id IN "
            "(SELECT id FROM tasks WHERE status = 'done' AND reviewed_at IS NOT NULL)",
            (sprint_id,)).rowcount
        carried = conn.execute(
            "UPDATE task_sprints SET outcome = 'carried' WHERE sprint_id = ? "
            "AND outcome IS NULL", (sprint_id,)).rowcount

        # Move unfinished tasks' pointer to the named next cycle (explicit
        # re-commit → ledger rows there) or the icebox (re-commit gate).
        moved = conn.execute(
            "SELECT id, scheduled_week FROM tasks WHERE sprint_id = ? AND status != 'done'",
            (sprint_id,)
        ).fetchall()
        if next_sprint_id:
            for r in moved:
                conn.execute("UPDATE tasks SET sprint_id = ? WHERE id = ?",
                             (next_sprint_id, r["id"]))
                conn.execute(
                    "INSERT OR IGNORE INTO task_sprints (task_id, sprint_id, committed_at) "
                    "VALUES (?,?,?)", (r["id"], next_sprint_id, now))
        else:
            # Cohesive sprint/week model: stamp carried tasks with NEXT week's
            # ISO (if unset) BEFORE the pointer clears — they surface in the
            # "Next Week" drawer instead of vanishing into the backlog, and the
            # next cycle's creation-sync (auto_commit_scheduled) picks them up.
            # Same anchor formula as the auto-create below, so the stamp always
            # names the week the auto-created cycle will occupy.
            anchor = (this_end + 1) if (this_end and this_end < now) else (now + 7 * 86400)
            nxt_iso = _iso_week_str(anchor)
            for r in moved:
                if not r["scheduled_week"]:
                    conn.execute("UPDATE tasks SET scheduled_week = ? WHERE id = ?",
                                 (nxt_iso, r["id"]))
                    _log_event(conn, r["id"], "task_updated",
                               {"changed": {"scheduled_week": {"from": None, "to": nxt_iso}},
                                "via": "close_sprint_carry"})
            conn.execute(
                "UPDATE tasks SET sprint_id = NULL WHERE sprint_id = ? AND status != 'done'",
                (sprint_id,))
        conn.commit()
    finally:
        conn.close()

    # Auto-create + start the NEXT week's cycle (the week following this one),
    # unless the caller named a next cycle or already has a live one for that
    # week. Done OUTSIDE the txn via the normal constructors so the ledger/name
    # rules stay in one place.
    created = None
    if not next_sprint_id and auto_create:
        # Anchor the next week: if this cycle already ENDED (the normal on-time
        # roll), the week after its end IS the current week; if it's being wrapped
        # EARLY or it's the legacy 14-day sprint (end in the future), fall to the
        # week after THIS one so we don't jump weeks ahead. create_cycle dedups by
        # week, so a pre-planned week is adopted rather than forked.
        anchor = (this_end + 1) if (this_end and this_end < now) else (now + 7 * 86400)
        nxt_start, nxt_end = _week_window(anchor)
        created = create_cycle(start_date=nxt_start, end_date=nxt_end)
        start_sprint(created["id"])   # idempotent — also activates an adopted week
    return {"id": sprint_id, "status": "completed",
            "next_sprint": next_sprint_id or (created or {}).get("id"),
            "auto_created": created, "delivered": delivered, "carried": carried}


def delete_cycle(sprint_id: str) -> dict:
    """Guarded HARD delete of a cycle — for removing a mistakenly-created or
    empty planning/active cycle (plan-week makes them easy to spawn). Refuses a
    COMPLETED cycle: a delivered timebox is history, archive don't erase (same
    doctrine as delete_task refusing accepted work). Committed tasks are returned
    to the icebox (sprint_id NULL) so nothing is orphaned; the cycle's
    commit-ledger rows are removed with it (it never delivered — nothing to keep).
    Order respects the FKs: NULL the tasks, drop the ledger rows, then the row."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT status, name FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "cycle not found"}
        if row["status"] == "completed":
            return {"status": "error",
                    "error": "refusing to delete a completed cycle — it's history "
                             "(a delivered timebox is the audit record, archive not erase)"}
        freed = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE sprint_id = ?", (sprint_id,)).fetchone()[0]
        conn.execute("UPDATE tasks SET sprint_id = NULL WHERE sprint_id = ?", (sprint_id,))
        conn.execute("DELETE FROM task_sprints WHERE sprint_id = ?", (sprint_id,))
        conn.execute("DELETE FROM sprints WHERE id = ?", (sprint_id,))
        conn.commit()
        return {"status": "deleted", "sprint_id": sprint_id,
                "name": row["name"], "tasks_freed": freed}
    finally:
        conn.close()


def validate_sprint_target(sprint_id: Optional[str]) -> dict:
    """Read-only preflight for writers that cannot create + commit atomically.

    The base task INSERT happens in the external Hermes CLI.  Create callers use
    this before crossing that boundary so an already-invalid target does not
    leave an avoidable unassigned task.  ``assign_task_sprint`` still repeats the
    check in its own transaction: the cycle can close after this preflight.
    """
    if not sprint_id:
        return {"status": "ok", "sprint_id": None}
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "error", "reason": "not_found",
                "error": f"sprint '{sprint_id}' not found"}
    if row["status"] == "completed":
        return {"status": "error", "reason": "completed",
                "error": f"sprint '{sprint_id}' is completed — commit to the active cycle instead"}
    return {"status": "ok", "sprint_id": sprint_id}


def assign_task_sprint(task_id: str, sprint_id: Optional[str]) -> dict:
    """Commit a task to a cycle (or pull it). The commit-ledger is APPEND-ONLY:
    committing writes a dated task_sprints row; pulling mid-cycle stamps the
    open row `dropped` instead of deleting it, so closed cycles stay
    reconstructable. Re-committing a previously-dropped task reopens its row.

    SCHEDULED_WEEK SYNC: committing a task to a sprint ALSO clears its
    scheduled_week if it points to a different ISO week — prevents the
    dual-appearance bug where a task shows in both the cycle board and the
    scheduled-for-future list."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, sprint_id, scheduled_week FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        prior_sprint = row["sprint_id"]
        prior_week = row["scheduled_week"]
        now = int(time.time())
        if sprint_id:
            sprint_row = conn.execute("SELECT status FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
            if sprint_row is None:
                return {"status": "error", "error": f"sprint '{sprint_id}' not found"}
            if sprint_row["status"] == "completed":
                return {"status": "error", "error": f"sprint '{sprint_id}' is completed — commit to the active cycle instead"}
            conn.execute("UPDATE tasks SET sprint_id = ? WHERE id = ?",
                         (sprint_id, task_id))
            conn.execute(
                "INSERT OR IGNORE INTO task_sprints (task_id, sprint_id, committed_at) "
                "VALUES (?,?,?)", (task_id, sprint_id, now))
            # A re-commit of a dropped task reopens its ledger row.
            conn.execute(
                "UPDATE task_sprints SET outcome = NULL WHERE task_id = ? AND sprint_id = ? "
                "AND outcome = 'dropped'", (task_id, sprint_id))
            # SCHEDULED_WEEK SYNC: clear scheduled_week if it points to a
            # different week than this sprint (prevents dual-appearance).
            if prior_week:
                sp = conn.execute(
                    "SELECT start_date FROM sprints WHERE id = ?",
                    (sprint_id,)).fetchone()
                if sp and sp["start_date"]:
                    from datetime import date
                    d = date.fromtimestamp(sp["start_date"])
                    iso = d.isocalendar()
                    sprint_week = f"{iso[0]}-W{iso[1]:02d}"
                    if sprint_week != prior_week:
                        conn.execute(
                            "UPDATE tasks SET scheduled_week = NULL WHERE id = ?",
                            (task_id,))
                        _log_event(conn, task_id, "task_updated",
                                   {"changed": {"scheduled_week": {"from": prior_week, "to": None}},
                                    "via": "assign_task_sprint_sync"})
            _log_event(conn, task_id, "cycle_committed",
                       {"cycle": sprint_id, "via": "dashboard"})
        else:
            conn.execute("UPDATE tasks SET sprint_id = NULL WHERE id = ?",
                         (task_id,))
            if prior_sprint:
                conn.execute(
                    "UPDATE task_sprints SET outcome = 'dropped' WHERE task_id = ? "
                    "AND sprint_id = ? AND outcome IS NULL",
                    (task_id, prior_sprint))
                _log_event(conn, task_id, "cycle_dropped",
                           {"cycle": prior_sprint, "via": "dashboard"})
        conn.commit()
        return {"task_id": task_id, "sprint_id": sprint_id, "status": "assigned"}
    finally:
        conn.close()


def bulk_assign_sprint(task_ids: list, sprint_id: Optional[str]) -> dict:
    """Commit (or pull) SEVERAL tasks at once — the multi-select path (Phase F).
    Routes every id through assign_task_sprint so each move records its own
    commit-ledger row (never a raw batch write); returns per-id results. Mirrors
    bulk_accept: the sanctioned bulk verb, so a batch can't skip the ledger."""
    results, ok = {}, 0
    for tid in task_ids or []:
        r = assign_task_sprint(tid, sprint_id)
        results[tid] = r.get("status") or r.get("error")
        if r.get("status") == "assigned":
            ok += 1
    return {"status": "ok", "assigned": ok, "total": len(task_ids or []),
            "sprint_id": sprint_id, "results": results}


def reorder_cycle_tasks(sprint_id: str, ordered_ids: list) -> dict:
    """Persist a manual drag-reorder of a cycle's board cards. `ordered_ids` is
    the new full order of the cycle's tasks (all columns, top→bottom); each gets
    board_order = its index. Only ids actually committed to this cycle are written
    (strangers are ignored), so a stale client can't reorder foreign tasks."""
    ids = [t for t in (ordered_ids or []) if t]
    conn = get_conn()
    try:
        if not conn.execute("SELECT 1 FROM sprints WHERE id = ?", (sprint_id,)).fetchone():
            return {"status": "error", "error": "cycle not found"}
        in_cycle = {r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE sprint_id = ?", (sprint_id,)).fetchall()}
        applied = 0
        for i, tid in enumerate(ids):
            if tid in in_cycle:
                conn.execute("UPDATE tasks SET board_order = ? WHERE id = ?", (i, tid))
                applied += 1
        conn.commit()
        return {"status": "ok", "sprint_id": sprint_id, "reordered": applied}
    finally:
        conn.close()


def roll_cycle() -> dict:
    """The weekly auto-roll (called by the sweeper; idempotent): when the active
    cycle's window has passed, close it (stamping delivered/carried outcomes)
    and open the next week's cycle EMPTY — the re-commit gate means carry-overs
    re-enter via the standup's candidate plan, never automatically."""
    conn = get_conn()
    try:
        active = conn.execute(
            "SELECT id, end_date FROM sprints WHERE status = 'active' "
            "ORDER BY end_date ASC LIMIT 1").fetchone()
    finally:
        conn.close()
    now = int(time.time())
    if not active or active["end_date"] > now:
        return {"rolled": False, "active": active["id"] if active else None}
    # close_sprint now owns the create+start of next week's cycle (LOCKED:
    # auto-create on close), so the roll is just a close — no double-create.
    closed = close_sprint(active["id"])
    return {"rolled": True, "closed": closed, "next_cycle": closed.get("next_sprint")}


def finish_sprint() -> dict:
    """The operator's explicit 'Finish Sprint' roll-forward (the 🏁 button) — a
    deliberate hand-off, distinct from the auto roll_cycle sweeper (which empties
    to the icebox and lets carry-overs re-compete at standup). In one flow: archive
    the finished work (done/rejected), roll the unfinished pile into the NEXT cycle,
    close + activate, and guarantee a +2 planning slot so planning is never a dead
    end. Reuses create_cycle / start_sprint and the same task_sprints ledger stamps
    (delivered/carried/dropped) as close_sprint, so the audit trail stays consistent.
    Returns a summary for the confirmation modal + success toast."""
    active = get_active_cycle()
    if not active:
        return {"status": "error", "error": "no active sprint to finish"}
    active_id, active_name = active["id"], active["name"]
    now = int(time.time())

    # 1. Resolve exact calendar slots. A distant planning row is not "next";
    #    W+1 and W+2 are derived independently from the active cycle's week.
    slots = resolve_following_cycle_slots(active, ensure=True)
    next_cycle = slots["next"]["cycle"]
    plus2_cycle = slots["plus2"]["cycle"]
    next_id, next_name = next_cycle["id"], next_cycle["name"]
    if next_id == active_id:
        return {"status": "error", "error": "could not resolve a distinct next cycle"}
    plus2_id, plus2_name = plus2_cycle["id"], plus2_cycle["name"]

    # 2. Archive + roll + close, in ONE transaction.
    #    "Finished" = ACCEPTED work only: done+reviewed_at (the human accept-gate
    #    cannot be skipped by a sprint boundary) OR rejected (terminal). Done-but-
    #    unreviewed and review carry forward so the human can still accept them.
    _FINISHED = "((status = 'done' AND reviewed_at IS NOT NULL) OR status = 'rejected')"
    conn = get_conn()
    try:
        # Idempotency latch: only an ACTIVE sprint can be finished. If a concurrent
        # finish already closed it, bail with zero writes (rolls back the txn).
        cur = conn.execute(
            "UPDATE sprints SET status = 'completed', closed_at = ? "
            "WHERE id = ? AND status = 'active'", (now, active_id))
        if cur.rowcount != 1:
            conn.rollback()
            return {"status": "error", "error": "sprint is not active (already finished?)"}

        # Archive the finished work: stamp archived_at (kept on the now-completed
        # sprint as history), then stamp the commit-ledger outcomes.
        archived = conn.execute(
            f"UPDATE tasks SET archived_at = ? WHERE sprint_id = ? "
            f"AND archived_at IS NULL AND {_FINISHED}", (now, active_id)).rowcount
        conn.execute(
            "UPDATE task_sprints SET outcome = 'delivered' WHERE sprint_id = ? "
            "AND outcome IS NULL AND task_id IN "
            "(SELECT id FROM tasks WHERE status = 'done' AND reviewed_at IS NOT NULL)",
            (active_id,))
        conn.execute(
            "UPDATE task_sprints SET outcome = 'dropped' WHERE sprint_id = ? "
            "AND outcome IS NULL AND task_id IN "
            "(SELECT id FROM tasks WHERE status = 'rejected')", (active_id,))

        # Roll the unfinished pile (everything NOT finished: pending + done-unreviewed
        # + review) into next: move the pointer + append a dated commit-ledger row
        # (mirrors assign_task_sprint).
        pending = conn.execute(
            f"SELECT id FROM tasks WHERE sprint_id = ? AND NOT {_FINISHED}",
            (active_id,)).fetchall()
        for r in pending:
            conn.execute("UPDATE tasks SET sprint_id = ? WHERE id = ?", (next_id, r["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO task_sprints (task_id, sprint_id, committed_at) "
                "VALUES (?,?,?)", (r["id"], next_id, now))
            _log_event(conn, r["id"], "cycle_committed",
                       {"cycle": next_id, "via": "finish_sprint"})
        moved = len(pending)
        # The moved tasks' OLD (active) ledger rows are still open → stamp 'carried'.
        conn.execute(
            "UPDATE task_sprints SET outcome = 'carried' WHERE sprint_id = ? "
            "AND outcome IS NULL", (active_id,))
        conn.commit()
    finally:
        conn.close()

    # 3. Activate the next cycle (separate conn — start_sprint owns its own).
    start_sprint(next_id)
    # Cohesive sprint/week model: the activated cycle pulls in every task
    # scheduled for its ISO week (create_cycle already did this for a freshly
    # created slot; idempotent for that path, load-bearing for an adopted one).
    auto_commit_scheduled(next_id)

    return {
        "status": "ok",
        "finished": {"id": active_id, "name": active_name},
        "archived": archived,
        "moved": moved,
        "activated": {"id": next_id, "name": next_name},
        "next_up": {"id": plus2_id, "name": plus2_name},
    }


def get_velocity() -> list[dict]:
    """The cycle_velocity VIEW: per cycle, committed count vs velocity
    (accepted human-intent tasks). Read-only."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM cycle_velocity ORDER BY start_date DESC").fetchall()]
    finally:
        conn.close()


# --- Views ---

def _week_bucket_tasks(week_where: str, week_params: tuple, order: str,
                       project_id: Optional[str] = None) -> list[dict]:
    """Shared query shape for the sprint-less week buckets (next_week / future /
    icebox): no sprint + the bucket's scheduled_week predicate. Personal/system
    projects stay excluded from the unscoped views (Phase 1 §3.4), same as the
    icebox always did.

    This predicate is THE hiding mechanism in this codebase — verified, journey
    fase 1 step 5: nothing else filters projects by `kind` (`list_projects()`
    returns `proj_inbox` like any other row and the Projects tab renders it), so
    "hidden like proj_inbox" means exactly "kept out of cycle planning" and
    nothing more.

    `sales` (m09's `proj_ventas`) joins the excluded set for the same reason the
    other two are in it: a cadence card is commercial work with a due date the
    server derived, and its home is Hoy's fifth band via
    `canvas.plan_candidates`' `why='cliente'` source. Letting it into the backlog
    the operator grooms for a DELIVERY cycle would mix two planning horizons in
    one list."""
    conn = get_conn()
    try:
        if project_id:
            rows = conn.execute(
                f"SELECT t.* FROM tasks t WHERE t.sprint_id IS NULL AND {week_where} "
                f"AND t.project_id = ? ORDER BY {order}",
                (*week_params, project_id)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT t.* FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
                f"WHERE t.sprint_id IS NULL AND {week_where} "
                "AND COALESCE(p.kind, 'product') NOT IN ('personal', 'system', 'sales') "
                f"ORDER BY {order}",
                week_params
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_icebox_tasks(project_id: Optional[str] = None) -> list[dict]:
    """The Backlog: tasks with no sprint AND no scheduled_week — TRULY
    unscheduled (cohesive sprint/week model). Week-scheduled but uncommitted
    tasks live in get_next_week_tasks / get_future_tasks instead, so the three
    buckets partition the sprint-less set.

    Phase 1 (§3.4): personal-admin lives in `kind='personal'` projects, which
    never run cycles — so they must not leak into sprint planning. Filter them
    out unless the caller explicitly asks for that project. System projects (the
    untriaged Inbox) are likewise excluded from the unscoped icebox."""
    return _week_bucket_tasks(
        "t.scheduled_week IS NULL", (), "t.created_at DESC", project_id)


def get_next_week_tasks(project_id: Optional[str] = None) -> list[dict]:
    """The "Next Week" bucket: tasks scheduled for next ISO week (or a stale
    current/past-week tag that never got committed — <= keeps them visible in
    the nearest actionable drawer instead of vanishing) with no sprint. These
    auto-commit when next week's cycle is created (auto_commit_scheduled)."""
    nxt = _iso_week_str(offset_weeks=1)
    return _week_bucket_tasks(
        "t.scheduled_week IS NOT NULL AND t.scheduled_week <= ?", (nxt,),
        "t.scheduled_week ASC, t.priority DESC, t.created_at DESC", project_id)


def get_future_tasks(project_id: Optional[str] = None) -> list[dict]:
    """The "Future" bucket: tasks scheduled +2 weeks and beyond, no sprint.
    Exact complement of get_next_week_tasks over the scheduled set (> next
    week's ISO), so every non-ISO tag like 'someday' still lands in exactly one
    bucket ('s' sorts after '2' → here). Ordered by week for the grouped UI."""
    nxt = _iso_week_str(offset_weeks=1)
    return _week_bucket_tasks(
        "t.scheduled_week IS NOT NULL AND t.scheduled_week > ?", (nxt,),
        "t.scheduled_week ASC, t.priority DESC, t.created_at DESC", project_id)


def get_delivered_sprints(project_id: Optional[str] = None) -> list[dict]:
    """Completed sprints with delivered task counts."""
    conn = get_conn()
    try:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM sprints WHERE status = 'completed' AND project_id = ? ORDER BY closed_at DESC",
                (project_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sprints WHERE status = 'completed' ORDER BY closed_at DESC"
            ).fetchall()
        sprints = [dict(r) for r in rows]
        for s in sprints:
            total = conn.execute("SELECT COUNT(*) FROM tasks WHERE sprint_id = ?", (s["id"],)).fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE sprint_id = ? AND status = 'done'", (s["id"],)
            ).fetchone()[0]
            s["total_tasks"] = total
            s["delivered_tasks"] = done
            s["delivery_rate"] = int(done / total * 100) if total > 0 else 0
        return sprints
    finally:
        conn.close()


def get_active_sprint(project_id: Optional[str] = None) -> Optional[dict]:
    """Get the currently active sprint/cycle. Prefers the active cycle whose
    window CONTAINS now (the actually-current one) so a future planned/started
    cycle can't shadow it; falls back to the latest-starting active cycle."""
    now = int(time.time())
    conn = get_conn()
    try:
        proj = " AND project_id = ?" if project_id else ""
        args = (project_id,) if project_id else ()
        row = conn.execute(
            f"SELECT * FROM sprints WHERE status = 'active'{proj} "
            "AND start_date <= ? AND end_date >= ? ORDER BY start_date DESC LIMIT 1",
            (*args, now, now)).fetchone() or conn.execute(
            f"SELECT * FROM sprints WHERE status = 'active'{proj} "
            "ORDER BY start_date DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# The Cycle tab's board columns (Phase A). Every board status maps to exactly
# one column; blocked folds into Backlog (flagged), rejected into Done (muted),
# so the mobile 4-wide carousel never grows a 5th lane.
_COL_OF = {
    "backlog": "backlog", "ready": "backlog", "blocked": "backlog",
    "in_progress": "in_progress",
    "review": "review",
    "done": "done", "rejected": "done",
}
_BOARD_TASK_FIELDS = (
    "t.id, t.title, t.status, t.priority, t.assignee, t.delegate, t.origin, "
    "t.project_id, t.progress_pct, t.progress_note, t.reviewed_at, "
    "t.rejection_reason, t.completed_at, t.created_at, t.due_date, "
    "p.name AS project_name, p.color AS project_color, p.icon AS project_icon, "
    "p.kind AS project_kind"
)


_STATUS_ORDER = {"blocked": 0, "at-risk": 1, "on-track": 2}


def _cycle_project_status(total, done, blocked, rejected, elapsed_frac):
    """on-track / at-risk / blocked for one project's slice of a cycle:
    - blocked  → any task in the slice is blocked.
    - at-risk  → a rejection, OR progress is lagging the elapsed-time pace by more
                 than 15 points while work remains (behind the ideal burndown).
    - on-track → otherwise. A just-started or finished slice is never 'at-risk' by
                 pace (elapsed≈0 or done==total)."""
    if blocked > 0:
        return "blocked"
    done_frac = (done / total) if total else 1.0
    behind = total > done and done_frac < (elapsed_frac - 0.15)
    if rejected > 0 or behind:
        return "at-risk"
    return "on-track"


def get_cycle_board(sprint_id: Optional[str] = None) -> dict:
    """The Cycle tab's board, composed SERVER-SIDE (same ratchet spirit as the
    Today canvas: the dashboard tab reads THIS, it never re-filters /api/tasks).

    Returns the active cycle (or the one requested) + its committed tasks grouped
    into board columns, the velocity number (cycle_velocity VIEW), a completed_at-
    derived burndown series, days-left, and the icebox (unplanned tasks available
    to commit). Read-only; every write the tab issues goes through an existing
    audited verb (assign_task_sprint / set_task_status)."""
    conn = get_conn()
    try:
        if sprint_id:
            crow = conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
        else:
            # Default board = the now-containing active cycle (never a future one);
            # fall back to the latest active. Mirrors get_active_sprint.
            _now = int(time.time())
            crow = conn.execute(
                "SELECT * FROM sprints WHERE status = 'active' AND start_date <= ? "
                "AND end_date >= ? ORDER BY start_date DESC LIMIT 1", (_now, _now)
            ).fetchone() or conn.execute(
                "SELECT * FROM sprints WHERE status = 'active' ORDER BY start_date DESC LIMIT 1"
            ).fetchone()
        if crow is None:
            # any_cycles distinguishes "no cycle is active this week" (others
            # exist) from a truly empty slate (the Cycle tab's first-run state).
            any_cycles = conn.execute("SELECT COUNT(*) FROM sprints").fetchone()[0] > 0
            return {"has_active": False, "any_cycles": any_cycles, "cycle": None,
                    "columns": {"backlog": [], "in_progress": [], "review": [], "done": []},
                    "burndown": [], "icebox": get_icebox_tasks(),
                    "next_week": get_next_week_tasks(), "future": get_future_tasks(),
                    "next_cycle": _next_week_cycle(),
                    "week_meta": {"next": _iso_week_str(offset_weeks=1),
                                  "plus2": _iso_week_str(offset_weeks=2)},
                    "counts": {}}
        cyc = dict(crow)
        cid = cyc["id"]

        rows = conn.execute(
            f"SELECT {_BOARD_TASK_FIELDS} FROM tasks t "
            "LEFT JOIN projects p ON t.project_id = p.id WHERE t.sprint_id = ? "
            # Manual drag order first (NULL → last), then priority/recency.
            "ORDER BY COALESCE(t.board_order, 2000000000), t.priority DESC, t.created_at ASC",
            (cid,)
        ).fetchall()
        columns = {"backlog": [], "in_progress": [], "review": [], "done": []}
        done_ts = []
        for r in rows:
            t = dict(r)
            t["blocked"] = t["status"] == "blocked"
            t["rejected"] = t["status"] == "rejected"
            columns[_COL_OF.get(t["status"], "backlog")].append(t)
            if t["status"] == "done" and t["completed_at"]:
                done_ts.append(t["completed_at"])

        committed = len(rows)
        done_n = len(columns["done"])
        vrow = conn.execute(
            "SELECT committed, velocity FROM cycle_velocity WHERE sprint_id = ?", (cid,)
        ).fetchone()
        velocity = (vrow["velocity"] if vrow and vrow["velocity"] is not None else 0)

        # Burndown, derived from completed_at — no snapshot table. For each day
        # in [start, min(now, end)]: remaining = committed - done-by-day-end;
        # ideal = linear from committed → 0 across the window.
        burndown = _burndown(cyc["start_date"], cyc["end_date"], committed, done_ts)
    finally:
        conn.close()

    now = int(time.time())
    total_days = max(1, round((cyc["end_date"] - cyc["start_date"]) / 86400))
    days_left = max(0, -(-(cyc["end_date"] - now) // 86400)) if cyc["end_date"] > now else 0
    cyc.update({
        "committed": committed, "done": done_n,
        "progress_pct": int(done_n / committed * 100) if committed else 0,
        "velocity": velocity, "days_left": days_left, "total_days": total_days,
    })

    # Per-project rollup for the cycle detail view: each project with work in this
    # cycle, its progress, and a derived on-track/at-risk/blocked status. Slices
    # ordered worst-first (blocked → at-risk → on-track), then by size.
    span = cyc["end_date"] - cyc["start_date"]
    elapsed_frac = min(1.0, max(0.0, (now - cyc["start_date"]) / span)) if span > 0 else 1.0
    by_proj = {}
    for r in rows:
        pid = r["project_id"] or "__none__"
        p = by_proj.get(pid)
        if p is None:
            p = by_proj[pid] = {
                "project_id": r["project_id"], "name": r["project_name"] or "No project",
                "color": r["project_color"], "icon": r["project_icon"] or "📦",
                "total": 0, "done": 0, "in_progress": 0, "review": 0, "backlog": 0,
                "blocked": 0, "rejected": 0}
        p["total"] += 1
        st = r["status"]
        p[st if st in ("done", "in_progress", "review", "blocked", "rejected") else "backlog"] += 1
    projects = []
    for p in by_proj.values():
        p["progress_pct"] = int(p["done"] / p["total"] * 100) if p["total"] else 0
        p["status"] = _cycle_project_status(
            p["total"], p["done"], p["blocked"], p["rejected"], elapsed_frac)
        projects.append(p)
    projects.sort(key=lambda x: (_STATUS_ORDER.get(x["status"], 3), -x["total"], x["name"]))

    return {
        "has_active": True,
        "cycle": cyc,
        "columns": columns,
        "burndown": burndown,
        "projects": projects,
        # The 3 bottom drawers (cohesive sprint/week model): a partition of the
        # sprint-less set — Next Week (≤ +1), Future (≥ +2), Backlog (untagged).
        "icebox": get_icebox_tasks(),
        "next_week": get_next_week_tasks(),
        "future": get_future_tasks(),
        # The next week's cycle if one already exists (planning or active) — the
        # UI's "commit to cycle" affordance on Next Week cards needs its id.
        "next_cycle": _next_week_cycle(),
        "week_meta": {"next": _iso_week_str(offset_weeks=1),
                      "plus2": _iso_week_str(offset_weeks=2)},
        "counts": {k: len(v) for k, v in columns.items()},
    }


def _next_week_cycle() -> Optional[dict]:
    """The non-completed cycle whose start falls in NEXT week's Mon→Sun window,
    if one exists — the target of the Next Week drawer's commit affordance."""
    ws, we = _week_window(int(time.time()) + 7 * 86400)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, name, status, start_date, end_date FROM sprints "
            "WHERE start_date >= ? AND start_date <= ? AND status != 'completed' "
            "ORDER BY start_date LIMIT 1", (ws, we)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_project_detail(project_ref: str) -> dict:
    """Project detail (Phase E): the project row + its tasks grouped into board
    columns + quick stats (total, done, in the active cycle, initiative count) +
    linked initiatives + the active cycle id (so the modal can 'commit to active
    cycle'). Read-only; accepts an id or a slug. Every write the modal issues
    goes through an existing audited verb."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE id = ? OR slug = ?",
            (project_ref, project_ref)).fetchone()
        if row is None:
            return {"status": "error", "error": "project not found"}
        pid = row["id"]
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())
        task_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (pid,)).fetchone()[0]
        proj["task_count"] = task_count

        rows = conn.execute(
            "SELECT id, title, status, priority, assignee, origin, sprint_id, "
            "planned_for, reviewed_at, rejection_reason, completed_at, created_at "
            "FROM tasks WHERE project_id = ? ORDER BY priority DESC, created_at DESC",
            (pid,)).fetchall()
        columns = {"backlog": [], "in_progress": [], "review": [], "done": []}
        done_n = 0
        for r in rows:
            t = dict(r)
            t["blocked"] = t["status"] == "blocked"
            t["rejected"] = t["status"] == "rejected"
            columns[_COL_OF.get(t["status"], "backlog")].append(t)
            if t["status"] == "done":
                done_n += 1

        active = conn.execute(
            "SELECT id, name FROM sprints WHERE status = 'active' "
            "ORDER BY start_date DESC LIMIT 1").fetchone()
        active_id = active["id"] if active else None
        in_cycle = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND sprint_id = ?",
            (pid, active_id)).fetchone()[0] if active_id else 0

        inits = [dict(r) for r in conn.execute(
            "SELECT id, title, status, health, progress, quarter, tier "
            "FROM initiatives WHERE project_id = ? ORDER BY quarter, tier", (pid,)).fetchall()]
    finally:
        conn.close()

    return {
        "project": proj,
        "columns": columns,
        "counts": {k: len(v) for k, v in columns.items()},
        "initiatives": inits,
        "active_cycle": {"id": active_id, "name": active["name"] if active else None},
        "stats": {"total": task_count, "done": done_n,
                  "done_pct": int(done_n / task_count * 100) if task_count else 0,
                  "in_active_cycle": in_cycle, "initiatives": len(inits)},
    }


def get_calendar(weeks_back: int = 2, weeks_fwd: int = 5) -> dict:
    """The Cycle calendar strip (Phase B): one cell per ISO week, from
    `weeks_back` weeks before this week through `weeks_fwd` after. Each cell maps
    to the cycle whose Monday falls in that week (if any) + its committed/done
    counts, delivery rate, and the distinct project-color dots for projects with
    committed tasks (cross-project legibility). Empty future weeks carry
    cycle_id=None → the UI offers "Plan". Read-only."""
    import datetime as _dtm
    now = int(time.time())
    base_start, _ = _week_window(now)  # this week's Monday
    conn = get_conn()
    try:
        cells = []
        for off in range(-weeks_back, weeks_fwd + 1):
            # Re-snap from a mid-week anchor so DST shifts can't drift the window.
            ws, we = _week_window(base_start + off * 7 * 86400 + 3 * 86400)
            iso = _dtm.date.fromtimestamp(ws).isocalendar()
            cell = {"iso": f"{iso[0]}-W{iso[1]:02d}", "week": iso[1], "year": iso[0],
                    "start": ws, "end": we, "offset": off, "is_current": off == 0}
            row = conn.execute(
                "SELECT * FROM sprints WHERE start_date >= ? AND start_date <= ? "
                "ORDER BY start_date LIMIT 1", (ws, we)).fetchone()
            if row:
                s = dict(row); cid = s["id"]
                total = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE sprint_id=?", (cid,)).fetchone()[0]
                done = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE sprint_id=? AND status='done'",
                    (cid,)).fetchone()[0]
                dots = [r[0] for r in conn.execute(
                    "SELECT DISTINCT p.color FROM tasks t JOIN projects p ON t.project_id=p.id "
                    "WHERE t.sprint_id=? AND p.color IS NOT NULL", (cid,)).fetchall()]
                cell.update({"cycle_id": cid, "name": s["name"], "status": s["status"],
                             "committed": total, "done": done,
                             "delivery_rate": int(done / total * 100) if total else 0,
                             "project_dots": dots})
            else:
                cell.update({"cycle_id": None, "name": None, "status": "empty",
                             "committed": 0, "done": 0, "delivery_rate": 0,
                             "project_dots": []})
            cells.append(cell)
        cur_iso = _dtm.date.fromtimestamp(base_start).isocalendar()
        return {"cells": cells, "current_iso": f"{cur_iso[0]}-W{cur_iso[1]:02d}"}
    finally:
        conn.close()


def _burndown(start: int, end: int, committed: int, done_ts: list) -> list:
    """A completed_at-derived burndown: one point per day of the cycle window up
    to today. remaining = committed - (# done by end-of-day); ideal is linear."""
    import datetime as _dtm
    now = int(time.time())
    d0 = _dtm.date.fromtimestamp(start)
    d1 = _dtm.date.fromtimestamp(min(end, now))
    span = max(1, (_dtm.date.fromtimestamp(end) - d0).days)
    out, i = [], 0
    day = d0
    while day <= d1:
        day_end = int(time.mktime((day + _dtm.timedelta(days=1)).timetuple()))
        done_by = sum(1 for ts in done_ts if ts and ts < day_end)
        ideal = round(committed * (1 - i / span), 1)
        out.append({"day": day.isoformat(), "remaining": committed - done_by,
                    "ideal": max(0.0, ideal)})
        day += _dtm.timedelta(days=1)
        i += 1
    return out
