"""Exact weekly-slot contract for Finish Sprint.

The board's W+1/W+2 slots are calendar positions, not "the first two future
rows".  These tests deliberately seed distant and project-specific cycles so a
positional resolver cannot pass by accident.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
try:
    from dashboard import db as _db, sprints as _sprints

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover - dependency-less CI
    _READY = False


@unittest.skipUnless(_READY, "dashboard modules / kanban.db unavailable")
class FinishSprintSlots(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_test_finish_slots_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp_db = Path(tmp)
        self._orig = (_db.KANBAN_DB, _sprints.KANBAN_DB)
        _db.KANBAN_DB = self.tmp_db
        _sprints.KANBAN_DB = self.tmp_db
        _sprints.ensure_cycle_schema()

    def tearDown(self):
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig
        try:
            self.tmp_db.unlink()
        except OSError:
            pass

    def _reset(self):
        conn = _sprints.get_conn()
        try:
            conn.execute("UPDATE tasks SET sprint_id = NULL")
            conn.execute("DELETE FROM task_sprints")
            conn.execute("DELETE FROM sprints")
            conn.commit()
        finally:
            conn.close()

    def _product_project(self):
        conn = _sprints.get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM projects WHERE COALESCE(kind, 'product') "
                "NOT IN ('personal', 'system') AND archived_at IS NULL LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self.skipTest("no product project in fixture DB")
        return row["id"]

    def _task(self):
        tid = f"t_finish_slots_{uuid.uuid4().hex[:8]}"
        conn = _sprints.get_conn()
        try:
            conn.execute(
                "INSERT INTO tasks "
                "(id,title,status,created_at,assignee,origin,priority,project_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tid, "finish-slot fixture", "backlog", int(time.time()),
                 "ricardo", "ricardo", 1, self._product_project()),
            )
            conn.commit()
        finally:
            conn.close()
        return tid

    def _cycle(self, base_start, offset, *, active=False):
        ws, we = _sprints._week_window(base_start + offset * 7 * 86400 + 3 * 86400)
        cycle = _sprints.create_cycle(start_date=ws, end_date=we)
        if active:
            _sprints.start_sprint(cycle["id"])
        return _sprints.get_sprint(cycle["id"])

    def _row(self, sprint_id):
        return _sprints.get_sprint(sprint_id)

    def _task_sprint(self, task_id):
        conn = _sprints.get_conn()
        try:
            return conn.execute(
                "SELECT sprint_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["sprint_id"]
        finally:
            conn.close()

    def test_finish_uses_exact_w_plus_1_and_w_plus_2_for_all_occupancy_cases(self):
        for has_next in (False, True):
            for has_plus2 in (False, True):
                with self.subTest(has_next=has_next, has_plus2=has_plus2):
                    self._reset()
                    base_start, _ = _sprints._week_window()
                    active = self._cycle(base_start, 0, active=True)
                    expected_next = self._cycle(base_start, 1) if has_next else None
                    expected_plus2 = self._cycle(base_start, 2) if has_plus2 else None
                    distant = self._cycle(base_start, 40)
                    tid = self._task()
                    _sprints.assign_task_sprint(tid, active["id"])

                    result = _sprints.finish_sprint()
                    activated = self._row(result["activated"]["id"])
                    next_up = self._row(result["next_up"]["id"])

                    self.assertEqual(_sprints._sprint_to_iso_week(activated),
                                     _sprints._iso_week_str(base_start, 1))
                    self.assertEqual(_sprints._sprint_to_iso_week(next_up),
                                     _sprints._iso_week_str(base_start, 2))
                    if expected_next:
                        self.assertEqual(activated["id"], expected_next["id"])
                    if expected_plus2:
                        self.assertEqual(next_up["id"], expected_plus2["id"])
                    self.assertEqual(activated["status"], "active")
                    self.assertEqual(next_up["status"], "planning")
                    self.assertEqual(self._row(distant["id"])["status"], "planning")
                    self.assertEqual(self._task_sprint(tid), activated["id"])

    def test_project_sprint_cannot_fill_global_next_slot(self):
        self._reset()
        base_start, _ = _sprints._week_window()
        active = self._cycle(base_start, 0, active=True)
        ws, we = _sprints._week_window(base_start + 10 * 86400)
        project_sprint = _sprints.create_sprint(
            self._product_project(), "Client sprint in next week",
            start_date=ws, end_date=we,
        )
        tid = self._task()
        _sprints.assign_task_sprint(tid, active["id"])

        result = _sprints.finish_sprint()

        self.assertNotEqual(result["activated"]["id"], project_sprint["id"])
        activated = self._row(result["activated"]["id"])
        self.assertIsNone(activated["project_id"])
        self.assertEqual(_sprints._sprint_to_iso_week(activated),
                         _sprints._iso_week_str(base_start, 1))

    def test_read_model_keeps_missing_adjacent_slots_empty_despite_distant_cycle(self):
        self._reset()
        base_start, _ = _sprints._week_window()
        active = self._cycle(base_start, 0, active=True)
        distant = self._cycle(base_start, 40)

        slots = _sprints.get_board_cycle_slots()

        self.assertEqual(slots["anchor"]["id"], active["id"])
        self.assertIsNone(slots["next"]["cycle"])
        self.assertIsNone(slots["plus2"]["cycle"])
        self.assertEqual(slots["next"]["iso_week"],
                         _sprints._iso_week_str(base_start, 1))
        self.assertEqual(slots["plus2"]["iso_week"],
                         _sprints._iso_week_str(base_start, 2))
        self.assertEqual(self._row(distant["id"])["status"], "planning")

    def test_iso_year_rollover_is_date_based(self):
        self._reset()
        # Monday of ISO 2020-W53; the following slots are 2021-W01 and W02.
        base_start, _ = _sprints._week_window(
            int(time.mktime((2020, 12, 28, 12, 0, 0, 0, 0, -1))))
        active = self._cycle(base_start, 0, active=True)
        tid = self._task()
        _sprints.assign_task_sprint(tid, active["id"])

        result = _sprints.finish_sprint()

        self.assertEqual(_sprints._sprint_to_iso_week(
            self._row(result["activated"]["id"])), "2021-W01")
        self.assertEqual(_sprints._sprint_to_iso_week(
            self._row(result["next_up"]["id"])), "2021-W02")


if __name__ == "__main__":
    unittest.main()
