"""Contract for m02_spine — the phase-1 spine migration.

What this pins, and why each part is here rather than trusted:

  1. **It lands through the runner.** The migration is only real if it is
     REGISTERED: an apply function nobody calls is a file, not a migration. The
     first case drives `runner.run()` and reads the ledger.
  2. **The shape is what the callers were promised.** `dashboard/brief.py` already
     ships forward-schema guards (`"project_id" in _columns(conn, "deals")`,
     `task_dispatches` column-set check) that degrade to empty lists today. Those
     guards are one half of a contract; these `PRAGMA table_info` assertions are
     the other half — without them the guards would silently stay closed forever
     and nothing would fail.
  3. **A rerun is safe.** The runner records the name once, so the normal path
     runs the body once. But a crash between the DDL and the ledger COMMIT, or a
     hand-rebuilt ledger, must not turn "already applied" into an exception (a
     duplicate `ADD COLUMN` raises) or a double-write. `test_reapply_*` calls the
     apply function a SECOND time against the already-migrated DB.
  4. **The backfills are asserted as RELATIONSHIPS, not as today's numbers.**
     `autonomy` after == (NULL + dispatch) before, and `auto` before == `auto`
     after. Hardcoding {ask: 252, auto: 5} would pin a snapshot of a DB that
     changes every day, so a correct migration would start failing on Thursday.
  5. **What must NOT change.** `tasks.epic_id` / `tasks.initiative_id` are
     hermes-owned and frozen for phase 1, and `assignee` stays a display field —
     so the executor backfill is only trustworthy if it provably read `assignee`
     without rewriting it. Asserted per-row against a pre-migration snapshot.

DB isolation: a COPY of ~/.hermes/kanban.db per test, via `db.KANBAN_DB` /
`sprints.KANBAN_DB` — the test_context_endpoint.py / test_migration_runner.py
pattern. The real DB is never opened for writing, and `runner.run_backup` is
always stubbed so no test writes into the operator's ~/.hermes/backups.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_m02_spine.py   # from orchestrator/
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_READY = False
try:
    from dashboard import db as _db, sprints as _sprints, orchestration as _orch
    from dashboard.migrations import runner
    from dashboard.migrations.m02_spine import (
        ARCHIVED_TOPIC_IDS, CHAT_ID, EXECUTOR_BY_ASSIGNEE, NAMED_THREADS,
        STALE_SPRINT_ID, m02_spine,
    )

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

# The columns m02 adds, by table. Kept as data so the "exists" case and the
# "re-apply is safe" case cannot drift apart.
NEW_COLUMNS = {
    "deals": ["project_id"],
    "projects": ["status", "account_id", "delivered_at", "quarter", "tier",
                 "why", "success_check", "health", "confidence", "repo_path"],
    "tasks": ["executor_kind", "executor_target", "thread_id"],
}
NEW_TABLES = ["threads", "task_dispatches"]
NEW_INDEXES = ["idx_deals_project", "idx_projects_account",
               "idx_tasks_project_status", "idx_task_dispatches_task"]
THREADS_COLUMNS = {"thread_id", "chat_id", "name", "project_id", "role",
                   "status", "last_activity_at"}
DISPATCH_COLUMNS = {"id", "task_id", "executor_kind", "executor_target", "state",
                    "thread_id", "exit_code", "stdout_tail", "note",
                    "created_at", "updated_at"}


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class M02Spine(unittest.TestCase):
    """Every case gets a fresh copy of the live DB, migrated in setUp."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_m02_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints_db = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        # orchestration.ensure_schema() mkdirs a sidecar dir — keep it out of ~/.hermes.
        self.orch_dir = Path(tempfile.mkdtemp(prefix="orch_m02_test_"))
        self._orig_orch, self._orig_specs = _orch.ORCH_DIR, _orch.SPECS_DIR
        _orch.ORCH_DIR, _orch.SPECS_DIR = self.orch_dir, self.orch_dir / "specs"
        # Never shell out to bin/backup-kanban from a test.
        self._orig_backup = runner.run_backup
        self.backup_calls = []
        runner.run_backup = lambda: self.backup_calls.append(1)

        # Snapshot everything the migration is supposed to derive FROM (and
        # everything it must not touch) before it runs.
        self.before = self._snapshot()
        # A live copy may already carry the name row once this ships; drop it so
        # every case exercises a real application.
        self._exec("DELETE FROM orch_migrations WHERE name = 'm02_spine'")
        self.result = runner.run()

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
        conn = sqlite3.connect(str(self.tmp))
        conn.row_factory = sqlite3.Row
        return conn

    def _rows(self, sql, args=()):
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

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

    def _columns(self, table):
        conn = self._conn()
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def _histogram(self, table, column):
        return {r[column]: r["n"] for r in self._rows(
            f"SELECT {column}, COUNT(*) n FROM {table} GROUP BY {column}")}

    def _snapshot(self):
        """The pre-migration facts every backfill assertion is measured against."""
        return {
            "autonomy": self._histogram("tasks", "autonomy"),
            "assignee": self._histogram("tasks", "assignee"),
            "task_count": self._one("SELECT COUNT(*) FROM tasks"),
            # Frozen columns: id → (epic_id, initiative_id, assignee).
            "frozen": {r["id"]: (r["epic_id"], r["initiative_id"], r["assignee"])
                       for r in self._rows(
                           "SELECT id, epic_id, initiative_id, assignee FROM tasks")},
            # Live copies accumulate real dispatch/provenance state between test
            # runs; assertions must scope to rows the MIGRATION changed, never
            # to a whole-DB emptiness that rots the first time dispatch is used.
            "executor_pre": {r["id"]: (r["executor_kind"], r["executor_target"])
                             for r in self._rows(
                                 "SELECT id, executor_kind, executor_target FROM tasks")}
            if "executor_kind" in self._columns("tasks") else {},
            "thread_pre": {r["id"]: r["thread_id"] for r in self._rows(
                "SELECT id, thread_id FROM tasks")}
            if "thread_id" in self._columns("tasks") else {},
        }

    # --- 1: it lands through the runner ----------------------------------
    def test_registered_and_recorded_in_the_ledger(self):
        self.assertIn("m02_spine", [n for n, _ in runner.MIGRATIONS])
        self.assertIn("m02_spine", self.result["applied"])
        self.assertIsNotNone(
            self._one("SELECT applied_at FROM orch_migrations WHERE name = 'm02_spine'"))
        # m01 must still precede it: brief_runs exists before anything reads it.
        order = [n for n, _ in runner.MIGRATIONS]
        self.assertLess(order.index("m01_brief_runs"), order.index("m02_spine"))
        # Pending work → the backup gate fired (fail-closed contract of the floor).
        self.assertTrue(self.backup_calls)

    # --- 2: the shape the callers were promised --------------------------
    def test_columns_tables_and_indexes_exist(self):
        for table, columns in NEW_COLUMNS.items():
            have = self._columns(table)
            for column in columns:
                self.assertIn(column, have, f"{table}.{column}")
        self.assertTrue(THREADS_COLUMNS.issubset(self._columns("threads")))
        self.assertTrue(DISPATCH_COLUMNS.issubset(self._columns("task_dispatches")))
        indexes = {r["name"] for r in self._rows(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        for index in NEW_INDEXES:
            self.assertIn(index, indexes)

    def test_brief_forward_schema_guards_now_open(self):
        """The other side of dashboard/brief.py's `_columns` guards.

        `compose_needs_you` degrades to `orphan_won_deals: []` and `compose_agents`
        to `dispatches: []` until exactly these exist. Asserting the guard's own
        predicate — not a paraphrase — is what makes this a contract rather than
        two files that happen to agree today."""
        from dashboard import brief
        conn = self._conn()
        try:
            self.assertIn("project_id", brief._columns(conn, "deals"))
            self.assertLessEqual(
                {"id", "task_id", "executor_kind", "state", "created_at"},
                brief._columns(conn, "task_dispatches"))
        finally:
            conn.close()

    def test_check_constraints_reject_unknown_vocabulary(self):
        """The CHECK clauses are the point of the two new tables: a role or a
        dispatch state outside the vocabulary is a routing bug that must fail
        loudly at the write, not read as a mystery value later."""
        conn = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO threads (thread_id, chat_id, name, role) "
                    "VALUES (999999, '1', 'bogus', 'wizard')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO task_dispatches "
                    "(id, task_id, executor_kind, state, created_at, updated_at) "
                    "VALUES ('d1', 't1', 'hermes', 'maybe', 1, 1)")
        finally:
            conn.close()

    # --- 3: a rerun is safe ----------------------------------------------
    def test_reapply_is_idempotent(self):
        """Simulates the partial-failure rerun: apply the body AGAIN against the
        already-migrated DB. A duplicate `ADD COLUMN` raises in SQLite, so an
        unguarded ALTER fails this loudly."""
        before = {
            "threads": self._one("SELECT COUNT(*) FROM threads"),
            "autonomy": self._histogram("tasks", "autonomy"),
            "executor": self._histogram("tasks", "executor_kind"),
            "projects": self._one("SELECT COUNT(*) FROM projects"),
            "columns": {t: self._columns(t) for t in NEW_COLUMNS},
        }
        conn = self._conn()
        try:
            m02_spine(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._one("SELECT COUNT(*) FROM threads"), before["threads"])
        self.assertEqual(self._histogram("tasks", "autonomy"), before["autonomy"])
        self.assertEqual(self._histogram("tasks", "executor_kind"), before["executor"])
        self.assertEqual(self._one("SELECT COUNT(*) FROM projects"), before["projects"])
        self.assertEqual({t: self._columns(t) for t in NEW_COLUMNS}, before["columns"])

    def test_reapply_does_not_clobber_an_edited_seed_row(self):
        """`INSERT OR IGNORE`, not `INSERT OR REPLACE`: the seed names are
        provisional and the operator is expected to edit them. A rerun that reset a
        renamed thread would make the registry unsafe to touch."""
        self._exec("UPDATE threads SET name = 'Renombrado', project_id = 'proj_inbox' "
                   "WHERE thread_id = 7348")
        conn = self._conn()
        try:
            m02_spine(conn)
            conn.commit()
        finally:
            conn.close()
        row = self._rows("SELECT name, project_id FROM threads WHERE thread_id = 7348")[0]
        self.assertEqual(row["name"], "Renombrado")
        self.assertEqual(row["project_id"], "proj_inbox")

    # --- 4: the thread registry ------------------------------------------
    def test_threads_seed(self):
        rows = {r["thread_id"]: r for r in self._rows("SELECT * FROM threads")}
        self.assertGreaterEqual(len(rows), 9)
        # m02's seed must all be PRESENT and correct — but it is not the whole
        # table forever: later migrations legitimately register new threads
        # (m25 added 🎨 Designer / 15957). Asserting an exact total pinned
        # "no migration ever adds a thread", which was merely true, never
        # required. The per-thread assertions below are the real guarantee.
        self.assertGreaterEqual(len(rows), len(NAMED_THREADS) + len(ARCHIVED_TOPIC_IDS))
        # Exactly one ritual destination. m12 intentionally made `station` the
        # stable routing identity and renamed the editable display name.
        hoy = [r for r in rows.values() if r.get("station") == "ritual"]
        self.assertEqual(len(hoy), 1)
        self.assertEqual(hoy[0]["thread_id"], 15185)
        self.assertEqual(hoy[0]["role"], "ops")
        self.assertIsNone(hoy[0]["project_id"])
        self.assertEqual(hoy[0]["status"], "active")
        # Every named topic: active, right role, one chat. The chat id itself is
        # tenant CONFIG ($HERMES_DEFAULT_CHAT_ID): rows carry whatever id seeded
        # them historically, so the invariant is ONE shared chat — and equality
        # with the module constant only when the config is present.
        named_chats = {rows[t[0]]["chat_id"] for t in NAMED_THREADS}
        self.assertEqual(len(named_chats), 1)
        if CHAT_ID:
            self.assertEqual(named_chats, {CHAT_ID})
        for thread_id, _seed_name, role, project_id in NAMED_THREADS:
            row = rows[thread_id]
            self.assertEqual(row["role"], role)
            self.assertEqual(row["project_id"], project_id)
            self.assertEqual(row["status"], "active")
        # Every other live binding: registered but parked.
        for thread_id in ARCHIVED_TOPIC_IDS:
            row = rows[thread_id]
            self.assertEqual(row["status"], "archived")
            self.assertEqual(row["role"], "personal")
            self.assertEqual(row["name"], f"topic {thread_id}")

    def test_the_one_project_binding_points_at_a_real_project(self):
        """A binding naming a project that does not exist would route dispatches
        into nothing. There is exactly ONE binding in phase 1 — so it is cheap to
        require that it resolve."""
        bound = self._rows(
            "SELECT thread_id, project_id FROM threads WHERE project_id IS NOT NULL")
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["thread_id"], 7363)
        self.assertEqual(bound[0]["project_id"], "proj_orchestrator")
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM projects WHERE id = ?",
                      (bound[0]["project_id"],)), 1)

    # --- 5: the backfills, as relationships ------------------------------
    def test_autonomy_collapses_null_and_dispatch_into_ask(self):
        before, after = self.before["autonomy"], self._histogram("tasks", "autonomy")
        expected_ask = before.get(None, 0) + before.get("dispatch", 0) + before.get("ask", 0)
        self.assertEqual(after.get("ask", 0), expected_ask)
        self.assertEqual(after.get("auto", 0), before.get("auto", 0))
        self.assertNotIn(None, after)
        self.assertNotIn("dispatch", after)
        self.assertEqual(sum(after.values()), self.before["task_count"])

    def test_executor_backfill_maps_every_task(self):
        mapping = dict((a, (k, t)) for a, k, t in EXECUTOR_BY_ASSIGNEE)
        rows = self._rows(
            "SELECT id, assignee, executor_kind, executor_target FROM tasks")
        self.assertEqual(len(rows), self.before["task_count"])
        for row in rows:
            pre = self.before["executor_pre"].get(row["id"], (None, None))
            if pre[0] is None:
                # Rows the migration routed: derived from assignee, never guessed.
                kind, target = mapping.get(row["assignee"], ("human", None))
                self.assertEqual(row["executor_kind"], kind, row["assignee"])
                self.assertEqual(row["executor_target"], target, row["assignee"])
            else:
                # Rows dispatch already routed on the live copy: untouched.
                self.assertEqual(
                    (row["executor_kind"], row["executor_target"]), pre, row["id"])
        # No task is left without an executor: an unrouted task is a task the
        # dispatcher would have to guess about.
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM tasks WHERE executor_kind IS NULL"), 0)
        # thread_id is stamped by dispatch and MCP provenance, never by the
        # migration: values must be byte-identical before vs after.
        self.assertEqual(
            {r["id"]: r["thread_id"]
             for r in self._rows("SELECT id, thread_id FROM tasks")},
            self.before["thread_pre"])

    def test_frozen_columns_and_assignee_are_untouched(self):
        """epic_id / initiative_id are hermes-owned and out of scope for phase 1;
        `assignee` is the field the backfill READ, so rewriting it would destroy
        the evidence the derivation was based on."""
        after = {r["id"]: (r["epic_id"], r["initiative_id"], r["assignee"])
                 for r in self._rows(
                     "SELECT id, epic_id, initiative_id, assignee FROM tasks")}
        self.assertEqual(after, self.before["frozen"])

    # --- 6: hygiene -------------------------------------------------------
    def test_hygiene_repairs(self):
        from dashboard import strategy
        statuses = {r["status"] for r in self._rows("SELECT DISTINCT status FROM initiatives")}
        self.assertNotIn("in_progress", statuses)
        self.assertLessEqual(statuses - {None}, set(strategy.STATUSES))

        sprint = self._rows("SELECT status, closed_at FROM sprints WHERE id = ?",
                            (STALE_SPRINT_ID,))
        if sprint:  # absent on a copy that predates it — the assertion is conditional
            self.assertEqual(sprint[0]["status"], "completed")
            self.assertIsNotNone(sprint[0]["closed_at"])

        self.assertEqual(
            self._one("SELECT COUNT(*) FROM projects "
                      "WHERE id LIKE 'patch-test-%' AND archived_at IS NULL"), 0)

    def test_hygiene_archives_rather_than_deletes_a_referenced_fixture(self):
        """The guard that keeps cleanup from creating a dangling reference: a
        `patch-test-*` project with a task pointing at it is archived, never
        DELETEd. (Live has zero such projects, so this seeds the case rather than
        trusting that it never happens.)"""
        self._exec("INSERT INTO projects (id, slug, name, created_at) "
                   "VALUES ('patch-test-referenced', 'patch-test-referenced', 'ref', 1)")
        self._exec("INSERT INTO projects (id, slug, name, created_at) "
                   "VALUES ('patch-test-orphan', 'patch-test-orphan', 'orphan', 1)")
        task_id = self._one("SELECT id FROM tasks LIMIT 1")
        self._exec("UPDATE tasks SET project_id = 'patch-test-referenced' WHERE id = ?",
                   (task_id,))
        conn = self._conn()
        try:
            m02_spine(conn)
            conn.commit()
        finally:
            conn.close()
        referenced = self._rows(
            "SELECT archived_at FROM projects WHERE id = 'patch-test-referenced'")
        self.assertEqual(len(referenced), 1)
        self.assertIsNotNone(referenced[0]["archived_at"])
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM projects WHERE id = 'patch-test-orphan'"), 0)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class BoardStatuses(unittest.TestCase):
    """The same-commit sprints.py change: 14 live tasks sit in `rejected` /
    `cancelled` and `set_task_status` refused to name either, so the board could
    show them but never move them."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_m02_board_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints_db = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp

    def tearDown(self):
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints_db
        try:
            self.tmp.unlink()
        except Exception:
            pass

    def _status(self, task_id):
        conn = sqlite3.connect(str(self.tmp))
        try:
            return conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_rejected_and_cancelled_are_movable_statuses(self):
        self.assertLessEqual({"rejected", "cancelled"}, _sprints._BOARD_STATUSES)
        conn = sqlite3.connect(str(self.tmp))
        task_id = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
        conn.close()
        for status in ("rejected", "cancelled"):
            res = _sprints.set_task_status(task_id, status)
            self.assertNotEqual(res.get("status"), "error", res)
            self.assertEqual(self._status(task_id), status)
        # The gate still rejects genuine nonsense — widening it is not disabling it.
        self.assertEqual(
            _sprints.set_task_status(task_id, "wizard").get("status"), "error")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
