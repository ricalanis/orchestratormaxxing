"""
Object graph layer (PRD Phase 2).

Makes the plan one coherent, honest graph:
- **Epics** — a groupable chunk of work between Project and Task (sidecar table
  + `tasks.epic_id`). Epics are the batch-dispatch unit.
- **Agent registry + trust_grade** — agents (human + AI) as first-class,
  discovered from task assignees and graded by their *outcome history*
  (earned, not declared). The dial that lets one operator scale to many agents.
- **Derived progress** — epic / sprint / project / initiative progress computed
  from tasks (done/total), never hand-typed.
- **Task ↔ Session hard link** — `tasks.session_id` is authoritative; a task
  knows its run and a session knows its tasks.

All sidecar tables live on the Hermes kanban DB, like projects/sprints.
"""
import re
import time
import uuid
from . import db


def _gen(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def ensure_schema() -> None:
    """Idempotently create the Phase-2 tables + columns. Safe to call at startup."""
    conn = db.get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS epics (
                id          TEXT PRIMARY KEY,
                project_id  TEXT,
                title       TEXT NOT NULL,
                description TEXT,
                status      TEXT DEFAULT 'open',
                created_at  INTEGER,
                archived_at INTEGER
            )""")
        # Phase 2: an epic belongs to an initiative (roadmap.json id) — the
        # middle arrow of the Initiative→Epic→Task roll-up. Nullable: epics can
        # exist before their strategic parent is declared.
        epic_cols = [r[1] for r in conn.execute("PRAGMA table_info(epics)").fetchall()]
        if "initiative_id" not in epic_cols:
            conn.execute("ALTER TABLE epics ADD COLUMN initiative_id TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id             TEXT PRIMARY KEY,
                name           TEXT UNIQUE,
                kind           TEXT,
                host           TEXT,
                skills         TEXT,
                trust_override TEXT,
                notes          TEXT,
                created_at     INTEGER,
                last_seen      INTEGER
            )""")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "epic_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN epic_id TEXT")
        # P0-1: direct task→initiative attribution (nullable FK). The epic hop
        # (epic.initiative_id) stays the *preferred* attribution — this column
        # lets a task be attributed to an initiative WITHOUT an epic, which is
        # what breaks the non-injective "94/94/94" roll-up (three initiatives
        # sharing a project all inheriting the same project %). Forward-only:
        # the column ships empty; the classification backfill is a separate
        # delegated task. SQLite only allows ADD COLUMN ... REFERENCES when the
        # default is NULL (it is) — the FK is then enforced per-connection via
        # the PRAGMA foreign_keys=ON in db.get_conn().
        if "initiative_id" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN initiative_id TEXT "
                "REFERENCES initiatives(id)")
        # `reviewed_at` = when the operator accepted an agent's completion (the
        # human gate, PRD §1/§7). NULL on a done agent-task means "awaiting your
        # review" → it surfaces in the operator's Inbox. On first creation we
        # backfill every EXISTING done task as already-reviewed (they're shipped
        # history — deriving them as unreviewed would wrongly flood the Inbox).
        if "reviewed_at" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN reviewed_at INTEGER")
            conn.execute(
                "UPDATE tasks SET reviewed_at = COALESCE(completed_at, created_at) "
                "WHERE status = 'done'"
            )
        # ---- PRD Phase 3: the push/pull loop columns ----
        # `pool`     — 1 = the task sits in the OPEN pool, claimable by any trusted
        #              agent (vs 0 = assigned-queue only / operator-held). `list_pool`
        #              returns open-pool tasks + the caller's own assigned queue.
        # `autonomy` — 'auto' (may auto-accept when a high-trust agent passes it) |
        #              'dispatch' (always escalates to the operator's Inbox on result,
        #              the conservative default — enforces the human gate, §7).
        # `progress_note`/`progress_pct` — the live "what I'm doing" an agent pushes
        #              via report_progress; shown on the Fleet board's Working cards.
        if "pool" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN pool INTEGER DEFAULT 0")
        if "autonomy" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN autonomy TEXT")
            # Conservative backfill: everything is 'dispatch' (nothing auto-accepts)
            # until the operator or a derived rule promotes a task to 'auto'.
            conn.execute("UPDATE tasks SET autonomy = 'dispatch' WHERE autonomy IS NULL")
        if "progress_note" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN progress_note TEXT")
        if "progress_pct" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN progress_pct INTEGER")
        # `rejection_reason` — the operator's optional note when they REJECT a
        # task (status='rejected'). Sidecar column, like the others above: the
        # human gate can push a completion back *down* with a reason, mirroring
        # `accept`. NULL = no reason given (or never rejected).
        if "rejection_reason" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN rejection_reason TEXT")
        conn.commit()
    finally:
        conn.close()


