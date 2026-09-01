"""review_queue contract: pinned metric definitions + read-only enforcement.

Golden fixture authored by hand (Tier-0: the contract is never the worker's).
Timeline (now = 1_000_000_000, window = 7d, since = now - 604800):

  tA review   esc @ now-400000 (arrival), esc @ now-100000 (rework), no terminal
              → censored, WIP, waiting 111.11h
  tB done     esc @ now-300000 (arrival), accepted @ now-200000 → wait 27.78h
  tC done     esc @ now-1000000 (pre-window), esc @ now-50000 (rework only),
              accepted @ now-900000 → NOT an arrival
  tD review   no escalation events, created @ now-10000 → WIP, waiting 2.78h
  tE done     esc @ now-250000 (arrival), rejected @ now-240000 → wait 2.78h

Hand-computed goldens:
  wip=2  arrivals=3  arrivals_per_day=round(3/7,3)=0.429
  window escalations=5 → rework_events=5-3=2
  waits=[27.78, 2.78] → median=mean=15.28, completed=2, censored=1
  oldest_open_hours=111.11
  littles_law_predicted_wip=round(0.429*(15.28/24),2)=0.27

The sha256 byte-identical assertion (pattern from test_journey_pulse) is the
read-only ENFORCEMENT for the module — review_queue may never write.
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db as _db  # noqa: E402

_SANDBOX_DB = Path(os.environ["HERMES_KANBAN_DB"])  # conftest guarantees this

NOW = 1_000_000_000


def _sha(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReviewQueueGolden(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="review-queue-")
        self.tmp = Path(self.tmpdir) / "kanban.db"
        shutil.copy(_SANDBOX_DB, self.tmp)
        self._orig = _db.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        conn = sqlite3.connect(str(self.tmp))  # no FK pragma: fixture reset
        for table in ("task_events", "task_ledger", "task_runs", "tasks"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass  # table absent in some DB generations — fine
        tasks = [
            # tD inserted FIRST so unsorted SELECT order != waiting-sorted order
            ("tD", "review no-esc", "review", None, NOW - 10000),
            ("tA", "arrival censored", "review", "worker-agent", NOW - 500000),
            ("tB", "arrival accepted", "done", "worker-agent", NOW - 500000),
            ("tC", "pre-window rework", "done", "worker-agent", NOW - 1200000),
            ("tE", "arrival rejected", "done", "worker-agent", NOW - 500000),
            ("tF", "esc 1s outside window", "done", "worker-agent", NOW - 700000),
            ("tG", "arrival exactly at now", "done", "worker-agent", NOW - 500000),
        ]
        conn.executemany(
            "INSERT INTO tasks (id, title, status, assignee, created_at) "
            "VALUES (?,?,?,?,?)", tasks)
        events = [
            ("tA", "escalated_review", NOW - 400000),
            ("tA", "escalated_review", NOW - 100000),
            ("tB", "escalated_review", NOW - 300000),
            ("tB", "accepted", NOW - 200000),
            ("tC", "escalated_review", NOW - 1000000),
            ("tC", "accepted", NOW - 900000),
            ("tC", "escalated_review", NOW - 50000),
            ("tE", "escalated_review", NOW - 250000),
            ("tE", "rejected", NOW - 240000),
            # window-boundary probes: tF sits 1s OUTSIDE (must never count),
            # tG sits exactly AT now (must count — the window is inclusive)
            ("tF", "escalated_review", NOW - 604801),
            ("tG", "escalated_review", NOW),
            # respawn noise must be invisible to every metric:
            ("tA", "respawn_guarded", NOW - 90000),
            ("tD", "respawn_guarded", NOW - 9000),
        ]
        conn.executemany(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES (?,?,?)",
            events)
        conn.commit()
        conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_golden_metrics_and_read_only(self):
        from dashboard.review_queue import review_queue_summary
        before = _sha(self.tmp)
        out = review_queue_summary(days=7, now=NOW)
        after = _sha(self.tmp)
        self.assertEqual(before, after, "review_queue_summary WROTE to the DB")

        self.assertEqual(out["window_days"], 7)
        self.assertEqual(out["wip"], 2)
        self.assertEqual(out["arrivals"], 4)  # tA tB tE tG — tF is 1s outside
        self.assertEqual(out["arrivals_per_day"], 0.571)
        self.assertEqual(out["rework_events"], 2)  # 6 window escalations - 4 arrivals
        w = out["wait_hours"]
        self.assertEqual(w["median"], 15.28)
        self.assertEqual(w["mean"], 15.28)
        self.assertEqual(w["completed"], 2)
        self.assertEqual(w["censored"], 2)  # tA + tG
        self.assertEqual(w["oldest_open_hours"], 111.11)
        self.assertEqual(out["littles_law_predicted_wip"], 0.36)

        ids = [e["task_id"] for e in out["queue"]]
        self.assertEqual(ids, ["tA", "tD"])  # sorted by waiting, longest first
        self.assertEqual(out["queue"][0]["waiting_hours"], 111.11)
        self.assertEqual(out["queue"][1]["waiting_hours"], 2.78)
        # the explainer must produce a real verdict, not degrade silently
        for entry in out["queue"]:
            self.assertIsNotNone(entry["would_auto_accept_if_passed"],
                                 f"explainer unavailable: {entry}")
            self.assertFalse(entry["would_auto_accept_if_passed"], entry)
            self.assertTrue(entry["blocking_reason"], entry)

    def test_empty_window_degrades_to_nulls(self):
        from dashboard.review_queue import review_queue_summary
        out = review_queue_summary(days=1, now=NOW - 3_000_000)
        self.assertEqual(out["window_days"], 1)  # the clamp must not inflate
        self.assertEqual(out["arrivals"], 0)
        self.assertEqual(out["rework_events"], 0)
        self.assertIsNone(out["wait_hours"]["median"])
        self.assertIsNone(out["littles_law_predicted_wip"])

    def test_wall_clock_default_now_works(self):
        """now=None (the production path) must resolve to the wall clock, not
        crash — golden-free smoke, kills the now-resolution deletion mutant."""
        from dashboard.review_queue import review_queue_summary
        out = review_queue_summary(days=7)
        self.assertIsInstance(out["wip"], int)
        self.assertIsInstance(out["now"], int)
        self.assertGreater(out["now"], NOW)


if __name__ == "__main__":
    unittest.main()
