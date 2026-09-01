"""Focused contract for the bounded Codex Sol planner wrapper."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "plugins/claudemaxxing/skills/solplan/scripts/run_solplan.py"
SPEC = importlib.util.spec_from_file_location("run_solplan", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

VALID_PLAN = """\
SUMMARY
One bounded plan.
STEPS
1. Read the target.
CONTRACT
The check passes.
EXECUTION SHAPE
ROOT-DIRECT
RISKS / ASSUMPTIONS
None.
OUT OF SCOPE
Implementation.
"""


class SolplanRunnerTest(unittest.TestCase):
    def make_fake(self, root, name, body):
        fake = root / name
        fake.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
        fake.chmod(0o755)
        return fake

    def run_cli(self, root, fake, timeout=4):
        runner = subprocess.Popen(
            [sys.executable, str(RUNNER), "--workdir", str(root),
             "--heartbeat-seconds", "300", "--codex-bin", str(fake)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            return runner.communicate("plan this", timeout=timeout) + (runner.returncode,)
        except subprocess.TimeoutExpired:
            os.kill(runner.pid, signal.SIGTERM)
            runner.communicate(timeout=7)
            self.fail("Solplan CLI deadlocked while draining child pipes")

    def assert_pid_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def assert_pid_not_running(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                return
            time.sleep(0.02)
        self.fail(f"process {pid} is still running")

    def test_command_is_ultra_bounded_multi_agent_read_only_and_ignores_user_config(self):
        cmd = MODULE.command(codex="codex", output=Path("/tmp/plan"),
                             workdir=Path("/tmp"), brief="brief")
        joined = " ".join(cmd)
        self.assertIn('model_reasoning_effort="ultra"', cmd)
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--enable multi_agent", joined)
        self.assertIn("agents.max_threads=4", cmd)
        self.assertIn("agents.max_depth=1", cmd)
        self.assertIn("--sandbox read-only", joined)
        self.assertIn("--json", cmd)
        self.assertNotIn("dangerously-bypass-hook-trust", joined)

    def test_stream_and_cleanup_limits_are_exact(self):
        self.assertEqual(MODULE.READ_CHUNK_BYTES, 65_536)
        self.assertEqual(MODULE.MAX_EVENT_BYTES, 1_048_576)
        self.assertEqual(MODULE.MAX_PLAN_BYTES, 262_144)
        self.assertEqual(MODULE.CLEANUP_GRACE_SECONDS, 5.0)

    def test_plan_contract_is_exact_and_bounded(self):
        self.assertEqual(MODULE.validate_plan(VALID_PLAN), VALID_PLAN.strip())
        self.assertIn("Root‑direct", MODULE.validate_plan(
            VALID_PLAN.replace("ROOT-DIRECT", "Root‑direct")))
        with self.assertRaisesRegex(ValueError, "expected headings"):
            MODULE.validate_plan(VALID_PLAN.replace("CONTRACT\n", ""))
        with self.assertRaisesRegex(ValueError, "EXECUTION SHAPE"):
            MODULE.validate_plan(VALID_PLAN.replace("ROOT-DIRECT", "undecided"))
        with self.assertRaisesRegex(ValueError, "1,200-word"):
            MODULE.validate_plan(VALID_PLAN + " word" * 1_200)
        base_words = len(VALID_PLAN.split())
        exact_limit = VALID_PLAN + " word" * (1_200 - base_words)
        self.assertEqual(len(exact_limit.split()), 1_200)
        MODULE.validate_plan(exact_limit)
        with self.assertRaisesRegex(ValueError, "1,200-word"):
            MODULE.validate_plan(exact_limit + " word")
        false_shape = VALID_PLAN.replace(
            "The check passes.", "ROOT-DIRECT appears outside the execution shape.",
        ).replace("ROOT-DIRECT\nRISKS", "undecided\nRISKS")
        with self.assertRaisesRegex(ValueError, "EXECUTION SHAPE"):
            MODULE.validate_plan(false_shape)

    def test_error_lifecycle_events_are_content_free(self):
        self.assertEqual(MODULE.event_progress({"type": "turn.failed"}),
                         "planning turn failed")
        self.assertEqual(MODULE.event_progress({"type": "error", "message": "secret"}),
                         "planner reported an error")
        self.assertEqual(MODULE.event_progress({
            "type": "item.started", "item": {"type": "secret_custom_type"},
        }), "work item started")
        self.assertEqual(MODULE.event_progress({
            "type": "item.completed", "item": {"type": "collab_tool_call"},
        }), "collaboration call completed")

    def test_jsonl_framer_discards_oversized_records_with_bounded_state(self):
        framer = MODULE.JsonlFramer(max_record_bytes=32)
        self.assertEqual(framer.feed(b"x" * 100), [])
        self.assertLessEqual(len(framer.buffer), 32)
        self.assertTrue(framer.discarding)
        record = b'{"type":"turn.started"}'
        self.assertEqual(framer.feed(b"\n" + record + b"\n"), [record])
        self.assertEqual(framer.oversized, 1)
        self.assertEqual(framer.feed(record), [])
        self.assertEqual(framer.finish(), [record])
        self.assertEqual(framer.finish(), [])

        fragmented = MODULE.JsonlFramer(max_record_bytes=32)
        fragmented.feed(b"a" * 20)
        fragmented.feed(b"b" * 20)
        self.assertTrue(fragmented.discarding)
        self.assertEqual(fragmented.buffer, b"")
        self.assertEqual(fragmented.oversized, 1)

        exact = MODULE.JsonlFramer(max_record_bytes=len(record))
        self.assertEqual(exact.feed(record + b"\n"), [record])

    def test_cleanup_signal_guard_ignores_then_restores_signals(self):
        previous = {signum: signal.getsignal(signum)
                    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with MODULE._cleanup_signal_guard():
            for signum in previous:
                self.assertEqual(signal.getsignal(signum), signal.SIG_IGN)
        for signum, handler in previous.items():
            self.assertEqual(signal.getsignal(signum), handler)

    def test_runner_closes_child_stdin_streams_sanitized_progress_and_returns_only_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake-codex"
            fake.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import json, pathlib, sys
                assert sys.stdin.read() == ""
                import os
                assert os.environ["SOLPLAN_CHILD"] == "1"
                args = sys.argv[1:]
                assert "--json" in args
                assert pathlib.Path(args[args.index("--cd") + 1]).is_absolute()
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                out.write_text({VALID_PLAN!r})
                print(json.dumps({{"type": "thread.started", "thread_id": "secret-thread"}}), flush=True)
                print(json.dumps({{"type": "turn.started"}}), flush=True)
                print(json.dumps({{
                    "type": "item.started",
                    "item": {{"id": "secret-item", "type": "command_execution",
                              "command": "cat /secret/path", "status": "in_progress"}},
                }}), flush=True)
                print(json.dumps({{
                    "type": "item.completed",
                    "item": {{"id": "secret-message", "type": "agent_message",
                              "text": "hidden reasoning and draft content"}},
                }}), flush=True)
                print(json.dumps({{"type": "turn.completed", "usage": {{"output_tokens": 10}}}}), flush=True)
            """))
            fake.chmod(0o755)
            updates = []
            result = MODULE.run(brief="plan this", workdir=root, codex=str(fake),
                                progress=updates.append, heartbeat_seconds=1)
            self.assertEqual(result, VALID_PLAN.strip())
            progress = "\n".join(updates)
            self.assertIn("planner thread started", progress)
            self.assertIn("Ultra planning turn started", progress)
            self.assertIn("read-only command started", progress)
            self.assertIn("planner update completed", progress)
            self.assertIn("planning turn completed", progress)
            self.assertNotIn("secret-thread", progress)
            self.assertNotIn("/secret/path", progress)
            self.assertNotIn("hidden reasoning", progress)

    def test_silent_long_run_has_liveness_heartbeats_and_no_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "slow-codex"
            fake.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import json, pathlib, sys, time
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                print(json.dumps({{"type": "turn.started"}}), flush=True)
                time.sleep(0.15)
                out.write_text({VALID_PLAN!r})
                print(json.dumps({{"type": "turn.completed"}}), flush=True)
            """))
            fake.chmod(0o755)
            updates = []
            started = time.monotonic()
            result = MODULE.run(brief="plan this", workdir=root, codex=str(fake),
                                progress=updates.append, heartbeat_seconds=0.04)
            self.assertEqual(result, VALID_PLAN.strip())
            self.assertGreaterEqual(time.monotonic() - started, 0.12)
            self.assertTrue(any("still working" in update for update in updates), updates)
            self.assertTrue(any("last: Ultra planning turn started" in update
                                for update in updates), updates)

    def test_interruption_terminates_the_planner_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pid"
            fake = root / "interruptible-codex"
            fake.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import json, os, pathlib, time
                pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
                print(json.dumps({{"type": "thread.started"}}), flush=True)
                time.sleep(60)
            """))
            fake.chmod(0o755)

            def interrupt_on_event(message):
                if "planner thread started" in message:
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                MODULE.run(brief="plan this", workdir=root, codex=str(fake),
                           progress=interrupt_on_event, heartbeat_seconds=1)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_sigterm_exits_conventionally_and_reaps_the_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pid"
            fake = root / "signal-codex"
            fake.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import json, os, pathlib, time
                pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
                print(json.dumps({{"type": "thread.started"}}), flush=True)
                time.sleep(60)
            """))
            fake.chmod(0o755)
            runner = subprocess.Popen(
                [sys.executable, str(RUNNER), "--workdir", str(root),
                 "--heartbeat-seconds", "300", "--codex-bin", str(fake)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert runner.stdin is not None
            runner.stdin.write("plan this")
            runner.stdin.close()
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists(), "planner child did not start")
            child_pid = int(pid_file.read_text())
            os.kill(runner.pid, signal.SIGTERM)
            self.assertEqual(runner.wait(timeout=7), 128 + signal.SIGTERM)
            assert runner.stdout is not None and runner.stderr is not None
            runner.stdout.close()
            runner.stderr.close()
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_cli_success_keeps_stdout_final_and_progress_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "stream-codex"
            fake.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import json, pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                out.write_text({VALID_PLAN!r})
                print(json.dumps({{
                    "type": "item.started",
                    "item": {{"type": "command_execution", "command": "read /secret"}},
                }}), flush=True)
                print(json.dumps({{
                    "type": "item.completed",
                    "item": {{"type": "agent_message", "text": "private draft"}},
                }}), flush=True)
                print(json.dumps({{"type": "turn.completed"}}), flush=True)
            """))
            fake.chmod(0o755)
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--workdir", str(root),
                 "--heartbeat-seconds", "300", "--codex-bin", str(fake)],
                input="plan this",
                capture_output=True,
                text=True,
                timeout=7,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, VALID_PLAN.strip() + "\n")
            self.assertIn("read-only command started", result.stderr)
            self.assertIn("planner update completed", result.stderr)
            self.assertNotIn("/secret", result.stderr)
            self.assertNotIn("private draft", result.stderr)

    def test_cli_drains_partial_stdout_while_oversized_stderr_is_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "partial-codex", f"""
                import os, pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                os.write(1, b'{{"type":"turn.started"')
                os.write(2, b'x' * (2 * 1024 * 1024))
                os.write(1, b'}}\\n')
                out.write_text({VALID_PLAN!r})
                os.write(1, b'{{"type":"turn.completed"}}\\n')
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stdout, VALID_PLAN.strip() + "\n")
            self.assertNotIn("x", stderr)

    def test_child_content_never_reaches_failure_diagnostics(self):
        secret = "CHILD_SECRET_7f19"  # gitleaks:allow
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failing = self.make_fake(root, "failing-codex", f"""
                import os
                os.write(2, b'\\x1b[31m{secret}\\x1b[0m\\n')
                raise SystemExit(2)
            """)
            stdout, stderr, status = self.run_cli(root, failing)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertNotIn(secret, stderr)

    def test_failure_reports_only_content_free_stream_counts(self):
        secret = "SUPPRESSED_STDERR_a132"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "counting-failure-codex", f"""
                import os
                os.write(1, b'not-json\\n')
                os.write(1, b'x' * ({MODULE.MAX_EVENT_BYTES} + 1) + b'\\n')
                os.write(2, {secret.encode()!r})
                raise SystemExit(2)
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertIn(f"child stderr suppressed: {len(secret.encode())} bytes", stderr)
            self.assertIn("malformed events: 1", stderr)
            self.assertIn("oversized events: 1", stderr)
            self.assertNotIn(secret, stderr)
            self.assertNotIn("\x1b", stderr)

            invalid = self.make_fake(root, "invalid-codex", f"""
                import json, pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                out.write_text('not a plan {secret}')
                print(json.dumps({{"type": "item.started", "item": {{"type": "{secret}"}}}}))
            """)
            stdout, stderr, status = self.run_cli(root, invalid)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertNotIn(secret, stderr)

    def test_initial_progress_interruption_reaps_child_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pid"
            fake = self.make_fake(root, "initial-interrupt-codex", f"""
                import os, pathlib, time
                pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
                time.sleep(60)
            """)

            def interrupt_initial(_message):
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise KeyboardInterrupt

            pid = None
            try:
                with self.assertRaises(KeyboardInterrupt):
                    MODULE.run(brief="plan this", workdir=root, codex=str(fake),
                               progress=interrupt_initial, heartbeat_seconds=1)
                pid = int(pid_file.read_text())
                self.assert_pid_gone(pid)
            finally:
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_interruption_kills_sigterm_ignoring_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leader_file = root / "leader.pid"
            child_file = root / "child.pid"
            grandchild_code = (
                "import os,pathlib,signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"pathlib.Path({str(child_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            fake = self.make_fake(root, "descendant-codex", f"""
                import json, os, pathlib, subprocess, sys, time
                pathlib.Path({str(leader_file)!r}).write_text(str(os.getpid()))
                subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])
                deadline = time.monotonic() + 2
                while not pathlib.Path({str(child_file)!r}).exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                print(json.dumps({{'type': 'thread.started'}}), flush=True)
                time.sleep(60)
            """)

            def interrupt_on_event(message):
                if "planner thread started" in message:
                    raise KeyboardInterrupt

            pids = []
            original_grace = MODULE.CLEANUP_GRACE_SECONDS
            MODULE.CLEANUP_GRACE_SECONDS = 0.1
            try:
                started = time.monotonic()
                with self.assertRaises(KeyboardInterrupt):
                    MODULE.run(brief="plan this", workdir=root, codex=str(fake),
                               progress=interrupt_on_event, heartbeat_seconds=1)
                self.assertGreaterEqual(time.monotonic() - started, 0.08)
                pids = [int(leader_file.read_text()), int(child_file.read_text())]
                self.assert_pid_gone(pids[0])
                self.assert_pid_not_running(pids[1])
            finally:
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                MODULE.CLEANUP_GRACE_SECONDS = original_grace

    def test_success_does_not_wait_on_inherited_pipe_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_file = root / "inherited.pid"
            child_code = (
                "import os,pathlib,time; "
                f"pathlib.Path({str(child_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            fake = self.make_fake(root, "inherited-pipe-codex", f"""
                import pathlib, subprocess, sys, time
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                subprocess.Popen([sys.executable, '-c', {child_code!r}])
                deadline = time.monotonic() + 2
                while not pathlib.Path({str(child_file)!r}).exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                out.write_text({VALID_PLAN!r})
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stdout, VALID_PLAN.strip() + "\n")
            self.assert_pid_not_running(int(child_file.read_text()))

    def test_oversized_final_plan_is_rejected_without_echoing_content(self):
        secret = "OVERSIZED_SECRET_d41a"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "oversized-plan-codex", f"""
                import pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                out.write_text({secret!r} + 'x' * (300 * 1024))
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertIn("final response exceeds", stderr)
            self.assertNotIn(secret, stderr)

    def test_final_plan_at_exact_byte_limit_is_allowed(self):
        padding = MODULE.MAX_PLAN_BYTES - len(VALID_PLAN.encode())
        self.assertGreater(padding, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "exact-plan-codex", f"""
                import pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                out.write_bytes({VALID_PLAN.encode()!r} + b' ' * {padding})
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stdout, VALID_PLAN.strip() + "\n")

    def test_trailing_partial_json_event_is_processed_after_leader_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "trailing-event-codex", f"""
                import os, pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                os.write(1, b'{{"type":"turn.completed"}}')
                out.write_text({VALID_PLAN!r})
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stdout, VALID_PLAN.strip() + "\n")
            self.assertIn("planning turn completed", stderr)

    def test_final_plan_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target-plan"
            target.write_text(VALID_PLAN)
            fake = self.make_fake(root, "symlink-plan-codex", f"""
                import os, pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                os.symlink({str(target)!r}, out)
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertIn("not a regular file", stderr)

    def test_final_plan_invalid_utf8_is_rejected_without_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "invalid-utf8-codex", """
                import os, pathlib, sys
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                out.write_bytes(b'\\xffPRIVATE')
            """)
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertIn("not valid UTF-8", stderr)
            self.assertNotIn("PRIVATE", stderr)

    def test_final_plan_is_read_only_after_leader_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = self.make_fake(root, "late-plan-codex", f"""
                import os, pathlib, sys, time
                args = sys.argv[1:]
                out = pathlib.Path(args[args.index("--output-last-message") + 1])
                os.close(1)
                os.close(2)
                time.sleep(0.15)
                out.write_text({VALID_PLAN!r})
            """)
            started = time.monotonic()
            stdout, stderr, status = self.run_cli(root, fake)
            self.assertEqual(status, 0, stderr)
            self.assertGreaterEqual(time.monotonic() - started, 0.12)
            self.assertEqual(stdout, VALID_PLAN.strip() + "\n")


if __name__ == "__main__":
    unittest.main()
