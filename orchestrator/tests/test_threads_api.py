"""Contract for the thread registry API — spec §2 ("Thread registry").

`GET /api/threads` + `PATCH /api/threads/{id}` are the Agents threads panel. The
registry is the routing table `dispatch._resolve_thread` reads, so an edit made
here decides where a dispatched task is ANNOUNCED. That is why validation is
tested harder than the happy path:

  * **A bad `role` must be a typed 400, not an IntegrityError.** The column
    carries a CHECK (`code|growth|ops|health|personal`); a route that let the
    value reach SQLite would answer 500 and teach nothing. Spec §2: free text
    becomes 22 roles inside a month.
  * **A `project_id` that does not exist must be REFUSED.** Accepting it looks
    like success and then routes every dispatch for that project to the "Hoy"
    fallback forever — a silent lie, the exact class this phase exists to kill.
  * **`{"project_id": null}` clears the binding; an ABSENT key leaves it.**
    PATCH semantics, and the reason the module takes the raw body dict instead
    of keyword arguments defaulting to None. Both directions are asserted,
    because a `.get()`-based implementation passes the first and fails the
    second — while looking correct.
  * **A missing thread is 404, and an unparseable id is a missing thread** (not
    a 500 from `int()`).
  * **Active-first ordering** — an archived topic is history, and history never
    outranks a live thread in a list a human scans top-down.

There is deliberately no create/delete verb: the registry is hand-seeded by
m02_spine and a thread is never auto-created for a project, so the list cannot
grow with the backlog. `test_no_write_verbs_beyond_patch` pins that absence.

DB isolation: a COPY of ~/.hermes/kanban.db per test with `runner.run()` on top
(so the real `threads` table shape is exercised, not a hand-rolled one), plus a
self-contained fixture. `runner.run_backup` is stubbed. The real DB is never
opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_threads_api.py   # from orchestrator/
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
# legitimate skip, but a missing registry module is the very thing under test —
# swallowing that ImportError would turn a deleted feature into a green run.
from dashboard import threads as _threads

_READY = False
_CLIENT = None
_IMPORT_DB = None
try:
    from dashboard import db as _db, sprints as _sprints
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_threads_import_", suffix=".db")
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
PROJECT_A = "proj_thr_a"
PROJECT_B = "proj_thr_b"
T_CODE = 970001        # active, bound to PROJECT_A, most recent activity
T_OPS = 970002         # active, unbound, older activity
T_OLD = 970003         # archived
CHAT = "1234567890"


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _ThreadsCase(unittest.TestCase):

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_threads_test_", suffix=".db")
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

    def _conn(self):
        c = sqlite3.connect(str(self.tmp))
        c.row_factory = sqlite3.Row
        return c

    def _seed(self):
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        for pid, slug, name in ((PROJECT_A, "thr-a", "Thread Project A"),
                                (PROJECT_B, "thr-b", "Thread Project B")):
            c.execute("INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
                      (pid, slug, name, NOW))
        for tid, name, project, role, status, last in (
                (T_CODE, "🧑‍💻 Code", PROJECT_A, "code", "active", NOW),
                (T_OPS, "⚙️ Ops", None, "ops", "active", NOW - 5000),
                (T_OLD, "topic 970003", None, "personal", "archived", NOW + 9999)):
            c.execute("INSERT INTO threads (thread_id, chat_id, name, project_id, role, "
                      "status, last_activity_at) VALUES (?,?,?,?,?,?,?)",
                      (tid, CHAT, name, project, role, status, last))
        c.commit()
        c.close()

    def _threads(self):
        res = _CLIENT.get("/api/threads")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _row(self, thread_id):
        return next(t for t in self._threads()["threads"] if t["thread_id"] == thread_id)


class ListThreads(_ThreadsCase):

    def test_the_registry_is_readable(self):
        body = self._threads()
        ids = [t["thread_id"] for t in body["threads"]]
        for tid in (T_CODE, T_OPS, T_OLD):
            self.assertIn(tid, ids)
        self.assertEqual(body["roles"], list(_threads.ROLES))

    def test_archived_is_a_flag_not_an_omission(self):
        """The panel shows history; the flag is what keeps it out of pickers."""
        self.assertTrue(self._row(T_OLD)["archived"])
        self.assertFalse(self._row(T_CODE)["archived"])
        body = self._threads()
        self.assertEqual(body["archived"], sum(1 for t in body["threads"] if t["archived"]))
        self.assertEqual(body["active"], sum(1 for t in body["threads"] if not t["archived"]))

    def test_active_sorts_before_archived_even_when_archived_is_newer(self):
        """T_OLD carries the NEWEST last_activity_at on purpose: recency must not
        be able to lift a dead topic above a live one."""
        rows = self._threads()["threads"]
        positions = {t["thread_id"]: i for i, t in enumerate(rows)}
        self.assertLess(positions[T_CODE], positions[T_OLD])
        self.assertLess(positions[T_OPS], positions[T_OLD])
        # Within the active group, most-recently-active first.
        self.assertLess(positions[T_CODE], positions[T_OPS])

    def test_the_bound_project_is_resolved_for_the_table(self):
        row = self._row(T_CODE)
        self.assertEqual(row["project_id"], PROJECT_A)
        self.assertEqual(row["project_name"], "Thread Project A")
        self.assertIsNone(self._row(T_OPS)["project_id"])
        self.assertIsNone(self._row(T_OPS)["project_name"])


class PatchThread(_ThreadsCase):

    def _patch(self, thread_id, body):
        return _CLIENT.patch(f"/api/threads/{thread_id}", json=body)

    def test_rename(self):
        res = self._patch(T_OPS, {"name": "⚙️ Operaciones"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["thread"]["name"], "⚙️ Operaciones")
        self.assertEqual(res.json()["updated"], ["name"])
        self.assertEqual(self._row(T_OPS)["name"], "⚙️ Operaciones")

    def test_an_empty_name_is_refused(self):
        self.assertEqual(self._patch(T_OPS, {"name": "  "}).status_code, 400)
        self.assertEqual(self._row(T_OPS)["name"], "⚙️ Ops")

    def test_role_must_be_one_of_the_enum(self):
        # Named for the guarantee, not the count: m25 made it six by adding
        # 'design'. A test whose name pins the cardinality turns "we grew the
        # vocabulary" into "a test broke".
        for role in _threads.ROLES:
            with self.subTest(role=role):
                res = self._patch(T_OPS, {"role": role})
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(res.json()["thread"]["role"], role)

    def test_a_role_outside_the_enum_is_a_typed_400_not_a_500(self):
        """The CHECK would raise IntegrityError → 500. Validating first turns it
        into an error a human can act on, and names the legal values."""
        res = self._patch(T_OPS, {"role": "marketing"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("marketing", res.json()["detail"])
        for role in _threads.ROLES:
            self.assertIn(role, res.json()["detail"])
        self.assertEqual(self._row(T_OPS)["role"], "ops")

    def test_binding_to_a_project(self):
        res = self._patch(T_OPS, {"project_id": PROJECT_B})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["thread"]["project_id"], PROJECT_B)
        self.assertEqual(res.json()["thread"]["project_name"], "Thread Project B")

    def test_binding_to_a_project_that_does_not_exist_is_refused(self):
        """A phantom binding would silently route every dispatch for that
        project to the Hoy fallback forever."""
        res = self._patch(T_OPS, {"project_id": "proj_does_not_exist"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIsNone(self._row(T_OPS)["project_id"])

    def test_explicit_null_clears_the_binding(self):
        res = self._patch(T_CODE, {"project_id": None})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()["thread"]["project_id"])
        self.assertIsNone(self._row(T_CODE)["project_id"])

    def test_an_absent_key_leaves_the_binding_alone(self):
        """The half a `.get()`-based implementation silently fails: renaming a
        bound thread must not unbind it."""
        res = self._patch(T_CODE, {"name": "🧑‍💻 Código"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self._row(T_CODE)["project_id"], PROJECT_A)

    def test_status_toggles_archive(self):
        self.assertEqual(self._patch(T_CODE, {"status": "archived"}).status_code, 200)
        self.assertTrue(self._row(T_CODE)["archived"])
        self.assertEqual(self._patch(T_CODE, {"status": "active"}).status_code, 200)
        self.assertFalse(self._row(T_CODE)["archived"])

    def test_an_unknown_status_is_refused(self):
        res = self._patch(T_CODE, {"status": "paused"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self._row(T_CODE)["status"], "active")

    def test_several_fields_in_one_patch(self):
        res = self._patch(T_OPS, {"name": "🌱 Growth", "role": "growth",
                                  "project_id": PROJECT_B, "status": "archived"})
        self.assertEqual(res.status_code, 200, res.text)
        row = self._row(T_OPS)
        self.assertEqual((row["name"], row["role"], row["project_id"], row["archived"]),
                         ("🌱 Growth", "growth", PROJECT_B, True))

    def test_a_rejected_field_writes_nothing_at_all(self):
        """One invalid key must not leave the valid ones half-applied."""
        res = self._patch(T_OPS, {"name": "renamed", "role": "nope"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self._row(T_OPS)["name"], "⚙️ Ops")

    def test_an_unknown_thread_is_404(self):
        self.assertEqual(self._patch(999999, {"name": "ghost"}).status_code, 404)

    def test_an_unparseable_thread_id_is_404_not_500(self):
        self.assertEqual(self._patch("not-an-id", {"name": "ghost"}).status_code, 404)

    def test_an_empty_patch_is_refused(self):
        self.assertEqual(self._patch(T_OPS, {}).status_code, 400)

    def test_an_unknown_field_is_refused(self):
        """Silently ignoring `chat_id` would look like it was applied."""
        res = self._patch(T_OPS, {"chat_id": "1"})
        self.assertEqual(res.status_code, 400, res.text)


class RegistryShape(_ThreadsCase):

    def test_no_write_verbs_beyond_patch(self):
        """Spec §2: a thread is NEVER auto-created for a project, so the list
        cannot grow with the backlog. The absence of create/delete is the
        mechanism — assert it, so a future 'completeness' pass has to argue."""
        routes = {(getattr(r, "path", None), m)
                  for r in _CLIENT.app.routes for m in (getattr(r, "methods", None) or [])}
        thread_routes = {(p, m) for (p, m) in routes if p and p.startswith("/api/threads")}
        self.assertEqual({m for (_p, m) in thread_routes} - {"HEAD"}, {"GET", "PATCH"},
                         f"unexpected verbs on the thread registry: {sorted(thread_routes)}")

    def test_the_role_enum_matches_the_schema_check(self):
        """The mirrored tuple must not drift from the CHECK it mirrors."""
        conn = self._conn()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='threads'"
            ).fetchone()[0]
        finally:
            conn.close()
        for role in _threads.ROLES:
            self.assertIn(f"'{role}'", sql, f"role '{role}' is not in the schema CHECK")


if __name__ == "__main__":
    unittest.main()
