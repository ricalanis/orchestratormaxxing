"""command-guard plugin — block dangerous terminal commands before Hermes runs them.

A ``pre_tool_call`` hook for the ``terminal`` tool that shells out to the cmaxxing
cross-host runner ``agent-guard pre --format json`` (the same denylist every other
host enforces) and returns ``{"action": "block", "message": ...}`` on a deny.

Fail-open by construction: every exception — a missing binary, a timeout, bad
JSON — yields ``None`` (let it through). Hermes hooks must never brick the
terminal tool; whether the guard is actually present is measured elsewhere, by
``agent-guard status --gate``. No relative imports: the harness contract loads
this file standalone.

Binary resolution: ``$ORCHESTRATORMAXXING_AGENT_GUARD`` (exclusive when set) →
``shutil.which("agent-guard")`` → ``~/.local/bin/agent-guard``.
"""

import json
import os
import shutil
import subprocess

_TIMEOUT_SECONDS = 5


def _guard_binary():
    explicit = os.environ.get("ORCHESTRATORMAXXING_AGENT_GUARD", "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    found = shutil.which("agent-guard")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/agent-guard")


def _decide(command):
    payload = json.dumps({"tool_name": "terminal", "tool_input": {"command": command}})
    proc = subprocess.run(
        [_guard_binary(), "pre", "--format", "json"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        return None
    return data


def _on_pre_tool_call(tool_name="", args=None, **_):
    """Return a block dict for a dangerous terminal command, else None."""
    try:
        if tool_name != "terminal" or not isinstance(args, dict):
            return None
        command = args.get("command")
        if not isinstance(command, str) or not command:
            return None
        verdict = _decide(command)
        if verdict is None or verdict.get("decision") != "deny":
            return None
        reason = verdict.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "agent-guard: blocked by the dangerous-command guard. Matched pattern: %s" % (
                verdict.get("pattern"),
            )
        return {"action": "block", "message": reason}
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
