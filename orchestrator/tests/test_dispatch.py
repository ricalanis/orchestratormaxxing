"""Contract for honest dispatch — phase-1 step 7.

The control this replaces wrote `tasks.assignee` and toasted "Dispatched to
<agent>". Nothing spawned, nothing was notified, and the operator pressed it 111
times. So the thing under test here is not "does dispatch work" — it is
**does the system ever claim something happened that did not**.

What that forces this file to pin:

  1. **The EXACT argv of every CLI crossing.** `hermes kanban assign <id>
     default` is POSITIONAL (`--assignee` is not a flag this verb has), and a
     wrong flag would fail *silently in production* while a mocked "it was
     called" assertion stayed green. Every verb's argv is asserted element for
     element, including the absolutely-resolved binary — because a bare
     "hermes" is exactly the bug that 500'd every task create after a reboot.
  2. **The state is observed, never assumed.** `delivered` only when the send
     stub exits 0; `send_failed` (not `spawn_failed`) when the work started and
     only the message failed; `spawn_failed` when the executor refused. The note
     records which side effects landed, so a partial dispatch reads as partial.
  3. **The refusals that must cost nothing.** A done task and a codex dispatch
     with no workspace must not touch the CLI or spawn anything — an outbox row
     in `spawn_failed` with a typed code is the entire effect.
  4. **Red line 10 as an assertion.** The claude branch must never spawn. That
     is policy, not an engineering gap, so it is tested rather than commented.
  5. **The absences that are guards.** `task_runs` is Hermes-owned (ruling 4) —
     the dashboard writes the OUTBOX instead, including Codex's exit code. And
     dispatch is deliberately not an MCP verb (ruling 2): the click is the
     approval, so the absence from mcp_server.py is asserted here.

Every CLI/OS crossing goes through `dispatch._run_cli` / `dispatch._spawn`, so
no test here runs hermes, spawns a Codex, or sends the operator a Telegram message.

DB isolation: a COPY of ~/.hermes/kanban.db per test, schema brought up by
`runner.run()`; `runner.run_backup` stubbed everywhere. The real DB is never
opened for writing.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_dispatch.py   # from orchestrator/
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

_READY = False
_CLIENT = None
_IMPORT_DB = None
_MCP = None
try:
    from dashboard import db as _db, sprints as _sprints, dispatch as _dispatch
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
        # dashboard.api (and mcp_server) run the migration runner at import.
        # Point every DB layer at a throwaway copy FIRST so neither import can
        # touch the real DB, then hand the globals back.
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_dispatch_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        from dashboard.api import app
        from starlette.testclient import TestClient

        _prev_env = os.environ.get("HERMES_KANBAN_DB")
        os.environ["HERMES_KANBAN_DB"] = str(_IMPORT_DB)
        import mcp_server as _MCP
        if _prev_env is None:
            os.environ.pop("HERMES_KANBAN_DB", None)
        else:
            os.environ["HERMES_KANBAN_DB"] = _prev_env

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
PROJECT_BOUND = "proj_disp_bound"       # carries a bound thread
PROJECT_LOOSE = "proj_disp_loose"       # no thread → Hoy fallback
BOUND_THREAD = 990001
HOY_THREAD = 15185                      # seeded by m02_spine on the live DB
HOY_NAME = "📅 Hoy"                    # renamed and station-bound by m12
CHAT = "1234567890"


class FakeProc:
    """Stand-in for the detached Codex process. `communicate()` is what the
    reaper calls; nothing else about a Popen is used."""

    def __init__(self, out="ok\n", code=0):
        self._out, self.returncode = out, code

    def communicate(self):
        return self._out, None


@unittest.skipUnless(_READY, "dashboard / kanban.db unavailable")
class _DispatchCase(unittest.TestCase):
    """Live-DB copy + the migration runner (so m02_spine's threads/outbox are
    real), a self-contained task+thread fixture on top, and BOTH process seams
    replaced by recorders."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_dispatch_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        self._orig_mcp_db = _MCP.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = _MCP.KANBAN_DB = self.tmp
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        runner.run()

        # --- the seams ---------------------------------------------------
        self.calls = []          # every argv that crossed _run_cli, in order
        self.spawned = []        # every argv that crossed _spawn
        self.exit_codes = {}     # {"assign": 1} to make one verb fail
        self.reaped = []
        self._orig_run, self._orig_spawn = _dispatch._run_cli, _dispatch._spawn
        self._orig_reaper = _dispatch._start_reaper
        self.next_proc = FakeProc()

        def fake_run(argv, timeout=30):
            self.calls.append(list(argv))
            verb = argv[2] if len(argv) > 2 and argv[1] == "kanban" else argv[1]
            code = self.exit_codes.get(verb, 0)
            return code, "", f"stub failure for {verb}" if code else ""

        def fake_spawn(argv):
            self.spawned.append(list(argv))
            if isinstance(self.next_proc, Exception):
                raise self.next_proc
            return self.next_proc

        _dispatch._run_cli = fake_run
        _dispatch._spawn = fake_spawn
        _dispatch._start_reaper = lambda did, proc: self.reaped.append((did, proc))
        self._seed()

    def tearDown(self):
        _dispatch._run_cli, _dispatch._spawn = self._orig_run, self._orig_spawn
        _dispatch._start_reaper = self._orig_reaper
        runner.run_backup = self._orig_backup
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints
        _MCP.KANBAN_DB = self._orig_mcp_db
        try:
            self.tmp.unlink()
        except Exception:
            pass

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

    def _task(self, task_id):
        return self._rows("SELECT * FROM tasks WHERE id = ?", (task_id,))[0]

    def _outbox(self, task_id):
        return self._rows(
            "SELECT * FROM task_dispatches WHERE task_id = ? ORDER BY created_at", (task_id,))

    def _seed(self):
        c = self._conn()
        c.execute("PRAGMA foreign_keys = ON")
        for pid, slug, name, repo in (
                (PROJECT_BOUND, "disp-bound", "Bound Project", None),
                (PROJECT_LOOSE, "disp-loose", "Loose Project", str(REPO))):
            c.execute("INSERT INTO projects (id, slug, name, created_at, repo_path) "
                      "VALUES (?,?,?,?,?)", (pid, slug, name, NOW, repo))
        c.execute("INSERT INTO threads (thread_id, chat_id, name, project_id, role, status) "
                  "VALUES (?,?,?,?,?,?)",
                  (BOUND_THREAD, CHAT, "🧑‍💻 Bound", PROJECT_BOUND, "code", "active"))
        c.execute(
            "INSERT INTO threads (thread_id, chat_id, name, project_id, role, status, station) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
            "name=excluded.name, project_id=NULL, status='active', station=excluded.station",
            (HOY_THREAD, CHAT, HOY_NAME, None, "ops", "active", "ritual"),
        )
        for tid, title, status, project, workspace in (
                ("t_disp_ready", "Ready task", "ready", PROJECT_BOUND, None),
                ("t_disp_todo", "Todo task", "todo", PROJECT_BOUND, None),
                ("t_disp_sched", "Scheduled task", "scheduled", PROJECT_BOUND, None),
                ("t_disp_done", "Done task", "done", PROJECT_BOUND, None),
                ("t_disp_loose", "Unbound project task", "ready", PROJECT_LOOSE, None),
                ("t_disp_nows", "No workspace task", "ready", PROJECT_BOUND, None),
                ("t_disp_ws", "Workspace task", "ready", PROJECT_BOUND, str(REPO))):
            c.execute(
                "INSERT INTO tasks (id, title, body, status, created_at, workspace_kind, "
                "workspace_path, project_id) VALUES (?,?,?,?,?,'scratch',?,?)",
                (tid, title, "context\n\n## Acceptance\n- the gate is green\n- nothing else\n",
                 status, NOW, workspace, project))
        c.commit()
        c.close()

    # --- argv shorthands --------------------------------------------------
    @property
    def hermes(self):
        return _db.hermes_bin()

    def kanban_calls(self):
        return [c for c in self.calls if len(c) > 1 and c[1] == "kanban"]

    def send_calls(self):
        return [c for c in self.calls if len(c) > 1 and c[1] == "send"]


