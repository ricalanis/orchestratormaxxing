"""Contract for the m06 spine: `tasks.deal_id`, its three writers, and the
reads that only became possible once a task could name its client.

Journey fase 1, step 4. Four things are tested, and each one exists because the
same failure has a cheap, plausible, WRONG version:

  * **The migration's floor is in the storage engine, not in Python.** The
    partial UNIQUE index is what makes "one open cadence task per deal" true for
    a writer that never goes through `cadence.py` — the materializer runs from
    more than one entry point, so an application-only rule is a rule that
    survives until the next caller (spec regla 7). Asserted by making SQLite
    refuse the second row, and by proving each conjunct of the partial predicate
    does something: a HUMAN may open five tasks on one deal, and a settled task
    must free the slot.

  * **Exactly three writers, and the generic PATCH is not one of them**
    (ruling 5). Pydantic ignores unknown fields, so the tempting version of this
    — leave `deal_id` off `TaskUpdate` — answers **200** to a body that sets it
    and writes nothing: the caller believes the link landed. The refusal is
    therefore asserted as a typed 400 AND by reading the column back.

  * **The drilldown red-proof.** Before this step `deal_drilldown` walked
    `deals.initiative_id → epics → tasks`, so a deal that had never been wired
    to a quarterly bet answered `{initiative: None, tasks: []}` — i.e. every
    real deal on the board. The test fabricates exactly that deal (a deal task,
    no initiative) and demands its tasks back. It is red against pre-step-4 code
    by construction, not by hope.

  * **The ancestors red-proof.** `context._project_context` returned a
    hard-coded `[]`, making the project the only entity in the drawer with no
    breadcrumb. A project with an account and two deals must now produce all
    three, in outside-in order.

DB isolation: a COPY of the session sandbox per test with `runner.run()` on top,
so the REAL migrated shape is exercised rather than a hand-rolled one, plus a
self-contained fixture spine. `runner.run_backup` is stubbed and the hermes CLI
is never invoked (the create path stubs `subprocess.run` — the CLI's own create
is not what step 4 adds, and shelling out would write outside the per-test copy).
The operator's live DB is never opened.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_task_deal_writers.py  # from orchestrator/
"""
import atexit
import json
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

# Outside the availability guard: the modules under test. An ImportError here is
# a deleted feature, not a skippable environment problem.
from dashboard import stagekind as _stagekind
from dashboard.migrations import m06_task_deal as _m06

_READY = False
_CLIENT = None
_IMPORT_DB = None
try:
    from dashboard import db as _db, sprints as _sprints, crm as _crm
    from dashboard import context as _context, canvas as _canvas
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ the per-session sandbox copy tests/conftest.py exports, never the live DB.
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_m06_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        from dashboard import api as _api
        from dashboard.api import app
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
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
ACCOUNT = "acct_m06"
DEAL_WON = "deal_m06_won"          # won, delivered by PROJECT
DEAL_LEAD = "deal_m06_lead"        # lead, no project — the sales-task case
PROJECT = "proj_m06"
TASK_DEAL = "task_m06_deal"        # carries deal_id, no project
TASK_PROJECT = "task_m06_proj"     # project task of the delivering project
TASK_BOTH = "task_m06_both"        # both — must appear ONCE in the union
TASK_PLAIN = "task_m06_plain"      # neither — must never appear


