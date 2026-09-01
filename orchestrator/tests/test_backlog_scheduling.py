"""Backlog + Scheduling (Phase 1) — API contract regression guard.

Pins two endpoints added in Phase 1:
  - PATCH /api/tasks/{id} {scheduled_week}  → sets tasks.scheduled_week, and with
    {assign_active_cycle:true} also commits the task to the active cycle.
  - GET  /api/tasks?backlog=true            → only tasks with NO sprint_id AND NO
    scheduled_week (the truly-unscheduled Backlog set).

Isolation: dashboard.api runs ensure_schema() at import, so the DB layers are
pointed at a COPY of ~/.hermes/kanban.db BEFORE the import — the real DB is never
touched. If there's no kanban.db to copy (fresh box / CI), the whole case skips.

Stdlib unittest (pytest-discoverable). Run:
    python -m unittest tests.test_backlog_scheduling
    python -m pytest tests/test_backlog_scheduling.py
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
try:
    from dashboard import db as _db, sprints as _sprints  # safe: no import side effects

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_backlog_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB
        _sprints.KANBAN_DB = _TMP_DB

        from dashboard.api import app  # ensure_schema() runs here, on the copy
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover - environment without deps/DB
    _READY = False


@atexit.register
def _cleanup_tmp_db():  # pragma: no cover
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


def _a_backlog_task_id():
    """An id from the current backlog set (no sprint, no scheduled_week)."""
    j = _CLIENT.get("/api/tasks?backlog=true").json()
    return j["tasks"][0]["id"] if j["tasks"] else None


class _RepointDB(unittest.TestCase):
    """Re-assert BOTH KANBAN_DB globals at each test start.

    db.KANBAN_DB / sprints.KANBAN_DB are shared module globals. Sibling test
    modules clobber them at collection/setUp time — notably test_crm_growth
    repoints db.KANBAN_DB (only) at *import*, which runs during pytest
    collection and leaves the write path (sprints.set_scheduled_week) and the
    read path (db.get_task) pointed at different temp DBs. Re-pointing both to
    our copy here makes this module order-independent (the same good-citizen
    pattern test_cycle_endpoints_errors / test_auto_commit_cycle use)."""

    def setUp(self):
        # Save the globals as collection/sibling modules left them, force BOTH
        # to our copy for the duration of the test, then restore in tearDown so
        # we don't leak our DB into later modules (good-citizen pattern).
        self._orig_db, self._orig_spr = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _sprints.KANBAN_DB = _TMP_DB

    def tearDown(self):
        _db.KANBAN_DB = self._orig_db
        _sprints.KANBAN_DB = self._orig_spr


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class SchemaMigration(_RepointDB):
    def test_scheduled_week_column_exists(self):
        # canvas.ensure_schema() (and the standalone migration) add the column.
        conn = _sprints.get_conn()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        finally:
            conn.close()
        self.assertIn("scheduled_week", cols)
        self.assertIn("due_date", cols)

    def test_migration_is_idempotent(self):
        from dashboard.migrations import phase1_backlog_scheduling as m
        r1 = m.run()
        r2 = m.run()
        self.assertEqual(r1["status"], "ok")
        self.assertTrue(r2["scheduled_week_present"])
        # Second run adds nothing (column already present).
        self.assertEqual(r2["added"], [])


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class PatchScheduledWeek(_RepointDB):
    def test_patch_sets_scheduled_week(self):
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid, "need at least one backlog task in the fixture DB")
        r = _CLIENT.patch(f"/api/tasks/{tid}", json={"scheduled_week": "2026-W40"})
        self.assertEqual(r.status_code, 200)
        got = _CLIENT.get(f"/api/tasks/{tid}").json()["task"]
        self.assertEqual(got["scheduled_week"], "2026-W40")

    def test_patch_unknown_task_is_404(self):
        r = _CLIENT.patch("/api/tasks/t_does_not_exist", json={"scheduled_week": "2026-W40"})
        self.assertEqual(r.status_code, 404)

    def test_clear_scheduled_week_returns_to_backlog(self):
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid)
        _CLIENT.patch(f"/api/tasks/{tid}", json={"scheduled_week": "2026-W41"})
        # Now clear it — the task should be schedulable back into the backlog.
        r = _CLIENT.patch(f"/api/tasks/{tid}", json={"clear_scheduled_week": True})
        self.assertEqual(r.status_code, 200)
        got = _CLIENT.get(f"/api/tasks/{tid}").json()["task"]
        self.assertIsNone(got["scheduled_week"])

    def test_null_scheduled_week_is_a_no_op(self):
        """The contract the client's clear-path depends on: {scheduled_week: null}
        is indistinguishable from "field absent" (the `is not None` guard), so it
        leaves the week UNCHANGED. Pinned so the server can't quietly start
        honouring it while the client sends clear_scheduled_week."""
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid)
        _CLIENT.patch(f"/api/tasks/{tid}", json={"scheduled_week": "2026-W43"})
        r = _CLIENT.patch(f"/api/tasks/{tid}", json={"scheduled_week": None})
        self.assertEqual(r.status_code, 200)
        got = _CLIENT.get(f"/api/tasks/{tid}").json()["task"]
        self.assertEqual(got["scheduled_week"], "2026-W43")
        # …and the explicit flag is what actually clears it.
        _CLIENT.patch(f"/api/tasks/{tid}", json={"clear_scheduled_week": True})
        self.assertIsNone(_CLIENT.get(f"/api/tasks/{tid}").json()["task"]["scheduled_week"])

    def test_sprint_null_uncommits_the_task(self):
        """Leaving This Week un-commits a cycle-pinned card via the /sprint
        endpoint — without it the board's cycle branch pins the card and it
        bounces straight back into This Week."""
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid)
        active = _sprints.get_active_sprint()
        if not active:
            self.skipTest("no active cycle in fixture DB")
        _CLIENT.patch(f"/api/tasks/{tid}",
                      json={"scheduled_week": "2026-W29", "assign_active_cycle": True})
        self.assertEqual(_CLIENT.get(f"/api/tasks/{tid}").json()["task"]["sprint_id"], active["id"])
        r = _CLIENT.patch(f"/api/tasks/{tid}/sprint", json={"sprint_id": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(_CLIENT.get(f"/api/tasks/{tid}").json()["task"]["sprint_id"])

    def test_clear_week_does_not_uncommit_but_a_different_week_does(self):
        """The asymmetry the client's cycle handling is built on.

        set_scheduled_week sprint-syncs by itself — setting a week that differs
        from the active cycle's pulls the task out of its sprint. But that branch
        is guarded on a TRUTHY week, so clearing (the Unscheduled destination,
        which must send clear_scheduled_week) leaves the commitment standing.
        Hence the board's mover has to send PATCH /sprint {sprint_id:null}
        itself on that one path: without it the card is still cycle-pinned and
        backlogColumnFor bounces it back into This Week. Pinned so a server-side
        "cleanup" of either half silently breaks the board."""
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid)
        active = _sprints.get_active_sprint()
        if not active:
            self.skipTest("no active cycle in fixture DB")
        active_week = _sprints._sprint_to_iso_week(active)
        self.assertTrue(active_week, "active cycle must resolve to an ISO week")
        other_week = "2026-W29" if active_week != "2026-W29" else "2026-W30"

        # Clearing the week keeps the commitment — the client must un-commit.
        _CLIENT.patch(f"/api/tasks/{tid}",
                      json={"scheduled_week": active_week, "assign_active_cycle": True})
        self.assertEqual(_CLIENT.get(f"/api/tasks/{tid}").json()["task"]["sprint_id"], active["id"])
        _CLIENT.patch(f"/api/tasks/{tid}", json={"clear_scheduled_week": True})
        got = _CLIENT.get(f"/api/tasks/{tid}").json()["task"]
        self.assertIsNone(got["scheduled_week"])
        self.assertEqual(got["sprint_id"], active["id"],
                         "clear_scheduled_week must NOT sprint-sync — the client's "
                         "/sprint PATCH is the only thing that un-commits here")

        # A different week un-commits on its own.
        _CLIENT.patch(f"/api/tasks/{tid}", json={"scheduled_week": other_week})
        self.assertIsNone(_CLIENT.get(f"/api/tasks/{tid}").json()["task"]["sprint_id"])

    def test_this_week_assigns_active_cycle(self):
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid)
        active = _sprints.get_active_sprint()
        if not active:
            self.skipTest("no active cycle in fixture DB")
        # The week must be the ACTIVE cycle's own — this is the "move to This
        # Week" path. A hardcoded literal was a time bomb: once the active cycle
        # moved past it, assign_task_sprint's scheduled-week sync (a task
        # committed to a cycle can't also sit in a different week's bucket)
        # cleared the field and the assertion below went red on the calendar.
        week = _sprints._sprint_to_iso_week(active)
        self.assertTrue(week, "active cycle must resolve to an ISO week")
        r = _CLIENT.patch(f"/api/tasks/{tid}",
                          json={"scheduled_week": week, "assign_active_cycle": True})
        self.assertEqual(r.status_code, 200)
        got = _CLIENT.get(f"/api/tasks/{tid}").json()["task"]
        self.assertEqual(got["scheduled_week"], week)
        self.assertEqual(got["sprint_id"], active["id"])


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class BacklogFilter(_RepointDB):
    def test_backlog_only_unscheduled(self):
        j = _CLIENT.get("/api/tasks?backlog=true").json()
        for t in j["tasks"]:
            self.assertIsNone(t["sprint_id"], f"{t['id']} has a sprint")
            self.assertFalse(t.get("scheduled_week"), f"{t['id']} has a scheduled_week")

    def test_scheduling_removes_from_backlog(self):
        tid = _a_backlog_task_id()
        self.assertIsNotNone(tid)
        before = {t["id"] for t in _CLIENT.get("/api/tasks?backlog=true").json()["tasks"]}
        self.assertIn(tid, before)
        _CLIENT.patch(f"/api/tasks/{tid}", json={"scheduled_week": "2026-W42"})
        after = {t["id"] for t in _CLIENT.get("/api/tasks?backlog=true").json()["tasks"]}
        self.assertNotIn(tid, after)
        self.assertEqual(len(after), len(before) - 1)

    def test_backlog_false_is_full_firehose(self):
        full = _CLIENT.get("/api/tasks").json()["total"]
        backlog = _CLIENT.get("/api/tasks?backlog=true").json()["total"]
        # The backlog is a strict subset of all tasks.
        self.assertLessEqual(backlog, full)


if __name__ == "__main__":
    unittest.main(verbosity=2)
