"""Integration test: the SSE security gates hold in the CONTAINER, not just
in-process. Boots the Coolify image via docker compose, waits for the
mcp-sse healthcheck, and runs the same security contract as
test_sse_security.py — but over real HTTP against http://localhost:5556, so
the Dockerfile, the env plumbing, and the WSGI stack are all exercised end
to end.

OPT-IN: skipped unless RUN_CONTAINER_TESTS=1 (or docker is unavailable), so
the fast `unittest discover` after every change doesn't build+boot Docker.
Run it deliberately:

    RUN_CONTAINER_TESTS=1 python -m unittest tests.test_container_security

Lifecycle: build image → seed a temp DB dir → compose up → poll /health →
assert → compose down -v (always, via addCleanup). Uses a fixed project name
so a crashed prior run is torn down cleanly on the next.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "tests" / "docker-compose.test.yml"
PROJECT = "hermes-citest"
IMAGE = "hermes-orchestrator:citest"
BASE = "http://localhost:5556"

# Contract mirrors test_sse_security.py (kept in sync by intent). The token is
# what we start the container with; the origin allowlist is set in the test
# compose to include http://localhost:5556.
TOKEN = "container-test-token-xyz"
ALLOWED_ORIGIN = "http://localhost:5556"
BAD_ORIGIN = "https://evil.example.com"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "compose", "version"],
                       capture_output=True, text=True)
    return r.returncode == 0


def _req(method, path, headers=None, timeout=5):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=(b"{}" if method == "POST" else None),
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()   # HTTPMessage: case-insensitive .get()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@unittest.skipUnless(
    os.environ.get("RUN_CONTAINER_TESTS") == "1" and _docker_ok(),
    "container test — set RUN_CONTAINER_TESTS=1 with docker available")
class TestContainerSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build the image the compose references.
        subprocess.run(["docker", "build", "-t", IMAGE, "."],
                       cwd=ROOT, check=True)
        # Temp data dir → /data; seed with a real DB if present so the
        # dashboard also comes up healthy (mcp-sse doesn't need rows).
        cls.data_dir = Path(tempfile.mkdtemp(prefix="citest-data-"))
        seed = Path.home() / ".hermes" / "kanban.db"
        if seed.exists():
            shutil.copy(seed, cls.data_dir / "orchestrator.db")
        cls.env = {**os.environ, "IMAGE": IMAGE,
                   "TEST_DATA_DIR": str(cls.data_dir),
                   "HERMES_MCP_SSE_TOKEN": TOKEN}
        cls._compose("down", "-v")  # clean any crashed prior run
        cls.addClassCleanup(cls._teardown)
        up = cls._compose("up", "-d")
        if up.returncode != 0:
            raise RuntimeError(f"docker compose up failed:\n{up.stderr}")
        cls._wait_healthy()

    @classmethod
    def _compose(cls, *args):
        return subprocess.run(
            ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args],
            cwd=ROOT, env=cls.env, capture_output=True, text=True)

    @classmethod
    def _teardown(cls):
        cls._compose("down", "-v")
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @classmethod
    def _wait_healthy(cls, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=4) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(3)
        logs = cls._compose("logs", "--tail", "40").stdout
        raise RuntimeError(f"mcp-sse container never became healthy:\n{logs}")

    # --- P0-1: Bearer auth ---
    def test_no_token_401(self):
        status, headers, _ = _req("POST", "/messages?session_id=x")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")

    def test_wrong_token_401(self):
        status, _, _ = _req("POST", "/messages?session_id=x",
                            {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_correct_token_passes_auth(self):
        # 404 = past auth (unknown session id is the next gate).
        status, _, _ = _req("POST", "/messages?session_id=x",
                            {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 404)

    def test_sse_requires_auth(self):
        status, _, _ = _req("GET", "/sse", timeout=3)
        self.assertEqual(status, 401)

    # --- P0-3: Origin validation ---
    def test_disallowed_origin_403(self):
        status, _, _ = _req("POST", "/messages?session_id=x",
                            {"Authorization": f"Bearer {TOKEN}", "Origin": BAD_ORIGIN})
        self.assertEqual(status, 403)

    def test_allowed_origin_passes(self):
        status, _, _ = _req("POST", "/messages?session_id=x",
                            {"Authorization": f"Bearer {TOKEN}", "Origin": ALLOWED_ORIGIN})
        self.assertEqual(status, 404)

    def test_absent_origin_cli_client_passes(self):
        status, _, _ = _req("POST", "/messages?session_id=x",
                            {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 404)

    # --- /health open + reports auth ---
    def test_health_open_reports_bearer(self):
        status, _, body = _req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["auth"], "bearer")

    # --- P0-2: CORS preflight reflects the allowlist ---
    def test_cors_preflight_allowlisted(self):
        status, headers, _ = _req("OPTIONS", "/messages", {
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, Authorization"})
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)

    def test_cors_preflight_rejects_unlisted(self):
        _, headers, _ = _req("OPTIONS", "/messages", {
            "Origin": BAD_ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), BAD_ORIGIN)


if __name__ == "__main__":
    unittest.main()
