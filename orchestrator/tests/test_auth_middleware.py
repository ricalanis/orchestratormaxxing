"""Auth middleware regression guard.

Pins the Bearer auth gate on mutating endpoints:
  - POST/PATCH/DELETE/PUT without a token → 401 + WWW-Authenticate
  - POST/PATCH/DELETE/PUT with a wrong token → 401
  - POST/PATCH/DELETE/PUT with the correct token → passes the gate (200/404 etc.)
  - GET requests never need a token (200)

DB isolation: a temp copy of ~/.hermes/kanban.db is used so the
``hermes kanban create`` subprocess (invoked by POST /api/tasks) writes
to the temp DB, not production. Both the Python modules and the CLI
subprocess honour ``HERMES_KANBAN_DB``.
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Set a known test token BEFORE importing dashboard.api.
_TEST_TOKEN = "test-token-regression-guard-12345"
os.environ["HERMES_DASHBOARD_TOKEN"] = _TEST_TOKEN

# --- DB isolation: redirect kanban DB to a temp copy BEFORE import ---
_REAL_DB = Path.home() / ".hermes" / "kanban.db"
_TMP_DB = None
if _REAL_DB.exists():
    _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_auth_", suffix=".db")
    os.close(_fd)
    shutil.copy(_REAL_DB, _tmp)
    _TMP_DB = Path(_tmp)
    os.environ["HERMES_KANBAN_DB"] = str(_TMP_DB)


@atexit.register
def _cleanup_tmp_db():
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import api as _api_mod  # noqa: E402 — env must be set before this import
from dashboard.api import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_CLIENT = TestClient(app, raise_server_exceptions=False)

_AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}"}
_BAD = {"Authorization": "Bearer wrong-token"}


class AuthGate(unittest.TestCase):
    """Mutating requests without a valid Bearer token get 401."""

    def setUp(self):
        # conftest sets TESTING=1 to bypass the gate for the rest of the suite;
        # this guard is the one place that must exercise real enforcement, so
        # clear it per-test (the middleware reads TESTING per-request) and
        # restore it after so downstream modules keep their bypass.
        self._prev_testing = os.environ.pop("TESTING", None)
        # _DASH_TOKEN is captured once at dashboard.api import. Under full-suite
        # collection another module (tests/test_attachments.py since 2026-08-01)
        # imports dashboard.api first, while conftest's HERMES_DASHBOARD_TOKEN=""
        # is in effect — freezing the token as None and leaving the gate
        # dev-mode-open. Pin the module global per-test so this guard asserts
        # real enforcement regardless of collection order; restore in tearDown.
        self._prev_dash_token = _api_mod._DASH_TOKEN
        _api_mod._DASH_TOKEN = _TEST_TOKEN

    def tearDown(self):
        _api_mod._DASH_TOKEN = self._prev_dash_token
        if self._prev_testing is not None:
            os.environ["TESTING"] = self._prev_testing

    def test_post_without_token_is_401(self):
        r = _CLIENT.post("/api/tasks", json={"title": "should_be_blocked"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)

    def test_post_with_wrong_token_is_401(self):
        r = _CLIENT.post("/api/tasks", json={"title": "should_be_blocked"},
                         headers=_BAD)
        self.assertEqual(r.status_code, 401)

    def test_patch_without_token_is_401(self):
        r = _CLIENT.patch("/api/tasks/t_nonexist", json={"title": "x"})
        self.assertEqual(r.status_code, 401)

    def test_delete_without_token_is_401(self):
        r = _CLIENT.delete("/api/tasks/t_nonexist")
        self.assertEqual(r.status_code, 401)

    def test_post_with_correct_token_passes_gate(self):
        # The task will be created (200) — auth passes, business logic runs.
        r = _CLIENT.post("/api/tasks",
                         json={"title": "AUTH_REGRESSION_TEST_delete_me",
                               "assignee": "default"},
                         headers=_AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json().get("task_id")
        # Clean up.
        if tid:
            _CLIENT.delete(f"/api/tasks/{tid}", headers=_AUTH)

    def test_delete_with_correct_token_passes_gate(self):
        # Nonexistent task → 404 (not 401) — auth passes, business logic 404s.
        r = _CLIENT.delete("/api/tasks/t_nonexist", headers=_AUTH)
        self.assertEqual(r.status_code, 404)

    def test_get_never_needs_token(self):
        r = _CLIENT.get("/api/tasks?status=ready")
        self.assertEqual(r.status_code, 200)

    def test_get_health_never_needs_token(self):
        r = _CLIENT.get("/healthz")
        self.assertEqual(r.status_code, 200)


    # --- Evidencia de conversaciones: los GET también quedan detrás del bearer.
    # No es defensa contra quien ya puede abrir la página (el dashboard le sirve
    # el token en el HTML y vive solo en el tailnet, que es la frontera real);
    # es lo que impide que un cliente sin credencial —incluido el caso "sin
    # token configurado", que hoy deja todo abierto— lea habla de clientes.

    def test_suggestions_get_requires_token(self):
        r = _CLIENT.get("/api/suggestions")
        self.assertEqual(r.status_code, 401)

    def test_objectives_get_requires_token(self):
        self.assertEqual(_CLIENT.get("/api/objectives").status_code, 401)

    def test_a_suggestion_subpath_is_also_protected(self):
        """Un frozenset de coincidencia exacta dejaría abierta cualquier
        subruta."""
        self.assertEqual(_CLIENT.get("/api/suggestions/sug_abc").status_code, 401)

    def test_suggestions_get_accepts_operator_token(self):
        from dashboard.migrations import m15_differential_capture as m15
        from dashboard import db as _db
        conn = _db.get_conn()
        try:
            m15.apply(conn); conn.commit()
        finally:
            conn.close()
        self.assertEqual(_CLIENT.get("/api/suggestions", headers=_AUTH).status_code, 200)

    def test_the_numbers_only_status_stays_open(self):
        """`capture/status` no lleva citas: es el monitoreo que puede vivir
        fuera de la frontera."""
        from dashboard.migrations import m15_differential_capture as m15
        from dashboard.migrations import m16_capture_receipts as m16
        from dashboard import db as _db
        conn = _db.get_conn()
        try:
            m15.apply(conn); m16.apply(conn); conn.commit()
        finally:
            conn.close()
        self.assertEqual(_CLIENT.get("/api/capture/status").status_code, 200)

    def test_sensitive_personal_get_requires_token(self):
        r = _CLIENT.get("/api/personal/okrs")
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)

    def test_sensitive_personal_get_rejects_wrong_token(self):
        r = _CLIENT.get("/api/personal/okrs", headers=_BAD)
        self.assertEqual(r.status_code, 401)

    def test_sensitive_personal_get_accepts_operator_token(self):
        # conftest re-points dashboard.db after collection; install this
        # feature's additive schema on that final sandbox before exercising it.
        from dashboard import okrs
        okrs.ensure_schema()
        r = _CLIENT.get("/api/personal/okrs", headers=_AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json().get("objectives", [])), 4)

    def test_401_has_www_authenticate(self):
        r = _CLIENT.post("/api/tasks", json={"title": "x"})
        self.assertEqual(r.headers.get("WWW-Authenticate", ""), 'Bearer realm="hermes-dashboard"')

    def test_401_body_has_detail(self):
        r = _CLIENT.post("/api/tasks", json={"title": "x"})
        body = r.json()
        self.assertIn("unauthorized", body.get("detail", "").lower())


if __name__ == "__main__":
    unittest.main()
