"""Regression guard for the task-comments system (dashboard/api.py + comments.py).

Pins the CRUD HTTP contract the drawer's Comments section depends on:
  - POST /api/tasks/{id}/comments  {text|body, author} → 200 + created row,
    404 on an unknown task, 400 on an empty body,
  - GET  /api/tasks/{id}/comments  → oldest→newest list,
  - DELETE /api/comments/{id}      → 200, then 404 (gone stays gone).

Key invariant under test: the code targets hermes' EXISTING task_comments shape
(id INTEGER AUTOINCREMENT, created_at INTEGER epoch), not a `TEXT`/`TIMESTAMP`
variant — so it works against the live DB and a fresh one alike.

Isolation (the test_context_endpoint pattern): setUp points `db.KANBAN_DB` at a
COPY of ~/.hermes/kanban.db and seeds a task; tearDown restores it. Rebinding the
pointer *per test* (not once at import) is deliberate — every TestClient test
file reassigns that shared global, so an import-time copy gets clobbered by
whichever module imports last. The app reads db.KANBAN_DB at call time, so the
setUp rebind steers it correctly during each test. Real DB is never written.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        from dashboard.api import app  # ensure_schema runs on import (against whatever DB)
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover - environment without deps/DB
    _READY = False

_TASK_ID = "t_comment_test"


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class TaskComments(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_comments_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig = _db.KANBAN_DB
        _db.KANBAN_DB = self.tmp   # steer the app's get_conn() at call time
        c = sqlite3.connect(tmp)
        c.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind, "
            "consecutive_failures, goal_mode) VALUES (?,?,?,?,?,?,?)",
            (_TASK_ID, "Comment Test Task", "todo", int(time.time()), "none", 0, 0),
        )
        c.commit()
        c.close()

    def tearDown(self):
        _db.KANBAN_DB = self._orig
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def test_full_lifecycle(self):
        base = f"/api/tasks/{_TASK_ID}/comments"
        # Empty to start (fresh task).
        r = _CLIENT.get(base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["comments"], [])

        # Create one.
        r = _CLIENT.post(base, json={"text": "first comment", "author": "tester"})
        self.assertEqual(r.status_code, 200)
        created = r.json()["comment"]
        self.assertEqual(created["author"], "tester")
        self.assertEqual(created["body"], "first comment")
        self.assertIsInstance(created["created_at"], int)  # epoch, not a TIMESTAMP string
        cid = created["id"]

        # It shows up in the list.
        r = _CLIENT.get(base)
        self.assertEqual([c["body"] for c in r.json()["comments"]], ["first comment"])

        # Delete it → gone; deleting again → 404.
        self.assertEqual(_CLIENT.delete(f"/api/comments/{cid}").status_code, 200)
        self.assertEqual(_CLIENT.get(base).json()["comments"], [])
        self.assertEqual(_CLIENT.delete(f"/api/comments/{cid}").status_code, 404)

    def test_body_alias_and_ordering(self):
        base = f"/api/tasks/{_TASK_ID}/comments"
        # `body` is accepted as an alias for `text`.
        self.assertEqual(_CLIENT.post(base, json={"body": "older", "author": "a"}).status_code, 200)
        time.sleep(1)  # created_at is second-resolution; ensure a strict ordering
        self.assertEqual(_CLIENT.post(base, json={"text": "newer", "author": "b"}).status_code, 200)
        bodies = [c["body"] for c in _CLIENT.get(base).json()["comments"]]
        self.assertEqual(bodies, ["older", "newer"])  # oldest → newest

    def test_empty_body_is_400(self):
        r = _CLIENT.post(f"/api/tasks/{_TASK_ID}/comments", json={"text": "   ", "author": "x"})
        self.assertEqual(r.status_code, 400)

    def test_unknown_task_is_404(self):
        r = _CLIENT.post("/api/tasks/t_does_not_exist/comments", json={"text": "hi", "author": "x"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