class Idempotency(_DispatchCase):
    """A double-tap, a retried fetch and a re-fired cron all carry the same
    dispatch_id — and must produce ONE dispatch."""

    def test_the_same_dispatch_id_twice_is_one_row_and_one_set_of_side_effects(self):
        first = _dispatch.dispatch_task("t_disp_ready", "hermes", dispatch_id="disp_fixed")
        self.assertEqual(first["state"], "delivered", first)
        self.assertFalse(first["idempotent"])
        calls_after_first = len(self.calls)

        second = _dispatch.dispatch_task("t_disp_ready", "hermes", dispatch_id="disp_fixed")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["dispatch_id"], "disp_fixed")
        self.assertEqual(second["state"], "delivered")
        self.assertEqual(len(self.calls), calls_after_first,
                         "a repeated dispatch_id re-fired a side effect")
        self.assertEqual(len(self._outbox("t_disp_ready")), 1)

    def test_a_replay_names_the_thread_the_dispatch_actually_went_to(self):
        """A replay that asks for a DIFFERENT destination still gets the stored
        one — naming the thread this call would have picked would toast a
        destination the message never reached."""
        first = _dispatch.dispatch_task("t_disp_loose", "claude", dispatch_id="disp_replay")
        self.assertEqual(first["thread_name"], HOY_NAME)
        again = _dispatch.dispatch_task("t_disp_loose", "claude",
                                        dispatch_id="disp_replay", thread_id=BOUND_THREAD)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["thread_id"], HOY_THREAD)
        self.assertEqual(again["thread_name"], HOY_NAME)

    def test_two_dispatches_without_an_id_are_two_rows(self):
        _dispatch.dispatch_task("t_disp_ready", "hermes")
        _dispatch.dispatch_task("t_disp_ready", "hermes")
        self.assertEqual(len(self._outbox("t_disp_ready")), 2)


