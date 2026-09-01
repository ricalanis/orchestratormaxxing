"""
Phase 6 — Strategy in the DB (the roadmap becomes first-class data).

roadmap.json was strategy living in a git-synced code directory: no events, no
FKs the DB could enforce, and every writer hand-rolled its own load/save. This
module moves initiatives into the kanban DB with an `initiative_events` audit
spine mirroring task_events — same doctrine as everything else: one validated
write path, every mutation is an event, derived stays derived.

roadmap.json survives ONE release as a GENERATED EXPORT (written after every
mutation, marked `_generated`) so external readers keep working; then it dies.

Sidecar on the kanban DB, same pattern as sprints/canvas/governance.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from . import db
from . import object_graph

ROADMAP_EXPORT = Path(__file__).parent / "roadmap.json"

# The full mutable field set (validate_initiative_fields governs the enums).
FIELDS = ("title", "project_id", "owner", "status", "tier", "quarter",
          "confidence", "health", "progress", "why", "success_check", "description")
STATUSES = ("planned", "active", "shipped", "dropped")


def _now() -> int:
    return int(time.time())


def _log(conn, initiative_id: str, kind: str, payload: dict) -> None:
    """Append to the initiative_events audit spine (best-effort, mirrors
    task_events: every strategy mutation is a recorded event)."""
    try:
        conn.execute(
            "INSERT INTO initiative_events (initiative_id, kind, payload, created_at) "
            "VALUES (?,?,?,?)",
            (initiative_id, kind, json.dumps(payload), _now()),
        )
    except Exception:
        pass


def ensure_schema() -> None:
    """Idempotent Phase-6 install: the initiatives table + event spine, then a
    ONE-TIME migration of roadmap.json rows (guarded by an orch_meta marker so
    a hand-edited legacy file can never silently re-import). Safe at startup."""
    conn = db.get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS initiatives (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                project_id    TEXT REFERENCES projects(id),
                owner         TEXT,
                status        TEXT DEFAULT 'planned',
                tier          TEXT,
                quarter       TEXT,
                confidence    TEXT,
                health        TEXT,
                progress      INTEGER,       -- ONLY for epic-less initiatives (D2)
                why           TEXT,
                success_check TEXT,
                description   TEXT,
                created_at    INTEGER,
                updated_at    INTEGER
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS initiative_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                initiative_id TEXT NOT NULL,
                kind          TEXT NOT NULL,
                payload       TEXT,
                created_at    INTEGER NOT NULL
            )""")
        conn.execute("CREATE TABLE IF NOT EXISTS orch_meta (key TEXT PRIMARY KEY, value TEXT)")
        # P3 — performance index on initiative_events (audit spine queries).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_initiative_events_init "
            "ON initiative_events(initiative_id, created_at)")
        migrated = conn.execute(
            "SELECT value FROM orch_meta WHERE key = 'strategy_migrated'").fetchone()
        conn.commit()
    finally:
        conn.close()
    if not migrated:
        _migrate_roadmap_json()


def _migrate_roadmap_json() -> dict:
    """One-time import of roadmap.json → the initiatives table. Idempotent on
    ids; stamps orch_meta.strategy_migrated so it never re-runs (after this,
    the FILE is a generated export, not a source)."""
    data = {"initiatives": []}
    if ROADMAP_EXPORT.exists():
        try:
            data = json.loads(ROADMAP_EXPORT.read_text())
        except Exception:
            data = {"initiatives": []}
    now = _now()
    conn = db.get_conn()
    try:
        n = 0
        for init in data.get("initiatives", []):
            if not init.get("id"):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO initiatives (id, title, project_id, owner, status, "
                "tier, quarter, confidence, health, progress, why, success_check, "
                "description, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (init["id"], init.get("title") or init["id"], init.get("project_id"),
                 init.get("owner"), init.get("status") or "planned", init.get("tier"),
                 init.get("quarter"), init.get("confidence"), init.get("health"),
                 init.get("progress"), init.get("why"), init.get("success_check"),
                 init.get("description"), now, now))
            if cur.rowcount:
                _log(conn, init["id"], "initiative_created",
                     {"via": "roadmap-json-migration"})
                n += 1
        conn.execute("INSERT OR REPLACE INTO orch_meta (key, value) VALUES "
                     "('strategy_migrated', ?)", (str(now),))
        conn.commit()
    finally:
        conn.close()
    export_roadmap()
    return {"migrated": n}


# ---------------------------------------------------------------- reads

def list_initiatives() -> list:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM initiatives ORDER BY quarter, tier, created_at").fetchall()]
    finally:
        conn.close()


def get_initiative(initiative_id: str) -> Optional[dict]:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM initiatives WHERE id = ?",
                           (initiative_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_events(initiative_id: str, limit: int = 50) -> list:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM initiative_events WHERE initiative_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (initiative_id, limit)).fetchall()]
    finally:
        conn.close()


