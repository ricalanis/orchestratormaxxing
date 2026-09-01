"""Contract for the project lifecycle — fase 1, step 1 (the data floor).

`projects.status` shipped in m02_spine and then sat NULL on 13 of the 18 live
rows, because the only thing that ever wrote it was m03's initiative fold (5
rows) and one conversion verb nobody had pressed. A lifecycle column that is
NULL for most of the table is not a lifecycle: every reader has to invent a
meaning for the absence, and the four of them invent different ones.

Three properties, and each is a section below.

  1. **The floor is filled, from EVIDENCE.** m04 gives every non-archived
     project a status derived from its own tasks — unsettled work → `active`,
     every task settled → `delivered`, no tasks at all → `planned`. Evidence,
     not a default: a blanket `planned` would claim a delivered project is about
     to start. Fabricated fixtures cover all three branches, so the rule is
     asserted rather than the current live shape (which would go vacuous the
     moment the migration lands on the operator's DB).
  2. **One writer owns the column.** `sprints.set_project_status` validates the
     vocabulary, stamps `delivered_at` once, and — ruling 8 — RECEIVES the
     caller's connection instead of opening its own, so a status change can ride
     inside the transaction that caused it. That last part is asserted the only
     way it can be: the write must be invisible to a second connection until the
     CALLER commits, and must vanish on the caller's rollback.
  3. **New rows start somewhere.** `create_project` writes `planned` (it wrote
     NULL, which is how the 13 got there), and the PATCH route routes `status`
     through the writer — so an unknown value is a 400 at the edge instead of an
     arbitrary string in the column.

Red-proofs (Tier-1c — a contract that has never failed against the bug it names
is unfalsified). Measured on the pre-change tree, 2026-08-01:
  * 13 non-archived projects with NULL status on the live DB → §1 red.
  * `create_project` → `status IS NULL` → §3 red.
  * `sprints.set_project_status` / `PROJECT_STATUSES` / the m04 module did not
    exist → §2 red at import.

DB isolation: a COPY of the session sandbox per test, via `db.KANBAN_DB` /
`sprints.KANBAN_DB` — the test_m03_fold.py / test_dispatch.py pattern.
`runner.run_backup` is always stubbed, so no test shells out to
`bin/backup-kanban` or writes into ~/.hermes/backups.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_project_lifecycle.py   # from orchestrator/
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Imported on its own so a syntax error in the migration is a FAILURE and not a
# skip: folding this into the `_READY` guard below would let a broken module
# silently skip every case and exit 0 (m03's rule, same reason).
_MODULE_ERROR = None
try:
    from dashboard.migrations.m04_project_lifecycle import (
        PLANNED, ACTIVE, DELIVERED, TERMINAL_TASK_STATUSES,
        m04_project_lifecycle,
    )
except Exception as exc:  # pragma: no cover - asserted below
    _MODULE_ERROR = exc

_READY = False
_CLIENT = None
try:
    from dashboard import db as _db, sprints as _sprints, orchestration as _orch
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ the per-session sandbox copy tests/conftest.py exports, never the
    # operator's live DB.
    if _REAL_DB.exists():
        runner.run_backup = lambda: None      # before api's import-time run()
        from dashboard.api import app
        from starlette.testclient import TestClient
        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover
    _READY = False

MIGRATION_NAME = "m04_project_lifecycle"


class ModuleImports(unittest.TestCase):
    """Unconditional: the migration must load and be WIRED.

    A migration that exists but is not in `MIGRATIONS` is a file, not a
    migration — it would never run on the live DB and every case below (which
    drives the runner) would still pass by calling it directly."""

    def test_the_migration_module_imports(self):
        self.assertIsNone(_MODULE_ERROR, f"import failed: {_MODULE_ERROR!r}")

    def test_the_migration_is_registered_in_the_runner(self):
        from dashboard.migrations import runner as _runner
        names = [n for n, _ in _runner.MIGRATIONS]
        self.assertIn(MIGRATION_NAME, names, names)
        # After m02 (which ADDS projects.status) and m03 (which writes 5 of
        # them): m04 fills what is left, so it must run last of the three.
        self.assertGreater(names.index(MIGRATION_NAME), names.index("m03_initiatives_fold"))

    def test_the_status_vocabulary_is_the_declared_one(self):
        from dashboard import sprints as _s
        self.assertEqual(_s.PROJECT_STATUSES,
                         ("planned", "active", "delivered", "archived"))
        # `delivering` is deliberately unshipped (decisions log): m02's docstring
        # named it, nothing ever wrote it, and a status no verb produces is a
        # value every reader must branch on for nothing.
        self.assertNotIn("delivering", _s.PROJECT_STATUSES)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _LifecycleCase(unittest.TestCase):
    """A private copy of the session sandbox, with the runner pointed at it."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_m04_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints_db = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        # orchestration.ensure_schema() mkdirs a sidecar dir — keep it out of ~/.hermes.
        self.orch_dir = Path(tempfile.mkdtemp(prefix="orch_m04_test_"))
        self._orig_orch, self._orig_specs = _orch.ORCH_DIR, _orch.SPECS_DIR
        _orch.ORCH_DIR, _orch.SPECS_DIR = self.orch_dir, self.orch_dir / "specs"
        self._orig_backup = runner.run_backup
        self.backup_calls = []
        runner.run_backup = lambda: self.backup_calls.append(1)

    def tearDown(self):
        runner.run_backup = self._orig_backup
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints_db
        _orch.ORCH_DIR, _orch.SPECS_DIR = self._orig_orch, self._orig_specs
        shutil.rmtree(self.orch_dir, ignore_errors=True)
        try:
            self.tmp.unlink()
        except Exception:
            pass

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        conn = sqlite3.connect(str(self.tmp), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _one(self, sql, args=()):
        conn = self._conn()
        try:
            row = conn.execute(sql, args).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _exec(self, sql, args=()):
        conn = self._conn()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def _status(self, project_id):
        return self._one("SELECT status FROM projects WHERE id = ?", (project_id,))

    def _all_statuses(self):
        conn = self._conn()
        try:
            return {r["id"]: r["status"]
                    for r in conn.execute("SELECT id, status FROM projects")}
        finally:
            conn.close()

    def _project(self, pid, *, archived=False, status=None):
        now = int(time.time())
        self._exec(
            "INSERT INTO projects (id, slug, name, created_at, archived_at, status, kind) "
            "VALUES (?,?,?,?,?,?, 'product')",
            (pid, pid, pid, now, now if archived else None, status))

    def _task(self, tid, project_id, status, *, accepted=False):
        """`accepted` stamps `reviewed_at`, and it is not decoration: the canvas
        ensure chain rewrites every `done AND reviewed_at IS NULL` row to
        `review` at EVERY startup (phase 3 item 2 — `done` means accepted). So a
        `done` fixture without it is a `review` fixture by the time any migration
        sees it, which is a real property of this system and not a quirk of the
        seed (see `test_a_finished_but_unaccepted_task_is_still_open_work`)."""
        now = int(time.time())
        self._exec(
            "INSERT INTO tasks (id, title, status, project_id, created_at, reviewed_at) "
            "VALUES (?,?,?,?,?,?)",
            (tid, tid, status, project_id, now, now if accepted else None))

    def _rewind_ledger(self):
        """Un-apply m04's LEDGER row so the runner will really run it.

        Not a data rewind: m04 only ever fills NULLs, so leaving whatever it
        already wrote on the copy is harmless — every case below asserts on rows
        this test fabricated, which are NULL by construction. Without this the
        migration is skipped as already-applied the moment the operator's DB
        (and therefore the session sandbox copied from it) has seen it, and the
        section would go quietly vacuous."""
        self._exec("DELETE FROM orch_migrations WHERE name = ?", (MIGRATION_NAME,))


# ---------------------------------------------------------------- §1 backfill

class Backfill(_LifecycleCase):
    """Every non-archived project ends the migration with a status, and the one
    it gets is the one its own tasks justify."""

    # The three evidence branches, fabricated rather than found: `tasks` is the
    # seed, `want` is the status the rule must derive from it.
    OPEN = "proj_m04_open"          # unsettled work → active
    SETTLED = "proj_m04_settled"    # every task settled → delivered
    EMPTY = "proj_m04_empty"        # no tasks at all → planned
    ARCHIVED = "proj_m04_archived"  # out of scope entirely
    HUMAN = "proj_m04_human"        # already carries a human's status
    UNACCEPTED = "proj_m04_unaccepted"   # its only task is done-but-unaccepted

    def setUp(self):
        super().setUp()
        self._rewind_ledger()
        self._project(self.OPEN)
        self._task("t_m04_open_1", self.OPEN, "in_progress")
        self._task("t_m04_open_2", self.OPEN, "done", accepted=True)
        self._project(self.SETTLED)
        for i, st in enumerate(TERMINAL_TASK_STATUSES):
            self._task(f"t_m04_settled_{i}", self.SETTLED, st, accepted=True)
        self._project(self.UNACCEPTED)
        self._task("t_m04_unaccepted_1", self.UNACCEPTED, "done")   # no reviewed_at
        self._project(self.EMPTY)
        self._project(self.ARCHIVED, archived=True)
        self._project(self.HUMAN, status="planned")
        self._task("t_m04_human_1", self.HUMAN, "in_progress")   # evidence says active

    def test_the_column_is_null_before_the_migration_runs(self):
        """The red-proof anchor. Measured on the live DB 2026-08-01: 13
        non-archived projects with NULL status, which is what m04 exists for.
        Asserted on the FABRICATED rows too, so it stays falsifiable after the
        live DB has been migrated."""
        for pid in (self.OPEN, self.SETTLED, self.EMPTY, self.ARCHIVED):
            self.assertIsNone(self._status(pid), pid)
        self.assertGreaterEqual(
            self._one("SELECT COUNT(*) FROM projects "
                      "WHERE archived_at IS NULL AND status IS NULL"), 3)

    def test_no_live_project_is_left_without_a_status(self):
        out = runner.run()
        self.assertIn(MIGRATION_NAME, out["applied"], out)
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM projects "
                      "WHERE archived_at IS NULL AND status IS NULL"), 0)
        # The ledger row is what stops it from re-running.
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM orch_migrations WHERE name = ?", (MIGRATION_NAME,)), 1)
        # The backup gate covers new migrations, not just the ones it shipped with.
        self.assertTrue(self.backup_calls, "no backup ran before pending versioned DDL")

    def test_unsettled_work_reads_as_active(self):
        runner.run()
        self.assertEqual(self._status(self.OPEN), ACTIVE)

    def test_every_task_settled_reads_as_delivered(self):
        runner.run()
        self.assertEqual(self._status(self.SETTLED), DELIVERED)

    def test_no_tasks_at_all_reads_as_planned(self):
        runner.run()
        self.assertEqual(self._status(self.EMPTY), PLANNED)

    def test_a_finished_but_unaccepted_task_is_still_open_work(self):
        """`done` means ACCEPTED in this schema — the canvas ensure chain moves
        every `done AND reviewed_at IS NULL` row to `review` on each startup. So
        a project whose only task is finished-but-unaccepted still needs the operator,
        and calling it `delivered` would close a project over an open review.
        Found the hard way: the first version of this fixture seeded a bare
        `done` and the migration read the project as `active` — correctly."""
        runner.run()
        self.assertEqual(
            self._one("SELECT status FROM tasks WHERE id = 't_m04_unaccepted_1'"), "review")
        self.assertEqual(self._status(self.UNACCEPTED), ACTIVE)

    def test_a_delivered_backfill_invents_no_delivery_DATE(self):
        """The status is derivable from evidence; the DATE is not. Stamping
        `delivered_at = now` on a project delivered months ago would turn "we do
        not know when" into a specific false claim, and `delivered_at` is what
        the drawer renders."""
        runner.run()
        self.assertIsNone(self._one(
            "SELECT delivered_at FROM projects WHERE id = ?", (self.SETTLED,)))

    def test_an_archived_project_is_out_of_scope(self):
        """`archived_at` already says what an archived row is; the backfill is
        about the rows the UI still renders. Left NULL deliberately — a scope
        this test would notice widening."""
        runner.run()
        self.assertIsNone(self._status(self.ARCHIVED))

    def test_a_status_a_human_set_is_never_overwritten(self):
        """SEEDED, not inferred from today's all-NULL data: a migration with no
        `status IS NULL` guard would pass against a table where nothing has a
        value yet, and destroy work on the day one does. The evidence here says
        `active`; the human said `planned`; the human wins."""
        runner.run()
        self.assertEqual(self._status(self.HUMAN), "planned")

    def test_a_rerun_changes_nothing(self):
        runner.run()
        first = self._all_statuses()
        self._rewind_ledger()
        runner.run()
        self.assertEqual(first, self._all_statuses())