class HermesBranch(_DispatchCase):
    """The verbs, exactly as the hermes CLI declares them."""

    def test_a_ready_task_assigns_subscribes_and_delivers(self):
        res = _dispatch.dispatch_task("t_disp_ready", "hermes")
        self.assertEqual(res["state"], "delivered", res)
        self.assertEqual(res["executor_kind"], "hermes")
        self.assertEqual(res["executor_target"], "default")

        # EXACT argv — `assign` takes the profile POSITIONALLY, and the binary is
        # absolutely resolved (a bare "hermes" FileNotFoundErrors under systemd).
        self.assertEqual(self.kanban_calls()[0],
                         [self.hermes, "kanban", "assign", "t_disp_ready", "default"])
        # A ready task needs NO transition — the second verb is the subscription.
        self.assertEqual(
            self.kanban_calls()[1],
            [self.hermes, "kanban", "notify-subscribe", "t_disp_ready",
             "--platform", "telegram", "--chat-id", CHAT, "--thread-id", str(BOUND_THREAD)])
        self.assertEqual(len(self.kanban_calls()), 2)

        # …then the brief, to the bound thread.
        send = self.send_calls()[0]
        self.assertEqual(send[:5],
                         [self.hermes, "send", "--to", f"telegram:{CHAT}:{BOUND_THREAD}", "-q"])
        self.assertIn("▶ Hermes (runs now)", send[5])
        self.assertIn("the gate is green", send[5])          # its own acceptance text
        self.assertIn("?entity=task:t_disp_ready", send[5])  # the deep link

        # The executor columns, and NOT task_runs (ruling 4).
        task = self._task("t_disp_ready")
        self.assertEqual((task["executor_kind"], task["executor_target"], task["thread_id"]),
                         ("hermes", "default", BOUND_THREAD))
        row = self._outbox("t_disp_ready")[0]
        self.assertEqual(row["state"], "delivered")
        self.assertIn("assign:default", row["note"])
        self.assertIn("notify-subscribe", row["note"])

    def test_a_todo_task_is_promoted_dependency_checked(self):
        res = _dispatch.dispatch_task("t_disp_todo", "hermes")
        self.assertEqual(res["state"], "delivered", res)
        self.assertEqual(self.kanban_calls()[1],
                         [self.hermes, "kanban", "promote", "t_disp_todo"])
        # --force would defeat the dependency check, which is the point of promote.
        self.assertNotIn("--force", self.kanban_calls()[1])

    def test_a_scheduled_task_is_unblocked(self):
        _dispatch.dispatch_task("t_disp_sched", "hermes")
        self.assertEqual(self.kanban_calls()[1],
                         [self.hermes, "kanban", "unblock", "t_disp_sched"])

    def test_a_done_task_is_refused_before_any_cli_call(self):
        """Not a transport failure — a category error. It must cost nothing."""
        res = _dispatch.dispatch_task("t_disp_done", "hermes")
        self.assertEqual(res["state"], "spawn_failed", res)
        self.assertEqual(res["code"], "not_dispatchable")
        self.assertEqual(self.calls, [], "a non-dispatchable task still ran a CLI verb")
        self.assertEqual(self.spawned, [])
        row = self._outbox("t_disp_done")[0]
        self.assertEqual(row["state"], "spawn_failed")
        self.assertIn("not be handed", row["note"])

    def test_a_failed_promote_stops_the_saga_and_never_announces(self):
        self.exit_codes["promote"] = 1
        res = _dispatch.dispatch_task("t_disp_todo", "hermes")
        self.assertEqual(res["state"], "spawn_failed", res)
        self.assertEqual(res["code"], "not_ready")
        self.assertEqual(self.send_calls(), [],
                         "a dispatch that did not happen was announced anyway")
        self.assertIn("assign:default", self._outbox("t_disp_todo")[0]["note"])

    def test_a_failed_subscription_is_recorded_not_promoted_to_a_failure(self):
        """The task IS assigned and ready — the gateway will spawn it. Calling
        that a failed dispatch would be the mirror image of the original lie."""
        self.exit_codes["notify-subscribe"] = 3
        res = _dispatch.dispatch_task("t_disp_ready", "hermes")
        self.assertEqual(res["state"], "delivered", res)
        self.assertIn("notify-subscribe-failed:3", res["note"])

    def test_a_profile_that_does_not_exist_on_disk_is_refused(self):
        res = _dispatch.dispatch_task("t_disp_ready", "hermes", executor_target="researcher")
        self.assertEqual(res["code"], "unsupported_target")
        self.assertEqual(self._outbox("t_disp_ready"), [])


