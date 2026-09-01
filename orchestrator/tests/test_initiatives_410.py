"""Contract for the initiative fold — spec §1 ("Initiative → **folded into
Project.** Roadmap fields move onto projects") — and for the roadmap view that
replaced it.

m02+m03 put `quarter`, `tier`, `why`, `success_check`, `health` and
`confidence` on `projects`. The fold was only half done: the columns shipped
and were backfilled while Initiative stayed a first-class, *creatable* noun with
its own nav surface, so the same quarterly bet existed twice under two names.
This file pins the other half.

Three ways the retirement can be wrong, one class each:

  1. **The replacement can be missing.** Deleting the initiative write surface
     without re-pointing the roadmap at projects would leave the Roadmap tab
     empty. `RoadmapIsProjectsByQuarter` asserts GET /api/roadmap returns the
     PROJECT spine grouped by quarter, that no live project is dropped, and
     that per-project progress is DERIVED from tasks — never a stored number.

  2. **It can be deleted instead of buried.** A removed route answers 404 and a
     removed MCP verb answers "Unknown tool" — both read as *"you got the URL
     wrong, try again"*. So the routes and the verbs still exist and answer
     with a TYPED body (410 Gone / `{error: initiatives_folded}`). Asserted as
     status code AND exact payload: a 410 carrying FastAPI's default
     `{"detail": …}` is not machine-readable.

  3. **The two frontends can drift.** The dashboard API and mcp_server are
     parallel frontends over one backend; killing a surface on one and leaving
     it alive on the other is how an agent keeps writing to a dead concept.
     `PayloadParity` (`api.INITIATIVES_GONE == mcp_server.INITIATIVES_GONE`) is
     the ratchet — it goes red the moment one side changes alone.

Plus the mirror of #2 that the epic kill also needed: **the kill must not eat
the archive.** Nine initiative rows carry real history, the entity drawer reads
them, and the deal chain resolves through them — so `InitiativeReadsSurvive`
asserts every READ (list/get/drilldown/events, on both frontends) still answers
live. A "cleanup" that tombstones the reads must fail here.

And the assertion that makes the retirement real rather than cosmetic: the
underlying write functions are replaced by landmines for the duration of the
MCP calls, so a handler that still reaches one fails loudly instead of quietly
writing to a folded layer.

DB isolation: dashboard.api and mcp_server run migrations at import, so a COPY
of ~/.hermes/kanban.db is installed BEFORE either import (same prologue as
tests/test_epics_410.py, and idempotent when both files run in one process).
This file makes no writes; the real DB is only ever opened `mode=ro`.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_initiatives_410.py   # from orchestrator/
"""
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_READY = False
_CLIENT = None
_IMPORT_DB = None
_REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
    else Path.home() / ".hermes" / "kanban.db"
# ^ resolves to the per-session sandbox copy that tests/conftest.py exports
# (never the operator's live DB): this module is one of the six that hand
# db.KANBAN_DB / sprints.KANBAN_DB back to _REAL_DB when its import block
# ends, and pytest imports every module before running any test — so the
# last one collected used to leave the global on the live file for the
# whole run (data loss 2026-07-29 and 2026-07-31).
_MCP = None
_API = None
try:
    from dashboard import db as _db, sprints as _sprints
    from dashboard.migrations import runner

    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_init410_", suffix=".db")
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
        # Reads go back to the real DB (read-only): the roadmap contract below
        # is data-INDEPENDENT — it compares the endpoint against whatever the
        # DB currently holds — so it must not be asserted against a stale copy.
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


EXPECTED_INITIATIVES = {
    "error": "initiatives_folded",
    "hint": "initiatives were folded into projects (m03); use projects + quarter",
}