# ---- Derived progress (done/total over a task foreign key) ----

# `col` is always one of these fixed internal names, never user input.
_PROGRESS_COLS = {"epic_id", "project_id", "sprint_id"}


def _progress(conn, col: str, val: str) -> dict:
    if col not in _PROGRESS_COLS:
        raise ValueError(col)
    total = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {col} = ?", (val,)).fetchone()[0]
    done = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {col} = ? AND status = 'done'", (val,)).fetchone()[0]
    return {"task_total": total, "task_done": done, "progress": round(done / total * 100) if total else 0}


# ---- Initiative field vocabulary (Phase 2 item 4 — one validated schema) ----
# quarter replaces the old trimester+month pair: ONE calendar quarter,
# `YYYY-Q[1-4]` (sortable as a plain string). tier = the quarterly-bet type
# (§6.3: cap ~2 commit + 2 bet + 1 explore); health is human-set and SEPARATE
# from derived % (D3 — 80% done can still be off-track).
QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")
INITIATIVE_TIERS = ("commit", "bet", "explore")
INITIATIVE_HEALTH = ("on-track", "at-risk", "off-track")


def validate_initiative_fields(fields: dict) -> str:
    """Return an error message for invalid initiative fields, or '' if clean.
    Only judges keys that are present — callers pass partial updates."""
    if "trimester" in fields or "month" in fields:
        return "trimester/month were replaced by 'quarter' (YYYY-Q[1-4])"
    q = fields.get("quarter")
    if q is not None and not QUARTER_RE.match(str(q)):
        return f"quarter '{q}' must match YYYY-Q[1-4] (e.g. 2026-Q3)"
    t = fields.get("tier")
    if t is not None and t not in INITIATIVE_TIERS:
        return f"tier '{t}' must be one of {INITIATIVE_TIERS}"
    h = fields.get("health")
    if h is not None and h not in INITIATIVE_HEALTH:
        return f"health '{h}' must be one of {INITIATIVE_HEALTH}"
    return ""


def initiative_epic_count(initiative_id: str) -> int:
    """Live (non-archived) epics linked to a roadmap initiative."""
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM epics WHERE initiative_id = ? AND archived_at IS NULL",
            (initiative_id,),
        ).fetchone()[0]
    finally:
        conn.close()


# A task's initiative is COALESCE(epic.initiative_id, task.initiative_id): the
# epic hop wins when set (the more specific batch-dispatch grouping), else the
# task's own direct attribution (P0-1). One expression, reused by the roll-up
# and the unattributed count so they can never diverge.
_TASK_INITIATIVE = (
    "COALESCE((SELECT e.initiative_id FROM epics e "
    "WHERE e.id = tasks.epic_id AND e.archived_at IS NULL), "
    "tasks.initiative_id)"
)


