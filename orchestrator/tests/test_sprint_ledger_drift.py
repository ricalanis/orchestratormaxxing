"""Regression guard for P2-1 — the sprint_id ⇔ task_sprints consistency check
(revised-final-plan §2.3).

sprints.sprint_ledger_drift() must catch BOTH directions of dual-store drift:
  forward  — a task with sprint_id set but no matching OPEN ledger row,
  reverse  — an OPEN ledger row whose task no longer points at that sprint,
and a clean store reports drift=0 / ok=True. Also pins the healthz wiring: the
check is surfaced in /healthz `checks` but NEVER gates (drift keeps a 200, since
a drifted ledger is a data-integrity signal, not a dependency outage).

DB isolation: a COPY of ~/.hermes/kanban.db, mutated to induce (or clear) drift.
Real DB untouched. Stdlib unittest, pytest-discoverable.

Run: python -m unittest tests.test_sprint_ledger_drift   # from orchestrator/
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
    from dashboard import db as _db, sprints as _sprints

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

NOW = int(time.time())


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class SprintLedgerDrift(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_drift_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_spr = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        # A clean slate: no cycle pointers, no ledger rows.
        c = sqlite3.connect(str(self.tmp))
        c.execute("UPDATE tasks SET sprint_id = NULL")
        c.execute("DELETE FROM task_sprints")
        # One valid, consistent commit (a task + its matching OPEN ledger row).
        tid = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
        sid = c.execute("SELECT id FROM sprints LIMIT 1").fetchone()
        self.sid = sid[0] if sid else "cyc_x"
        self.good_task = tid
        c.execute("UPDATE tasks SET sprint_id = ? WHERE id = ?", (self.sid, tid))
        c.execute("INSERT INTO task_sprints (task_id, sprint_id, committed_at, outcome) "
                  "VALUES (?,?,?,NULL)", (tid, self.sid, NOW))
        c.commit(); c.close()

    def tearDown(self):
        _db.KANBAN_DB = self._orig_db
        _sprints.KANBAN_DB = self._orig_spr
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _a_task(self, exclude):
        c = sqlite3.connect(str(self.tmp))
        r = c.execute("SELECT id FROM tasks WHERE id != ? LIMIT 1", (exclude,)).fetchone()
        c.close()
        return r[0]

    def test_clean_store_has_no_drift(self):
        d = _sprints.sprint_ledger_drift()
        self.assertTrue(d["ok"])
        self.assertEqual(d["drift"], 0)

    def test_forward_drift_detected(self):
        # A task points at a sprint but has NO open ledger row.
        tid = self._a_task(self.good_task)
        c = sqlite3.connect(str(self.tmp))
        c.execute("UPDATE tasks SET sprint_id = ? WHERE id = ?", (self.sid, tid))
        c.commit(); c.close()
        d = _sprints.sprint_ledger_drift()
        self.assertFalse(d["ok"])
        self.assertEqual(d["forward_orphans"], 1)
        self.assertEqual(d["reverse_orphans"], 0)
        self.assertIn(tid, d["sample_forward"])

    def test_reverse_drift_detected(self):
        # An OPEN ledger row whose task points elsewhere (sprint_id NULL here).
        tid = self._a_task(self.good_task)
        c = sqlite3.connect(str(self.tmp))
        c.execute("INSERT INTO task_sprints (task_id, sprint_id, committed_at, outcome) "
                  "VALUES (?,?,?,NULL)", (tid, self.sid, NOW))   # task.sprint_id is NULL
        c.commit(); c.close()
        d = _sprints.sprint_ledger_drift()
        self.assertFalse(d["ok"])
        self.assertEqual(d["reverse_orphans"], 1)
        self.assertEqual(d["forward_orphans"], 0)

    def test_healthz_surfaces_drift_without_gating(self):
        # Induce drift, then hit /healthz: the check is reported (ok=false) but the
        # endpoint still returns 200 — drift never gates.
        from fastapi.testclient import TestClient
        from dashboard.api import app
        tid = self._a_task(self.good_task)
        c = sqlite3.connect(str(self.tmp))
        c.execute("UPDATE tasks SET sprint_id = ? WHERE id = ?", (self.sid, tid))
        c.commit(); c.close()
        r = TestClient(app).get("/healthz")
        self.assertEqual(r.status_code, 200)                 # NOT 503
        body = r.json()
        self.assertIn("sprint_ledger", body["checks"])
        self.assertFalse(body["checks"]["sprint_ledger"]["ok"])
        self.assertNotIn("sprint_ledger", body["degraded"])  # informational only

    def test_reconcile_forward_orphan_ready_task_gets_open_commit(self):
        # A ready (not-done) forward orphan should get an OPEN ledger row
        # (outcome=NULL), not "delivered" — the task hasn't been delivered yet.
        tid = self._a_task(self.good_task)
        c = sqlite3.connect(str(self.tmp))
        # Set sprint_id AND ensure completed_at is NULL (ready task)
        c.execute("UPDATE tasks SET sprint_id = ?, completed_at = NULL WHERE id = ?",
                   (self.sid, tid))
        c.commit(); c.close()
        d = _sprints.sprint_ledger_drift()
        self.assertEqual(d["forward_orphans"], 1)
        r = _sprints.reconcile_sprint_ledger()
        self.assertGreaterEqual(r["forward_repaired"], 1)
        c = sqlite3.connect(str(self.tmp))
        row = c.execute(
            "SELECT outcome FROM task_sprints WHERE task_id = ? AND sprint_id = ?",
            (tid, self.sid)).fetchone()
        c.close()
        self.assertIsNotNone(row, "ledger row was not inserted")
        self.assertIsNone(row[0], f"outcome should be NULL for ready task, got {row[0]}")

    def test_reconcile_forward_orphan_done_task_gets_delivered(self):
        # A done forward orphan (completed_at set) should get "delivered".
        tid = self._a_task(self.good_task)
        c = sqlite3.connect(str(self.tmp))
        c.execute("UPDATE tasks SET sprint_id = ?, completed_at = ? WHERE id = ?",
                   (self.sid, NOW, tid))
        c.commit(); c.close()
        r = _sprints.reconcile_sprint_ledger()
        self.assertGreaterEqual(r["forward_repaired"], 1)
        c = sqlite3.connect(str(self.tmp))
        row = c.execute(
            "SELECT outcome FROM task_sprints WHERE task_id = ? AND sprint_id = ?",
            (tid, self.sid)).fetchone()
        c.close()
        self.assertEqual(row[0], "delivered")


if __name__ == "__main__":
    unittest.main()