class CodexBranch(_DispatchCase):
    """The only fully-autonomous onward path — and the one that must refuse to
    start without a real directory."""

    def test_no_workspace_fails_the_outbox_row_and_spawns_nothing(self):
        res = _dispatch.dispatch_task("t_disp_nows", "codex")
        self.assertEqual(res["state"], "spawn_failed", res)
        self.assertEqual(res["code"], "no_workspace")
        self.assertEqual(self.spawned, [], "codex was spawned with no workspace")
        self.assertEqual(self.send_calls(), [])
        self.assertIn("workspace_path", self._outbox("t_disp_nows")[0]["note"])

    def test_a_task_workspace_is_spawned_detached_with_the_verified_flags(self):
        res = _dispatch.dispatch_task("t_disp_ws", "codex")
        self.assertEqual(res["state"], "delivered", res)
        argv = self.spawned[0]
        self.assertEqual(argv[:6],
                         [_dispatch._codex_bin(), "exec", "-C", str(REPO), "-s", "workspace-write"])
        self.assertIn("▶ Codex (runs now)", argv[6])
        self.assertIn("the gate is green", argv[6])
        # The resolved path is what the task and the row now carry.
        self.assertEqual(res["executor_target"], str(REPO))
        self.assertEqual(self._task("t_disp_ws")["executor_target"], str(REPO))
        self.assertEqual(self._outbox("t_disp_ws")[0]["executor_target"], str(REPO))
        self.assertEqual(self.kanban_calls(), [], "codex went through the hermes gateway")

    def test_the_project_repo_path_is_the_fallback_workspace(self):
        res = _dispatch.dispatch_task("t_disp_loose", "codex")
        self.assertEqual(res["state"], "delivered", res)
        self.assertEqual(self.spawned[0][3], str(REPO))

    def test_a_spawn_that_raises_is_a_typed_failure_not_a_500(self):
        self.next_proc = FileNotFoundError("codex: not found")
        res = _dispatch.dispatch_task("t_disp_ws", "codex")
        self.assertEqual(res["state"], "spawn_failed")
        self.assertEqual(res["code"], "spawn_error")

    def test_the_reaper_writes_the_outbox_row_and_never_task_runs(self):
        """Ruling 4: task_runs is Hermes-owned. A dashboard-written run row
        would invent runs no worker executed — the same lie, different table."""
        runs_before = self._one("SELECT COUNT(*) FROM task_runs")
        res = _dispatch.dispatch_task("t_disp_ws", "codex")
        did, proc = self.reaped[0]
        self.assertEqual(did, res["dispatch_id"])
        _dispatch._reap(did, FakeProc(out="x" * 3000, code=7))

        row = self._outbox("t_disp_ws")[0]
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(len(row["stdout_tail"]), _dispatch.STDOUT_TAIL_CHARS)
        self.assertEqual(row["state"], "delivered",
                         "the reaper rewrote the delivery state")
        self.assertEqual(self._one("SELECT COUNT(*) FROM task_runs"), runs_before)


class ClaudeBranch(_DispatchCase):
    """Red line 10, as an assertion."""

    def test_claude_never_spawns_anything(self):
        res = _dispatch.dispatch_task("t_disp_ready", "claude")
        self.assertEqual(res["state"], "delivered", res)
        self.assertEqual(self.spawned, [], "claude onward-delegation was automated")
        self.assertEqual(self.kanban_calls(), [], "claude went through the hermes gateway")
        # …and the only thing that DID happen is the brief.
        send = self.send_calls()[0]
        self.assertIn("✋ Claude (brief & notify)", send[5])
        self.assertIn("Paste into a Claude session", send[5])
        self.assertEqual(self._outbox("t_disp_ready")[0]["note"], "brief; send")

    def test_the_module_contains_no_spawn_in_the_claude_path(self):
        src = (REPO / "dashboard" / "dispatch.py").read_text()
        body = src.split("def _dispatch_claude(")[1].split("\ndef ")[0]
        code = body.split('"""')[-1]          # the statements, not the docstring
        self.assertNotIn("_spawn", code)
        self.assertNotIn("Popen", code)


