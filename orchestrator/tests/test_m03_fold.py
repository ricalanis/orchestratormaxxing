"""Contract for m03_initiatives_fold — the phase-2 initiatives fold.

The migration moves a whole layer's data onto another layer and then leaves the
old one standing as an audit trail. That shape has exactly three ways to be
wrong, and each is a section below:

  1. **Something is left behind.** An initiative with no fold event is a
     workstream that silently stopped being represented anywhere the UI will
     still render — the layer disappears from the screen in the same release, so
     a gap here is invisible rather than annoying. `test_every_initiative_is_
     accounted_for` requires the mapping and the DB to agree in BOTH directions.
  2. **Something is overwritten.** The fold copies six fields plus a status onto
     projects. A copy that wins over an existing value would destroy work a human
     did on the destination — so the no-overwrite case is SEEDED (a project field
     set to a human value before the migration runs), not inferred from today's
     all-NULL data, which would pass against a migration with no guard at all.
  3. **A frozen column moves.** `tasks.initiative_id` / `epic_id` are the
     attribution trail this mapping was derived from and the only record of
     provenance once the layer is hidden; `initiatives` itself must come out
     byte-identical. Asserted per row against a pre-migration snapshot, with the
     ONE explicit exception the design allows: the retargeted workstream's open
     tasks change `project_id`.

DB isolation: a COPY of ~/.hermes/kanban.db per test, via `db.KANBAN_DB` /
`sprints.KANBAN_DB` — the test_m02_spine.py / test_migration_runner.py pattern.
The real DB is never opened for writing, and `runner.run_backup` is always
stubbed so no test writes into the operator's ~/.hermes/backups.

Both classes bring the copy up to m02 FIRST and then REWIND m03 (see `_rewind`),
so every delta measured below is m03's and not m02's — m02 rewrites
`initiatives.status` in its hygiene step, and attributing that to m03 would be a
false green on §3. The rewind also makes these cases independent of whether the
live DB has already been migrated: registering a migration in `runner.py` arms
it for the next dashboard/MCP process start, so `~/.hermes/kanban.db` acquires
m03 on its own schedule and a test that assumed a virgin copy would flip from
green to vacuous the moment a gateway process restarted.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_m03_fold.py   # from orchestrator/
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The module under test imports NOTHING but the stdlib (both package __init__
# files are empty), so it is imported on its own and a failure here is a FAILURE,
# never a skip. The `_READY` guard below skips on a missing DB or a dashboard
# environment that cannot import — but folding this import into that guard would
# mean a syntax error in the migration silently skipped all 19 cases and exited
# 0, which is a false green rather than a missing environment.
_MODULE_ERROR = None
try:
    from dashboard.migrations.m03_initiatives_fold import (
        FOLD_EVENT_KIND, FOLDS, INITIATIVE_STATUS_TO_PROJECT_STATUS,
        ROADMAP_FIELDS, TERMINAL_TASK_STATUSES, m03_initiatives_fold,
    )
except Exception as exc:  # pragma: no cover - asserted below
    _MODULE_ERROR = exc

_READY = False
try:
    from dashboard import db as _db, sprints as _sprints, orchestration as _orch
    from dashboard.migrations import runner

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    _READY = _MODULE_ERROR is None and _REAL_DB.exists()
except Exception:  # pragma: no cover
    _READY = False

MIGRATION_NAME = "m03_initiatives_fold"


class ModuleImports(unittest.TestCase):
    """Unconditional: the migration file must at least be loadable."""

    def test_the_module_under_test_imports(self):
        self.assertIsNone(_MODULE_ERROR, f"import failed: {_MODULE_ERROR!r}")


class _FoldCase(unittest.TestCase):
    """Copy the live DB, bring it up to *just before* m03, snapshot.

    Subclasses decide when m03 itself runs: the accounting/landing cases want it
    applied in setUp, the guard cases need to seed the DB first."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_m03_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints_db = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        _sprints.KANBAN_DB = self.tmp
        # orchestration.ensure_schema() mkdirs a sidecar dir — keep it out of ~/.hermes.
        self.orch_dir = Path(tempfile.mkdtemp(prefix="orch_m03_test_"))
        self._orig_orch, self._orig_specs = _orch.ORCH_DIR, _orch.SPECS_DIR
        _orch.ORCH_DIR, _orch.SPECS_DIR = self.orch_dir, self.orch_dir / "specs"
        # Never shell out to bin/backup-kanban from a test.
        self._orig_backup = runner.run_backup
        self.backup_calls = []
        runner.run_backup = lambda: self.backup_calls.append(1)
        self._orig_migrations = list(runner.MIGRATIONS)

        # Everything up to (not including) m03, so the snapshot is a post-m02 DB.
        runner.MIGRATIONS = [(n, f) for n, f in self._orig_migrations
                             if n != MIGRATION_NAME]
        runner.run()
        runner.MIGRATIONS = self._orig_migrations
        self.rewound = self._rewind()
        self.backup_calls.clear()
        self.before = self._snapshot()

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

    def _rewind(self) -> int:
        """Put the COPY back to its pre-m03 state, driving off m03's own audit rows.

        Not test scaffolding for its own sake: this is the inverse of the
        migration expressed ONLY in terms of what the `initiative_folded` events
        claim happened. It works because those events are a complete account of
        every write — if a future edit made m03 change something it did not
        record, the rewind would leave that change behind and the landing cases
        would start failing, which is exactly the right alarm. A copy that has
        never been migrated has no events, so this is a no-op there.

        Returns the number of folds rewound."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT initiative_id, payload FROM initiative_events WHERE kind = ?",
                (FOLD_EVENT_KIND,)).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                target = payload["target_project_id"]
                for field in payload["donated_fields"]:
                    # Column names come from the frozen tuple, never the payload.
                    self.assertIn(field, ROADMAP_FIELDS)
                    conn.execute(
                        f"UPDATE projects SET {field} = NULL WHERE id = ?", (target,))
                if payload["project_status"]:
                    conn.execute(
                        "UPDATE projects SET status = NULL WHERE id = ?", (target,))
                for task_id in payload["repointed_task_ids"]:
                    conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?",
                                 (payload["parent_project_id"], task_id))
            conn.execute("DELETE FROM initiative_events WHERE kind = ?",
                         (FOLD_EVENT_KIND,))
            conn.execute("DELETE FROM orch_migrations WHERE name = ?",
                         (MIGRATION_NAME,))
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def _apply_body(self):
        """Drive the apply function directly — the partial-failure rerun path."""
        conn = self._conn()
        try:
            m03_initiatives_fold(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self):
        return runner.run()

    def _initiatives(self):
        return {r["id"]: r for r in self._rows("SELECT * FROM initiatives")}

    def _projects(self):
        cols = ", ".join(("id", "status") + ROADMAP_FIELDS)
        return {r["id"]: r for r in self._rows(f"SELECT {cols} FROM projects")}

    def _fold_events(self):
        return {r["initiative_id"]: json.loads(r["payload"]) for r in self._rows(
            "SELECT initiative_id, payload FROM initiative_events WHERE kind = ?",
            (FOLD_EVENT_KIND,))}

    def _snapshot(self):
        return {
            "initiatives": self._initiatives(),
            "projects": self._projects(),
            "tasks": {r["id"]: (r["epic_id"], r["initiative_id"], r["project_id"],
                                r["status"])
                      for r in self._rows(
                          "SELECT id, epic_id, initiative_id, project_id, status "
                          "FROM tasks")},
            "deal_initiatives": {r["id"]: r["initiative_id"] for r in self._rows(
                "SELECT id, initiative_id FROM deals")},
            "events": self._one("SELECT COUNT(*) FROM initiative_events"),
        }


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class M03Fold(_FoldCase):
    """The migration applied through the runner against the live copy."""

    def setUp(self):
        super().setUp()
        self.result = self._migrate()

    # --- 1: it lands through the runner ----------------------------------
    def test_registered_and_recorded_in_the_ledger(self):
        """An apply function nobody calls is a file, not a migration."""
        order = [n for n, _ in runner.MIGRATIONS]
        self.assertIn(MIGRATION_NAME, order)
        self.assertIn(MIGRATION_NAME, self.result["applied"])
        self.assertIsNotNone(self._one(
            "SELECT applied_at FROM orch_migrations WHERE name = ?", (MIGRATION_NAME,)))
        # AFTER m02: m03 writes into columns m02 adds (projects.quarter/tier/...).
        # Registered in the wrong order it would fail on a fresh DB only.
        self.assertLess(order.index("m02_spine"), order.index(MIGRATION_NAME))
        # Pending work → the floor's backup gate fired.
        self.assertTrue(self.backup_calls)

    # --- 2: nothing is left behind ---------------------------------------
    def test_every_initiative_is_accounted_for(self):
        """Both directions, on purpose.

        `FOLDS ⊆ DB` catches a mapping row for an initiative that no longer
        exists; `DB ⊆ FOLDS` catches an initiative created after the mapping was
        frozen — which is the failure that matters, because the UI stops
        rendering initiatives in the same release and an unfolded one would
        simply vanish. If this goes red with a NEW id, the fix is a decision:
        add it to FOLDS in dashboard/migrations/m03_initiatives_fold.py."""
        in_db = set(self._initiatives())
        in_map = {f.initiative for f in FOLDS}
        self.assertEqual(len(in_map), len(FOLDS), "duplicate initiative in FOLDS")
        self.assertEqual(in_map, in_db)
        self.assertEqual(set(self._fold_events()), in_db)
        # Exactly one ledger row each — a second, contradictory account of the
        # same fold is worse than none.
        rows = self._rows(
            "SELECT initiative_id, COUNT(*) n FROM initiative_events "
            "WHERE kind = ? GROUP BY initiative_id", (FOLD_EVENT_KIND,))
        self.assertTrue(rows)
        self.assertEqual({r["n"] for r in rows}, {1})

    def test_fold_event_payload_is_a_readable_decision_record(self):
        """The event is the ONLY marker that an initiative is folded (status is
        deliberately left alone), so its payload has to carry the decision, not
        just the fact."""
        events = self._fold_events()
        projects = self._projects()
        for fold in FOLDS:
            payload = events[fold.initiative]
            self.assertEqual(payload["via"], "m03_initiatives_fold")
            self.assertEqual(payload["decision"], fold.decision)
            self.assertEqual(payload["target_project_id"], fold.target)
            self.assertEqual(payload["parent_project_id"], fold.parent)
            self.assertIn(payload["decision"], ("merge", "retarget"))
            # The decision word must match the geometry it describes.
            self.assertEqual(fold.decision == "retarget", fold.target != fold.parent,
                             fold.initiative)
            # A fold into a project that does not exist is not a fold.
            self.assertIn(fold.target, projects, fold.initiative)
            self.assertTrue(payload["note"].strip(), fold.initiative)

    def test_one_donor_per_target_project(self):
        """`proj_orchestrator` has six initiatives and ONE set of roadmap columns.
        Two donors would resolve that conflict by list order — i.e. by accident —
        and the loser's fields would silently not land."""
        donors = {}
        for fold in FOLDS:
            if fold.donates_fields:
                donors.setdefault(fold.target, []).append(fold.initiative)
        for target, initiatives in donors.items():
            self.assertEqual(len(initiatives), 1, f"{target}: {initiatives}")

    # --- 3: the fields land, without overwriting --------------------------
    def test_roadmap_fields_land_on_the_target_project(self):
        after = self._projects()
        for fold in FOLDS:
            if not fold.donates_fields:
                continue
            initiative = self.before["initiatives"][fold.initiative]
            project_before = self.before["projects"][fold.target]
            for field in ROADMAP_FIELDS:
                value = initiative[field]
                blank = value is None or (isinstance(value, str) and not value.strip())
                if project_before[field] is not None:
                    expected = project_before[field]   # never overwritten
                elif blank:
                    expected = None                    # '' is absent, not an answer
                else:
                    expected = value
                self.assertEqual(after[fold.target][field], expected,
                                 f"{fold.initiative} → {fold.target}.{field}")
        # ...and the fold really did something: at least one field moved. A
        # migration that copies nothing would otherwise pass every case above.
        moved = sum(len(p["donated_fields"]) for p in self._fold_events().values())
        self.assertGreater(moved, 0)

    def test_project_status_is_mapped_from_the_initiative_status(self):
        """`projects.status` did not exist before m02, so this is the one place a
        project's lifecycle is seeded from history instead of from a human."""
        after = self._projects()
        for fold in FOLDS:
            if not fold.donates_fields:
                continue
            before = self.before["projects"][fold.target]["status"]
            mapped = INITIATIVE_STATUS_TO_PROJECT_STATUS.get(
                self.before["initiatives"][fold.initiative]["status"])
            expected = before if before is not None else mapped
            self.assertEqual(after[fold.target]["status"], expected, fold.target)
            self.assertIn(after[fold.target]["status"],
                          ("planned", "active", "delivering", "delivered", "archived"))

    def test_the_status_map_covers_the_whole_initiative_vocabulary(self):
        """Asserted as a TABLE, not through today's rows.

        Live carries only `planned` and `active`, so a map that had silently lost
        `shipped` or `dropped` would pass every data-driven case here and then
        leave a project's lifecycle NULL the first time an initiative was closed.
        The pair of vocabularies is the contract; both ends are pinned."""
        from dashboard import strategy
        self.assertEqual(set(INITIATIVE_STATUS_TO_PROJECT_STATUS), set(strategy.STATUSES))
        self.assertEqual(
            INITIATIVE_STATUS_TO_PROJECT_STATUS,
            {"planned": "planned", "active": "active",
             "shipped": "delivered", "dropped": "archived"})
        # ...and every mapped value is a real `projects.status` (spec §1).
        self.assertLessEqual(
            set(INITIATIVE_STATUS_TO_PROJECT_STATUS.values()),
            {"planned", "active", "delivering", "delivered", "archived"})

    def test_non_donor_initiatives_write_nothing(self):
        """Four of the six proj_orchestrator initiatives donate no fields at all.
        Their fields are not lost — they stay readable in `initiatives` forever,
        which is why that table is kept."""
        events = self._fold_events()
        for fold in FOLDS:
            if fold.donates_fields:
                continue
            self.assertEqual(events[fold.initiative]["donated_fields"], {},
                             fold.initiative)
            self.assertIsNone(events[fold.initiative]["project_status"],
                              fold.initiative)

    def test_projects_outside_the_mapping_are_untouched(self):
        targets = {f.target for f in FOLDS}
        after = self._projects()
        for project_id, before in self.before["projects"].items():
            if project_id in targets:
                continue
            self.assertEqual(after[project_id], before, project_id)

    # --- 4: the frozen columns -------------------------------------------
    def test_frozen_columns_are_byte_identical(self):
        """`tasks.epic_id` / `tasks.initiative_id` and the whole `initiatives`
        table come out unchanged; `tasks.project_id` changes ONLY for the ids the
        fold events themselves declare as re-pointed. Reading the exception out
        of the audit row rather than hardcoding it means an undeclared move is a
        failure even if the migration also 'documents' it somewhere else."""
        repointed = set()
        for payload in self._fold_events().values():
            repointed.update(payload["repointed_task_ids"])
        after = {r["id"]: (r["epic_id"], r["initiative_id"], r["project_id"],
                           r["status"])
                 for r in self._rows(
                     "SELECT id, epic_id, initiative_id, project_id, status FROM tasks")}
        self.assertEqual(set(after), set(self.before["tasks"]))
        for task_id, row in after.items():
            was = self.before["tasks"][task_id]
            self.assertEqual(row[0], was[0], f"{task_id}.epic_id")
            self.assertEqual(row[1], was[1], f"{task_id}.initiative_id")
            self.assertEqual(row[3], was[3], f"{task_id}.status")
            if task_id not in repointed:
                self.assertEqual(row[2], was[2], f"{task_id}.project_id")
        # The `initiatives` table is the frozen audit trail: m03 READS it only.
        self.assertEqual(self._initiatives(), self.before["initiatives"])
        # deals.initiative_id stays as-is — deals are not auto-linked here.
        self.assertEqual(
            {r["id"]: r["initiative_id"]
             for r in self._rows("SELECT id, initiative_id FROM deals")},
            self.before["deal_initiatives"])

    def test_the_fold_marker_is_the_event_not_a_status_rewrite(self):
        """`strategy.STATUSES` has no value that means "folded": `shipped` and
        `dropped` would both be false claims about work that is continuing on a
        project. So the status column keeps carrying human intent and the event
        carries the fold."""
        from dashboard import strategy
        statuses = {r["status"] for r in self._rows("SELECT DISTINCT status FROM initiatives")}
        self.assertLessEqual(statuses - {None}, set(strategy.STATUSES))
        self.assertEqual(
            {i: r["status"] for i, r in self._initiatives().items()},
            {i: r["status"] for i, r in self.before["initiatives"].items()})

    # --- 5: a rerun is safe -----------------------------------------------
    def test_reapply_is_idempotent(self):
        """The runner records the name once, so the body normally runs once. A
        crash between the writes and the ledger COMMIT — or a hand-rebuilt
        ledger — must not double-write the audit spine or move a task twice."""
        before = {
            "projects": self._projects(),
            "tasks": {r["id"]: r["project_id"] for r in self._rows(
                "SELECT id, project_id FROM tasks")},
            "events": self._one("SELECT COUNT(*) FROM initiative_events"),
            "payloads": self._fold_events(),
        }
        self._apply_body()
        self.assertEqual(self._projects(), before["projects"])
        self.assertEqual({r["id"]: r["project_id"] for r in self._rows(
            "SELECT id, project_id FROM tasks")}, before["tasks"])
        self.assertEqual(self._one("SELECT COUNT(*) FROM initiative_events"),
                         before["events"])
        self.assertEqual(self._fold_events(), before["payloads"])

    def test_reapply_does_not_clobber_a_field_edited_after_the_fold(self):
        """The `IS NULL` predicate is not just an idempotency trick: once a value
        is on the project it belongs to the project, and a rerun must not pull
        the initiative's older copy back over it."""
        target = next(f.target for f in FOLDS if f.donates_fields)
        self._exec("UPDATE projects SET quarter = '2099-Q4', status = 'delivering' "
                   "WHERE id = ?", (target,))
        self._apply_body()
        row = self._projects()[target]
        self.assertEqual(row["quarter"], "2099-Q4")
        self.assertEqual(row["status"], "delivering")


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class M03FoldGuards(_FoldCase):
    """Cases that must SEED the DB before the migration runs.

    Today's data is all-NULL projects and zero open attributed tasks, so the
    no-overwrite and re-point guards would pass against a migration that had no
    guards at all. Each case below creates the condition it claims to test."""

    def test_existing_project_values_are_never_overwritten(self):
        fold = next(f for f in FOLDS if f.donates_fields)
        self._exec(
            "UPDATE projects SET quarter = ?, why = ?, status = ? WHERE id = ?",
            ("2099-Q1", "operator wrote this", "delivering", fold.target))
        self._migrate()
        row = self._projects()[fold.target]
        self.assertEqual(row["quarter"], "2099-Q1")
        self.assertEqual(row["why"], "operator wrote this")
        self.assertEqual(row["status"], "delivering")
        # The un-seeded fields still landed — the guard is per column, not a
        # per-project "skip if anything is set".
        initiative = self.before["initiatives"][fold.initiative]
        self.assertEqual(row["tier"], initiative["tier"])
        # ...and the audit row reports what ACTUALLY landed, not what was tried.
        payload = self._fold_events()[fold.initiative]
        self.assertNotIn("quarter", payload["donated_fields"])
        self.assertNotIn("why", payload["donated_fields"])
        self.assertIsNone(payload["project_status"])

    def test_blank_initiative_fields_are_not_copied(self):
        """Two live initiatives carry `why = ''` from the create form. Copying
        that would turn "never filled in" into "answered, with nothing" — the
        project page would render an empty section instead of prompting."""
        fold = next(f for f in FOLDS if f.donates_fields)
        self._exec("UPDATE initiatives SET why = '', success_check = '   ' "
                   "WHERE id = ?", (fold.initiative,))
        self._exec("UPDATE projects SET why = NULL, success_check = NULL "
                   "WHERE id = ?", (fold.target,))
        self._migrate()
        row = self._projects()[fold.target]
        self.assertIsNone(row["why"])
        self.assertIsNone(row["success_check"])
        payload = self._fold_events()[fold.initiative]
        self.assertNotIn("why", payload["donated_fields"])
        self.assertNotIn("success_check", payload["donated_fields"])

    def test_retarget_repoints_open_attributed_tasks_only(self):
        """The move that makes a retarget real rather than a field copy.

        A done task already rolled up to its project when it was completed;
        moving it later rewrites delivery history that has been reported. Open
        work is the only thing a re-point is for — and `initiative_id` survives
        the move, because it is the provenance record."""
        fold = next(f for f in FOLDS if f.decision == "retarget")
        # `reviewed_at` is set on the done fixture on purpose: `canvas.ensure_schema`
        # promotes every unreviewed `done` task to `review` at each startup, so an
        # unreviewed fixture would arrive at m03 as OPEN work and the case would
        # assert the opposite of what it claims to.
        seed = [("t_m03_open", "ready"), ("t_m03_done", "done"),
                ("t_m03_rejected", "rejected")]
        for task_id, status in seed:
            self._exec(
                "INSERT INTO tasks (id, title, status, project_id, initiative_id, "
                "reviewed_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, f"m03 fixture {status}", status, fold.parent,
                 fold.initiative, 1, 1))
        self._migrate()
        rows = {r["id"]: r for r in self._rows(
            "SELECT id, project_id, initiative_id, status FROM tasks WHERE id LIKE 't_m03_%'")}
        self.assertEqual(rows["t_m03_open"]["project_id"], fold.target)
        self.assertEqual(rows["t_m03_done"]["project_id"], fold.parent)
        self.assertEqual(rows["t_m03_rejected"]["project_id"], fold.parent)
        for row in rows.values():
            self.assertEqual(row["initiative_id"], fold.initiative)
        payload = self._fold_events()[fold.initiative]
        self.assertEqual(payload["repointed_task_ids"], ["t_m03_open"])
        # ...and a rerun does not move the settled ones later.
        self._apply_body()
        self.assertEqual(
            {r["id"]: r["project_id"] for r in self._rows(
                "SELECT id, project_id FROM tasks WHERE id LIKE 't_m03_%'")},
            {"t_m03_open": fold.target, "t_m03_done": fold.parent,
             "t_m03_rejected": fold.parent})

    def test_a_merge_never_moves_a_task(self):
        """On a merge the target IS the parent, so there is nothing to move. A
        task attributed to a merged initiative but sitting in some OTHER project
        stays there: dragging it "home" would be a second, unrequested migration
        hiding inside this one."""
        fold = next(f for f in FOLDS if f.decision == "merge")
        self._exec(
            "INSERT INTO tasks (id, title, status, project_id, initiative_id, "
            "created_at) VALUES ('t_m03_stray','m03 stray','ready','proj_inbox',?,1)",
            (fold.initiative,))
        self._migrate()
        self.assertEqual(
            self._one("SELECT project_id FROM tasks WHERE id = 't_m03_stray'"),
            "proj_inbox")
        self.assertEqual(
            self._fold_events()[fold.initiative]["repointed_task_ids"], [])

    def test_a_missing_initiative_gets_no_fold_event(self):
        """A DB that never had the row (a fresh bootstrap, an older copy) is not
        a failure — but fabricating an audit row for a fold that did not happen
        would be."""
        fold = FOLDS[0]
        self._exec("DELETE FROM initiatives WHERE id = ?", (fold.initiative,))
        self._migrate()
        events = self._fold_events()
        self.assertNotIn(fold.initiative, events)
        # Every other fold still landed — one missing row does not abort the batch.
        self.assertEqual(set(events),
                         {f.initiative for f in FOLDS} - {fold.initiative})

    def test_a_missing_target_project_is_skipped_not_invented(self):
        """A fold with no destination is not a fold. Skipping leaves the
        initiative exactly as it was, which is recoverable; creating the project
        would mint a noun out of a missing FK."""
        fold = FOLDS[0]
        self._exec("UPDATE tasks SET project_id = 'proj_inbox' WHERE project_id = ?",
                   (fold.target,))
        self._exec("DELETE FROM projects WHERE id = ?", (fold.target,))
        self._migrate()
        self.assertNotIn(fold.initiative, self._fold_events())
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM projects WHERE id = ?", (fold.target,)), 0)

    def test_terminal_task_statuses_match_the_briefs_definition_of_open(self):
        """The re-point predicate is only correct if "open" means the same thing
        here as everywhere else the operator is shown what still needs him."""
        from dashboard import brief
        source = Path(brief.__file__).read_text()
        self.assertIn("NOT IN ('done', 'rejected', 'cancelled')", source)
        self.assertEqual(set(TERMINAL_TASK_STATUSES),
                         {"done", "rejected", "cancelled"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
