"""Contract for attachments + the five-facet project hub (journey F3.5).

`POST/GET/DELETE /api/attachments` and `GET /api/projects/{id}/hub` are what the
four host skills (Claude · Codex plugin · Hermes · OpenCode) call when a deep
planning session ends. The writers are therefore automated, repeated, and on
four machines — which is what decides what this file tests hardest:

  * **Every refusal must be a typed 400, not a 500 and not a silent 200.**
    `node_kind` and `kind` carry CHECKs, `(url, path)` carries a CHECK, and
    `node_id` carries NO foreign key (SQLite cannot declare one whose target
    table depends on another column). So a bad enum reaching SQLite answers 500
    and teaches nothing, and a bad `node_id` reaching SQLite answers **200** and
    renders an empty facet forever — the quiet lie this phase exists to kill.
  * **Registering the same artifact twice must UPDATE, not duplicate.** A skill
    that re-runs, a planning-repo re-sync, or two hosts finishing the same plan
    must converge on one row. Asserted at both layers: the application upsert
    AND the two partial UNIQUE indexes underneath it (a rule the application
    enforces alone is a rule that survives until the next writer — spec regla 7).
  * **The partial indexes must actually be partial.** A plain
    `UNIQUE(node_kind, node_id, kind, url)` looks identical in a passing
    duplicate-url test and then rejects the second path-only row, because SQLite
    treats NULLs as distinct. Both halves are asserted.
  * **The hub UNIONS derived facts and never duplicates tasks.** Conversations
    include the Fireflies meetings of the project's deals (`deals.project_id`)
    and code includes `projects.repo_path`, each folding away when a registered
    pointer already names it. `tasks` is a COUNT over the real table — an
    attachment can never add to it.

DB isolation: a COPY of the sandbox kanban.db per test with `runner.run()` on
top (so the real, migrated table shape is exercised, not a hand-rolled one),
plus a self-contained fixture spine. `runner.run_backup` is stubbed. The
operator's live DB is never opened.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_attachments.py   # from orchestrator/
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

# Imported OUTSIDE the availability guard on purpose: a missing kanban.db is a
# legitimate skip, but a missing module is the very thing under test — swallowing
# that ImportError would turn a deleted feature into a green run.
from dashboard import attachments as _att
from dashboard.migrations import m10_attachments as _m10

_READY = False
_CLIENT = None
_IMPORT_DB = None
try:
    from dashboard import db as _db, sprints as _sprints
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ resolves to the per-session sandbox copy tests/conftest.py exports, never
    # the operator's live DB.
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_att_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
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
PROJECT = "proj_att_hub"
PROJECT_SLUG = "att-hub"
REPO_PATH = "/home/operator/dev/att-hub"
BARE_PROJECT = "proj_att_bare"      # same spine, no repo_path
ACCOUNT = "acct_att"
DEAL = "deal_att"
TRANSCRIPT = "TRX_ATT123"
MEETING = "ff_att_1"
PLAN_PATH = "att-hub/2026-08-01_hub.md"
TASK_OPEN_A = "task_att_open_a"
TASK_OPEN_B = "task_att_open_b"
TASK_DONE = "task_att_done"
TASK_ARCHIVED = "task_att_archived"


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _AttachmentsCase(unittest.TestCase):

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_att_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        runner.run()
        # The sandbox is a copy of the LIVE DB, which now legitimately carries
        # real attachment rows (the plan-to-repo skill registers them). These
        # tests assert exact row sets, so they own an empty table — emptiness
        # is a fixture precondition here, never a claim about the live system.
        conn = sqlite3.connect(str(self.tmp))
        conn.execute("DELETE FROM attachments")
        conn.commit()
        conn.close()
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
        """The spine the hub reads: project (+ repo) → deal → meeting, plus the
        four tasks that make the open/total counts distinguishable."""
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO projects (id, slug, name, created_at, repo_path) "
                  "VALUES (?,?,?,?,?)",
                  (PROJECT, PROJECT_SLUG, "Attachment Hub", NOW, REPO_PATH))
        c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                  (BARE_PROJECT, "att-bare", "Attachment Bare", NOW))
        c.execute("INSERT OR REPLACE INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  (ACCOUNT, "Attachment Co", NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, project_id, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (DEAL, ACCOUNT, "Hub deal", "won", 1000.0, PROJECT, NOW, NOW))
        c.execute("INSERT INTO fireflies_meetings (id, deal_id, transcript_id, title, "
                  "meeting_date, duration_seconds, signals, fetched_at, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (MEETING, DEAL, TRANSCRIPT, "Kickoff Attachment Co",
                   "2026-07-20", 1800, "{}", NOW, NOW))
        for tid, status, archived in ((TASK_OPEN_A, "backlog", None),
                                      (TASK_OPEN_B, "in_progress", None),
                                      (TASK_DONE, "done", None),
                                      (TASK_ARCHIVED, "backlog", NOW)):
            c.execute("INSERT INTO tasks (id, title, status, created_at, project_id, "
                      "archived_at) VALUES (?,?,?,?,?,?)",
                      (tid, f"task {tid}", status, NOW, PROJECT, archived))
        c.commit()
        c.close()

    # --- API shorthands --------------------------------------------------
    def _post(self, **body):
        return _CLIENT.post("/api/attachments", json=body)

    def _plan(self, **overrides):
        body = {"node_kind": "project", "node_id": PROJECT, "kind": "plan",
                "title": "Plan profundo — hub", "path": PLAN_PATH,
                "source_agent": "claude"}
        body.update(overrides)
        return self._post(**body)

    def _list(self, node_kind="project", node_id=PROJECT):
        return _CLIENT.get("/api/attachments",
                           params={"node_kind": node_kind, "node_id": node_id})

    def _hub(self, ref=PROJECT):
        return _CLIENT.get(f"/api/projects/{ref}/hub")

    def _rows(self):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM attachments ORDER BY created_at, id")]
        finally:
            c.close()


class Migration(_AttachmentsCase):
    """m10 exists, is ledgered, and installed the floor the rest stands on."""

    def test_the_migration_is_registered_and_ledgered(self):
        self.assertIn("m10_attachments", [n for n, _ in runner.MIGRATIONS])
        c = self._conn()
        try:
            names = {r[0] for r in c.execute("SELECT name FROM orch_migrations")}
        finally:
            c.close()
        self.assertIn("m10_attachments", names)

    def test_it_lands_after_m05(self):
        """Ordering hygiene: the versioned list is applied in order, and a
        reader of the ledger should see the phases in the order they shipped."""
        names = [n for n, _ in runner.MIGRATIONS]
        self.assertLess(names.index("m05_retire_delivered_stage"),
                        names.index("m10_attachments"))

    def test_the_table_and_all_three_indexes_exist(self):
        """Scoped by `tbl_name` on purpose: this DB already carries
        `idx_attachments_task` on hermes' unrelated `task_attachments`, so
        matching on the name prefix alone answers a different question."""
        c = self._conn()
        try:
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
            ).fetchone())
            found = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='attachments' AND name NOT LIKE 'sqlite_autoindex%'")}
        finally:
            c.close()
        # Equality, not containment: an index this contract does not name is one
        # nobody declared. `sqlite_autoindex_attachments_1` is excluded because
        # SQLite mints it for the TEXT PRIMARY KEY — it is not ours to declare.
        self.assertEqual(found, set(_m10.INDEXES))

    def test_it_refuses_a_table_whose_index_name_was_taken_elsewhere(self):
        """Index names are GLOBAL in SQLite, and `CREATE INDEX IF NOT EXISTS`
        against a name already used on ANOTHER table is silently skipped (not an
        error). Without this check m10 would commit, ledger itself as applied,
        and leave the anti-duplication UNIQUE indexes simply absent — every
        repeated registration from the four host skills landing as a duplicate,
        with nothing to say so. The namespace is genuinely shared: hermes owns
        `idx_attachments_task` on `task_attachments`."""
        c = self._conn()
        try:
            c.execute("DROP TABLE attachments")
            c.execute("CREATE INDEX idx_attachments_url ON projects(slug)")
            with self.assertRaises(RuntimeError) as caught:
                _m10.m10_attachments(c)
            self.assertIn("idx_attachments_url", str(caught.exception))
            c.rollback()
        finally:
            c.close()

    def test_the_module_vocabularies_match_the_schema_checks(self):
        """`attachments.py` mirrors the CHECKs so a bad value is a 400 instead of
        a 500. A mirror that drifts from what it mirrors is worse than none."""
        c = self._conn()
        try:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name='attachments'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(tuple(_att.NODE_KINDS), _m10.NODE_KINDS)
        self.assertEqual(tuple(_att.KINDS), _m10.KINDS)
        for value in tuple(_att.NODE_KINDS) + tuple(_att.KINDS):
            self.assertIn(f"'{value}'", sql,
                          f"'{value}' is not in the schema CHECK")


class Validation(_AttachmentsCase):
    """The refusal matrix — every one a typed 400, and every one writing nothing."""

    def test_a_node_kind_outside_the_enum_is_a_typed_400(self):
        res = self._post(node_kind="epic", node_id=PROJECT, kind="plan",
                         title="t", path="x.md")
        self.assertEqual(res.status_code, 400, res.text)
        detail = res.json()["detail"]
        self.assertIn("epic", detail)
        for kind in _att.NODE_KINDS:
            self.assertIn(kind, detail)
        self.assertEqual(self._rows(), [])

    def test_a_kind_outside_the_enum_is_a_typed_400(self):
        res = self._post(node_kind="project", node_id=PROJECT, kind="video",
                         title="t", url="https://example.com/v")
        self.assertEqual(res.status_code, 400, res.text)
        detail = res.json()["detail"]
        self.assertIn("video", detail)
        for kind in _att.KINDS:
            self.assertIn(kind, detail)
        self.assertEqual(self._rows(), [])

    def test_neither_url_nor_path_is_refused(self):
        res = self._post(node_kind="project", node_id=PROJECT, kind="plan",
                         title="a pointer at nothing")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self._rows(), [])

    def test_blank_url_and_path_are_refused_like_absent_ones(self):
        """`""` must not satisfy the CHECK — an empty string is not a pointer."""
        res = self._post(node_kind="project", node_id=PROJECT, kind="plan",
                         title="blank", url="", path="   ")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self._rows(), [])

    def test_an_empty_title_is_refused(self):
        res = self._post(node_kind="project", node_id=PROJECT, kind="plan",
                         title="   ", path=PLAN_PATH)
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self._rows(), [])

    def test_a_node_that_does_not_exist_is_refused(self):
        """The one refusal SQLite cannot make: `node_id` has no FK, so without
        this check the row is accepted and the facet is empty forever."""
        res = self._post(node_kind="project", node_id="proj_ghost", kind="plan",
                         title="ghost plan", path=PLAN_PATH)
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("proj_ghost", res.json()["detail"])
        self.assertEqual(self._rows(), [])

    def test_the_node_is_checked_in_the_table_its_kind_names(self):
        """A real project id is NOT a real deal id. Validating against the wrong
        table (or against `id` in any table) would let a cross-kind pointer in."""
        res = self._post(node_kind="deal", node_id=PROJECT, kind="plan",
                         title="wrong table", path=PLAN_PATH)
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self._rows(), [])

    def test_every_node_kind_is_actually_writable(self):
        """The mirror must not over-refuse either: each of the four kinds
        resolves against a real table and accepts a real id."""
        for node_kind, node_id in (("project", PROJECT), ("deal", DEAL),
                                   ("account", ACCOUNT), ("task", TASK_OPEN_A)):
            with self.subTest(node_kind=node_kind):
                res = self._post(node_kind=node_kind, node_id=node_id,
                                 kind="resource", title=f"{node_kind} doc",
                                 url=f"https://drive.example/{node_kind}")
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(res.json()["attachment"]["node_kind"], node_kind)

    def test_listing_with_a_bad_node_kind_is_a_typed_400(self):
        self.assertEqual(self._list(node_kind="epic").status_code, 400)

    def test_removing_something_that_does_not_exist_is_404(self):
        res = _CLIENT.delete("/api/attachments/att_ghost")
        self.assertEqual(res.status_code, 404, res.text)


class Upsert(_AttachmentsCase):
    """The anti-duplication floor, at both layers."""

    def test_the_same_path_twice_updates_instead_of_duplicating(self):
        first = self._plan()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["created"])
        original = first.json()["attachment"]

        second = self._plan(title="Plan profundo — hub (v2)", source_agent="codex")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["created"])
        updated = second.json()["attachment"]

        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["created_at"], original["created_at"])
        self.assertEqual(updated["title"], "Plan profundo — hub (v2)")
        self.assertEqual(updated["source_agent"], "codex")
        self.assertEqual(len(self._rows()), 1)

    def test_the_same_url_twice_updates_instead_of_duplicating(self):
        body = {"node_kind": "project", "node_id": PROJECT, "kind": "resource",
                "url": "https://drive.example/brief", "title": "Brief"}
        self.assertTrue(self._post(**body).json()["created"])
        again = self._post(**dict(body, title="Brief v2"))
        self.assertFalse(again.json()["created"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Brief v2")

    def test_a_different_target_is_a_different_attachment(self):
        """The upsert must not collapse two genuinely different pointers."""
        self._plan()
        self._plan(path="att-hub/2026-08-02_otro.md", title="Otro plan")
        self.assertEqual(len(self._rows()), 2)

    def test_the_same_target_on_another_node_is_a_different_attachment(self):
        """The key includes the node: the same plan file may legitimately hang
        off two projects."""
        self._plan()
        self._plan(node_id=BARE_PROJECT)
        self.assertEqual(len(self._rows()), 2)

    def test_the_same_target_under_another_kind_is_a_different_attachment(self):
        self._plan()
        self._plan(kind="resource", title="same file, as a resource")
        self.assertEqual(len(self._rows()), 2)

    def test_the_engine_refuses_a_duplicate_even_without_the_application(self):
        """Spec regla 7 — a rule only the application enforces survives exactly
        until the next writer (a repair script, the hermes CLI, a psql-style
        poke). The partial UNIQUE index is that rule in the engine."""
        self._plan()
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO attachments (id, node_kind, node_id, kind, "
                          "path, title, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          ("att_raw_dup", "project", PROJECT, "plan", PLAN_PATH,
                           "raw duplicate", NOW, NOW))
            c.rollback()
        finally:
            c.close()
        self.assertEqual(len(self._rows()), 1)

    def test_the_unique_indexes_are_partial_not_plain(self):
        """The half a non-partial `UNIQUE(node_kind,node_id,kind,url)` fails:
        SQLite treats NULLs as distinct, so a plain index would *pass* the
        duplicate-url test above and then reject the second path-only row here
        for colliding on NULL... or, worse, silently permit ten path-only rows
        while claiming uniqueness. Both path-only rows must be accepted, and a
        duplicate among them must still be refused."""
        self._plan()
        self._plan(path="att-hub/2026-08-03_tercero.md", title="Tercero")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["url"] is None for r in rows))

        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO attachments (id, node_kind, node_id, kind, "
                          "path, title, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          ("att_raw_dup2", "project", PROJECT, "plan",
                           "att-hub/2026-08-03_tercero.md", "dup", NOW, NOW))
            c.rollback()
        finally:
            c.close()

    def test_the_engine_refuses_a_pointer_at_nothing(self):
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO attachments (id, node_kind, node_id, kind, "
                          "title, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                          ("att_raw_null", "project", PROJECT, "plan",
                           "nothing", NOW, NOW))
            c.rollback()
        finally:
            c.close()


class ListAndRemove(_AttachmentsCase):

    def test_the_list_is_flat_and_grouped(self):
        self._plan()
        self._post(node_kind="project", node_id=PROJECT, kind="resource",
                   title="Brief", url="https://drive.example/brief")
        body = self._list().json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(set(body["by_kind"]), set(_att.KINDS),
                         "an empty facet must be an empty list, not a missing key")
        self.assertEqual([a["path"] for a in body["by_kind"]["plan"]], [PLAN_PATH])
        self.assertEqual(body["by_kind"]["code"], [])

    def test_the_list_is_scoped_to_the_node(self):
        self._plan()
        self._post(node_kind="deal", node_id=DEAL, kind="resource",
                   title="Deal doc", url="https://drive.example/deal")
        self.assertEqual(self._list().json()["count"], 1)
        self.assertEqual(self._list(node_kind="deal", node_id=DEAL).json()["count"], 1)
        self.assertEqual(self._list(node_id=BARE_PROJECT).json()["count"], 0)

    def test_remove_deletes_exactly_one_pointer(self):
        aid = self._plan().json()["attachment"]["id"]
        self._post(node_kind="project", node_id=PROJECT, kind="resource",
                   title="Brief", url="https://drive.example/brief")
        res = _CLIENT.delete(f"/api/attachments/{aid}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["removed"], aid)
        remaining = self._rows()
        self.assertEqual([r["kind"] for r in remaining], ["resource"])

    def test_removing_twice_is_404_not_a_silent_ok(self):
        aid = self._plan().json()["attachment"]["id"]
        self.assertEqual(_CLIENT.delete(f"/api/attachments/{aid}").status_code, 200)
        self.assertEqual(_CLIENT.delete(f"/api/attachments/{aid}").status_code, 404)


class ProjectHub(_AttachmentsCase):
    """The five facets, over the seeded spine."""

    def test_an_unknown_project_is_404(self):
        self.assertEqual(self._hub("proj_ghost").status_code, 404)

    def test_the_hub_resolves_an_id_or_a_slug(self):
        for ref in (PROJECT, PROJECT_SLUG):
            with self.subTest(ref=ref):
                res = self._hub(ref)
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(res.json()["project"]["id"], PROJECT)

    def test_all_six_facets_are_present_even_when_empty(self):
        facets = self._hub(BARE_PROJECT).json()["facets"]
        self.assertEqual(set(facets),
                         {"conversations", "resources", "code", "plans", "proposals", "tasks"})
        for name in ("conversations", "resources", "code", "plans"):
            self.assertEqual(facets[name], {"items": [], "count": 0})
        self.assertEqual(facets["tasks"], {"open": 0, "total": 0})

    def test_a_registered_plan_lands_in_the_plans_facet(self):
        self._plan()
        plans = self._hub().json()["facets"]["plans"]
        self.assertEqual(plans["count"], 1)
        item = plans["items"][0]
        self.assertEqual((item["path"], item["source"], item["source_agent"]),
                         (PLAN_PATH, "attachment", "claude"))

    def test_conversations_derive_from_the_deals_of_the_project(self):
        """The join the facet exists for: `fireflies_meetings.deal_id` →
        `deals.project_id`. Nobody registered this meeting; it belongs on the
        hub anyway."""
        conv = self._hub().json()["facets"]["conversations"]
        self.assertEqual(conv["count"], 1)
        item = conv["items"][0]
        self.assertEqual(item["source"], "fireflies")
        self.assertEqual(item["transcript_id"], TRANSCRIPT)
        self.assertEqual(item["deal_id"], DEAL)
        self.assertEqual(item["deal_title"], "Hub deal")

    def test_a_meeting_of_another_projects_deal_stays_out(self):
        c = self._conn()
        c.execute("INSERT INTO deals (id, account_id, title, stage, project_id, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                  ("deal_att_other", ACCOUNT, "Other", "won", BARE_PROJECT, NOW, NOW))
        c.execute("INSERT INTO fireflies_meetings (id, deal_id, transcript_id, title, "
                  "meeting_date, fetched_at, created_at) VALUES (?,?,?,?,?,?,?)",
                  ("ff_att_other", "deal_att_other", "TRX_OTHER", "Otro",
                   "2026-07-21", NOW, NOW))
        c.commit()
        c.close()
        ids = [i["transcript_id"]
               for i in self._hub().json()["facets"]["conversations"]["items"]]
        self.assertEqual(ids, [TRANSCRIPT])

    def test_a_registered_conversation_folds_the_derived_meeting(self):
        """Registration wins over derivation: the skills are being told to
        register meetings, so the same conversation must not render twice."""
        res = self._post(node_kind="project", node_id=PROJECT, kind="conversation",
                         title="Kickoff (registrado)",
                         url=f"https://app.fireflies.ai/view/{TRANSCRIPT}")
        self.assertEqual(res.status_code, 200, res.text)
        conv = self._hub().json()["facets"]["conversations"]
        self.assertEqual(conv["count"], 1)
        self.assertEqual(conv["items"][0]["source"], "attachment")

    def test_an_unrelated_conversation_attachment_adds_to_the_facet(self):
        """The fold is keyed on the transcript id, not on 'any attachment
        suppresses everything' — a WhatsApp thread and a Fireflies meeting are
        two conversations."""
        self._post(node_kind="project", node_id=PROJECT, kind="conversation",
                   title="Hilo de WhatsApp", url="https://wa.example/thread/9")
        conv = self._hub().json()["facets"]["conversations"]
        self.assertEqual(conv["count"], 2)
        self.assertEqual({i["source"] for i in conv["items"]},
                         {"attachment", "fireflies"})

    def test_code_derives_from_repo_path(self):
        code = self._hub().json()["facets"]["code"]
        self.assertEqual(code["count"], 1)
        self.assertEqual(code["items"][0]["source"], "project")
        self.assertEqual(code["items"][0]["path"], REPO_PATH)

    def test_a_registered_repo_folds_the_derived_one(self):
        self._post(node_kind="project", node_id=PROJECT, kind="code",
                   title="att-hub (GitHub)", path=REPO_PATH,
                   url="https://github.com/operator/att-hub")
        code = self._hub().json()["facets"]["code"]
        self.assertEqual(code["count"], 1)
        self.assertEqual(code["items"][0]["source"], "attachment")

    def test_tasks_are_counted_where_they_live(self):
        """Open excludes done AND archived; total is every task of the project.
        Both come from `tasks`, never from attachment rows."""
        tasks = self._hub().json()["facets"]["tasks"]
        self.assertEqual(tasks, {"open": 2, "total": 4})

    def test_an_attachment_can_never_change_the_task_count(self):
        """The rule the generic table must NOT break: work has its own writers,
        and a pointer here would be a second answer to 'what is open'."""
        before = self._hub().json()["facets"]["tasks"]
        self._plan()
        self._post(node_kind="task", node_id=TASK_OPEN_A, kind="resource",
                   title="Task doc", url="https://drive.example/task")
        after = self._hub().json()
        self.assertEqual(after["facets"]["tasks"], before)
        # …and a task-scoped attachment is not a project-scoped one.
        self.assertEqual(after["facets"]["resources"]["count"], 0)

    def test_the_hub_is_read_only(self):
        """A GET must not create rows — the derived facets are computed, never
        materialized (materializing them would make the union a duplication)."""
        self._hub()
        self._hub()
        self.assertEqual(self._rows(), [])


class ModuleLayer(_AttachmentsCase):
    """The non-HTTP callers (MCP, a skill, a cron) get dicts with typed codes,
    not exceptions — the same convention as threads/sprints/crm."""

    def test_errors_are_typed_dicts(self):
        for kwargs, code in (
                (dict(node_kind="epic", node_id=PROJECT, kind="plan",
                      title="t", path="p"), "bad_node_kind"),
                (dict(node_kind="project", node_id=PROJECT, kind="video",
                      title="t", path="p"), "bad_kind"),
                (dict(node_kind="project", node_id=PROJECT, kind="plan",
                      title="t"), "missing_target"),
                (dict(node_kind="project", node_id=PROJECT, kind="plan",
                      title="", path="p"), "bad_title"),
                (dict(node_kind="project", node_id="ghost", kind="plan",
                      title="t", path="p"), "unknown_node")):
            with self.subTest(code=code):
                res = _att.add_attachment(**kwargs)
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], code)

    def test_remove_and_hub_report_not_found(self):
        self.assertEqual(_att.remove("att_ghost")["code"], "not_found")
        self.assertEqual(_att.list_project_hub("proj_ghost")["code"], "not_found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
