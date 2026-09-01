"""Contract for the epic kill — spec §1 ("Epic → **Killed.** 1 row, 0 tasks, no
page") and the `dispatch_to_agent` retirement (spec §2, the honesty rule).

A retired surface has three ways to be wrong, and this file pins all three:

  1. **It can be deleted instead of buried.** A removed route answers 404 and a
     removed MCP verb answers "Unknown tool" — both read as *"you got the URL
     wrong, try again"*. So the routes and the verbs still exist and answer with
     a TYPED body (410 Gone / `{error: epics_folded}`) that says the concept is
     gone. Asserted here as status code AND exact payload, because a 410 with a
     FastAPI-default `{"detail": ...}` body is not machine-readable.

  2. **The two frontends can drift.** The dashboard API and mcp_server are
     parallel frontends over one backend; killing a surface on one and leaving
     it alive on the other is how an agent keeps writing to a dead concept. The
     payload identity assertion (`api.EPICS_GONE == mcp_server.EPICS_GONE`) is
     the ratchet — it goes red the moment one side changes alone.

  3. **The kill can eat the audit trail.** `tasks.epic_id` is FROZEN, not
     dropped (spec §1: "Additive only. Never DROP COLUMN"): still read for
     display, never written. So this file also asserts the column and the READ
     path survive — a "cleanup" that deletes them must fail here.

Plus the assertion that makes the retirement real rather than cosmetic: every
underlying write function is replaced by a landmine for the duration of the
call, so a handler that still reaches one fails loudly instead of quietly
writing to a dead layer.

DB isolation: dashboard.api and mcp_server run migrations at import, so a COPY
of ~/.hermes/kanban.db is installed BEFORE either import. This file makes no
writes; the real DB is never opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_epics_410.py   # from orchestrator/
"""
import atexit
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

_READY = False
_CLIENT = None
_IMPORT_DB = None
_MCP = None
_API = None
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_epics410_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        import dashboard.api as _API
        from starlette.testclient import TestClient

        _prev_env = os.environ.get("HERMES_KANBAN_DB")
        os.environ["HERMES_KANBAN_DB"] = str(_IMPORT_DB)
        import mcp_server as _MCP
        if _prev_env is None:
            os.environ.pop("HERMES_KANBAN_DB", None)
        else:
            os.environ["HERMES_KANBAN_DB"] = _prev_env

        _CLIENT = TestClient(_API.app, raise_server_exceptions=False)
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


