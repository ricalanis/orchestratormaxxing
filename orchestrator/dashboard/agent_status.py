"""
Agent status monitor — detects what agents are currently doing.
Checks tmux sessions, Hermes cron jobs, and Claude Code project activity.
"""
import subprocess
import time
import json
from pathlib import Path
from typing import Optional


def _run(cmd: list[str], timeout: int = 5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def get_tmux_sessions() -> list[dict]:
    """List active tmux sessions with metadata."""
    out = _run(["tmux", "ls", "-F", "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}"])
    sessions = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, created, windows, attached = parts[0], parts[1], parts[2], parts[3]
        # Classify session
        agent_type = "unknown"
        if name.startswith("task-"):
            agent_type = "claude-code"
        elif name in ("hermes",):
            agent_type = "hermes"
        elif "opencode" in name.lower():
            agent_type = "opencode"
        sessions.append({
            "name": name,
            "created": int(created) if created.isdigit() else 0,
            "windows": int(windows) if windows.isdigit() else 0,
            "attached": attached == "1",
            "agent_type": agent_type,
            "status": "active",
        })
    return sessions


def get_hermes_cron() -> list[dict]:
    """Get Hermes cron jobs and their status."""
    out = _run(["hermes", "cron", "list", "--json"], timeout=10)
    if not out:
        # Fallback: parse text output
        out = _run(["hermes", "cron", "list"], timeout=10)
        # Simple parse — return raw for now
        if out:
            return [{"raw": out, "status": "active"}]
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def get_claude_sessions() -> list[dict]:
    """Detect recent Claude Code sessions from ~/.claude/projects/."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []
    sessions = []
    for proj_dir in claude_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        # Each .jsonl file is a session
        for session_file in proj_dir.glob("*.jsonl"):
            stat = session_file.stat()
            age = time.time() - stat.st_mtime
            if age < 3600:  # active in last hour
                status = "active"
            elif age < 86400:  # active in last 24h
                status = "recent"
            else:
                continue  # skip old sessions
            sessions.append({
                "project": proj_dir.name,
                "session_file": session_file.name,
                "modified": int(stat.st_mtime),
                "age_seconds": age,
                "status": status,
            })
    sessions.sort(key=lambda x: x["modified"], reverse=True)
    return sessions[:10]  # top 10 most recent


def get_agent_status() -> dict:
    """Aggregate all agent statuses for the dashboard sidebar."""
    tmux = get_tmux_sessions()
    claude = get_claude_sessions()

    agents = []

    # Hermes (self)
    agents.append({
        "name": "Hermes",
        "type": "hermes",
        "status": "active",
        "detail": "Gateway running",
        "sessions": [],
    })

    # Claude Code
    active_tmux = [s for s in tmux if s["agent_type"] == "claude-code"]
    agents.append({
        "name": "Claude Code",
        "type": "claude-code",
        "status": "active" if active_tmux or claude else "idle",
        "detail": f"{len(active_tmux)} tmux sessions, {len(claude)} recent sessions",
        "sessions": active_tmux + claude[:3],
    })

    # OpenCode (check if installed)
    opencode_path = _run(["which", "opencode"])
    if opencode_path:
        agents.append({
            "name": "OpenCode",
            "type": "opencode",
            "status": "idle",
            "detail": "Installed, no active sessions",
            "sessions": [],
        })
    else:
        agents.append({
            "name": "OpenCode",
            "type": "opencode",
            "status": "offline",
            "detail": "Not installed on this machine",
            "sessions": [],
        })

    # Tmux sessions (generic)
    other_tmux = [s for s in tmux if s["agent_type"] not in ("claude-code", "hermes")]
    if other_tmux:
        agents.append({
            "name": "Other Sessions",
            "type": "tmux",
            "status": "active",
            "detail": f"{len(other_tmux)} sessions",
            "sessions": other_tmux,
        })

    # Cron jobs
    cron = get_hermes_cron()
    if cron:
        agents.append({
            "name": "Cron Jobs",
            "type": "cron",
            "status": "active",
            "detail": f"{len(cron)} jobs scheduled",
            "sessions": cron[:5],
        })

    return {
        "agents": agents,
        "tmux_sessions": tmux,
        "claude_sessions": claude,
        "timestamp": int(time.time()),
    }