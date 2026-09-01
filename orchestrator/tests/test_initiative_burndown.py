"""Regression guard for P2-3 — per-initiative burndown (revised-final-plan Phase 3).

graph.initiative_burndown() is the initiative analogue of the cycle burndown:
remaining accepted-done tasks vs the linear ideal over the initiative's quarter
window (task-span fallback). Pins: committed = the attributed task_total, the
series burns DOWN as accepted-done tasks land, a too-small initiative charts
nothing, and _quarter_window maps YYYY-Qn correctly.

DB isolation: a COPY of ~/.hermes/kanban.db seeded with a self-contained
initiative + tasks. Real DB untouched. Stdlib unittest, pytest-discoverable.

Run: python -m unittest tests.test_initiative_burndown   # from orchestrator/
"""
import os
import shutil
import sqlite3
import sys
import time
import tempfile
import unittest
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
try:
    from dashboard import db as _db, object_graph as _graph, strategy as _strategy

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class InitiativeBurndown(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_burn_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig = _db.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _graph.ensure_schema()
        # Quarter that CONTAINS today, so the burndown window is open.
        now = datetime.date.today()
        self.quarter = f"{now.year}-Q{(now.month - 1) // 3 + 1}"
        q_start = int(time.mktime(datetime.date(now.year, ((now.month - 1) // 3) * 3 + 1, 1).timetuple()))
        c = sqlite3.connect(str(self.tmp))
        c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                  ("proj_burn", "burn", "Burn Test", q_start))
        c.execute("INSERT INTO initiatives (id, title, project_id, status, quarter, created_at) "
                  "VALUES (?,?,?,?,?,?)", ("init_burn", "Burn Init", "proj_burn", "in_progress", self.quarter, q_start))
        # 3 tasks in the sole-initiative project: 2 accepted-done (completed in-window), 1 open.
        mid = q_start + 86400
        for tid, status, comp, rev in (
            ("t_burn_1", "done", mid, mid),
            ("t_burn_2", "done", mid + 86400, mid + 86400),
            ("t_burn_3", "ready", None, None),
        ):
            c.execute("INSERT INTO tasks (id, title, project_id, status, created_at, completed_at, "
                      "reviewed_at, workspace_kind, consecutive_failures, goal_mode) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (tid, f"T {tid}", "proj_burn", status, q_start, comp, rev, "none", 0, 0))
        c.commit(); c.close()

    def tearDown(self):
        _db.KANBAN_DB = self._orig
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def test_quarter_window(self):
        s, e = _graph._quarter_window("2026-Q3")
        self.assertEqual(datetime.date.fromtimestamp(s).isoformat(), "2026-07-01")
        self.assertEqual(datetime.date.fromtimestamp(e).isoformat(), "2026-09-30")
        self.assertEqual(_graph._quarter_window("nope"), (None, None))

    def test_burndown_series_burns_down(self):
        b = _graph.initiative_burndown(_strategy.get_initiative("init_burn"))
        self.assertGreaterEqual(len(b), 2)
        # committed = 3 attributed tasks; remaining starts at 3 and never rises.
        self.assertEqual(b[0]["remaining"], 3)
        remains = [p["remaining"] for p in b]
        self.assertEqual(remains, sorted(remains, reverse=True))   # monotonically non-increasing
        self.assertLess(b[-1]["remaining"], b[0]["remaining"])     # actually burned down
        self.assertTrue(all("ideal" in p for p in b))

    def test_too_small_or_future_quarter_charts_nothing(self):
        # A future quarter → window hasn't started → empty series.
        c = sqlite3.connect(str(self.tmp))
        c.execute("UPDATE initiatives SET quarter = '2099-Q1' WHERE id = 'init_burn'")
        c.commit(); c.close()
        self.assertEqual(_graph.initiative_burndown(_strategy.get_initiative("init_burn")), [])


if __name__ == "__main__":
    unittest.main()
