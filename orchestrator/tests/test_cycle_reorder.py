"""Regression guard for cycle-board drag-reorder persistence
(sprints.reorder_cycle_tasks + get_cycle_board's board_order ordering).

DB isolation: a wiped/seeded COPY of ~/.hermes/kanban.db; the real DB is never
touched. Skips where no DB exists. Stdlib unittest, pytest-discoverable.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
try:
    from dashboard import db as _db, sprints as _sprints  # no import side effects

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False


@unittest.skipUnless(_READY, "dashboard.sprints / kanban.db unavailable")
class CycleReorder(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_reorder_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig = (_db.KANBAN_DB, _sprints.KANBAN_DB)
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        _sprints.ensure_cycle_schema()
        conn = sqlite3.connect(str(self.tmp))
        # `create_cycle()` auto-commits tasks already scheduled for its ISO
        # week. Clear that forward-planning state too, or rows copied from the
        # live DB leak into this four-task reorder fixture.
        conn.execute(
            "UPDATE tasks SET sprint_id = NULL, board_order = NULL, scheduled_week = NULL, "
            "deal_id = NULL"  # copied cadence rows must not re-enter idx_tasks_deal_cadence_open when forced to 'ready'
        )
        conn.execute("DELETE FROM task_sprints")
        conn.execute("DELETE FROM sprints")
        conn.commit()
        self.cid = _sprints.create_cycle()["id"]
        _sprints.start_sprint(self.cid)
        conn.row_factory = sqlite3.Row
        self.ids = [r["id"] for r in conn.execute("SELECT id FROM tasks LIMIT 4")]
        conn.close()
        for t in self.ids:
            _sprints.assign_task_sprint(t, self.cid)
            _sprints.set_task_status(t, "ready")   # all land in the backlog column

    def tearDown(self):
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _backlog(self):
        return [t["id"] for t in _sprints.get_cycle_board(self.cid)["columns"]["backlog"]]

    def test_reorder_persists_and_reads_back(self):
        reversed_order = list(reversed(self._backlog()))
        r = _sprints.reorder_cycle_tasks(self.cid, reversed_order)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["reordered"], len(self.ids))
        self.assertEqual(self._backlog(), reversed_order)   # get_cycle_board honors board_order

    def test_ignores_tasks_not_in_cycle(self):
        r = _sprints.reorder_cycle_tasks(self.cid, ["t_stranger"] + self.ids)
        self.assertEqual(r["reordered"], len(self.ids))     # stranger skipped

    def test_unknown_cycle_errors(self):
        self.assertEqual(_sprints.reorder_cycle_tasks("cyc_nope", self.ids)["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