# ------------------------------------------------------------ §2 the writer

class LifecycleWriter(_LifecycleCase):
    """`set_project_status(conn, project_id, status, *, via)` — ruling 8."""

    PID = "proj_m04_writer"

    def setUp(self):
        super().setUp()
        self._project(self.PID, status="active")

    def test_an_unknown_status_is_refused_and_writes_nothing(self):
        conn = _sprints.get_conn()
        try:
            with self.assertRaises(ValueError):
                _sprints.set_project_status(conn, self.PID, "delivering", via="test")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._status(self.PID), "active")

    def test_a_valid_status_lands(self):
        conn = _sprints.get_conn()
        try:
            res = _sprints.set_project_status(conn, self.PID, "delivered", via="test")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._status(self.PID), "delivered")
        self.assertEqual(res["previous"], "active")
        self.assertEqual(res["via"], "test")

    def test_delivering_stamps_a_date_once(self):
        conn = _sprints.get_conn()
        try:
            _sprints.set_project_status(conn, self.PID, "delivered", via="test")
            conn.commit()
            first = self._one("SELECT delivered_at FROM projects WHERE id = ?", (self.PID,))
            self.assertTrue(first)
            # Re-delivering must not rewrite the day it was delivered.
            _sprints.set_project_status(conn, self.PID, "active", via="test")
            _sprints.set_project_status(conn, self.PID, "delivered", via="test")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            self._one("SELECT delivered_at FROM projects WHERE id = ?", (self.PID,)), first)

    def test_it_writes_on_the_CALLERS_transaction_and_opens_none_of_its_own(self):
        """The whole point of ruling 8: a status change rides inside the
        transaction that caused it (deliver_deal, mark_project_delivered, the
        cadence reconciler), so it cannot commit half a delivery. A writer that
        opened its own connection would make the change visible immediately and
        survive the caller's rollback — both asserted against here."""
        conn = _sprints.get_conn()
        try:
            _sprints.set_project_status(conn, self.PID, "delivered", via="test")
            # Uncommitted: a second connection still sees the old value.
            self.assertEqual(self._status(self.PID), "active")
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(self._status(self.PID), "active")

    def test_an_unknown_project_is_reported_not_invented(self):
        conn = _sprints.get_conn()
        try:
            self.assertIsNone(
                _sprints.set_project_status(conn, "proj_does_not_exist", "active", via="test"))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM projects WHERE id = 'proj_does_not_exist'"), 0)


