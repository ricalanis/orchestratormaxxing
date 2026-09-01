#!/usr/bin/env python3
"""
Idempotently register the orchestration hook (orch-notify.py) in the GLOBAL
Claude Code settings (~/.claude/settings.json) so feature-3 notifications fire
in *every* session on this machine.

Adds a command hook on Notification, PreCompact, and Stop pointing at
orch-notify.py — but only if an entry for that script isn't already present
(matched by the script path substring), so re-running is safe. Existing hooks
(claude-hook-notify, session-log, mem-audit, …) are preserved untouched.

Usage:  python3 hooks/install-hooks.py [--dry-run]
"""
import json
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
SCRIPT = (Path(__file__).resolve().parent / "orch-notify.py")
EVENTS = ("Notification", "PreCompact", "Stop")
# UUID→tmux stamping (SessionStart): writes @claude-session-id on the
# enclosing tmux session so dashboard send/capture resolves ANY session.
STAMP_SCRIPT = (Path(__file__).resolve().parent / "tmux-stamp.sh")


def already_present(hook_list, needle: str) -> bool:
    for group in hook_list or []:
        for h in group.get("hooks", []):
            if needle in (h.get("command") or ""):
                return True
    return False


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not SCRIPT.exists():
        print(f"✗ hook script missing: {SCRIPT}", file=sys.stderr)
        return 1
    cmd = f"python3 {SCRIPT}"

    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except Exception as e:
            print(f"✗ could not parse {SETTINGS}: {e}", file=sys.stderr)
            return 1
    hooks = settings.setdefault("hooks", {})

    added = []
    for event in EVENTS:
        lst = hooks.setdefault(event, [])
        if already_present(lst, "orch-notify.py"):
            continue
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if event == "Notification":
            entry["matcher"] = "*"
        lst.append(entry)
        added.append(event)

    # SessionStart → tmux UUID stamping (the persistent session mapping).
    lst = hooks.setdefault("SessionStart", [])
    if not already_present(lst, "tmux-stamp.sh"):
        lst.append({"hooks": [{"type": "command",
                               "command": f"bash {STAMP_SCRIPT}", "timeout": 5}]})
        added.append("SessionStart(tmux-stamp)")

    if not added:
        print("✓ orch-notify + tmux-stamp hooks already registered — nothing to do")
        return 0

    if dry:
        print(f"[dry-run] would add hooks to: {', '.join(added)}")
        return 0

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2))
    print(f"✓ registered orch-notify hook on: {', '.join(added)}")
    print(f"  → {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
