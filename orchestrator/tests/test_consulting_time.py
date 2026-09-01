"""Consultant time ledger contract.

The real Hermes DB is copied before the app import. Tests create only namespaced
projects/tasks/entries in that copy and restore the shared DB pointer afterward.
"""
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_IMPORT_ERROR = None
_TMP_DB = None
_db = _ct = _client = None
try:
    from dashboard import db as _db

    real = Path.home() / ".hermes" / "kanban.db"
    if real.exists():
        fd, tmp = tempfile.mkstemp(prefix="kanban_test_consulting_", suffix=".db")
        os.close(fd)
        shutil.copy(real, tmp)
        _TMP_DB = Path(tmp)
        original = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard import consulting_time as _ct
            _ct.ensure_schema()
            from dashboard.api import app
            from starlette.testclient import TestClient
            _client = TestClient(app, raise_server_exceptions=False)
            _READY = True
        finally:
            _db.KANBAN_DB = original
except Exception as exc:
    _IMPORT_ERROR = exc
    _READY = False


@atexit.register
def _cleanup():  # pragma: no cover
    if _TMP_DB and _TMP_DB.exists():
        _TMP_DB.unlink()


class ContractAvailable(unittest.TestCase):
    def test_feature_imports_against_copied_database(self):
        self.assertTrue(_READY, f"consulting-time feature unavailable: {_IMPORT_ERROR!r}")


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class ConsultingTimeBase(unittest.TestCase):
    P1 = "proj_test_consulting_a"
    P2 = "proj_test_consulting_b"
    T1 = "t_test_consulting_a"
    T2 = "t_test_consulting_b"

    def setUp(self):
        self.saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _ct.ensure_schema()
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM consulting_time_entries")
            conn.execute("DELETE FROM tasks WHERE id IN (?, ?)", (self.T1, self.T2))
            conn.execute("DELETE FROM projects WHERE id IN (?, ?)", (self.P1, self.P2))
            conn.executemany(
                "INSERT INTO projects (id, slug, name, description, color, icon, created_at, kind) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(self.P1, "test-consulting-a", "Test Consulting A", "", "#2563eb", "A", 1, "product"),
                 (self.P2, "test-consulting-b", "Test Consulting B", "", "#059669", "B", 1, "product")],
            )
            conn.executemany(
                "INSERT INTO tasks (id, title, status, created_at, workspace_kind, "
                "consecutive_failures, goal_mode, block_recurrences, project_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(self.T1, "Task A", "ready", 1, "scratch", 0, 0, 0, self.P1),
                 (self.T2, "Task B", "ready", 1, "scratch", 0, 0, 0, self.P2)],
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self.saved

    def assert_domain_error(self, status, fn, *args, **kwargs):
        with self.assertRaises(_ct.ConsultingTimeError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.status_code, status)


class SchemaAndManual(ConsultingTimeBase):
    def test_schema_is_idempotent_and_global_timer_index_exists(self):
        _ct.ensure_schema()
        _ct.ensure_schema()
        conn = _db.get_conn()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_consulting_time_active_timer'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("WHERE source = 'timer' AND ended_at IS NULL", sql)

    def test_manual_entry_and_validation(self):
        row = _ct.create_manual(self.P1, "2026-07-21", 90, "Discovery workshop", self.T1, True)
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["duration_seconds"], 5400)
        self.assertEqual(row["project_id"], self.P1)
        self.assertEqual(row["task_id"], self.T1)
        self.assertEqual(row["billable"], 1)
        self.assertIsNone(row["started_at"])

        self.assert_domain_error(404, _ct.create_manual, "proj_missing", "2026-07-21", 1, "x")
        self.assert_domain_error(404, _ct.create_manual, self.P1, "2026-07-21", 1, "x", "t_missing")
        self.assert_domain_error(400, _ct.create_manual, self.P1, "2026-07-21", 1, "x", self.T2)
        for minutes in (0, 1441, 1.5, "60"):
            self.assert_domain_error(400, _ct.create_manual, self.P1, "2026-07-21", minutes, "x")
        self.assert_domain_error(400, _ct.create_manual, self.P1, "bad-date", 60, "x")
        self.assert_domain_error(400, _ct.create_manual, self.P1, "2099-01-01", 60, "x")
        self.assert_domain_error(400, _ct.create_manual, self.P1, "2026-07-21", 60, "   ")


