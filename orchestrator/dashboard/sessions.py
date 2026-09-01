"""
Unified session monitor — detects active Claude Code and OpenCode sessions
across local machine and remote hosts (Mac, VPS) via Tailscale SSH.

Each session is normalized to a common format regardless of source.
"""
import subprocess
import json
import time
import os
import re
import socket
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

# Strip terminal control sequences (colour, cursor moves) that leak into
# transcripts from slash-command stdout so the modal reads as clean text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Za-z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CMD_NAME_RE = re.compile(r"<command-name>\s*(/?[^<]+?)\s*</command-name>")


def _clean_message_text(text: str) -> Optional[str]:
    """Normalise one message body for display: strip ANSI, collapse a
    slash-command envelope (<command-name>/model</command-name> …) to just the
    command, and drop pure harness meta (caveats). Returns None to skip."""
    if not isinstance(text, str):
        return None
    text = _ANSI_RE.sub("", text)
    # Slash-command invocations are stored as a tag envelope — show the command.
    m = _CMD_NAME_RE.search(text)
    if m:
        return m.group(1).strip()
    # Drop local-command / caveat envelopes entirely (harness plumbing, not chat).
    if "<local-command-caveat>" in text or "<command-message>" in text:
        return None
    # Strip any residual angle-bracket harness tags but keep surrounding prose.
    text = re.sub(r"</?(command|local-command)[^>]*>", "", text)
    text = text.strip()
    return text or None

# --- Config ---
# Remote fleet inventory — loaded from config, never hardcoded. Provide rows via
# the HERMES_REMOTE_HOSTS env var (a JSON array) or ~/.hermes/remote-hosts.json.
# Row shape: {"name": ..., "host": ..., "user": ..., "tmux_user": ...,
#             "tailscale_ip": ..., "icon": ..., "home": ...}
def _load_remote_hosts() -> list:
    raw = os.environ.get("HERMES_REMOTE_HOSTS", "").strip()
    if not raw:
        cfg_path = Path.home() / ".hermes" / "remote-hosts.json"
        try:
            raw = cfg_path.read_text() if cfg_path.exists() else ""
        except OSError:
            raw = ""
    if not raw:
        return []
    try:
        hosts = json.loads(raw)
    except ValueError:
        return []
    return hosts if isinstance(hosts, list) else []


REMOTE_HOSTS = _load_remote_hosts()

# Local machine info
LOCAL_HOST = {
    "name": socket.gethostname(),
    "host": socket.gethostname(),
    "icon": "🖥️",
}

SSH_TIMEOUT = 8


def _run_local(cmd: list[str], timeout: int = 5) -> str:
    """Run a command locally, return stdout."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _run_remote(host: str, user: str, cmd: str, timeout: int = SSH_TIMEOUT) -> str:
    """Run a command on a remote host via Tailscale SSH."""
    try:
        r = subprocess.run(
            ["tailscale", "ssh", f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    except Exception:
        return ""


def _run_remote_rc(host: str, user: str, cmd: str,
                   timeout: int = SSH_TIMEOUT) -> tuple[Optional[int], str, str]:
    """Run remotely while preserving failure versus empty successful output."""
    try:
        r = subprocess.run(
            ["tailscale", "ssh", f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return None, "", str(e)


def _online_remote_hosts() -> list[dict]:
    """The REMOTE_HOSTS currently online, via ONE `tailscale status` (the scan
    functions used to call it once PER host, per category — redundant). A host
    is online if it appears in the status and its line isn't marked offline."""
    status = _run_local(["tailscale", "status"], timeout=3)
    if not status or "__TIMEOUT__" in status:
        return []
    online = []
    for host in REMOTE_HOSTS:
        if host["host"] not in status:
            continue
        line = next((l for l in status.splitlines() if host["host"] in l), "")
        if line and "offline" not in line:
            online.append(host)
    return online


def _probe_hosts(fn, hosts) -> list:
    """Run fn(host) for every host CONCURRENTLY and flatten the list results.
    Per-host work is independent `tailscale ssh` I/O, so fanning it out cuts a
    multi-host scan from sum-of-hosts to slowest-host. Runs inline for 0/1 host
    (no thread overhead); a failing host contributes [] rather than aborting."""
    if not hosts:
        return []
    if len(hosts) == 1:
        try:
            return list(fn(hosts[0]) or [])
        except Exception:
            return []
    def _safe(h):
        try:
            return list(fn(h) or [])
        except Exception:
            return []
    out = []
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
        for res in ex.map(_safe, hosts):
            out.extend(res)
    return out


# --- Claude Code Sessions ---

