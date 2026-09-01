"""
Phase 1 — identity & project joins ("install the keys").

The canonical taxonomy (the closed project namespace), the agent registry seed,
the ownership/origin closed sets, and the idempotent ensure/seed primitives that
install these keys onto the Hermes kanban DB. Everything here is a sidecar on the
same DB (like projects.py / graph.py) and safe to call at startup.

Design source: knowledge/deep-design-interaction-layers.md §3.3, §3.4, Phase 1.
"""
import time
from . import db

# --- Canonical taxonomy (the closed project slug set) ----------------------
# projects.slug is THE one FK namespace (db.resolve_project). `kind`:
#   product  — real product/lab work; rolls up into quarter math.
#   personal — personal-admin (§3.4); never gets cycles, never rolls up.
#   system   — machinery (the untriaged Inbox); excluded from both.
# Only the *new* projects are listed; the 4 pre-existing ones default to product.
#   slug,       name,                     kind,       icon,  color
TAXONOMY = [
    ("inbox",      "Inbox · untriaged",   "system",   "📥",  "#64748b"),
    ("gpu_ops",    "GPU / Model Ops",     "product",  "🖥️",  "#8b5cf6"),
    ("admin",      "Admin",               "personal", "📄",  "#f59e0b"),
    ("learning",   "Learning",            "personal", "🎓",  "#10b981"),
]

INBOX_SLUG = "inbox"

# --- Ownership & origin model (§3.3) ---------------------------------------
# origin: the closed set that REPLACES the created_by mess. Every task carries
# exactly one (ratcheted by orch-verify).
ORIGINS = {"operator", "hermes", "agent", "decomposed"}

# created_by normalized target set (backfill user→operator). Not ratcheted, but
# kept clean by the identity trigger.
CREATED_BY_ALLOWED = {"operator", "hermes", "auto-decomposer"}  # + "agent:<name>"

# The agent registry seed. kind uses the FINE grades graph._agent_kind emits
# (human | claude-code | hermes | ollama-worker) so the Agents tab keeps its
# split; operator is the one human. trust is EARNED (from outcome history), so we
# seed no trust_override — the registry just makes membership explicit.
#   name,           kind
AGENTS_SEED = [
    ("operator",      "human"),
    ("claude-code",   "claude-code"),
    ("hermes",        "hermes"),
    ("kimi-coder",    "ollama-worker"),
    ("glm-coder",     "ollama-worker"),
    ("ollama-worker", "ollama-worker"),
]

_HUMAN = ("operator", "user")

# The full closed slug set (pre-existing + taxonomy). Classification and the
# ratchet validate labels against this.
BASE_SLUGS = {"orchestrator"}
ALL_SLUGS = BASE_SLUGS | {t[0] for t in TAXONOMY}


def ensure_schema() -> None:
    """Idempotently add the Phase-1 columns. Safe at startup."""
    conn = db.get_conn()
    try:
        pcols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
        if "kind" not in pcols:
            conn.execute("ALTER TABLE projects ADD COLUMN kind TEXT DEFAULT 'product'")
            # Existing projects are all product work.
            conn.execute("UPDATE projects SET kind = 'product' WHERE kind IS NULL")
        # Ownership & origin columns (§3.3). owner = the accountable human (ALWAYS
        # set); delegate = the agent currently acting (NULL when a human owns it,
        # NOT a reassignment of owner); origin = the closed-set author replacing
        # the created_by mess.
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "origin" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN origin TEXT")
        if "owner" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner TEXT")
        if "delegate" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN delegate TEXT")
        # parent_id — the containment self-pointer (§3.1). CONTAINMENT (a task is a
        # subtask OF its decompose root) is distinct from the dependency DAG in
        # task_links. Reserved + populated from 'decomposed' events (item 5); a
        # plain nullable column (not a declared FK) so it never blocks a parent
        # delete. epic_id (from graph.ensure_schema) is likewise reserved.
        if "parent_id" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN parent_id TEXT")
        conn.commit()
    finally:
        conn.close()


def seed_agents() -> None:
    """Seed the agent registry (idempotent, keyed on the UNIQUE name). Gives
    trust_grade_for() a real membership list; trust stays earned, not declared."""
    import uuid
    conn = db.get_conn()
    try:
        now = int(time.time())
        for name, kind in AGENTS_SEED:
            exists = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO agents (id, name, kind, created_at) VALUES (?,?,?,?)",
                    (f"agent_{uuid.uuid4().hex[:8]}", name, kind, now),
                )
        conn.commit()
    finally:
        conn.close()


