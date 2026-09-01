"""Regression guard for the PARALLEL session scanner (dashboard/sessions.py).

The scanner fans remote SSH probes out concurrently (`_probe_hosts`) instead of
looping hosts serially. That's a performance-critical, easy-to-silently-break
property: a stray `for host in hosts: ...` refactor would still pass every
correctness check while quietly going back to sum-of-hosts latency. These tests
pin BOTH the correctness (flatten / order / per-host failure isolation / online
filtering) AND concurrency (overlapping probe starts) with no real network.

Stdlib `unittest` (no pytest dependency), but written as TestCase classes so
pytest discovers/runs them too. Run any of:
    python -m unittest tests.test_sessions_parallel      # from orchestrator/
    python -m pytest tests/test_sessions_parallel.py     # if pytest installed
    python tests/test_sessions_parallel.py               # standalone
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# Make `dashboard` importable however this file is invoked (orchestrator/ on path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import sessions  # noqa: E402

# Concurrency budget: a per-host "probe" sleeps this long. Serial N hosts would
# take N*DELAY; parallel takes ~DELAY. Thresholds below are deliberately loose
# so a slow/loaded CI box still distinguishes parallel from serial.
DELAY = 0.25


class ProbeHostsCorrectness(unittest.TestCase):
    """`_probe_hosts(fn, hosts)` — aggregation semantics."""

    def test_zero_hosts_returns_empty(self):
        self.assertEqual(sessions._probe_hosts(lambda h: [h], []), [])

    def test_single_host_runs_inline_and_flattens(self):
        # 0/1 host skips the thread pool (no overhead) but must still flatten.
        self.assertEqual(sessions._probe_hosts(lambda h: [h, h], ["a"]), ["a", "a"])

    def test_flattens_and_preserves_host_order(self):
        # ex.map preserves input order — the board relies on stable ordering.
        out = sessions._probe_hosts(lambda h: [f"{h}-1", f"{h}-2"], ["a", "b", "c"])
        self.assertEqual(out, ["a-1", "a-2", "b-1", "b-2", "c-1", "c-2"])

    def test_none_result_is_treated_as_empty(self):
        self.assertEqual(sessions._probe_hosts(lambda h: None, ["a", "b"]), [])

    def test_failing_host_is_isolated_not_fatal(self):
        # One host's SSH dying must NOT abort the whole scan — the classic
        # "a flaky Mac takes down the sessions view" regression.
        def probe(h):
            if h == "bad":
                raise RuntimeError("tailscale ssh exploded")
            return [h]

        self.assertEqual(
            sessions._probe_hosts(probe, ["good1", "bad", "good2"]),
            ["good1", "good2"],
        )

    def test_single_failing_host_returns_empty(self):
        def boom(h):
            raise RuntimeError("dead")

        self.assertEqual(sessions._probe_hosts(boom, ["only"]), [])


class ProbeHostsConcurrency(unittest.TestCase):
    """`_probe_hosts` must run hosts CONCURRENTLY (the parallelization itself)."""

    def test_multiple_hosts_run_in_parallel(self):
        hosts = [f"h{i}" for i in range(4)]

        def slow_probe(h):
            time.sleep(DELAY)
            return [h]

        start = time.perf_counter()
        out = sessions._probe_hosts(slow_probe, hosts)
        elapsed = time.perf_counter() - start

        self.assertEqual(sorted(out), sorted(hosts))  # every host aggregated
        serial = DELAY * len(hosts)                   # == 1.0s if it regressed to a loop
        self.assertLess(
            elapsed, serial * 0.6,
            f"scan took {elapsed:.2f}s for {len(hosts)} hosts — expected ~{DELAY:.2f}s "
            f"(parallel), not ~{serial:.2f}s (serial). Parallelization regressed.",
        )

    def test_many_hosts_all_aggregate_despite_worker_cap(self):
        # >8 hosts exceed the max_workers cap → they batch, but ALL must return.
        hosts = [f"h{i}" for i in range(20)]
        out = sessions._probe_hosts(lambda h: [h], hosts)
        self.assertEqual(sorted(out), sorted(hosts))


class OnlineRemoteHosts(unittest.TestCase):
    """`_online_remote_hosts` — one tailscale status call, offline filtered out."""

    _HOSTS = [{"name": "Mac", "host": "remote-mac", "user": "root"}]

    def _status(self, line):
        return mock.patch.object(sessions, "_run_local", return_value=line)

    def test_online_host_included(self):
        line = "100.64.0.9   remote-mac   operator@   macOS   active; direct"
        with mock.patch.object(sessions, "REMOTE_HOSTS", self._HOSTS), self._status(line):
            self.assertEqual(
                [h["host"] for h in sessions._online_remote_hosts()],
                ["remote-mac"],
            )

    def test_offline_host_excluded(self):
        line = "100.64.0.9   remote-mac   operator@   macOS   offline"
        with mock.patch.object(sessions, "REMOTE_HOSTS", self._HOSTS), self._status(line):
            self.assertEqual(sessions._online_remote_hosts(), [])

    def test_host_absent_from_status_excluded(self):
        with mock.patch.object(sessions, "REMOTE_HOSTS", self._HOSTS), \
                self._status("100.1.2.3   some-other-box   active"):
            self.assertEqual(sessions._online_remote_hosts(), [])

    def test_tailscale_timeout_returns_empty(self):
        with mock.patch.object(sessions, "REMOTE_HOSTS", self._HOSTS), \
                self._status("__TIMEOUT__"):
            self.assertEqual(sessions._online_remote_hosts(), [])

    def test_calls_tailscale_status_exactly_once(self):
        # The whole point of the helper: don't re-run `tailscale status` per host.
        with mock.patch.object(sessions, "REMOTE_HOSTS", self._HOSTS), \
                mock.patch.object(sessions, "_run_local", return_value="") as m:
            sessions._online_remote_hosts()
            self.assertEqual(m.call_count, 1)


class ScannerIntegration(unittest.TestCase):
    """End-to-end: the real scanner aggregates local + N mock remote hosts,
    concurrently, with the SSH layer mocked out (no network)."""

    def test_get_claude_code_sessions_aggregates_hosts_concurrently(self):
        mock_hosts = [{"host": "mac"}, {"host": "vps"}]
        both_started = threading.Event()
        start_lock = threading.Lock()
        started = 0

        def remote_probe(host):
            nonlocal started
            with start_lock:
                started += 1
                if started == len(mock_hosts):
                    both_started.set()
            # A serial implementation cannot start the second probe while the
            # first is waiting. This proves overlap without charging slow hosts
            # for thread-pool startup time.
            if not both_started.wait(timeout=1):
                raise AssertionError("remote probes did not overlap")
            return [{"session_id": host["host"], "agent": "claude-code"}]

        with mock.patch.object(sessions, "_get_claude_projects_local",
                               return_value=[{"session_id": "local"}]), \
             mock.patch.object(sessions, "_online_remote_hosts",
                               return_value=mock_hosts), \
             mock.patch.object(sessions, "_get_claude_projects_remote",
                               side_effect=remote_probe):
            out = sessions.get_claude_code_sessions()

        ids = {s["session_id"] for s in out}
        self.assertEqual(ids, {"local", "mac", "vps"})     # local + both hosts merged

    def test_scanner_survives_a_dead_host(self):
        # A dead remote host must not sink the scan: local + the good host survive.
        def remote_probe(host):
            if host["host"] == "dead":
                raise RuntimeError("ssh: connect to host dead port 22: timed out")
            return [{"session_id": host["host"]}]

        with mock.patch.object(sessions, "_get_claude_projects_local",
                               return_value=[{"session_id": "local"}]), \
             mock.patch.object(sessions, "_online_remote_hosts",
                               return_value=[{"host": "dead"}, {"host": "alive"}]), \
             mock.patch.object(sessions, "_get_claude_projects_remote",
                               side_effect=remote_probe):
            out = sessions.get_claude_code_sessions()

        self.assertEqual({s["session_id"] for s in out}, {"local", "alive"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
