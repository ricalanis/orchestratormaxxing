"""conftest.py — shared pytest fixtures for the orchestrator test suite.

Two process-wide invariants, both established at conftest IMPORT time (pytest
loads conftest.py before it collects — and therefore before it imports — any
test module, which is the only moment early enough for either of them).

1. DB SANDBOX — the suite must never open ~/.hermes/kanban.db
   ------------------------------------------------------------------
   Twice the suite wrote fixtures into the operator's live CRM (2026-07-29:
   600+ rows; 2026-07-31: 34 fixture deals + 25 fixture projects + accounts,
   contacts and events). The mechanism is a shared module global, not a single
   careless test:

     * `dashboard.db.KANBAN_DB` (and `dashboard.sprints.KANBAN_DB`) are plain
       module globals, resolved once at import from $HERMES_KANBAN_DB else
       Path.home()/'.hermes'/'kanban.db'.
     * Most test modules repoint them at a private tmp copy at IMPORT time, and
       six modules hand the global back to the REAL path when their import
       block finishes (test_deliver_deal, test_threads_api, test_dispatch,
       test_epics_410, test_initiatives_410, test_omnisearch).
     * pytest imports EVERY test module during collection before running ANY
       test. So whichever of those six is collected last leaves the global
       pointing at the live DB for the entire run — measured: at
       `pytest_collection_finish`, KANBAN_DB == the live path.
     * Modules that redirected only at import and never re-assert per test
       (test_crm_growth, test_readiness, …) then run their fixtures through it.
       Measured on a canary copy: 2069 live-DB connections, 1530 of them from
       test_crm_growth and 452 from test_readiness — i.e. exactly the observed
       leak signature (Stall Co, Readiness deal, Delivered *, Lost Co).

   The fix is to make the DEFAULT safe rather than to police 60 modules: copy
   the live DB once per session into a temp sandbox and export
   $HERMES_KANBAN_DB before anything imports dashboard.*. Every module that
   resolves through the env var (dashboard.db, dashboard.sprints, mcp_server,
   dashboard.day_review, and the six modules above, which were edited to honour
   it) then lands in the sandbox, and per-module tmp copies build on top of a
   safe base instead of on the real file.

   `dashboard.db.assert_not_live_db` is the second layer: a runtime tripwire
   that raises if a test run ever resolves the live path again, so a future
   regression fails loudly instead of corrupting data.

2. AUTH BYPASS — TESTING=1 (and an empty HERMES_DASHBOARD_TOKEN)
   ------------------------------------------------------------------
   Set before any test imports dashboard.api, so MutatingAuthMiddleware takes
   its test-mode bypass and the TestClient can make POST/PATCH/DELETE calls
   without an Authorization header.

   Why TESTING=1 and not just an empty token: test modules import dashboard.api
   at process scope, and the token is captured once at import. The dedicated
   auth guard (test_auth_middleware.py) sets a real HERMES_DASHBOARD_TOKEN
   before its own import, which — because the module is cached — would
   otherwise leak a live token into every other test module and 401 their
   mutating calls. The TESTING bypass is evaluated per-request and
   short-circuits before that cached token is consulted, so suite ordering
   can't poison the gate. The auth guard clears TESTING around its own cases to
   assert real enforcement; the DB tripwire above also keys off
   PYTEST_CURRENT_TEST, so it stays armed through those windows.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# --- 2. auth bypass (must precede any dashboard.api import) ------------------
os.environ["TESTING"] = "1"
os.environ["HERMES_DASHBOARD_TOKEN"] = ""
# Operator identity is config (ORCHESTRATORMAXXING_OPERATOR_ALIASES): the digestion
# fixtures seed owner "Ric", so the suite-wide baseline must include it. Tests
# that pin other aliases save/restore rather than pop, so this survives the run.
os.environ.setdefault("ORCHESTRATORMAXXING_OPERATOR_ALIASES", "ric, operator")

# --- 1. DB sandbox (must precede any dashboard.db / mcp_server import) -------
LIVE_DB = Path.home() / ".hermes" / "kanban.db"
SESSION_DB = None
_SANDBOX_DIR = None


def _install_db_sandbox() -> None:
    """Point $HERMES_KANBAN_DB at a per-session copy of the live DB.

    Idempotent, and deliberately a no-op when $HERMES_KANBAN_DB already names
    something other than the live file (an outer harness that pinned its own
    fixture DB stays in charge). If the env var *is* the live path — which is
    what the systemd unit exports, so it is inherited by any shell started from
    it — it is overridden: no test run may point there.
    """
    global SESSION_DB, _SANDBOX_DIR
    if SESSION_DB is not None:
        return

    preset = os.environ.get("HERMES_KANBAN_DB")
    if preset:
        try:
            already_safe = os.path.realpath(preset) != os.path.realpath(str(LIVE_DB))
        except Exception:
            already_safe = False
        if already_safe:
            SESSION_DB = Path(preset)
            _repoint_imported_modules()
            return

    _SANDBOX_DIR = tempfile.mkdtemp(prefix="hermes_test_session_")
    sandbox = Path(_SANDBOX_DIR) / "kanban.db"
    if LIVE_DB.exists():
        # Copy, never open: the source is read once here and never again.
        shutil.copy(LIVE_DB, sandbox)
    else:
        sandbox.touch()
    SESSION_DB = sandbox
    os.environ["HERMES_KANBAN_DB"] = str(sandbox)
    _repoint_imported_modules()


def _repoint_imported_modules() -> None:
    """Re-point module globals for anything already imported.

    conftest normally wins the race (pytest imports it before collecting), but
    a plugin, an `-p` module or a rootdir conftest can pull dashboard.db in
    first; then its KANBAN_DB is already frozen at the live path and setting the
    env var alone would not move it.
    """
    for name, attr in (("dashboard.db", "KANBAN_DB"),
                       ("dashboard.sprints", "KANBAN_DB"),
                       ("mcp_server", "KANBAN_DB")):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, attr):
            setattr(mod, attr, Path(SESSION_DB))


@atexit.register
def _cleanup_db_sandbox():  # pragma: no cover
    if _SANDBOX_DIR:
        shutil.rmtree(_SANDBOX_DIR, ignore_errors=True)


_install_db_sandbox()


def pytest_collection_finish(session):
    """Belt-and-braces: collection is where the damage was done (every test
    module's import block runs here), so re-assert the sandbox once collection
    is over, before the first test runs.

    It REPORTS as well as repairs. A silent repair would hide exactly the
    regression this file exists to prevent — the module global was left on the
    live path and nothing said so — so any offender is named on stderr. The
    repair keeps the run safe; the message keeps it visible.
    """
    live = os.path.realpath(str(LIVE_DB))
    offenders = []
    for name, attr in (("dashboard.db", "KANBAN_DB"),
                       ("dashboard.sprints", "KANBAN_DB"),
                       ("mcp_server", "KANBAN_DB")):
        mod = sys.modules.get(name)
        if mod is None or not hasattr(mod, attr):
            continue
        try:
            if os.path.realpath(str(getattr(mod, attr))) == live:
                offenders.append(f"{name}.{attr}")
        except Exception:
            pass
    _repoint_imported_modules()
    if offenders:
        sys.stderr.write(
            "\n[conftest] WARNING: collection left {} pointing at the LIVE "
            "kanban.db ({}). Repointed at the session sandbox {} so this run is "
            "safe, but a test module is handing the global back to "
            "Path.home()/'.hermes'/'kanban.db' at import — fix it there.\n"
            .format(", ".join(offenders), live, SESSION_DB))
