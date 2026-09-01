"""Contract for `crm.delivery_drift()` — the read-only coherence check that
the delivery leg was missing (run 2026-08-17-delivery-drift).

The three conversion verbs (won → `deliver_deal` → `mark_project_delivered`)
are HUMAN-ONLY by design, and verb 3 deliberately never touches `deals.stage`
(ruling 2 / CRITICAL-2): a won deal stays `won` forever, and "delivered" is a
READ over `deals.project_id → projects.status`. That separation is correct, but
it left a hole — a project can be marked delivered while the deal that funds it
is still sitting open in the pipeline, because verb 3 selects
`WHERE project_id = ? AND stage = 'won'` and finds zero rows when the deal was
never linked (verb 2 was skipped). Observed 2026-08-17: a $100K deal in
`stalled` while its initiative's project was already `delivered`.

`delivery_drift()` REPORTS that drift; it never repairs it. Repair is a human
tap on the verbs, by design (spec red line 11). These tests pin the two drift
shapes, the `ok` flag, the archived-project exclusion, and the additive
`uncovered_open_deals` key on `mark_project_delivered`.

DB isolation: a COPY of the conftest sandbox DB per test, schema brought up by
`runner.run()`. `runner.run_backup` is stubbed everywhere — no test writes into
the operator's ~/.hermes/backups. The real DB is never opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_delivery_drift.py   # from orchestrator/
"""
import atexit
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

_READY = False
_IMPORT_DB = None
try:
    from dashboard import db as _db, sprints as _sprints, crm as _crm
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # Resolves to the per-session sandbox copy that tests/conftest.py exports
    # (never the operator's live DB). Same import-time redirect pattern as
    # test_deliver_deal.py: dashboard.api runs the migration runner at import,
    # so point every DB layer at a throwaway copy FIRST so import can never
    # touch the real DB, then hand the globals back — each test redirects to
    # its own copy in setUp.
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_drift_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        # Import dashboard.api so the migration runner applies; we don't need
        # the TestClient here (delivery_drift is a direct crm call), but the
        # import-time side effects must land on the throwaway copy.
        from dashboard import api  # noqa: F401
        _db.KANBAN_DB = _sprints.KANBAN_DB = _REAL_DB
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


@atexit.register
def _cleanup_import_db():  # pragma: no cover
    try:
        if _IMPORT_DB and _IMPORT_DB.exists():
            _IMPORT_DB.unlink()
    except Exception:
        pass


