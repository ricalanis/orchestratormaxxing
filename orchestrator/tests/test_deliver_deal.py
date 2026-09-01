"""Contract for "Deliver this" — conversion verb 2/3 (phase-1 step 6).

The verb exists to make an orphaned won deal *structurally impossible*: it
creates `deals.project_id` at the one moment a human is guaranteed to be paying
attention. So what this file pins is the join and its guards, not the happy path
alone.

  1. **The join, in the direction the spec chose.** Many deals → one project.
     Acme funds one delivery with three deals, so a second won deal of the
     same account delivering into the SAME project is the normal case; a verb
     that minted a project per deal would reproduce exactly the shape the join
     direction was chosen to prevent. Asserted, not assumed.
  2. **Idempotency is atomic, not advisory.** Cron, a double-tap and a retried
     fetch all re-fire this verb. A second call must return the existing link,
     write no second event, and never re-point the deal.
  3. **Every refusal is typed, and refuses BEFORE it creates anything.** A
     rejected call that had already created its project would litter the
     registry with orphan projects — the mirror image of the bug being fixed. The
     project-count is asserted across every refusal path.
  4. **The drawer opens with the answer already chosen.** `deliver.default_project_id`
     is the account's existing project when one exists (red line 6: pickers are
     escape hatches, not paths). Tested on both sides of the first delivery, so
     the default is proven to *appear*, not merely to be present.
  5. **The verb is unreachable from MCP, by construction.** Red line 11 — agents
     propose conversions into the brief and the operator taps. The absence from
     mcp_server.py IS the guard, so it is asserted here rather than trusted to
     survive the next parity sweep.

DB isolation: a COPY of ~/.hermes/kanban.db per test, schema brought up by
`runner.run()`. `runner.run_backup` is stubbed everywhere — no test writes into
the operator's ~/.hermes/backups. The real DB is never opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_deliver_deal.py   # from orchestrator/
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
    from dashboard import db as _db, sprints as _sprints, crm as _crm, context as _context
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ resolves to the per-session sandbox copy that tests/conftest.py exports
    # (never the operator's live DB): this module is one of the six that hand
    # db.KANBAN_DB / sprints.KANBAN_DB back to _REAL_DB when its import block
    # ends, and pytest imports every module before running any test — so the
    # last one collected used to leave the global on the live file for the
    # whole run (data loss 2026-07-29 and 2026-07-31).
    if _REAL_DB.exists():
        # dashboard.api runs the migration runner at import. Point every DB layer
        # at a throwaway copy FIRST so that import can never touch the real DB,
        # then hand the globals back — each test redirects to its own copy.
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_deliver_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        from dashboard import api as _api
        app = _api.app

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
ACCOUNT = "acct_dlv_main"
ACCOUNT_NAME = "Deliver Test Client"
OTHER_ACCOUNT = "acct_dlv_other"
EXISTING_PROJECT = "proj_dlv_existing"
ARCHIVED_PROJECT = "proj_dlv_archived"


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _DeliverCase(unittest.TestCase):
    """Live-DB copy + the migration runner (so m02_spine's columns are real), a
    self-contained CRM fixture on top."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_deliver_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self.workspace = Path(tempfile.mkdtemp(prefix="proposal_workspace_"))
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
        shutil.rmtree(self.workspace, ignore_errors=True)

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        c = sqlite3.connect(str(self.tmp))
        c.row_factory = sqlite3.Row
        return c

    def _rows(self, sql, args=()):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()

    def _one(self, sql, args=()):
        c = self._conn()
        try:
            row = c.execute(sql, args).fetchone()
            return row[0] if row else None
        finally:
            c.close()

    def _deal(self, deal_id):
        return self._rows("SELECT * FROM deals WHERE id = ?", (deal_id,))[0]

    def _project(self, project_id):
        rows = self._rows("SELECT * FROM projects WHERE id = ?", (project_id,))
        return rows[0] if rows else None

    def _project_count(self):
        return self._one("SELECT COUNT(*) FROM projects")

    def _delivered_events(self, deal_id):
        return self._rows(
            "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = 'delivered_link'",
            (deal_id,))

    def _seed(self):
        """Two accounts, four deals, two projects — everything the verb branches
        on, and nothing that depends on live data."""
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        for aid, name in ((ACCOUNT, ACCOUNT_NAME), (OTHER_ACCOUNT, "Other Deliver Client")):
            c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                      (aid, name, NOW))
        c.execute("INSERT INTO contacts (id, account_id, name, created_at) VALUES (?,?,?,?)",
                  ("cont_dlv", ACCOUNT, "Ada Lovelace", NOW))
        for pid, slug, name, archived in (
                (EXISTING_PROJECT, "dlv-existing", "Existing Delivery", None),
                (ARCHIVED_PROJECT, "dlv-archived", "Archived Delivery", NOW)):
            c.execute("INSERT INTO projects (id, slug, name, created_at, archived_at) "
                      "VALUES (?,?,?,?,?)", (pid, slug, name, NOW, archived))
        for did, aid, title, stage in (
                ("deal_dlv_won", ACCOUNT, "Won deal one", "won"),
                ("deal_dlv_won2", ACCOUNT, "Won deal two (same client)", "won"),
                ("deal_dlv_other", OTHER_ACCOUNT, "Won deal, other client", "won"),
                ("deal_dlv_open", ACCOUNT, "Still selling", "proposal")):
            c.execute("INSERT INTO deals (id, account_id, title, stage, value, created_at, "
                      "updated_at) VALUES (?,?,?,?,?,?,?)",
                      (did, aid, title, stage, 1000.0, NOW, NOW))
        c.commit()
        c.close()


