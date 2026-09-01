#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import contextlib
import io
import json
import os
import pathlib
import runpy
import signal
import sys
import tempfile
import time

root = pathlib.Path(sys.argv[1])
module = runpy.run_path(str(root / "bin/harness-verify"))
run_contract = module.get("run_behavioral_contract")
assert callable(run_contract), "harness-observation: contract runner missing"
main = module.get("main")
assert callable(main), "harness-observation: verifier main missing"
fixture = root / "tests/harness-observation/fixture.py"


def child_is_gone(pid):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


with tempfile.TemporaryDirectory(prefix="harness-observation-test-") as td:
    base = pathlib.Path(td)
    scratch = base / "scratch"
    scratch.mkdir()

    def exercise(mode):
        state = base / mode
        state.mkdir()
        env = {
            "OBSERVATION_FIXTURE_MODE": mode,
            "OBSERVATION_FIXTURE_STATE": str(state),
        }
        started = time.monotonic()
        result = run_contract(
            f"fixture-{mode}", [sys.executable, str(fixture)],
            timeout_seconds=0.2, retry_observer=True,
            extra_env=env, scratch_parent=str(scratch),
        )
        assert time.monotonic() - started < 2, f"{mode}: timeout cleanup was not bounded"
        assert not list(scratch.iterdir()), f"{mode}: runner leaked scratch state"
        return result, state

    recovered, recovered_state = exercise("timeout-then-pass")
    assert recovered["outcome"] == "recovered", recovered
    assert recovered["key"] == "contract:fixture-timeout-then-pass:recovered"
    assert [a["outcome"] for a in recovered["attempts"]] == ["timeout", "pass"]
    assert recovered["attempts"][0]["last_stage"] == "waiting-child"
    assert recovered["attempts"][0]["termination"] == "kill", recovered
    assert recovered["attempts"][0]["returncode"] is not None

    inconclusive, timeout_state = exercise("timeout")
    assert inconclusive["outcome"] == "inconclusive", inconclusive
    assert inconclusive["key"] == "contract:fixture-timeout:inconclusive"
    assert [a["outcome"] for a in inconclusive["attempts"]] == ["timeout", "timeout"]
    assert all(a["last_stage"] == "waiting-child" for a in inconclusive["attempts"])
    assert all(len(a.get("process_snapshot", [])) >= 1 for a in inconclusive["attempts"])
    assert all(a["termination"] == "kill" for a in inconclusive["attempts"])
    assert all(a["returncode"] is not None for a in inconclusive["attempts"])

    for state in (recovered_state, timeout_state):
        for pid_file in state.glob("child-*.pid"):
            assert child_is_gone(int(pid_file.read_text())), f"child survived timeout: {pid_file}"

    # Negative fixture for the deadline-vs-clean-exit reclassification
    # (lq-258f3a34): a leader that exits 0 while a DESCENDANT keeps the stdout
    # pipe open past the deadline must NOT be reclassified as pass — the
    # process tree never terminated within budget (the drain communicate times
    # out), so blessing it would hide leaked descendants behind a clean leader.
    leaky_state = base / "clean-exit-open-pipe"
    leaky_state.mkdir()
    leaky_started = time.monotonic()
    leaky = run_contract(
        "fixture-clean-exit-open-pipe", [sys.executable, str(fixture)],
        timeout_seconds=1.0, retry_observer=True,
        extra_env={
            "OBSERVATION_FIXTURE_MODE": "clean-exit-open-pipe",
            "OBSERVATION_FIXTURE_STATE": str(leaky_state),
        },
        scratch_parent=str(scratch),
    )
    leaky_elapsed = time.monotonic() - leaky_started
    for pid_file in leaky_state.glob("holder-*.pid"):
        # The runner decided NOT to bless this contract; it must also not have
        # signalled the group it merely probed (killpg with signal 0).
        os.kill(int(pid_file.read_text()), 0)
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    assert leaky["outcome"] == "inconclusive", leaky
    assert [a["outcome"] for a in leaky["attempts"]] == ["timeout", "timeout"], leaky
    assert all(a["returncode"] == 0 for a in leaky["attempts"]), leaky
    assert all(a["stderr_tail"] == "" for a in leaky["attempts"]), leaky
    assert leaky_elapsed < 6, "clean-exit-open-pipe case was not bounded"
    assert not list(scratch.iterdir()), "clean-exit-open-pipe leaked scratch state"

    # A pipe-holding descendant that ESCAPED the process group (new session):
    # invisible to the group probe, so only the drain guard blocks a false
    # pass. Must stay inconclusive.
    escaped_state = base / "clean-exit-escaped-pipe-holder"
    escaped_state.mkdir()
    escaped = run_contract(
        "fixture-clean-exit-escaped-pipe-holder", [sys.executable, str(fixture)],
        timeout_seconds=1.0, retry_observer=True,
        extra_env={
            "OBSERVATION_FIXTURE_MODE": "clean-exit-escaped-pipe-holder",
            "OBSERVATION_FIXTURE_STATE": str(escaped_state),
        },
        scratch_parent=str(scratch),
    )
    for pid_file in escaped_state.glob("escaped-*.pid"):
        os.kill(int(pid_file.read_text()), 0)
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    assert escaped["outcome"] == "inconclusive", escaped
    assert [a["outcome"] for a in escaped["attempts"]] == ["timeout", "timeout"], escaped
    assert all(a["returncode"] == 0 for a in escaped["attempts"]), escaped
    assert not list(scratch.iterdir()), "escaped-pipe-holder leaked scratch state"

    # The REAL race (lq-258f3a34, observed live 2026-08-07): the child exits 0
    # on its own inside the window between communicate()'s deadline and the
    # terminator's poll — no signal ever sent, pipes drained clean. Simulate
    # the load-induced deschedule deterministically: the snapshot step waits
    # (without reaping) until the child has actually exited.
    contract_globals = run_contract.__globals__
    real_snapshot = contract_globals["_process_snapshot"]

    def waiting_snapshot(pid):
        if hasattr(os, "waitid"):
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        else:
            # macOS CPython < 3.13 ships no os.waitid (caught live on the macOS CI
            # runner, Python 3.12). Wait for the child to become a zombie WITHOUT
            # reaping it — the same observable point as WEXITED|WNOWAIT.
            import subprocess as _sp
            import time as _t
            deadline = _t.time() + 30
            while _t.time() < deadline:
                r = _sp.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=5)
                st = r.stdout.strip()
                if not st or st.startswith("Z"):
                    break
                _t.sleep(0.01)
        return real_snapshot(pid)

    raced_state = base / "late-clean-exit"
    raced_state.mkdir()
    contract_globals["_process_snapshot"] = waiting_snapshot
    try:
        raced = run_contract(
            "fixture-late-clean-exit", [sys.executable, str(fixture)],
            timeout_seconds=0.2, retry_observer=True,
            extra_env={
                "OBSERVATION_FIXTURE_MODE": "late-clean-exit",
                "OBSERVATION_FIXTURE_STATE": str(raced_state),
            },
            scratch_parent=str(scratch),
        )
    finally:
        contract_globals["_process_snapshot"] = real_snapshot
    assert raced["outcome"] == "passed", raced
    assert [a["outcome"] for a in raced["attempts"]] == ["pass"], raced
    assert raced["attempts"][0]["returncode"] == 0, raced
    assert raced["attempts"][0]["termination"] == "already-exited", raced
    assert not list(scratch.iterdir()), "late-clean-exit leaked scratch state"

    # Same clean late exit, but a descendant DETACHED from the pipes and stayed
    # in the process group: the drain succeeds, so only the group probe stands
    # between this leak and a false pass. Must stay inconclusive.
    daemon_state = base / "late-clean-exit-detached-daemon"
    daemon_state.mkdir()
    contract_globals["_process_snapshot"] = waiting_snapshot
    try:
        leaked = run_contract(
            "fixture-late-clean-exit-detached-daemon", [sys.executable, str(fixture)],
            timeout_seconds=0.2, retry_observer=True,
            extra_env={
                "OBSERVATION_FIXTURE_MODE": "late-clean-exit-detached-daemon",
                "OBSERVATION_FIXTURE_STATE": str(daemon_state),
            },
            scratch_parent=str(scratch),
        )
    finally:
        contract_globals["_process_snapshot"] = real_snapshot
    for pid_file in daemon_state.glob("daemon-*.pid"):
        os.kill(int(pid_file.read_text()), 0)
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    assert leaked["outcome"] == "inconclusive", leaked
    assert [a["outcome"] for a in leaked["attempts"]] == ["timeout", "timeout"], leaked
    assert all(a["returncode"] == 0 for a in leaked["attempts"]), leaked
    assert all(a["stderr_tail"] == "" for a in leaked["attempts"]), leaked
    assert not list(scratch.iterdir()), "detached-daemon case leaked scratch state"

    failed, _ = exercise("failure")
    assert failed["outcome"] == "failed", failed
    assert len(failed["attempts"]) == 1, "measured failure must not be retried"
    assert failed["attempts"][0]["returncode"] == 7
    assert "deterministic fixture stdout" in failed["attempts"][0]["stdout_tail"]
    assert "deterministic fixture failure" in failed["attempts"][0]["stderr_tail"]


def main_case(issues, results):
    globals_ = main.__globals__
    old_audit = globals_["audit"]
    old_argv = sys.argv
    out = io.StringIO()
    globals_["audit"] = lambda _root: (issues, results)
    sys.argv = ["harness-verify", "--repo", str(root), "--json"]
    try:
        with contextlib.redirect_stdout(out):
            try:
                main()
            except SystemExit as ex:
                rc = ex.code
            else:
                raise AssertionError("harness-observation: verifier main did not exit")
    finally:
        globals_["audit"] = old_audit
        sys.argv = old_argv
    return rc, json.loads(out.getvalue())


rc, report = main_case([], [{"id": "x", "outcome": "recovered", "attempts": [{}, {}]}])
assert rc == 0 and report["inconclusive"] == 0 and report["errors"] == 0
rc, report = main_case([], [{"id": "x", "outcome": "inconclusive", "attempts": [{}, {}]}])
assert rc == 2 and report["inconclusive"] == 1 and report["errors"] == 0
rc, report = main_case([("error", "bin/x", "measured")], [])
assert rc == 1 and report["errors"] == 1 and report["issues"][0]["key"] == "issue:bin/x:measured"

print("harness-observation contract: PASS")
PY
