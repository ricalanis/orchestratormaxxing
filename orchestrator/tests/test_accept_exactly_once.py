"""accept_task must be exactly-once under concurrent acceptance.

The race (observed live: t_c7ab4210 carries TWO `accepted` events): accept_task
does SELECT → unconditional UPDATE (COALESCE masks the second write) →
unconditional `accepted` event log. Two concurrent callers that both read
`reviewed_at IS NULL` both update, both log, and — for an agent task — both can
append the manual verification-ledger fallback row.

The exactly-once contract asserted here (all four properties):
  1. exactly one caller returns result="accepted";
  2. every loser returns result="already_reviewed" with the stored timestamp;
  3. exactly one `accepted` event exists in task_events;
  4. at most one role='verification' ledger row exists.

DETERMINISM — this test never relies on threads happening to interleave. It
patches sprints.get_conn with a wrapper whose execute() parks at a
threading.Barrier immediately after accept_task's initial SELECT, so both
threads are provably past the read before either writes (the exact interleave
the race needs; a Sol-critique fix over the earlier "launch two threads and
hope" design). Proven red against the pre-fix sprints.py (2026-08-09): both
threads returned "accepted" and task_events held 2 rows.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db as _db          # noqa: E402
from dashboard import sprints as _sprints  # noqa: E402

_SANDBOX_DB = Path(os.environ["HERMES_KANBAN_DB"])  # conftest guarantees this


class _BarrierConn:
    """Proxy around a real sqlite3 connection: parks at the barrier right after
    accept_task's initial SELECT so every concurrent caller is past the read
    before any caller writes."""

    def __init__(self, real, barrier):
        self._real = real
        self._barrier = barrier
        self._tripped = False

    def execute(self, sql, *args):
        cur = self._real.execute(sql, *args)
        if not self._tripped and sql.lstrip().upper().startswith(
                "SELECT STATUS, REVIEWED_AT"):
            self._tripped = True
            self._barrier.wait(timeout=10)
        return cur

    def __getattr__(self, name):
        return getattr(self._real, name)


class AcceptExactlyOnce(unittest.TestCase):
    TASK_ID = "t_hv_accept_once"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="accept-once-")
        self.tmp = Path(self.tmpdir) / "kanban.db"
        shutil.copy(_SANDBOX_DB, self.tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        conn = sqlite3.connect(str(self.tmp))
        conn.execute("DELETE FROM tasks WHERE id = ?", (self.TASK_ID,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (self.TASK_ID,))
        conn.execute("DELETE FROM task_ledger WHERE task_id = ?", (self.TASK_ID,))
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at) "
            "VALUES (?, 'exactly-once fixture', 'review', 'worker-agent', 0)",
            (self.TASK_ID,))
        conn.commit()
        conn.close()

    def tearDown(self):
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_accepts_are_exactly_once(self):
        barrier = threading.Barrier(2)
        real_get_conn = _sprints.get_conn
        _sprints.get_conn = lambda: _BarrierConn(real_get_conn(), barrier)
        results = [None, None]
        try:
            def call(i):
                results[i] = _sprints.accept_task(self.TASK_ID)

            threads = [threading.Thread(target=call, args=(i,)) for i in (0, 1)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            _sprints.get_conn = real_get_conn

        self.assertTrue(all(r is not None for r in results),
                        f"a caller never returned: {results}")
        outcomes = sorted(r["result"] for r in results)
        # property 1 + 2: one winner, every loser gets already_reviewed
        self.assertEqual(outcomes, ["accepted", "already_reviewed"],
                         f"expected exactly one winner, got {results}")
        # property 2 (timestamp): the loser reports the stored (winner's) stamp
        stamps = {r["reviewed_at"] for r in results}
        self.assertEqual(len(stamps), 1,
                         f"loser did not return the stored reviewed_at: {results}")
        conn = sqlite3.connect(str(self.tmp))
        try:
            accepted_events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'accepted'",
                (self.TASK_ID,)).fetchone()[0]
            ledger_rows = conn.execute(
                "SELECT COUNT(*) FROM task_ledger WHERE task_id = ? AND role = 'verification'",
                (self.TASK_ID,)).fetchone()[0]
            stored = conn.execute(
                "SELECT reviewed_at, status FROM tasks WHERE id = ?",
                (self.TASK_ID,)).fetchone()
        finally:
            conn.close()
        # property 3: exactly one accepted event
        self.assertEqual(accepted_events, 1,
                         f"expected exactly 1 accepted event, found {accepted_events}")
        # property 4: at most one manual verification-ledger row
        self.assertLessEqual(ledger_rows, 1,
                             f"expected <=1 verification ledger row, found {ledger_rows}")
        self.assertEqual(stored[1], "done")
        self.assertEqual(stored[0], stamps.pop())

    def test_sequential_second_accept_is_noop(self):
        """Sequential idempotence (the documented behavior) must survive the fix."""
        first = _sprints.accept_task(self.TASK_ID)
        second = _sprints.accept_task(self.TASK_ID)
        self.assertEqual(first["result"], "accepted")
        self.assertEqual(second["result"], "already_reviewed")
        self.assertEqual(first["reviewed_at"], second["reviewed_at"])
        conn = sqlite3.connect(str(self.tmp))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'accepted'",
                (self.TASK_ID,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_reaccept_after_reject_still_lands(self):
        """A reviewed-then-rejected task must remain re-acceptable (the WHERE
        clause guards the concurrent same-state race, not legitimate
        state transitions)."""
        _sprints.accept_task(self.TASK_ID)
        _sprints.reject_task(self.TASK_ID, "changed my mind")
        again = _sprints.accept_task(self.TASK_ID)
        self.assertEqual(again["result"], "accepted")
        conn = sqlite3.connect(str(self.tmp))
        try:
            status = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (self.TASK_ID,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "done")


if __name__ == "__main__":
    unittest.main()
