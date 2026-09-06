#!/usr/bin/env python3
"""Run one observable, read-only Codex planner and emit its validated final plan."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


SECTIONS = (
    "SUMMARY",
    "STEPS",
    "CONTRACT",
    "EXECUTION SHAPE",
    "RISKS / ASSUMPTIONS",
    "OUT OF SCOPE",
)
HEADING = re.compile(r"^#{0,3}\s*(SUMMARY|STEPS|CONTRACT|EXECUTION SHAPE|RISKS / ASSUMPTIONS|OUT OF SCOPE)\s*$")
READ_CHUNK_BYTES = 65_536
MAX_EVENT_BYTES = 1_048_576
MAX_PLAN_BYTES = 262_144
CLEANUP_GRACE_SECONDS = 5.0
LEADER_POLL_SECONDS = 0.1
ITEM_LABELS = {
    "agent_message": "planner update",
    "collab_tool_call": "collaboration call",
    "command_execution": "read-only command",
    "file_change": "file change",
    "mcp_tool_call": "tool call",
    "plan_update": "plan update",
    "reasoning": "reasoning step",
    "web_search": "web search",
}


class PlannerInterrupted(Exception):
    """Raised when the runner receives a terminating external signal."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"interrupted by signal {signum}")


REQUIRED_SECTIONS = SECTIONS


def validate_plan(plan: str) -> str:
    text = plan.strip()
    lines = text.splitlines()
    matches = [(index, match.group(1)) for index, line in enumerate(lines)
               if (match := HEADING.match(line.strip()))]
    headings = [heading for _, heading in matches]
    if tuple(headings) != SECTIONS:
        raise ValueError(f"expected headings {SECTIONS!r}, got {tuple(headings)!r}")
    if len(text.split()) > 1_200:
        raise ValueError("plan exceeds the 1,200-word contract")
    for i, (start, heading) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(lines)
        content = " ".join(lines[start + 1:end]).strip()
        if heading in REQUIRED_SECTIONS and not content:
            raise ValueError(f"{heading} section is empty")
    shape_start = matches[3][0] + 1
    shape_end = matches[4][0]
    shape = " ".join(lines[shape_start:shape_end]).upper()
    for dash in ("‐", "‑", "‒", "–", "—", "−"):
        shape = shape.replace(dash, "-")
    has_root = re.search(r"\bROOT\s*-\s*DIRECT\b", shape) is not None
    has_fanout = re.search(r"\bFANOUT\b", shape) is not None
    if has_root and has_fanout:
        raise ValueError("EXECUTION SHAPE must choose exactly one of ROOT-DIRECT or FANOUT")
    if not (has_root or has_fanout):
        raise ValueError("EXECUTION SHAPE must choose ROOT-DIRECT or FANOUT")
    return text


def command(*, codex: str, output: Path, workdir: Path, brief: str, planner: str = "sol") -> list[str]:
    models = {"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"}
    if not isinstance(planner, str) or planner not in models:
        raise ValueError(f"unknown planner: {planner}")
    return [
        codex,
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--model",
        models[planner],
        "--config",
        'model_reasoning_effort="ultra"',
        "--enable",
        "multi_agent",
        "--config",
        "agents.max_threads=4",
        "--config",
        "agents.max_depth=1",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--cd",
        str(workdir),
        "--json",
        "--output-last-message",
        str(output),
        brief,
    ]


def event_progress(event: object) -> str | None:
    """Return a content-free lifecycle update for one Codex JSONL event."""
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type == "thread.started":
        return "planner thread started"
    if event_type == "turn.started":
        return "Ultra planning turn started"
    if event_type == "turn.completed":
        return "planning turn completed"
    if event_type == "turn.failed":
        return "planning turn failed"
    if event_type == "error":
        return "planner reported an error"
    if event_type not in ("item.started", "item.completed", "item.updated"):
        return None
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    label = ITEM_LABELS.get(item_type, "work item") if isinstance(item_type, str) else "work item"
    action = event_type.removeprefix("item.")
    return f"{label} {action}"


