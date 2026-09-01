"""Contract for the m00 migration floor (dashboard/migrations/runner.py).

Pins the two guarantees the floor exists to provide:

  1. **One chain, no drift.** `dashboard/api.py` and `mcp_server.py` used to keep
     separate hand-maintained `ensure_schema()` lists, and the MCP server's had
     become a strict SUBSET — a DB it bootstrapped came up without
     fireflies_meetings / events / event_attendance / nurture_sequences /
     task_comments / consulting_time_entries. `test_entrypoints_bootstrap_
     identical_schema` imports BOTH entrypoints in real subprocesses against
     equally-stripped DB copies and diffs `sqlite_master`. Provably red before
     the runner landed (the MCP copy was missing 5 tables).
  2. **An all-or-nothing versioned ledger.** `orch_migrations` records what ran;
     the 7 pre-ledger migrations are backfilled by name so they never re-apply;
     a migration and its name row commit (or roll back) together; the
     `bin/backup-kanban` gate fires only when there is pending work and fails
     closed.

DB isolation: a COPY of ~/.hermes/kanban.db per test, via `db.KANBAN_DB` /
`sprints.KANBAN_DB` (in-process) and `HERMES_KANBAN_DB` (subprocess) — the
test_context_endpoint.py pattern. The real DB is never opened for writing, and
`runner.run_backup` is always stubbed so no test can write into the operator's
~/.hermes/backups. `orchestration.ORCH_DIR` is likewise redirected at a tmpdir.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_migration_runner.py   # from orchestrator/
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_READY = False
try:
    from dashboard import db as _db, sprints as _sprints, orchestration as _orch
    from dashboard.migrations import runner

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

# The pre-ledger migrations, backfilled by name. Must match the module filenames
# in dashboard/migrations/ 1:1.
HISTORICAL = [
    "p0_2_unify_cycle",
    "p1_3_initiative_attribution",
    "p2_4_backlog_cleanup",
    "p3_indexes",
    "phase1_backlog_scheduling",
    "crm_growth",
    "daily_reflections",
]

# Tables the FULL ensure chain creates and the MCP server's old subset list did
# not. Stripped from both copies in the drift test so the comparison is
# discriminating instead of trivially green.
DRIFT_TABLES = [
    "fireflies_meetings",
    "events",
    "event_attendance",
    "nurture_sequences",
    "task_comments",
    "consulting_time_entries",
]


def _strip_copy(dest: Path) -> None:
    """A copy of the live DB with the dashboard-owned tables removed, so the
    ensure chain has real work to do (the hermes-owned tables stay — a truly
    empty file can't bootstrap: object_graph ALTERs `tasks`)."""
    shutil.copy(_REAL_DB, dest)
    conn = sqlite3.connect(str(dest))
    for table in DRIFT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.close()


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class MigrationRunnerLedger(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_mig_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        # Both modules resolve the DB path from their own module global.
        self._orig_db, self._orig_sprints_db = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        # orchestration.ensure_schema() mkdirs its sidecar directory — keep that
        # out of ~/.hermes.
        self.orch_dir = Path(tempfile.mkdtemp(prefix="orch_mig_test_"))
        self._orig_orch, self._orig_specs = _orch.ORCH_DIR, _orch.SPECS_DIR
        _orch.ORCH_DIR, _orch.SPECS_DIR = self.orch_dir, self.orch_dir / "specs"
        # Never shell out to bin/backup-kanban from a test: it writes real
        # snapshots into the operator's ~/.hermes/backups.
        self._orig_backup = runner.run_backup
        self.backup_calls = []
        runner.run_backup = lambda: self.backup_calls.append(1)
        self._orig_migrations = list(runner.MIGRATIONS)
        # The live copy already carries a ledger; drop it so run() has to build
        # one from scratch.
        conn = sqlite3.connect(str(self.tmp))
        conn.execute("DROP TABLE IF EXISTS orch_migrations")
        conn.commit()
        conn.close()

    def tearDown(self):
        runner.MIGRATIONS = self._orig_migrations
        runner.run_backup = self._orig_backup
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints_db
        _orch.ORCH_DIR, _orch.SPECS_DIR = self._orig_orch, self._orig_specs
        shutil.rmtree(self.orch_dir, ignore_errors=True)
        try:
            self.tmp.unlink()
        except Exception:
            pass

    # --- helpers ---------------------------------------------------------
    def _ledger(self):
        conn = sqlite3.connect(str(self.tmp))
        try:
            return {r[0]: r[1] for r in
                    conn.execute("SELECT name, applied_at FROM orch_migrations")}
        finally:
            conn.close()

    def _has_table(self, name) -> bool:
        conn = sqlite3.connect(str(self.tmp))
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,)).fetchone() is not None
        finally:
            conn.close()

    # --- 1: the ledger is created, backfilled, and applied ----------------
    def test_creates_ledger_backfilling_history_and_applying_registered(self):
        # Generic over runner.MIGRATIONS on purpose. This assertion used to read
        # `assertEqual(runner.MIGRATIONS, [])`, which pinned an *accident* of the
        # commit that introduced the floor (nothing stood on it yet) as if it
        # were a requirement — so the first real migration made a passing test
        # fail for being correct. What is required is the RELATIONSHIP: history
        # is backfilled, everything registered is applied, and the ledger is
        # exactly their union.
        registered = [n for n, _ in runner.MIGRATIONS]
        res = runner.run()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(sorted(res["backfilled"]), sorted(HISTORICAL))
        self.assertEqual(res["applied"], registered)
        self.assertEqual(sorted(self._ledger()), sorted(HISTORICAL + registered))
        # A versioned name colliding with a backfilled one would be recorded as
        # already-applied and silently never run.
        self.assertEqual(set(HISTORICAL) & set(registered), set())
        # ...and the legacy phase really ran: the ensure chain's tables are here.
        for table in DRIFT_TABLES:
            self.assertTrue(self._has_table(table), table)

    # --- 2: re-running changes nothing ------------------------------------
    def test_second_run_is_a_no_op(self):
        runner.run()
        # Stamp a recognisable applied_at: if the second run re-wrote the rows
        # (INSERT OR REPLACE, or a re-apply) the stamp would move.
        conn = sqlite3.connect(str(self.tmp))
        conn.execute("UPDATE orch_migrations SET applied_at = 1")
        conn.commit()
        conn.close()
        before = self._ledger()
        res = runner.run()
        self.assertEqual(self._ledger(), before)
        self.assertEqual(set(before.values()), {1})
        self.assertEqual(res["backfilled"], [])
        self.assertEqual(res["applied"], [])

    # --- 3: a failing migration leaves no trace ---------------------------
    def test_failed_migration_leaves_neither_schema_nor_name_row(self):
        def _boom(conn):
            conn.execute("CREATE TABLE mig_boom_marker (x TEXT)")
            raise RuntimeError("boom")

        runner.MIGRATIONS = [("m_test_boom", _boom)]
        with self.assertRaises(RuntimeError):
            runner.run()
        self.assertNotIn("m_test_boom", self._ledger())
        self.assertFalse(self._has_table("mig_boom_marker"))

    # --- 4: concurrent callers apply once ---------------------------------
    def test_concurrent_runs_apply_the_migration_once(self):
        calls = []
        guard = threading.Lock()

        def _once(conn):
            with guard:
                calls.append(1)
            conn.execute("CREATE TABLE mig_concurrent_marker (x TEXT)")

        runner.MIGRATIONS = [("m_test_concurrent", _once)]
        errors = []

        def _worker():
            try:
                runner.run()
            except BaseException as exc:  # pragma: no cover - the assertion
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual([repr(e) for e in errors], [])
        self.assertEqual(len(calls), 1)
        self.assertIn("m_test_concurrent", self._ledger())
        self.assertTrue(self._has_table("mig_concurrent_marker"))
        # The gate is pending-driven, so the loser of the race skips the backup.
        self.assertEqual(len(self.backup_calls), 1)

    # --- 5: the backup gate -----------------------------------------------
    def test_backup_gate_fires_only_when_migrations_are_pending(self):
        # First run drains whatever is registered: ONE snapshot for the whole
        # pending batch (none at all if nothing is registered).
        runner.run()
        self.assertLessEqual(len(self.backup_calls), 1)
        self.backup_calls.clear()
        runner.run()  # everything registered is now applied → nothing pending
        self.assertEqual(self.backup_calls, [])

        def _noop(conn):
            conn.execute("CREATE TABLE mig_gate_marker (x TEXT)")

        runner.MIGRATIONS = [("m_test_gate", _noop)]
        runner.run()
        self.assertEqual(len(self.backup_calls), 1)
        # ...and once applied it stops being pending, so no further backups.
        runner.run()
        self.assertEqual(len(self.backup_calls), 1)

    def test_backup_failure_aborts_the_versioned_phase(self):
        applied = []

        def _never(conn):  # pragma: no cover - must not be reached
            applied.append(1)
            conn.execute("CREATE TABLE mig_unbacked_marker (x TEXT)")

        def _fail():
            raise RuntimeError("backup-kanban exited 3")

        runner.MIGRATIONS = [("m_test_unbacked", _never)]
        runner.run_backup = _fail
        with self.assertRaises(RuntimeError):
            runner.run()
        # Fail closed: no snapshot → no DDL, no name row.
        self.assertEqual(applied, [])
        self.assertNotIn("m_test_unbacked", self._ledger())
        self.assertFalse(self._has_table("mig_unbacked_marker"))

    def test_backup_script_is_resolved_absolutely_and_exists(self):
        self.assertTrue(runner.BACKUP_SCRIPT.is_absolute())
        self.assertTrue(runner.BACKUP_SCRIPT.exists(), runner.BACKUP_SCRIPT)
        self.assertEqual(runner.BACKUP_SCRIPT.name, "backup-kanban")