# The create-time identity assertion (§3.3 "badge rule = create-time assertion").
# One AFTER-INSERT trigger so EVERY writer — the dashboard, the MCP server, AND
# the external hermes CLI — lands a task with a closed-set origin, an accountable
# owner, and (when an agent holds it) a delegate. This is what keeps the origin
# ratchet green even for tasks Hermes creates outside our code. Session-identity
# stamping (item 4) can still OVERRIDE origin='hermes' at create in our paths;
# this is the baseline floor derived from created_by.
_IDENTITY_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_task_identity
AFTER INSERT ON tasks
BEGIN
    UPDATE tasks SET created_by = 'ricardo'
        WHERE id = NEW.id AND created_by = 'user';
    UPDATE tasks SET origin = CASE
            WHEN NEW.created_by IN ('ricardo', 'user') THEN 'ricardo'
            WHEN NEW.created_by = 'auto-decomposer'    THEN 'decomposed'
            WHEN NEW.created_by = 'hermes'             THEN 'hermes'
            ELSE 'agent' END
        WHERE id = NEW.id AND origin IS NULL;
    UPDATE tasks SET owner = 'ricardo'
        WHERE id = NEW.id AND owner IS NULL;
    UPDATE tasks SET delegate = NEW.assignee
        WHERE id = NEW.id AND delegate IS NULL
          AND NEW.assignee IS NOT NULL AND NEW.assignee NOT IN ('ricardo', 'user');
END;
"""


# project_id NOT-NULL-by-default (item 4). A task must always land in SOME
# project; if a writer (the external hermes CLI, a bare create) leaves it NULL,
# floor it to the untriaged Inbox rather than let it become an invisible orphan
# again (the 44-NULL regression). Our own create paths resolve a better project
# (explicit / session) and OVERWRITE this floor; the decomposer-inherit trigger
# (item 5) likewise overwrites Inbox with the parent's project.
_DEFAULT_PROJECT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_task_default_project
AFTER INSERT ON tasks
WHEN NEW.project_id IS NULL
BEGIN
    UPDATE tasks SET project_id = (SELECT id FROM projects WHERE slug = 'inbox')
        WHERE id = NEW.id AND project_id IS NULL;
END;
"""


# Decomposer inherits project onto children (item 5). The auto-decomposer (often
# the external hermes CLI) writes a 'decomposed' event on the ROOT task carrying
# {"child_ids": [...]}. Fire on that event: set each child's parent_id
# (containment) and inherit the root's project_id (overwriting only the NULL/Inbox
# floor, so an explicit child project is respected) and its reserved epic_id.
# child_ids is JSON → json_each (json1, confirmed available). This keeps
# decomposed subtasks out of the Inbox and in their parent's project.
_DECOMPOSE_INHERIT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_decompose_inherit
AFTER INSERT ON task_events
WHEN NEW.kind = 'decomposed'
 AND NEW.payload IS NOT NULL
 AND json_extract(NEW.payload, '$.child_ids') IS NOT NULL
BEGIN
    UPDATE tasks SET parent_id = NEW.task_id
        WHERE id IN (SELECT value FROM json_each(json_extract(NEW.payload, '$.child_ids')))
          AND parent_id IS NULL;
    UPDATE tasks SET project_id = (SELECT project_id FROM tasks WHERE id = NEW.task_id)
        WHERE id IN (SELECT value FROM json_each(json_extract(NEW.payload, '$.child_ids')))
          AND (project_id IS NULL OR project_id = (SELECT id FROM projects WHERE slug = 'inbox'))
          AND (SELECT project_id FROM tasks WHERE id = NEW.task_id) IS NOT NULL;
    UPDATE tasks SET epic_id = (SELECT epic_id FROM tasks WHERE id = NEW.task_id)
        WHERE id IN (SELECT value FROM json_each(json_extract(NEW.payload, '$.child_ids')))
          AND epic_id IS NULL
          AND (SELECT epic_id FROM tasks WHERE id = NEW.task_id) IS NOT NULL;
