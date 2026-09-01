"""Contract for the 3x-daily ritual composer (dashboard/brief.py + m01_brief_runs).

What this pins, and why each one is load-bearing:

  1. **The five-block skeleton is unconditional.** Same keys, same order, on a
     noisy day and on an empty one. The skeleton IS the accommodation — a brief
     whose shape changes with the data teaches the operator to *read* it instead of
     to *look at* it.
  2. **Idempotency per (date, slot).** Cron delivery is at-least-once; a second
     compose must return the STORED payload and never a second row, or a
     retried job posts the same brief to Telegram twice.
  3. **The write boundary.** Morning commits the day's plan when — and only
     when — no plan exists; an existing plan is never touched (the commit is
     `replace=True`, so the zero-plan precondition is what stands between a
     forward default and wiping a plan the operator made by hand). Midday and
     evening leave `tasks.planned_for` and the task_events log alone.
  4. **The forward-schema guards, on BOTH sides.** `deals.project_id` and
     `task_dispatches` arrive in a later migration. Absent → the composer emits
     an empty list (asserted against a DB that provably HAS won deals, so the
     empty result is a guard firing and not an empty universe); present →
     the same rows appear. A guard tested only on the side it is currently on
     is a guard that has never been exercised.
  5. **The hard 12-line cap.** Asserted on a maximally-noisy seed for all three
     slots, plus the em-dash floor on an empty day.

DB isolation: a COPY of ~/.hermes/kanban.db per test (the
test_context_endpoint.py / test_migration_runner.py pattern), with the schema
brought up by `runner.run()` itself — so the migration REGISTRATION is exercised
by every test here, not just asserted about. `runner.run_backup` is always
stubbed: no test may write into the operator's ~/.hermes/backups. The real DB is
never opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_brief.py   # from orchestrator/
"""
import calendar
import datetime as _dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_READY = False
try:
    from dashboard import brief
    from dashboard import db as _db, sprints as _sprints, orchestration as _orch
    from dashboard.migrations import runner

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

# A fixed synthetic day, so nothing here depends on when the suite runs.
DATE = "2026-07-15"

# Data tables the "empty day" copy is stripped of. The schema (and the
# hermes-owned tables an ensure chain ALTERs) stays — a genuinely empty file
# cannot bootstrap.
# `task_dispatches` belongs here for the same reason as the rest: it is a data
# table the composer reads. Leaving it out made "an empty day" a lie — the live
# copy's own dispatch rows leaked into the EmptyDay cases, which went permanently
# red the moment the first real dispatch happened (and would have got worse with
# every one after it).
_WIPE = ("task_events", "task_runs", "tasks", "deal_events", "deals",
         "projects", "brief_runs", "task_dispatches")


