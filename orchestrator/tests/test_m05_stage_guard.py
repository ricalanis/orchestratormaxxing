"""Contract for m05 — the storage-engine half of retiring `deals.stage='delivered'`.

`crm.create_deal` / `crm.update_deal` refuse the value, the MCP handlers refuse
it, and the REST routes 400 on it (all pinned in `tests/test_crm_growth.py`).
Every one of those is a rule in OUR code, which is a rule that holds until the
next writer: the hermes CLI, a repair script, a future endpoint, a psql-style
poke at the file. Spec regla 7 is that the invariant lives in the engine too, so
this file is the one that asserts the trigger pair actually fires — the layer
the application tests cannot prove.

The three questions it answers, and why each needs its own case:
  1. **Does the DDL exist after the runner runs?** (A migration registered but
     not applied is the failure mode `test_migration_runner` cannot see for a
     specific migration.)
  2. **Does it fire on both verbs?** INSERT and `UPDATE OF stage` are separate
     trigger objects in SQLite — one of them passing says nothing about the
     other.
  3. **Does it stay out of the way otherwise?** A guard that also blocked
     writing `won`, or editing the notes of a legacy row, would be a bigger
     outage than the bug.

Plus the refusal branch: the migration ASSERTS the table is clean rather than
rewriting stages, so a DB that still carries a `delivered` row must abort the
whole versioned phase (ledger row included) instead of quietly "fixing" history.

DB isolation: a COPY of the sandbox kanban.db per test, schema brought up by
`runner.run()`, with `runner.run_backup` stubbed — no test writes into the
operator's ~/.hermes.

Run: .venv/bin/python -m pytest tests/test_m05_stage_guard.py   # from orchestrator/
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
    from dashboard import db as _db, sprints as _sprints
    from dashboard.migrations import runner
    from dashboard.migrations import m05_retire_delivered_stage as m05

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    _READY = _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False


@unittest.skipUnless(_READY, "kanban.db unavailable")
class StageGuard(unittest.TestCase):
    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_m05_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        runner.run()

    def tearDown(self):
        runner.run_backup = self._orig_backup
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints
        try:
            self.tmp.unlink()
        except Exception:
            pass

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        c = sqlite3.connect(str(self.tmp))
        c.row_factory = sqlite3.Row
        return c

    def _seed_deal(self, deal_id="deal_m05", stage="won"):
        c = self._conn()
        try:
            c.execute("INSERT OR REPLACE INTO accounts (id, name, created_at) "
                      "VALUES (?,?,?)", ("acct_m05", "M05 Guard Co", 1))
            c.execute("INSERT INTO deals (id, account_id, title, stage, value, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                      (deal_id, "acct_m05", "Guarded deal", stage, 1000.0, 1, 1))
            c.commit()
        finally:
            c.close()
        return deal_id

    # --- 1: the migration is registered and applied -----------------------
    def test_the_migration_is_registered_and_ledgered(self):
        self.assertIn("m05_retire_delivered_stage", [n for n, _ in runner.MIGRATIONS])
        c = self._conn()
        try:
            names = {r[0] for r in c.execute("SELECT name FROM orch_migrations")}
        finally:
            c.close()
        self.assertIn("m05_retire_delivered_stage", names)

    def test_both_triggers_exist(self):
        """Two objects, because SQLite has no multi-event trigger — the plan's
        single `trg_deal_stage_guard` is physically this pair."""
        c = self._conn()
        try:
            found = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'trg_deal_stage_guard%'")}
        finally:
            c.close()
        self.assertEqual(found, set(m05.TRIGGERS))

    # --- 2: it fires on both verbs ---------------------------------------
    def test_a_raw_insert_of_the_retired_stage_is_aborted(self):
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                c.execute("INSERT INTO deals (id, account_id, title, stage, created_at) "
                          "VALUES (?,?,?,?,?)",
                          ("deal_m05_raw", "acct_m05", "raw", "delivered", 1))
            self.assertIn("retired", str(caught.exception))
            c.rollback()
            self.assertIsNone(c.execute(
                "SELECT 1 FROM deals WHERE id = ?", ("deal_m05_raw",)).fetchone())
        finally:
            c.close()

    def test_a_raw_update_to_the_retired_stage_is_aborted(self):
        """The path the retired UI control used to drive, and the one a repair
        script would reach for. Bypassing `crm.py` must not bypass the rule."""
        did = self._seed_deal()
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                c.execute("UPDATE deals SET stage = 'delivered' WHERE id = ?", (did,))
            self.assertIn("retired", str(caught.exception))
            c.rollback()
            self.assertEqual(c.execute(
                "SELECT stage FROM deals WHERE id = ?", (did,)).fetchone()[0], "won")
        finally:
            c.close()

    def test_a_blanket_update_cannot_smuggle_the_stage_in(self):
        """`UPDATE deals SET stage = ...` with no WHERE is how a bulk fix goes
        wrong; the guard is per-row, so the whole statement aborts."""
        self._seed_deal()
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE deals SET stage = 'delivered'")
            c.rollback()
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM deals WHERE stage = 'delivered'").fetchone()[0], 0)
        finally:
            c.close()

    # --- 3: it stays out of the way --------------------------------------
    def test_every_live_stage_is_still_writable(self):
        did = self._seed_deal(stage="lead")
        c = self._conn()
        try:
            for stage in ("engaged", "qualified", "demo", "proposal", "stalled",
                          "won", "lost"):
                c.execute("UPDATE deals SET stage = ? WHERE id = ?", (stage, did))
            c.commit()
            self.assertEqual(c.execute(
                "SELECT stage FROM deals WHERE id = ?", (did,)).fetchone()[0], "lost")
        finally:
            c.close()

    def test_other_columns_of_a_row_stay_editable(self):
        """`BEFORE UPDATE OF stage`, not a bare `BEFORE UPDATE`: the ban is on
        the VALUE being written, not on the row existing."""
        did = self._seed_deal()
        c = self._conn()
        try:
            c.execute("UPDATE deals SET notes = ?, value = ? WHERE id = ?",
                      ("still editable", 4200.0, did))
            c.commit()
            row = c.execute("SELECT notes, value FROM deals WHERE id = ?",
                            (did,)).fetchone()
            self.assertEqual((row[0], row[1]), ("still editable", 4200.0))
        finally:
            c.close()

    # --- 4: the migration asserts, it does not rewrite --------------------
    def test_the_migration_refuses_a_dirty_table_instead_of_rewriting_it(self):
        """A `delivered` row would mean a writer exists that nobody knows about.
        Flipping it to `won` would be a stage change with no event and no human
        — the exact quiet history edit the retirement exists to stop."""
        c = self._conn()
        try:
            # Drop the guard first: this simulates the state m05 was DESIGNED
            # for (a table that predates it), which the guard itself prevents.
            for trg in m05.TRIGGERS:
                c.execute(f"DROP TRIGGER IF EXISTS {trg}")
            c.execute("INSERT OR REPLACE INTO accounts (id, name, created_at) "
                      "VALUES (?,?,?)", ("acct_m05", "M05 Guard Co", 1))
            c.execute("INSERT INTO deals (id, account_id, title, stage, created_at) "
                      "VALUES (?,?,?,?,?)",
                      ("deal_m05_legacy", "acct_m05", "legacy", "delivered", 1))
            c.commit()

            with self.assertRaises(RuntimeError) as caught:
                m05.m05_retire_delivered_stage(c)
            self.assertIn("deal_m05_legacy", str(caught.exception))
            # Nothing rewritten, nothing installed.
            self.assertEqual(c.execute(
                "SELECT stage FROM deals WHERE id = ?",
                ("deal_m05_legacy",)).fetchone()[0], "delivered")
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_deal_stage_guard%'").fetchone()[0], 0)
        finally:
            c.close()

    def test_a_clean_run_is_idempotent(self):
        c = self._conn()
        try:
            res = m05.m05_retire_delivered_stage(c)
            self.assertEqual(res["delivered_rows"], 0)
            self.assertEqual(sorted(res["triggers"]), sorted(m05.TRIGGERS))
            c.commit()
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_deal_stage_guard%'").fetchone()[0], 2)
        finally:
            c.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