class JsonlFramer:
    """Incrementally frame bounded JSONL records without retaining oversized input."""

    def __init__(self, max_record_bytes: int = MAX_EVENT_BYTES):
        self.max_record_bytes = max_record_bytes
        self.buffer = bytearray()
        self.discarding = False
        self.oversized = 0

    def feed(self, data: bytes) -> list[bytes]:
        records: list[bytes] = []
        cursor = 0
        while cursor < len(data):
            if self.discarding:
                newline = data.find(b"\n", cursor)
                if newline < 0:
                    return records
                self.discarding = False
                cursor = newline + 1
                continue

            newline = data.find(b"\n", cursor)
            end = len(data) if newline < 0 else newline
            piece = data[cursor:end]
            remaining = self.max_record_bytes - len(self.buffer)
            if len(piece) > remaining:
                self.buffer.clear()
                self.oversized += 1
                if newline < 0:
                    self.discarding = True
                    return records
            else:
                self.buffer.extend(piece)
                if newline >= 0:
                    records.append(bytes(self.buffer))
                    self.buffer.clear()
            if newline < 0:
                return records
            cursor = newline + 1
        return records

    def finish(self) -> list[bytes]:
        if self.discarding or not self.buffer:
            self.buffer.clear()
            return []
        record = bytes(self.buffer)
        self.buffer.clear()
        return [record]


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def _cleanup_signal_guard():
    """Prevent repeated cancellation signals from interrupting escalation."""
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
        except ValueError:
            # signal.signal is restricted to the main thread.
            pass
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Terminate and reap the planner's complete dedicated process group."""
    with _cleanup_signal_guard():
        _terminate_process_group(proc)


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + CLEANUP_GRACE_SECONDS
    while _group_exists(pgid) and time.monotonic() < deadline:
        try:
            proc.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.01)

    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()
    except BaseException:
        try:
            proc.wait(timeout=1)
        except (BaseException, subprocess.TimeoutExpired):
            pass


def _parse_records(records: list[bytes], stats: dict[str, int],
                   progress: Callable[[str], None]) -> str | None:
    last_update = None
    for record in records:
        if not record:
            continue
        try:
            event = json.loads(record)
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats["malformed_events"] += 1
            continue
        update = event_progress(event)
        if update:
            progress(f"solplan: {update}")
            last_update = update
    return last_update


def _read_ready(
    selector: selectors.BaseSelector,
    ready,
    framer: JsonlFramer,
    stats: dict[str, int],
    progress: Callable[[str], None],
) -> str | None:
    last_update = None
    for key, _ in ready:
        try:
            chunk = os.read(key.fd, READ_CHUNK_BYTES)
        except BlockingIOError:
            continue
        if not chunk:
            try:
                selector.unregister(key.fileobj)
            except KeyError:
                pass
            continue
        if key.data == "stderr":
            stats["stderr_bytes"] += len(chunk)
            continue
        update = _parse_records(framer.feed(chunk), stats, progress)
        if update:
            last_update = update
    return last_update