# One process' worth of `runner.run()`, started at a shared wall-clock instant so
# N of them collide inside the LEGACY phase instead of politely queueing behind
# each other's python startup.
_CONCURRENT_RUN = (
    "import os, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from dashboard.migrations import runner\n"
    "runner.run_backup = lambda: None\n"   # never write into ~/.hermes/backups
    "start = float(os.environ['START_AT'])\n"
    "while time.time() < start:\n"
    "    time.sleep(0.002)\n"
    "runner.run()\n"
)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class ConcurrentStartup(unittest.TestCase):
    """Two processes open this DB at startup — the dashboard (`dashboard.api`,
    at import time) and the MCP server — and both call `runner.run()`.

    The versioned phase was serialized from the start (BEGIN IMMEDIATE). The
    LEGACY phase was not, and it is full of `PRAGMA table_info` →
    `if column not in cols` → `ALTER TABLE` sequences: a textbook TOCTOU. With
    any legacy DDL actually pending, N concurrent starts meant N-1 processes
    dying on `duplicate column name: …` — the dashboard failing to boot outright
    (api.py runs it at import) and the MCP server coming up crippled and silent
    (it swallows the exception into `_LOOP_OK = False`).

    RED-PROOF: run against the runner without the `_legacy_lock()` and 2 of 4
    processes exit 1 with `sqlite3.OperationalError: duplicate column name:
    initiative_id` (dropping `epics` makes object_graph.ensure_schema recreate
    the table and then re-add its `initiative_id` column — exactly one pending
    legacy ALTER, which is all it takes).
    """

    N = 4

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="mig_concurrent_"))
        self.db = self.workdir / "kanban.db"
        shutil.copy(_REAL_DB, self.db)
        # Exactly one pending legacy DDL: `epics` is recreated by
        # object_graph.ensure_schema, which then ALTERs on its initiative_id.
        conn = sqlite3.connect(str(self.db))
        conn.execute("DROP TABLE IF EXISTS epics")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_concurrent_startups_survive_a_pending_legacy_ddl(self):
        env = dict(os.environ)
        env.update({
            "HERMES_KANBAN_DB": str(self.db),
            "TESTING": "1",
            "HERMES_DASHBOARD_TOKEN": "",
            "ORCH_DIR": str(self.workdir / "orchestration"),
            "START_AT": str(time.time() + 3.0),
        })
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _CONCURRENT_RUN, str(REPO)],
                cwd=str(REPO), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(self.N)
        ]
        results = []
        for p in procs:
            out, err = p.communicate(timeout=300)
            results.append((p.returncode, err))

        failures = [err[-600:] for rc, err in results if rc != 0]
        self.assertEqual(failures, [], f"{len(failures)}/{self.N} concurrent runs crashed")
        # ...and the phase they were racing in actually did its work.
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(epics)")]
        finally:
            conn.close()
        self.assertIn("initiative_id", cols)