def initiative_progress(initiative: dict) -> dict:
    """Derived progress for a roadmap initiative — the honest roll-up (D2, P0-1).

    Attribution (COALESCE(epic.initiative_id, task.initiative_id)):
      1. a task's epic points at this initiative, OR
      2. the task is directly attributed to it (tasks.initiative_id), OR
      3. its project has EXACTLY ONE initiative — then the whole project rolls
         up unambiguously (the safe coarse fallback).

    Shared-project suppression (the fix): when a project has >1 initiative, an
    unattributed task can't be non-injectively inherited by all of them (the old
    "94/94/94" bug). Such tasks count toward NO initiative's % and surface as
    `unattributed` instead — honest zero beats a misleading aggregate.

    'Done' counts only ACCEPTED work — status='done' AND reviewed_at set (the
    human gate): an agent calling its work done doesn't move strategy until the
    operator accepts it. task_in_flight feeds the stalled-bet flag (an 'active'
    initiative with 0 in-flight tasks is a claim, not work)."""
    conn = db.get_conn()
    try:
        iid = initiative.get("id")
        epic_count = conn.execute(
            "SELECT COUNT(*) FROM epics WHERE initiative_id = ? AND archived_at IS NULL",
            (iid,),
        ).fetchone()[0]
        pid = db.resolve_project(initiative.get("project_id"), conn)
        siblings = conn.execute(
            "SELECT COUNT(*) FROM initiatives WHERE project_id = ?", (pid,)
        ).fetchone()[0] if pid else 0
        shared = siblings > 1

        attributed = f"{_TASK_INITIATIVE} = ?"
        if pid and not shared:
            # Sole initiative in its project → project-wide fallback is
            # unambiguous (every project task belongs to it), plus any explicit
            # attribution that reaches beyond the project.
            where, params, scope = f"(project_id = ? OR {attributed})", (pid, iid), "project"
        else:
            # No project, or a SHARED project: only explicitly-attributed tasks
            # count. Unattributed project tasks become `unattributed`, never an
            # inherited percentage.
            where, params = attributed, (iid,)
            scope = "epics" if epic_count else "attributed"

        total, done, in_flight = conn.execute(
            f"""SELECT COUNT(*),
                       COALESCE(SUM(status = 'done' AND reviewed_at IS NOT NULL), 0),
                       COALESCE(SUM(status = 'in_progress'), 0)
                FROM tasks WHERE {where}""",
            params,
        ).fetchone()

        # In a shared project, how many of its tasks belong to no initiative at
        # all — the honestly-unattributed pool the UI shows instead of a fake %.
        unattributed = 0
        if shared:
            unattributed = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE project_id = ? "
                f"AND {_TASK_INITIATIVE} IS NULL", (pid,),
            ).fetchone()[0]

        return {"task_total": total, "task_done": done, "task_in_flight": in_flight,
                "progress": round(done / total * 100) if total else 0,
                "scope": scope, "epic_count": epic_count,
                "shared_project": shared, "unattributed": unattributed}
    finally:
        conn.close()


def _quarter_window(quarter):
    """(start_ts, end_ts) for a 'YYYY-Q[1-4]' quarter, or (None, None)."""
    import datetime as _d
    if not quarter or not QUARTER_RE.match(str(quarter)):
        return None, None
    year, q = int(str(quarter)[:4]), int(str(quarter)[-1])
    m0 = (q - 1) * 3 + 1
    start = _d.date(year, m0, 1)
    end = (_d.date(year, 12, 31) if m0 + 2 == 12
           else _d.date(year, m0 + 3, 1) - _d.timedelta(days=1))
    return int(time.mktime(start.timetuple())), int(time.mktime(end.timetuple()))


def initiative_burndown(initiative: dict) -> list:
    """P2-3: the initiative analogue of the cycle burndown — remaining
    accepted-done tasks vs the linear ideal, one point per day across the
    initiative's QUARTER window (falling back to the task-creation span when it
    has no quarter). Uses the SAME attributed scope as initiative_progress (so
    `committed` matches its task_total), and reuses sprints._burndown (this
    literally 'extends the cycle burndown'). [] when there's too little to chart
    or the quarter hasn't started."""
    conn = db.get_conn()
    try:
        iid = initiative.get("id")
        pid = db.resolve_project(initiative.get("project_id"), conn)
        siblings = conn.execute(
            "SELECT COUNT(*) FROM initiatives WHERE project_id = ?", (pid,)
        ).fetchone()[0] if pid else 0
        attributed = f"{_TASK_INITIATIVE} = ?"
        if pid and siblings <= 1:
            where, params = f"(project_id = ? OR {attributed})", (pid, iid)
        else:
            where, params = attributed, (iid,)
        rows = conn.execute(
            f"SELECT created_at, completed_at, status, reviewed_at FROM tasks WHERE {where}",
            params).fetchall()
    finally:
        conn.close()
    committed = len(rows)
    if committed < 2:
        return []
    done_ts = [r["completed_at"] for r in rows
               if r["status"] == "done" and r["reviewed_at"] and r["completed_at"]]
    start, end = _quarter_window(initiative.get("quarter"))
    if not start:                                   # no quarter → the task span
        cts = [r["created_at"] for r in rows if r["created_at"]]
        start = min(cts) if cts else int(time.time())
        end = int(time.time())
    from . import sprints
    return sprints._burndown(start, end, committed, done_ts)