class Delivery(_DispatchCase):
    """The send step — and the difference between "it failed" and "it ran but
    nobody was told"."""

    def test_a_failing_send_is_send_failed_and_the_note_says_what_landed(self):
        self.exit_codes["send"] = 1
        res = _dispatch.dispatch_task("t_disp_ready", "hermes")
        self.assertEqual(res["state"], "send_failed", res)
        self.assertEqual(res["code"], "send_failed")
        note = self._outbox("t_disp_ready")[0]["note"]
        self.assertIn("assign:default", note)          # this DID happen
        self.assertIn("notify-subscribe", note)        # so did this
        self.assertIn("hermes send exited 1", note)    # this did not
        # The work is genuinely underway, so the task keeps its executor columns.
        self.assertEqual(self._task("t_disp_ready")["executor_kind"], "hermes")


class ThreadResolution(_DispatchCase):
    """"The operator never picks a thread." The destination is derived; the picker is
    the escape hatch, not the path."""

    def test_a_bound_project_sends_to_its_own_thread(self):
        res = _dispatch.dispatch_task("t_disp_ready", "claude")
        self.assertEqual(res["thread_id"], BOUND_THREAD)
        self.assertEqual(res["thread_name"], "🧑‍💻 Bound")
        self.assertFalse(res["thread_fallback"])
        self.assertEqual(res["thread_source"], "project")

    def test_an_unbound_project_falls_back_to_hoy_and_says_so(self):
        res = _dispatch.dispatch_task("t_disp_loose", "claude")
        self.assertEqual(res["thread_id"], HOY_THREAD)
        self.assertEqual(res["thread_name"], HOY_NAME)
        self.assertTrue(res["thread_fallback"], "a fallback was reported as a binding")

    def test_an_explicit_thread_wins_and_an_unknown_one_is_refused(self):
        ok = _dispatch.dispatch_task("t_disp_loose", "claude", thread_id=BOUND_THREAD)
        self.assertEqual(ok["thread_id"], BOUND_THREAD)
        self.assertEqual(ok["thread_source"], "explicit")

        bad = _dispatch.dispatch_task("t_disp_loose", "claude", thread_id=424242)
        self.assertEqual(bad["code"], "unknown_thread")
        self.assertEqual(self.send_calls(), self.send_calls()[:1],
                         "an unknown thread still produced a message")


