#!/usr/bin/env python3
"""Deterministic child used by the harness-observation contract."""
import os
import subprocess
import sys
import time


def trace(kind, stage):
    path = os.environ.get("HARNESS_TRACE_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{kind} {stage}\n")


mode = os.environ["OBSERVATION_FIXTURE_MODE"]
state_dir = os.environ["OBSERVATION_FIXTURE_STATE"]
attempt = int(os.environ.get("HARNESS_CONTRACT_ATTEMPT", "0"))

if mode == "failure":
    trace("BEGIN", "measured-failure")
    print("deterministic fixture stdout")
    print("deterministic fixture failure", file=sys.stderr)
    raise SystemExit(7)

if mode == "timeout-then-pass" and attempt == 2:
    trace("BEGIN", "retry-pass")
    trace("END", "retry-pass")
    raise SystemExit(0)

if mode == "late-clean-exit":
    # Outlive the runner's deadline, then exit 0 with nothing left behind —
    # the child's half of the deadline-vs-clean-exit race (lq-258f3a34).
    trace("BEGIN", "late-clean-exit")
    time.sleep(0.5)
    trace("END", "late-clean-exit")
    raise SystemExit(0)

if mode == "late-clean-exit-detached-daemon":
    # Exit 0 past the deadline leaving a descendant that DETACHED from the
    # stdout/stderr pipes but stayed in the process group: the drain succeeds,
    # so only the group probe can catch this leak (lq-258f3a34 refinement).
    trace("BEGIN", "late-clean-exit-detached-daemon")
    daemon = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with open(os.path.join(state_dir, f"daemon-{attempt}.pid"), "w", encoding="utf-8") as fh:
        fh.write(str(daemon.pid))
    time.sleep(0.5)
    trace("END", "late-clean-exit-detached-daemon")
    raise SystemExit(0)

if mode == "clean-exit-escaped-pipe-holder":
    # Exit 0 leaving a pipe-holding descendant that ESCAPED the process group
    # (new session): the group probe cannot see it, so only the drain guard
    # stands between this leak and a false pass (lq-258f3a34 refinement).
    trace("BEGIN", "clean-exit-escaped-pipe-holder")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        start_new_session=True,
    )
    with open(os.path.join(state_dir, f"escaped-{attempt}.pid"), "w", encoding="utf-8") as fh:
        fh.write(str(holder.pid))
    trace("END", "clean-exit-escaped-pipe-holder")
    raise SystemExit(0)

if mode == "clean-exit-open-pipe":
    # Exit 0 while a descendant keeps the stdout/stderr pipes open past the
    # runner's deadline: communicate() must raise TimeoutExpired even though
    # this leader already completed cleanly — the deterministic reproduction
    # of the deadline-vs-clean-exit race (lq-258f3a34).
    trace("BEGIN", "clean-exit-open-pipe")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    with open(os.path.join(state_dir, f"holder-{attempt}.pid"), "w", encoding="utf-8") as fh:
        fh.write(str(holder.pid))
    trace("END", "clean-exit-open-pipe")
    raise SystemExit(0)

trace("BEGIN", "waiting-child")
ready = os.path.join(state_dir, f"child-{attempt}.ready")
child = subprocess.Popen([
    sys.executable, "-c",
    ("import pathlib,signal,time,sys; "
     "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
     "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)"),
    ready,
])
with open(os.path.join(state_dir, f"child-{attempt}.pid"), "w", encoding="utf-8") as fh:
    fh.write(str(child.pid))
deadline = time.monotonic() + 2
while not os.path.exists(ready) and time.monotonic() < deadline:
    time.sleep(0.01)
if not os.path.exists(ready):
    raise RuntimeError("child did not install TERM-resistant fixture")
time.sleep(30)
