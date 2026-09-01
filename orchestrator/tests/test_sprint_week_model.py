"""Cohesive sprint/week/backlog model — regression guard.

Pins the 4-bucket model (this week / next week / future / backlog) and its two
sync points:

  - get_cycle_board() returns next_week + future + icebox as a PARTITION of the
    sprint-less set (icebox = truly unscheduled only).
  - Sprint creation auto-commits tasks whose scheduled_week matches the new
    cycle's ISO week (auto_commit_scheduled, `cycle_auto_committed` event).
  - close_sprint() stamps unfinished tasks with next week's scheduled_week
    before their sprint pointer clears (they carry visibly, not into the void).
  - canvas.get_day_plan() groups the Later drawer into the same 4 buckets.

Isolation: every test runs against a FRESH copy of ~/.hermes/kanban.db (these
flows mutate sprints heavily — close/create/start — so a shared module copy
would leak state between tests). The real DB is never touched. If there is no
kanban.db to copy (fresh box / CI), the whole module skips.

Stdlib unittest (pytest-discoverable). Run:
    python -m pytest tests/test_sprint_week_model.py
"""
import datetime as _dt
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
    from dashboard import db as _db, sprints as _sprints, canvas as _canvas

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover - environment without deps/DB
    _READY = False


def _tid() -> str:
    return f"t_wkmodel_{uuid.uuid4().hex[:8]}"


@unittest.skipUnless(_READY, "dashboard modules / kanban.db unavailable")
class _FreshCopy(unittest.TestCase):
    """Per-TEST fresh copy of the real DB, with all three module globals
    repointed at it (and restored after), so mutating sprint flows can't leak
    between tests or into sibling modules."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_test_wkmodel_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp_db = Path(tmp)
        self._orig = (_db.KANBAN_DB, _sprints.KANBAN_DB)
        _db.KANBAN_DB = self.tmp_db
        _sprints.KANBAN_DB = self.tmp_db
        # Idempotent schema guards (the copy normally has everything already).
        _canvas.ensure_schema()
        _sprints.ensure_cycle_schema()

    def tearDown(self):
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig
        try:
            self.tmp_db.unlink()
        except OSError:
            pass

    # -- fixture helpers -------------------------------------------------
    def _product_project(self) -> str:
        """A product-kind project id — a DB trigger homes untagged inserts in
        the system Inbox project, which the unscoped planning views (rightly)
        exclude, so fixtures must claim a product project explicitly."""
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

    def _mk_task(self, **kw) -> str:
        """Insert a minimal human task; extra columns via kwargs."""
        tid = _tid()
        cols = {"id": tid, "title": f"wkmodel fixture {tid}", "status": "backlog",
                "created_at": int(time.time()), "assignee": "ricardo",
                "origin": "ricardo", "priority": 3,
                "project_id": self._product_project(), **kw}
        conn = _sprints.get_conn()
        try:
            conn.execute(
                f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()))
            conn.commit()
        finally:
            conn.close()
        return tid

    def _task(self, tid: str) -> dict:
        conn = _sprints.get_conn()
        try:
            return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        finally:
            conn.close()

    def _events(self, tid: str, kind: str) -> list:
        conn = _sprints.get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? AND kind = ?",
                (tid, kind)).fetchall()]
        finally:
            conn.close()

    def _active(self) -> dict:
        """The active cycle, creating+starting this week's if the copy has none."""
        active = _sprints.get_active_sprint()
        if not active:
            c = _sprints.create_cycle()
            _sprints.start_sprint(c["id"])
            active = _sprints.get_active_sprint()
        return active


class CycleBoardBuckets(_FreshCopy):
    """get_cycle_board(): next_week / future / icebox partition correctly."""

    def test_buckets_partition_the_sprintless_set(self):
        self._active()
        nxt = _sprints._iso_week_str(offset_weeks=1)
        far = _sprints._iso_week_str(offset_weeks=5)
        t_next = self._mk_task(scheduled_week=nxt)
        t_future = self._mk_task(scheduled_week=far)
        t_backlog = self._mk_task()

        board = _sprints.get_cycle_board()
        buckets = {k: {t["id"] for t in board[k]} for k in ("next_week", "future", "icebox")}

        self.assertIn(t_next, buckets["next_week"])
        self.assertIn(t_future, buckets["future"])
        self.assertIn(t_backlog, buckets["icebox"])
        # Partition: each fixture appears in exactly ONE bucket.
        for tid in (t_next, t_future, t_backlog):
            self.assertEqual(
                sum(tid in b for b in buckets.values()), 1,
                f"{tid} must live in exactly one bucket")
        self.assertEqual(board["week_meta"]["next"], nxt)

    def test_someday_tag_lands_in_future(self):
        # Live data has non-ISO tags ('someday'); they must not vanish.
        self._active()
        t = self._mk_task(scheduled_week="someday")
        board = _sprints.get_cycle_board()
        self.assertIn(t, {x["id"] for x in board["future"]})
        self.assertNotIn(t, {x["id"] for x in board["icebox"]})

    def test_icebox_is_truly_unscheduled_only(self):
        self._mk_task(scheduled_week=_sprints._iso_week_str(offset_weeks=1))
        for t in _sprints.get_icebox_tasks():
            self.assertIsNone(t["sprint_id"], f"{t['id']} has a sprint")
            self.assertIsNone(t["scheduled_week"], f"{t['id']} has a scheduled_week")

    def test_committed_task_is_in_no_drawer(self):
        active = self._active()
        t = self._mk_task(scheduled_week=_sprints._iso_week_str())
        _sprints.assign_task_sprint(t, active["id"])
        board = _sprints.get_cycle_board()
        for k in ("next_week", "future", "icebox"):
            self.assertNotIn(t, {x["id"] for x in board[k]}, f"committed task leaked into {k}")