class _BriefCase(unittest.TestCase):
    """Live-DB copy + the migration runner, per test."""

    empty = False

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_brief_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints_db = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        self.orch_dir = Path(tempfile.mkdtemp(prefix="orch_brief_test_"))
        self._orig_orch, self._orig_specs = _orch.ORCH_DIR, _orch.SPECS_DIR
        _orch.ORCH_DIR, _orch.SPECS_DIR = self.orch_dir, self.orch_dir / "specs"
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        # The composer reads the proactive layer's queue off disk. Repoint it at
        # an empty temp dir for every test, or the LIVE queue leaks in and "an
        # empty day" stops being empty — the same lie task_dispatches told the
        # EmptyDay cases before it joined _WIPE.
        self.intent_dir = Path(tempfile.mkdtemp(prefix="intentq_brief_test_"))
        self._orig_intent_dir = brief.INTENT_QUEUE_DIR
        brief.INTENT_QUEUE_DIR = str(self.intent_dir)
        if self.empty:
            self._wipe()
        # Brings up brief_runs THROUGH the registered migration — so every test
        # in this file also asserts the registration works.
        runner.run()
        self.midnight = brief.midnight_ts(DATE)
        self.noon = self.midnight + 12 * 3600

    def tearDown(self):
        runner.run_backup = self._orig_backup
        brief.INTENT_QUEUE_DIR = self._orig_intent_dir
        shutil.rmtree(self.intent_dir, ignore_errors=True)
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints_db
        _orch.ORCH_DIR, _orch.SPECS_DIR = self._orig_orch, self._orig_specs
        shutil.rmtree(self.orch_dir, ignore_errors=True)
        try:
            self.tmp.unlink()
        except Exception:
            pass

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        c = sqlite3.connect(str(self.tmp))
        c.row_factory = sqlite3.Row
        return c

    def _wipe(self):
        c = sqlite3.connect(str(self.tmp))
        c.execute("PRAGMA foreign_keys = OFF")
        for table in _WIPE:
            try:
                c.execute(f"DELETE FROM {table}")
            except sqlite3.Error:
                pass
        c.commit()
        c.close()

    def _task(self, c, tid, title, status, project="proj_brief_loud", **kw):
        cols = dict(id=tid, title=title, status=status, project_id=project,
                    created_at=self.midnight, workspace_kind="none",
                    consecutive_failures=0, goal_mode=0, assignee="ricardo")
        cols.update(kw)
        names = ",".join(cols)
        c.execute(f"INSERT INTO tasks ({names}) VALUES ({','.join('?' * len(cols))})",
                  tuple(cols.values()))

    def _seed_noise(self):
        """A maximally-noisy day: blocked work, a review queue, completions,
        quiet projects, deals moving and going cold, runs finishing and
        crashing. Layered ON TOP of the live copy's own data on purpose — the
        line cap must survive a real day, not a tidy fixture."""
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        for pid, slug, name in (("proj_brief_loud", "brief-loud", "Brief Loud"),
                                ("proj_brief_quiet", "brief-quiet", "Brief Quiet")):
            c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                      (pid, slug, name, self.midnight))
        # Nothing is planned for the test day until a test says so.
        c.execute("UPDATE tasks SET planned_for = NULL, plan_order = NULL "
                  "WHERE planned_for = ?", (DATE,))
        for i in range(3):
            self._task(c, f"t_brief_blocked_{i}", f"Blocked work item number {i}", "blocked")
        for i in range(2):
            self._task(c, f"t_brief_review_{i}", f"Awaiting review number {i}", "review")
        self.done_ids = []
        for i in range(4):
            tid = f"t_brief_done_{i}"
            self.done_ids.append(tid)
            self._task(c, tid, f"Shipped thing number {i}", "done",
                       completed_at=self.noon, reviewed_at=self.noon)
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                      "VALUES (?,?,?,?)", (tid, "completed", "{}", self.noon))
        # A live project with open work and no completions → "quiet".
        self._task(c, "t_brief_quiet_0", "Untouched work", "todo", project="proj_brief_quiet")
        # Runs: in flight, finished, crashed. (task_runs.id is an AUTOINCREMENT
        # INTEGER — let SQLite assign it.)
        #
        # The in-flight pair carries a LIVE heartbeat, not just `status =
        # 'running'`: the composer bounds "running" by liveness now, and a
        # fixture that seeds a two-week-old row and calls it running is exactly
        # the reading that put "🤖 Agents · 5 running" in every real brief.
        for i in range(2):
            c.execute("INSERT INTO task_runs (task_id, status, started_at, last_heartbeat_at) "
                      "VALUES (?,?,?,?)",
                      ("t_brief_quiet_0", "running", self.noon, int(time.time())))
        for i in range(3):
            c.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                      "VALUES (?,?,?,?,?)",
                      (f"t_brief_blocked_{i}", "crashed", "crashed", self.midnight, self.noon))
        for i in range(2):
            c.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                      "VALUES (?,?,?,?,?)",
                      (f"t_brief_done_{i}", "done", "completed", self.midnight, self.noon))
        # Deals: moved today, and gone cold.
        c.execute("INSERT OR IGNORE INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  ("acct_brief", "Brief Account", self.midnight))
        for i in range(3):
            did = f"deal_brief_moved_{i}"
            c.execute("INSERT INTO deals (id, account_id, title, stage, value, created_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (did, "acct_brief", f"Moving deal {i}", "proposal", 25000 + i, self.midnight))
            c.execute("INSERT INTO deal_events (deal_id, kind, payload, created_at) "
                      "VALUES (?,?,?,?)",
                      (did, "stage_changed", json.dumps({"from": "lead", "to": "proposal"}),
                       self.noon))
        for i in range(2):
            c.execute("INSERT INTO deals (id, account_id, title, stage, value, "
                      "last_touch_date, created_at) VALUES (?,?,?,?,?,?,?)",
                      (f"deal_brief_cold_{i}", "acct_brief", f"Cold deal {i}", "engaged",
                       50000 + i, "2026-05-01", self.midnight))
        c.commit()
        c.close()


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class ComposeShape(_BriefCase):
    """The payload contract."""

    def setUp(self):
        super().setUp()
        self._seed_noise()

    def test_payload_carries_the_five_blocks_and_their_keys(self):
        p = brief.compose("evening", DATE)
        self.assertEqual(p["date"], DATE)
        self.assertEqual(p["slot"], "evening")
        for key in ("needs_you", "money", "delivery", "agents", "next"):
            self.assertIn(key, p, key)
        self.assertEqual(
            set(p["needs_you"]) >= {"blocked", "review_count", "orphan_won_deals", "count"}, True)
        self.assertEqual(
            set(p["money"]) >= {"moved", "stale_over_14d", "pipeline_open_value"}, True)
        self.assertEqual(set(p["delivery"]) >= {"projects_moved", "projects_quiet"}, True)
        self.assertEqual(set(p["agents"]) >= {"running", "finished", "failed", "dispatches"}, True)
        self.assertEqual(set(p["next"]) >= {"tasks", "plan_committed"}, True)
        self.assertLessEqual(len(p["next"]["tasks"]), 3)   # "1 to 3 things, never more"

    def test_blocks_report_the_seeded_day(self):
        p = brief.compose("evening", DATE)
        titles = {b["title"] for b in p["needs_you"]["blocked"]}
        self.assertTrue({f"Blocked work item number {i}" for i in range(3)} <= titles)
        self.assertGreaterEqual(p["needs_you"]["review_count"], 2)
        moved = {d["id"] for d in p["money"]["moved"]}
        self.assertTrue({f"deal_brief_moved_{i}" for i in range(3)} <= moved)
        self.assertEqual([d["to"] for d in p["money"]["moved"] if d["id"] == "deal_brief_moved_0"],
                         ["proposal"])
        cold = {d["id"]: d["days"] for d in p["money"]["stale_over_14d"]}
        self.assertIn("deal_brief_cold_0", cold)
        self.assertGreater(cold["deal_brief_cold_0"], 14)
        by_project = {x["id"]: x["done_today"] for x in p["delivery"]["projects_moved"]}
        self.assertEqual(by_project.get("proj_brief_loud"), 4)
        self.assertIn("Brief Quiet", p["delivery"]["projects_quiet"])
        self.assertTrue(set(self.done_ids) <= {t["id"] for t in p["delivery"]["done"]})
        self.assertGreaterEqual(p["agents"]["running"], 2)
        self.assertGreaterEqual(len(p["agents"]["failed"]), 3)
        self.assertGreaterEqual(len(p["agents"]["finished"]), 2)

    def test_pipeline_open_value_equals_direct_sql(self):
        p = brief.compose("midday", DATE)
        c = self._conn()
        closed = tuple(brief._open_stage_filter())
        ph = ",".join("?" * len(closed))
        expected = c.execute(
            f"SELECT COALESCE(SUM(value), 0) FROM deals WHERE stage NOT IN ({ph})",
            closed).fetchone()[0]
        c.close()
        self.assertEqual(p["money"]["pipeline_open_value"], float(expected))
        self.assertGreater(p["money"]["pipeline_open_value"], 0)  # not trivially green

    def test_a_dated_brief_cannot_see_past_its_own_midnight(self):
        """Every window used to be open-ended above, so a brief for a past date
        reported whatever happened AFTER it — and once more than _LIST_CAP deals
        moved on some later day, those rows filled the list and evicted the ones
        the brief is about. (Observed live: 21 stage changes on 2026-07-29 made
        `compose(..., '2026-07-15')` report none of 07-15's own.)"""
        after = brief.end_ts(DATE) + 3600
        c = self._conn()
        try:
            c.execute("INSERT INTO deal_events (deal_id, kind, payload, created_at) "
                      "VALUES (?,?,?,?)",
                      ("deal_brief_moved_0", "stage_changed",
                       json.dumps({"from": "proposal", "to": "won"}), after))
            c.execute("INSERT INTO task_dispatches (id, task_id, executor_kind, state, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                      ("disp_brief_future", "t_brief_quiet_0", "codex", "delivered",
                       after, after))
            c.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                      "VALUES (?,?,?,?,?)",
                      ("t_brief_quiet_0", "crashed", "crashed", after, after + 60))
            c.commit()
        finally:
            c.close()
        p = brief.compose("evening", DATE)
        self.assertEqual([m for m in p["money"]["moved"] if m.get("to") == "won"], [])
        self.assertNotIn("disp_brief_future", [d["id"] for d in p["agents"]["dispatches"]])
        self.assertNotIn(after + 60, [r.get("ended_at") for r in p["agents"]["failed"]])
        # ...and the day's own rows are still there, so this is a BOUND, not a mute.
        self.assertTrue({f"deal_brief_moved_{i}" for i in range(3)}
                        <= {d["id"] for d in p["money"]["moved"]})

    def test_since_ts_is_midnight_then_the_previous_brief(self):
        c = self._conn()
        self.assertEqual(brief.since_ts(c, DATE), self.midnight)
        c.close()
        first = brief.get_or_compose("morning", DATE)
        second = brief.get_or_compose("midday", DATE)
        self.assertEqual(second["payload"]["since_ts"], first["created_at"])

    def test_timezone_is_america_monterrey_not_the_server_clock(self):
        # 2026-07-15 00:00 in Monterrey (UTC-6, no DST since 2022) is 06:00Z.
        self.assertEqual(brief.midnight_ts(DATE),
                         calendar.timegm((2026, 7, 15, 6, 0, 0, 0, 0, 0)))
        self.assertEqual(str(brief.TZ), "America/Monterrey")
        self.assertEqual(brief.valid_date(None), brief.today())


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class CloseCommercialLine(_BriefCase):
    """Close attributes accepted tasks through explicit commercial lineage."""

    empty = True

    def _account(self, c, aid, name):
        c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  (aid, name, self.midnight))

    def _project(self, c, pid, name, account_id=None):
        c.execute("INSERT INTO projects (id, slug, name, account_id, created_at) "
                  "VALUES (?,?,?,?,?)", (pid, pid, name, account_id, self.midnight))

    def _deal(self, c, did, account_id):
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (did, account_id, f"Deal {did}", "proposal", 1000, self.midnight))

    def _done(self, c, tid, *, deal_id=None, project_id=None, title=None):
        self._task(c, tid, title or f"Done {tid}", "done", project=project_id,
                   deal_id=deal_id, completed_at=self.noon, reviewed_at=self.noon)

    def _commercial_lines(self):
        text = brief.render_telegram(brief.compose("evening", DATE))
        return text, [line for line in text.split("\n") if "Hoy moviste " in line]

    def test_deal_direct_attribution(self):
        c = self._conn()
        self._account(c, "acct_direct", "Cuenta Directa")
        self._deal(c, "deal_direct", "acct_direct")
        self._done(c, "t_direct", deal_id="deal_direct")
        c.commit()
        c.close()

        _, lines = self._commercial_lines()
        self.assertEqual(lines, ["• Hoy moviste Cuenta Directa (1)."])

    def test_project_fallback_attribution(self):
        c = self._conn()
        self._account(c, "acct_project", "Cuenta Proyecto")
        self._project(c, "proj_fallback", "Proyecto Fallback", "acct_project")
        self._done(c, "t_project", project_id="proj_fallback")
        c.commit()
        c.close()

        _, lines = self._commercial_lines()
        self.assertEqual(lines, ["• Hoy moviste Cuenta Proyecto (1)."])

    def test_deal_precedes_project_and_each_task_counts_once(self):
        c = self._conn()
        self._account(c, "acct_deal", "Cuenta Deal")
        self._account(c, "acct_project", "Cuenta Proyecto")
        self._project(c, "proj_both", "Proyecto Ambos", "acct_project")
        self._deal(c, "deal_both", "acct_deal")
        self._done(c, "t_both", deal_id="deal_both", project_id="proj_both")
        c.commit()
        c.close()

        text, lines = self._commercial_lines()
        self.assertEqual(lines, ["• Hoy moviste Cuenta Deal (1)."])
        self.assertNotIn("Cuenta Proyecto", text)

    def test_no_lineage_omits_the_line_without_breaking_the_empty_floor(self):
        c = self._conn()
        self._account(c, "acct_title_only", "Cuenta Solo Titulo")
        self._done(c, "t_unattributed", title="Mover Cuenta Solo Titulo")
        c.commit()
        c.close()

        text, lines = self._commercial_lines()
        self.assertEqual(lines, [])
        self.assertIn(f"{brief.LABELS['delivery']} {brief.EMPTY}", text)

    def test_more_than_three_accounts_are_truncated_with_a_spanish_remainder(self):
        c = self._conn()
        for i, name in enumerate(("Alfa", "Beta", "Delta", "Gamma", "Zeta")):
            aid, did = f"acct_{i}", f"deal_{i}"
            self._account(c, aid, name)
            self._deal(c, did, aid)
            self._done(c, f"t_{i}", deal_id=did)
        c.commit()
        c.close()

        text, lines = self._commercial_lines()
        self.assertEqual(lines, ["• Hoy moviste Alfa (1), Beta (1) y Delta (1) +2 más."])
        self.assertNotIn("Gamma (1)", text)
        self.assertNotIn("Zeta (1)", text)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class ForwardSchemaGuards(_BriefCase):
    """`deals.project_id` and `task_dispatches` — both sides of the guard.

    These landed in m02_spine, and until it shipped the "absent" half was free:
    the runner had not created the schema yet, so the fixture tested it by doing
    nothing. That was a fact about migration HISTORY, not a requirement — and the
    moment m02 registered, two cases here started failing for being correct
    (`assertEqual(_columns(c, "task_dispatches"), set())`), while the two "present"
    cases started failing on `duplicate column name` because they built the schema
    the migration now builds.

    What is actually required is the RELATIONSHIP: the composer reads the schema
    at query time and degrades to `[]` when it is missing, instead of raising or
    fabricating. So the fixture now OWNS the schema state in both directions — it
    drops the column/table to exercise the closed guard, and uses the migration's
    own schema (seeding only rows) to exercise the open one. Neither half depends
    on how far the ledger has been applied, so neither has to be rewritten again
    when the next migration lands.
    """

    def setUp(self):
        super().setUp()
        self._seed_noise()
        c = self._conn()
        self.won = c.execute("SELECT COUNT(*) FROM deals WHERE stage = 'won'").fetchone()[0]
        self.orphan_won = c.execute(
            "SELECT COUNT(*) FROM deals WHERE stage = 'won' AND project_id IS NULL"
        ).fetchone()[0]
        c.close()

    def test_orphan_won_deals_is_empty_while_the_column_is_absent(self):
        c = self._conn()
        # SQLite refuses to drop an indexed column, so the m02 index goes first.
        c.execute("DROP INDEX IF EXISTS idx_deals_project")
        c.execute("ALTER TABLE deals DROP COLUMN project_id")
        c.commit()
        self.assertNotIn("project_id", brief._columns(c, "deals"))
        c.close()
        p = brief.compose("evening", DATE)
        self.assertEqual(p["needs_you"]["orphan_won_deals"], [])
        # The empty list is a GUARD, not an empty universe: the DB has won deals.
        self.assertGreater(self.won, 0)

    def test_orphan_won_deals_populates_once_the_column_exists(self):
        c = self._conn()
        # m02_spine brought the column; only the row is this test's.
        self.assertIn("project_id", brief._columns(c, "deals"))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  ("deal_brief_orphan", "acct_brief", "Won and unlinked", "won",
                   99000, self.midnight))
        c.commit()
        c.close()
        p = brief.compose("evening", DATE)
        ids = {d["id"] for d in p["needs_you"]["orphan_won_deals"]}
        self.assertIn("deal_brief_orphan", ids)
        self.assertEqual(len(ids), self.orphan_won + 1)
        self.assertGreaterEqual(p["needs_you"]["count"], len(ids))

    def test_dispatches_are_empty_while_the_table_is_absent(self):
        c = self._conn()
        c.execute("DROP TABLE task_dispatches")
        c.commit()
        self.assertEqual(brief._columns(c, "task_dispatches"), set())
        c.close()
        self.assertEqual(brief.compose("midday", DATE)["agents"]["dispatches"], [])

    def test_dispatches_populate_once_the_table_exists(self):
        c = self._conn()
        # m02_spine brought the table (with its state CHECK); only the rows are
        # this test's — so a drift between the migration's shape and the shape
        # the composer expects fails here instead of being papered over by a
        # hand-rolled CREATE TABLE that only has to satisfy the reader.
        c.execute("INSERT INTO task_dispatches (id, task_id, executor_kind, state, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                  ("disp_brief_1", "t_brief_quiet_0", "codex", "delivered",
                   self.noon, self.noon))
        # Stamped BEFORE the window opens → excluded, so the since_ts filter is
        # exercised rather than assumed.
        c.execute("INSERT INTO task_dispatches (id, task_id, executor_kind, state, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                  ("disp_brief_old", "t_brief_quiet_0", "hermes", "requested",
                   self.midnight - 5000, self.midnight - 5000))
        c.commit()
        c.close()
        d = brief.compose("midday", DATE)["agents"]["dispatches"]
        self.assertEqual([x["id"] for x in d], ["disp_brief_1"])
        self.assertEqual(d[0]["executor_kind"], "codex")


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class Idempotency(_BriefCase):
    """(date, slot) is the primary key, and that IS the anti-double-post."""

    def setUp(self):
        super().setUp()
        self._seed_noise()

    def _rows(self, date=DATE):
        c = self._conn()
        try:
            return c.execute("SELECT COUNT(*) FROM brief_runs WHERE date = ?",
                             (date,)).fetchone()[0]
        finally:
            c.close()

    def test_recompose_returns_the_stored_payload_and_no_second_row(self):
        first = brief.get_or_compose("evening", DATE)
        self.assertFalse(first["already_composed"])
        self.assertFalse(first["sent"])
        second = brief.get_or_compose("evening", DATE)
        self.assertTrue(second["already_composed"])
        self.assertEqual(second["payload"], first["payload"])
        self.assertEqual(second["rendered_md"], first["rendered_md"])
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(self._rows(), 1)

    def test_rendered_md_is_stored_and_matches_the_renderer(self):
        res = brief.get_or_compose("morning", DATE)
        self.assertEqual(res["rendered_md"], brief.render_telegram(res["payload"]))
        self.assertTrue(res["rendered_md"].strip())

    def test_mark_sent_stamps_once_and_is_idempotent(self):
        brief.get_or_compose("midday", DATE)
        first = brief.mark_sent("midday", DATE)
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["already_sent"])
        self.assertTrue(first["sent_at"])
        second = brief.mark_sent("midday", DATE)
        self.assertTrue(second["already_sent"])
        self.assertEqual(second["sent_at"], first["sent_at"])
        self.assertTrue(brief.get_or_compose("midday", DATE)["sent"])

    def test_mark_sent_without_a_brief_is_an_error(self):
        res = brief.mark_sent("evening", DATE)
        self.assertEqual(res["status"], "error")

    def test_latest_returns_the_most_recent_row(self):
        brief.get_or_compose("morning", DATE)
        brief.get_or_compose("evening", DATE)
        self.assertEqual(brief.latest()["slot"], "evening")

    def test_bad_slot_and_bad_date_are_rejected(self):
        for bad in ("noon", "", "MORNING"):
            with self.assertRaises(ValueError):
                brief.get_or_compose(bad, DATE)
        with self.assertRaises(ValueError):
            brief.get_or_compose("morning", "15-07-2026")
        with self.assertRaises(ValueError):
            brief.compose("morning", "2026-02-31")


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class WriteBoundary(_BriefCase):
    """The ONE sanctioned write, and the silence everywhere else."""

    def setUp(self):
        super().setUp()
        self._seed_noise()

    def _plan(self):
        c = self._conn()
        try:
            return sorted(r["id"] for r in
                          c.execute("SELECT id FROM tasks WHERE planned_for = ?",
                                    (DATE,)).fetchall())
        finally:
            c.close()

    def _task_snapshot(self):
        c = self._conn()
        try:
            return (sorted((r["id"], r["planned_for"], r["plan_order"], r["status"])
                           for r in c.execute(
                               "SELECT id, planned_for, plan_order, status FROM tasks")),
                    c.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
        finally:
            c.close()

    def test_morning_commits_the_plan_when_the_day_is_empty(self):
        self.assertEqual(self._plan(), [])
        p = brief.compose("morning", DATE)
        self.assertTrue(p["next"]["plan_committed"])
        planned = self._plan()
        self.assertTrue(0 < len(planned) <= 3)
        self.assertTrue({t["id"] for t in p["next"]["tasks"]} <= set(planned))
        for t in p["next"]["tasks"]:
            self.assertTrue(t["why"])

    def test_morning_never_touches_an_existing_plan(self):
        c = self._conn()
        c.execute("UPDATE tasks SET planned_for = ?, plan_order = 0 WHERE id = ?",
                  (DATE, "t_brief_quiet_0"))
        c.commit()
        c.close()
        before = self._task_snapshot()
        p = brief.compose("morning", DATE)
        self.assertFalse(p["next"]["plan_committed"])
        self.assertEqual(self._plan(), ["t_brief_quiet_0"])
        self.assertEqual(self._task_snapshot(), before)

    def test_midday_and_evening_never_write(self):
        """Scoped to tasks + task_events: those are what the plan-commit rule
        protects. (canvas.get_day_plan's own session_events hygiene is shared
        with the Today tab and is not the brief's write.)"""
        for slot in ("midday", "evening"):
            before = self._task_snapshot()
            brief.compose(slot, DATE)
            self.assertEqual(self._task_snapshot(), before, slot)
        self.assertEqual(self._plan(), [])


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class Renderer(_BriefCase):
    """The renderer is a pure function of the payload, and the cap is hard."""

    def setUp(self):
        super().setUp()
        self._seed_noise()

    def test_never_exceeds_twelve_lines_on_a_maximally_noisy_day(self):
        for slot in brief.SLOTS:
            text = brief.render_telegram(brief.compose(slot, DATE))
            lines = text.split("\n")
            self.assertLessEqual(len(lines), brief.MAX_LINES, f"{slot}:\n{text}")
            for line in lines:
                self.assertTrue(line.strip(), f"blank line in {slot}")

    def test_block_order_is_fixed_and_every_block_is_present(self):
        for slot in brief.SLOTS:
            text = brief.render_telegram(brief.compose(slot, DATE))
            positions = []
            for key in brief.BLOCK_ORDER:
                label = brief.LABELS[key]
                self.assertIn(label, text, f"{slot} missing {label}")
                positions.append(text.index(label))
            self.assertEqual(positions, sorted(positions), f"{slot} block order drifted")

    def test_close_leads_with_the_named_done_list(self):
        payload = brief.compose("evening", DATE)
        lines = brief.render_telegram(payload).split("\n")
        self.assertIn("Close", lines[0])
        self.assertIn("this week", lines[0])
        self.assertIn(f"{payload['delivery']['done_count']} done", lines[0])
        self.assertTrue(lines[1].startswith("✅"), lines[1])
        # A NAMED list, not just a count.
        self.assertIn(payload["delivery"]["done"][0]["title"][:12], lines[1])
        # ...and it leads: the DONE line precedes every block.
        self.assertLess(1, min(lines.index(l) for l in lines
                               if l.startswith(brief.LABELS["needs_you"])))

    def test_render_is_pure_and_needs_no_database(self):
        payload = brief.compose("midday", DATE)
        _db.KANBAN_DB = Path("/nonexistent/brief-renderer-must-not-read-this.db")
        try:
            a = brief.render_telegram(payload)
            b = brief.render_telegram(payload)
        finally:
            _db.KANBAN_DB = self.tmp
        self.assertEqual(a, b)
        self.assertTrue(a)

    def test_truncation_drops_the_lowest_emphasis_block_first(self):
        """A payload too big for the cap loses items from the slot's least
        weighted block, never from the one the slot exists to show."""
        payload = brief.compose("morning", DATE)
        payload["agents"]["failed"] = [
            {"id": f"r{i}", "task_id": f"t{i}", "title": f"Failed run {i}",
             "status": "crashed", "outcome": "crashed"} for i in range(15)
        ]
        payload["next"]["tasks"] = [
            {"id": f"n{i}", "title": f"Next thing {i}", "why": "cycle"} for i in range(3)
        ]
        text = brief.render_telegram(payload)
        self.assertLessEqual(len(text.split("\n")), brief.MAX_LINES)
        # morning weights ➡️ Next highest and 🤖 Agents lowest.
        for i in range(3):
            self.assertIn(f"Next thing {i}", text)
        self.assertNotIn("Failed run 14", text)

    def test_close_keeps_the_commercial_line_when_the_cap_trims_delivery(self):
        c = self._conn()
        c.execute("UPDATE tasks SET deal_id = ? WHERE id = ?",
                  ("deal_brief_moved_0", self.done_ids[0]))
        c.commit()
        c.close()

        text = brief.render_telegram(brief.compose("evening", DATE))
        self.assertLessEqual(len(text.split("\n")), brief.MAX_LINES)
        commercial = next(line for line in text.splitlines()
                          if line.startswith("• Hoy moviste "))
        self.assertIn("Brief Account (1)", commercial)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class DeepLinks(_BriefCase):
    """Spec §3 Actionability: "Every line naming an entity carries
    https://<dash>/?entity=task:t_xxx … Never an id to copy, never 'check the
    dashboard.'"

    Before this the renderer built every line from `_clip(title)` alone: the
    payload carried the ids, the message did not, and `_clip` had usually eaten
    the end of the title too — so the tap count from a Telegram line to the
    entity was unbounded (switch app, search by a truncated name).
    """

    # (block, list key) → the entity type its deep link must name.
    LINKABLE = (
        ("needs_you", "blocked", "task", "id"),
        ("needs_you", "orphan_won_deals", "deal", "id"),
        ("money", "moved", "deal", "id"),
        ("money", "stale_over_14d", "deal", "id"),
        ("delivery", "projects_moved", "project", "id"),
        # A failed RUN links to its task: the run has no drawer, and the task is
        # where the action lives.
        ("agents", "failed", "task", "task_id"),
        ("next", "tasks", "task", "id"),
    )

    def _payload(self, **over):
        p = {
            "date": DATE, "slot": "midday", "since_ts": self.midnight,
            "needs_you": {
                "blocked": [{"id": "t_link_blocked", "title": "Blocked thing"}],
                "blocked_count": 1, "review_count": 0,
                "orphan_won_deals": [{"id": "deal_link_orphan", "title": "Won thing",
                                      "value": 1000}],
                "count": 2},
            "money": {
                "moved": [{"id": "deal_link_moved", "title": "Moved thing",
                           "from": "lead", "to": "won"}],
                "stale_over_14d": [{"id": "deal_link_cold", "title": "Cold thing",
                                    "days": 30}],
                "pipeline_open_value": 1000.0, "touch_alerts": {}},
            "delivery": {
                "projects_moved": [{"id": "proj_link_moved", "name": "Loud", "done_today": 2}],
                "projects_quiet": [], "done": [], "done_count": 0, "done_week": 0},
            "agents": {
                "running": 0, "finished": [],
                "failed": [{"id": 9, "task_id": "t_link_failed", "title": "Failed thing",
                            "status": "crashed", "outcome": "crashed"}],
                "dispatches": []},
            "next": {"tasks": [{"id": "t_link_next", "title": "Next thing",
                                "why": "planned"}], "plan_committed": False},
        }
        for block, value in over.items():
            p[block].update(value)
        return p

    def test_every_payload_item_with_an_id_renders_its_deep_link(self):
        """Asserted on the block BUILDERS, so the contract is about the lines the
        renderer makes rather than about which of them survive the 12-line cap
        (the cap is asserted separately, below)."""
        payload = self._payload()
        base = brief.dispatch._dashboard_url()
        for block, key, kind, id_field in self.LINKABLE:
            _, _, items = brief._BUILDERS[block](payload)
            for item in payload[block][key]:
                want = f"{base}/?entity={kind}:{item[id_field]}"
                hit = [line for line in items if want in line]
                self.assertEqual(len(hit), 1,
                                 f"{block}.{key} → no line carrying {want}: {items}")
                # The link RIDES on the item's own line — it never becomes a
                # line of its own, which is what keeps the cap untouched.
                self.assertTrue(hit[0].startswith(hit[0].split(" — ")[0]))

    def test_the_rendered_message_carries_the_links_and_still_fits_the_cap(self):
        # Six item lines + five labels + the title = exactly MAX_LINES, so
        # nothing is trimmed and every rendered item can be checked.
        payload = self._payload(money={"stale_over_14d": []})
        text = brief.render_telegram(payload)
        lines = text.split("\n")
        self.assertLessEqual(len(lines), brief.MAX_LINES, text)
        items = [l for l in lines if l.startswith("• ")]
        self.assertEqual(len(items), 6, text)
        for line in items:
            self.assertIn("?entity=", line, f"unlinked item line: {line}")
        for wanted in ("task:t_link_blocked", "deal:deal_link_orphan", "deal:deal_link_moved",
                       "project:proj_link_moved", "task:t_link_failed", "task:t_link_next"):
            self.assertIn(f"?entity={wanted}", text)

    def test_the_cap_still_holds_on_a_maximally_noisy_real_day(self):
        """Links lengthen lines; they must not add any. Same seeded noise the
        Renderer suite uses, re-asserted here because THIS change is the one
        that could have broken it."""
        self._seed_noise()
        for slot in brief.SLOTS:
            text = brief.render_telegram(brief.compose(slot, DATE))
            self.assertLessEqual(len(text.split("\n")), brief.MAX_LINES, f"{slot}:\n{text}")

    def test_an_item_without_an_id_is_not_linked_to_nothing(self):
        payload = self._payload(next={"tasks": [{"id": None, "title": "Idless thing",
                                                 "why": "planned"}]})
        _, _, items = brief._next_block(payload)
        self.assertEqual(items, ["Idless thing (planned)"])

    def test_the_base_url_is_the_dispatch_module_s_single_source(self):
        prior = os.environ.get("DASHBOARD_URL")
        os.environ["DASHBOARD_URL"] = "https://dash.example/"
        try:
            self.assertEqual(brief.entity_link("task", "t_x"),
                             "https://dash.example/?entity=task:t_x")
            self.assertTrue(brief.render_telegram(self._payload())
                            .count("https://dash.example/?entity=") >= 5)
        finally:
            if prior is None:
                os.environ.pop("DASHBOARD_URL", None)
            else:
                os.environ["DASHBOARD_URL"] = prior


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class RunningIsBounded(_BriefCase):
    """🤖 Agents · N running must mean N runs that are actually running.

    `status = 'running' AND ended_at IS NULL` is a claim the row makes about
    itself forever. On the live DB it counted five runs abandoned 18–19 days
    earlier, and that number went out in every brief, three times a day, in the
    one instrument whose whole purpose is deciding what is real.
    """

    empty = True

    def _run(self, task_id, **cols):
        c = self._conn()
        try:
            cols.setdefault("status", "running")
            cols["task_id"] = task_id
            names = ",".join(cols)
            c.execute(f"INSERT INTO task_runs ({names}) "
                      f"VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))
            c.commit()
        finally:
            c.close()

    def _running(self):
        conn = _db.get_conn()
        try:
            return brief.compose_agents(conn, self.midnight)["running"]
        finally:
            conn.close()

    def test_an_eighteen_day_old_running_row_counts_zero(self):
        now = int(time.time())
        old = now - 18 * 86400
        # Exactly the shape of the five live rows: started and (sometimes)
        # heartbeat 18 days ago, a claim lease that expired the same day.
        self._run("t_stale_no_beat", started_at=old, claim_expires=old + 900)
        self._run("t_stale_beat", started_at=old, last_heartbeat_at=old + 80,
                  claim_expires=old + 1800)
        self.assertEqual(self._running(), 0)

    def test_a_fresh_run_counts_one(self):
        now = int(time.time())
        self._run("t_fresh", started_at=now - 30, last_heartbeat_at=now - 5)
        self.assertEqual(self._running(), 1)
        # ...and the stale ones beside it change nothing.
        self._run("t_stale", started_at=now - 18 * 86400)
        self.assertEqual(self._running(), 1)

    def test_a_live_claim_lease_counts_even_without_a_heartbeat(self):
        """A run that holds an unexpired lease is running by the system's own
        bookkeeping, heartbeat or not — the lease is the stronger signal."""
        now = int(time.time())
        self._run("t_leased", started_at=now - 6 * 3600, claim_expires=now + 600)
        self.assertEqual(self._running(), 1)

    def test_an_ended_run_is_never_running_whatever_its_status_says(self):
        now = int(time.time())
        self._run("t_ended", started_at=now - 60, last_heartbeat_at=now,
                  ended_at=now - 10)
        self.assertEqual(self._running(), 0)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class ForceRecompose(_BriefCase):
    """`?force=1` — the recovery route, and the ONLY thing that overwrites a row.

    2026-07-29: the morning brief composed at 06:01, eleven minutes before
    m02_spine added `deals.project_id`. The forward-schema guard fired, the
    payload froze with `orphan_won_deals: []`, and four won deals worth $194,500
    were invisible on every surface reading the stored payload for the whole day
    — recoverable only by hand-written SQL.
    """

    def setUp(self):
        super().setUp()
        self._seed_noise()

    def _rows(self):
        c = self._conn()
        try:
            return c.execute("SELECT COUNT(*) FROM brief_runs WHERE date = ?",
                             (DATE,)).fetchone()[0]
        finally:
            c.close()

    def _block_one_more(self, tid):
        c = self._conn()
        try:
            self._task(c, tid, f"Newly blocked {tid}", "blocked")
            c.commit()
        finally:
            c.close()

    def test_without_force_the_stored_row_wins_even_when_reality_moved(self):
        first = brief.get_or_compose("midday", DATE)
        self._block_one_more("t_force_control")
        second = brief.get_or_compose("midday", DATE)
        self.assertTrue(second["already_composed"])
        self.assertEqual(second["payload"], first["payload"])
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(self._rows(), 1)

    def test_force_overwrites_the_row_with_a_fresh_composition(self):
        first = brief.get_or_compose("midday", DATE)
        self.assertNotIn("t_force_new", [b["id"] for b in first["payload"]["needs_you"]["blocked"]])
        self._block_one_more("t_force_new")

        forced = brief.get_or_compose("midday", DATE, force=True)
        self.assertFalse(forced["already_composed"])
        self.assertIn("t_force_new",
                      [b["id"] for b in forced["payload"]["needs_you"]["blocked"]])
        self.assertNotEqual(forced["rendered_md"], first["rendered_md"])
        # OVERWRITE, not a second row: (date, slot) is still the key.
        self.assertEqual(self._rows(), 1)
        self.assertGreaterEqual(forced["created_at"], first["created_at"])
        # ...and the read after it is the forced one.
        self.assertEqual(brief.get_or_compose("midday", DATE)["payload"],
                         forced["payload"])

    def test_force_clears_sent_so_the_repaired_brief_can_go_out(self):
        brief.get_or_compose("morning", DATE)
        brief.mark_sent("morning", DATE)
        self.assertTrue(brief.get_or_compose("morning", DATE)["sent"])
        forced = brief.get_or_compose("morning", DATE, force=True)
        self.assertFalse(forced["sent"])
        self.assertIsNone(forced["sent_at"])
        self.assertIsNone(forced["acknowledged_at"])

    def test_a_forced_recompose_is_not_its_own_since_horizon(self):
        """The row being replaced must not become the "since your last brief"
        boundary, or the repaired brief reports the window since the broken one
        it exists to replace — i.e. nothing."""
        first = brief.get_or_compose("midday", DATE)
        forced = brief.get_or_compose("midday", DATE, force=True)
        self.assertEqual(forced["payload"]["since_ts"], first["payload"]["since_ts"])
        self.assertEqual([d["id"] for d in forced["payload"]["money"]["moved"]],
                         [d["id"] for d in first["payload"]["money"]["moved"]])

    def test_force_still_rejects_a_bad_slot_or_date(self):
        with self.assertRaises(ValueError):
            brief.get_or_compose("noon", DATE, force=True)
        with self.assertRaises(ValueError):
            brief.get_or_compose("morning", "15-07-2026", force=True)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class OrphanWonDealsEndpoint(_BriefCase):
    """The ⚠️ block's orphan rows are a LIVE read on the web, not a mirror of
    the stored brief — same SQL as the composer, different freshness."""

    def setUp(self):
        super().setUp()
        self._seed_noise()
        c = self._conn()
        try:
            for i, value in enumerate((9000, 3000)):
                c.execute("INSERT INTO deals (id, account_id, title, stage, value, created_at) "
                          "VALUES (?,?,?,'won',?,?)",
                          (f"deal_orphan_{i}", "acct_brief", f"Orphan won {i}", value,
                           self.midnight))
            c.execute("INSERT INTO deals (id, account_id, title, stage, value, project_id, "
                      "created_at) VALUES (?,?,?,'won',?,?,?)",
                      ("deal_delivered_0", "acct_brief", "Already delivered", 1000,
                       "proj_brief_loud", self.midnight))
            c.commit()
        finally:
            c.close()

    def test_the_endpoint_and_the_composer_answer_from_the_same_query(self):
        live = brief.orphan_won_deals_now()
        ids = [d["id"] for d in live]
        self.assertIn("deal_orphan_0", ids)
        self.assertIn("deal_orphan_1", ids)
        self.assertNotIn("deal_delivered_0", ids, "a delivered deal is not an orphan")
        # Highest value first — the list is triaged, not arbitrary. (Asserted as
        # a RELATIVE order: the live copy carries orphans of its own, and this
        # contract is about the ranking, not about beating them.)
        self.assertLess(ids.index("deal_orphan_0"), ids.index("deal_orphan_1"))
        self.assertEqual([d["value"] for d in live], sorted((d["value"] for d in live),
                                                            reverse=True))
        composed = brief.compose("midday", DATE)["needs_you"]["orphan_won_deals"]
        self.assertEqual([d["id"] for d in composed], ids)

    def test_delivering_a_deal_clears_it_from_the_live_read_immediately(self):
        """The brief's copy stays frozen until the next slot; this one must not."""
        stored = brief.get_or_compose("midday", DATE)
        self.assertIn("deal_orphan_0",
                      [d["id"] for d in stored["payload"]["needs_you"]["orphan_won_deals"]])
        c = self._conn()
        try:
            c.execute("UPDATE deals SET project_id = 'proj_brief_loud' WHERE id = ?",
                      ("deal_orphan_0",))
            c.commit()
        finally:
            c.close()
        self.assertNotIn("deal_orphan_0", [d["id"] for d in brief.orphan_won_deals_now()])
        # ...while the stored brief is unchanged, which is exactly why the web
        # read had to stop being a mirror of it.
        self.assertIn("deal_orphan_0",
                      [d["id"] for d in brief.get_or_compose("midday", DATE)
                       ["payload"]["needs_you"]["orphan_won_deals"]])


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class EmptyDay(_BriefCase):
    """Nothing happened. The skeleton still arrives, in em-dashes."""

    empty = True

    def test_five_blocks_on_an_empty_database(self):
        p = brief.compose("morning", DATE)
        for key in ("needs_you", "money", "delivery", "agents", "next"):
            self.assertIn(key, p, key)
        self.assertEqual(p["needs_you"]["count"], 0)
        self.assertEqual(p["needs_you"]["blocked"], [])
        self.assertEqual(p["money"]["moved"], [])
        self.assertEqual(p["money"]["pipeline_open_value"], 0.0)
        self.assertEqual(p["delivery"]["projects_moved"], [])
        self.assertEqual(p["delivery"]["done"], [])
        self.assertEqual(p["agents"], {"running": 0, "finished": [], "failed": [],
                                       "dispatches": []})
        self.assertEqual(p["next"]["tasks"], [])
        # Nothing to commit → nothing committed. A forward default still needs
        # something to be forward ABOUT.
        self.assertFalse(p["next"]["plan_committed"])

    def test_every_block_renders_exactly_one_em_dash_line(self):
        text = brief.render_telegram(brief.compose("morning", DATE))
        lines = text.split("\n")
        self.assertEqual(len(lines), 1 + len(brief.BLOCK_ORDER))
        for key, line in zip(brief.BLOCK_ORDER, lines[1:]):
            self.assertEqual(line, f"{brief.LABELS[key]} {brief.EMPTY}")

    def test_close_says_so_instead_of_faking_momentum(self):
        text = brief.render_telegram(brief.compose("evening", DATE))
        self.assertIn(brief.EMPTY, text.split("\n")[1])
        self.assertLessEqual(len(text.split("\n")), brief.MAX_LINES)


# The routes, crossed for real: a live FastAPI app over the tmp DB, through the
# auth middleware, in its own process (Tier 1c — a composer suite that never
# touches the app is a statement about the composer, not about the endpoint).
_ROUTE_SCRIPT = r"""
import json, sys
from fastapi.testclient import TestClient
from dashboard import api

c = TestClient(api.app)
date = sys.argv[1]
out = {"paths": sorted(r.path for r in api.app.routes if "/api/brief" in getattr(r, "path", ""))}
r1 = c.post(f"/api/brief/morning?date={date}")
out["first"] = [r1.status_code, r1.json().get("already_composed"), bool(r1.json().get("rendered_md"))]
r2 = c.post(f"/api/brief/morning?date={date}")
out["second"] = [r2.status_code, r2.json().get("already_composed"),
                 r2.json().get("rendered_md") == r1.json().get("rendered_md")]
out["bad_slot"] = c.post(f"/api/brief/noon?date={date}").status_code
s1 = c.post(f"/api/brief/morning/sent?date={date}")
out["sent"] = [s1.status_code, bool(s1.json().get("sent_at"))]
out["sent_404"] = c.post(f"/api/brief/evening/sent?date={date}").status_code
lt = c.get("/api/brief/latest")
out["latest"] = [lt.status_code, lt.json().get("slot"), lt.json().get("sent")]
# The recovery route, LAST (it clears sent_at, which the reads above assert on).
r3 = c.post(f"/api/brief/morning?date={date}&force=1")
out["forced"] = [r3.status_code, r3.json().get("already_composed"), r3.json().get("sent")]
ow = c.get("/api/crm/deals/orphan-won")
out["orphan_won"] = [ow.status_code, sorted(ow.json().keys()),
                     all(sorted(d) == ["id", "title", "value"] for d in ow.json()["deals"])]
print(json.dumps(out))
"""


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class Routes(_BriefCase):
    def setUp(self):
        super().setUp()
        self._seed_noise()

    def test_routes_round_trip_through_the_real_app(self):
        env = dict(os.environ)
        env.update({"HERMES_KANBAN_DB": str(self.tmp), "TESTING": "1",
                    "HERMES_DASHBOARD_TOKEN": "", "ORCH_DIR": str(self.orch_dir)})
        proc = subprocess.run([sys.executable, "-c", _ROUTE_SCRIPT, DATE],
                              cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["paths"], ["/api/brief/latest", "/api/brief/{slot}",
                                        "/api/brief/{slot}/sent"])
        # ?force=1 recomposes and clears sent; the orphan read is live and typed.
        self.assertEqual(out["forced"], [200, False, False])
        self.assertEqual(out["orphan_won"], [200, ["count", "deals"], True])
        self.assertEqual(out["first"], [200, False, True])
        self.assertEqual(out["second"], [200, True, True])
        self.assertEqual(out["bad_slot"], 400)
        self.assertEqual(out["sent"], [200, True])
        self.assertEqual(out["sent_404"], 404)
        self.assertEqual(out["latest"], [200, "morning", True])


class SelectIntentsSurface(_BriefCase):
    """The proactive layer's SELECT intents reach the human, carrying their age.

    Regression cover for the 2026-08-10 incident: the watcher enqueued
    load-bearing intents that nothing ever read, and because a re-fired watcher
    is idempotent while an item is open, a three-week-old signal looked new
    every Monday. Both halves are asserted here — that the intents appear at
    all, and that their true age travels with them.
    """

    def _write_queue(self, rows):
        with open(self.intent_dir / "intent-queue.jsonl", "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    @staticmethod
    def _iso(days_ago):
        return (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_open_select_intents_surface_with_age_and_chronic_flag(self):
        # Deliberately written NEWEST-first: if the composer's sort is removed,
        # file order alone would still have satisfied an oldest-first fixture.
        self._write_queue([
            {"id": "iq-fresh", "ts": self._iso(1), "kind": "velocity-gap",
             "load_bearing": True, "status": "open", "intent": "cycle delivery-rate 40%"},
            {"id": "iq-chronic", "ts": self._iso(22), "kind": "scorecard-miss",
             "load_bearing": True, "status": "open", "intent": "5 weekly KPIs below target"},
        ])
        got = brief.open_select_intents()
        self.assertEqual([i["id"] for i in got], ["iq-chronic", "iq-fresh"])  # oldest first
        self.assertEqual(got[0]["age_days"], 22)
        self.assertTrue(got[0]["chronic"])
        self.assertFalse(got[1]["chronic"])

    def test_auto_dismissed_and_resolved_intents_never_reach_the_human_block(self):
        self._write_queue([
            {"id": "iq-auto", "ts": self._iso(30), "load_bearing": False,
             "status": "open", "intent": "internal idempotent thing"},
            {"id": "iq-done", "ts": self._iso(30), "load_bearing": True,
             "status": "resolved", "intent": "already handled"},
            {"id": "iq-no", "ts": self._iso(30), "load_bearing": True,
             "status": "dismissed", "intent": "operator already said no"},
        ])
        self.assertEqual(brief.open_select_intents(), [])

    def test_missing_queue_degrades_to_empty_not_an_exception(self):
        self.assertEqual(brief.open_select_intents(), [])   # no file written at all

    def test_chronic_is_inclusive_at_the_boundary(self):
        """Exactly at the threshold counts as chronic — an off-by-one here is a
        signal that has been ignored for two weeks rendering as routine."""
        self.assertEqual(brief._INTENT_CHRONIC_DAYS, 14)   # the policy, pinned
        self._write_queue([
            {"id": "iq-edge", "ts": self._iso(14), "load_bearing": True,
             "status": "open", "intent": "exactly at the line"},
            {"id": "iq-under", "ts": self._iso(13), "load_bearing": True,
             "status": "open", "intent": "one day under"},
        ])
        got = {i["id"]: i for i in brief.open_select_intents()}
        self.assertTrue(got["iq-edge"]["chronic"])
        self.assertFalse(got["iq-under"]["chronic"])

    def test_naive_and_unparsable_timestamps_do_not_break_or_lie(self):
        """A naive stamp is read as UTC (not silently shifted); an unparsable one
        yields age None rather than a fabricated number, and still surfaces."""
        naive = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%S")   # no Z
        self._write_queue([
            {"id": "iq-naive", "ts": naive, "load_bearing": True,
             "status": "open", "intent": "naive stamp"},
            {"id": "iq-bad", "ts": "not-a-timestamp", "load_bearing": True,
             "status": "open", "intent": "unparsable stamp"},
        ])
        got = {i["id"]: i for i in brief.open_select_intents()}
        self.assertEqual(got["iq-naive"]["age_days"], 9)
        self.assertFalse(got["iq-naive"]["chronic"])
        self.assertIsNone(got["iq-bad"]["age_days"])
        self.assertFalse(got["iq-bad"]["chronic"])
        self.assertEqual(len(got), 2)   # neither is dropped

    def test_same_day_intent_reports_zero_days_not_one(self):
        self._write_queue([{"id": "iq-now", "ts": self._iso(0), "load_bearing": True,
                            "status": "open", "intent": "just tripped"}])
        self.assertEqual(brief.open_select_intents()[0]["age_days"], 0)

    def test_unknown_age_sorts_last_and_ties_break_deterministically(self):
        """An intent whose stamp cannot be read must never outrank one whose age
        is known and large; equal ages order by id so the brief is stable."""
        self._write_queue([
            {"id": "iq-bbb", "ts": self._iso(7), "load_bearing": True, "status": "open", "intent": "b"},
            {"id": "iq-unknown", "ts": "garbage", "load_bearing": True, "status": "open", "intent": "u"},
            {"id": "iq-aaa", "ts": self._iso(7), "load_bearing": True, "status": "open", "intent": "a"},
        ])
        self.assertEqual([i["id"] for i in brief.open_select_intents()],
                         ["iq-aaa", "iq-bbb", "iq-unknown"])

    def test_kind_survives_to_the_payload(self):
        self._write_queue([{"id": "iq-k", "ts": self._iso(2), "kind": "velocity-gap",
                            "load_bearing": True, "status": "open", "intent": "x"}])
        self.assertEqual(brief.open_select_intents()[0]["kind"], "velocity-gap")

    def test_intents_add_to_the_block_count_exactly_once_each(self):
        """The header number must equal what the block is actually reporting: a
        sign error here understates the very queue this surfacing exists for."""
        self._write_queue([])
        base = brief.compose("evening", DATE, replacing=True)["needs_you"]["count"]
        self._write_queue([
            {"id": "iq-a", "ts": self._iso(3), "load_bearing": True, "status": "open", "intent": "a"},
            {"id": "iq-b", "ts": self._iso(2), "load_bearing": True, "status": "open", "intent": "b"},
        ])
        n = brief.compose("evening", DATE, replacing=True)["needs_you"]
        self.assertEqual(n["count"], base + 2)
        # Pin the whole arithmetic, not just the delta: every term adds.
        self.assertEqual(
            n["count"],
            n["blocked_count"] + n["review_count"] + len(n["orphan_won_deals"]) + len(n["intents"]))

    def test_a_single_open_intent_takes_exactly_one_line(self):
        """One pending decision needs one bullet. The named-list widening below
        is for a BACKLOG of decisions, not for every lone intent."""
        self._write_queue([
            {"id": "iq-only", "ts": self._iso(9), "load_bearing": True,
             "status": "open", "intent": "SOLE unresolved decision"},
        ])
        text = brief.render_telegram(brief.compose("morning", DATE, replacing=True))
        self.assertIn("1 intent(s) to decide", text)
        self.assertIn("SOLE unresolved decision", text)

    def test_a_backlog_of_intents_names_up_to_three_oldest_first(self):
        """Superseded 2026-08-16: this used to assert that exactly ONE intent
        earned a bullet however many were open. That held while one decision was
        pending at a time, and broke the moment a batch arrived — five SELECTs
        deferred on 2026-08-09 rendered as a single bullet plus a count, which
        reads as "one thing to decide" and let the other four dissolve. The
        widening is capped at three and stays oldest-first, so the block still
        has room for blocked work and unlinked won deals."""
        self._write_queue([
            {"id": "iq-old", "ts": self._iso(30), "load_bearing": True,
             "status": "open", "intent": "OLDEST unresolved decision"},
            {"id": "iq-mid", "ts": self._iso(5), "load_bearing": True,
             "status": "open", "intent": "MIDDLE decision"},
            {"id": "iq-new", "ts": self._iso(1), "load_bearing": True,
             "status": "open", "intent": "NEWEST decision"},
        ])
        text = brief.render_telegram(brief.compose("morning", DATE, replacing=True))
        self.assertIn("3 intent(s) to decide", text)
        self.assertIn("OLDEST unresolved decision", text)
        self.assertIn("MIDDLE decision", text)
        self.assertIn("NEWEST decision", text)
        # Oldest first — the age is the whole reason these are surfaced.
        self.assertLess(text.index("OLDEST unresolved decision"),
                        text.index("MIDDLE decision"))
        self.assertLess(text.index("MIDDLE decision"), text.index("NEWEST decision"))

    def test_the_named_intent_list_is_capped_at_three(self):
        """The cap is the load-bearing half of the widening: without it, a queue
        that grows makes the needs-you block all intents, and the one block that
        must never be skimmed becomes the one that trains skimming."""
        self._write_queue([
            {"id": f"iq-{n}", "ts": self._iso(40 - n), "load_bearing": True,
             "status": "open", "intent": f"DECISION NUMBER {n}"}
            for n in range(6)
        ])
        text = brief.render_telegram(brief.compose("morning", DATE, replacing=True))
        self.assertIn("6 intent(s) to decide", text)
        named = [n for n in range(6) if f"DECISION NUMBER {n}" in text]
        self.assertEqual(len(named), 3, f"expected 3 named, got {named}")
        # And they are the three oldest (highest ts age → lowest n here).
        self.assertEqual(named, [0, 1, 2])

    def test_payload_and_render_carry_the_intents(self):
        self._write_queue([
            {"id": "iq-chronic", "ts": self._iso(22), "kind": "scorecard-miss",
             "load_bearing": True, "status": "open", "intent": "5 weekly KPIs below target"},
        ])
        p = brief.compose("evening", DATE, replacing=True)
        n = p["needs_you"]
        self.assertIn("intents", n)
        self.assertEqual([i["id"] for i in n["intents"]], ["iq-chronic"])
        # The intent is counted, so the block header cannot report a smaller
        # number than the lines beneath it.
        self.assertGreaterEqual(n["count"], 1)
        text = brief.render_telegram(p)
        self.assertIn("1 intent(s) to decide", text)
        self.assertIn("5 weekly KPIs below target", text)
        self.assertIn("22d", text)


class MorningPlanExcludesBlockedWork(_BriefCase):
    """The morning slot must never commit BLOCKED work as one of the day's three.

    Observed live 2026-08-11: "Revisar entregables de D4 del cliente" was reported in
    the ⚠️ Needs you block as blocked AND committed as the day's #1 — the plan
    disagreeing with itself. `plan_candidates` excluded `review` (someone else's
    turn) from all four candidate sources but never `blocked` (waiting on an
    unblock), so work the operator provably cannot do outranked work he can.
    """

    empty = True

    def setUp(self):
        super().setUp()
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                  ("proj_blk", "blk", "Blocked Probe", self.midnight))
        # Both overdue, so both qualify on every axis EXCEPT status. The blocked
        # one is the more overdue and the higher priority, so if it is eligible
        # at all it wins the ordering — which is exactly what went wrong live.
        self._task(c, "t_blocked_probe", "Blocked and overdue", "blocked",
                   project="proj_blk", due_date="2026-07-01", priority=3)
        self._task(c, "t_todo_probe", "Workable and overdue", "todo",
                   project="proj_blk", due_date="2026-07-02", priority=1)
        c.commit()
        c.close()

    def test_blocked_work_is_never_a_plan_candidate(self):
        from dashboard import canvas
        ids = {c["id"] for c in (canvas.plan_candidates(DATE).get("candidates") or [])}
        self.assertIn("t_todo_probe", ids)
        self.assertNotIn("t_blocked_probe", ids)

    def test_a_task_blocked_AFTER_planning_drops_out_of_next(self):
        """The other half: excluding blocked work from CANDIDATES only governs
        what gets proposed. A task planned while workable and blocked an hour
        later is still in today's plan, and `compose_next` filtered only
        done/rejected — so live on 2026-08-11 a blocked task rendered as the
        day's #1 (its event trail: planned -> blocked 54s later -> planned
        again). Your three things must be three things you can actually do.
        """
        c = self._conn()
        c.execute("UPDATE tasks SET planned_for = ?, plan_order = 1 WHERE id = ?",
                  (DATE, "t_blocked_probe"))
        c.execute("UPDATE tasks SET planned_for = ?, plan_order = 2 WHERE id = ?",
                  (DATE, "t_todo_probe"))
        c.commit()
        c.close()
        p = brief.compose("morning", DATE)
        # A plan already exists, so nothing is committed — this is the RENDER path.
        self.assertFalse(p["next"]["plan_committed"])
        planned = {t["id"] for t in p["next"]["tasks"]}
        self.assertIn("t_todo_probe", planned)
        self.assertNotIn("t_blocked_probe", planned)
        self.assertEqual(p["needs_you"]["blocked_count"], 1)

    def test_morning_commits_workable_work_and_still_reports_the_blocked_one(self):
        p = brief.compose("morning", DATE)
        planned = {t["id"] for t in p["next"]["tasks"]}
        self.assertTrue(p["next"]["plan_committed"])
        self.assertIn("t_todo_probe", planned)
        self.assertNotIn("t_blocked_probe", planned)
        # Excluding it from the PLAN must not hide it: it is still the thing
        # that needs you, counted in its own block.
        self.assertEqual(p["needs_you"]["blocked_count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