def initiative_drilldown(initiative: dict) -> dict:
    """The Initiative→Project→Cycle→Task tree for one roadmap initiative.
    Task universe = the initiative's epics' tasks when it has ≥1 epic, else the
    whole project's. Cycles are the project's sprints; tasks outside any sprint
    land in 'unscheduled'. Done tasks distinguish accepted (reviewed_at set)
    from awaiting review — same gate the roll-up counts."""
    conn = db.get_conn()
    try:
        iid = initiative.get("id")
        pid = db.resolve_project(initiative.get("project_id"), conn)
        proj = None
        if pid:
            row = conn.execute(
                "SELECT id, slug, name, icon FROM projects WHERE id = ?", (pid,)
            ).fetchone()
            proj = dict(row) if row else None
        epics = [dict(r) for r in conn.execute(
            "SELECT id, title, status, description, initiative_id, project_id FROM epics "
            "WHERE initiative_id = ? AND archived_at IS NULL ORDER BY created_at",
            (iid,))]
        fields = ("id, title, status, assignee, sprint_id, epic_id, "
                  "(status = 'done' AND reviewed_at IS NOT NULL) AS accepted")
        if epics:
            rows = conn.execute(
                f"SELECT {fields} FROM tasks WHERE epic_id IN "
                "(SELECT id FROM epics WHERE initiative_id = ? AND archived_at IS NULL) "
                "ORDER BY created_at DESC", (iid,)).fetchall()
            scope = "epics"
        elif pid:
            rows = conn.execute(
                f"SELECT {fields} FROM tasks WHERE project_id = ? "
                "ORDER BY created_at DESC", (pid,)).fetchall()
            scope = "project"
        else:
            rows, scope = [], "none"
        tasks = [dict(r) for r in rows]
        for e in epics:
            e["tasks"] = [t for t in tasks if t["epic_id"] == e["id"]]
        cycles = []
        if pid:
            cycles = [dict(r) for r in conn.execute(
                "SELECT id, name, status FROM sprints WHERE project_id = ? "
                "ORDER BY created_at DESC", (pid,))]
        for c in cycles:
            c["tasks"] = [t for t in tasks if t["sprint_id"] == c["id"]]
        scheduled = {t["id"] for c in cycles for t in c["tasks"]}
        return {
            "initiative": {"id": iid, "title": initiative.get("title")},
            "project": proj,
            "scope": scope,
            "epics": epics,
            "cycles": cycles,
            "unscheduled": [t for t in tasks if t["id"] not in scheduled],
            "task_total": len(tasks),
        }
    finally:
        conn.close()


def project_progress(project_id: str) -> dict:
    conn = db.get_conn()
    try:
        return _progress(conn, "project_id", project_id)
    finally:
        conn.close()


# ---- Epics ----

def list_epics(project_id: str = None) -> list:
    conn = db.get_conn()
    try:
        if project_id:
            rows = conn.execute("SELECT * FROM epics WHERE archived_at IS NULL AND project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM epics WHERE archived_at IS NULL ORDER BY created_at DESC").fetchall()
        epics = []
        for r in rows:
            e = dict(r)
            e.update(_progress(conn, "epic_id", e["id"]))
            epics.append(e)
        return epics
    finally:
        conn.close()


def create_epic(project_id: str, title: str, description: str = "",
                initiative_id: str = None) -> dict:
    conn = db.get_conn()
    try:
        pid = db.resolve_project(project_id, conn)
        if not pid:
            return {"error": f"project '{project_id}' resolves to no project"}
        eid = _gen("epic")
        conn.execute(
            "INSERT INTO epics (id, project_id, initiative_id, title, description, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, pid, initiative_id, title, description, "open", int(time.time())),
        )
        conn.commit()
        return {"id": eid, "project_id": pid, "initiative_id": initiative_id, "status": "created"}
    finally:
        conn.close()