class AutoCommitOnCreation(_FreshCopy):
    """Sprint creation auto-commits scheduled_week-matching tasks."""

    # Far enough out that the copied DB can't already have a cycle there.
    _OFFSET = 30

    def test_create_cycle_commits_matching_tasks(self):
        week = _sprints._iso_week_str(offset_weeks=self._OFFSET)
        t = self._mk_task(scheduled_week=week)
        t_done = self._mk_task(scheduled_week=week, status="done",
                               completed_at=int(time.time()))

        cyc = _sprints.create_cycle(
            start_date=int(time.time()) + self._OFFSET * 7 * 86400)
        self.assertIn(t, cyc.get("auto_committed", []))

        got = self._task(t)
        self.assertEqual(got["sprint_id"], cyc["id"])
        self.assertEqual(got["scheduled_week"], week)  # matches → kept, not cleared
        # Commit-ledger row is open (append-only ledger semantics preserved).
        conn = _sprints.get_conn()
        try:
            row = conn.execute(
                "SELECT outcome FROM task_sprints WHERE task_id = ? AND sprint_id = ?",
                (t, cyc["id"])).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNone(row["outcome"])
        self.assertTrue(self._events(t, "cycle_auto_committed"))
        # Settled work stays put.
        self.assertIsNone(self._task(t_done)["sprint_id"])

    def test_auto_commit_is_idempotent(self):
        week = _sprints._iso_week_str(offset_weeks=self._OFFSET)
        t = self._mk_task(scheduled_week=week)
        cyc = _sprints.create_cycle(
            start_date=int(time.time()) + self._OFFSET * 7 * 86400)
        self.assertEqual(_sprints.auto_commit_scheduled(cyc["id"]), [])
        self.assertEqual(self._task(t)["sprint_id"], cyc["id"])


class CloseSprintStamping(_FreshCopy):
    """close_sprint() stamps unfinished tasks with next week's scheduled_week."""

    def _past_cycle(self, weeks_ago: int = 10) -> dict:
        ts = int(time.time()) - weeks_ago * 7 * 86400
        ws, we = _sprints._week_window(ts)
        c = _sprints.create_cycle(start_date=ws, end_date=we)
        return _sprints.get_sprint(c["id"])

    def test_close_stamps_unfinished_with_next_week(self):
        cyc = self._past_cycle()
        t = self._mk_task()
        t_tagged = self._mk_task(scheduled_week="2030-W01")
        _sprints.assign_task_sprint(t, cyc["id"])
        conn = _sprints.get_conn()
        try:  # tag AFTER committing (set_scheduled_week would pull it out)
            conn.execute("UPDATE tasks SET scheduled_week = '2030-W01' WHERE id = ?",
                         (t_tagged,))
            conn.commit()
        finally:
            conn.close()
        _sprints.assign_task_sprint(t_tagged, cyc["id"])
        # assign_task_sprint's week-sync clears a mismatched tag; re-tag to pin
        # the "already set → untouched" branch.
        conn = _sprints.get_conn()
        try:
            conn.execute("UPDATE tasks SET scheduled_week = '2030-W01' WHERE id = ?",
                         (t_tagged,))
            conn.commit()
        finally:
            conn.close()

        _sprints.close_sprint(cyc["id"], auto_create=False)

        expected = _sprints._iso_week_str(cyc["end_date"] + 1)
        got = self._task(t)
        self.assertIsNone(got["sprint_id"])
        self.assertEqual(got["scheduled_week"], expected)
        # An already-set scheduled_week is respected, not overwritten.
        self.assertEqual(self._task(t_tagged)["scheduled_week"], "2030-W01")
        # Ledger row stamped carried (unchanged semantics).
        conn = _sprints.get_conn()
        try:
            row = conn.execute(
                "SELECT outcome FROM task_sprints WHERE task_id = ? AND sprint_id = ?",
                (t, cyc["id"])).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["outcome"], "carried")

    def test_close_with_autocreate_rolls_carry_into_next_cycle(self):
        # The full loop: stamp on close → auto-create next week's cycle →
        # creation-sync commits the carried task into it.
        cyc = self._past_cycle()
        t = self._mk_task()
        _sprints.assign_task_sprint(t, cyc["id"])

        res = _sprints.close_sprint(cyc["id"])  # auto_create=True default
        nxt = res.get("next_sprint")
        self.assertTrue(nxt)
        got = self._task(t)
        self.assertEqual(got["sprint_id"], nxt)
        self.assertEqual(got["scheduled_week"],
                         _sprints._sprint_to_iso_week(_sprints.get_sprint(nxt)))
        self.assertTrue(self._events(t, "cycle_auto_committed"))