# The schema-set snapshot, run inside each entrypoint's own process.
_SNAPSHOT = (
    "import importlib, os, sqlite3, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "conn = sqlite3.connect(os.environ['HERMES_KANBAN_DB'])\n"
    "rows = conn.execute("
    "    \"SELECT type, name FROM sqlite_master ORDER BY type, name\").fetchall()\n"
    "print('\\n'.join(f'{t}:{n}' for t, n in rows))\n"
)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class EntrypointSchemaParity(unittest.TestCase):
    """The drift fix: both processes must bootstrap the SAME schema."""

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="mig_parity_"))

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _bootstrap(self, module: str) -> str:
        db_path = self.workdir / f"{module.replace('.', '_')}.db"
        _strip_copy(db_path)
        env = dict(os.environ)
        env.update({
            "HERMES_KANBAN_DB": str(db_path),
            "TESTING": "1",
            "HERMES_DASHBOARD_TOKEN": "",
            # keep orchestration's sidecar mkdir out of ~/.hermes
            "ORCH_DIR": str(self.workdir / "orchestration"),
            # pending versioned migrations take an online backup before DDL;
            # keep that fixture artifact inside this test's disposable root.
            "HERMES_BACKUP_DIR": str(self.workdir / "backups"),
        })
        proc = subprocess.run(
            [sys.executable, "-c", _SNAPSHOT, module],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"import {module} failed: {proc.stderr[-2000:]}")
        return proc.stdout

    def test_entrypoints_bootstrap_identical_schema(self):
        api_schema = self._bootstrap("dashboard.api")
        mcp_schema = self._bootstrap("mcp_server")
        self.assertEqual(api_schema.splitlines(), mcp_schema.splitlines())
        # Guard against a trivially-equal pass: the stripped tables must have
        # been recreated by BOTH chains (this is exactly what drifted).
        for table in DRIFT_TABLES:
            self.assertIn(f"table:{table}", api_schema.splitlines(), table)
            self.assertIn(f"table:{table}", mcp_schema.splitlines(), table)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