class Join(_DeliverCase):
    """The write the whole step exists for."""

    def test_delivering_into_an_existing_project_creates_the_join(self):
        res = _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered", res)
        self.assertEqual(res["project_id"], EXISTING_PROJECT)
        self.assertFalse(res["created_project"])

        self.assertEqual(self._deal("deal_dlv_won")["project_id"], EXISTING_PROJECT)
        project = self._project(EXISTING_PROJECT)
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["account_id"], ACCOUNT)

        events = self._delivered_events("deal_dlv_won")
        self.assertEqual(len(events), 1)
        self.assertIn(EXISTING_PROJECT, events[0]["payload"])

    def test_new_project_branch_creates_an_active_project_bound_to_the_account(self):
        before = self._project_count()
        res = _crm.deliver_deal("deal_dlv_won", new_project_name=ACCOUNT_NAME)
        self.assertEqual(res["status"], "delivered", res)
        self.assertTrue(res["created_project"])
        self.assertEqual(self._project_count(), before + 1)

        project = self._project(res["project_id"])
        self.assertEqual(project["name"], ACCOUNT_NAME)
        self.assertEqual(project["slug"], "deliver-test-client")
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["account_id"], ACCOUNT)
        self.assertIsNone(project["repo_path"])
        self.assertEqual(self._deal("deal_dlv_won")["project_id"], res["project_id"])

    def test_new_project_preserves_a_valid_repo_path_when_supplied(self):
        res = _crm.deliver_deal("deal_dlv_won", new_project_name=ACCOUNT_NAME,
                                repo_path=str(self.workspace))
        self.assertEqual(res["status"], "delivered", res)
        self.assertEqual(self._project(res["project_id"])["repo_path"],
                         str(self.workspace.resolve()))

    def test_a_colliding_slug_does_not_block_the_delivery(self):
        """projects.slug is NOT NULL UNIQUE. Two clients whose names slugify the
        same (or one client delivered twice under different deals) would raise
        inside the INSERT and surface as a 500 on the one action that must never
        fail — so the collision is resolved, not risked."""
        first = _crm.deliver_deal("deal_dlv_won", new_project_name=ACCOUNT_NAME,
                                  repo_path=str(self.workspace))
        second_workspace = Path(tempfile.mkdtemp(prefix="proposal_workspace_second_"))
        self.addCleanup(shutil.rmtree, second_workspace, True)
        second = _crm.deliver_deal("deal_dlv_other", new_project_name=ACCOUNT_NAME,
                                   repo_path=str(second_workspace))
        self.assertEqual(second["status"], "delivered", second)
        self.assertNotEqual(second["project_id"], first["project_id"])
        slugs = {self._project(first["project_id"])["slug"],
                 self._project(second["project_id"])["slug"]}
        self.assertEqual(slugs, {"deliver-test-client", "deliver-test-client-2"})

    def test_many_deals_land_on_one_project(self):
        """The Acme shape (spec §1): three deals fund one delivery. The
        second deal must be able to name the first deal's project."""
        _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        res = _crm.deliver_deal("deal_dlv_won2", project_id=EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered", res)
        self.assertEqual(
            {r["project_id"] for r in self._rows(
                "SELECT project_id FROM deals WHERE account_id = ? AND stage = 'won'",
                (ACCOUNT,))},
            {EXISTING_PROJECT})

    def test_a_second_account_never_re_points_an_existing_binding(self):
        """`account_id` is COALESCEd, not overwritten: this verb owns the
        deal→project join, not account administration. Silently rebinding a
        project to another client would rewrite someone else's delivery history."""
        _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        res = _crm.deliver_deal("deal_dlv_other", project_id=EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered", res)
        self.assertEqual(self._project(EXISTING_PROJECT)["account_id"], ACCOUNT)


class Guards(_DeliverCase):
    """Every refusal is typed — and leaves nothing behind."""

    def test_a_deal_that_is_not_won_is_refused(self):
        before = self._project_count()
        res = _crm.deliver_deal("deal_dlv_open", project_id=EXISTING_PROJECT)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "not_won")
        self.assertIsNone(self._deal("deal_dlv_open")["project_id"])
        self.assertIsNone(self._project(EXISTING_PROJECT)["status"])
        self.assertEqual(self._project_count(), before)

    def test_an_unknown_deal_is_refused(self):
        res = _crm.deliver_deal("deal_does_not_exist", project_id=EXISTING_PROJECT)
        self.assertEqual(res["code"], "not_found")

    def test_a_missing_target_is_refused_rather_than_guessed(self):
        """The modal supplies the default (the account's project, else the
        account name). The API stays explicit *because* of that: a silent
        server-side default would mint a second project for a client that
        already has one — the exact shape the join direction exists to prevent."""
        res = _crm.deliver_deal("deal_dlv_won")
        self.assertEqual(res["code"], "project_required")
        self.assertIsNone(self._deal("deal_dlv_won")["project_id"])

    def test_new_project_refuses_an_invalid_supplied_repo_path(self):
        before = self._project_count()
        relative = _crm.deliver_deal("deal_dlv_won", new_project_name="Relative",
                                     repo_path="relative/path")
        self.assertEqual(relative["code"], "repo_path_invalid")
        self.assertEqual(self._project_count(), before)

    def test_an_unknown_or_archived_project_is_refused_before_anything_is_created(self):
        before = self._project_count()
        unknown = _crm.deliver_deal("deal_dlv_won", project_id="proj_nope")
        self.assertEqual(unknown["code"], "project_not_found")
        archived = _crm.deliver_deal("deal_dlv_won", project_id=ARCHIVED_PROJECT)
        self.assertEqual(archived["code"], "project_archived")
        self.assertEqual(self._project_count(), before)
        self.assertIsNone(self._deal("deal_dlv_won")["project_id"])

    def test_a_second_delivery_is_idempotent(self):
        first = _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        count = self._project_count()
        second = _crm.deliver_deal("deal_dlv_won", new_project_name="Should Never Exist")
        self.assertEqual(second["status"], "already_delivered")
        self.assertEqual(second["project_id"], first["project_id"])
        # No re-point, no second project, no second audit row.
        self.assertEqual(self._deal("deal_dlv_won")["project_id"], EXISTING_PROJECT)
        self.assertEqual(self._project_count(), count)
        self.assertEqual(len(self._delivered_events("deal_dlv_won")), 1)


class DrawerContext(_DeliverCase):
    """What the drawer reads — the other half of the "zero required decisions"
    contract lives in context.py, so it is pinned here next to the verb."""

    def test_account_is_a_navigable_entity_type(self):
        self.assertIn("account", _context.ENTITY_TYPES)
        ctx = _context.build_context("account", ACCOUNT)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["entity"]["title"], ACCOUNT_NAME)
        kinds = {c["type"] for c in ctx["children"]}
        self.assertLessEqual({"deal", "contact"}, kinds)
        self.assertIsNone(_context.build_context("account", "acct_nope"))

    def test_the_deal_breadcrumb_no_longer_dead_ends_on_the_account(self):
        ctx = _context.build_context("deal", "deal_dlv_won")
        account = [a for a in ctx["ancestors"] if a["type"] == "account"][0]
        self.assertTrue(account["clickable"])
        self.assertIn("deliver", ctx["actions"])

    def test_the_delivering_project_shows_up_on_both_ends(self):
        _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        deal_children = _context.build_context("deal", "deal_dlv_won")["children"]
        self.assertIn(EXISTING_PROJECT, [c["id"] for c in deal_children if c["type"] == "project"])
        account_children = _context.build_context("account", ACCOUNT)["children"]
        self.assertIn(EXISTING_PROJECT,
                      [c["id"] for c in account_children if c["type"] == "project"])

    def test_the_picker_opens_with_a_default_only_once_the_client_has_a_project(self):
        before = _context.build_context("deal", "deal_dlv_won2")["entity"]["deliver"]
        self.assertIsNone(before["default_project_id"])
        self.assertEqual(before["new_project_name"], ACCOUNT_NAME)
        self.assertTrue(before["projects"])          # escape hatch is populated
        self.assertFalse(any(p["account"] for p in before["projects"]))

        _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)

        after = _context.build_context("deal", "deal_dlv_won2")["entity"]["deliver"]
        self.assertEqual(after["default_project_id"], EXISTING_PROJECT)
        self.assertEqual(after["projects"][0]["id"], EXISTING_PROJECT)
        self.assertTrue(after["projects"][0]["account"])
        # The archived project is never offered as a delivery target.
        self.assertNotIn(ARCHIVED_PROJECT, [p["id"] for p in after["projects"]])