NOW = int(time.time())
ACCOUNT = "acct_drift_main"
ACCOUNT_NAME = "Drift Test Client"
PROJECT_DELIVERED = "proj_drift_delivered"
PROJECT_DELIVERED_COVERED = "proj_drift_covered"
PROJECT_ACTIVE = "proj_drift_active"
PROJECT_ARCHIVED = "proj_drift_archived"
INITIATIVE = "init_drift_main"
INITIATIVE_COVERED = "init_drift_covered"
INITIATIVE_ACTIVE = "init_drift_active"
INITIATIVE_ARCHIVED = "init_drift_archived"


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _DriftCase(unittest.TestCase):
    """Live-DB copy + the migration runner (so m02_spine's columns are real), a
    self-contained CRM fixture on top."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_drift_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        runner.run()
        self._seed()

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

    def _seed(self):
        """One account, four projects (delivered-drifting, delivered-covered,
        active, archived-delivered), and the deals/initiatives that exercise
        every branch of `delivery_drift`."""
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  (ACCOUNT, ACCOUNT_NAME, NOW))
        c.execute("INSERT INTO contacts (id, account_id, name, created_at) VALUES (?,?,?,?)",
                  ("cont_drift", ACCOUNT, "Ada Drift", NOW))
        # Projects: the drifting delivered one, a covered delivered one, an
        # active one, and an archived delivered one (archived must be excluded).
        for pid, slug, name, status, archived in (
                (PROJECT_DELIVERED, "drift-delivered", "Drift Delivered",
                 "delivered", None),
                (PROJECT_DELIVERED_COVERED, "drift-covered", "Covered Delivered",
                 "delivered", None),
                (PROJECT_ACTIVE, "drift-active", "Active Project",
                 "active", None),
                (PROJECT_ARCHIVED, "drift-archived", "Archived Delivered",
                 "delivered", NOW)):
            c.execute(
                "INSERT INTO projects (id, slug, name, created_at, status, "
                "delivered_at, archived_at) VALUES (?,?,?,?,?,?,?)",
                (pid, slug, name, NOW, status,
                 "2026-08-13T21:43:56-06:00" if status == "delivered" else None,
                 archived))
        # Initiatives: one per project, so deals can point at them.
        for iid, pid in (
                (INITIATIVE, PROJECT_DELIVERED),
                (INITIATIVE_COVERED, PROJECT_DELIVERED_COVERED),
                (INITIATIVE_ACTIVE, PROJECT_ACTIVE),
                (INITIATIVE_ARCHIVED, PROJECT_ARCHIVED)):
            c.execute(
                "INSERT INTO initiatives (id, title, project_id, status, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?)",
                (iid, f"Init for {pid}", pid, "planned", NOW, NOW))
        # Deals:
        #   deal_drift_open — stalled, reachable via INITIATIVE → PROJECT_DELIVERED
        #   deal_drift_won  — won, linked directly to PROJECT_DELIVERED_COVERED
        #   deal_drift_active_open — proposal, on the active project (no drift)
        #   deal_archived_open — open, on the archived delivered project (excluded)
        for did, title, stage, value, init_id, proj_id in (
                ("deal_drift_open", "Drift Open Deal", "stalled", 100000.0,
                 INITIATIVE, None),
                ("deal_drift_won", "Covered Won Deal", "won", 50000.0,
                 INITIATIVE_COVERED, PROJECT_DELIVERED_COVERED),
                ("deal_drift_active_open", "Active Open Deal", "proposal", 30000.0,
                 INITIATIVE_ACTIVE, None),
                ("deal_archived_open", "Archived Open Deal", "engaged", 20000.0,
                 INITIATIVE_ARCHIVED, None)):
            c.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "initiative_id, project_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, ACCOUNT, title, stage, value, "MXN", init_id, proj_id, NOW, NOW))
        c.commit()
        c.close()


class Shape(_DriftCase):
    """C2 — the dict shape and the `ok` flag."""

    def test_top_level_keys_are_exactly_checked_at_drift_counts_ok(self):
        r = _crm.delivery_drift()
        self.assertEqual(set(r.keys()), {"checked_at", "drift", "counts", "ok"}, r.keys())

    def test_counts_keys_are_the_three_integers(self):
        r = _crm.delivery_drift()
        self.assertEqual(set(r["counts"].keys()),
                         {"delivered_project_no_won_deal",
                          "open_deal_on_delivered_project", "total"})

    def test_ok_is_true_iff_total_is_zero(self):
        # The seeded fixture has drift (a delivered project with no won deal,
        # plus an open deal on a delivered project), so ok must be False here.
        r = _crm.delivery_drift()
        self.assertEqual(r["ok"], r["counts"]["total"] == 0)
        self.assertGreater(r["counts"]["total"], 0)
        self.assertFalse(r["ok"])

    def test_drift_kinds_are_in_the_allowed_set(self):
        r = _crm.delivery_drift()
        kinds = {d["kind"] for d in r["drift"]}
        self.assertLessEqual(kinds,
                             {"delivered_project_no_won_deal",
                              "open_deal_on_delivered_project"})


class DeliveredProjectNoWonDeal(_DriftCase):
    """C3 part 1 — delivered projects with no linked won deal."""

    def test_delivered_project_with_a_linked_won_deal_is_not_drift(self):
        """C6 test 1: PROJECT_DELIVERED_COVERED has deal_drift_won linked
        directly with stage='won', so it must NOT appear as drift."""
        r = _crm.delivery_drift()
        rows = [d for d in r["drift"]
                if d["kind"] == "delivered_project_no_won_deal"
                and d["project_id"] == PROJECT_DELIVERED_COVERED]
        self.assertEqual(rows, [], "covered delivered project must not drift")

    def test_delivered_project_with_no_won_deal_drifts_and_carries_candidates(self):
        """C6 test 2: PROJECT_DELIVERED has no deal with project_id=<it> AND
        stage='won'. deal_drift_open is reachable via INITIATIVE and must
        appear in candidate_deals."""
        r = _crm.delivery_drift()
        rows = [d for d in r["drift"]
                if d["kind"] == "delivered_project_no_won_deal"
                and d["project_id"] == PROJECT_DELIVERED]
        self.assertEqual(len(rows), 1, r["drift"])
        row = rows[0]
        self.assertEqual(row["linked_won_deals"], 0)
        self.assertEqual(row["project_name"], "Drift Delivered")
        self.assertIsNotNone(row["delivered_at"])
        cand_ids = [c["id"] for c in row["candidate_deals"]]
        self.assertIn("deal_drift_open", cand_ids)
        # Each candidate carries the required fields.
        cand = [c for c in row["candidate_deals"] if c["id"] == "deal_drift_open"][0]
        self.assertEqual(cand["stage"], "stalled")
        self.assertEqual(cand["value"], 100000.0)
        self.assertEqual(cand["currency"], "MXN")


class NonCommercialProjectIsNotDrift(_DriftCase):
    """A delivered project with no commercial evidence is skipped.

    Added 2026-08-18 after the first live run: five internal/personal projects are `delivered`
    with no account and no deal on either path, and they buried the single real
    drift case 5-to-1. A check that cries wolf five times out of six is a check
    that gets ignored — which is the failure mode this whole detector exists to
    prevent.
    """

    def _insert_project(self, pid, name, account_id):
        c = self._conn()
        c.execute(
            "INSERT INTO projects (id, slug, name, status, delivered_at, "
            "account_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, pid.replace("_", "-"), name, "delivered", NOW, account_id, NOW))
        c.commit()
        c.close()

    def test_delivered_project_with_no_account_and_no_deals_is_skipped(self):
        self._insert_project("proj_internal_drift", "Internal Thing", None)
        r = _crm.delivery_drift()
        rows = [d for d in r["drift"] if d.get("project_id") == "proj_internal_drift"]
        self.assertEqual(rows, [], "a never-commercial project is not drift")

    def test_delivered_project_with_an_account_still_drifts(self):
        """An account IS commercial evidence: someone meant to bill this."""
        self._insert_project("proj_acct_drift", "Billed Thing", ACCOUNT)
        r = _crm.delivery_drift()
        rows = [d for d in r["drift"]
                if d["kind"] == "delivered_project_no_won_deal"
                and d["project_id"] == "proj_acct_drift"]
        self.assertEqual(len(rows), 1, r["drift"])
        self.assertEqual(rows[0]["linked_won_deals"], 0)
        self.assertEqual(rows[0]["candidate_deals"], [])


class OpenDealOnDeliveredProject(_DriftCase):
    """C3 part 2 — open deals reachable from a delivered project."""

    def test_open_deal_on_delivered_project_appears(self):
        """C6 test 3: deal_drift_open (stalled) is reachable via INITIATIVE →
        PROJECT_DELIVERED (delivered), so it must produce an
        open_deal_on_delivered_project row."""
        r = _crm.delivery_drift()
        rows = [d for d in r["drift"]
                if d["kind"] == "open_deal_on_delivered_project"
                and d["deal_id"] == "deal_drift_open"]
        self.assertEqual(len(rows), 1, r["drift"])
        row = rows[0]
        self.assertEqual(row["stage"], "stalled")
        self.assertEqual(row["value"], 100000.0)
        self.assertEqual(row["currency"], "MXN")
        self.assertEqual(row["project_id"], PROJECT_DELIVERED)
        self.assertEqual(row["project_name"], "Drift Delivered")
        self.assertIsNotNone(row["delivered_at"])

    def test_archived_delivered_project_is_excluded(self):
        """C3: archived projects are excluded from both detectors. The archived
        delivered project has an open deal via its initiative, but it must not
        produce any drift row."""
        r = _crm.delivery_drift()
        for d in r["drift"]:
            self.assertNotEqual(d.get("project_id"), PROJECT_ARCHIVED)
            self.assertNotEqual(d.get("deal_id"), "deal_archived_open")

    def test_active_project_produces_no_drift(self):
        """C6 test 4: a non-delivered project produces no drift at all. The
        active project has an open deal via its initiative, but it is not
        delivered, so neither detector fires for it."""
        r = _crm.delivery_drift()
        for d in r["drift"]:
            self.assertNotEqual(d.get("project_id"), PROJECT_ACTIVE)
        # And removing all delivered projects would make ok True — verified
        # structurally by asserting the active project's deal is not in drift.
        active_rows = [d for d in r["drift"]
                       if d.get("deal_id") == "deal_drift_active_open"]
        self.assertEqual(active_rows, [])


class NoDriftCase(_DriftCase):
    """C6 test 4 (the positive form): a fixture with no delivered projects at
    all produces `ok is True` and an empty drift list."""

    def _seed(self):
        c = self._conn()
        c.execute("PRAGMA foreign_keys = OFF")
        # Wipe the inherited live-DB rows so this fixture is truly self-contained
        # — the sandbox copy carries every delivered project from the operator's
        # real CRM, and a "no drift at all" assertion requires a clean slate.
        for tbl in ("deal_events", "deals", "initiatives", "projects",
                   "contacts", "accounts"):
            c.execute(f"DELETE FROM {tbl}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  (ACCOUNT, ACCOUNT_NAME, NOW))
        c.execute("INSERT INTO contacts (id, account_id, name, created_at) VALUES (?,?,?,?)",
                  ("cont_drift", ACCOUNT, "Ada Drift", NOW))
        # Only an active project — nothing delivered, nothing archived.
        c.execute(
            "INSERT INTO projects (id, slug, name, created_at, status) "
            "VALUES (?,?,?,?,?)",
            (PROJECT_ACTIVE, "drift-active", "Active Project", NOW, "active"))
        c.execute(
            "INSERT INTO initiatives (id, title, project_id, status, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            (INITIATIVE_ACTIVE, "Init active", PROJECT_ACTIVE, "planned", NOW, NOW))
        c.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "initiative_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("deal_drift_active_open", ACCOUNT, "Active Open Deal", "proposal",
             30000.0, "MXN", INITIATIVE_ACTIVE, NOW, NOW))
        c.commit()
        c.close()

    def test_no_drift_at_all_ok_is_true(self):
        r = _crm.delivery_drift()
        self.assertEqual(r["drift"], [])
        self.assertEqual(r["counts"]["total"], 0)
        self.assertTrue(r["ok"])


class MarkDeliveredWarning(_DriftCase):
    """C4 + C6 test 5 — `mark_project_delivered` gains an additive
    `uncovered_open_deals` key on both branches, and never writes to deals."""

    def test_mark_delivered_on_drifting_fixture_returns_uncovered_open_deals(self):
        """C6 test 5: marking PROJECT_DELIVERED (which has an open deal via its
        initiative) returns non-empty uncovered_open_deals. The verb still does
        not write to deals — stage is unchanged."""
        # PROJECT_DELIVERED is already status='delivered' in the seed, so this
        # exercises the already_delivered branch.
        res = _crm.mark_project_delivered(PROJECT_DELIVERED)
        self.assertEqual(res["status"], "already_delivered", res)
        self.assertIn("uncovered_open_deals", res)
        ids = [d["id"] for d in res["uncovered_open_deals"]]
        self.assertIn("deal_drift_open", ids)
        # Each entry carries the required fields.
        entry = [d for d in res["uncovered_open_deals"] if d["id"] == "deal_drift_open"][0]
        self.assertEqual(entry["stage"], "stalled")
        self.assertEqual(entry["value"], 100000.0)
        self.assertEqual(entry["currency"], "MXN")
        # The verb never writes to deals: the open deal's stage is unchanged.
        c = self._conn()
        try:
            stage = c.execute("SELECT stage FROM deals WHERE id = 'deal_drift_open'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(stage, "stalled")

    def test_mark_delivered_returns_empty_uncovered_when_all_reachable_are_won(self):
        """C6 test 5 (second half): when every reachable deal is won,
        uncovered_open_deals is []. PROJECT_DELIVERED_COVERED has deal_drift_won
        (stage='won') via INITIATIVE_COVERED, so it has no OPEN reachable deals."""
        res = _crm.mark_project_delivered(PROJECT_DELIVERED_COVERED)
        self.assertEqual(res["status"], "already_delivered", res)
        self.assertEqual(res["uncovered_open_deals"], [])

    def test_existing_returned_keys_are_unchanged(self):
        """C4: existing returned keys are unchanged — only an additive key."""
        res = _crm.mark_project_delivered(PROJECT_DELIVERED)
        # The already_delivered branch keeps its original keys plus the new one.
        self.assertEqual(set(res.keys()),
                         {"status", "project_id", "project_name", "delivered_at",
                          "delivered_deals", "uncovered_open_deals"})


class ReadOnly(_DriftCase):
    """C1 — `delivery_drift` performs zero writes."""

    def test_no_write_keywords_in_source(self):
        import inspect
        src = inspect.getsource(_crm.delivery_drift)
        # The function body must not contain any write SQL keyword. The
        # docstring mentions UPDATE/INSERT/DELETE/COMMIT in prose ("performs
        # zero writes (no UPDATE / INSERT / DELETE / COMMIT)"), so strip the
        # docstring before checking — the contract's check inspects the source
        # the same way and the docstring is not a write.
        # Remove the docstring (the first triple-quoted block) for the check.
        import ast
        tree = ast.parse(inspect.getsource(_crm.delivery_drift))
        # The FunctionDef's body[0] is the docstring Expr(Constant(str)).
        func_node = tree.body[0]
        body = func_node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        # Reconstruct the source without the docstring.
        import textwrap
        src_no_doc = "\n".join(
            textwrap.dedent(ast.unparse(stmt)) for stmt in body)
        for kw in ("UPDATE ", "INSERT ", "DELETE ", "COMMIT"):
            self.assertNotIn(kw, src_no_doc.upper(),
                             f"delivery_drift source contains {kw!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()