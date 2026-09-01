"""Regression guard for the cycle-lifecycle HTTP error contract (dashboard/api.py).

The create/load/delete endpoints used to return either 200-with-an-error-body or
an opaque 500 on bad input; they were fixed to emit explicit 4xx codes a client
can branch on. That contract is easy to silently break (drop an `_or_http`, a
validation line, a `get_sprint` existence check) while every happy-path check
still passes. These tests pin the status codes.

Isolation: dashboard.api runs ensure_schema() at import, so the DB layers are
pointed at a COPY of ~/.hermes/kanban.db BEFORE the import — the real DB is never
touched. If there's no kanban.db to copy (fresh box / CI), the whole case skips.

Stdlib unittest (no pytest dependency); pytest-discoverable. Run:
    python -m unittest tests.test_cycle_endpoints_errors     # from orchestrator/
    python -m pytest tests/test_cycle_endpoints_errors.py    # if pytest installed
"""
import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- Point every DB layer at a throwaway copy, THEN import the app. ------------
_READY = False
_CLIENT = None
_TMP_DB = None
try:
    from dashboard import db as _db, sprints as _sprints  # safe: no import side effects

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_errs_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        # get_conn() reads these module globals at call time, so reassigning them
        # here — before importing api (which calls ensure_schema on import) —
        # redirects every write to the copy.
        _db.KANBAN_DB = _TMP_DB
        _sprints.KANBAN_DB = _TMP_DB

        from dashboard.api import app
        from starlette.testclient import TestClient

        # No `with` context manager → app lifespan (the SSH sweeper) never starts.
        # raise_server_exceptions=False so an unhandled 500 shows AS 500, not raise.
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


def _future(days):
    """A unix timestamp `days` out — used to create fixture cycles in distinct
    ISO weeks (create_cycle dedups by week, so fixtures must not collide)."""
    return int(time.time()) + days * 86400


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class CycleCreateErrors(unittest.TestCase):
    def test_non_numeric_start_date_is_400(self):
        # Would otherwise 500 in _week_window(date.fromtimestamp("abc")).
        self.assertEqual(_CLIENT.post("/api/sprints", json={"start_date": "abc"}).status_code, 400)

    def test_bool_start_date_is_400(self):
        self.assertEqual(_CLIENT.post("/api/sprints", json={"start_date": True}).status_code, 400)

    def test_unknown_project_is_404(self):
        # Would otherwise 500 on the sprints FK insert.
        r = _CLIENT.post("/api/sprints", json={"project_id": "proj_does_not_exist", "name": "X"})
        self.assertEqual(r.status_code, 404)

    def test_valid_cycle_is_200(self):
        self.assertEqual(_CLIENT.post("/api/sprints", json={"start_date": _future(300)}).status_code, 200)

    def test_end_before_start_is_400(self):
        # Backend guard behind the New-cycle form's inline end≥start validation.
        r = _CLIENT.post("/api/sprints", json={"start_date": _future(400), "end_date": _future(390)})
        self.assertEqual(r.status_code, 400)

    def test_valid_range_is_200(self):
        r = _CLIENT.post("/api/sprints", json={"start_date": _future(410), "end_date": _future(417)})
        self.assertEqual(r.status_code, 200)


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class CycleLoadErrors(unittest.TestCase):
    def test_board_explicit_missing_sprint_is_404(self):
        r = _CLIENT.get("/api/cycle/active/board", params={"sprint_id": "cyc_missing"})
        self.assertEqual(r.status_code, 404)

    def test_board_no_id_is_200(self):
        # No id = "the active cycle (or none)" — a legitimate empty board, not 404.
        self.assertEqual(_CLIENT.get("/api/cycle/active/board").status_code, 200)

    def test_calendar_negative_weeks_is_400(self):
        self.assertEqual(_CLIENT.get("/api/cycles/calendar", params={"weeks_back": -1}).status_code, 400)

    def test_calendar_window_too_large_is_400(self):
        r = _CLIENT.get("/api/cycles/calendar", params={"weeks_back": 60, "weeks_fwd": 60})
        self.assertEqual(r.status_code, 400)

    def test_calendar_normal_is_200(self):
        self.assertEqual(_CLIENT.get("/api/cycles/calendar").status_code, 200)

    def test_sprint_tasks_missing_cycle_is_404(self):
        self.assertEqual(_CLIENT.get("/api/sprints/cyc_missing/tasks").status_code, 404)


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class CycleDeleteErrors(unittest.TestCase):
    def test_delete_missing_is_404(self):
        self.assertEqual(_CLIENT.delete("/api/sprints/cyc_missing").status_code, 404)

    def test_delete_completed_is_409(self):
        # A completed cycle is history — the delete is refused with a CONFLICT.
        cid = _sprints.create_cycle(start_date=_future(310))["id"]
        _sprints.start_sprint(cid)
        _sprints.close_sprint(cid, auto_create=False)
        self.assertEqual(_CLIENT.delete(f"/api/sprints/{cid}").status_code, 409)

    def test_delete_planning_is_200(self):
        cid = _sprints.create_cycle(start_date=_future(320))["id"]
        self.assertEqual(_CLIENT.delete(f"/api/sprints/{cid}").status_code, 200)


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class CycleBulkCommitErrors(unittest.TestCase):
    def test_commit_to_missing_cycle_is_404(self):
        r = _CLIENT.post("/api/cycles/cyc_missing/commit", json={"task_ids": ["t_x"]})
        self.assertEqual(r.status_code, 404)

    def test_task_ids_not_a_list_is_400(self):
        # "icebox" is a valid target (pull), so this isolates the type check.
        r = _CLIENT.post("/api/cycles/icebox/commit", json={"task_ids": "not-a-list"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