def _get_running_claude_pids() -> set:
    """Get PIDs of all running claude processes."""
    try:
        r = subprocess.run(["pgrep", "-f", "claude.*dangerously-skip"], 
                          capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return {int(pid) for pid in r.stdout.strip().split("\n") if pid.strip().isdigit()}
    except Exception:
        pass
    return set()


def _get_running_opencode_pids() -> set:
    """Get PIDs of all running opencode processes."""
    try:
        r = subprocess.run(["pgrep", "-f", "opencode"], 
                          capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return {int(pid) for pid in r.stdout.strip().split("\n") if pid.strip().isdigit()}
    except Exception:
        pass
    return set()


def _is_process_alive(session_file: str, agent: str = "claude-code") -> bool:
    """Check if there's a running process for this session by checking
    if the jsonl file is still being written to (modified in last 30s)
    AND there's a running process matching the project path."""
    try:
        stat = Path(session_file).stat()
        age = time.time() - stat.st_mtime
        # A file modified in the last 30 seconds is likely being actively written
        if age < 30:
            return True
        # Also check if the file is currently open by any process
        r = subprocess.run(["fuser", session_file], 
                          capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return True
    except Exception:
        pass
    return False


def _extract_cwd_from_jsonl(session_file: Path) -> Optional[str]:
    """Extract the cwd field from the early metadata lines of a .jsonl transcript."""
    try:
        with open(session_file, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = data.get("cwd")
                if cwd:
                    return cwd
                # Some early messages nest cwd under message / origin
                msg = data.get("message", {})
                if isinstance(msg, dict):
                    cwd = msg.get("cwd") or msg.get("origin", {}).get("cwd")
                    if cwd:
                        return cwd
    except Exception:
        pass
    return None


def _get_claude_projects_local() -> list[dict]:
    """Get Claude Code sessions from ~/.claude/projects/ on local machine."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    sessions = []
    running_claude = _get_running_claude_pids()

    for proj_dir in claude_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        project_name = proj_dir.name

        # Each .jsonl file is a session transcript
        for session_file in proj_dir.glob("*.jsonl"):
            stat = session_file.stat()
            age = time.time() - stat.st_mtime

            if age > 86400:
                continue  # skip sessions older than 24h

            # Determine real status: is the file being actively written?
            file_is_hot = age < 30  # modified in last 30s
            file_locked = False
            try:
                r = subprocess.run(["fuser", str(session_file)],
                                  capture_output=True, text=True, timeout=2)
                file_locked = r.returncode == 0 and r.stdout.strip()
            except Exception:
                pass

            if file_is_hot or file_locked:
                status = "active"
            elif age < 300:
                status = "recent"   # modified in last 5min but not actively writing
            elif age < 3600:
                status = "idle"      # modified in last hour
            else:
                status = "idle"      # modified in last 24h

            # Try to get the last line for a preview
            preview = ""
            try:
                with open(session_file, 'r') as f:
                    lines = f.readlines()
                    if lines:
                        last = lines[-1]
                        # Try to parse as JSON for a message preview
                        try:
                            data = json.loads(last)
                            if data.get("type") == "assistant":
                                msg = data.get("message", {})
                                content = msg.get("content", [])
                                if isinstance(content, list) and content:
                                    preview = str(content[0].get("text", ""))[:120]
                            elif data.get("type") == "user":
                                msg = data.get("message", {})
                                content = msg.get("content", "")
                                if isinstance(content, str):
                                    preview = content[:120]
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass

            cwd = _extract_cwd_from_jsonl(session_file)
            display_name = os.path.basename(cwd) if cwd else session_file.stem

            sessions.append({
                "agent": "claude-code",
                "host": "local",
                "host_name": LOCAL_HOST["name"],
                "host_icon": LOCAL_HOST["icon"],
                "host_machine": LOCAL_HOST["host"],
                "project": project_name.replace("-", "/"),
                "session_id": session_file.stem,
                "display_name": display_name,
                "cwd": cwd,                       # full cwd → matched to a tmux pane path
                "project_dir": project_name,      # ~/.claude/projects/<project_dir>/
                "status": status,
                "age_seconds": int(age),
                "modified": int(stat.st_mtime),
                "preview": preview,
                "size_kb": int(stat.st_size / 1024),
            })

    # UUID→tmux attachment (one registry call for the whole list): a session is
    # 'live' when a tmux terminal answers for it — stamped option first; else
    # the legacy cwd fallback, where the MOST RECENT transcript per cwd wins
    # the unstamped terminal (so siblings don't all claim one pane).
    reg = _tmux_registry("local")
    by_uuid = {r["uuid"]: r["name"] for r in reg if r["uuid"]}
    unstamped_by_path = {}
    for r in reg:
        if not r["uuid"]:
            unstamped_by_path.setdefault(r["path"], r["name"])
    latest_for_cwd = {}
    for s in sessions:
        cwd = s.get("cwd")
        if cwd and (cwd not in latest_for_cwd or s["modified"] > latest_for_cwd[cwd][1]):
            latest_for_cwd[cwd] = (s["session_id"], s["modified"])
    for s in sessions:
        t = by_uuid.get(s["session_id"])
        if not t:
            cwd = s.get("cwd")
            if cwd in unstamped_by_path and latest_for_cwd.get(cwd, (None,))[0] == s["session_id"]:
                t = unstamped_by_path[cwd]
        s["tmux_attached"] = bool(t)
        s["tmux_session"] = t

    sessions.sort(key=lambda x: x["modified"], reverse=True)
    return sessions


def _get_claude_projects_remote(host: dict) -> list[dict]:
    """Get Claude Code sessions from a remote host via SSH."""
    home = host.get("home", "~")
    cmd = (
        f'find {home}/.claude/projects -name "*.jsonl" -mtime -1 '
        f'2>/dev/null | head -20'
    )
    result = _run_remote(host["host"], host["user"], cmd)
    if not result or result == "__TIMEOUT__":
        return []

    sessions = []
    for line in result.splitlines():
        line = line.strip()
        if not line:
            continue
        # Extract project name and session ID from path
        # Path: ~/.claude/projects/<project>/<session>.jsonl
        parts = line.split("/")
        if len(parts) < 2:
            continue
        session_file = parts[-1]
        project_name = parts[-2] if len(parts) >= 2 else "unknown"
        session_id = session_file.replace(".jsonl", "")

        # Get modification time
        time_cmd = f'stat -c %Y "{line}" 2>/dev/null || stat -f %m "{line}" 2>/dev/null'
        time_result = _run_remote(host["host"], host["user"], time_cmd, timeout=5)
        mtime = int(time_result) if time_result and time_result.isdigit() else int(time.time())

        age = time.time() - mtime
        if age < 300:
            status = "active"
        elif age < 3600:
            status = "recent"
        else:
            status = "idle"

        # Use the project directory name as the display name. The project path is
        # already available from the find output (e.g. /Users/.../.claude/projects/<project>),
        # so we avoid fragile remote jsonl parsing over SSH.
        project_path = project_name.replace("-", "/")
        display_name = os.path.basename(project_path) if project_path else session_id

        sessions.append({
            "agent": "claude-code",
            "host": host["host"],
            "host_name": host["name"],
            "host_icon": host.get("icon", "🖥️"),
            "host_machine": host["host"],
            "project": project_name.replace("-", "/"),
            "session_id": session_id,
            "display_name": display_name,
            "cwd": project_path,          # decoded project path (best-effort remote cwd)
            "project_dir": project_name,  # encoded dir name → matched to encoded tmux path
            "status": status,
            "age_seconds": int(age),
            "modified": mtime,
            "preview": "",
            "size_kb": 0,
        })

    # Remote UUID→tmux attachment — the SAME registry abstraction as local
    # (one SSH round-trip): stamped option first, then the un-gated cwd
    # fallback with most-recent-transcript disambiguation.
    try:
        reg = _tmux_registry(host["host"])
        by_uuid = {r["uuid"]: r["name"] for r in reg if r["uuid"]}
        unstamped_by_path = {}
        for r in reg:
            if not r["uuid"]:
                unstamped_by_path.setdefault(r["path"], r["name"])
        latest_for_cwd = {}
        for s in sessions:
            cwd = s.get("cwd")
            if cwd and (cwd not in latest_for_cwd or s["modified"] > latest_for_cwd[cwd][1]):
                latest_for_cwd[cwd] = (s["session_id"], s["modified"])
        for s in sessions:
            t = by_uuid.get(s["session_id"])
            if not t:
                cwd = s.get("cwd")
                if cwd in unstamped_by_path and latest_for_cwd.get(cwd, (None,))[0] == s["session_id"]:
                    t = unstamped_by_path[cwd]
            s["tmux_attached"] = bool(t)
            s["tmux_session"] = t
    except Exception:
        for s in sessions:
            s.setdefault("tmux_attached", False)
            s.setdefault("tmux_session", None)

    sessions.sort(key=lambda x: x["modified"], reverse=True)
    return sessions


def get_claude_code_sessions() -> list[dict]:
    """Get all Claude Code sessions across all hosts."""
    sessions = []
    sessions.extend(_get_claude_projects_local())
    # Remote hosts probed CONCURRENTLY (was a serial for-loop with a redundant
    # per-host tailscale status).
    sessions.extend(_probe_hosts(_get_claude_projects_remote, _online_remote_hosts()))
    return sessions


# --- OpenCode Sessions ---

def _get_opencode_sessions_local() -> list[dict]:
    """Get OpenCode sessions from local machine."""
    # OpenCode stores sessions in ~/.local/share/opencode/ or ~/.config/opencode/
    opencode_data = Path.home() / ".local" / "share" / "opencode"
    if not opencode_data.exists():
        opencode_data = Path.home() / ".config" / "opencode"

    if not opencode_data.exists():
        return []

    sessions = []
    # Look for session files
    for session_dir in [opencode_data / "sessions", opencode_data / "projects"]:
        if not session_dir.exists():
            continue
        for sf in session_dir.rglob("*.json"):
            stat = sf.stat()
            age = time.time() - stat.st_mtime
            if age > 86400:
                continue
            if age < 300:
                status = "active"
            elif age < 3600:
                status = "recent"
            else:
                status = "idle"
            sessions.append({
                "agent": "opencode",
                "host": "local",
                "host_name": LOCAL_HOST["name"],
                "host_icon": LOCAL_HOST["icon"],
                "host_machine": LOCAL_HOST["host"],
                "project": sf.parent.name,
                "session_id": sf.stem,
                "status": status,
                "age_seconds": int(age),
                "modified": int(stat.st_mtime),
                "preview": "",
                "size_kb": int(stat.st_size / 1024),
            })

    # Also check tmux for opencode sessions
    tmux_out = _run_local(["tmux", "ls", "-F", "#{session_name}|#{session_created}"])
    for line in tmux_out.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, created = parts[0], parts[1]
        if "opencode" in name.lower():
            age = time.time() - (int(created) if created.isdigit() else 0)
            sessions.append({
                "agent": "opencode",
                "host": "local",
                "host_name": "Linux GPU",
                "project": name,
                "session_id": name,
                "status": "active" if age < 300 else "recent",
                "age_seconds": int(age),
                "modified": int(created) if created.isdigit() else 0,
                "preview": "",
                "size_kb": 0,
            })

    sessions.sort(key=lambda x: x["modified"], reverse=True)
    return sessions


def _get_opencode_sessions_remote(host: dict) -> list[dict]:
    """Get OpenCode sessions from remote host."""
    home = host.get("home", "~")
    cmd = (
        f'find {home}/.local/share/opencode {home}/.config/opencode -name "*.json" '
        f'-mtime -1 2>/dev/null | head -10'
    )
    result = _run_remote(host["host"], host["user"], cmd)
    if not result or result == "__TIMEOUT__":
        return []

    sessions = []
    for line in result.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("/")
        session_file = parts[-1]
        project_name = parts[-2] if len(parts) >= 2 else "unknown"
        session_id = session_file.replace(".json", "")

        time_cmd = f'stat -c %Y "{line}" 2>/dev/null || stat -f %m "{line}" 2>/dev/null'
        time_result = _run_remote(host["host"], host["user"], time_cmd, timeout=5)
        mtime = int(time_result) if time_result and time_result.isdigit() else int(time.time())
        age = time.time() - mtime

        if age < 300:
            status = "active"
        elif age < 3600:
            status = "recent"
        else:
            status = "idle"

        sessions.append({
            "agent": "opencode",
            "host": host["host"],
            "host_name": host["name"],
            "project": project_name,
            "session_id": session_id,
            "status": status,
            "age_seconds": int(age),
            "modified": mtime,
            "preview": "",
            "size_kb": 0,
        })

    sessions.sort(key=lambda x: x["modified"], reverse=True)
    return sessions


def get_opencode_sessions() -> list[dict]:
    """Get all OpenCode sessions across all hosts."""
    sessions = []
    sessions.extend(_get_opencode_sessions_local())
    # Remote hosts probed CONCURRENTLY (was a serial for-loop).
    sessions.extend(_probe_hosts(_get_opencode_sessions_remote, _online_remote_hosts()))
    return sessions


# --- Tmux Sessions (for interactive agent sessions) ---

def get_tmux_sessions() -> list[dict]:
    """Get local tmux sessions with details."""
    out = _run_local(["tmux", "ls", "-F",
                      "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}|#{session_path}"])
    sessions = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name = parts[0]
        created = parts[1] if parts[1].isdigit() else "0"
        windows = parts[2] if parts[2].isdigit() else "0"
        attached = parts[3] == "1"
        path = parts[4] if len(parts) > 4 else ""

        agent_type = "unknown"
        origin = "user"  # default: user-launched
        if name.startswith("hermes-"):
            agent_type = "claude-code"
            origin = "hermes"  # Hermes-launched
        elif name.startswith("task-") or name.startswith("claude-"):
            agent_type = "claude-code"
            origin = "user"  # user-launched via `c` command
        elif name.startswith("codex-"):
            agent_type = "codex"
        elif "opencode" in name.lower():
            agent_type = "opencode"
        elif name in ("hermes",):
            agent_type = "hermes"
            origin = "hermes"

        sessions.append({
            "name": name,
            "display_name": name,
            "agent_type": agent_type,
            "origin": origin,
            "created": int(created),
            "windows": int(windows),
            "attached": attached,
            "path": path,
            "status": "active",
        })
    return sessions


# --- Remote tmux sessions ---

def get_remote_tmux(host: dict) -> list[dict]:
    """Get tmux sessions from a remote host (as the tmux-owning login user)."""
    cmd = 'tmux ls -F "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}|#{session_path}" 2>/dev/null'
    result = _run_remote(host["host"], host.get("tmux_user") or host["user"], cmd)
    if not result or result == "__TIMEOUT__":
        return []

    sessions = []
    for line in result.splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name = parts[0]
        created = parts[1] if parts[1].isdigit() else "0"
        windows = parts[2] if parts[2].isdigit() else "0"
        attached = parts[3] == "1"
        path = parts[4] if len(parts) > 4 else ""

        agent_type = "unknown"
        origin = "user"
        if name.startswith("hermes-"):
            agent_type = "claude-code"
            origin = "hermes"
        elif name.startswith("task-") or name.startswith("claude-"):
            agent_type = "claude-code"
            origin = "user"
        elif name.startswith("codex-"):
            agent_type = "codex"
        elif "opencode" in name.lower():
            agent_type = "opencode"

        sessions.append({
            "name": name,
            "display_name": name,
            "agent_type": agent_type,
            "origin": origin,
            "created": int(created),
            "windows": int(windows),
            "attached": attached,
            "path": path,
            "host": host["host"],
            "host_name": host["name"],
            "status": "active",
        })
    return sessions


# --- Link transcripts to their tmux session (the names `cs` / `c ls` show) ---

def _enrich_claude_with_tmux(claude: list[dict], tmux_local: list[dict],
                             tmux_remote: list[dict]) -> None:
    """Attach the tmux session name (e.g. `claude-dashboard`) to each Claude Code
    transcript, matched by working directory — the same `claude-*` sessions the
    `cs` / `c ls` command lists. The link key is the cwd: a transcript knows its
    cwd; a tmux session knows `#{pane_current_path}`.

    When several `claude-*` sessions share one cwd (e.g. `claude-cmd` and
    `claude-orchestrator` both in .../orchestratormaxxing), cwd alone can't say which
    transcript is which, so we assign greedily newest-transcript→newest-session
    and flag `tmux_ambiguous` (an exact link would need per-process lsof; see the
    plan). A transcript with no live `claude-*` session in its cwd keeps its
    directory-basename display name. Mutates `claude` in place."""
    def norm(p):
        # casefold so a macOS case-insensitive path (/Users/x/Dev) still matches
        # a tmux pane path recorded as /Users/x/dev.
        return (p or "").rstrip("/").casefold()

    # claude-* tmux sessions grouped by host key ('local' | <remote host>).
    tmux_by_host: dict = {}
    for s in tmux_local:
        if str(s.get("name", "")).startswith("claude-") and s.get("path"):
            tmux_by_host.setdefault("local", []).append(s)
    for s in tmux_remote:
        if str(s.get("name", "")).startswith("claude-") and s.get("path"):
            tmux_by_host.setdefault(s.get("host"), []).append(s)

    def host_key(c):
        return "local" if c.get("host") == "local" else c.get("host")

    for hk, tmuxes in tmux_by_host.items():
        # Group the host's sessions by cwd so we know when a cwd is contested.
        by_cwd: dict = {}
        for t in tmuxes:
            by_cwd.setdefault(norm(t.get("path")), []).append(t)

        for cwd, tlist in by_cwd.items():
            # Candidate transcripts for this cwd: exact cwd match, else the
            # encoded-path match (robust for remote, where the real cwd isn't
            # parsed): tmux path `/a/b` encodes to the project dir `-a-b`.
            enc = cwd.replace("/", "-")
            cands = [c for c in claude if host_key(c) == hk
                     and (norm(c.get("cwd")) == cwd
                          or (c.get("project_dir") or "").casefold() == enc)]
            if not cands:
                continue
            cands.sort(key=lambda x: -(x.get("modified") or 0))          # newest first
            ambiguous = len(tlist) > 1
            tlist.sort(key=lambda x: (0 if x.get("attached") else 1,     # attached first
                                      -(x.get("created") or 0)))         # then newest
            ci = 0
            for t in tlist:
                if ci >= len(cands):
                    break
                c = cands[ci]
                ci += 1
                # SEMANTIC SPLIT (live bug: this line used to OVERWRITE the
                # registry's tmux_attached with tmux's client-attached flag,
                # zeroing every detached session):
                #   tmux_attached        = a live TERMINAL exists (registry-
                #                          authoritative; only fill gaps here)
                #   tmux_client_attached = a human is viewing the pane
                if not c.get("tmux_session"):
                    c["tmux_session"] = t.get("name")
                    c["tmux_attached"] = True
                c["tmux_client_attached"] = bool(t.get("attached"))
                c["tmux_ambiguous"] = ambiguous


# --- Unified API ---

# get_all_sessions is polled by /api/tasks, /api/sessions and the summary widget;
# the underlying scan shells out to tailscale + SSH, so serve from a short TTL cache.
_SESSIONS_CACHE = {"ts": 0.0, "data": None}
SESSIONS_CACHE_TTL = 20
# The scan shells out to `tailscale ssh` per remote host (SSH_TIMEOUT each), so a
# cold recompute can take 10-20s. The old cache recomputed SYNCHRONOUSLY on TTL
# expiry — and since every caller is an `async def` handler invoking this on the
# event loop, one stale-cache request froze the WHOLE dashboard for the entire
# scan (a 1ms cycle-create POST would block 7s+ behind it). Fix:
# serve-stale-while-revalidate — return the cached snapshot instantly and refresh
# in a daemon thread, so no request ever blocks on the scan (only the one-time
# cold start does). Data is at most TTL+scan seconds stale, which is fine here.
_REFRESH_LOCK = threading.Lock()
_REFRESHING = {"on": False}


def _refresh_sessions_cache() -> None:
    try:
        data = _compute_all_sessions()
        _SESSIONS_CACHE["data"] = data          # atomic ref swap (GIL) — readers safe
        _SESSIONS_CACHE["ts"] = time.time()
    finally:
        _REFRESHING["on"] = False


def get_all_sessions(force: bool = False) -> dict:
    now = time.time()
    have = _SESSIONS_CACHE["data"] is not None
    fresh = have and now - _SESSIONS_CACHE["ts"] < SESSIONS_CACHE_TTL
    if not force and fresh:
        return _SESSIONS_CACHE["data"]
    # Cold cache or explicit force: nothing to serve, so compute synchronously.
    if force or not have:
        _refresh_sessions_cache()
        return _SESSIONS_CACHE["data"]
    # Warm but stale: hand back the last snapshot NOW and revalidate off-thread.
    if not _REFRESHING["on"]:
        with _REFRESH_LOCK:
            if not _REFRESHING["on"]:
                _REFRESHING["on"] = True
                threading.Thread(target=_refresh_sessions_cache, daemon=True).start()
    return _SESSIONS_CACHE["data"]


def cache_meta() -> dict:
    """Freshness of the sessions snapshot the last get_all_sessions() served — so
    the UI can show cache-vs-fresh. `age_seconds` = time since that scan actually
    ran; `fresh` = still within TTL; `refreshing` = a background revalidation is
    in flight (serve-stale). Read-only; never mutates the cached object."""
    ts = _SESSIONS_CACHE["ts"]
    age = int(time.time() - ts) if ts else None
    return {
        "scanned_at": ts or None,
        "age_seconds": max(0, age) if age is not None else None,
        "ttl_seconds": SESSIONS_CACHE_TTL,
        "fresh": age is not None and age < SESSIONS_CACHE_TTL,
        "refreshing": _REFRESHING["on"],
    }


# --- Transcript pruning (hidden-set, NOT deletion) -----------------------------
# `orchestrator sessions --prune-transcripts` hides old transcript-only rows
# from the listing. The transcript FILES are never touched (they hold resume/
# audit history); pruning writes the session ids to a persistent hidden-set,
# and the enumeration filter drops them — UNTIL a hidden session shows new
# activity (transcript mtime past its prune point) or gains a terminal, at
# which point it auto-unhides. Pruning is display hygiene, never data loss.
HIDDEN_SESSIONS_FILE = Path.home() / ".hermes" / "hidden-sessions.json"


def _load_hidden() -> dict:
    try:
        return json.loads(HIDDEN_SESSIONS_FILE.read_text())
    except Exception:
        return {}


def _save_hidden(hidden: dict) -> None:
    try:
        HIDDEN_SESSIONS_FILE.parent.mkdir(exist_ok=True)
        HIDDEN_SESSIONS_FILE.write_text(json.dumps(hidden, indent=1))
    except Exception:
        pass


def _filter_hidden(claude: list) -> list:
    """Drop hidden sessions; auto-unhide any that woke up (new activity or a
    live terminal). Mutates the hidden-set file when an unhide happens."""
    hidden = _load_hidden()
    if not hidden:
        return claude
    kept, changed = [], False
    for sess in claude:
        h = hidden.get(str(sess.get("session_id")))
        if not h:
            kept.append(sess)
        elif sess.get("tmux_attached") or (sess.get("modified") or 0) > h.get("modified_at_prune", 0):
            del hidden[str(sess["session_id"])]
            changed = True
            kept.append(sess)
    if changed:
        _save_hidden(hidden)
    return kept


def prune_transcript_sessions(hours: float = 48.0) -> dict:
    """Hide transcript-only sessions idle longer than `hours`. Never touches a
    session with a live terminal, and never deletes a transcript file."""
    data = get_all_sessions()
    now = int(time.time())
    cutoff = now - int(hours * 3600)
    hidden = _load_hidden()
    pruned = []
    for sess in data.get("claude_code", []):
        sid = str(sess.get("session_id"))
        if sess.get("tmux_attached") or sid in hidden:
            continue
        modified = sess.get("modified") or 0
        if modified < cutoff:
            hidden[sid] = {"pruned_at": now, "modified_at_prune": modified,
                           "host": sess.get("host", "local"),
                           "project": sess.get("project")}
            pruned.append(sid)
    if pruned:
        _save_hidden(hidden)
        _SESSIONS_CACHE["ts"] = 0.0  # bust: next read reflects the prune
    return {"pruned": len(pruned), "ids": pruned, "hours": hours,
            "hidden_total": len(hidden)}


def _compute_all_sessions() -> dict:
    """Get all sessions across all hosts and agents.
    Claude Code sessions are prioritized (especially those created with 'c' command).
    """
    hosts_online = _online_remote_hosts()
    # The independent scan categories each do their own remote I/O, so run them
    # CONCURRENTLY (and inside claude/opencode the hosts fan out too). Net: the
    # scan is bounded by the slowest single probe, not the sum of all of them.
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_claude = ex.submit(get_claude_code_sessions)
        f_opencode = ex.submit(get_opencode_sessions)
        f_tmux_local = ex.submit(get_tmux_sessions)
        f_tmux_remote = ex.submit(_probe_hosts, get_remote_tmux, hosts_online)
        claude = f_claude.result()
        opencode = f_opencode.result()
        tmux_local = f_tmux_local.result()
        tmux_remote = f_tmux_remote.result()

    # Link each transcript to its tmux session name (what `cs` / `c ls` shows).
    claude = _filter_hidden(claude)
    _enrich_claude_with_tmux(claude, tmux_local, tmux_remote)

    # Add priority scoring
    for s in claude:
        s["priority"] = 10
        sid = s.get("session_id", "") or ""
        project = str(s.get("project", "")).lower()
        
        # Strong boost for sessions created with the 'c' command
        if sid.startswith("claude-"):
            s["priority"] = 25
            s["source"] = "claude-cmd"
        elif re.search(r"claude[-_]", sid, re.IGNORECASE):
            s["priority"] = 20
            s["source"] = "claude"
        elif "claude" in project:
            s["priority"] = 15
            s["source"] = "project"
        else:
            s["source"] = "other"

    # Also score tmux sessions: claude-<project> sessions are from the `c` command
    for s in tmux_local + tmux_remote:
        name = s.get("name", "")
        if name.startswith("claude-"):
            s["priority"] = 25
            s["source"] = "claude-cmd"
        elif name.startswith("task-"):
            s["priority"] = 20
            s["source"] = "claude"
        else:
            s["priority"] = 0
            s["source"] = "other"


    # --- Orchestration enrichment: role + feature + context-fullness (features
    # 1 & 4). Role comes from the registry first, else parsed from the tmux name
    # (`claude-<proj>-<role>`); context estimate flags sessions due for /compact.
    try:
        from . import orchestration as _orch
        _meta = _orch.all_session_meta()
    except Exception:
        _orch, _meta = None, {}
    for s in claude:
        sid = str(s.get("session_id", "") or "")
        name = str(s.get("display_name", "") or "")
        m = _meta.get(sid) or _meta.get(name) or {}
        role = m.get("role")
        if not role and _orch:
            role = _orch.role_from_name(sid) or _orch.role_from_name(name)
        s["role"] = role
        s["feature"] = m.get("feature")
        s["tag"] = m.get("tag")
        s["auto_compact"] = bool(m.get("auto_compact"))
        s["auto_abort"] = bool(m.get("auto_abort", 1))
        if _orch:
            s.update(_orch.context_estimate(s.get("size_kb")))
    for s in tmux_local + tmux_remote:
        nm = s.get("name", "")
        m = _meta.get(nm) or {}
        s["role"] = m.get("role") or (_orch.role_from_name(nm) if _orch else None)
        s["feature"] = m.get("feature")

    # Add human-readable last activity for UI
    import datetime as _dt
    for s in claude + opencode:
        if "modified" in s and s["modified"]:
            try:
                dt = _dt.datetime.fromtimestamp(s["modified"])
                s["last_active"] = dt.strftime("%H:%M")
                s["last_active_relative"] = "just now" if (int(_dt.datetime.now().timestamp()) - s["modified"]) < 120 else dt.strftime("%H:%M")
            except Exception:
                s["last_active"] = "—"

    for s in tmux_local + tmux_remote:
        if "created" in s and s["created"]:
            try:
                dt = _dt.datetime.fromtimestamp(s["created"])
                s["last_active"] = dt.strftime("%H:%M")
                s["last_active_relative"] = "just now" if (int(_dt.datetime.now().timestamp()) - s["created"]) < 120 else dt.strftime("%H:%M")
            except Exception:
                s["last_active"] = "—"

    for s in opencode:
        s["priority"] = 5

    # Sort: higher priority first, then by recency
    claude_sorted = sorted(claude, key=lambda x: (-x.get("priority", 0), -x.get("modified", 0)))
    opencode_sorted = sorted(opencode, key=lambda x: (-x.get("priority", 0), -x.get("modified", 0)))


    # Summary for dashboard header / visibility
    claude_active = [s for s in claude if s.get("status") == "active"]
    tmux_c_cmd = [s for s in (tmux_local + tmux_remote) if s.get("source") == "claude-cmd"]
    claude_summary = {
        "total": len(claude),
        "active": len(claude_active),
        "with_c_command": len([s for s in claude if s.get("source") == "claude-cmd"]) + len(tmux_c_cmd),
        "last_updated": __import__("datetime").datetime.now().strftime("%H:%M:%S")
    }

    return {
        "claude_code": claude_sorted,
        "claude_code_active": [s for s in claude_sorted if s.get("status") in ("active", "recent")],
        "claude_code_idle": [s for s in claude_sorted if s.get("status") == "idle"],
        "opencode": opencode_sorted,
        "opencode_active": [s for s in opencode_sorted if s.get("status") in ("active", "recent")],
        "opencode_idle": [s for s in opencode_sorted if s.get("status") == "idle"],
        "tmux_local": tmux_local,
        "tmux_remote": tmux_remote,
        "hosts": {
            "local": {"name": LOCAL_HOST["name"], "icon": LOCAL_HOST["icon"], "machine": LOCAL_HOST["host"], "online": True},
            **{h["host"]: {"name": h["name"], "icon": h.get("icon", "🖥️"), "machine": h["host"], "online": h in hosts_online}
               for h in REMOTE_HOSTS}
        },
        "total_active": sum(1 for s in claude + opencode if s["status"] == "active"),
        "total_sessions": len(claude) + len(opencode),
        "timestamp": int(time.time()),
        "last_updated_human": __import__("datetime").datetime.now().strftime("%H:%M:%S"),
        "claude_summary": claude_summary,
    }
def revive_session(host: str, session_name: str) -> dict:
    """Revive a KNOWN session on its origin machine: recreate its tmux session
    and relaunch claude — resuming the original conversation (--resume) in the
    original cwd when the name is a transcript UUID.

    GUARD: only a name that is (a) a `claude-*` or `hermes-*` convention session
    or (b) a session id with an existing transcript may be revived. Anything else
    is rejected — an unvalidated name would spawn a fresh
    --dangerously-skip-permissions agent for ANY string a client sends
    (found live: a blind API probe created a privileged session named
    'zzz-nope')."""
    is_named = session_name.startswith("claude-") or session_name.startswith("hermes-")
    cwd = None if is_named else _transcript_cwd(host, session_name)
    has_transcript = (not is_named) and (
        cwd is not None or (host == "local" and _local_session_file(session_name) is not None))
    if not is_named and not has_transcript:
        return {"status": "error",
                "error": f"unknown session '{session_name}' — revive only resurrects "
                         "sessions with an existing transcript (or claude-*/hermes-* sessions)"}

    claude_cmd = ["claude", "--dangerously-skip-permissions"]
    if has_transcript:
        claude_cmd += ["--resume", session_name]

    if host == "local":
        try:
            check = subprocess.run(["tmux", "has-session", "-t", session_name],
                                   capture_output=True, text=True, timeout=3)
            if check.returncode == 0:
                return {"status": "exists", "message": f"Session {session_name} already running"}
            tmux_cmd = ["tmux", "new-session", "-d", "-s", session_name]
            if cwd:
                tmux_cmd += ["-c", cwd]
            subprocess.run(tmux_cmd + claude_cmd, capture_output=True, text=True, timeout=10)
            return {"status": "revived", "host": "local", "session": session_name,
                    "resumed": has_transcript, "cwd": cwd}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    host_config = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
    if not host_config:
        return {"status": "error", "error": "Host not found"}
    escaped = session_name.replace("'", "'\\''")
    cd_part = f'-c "{cwd}" ' if cwd else ""
    remote_claude = " ".join(claude_cmd)
    cmd = f'tmux new-session -d -s "{escaped}" {cd_part}"{remote_claude}" 2>&1'
    result = _run_remote(host_config["host"], host_config["user"], cmd, timeout=15)
    if "__TIMEOUT__" in result:
        return {"status": "error", "error": "SSH timeout"}
    return {"status": "revived", "host": host, "session": session_name,
            "resumed": has_transcript, "cwd": cwd}


def create_session(host: str, name: str, cwd: Optional[str] = None) -> dict:
    """Spin up a FRESH Claude Code tmux session from the UI (+ New Session).

    The name is normalized to the `claude-*` convention so the whole scan +
    revive machinery recognizes it (same guard rationale as revive_session — a
    `claude-*` name is what marks a session as ours). Launches
    `claude --dangerously-skip-permissions` in a detached tmux session on the
    chosen host. Refuses to clobber an existing session of the same name."""
    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (name or "").strip())
    slug = slug.strip("-") or "session"
    # Hermes-launched sessions use `hermes-` prefix; user sessions keep `claude-`.
    # This is how the dashboard distinguishes who owns a session.
    if slug.startswith("claude-"):
        session_name = slug  # keep as-is if already has a prefix
    elif slug.startswith("hermes-"):
        session_name = slug
    else:
        session_name = f"hermes-{slug}"  # default: Hermes-origin
    claude_cmd = ["claude", "--dangerously-skip-permissions"]

    if host == "local":
        try:
            check = subprocess.run(["tmux", "has-session", "-t", session_name],
                                   capture_output=True, text=True, timeout=3)
            if check.returncode == 0:
                return {"status": "exists",
                        "message": f"Session {session_name} already running"}
            tmux_cmd = ["tmux", "new-session", "-d", "-s", session_name]
            if cwd:
                tmux_cmd += ["-c", cwd]
            subprocess.run(tmux_cmd + claude_cmd, capture_output=True, text=True, timeout=10)
            return {"status": "created", "host": "local", "session": session_name}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    host_config = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
    if not host_config:
        return {"status": "error", "error": "Host not found"}
    escaped = session_name.replace("'", "'\\''")
    cd_part = f'-c "{cwd}" ' if cwd else ""
    remote_claude = " ".join(claude_cmd)
    # has-session guard first so we don't silently double-launch on the remote.
    guard = (f'tmux has-session -t "{escaped}" 2>/dev/null && echo __EXISTS__ || '
             f'tmux new-session -d -s "{escaped}" {cd_part}"{remote_claude}" 2>&1')
    result = _run_remote(host_config["host"], host_config["user"], guard, timeout=15)
    if "__TIMEOUT__" in result:
        return {"status": "error", "error": "SSH timeout"}
    if "__EXISTS__" in result:
        return {"status": "exists", "message": f"Session {session_name} already running"}
    return {"status": "created", "host": host, "session": session_name}


def kill_session(host: str, session_name: str) -> dict:
    """Kill a live tmux session on its origin machine — the teeth behind
    auto-abort (feature 6). Resolves a jsonl session-id to its live tmux target
    (a session named directly, or the tmux pane sharing its cwd) before killing,
    so aborting by session_id works too. Best-effort: a session that's already
    gone is a success, not an error."""
    if host == "local":
        target = session_name
        if not subprocess.run(["tmux", "has-session", "-t", session_name],
                              capture_output=True, timeout=3).returncode == 0:
            resolved = _resolve_local_tmux_target(session_name)
            if resolved:
                target = resolved
            else:
                return {"status": "gone", "session": session_name,
                        "detail": "no live tmux session to kill"}
        try:
            r = subprocess.run(["tmux", "kill-session", "-t", target],
                              capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return {"status": "killed", "host": "local", "session": target}
            return {"status": "gone", "session": target, "detail": r.stderr.strip()}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    host_config = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
    if not host_config:
        return {"status": "error", "error": "Host not found"}
    escaped = session_name.replace("'", "'\\''")
    cmd = f'tmux kill-session -t "{escaped}" 2>&1 || true'
    result = _run_remote(host_config["host"], host_config["user"], cmd, timeout=10)
    if "__TIMEOUT__" in result:
        return {"status": "error", "error": "SSH timeout"}
    return {"status": "killed", "host": host, "session": session_name}


def _local_session_file(session_name: str) -> Optional[Path]:
    """Locate the .jsonl transcript for a local session UUID, if present."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None
    for proj_dir in claude_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        f = proj_dir / f"{session_name}.jsonl"
        if f.exists():
            return f
    return None


# The tmux user option carrying the Claude Code session UUID — stamped by the
# SessionStart hook (hooks/tmux-stamp.sh) the moment Claude starts inside a
# tmux pane, on EVERY machine. THE persistent UUID→tmux mapping; the cwd
# heuristic is only the legacy fallback for sessions that predate stamping.
TMUX_UUID_OPTION = "@claude-session-id"
# Delimiter is '|' — tmux ESCAPES non-printable bytes in -F output (a \x1f
# arrives as the literal string '\037'), so control-char delimiters silently
# break the split (live bug caught on first deploy).
_TMUX_REG_FMT = "#{session_name}|#{session_path}|#{" + TMUX_UUID_OPTION + "}"


def _tmux_registry(host: str) -> list[dict]:
    """Every live tmux session on `host` as {name, path, uuid} in ONE call.
    The ONLY host-aware part is the transport (local subprocess vs Tailscale
    SSH); the format string, parsing, and semantics are identical — the
    consistent-abstraction requirement."""
    if host == "local":
        out = _run_local(["tmux", "ls", "-F", _TMUX_REG_FMT])
    else:
        cfg = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
        if not cfg:
            return []
        out = _run_remote(cfg["host"], cfg.get("tmux_user") or cfg["user"],
                          f"tmux ls -F '{_TMUX_REG_FMT}' 2>/dev/null || true", timeout=8)
        if not out or out == "__TIMEOUT__":
            return []
    reg = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 2:      # error text ("no server running") never splits
            reg.append({"name": parts[0], "path": parts[1],
                        "uuid": (parts[2] or None) if len(parts) > 2 else None})
    return reg


def _transcript_cwd(host: str, session_id: str) -> Optional[str]:
    """The cwd recorded in a session's transcript — the key for the legacy
    fallback. Local reads the jsonl directly; remote does one SSH grep."""
    if host == "local":
        session_file = _local_session_file(session_id)
        return _extract_cwd_from_jsonl(session_file) if session_file else None
    cfg = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
    if not cfg:
        return None
    home = cfg.get("home", "~")
    cmd = (f'f=$(find {home}/.claude/projects -name "{session_id}.jsonl" 2>/dev/null | head -1); '
           f'[ -n "$f" ] && head -5 "$f" | grep -o \'"cwd":"[^"]*"\' | head -1 || true')
    out = _run_remote(cfg["host"], cfg["user"], cmd, timeout=8)
    if out and out != "__TIMEOUT__" and '"cwd":"' in out:
        return out.split('"cwd":"', 1)[1].rstrip('"')
    return None


def resolve_tmux_target(host: str, session_name: str) -> Optional[str]:
    """THE resolver: any session identifier → the live tmux session that owns
    it, on any host. NO host special-casing here — host only parameterizes the
    two data accessors (_tmux_registry / _transcript_cwd). Authority order:
      1. a tmux session named exactly that (c-command sessions);
      2. the STAMPED registry (@claude-session-id == UUID) — persistent,
         survives idleness;
      3. legacy cwd fallback, UN-GATED (no freshness requirement), unstamped
         sessions preferred (a stamped one already belongs to another UUID)."""
    reg = _tmux_registry(host)
    for r in reg:
        if r["name"] == session_name:
            return session_name
    for r in reg:
        if r["uuid"] == session_name:
            return r["name"]
    cwd = _transcript_cwd(host, session_name)
    if cwd:
        cands = [r for r in reg if r["path"] == cwd]
        unstamped = [r for r in cands if not r["uuid"]]
        pick = unstamped or cands
        if pick:
            return pick[0]["name"]
    return None


def _find_local_tmux_for_cwd(cwd: str) -> Optional[str]:
    """Legacy-named helper (kept for callers): un-gated cwd match, unstamped
    preferred."""
    if not cwd:
        return None
    cands = [r for r in _tmux_registry("local") if r["path"] == cwd]
    unstamped = [r for r in cands if not r["uuid"]]
    pick = unstamped or cands
    return pick[0]["name"] if pick else None


def _resolve_session_source(host: str, session_name: str, lines: int = 50) -> tuple:
    """Route a session id to its best readable source.

    Returns (kind, payload):
      - ("terminal", <captured pane text>)  live tmux pane
      - ("transcript", <raw .jsonl text>)   Claude Code transcript (to be parsed)
      - ("empty", "")                       nothing found
    """
    if host == "local":
        # Sessions created with the `c` command are named for their session id.
        # THE resolver, every identifier shape (named tmux → stamped registry →
        # un-gated cwd fallback). The old <120s 'hot' requirement is GONE:
        # idleness no longer demotes a live terminal to transcript-only.
        target = resolve_tmux_target("local", session_name)
        if target:
            live = _run_local(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
            if live and "no server running" not in live and "can't find session" not in live:
                return ("terminal", live)
        session_file = _local_session_file(session_name)
        if session_file is not None:
            try:
                return ("transcript", session_file.read_text())
            except Exception:
                return ("empty", "")
        return ("empty", "")

    host_config = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
    if not host_config:
        return ("empty", "")
    # SAME resolver for remote (the consistent abstraction): a UUID resolves
    # through the stamped registry / cwd fallback over SSH before capture.
    target = resolve_tmux_target(host, session_name) or session_name
    cmd = f'tmux capture-pane -t "{target}" -p -S -{lines} 2>/dev/null'
    tmux_user = host_config.get("tmux_user") or host_config["user"]
    result = _run_remote(host_config["host"], tmux_user, cmd, timeout=10)
    if result and result != "__TIMEOUT__" and "no server" not in result and "can't find" not in result:
        return ("terminal", result)
    # Fall back to the .jsonl transcript, tailed over SSH.
    home = host_config.get("home", "~")
    cmd = f'find {home}/.claude/projects -name "{session_name}.jsonl" 2>/dev/null | head -1'
    file_path = _run_remote(host_config["host"], host_config["user"], cmd, timeout=8)
    if file_path and file_path != "__TIMEOUT__":
        cmd = f'tail -{lines * 2} "{file_path}" 2>/dev/null'
        raw = _run_remote(host_config["host"], host_config["user"], cmd, timeout=10)
        if raw and raw != "__TIMEOUT__":
            return ("transcript", raw)
    return ("empty", "")


def get_session_view(host: str, session_name: str, lines: int = 50) -> dict:
    """Structured session view for the dashboard modal.

    - kind "terminal": `output` is a live pane capture (render as a terminal).
    - kind "transcript": `messages` is a list of {role, text, tool} turns
      (render as a conversation); `output` keeps the flat text for fallback.
    """
    kind, payload = _resolve_session_source(host, session_name, lines)
    if kind == "terminal":
        return {"kind": "terminal", "output": payload, "messages": []}
    if kind == "transcript":
        return {
            "kind": "transcript",
            "output": _format_jsonl_transcript(payload, lines),
            "messages": _structured_transcript(payload, lines),
        }
    return {"kind": "empty", "output": "", "messages": []}


def get_session_output(host: str, session_name: str, lines: int = 50) -> str:
    """Flat-text session output (kept for callers that only need a string)."""
    return get_session_view(host, session_name, lines)["output"]


def _read_jsonl_transcript(session_file: Path, lines: int = 50) -> str:
    """Read the last N meaningful messages from a Claude Code .jsonl transcript."""
    try:
        text = session_file.read_text()
    except Exception as e:
        return f"Error reading transcript: {e}"
    return _format_jsonl_transcript(text, lines)


def _structured_transcript(text: str, lines: int = 50) -> list:
    """Parse raw Claude Code .jsonl transcript text into a list of typed turns
    for the conversation UI. Each item: {role, text, tool}.
      role: user | assistant | tool | result
    Consecutive tool calls are kept as their own compact rows."""
    try:
        all_lines = text.strip().split("\n")
    except Exception:
        return []
    msgs = []

    def add(role, text_val="", tool=""):
        cleaned = _clean_message_text(text_val) if text_val else ""
        if text_val and not cleaned and role != "tool":
            return
        msgs.append({"role": role, "text": (cleaned or "")[:2000], "tool": tool})

    for line in all_lines[-(lines * 3):]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_type = data.get("type", "")
        if msg_type == "user":
            content = data.get("message", {}).get("content", "")
            if isinstance(content, str) and content.strip():
                if not content.startswith("[{"):
                    add("user", content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        add("user", item["text"])
        elif msg_type == "assistant":
            content = data.get("message", {}).get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            add("assistant", item["text"])
                        elif item.get("type") == "tool_use":
                            add("tool", tool=item.get("name", "tool"))
        elif msg_type == "result":
            result_text = data.get("result", "")
            if result_text:
                add("result", result_text)

    return msgs[-(lines):]


def _format_jsonl_transcript(text: str, lines: int = 50) -> str:
    """Format raw Claude Code .jsonl transcript text (local file or remote tail)
    into the readable 👤/🤖/🔧 message stream shown in the session modal."""
    try:
        all_lines = text.strip().split("\n")
        # Parse last `lines*2` entries (some are metadata)
        output = []
        for line in all_lines[-(lines * 2):]:
            try:
                data = json.loads(line)
                msg_type = data.get("type", "")
                
                if msg_type == "user":
                    msg = data.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        # Skip tool results, just show the prompt
                        if not content.startswith("[{"):
                            cleaned = _clean_message_text(content)
                            if cleaned:
                                output.append(f"👤 {cleaned[:200]}")
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                cleaned = _clean_message_text(item["text"])
                                if cleaned:
                                    output.append(f"👤 {cleaned[:200]}")

                elif msg_type == "assistant":
                    msg = data.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("type") == "text":
                                    cleaned = _clean_message_text(item["text"])
                                    if cleaned:
                                        output.append(f"🤖 {cleaned[:300]}")
                                elif item.get("type") == "tool_use":
                                    tool = item.get("name", "unknown")
                                    output.append(f"🔧 {tool}")

                elif msg_type == "result":
                    result_text = data.get("result", "")
                    if result_text:
                        cleaned = _clean_message_text(result_text)
                        if cleaned:
                            output.append(f"✅ {cleaned[:200]}")
            except json.JSONDecodeError:
                pass
        
        return "\n".join(output[-lines:]) if output else "(No output in transcript)"
    except Exception as e:
        return f"Error reading transcript: {e}"


def _resolve_local_tmux_target(session_name: str) -> Optional[str]:
    """Back-compat wrapper — the real logic lives in resolve_tmux_target."""
    return resolve_tmux_target("local", session_name)


def send_to_session(host: str, session_name: str, text: str) -> dict:
    """Send a prompt to ANY session's terminal on any host: ONE resolver
    (named tmux → stamped @claude-session-id registry → cwd fallback), then
    host-appropriate transport for the send itself."""
    import shutil

    host_config = None
    if host != "local":
        host_config = next((h for h in REMOTE_HOSTS if h["host"] == host), None)
        if host_config is None:
            return {
                "status": "error",
                "confirmed_executing": False,
                "error": "unknown_host",
            }

    target = resolve_tmux_target(host, session_name)
    if not target:
        return {"status": "error",
                "error": "No live terminal for this session (transcript only — revive it)."}
    if host == "local":
        try:
            tmux_send = shutil.which("tmux-send") or str(Path.home() / ".local/bin/tmux-send")
            result = subprocess.run(
                [tmux_send, target, text],
                capture_output=True,
                text=True,
                timeout=35,
            )
            if result.returncode == 0:
                return {
                    "status": "sent",
                    "confirmed_executing": True,
                    "host": "local",
                    "session": target,
                }
            return {
                "status": "error",
                "confirmed_executing": False,
                "error": result.stderr.strip()[-500:],
            }
        except Exception as e:
            return {
                "status": "error",
                "confirmed_executing": False,
                "error": str(e),
            }
    tmux_user = host_config.get("tmux_user") or host_config["user"]
    remote_cmd = (f'"$HOME/.local/bin/tmux-send" {shlex.quote(target)} '
                  f'{shlex.quote(text)}')
    rc, stdout, stderr = _run_remote_rc(
        host_config["host"], tmux_user, remote_cmd, timeout=35
    )
    if rc == 0:
        return {
            "status": "sent",
            "confirmed_executing": True,
            "host": host,
            "session": target,
        }
    return {
        "status": "error",
        "confirmed_executing": False,
        "error": (stderr or stdout).strip()[-500:],
    }
