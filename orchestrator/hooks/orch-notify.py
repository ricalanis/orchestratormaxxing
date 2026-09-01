#!/usr/bin/env python3
"""
Claude Code → Hermes orchestration bridge (feature 3: hooks notification).

Registered on the `Notification`, `PreCompact`, and `Stop` hook events. When a
Claude Code session needs your input (permission prompt, a question, or it's
been idle waiting), the `Notification` event fires — we forward it to the
dashboard's /api/session-events so a *blocked* agent surfaces in the fleet
console's attention queue instead of silently waiting for you to notice.

  Notification → kind=input_needed   (surfaces in the "needs you" queue)
  PreCompact   → kind=compact_suggested / compacted
  Stop         → kind=stop           (also clears that session's open asks)

Pure stdlib, sub-second timeout, never raises: a hook must never break the
session it's attached to. Reads the hook JSON on stdin (Claude Code passes
session_id, hook_event_name, cwd, message, …).
"""
import json
import os
import sys
import urllib.request

DASHBOARD_URL = os.environ.get("ORCH_DASHBOARD_URL", "http://127.0.0.1:3000")


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id") or ""
    cwd = data.get("cwd") or os.getcwd()
    project = os.path.basename(cwd) if cwd else ""

    kind_map = {
        "Notification": "input_needed",
        "PreCompact": "compact_suggested",
        "Stop": "stop",
        "SessionEnd": "stop",
    }
    kind = kind_map.get(event)
    if not kind or not session_id:
        return 0

    payload = {
        "session_key": session_id,
        "kind": kind,
        "host": "local",
        "project": project,
        "cwd": cwd,
        "message": data.get("message") or data.get("notification") or "",
        "event": event,
    }
    try:
        req = urllib.request.Request(
            f"{DASHBOARD_URL}/api/session-events",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass  # dashboard down / not this machine — a notification is best-effort
    return 0


if __name__ == "__main__":
    sys.exit(main())
