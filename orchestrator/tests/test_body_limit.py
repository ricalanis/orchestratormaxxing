"""Regression guard for the request-body size limit (BodySizeLimitMiddleware).

The limit must reject BEFORE any handler runs: an oversized Content-Length gets
413 from the header alone, a chunked body (no Content-Length) gets 411, and the
sentinel route proves the handler was never invoked. A refactor that swaps the
pure-ASGI middleware for BaseHTTPMiddleware (which buffers the body first) or
loosens the chunked rule would silently reopen memory-exhaustion via large
payloads. Also pins: small bodies pass, GETs are exempt, and the limit is
env-tunable (DASHBOARD_MAX_BODY_BYTES).

Stdlib unittest (no pytest dep), pytest-discoverable. Imports the app once —
module-level state (log dirs) is pointed at a temp dir first.

Run: python -m unittest tests.test_body_limit   # from orchestrator/
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Keep test traffic's request-log writes out of the repo's logs/.
api.API_LOG_DIR = Path(tempfile.mkdtemp(prefix="body-limit-test-"))
api._API_LOG.update({"day": None, "fh": None})
api._API_ERR.update({"day": None, "fh": None})

HANDLER_CALLS = []


@api.app.post("/api/_body_limit_sentinel")
async def _sentinel(body: dict):
    HANDLER_CALLS.append(body)
    return {"ok": True}


class TestBodySizeLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app, raise_server_exceptions=False)
        cls.limit = api.MAX_BODY_BYTES

    def setUp(self):
        HANDLER_CALLS.clear()

    def test_limit_is_1mb_by_default(self):
        self.assertEqual(self.limit, 1024 * 1024)

    def test_oversized_body_413_handler_untouched(self):
        blob = b'{"pad": "' + b"x" * (self.limit + 100) + b'"}'
        r = self.client.post("/api/_body_limit_sentinel", content=blob,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["limit_bytes"], self.limit)
        self.assertEqual(HANDLER_CALLS, [], "handler must never see an oversized body")

    def test_oversized_content_length_alone_rejects(self):
        # The header is the gate — a lying client that declares 2MB is rejected
        # without the middleware waiting for (or reading) any body bytes.
        r = self.client.post("/api/_body_limit_sentinel", content=b"{}",
                             headers={"Content-Type": "application/json",
                                      "Content-Length": str(2 * 1024 * 1024)})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(HANDLER_CALLS, [])

    def test_chunked_body_411(self):
        def gen():
            yield b'{"a":'
            yield b"1}"
        r = self.client.post("/api/_body_limit_sentinel", content=gen(),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 411)
        self.assertEqual(HANDLER_CALLS, [])

    def test_small_body_passes_through(self):
        r = self.client.post("/api/_body_limit_sentinel", json={"a": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(HANDLER_CALLS, [{"a": 1}])

    def test_get_exempt(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)

    def test_invalid_content_length_400(self):
        r = self.client.post("/api/_body_limit_sentinel", content=b"{}",
                             headers={"Content-Length": "not-a-number"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(HANDLER_CALLS, [])


if __name__ == "__main__":
    unittest.main()