class TimerAndSummary(ConsultingTimeBase):
    def test_timer_is_global_persistent_and_server_timed(self):
        self.assert_domain_error(404, _ct.start_timer, self.P1, "Missing task", "t_missing", True,
                                 now=1_721_546_399)
        self.assert_domain_error(400, _ct.start_timer, self.P1, "Wrong project task", self.T2, True,
                                 now=1_721_546_399)
        started = _ct.start_timer(self.P1, "Client delivery", self.T1, True, now=1_721_546_400)
        recovered = _ct.get_active_timer()
        self.assertEqual(recovered["id"], started["id"])
        self.assert_domain_error(409, _ct.start_timer, self.P2, "Overlap", self.T2, True,
                                 now=1_721_546_401)
        stopped = _ct.stop_timer(started["id"], now=1_721_546_520)
        self.assertEqual(stopped["duration_seconds"], 120)
        self.assertIsNone(_ct.get_active_timer())
        self.assert_domain_error(409, _ct.stop_timer, started["id"], now=1_721_546_600)
        self.assert_domain_error(404, _ct.stop_timer, "cte_missing", now=1_721_546_600)

    def test_summary_and_delete_are_project_scoped(self):
        _ct.create_manual(self.P1, "2026-06-21", 60, "Today")
        _ct.create_manual(self.P1, "2026-06-20", 30, "This week")
        _ct.create_manual(self.P1, "2026-06-01", 120, "This month")
        _ct.create_manual(self.P1, "2026-06-30", 15, "Month end")
        other = _ct.create_manual(self.P2, "2026-06-21", 999, "Other project")
        ledger = _ct.get_project_ledger(self.P1, today="2026-06-21")
        self.assertEqual(ledger["summary"], {
            "today_seconds": 3600,
            "week_seconds": 5400,
            "month_seconds": 13500,
        })
        self.assertEqual(len(ledger["entries"]), 4)
        self.assertEqual(_ct.delete_entry(other["id"])["id"], other["id"])
        self.assert_domain_error(404, _ct.delete_entry, other["id"])

    def test_december_summary_uses_calendar_year_end(self):
        _ct.create_manual(self.P1, "2025-12-01", 10, "December")
        _ct.create_manual(self.P1, "2025-12-31", 20, "Year end")
        ledger = _ct.get_project_ledger(self.P1, today="2025-12-15")
        self.assertEqual(ledger["summary"]["month_seconds"], 1800)


class Endpoints(ConsultingTimeBase):
    def test_manual_list_and_validation_status_codes(self):
        created = _client.post("/api/consulting-time", json={
            "project_id": self.P1, "work_date": "2026-07-21", "minutes": 45,
            "description": "API entry", "task_id": self.T1, "billable": False,
        })
        self.assertEqual(created.status_code, 200)
        self.assertFalse(bool(created.json()["billable"]))
        listed = _client.get("/api/consulting-time", params={
            "project_id": self.P1, "today": "2026-07-21"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["summary"]["today_seconds"], 2700)
        self.assertEqual(_client.get("/api/consulting-time").status_code, 422)
        self.assertEqual(_client.post("/api/consulting-time", json={
            "project_id": self.P1, "work_date": "2026-07-21", "minutes": 45,
            "description": "wrong task", "task_id": self.T2}).status_code, 400)
        self.assertEqual(_client.post("/api/consulting-time", json={
            "project_id": "proj_missing", "work_date": "2026-07-21", "minutes": 45,
            "description": "missing project"}).status_code, 404)

    def test_timer_conflict_stop_recovery_and_delete_endpoints(self):
        start = _client.post("/api/consulting-time/timer/start", json={
            "project_id": self.P1, "description": "API timer", "task_id": self.T1})
        self.assertEqual(start.status_code, 200)
        entry_id = start.json()["id"]
        active = _client.get("/api/consulting-time/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()["active"]["id"], entry_id)
        conflict = _client.post("/api/consulting-time/timer/start", json={"project_id": self.P2})
        self.assertEqual(conflict.status_code, 409)
        stopped = _client.post(f"/api/consulting-time/{entry_id}/stop")
        self.assertEqual(stopped.status_code, 200)
        self.assertGreaterEqual(stopped.json()["duration_seconds"], 1)
        self.assertEqual(_client.post(f"/api/consulting-time/{entry_id}/stop").status_code, 409)
        self.assertEqual(_client.delete(f"/api/consulting-time/{entry_id}").status_code, 200)
        self.assertEqual(_client.delete(f"/api/consulting-time/{entry_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