def read_final_plan(output: Path, planner: str = "sol") -> str:
    # O_NONBLOCK keeps opening a FIFO/socket from blocking on a missing writer;
    # the fstat below still rejects any non-regular file.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(output, flags)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{planner.title()} planner returned success without a final response") from exc
    except OSError as exc:
        raise RuntimeError(f"{planner.title()} planner final response is not a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"{planner.title()} planner final response is not a regular file")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            payload = handle.read(MAX_PLAN_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(payload) > MAX_PLAN_BYTES:
        raise RuntimeError(f"{planner.title()} planner final response exceeds {MAX_PLAN_BYTES} bytes")
    try:
        plan = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{planner.title()} planner final response is not valid UTF-8") from exc
    try:
        return validate_plan(plan)
    except ValueError as exc:
        raise RuntimeError(f"invalid {planner.title()} plan: {exc}") from exc


def _stderr_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(
    *,
    brief: str,
    workdir: Path,
    codex: str = "codex",
    progress: Callable[[str], None] = _stderr_progress,
    heartbeat_seconds: float = 30,
    planner: str = "sol",
) -> str:
    if not brief.strip():
        raise ValueError("the planning brief is empty")
    if len(brief.encode("utf-8")) > 96_000:
        raise ValueError("the planning brief exceeds 96,000 bytes")
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be positive")
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise ValueError(f"workdir is not a directory: {workdir}")

    original_progress = progress
    progress = lambda message: original_progress(message.replace("solplan:", f"{planner}plan:", 1))
    env = os.environ.copy()
    env["OMAXX_PLANNER_CHILD"] = planner
    env["SOLPLAN_CHILD"] = "1"
    env["ORCHESTRATORMAXXING_HARNESS_CHILD"] = "1"
    with tempfile.TemporaryDirectory(prefix="solplan-") as tmp:
        output = Path(tmp) / "plan.md"
        proc = subprocess.Popen(
            command(codex=codex, output=output, workdir=workdir, brief=brief, planner=planner),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        selector = None
        framer = JsonlFramer()
        stats = {"malformed_events": 0, "stderr_bytes": 0}
        status = None
        try:
            assert proc.stdout is not None and proc.stderr is not None
            os.set_blocking(proc.stdout.fileno(), False)
            os.set_blocking(proc.stderr.fileno(), False)
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            started_at = last_update = time.monotonic()
            last_phase = "process launched"
            progress(f"{planner}plan: {planner.title()} Ultra planner launched; no wall-clock deadline")

            while proc.poll() is None:
                now = time.monotonic()
                wait_for = max(0.0, heartbeat_seconds - (now - last_update))
                ready = selector.select(min(wait_for, LEADER_POLL_SECONDS))
                if not ready:
                    if time.monotonic() - last_update >= heartbeat_seconds:
                        elapsed = int(time.monotonic() - started_at)
                        progress(f"solplan: still working ({elapsed}s elapsed; last: {last_phase})")
                        last_update = time.monotonic()
                    continue
                update = _read_ready(selector, ready, framer, stats, progress)
                if update:
                    last_phase = update
                    last_update = time.monotonic()

            status = proc.wait()
            # A descendant may retain the pipes after the leader exits. No valid
            # planner work may outlive codex exec, so terminate residual members.
            terminate_process_group(proc)

            drain_deadline = time.monotonic() + 1
            while selector.get_map() and time.monotonic() < drain_deadline:
                ready = selector.select(0.05)
                if not ready:
                    break
                update = _read_ready(selector, ready, framer, stats, progress)
                if update:
                    last_phase = update
            update = _parse_records(framer.finish(), stats, progress)
            if update:
                last_phase = update
        except BaseException:
            terminate_process_group(proc)
            raise
        finally:
            if selector is not None:
                selector.close()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

        assert status is not None
        if status != 0:
            details = [f"child stderr suppressed: {stats['stderr_bytes']} bytes"]
            if stats["malformed_events"]:
                details.append(f"malformed events: {stats['malformed_events']}")
            if framer.oversized:
                details.append(f"oversized events: {framer.oversized}")
            raise RuntimeError(f"{planner.title()} planner exited with status {status} ({'; '.join(details)})")
        return read_final_plan(output, planner=planner)


def main(argv: list[str] | None = None, *, planner: str = "sol") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=os.getcwd())
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    parser.add_argument("--codex-bin", default="codex", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.heartbeat_seconds <= 300:
        parser.error("--heartbeat-seconds must be between 1 and 300")

    previous_handlers = {}

    def interrupt(signum, _frame):
        raise PlannerInterrupted(signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, interrupt)
    try:
        print(run(brief=sys.stdin.read(), workdir=Path(args.workdir),
                  heartbeat_seconds=args.heartbeat_seconds, codex=args.codex_bin, planner=planner))
    except PlannerInterrupted as exc:
        print(f"{planner}plan: {exc}", file=sys.stderr)
        return 128 + exc.signum
    except KeyboardInterrupt:
        print(f"{planner}plan: interrupted", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"{planner}plan: {exc}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
