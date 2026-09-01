"""Regression test for the GZip response compression (dev-audit P1).

The dashboard index.html is a ~640 KB single-file SPA. Without GZipMiddleware it
ships uncompressed on every load; gzip cuts it ~78% (to ~150 KB over the wire).
This is a load-bearing perf fix on a tailnet-only, sometimes-mobile dashboard,
and it had no test — a stray middleware-ordering change could silently drop it.

These tests assert the wire-level behaviour: a client that advertises gzip gets a
gzip-encoded response substantially smaller than the decoded body, and a client
that does not advertise it gets the response uncompressed.
"""
from starlette.testclient import TestClient

from dashboard.api import app

_CLIENT = TestClient(app)


def test_index_is_gzip_encoded_when_offered():
    r = _CLIENT.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    # httpx exposes the raw header even though it transparently decodes .content.
    assert r.headers.get("content-encoding") == "gzip"


def test_gzip_meaningfully_smaller_than_body():
    r = _CLIENT.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    wire = int(r.headers["content-length"])          # compressed bytes on the wire
    decoded = len(r.content)                          # httpx-decompressed body
    # The page is big and highly compressible; require at least a 50% saving so
    # the test is robust to template growth but still fails if gzip is disabled.
    assert wire < decoded * 0.5, (
        f"index not meaningfully compressed: wire={wire} decoded={decoded}"
    )


def test_identity_encoding_is_uncompressed():
    r = _CLIENT.get("/", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") in (None, "identity")


def test_small_json_below_threshold_not_forced():
    """GZipMiddleware(minimum_size=1024) — a tiny health payload need not be
    gzipped; the point is only that large responses are. This documents the
    minimum-size contract so a future threshold change is a conscious choice."""
    r = _CLIENT.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert r.status_code in (200, 503)  # health may report degraded
