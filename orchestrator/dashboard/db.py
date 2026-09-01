"""
Hermes Orchestrator Dashboard — Database read layer.
Reads directly from the Hermes kanban SQLite DB (~/.hermes/kanban.db).
All writes go through `hermes kanban` CLI to avoid schema coupling.
"""
import os
import sqlite3
import time
import json
import datetime as _dt
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

# Pure module (no DB handle, no clock, no dashboard imports) — safe to import
# from the row-mapping hot path without a cycle.
from . import stagekind as _stagekind

KANBAN_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
    else Path.home() / ".hermes" / "kanban.db"


# --- the ONE dashboard address --------------------------------------------
#
# Every deep link the system emits — the 3x-daily Telegram brief (`brief.
# entity_link`), the dispatch task link, the MCP `get_dashboard_url` answer —
# is only useful if the URL resolves on the device that reads it. The operator
# reads them on their phone.
#
# There used to be three answers: `dispatch._dashboard_url()` and
# `mcp_server.DASHBOARD_URL` both defaulted to `http://127.0.0.1:3000`
# (loopback — every link tapped from Telegram was dead), and
# `tool_get_dashboard_url` ANNOUNCED a third, divergent one with the wrong
# protocol. Three constants that agree are not one source of truth; they are
# three that have not drifted yet. This module is the one both processes
# already import, so the resolver lives here and everything else delegates.
#
# The tenant's reachable address (typically the tailscale-serve HTTPS front
# that terminates onto the dashboard's loopback bind) is CONFIG, not code: it
# lives in ~/.config/orchestratormaxxing/fleet.env as ORCHESTRATORMAXXING_DASHBOARD_URL,
# deployed per machine by install-fleet.sh. The shipped default is the
# dashboard's own loopback bind — the only address true on every machine with
# no configuration at all.
DASHBOARD_URL_DEFAULT = "http://127.0.0.1:3000"


def _fleet_dashboard_url() -> str:
    """ORCHESTRATORMAXXING_DASHBOARD_URL from fleet.env (KEY=VALUE lines, `#`
    comments, optional `export ` prefix and double quotes, no shell
    expansion — same grammar as bin/agent-done-notify's reader). A missing
    file or key degrades to "": a machine without fleet.env is a standalone
    client. Path override: $ORCHESTRATORMAXXING_FLEET_ENV."""
    override = os.environ.get("ORCHESTRATORMAXXING_FLEET_ENV", "").strip()
    path = Path(override) if override else \
        Path.home() / ".config" / "orchestratormaxxing" / "fleet.env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if key == "ORCHESTRATORMAXXING_DASHBOARD_URL":
                return value
    except (OSError, UnicodeError, ValueError):
        pass
    return ""


def dashboard_url() -> str:
    """The dashboard's base URL, without a trailing slash.

    Resolution: `$DASHBOARD_URL` (legacy explicit override — one line, no
    code change, which is how a tunnel or a rename is absorbed) >
    `$ORCHESTRATORMAXXING_DASHBOARD_URL` (process env) > fleet.env > the neutral
    loopback default. An empty value is not a URL and falls through. The
    slash is stripped HERE so no caller has to think about whether its own
    `f"{base}/?entity=…"` will double it."""
    return (os.environ.get("DASHBOARD_URL")
            or os.environ.get("ORCHESTRATORMAXXING_DASHBOARD_URL")
            or _fleet_dashboard_url()
            or DASHBOARD_URL_DEFAULT).rstrip("/")


def hermes_bin() -> str:
    """Absolute path to the hermes CLI: PATH lookup first, else the standard
    install location. A systemd --user unit's default PATH lacks ~/.local/bin,
    so a bare "hermes" subprocess works in a dev shell and FileNotFoundErrors
    after a reboot (2026-07-28: every POST /api/tasks 500'd this way)."""
    import shutil
    return shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")


def live_db_path() -> Path:
    """The operator's real kanban DB — the one path a test run must never open."""
    return Path.home() / ".hermes" / "kanban.db"


def assert_not_live_db(path) -> None:
    """Data-loss tripwire: refuse to open the operator's live kanban.db from a
    test run.

    Twice (2026-07-29: 600+ rows; 2026-07-31: 34 fixture deals, 25 fixture
    projects, plus accounts/contacts/events) the pytest suite wrote fixtures
    straight into ~/.hermes/kanban.db. The mechanism is a shared module global:
    test modules repoint `db.KANBAN_DB` at a tmp copy at IMPORT time, and a
    handful hand it back to the *real* path when their import finishes. pytest
    imports every test module during collection BEFORE running any test, so the
    last such module leaves the global pointing at the live DB — and every test
    class that redirected only at import time (test_crm_growth, test_readiness,
    …) then runs its fixtures through it.

    A conftest sandbox fixes the default; this makes a regression LOUD instead
    of silent. Gated strictly on a test marker in the environment:
      * `TESTING` — set by tests/conftest.py,
      * `PYTEST_CURRENT_TEST` — set by pytest itself for every test.
    Production never sets either: the systemd unit
    (~/.config/systemd/user/hermes-dashboard.service{,.d/env.conf}) sets only
    DASHBOARD_BIND/PORT, FIREFLIES_API_KEY, HERMES_KANBAN_DB and PATH — so the
    guard cannot fire against the running dashboard, which legitimately resolves
    exactly this path.
    """
    if not (os.environ.get("TESTING") or os.environ.get("PYTEST_CURRENT_TEST")):
        return
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
        live = os.path.realpath(str(live_db_path()))
    except Exception:  # pragma: no cover — never let the guard itself break a run
        return
    if resolved == live:
        raise RuntimeError(
            "test run resolved the LIVE kanban.db "
            f"({live}). Refusing to open it — a test that writes here corrupts "
            "the operator's CRM (happened 2026-07-29 and 2026-07-31). Point "
            "db.KANBAN_DB / sprints.KANBAN_DB (or $HERMES_KANBAN_DB) at the "
            "per-session sandbox copy created by tests/conftest.py instead of "
            "at Path.home()/'.hermes'/'kanban.db'."
        )


