"""Attachments — the five-facet project hub, and the pointer table under it.

Journey F3.5 (directiva ADICIÓN 7). A project is a **hub of five facets**:
conversations (Fireflies) · resources (Drive) · code (GitHub) · plans (the
`~/dev/planning` repo) · tasks (the orchestrator). This module is the surface
over `attachments`, the generic pointer table created by migration
`m10_attachments`, plus the read that projects a project into those five
facets.

Three rules are enforced here rather than left to the caller, each because the
writers are FOUR HOST SKILLS (Claude · Codex plugin · Hermes · OpenCode) all
firing the same sentence — *"plan profundo terminado → attachment registrado"*:

  * **`node_kind` and `kind` are fixed enums.** Both carry a CHECK in the
    schema; both are validated against the mirrored tuples *before* the write so
    a bad value is a typed 400 naming the allowed vocabulary, not a raw
    IntegrityError 500. Same rule, same reason as `threads.ROLES`.
  * **The node must exist.** `attachments.node_id` carries no foreign key —
    SQLite cannot declare one whose target table depends on another column — so
    existence is checked against the table `node_kind` names. A pointer at a row
    that does not exist looks like success and then renders an empty facet
    forever; that is the class of quiet lie this phase exists to kill.
  * **Registering the same artifact twice UPDATES, it does not duplicate.** A
    re-run of a skill, a planning-repo re-sync, or two hosts finishing the same
    plan must converge on one row. `add_attachment` upserts against the same
    keys the two partial UNIQUE indexes enforce (`node_kind, node_id, kind, url`
    and `…, path`), so the outcome is identical whether the dedupe is won by the
    application or by the engine.

**An attachment is a pointer, never a copy** — which is why `tasks` is the one
facet with no attachment rows: tasks already exist, with their own lifecycle and
their own writers, so `list_project_hub` COUNTS them where they live. Shadowing
them here would create a second truth about what work a project has.

Module-layer convention (same as `threads`/`sprints`/`crm`): errors are returned
as `{"status": "error", "code": ..., "error": ...}` dicts rather than raised, so
the HTTP edge maps them to codes and any non-HTTP caller (MCP, a skill, a cron)
gets a dict. Every connection comes from `db.get_conn()` at call time, so a test
that repoints `db.KANBAN_DB` at a copy is honoured.
"""
import sqlite3
import time
import uuid
from typing import Optional

from . import db

# The CHECK constraints on attachments, mirrored (m10_attachments.py). The value
# is the vocabulary; the mapping is what makes `node_kind` checkable: each kind
# names the table its `node_id` must exist in.
NODE_TABLES = {
    "account": "accounts",
    "deal": "deals",
    "project": "projects",
    "task": "tasks",
}
NODE_KINDS = tuple(NODE_TABLES)
KINDS = ("conversation", "resource", "code", "plan")

# The four facets that are backed by attachment rows, in the order the drawer
# reads them. `tasks` is deliberately absent — see the module docstring.
FACET_OF_KIND = {
    "conversation": "conversations",
    "resource": "resources",
    "code": "code",
    "plan": "plans",
}

# A task is OPEN unless it is finished or archived. Mirrors the definition
# `sprints.archive_project` uses for the same question, plus the archive filter
# `sprints` applies elsewhere — a project hub must not count parked work as live.
_OPEN_TASKS = "status NOT IN ('done', 'rejected') AND archived_at IS NULL"


def _now() -> int:
    return int(time.time())


def _gen() -> str:
    return f"att_{uuid.uuid4().hex[:8]}"


def _error(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "error": message}


