"""Security regression guard for the MCP SSE server (P0 hardening).

Pins the three gates: Bearer auth (401 without/with-wrong token when a token
is configured; pass with the right one; dev mode open when unconfigured),
Origin validation (403 for an unlisted browser Origin; absent Origin — a CLI
MCP client — passes), and the unauthenticated /health. A refactor that
reorders the middleware, loosens the allowlist, or drops WWW-Authenticate
fails here before it ships.

The token is read PER-REQUEST (env first, file fallback), so tests toggle
os.environ directly — no module reload needed. Stdlib unittest, no pytest.

Run: python -m unittest tests.test_sse_security   # from orchestrator/
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_sse_server as sse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

GOOD = "test-token-abc123"
ALLOWED_ORIGIN = "http://localhost:5555"
BAD_ORIGIN = "https://evil.example.com"


class TestSSESecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(sse.app, raise_server_exceptions=False)

    def setUp(self):
        os.environ["HERMES_MCP_SSE_TOKEN"] = GOOD
        sse._RATE_BUCKETS.clear()  # isolation: no cross-test rate carryover

    def tearDown(self):
        os.environ.pop("HERMES_MCP_SSE_TOKEN", None)

    def _msg(self, **kw):
        return self.client.post("/messages?session_id=nope", json={}, **kw)

    # --- P0-1: Bearer auth ---
    def test_no_token_401(self):
        r = self._msg()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    def test_wrong_token_401(self):
        r = self._msg(headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(r.status_code, 401)

    def test_correct_token_passes_auth(self):
        # 404 = past the auth gate (unknown session id is the NEXT check).
        r = self._msg(headers={"Authorization": f"Bearer {GOOD}"})
        self.assertEqual(r.status_code, 404)

    def test_sse_requires_auth_too(self):
        # TestClient would hang on a real SSE stream; an unauthorized request
        # is rejected before streaming starts, so this returns immediately.
        r = self.client.get("/sse")
        self.assertEqual(r.status_code, 401)

    def test_dev_mode_open_when_unconfigured(self):
        os.environ.pop("HERMES_MCP_SSE_TOKEN", None)
        if sse._TOKEN_FILE.exists():
            self.skipTest("token file present on this machine — dev mode not reachable")
        r = self._msg()
        self.assertEqual(r.status_code, 404)  # straight to the session check

    # --- P0-3: Origin validation ---
    def test_disallowed_origin_403(self):
        r = self._msg(headers={"Authorization": f"Bearer {GOOD}", "Origin": BAD_ORIGIN})
        self.assertEqual(r.status_code, 403)

    def test_allowed_origin_passes(self):
        r = self._msg(headers={"Authorization": f"Bearer {GOOD}", "Origin": ALLOWED_ORIGIN})
        self.assertEqual(r.status_code, 404)

    def test_absent_origin_cli_client_passes(self):
        r = self._msg(headers={"Authorization": f"Bearer {GOOD}"})
        self.assertEqual(r.status_code, 404)

    # --- /health stays open ---
    def test_health_unauthenticated(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["auth"], "bearer")

    def test_health_reports_dev_mode(self):
        os.environ.pop("HERMES_MCP_SSE_TOKEN", None)
        if sse._TOKEN_FILE.exists():
            self.skipTest("token file present — dev mode not reachable")
        r = self.client.get("/health")
        self.assertEqual(r.json()["auth"], "DEV-MODE-OPEN")

    # --- P0-2: CORS reflects the allowlist ---
    def test_cors_preflight_allowlisted(self):
        r = self.client.options("/messages", headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, Authorization"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), ALLOWED_ORIGIN)

    def test_cors_preflight_rejects_unlisted(self):
        r = self.client.options("/messages", headers={
            "Origin": BAD_ORIGIN,
            "Access-Control-Request-Method": "POST"})
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), BAD_ORIGIN)


if __name__ == "__main__":
    unittest.main()