END;
"""


def install_triggers() -> None:
    """Install the identity + default-project + decompose-inherit triggers (idempotent)."""
    conn = db.get_conn()
    try:
        conn.executescript(_IDENTITY_TRIGGER)
        conn.executescript(_DEFAULT_PROJECT_TRIGGER)
        conn.executescript(_DECOMPOSE_INHERIT_TRIGGER)
        conn.commit()
    finally:
        conn.close()


def backfill_containment() -> dict:
    """One-time (idempotent) backfill of containment + inheritance from existing
    'decomposed' events: set each child's parent_id and inherit the root's
    project_id (only over NULL/Inbox) + epic_id. Re-runnable (guarded)."""
    import json as _json
    conn = db.get_conn()
    try:
        inbox = db.resolve_project(INBOX_SLUG, conn)
        events = conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind = 'decomposed' AND payload IS NOT NULL"
        ).fetchall()
        set_parent = set_project = set_epic = 0
        for ev in events:
            root = ev["task_id"]
            try:
                child_ids = (_json.loads(ev["payload"]) or {}).get("child_ids") or []
            except Exception:
                continue
            root_row = conn.execute("SELECT project_id, epic_id FROM tasks WHERE id = ?", (root,)).fetchone()
            if not root_row:
                continue
            rproj, repic = root_row["project_id"], root_row["epic_id"]
            for cid in child_ids:
                set_parent += conn.execute(
                    "UPDATE tasks SET parent_id = ? WHERE id = ? AND parent_id IS NULL", (root, cid)
                ).rowcount
                if rproj:
                    set_project += conn.execute(
                        "UPDATE tasks SET project_id = ? WHERE id = ? "
                        "AND (project_id IS NULL OR project_id = ?)", (rproj, cid, inbox)
                    ).rowcount
                if repic:
                    set_epic += conn.execute(
                        "UPDATE tasks SET epic_id = ? WHERE id = ? AND epic_id IS NULL", (repic, cid)
                    ).rowcount
        conn.commit()
        return {"parent_set": set_parent, "project_inherited": set_project, "epic_inherited": set_epic}
    finally:
        conn.close()


def inbox_count() -> int:
    """How many tasks sit untriaged in the Inbox. Surfaced so the Inbox can't
    silently grow to 44 orphans again (item 4)."""
    pid = db.resolve_project(INBOX_SLUG)
    if not pid:
        return 0
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status != 'done'", (pid,)
        ).fetchone()[0]
    finally:
        conn.close()


def resolve_create_project(explicit: "str | None", session_key: "str | None") -> str:
    """Resolve the project a new task should land in (item 4), in priority order:
    explicit arg → the creating session's session_meta.project → the Inbox floor.
    Always returns a canonical project id (never None)."""
    pid = db.resolve_project(explicit) if explicit else None
    if pid:
        return pid
    if session_key:
        try:
            from . import orchestration as _orch
            meta = _orch.get_session_meta(session_key)
            if meta and meta.get("project"):
                pid = db.resolve_project(meta["project"])
                if pid:
                    return pid
        except Exception:
            pass
    return inbox_id()


def backfill_identity() -> dict:
    """One-time (idempotent) backfill of the ownership/origin model onto existing
    rows. Guarded so re-runs are no-ops: origin/owner set only where NULL,
    created_by only where 'user'."""
    conn = db.get_conn()
    try:
        cb = conn.execute("UPDATE tasks SET created_by = 'ricardo' WHERE created_by = 'user'").rowcount
        og = conn.execute(
            "UPDATE tasks SET origin = CASE "
            "  WHEN created_by IN ('ricardo','user') THEN 'ricardo' "
            "  WHEN created_by = 'auto-decomposer'   THEN 'decomposed' "
            "  WHEN created_by = 'hermes'            THEN 'hermes' "
            "  ELSE 'agent' END "
            "WHERE origin IS NULL"
        ).rowcount
        ow = conn.execute("UPDATE tasks SET owner = 'ricardo' WHERE owner IS NULL").rowcount
        dl = conn.execute(
            "UPDATE tasks SET delegate = assignee "
            "WHERE delegate IS NULL AND assignee IS NOT NULL AND assignee NOT IN ('ricardo','user')"
        ).rowcount
        conn.commit()
        return {"created_by_normalized": cb, "origin_set": og, "owner_set": ow, "delegate_set": dl}
    finally:
        conn.close()


def ensure_identity() -> None:
    """The full Phase-1 identity install (idempotent): columns, agent seed,
    create-time trigger, and the one-time backfill. Safe at startup."""
    ensure_schema()
    seed_agents()
    install_triggers()
    backfill_identity()
    backfill_containment()


def ensure_taxonomy() -> dict:
    """Create the canonical taxonomy projects if absent (idempotent, keyed on
    slug). Returns the slug→id map for the whole namespace."""
    conn = db.get_conn()
    try:
        now = int(time.time())
        for slug, name, kind, icon, color in TAXONOMY:
            exists = conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone()
            if not exists:
                pid = f"proj_{slug}"
                # Guard against an id collision (slug differs but id taken).
                if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
                    pid = f"proj_{slug}_{now}"
                conn.execute(
                    "INSERT INTO projects (id, slug, name, description, color, icon, kind, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (pid, slug, name, "", color, icon, kind, now),
                )
        conn.commit()
        rows = conn.execute("SELECT slug, id FROM projects").fetchall()
        return {r["slug"]: r["id"] for r in rows}
    finally:
        conn.close()


def inbox_id() -> str:
    """Canonical project id for the untriaged Inbox (ensures it exists)."""
    pid = db.resolve_project(INBOX_SLUG)
    if not pid:
        ensure_taxonomy()
        pid = db.resolve_project(INBOX_SLUG)
    return pid