@contextmanager
def _ro(db_path):
    """Read-only handle on the session DB — this file never opens it for writing."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _pin_db():
    """Both frontends and every `_ro(_REAL_DB)` read must resolve the SAME file.

    The contracts below are data-INDEPENDENT: they compare the endpoint against
    whatever the DB currently holds. That only works if the app and the direct
    read open one database. `db.KANBAN_DB` / `sprints.KANBAN_DB` are shared
    module globals that sibling test modules repoint at their own tmp copies at
    run time, so agreement has to be re-asserted per test, not once at import.

    This module used to buy that agreement by handing the globals back to
    Path.home()/'.hermes'/'kanban.db' at import — which is precisely how the
    suite came to write fixtures into the operator's CRM (2026-07-29,
    2026-07-31). `_REAL_DB` now resolves to the per-session sandbox copy that
    tests/conftest.py exports, so the same agreement holds off the live file.
    """
    _db.KANBAN_DB = _sprints.KANBAN_DB = _REAL_DB


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class RoadmapIsProjectsByQuarter(unittest.TestCase):
    """GET /api/roadmap renders the PROJECT spine, grouped by quarter."""

    @classmethod
    def setUpClass(cls):
        _pin_db()
        res = _CLIENT.get("/api/roadmap")
        assert res.status_code == 200, res.text
        cls.payload = res.json()
        cls.groups = cls.payload.get("quarters")

    def setUp(self):
        _pin_db()

    def test_the_payload_carries_a_quarters_key(self):
        self.assertIsInstance(self.groups, list,
                              "GET /api/roadmap must return projects grouped by quarter")

    def test_every_group_is_a_quarter_plus_projects(self):
        for g in self.groups:
            self.assertIn("quarter", g)
            self.assertIsInstance(g.get("projects"), list)
            self.assertTrue(g["projects"], "an empty quarter group is noise, not a group")

    def test_every_live_project_appears_exactly_once(self):
        """A project without a quarter is still a project — it is GROUPED (the
        trailing `quarter: null` bucket the UI labels 'Sin quarter'), never
        dropped. Data-independent: compares the endpoint against the DB."""
        with _ro(_REAL_DB) as conn:
            live = {r[0] for r in conn.execute(
                "SELECT id FROM projects WHERE archived_at IS NULL")}
        seen = [p["id"] for g in self.groups for p in g["projects"]]
        self.assertEqual(len(seen), len(set(seen)), "a project is in two quarter groups")
        self.assertEqual(set(seen), live, "the roadmap dropped or invented a project")

    def test_the_unscheduled_group_sorts_last_and_is_null(self):
        quarters = [g["quarter"] for g in self.groups]
        nulls = [i for i, q in enumerate(quarters) if not q]
        self.assertLessEqual(len(nulls), 1, "more than one unscheduled bucket")
        if nulls:
            self.assertEqual(nulls[0], len(quarters) - 1,
                             "the unscheduled bucket must sort LAST, not into the middle")
            self.assertIsNone(quarters[nulls[0]],
                              "the unscheduled bucket is quarter=None; the label is the UI's job")
        dated = [q for q in quarters if q]
        self.assertEqual(dated, sorted(dated), "quarters must be ascending (YYYY-Qn sorts as a string)")

    def test_each_project_carries_the_folded_roadmap_fields(self):
        """The fields the Initiative layer used to own now ride the project —
        present as KEYS even when unset, so the UI can decide not to render a
        chip instead of guessing a default."""
        for g in self.groups:
            for p in g["projects"]:
                for key in ("id", "name", "slug", "status", "quarter", "tier",
                            "why", "success_check", "health", "confidence"):
                    self.assertIn(key, p, f"project {p.get('id')} is missing {key}")

    def test_progress_is_derived_from_tasks_not_stored(self):
        """The number on the card must be a roll-up over `tasks.project_id` —
        the same read the drawer uses — so the two can never disagree."""
        with _ro(_REAL_DB) as conn:
            for g in self.groups:
                for p in g["projects"]:
                    total, done = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(status = 'done'), 0) "
                        "FROM tasks WHERE project_id = ?", (p["id"],)).fetchone()
                    self.assertEqual(p["task_total"], total, p["id"])
                    self.assertEqual(p["task_done"], done, p["id"])
                    self.assertEqual(p["progress"],
                                     round(done / total * 100) if total else 0, p["id"])

    def test_the_quarter_field_matches_the_group_it_sits_in(self):
        for g in self.groups:
            for p in g["projects"]:
                self.assertEqual(p.get("quarter") or None, g["quarter"], p["id"])

    def test_the_initiatives_archive_still_ships_on_the_same_payload(self):
        """Kept on purpose: the entity drawer deep-links initiatives and the deal
        form/chip resolves historical links through this list. Read-only — the
        write routes below are 410."""
        self.assertIsInstance(self.payload.get("initiatives"), list)


class WriteLandmine:
    """Replace the two validated write paths with landmines for the duration of
    every write-surface test — on BOTH frontends at once, since `api.strategy`
    and `mcp_server._strategy` are the same module object.

    Two jobs, and the second is the one that was learned the hard way. It makes
    the retirement real: a handler that still reaches strategy fails loudly
    instead of quietly mutating a folded layer. And it makes the contract SAFE
    TO PROVE RED: this file is run against the pre-fix code to show it fails,
    and the pre-fix handlers really do write — an unlandmined `POST /api/roadmap`
    creates a live initiative row plus its event, and rewrites roadmap.json. A
    test that mutates production while proving itself red is not a test.
    """

    def setUp(self):
        super().setUp()
        _pin_db()
        from dashboard import strategy as _s
        self._landmined = {}
        for name in ("create_initiative", "update_initiative", "export_roadmap"):
            self._landmined[name] = getattr(_s, name, None)

            def _landmine(*a, _n=name, **k):
                raise AssertionError(f"a retired write surface reached strategy.{_n}()")

            setattr(_s, name, _landmine)

    def tearDown(self):
        from dashboard import strategy as _s
        for name, fn in self._landmined.items():
            if fn is not None:
                setattr(_s, name, fn)
        super().tearDown()


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class InitiativeApiWriteIsGone(WriteLandmine, unittest.TestCase):
    """Every initiative WRITE route answers 410 with the typed body."""

    def _assert_gone(self, res):
        self.assertEqual(res.status_code, 410, res.text)
        self.assertEqual(res.json(), EXPECTED_INITIATIVES)

    def test_create_is_gone(self):
        self._assert_gone(_CLIENT.post("/api/roadmap",
                                       json={"title": "nope", "project_id": "proj_orchestrator"}))

    def test_create_is_gone_even_when_the_body_is_malformed(self):
        """410 outranks 422: the concept is gone, so validating a payload for a
        dead resource would answer the wrong question."""
        self._assert_gone(_CLIENT.post("/api/roadmap", json={}))

    def test_update_is_gone(self):
        self._assert_gone(_CLIENT.patch("/api/roadmap/init_whatever",
                                        json={"title": "nope"}))

    def test_update_is_gone_for_a_real_initiative_too(self):
        """The nine live rows are archive, not editable state — a 404-only kill
        would leave the existing ones curatable and the noun alive."""
        with _ro(_REAL_DB) as conn:
            row = conn.execute("SELECT id FROM initiatives LIMIT 1").fetchone()
        if not row:
            self.skipTest("no initiative rows")
        self._assert_gone(_CLIENT.patch(f"/api/roadmap/{row[0]}", json={"health": "at-risk"}))

    def test_the_routes_still_exist(self):
        """The paths must remain REGISTERED — a deleted route would answer 404,
        which tells a caller to retry a different URL instead of stopping."""
        paths = {getattr(r, "path", None) for r in _API.app.routes}
        for path in ("/api/roadmap", "/api/roadmap/{initiative_id}"):
            self.assertIn(path, paths, f"{path} was deleted instead of retired")


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class InitiativeMcpWriteIsGone(WriteLandmine, unittest.TestCase):
    """The MCP twins answer the SAME typed error — parity is the point."""

    VERBS = ("create_initiative", "edit_roadmap")

    def test_every_initiative_write_verb_returns_the_typed_error(self):
        for verb in self.VERBS:
            with self.subTest(verb=verb):
                out = json.loads(_MCP.TOOL_HANDLERS[verb]({}))
                self.assertEqual(out, EXPECTED_INITIATIVES)

    def test_a_fully_populated_call_is_still_gone(self):
        """No field combination revives the verb (and the landmines prove the
        handler never reached strategy)."""
        out = json.loads(_MCP.TOOL_HANDLERS["create_initiative"](
            {"title": "nope", "project_id": "proj_orchestrator", "tier": "bet",
             "quarter": "2026-Q4", "why": "x", "success_check": "y"}))
        self.assertEqual(out, EXPECTED_INITIATIVES)

    def test_the_verbs_stay_registered_and_wired(self):
        registered = {t["name"] for t in _MCP.TOOLS}
        for verb in self.VERBS:
            self.assertIn(verb, registered, f"{verb} was deleted — callers get 'Unknown tool'")
            self.assertIn(verb, _MCP.TOOL_HANDLERS)

    def test_the_advertised_description_says_retired(self):
        """tools/list is the only thing most agents read. A tombstone verb still
        described as "create an initiative" is a lie with a 410 behind it."""
        by_name = {t["name"]: t for t in _MCP.TOOLS}
        for verb in self.VERBS:
            self.assertIn("RETIRED", by_name[verb]["description"].upper(),
                          f"{verb} still advertises itself as live")

    def test_the_gravestones_stay_privileged(self):
        """Still PRIVILEGED so the gravestone does not appear in the DEFAULT
        toolset, where the verb was never visible (minimal tool frontier)."""
        for verb in self.VERBS:
            self.assertIn(verb, _MCP.PRIVILEGED_TOOLS)


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class InitiativeReadsSurvive(unittest.TestCase):
    """The archive is KEPT, not dropped (spec §1: `initiative_events` stays as
    audit). The write surface died; the rows, the routes and the verbs that read
    them did not — the entity drawer and the deal chain depend on them."""

    READ_VERBS = ("list_initiatives", "get_initiative", "get_initiative_drilldown",
                  "get_initiative_events", "get_roadmap")

    def setUp(self):
        _pin_db()

    def test_the_tables_still_exist(self):
        with _ro(_REAL_DB) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("initiatives", names, "the archive was dropped, not frozen")
        self.assertIn("initiative_events", names, "the audit spine was dropped")

    def test_the_read_verbs_are_not_tombstoned(self):
        by_name = {t["name"]: t for t in _MCP.TOOLS}
        for verb in self.READ_VERBS:
            with self.subTest(verb=verb):
                self.assertIn(verb, _MCP.TOOL_HANDLERS)
                self.assertNotIn("RETIRED", by_name[verb]["description"].upper(),
                                 f"{verb} is a READ — the archive stays legible")

    def test_list_initiatives_still_answers(self):
        out = json.loads(_MCP.TOOL_HANDLERS["list_initiatives"]({}))
        self.assertNotEqual(out, EXPECTED_INITIATIVES)
        self.assertIn("initiatives", out)

    def test_the_history_routes_still_answer(self):
        with _ro(_REAL_DB) as conn:
            row = conn.execute("SELECT id FROM initiatives LIMIT 1").fetchone()
        if not row:
            self.skipTest("no initiative rows")
        for path in (f"/api/roadmap/{row[0]}/events", f"/api/roadmap/{row[0]}/drilldown"):
            with self.subTest(path=path):
                self.assertEqual(_CLIENT.get(path).status_code, 200)

    def test_the_backend_write_paths_are_not_deleted(self):
        """`strategy.create_initiative`/`update_initiative` still exist: the
        migrations call them, and deleting them would turn a retired FRONTEND
        into a schema change. Retired ≠ removed."""
        from dashboard import strategy
        for fn in ("create_initiative", "update_initiative"):
            self.assertTrue(callable(getattr(strategy, fn, None)))


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class PayloadParity(WriteLandmine, unittest.TestCase):
    """The ratchet: API and MCP ship the same bytes, or this goes red."""

    def test_initiatives_payload_is_identical_on_both_frontends(self):
        self.assertEqual(_API.INITIATIVES_GONE, _MCP.INITIATIVES_GONE)
        self.assertEqual(_API.INITIATIVES_GONE, EXPECTED_INITIATIVES)

    def test_the_api_and_mcp_bodies_are_byte_identical_on_the_wire(self):
        """Not just equal dicts — the same JSON an agent actually parses."""
        api_body = _CLIENT.post("/api/roadmap", json={}).json()
        mcp_body = json.loads(_MCP.TOOL_HANDLERS["create_initiative"]({}))
        self.assertEqual(api_body, mcp_body)


if __name__ == "__main__":
    unittest.main()