def get_conn():
    assert_not_live_db(KANBAN_DB)
    conn = sqlite3.connect(str(KANBAN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    # Phase 1 (item 1): enforce declared foreign keys on every connection. SQLite
    # defaults foreign_keys OFF *per connection*, so this must be set here (not
    # once globally). sprints.get_conn already does it; db.get_conn is the shared
    # factory behind graph/loop/orchestration/memory, so this aligns them all.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --- Canonical project namespace (Phase 1, item 1) -------------------------
# projects.slug is THE ONE FK namespace: every registry (tasks.project_id,
# sprints.project_id, epics.project_id, roadmap initiatives, graph Project nodes,
# memory/jsonl dirs) resolves a project through this single function. A project
# is referable by its id (proj_*) or its slug; resolve_project collapses both
# to the canonical projects.id so no layer invents its own name space. (The
# legacy "hermes" alias died with the Phase-2 roadmap.json migration — every
# registry now stores canonical ids.)


def resolve_project(ref: Optional[str], conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Map any project reference (id | slug) → canonical projects.id.
    Returns None if it resolves to nothing (an orphan key). Accepts an optional
    open connection so callers inside a transaction don't reopen the DB."""
    if not ref:
        return None
    own = conn is None
    conn = conn or get_conn()
    try:
        # already a canonical id?
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (ref,)).fetchone()
        if row:
            return row["id"]
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (ref,)).fetchone()
        return row["id"] if row else None
    finally:
        if own:
            conn.close()


@dataclass
class Task:
    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_path: Optional[str]
    result: Optional[str]
    consecutive_failures: int
    last_failure_error: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str] = None
    sprint_id: Optional[str] = None
    initiative_id: Optional[str] = None
    reviewed_at: Optional[int] = None
    pool: int = 0
    autonomy: Optional[str] = None
    progress_note: Optional[str] = None
    progress_pct: Optional[int] = None
    rejection_reason: Optional[str] = None
    origin: Optional[str] = None
    owner: Optional[str] = None
    delegate: Optional[str] = None
    parent_id: Optional[str] = None
    epic_id: Optional[str] = None
    planned_for: Optional[str] = None
    plan_order: Optional[int] = None
    due_date: Optional[str] = None
    scheduled_week: Optional[str] = None
    contract_cmd: Optional[str] = None
    current_run_id: Optional[int] = None
    current_step_key: Optional[str] = None
    archived_at: Optional[int] = None
    pinned_bottom: int = 0
    # --- the commercial lineage (journey fase 1, step 4 / m06) ---------------
    # `deal_id` is the stored fact; everything under it is JOINED, not stored —
    # the board feed is the surface the contextChip's client branch reads, and a
    # chip that had to fetch the deal per card would be a round-trip per card.
    # They are plain dataclass fields (not properties) so `to_dict()` — which is
    # `asdict()` — carries them into the /api/tasks payload.
    deal_id: Optional[str] = None
    deal_title: Optional[str] = None
    deal_stage: Optional[str] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    project_status: Optional[str] = None
    # DERIVED at read time by `dashboard/stagekind.py` unless the row carries an
    # explicit value (the materializer's stamp, or the operator's correction).
    stage_kind: Optional[str] = None

    @property
    def assignee_type(self) -> str:
        """Classify assignee for dashboard coloring."""
        if not self.assignee:
            return "unassigned"
        a = self.assignee.lower()
        if a in ("ricardo", "user"):
            return "human"
        if "claude" in a:
            return "claude"
        if "opencode" in a or "kimi" in a or "glm" in a:
            return "opencode"
        if a in ("default", "hermes"):
            return "hermes"
        return "other"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def to_dict(self) -> dict:
        d = asdict(self)
        d["assignee_type"] = self.assignee_type
        d["age_seconds"] = self.age_seconds
        # Set by api.enrich_tasks_with_sessions; asdict() only sees dataclass fields
        d["session_link"] = getattr(self, "session_link", None)
        # Set by api.attach_ledger_digest (review tasks only): compact files/risks
        # counts so the Review card can show them without a per-card fetch.
        d["ledger_digest"] = getattr(self, "ledger_digest", None)
        return d


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        assignee=row["assignee"],
        status=row["status"],
        priority=row["priority"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        workspace_path=row["workspace_path"],
        result=row["result"],
        consecutive_failures=row["consecutive_failures"],
        last_failure_error=row["last_failure_error"],
        session_id=row["session_id"],
        project_id=row["project_id"] if "project_id" in row.keys() else None,
        sprint_id=row["sprint_id"] if "sprint_id" in row.keys() else None,
        initiative_id=row["initiative_id"] if "initiative_id" in row.keys() else None,
        reviewed_at=row["reviewed_at"] if "reviewed_at" in row.keys() else None,
        pool=(row["pool"] if "pool" in row.keys() and row["pool"] is not None else 0),
        autonomy=row["autonomy"] if "autonomy" in row.keys() else None,
        progress_note=row["progress_note"] if "progress_note" in row.keys() else None,
        progress_pct=row["progress_pct"] if "progress_pct" in row.keys() else None,
        rejection_reason=row["rejection_reason"] if "rejection_reason" in row.keys() else None,
        origin=row["origin"] if "origin" in row.keys() else None,
        owner=row["owner"] if "owner" in row.keys() else None,
        delegate=row["delegate"] if "delegate" in row.keys() else None,
        parent_id=row["parent_id"] if "parent_id" in row.keys() else None,
        epic_id=row["epic_id"] if "epic_id" in row.keys() else None,
        planned_for=row["planned_for"] if "planned_for" in row.keys() else None,
        plan_order=row["plan_order"] if "plan_order" in row.keys() else None,
        due_date=row["due_date"] if "due_date" in row.keys() else None,
        scheduled_week=row["scheduled_week"] if "scheduled_week" in row.keys() else None,
        contract_cmd=row["contract_cmd"] if "contract_cmd" in row.keys() else None,
        current_run_id=row["current_run_id"] if "current_run_id" in row.keys() else None,
        current_step_key=row["current_step_key"] if "current_step_key" in row.keys() else None,
        archived_at=row["archived_at"] if "archived_at" in row.keys() else None,
        pinned_bottom=(row["pinned_bottom"]
                       if "pinned_bottom" in row.keys() and row["pinned_bottom"] is not None else 0),
        deal_id=_opt(row, "deal_id"),
        deal_title=_opt(row, "deal_title"),
        deal_stage=_opt(row, "deal_stage"),
        account_id=_opt(row, "account_id"),
        account_name=_opt(row, "account_name"),
        project_status=_opt(row, "project_status"),
        # Explicit value wins; otherwise the rule reads the joined facts. Done
        # HERE, in the one row-mapper every task read goes through, so the board,
        # the drawer and the Today canvas can never disagree about a task's
        # stage — the failure mode of deriving it per-surface.
        stage_kind=_stagekind.derive(
            row,
            deal_stage=_opt(row, "deal_stage"),
            project_status=_opt(row, "project_status")),
    )


