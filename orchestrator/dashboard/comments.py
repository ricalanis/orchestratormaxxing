"""Task comments — write layer for the entity-drawer Comments section.

The kanban DB already ships a `task_comments` table (created by `hermes kanban`
and written by `hermes kanban comment`): 44+ rows with the shape

    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT,
    body TEXT, created_at INTEGER   -- unix epoch seconds

so `ensure_schema()` recreates *that* shape for a fresh/empty DB (NOT the
`id TEXT` / `TIMESTAMP` variant a naive migration might imagine — a mismatched
CREATE IF NOT EXISTS would be a silent no-op on the live DB and diverge on a
fresh one). Reads live in `db.get_task_comments`; this module owns the writes.

Writes are direct SQL (the `sprints`/`identity` precedent for the sidecar's own
columns) rather than the CLI, because (a) there is no `hermes kanban` verb to
*delete* a comment, and (b) direct SQL keeps the feature self-contained and
testable against a copied DB. created_at is stored as epoch seconds to match the
existing rows and the `ORDER BY created_at ASC` read.
"""
import time
from typing import Optional

from . import db


def ensure_schema() -> None:
    """Idempotently ensure task_comments exists, matching hermes' shape."""
    conn = db.get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    TEXT NOT NULL,
                author     TEXT NOT NULL,
                body       TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def add_comment(task_id: str, body: str, author: str) -> dict:
    """Append a comment to a task. Returns the created row, or an error dict.

    Guards: the task must exist (404 at the edge) and the body must be non-empty
    (400) — an empty comment is never worth a row."""
    body = (body or "").strip()
    author = (author or "").strip() or "ricardo"
    if not body:
        return {"status": "error", "error": "comment body is empty"}
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not exists:
            return {"status": "error", "error": "task not found"}
        created_at = int(time.time())
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
            (task_id, author, body, created_at),
        )
        conn.commit()
        return {
            "status": "created",
            "comment": {
                "id": cur.lastrowid,
                "task_id": task_id,
                "author": author,
                "body": body,
                "created_at": created_at,
            },
        }
    finally:
        conn.close()


def delete_comment(comment_id) -> dict:
    """Hard-delete one comment by id. 404 if it doesn't exist."""
    conn = db.get_conn()
    try:
        cur = conn.execute("DELETE FROM task_comments WHERE id = ?", (comment_id,))
        conn.commit()
        if cur.rowcount == 0:
            return {"status": "error", "error": "comment not found"}
        return {"status": "deleted", "comment_id": comment_id}
    finally:
        conn.close()
