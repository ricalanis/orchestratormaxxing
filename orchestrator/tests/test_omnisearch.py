"""Contract for the omnisearch extension — spec §4 ("Search is the real
navigation": typing a name is recall-free and beats remembering which tab owns
a thing; the nav bar is the fallback, not the path).

`GET /api/search` covered tasks/deals/sessions/memory. The two nouns an operator
actually types — the delivery (**project**) and the client (**account** /
**contact**) — were unreachable by name. This pins the extension and, more
importantly, the shape it must keep:

  * **Every hit carries the same six keys** (`type,id,title,subtitle,tab,entity`).
    The dropdown renderer is type-GENERIC — it reads title/subtitle, falls back
    to a bullet for an unknown icon, and on Enter does `switchTab(r.tab)` then
    `openEntity(...)` if `r.entity` is set. So a new type needs no frontend
    change, and a hit missing a key silently renders blank. Asserted per hit.
  * **`entity` must name a type the drawer can actually open.** `openEntity`
    ignores anything outside its nav set, so a `contact:` entity would render a
    row that swallows the click. Contacts therefore point at their ACCOUNT —
    the nearest addressable context — and that is asserted, not assumed.
  * **`tab` must be a real tab.** A hit routing to a nonexistent tab is a click
    into nothing.
  * **The pre-existing types must survive the edit** — the new blocks are
    inserted into one function, and a mis-scoped early `break` or return would
    truncate the older ones. A task hit is asserted alongside the new ones.
  * **Per-type caps are per TYPE**, not global: five matching projects must not
    be able to starve the accounts block.
  * **One broken listing degrades that block only.** Each block is
    individually try/except'd; a raising `crm.list_accounts` must still return
    the project and task hits, never a 500 on the omnibar.

DB isolation: a COPY of ~/.hermes/kanban.db per test with `runner.run()` on top
plus a fixture whose names share one distinctive token. The real DB is never
opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_omnisearch.py   # from orchestrator/
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
_CLIENT = None
_IMPORT_DB = None
try:
    from dashboard import db as _db, sprints as _sprints, crm as _crm
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_omni_import_", suffix=".db")
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
TOKEN = "zephyrix"                       # the one substring every fixture shares
ACCOUNT = "acct_omni_zephyrix"
CONTACT = "cont_omni_zephyrix"
PROJECT = "proj_omni_zephyrix"
TASK = "t_omni_zephyrix"
RESULT_KEYS = {"type", "id", "title", "subtitle", "tab", "entity"}

# The tabs the shell actually knows (index.html TAB_WORKSPACE). A hit routing
# anywhere else is a click into nothing.
KNOWN_TABS = {"today", "board", "projects", "cycle", "my-tasks", "agent-tasks",
              "archive", "sessions", "roadmap", "crm", "growth", "memory",
              "graph", "lakehouse", "reflection", "daily", "plate", "supps",
              "usage", "health", "coordinators"}

# The types `openEntity` will actually open (index.html ED_NAV_TYPES, which
# mirrors dashboard/context.py ENTITY_TYPES).
DRAWER_TYPES = {"task", "project", "initiative", "deal", "session", "account"}


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _SearchCase(unittest.TestCase):

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_omni_test_", suffix=".db")
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

    def _seed(self):
        c = sqlite3.connect(str(self.tmp))
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO accounts (id, name, domain, created_at) VALUES (?,?,?,?)",
                  (ACCOUNT, "Zephyrix Labs", "zephyrix.example", NOW))
        c.execute("INSERT INTO contacts (id, account_id, name, email, role, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (CONTACT, ACCOUNT, "Zephyrix Contact", "hola@zephyrix.example", "CTO", NOW))
        c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                  (PROJECT, "zephyrix-delivery", "Zephyrix Delivery", NOW))
        c.execute("INSERT INTO tasks (id, title, status, created_at, project_id) "
                  "VALUES (?,?,?,?,?)", (TASK, "Zephyrix onboarding", "todo", NOW, PROJECT))
        c.commit()
        c.close()

    def _search(self, q=TOKEN, **params):
        res = _CLIENT.get("/api/search", params={"q": q, **params})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["results"]

    def _of_type(self, kind, results=None):
        return [r for r in (results if results is not None else self._search())
                if r["type"] == kind]


class NewTypesAreSearchable(_SearchCase):

    def test_projects_are_findable_by_name(self):
        hits = self._of_type("project")
        self.assertEqual([h["id"] for h in hits], [PROJECT])
        self.assertEqual(hits[0]["title"], "Zephyrix Delivery")
        self.assertEqual(hits[0]["entity"], f"project:{PROJECT}")
        self.assertEqual(hits[0]["tab"], "projects")

    def test_projects_are_findable_by_slug(self):
        """Half the point of the slug is that it is what gets typed."""
        hits = self._of_type("project", self._search("zephyrix-del"))
        self.assertEqual([h["id"] for h in hits], [PROJECT])

    def test_accounts_are_findable(self):
        hits = self._of_type("account")
        self.assertEqual([h["id"] for h in hits], [ACCOUNT])
        self.assertEqual(hits[0]["title"], "Zephyrix Labs")
        self.assertEqual(hits[0]["entity"], f"account:{ACCOUNT}")

    def test_contacts_are_findable_by_name_and_by_email(self):
        by_name = self._of_type("contact", self._search("Zephyrix Contact"))
        self.assertEqual([h["id"] for h in by_name], [CONTACT])
        by_mail = self._of_type("contact", self._search("hola@zephyrix"))
        self.assertEqual([h["id"] for h in by_mail], [CONTACT])

    def test_a_contact_hit_opens_its_account(self):
        """There is no `contact` entity type in the drawer, and `openEntity`
        ignores what it cannot open — so a `contact:` id would render a row that
        swallows the click. The account is the nearest addressable context."""
        hit = self._of_type("contact")[0]
        self.assertEqual(hit["entity"], f"account:{ACCOUNT}")

    def test_the_contact_subtitle_names_its_account(self):
        self.assertIn("Zephyrix Labs", self._of_type("contact")[0]["subtitle"])


class ShapeIsPreserved(_SearchCase):

    def test_every_hit_carries_the_six_keys_the_renderer_reads(self):
        results = self._search()
        self.assertTrue(results)
        for r in results:
            with self.subTest(hit=r.get("id")):
                self.assertEqual(set(r), RESULT_KEYS)
                self.assertIsInstance(r["title"], str)
                self.assertIsInstance(r["subtitle"], str)

    def test_every_hit_routes_to_a_tab_that_exists(self):
        for r in self._search():
            with self.subTest(hit=r.get("id")):
                self.assertIn(r["tab"], KNOWN_TABS)

    def test_every_entity_names_a_type_the_drawer_can_open(self):
        for r in self._search():
            if not r["entity"]:
                continue
            with self.subTest(hit=r.get("id")):
                self.assertIn(r["entity"].split(":")[0], DRAWER_TYPES)

    def test_the_pre_existing_types_still_work(self):
        """The new blocks are inserted into one function; a mis-scoped break
        would truncate the older ones."""
        self.assertEqual([h["id"] for h in self._of_type("task")], [TASK])

    def test_an_empty_query_returns_nothing(self):
        res = _CLIENT.get("/api/search", params={"q": "   "})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["results"], [])


class BlocksAreIndependent(_SearchCase):

    def test_the_cap_is_per_type_not_global(self):
        """Five matching projects must not be able to starve the account block."""
        c = sqlite3.connect(str(self.tmp))
        for i in range(6):
            c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                      (f"{PROJECT}_{i}", f"zephyrix-extra-{i}", f"Zephyrix Extra {i}", NOW))
        c.commit()
        c.close()
        results = self._search(limit=2)
        self.assertEqual(len(self._of_type("project", results)), 2)
        self.assertEqual(len(self._of_type("account", results)), 1)
        self.assertEqual(len(self._of_type("task", results)), 1)

    def test_a_broken_listing_degrades_only_its_own_block(self):
        """The omnibar must never 500 — a raising listing costs its hits, not
        the search."""
        orig = _crm.list_accounts
        _crm.list_accounts = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            results = self._search()
        finally:
            _crm.list_accounts = orig
        self.assertEqual(self._of_type("account", results), [])
        self.assertEqual([h["id"] for h in self._of_type("project", results)], [PROJECT])
        self.assertEqual([h["id"] for h in self._of_type("task", results)], [TASK])


if __name__ == "__main__":
    unittest.main()
