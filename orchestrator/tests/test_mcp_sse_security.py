"""Regression guard for the MCP SSE server's P0 security hardening.

Three gates are pinned:

1. BEARER AUTH — when a token is configured (HERMES_MCP_SSE_TOKEN), /sse and
   /messages reject without a matching ``Authorization: Bearer ***\n   (401 + WWW-Authenticate). With no token configured, dev mode passes.

2. CORS LOCKDOWN — the CORS middleware no longer allows ``*``. A preflight
   OPTIONS request from an unlisted Origin gets no ``Access-Control-Allow-
   Origin`` header; a listed Origin does.

3. ORIGIN VALIDATION — a POST /messages with a present-but-unlisted ``Origin``
   header is 403'd even if the Bearer token is correct (defense in depth vs
   CSRF from a rogue browser tab).

Stdlib unittest (no pytest dep), pytest-discoverable. Uses FastAPI TestClient.

Run: python -m unittest tests.test_mcp_sse_security   # from orchestrator/
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import the module so we can patch its _configured_token / ALLOWED_ORIGINS.
import mcp_sse_server as sse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# A fixed token for the auth tests. We patch _configured_token so we don't
# depend on a token file existing on the test machine.
_TEST_TOKEN = "test-secret-token-xyz"


class TestBearerAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(sse.app, raise_server_exceptions=False)

    def _patch_token(self):
        return patch.object(sse, "_configured_token", return_value=_TEST_TOKEN)

    def test_sse_rejects_without_token(self):
        with self._patch_token():
            r = self.client.get("/sse")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    def test_sse_rejects_wrong_token(self):
        with self._patch_token():
            r = self.client.get(
                "/sse", headers={"Authorization": "Bearer wrong-token"}
            )
        self.assertEqual(r.status_code, 401)

    def test_sse_accepts_correct_token(self):
        # /sse returns a StreamingResponse that blocks forever. We patch it
        # to a plain JSONResponse so TestClient doesn't hang on the infinite
        # SSE stream. The point is to confirm auth passed (we reach the
        # StreamingResponse construction, not a 401 JSONResponse).
        from fastapi.responses import JSONResponse as _JR

        def _fake_stream(content, *a, **kw):
            return _JR({"ok": True, "stream": "patched"}, status_code=200)

        with self._patch_token():
            with patch.object(sse, "StreamingResponse", side_effect=_fake_stream):
                r = self.client.get(
                    "/sse",
                    headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
                )
        self.assertEqual(r.status_code, 200)

    def test_messages_rejects_without_token(self):
        with self._patch_token():
            r = self.client.post(
                "/messages?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
        self.assertEqual(r.status_code, 401)

    def test_messages_rejects_wrong_token(self):
        with self._patch_token():
            r = self.client.post(
                "/messages?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Authorization": "Bearer nope"},
            )
        self.assertEqual(r.status_code, 401)

    def test_health_stays_open(self):
        # /health must never require auth — liveness must stay observable.
        with self._patch_token():
            r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["auth"], "bearer")

    def test_dev_mode_no_token_passes(self):
        # When no token is configured, the server is in dev mode — requests
        # must pass through (the warning is on stderr, not a rejection).
        with patch.object(sse, "_configured_token", return_value=""):
            r = self.client.post(
                "/messages?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
        # 404 (unknown session) — NOT 401. Dev mode auth skipped.
        self.assertEqual(r.status_code, 404)


class TestCorsLockdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(sse.app, raise_server_exceptions=False)

    def test_wildcard_origin_is_not_allowed(self):
        # A preflight from an unlisted origin must not get an ACAO header.
        r = self.client.options(
            "/sse",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # 400 from CORSMiddleware (origin not in allowlist) or 200 without
        # the ACAO header — either way, no access granted.
        acao = r.headers.get("access-control-allow-origin")
        self.assertIsNone(
            acao,
            f"wildcard leak: ACAO={acao!r} for an unlisted origin",
        )

    def test_listed_origin_gets_acao(self):
        r = self.client.options(
            "/messages",
            headers={
                "Origin": "http://127.0.0.1:5555",
                "Access-Control-Request-Method": "POST",
            },
        )
        acao = r.headers.get("access-control-allow-origin")
        self.assertEqual(acao, "http://127.0.0.1:5555")

    def test_no_wildcard_in_allowlist(self):
        # The allowlist must never contain "*".
        self.assertNotIn("*", sse.ALLOWED_ORIGINS,
                         "CORS allowlist must be explicit, never '*'")

    def test_env_extends_allowlist(self):
        # HERMES_MCP_CORS_ORIGINS should extend (not replace) the defaults,
        # and the served allowlist is exactly the computed one.
        with patch.dict(
            os.environ,
            {"HERMES_MCP_CORS_ORIGINS": "https://myhost.tailnet-example.ts.net"},
        ):
            origins = sse._compute_allowed_origins()
        self.assertIn("http://127.0.0.1:5555", origins)
        self.assertIn("https://myhost.tailnet-example.ts.net", origins)
        self.assertEqual(sse.ALLOWED_ORIGINS, sse._compute_allowed_origins())


class TestOriginValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(sse.app, raise_server_exceptions=False)

    def test_messages_rejects_unlisted_origin(self):
        # Even with a correct Bearer token, a present-but-unlisted Origin
        # is 403 (defense in depth vs CSRF).
        with patch.object(sse, "_configured_token", return_value=_TEST_TOKEN):
            r = self.client.post(
                "/messages?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={
                    "Authorization": f"Bearer {_TEST_TOKEN}",
                    "Origin": "https://evil.example.com",
                },
            )
        self.assertEqual(r.status_code, 403)

    def test_messages_accepts_listed_origin(self):
        # A listed origin with a correct token reaches the handler (404 for
        # an unknown session — not 401/403).
        with patch.object(sse, "_configured_token", return_value=_TEST_TOKEN):
            r = self.client.post(
                "/messages?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={
                    "Authorization": f"Bearer {_TEST_TOKEN}",
                    "Origin": "http://127.0.0.1:5555",
                },
            )
        self.assertEqual(r.status_code, 404)  # unknown session, not auth/origin

    def test_messages_accepts_absent_origin(self):
        # Non-browser MCP clients (claude mcp add) send no Origin header.
        # Absent Origin = allow (only present-and-unlisted is rejected).
        with patch.object(sse, "_configured_token", return_value=_TEST_TOKEN):
            r = self.client.post(
                "/messages?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            )
        self.assertEqual(r.status_code, 404)  # unknown session, not 403


if __name__ == "__main__":
    unittest.main()

class TestWebhookHasItsOwnBudget(unittest.TestCase):
    """El webhook de WhatsApp no comparte cubeta con el MCP.

    Compartirla acopla dos cosas que no tienen nada que ver: el sync de WhatsApp
    manda en ráfaga cuando vacía historial, agota el presupuesto del MCP, y el
    429 resultante puede callar su webhook durante toda la vida del proceso
    emisor. Se midió exactamente eso el 2026-08-04 — 24 rechazos y después
    silencio — y el síntoma es indistinguible de «nadie te escribió», que es lo
    que lo hace caro: no se ve como una falla, se ve como calma.
    """

    def setUp(self):
        sse._RATE_BUCKETS.clear()
        sse._METRICS["rate_limit_rejections_total"] = 0

    def tearDown(self):
        sse._RATE_BUCKETS.clear()

    def _req(self):
        class _C:
            host = "127.0.0.1"
        class _R:
            client = _C()
            headers = {}
        return _R()

    def test_the_webhook_budget_is_far_larger_than_the_mcp_one(self):
        self.assertGreater(sse._WEBHOOK_RATE_LIMIT, sse._RATE_LIMIT * 10)

    def test_a_burst_that_would_kill_the_mcp_budget_passes_the_webhook(self):
        r = self._req()
        for _ in range(sse._RATE_LIMIT + 50):
            self.assertIsNone(
                sse._rate_limited(r, limit=sse._WEBHOOK_RATE_LIMIT, bucket_key="wa"),
                "una ráfaga de historial no puede tumbar el webhook")

    def test_the_mcp_endpoint_is_still_limited(self):
        """El arreglo no puede ser «quitarle el límite a todo»."""
        r = self._req()
        for _ in range(sse._RATE_LIMIT):
            self.assertIsNone(sse._rate_limited(r))
        self.assertIsNotNone(sse._rate_limited(r), "el MCP sigue acotado")

    def test_spending_the_webhook_budget_leaves_the_mcp_untouched(self):
        r = self._req()
        for _ in range(sse._RATE_LIMIT + 50):
            sse._rate_limited(r, limit=sse._WEBHOOK_RATE_LIMIT, bucket_key="wa")
        self.assertIsNone(sse._rate_limited(r), "las cubetas no se tocan")


class TestAnIgnoredWebhookExplainsItself(unittest.TestCase):
    """Un webhook ignorado en silencio es indiagnosticable: el emisor recibe 200,
    no registra nada, y del otro lado no aparece ningún pulso."""

    def test_the_jid_is_found_in_every_shape_the_sender_might_use(self):
        for cuerpo in ({"chat_jid": "a@g.us"},
                       {"data": {"chat_jid": "a@g.us"}},
                       {"message": {"chatJid": "a@g.us"}},
                       {"key": {"remoteJid": "a@g.us"}},
                       {"data": {"message": {"chat_jid": "a@g.us"}}}):
            self.assertEqual(sse._extraer_jid(cuerpo), "a@g.us", cuerpo)

    def test_something_that_is_not_a_jid_is_not_accepted(self):
        self.assertEqual(sse._extraer_jid({"chat": "hola"}), "")
        self.assertEqual(sse._extraer_jid({"chat_jid": 12345}), "")

    def test_an_unknown_shape_reports_its_keys_and_never_its_values(self):
        cuerpo = {"foo": "SECRETO", "meta": {"bar": "TAMBIEN SECRETO"}}
        self.assertEqual(sse._extraer_jid(cuerpo), "")
        forma = sorted(cuerpo.keys())
        self.assertEqual(forma, ["foo", "meta"])
        self.assertNotIn("SECRETO", " ".join(forma))


class TestTheWebhookRouteActuallyUsesItsOwnBudget(unittest.TestCase):
    """Probar que la FUNCIÓN acepta una cubeta aparte no prueba que la RUTA la
    use — y el cableado es justo donde estuvo el bug. Este contrato entra por la
    puerta de HTTP, como el emisor real."""

    def setUp(self):
        sse._RATE_BUCKETS.clear()
        self.client = TestClient(sse.app, client=("127.0.0.1", 5555))

    def tearDown(self):
        sse._RATE_BUCKETS.clear()

    def test_a_burst_through_the_route_is_not_throttled(self):
        with patch.dict(os.environ, {"WACLI_WEBHOOK_SECRET": ""}), \
             patch("dashboard.whatsapp.record_activity", lambda *a, **k: None):
            for i in range(sse._RATE_LIMIT + 30):
                r = self.client.post(sse.WHATSAPP_WEBHOOK_PATH,
                                     json={"chat_jid": f"x{i}@g.us"})
                self.assertEqual(r.status_code, 200,
                                 f"el webhook se estranguló en la petición {i}")
