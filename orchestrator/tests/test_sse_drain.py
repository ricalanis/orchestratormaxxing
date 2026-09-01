"""Graceful-shutdown drain for the MCP SSE server.

Verifies the SIGTERM path drains in-flight streams instead of dropping them:
the stream generator emits a final `close` event on the _CLOSE sentinel and
unregisters; drain_sse_connections signals every active stream and waits for
them to unwind; and the wait is BOUNDED (a stream that never consumes the
sentinel can't hang the shutdown past the timeout). A regression that drops
the close event or unbounds the wait would break clean container redeploys.

Pure asyncio (IsolatedAsyncioTestCase) — no server, no signals: the generator
and drain fn are module-level and tested directly. Stdlib unittest, no pytest.

Run: python -m unittest tests.test_sse_drain   # from orchestrator/
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_sse_server as sse  # noqa: E402


async def _never_disconnected():
    return False


class TestSSEDrain(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sse._SESSIONS.clear()
        sse._SHUTTING_DOWN = False

    def tearDown(self):
        sse._SESSIONS.clear()
        sse._SHUTTING_DOWN = False

    async def test_generator_emits_close_on_sentinel_and_unregisters(self):
        q = asyncio.Queue()
        sse._SESSIONS["s1"] = q
        q.put_nowait("hello")        # a normal message
        q.put_nowait(sse._CLOSE)     # then the drain sentinel

        frames = []
        async for frame in sse._event_stream("s1", q, _never_disconnected):
            frames.append(frame)

        # endpoint handshake, the message, then a close event — in order.
        self.assertIn("event: endpoint", frames[0])
        self.assertTrue(any("event: message" in f and "hello" in f for f in frames))
        self.assertIn("event: close", frames[-1])
        # generator removed itself from the registry on exit.
        self.assertNotIn("s1", sse._SESSIONS)

    async def test_drain_signals_all_and_waits_for_unwind(self):
        # Two live streams, each consumed by a background task.
        collected = {"a": [], "b": []}

        async def consume(sid):
            q = asyncio.Queue()
            sse._SESSIONS[sid] = q
            async for frame in sse._event_stream(sid, q, _never_disconnected):
                collected[sid].append(frame)

        tasks = [asyncio.create_task(consume("a")), asyncio.create_task(consume("b"))]
        await asyncio.sleep(0.05)  # let both register + start awaiting
        self.assertEqual(len(sse._SESSIONS), 2)

        drained = await sse.drain_sse_connections(timeout=5)
        self.assertEqual(drained, 2)
        self.assertEqual(sse._SESSIONS, {})           # both unwound
        self.assertTrue(sse._SHUTTING_DOWN)
        for t in tasks:
            await asyncio.wait_for(t, timeout=1)      # generators actually finished
        for sid in ("a", "b"):
            self.assertTrue(any("event: close" in f for f in collected[sid]),
                            f"{sid} never received the close event")

    async def test_drain_is_bounded_when_stream_never_consumes(self):
        # A registered session with NO consumer — the sentinel is never read.
        sse._SESSIONS["stuck"] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        drained = await sse.drain_sse_connections(timeout=0.3)
        elapsed = loop.time() - t0
        self.assertEqual(drained, 1)
        self.assertLess(elapsed, 1.0)                 # returned near the timeout, didn't hang
        self.assertIn("stuck", sse._SESSIONS)          # still there — but shutdown proceeds

    async def test_drain_noop_with_zero_sessions(self):
        drained = await sse.drain_sse_connections(timeout=1)
        self.assertEqual(drained, 0)

    async def test_handle_exit_drains_then_flags_exit(self):
        # The uvicorn wiring, in-process (no subprocess/signals): a real
        # DrainingServer, a real consumed stream, drive handle_exit(SIGTERM)
        # and assert it drained the stream AND set should_exit.
        import signal
        import uvicorn
        server = sse.DrainingServer(uvicorn.Config(sse.app))
        collected = []

        async def consume(sid):
            q = asyncio.Queue()
            sse._SESSIONS[sid] = q
            async for frame in sse._event_stream(sid, q, _never_disconnected):
                collected.append(frame)

        t = asyncio.create_task(consume("x"))
        await asyncio.sleep(0.05)
        self.assertEqual(len(sse._SESSIONS), 1)

        server.handle_exit(signal.SIGTERM, None)   # first signal → drain, don't exit yet
        await asyncio.sleep(0.4)                    # let the drain task run

        self.assertEqual(sse._SESSIONS, {})         # stream drained
        self.assertTrue(server.should_exit)         # uvicorn told to stop, AFTER draining
        self.assertTrue(any("event: close" in f for f in collected))
        await asyncio.wait_for(t, timeout=1)

    async def test_new_stream_refused_while_shutting_down(self):
        # Once draining, /sse must refuse new connections (503) — checked at the
        # unit level via the flag the handler reads.
        sse._SHUTTING_DOWN = True
        from fastapi.testclient import TestClient
        import os
        os.environ.pop("HERMES_MCP_SSE_TOKEN", None)  # dev mode so auth doesn't mask the 503
        if sse._TOKEN_FILE.exists():
            self.skipTest("token file present — dev mode not reachable")
        with TestClient(sse.app) as c:
            r = c.get("/sse")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
