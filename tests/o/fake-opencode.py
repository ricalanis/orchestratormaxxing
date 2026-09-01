#!/usr/bin/env python3
"""PTY fake for the public o runtime contract.

It renders the 1.18-style launch footer without a placeholder, then completes
turns through the same machine-only bind-event path used by the OpenCode
plugin. Tests therefore cannot pass on the old ``Ask anything`` pane lore.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def payload_for(line: str, turn: int) -> dict[str, str]:
    pane = os.environ.get("TMUX_PANE", "%0").lstrip("%") or "0"
    base = {
        "session_id": f"ses_fake{pane}",
        "message_id": f"msg_fake{pane}_{turn}",
        "finish": "stop",
        "text": f"FINAL-TURN-{turn}",
        "error_code": "",
    }
    if "EMPTY_FINAL" in line:
        base["text"] = ""
    elif "INCOMPLETE_TURN" in line:
        base["finish"] = "tool-calls"
        base["text"] = ""
    elif "PROVIDER_ERROR" in line:
        base["finish"] = "error"
        base["text"] = ""
        base["error_code"] = "429"
    elif "OVERSIZE_FINAL" in line:
        base["text"] = "X" * 70000
    return base


def run_mode(argv: list[str]) -> int:
    """`opencode run --format json …` — the TUI-less delegated-turn transport."""
    if os.environ.get("ORCHESTRATORMAXXING_O_DELEGATED") != "1":
        print("RUN_WITHOUT_DELEGATION_MARK", file=sys.stderr)
        return 95
    agent = session = None
    prompt = []
    it = iter(range(len(argv)))
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--agent":
            agent = argv[i + 1]; i += 2
        elif a in ("-s", "--session"):
            session = argv[i + 1]; i += 2
        elif a in ("--format",):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            prompt.append(a); i += 1
    line = " ".join(prompt)
    # A fresh worker mints a UNIQUE session id (pane indices repeat across tmux
    # sessions, which once leaked one worker's turn counter into another).
    sid = session or f"ses_fake{os.getpid()}"
    state = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fake-oc-{sid}.turn")
    try:
        turn = int(open(state).read().strip()) + 1
    except (OSError, ValueError):
        turn = 1
    with open(state, "w", encoding="utf-8") as fh:
        fh.write(str(turn))
    if "NO_EVENT" in line:
        import time
        time.sleep(600)          # the runner's timeout is the only way out
        return 0
    pay = payload_for(line, turn)
    pay["session_id"] = sid
    mid = pay["message_id"]
    def ev(kind, part):
        part = dict(part); part["sessionID"] = sid; part["messageID"] = mid
        print(json.dumps({"type": kind, "sessionID": sid, "part": part}, separators=(",", ":")), flush=True)
    ev("step_start", {"type": "step-start"})
    if "PROVIDER_ERROR" in line:
        print(json.dumps({"type": "error", "sessionID": sid,
                          "error": {"name": "ProviderRateLimit"},
                          "part": {"sessionID": sid, "messageID": mid}}, separators=(",", ":")), flush=True)
        return 1
    if pay["text"]:
        ev("text", {"type": "text", "text": pay["text"]})
    reason = "stop" if pay["finish"] == "stop" else pay["finish"]
    ev("step_finish", {"type": "step-finish", "reason": reason})
    return 0


def main() -> int:
    if os.environ.get("ORCHESTRATORMAXXING_HARNESS_CHILD") != "1":
        print("UNMARKED_WORKER", file=sys.stderr)
        return 91
    if args_log := os.environ.get("OPENCODE_ARGS_LOG"):
        with open(args_log, "a", encoding="utf-8") as stream:
            stream.write(" ".join(sys.argv[1:]) + "\n")
    if sys.argv[1:2] == ["run"]:
        return run_mode(sys.argv[2:])
    delegated = os.environ.get("ORCHESTRATORMAXXING_O_DELEGATED") == "1"
    if "--auto" in sys.argv[1:] and not delegated:
        print("UNMARKED_DELEGATED_WORKER", file=sys.stderr)
        return 93
    if "--auto" not in sys.argv[1:] and delegated:
        print("INTERACTIVE_WORKER_WAS_MARKED_DELEGATED", file=sys.stderr)
        return 94
    if "--startup-probe" in sys.argv[1:]:
        attached = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_attached}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        ).stdout.strip()
        if marker := os.environ.get("OPENCODE_STARTUP_MARKER"):
            with open(marker, "w", encoding="utf-8") as stream:
                stream.write(attached or "unreadable")
        print(f"STARTUP-ATTACHED:{attached or 'unreadable'}", flush=True)
        return 0 if attached not in ("", "0") else 92
    print("workspace:main                                      1.18.5", flush=True)
    turn = 0
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        turn += 1
        print("⬝⬝⬝⬝⬝⬝⬝⬝  esc interrupt", flush=True)
        if "NO_EVENT" in line:
            # Simulate a model turn that never reaches a typed terminal event.
            # The runtime must keep it pending and refuse overlapping sends.
            print(f"TURN-{turn}-PENDING", flush=True)
            continue
        proc = subprocess.run(
            ["o", "bind-event", "--json"],
            input=json.dumps(payload_for(line, turn)),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode:
            print(f"BIND-FAILED:{proc.returncode}", flush=True)
            continue
        print(f"TURN-{turn}-IDLE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
