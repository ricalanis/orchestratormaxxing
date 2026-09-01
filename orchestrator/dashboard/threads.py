"""The Telegram thread registry — reads and edits for the Agents threads panel.

Consolidation spec §2 ("Thread registry"). The table is created and hand-seeded
by migration `m02_spine`; this module is only its *surface*. Two rules from the
spec are enforced here rather than left to the caller:

  * **`role` is a fixed 5-value enum** (`code · growth · ops · health · personal`)
    — free text becomes 22 roles inside a month. The DB already carries the
    CHECK; we validate against the same tuple *before* the write so a bad role
    is a typed 400 naming the allowed values, not a raw IntegrityError 500.
  * **A thread is never auto-created for a project.** There is no `create` verb
    in this module on purpose: the registry is hand-seeded and edited, and a
    design whose thread list grows with the backlog punishes the operator for having
    ideas. `project_id` is nullable and clearing it is a first-class edit.

`project_id` is FK-validated against a live project (or explicitly cleared with
`null`) — binding a thread to a project id that does not exist would make the
dispatch resolver silently fall back to "Hoy" forever, which is exactly the
class of quiet lie the dispatch work removed.

Module-layer convention (same as `sprints`/`crm`): errors are returned as
`{"status": "error", "code": ..., "error": ...}` dicts rather than raised, so
the HTTP edge (`_or_http`) maps them to codes and any non-HTTP caller gets a
dict. Every connection comes from `db.get_conn()` at call time, so a test that
repoints `db.KANBAN_DB` at a copy is honoured.
"""
from typing import Optional

from . import db

# The CHECK constraint on threads.role, mirrored (m02_spine.py, widened by
# m25_thread_role_design.py). Adding a value here WITHOUT the matching migration
# turns every PATCH to it into an IntegrityError 500 — test_threads_api's
# role test walks this tuple against the live CHECK precisely to catch that.
ROLES = ("code", "growth", "ops", "health", "personal", "design")

# `status` carries no CHECK in the schema, but the registry only has two states:
# it is in a picker, or it is not. Anything else is a typo that would make a
# thread invisible to both the panel and the dispatch resolver.
STATUSES = ("active", "archived")

# Fields a PATCH may touch. `thread_id` and `chat_id` are identity, not content.
EDITABLE = ("name", "role", "project_id", "status")


def _error(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "error": message}


def _row(r) -> dict:
    """One registry row as the panel wants it: the stored columns plus the
    derived `archived` flag (the panel greys these and keeps them out of
    pickers) and the bound project's name so the table needs no second fetch."""
    d = dict(r)
    d["archived"] = (d.get("status") or "") == "archived"
    return d


def list_threads() -> dict:
    """The whole registry, **active first** — an archived topic is history, and
    history never outranks a live thread in a list a human scans top-down.
    Within a group: most-recently-active first, then thread_id so the order is
    total and stable (never the arbitrary order SQLite hands back on ties)."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT t.*, p.name AS project_name, p.slug AS project_slug "
            "FROM threads t LEFT JOIN projects p ON p.id = t.project_id "
            "ORDER BY (t.status <> 'active'), COALESCE(t.last_activity_at, 0) DESC, "
            "         t.thread_id"
        ).fetchall()
    finally:
        conn.close()
    threads = [_row(r) for r in rows]
    return {
        "threads": threads,
        "roles": list(ROLES),
        "active": sum(1 for t in threads if not t["archived"]),
        "archived": sum(1 for t in threads if t["archived"]),
    }


def get_thread(thread_id) -> Optional[dict]:
    try:
        wanted = int(thread_id)
    except (TypeError, ValueError):
        return None
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT t.*, p.name AS project_name, p.slug AS project_slug "
            "FROM threads t LEFT JOIN projects p ON p.id = t.project_id "
            "WHERE t.thread_id = ?", (wanted,)).fetchone()
    finally:
        conn.close()
    return _row(row) if row is not None else None


def update_thread(thread_id, fields: dict) -> dict:
    """PATCH one registry row. Only the keys PRESENT in `fields` are written —
    `{"project_id": null}` clears the binding, an absent `project_id` leaves it
    alone. That distinction is the whole reason this takes the raw body dict
    instead of keyword arguments with `None` defaults."""
    try:
        wanted = int(thread_id)
    except (TypeError, ValueError):
        return _error("not_found", f"thread '{thread_id}' not found")

    fields = fields or {}
    unknown = [k for k in fields if k not in EDITABLE]
    if unknown:
        return _error("unknown_field",
                      f"not editable: {', '.join(sorted(unknown))} "
                      f"(editable: {', '.join(EDITABLE)})")
    supplied = [k for k in EDITABLE if k in fields]
    if not supplied:
        return _error("empty_patch",
                      f"nothing to update (editable: {', '.join(EDITABLE)})")

    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (wanted,)).fetchone()
        if row is None:
            return _error("not_found", f"thread {wanted} not found")

        sets, params = [], []

        if "name" in fields:
            name = str(fields["name"] or "").strip()
            if not name:
                return _error("bad_name", "name cannot be empty")
            sets.append("name = ?")
            params.append(name)

        if "role" in fields:
            role = str(fields["role"] or "").strip().lower()
            if role not in ROLES:
                return _error("bad_role",
                              f"role '{fields['role']}' is not one of {', '.join(ROLES)}")
            sets.append("role = ?")
            params.append(role)

        if "status" in fields:
            status = str(fields["status"] or "").strip().lower()
            if status not in STATUSES:
                return _error("bad_status",
                              f"status '{fields['status']}' is not one of "
                              f"{', '.join(STATUSES)}")
            sets.append("status = ?")
            params.append(status)

        if "project_id" in fields:
            project_id = fields["project_id"]
            if project_id in (None, ""):
                sets.append("project_id = NULL")
            else:
                pid = str(project_id)
                exists = conn.execute(
                    "SELECT id FROM projects WHERE id = ?", (pid,)).fetchone()
                if exists is None:
                    return _error("unknown_project", f"project '{pid}' not found")
                sets.append("project_id = ?")
                params.append(pid)

        params.append(wanted)
        conn.execute(f"UPDATE threads SET {', '.join(sets)} WHERE thread_id = ?", params)
        conn.commit()

        updated = conn.execute(
            "SELECT t.*, p.name AS project_name, p.slug AS project_slug "
            "FROM threads t LEFT JOIN projects p ON p.id = t.project_id "
            "WHERE t.thread_id = ?", (wanted,)).fetchone()
    finally:
        conn.close()
    return {"status": "ok", "updated": supplied, "thread": _row(updated)}