class MarkDelivered(_DeliverCase):
    """Conversion verb 3/3 — the delivery leg's terminal state.

    REPLACED CONTRACT (ruling 2, journey fase 1 step 3). The previous version of
    this block asserted that marking a project delivered ALSO moved every won
    deal into `stage = 'delivered'`. That was the CRITICAL-2 bug: it made a
    commercial fact (this deal was won) depend on a delivery fact, so the
    pipeline lost the deal the moment the work shipped, and two different
    truths — "the money landed" and "the work shipped" — shared one column.

    The truth now: **a won deal stays won forever.** Delivery lives on
    `projects.status = 'delivered'`, and every "delivered" READ derives from
    `deals.project_id → projects.status`. So the assertions below are the exact
    inverse of the ones they replace, and they were run against the pre-change
    code and observed RED (the old verb wrote 'delivered' onto both deals)
    before the rewrite landed — an unfalsified contract tests nothing.
    """

    def _deliver_two(self):
        """Two won deals on one project, plus an open one that must not move."""
        _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        _crm.deliver_deal("deal_dlv_won2", project_id=EXISTING_PROJECT)
        c = self._conn()
        try:
            # A live deal pointed at the same project: delivering the PROJECT
            # must not sweep open pipeline closed.
            c.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                      (EXISTING_PROJECT, "deal_dlv_open"))
            c.commit()
        finally:
            c.close()

    def test_marking_delivered_leaves_every_funding_deal_won(self):
        """THE red-proof assertion of step 3: the verb never touches
        `deals.stage`. Pre-change this read 'delivered' for both deals."""
        self._deliver_two()
        before = {did: self._deal(did)["closed_at"]
                  for did in ("deal_dlv_won", "deal_dlv_won2")}
        res = _crm.mark_project_delivered(EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered", res)

        for did in ("deal_dlv_won", "deal_dlv_won2"):
            deal = self._deal(did)
            self.assertEqual(deal["stage"], "won",
                             f"{did} must still be won — won is terminal commercial success")
            # ...and the delivery did not restamp the sales close either.
            self.assertEqual(deal["closed_at"], before[did])

    def test_the_project_carries_the_delivery_and_the_deals_are_only_named(self):
        self._deliver_two()
        res = _crm.mark_project_delivered(EXISTING_PROJECT)
        # Renamed with the semantics: nothing MOVES any more, the deals are the
        # ones this delivery covers.
        self.assertEqual(sorted(d["id"] for d in res["delivered_deals"]),
                         ["deal_dlv_won", "deal_dlv_won2"])
        self.assertNotIn("moved_deals", res)

        project = self._project(EXISTING_PROJECT)
        self.assertEqual(project["status"], "delivered")
        self.assertTrue(project["delivered_at"], "delivered_at must be stamped")
        self.assertEqual(project["delivered_at"], res["delivered_at"])

    def test_the_status_goes_through_the_single_lifecycle_writer(self):
        """Ruling 8: `projects.status` has exactly one runtime writer, and it
        receives this verb's transaction. Proven by making the writer fail — if
        the verb wrote the column itself, the project would still flip."""
        self._deliver_two()
        original = _sprints.set_project_status
        calls = []

        def _boom(conn, project_id, status, *, via):
            calls.append((project_id, status, via))
            raise RuntimeError("lifecycle writer refused")

        _sprints.set_project_status = _boom
        try:
            with self.assertRaises(RuntimeError):
                _crm.mark_project_delivered(EXISTING_PROJECT)
        finally:
            _sprints.set_project_status = original
        self.assertEqual(calls, [(EXISTING_PROJECT, "delivered", "mark_project_delivered")])
        # Nothing committed: neither the status (still `active`, where
        # deliver_deal — routed through the same writer — left it) nor the audit
        # rows.
        self.assertEqual(self._project(EXISTING_PROJECT)["status"], "active")
        self.assertEqual(self._rows(
            "SELECT id FROM deal_events WHERE kind = 'project_delivered' "
            "AND deal_id IN (?, ?)",
            ("deal_dlv_won", "deal_dlv_won2")), [])

    def test_the_audit_row_is_one_project_delivered_event_and_no_stage_change(self):
        """There is no project_events table in this schema, so the audit row is
        a deal_event per covered deal — but a `stage_changed` row would now be a
        lie about a stage that never changed."""
        self._deliver_two()
        _crm.mark_project_delivered(EXISTING_PROJECT)
        for did in ("deal_dlv_won", "deal_dlv_won2"):
            kinds = [r["kind"] for r in self._rows(
                "SELECT kind FROM deal_events WHERE deal_id = ? ORDER BY id", (did,))]
            self.assertEqual(kinds.count("project_delivered"), 1, kinds)
            payloads = [r["payload"] for r in self._rows(
                "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = 'stage_changed'",
                (did,))]
            self.assertFalse([p for p in payloads if "delivered" in (p or "")], payloads)

    def test_the_delivered_history_bucket_is_derived_not_stored(self):
        """The pipeline's `delivered` column is now a READ over the join: won
        deals whose project is delivered. They leave the `won` column, and no
        row anywhere carries `stage = 'delivered'`."""
        self._deliver_two()
        _crm.mark_project_delivered(EXISTING_PROJECT)
        pipe = _crm.pipeline()
        delivered_ids = {d["id"] for d in pipe["by_stage"]["delivered"]}
        won_ids = {d["id"] for d in pipe["by_stage"]["won"]}
        self.assertEqual({"deal_dlv_won", "deal_dlv_won2"} & delivered_ids,
                         {"deal_dlv_won", "deal_dlv_won2"})
        self.assertFalse({"deal_dlv_won", "deal_dlv_won2"} & won_ids)
        # The won deal of the OTHER (undelivered) project stays in the column.
        self.assertIn("deal_dlv_other", won_ids)
        self.assertEqual(pipe["counts"]["delivered"], len(pipe["by_stage"]["delivered"]))
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM deals WHERE stage = 'delivered'"), 0)

    def test_non_won_deals_are_untouched(self):
        self._deliver_two()
        _crm.mark_project_delivered(EXISTING_PROJECT)
        still_open = self._deal("deal_dlv_open")
        self.assertEqual(still_open["stage"], "proposal")
        self.assertIsNone(still_open["closed_at"])
        # ...and a won deal belonging to ANOTHER project is not swept either.
        self.assertEqual(self._deal("deal_dlv_other")["stage"], "won")
        # An open deal pointed at a delivered project is NOT delivered history.
        self.assertNotIn("deal_dlv_open",
                         {d["id"] for d in _crm.pipeline()["by_stage"]["delivered"]})

    def test_a_second_call_is_idempotent_and_covers_nothing(self):
        self._deliver_two()
        first = _crm.mark_project_delivered(EXISTING_PROJECT)
        # A deal that becomes won AFTER the project was delivered must not be
        # re-evented by a replayed verb.
        c = self._conn()
        try:
            c.execute("UPDATE deals SET stage = 'won', project_id = ? WHERE id = ?",
                      (EXISTING_PROJECT, "deal_dlv_open"))
            c.commit()
        finally:
            c.close()
        second = _crm.mark_project_delivered(EXISTING_PROJECT)
        self.assertEqual(second["status"], "already_delivered")
        self.assertEqual(second["delivered_deals"], [])
        self.assertEqual(second["delivered_at"], first["delivered_at"])
        self.assertEqual(self._deal("deal_dlv_open")["stage"], "won")
        self.assertEqual(self._rows(
            "SELECT id FROM deal_events WHERE deal_id = ? AND kind = 'project_delivered'",
            ("deal_dlv_open",)), [])

    def test_an_unknown_project_is_refused(self):
        res = _crm.mark_project_delivered("proj_does_not_exist")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "not_found")

    def test_a_project_with_no_deals_still_closes(self):
        res = _crm.mark_project_delivered(EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered")
        self.assertEqual(res["delivered_deals"], [])
        self.assertEqual(self._project(EXISTING_PROJECT)["status"], "delivered")

    def test_the_route_is_human_only_and_types_its_failures(self):
        self._deliver_two()
        ok = _api.api_project_mark_delivered(EXISTING_PROJECT)
        self.assertEqual(ok["status"], "delivered")
        again = _api.api_project_mark_delivered(EXISTING_PROJECT)
        self.assertEqual(again["status"], "already_delivered")
        with self.assertRaises(_api.HTTPException) as raised:
            _api.api_project_mark_delivered("proj_nope")
        self.assertEqual(raised.exception.status_code, 404)
        # Red line 11 again: the third conversion verb is no more agent-reachable
        # than the second. The absence from the MCP surface IS the guard.
        self.assertIn("/api/projects/{project_id}/delivered",
                      {getattr(r, "path", None) for r in app.routes})
        self.assertNotIn("mark_project_delivered", (REPO / "mcp_server.py").read_text())

    def test_the_drawer_offers_the_verb_exactly_when_it_applies(self):
        """The project drawer's action footer had ONE action (+ New task here);
        the verb has to be reachable from the spine or it does not exist."""
        src = (REPO / "dashboard" / "templates" / "index.html").read_text()
        self.assertIn("markProjectDelivered(", src)
        self.assertIn("✅ Entregado", src)
        self.assertIn("/delivered`", src)
        # Offered only while it applies — never a control the eye must learn to skip.
        self.assertIn("e.status !== 'delivered'", src)
        # ...and the context builder has to carry the status the branch reads.
        ctx = _context.build_context("project", EXISTING_PROJECT)
        self.assertIn("status", ctx["entity"])
        _crm.mark_project_delivered(EXISTING_PROJECT)
        self.assertEqual(_context.build_context("project", EXISTING_PROJECT)["entity"]["status"],
                         "delivered")


class SuggestInvoice(_DeliverCase):
    """m17/F3 — delivering is the natural moment to invoice: the response
    carries the hint (the drawer opens the 💵 confirm on it); the invoice
    stamp itself stays a separate human tap, never written here."""

    def test_deliver_returns_suggest_invoice_when_uninvoiced(self):
        res = _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered", res)
        self.assertTrue(res["suggest_invoice"])
        # A hint, not a write: the stamp is still NULL.
        self.assertIsNone(self._deal("deal_dlv_won")["invoiced_at"])

    def test_an_already_invoiced_deal_gets_no_suggestion(self):
        c = self._conn()
        c.execute("UPDATE deals SET invoiced_at = 1754000000 "
                  "WHERE id = 'deal_dlv_won'")
        c.commit()
        c.close()
        res = _crm.deliver_deal("deal_dlv_won", project_id=EXISTING_PROJECT)
        self.assertEqual(res["status"], "delivered", res)
        self.assertFalse(res["suggest_invoice"])


class Route(_DeliverCase):
    """The one route, and the deliberate absence of its MCP twin."""

    def test_the_route_delivers_and_types_its_failures(self):
        ok = _api.api_crm_deliver_deal(
            "deal_dlv_won", {"project_id": EXISTING_PROJECT})
        self.assertEqual(ok["status"], "delivered")
        self.assertEqual(self._deal("deal_dlv_won")["project_id"], EXISTING_PROJECT)

        again = _api.api_crm_deliver_deal(
            "deal_dlv_won", {"project_id": EXISTING_PROJECT})
        self.assertEqual(again["status"], "already_delivered")

        with self.assertRaises(_api.HTTPException) as missing:
            _api.api_crm_deliver_deal(
                "deal_nope", {"project_id": EXISTING_PROJECT})
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(_api.HTTPException) as not_won:
            _api.api_crm_deliver_deal(
                "deal_dlv_open", {"project_id": EXISTING_PROJECT})
        self.assertEqual(not_won.exception.status_code, 400)

    def test_the_new_project_branch_works_through_the_api_handler(self):
        body = _api.api_crm_deliver_deal(
            "deal_dlv_won2", {"new_project_name": "Http Created Delivery"})
        self.assertTrue(body["created_project"])
        self.assertEqual(self._project(body["project_id"])["account_id"], ACCOUNT)
        self.assertIsNone(self._project(body["project_id"])["repo_path"])

    def test_modal_only_sends_repo_path_when_the_optional_field_has_a_value(self):
        source = (REPO / "dashboard" / "templates" / "index.html").read_text()
        self.assertIn("Project folder <span", source)
        self.assertIn("if (picked === '__new__' && repoPath) body.repo_path = repoPath", source)
        self.assertNotIn("picked === '__new__' && !repoPath", source)

    def test_a_conversion_verb_is_not_reachable_from_an_agent(self):
        """Red line 11: agents may never fire a conversion verb — they propose it
        into the brief and the operator taps. The absence from the MCP surface IS the
        guard, so a future parity sweep has to fail this test to remove it."""
        self.assertIn("/api/crm/deals/{deal_id}/deliver",
                      {getattr(r, "path", None) for r in app.routes})
        mcp = (REPO / "mcp_server.py").read_text()
        self.assertNotIn("deliver_deal", mcp)
        self.assertNotIn("deliver_deal", (REPO / "dashboard" / "mcp_server.py").read_text()
                         if (REPO / "dashboard" / "mcp_server.py").exists() else "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
