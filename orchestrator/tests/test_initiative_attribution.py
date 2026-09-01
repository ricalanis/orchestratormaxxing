"""Regression guard for P1-3 — initiative attribution write path + injective roll-up.

Pins the setter behind the P1-3 backfill (and the §5.3 Assign→Initiative UI):
  - set_task_initiative() sets / clears tasks.initiative_id, validates the FK,
  - a directly-attributed task counts toward exactly ONE initiative (injective),
    and its siblings in the shared project don't inherit it,
  - the p1_3 migration is idempotent and never clobbers an existing attribution.

DB isolation: a COPY of ~/.hermes/kanban.db seeded with a shared-project mini
graph. Real DB untouched. Stdlib unittest, pytest-discoverable.

Run: python -m unittest tests.test_initiative_attribution   # from orchestrator/
"""
import os
import shutil
import sqlite3
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
try:
    from dashboard import db as _db, object_graph as _graph, strategy as _strategy

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

NOW = int(time.time())


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class InitiativeAttribution(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_attr_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig = _db.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _graph.ensure_schema()
        c = sqlite3.connect(str(self.tmp))
        c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                  ("proj_attr", "attr", "Attr Test", NOW))
        for iid, t in (("init_attr_a", "Init A"), ("init_attr_b", "Init B")):  # shared project
            c.execute("INSERT INTO initiatives (id, title, project_id, status, created_at) "
                      "VALUES (?,?,?,?,?)", (iid, t, "proj_attr", "in_progress", NOW))
        for tid in ("t_attr_1", "t_attr_2"):
            c.execute("INSERT INTO tasks (id, title, project_id, status, created_at, "
                      "workspace_kind, consecutive_failures, goal_mode, reviewed_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (tid, f"T {tid}", "proj_attr", "done", NOW, "none", 0, 0, NOW))
        c.commit(); c.close()

    def tearDown(self):
        _db.KANBAN_DB = self._orig
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _iid_of(self, tid):
        c = sqlite3.connect(str(self.tmp))
        row = c.execute("SELECT initiative_id FROM tasks WHERE id = ?", (tid,)).fetchone()
        c.close()
        return row[0] if row else None

    def test_set_and_clear(self):
        self.assertEqual(_graph.set_task_initiative("t_attr_1", "init_attr_a"),
                         {"task_id": "t_attr_1", "initiative_id": "init_attr_a"})
        self.assertEqual(self._iid_of("t_attr_1"), "init_attr_a")
        _graph.set_task_initiative("t_attr_1", None)          # clear
        self.assertIsNone(self._iid_of("t_attr_1"))

    def test_invalid_initiative_rejected(self):
        self.assertIn("not found", _graph.set_task_initiative("t_attr_1", "init_nope")["error"])
        self.assertIsNone(self._iid_of("t_attr_1"))

    def test_missing_task_reported(self):
        self.assertIn("not found", _graph.set_task_initiative("t_nope", "init_attr_a")["error"])

    def test_attribution_is_injective(self):
        # Shared project: before attribution both initiatives suppress (0 + unattributed).
        pa = _graph.initiative_progress(_strategy.get_initiative("init_attr_a"))
        self.assertTrue(pa["shared_project"])
        self.assertEqual(pa["task_total"], 0)
        self.assertGreaterEqual(pa["unattributed"], 2)

        _graph.set_task_initiative("t_attr_1", "init_attr_a")
        pa = _graph.initiative_progress(_strategy.get_initiative("init_attr_a"))
        pb = _graph.initiative_progress(_strategy.get_initiative("init_attr_b"))
        self.assertEqual(pa["task_total"], 1)          # A owns exactly its one task
        self.assertEqual(pa["progress"], 100)          # done + reviewed
        self.assertEqual(pb["task_total"], 0)          # sibling did NOT inherit it
        self.assertEqual(pa["unattributed"], 1)        # the other task still unattributed

    def test_migration_idempotent_and_no_clobber(self):
        from dashboard.migrations import p1_3_initiative_attribution as mig
        # Manually pre-attribute one mapped task to a DIFFERENT initiative; the
        # migration must NOT clobber it.
        first = next(iter(mig.ATTRIBUTION))
        c = sqlite3.connect(str(self.tmp))
        # ensure the mapped tasks/initiatives exist in this copy (they're real ids)
        exists = c.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (first,)).fetchone()[0]
        c.close()
        if not exists:
            self.skipTest("mapped tasks not present in this DB copy")
        _graph.set_task_initiative(first, "init_attr_a")
        r1 = mig.run()
        r2 = mig.run()
        self.assertEqual(self._iid_of(first), "init_attr_a")   # not clobbered
        self.assertGreaterEqual(r1["skipped_existing"], 1)
        self.assertEqual(r2["applied"], 0)                     # idempotent second run


if __name__ == "__main__":
    unittest.main()
