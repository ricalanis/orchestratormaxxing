"""Regression guard for P0-3 (/api/context) + P0-1 attribution (revised-final-plan §2-3).

Pins the one-call entity-context contract the drawer depends on:
  - build_context(type, id) → {entity, ancestors, children, actions} for each of
    task | project | initiative | deal | session,
  - unknown type → ValueError (endpoint 400), missing id → None (endpoint 404),
  - and the P0-1 injective roll-up: in a SHARED project a directly-attributed
    task (tasks.initiative_id) counts for exactly one initiative and the rest are
    `unattributed` — not the old non-injective inherited %.

DB isolation: a COPY of ~/.hermes/kanban.db seeded with a self-contained mini
graph, so the assertions don't depend on live data. Real DB is never touched.
Stdlib unittest, pytest-discoverable. Imports the read layer only (no app/network).

Run: python -m unittest tests.test_context_endpoint   # from orchestrator/
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
try:
    from dashboard import db as _db, object_graph as _graph, context as _context

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

NOW = int(time.time())


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class ContextEndpoint(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_ctx_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig = _db.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _graph.ensure_schema()  # make sure tasks.initiative_id exists on the copy
        self._seed()

    def tearDown(self):
        _db.KANBAN_DB = self._orig
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _seed(self):
        """A shared-project mini graph:
        project PX ─┬─ initiative IA ── deal DX (account AX)
                    └─ initiative IB
        task TA → attributed to IA (tasks.initiative_id)
        task TB → project-only (unattributed)
        session SX → linked to task TA
        """
        c = sqlite3.connect(str(self.tmp))
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                  ("proj_ctxtest", "ctxtest", "Ctx Test Project", NOW))
        for iid, title in (("init_ctx_a", "Initiative A"), ("init_ctx_b", "Initiative B")):
            c.execute("INSERT INTO initiatives (id, title, project_id, status, created_at) "
                      "VALUES (?,?,?,?,?)", (iid, title, "proj_ctxtest", "in_progress", NOW))
        c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  ("acct_ctx", "Ctx Account", NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, initiative_id, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  ("deal_ctx", "acct_ctx", "Ctx Deal", "proposal", "init_ctx_a", NOW))
        base = dict(status="done", created_at=NOW, workspace_kind="none",
                    consecutive_failures=0, goal_mode=0)
        c.execute("INSERT INTO tasks (id, title, project_id, initiative_id, reviewed_at, "
                  "session_id, status, created_at, workspace_kind, consecutive_failures, goal_mode) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ("t_ctx_a", "Task A (attributed)", "proj_ctxtest", "init_ctx_a", NOW,
                   "sess_ctx", base["status"], base["created_at"], base["workspace_kind"],
                   base["consecutive_failures"], base["goal_mode"]))
        c.execute("INSERT INTO tasks (id, title, project_id, status, created_at, "
                  "workspace_kind, consecutive_failures, goal_mode) VALUES (?,?,?,?,?,?,?,?)",
                  ("t_ctx_b", "Task B (unattributed)", "proj_ctxtest", "in_progress", NOW,
                   "none", 0, 0))
        c.execute("INSERT INTO session_meta (session_key, host, role, feature, project, "
                  "auto_compact, auto_abort, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  ("sess_ctx", "local", "coder", "ctx-feature", "proj_ctxtest", 0, 1, NOW, NOW))
        c.commit()
        c.close()

    # ---- shape + traversal -------------------------------------------------

    def test_task_context_ancestors_and_entity(self):
        ctx = _context.build_context("task", "t_ctx_a")
        self.assertEqual(ctx["entity"]["type"], "task")
        self.assertEqual(ctx["entity"]["initiative_id"], "init_ctx_a")
        atypes = {(a["type"], a["id"]) for a in ctx["ancestors"]}
        self.assertIn(("project", "proj_ctxtest"), atypes)
        self.assertIn(("initiative", "init_ctx_a"), atypes)
        self.assertIn(("deal", "deal_ctx"), atypes)  # deal reached via the initiative
        self.assertIn("actions", ctx)

    def test_unattributed_task_shows_only_project(self):
        ctx = _context.build_context("task", "t_ctx_b")
        atypes = {a["type"] for a in ctx["ancestors"]}
        self.assertIn("project", atypes)
        self.assertNotIn("initiative", atypes)  # honest: no fake initiative parent

    def test_project_children(self):
        ctx = _context.build_context("project", "proj_ctxtest")
        kinds = {(c["type"], c["id"]) for c in ctx["children"]}
        self.assertIn(("initiative", "init_ctx_a"), kinds)
        self.assertIn(("initiative", "init_ctx_b"), kinds)
        self.assertIn(("task", "t_ctx_a"), kinds)

    def test_deal_children_spine(self):
        ctx = _context.build_context("deal", "deal_ctx")
        self.assertEqual({a["type"] for a in ctx["ancestors"]}, {"account"})
        child_ids = {c["id"] for c in ctx["children"]}
        self.assertIn("init_ctx_a", child_ids)   # deal → initiative (down the spine)
        self.assertIn("t_ctx_a", child_ids)      # → its attributed task

    def test_session_children(self):
        ctx = _context.build_context("session", "sess_ctx")
        self.assertEqual({c["id"] for c in ctx["children"]}, {"t_ctx_a"})

    def test_unknown_type_and_missing_id(self):
        with self.assertRaises(ValueError):
            _context.build_context("widget", "x")
        self.assertIsNone(_context.build_context("task", "t_nope"))
        self.assertIsNone(_context.build_context("session", "sess_nope"))

    # ---- P0-1 injective roll-up (the reason the column exists) --------------

    def test_shared_project_roll_up_is_injective(self):
        from dashboard import strategy
        pa = _graph.initiative_progress(strategy.get_initiative("init_ctx_a"))
        pb = _graph.initiative_progress(strategy.get_initiative("init_ctx_b"))
        # IA owns exactly its one attributed task; IB owns none.
        self.assertTrue(pa["shared_project"])
        self.assertEqual(pa["task_total"], 1)
        self.assertEqual(pa["progress"], 100)      # the one attributed task is done+reviewed
        self.assertEqual(pb["task_total"], 0)
        self.assertEqual(pb["progress"], 0)        # NOT the old inherited project %
        # The unattributed task belongs to neither → surfaced honestly.
        self.assertGreaterEqual(pb["unattributed"], 1)


if __name__ == "__main__":
    unittest.main()