def _clean(value) -> Optional[str]:
    """Normalize an optional text field: whitespace-only is absent, not empty.

    `url=""` and `url="   "` must behave exactly like `url=None`, or the
    `CHECK (url IS NOT NULL OR path IS NOT NULL)` floor is trivially satisfied
    by an empty string and the row points at nothing.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row(r) -> dict:
    """One attachment row as a facet item.

    Every item a facet ships — attachment or derived — carries `source`, so the
    drawer can render one list per facet without knowing which rows came from
    this table and which were derived from Fireflies or `projects.repo_path`.
    """
    d = dict(r)
    d["source"] = "attachment"
    return d


def _validate(node_kind, kind, title, url, path) -> tuple:
    """Shared validation for a write. Returns `(error_dict, normalized)`."""
    nk = str(node_kind or "").strip().lower()
    if nk not in NODE_TABLES:
        return _error("bad_node_kind",
                      f"node_kind '{node_kind}' is not one of "
                      f"{', '.join(NODE_KINDS)}"), None
    k = str(kind or "").strip().lower()
    if k not in KINDS:
        return _error("bad_kind",
                      f"kind '{kind}' is not one of {', '.join(KINDS)}"), None
    clean_title = str(title or "").strip()
    if not clean_title:
        return _error("bad_title", "title cannot be empty"), None
    clean_url, clean_path = _clean(url), _clean(path)
    if clean_url is None and clean_path is None:
        return _error("missing_target",
                      "an attachment needs a url or a path (it is a pointer — "
                      "one that points at nothing is not an attachment)"), None
    return None, (nk, k, clean_title, clean_url, clean_path)


def add_attachment(node_kind: str, node_id: str, kind: str, title: str,
                   url=None, path=None, source_agent=None) -> dict:
    """Register (or re-register) one pointer on one node.

    **Upsert, not insert.** The identity of an attachment is its target on that
    node: `(node_kind, node_id, kind, url)` or `(node_kind, node_id, kind,
    path)` — the two keys the partial UNIQUE indexes enforce. A second
    registration of the same target UPDATES `title` / `source_agent` /
    `updated_at` and keeps the original `id` and `created_at`, so a skill that
    re-runs, or a second host that finishes the same plan, refreshes the row
    instead of appending a duplicate the drawer would render twice.

    The url lookup runs before the path lookup, and both run before the INSERT:
    that ordering is what makes a row carrying BOTH a url and a path safe to
    write — by the time we insert, neither key can already exist, so the engine's
    UNIQUE indexes cannot fire an IntegrityError we would have to translate.

    Re-keying is deliberately NOT an upsert: changing the url of an existing
    attachment is a different pointer, so it creates a row (and the old one is
    removed explicitly, or kept as the other artifact it names).
    """
    err, norm = _validate(node_kind, kind, title, url, path)
    if err:
        return err
    nk, k, clean_title, clean_url, clean_path = norm
    agent = _clean(source_agent)

    conn = db.get_conn()
    try:
        table = NODE_TABLES[nk]
        nid = str(node_id or "").strip()
        if not nid:
            return _error("unknown_node", f"{nk} '' not found")
        exists = conn.execute(
            f"SELECT id FROM {table} WHERE id = ?", (nid,)).fetchone()
        if exists is None:
            return _error("unknown_node", f"{nk} '{nid}' not found")

        found = None
        if clean_url is not None:
            found = conn.execute(
                "SELECT * FROM attachments WHERE node_kind = ? AND node_id = ? "
                "AND kind = ? AND url = ?", (nk, nid, k, clean_url)).fetchone()
        if found is None and clean_path is not None:
            found = conn.execute(
                "SELECT * FROM attachments WHERE node_kind = ? AND node_id = ? "
                "AND kind = ? AND path = ?", (nk, nid, k, clean_path)).fetchone()

        now = _now()
        if found is not None:
            conn.execute(
                "UPDATE attachments SET title = ?, source_agent = ?, updated_at = ? "
                "WHERE id = ?", (clean_title, agent, now, found["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM attachments WHERE id = ?",
                               (found["id"],)).fetchone()
            return {"status": "ok", "created": False, "attachment": _row(row)}

        att_id = _gen()
        conn.execute(
            "INSERT INTO attachments (id, node_kind, node_id, kind, url, path, "
            "title, source_agent, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (att_id, nk, nid, k, clean_url, clean_path, clean_title, agent, now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        return {"status": "ok", "created": True, "attachment": _row(row)}
    finally:
        conn.close()


def list_for(node_kind: str, node_id: str) -> dict:
    """Every attachment hanging off one node, grouped by kind.

    Grouped as well as flat because every consumer wants one of the two and
    neither should have to re-derive the other: the project drawer renders per
    facet, an agent asking "what is attached here" wants the list. `by_kind`
    always carries all four keys, so an empty facet is an empty list rather than
    a missing key the caller has to guard.

    Ordering is total and stable — kind, then most-recently-updated, then id —
    never the arbitrary order SQLite hands back on ties.
    """
    nk = str(node_kind or "").strip().lower()
    if nk not in NODE_TABLES:
        return _error("bad_node_kind",
                      f"node_kind '{node_kind}' is not one of "
                      f"{', '.join(NODE_KINDS)}")
    nid = str(node_id or "").strip()
    if not nid:
        return _error("missing_node_id", "node_id is required")

    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE node_kind = ? AND node_id = ? "
            "ORDER BY kind, updated_at DESC, id", (nk, nid)).fetchall()
    finally:
        conn.close()

    items = [_row(r) for r in rows]
    by_kind = {k: [] for k in KINDS}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)
    return {
        "status": "ok",
        "node_kind": nk,
        "node_id": nid,
        "attachments": items,
        "by_kind": by_kind,
        "count": len(items),
    }


def remove(attachment_id: str) -> dict:
    """Delete one attachment. A missing row is `not_found`, never a silent ok —
    a caller that believes it removed a pointer which is still on the board has
    been told the wrong thing."""
    aid = str(attachment_id or "").strip()
    if not aid:
        return _error("not_found", "attachment '' not found")
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (aid,)).fetchone()
        if row is None:
            return _error("not_found", f"attachment '{aid}' not found")
        conn.execute("DELETE FROM attachments WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "removed": aid, "attachment": _row(row)}


def _fireflies_for_project(conn, project_id: str) -> list:
    """The conversations a project ALREADY has, via the money→delivery join.

    `fireflies_meetings.deal_id` → `deals.project_id` → this project. These rows
    predate the attachments table and are the reason the conversations facet is
    not simply "kind = 'conversation'": the meetings of a project's deals belong
    on the project hub whether or not anyone remembered to register them.

    Degrades to `[]` on a DB without the table (same guard as
    `db.fireflies_meetings_for_deal`), so a hub read can never 500 because the
    Fireflies cache was never installed.
    """
    try:
        rows = conn.execute(
            "SELECT f.id, f.transcript_id, f.title, f.meeting_date, "
            "       f.duration_seconds, f.deal_id, d.title AS deal_title "
            "FROM fireflies_meetings f JOIN deals d ON d.id = f.deal_id "
            "WHERE d.project_id = ? "
            "ORDER BY f.meeting_date DESC, f.fetched_at DESC, f.id",
            (project_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) | {"source": "fireflies", "kind": "conversation"} for r in rows]


def list_project_hub(project_id: str) -> dict:
    """The five-facet read: one project projected into its whole surface.

    Each facet is `{"items": [...], "count": n}` and every item carries
    `source`, because two of the five are UNIONS of registered pointers with
    facts the system already knew:

      * **conversations** = the Fireflies meetings of this project's deals
        (`deals.project_id`) ∪ `kind = 'conversation'`.
      * **code** = `projects.repo_path`, when set ∪ `kind = 'code'`.
      * **resources** / **plans** = attachments only.
      * **tasks** = a COUNT over the `tasks` table — never attachment rows. The
        work lives there, with its own writers; duplicating it here would create
        a second answer to "what is open on this project".

    Registration WINS over derivation on a tie: a derived Fireflies row folds
    away when its `transcript_id` already appears inside a registered
    conversation's url or path, and the derived `repo_path` folds when some code
    attachment already names that exact path. Otherwise the same meeting would
    render twice the moment a skill registered it — which is precisely what the
    skills are being told to do.

    Accepts an id or a slug (same resolution as `sprints.get_project_detail`).
    Read-only.
    """
    ref = str(project_id or "").strip()
    if not ref:
        return _error("not_found", "project '' not found")

    conn = db.get_conn()
    try:
        proj = conn.execute(
            "SELECT id, slug, name, status, repo_path FROM projects "
            "WHERE id = ? OR slug = ?", (ref, ref)).fetchone()
        if proj is None:
            return _error("not_found", f"project '{ref}' not found")
        project = dict(proj)
        pid = project["id"]

        rows = conn.execute(
            "SELECT * FROM attachments WHERE node_kind = 'project' AND node_id = ? "
            "ORDER BY updated_at DESC, id", (pid,)).fetchall()
        attached = {k: [] for k in KINDS}
        for r in rows:
            attached.setdefault(r["kind"], []).append(_row(r))

        meetings = _fireflies_for_project(conn, pid)

        open_tasks = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE project_id = ? AND {_OPEN_TASKS}",
            (pid,)).fetchone()[0]
        total_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (pid,)).fetchone()[0]
        proposal_rows = conn.execute(
            "SELECT p.*, d.title AS deal_title, "
            "EXISTS(SELECT 1 FROM commercial_proposal_sends s WHERE s.packet_id=p.id) AS has_send "
            "FROM commercial_proposal_packets p JOIN deals d ON d.id=p.deal_id "
            "WHERE d.project_id=? ORDER BY p.updated_at DESC", (pid,)).fetchall()
    finally:
        conn.close()

    # --- conversations: registered pointers first, then the meetings they
    # don't already name.
    registered_targets = [
        (a.get("url") or "") + " " + (a.get("path") or "")
        for a in attached["conversation"]
    ]
    derived_meetings = [
        m for m in meetings
        if not any(m["transcript_id"] and m["transcript_id"] in t
                   for t in registered_targets)
    ]
    conversations = attached["conversation"] + derived_meetings

    # --- code: the repo the project already declares, unless a code attachment
    # already points at it.
    code = list(attached["code"])
    repo_path = _clean(project.get("repo_path"))
    if repo_path and not any((a.get("path") or "") == repo_path for a in code):
        code.append({
            "source": "project",
            "kind": "code",
            "id": None,
            "node_kind": "project",
            "node_id": pid,
            "url": None,
            "path": repo_path,
            "title": repo_path.rstrip("/").rsplit("/", 1)[-1] or repo_path,
            "source_agent": None,
        })

    proposals = [{
        "source": "commercial_proposal",
        "kind": "proposal",
        "id": row["id"],
        "node_kind": "project",
        "node_id": pid,
        "url": None,
        "path": f"{row['workspace_path'].rstrip('/')}/{row['proposal_path'].lstrip('/')}",
        "title": f"{row['deal_title']} · r{row['revision']} · "
                 f"{'sent' if row['has_send'] else 'verified' if row['verified_at'] else 'draft'}",
        "source_agent": None,
    } for row in proposal_rows]

    def facet(items) -> dict:
        return {"items": items, "count": len(items)}

    return {
        "status": "ok",
        "project": project,
        "facets": {
            "conversations": facet(conversations),
            "resources": facet(attached["resource"]),
            "code": facet(code),
            "plans": facet(attached["plan"]),
            "proposals": facet(proposals),
            # The one facet that is a count, not a list of pointers.
            "tasks": {"open": open_tasks, "total": total_tasks},
        },
    }
