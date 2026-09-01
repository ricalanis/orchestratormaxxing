"""Regression guard for P0-7 — server-side cycle auto-commit (revised-final-plan §6).

A task entering in_progress should auto-commit to the active cycle, server-side,
so agent work reaches the cycle without a human clicking ＋Cycle. Pins:
  - set_task_status(→in_progress) commits an uncommitted, opted-in task,
  - the per-task opt-out (auto_cycle=0) is respected,
  - an already-committed task is left alone (idempotent, no cycle churn),
  - loop.claim_task (the AGENT path) auto-commits too — the actual hole Fable R5
    flagged (fleet agents can't self-commit),
  - the dual store stays consistent: an open task_sprints ledger row is written.

DB isolation: a COPY of ~/.hermes/kanban.db (which has one active cycle after
P0-2), with both db.KANBAN_DB and sprints.KANBAN_DB redirected at it. Real DB
untouched. Stdlib unittest, pytest-discoverable, read layer only.

Run: python -m unittest tests.test_auto_commit_cycle   # from orchestrator/
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
    from dashboard import db as _db, sprints as _sprints, loop as _loop

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

NOW = int(time.time())


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class AutoCommitCycle(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_autocommit_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_spr = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        _sprints.ensure_cycle_schema()  # ensure the auto_cycle column exists on the copy

        # There must be exactly one active cycle to commit to (P0-2 leaves one).
        self.cycle = _sprints.get_active_sprint()
        self.assertIsNotNone(self.cycle, "no active cycle in the test DB")

    def tearDown(self):
        _db.KANBAN_DB = self._orig_db
        _sprints.KANBAN_DB = self._orig_spr
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _mk_task(self, tid, status="ready", pool=0, sprint_id=None, auto_cycle=1, assignee=None):
        c = sqlite3.connect(str(self.tmp))
        c.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind, "
            "consecutive_failures, goal_mode, pool, sprint_id, auto_cycle, assignee) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"T {tid}", status, NOW, "none", 0, 0, pool, sprint_id, auto_cycle, assignee))
        c.commit(); c.close()

    def _sprint_of(self, tid):
        c = sqlite3.connect(str(self.tmp))
        row = c.execute("SELECT sprint_id FROM tasks WHERE id = ?", (tid,)).fetchone()
        c.close()
        return row[0] if row else None

    def _open_ledger_rows(self, tid):
        c = sqlite3.connect(str(self.tmp))
        rows = c.execute(
            "SELECT sprint_id FROM task_sprints WHERE task_id = ? AND outcome IS NULL",
            (tid,)).fetchall()
        c.close()
        return [r[0] for r in rows]

    # -----------------------------------------------------------------

    def test_in_progress_auto_commits(self):
        self._mk_task("t_ac_1")
        res = _sprints.set_task_status("t_ac_1", "in_progress")
        self.assertEqual(self._sprint_of("t_ac_1"), self.cycle["id"])
        self.assertEqual(res.get("auto_committed_cycle"), self.cycle["id"])
        # Dual store: an OPEN ledger row on the active cycle (healthz invariant).
        self.assertIn(self.cycle["id"], self._open_ledger_rows("t_ac_1"))

    def test_opt_out_is_respected(self):
        self._mk_task("t_ac_2", auto_cycle=0)
        res = _sprints.set_task_status("t_ac_2", "in_progress")
        self.assertIsNone(self._sprint_of("t_ac_2"))
        self.assertNotIn("auto_committed_cycle", res)

    def test_opt_out_via_setter(self):
        self._mk_task("t_ac_2b")
        _sprints.set_auto_cycle("t_ac_2b", False)
        _sprints.set_task_status("t_ac_2b", "in_progress")
        self.assertIsNone(self._sprint_of("t_ac_2b"))

    def test_already_committed_is_untouched(self):
        # Pre-committed to SOME sprint (use the old closed one) → not moved.
        self._mk_task("t_ac_3", sprint_id="spr_083acc33")
        _sprints.set_task_status("t_ac_3", "in_progress")
        self.assertEqual(self._sprint_of("t_ac_3"), "spr_083acc33")

    def test_non_in_progress_does_not_commit(self):
        self._mk_task("t_ac_4")
        _sprints.set_task_status("t_ac_4", "blocked")
        self.assertIsNone(self._sprint_of("t_ac_4"))

    def test_agent_claim_auto_commits(self):
        # The Fable-R5 hole: an agent pulls a pool task → server commits it.
        self._mk_task("t_ac_5", status="ready", pool=1)
        res = _loop.claim_task("t_ac_5", "kimi-coder")
        self.assertEqual(res.get("status"), "claimed")
        self.assertEqual(self._sprint_of("t_ac_5"), self.cycle["id"])
        self.assertEqual(res.get("auto_committed_cycle"), self.cycle["id"])


if __name__ == "__main__":
    unittest.main()
