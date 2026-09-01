"""
Phase 3 — The Canvas / Today layer (the daily door into the system).

The canvas is a VIEW over the task spine plus the minimal forward-commitment
state the design allows (§3.2 "view, not entity"): `planned_for` + `plan_order`
(a task's membership in "today" — the commitment device, per the operator's decision
that the daily canvas IS the commitment layer, not the cycle) and `due_date`
(deadline-driven personal admin; overdue pins red).

Everything here is a server-side query (the Phase-3 ratchet): the Today tab,
the MCP `get_day_plan` verb, and the Hermes rituals all read the SAME
composition from this module — never a client-side filter.

Sidecar on the Hermes kanban DB, same pattern as sprints/graph/identity.
"""
import datetime as _dt
import json
import re
import sqlite3
import time
from typing import Optional

from . import db
from . import stagekind
from . import sprints

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The statuses that still need action (not settled). rejected is terminal-ish
# (resurrectable per the Phase-3 decision) and done is accepted work.
_SETTLED = ("done", "rejected")
_HUMAN = ("ricardo", "user")

# Statuses that disqualify a task from being PROPOSED as today's work: the
# operator cannot do them today, whatever their priority or due date says.
# `review` is someone else's turn. `blocked` is waiting on an unblock and is
# already reported, by its own count, in the ⚠️ Needs you block — proposing it
# as one of the day's three is the plan disagreeing with itself. Observed live
# 2026-08-11: a blocked task was committed as the day's #1 while simultaneously
# being listed as blocked. Excluding it here does NOT hide it; blocked_count is
# an independent query and still reports it as the thing needing attention.
_UNWORKABLE = ("review", "blocked")

# Personal tasks (proj_personal) are excluded from the Today canvas —
# the daily view is for professional work only. Personal tasks still
# show in the Kanban board and are queryable via MCP.
_PERSONAL_PROJECT = "proj_personal"


def _today() -> str:
    return _dt.date.today().isoformat()


def _valid_date(d: Optional[str]) -> Optional[str]:
    """Normalize/validate a plan date. None → today. Bad format/date → None."""
    if d in (None, "", "today"):
        return _today()
    d = str(d).strip()
    if not DATE_RE.match(d):
        return None
    try:
        return _dt.date.fromisoformat(d).isoformat()
    except ValueError:
        return None


def _log(conn, task_id: str, kind: str, payload: dict) -> None:
    """Append to the shared task_events audit log (best-effort)."""
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, kind, json.dumps(payload), int(time.time())),
        )
    except Exception:
        pass


def ensure_schema() -> None:
    """Idempotently add the Phase-3 canvas columns. Safe at startup.
    planned_for / due_date are TEXT ISO dates (YYYY-MM-DD, sortable as strings);
    plan_order is the position within a day's plan."""
    conn = db.get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "planned_for" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN planned_for TEXT")
        if "plan_order" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN plan_order INTEGER")
        if "due_date" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
        # Backlog Phase 1 (item 1): scheduled_week is the ISO-week bucket a task is
        # planned into ("2026-W28"), nullable. A task with no sprint_id AND no
        # scheduled_week is "truly unscheduled" → the Backlog lens. Kept here beside
        # due_date so the column is guaranteed at every boot, like the other
        # canvas fields; a standalone runnable migration mirrors this for the live DB
        # (dashboard/migrations/phase1_backlog_scheduling.py).
        if "scheduled_week" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN scheduled_week TEXT")
        # Phase 3 (item 2): `review` is a real status. Migrate any row still on
        # the old invisible predicate (done AND unreviewed = awaiting the
        # operator) — including rows an out-of-date external writer lands later,
        # which is why this runs at every startup, not once. Auditable per row.
        # Anti-respawn guard: skip tasks completed 3+ times — the Hermes CLI
        # dispatcher's complete_task() bypasses report_result()/route_result(),
        # so the loop.py anti-respawn guard never fires for those completions.
        # Without this guard, done→review migration loops forever on each
        # dashboard restart for dispatch tasks that always escalate.
        stale = conn.execute(
            "SELECT t.id FROM tasks t "
            "WHERE t.status = 'done' AND t.reviewed_at IS NULL "
            "  AND (SELECT COUNT(*) FROM task_events e "
            "       WHERE e.task_id = t.id AND e.kind = 'completed') < 3"
        ).fetchall()
        for r in stale:
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (r["id"],))
            _log(conn, r["id"], "status_changed",
                 {"from": "done", "to": "review", "via": "phase3-review-migration"})
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- plan writes