def _opt(row: sqlite3.Row, name: str):
    """A column that may not be in this SELECT (or in this schema) yet."""
    return row[name] if name in row.keys() else None


# The board feed's read. `tasks` alone can answer "which deal" but not "which
# CLIENT", and the contextChip's client branch needs the account NAME on every
# card — so the join happens once here rather than a fetch per card.
#
# LEFT joins throughout: a task with no deal, a deal with no account and a task
# with no project must all still come back. `t.*` keeps every existing column
# flowing untouched, so nothing that read a task field before can notice this.
_TASK_SELECT = (
    "SELECT t.*, d.title AS deal_title, d.stage AS deal_stage, "
    "       d.account_id AS account_id, a.name AS account_name, "
    "       p.status AS project_status "
    "FROM tasks t "
    "LEFT JOIN deals d ON d.id = t.deal_id "
    "LEFT JOIN accounts a ON a.id = d.account_id "
    "LEFT JOIN projects p ON p.id = t.project_id"
)


def _select_tasks(conn, where: str = "", params: tuple = (), order: str = "") -> list:
    """Run the joined read, degrading to the bare table on an older schema.

    `tasks.deal_id` arrives in m06 and `deals`/`accounts` in the CRM's own
    ensure_schema, so the joined SELECT is not valid on every DB this module can
    be pointed at (a pre-migration copy, a fixture DB built by hand). The
    fallback is not defensive noise: the dashboard runs its migrations at import
    and a hard failure here would mean the board renders nothing at all on a DB
    that is merely OLD. The narrow read returns the same rows minus the joined
    columns, and `_opt` already treats those as absent-is-None.
    """
    tail = f" WHERE {where}" if where else ""
    tail += f" ORDER BY {order}" if order else ""
    try:
        return conn.execute(_TASK_SELECT + tail, params).fetchall()
    except sqlite3.OperationalError:
        return conn.execute("SELECT t.* FROM tasks t" + tail, params).fetchall()


def get_all_tasks() -> list[Task]:
    conn = get_conn()
    try:
        rows = _select_tasks(conn, order="t.created_at DESC")
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()


def get_task(task_id: str) -> Optional[Task]:
    conn = get_conn()
    try:
        rows = _select_tasks(conn, where="t.id = ?", params=(task_id,))
        return _row_to_task(rows[0]) if rows else None
    finally:
        conn.close()


# --- Kanban card flags -----------------------------------------------------
# pinned_bottom is a *display* flag, not a lifecycle state: a task parked because
# it's waiting on someone else sinks to the bottom of its column while keeping its
# status. This is deliberately NOT `blocked` — blocked means an agent cannot
# proceed, which changes dispatch; parked changes only where the card sits.

def ensure_task_flags_schema() -> None:
    """Idempotently add the kanban card-flag columns to tasks. Safe every boot."""
    conn = get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "pinned_bottom" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN pinned_bottom INTEGER DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def set_pinned_bottom(task_id: str, pinned: bool) -> dict:
    """Park (or un-park) a task at the bottom of its kanban column.
    Returns {"status": "error", ...} when the task doesn't exist, matching the
    house convention the HTTP/MCP layers 404 on."""
    conn = get_conn()
    try:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            return {"status": "error", "error": f"task not found: {task_id}"}
        val = 1 if pinned else 0
        conn.execute("UPDATE tasks SET pinned_bottom = ? WHERE id = ?", (val, task_id))
        conn.commit()
        return {"status": "ok", "id": task_id, "pinned_bottom": val}
    finally:
        conn.close()


def get_task_comments(task_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_task_ledger(task_id: str, limit: int = 20) -> list[dict]:
    """Verification ledger for one task — each report_ledger row: what the agent
    did (summary), which files it touched, the risks it flagged, and whether the
    contract passed. Newest first. Powers the Review-card + drawer surfacing of
    agent work that was previously written to the DB with no read path."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, session_key, agent, role, summary, files_modified, risks, "
            "status, passed, created_at "
            "FROM task_ledger WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (task_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_task_events(task_id: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_task_runs(task_id: str, limit: int = 20) -> list:
    """Execution history for one task (audit Tier-1 gap: 40 task_runs rows,
    no read surface): attempts, steps, outcomes, crashes — newest first."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, profile, step_key, status, outcome, claim_lock, worker_pid, "
            "started_at, ended_at, last_heartbeat_at, summary, error "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_task_links(task_id: str) -> dict:
    """Return parent/child links for a task.

    task_links rows are (parent_id, child_id): parent_id is the parent OF
    child_id (see loop.escalate_discovery, which inserts (related_task, new_id)).
    Phase 0 (item 5): the two queries were inverted — this task's PARENTS are the
    rows where it is the child_id (read parent_id), and its CHILDREN are the rows
    where it is the parent_id (read child_id). The old code swapped both, so the
    graph showed every parent/child relationship backwards."""
    conn = get_conn()
    try:
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
        ).fetchall()
        children = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,)
        ).fetchall()
        return {
            "parents": [r[0] for r in parents],
            "children": [r[0] for r in children],
        }
    finally:
        conn.close()