class Route(_DispatchCase):
    """The one route — and the honest status codes."""

    def test_the_route_returns_200_with_the_state_that_actually_happened(self):
        ok = _CLIENT.post("/api/tasks/t_disp_ready/dispatch", json={"executor_kind": "hermes"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["state"], "delivered")

        # A failed dispatch is NOT an HTTP error: the saga ran, and hiding which
        # side effects landed behind a 500 is the dishonesty being removed.
        bad = _CLIENT.post("/api/tasks/t_disp_nows/dispatch", json={"executor_kind": "codex"})
        self.assertEqual(bad.status_code, 200, bad.text)
        self.assertEqual(bad.json()["state"], "spawn_failed")
        self.assertEqual(bad.json()["code"], "no_workspace")

    def test_malformed_requests_are_typed_http_errors(self):
        self.assertEqual(_CLIENT.post("/api/tasks/t_nope/dispatch",
                                      json={"executor_kind": "hermes"}).status_code, 404)
        self.assertEqual(_CLIENT.post("/api/tasks/t_disp_ready/dispatch",
                                      json={"executor_kind": "kimi-coder"}).status_code, 400)
        self.assertEqual(_CLIENT.post("/api/tasks/t_disp_ready/dispatch",
                                      json={}).status_code, 400)

    def test_the_history_endpoint_shows_dispatch_as_an_event(self):
        _dispatch.dispatch_task("t_disp_ready", "hermes", dispatch_id="disp_hist")
        res = _CLIENT.get("/api/tasks/t_disp_ready/dispatches")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual([d["id"] for d in res.json()["dispatches"]], ["disp_hist"])

    def test_dispatch_is_not_reachable_from_an_agent(self):
        """Ruling 2: phase-1 dispatch is human-initiated — the click IS the
        approval, which is why no ASK queue is needed yet. The absence from the
        MCP surface is the guard, so a parity sweep has to fail this to remove it.

        Scope note: the legacy PRIVILEGED verb `dispatch_to_agent` still exists
        and is the MCP twin of the control this step deleted — it writes
        `tasks.assignee` and nothing runs. It is out of THIS step's file scope
        (mcp_server.py takes the provenance stamp only) and belongs to the
        phase-2 MCP-parity sweep. What must never appear is an agent-reachable
        path into the SAGA."""
        self.assertIn("/api/tasks/{task_id}/dispatch",
                      {getattr(r, "path", None) for r in app.routes})
        self.assertNotIn("dispatch_task", _MCP.TOOL_HANDLERS)
        mcp_src = (REPO / "mcp_server.py").read_text()
        self.assertFalse("dispatch.dispatch_task" in mcp_src or "from dashboard import dispatch" in mcp_src,
                         "the dispatch saga became reachable from MCP")
        self.assertNotIn("dispatch_task", {t["name"] for t in _MCP.TOOLS})


class Provenance(_DispatchCase):
    """Inbound Telegram provenance — env-derived and registry-verified, or NULL.

    Known-partial by design (ruling 6): if the live gateway never sets
    HERMES_MCP_SESSION_KEY this stays dormant and the column stays honestly
    empty. What must NEVER happen is a stamped value that isn't evidence."""

    def setUp(self):
        super().setUp()
        self._orig_key = _MCP.SERVER_SESSION_KEY
        self._orig_cli = _MCP.run_hermes_cli

    def tearDown(self):
        _MCP.SERVER_SESSION_KEY = self._orig_key
        _MCP.run_hermes_cli = self._orig_cli
        super().tearDown()

    _seq = 0

    def _create(self, env_key, **args):
        _MCP.SERVER_SESSION_KEY = env_key
        created = {}

        def fake_cli(argv):
            Provenance._seq += 1
            tid = f"t_prov_{Provenance._seq}"
            created["id"] = tid
            c = self._conn()
            c.execute("INSERT INTO tasks (id, title, status, created_at, workspace_kind) "
                      "VALUES (?,?,'ready',?, 'scratch')", (tid, argv[1], NOW))
            c.commit()
            c.close()
            return 0, json.dumps({"id": tid}), ""

        _MCP.run_hermes_cli = fake_cli
        out = json.loads(_MCP.tool_create_task({"title": "provenance probe", **args}))
        return created["id"], out

    def test_a_gateway_topic_session_stamps_the_thread(self):
        tid, out = self._create(f"agent:main:telegram:dm:{CHAT}:{BOUND_THREAD}")
        self.assertEqual(self._task(tid)["thread_id"], BOUND_THREAD)
        self.assertEqual(out.get("thread_id"), BOUND_THREAD)

    def test_an_unregistered_topic_stamps_nothing(self):
        tid, _ = self._create(f"agent:main:telegram:dm:{CHAT}:424242")
        self.assertIsNone(self._task(tid)["thread_id"])

    def test_a_malformed_or_absent_key_stamps_nothing(self):
        for key in (None, "", "agent:main:telegram:dm:abc:def", "claude-code-session-1",
                    f"agent:main:slack:dm:{CHAT}:{BOUND_THREAD}"):
            tid, _ = self._create(key)
            self.assertIsNone(self._task(tid)["thread_id"], key)

    def test_a_model_supplied_thread_is_never_trusted(self):
        """Provenance a caller can choose is not provenance. The arg is ignored;
        only the environment-set gateway key counts."""
        tid, _ = self._create(None, session_key=f"agent:main:telegram:dm:{CHAT}:{BOUND_THREAD}",
                              thread_id=BOUND_THREAD)
        self.assertIsNone(self._task(tid)["thread_id"])


@unittest.skipUnless(_READY, "dashboard unavailable")
class CanonicalDashboardUrl(unittest.TestCase):
    """ONE address, resolved by ONE function, with the tenant's value in CONFIG.

    Deep links are the whole point of the brief (`entity_link`) and of
    dispatch's task links: a line in Telegram is only two taps from a state
    change if the URL in it resolves on the device reading it. Three answers
    once existed (two loopback defaults plus a third, divergent announced
    address with the wrong protocol), so the contract is about the SHAPE, not
    the string: one resolver, every reader delegating to it, and the tenant's
    reachable address arriving via config — `$DASHBOARD_URL` >
    `$ORCHESTRATORMAXXING_DASHBOARD_URL` > fleet.env — over a neutral loopback
    default that is true on any machine with no configuration at all."""

    # An injected fixture the tests place INTO config channels; it is never a
    # value the shipped code may carry.
    CANONICAL = "https://myhost.tailnet-example.ts.net:5555"

    def setUp(self):
        self._prior = {k: os.environ.get(k) for k in
                       ("DASHBOARD_URL", "ORCHESTRATORMAXXING_DASHBOARD_URL",
                        "ORCHESTRATORMAXXING_FLEET_ENV")}
        os.environ.pop("DASHBOARD_URL", None)
        os.environ.pop("ORCHESTRATORMAXXING_DASHBOARD_URL", None)
        # The dev/server machine may carry a real fleet.env; the contract
        # must not read it.
        os.environ["ORCHESTRATORMAXXING_FLEET_ENV"] = os.devnull

    def tearDown(self):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _mcp_url(self):
        return json.loads(_MCP.tool_get_dashboard_url({}))["url"]

    def test_the_fleet_address_reaches_every_reader(self):
        """The tenant's reachable (tailnet) address is config, not code: set it
        the way the fleet deploy does and every reader must announce it —
        through the env channel and through the fleet.env file channel (the
        systemd services export nothing, so the private runtime rides the
        file)."""
        os.environ["ORCHESTRATORMAXXING_DASHBOARD_URL"] = self.CANONICAL
        self.assertEqual(_db.dashboard_url(), self.CANONICAL)
        self.assertEqual(_dispatch._dashboard_url(), self.CANONICAL)
        self.assertEqual(self._mcp_url(), self.CANONICAL)
        os.environ.pop("ORCHESTRATORMAXXING_DASHBOARD_URL", None)
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(f"ORCHESTRATORMAXXING_DASHBOARD_URL={self.CANONICAL}\n")
        self.addCleanup(os.unlink, fh.name)
        os.environ["ORCHESTRATORMAXXING_FLEET_ENV"] = fh.name
        self.assertEqual(_db.dashboard_url(), self.CANONICAL)

    def test_every_reader_resolves_through_the_one_function(self):
        """Asserted under an OVERRIDE as well: three constants that happen to
        carry the same literal are not one source of truth — they are three that
        have not drifted yet."""
        os.environ["DASHBOARD_URL"] = "https://elsewhere.example:9443"
        for got in (_db.dashboard_url(), _dispatch._dashboard_url(), self._mcp_url()):
            self.assertEqual(got, "https://elsewhere.example:9443")

    def test_a_trailing_slash_never_doubles_in_a_deep_link(self):
        os.environ["DASHBOARD_URL"] = "https://dash.example/"
        self.assertEqual(_db.dashboard_url(), "https://dash.example")
        self.assertEqual(_dispatch.task_link("t_x"), "https://dash.example/?entity=task:t_x")
        self.assertEqual(self._mcp_url(), "https://dash.example")

    def test_an_empty_env_var_is_not_a_url(self):
        os.environ["DASHBOARD_URL"] = ""
        self.assertEqual(_db.dashboard_url(), _db.DASHBOARD_URL_DEFAULT)
        os.environ["ORCHESTRATORMAXXING_DASHBOARD_URL"] = self.CANONICAL
        self.assertEqual(_db.dashboard_url(), self.CANONICAL)
        os.environ["ORCHESTRATORMAXXING_DASHBOARD_URL"] = ""
        self.assertEqual(_db.dashboard_url(), _db.DASHBOARD_URL_DEFAULT)

    def test_the_tool_announces_exactly_one_address(self):
        """The second field was not documentation — an agent reading
        `tailscale_url` got a URL that has never worked."""
        os.environ["ORCHESTRATORMAXXING_DASHBOARD_URL"] = self.CANONICAL
        payload = json.loads(_MCP.tool_get_dashboard_url({}))
        self.assertNotIn("tailscale_url", payload)
        addresses = [v for v in payload.values()
                     if isinstance(v, str) and v.startswith(("http://", "https://"))]
        self.assertEqual(addresses, [self.CANONICAL], payload)

    def test_the_default_literal_lives_in_exactly_one_place(self):
        """Structural, because "they agree today" is what the three constants
        also said. A second copy of the literal is a second default."""
        carriers = []
        for rel in ("dashboard/db.py", "dashboard/dispatch.py", "mcp_server.py",
                    "dashboard/brief.py", "dashboard/api.py"):
            if "DASHBOARD_URL_DEFAULT = " in (REPO / rel).read_text():
                carriers.append(rel)
        self.assertEqual(carriers, ["dashboard/db.py"], carriers)
        self.assertIn('DASHBOARD_URL_DEFAULT = "http://127.0.0.1:3000"',
                      (REPO / "dashboard" / "db.py").read_text())
        self.assertNotIn("127.0.0.1:3000", (REPO / "dashboard" / "dispatch.py").read_text())
        # mcp_server may carry the loopback literal EXACTLY once: the
        # DASHBOARD_INTERNAL_URL default for the same-machine _dash() proxy,
        # which must not depend on tailscale. Human-facing links never use it.
        mcp_lines = [ln for ln in (REPO / "mcp_server.py").read_text().splitlines()
                     if "127.0.0.1:3000" in ln]
        self.assertEqual(len(mcp_lines), 1, mcp_lines)
        self.assertIn("DASHBOARD_INTERNAL_URL", mcp_lines[0])


@unittest.skipUnless(_READY, "dashboard unavailable")
class UiContract(unittest.TestCase):
    """index.html has no build step and no unit-test harness, so the honesty
    properties of the control are asserted against the source. (Playwright is
    deliberately not extended here: every drawer spec shares fixtures with the
    other drawer suites, and this step's rule is "never refactor index.html and
    ship a feature in the same session" — red line 12.)"""

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "dashboard" / "templates" / "index.html").read_text()

    def _has(self, needle):
        # assertIn against a 1 MB haystack dumps the whole file into the failure
        # report; a boolean keeps the red readable.
        return needle in self.src

    def test_the_flag_writing_control_is_gone(self):
        """The old dispatchTo helper → PATCH assignee → "Dispatched to <agent>".
        Deleted, not deprecated: a lying control left reachable is still a lying
        control."""
        self.assertFalse(self._has("dispatchTo("), "the flag-writing dispatch is still reachable")
        self.assertFalse(self._has("DISPATCH_AGENTS"), "the agent-name menu survived")

    def test_the_three_named_destinations_are_the_menu(self):
        for label in ("▶", "Hermes (runs now)", "Codex (runs now)", "✋", "Claude (brief & notify)"):
            self.assertTrue(self._has(label), label)
        # Two clicks, zero typed fields: the destination is still derived
        # server-side, so the body carries the kind and the idempotency key —
        # and nothing the operator has to type.
        self.assertTrue(self._has(
            "JSON.stringify({ executor_kind: kind, dispatch_id: dispatchId })"))

    def test_the_double_tap_cannot_fire_two_sagas(self):
        """dispatch.py has taken a `dispatch_id` since it shipped ("a double-tap
        must not send two Telegram messages or spawn two Codex runs") — but the
        UI never sent one, so the server minted a fresh key per call, INSERT OR
        IGNORE could never collide, and the guard was dead code from its only
        caller. On a saga that can hold for 60s of subprocess time."""
        self.assertTrue(self._has("crypto.randomUUID"), "no client-minted key")
        self.assertTrue(self._has("dispatch_id: dispatchId"), "the key is not sent")
        # In-flight guard + the visible half (the button the operator just hit).
        self.assertTrue(self._has("DISPATCH_INFLIGHT.has(key)"))
        self.assertTrue(self._has("DISPATCH_INFLIGHT.add(key)"))
        self.assertTrue(self._has("DISPATCH_INFLIGHT.delete(key)"))
        self.assertTrue(self._has("if (btn) btn.disabled = true;"))
        # Every caller hands the event over, or there is no button to disable.
        self.assertFalse(self._has("dispatchTask('${taskId}','${d.kind}')"),
                         "the card menu still calls dispatchTask without its event")
        self.assertFalse(self._has("dispatchTask('${e.id}','${d.kind}')"),
                         "the drawer still calls dispatchTask without its event")
        # The key survives a NO-ANSWER retry (that is the only way to learn
        # whether the first attempt ran) and is dropped once a response lands.
        body = self.src.split("async function dispatchTask(")[1].split("\n    }")[0]
        self.assertIn("DISPATCH_IDS.delete(key)", body)
        self.assertLess(body.index("DISPATCH_IDS.delete(key)"), body.index("} catch (e) {"),
                        "the key must be cleared on a RESPONSE, not on a network failure")

    def test_a_failed_claude_send_does_not_claim_anything_ran(self):
        """`send_failed` means two different things by kind. For hermes/codex the
        work is running and only the announcement failed. For claude the brief IS
        the dispatch — _dispatch_claude performs no side effect at all — so
        "claude is running" would be the exact lie this module exists to remove
        (red line 10: Claude is brief-and-notify; it never runs)."""
        block = self.src.split("function dispatchToastText(")[1].split("\n    }")[0]
        self.assertIn("r.executor_kind === 'claude'", block)
        self.assertIn("No se envió nada", block)
        # ...and the "is running" phrasing survives on exactly ONE code branch
        # (comment lines excluded — the rule is about what the toast can SAY).
        running = [line for line in block.splitlines()
                   if "is running" in line and not line.strip().startswith("//")]
        self.assertEqual(len(running), 1, running)
        self.assertNotIn("'claude'", running[0])

    def test_the_toast_is_derived_from_the_returned_state(self):
        """There is no branch that can render success without the server having
        said `delivered` — that is what "honest by construction" means here."""
        self.assertTrue(self._has("/dispatch`"))
        block = self.src.split("function dispatchToastText(")[1].split("\n    }")[0]
        for state in ("delivered", "spawn_failed", "send_failed"):
            self.assertIn(state, block)
        self.assertIn("thread_fallback", block)
        self.assertIn("thread_name", block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