EXPECTED_EPICS = {
    "error": "epics_folded",
    "hint": "epics were folded into projects (m03); tasks.epic_id is frozen audit",
}
EXPECTED_RETIRED = {
    "error": "retired",
    "hint": "use the dashboard dispatch (human-only); agents report via claim/report verbs",
}


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class EpicApiIsGone(unittest.TestCase):
    """Every epic write/list route answers 410 with the typed body."""

    def _assert_gone(self, res):
        self.assertEqual(res.status_code, 410, res.text)
        self.assertEqual(res.json(), EXPECTED_EPICS)

    def test_list_is_gone(self):
        self._assert_gone(_CLIENT.get("/api/epics"))

    def test_list_scoped_to_a_project_is_gone(self):
        self._assert_gone(_CLIENT.get("/api/epics", params={"project_id": "proj_orchestrator"}))

    def test_create_is_gone(self):
        self._assert_gone(_CLIENT.post("/api/epics",
                                       json={"project_id": "proj_orchestrator", "title": "nope"}))

    def test_create_is_gone_even_when_the_body_is_malformed(self):
        """410 outranks 400: the concept is gone, so validating a payload for a
        dead resource would answer the wrong question."""
        self._assert_gone(_CLIENT.post("/api/epics", json={}))

    def test_update_is_gone(self):
        self._assert_gone(_CLIENT.patch("/api/epics/epic_whatever", json={"title": "nope"}))

    def test_task_epic_assignment_is_gone(self):
        self._assert_gone(_CLIENT.patch("/api/tasks/t_whatever/epic",
                                        json={"epic_id": "epic_whatever"}))

    def test_the_routes_still_exist(self):
        """The paths must remain REGISTERED — a deleted route would answer 404,
        which tells a caller to retry a different URL instead of stopping."""
        paths = {getattr(r, "path", None) for r in _API.app.routes}
        for path in ("/api/epics", "/api/epics/{epic_id}", "/api/tasks/{task_id}/epic"):
            self.assertIn(path, paths, f"{path} was deleted instead of retired")


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class EpicMcpIsGone(unittest.TestCase):
    """The MCP twins answer the SAME typed error — parity is the point."""

    VERBS = ("list_epics", "create_epic", "assign_task_epic", "update_epic")

    def setUp(self):
        # Landmine every underlying write/read the retired verbs used to call.
        # A handler that still reaches one now fails the test loudly instead of
        # quietly mutating a folded layer.
        self._orig = {}
        for mod, name in ((_MCP._graph, "list_epics"), (_MCP._graph, "create_epic"),
                          (_MCP._graph, "update_epic"), (_MCP._graph, "assign_task_epic"),
                          (_MCP._sprints, "set_task_assignee")):
            self._orig[(mod, name)] = getattr(mod, name, None)

            def _landmine(*a, _n=name, **k):
                raise AssertionError(f"a retired verb reached {_n}()")

            setattr(mod, name, _landmine)

    def tearDown(self):
        for (mod, name), fn in self._orig.items():
            if fn is not None:
                setattr(mod, name, fn)

    def test_every_epic_verb_returns_the_typed_error(self):
        for verb in self.VERBS:
            with self.subTest(verb=verb):
                out = json.loads(_MCP.TOOL_HANDLERS[verb]({}))
                self.assertEqual(out, EXPECTED_EPICS)

    def test_the_verbs_stay_registered_and_wired(self):
        registered = {t["name"] for t in _MCP.TOOLS}
        for verb in self.VERBS:
            self.assertIn(verb, registered, f"{verb} was deleted — callers get 'Unknown tool'")
            self.assertIn(verb, _MCP.TOOL_HANDLERS)

    def test_the_advertised_description_says_retired(self):
        """tools/list is the only thing most agents read. A tombstone verb still
        described as "create an epic" is a lie with a 410 hidden behind it."""
        by_name = {t["name"]: t for t in _MCP.TOOLS}
        for verb in self.VERBS:
            self.assertIn("RETIRED", by_name[verb]["description"].upper(),
                          f"{verb} still advertises itself as live")

    def test_dispatch_to_agent_is_retired(self):
        out = json.loads(_MCP.TOOL_HANDLERS["dispatch_to_agent"]({}))
        self.assertEqual(out, EXPECTED_RETIRED)

    def test_dispatch_to_agent_stays_registered_and_privileged(self):
        """Registered so a caller gets the explanation. Still PRIVILEGED so the
        gravestone does not appear in the DEFAULT toolset, where the verb was
        never visible in the first place (minimal tool frontier)."""
        self.assertIn("dispatch_to_agent", {t["name"] for t in _MCP.TOOLS})
        self.assertIn("dispatch_to_agent", _MCP.PRIVILEGED_TOOLS)
        self.assertIn("RETIRED", {t["name"]: t for t in _MCP.TOOLS}
                      ["dispatch_to_agent"]["description"].upper())


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class PayloadParity(unittest.TestCase):
    """The ratchet: API and MCP ship the same bytes, or this goes red."""

    def test_epics_payload_is_identical_on_both_frontends(self):
        self.assertEqual(_API.EPICS_GONE, _MCP.EPICS_GONE)
        self.assertEqual(_API.EPICS_GONE, EXPECTED_EPICS)

    def test_retired_dispatch_payload_is_pinned(self):
        self.assertEqual(_MCP.DISPATCH_RETIRED, EXPECTED_RETIRED)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class FrozenAuditSurvives(unittest.TestCase):
    """`tasks.epic_id` is frozen, NOT dropped. The write surface died; the
    column and the read path did not (spec §1: additive only, never DROP
    COLUMN — the hermes CLI INSERTs tasks with explicit column lists and a
    schema rewrite under the running gateway is an outage, not a cleanup)."""

    def test_the_column_still_exists(self):
        conn = sqlite3.connect(f"file:{_IMPORT_DB}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        finally:
            conn.close()
        self.assertIn("epic_id", cols, "tasks.epic_id was dropped — that is frozen AUDIT")

    def test_the_read_path_still_exists(self):
        from dashboard import object_graph
        self.assertTrue(callable(getattr(object_graph, "list_epics", None)),
                        "the epic READ used for display was deleted with the write surface")


if __name__ == "__main__":
    unittest.main()