def add_task_link(parent_id: str, child_id: str) -> dict:
    """Create a parent→child dependency edge in the task_links DAG. Idempotent
    (INSERT OR IGNORE). Both ends validated so a bad id can't orphan an edge; a
    self-link is rejected. Mirrors the insert path in loop.escalate_discovery."""
    if not parent_id or not child_id:
        return {"error": "both parent_id and child_id are required"}
    if parent_id == child_id:
        return {"error": "a task cannot depend on itself"}
    conn = get_conn()
    try:
        for tid in (parent_id, child_id):
            if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone():
                return {"error": f"task '{tid}' not found"}
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id))
        conn.commit()
        return {"status": "linked", "parent_id": parent_id, "child_id": child_id}
    finally:
        conn.close()


def remove_task_link(parent_id: str, child_id: str) -> dict:
    """Drop a parent→child edge (no-op if it doesn't exist)."""
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id))
        conn.commit()
        return {"status": "unlinked", "parent_id": parent_id, "child_id": child_id}
    finally:
        conn.close()


def get_stats() -> dict:
    """Summary stats for dashboard."""
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]
        by_assignee = {}
        for row in conn.execute(
            "SELECT assignee, COUNT(*) as cnt FROM tasks GROUP BY assignee"
        ).fetchall():
            by_assignee[row["assignee"] or "unassigned"] = row["cnt"]
        # Phase 1 (item 4): surface the untriaged-Inbox count so it can't silently
        # grow back to 44 orphans. Non-done tasks in the inbox project.
        inbox_count = conn.execute(
            "SELECT COUNT(*) FROM tasks t JOIN projects p ON t.project_id = p.id "
            "WHERE p.slug = 'inbox' AND t.status != 'done'"
        ).fetchone()[0]
        return {
            "total": total,
            "by_status": by_status,
            "by_assignee": by_assignee,
            "inbox_count": inbox_count,
        }
    finally:
        conn.close()


def _projects_map() -> dict:
    """id → {name, color, icon} for enriching tasks with project display data."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, name, color, icon FROM projects").fetchall()
        return {r["id"]: {"name": r["name"], "color": r["color"], "icon": r["icon"]} for r in rows}
    finally:
        conn.close()


def delete_project(project_id: str) -> dict:
    """Guarded hard-delete of a project. REFUSES if it still owns tasks (re-home
    them first — the triage safeguard from the brief) and refuses proj_inbox (the
    triage sink is undeletable). Removes the last raw-SQL-only project cleanup."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, name FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "project not found"}
        if project_id == "proj_inbox":
            return {"status": "error", "error": "cannot delete proj_inbox (the triage inbox)"}
        n = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)).fetchone()[0]
        if n:
            return {"status": "error",
                    "error": f"project still owns {n} task(s) — re-home them first"}
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        return {"status": "deleted", "project_id": project_id, "name": row["name"]}
    finally:
        conn.close()


def get_archive(group: str = "day") -> dict:
    """The task graveyard: all completed tasks, organized by *when* they were
    done (calendar time). A read-only projection over completed_at — not a new
    status. Shows ALL assignees (most done work is agent work). Buckets by
    day / week / month."""
    import datetime as _dt

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'done' AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC"
        ).fetchall()
    finally:
        conn.close()

    pmap = _projects_map()

    def bucket_key_label(ts: int):
        d = _dt.datetime.fromtimestamp(ts)
        if group == "month":
            return d.strftime("%Y-%m"), d.strftime("%B %Y")
        if group == "week":
            iso = d.isocalendar()  # (year, week, weekday)
            monday = d - _dt.timedelta(days=d.weekday())
            return f"{iso[0]}-W{iso[1]:02d}", "Week of " + monday.strftime("%b %-d, %Y")
        # day
        return d.strftime("%Y-%m-%d"), d.strftime("%a, %b %-d, %Y")

    buckets = {}
    order = []
    for r in rows:
        task = _row_to_task(r).to_dict()
        p = pmap.get(task.get("project_id"))
        task["project_name"] = p["name"] if p else None
        task["project_color"] = p["color"] if p else None
        key, label = bucket_key_label(r["completed_at"])
        if key not in buckets:
            buckets[key] = {"key": key, "label": label, "count": 0, "tasks": []}
            order.append(key)
        buckets[key]["tasks"].append(task)
        buckets[key]["count"] += 1

    return {
        "group": group,
        "total": len(rows),
        "buckets": [buckets[k] for k in order],
    }


def get_task_history(task_id: str) -> list[dict]:
    """Full audit trail for a task, oldest → newest, from the task_events log.
    status_changed events are normalized to {from, to}."""
    import json as _json
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        payload = {}
        if r["payload"]:
            try:
                payload = _json.loads(r["payload"])
            except Exception:
                payload = {}
        item = {"kind": r["kind"], "created_at": r["created_at"], "payload": payload}
        if r["kind"] == "status_changed":
            item["from"] = payload.get("from")
            item["to"] = payload.get("to")
        out.append(item)
    return out