def plan_task(task_id: str, planned_for: Optional[str] = None,
              plan_order: Optional[int] = None,
              due_date: Optional[str] = None,
              clear_plan: bool = False) -> dict:
    """Plan (or unplan) ONE task: set its planned_for date / order / due_date.
    The single-card counterpart of plan_day — used by the Today tab's
    "→ today" / drag-reorder actions. Appends a planned/unplanned event."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT id, planned_for FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        sets, params = [], []
        event = None
        if clear_plan:
            sets += ["planned_for = NULL", "plan_order = NULL"]
            event = ("unplanned", {"was": row["planned_for"], "via": "plan_task"})
        elif planned_for is not None:
            date = _valid_date(planned_for)
            if not date:
                return {"status": "error", "error": f"planned_for '{planned_for}' must be YYYY-MM-DD"}
            sets.append("planned_for = ?")
            params.append(date)
            event = ("planned", {"for": date, "via": "plan_task"})
        if plan_order is not None:
            sets.append("plan_order = ?")
            params.append(int(plan_order))
        if due_date is not None:
            if due_date == "":
                sets.append("due_date = NULL")
            else:
                dd, due_error = sprints.normalize_due_date(due_date)
                if due_error:
                    return {"status": "error", "error": due_error}
                sets.append("due_date = ?")
                params.append(dd)
        if not sets:
            return {"status": "error", "error": "nothing to set (planned_for/plan_order/due_date/clear_plan)"}
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*params, task_id))
        if event:
            _log(conn, task_id, event[0], event[1])
        conn.commit()
        out = conn.execute(
            "SELECT id, planned_for, plan_order, due_date FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return {"status": "ok", **dict(out)}
    finally:
        conn.close()


def plan_day(task_ids: list, date: Optional[str] = None, replace: bool = True) -> dict:
    """Commit a day's plan — the morning-standup confirm writes THIS (§ rituals
    write state). Sets planned_for=date and plan_order=list-position for each
    task; with replace=True (default) tasks previously planned for that date but
    absent from the list are unplanned (the plan is the whole day, idempotent).
    Every change is an event (planned/unplanned), so the commitment is auditable."""
    date = _valid_date(date)
    if not date:
        return {"status": "error", "error": "date must be YYYY-MM-DD"}
    task_ids = [t for t in (task_ids or []) if t]
    conn = db.get_conn()
    try:
        known = {r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE id IN (%s)" % ",".join("?" * len(task_ids)), task_ids
        ).fetchall()} if task_ids else set()
        missing = [t for t in task_ids if t not in known]
        if missing:
            return {"status": "error", "error": f"unknown task id(s): {', '.join(missing)}"}

        prior = {r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE planned_for = ?", (date,)
        ).fetchall()}

        planned, unplanned = [], []
        for i, tid in enumerate(task_ids):
            conn.execute(
                "UPDATE tasks SET planned_for = ?, plan_order = ? WHERE id = ?",
                (date, i, tid),
            )
            if tid not in prior:
                _log(conn, tid, "planned", {"for": date, "order": i, "via": "plan_day"})
                planned.append(tid)
        if replace:
            for tid in sorted(prior - set(task_ids)):
                conn.execute(
                    "UPDATE tasks SET planned_for = NULL, plan_order = NULL WHERE id = ?",
                    (tid,),
                )
                _log(conn, tid, "unplanned", {"was": date, "via": "plan_day"})
                unplanned.append(tid)
        conn.commit()
        return {"status": "ok", "date": date, "planned": task_ids,
                "newly_planned": planned, "unplanned": unplanned,
                "count": len(task_ids)}
    finally:
        conn.close()


# ---------------------------------------------------------------- the Today view

_TASK_FIELDS = (
    "t.id, t.title, t.status, t.priority, t.assignee, t.delegate, t.origin, "
    "t.project_id, t.planned_for, t.plan_order, t.due_date, t.progress_pct, "
    "t.progress_note, t.reviewed_at, t.rejection_reason, t.created_at, "
    "t.started_at, t.completed_at, t.last_failure_error, "
    "t.sprint_id, t.scheduled_week, t.pinned_bottom, "
    "p.name AS project_name, p.color AS project_color, p.icon AS project_icon, "
    "p.kind AS project_kind"
)
_TASK_JOIN = "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id"

# --- the commercial lineage (journey fase 1, step 4 / m06) -------------------
# The SECOND of the two chokepoints (verified: the spec assumed one). This one
# feeds Hoy / Later / the day plan; `db._TASK_SELECT` feeds the board's
# /api/tasks. BOTH have to widen or the contextChip's client branch stays
# dormant on whichever surface was missed — which is exactly how a chip that is
# "live" renders nothing on three of four surfaces.
_TASK_FIELDS_DEAL = (
    _TASK_FIELDS
    + ", t.deal_id, t.stage_kind, d.title AS deal_title, d.stage AS deal_stage, "
      "d.account_id AS account_id, a.name AS account_name, "
      "p.status AS project_status"
)
_TASK_JOIN_DEAL = (
    _TASK_JOIN
    + " LEFT JOIN deals d ON d.id = t.deal_id"
      " LEFT JOIN accounts a ON a.id = d.account_id"
)


def _rows(conn, where: str, params: tuple, order: str, limit: int = 200) -> list:
    """Every Today/Later row read goes through here — so the stage derivation
    does too, once, rather than per caller.

    The wide read is attempted first and falls back to the narrow one on an
    older schema (pre-m06, or a hand-built fixture DB with no `deals` table).
    Same reasoning as `db._select_tasks`: the canvas rendering nothing at all
    would be a far worse failure than it rendering without the client chip.
    """
    tail = f" WHERE {where} ORDER BY {order} LIMIT {int(limit)}"
    try:
        rows = conn.execute(
            f"SELECT {_TASK_FIELDS_DEAL} {_TASK_JOIN_DEAL}{tail}", params).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            f"SELECT {_TASK_FIELDS} {_TASK_JOIN}{tail}", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stage_kind"] = stagekind.derive(
            d, deal_stage=d.get("deal_stage"), project_status=d.get("project_status"))
        out.append(d)
    return out


def _review_where() -> str:
    """Awaiting the operator's accept/reject. `review` is the real status
    (Phase 3 item 2); ensure_schema migrates any stray done+unreviewed row an
    out-of-date external writer lands, so the status alone is authoritative."""
    return "t.status = 'review'"


def _group_later(later: list) -> dict:
    """Split the Later drawer into the cohesive sprint/week model's 4 buckets:

      this_week — committed to the ACTIVE cycle (just not planned for today)
      next_week — scheduled_week ≤ next ISO week, no sprint (stale tags stay
                  visible in the nearest actionable bucket, mirroring
                  sprints.get_next_week_tasks)
      future    — scheduled_week beyond next week, no sprint; ALSO tasks
                  committed to a non-active (planned-ahead) cycle
      backlog   — no sprint, no scheduled_week (truly unscheduled)

    Pure regrouping of the already-fetched rows — the flat `later` list stays in
    the response for existing consumers; this adds the lens, not a new query."""
    from . import sprints as _sprints
    try:
        active = _sprints.get_active_sprint()
    except Exception:
        active = None
    active_id = (active or {}).get("id")
    next_iso = _sprints._iso_week_str(offset_weeks=1)
    groups = {"this_week": [], "next_week": [], "future": [], "backlog": []}
    for t in later:
        if t.get("sprint_id"):
            groups["this_week" if t["sprint_id"] == active_id else "future"].append(t)
        elif not t.get("scheduled_week"):
            groups["backlog"].append(t)
        elif t["scheduled_week"] <= next_iso:
            groups["next_week"].append(t)
        else:
            groups["future"].append(t)
    return groups


def get_day_plan(date: Optional[str] = None, include_candidates: bool = False) -> dict:
    """The Today canvas, composed SERVER-SIDE (the Phase-3 ratchet: the dashboard
    tab, the MCP verb, and the rituals all read this one query surface):

      do       — planned_for=date, human-actor, grouped by project lane
      review   — status='review' (all delegates) → accept/reject queue
      needs_you— blocked tasks + unresolved session input_needed events
      later    — unplanned human work (the collapsed drawer)
      overdue  — due_date < today, unsettled → pinned red at the top
    """
    date = _valid_date(date)
    if not date:
        return {"status": "error", "error": "date must be YYYY-MM-DD"}
    today = _today()
    human_ph = ",".join("?" * len(_HUMAN))
    settled_ph = ",".join("?" * len(_SETTLED))
    conn = db.get_conn()
    try:
        do = _rows(
            conn,
            f"t.planned_for = ? AND (t.assignee IS NULL OR t.assignee IN ({human_ph})) "
            "AND t.project_id != ?",
            (date, *_HUMAN, _PERSONAL_PROJECT),
            "COALESCE(t.plan_order, 999), t.priority DESC, t.created_at ASC",
        )
        # Blocked plan cards carry WHY: the reason lives in task_events (the
        # report_blocked payload), not on the task row — attach the latest one.
        blocked_do = [r["id"] for r in do if r["status"] == "blocked"]
        if blocked_do:
            ph = ",".join("?" * len(blocked_do))
            reasons: dict = {}
            for ev in conn.execute(
                    f"SELECT task_id, payload FROM task_events "
                    f"WHERE kind = 'blocked' AND task_id IN ({ph}) ORDER BY created_at",
                    blocked_do):
                try:
                    reasons[ev["task_id"]] = (json.loads(ev["payload"] or "{}") or {}).get("reason")
                except (json.JSONDecodeError, TypeError):
                    pass
            for r in do:
                if r["status"] == "blocked":
                    r["blocked_reason"] = reasons.get(r["id"])
        # UX audit #2 (the operator's call): the Today Review zone hides tasks that
        # already carry a structured `result` — those were reported via
        # report_result and await bulk-accept, not daily attention. They still
        # surface in My Work's Inbox; only THIS zone filters.
        review = _rows(
            conn,
            _review_where()
            + " AND (t.result IS NULL OR t.result = '') AND t.project_id != ?",
            (_PERSONAL_PROJECT,),
            "t.completed_at DESC, t.created_at DESC",
        )
        # review_total counts EVERY review-status task (including the
        # result-carrying ones the zone hides) — it drives the bulk-accept
        # button, which must clear the whole queue, not just the visible slice.
        review_total = conn.execute(
            f"SELECT COUNT(*) FROM tasks t WHERE {_review_where()} AND t.project_id != ?",
            (_PERSONAL_PROJECT,),
        ).fetchone()[0]
        blocked = _rows(
            conn, "t.status = 'blocked' AND t.project_id != ?",
            (_PERSONAL_PROJECT,),
            "t.priority DESC, t.created_at ASC",
        )
        later = _rows(
            conn,
            f"(t.planned_for IS NULL OR t.planned_for != ?) "
            f"AND (t.assignee IS NULL OR t.assignee IN ({human_ph})) "
            f"AND t.status NOT IN ({settled_ph}) AND t.status NOT IN ('blocked', 'review') "
            "AND t.project_id != ?",
            (date, *_HUMAN, *_SETTLED, _PERSONAL_PROJECT),
            "t.priority DESC, t.created_at DESC", limit=50,
        )
        # The red pin is the UN-TRIAGED slice of overdue: work that is past due
        # and NOT yet in today's plan. Once it is planned it belongs to `do` and
        # `do` alone — the same `planned_for != date` exclusion `later` carries
        # two queries up, and for the same reason. Without it the row was in BOTH
        # zones, so the Today tab painted the identical card twice (a red pin with
        # a live "→ Today" button ABOVE the plan card it had already been pulled
        # into) and `counts.overdue` kept counting planned work — the client's
        # optimistic `_todayRemoveFromPools` hid it for exactly one render and the
        # next poll brought it straight back. One action, every projection moves.
        overdue = _rows(
            conn,
            f"t.due_date IS NOT NULL AND t.due_date < ? "
            f"AND (t.planned_for IS NULL OR t.planned_for != ?) "
            f"AND t.status NOT IN ({settled_ph}) AND t.project_id != ?",
            (today, date, *_SETTLED, _PERSONAL_PROJECT),
            "t.due_date ASC, t.priority DESC",
        )
        # done_today: tasks actually completed on `date` (by completed_at),
        # INDEPENDENT of whether a plan was committed. This is the fallback the
        # Today header shows so "N done today" is right even with an empty plan
        # (do_done only counts planned-and-done). localtime boundary matches the
        # wrap-up digest and the server's own _today().
        done_today = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at IS NOT NULL "
            "AND date(completed_at, 'unixepoch', 'localtime') = ?",
            (date,),
        ).fetchone()[0]
    finally:
        conn.close()

    # Unresolved "an agent needs your input" asks (session_events) join the
    # Needs-you zone — the second half of its definition. Two hygiene steps
    # (both best-effort; a sessions-probe failure must never break Today):
    # idle sessions' asks auto-resolve first (they stopped waiting), and each
    # surviving ask gets a HUMAN-READABLE session_display (project name) so the
    # zone never shows bare UUIDs.
    try:
        from . import orchestration as _orch
        sessions_data = None
        try:
            from . import sessions as _sessions
            sessions_data = _sessions.get_all_sessions()
            _orch.resolve_superseded_inputs(sessions_data)
        except Exception:
            pass
        idx = _orch._session_index(sessions_data) if sessions_data else {}
        pending_input = []
        for ev in _orch.pending_input():
            key = str(ev.get("session_key") or "")
            s = idx.get(key)
            # False-positive display filter: a session that is ACTIVELY working
            # right now isn't waiting on you — hide its ask (unresolved: if it
            # goes quiet with the ask still open, it reappears as genuine).
            if s and s.get("status") in ("active", "recent"):
                continue
            name = (s or {}).get("project") or ""
            if not name:
                meta = _orch.get_session_meta(key)
                name = (meta or {}).get("project") or ""
            # A path-shaped project reduces to its basename; else short-id.
            ev["session_display"] = name.rstrip("/").rsplit("/", 1)[-1] if name else key[:8]
            ev["host"] = ev.get("host") or "local"
            pending_input.append(ev)
    except Exception:
        pending_input = []

    candidates = None
    if include_candidates:
        candidates = plan_candidates(date)

    return {
        "date": date,
        "is_today": date == today,
        **({"candidates": candidates} if candidates is not None else {}),
        "do": do,
        "review": review,
        "needs_you": {"blocked": blocked, "input_needed": pending_input},
        "later": later,
        "later_groups": _group_later(later),
        "overdue": overdue,
        "counts": {
            "do": len(do),
            "do_done": sum(1 for t in do if t["status"] == "done"),
            "done_today": done_today,
            "review": len(review),
            "review_total": review_total,
            "needs_you": len(blocked) + len(pending_input),
            "later": len(later),
            "overdue": len(overdue),
        },
    }


# ---------------------------------------------------------------- rituals (write state)
# The 09:00 standup and 19:00 wrap-up are the CLOCK that turns the spine (D12:
# rituals write state, not narration). Standup: plan_candidates → the operator
# confirms on Telegram → Hermes calls plan_day. Wrap-up: wrap_day stamps
# carried_over events + returns the digest the ritual delivers/archives.

def plan_candidates(date: Optional[str] = None) -> dict:
    """Compose the CANDIDATE plan the standup proposes (server-side; the ritual
    never invents the list): carry-overs (planned before `date`, unfinished —
    the re-commit gate means they COMPETE, never auto-roll), active-cycle tasks,
    and overdue. Plus the review/needs-you counts the standup reports."""
    date = _valid_date(date)
    if not date:
        return {"status": "error", "error": "date must be YYYY-MM-DD"}
    human_ph = ",".join("?" * len(_HUMAN))
    settled_ph = ",".join("?" * len(_SETTLED))
    unworkable_ph = ",".join("?" * len(_UNWORKABLE))
    conn = db.get_conn()
    try:
        carry = _rows(
            conn,
            f"t.planned_for IS NOT NULL AND t.planned_for < ? "
            f"AND (t.assignee IS NULL OR t.assignee IN ({human_ph})) "
            f"AND t.status NOT IN ({settled_ph}) AND t.status NOT IN ({unworkable_ph}) "
            "AND t.project_id != ?",
            (date, *_HUMAN, *_SETTLED, *_UNWORKABLE, _PERSONAL_PROJECT),
            "t.planned_for DESC, t.priority DESC", limit=30,
        )
        cycle = conn.execute(
            "SELECT id FROM sprints WHERE status = 'active' ORDER BY start_date DESC LIMIT 1"
        ).fetchone()
        cycle_tasks = _rows(
            conn,
            f"t.sprint_id = ? AND (t.assignee IS NULL OR t.assignee IN ({human_ph})) "
            f"AND t.status NOT IN ({settled_ph}) AND t.status NOT IN ({unworkable_ph}) "
            f"AND (t.planned_for IS NULL OR t.planned_for != ?) "
            "AND t.project_id != ?",
            (cycle["id"] if cycle else "", *_HUMAN, *_SETTLED, *_UNWORKABLE, date,
             _PERSONAL_PROJECT),
            "t.priority DESC, t.created_at ASC", limit=30,
        ) if cycle else []
        # `planned_for != date` for the same reason carry/cycle/cliente carry it:
        # a CANDIDATE is work you have not committed to yet. A card already in
        # today's plan proposed back to you at standup — or offered again in the
        # shelf's Overdue band — is the plan disagreeing with itself.
        overdue = _rows(
            conn,
            f"t.due_date IS NOT NULL AND t.due_date < ? "
            f"AND (t.planned_for IS NULL OR t.planned_for != ?) "
            f"AND t.status NOT IN ({settled_ph}) AND t.status NOT IN ({unworkable_ph}) "
            "AND t.project_id != ?",
            (date, date, *_SETTLED, *_UNWORKABLE, _PERSONAL_PROJECT),
            "t.due_date ASC, t.priority DESC",
        )
        # --- the FOURTH source: commercial work (journey fase 1, step 5) -----
        # Sales activity used to live in nurture_sequences / next_touch_date and
        # was therefore invisible to the one surface the operator actually opens at
        # 08:00. Any OPEN task that names a deal is a candidate — the cadence
        # materializer's cards, and equally a manual task the operator linked to
        # a client through the drawer. The `why` label is what routes it into the
        # planner's fifth band ("Cliente / venta").
        #
        # Its precedence is LAST in the dedup loop below on purpose: a client
        # card that is also overdue must render as OVERDUE. Urgency is the more
        # actionable of the two labels, and a card can only carry one.
        #
        # Falls back to [] on a pre-m06 schema — `_rows` degrades to the narrow
        # SELECT there, but this WHERE names `t.deal_id` directly, so the column
        # has to exist for the query to parse at all.
        try:
            cliente = _rows(
                conn,
                f"t.deal_id IS NOT NULL "
                f"AND (t.assignee IS NULL OR t.assignee IN ({human_ph})) "
                f"AND t.status NOT IN ({settled_ph}) AND t.status NOT IN ({unworkable_ph}) "
                f"AND (t.planned_for IS NULL OR t.planned_for != ?) "
                "AND t.project_id != ?",
                (*_HUMAN, *_SETTLED, *_UNWORKABLE, date, _PERSONAL_PROJECT),
                "COALESCE(t.due_date, '9999-12-31') ASC, t.priority DESC", limit=30,
            )
        except sqlite3.OperationalError:
            cliente = []
        review_count = conn.execute(
            f"SELECT COUNT(*) FROM tasks t WHERE {_review_where()} AND t.project_id != ?",
            (_PERSONAL_PROJECT,),
        ).fetchone()[0]
        blocked_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'blocked' AND project_id != ?",
            (_PERSONAL_PROJECT,),
        ).fetchone()[0]
    finally:
        conn.close()

    seen, candidates = set(), []
    for why, group in (("overdue", overdue), ("carry_over", carry),
                       ("cycle", cycle_tasks), ("cliente", cliente)):
        for t in group:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            candidates.append({**t, "why": why})
    return {
        "date": date,
        "candidates": candidates,
        "cycle_id": cycle["id"] if cycle else None,
        "review_count": review_count,
        "blocked_count": blocked_count,
    }


def wrap_day(date: Optional[str] = None) -> dict:
    """The 19:00 wrap-up write: stamp a `carried_over` event on every task
    still unfinished in `date`'s plan (idempotent — one event per task per
    date) and return the day's digest (done / carried / review / blocked) for
    the ritual to deliver + archive. planned_for is NOT advanced: tomorrow's
    standup re-proposes carry-overs and they compete fresh (Shape Up #5)."""
    date = _valid_date(date)
    if not date:
        return {"status": "error", "error": "date must be YYYY-MM-DD"}
    settled_ph = ",".join("?" * len(_SETTLED))
    conn = db.get_conn()
    try:
        planned = _rows(conn, "t.planned_for = ?", (date,),
                        "COALESCE(t.plan_order, 999)")
        carried = []
        for t in planned:
            if t["status"] in _SETTLED:
                continue
            dup = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'carried_over' "
                "AND json_extract(payload, '$.from') = ? LIMIT 1",
                (t["id"], date),
            ).fetchone()
            if not dup:
                _log(conn, t["id"], "carried_over", {"from": date, "via": "wrap_day"})
            carried.append(t)
        review = _rows(conn, _review_where(), (), "t.completed_at DESC")
        blocked = _rows(conn, "t.status = 'blocked'", (), "t.priority DESC")
        accepted_today = _rows(
            conn,
            "t.status = 'done' AND t.completed_at IS NOT NULL "
            "AND date(t.completed_at, 'unixepoch', 'localtime') = ?",
            (date,), "t.completed_at DESC",
        )
        conn.commit()
    finally:
        conn.close()
    done_planned = [t for t in planned if t["status"] == "done"]
    return {
        "date": date,
        "digest": {
            "planned": len(planned),
            "done_from_plan": len(done_planned),
            "carried_over": len(carried),
            "done_today_total": len(accepted_today),
            "awaiting_review": len(review),
            "blocked": len(blocked),
        },
        "done": done_planned,
        "carried": carried,
        "done_today": accepted_today,
        "review": review,
        "blocked": blocked,
    }