# The roadmap read AFTER the fold (spec §1: "Initiative → folded into Project.
# Roadmap fields move onto projects"). m02+m03 put quarter/tier/why/
# success_check/health/confidence on `projects`, so the quarterly roadmap is a
# read over the PROJECT spine — one noun, not two.
#
# Progress is DERIVED, never stored: it reuses object_graph._progress, the same
# accepted-done roll-up over `tasks.project_id` that project_progress() and the
# entity drawer already use, so the roadmap % cannot drift from the drawer %.
# (Called with a shared connection so one roadmap load is one connection, not
# one per project.)
def projects_by_quarter() -> list:
    """Live projects grouped by `projects.quarter`, newest-quarter-last.

    Returns `[{"quarter": "2026-Q3", "projects": [...]}, …]`. `YYYY-Qn` sorts
    correctly as a plain string; the UNSCHEDULED bucket (quarter NULL or empty)
    is emitted LAST with `quarter: None` — a project without a quarter is still
    a project, so it is grouped, never dropped. Each project carries its
    roadmap fields plus `task_total` / `task_done` / `progress`.
    """
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, slug, name, description, icon, color, kind, status, "
            "quarter, tier, why, success_check, health, confidence, account_id "
            "FROM projects WHERE archived_at IS NULL ORDER BY name"
        ).fetchall()
        groups: dict = {}
        for row in rows:
            p = dict(row)
            p.update(object_graph._progress(conn, "project_id", p["id"]))
            groups.setdefault((p.get("quarter") or ""), []).append(p)
    finally:
        conn.close()
    out = [{"quarter": q, "projects": groups[q]} for q in sorted(k for k in groups if k)]
    if "" in groups:
        out.append({"quarter": None, "projects": groups[""]})
    return out


# ---------------------------------------------------------------- writes

def create_initiative(title: str, project_id: str, owner: str = "ricardo",
                      tier: str = "bet", quarter: Optional[str] = None,
                      confidence: str = "medium", health: str = "on-track",
                      why: str = "", success_check: str = "",
                      description: str = "") -> dict:
    """The validated create path: project must resolve (canonical FK), quarter/
    tier/health validated, UUID id, audited event, export refreshed."""
    pid = db.resolve_project(project_id)
    if not pid:
        return {"status": "error", "error": f"project_id '{project_id}' resolves to no project"}
    if not quarter:
        t = time.localtime()
        quarter = f"{t.tm_year}-Q{(t.tm_mon - 1) // 3 + 1}"
    err = object_graph.validate_initiative_fields(
        {"quarter": quarter, "tier": tier, "health": health})
    if err:
        return {"status": "error", "error": err}
    iid = f"init_{uuid.uuid4().hex[:8]}"
    now = _now()
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO initiatives (id, title, project_id, owner, status, tier, quarter, "
            "confidence, health, progress, why, success_check, description, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, title, pid, owner, "planned", tier, quarter, confidence, health,
             0, why, success_check, description, now, now))
        _log(conn, iid, "initiative_created", {"title": title, "quarter": quarter,
                                               "tier": tier, "via": "create"})
        conn.commit()
    finally:
        conn.close()
    export_roadmap()
    return {"status": "created", "initiative": get_initiative(iid)}


def update_initiative(initiative_id: str, fields: dict) -> dict:
    """The validated update path (replaces every hand-rolled roadmap.json edit):
    enum validation, the D2 derived≠stored rule (typed progress rejected for an
    epic-backed initiative), per-change audit events, export refreshed."""
    fields = {k: v for k, v in (fields or {}).items() if k in FIELDS and v is not None}
    if not fields:
        return {"status": "error", "error": "no valid fields to update"}
    err = object_graph.validate_initiative_fields(fields)
    if err:
        return {"status": "error", "error": err}
    if "status" in fields and fields["status"] not in STATUSES:
        return {"status": "error", "error": f"status must be one of {STATUSES}"}
    if "project_id" in fields:
        pid = db.resolve_project(fields["project_id"])
        if not pid:
            return {"status": "error", "error": f"project_id '{fields['project_id']}' resolves to no project"}
        fields["project_id"] = pid
    prior = get_initiative(initiative_id)
    if prior is None:
        return {"status": "error", "error": "initiative not found"}
    if "progress" in fields and object_graph.initiative_epic_count(initiative_id) > 0:
        return {"status": "error",
                "error": "progress is derived for an initiative with epics — "
                         "complete/accept its tasks instead of typing a number"}
    conn = db.get_conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE initiatives SET {sets}, updated_at = ? WHERE id = ?",
                     (*fields.values(), _now(), initiative_id))
        changed = {k: {"from": prior.get(k), "to": v} for k, v in fields.items()
                   if prior.get(k) != v}
        if changed:
            kind = "status_changed" if list(changed) == ["status"] else "initiative_updated"
            _log(conn, initiative_id, kind, {"changed": changed, "via": "update"})
        conn.commit()
    finally:
        conn.close()
    export_roadmap()
    return {"status": "updated", "initiative": get_initiative(initiative_id)}


def drop_stored_progress(initiative_id: str) -> bool:
    """An initiative that gains its first epic becomes DERIVED — the stored
    number is dropped so it can't masquerade (the Phase-2 ratchet, DB form)."""
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "UPDATE initiatives SET progress = NULL, updated_at = ? "
            "WHERE id = ? AND progress IS NOT NULL", (_now(), initiative_id))
        if cur.rowcount:
            _log(conn, initiative_id, "initiative_updated",
                 {"changed": {"progress": {"to": None}},
                  "via": "epic-created-progress-now-derived"})
        conn.commit()
        dropped = bool(cur.rowcount)
    finally:
        conn.close()
    if dropped:
        export_roadmap()
    return dropped


# ---------------------------------------------------------------- export

def export_roadmap() -> None:
    """Regenerate roadmap.json FROM the DB (transition aid, one release only —
    marked _generated so nothing mistakes it for a source; the Phase-6 ratchet
    checks it stays in sync with the table)."""
    inits = []
    for i in list_initiatives():
        out = {k: v for k, v in i.items()
               if k not in ("created_at", "updated_at") and v is not None}
        inits.append(out)
    ROADMAP_EXPORT.write_text(json.dumps(
        {"_generated": True,
         "_source": "kanban.db:initiatives (Phase 6 — edit via MCP/API, never this file)",
         "initiatives": inits}, indent=2))