def update_epic(epic_id: str, title: str = None, description: str = None,
                status: str = None) -> dict:
    """Verb-audit gap: epics were create-only — no way to close one. status is
    a closed set (open|closed); closed epics still count in roll-ups (closed ≠
    archived)."""
    if status is not None and status not in ("open", "closed"):
        return {"error": "status must be 'open' or 'closed'"}
    sets, params = [], []
    for col, val in (("title", title), ("description", description), ("status", status)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return {"error": "nothing to update (title/description/status)"}
    conn = db.get_conn()
    try:
        cur = conn.execute(f"UPDATE epics SET {', '.join(sets)} WHERE id = ?",
                           (*params, epic_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"error": f"epic '{epic_id}' not found"}
        row = conn.execute("SELECT * FROM epics WHERE id = ?", (epic_id,)).fetchone()
        return {"status": "updated", "epic": dict(row)}
    finally:
        conn.close()


def register_agent(name: str, kind: str = None, host: str = None,
                   skills: str = None, notes: str = None) -> dict:
    """Verb-audit gap: explicit fleet onboarding (idempotent on name). Trust
    stays EARNED — registration never grants a grade."""
    if not (name or "").strip():
        return {"error": "name required"}
    conn = db.get_conn()
    try:
        existing = conn.execute("SELECT id FROM agents WHERE name = ?", (name.strip(),)).fetchone()
        if existing:
            sets, params = [], []
            for col, val in (("kind", kind), ("host", host), ("skills", skills), ("notes", notes)):
                if val is not None:
                    sets.append(f"{col} = ?")
                    params.append(val)
            if sets:
                conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE name = ?",
                             (*params, name.strip()))
                conn.commit()
            return {"status": "exists", "agent_id": existing["id"], "name": name.strip()}
        aid = _gen("agent")
        conn.execute(
            "INSERT INTO agents (id, name, kind, host, skills, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (aid, name.strip(), kind or _agent_kind(name), host, skills, notes, int(time.time())))
        conn.commit()
        return {"status": "registered", "agent_id": aid, "name": name.strip()}
    finally:
        conn.close()


def assign_task_epic(task_id: str, epic_id) -> dict:
    """Set (or clear, with epic_id=None) a task's epic. Both ends validated."""
    conn = db.get_conn()
    try:
        if epic_id is not None:
            row = conn.execute(
                "SELECT id FROM epics WHERE id = ? AND archived_at IS NULL", (epic_id,)
            ).fetchone()
            if not row:
                return {"error": f"epic '{epic_id}' not found"}
        cur = conn.execute("UPDATE tasks SET epic_id = ? WHERE id = ?", (epic_id, task_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"error": f"task '{task_id}' not found"}
        return {"task_id": task_id, "epic_id": epic_id}
    finally:
        conn.close()


def set_task_initiative(task_id: str, initiative_id) -> dict:
    """Set (or clear, with initiative_id=None) a task's DIRECT initiative
    attribution (P0-1 tasks.initiative_id). The write path for the P1-3
    attribution pass and the §5.3 'Assign → Initiative' affordance; both ends
    validated so a bad id can't break the roll-up (the FK would reject it
    anyway, but a clean error is friendlier)."""
    conn = db.get_conn()
    try:
        if initiative_id is not None:
            row = conn.execute(
                "SELECT id FROM initiatives WHERE id = ?", (initiative_id,)).fetchone()
            if not row:
                return {"error": f"initiative '{initiative_id}' not found"}
        cur = conn.execute("UPDATE tasks SET initiative_id = ? WHERE id = ?",
                           (initiative_id, task_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"error": f"task '{task_id}' not found"}
        return {"task_id": task_id, "initiative_id": initiative_id}
    finally:
        conn.close()


# ---- Agent registry + trust ----

def _agent_kind(name: str) -> str:
    n = (name or "").lower()
    if n in ("ricardo", "user"):
        return "human"
    if "claude" in n:
        return "claude-code"
    if n in ("hermes", "default"):
        return "hermes"
    if any(k in n for k in ("kimi", "glm", "qwen", "deepseek", "minimax", "gemini", "opencode", "ollama", "coder")):
        return "ollama-worker"
    return "other"


def _trust(done: int, failed: int) -> tuple:
    """Earned trust from outcome history. Conservative by construction: an agent
    is 'new' until it has a track record, so nothing auto-accepts early."""
    if done < 3:
        return ("new", 0.0)
    score = done / (done + failed) if (done + failed) else 0.0
    if score >= 0.9 and done >= 5:
        return ("high", score)
    if score >= 0.7:
        return ("medium", score)
    return ("low", score)


_TRUST_RANK = {"new": 0, "low": 1, "medium": 2, "high": 3}


def get_agents(online_names: set = None) -> list:
    """The agent registry: every assignee that has touched a task, graded by
    outcome history, plus any stored metadata/overrides. `online_names` (session
    display names/machines) marks who's live."""
    online_names = online_names or set()
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT assignee AS name, COUNT(*) AS n, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
            "FROM tasks WHERE assignee IS NOT NULL AND assignee != '' GROUP BY assignee"
        ).fetchall()
        agents = {}
        for r in rows:
            name = r["name"]
            done = r["done"] or 0
            failed = conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee = ? AND consecutive_failures > 0", (name,)).fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee = ? AND status IN ('in_progress','claimed','running')", (name,)).fetchone()[0]
            grade, score = _trust(done, failed)
            agents[name] = {
                "name": name, "kind": _agent_kind(name),
                "tasks_total": r["n"], "tasks_done": done, "tasks_failed": failed,
                "active": active, "trust_grade": grade, "trust_score": round(score, 2),
                "trust_overridden": False,
            }
        # Registry metadata + operator trust overrides.
        for reg in conn.execute("SELECT * FROM agents").fetchall():
            a = agents.setdefault(reg["name"], {
                "name": reg["name"], "kind": reg["kind"] or _agent_kind(reg["name"]),
                "tasks_total": 0, "tasks_done": 0, "tasks_failed": 0, "active": 0,
                "trust_grade": "new", "trust_score": 0.0, "trust_overridden": False,
            })
            if reg["kind"]:
                a["kind"] = reg["kind"]
            if reg["skills"]:
                a["skills"] = reg["skills"]
            if reg["trust_override"]:
                a["trust_grade"] = reg["trust_override"]
                a["trust_overridden"] = True
        # Online status from live sessions.
        for a in agents.values():
            a["online"] = a["kind"] != "human" and a["name"] in online_names

        return sorted(agents.values(), key=lambda x: (x["kind"] == "human", -_TRUST_RANK.get(x["trust_grade"], 0), -x["tasks_total"]))
    finally:
        conn.close()


def trust_grade_for(name: str) -> str:
    """The earned trust grade for a single agent (the auto-accept-vs-escalate
    dial, PRD §7). Operator override wins; otherwise derived from outcome
    history exactly like get_agents. Cheap enough to call per report_result."""
    if not name:
        return "new"
    conn = db.get_conn()
    try:
        reg = conn.execute("SELECT trust_override FROM agents WHERE name = ?", (name,)).fetchone()
        if reg and reg["trust_override"]:
            return reg["trust_override"]
        done = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee = ? AND status = 'done'", (name,)
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee = ? AND consecutive_failures > 0", (name,)
        ).fetchone()[0]
        return _trust(done, failed)[0]
    finally:
        conn.close()


def set_agent_trust(name: str, grade) -> dict:
    """Operator-only trust override (an agent can never raise its own)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
        now = int(time.time())
        if row:
            conn.execute("UPDATE agents SET trust_override = ? WHERE name = ?", (grade, name))
        else:
            conn.execute(
                "INSERT INTO agents (id, name, kind, trust_override, created_at) VALUES (?,?,?,?,?)",
                (_gen("agent"), name, _agent_kind(name), grade, now),
            )
        conn.commit()
        return {"name": name, "trust_grade": grade, "overridden": grade is not None}
    finally:
        conn.close()


# ---- Task <-> Session hard link ----

def set_task_session(task_id: str, session_id) -> dict:
    conn = db.get_conn()
    try:
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
        conn.commit()
        return {"task_id": task_id, "session_id": session_id}
    finally:
        conn.close()


def tasks_for_session(session_id: str) -> list:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, status, assignee, project_id FROM tasks WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