# --- ICP config (Strategy → ICP Editor) ------------------------------------
# A tiny key→value store for the operator's Ideal-Customer-Profile / growth
# targets (industries, positioning, revenue goal, avg ticket, close rate).
# Previously these lived only in HERMES_* env vars; this makes them editable at
# runtime while env stays the fallback (see growth.icp_config). One row per key.

def ensure_icp_schema() -> None:
    """Create the icp_config table if missing. Idempotent — safe every boot."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS icp_config ("
            " key TEXT PRIMARY KEY,"
            " value TEXT,"
            " updated_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def get_icp_config() -> dict:
    """Return {key: value} for every stored ICP setting ({} if none/no table)."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM icp_config").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except sqlite3.OperationalError:  # table not created yet
        return {}
    finally:
        conn.close()


def set_icp_config(items: dict) -> None:
    """Upsert each key→value (values stored as TEXT) with an updated_at stamp."""
    if not items:
        return
    ensure_icp_schema()
    now = int(time.time())
    conn = get_conn()
    try:
        for key, value in items.items():
            conn.execute(
                "INSERT INTO icp_config (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now),
            )
        conn.commit()
    finally:
        conn.close()


# --- Product catalog (Strategy → productized offers) -----------------------
# Ricardo's productized offers, each pinned to a value-ladder rung. Schema lives
# here; validation + the 3 seed defaults live in growth.py (product_* wrappers).

def ensure_products_schema() -> None:
    """Create the products table if missing. Idempotent — safe every boot."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS products ("
            " id TEXT PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " description TEXT,"
            " value_ladder_stage TEXT,"      # iman/entrada/core/recurrente
            " fixed_price_mxn REAL,"
            " ficha_html TEXT,"
            " created_at INTEGER,"
            " track TEXT)"                  # A = Datos→IA, B = Producto con Agentes
        )
        # Additive migration: revenue_model (recurring vs one-off) + recurrence_pattern + track
        pcols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
        for col in ("revenue_model", "recurrence_pattern", "track"):
            if col not in pcols:
                conn.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
        # Link deals to products
        dcols = [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        if "product_id" not in dcols:
            conn.execute("ALTER TABLE deals ADD COLUMN product_id TEXT")
        conn.commit()
    finally:
        conn.close()


def products_all() -> list[dict]:
    """Every product, oldest first ([] if the table doesn't exist yet)."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM products ORDER BY created_at ASC, id ASC").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def product_get(pid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def product_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO products (id, name, description, value_ladder_stage, "
            "fixed_price_mxn, ficha_html, created_at, track) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["name"], row.get("description"),
             row.get("value_ladder_stage"), row.get("fixed_price_mxn"),
             row.get("ficha_html"), row["created_at"],
             row.get("track") or "A"),
        )
        conn.commit()
    finally:
        conn.close()