class DayPlanLaterGroups(_FreshCopy):
    """canvas.get_day_plan(): the Later drawer groups by the 4 buckets."""

    def test_later_groups_bucket_membership(self):
        active = self._active()
        t_this = self._mk_task(sprint_id=active["id"])
        t_next = self._mk_task(scheduled_week=_sprints._iso_week_str(offset_weeks=1))
        t_future = self._mk_task(scheduled_week=_sprints._iso_week_str(offset_weeks=4))
        t_backlog = self._mk_task()

        d = _canvas.get_day_plan()
        self.assertIn("later_groups", d)
        groups = {k: {t["id"] for t in v} for k, v in d["later_groups"].items()}

        self.assertIn(t_this, groups["this_week"])
        self.assertIn(t_next, groups["next_week"])
        self.assertIn(t_future, groups["future"])
        self.assertIn(t_backlog, groups["backlog"])
        # The groups partition the flat list exactly (nothing lost, nothing doubled).
        flat = {t["id"] for t in d["later"]}
        self.assertEqual(set().union(*groups.values()), flat)
        self.assertEqual(sum(len(v) for v in groups.values()), len(d["later"]))

    def test_non_active_sprint_is_future_while_null_is_backlog(self):
        active = self._active()
        inactive = _sprints.create_cycle(
            start_date=active["start_date"] + 4 * 7 * 86400)
        t_inactive = self._mk_task(sprint_id=inactive["id"])
        t_null = self._mk_task()

        groups = {
            k: {t["id"] for t in v}
            for k, v in _canvas.get_day_plan()["later_groups"].items()
        }

        self.assertIn(t_inactive, groups["future"])
        self.assertIn(t_null, groups["backlog"])
        self.assertNotIn(t_inactive, groups["this_week"])
        self.assertNotIn(t_null, groups["this_week"])


class CanvasPersonalProjectExclusion(_FreshCopy):
    """Personal tasks stay stored but never enter professional daily surfaces."""

    def test_personal_tasks_are_excluded_from_today_and_candidates(self):
        today = _canvas._today()
        yesterday = (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
        product = self._product_project()

        fixtures = {
            "do": (
                self._mk_task(project_id="proj_personal", planned_for=today),
                self._mk_task(project_id=product, planned_for=today),
            ),
            "review": (
                self._mk_task(project_id="proj_personal", status="review"),
                self._mk_task(project_id=product, status="review"),
            ),
            "blocked": (
                self._mk_task(project_id="proj_personal", status="blocked"),
                self._mk_task(project_id=product, status="blocked"),
            ),
            "later": (
                self._mk_task(project_id="proj_personal"),
                self._mk_task(project_id=product),
            ),
            "overdue": (
                self._mk_task(project_id="proj_personal", due_date=yesterday),
                self._mk_task(project_id=product, due_date=yesterday),
            ),
            "candidate": (
                self._mk_task(
                    project_id="proj_personal", planned_for=yesterday),
                self._mk_task(
                    project_id=product, planned_for=yesterday),
            ),
        }

        plan = _canvas.get_day_plan(today)
        zone_ids = {
            "do": {t["id"] for t in plan["do"]},
            "review": {t["id"] for t in plan["review"]},
            "blocked": {t["id"] for t in plan["needs_you"]["blocked"]},
            "later": {t["id"] for t in plan["later"]},
            "overdue": {t["id"] for t in plan["overdue"]},
        }
        for zone in ("do", "review", "blocked", "later", "overdue"):
            personal, professional = fixtures[zone]
            self.assertNotIn(personal, zone_ids[zone])
            self.assertIn(professional, zone_ids[zone])

        candidates = _canvas.plan_candidates(today)
        candidate_ids = {t["id"] for t in candidates["candidates"]}
        personal_candidate, professional_candidate = fixtures["candidate"]
        personal_ids = {personal for personal, _ in fixtures.values()}
        self.assertTrue(candidate_ids.isdisjoint(personal_ids))
        self.assertIn(professional_candidate, candidate_ids)
        self.assertIn(fixtures["overdue"][1], candidate_ids)

        conn = _sprints.get_conn()
        try:
            expected_review = conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE status = 'review' AND project_id != 'proj_personal'"
            ).fetchone()[0]
            expected_blocked = conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE status = 'blocked' AND project_id != 'proj_personal'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(plan["counts"]["review_total"], expected_review)
        self.assertEqual(candidates["review_count"], expected_review)
        self.assertEqual(candidates["blocked_count"], expected_blocked)

        # The Kanban task source is untouched: personal rows still exist.
        for personal, _ in fixtures.values():
            self.assertEqual(self._task(personal)["project_id"], "proj_personal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
