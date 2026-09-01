"""Regression guard for the Cycle-tab empty-state signal.

get_cycle_board() reports `any_cycles` in its no-active-cycle response so the UI
can tell a first-run blank slate (→ the "No cycles yet" empty state + create-first
CTA) apart from "cycles exist but none is active this week" (→ the lighter
start-this-week prompt). A refactor that drops or inverts that flag would silently
show the wrong state on a fresh install. This pins the flag's three cases.

DB isolation: a COPY of ~/.hermes/kanban.db, wiped/seeded per case — the real DB
is never touched. Skips where no kanban.db exists. Stdlib unittest (no pytest dep),
pytest-discoverable. Imports only dashboard.sprints (no app import / no network).

Run: python -m unittest tests.test_cycle_empty_state   # from orchestrator/
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
    from dashboard import db as _db, sprints as _sprints  # no import side effects

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False


@unittest.skipUnless(_READY, "dashboard.sprints / kanban.db unavailable")
class CycleEmptyStateSignal(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_empty_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        # Redirect the DB layer at the copy for the duration of the test.
        self._orig_db, self._orig_spr = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp

    def tearDown(self):
        _db.KANBAN_DB = self._orig_db
        _sprints.KANBAN_DB = self._orig_spr
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _wipe_cycles(self):
        """Remove every cycle (and its membership) → a truly blank slate."""
        conn = sqlite3.connect(str(self.tmp))
        conn.execute("UPDATE tasks SET sprint_id = NULL")
        conn.execute("DELETE FROM task_sprints")
        conn.execute("DELETE FROM sprints")
        conn.commit()
        conn.close()

    def test_no_cycles_at_all_flags_empty(self):
        # First-run slate: has_active False AND any_cycles False → "No cycles yet".
        self._wipe_cycles()
        board = _sprints.get_cycle_board()
        self.assertFalse(board["has_active"])
        self.assertFalse(board["any_cycles"])
        self.assertIsNone(board["cycle"])

    def test_cycles_exist_but_none_active_is_not_empty(self):
        # A planning (not-started) cycle exists → not active, but any_cycles True,
        # so the UI shows "no active this week", NOT the first-run empty state.
        self._wipe_cycles()
        _sprints.create_cycle(start_date=int(time.time()) + 60 * 86400)
        board = _sprints.get_cycle_board()
        self.assertFalse(board["has_active"])
        self.assertTrue(board["any_cycles"])

    def test_active_cycle_has_no_empty_signal(self):
        # An active current-week cycle → has_active True; the empty-state branch
        # (and its any_cycles flag) is never reached.
        self._wipe_cycles()
        cid = _sprints.create_cycle()["id"]     # current week (Mon-snapped)
        _sprints.start_sprint(cid)
        board = _sprints.get_cycle_board()
        self.assertTrue(board["has_active"])
        self.assertIsNotNone(board["cycle"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