# --------------------------------------------------- §3 the entry points

class CreateProject(_LifecycleCase):

    def test_a_new_project_starts_planned(self):
        """Red today: `create_project` never wrote the column, which is how 13
        live rows came to carry NULL in the first place."""
        res = _sprints.create_project("M04 New", "m04-new")
        self.assertEqual(self._status(res["id"]), "planned")


class PatchRoute(_LifecycleCase):
    """PATCH /api/projects/{id} routes `status` through the writer."""

    PID = "proj_m04_patch"

    def setUp(self):
        super().setUp()
        self._project(self.PID, status="planned")

    def test_a_valid_status_is_written(self):
        r = _CLIENT.patch(f"/api/projects/{self.PID}", json={"status": "active"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._status(self.PID), "active")

    def test_an_unknown_status_is_a_400_and_writes_nothing(self):
        """The code is not enough to prove this one: before the change a
        status-only PATCH also 400'd — as "nothing to update (name/slug/…)",
        i.e. by not knowing the field existed. So the message has to name the
        rejected value and the vocabulary, which is what makes this red today."""
        r = _CLIENT.patch(f"/api/projects/{self.PID}", json={"status": "delivering"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("delivering", r.text)
        self.assertIn("delivered", r.text)      # the vocabulary is offered back
        self.assertEqual(self._status(self.PID), "planned")

    def test_a_status_on_a_project_that_does_not_exist_is_a_404(self):
        r = _CLIENT.patch("/api/projects/proj_nope", json={"status": "active"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_status_and_the_editable_fields_travel_together(self):
        r = _CLIENT.patch(f"/api/projects/{self.PID}",
                          json={"name": "Renamed by patch", "status": "delivered"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._status(self.PID), "delivered")
        self.assertEqual(self._one("SELECT name FROM projects WHERE id = ?", (self.PID,)),
                         "Renamed by patch")

    def test_an_empty_patch_is_still_refused(self):
        r = _CLIENT.patch(f"/api/projects/{self.PID}", json={})
        self.assertEqual(r.status_code, 400, r.text)


# ------------------------------------------------- §4 the untriaged floor

class InboxCount(_LifecycleCase):
    """`identity.inbox_count()` existed and nothing served it, so the untriaged
    pile was only visible to someone reading SQL — which is how it reached 44."""

    def test_the_endpoint_reports_the_inbox_floor(self):
        from dashboard import identity as _identity
        r = _CLIENT.get("/api/inbox/count")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["count"], _identity.inbox_count())
        self.assertEqual(body["project_id"], _identity.inbox_id())

    def test_the_count_follows_the_inbox(self):
        from dashboard import identity as _identity
        before = _CLIENT.get("/api/inbox/count").json()["count"]
        self._task("t_m04_inbox_new", _identity.inbox_id(), "todo")
        self.assertEqual(_CLIENT.get("/api/inbox/count").json()["count"], before + 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