def product_update(pid: str, fields: dict) -> bool:
    """Update whitelisted columns. Caller (growth.update_product) supplies only
    validated column names — never raw client keys (SQL-identifier safety)."""
    if not fields:
        return product_get(pid) is not None
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [pid]
    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE products SET {cols} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def product_delete(pid: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- 90-Day Plan tracker (Strategy → playbook execution) -------------------
# The playbook's 3-phase, 90-day plan as checkable milestones. Schema here;
# phase metadata + the seed defaults live in growth.py (plan_milestone_* wrappers).

def ensure_plan_schema() -> None:
    """Create the plan_milestones table if missing. Idempotent — safe every boot."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_milestones ("
            " id TEXT PRIMARY KEY,"
            " phase TEXT NOT NULL,"            # fundaciones/motor/optimizacion
            " title TEXT NOT NULL,"
            " description TEXT,"
            " sort_order INTEGER,"
            " completed INTEGER NOT NULL DEFAULT 0,"
            " completed_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def plan_milestones_all() -> list[dict]:
    """Every milestone, in plan order ([] if the table doesn't exist yet)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM plan_milestones ORDER BY sort_order ASC, id ASC").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def plan_milestone_get(mid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM plan_milestones WHERE id = ?", (mid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def plan_milestone_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO plan_milestones (id, phase, title, description, sort_order, "
            "completed, completed_at) VALUES (?,?,?,?,?,?,?)",
            (row["id"], row["phase"], row["title"], row.get("description"),
             row.get("sort_order"), row.get("completed", 0), row.get("completed_at")),
        )
        conn.commit()
    finally:
        conn.close()


def plan_milestone_set_completed(mid: str, completed: int, completed_at) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE plan_milestones SET completed = ?, completed_at = ? WHERE id = ?",
            (completed, completed_at, mid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Acquisition costs (Growth → CLTV:CAC) ---------------------------------
# Monthly marketing/acquisition spend per lead source. Feeds CAC = spend /
# customers-acquired. Schema here; validation + CLTV:CAC math in growth.py.

def ensure_acquisition_schema() -> None:
    """Create the acquisition_costs table if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_costs ("
            " id TEXT PRIMARY KEY,"
            " source TEXT NOT NULL,"          # LEAD_SOURCE vocabulary
            " cost_mxn REAL NOT NULL DEFAULT 0,"
            " month TEXT,"                    # YYYY-MM
            " notes TEXT,"
            " created_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def acquisition_costs_all() -> list[dict]:
    """Every cost row, newest month first ([] if the table doesn't exist yet)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM acquisition_costs ORDER BY month DESC, source ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def acquisition_cost_get(cid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM acquisition_costs WHERE id = ?", (cid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def acquisition_cost_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO acquisition_costs (id, source, cost_mxn, month, notes, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (row["id"], row["source"], row.get("cost_mxn", 0), row.get("month"),
             row.get("notes"), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()


def acquisition_cost_delete(cid: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM acquisition_costs WHERE id = ?", (cid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def acquisition_cost_totals_by_source() -> dict:
    """{source: total_cost_mxn} summed across all recorded months."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT source, SUM(cost_mxn) AS total FROM acquisition_costs "
            "GROUP BY source").fetchall()
        return {r["source"]: (r["total"] or 0.0) for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


# --- Events + attendance (Growth Operating Framework, Phase 2) ---------------
# Events are pipeline-generating moments (conferences, meetups, online talks)
# and attendance links captured contacts to them.

def ensure_events_schema() -> None:
    """Create events + event_attendance tables if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " id TEXT PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " event_date TEXT,"              # ISO date
            " kind TEXT,"                    # conference|meetup|online|clinic|other
            " location TEXT,"
            " cta TEXT,"
            " prep TEXT,"                   # JSON {targets[],relationships[],connectors[]}
            " notes TEXT,"
            " created_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS event_attendance ("
            " id TEXT PRIMARY KEY,"
            " event_id TEXT NOT NULL,"
            " contact_id TEXT NOT NULL,"
            " deal_id TEXT,"
            " captured_at INTEGER)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_attendance_event "
            "ON event_attendance(event_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_attendance_contact "
            "ON event_attendance(contact_id)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_attendance_unique "
            "ON event_attendance(event_id, contact_id)")
        conn.commit()
    finally:
        conn.close()


def events_all() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT e.*, (SELECT COUNT(*) FROM event_attendance ea WHERE ea.event_id = e.id) AS captured "
            "FROM events e ORDER BY COALESCE(event_date, '') DESC, created_at DESC").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def event_get(eid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def event_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO events (id, name, event_date, kind, location, cta, prep, "
            "notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row["name"], row.get("event_date"), row.get("kind"),
             row.get("location"), row.get("cta"), row.get("prep"),
             row.get("notes"), row["created_at"]))
        conn.commit()
    finally:
        conn.close()


def event_update(eid: str, fields: dict) -> bool:
    if not fields:
        return event_get(eid) is not None
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [eid]
    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE events SET {cols} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def event_delete(eid: str) -> bool:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM event_attendance WHERE event_id = ?", (eid,))
        cur = conn.execute("DELETE FROM events WHERE id = ?", (eid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def attendance_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO event_attendance (id, event_id, contact_id, "
            "deal_id, captured_at) VALUES (?,?,?,?,?)",
            (row["id"], row["event_id"], row["contact_id"],
             row.get("deal_id"), row["captured_at"]))
        conn.commit()
    finally:
        conn.close()


def attendance_for_event(event_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ea.*, c.name AS contact_name, c.email AS contact_email, "
            "a.name AS account_name, d.stage AS deal_stage "
            "FROM event_attendance ea "
            "JOIN contacts c ON c.id = ea.contact_id "
            "JOIN accounts a ON a.id = c.account_id "
            "LEFT JOIN deals d ON d.id = ea.deal_id "
            "WHERE ea.event_id = ? ORDER BY ea.captured_at DESC", (event_id,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def attendance_for_contact(contact_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ea.*, e.name AS event_name, e.event_date "
            "FROM event_attendance ea "
            "JOIN events e ON e.id = ea.event_id "
            "WHERE ea.contact_id = ? ORDER BY e.event_date DESC", (contact_id,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# --- Nurture sequences (per-deal Hook cadence) -----------------------------
# A 5-step Hook-model touch cadence per deal. Schema here; the generator +
# status vocabulary live in growth.py (nurture_* wrappers).

def ensure_nurture_schema() -> None:
    """Create the nurture_sequences table if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS nurture_sequences ("
            " id TEXT PRIMARY KEY,"
            " deal_id TEXT NOT NULL,"
            " step_number INTEGER NOT NULL,"
            " touch_type TEXT,"              # Hook stage: trigger/action/...
            " template_text TEXT,"
            " scheduled_date TEXT,"          # ISO YYYY-MM-DD
            " status TEXT NOT NULL DEFAULT 'pending',"  # pending/sent/skipped
            " created_at INTEGER)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nurture_deal "
            "ON nurture_sequences(deal_id, step_number)")
        conn.commit()
    finally:
        conn.close()


def nurture_for_deal(deal_id: str) -> list[dict]:
    """A deal's sequence, in step order ([] if none / no table)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM nurture_sequences WHERE deal_id = ? "
            "ORDER BY step_number ASC", (deal_id,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def nurture_get(nid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM nurture_sequences WHERE id = ?", (nid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def nurture_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO nurture_sequences (id, deal_id, step_number, touch_type, "
            "template_text, scheduled_date, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["deal_id"], row["step_number"], row.get("touch_type"),
             row.get("template_text"), row.get("scheduled_date"),
             row.get("status", "pending"), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()


def _nurture_set_status(conn, nid: str, status: str,
                        sent_at: Optional[str] = None) -> bool:
    """The SQL half of `nurture_set_status`, on the CALLER's connection.

    Ruling 8's shape (a writer that receives the transaction rather than opening
    one), applied to the cadence ledger: `crm.update_deal`'s closed-deal hygiene
    and the step-5 loop closure both need to move a step inside a transaction
    that is already open, and a second connection would commit the step change
    independently of the deal change it belongs to.

    `sent_at` (m07) is stamped only on →`sent`, and only when the column exists —
    it is what `crm.get_cadence_status`'s compliance arithmetic reads, and it was
    the missing storage that made compliance permanently 0. Moving a step OUT of
    `sent` clears it: a step that is pending again was not sent, and a stale
    stamp would count as compliant forever.
    """
    if status == "sent":
        stamp = sent_at or _dt.date.today().isoformat()
        try:
            cur = conn.execute(
                "UPDATE nurture_sequences SET status = ?, sent_at = ? WHERE id = ?",
                (status, stamp, nid))
            return cur.rowcount > 0
        except sqlite3.OperationalError:
            pass                      # pre-m07 schema: fall through to the
                                      # status-only write below.
    else:
        try:
            cur = conn.execute(
                "UPDATE nurture_sequences SET status = ?, sent_at = NULL WHERE id = ?",
                (status, nid))
            return cur.rowcount > 0
        except sqlite3.OperationalError:
            pass
    cur = conn.execute(
        "UPDATE nurture_sequences SET status = ? WHERE id = ?", (status, nid))
    return cur.rowcount > 0


def nurture_set_status(nid: str, status: str, *, conn=None,
                       sent_at: Optional[str] = None) -> bool:
    """Set one step's status. Transaction shell over `_nurture_set_status`.

    Pass `conn` to write inside an already-open transaction (the caller commits);
    omit it and this opens, writes and commits its own, exactly as before — every
    existing call site is unchanged.
    """
    if conn is not None:
        return _nurture_set_status(conn, nid, status, sent_at)
    own = get_conn()
    try:
        ok = _nurture_set_status(own, nid, status, sent_at)
        own.commit()
        return ok
    finally:
        own.close()


def nurture_delete_for_deal(deal_id: str) -> int:
    """Wipe a deal's sequence (used before regenerating). Returns rows removed."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM nurture_sequences WHERE deal_id = ?", (deal_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# --- Content pipeline (Growth → content calendar) --------------------------
# Content pieces linked to growth loops. Supersedes content_log as the source
# of truth for the publishing cadence + calendar. Schema here; validation + the
# cadence read model live in growth.py (content_piece_* wrappers).

def ensure_content_pieces_schema() -> None:
    """Create the content_pieces table if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS content_pieces ("
            " id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " topic TEXT,"
            " channel TEXT,"                 # blog/linkedin/twitter/youtube/newsletter/other
            " growth_loop TEXT,"             # autoridad/referido/producto
            " hook TEXT,"
            " publish_date TEXT,"            # ISO YYYY-MM-DD
            " status TEXT NOT NULL DEFAULT 'idea',"  # idea/draft/scheduled/published
            " created_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def content_pieces_all() -> list[dict]:
    """Every content piece, newest publish date first ([] if no table yet)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM content_pieces "
            "ORDER BY COALESCE(publish_date, '') DESC, created_at DESC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def content_piece_get(cid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM content_pieces WHERE id = ?", (cid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def content_piece_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO content_pieces (id, title, topic, channel, growth_loop, "
            "hook, publish_date, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row["title"], row.get("topic"), row.get("channel"),
             row.get("growth_loop"), row.get("hook"), row.get("publish_date"),
             row.get("status", "idea"), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()


def content_piece_update(cid: str, fields: dict) -> bool:
    """Update whitelisted columns (caller supplies validated names only)."""
    if not fields:
        return content_piece_get(cid) is not None
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [cid]
    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE content_pieces SET {cols} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def content_piece_delete(cid: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM content_pieces WHERE id = ?", (cid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def content_pieces_created_between(start: int, end: int) -> int:
    """Count of pieces created in [start, end) — feeds the weekly scorecard."""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM content_pieces WHERE created_at >= ? AND created_at < ?",
            (start, end)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


# --- Speaking pipeline (Growth → talks as pipeline generators) --------------
# Talks tracked as attraction-loop pipeline generators, optionally linked to a
# converted deal. Schema here; validation in growth.py (speaking_* wrappers).

def ensure_speaking_schema() -> None:
    """Create the speaking_events table if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS speaking_events ("
            " id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " event_name TEXT,"
            " event_date TEXT,"              # ISO YYYY-MM-DD
            " status TEXT NOT NULL DEFAULT 'proposed',"           # proposed/accepted/scheduled/delivered
            " attraction_loop_status TEXT NOT NULL DEFAULT 'none',"  # none/pre/during/post
            " deal_id TEXT,"                 # nullable → converted deal
            " created_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def speaking_events_all() -> list[dict]:
    """Every talk, soonest/newest event date first ([] if no table yet)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM speaking_events "
            "ORDER BY COALESCE(event_date, '') DESC, created_at DESC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def speaking_event_get(sid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM speaking_events WHERE id = ?", (sid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def speaking_event_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO speaking_events (id, title, event_name, event_date, status, "
            "attraction_loop_status, deal_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["title"], row.get("event_name"), row.get("event_date"),
             row.get("status", "proposed"),
             row.get("attraction_loop_status", "none"),
             row.get("deal_id"), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()


def speaking_event_update(sid: str, fields: dict) -> bool:
    """Update whitelisted columns (caller supplies validated names only)."""
    if not fields:
        return speaking_event_get(sid) is not None
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [sid]
    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE speaking_events SET {cols} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def speaking_event_delete(sid: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM speaking_events WHERE id = ?", (sid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Conversion snapshots (Today → funnel-over-time) -----------------------
# Weekly snapshots of the lead→discovery→proposal→won funnel. Written by the
# Monday-9am timer (bin/funnel-snapshot.sh). Schema here; compute + read models
# in growth.py (conversion_snapshot_* wrappers).

def ensure_conversion_schema() -> None:
    """Create the conversion_snapshots table if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversion_snapshots ("
            " id TEXT PRIMARY KEY,"
            " week_start TEXT NOT NULL UNIQUE,"   # ISO Monday YYYY-MM-DD
            " lead_count INTEGER NOT NULL DEFAULT 0,"
            " discovery_count INTEGER NOT NULL DEFAULT 0,"
            " proposal_count INTEGER NOT NULL DEFAULT 0,"
            " won_count INTEGER NOT NULL DEFAULT 0,"
            " lead_to_discovery_rate REAL,"
            " discovery_to_proposal_rate REAL,"
            " proposal_to_won_rate REAL,"
            " overall_rate REAL,"
            " created_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def conversion_snapshots_all() -> list[dict]:
    """All snapshots, oldest week first ([] if no table yet)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM conversion_snapshots ORDER BY week_start ASC").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def conversion_snapshot_get_week(week_start: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM conversion_snapshots WHERE week_start = ?",
            (week_start,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def conversion_snapshot_upsert(row: dict) -> None:
    """Insert or update the snapshot for its week_start (one per week)."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO conversion_snapshots (id, week_start, lead_count, "
            "discovery_count, proposal_count, won_count, lead_to_discovery_rate, "
            "discovery_to_proposal_rate, proposal_to_won_rate, overall_rate, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(week_start) DO UPDATE SET "
            "lead_count=excluded.lead_count, discovery_count=excluded.discovery_count, "
            "proposal_count=excluded.proposal_count, won_count=excluded.won_count, "
            "lead_to_discovery_rate=excluded.lead_to_discovery_rate, "
            "discovery_to_proposal_rate=excluded.discovery_to_proposal_rate, "
            "proposal_to_won_rate=excluded.proposal_to_won_rate, "
            "overall_rate=excluded.overall_rate, created_at=excluded.created_at",
            (row["id"], row["week_start"], row.get("lead_count", 0),
             row.get("discovery_count", 0), row.get("proposal_count", 0),
             row.get("won_count", 0), row.get("lead_to_discovery_rate"),
             row.get("discovery_to_proposal_rate"), row.get("proposal_to_won_rate"),
             row.get("overall_rate"), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()


# --- Time blocks (Today → weekly role-block calendar) ----------------------
# Ricardo's week is a set of role-specialized blocks (lead-gen playbook): SDR
# prospecting, AE discovery, Marketer content, Consultant delivery, Analyst
# pipeline math. Schema here; validation + seed live in growth.py.

def ensure_time_blocks_schema() -> None:
    """Create the time_blocks table if missing. Idempotent."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS time_blocks ("
            " id TEXT PRIMARY KEY,"
            " day_of_week INTEGER NOT NULL,"        # 0=Mon … 6=Sun (Python weekday)
            " start_time TEXT NOT NULL,"            # HH:MM
            " end_time TEXT NOT NULL,"              # HH:MM
            " role TEXT NOT NULL,"                  # sdr/ae/marketer/consultant/analyst
            " label TEXT NOT NULL,"
            " active INTEGER NOT NULL DEFAULT 1,"   # 1=in schedule, 0=paused
            " done_week TEXT,"                      # ISO week marked done (auto-resets weekly)
            " created_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()


def time_blocks_all() -> list[dict]:
    """Every block, ordered by weekday then start time ([] if no table yet)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM time_blocks "
            "ORDER BY day_of_week ASC, start_time ASC, created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def time_blocks_count() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM time_blocks").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def time_block_get(bid: str) -> Optional[dict]:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM time_blocks WHERE id = ?", (bid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def time_block_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO time_blocks (id, day_of_week, start_time, end_time, role, "
            "label, active, done_week, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row["day_of_week"], row["start_time"], row["end_time"],
             row["role"], row["label"], row.get("active", 1),
             row.get("done_week"), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()


def time_block_update(bid: str, fields: dict) -> bool:
    """Update whitelisted columns (caller supplies validated names only)."""
    if not fields:
        return time_block_get(bid) is not None
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [bid]
    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE time_blocks SET {cols} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def time_block_delete(bid: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM time_blocks WHERE id = ?", (bid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Fireflies meeting cache (CRM Lead Scoring + Fireflies Integration) ----
# Stores fetched Fireflies transcripts per deal so the scoring engine can read
# signals without re-querying the GraphQL API on every score recomputation.

def ensure_fireflies_schema() -> None:
    """Create the fireflies_meetings table + additive deal columns. Idempotent."""
    conn = get_conn()
    try:
        dcols = [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        for col in ("client_profile", "lead_score_details"):
            if col not in dcols:
                conn.execute(f"ALTER TABLE deals ADD COLUMN {col} TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fireflies_meetings ("
            " id TEXT PRIMARY KEY,"
            " deal_id TEXT NOT NULL,"
            " transcript_id TEXT NOT NULL,"
            " title TEXT,"
            " meeting_date TEXT,"
            " duration_seconds INTEGER,"
            " signals TEXT,"              # JSON: talk_ratio, questions, filler_density, ...
            " raw_summary TEXT,"
            " fetched_at INTEGER,"
            " created_at INTEGER)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fireflies_deal "
            "ON fireflies_meetings(deal_id, meeting_date DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fireflies_transcript "
            "ON fireflies_meetings(transcript_id)")
        conn.commit()
    finally:
        conn.close()


def fireflies_meeting_insert(row: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO fireflies_meetings "
            "(id, deal_id, transcript_id, title, meeting_date, duration_seconds, "
            "signals, raw_summary, fetched_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["deal_id"], row["transcript_id"], row.get("title"),
             row.get("meeting_date"), row.get("duration_seconds"),
             row.get("signals") if isinstance(row.get("signals"), str) else json.dumps(row.get("signals") or {}),
             row.get("raw_summary"), row.get("fetched_at"), row.get("created_at")),
        )
        conn.commit()
    finally:
        conn.close()


def fireflies_meetings_for_deal(deal_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM fireflies_meetings WHERE deal_id = ? "
            "ORDER BY meeting_date DESC, fetched_at DESC", (deal_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["signals"] = json.loads(d["signals"] or "{}")
            except Exception:
                d["signals"] = {}
            out.append(d)
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def fireflies_latest_for_deal(deal_id: str) -> Optional[dict]:
    rows = fireflies_meetings_for_deal(deal_id)
    return rows[0] if rows else None


def get_recent_activity(limit: int = 30) -> list[dict]:
    """Recent task events for activity feed."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT te.id, te.task_id, te.kind, te.payload, te.created_at,
                   t.title, t.assignee
            FROM task_events te
            LEFT JOIN tasks t ON te.task_id = t.id
            ORDER BY te.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()