class _SpineCase(unittest.TestCase):

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_m06_test_", suffix=".db")
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
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  (ACCOUNT, "M06 Client Co", NOW))
        c.execute("INSERT INTO projects (id, slug, name, status, account_id, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (PROJECT, "m06-delivery", "M06 Delivery", "active", ACCOUNT, NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, currency, "
                  "project_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (DEAL_WON, ACCOUNT, "M06 won deal", "won", 120000.0, "MXN",
                   PROJECT, NOW, NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, currency, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (DEAL_LEAD, ACCOUNT, "M06 lead deal", "lead", 30000.0, "MXN", NOW, NOW))
        for tid, project_id, deal_id, status in (
                (TASK_DEAL, None, DEAL_LEAD, "backlog"),
                (TASK_PROJECT, PROJECT, None, "in_progress"),
                (TASK_BOTH, PROJECT, DEAL_WON, "review"),
                (TASK_PLAIN, None, None, "backlog")):
            c.execute("INSERT INTO tasks (id, title, status, created_at, created_by, "
                      "project_id, deal_id) VALUES (?,?,?,?,?,?,?)",
                      (tid, f"m06 {tid}", status, NOW, "ricardo", project_id, deal_id))
        c.commit()
        c.close()

    def _col(self, task_id, column="deal_id"):
        c = self._conn()
        try:
            row = c.execute(f"SELECT {column} FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return row[column] if row else None
        finally:
            c.close()

    def _events(self, task_id):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "ORDER BY created_at, rowid", (task_id,))]
        finally:
            c.close()

    def _new_task(self, tid, **cols):
        c = self._conn()
        try:
            keys = ["id", "title", "status", "created_at"] + list(cols)
            vals = [tid, f"m06 {tid}", cols.pop("status", "backlog"), NOW] + list(cols.values())
            keys = [k for k in keys if k != "status"] + ["status"]
            c.execute("INSERT INTO tasks (id, title, created_at, status" +
                      "".join(f", {k}" for k in cols) + ") VALUES (?,?,?,?" +
                      ",?" * len(cols) + ")",
                      [tid, f"m06 {tid}", NOW, "backlog"] + list(cols.values()))
            c.commit()
        finally:
            c.close()


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class Migration(_SpineCase):
    """m06 is registered, ledgered, and installed a floor the engine enforces."""

    def test_the_migration_is_registered_and_ledgered(self):
        self.assertIn("m06_task_deal", [n for n, _ in runner.MIGRATIONS])
        c = self._conn()
        try:
            names = {r[0] for r in c.execute("SELECT name FROM orch_migrations")}
        finally:
            c.close()
        self.assertIn("m06_task_deal", names)

    def test_both_columns_exist(self):
        c = self._conn()
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        finally:
            c.close()
        self.assertIn("deal_id", cols)
        self.assertIn("stage_kind", cols)

    def test_the_two_indexes_landed_on_tasks(self):
        # Index names are GLOBAL in SQLite: `IF NOT EXISTS` silently skips a name
        # already taken by an index on ANOTHER table, which would leave the
        # uniqueness floor absent while the migration ledgered itself as applied.
        c = self._conn()
        try:
            ours = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'")}
        finally:
            c.close()
        for name in _m06.INDEXES:
            self.assertIn(name, ours)

    def test_the_migration_is_idempotent(self):
        runner.run()
        runner.run()
        c = self._conn()
        try:
            n = c.execute("SELECT COUNT(*) FROM orch_migrations "
                          "WHERE name = 'm06_task_deal'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 1)

    def test_stage_kind_was_NOT_backfilled(self):
        # A NULL means "ask the rule". A backfill would freeze today's answer
        # into the row and the task would stop moving through the cycle when its
        # deal did — the reason this migration writes no data at all.
        # Scope: the MIGRATION must stamp nothing. The live copy legitimately
        # carries cadence-minted tasks whose stage_kind the materializer stamps
        # (created_by='cadence') — whole-table emptiness would rot on every
        # sandbox refresh (the thread_id/attachments lesson, third time).
        c = self._conn()
        try:
            n = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE stage_kind IS NOT NULL "
                "AND COALESCE(created_by,'') != 'cadence'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 0)

    def test_the_check_constraint_refuses_a_stage_outside_the_vocabulary(self):
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE tasks SET stage_kind = 'nope' WHERE id = ?", (TASK_DEAL,))
            for kind in _stagekind.STAGE_KINDS:
                c.execute("UPDATE tasks SET stage_kind = ? WHERE id = ?", (kind, TASK_DEAL))
            c.rollback()
        finally:
            c.close()


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class CadenceUniqueness(_SpineCase):
    """Ruling 7: at most ONE open cadence-minted task per deal — in the engine."""

    def _cadence(self, c, tid, status="backlog", deal_id=DEAL_LEAD, created_by="cadence"):
        c.execute("INSERT INTO tasks (id, title, status, created_at, created_by, deal_id) "
                  "VALUES (?,?,?,?,?,?)", (tid, tid, status, NOW, created_by, deal_id))

    def test_a_second_open_cadence_task_on_the_same_deal_is_refused(self):
        c = self._conn()
        try:
            self._cadence(c, "cad_1")
            c.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                self._cadence(c, "cad_2", status="ready")
        finally:
            c.close()

    def test_a_HUMAN_may_open_as_many_as_they_like(self):
        # The constraint governs the robot, never the operator.
        c = self._conn()
        try:
            self._cadence(c, "hum_1", created_by="ricardo")
            self._cadence(c, "hum_2", created_by="ricardo")
            self._cadence(c, "hum_3", created_by="web")
            c.commit()
            n = c.execute("SELECT COUNT(*) FROM tasks WHERE deal_id = ? AND created_by != 'cadence'",
                          (DEAL_LEAD,)).fetchone()[0]
        finally:
            c.close()
        self.assertGreaterEqual(n, 3)

    def test_a_SETTLED_cadence_task_frees_the_slot(self):
        # Without `status NOT IN (done, rejected, cancelled)` in the partial
        # predicate, a deal could never be nagged again after its first task
        # closed — the constraint would become a permanent gag.
        for settled in ("done", "rejected", "cancelled"):
            with self.subTest(settled=settled):
                c = self._conn()
                try:
                    self._cadence(c, f"cad_{settled}_a")
                    c.commit()
                    c.execute("UPDATE tasks SET status = ? WHERE id = ?",
                              (settled, f"cad_{settled}_a"))
                    c.commit()
                    self._cadence(c, f"cad_{settled}_b")     # must not raise
                    c.commit()
                    c.execute("DELETE FROM tasks WHERE id IN (?, ?)",
                              (f"cad_{settled}_a", f"cad_{settled}_b"))
                    c.commit()
                finally:
                    c.close()

    def test_deal_less_cadence_tasks_do_not_collide(self):
        # NULLs are distinct in a SQLite UNIQUE index, but being explicit in the
        # partial predicate is what makes the intent readable — and asserting it
        # is what would catch someone "simplifying" the index later.
        c = self._conn()
        try:
            self._cadence(c, "cad_null_1", deal_id=None)
            self._cadence(c, "cad_null_2", deal_id=None)
            c.commit()
        finally:
            c.close()


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class Writers(_SpineCase):
    """Three writers, and no fourth."""

    def test_link_task_deal_validates_both_ids(self):
        self.assertEqual(_crm.link_task_deal("t_nope", DEAL_WON)["status"], "error")
        self.assertIn("not found", _crm.link_task_deal("t_nope", DEAL_WON)["error"])
        self.assertIn("not found", _crm.link_task_deal(TASK_PLAIN, "deal_nope")["error"])
        self.assertIn("required", _crm.link_task_deal(TASK_PLAIN, "")["error"])
        # …and none of the refusals wrote anything.
        self.assertIsNone(self._col(TASK_PLAIN))

    def test_link_task_deal_writes_the_column_and_logs_the_event(self):
        res = _crm.link_task_deal(TASK_PLAIN, DEAL_WON)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self._col(TASK_PLAIN), DEAL_WON)
        kinds = [e["kind"] for e in self._events(TASK_PLAIN)]
        self.assertIn("deal_linked", kinds)
        payload = json.loads([e for e in self._events(TASK_PLAIN)
                              if e["kind"] == "deal_linked"][-1]["payload"])
        self.assertEqual(payload["deal_id"], DEAL_WON)

    def test_the_named_route_links_and_404s_an_unknown_id(self):
        r = _CLIENT.patch(f"/api/tasks/{TASK_PLAIN}/deal", json={"deal_id": DEAL_WON})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._col(TASK_PLAIN), DEAL_WON)

        self.assertEqual(
            _CLIENT.patch(f"/api/tasks/{TASK_PLAIN}/deal",
                          json={"deal_id": "deal_nope"}).status_code, 404)
        self.assertEqual(
            _CLIENT.patch("/api/tasks/t_nope/deal", json={"deal_id": DEAL_WON}).status_code, 404)
        self.assertEqual(
            _CLIENT.patch(f"/api/tasks/{TASK_PLAIN}/deal", json={}).status_code, 400)

    def test_the_GENERIC_patch_REFUSES_deal_id(self):
        # Red line 2. The refusal is a typed 400, not a silent 200 — Pydantic
        # would have ignored the unknown field and answered success while
        # writing nothing, which is the failure this asserts against.
        r = _CLIENT.patch(f"/api/tasks/{TASK_PLAIN}", json={"deal_id": DEAL_WON})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("/api/tasks/{task_id}/deal", r.text)
        self.assertIsNone(self._col(TASK_PLAIN), "the generic patch wrote the column")

    def test_the_generic_patch_refuses_BEFORE_applying_the_legal_half(self):
        # A body that mixes a legal edit with an illegal one must write nothing —
        # a half-applied patch is worse than a rejected one, because the caller
        # cannot tell which half landed.
        r = _CLIENT.patch(f"/api/tasks/{TASK_PLAIN}",
                          json={"title": "renamed by a mixed patch", "deal_id": DEAL_WON})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self._col(TASK_PLAIN))
        self.assertNotEqual(self._col(TASK_PLAIN, "title"), "renamed by a mixed patch")

    def test_the_generic_patch_still_works_without_deal_id(self):
        r = _CLIENT.patch(f"/api/tasks/{TASK_PLAIN}", json={"title": "still editable"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._col(TASK_PLAIN, "title"), "still editable")

    def test_create_task_links_the_deal_as_a_sidecar(self):
        """POST /api/tasks with `deal_id`.

        The hermes CLI create is stubbed: it is not what this step adds, it
        writes through its own connection (outside this per-test copy), and
        requiring the binary would make the contract depend on the machine.
        What IS under test is everything after it — the sidecar link, its
        audit event, and the response flag.
        """
        created_id = "task_m06_created"

        class _Result:
            returncode = 0
            stdout = json.dumps({"id": created_id})
            stderr = ""

        def _fake_run(cmd, *a, **kw):
            c = self._conn()
            try:
                c.execute("INSERT INTO tasks (id, title, status, created_at, created_by) "
                          "VALUES (?,?,?,?,?)",
                          (created_id, "created with a deal", "backlog", NOW, "ricardo"))
                c.commit()
            finally:
                c.close()
            return _Result()

        real_run = _api.subprocess.run
        _api.subprocess.run = _fake_run
        try:
            r = _CLIENT.post("/api/tasks", json={"title": "created with a deal",
                                                 "deal_id": DEAL_WON})
        finally:
            _api.subprocess.run = real_run

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["deal_linked"], body)
        self.assertEqual(self._col(created_id), DEAL_WON)
        self.assertIn("deal_linked", [e["kind"] for e in self._events(created_id)])

    def test_create_task_reports_a_refused_deal_as_a_warning_not_a_500(self):
        created_id = "task_m06_created_bad"

        class _Result:
            returncode = 0
            stdout = json.dumps({"id": created_id})
            stderr = ""

        def _fake_run(cmd, *a, **kw):
            c = self._conn()
            try:
                c.execute("INSERT INTO tasks (id, title, status, created_at, created_by) "
                          "VALUES (?,?,?,?,?)",
                          (created_id, "bad deal", "backlog", NOW, "ricardo"))
                c.commit()
            finally:
                c.close()
            return _Result()

        real_run = _api.subprocess.run
        _api.subprocess.run = _fake_run
        try:
            r = _CLIENT.post("/api/tasks", json={"title": "bad deal", "deal_id": "deal_nope"})
        finally:
            _api.subprocess.run = real_run

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["deal_linked"])
        self.assertTrue(any("deal link failed" in w for w in body["warnings"]), body)
        self.assertIsNone(self._col(created_id))


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class DrilldownSpine(_SpineCase):
    """RED BEFORE STEP 4: a deal with no initiative answered `tasks: []`."""

    def test_a_deal_with_only_a_deal_task_returns_it(self):
        # DEAL_LEAD has no initiative, no project, and one task that names it.
        # Pre-step-4 `deal_drilldown` returned early on the missing initiative:
        # `{initiative: None, tasks: []}`.
        d = _crm.deal_drilldown(DEAL_LEAD)
        ids = [t["id"] for t in d["tasks"]]
        self.assertIn(TASK_DEAL, ids)
        self.assertIsNone(d["initiative"])

    def test_the_union_includes_the_delivering_projects_tasks(self):
        d = _crm.deal_drilldown(DEAL_WON)
        ids = [t["id"] for t in d["tasks"]]
        self.assertIn(TASK_PROJECT, ids, "the delivery leg is missing")
        self.assertIn(TASK_BOTH, ids)
        self.assertNotIn(TASK_PLAIN, ids, "an unrelated task leaked into the drilldown")

    def test_a_task_that_is_both_appears_exactly_once(self):
        d = _crm.deal_drilldown(DEAL_WON)
        ids = [t["id"] for t in d["tasks"]]
        self.assertEqual(ids.count(TASK_BOTH), 1)

    def test_the_groups_are_stage_kind_then_board_column(self):
        d = _crm.deal_drilldown(DEAL_WON)
        self.assertTrue(d["groups"], "no stage groups were built")
        kinds = [g["stage_kind"] for g in d["groups"]]
        # won deal + active project → ejecucion, per the derivation table.
        self.assertIn("ejecucion", kinds)
        group = [g for g in d["groups"] if g["stage_kind"] == "ejecucion"][0]
        self.assertEqual(group["count"], sum(c["count"] for c in group["columns"]))
        columns = {c["column"] for c in group["columns"]}
        self.assertIn("in_progress", columns)   # TASK_PROJECT
        self.assertIn("review", columns)        # TASK_BOTH
        # Every task in the flat list is in exactly one group cell.
        grouped = [t["id"] for g in d["groups"] for c in g["columns"] for t in c["tasks"]]
        self.assertEqual(sorted(grouped), sorted(t["id"] for t in d["tasks"]))

    def test_the_sales_task_of_a_lead_deal_groups_as_contacto(self):
        d = _crm.deal_drilldown(DEAL_LEAD)
        kinds = [g["stage_kind"] for g in d["groups"]]
        self.assertEqual(kinds, ["contacto"])

    def test_the_endpoint_serves_the_same_shape(self):
        r = _CLIENT.get(f"/api/crm/deals/{DEAL_WON}/drilldown")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("groups", body)
        self.assertIn(TASK_PROJECT, [t["id"] for t in body["tasks"]])


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class ProjectAncestors(_SpineCase):
    """RED BEFORE STEP 4: `_project_context` returned a hard-coded `[]`."""

    def test_the_project_breadcrumb_carries_its_client_and_its_deals(self):
        ctx = _context.build_context("project", PROJECT)
        types = [a["type"] for a in ctx["ancestors"]]
        self.assertEqual(types[0], "account", "the client must come first")
        self.assertIn("deal", types)
        ids = [a["id"] for a in ctx["ancestors"]]
        self.assertIn(ACCOUNT, ids)
        self.assertIn(DEAL_WON, ids)

    def test_the_project_entity_carries_cliente_and_valor_entregado(self):
        e = _context.build_context("project", PROJECT)["entity"]
        self.assertEqual(e["account_name"], "M06 Client Co")
        self.assertEqual(e["delivered_value"], 120000.0)
        self.assertEqual(e["deal_count"], 1)
        self.assertEqual(e["currency"], "MXN")

    def test_a_project_with_no_commercial_lineage_still_renders(self):
        c = self._conn()
        c.execute("INSERT INTO projects (id, slug, name, status, created_at) "
                  "VALUES ('proj_m06_bare','m06-bare','M06 Bare','planned',?)", (NOW,))
        c.commit()
        c.close()
        ctx = _context.build_context("project", "proj_m06_bare")
        self.assertEqual(ctx["ancestors"], [])
        self.assertIsNone(ctx["entity"]["delivered_value"])

    def test_a_task_drawer_carries_its_deal_and_its_stage(self):
        ctx = _context.build_context("task", TASK_BOTH)
        e = ctx["entity"]
        self.assertEqual(e["deal_id"], DEAL_WON)
        self.assertEqual(e["account_name"], "M06 Client Co")
        self.assertEqual(e["stage_kind"], "ejecucion")
        types = [a["type"] for a in ctx["ancestors"]]
        self.assertEqual(types[:2], ["account", "deal"],
                         "the commercial lineage leads the breadcrumb")


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class BoardFeed(_SpineCase):
    """BOTH chokepoints widen, or the client chip stays dormant on some surface."""

    def test_the_api_tasks_feed_carries_the_client(self):
        r = _CLIENT.get("/api/tasks")
        self.assertEqual(r.status_code, 200)
        by_id = {t["id"]: t for t in r.json()["tasks"]}
        t = by_id[TASK_BOTH]
        self.assertEqual(t["deal_id"], DEAL_WON)
        self.assertEqual(t["account_name"], "M06 Client Co")
        self.assertEqual(t["account_id"], ACCOUNT)
        self.assertEqual(t["deal_title"], "M06 won deal")
        self.assertEqual(t["stage_kind"], "ejecucion")
        # …and a task with no deal is untouched: the chip falls through to the
        # project branch exactly as it did before, carrying no client.
        self.assertIsNone(by_id[TASK_PROJECT]["deal_id"])
        self.assertIsNone(by_id[TASK_PROJECT]["account_name"])
        self.assertEqual(by_id[TASK_PROJECT]["stage_kind"], "ejecucion")
        self.assertIsNone(by_id[TASK_PLAIN]["deal_id"])
        self.assertIsNone(by_id[TASK_PLAIN]["account_name"])

    def test_the_canvas_read_carries_the_same_fields(self):
        # The SECOND chokepoint (Hoy / Later / plan). The spec assumed one; both
        # must widen or the chip is live on the board and dead on Today.
        conn = _db.get_conn()
        try:
            rows = _canvas._rows(conn, "t.id IN (?,?)", (TASK_BOTH, TASK_DEAL), "t.id")
        finally:
            conn.close()
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[TASK_BOTH]["deal_id"], DEAL_WON)
        self.assertEqual(by_id[TASK_BOTH]["account_name"], "M06 Client Co")
        self.assertEqual(by_id[TASK_BOTH]["stage_kind"], "ejecucion")
        self.assertEqual(by_id[TASK_DEAL]["stage_kind"], "contacto")

    def test_an_explicit_stage_kind_survives_both_read_paths(self):
        c = self._conn()
        c.execute("UPDATE tasks SET stage_kind = 'cobranza' WHERE id = ?", (TASK_BOTH,))
        c.commit()
        c.close()
        feed = {t["id"]: t for t in _CLIENT.get("/api/tasks").json()["tasks"]}
        self.assertEqual(feed[TASK_BOTH]["stage_kind"], "cobranza")
        conn = _db.get_conn()
        try:
            rows = _canvas._rows(conn, "t.id = ?", (TASK_BOTH,), "t.id")
        finally:
            conn.close()
        self.assertEqual(rows[0]["stage_kind"], "cobranza")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
