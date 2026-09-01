"""Day Review: reconstruct one local day from agent, task, git, and cron evidence.

This file is also deployed verbatim to ``~/.hermes/scripts/day-review.py``.  It
uses only the Python standard library so the Hermes scheduler can run it without
the dashboard virtualenv.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date as Date, datetime, time as Time, timedelta
from pathlib import Path
from typing import Any, Iterable


DEFAULT_HOME = Path.home()
DEFAULT_DEV_ROOT = DEFAULT_HOME / "dev"
DEFAULT_REVIEW_STORE = DEFAULT_HOME / ".hermes" / "memories" / "day-reviews.jsonl"
DEFAULT_TELEGRAM = (os.environ.get("DAY_REVIEW_TELEGRAM")
                    or os.environ.get("ORCHESTRATORMAXXING_NOTIFY_TARGET", ""))
SESSION_KINDS = {"claude_session", "codex_session", "tmux_session", "hermes_session"}
WORKING_HOURS = (7, 22)


def parse_day(value: str | Date | None = None) -> Date:
    if isinstance(value, Date):
        return value
    if not value or value == "today":
        return datetime.now().astimezone().date()
    return Date.fromisoformat(str(value))


def day_bounds(day: str | Date) -> tuple[int, int]:
    target = parse_day(day)
    start = datetime.combine(target, Time.min).astimezone()
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _iso_ts(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp())
    except ValueError:
        return None


def activity(start_ts: int, end_ts: int | None, kind: str, label: str,
             project: str | None, source: str, confidence: int,
             **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "start_ts": int(start_ts),
        "end_ts": int(end_ts) if end_ts is not None else None,
        "kind": kind,
        "label": label,
        "project": project,
        "source": source,
        "confidence": max(1, min(3, int(confidence))),
    }
    row.update({key: value for key, value in extra.items() if value is not None})
    return row


def _project_from_path(path: str | Path | None) -> str | None:
    if not path:
        return None
    value = Path(str(path))
    return value.name or None


def _in_day(ts: int | None, day: Date) -> bool:
    start, end = day_bounds(day)
    return ts is not None and start <= ts < end


def collect_claude(day: str | Date, home: Path = DEFAULT_HOME,
                   source: str = "claude:local") -> list[dict[str, Any]]:
    target = parse_day(day)
    start, end = day_bounds(target)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    root = home / ".claude" / "projects"
    if not root.exists():
        return []
    for path in root.glob("*/*.jsonl"):
        if path.name.startswith("request_dump_"):
            continue
        # A transcript not touched until before the target day cannot contain a
        # target-day append. Keep historical reads correct when mtimes allow it.
        try:
            if int(path.stat().st_mtime) < start:
                continue
        except OSError:
            continue
        try:
            handle = path.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                ts = _iso_ts(row.get("timestamp"))
                if ts is None or not start <= ts < end:
                    continue
                session_id = str(row.get("sessionId") or path.stem)
                cwd = row.get("cwd")
                project = _project_from_path(cwd) or _decode_claude_project(path.parent.name)
                key = (session_id, project or "unknown")
                group = groups.setdefault(key, {"first": ts, "last": ts, "cwd": cwd})
                group["first"] = min(group["first"], ts)
                group["last"] = max(group["last"], ts)
                if cwd:
                    group["cwd"] = cwd
    result = []
    for (session_id, project), group in groups.items():
        result.append(activity(
            group["first"], min(group["last"] + 300, end), "claude_session",
            f"Claude Code · {project}", project, source, 3,
            session_id=session_id, cwd=group.get("cwd"),
        ))
    return sorted(result, key=lambda row: row["start_ts"])


def _decode_claude_project(encoded: str) -> str | None:
    marker = str(Path.home()).replace("/", "-") + "-dev-"
    tail = encoded.split(marker, 1)[1] if marker in encoded else encoded.lstrip("-")
    # The complete path cannot be reversibly decoded because '-' is also legal
    # in directory names; the final component is still a useful project label.
    return tail or None


def collect_codex(day: str | Date, home: Path = DEFAULT_HOME,
                  tmux_sessions: Iterable[dict[str, Any]] | None = None,
                  source: str = "codex:local") -> list[dict[str, Any]]:
    target = parse_day(day)
    start, end = day_bounds(target)
    path = home / ".codex" / "history.jsonl"
    if not path.exists():
        return []
    tmux_projects = [row.get("project") for row in (tmux_sessions or [])
                     if row.get("kind") == "tmux_session" and row.get("project")]
    groups: dict[str, dict[str, Any]] = {}
    try:
        handle = path.open(errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _iso_ts(row.get("ts"))
            if ts is None or not start <= ts < end:
                continue
            sid = str(row.get("session_id") or "unknown")
            text = str(row.get("text") or "")
            group = groups.setdefault(sid, {"first": ts, "last": ts, "texts": []})
            group["first"] = min(group["first"], ts)
            group["last"] = max(group["last"], ts)
            if text:
                group["texts"].append(text[:200])
    result = []
    for sid, group in groups.items():
        joined = " ".join(group["texts"]).lower()
        project = next((str(name) for name in tmux_projects if str(name).lower() in joined), None)
        path_match = re.search(r"(?:~/|/home/[^/]+/)dev/([\w.-]+(?:/[\w.-]+)?)", joined)
        if not project and path_match:
            project = path_match.group(1).split("/")[-1]
        project = project or "unknown"
        result.append(activity(
            group["first"], min(group["last"] + 300, end), "codex_session",
            f"Codex · {project}", project, source, 2,
            session_id=sid, prompt_count=len(group["texts"]),
        ))
    return sorted(result, key=lambda row: row["start_ts"])


def _tmux_project(name: str) -> str | None:
    value = re.sub(r"^(claude|codex)-", "", name, flags=re.I)
    value = re.sub(r"-\d+$", "", value)
    return value or None


def collect_tmux(day: str | Date, runner=subprocess.run,
                 source: str = "tmux:local") -> list[dict[str, Any]]:
    target = parse_day(day)
    start, end = day_bounds(target)
    try:
        proc = runner(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_created}\t#{session_last_attached}\t#{session_attached}"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    rows = []
    now = int(datetime.now().timestamp())
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            created = int(parts[1])
        except ValueError:
            continue
        if not start <= created < end:
            continue
        attached = len(parts) > 3 and parts[3] == "1"
        end_ts = min(now, end) if attached else min(created + 300, end)
        project = _tmux_project(name)
        rows.append(activity(created, max(created, end_ts), "tmux_session",
                             f"tmux · {name}", project, source, 2,
                             session_name=name, active=attached))
    return sorted(rows, key=lambda row: row["start_ts"])


def _event_label(kind: str, title: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    if kind == "created":
        return "task_created", f"Created: {title}"
    if kind == "completed":
        return "task_completed", f"Completed: {title}"
    if kind == "claimed":
        return "task_started", f"Started: {title}"
    if kind in {"planned", "unplanned"}:
        return "task_created", f"{kind.title()}: {title}"
    if kind == "status_changed":
        before, after = payload.get("from"), payload.get("to")
        if after == "in_progress":
            return "task_started", f"Started: {title}"
        if after == "done":
            return "task_completed", f"Completed: {title}"
        if after == "review":
            return "task_completed", f"Moved to review: {title}"
        if before != after:
            return "task_created", f"Moved {before or '?'} → {after or '?'}: {title}"
    return None


def collect_kanban(day: str | Date, home: Path = DEFAULT_HOME,
                    source: str = "kanban") -> list[dict[str, Any]]:
    target = parse_day(day)
    start, end = day_bounds(target)
    path = home / ".hermes" / "kanban.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        workspace_expr = "t.workspace_path" if "workspace_path" in task_columns else "NULL"
        rows = conn.execute(
            f"SELECT e.id,e.kind,e.payload,e.created_at,t.title,{workspace_expr} AS workspace_path "
            "FROM task_events e LEFT JOIN tasks t ON t.id=e.task_id "
            "WHERE e.created_at>=? AND e.created_at<? "
            "AND e.kind IN ('created','status_changed','claimed','completed','planned','unplanned') "
            "ORDER BY e.created_at", (start, end)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        title = row["title"] or "Untitled task"
        rendered = _event_label(row["kind"], title, payload)
        if not rendered:
            continue
        kind, label = rendered
        result.append(activity(row["created_at"], row["created_at"], kind, label,
                               _project_from_path(row["workspace_path"]), source, 3,
                               event_id=row["id"]))
    return result


def collect_meetings(day: str | Date, home: Path = DEFAULT_HOME) -> list[dict[str, Any]]:
    """Fireflies meetings stored for this day (CRM sidecar table).

    fireflies_meetings only stores a DATE (no time-of-day), so these are
    returned as a separate top-level list rather than as hour-placed
    activities — the UI renders them in their own strip.
    """
    target = parse_day(day)
    path = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else home / ".hermes" / "kanban.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT title, deal_id, transcript_id, duration_seconds FROM fireflies_meetings "
            "WHERE meeting_date=? ORDER BY fetched_at", (target.isoformat(),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass
    return [{"title": row["title"] or "Meeting", "deal_id": row["deal_id"],
             "transcript_id": row["transcript_id"],
             "duration_seconds": row["duration_seconds"]} for row in rows]


def collect_hermes_sessions(day: str | Date, home: Path = DEFAULT_HOME,
                            source: str = "hermes") -> list[dict[str, Any]]:
    target = parse_day(day)
    start, end = day_bounds(target)
    path = home / ".hermes" / "state.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,started_at,ended_at,cwd,title,message_count FROM sessions "
            "WHERE started_at>=? AND started_at<? ORDER BY started_at", (start, end)).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    result = []
    for row in rows:
        project = _project_from_path(row["cwd"])
        label = row["title"] or project or str(row["id"])[:8]
        ended = int(row["ended_at"] or min(int(row["started_at"]) + 300, end))
        result.append(activity(int(row["started_at"]), min(ended, end), "hermes_session",
                               f"Hermes · {label}", project, source, 3,
                               session_id=row["id"], message_count=row["message_count"]))
    return result


def _git_repos(dev_root: Path, max_depth: int = 3) -> Iterable[Path]:
    if not dev_root.exists():
        return []
    found: list[Path] = []
    base_depth = len(dev_root.parts)
    for root, dirs, _files in os.walk(dev_root):
        current = Path(root)
        depth = len(current.parts) - base_depth
        if ".git" in dirs:
            found.append(current)
            dirs.remove(".git")
        dirs[:] = [name for name in dirs if name not in {"node_modules", ".venv", "venv", "__pycache__"}]
        if depth >= max_depth:
            dirs[:] = []
    return found


def collect_git(day: str | Date, dev_root: Path = DEFAULT_DEV_ROOT,
                author: str | None = None) -> list[dict[str, Any]]:
    target = parse_day(day)
    start_dt = datetime.fromtimestamp(day_bounds(target)[0]).astimezone().isoformat()
    end_dt = datetime.fromtimestamp(day_bounds(target)[1]).astimezone().isoformat()
    wanted = (author or os.environ.get("DAY_REVIEW_GIT_AUTHOR") or "Ricardo").casefold()
    result = []
    for repo in _git_repos(dev_root):
        try:
            proc = subprocess.run(
                ["git", "log", "--all", f"--since={start_dt}", f"--until={end_dt}",
                 "--format=%H%x1f%aI%x1f%an%x1f%s"],
                cwd=repo, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) != 4 or wanted not in parts[2].casefold():
                continue
            ts = _iso_ts(parts[1])
            if not _in_day(ts, target):
                continue
            result.append(activity(ts, ts, "git_commit",
                                   f"Commit · {repo.name}: {parts[3]}", repo.name,
                                   "git", 3, commit=parts[0][:12], author=parts[2]))
    return sorted(result, key=lambda row: row["start_ts"])


def collect_cron(day: str | Date, home: Path = DEFAULT_HOME,
                 source: str = "cron") -> list[dict[str, Any]]:
    target = parse_day(day)
    jobs_path = home / ".hermes" / "cron" / "jobs.json"
    output_root = home / ".hermes" / "cron" / "output"
    try:
        payload = json.loads(jobs_path.read_text())
        jobs = payload.get("jobs", payload if isinstance(payload, list) else [])
        names = {str(row.get("id")): row.get("name") or row.get("id") for row in jobs}
    except (OSError, json.JSONDecodeError, AttributeError):
        names = {}
    if not output_root.exists():
        return []
    prefix = target.isoformat()
    result = []
    for path in output_root.glob(f"*/{prefix}_*"):
        try:
            ts = int(path.stat().st_mtime)
        except OSError:
            continue
        if not _in_day(ts, target):
            # Prefer filename time because copied fixtures and restored logs may
            # have a different mtime.
            match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", path.name)
            if match:
                ts = _iso_ts(f"{match.group(1)}T{match.group(2)}:{match.group(3)}:{match.group(4)}") or ts
        if not _in_day(ts, target):
            continue
        job_id = path.parent.name
        label = names.get(job_id, job_id)
        result.append(activity(ts, ts, "cron_job", f"Cron · {label}", None,
                               source, 3, job_id=job_id, output=str(path)))
    return sorted(result, key=lambda row: row["start_ts"])


def collect_mac(day: str | Date, host: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Collect a bounded remote snapshot; failure is data, never an exception."""
    target = parse_day(day)
    remote = (host or os.environ.get("DAY_REVIEW_MAC_HOST")
              or os.environ.get("ORCHESTRATORMAXXING_EARTH_SSH") or "")
    if not remote:
        return [], "no earth host configured (ORCHESTRATORMAXXING_EARTH_SSH / DAY_REVIEW_MAC_HOST)"
    remote_code = r'''
import datetime,json,pathlib,subprocess
d=datetime.date.fromisoformat(%r); z=datetime.datetime.now().astimezone().tzinfo
s=int(datetime.datetime.combine(d,datetime.time.min,tzinfo=z).timestamp()); e=s+86400; out=[]
try:
 p=subprocess.run(["tmux","list-sessions","-F","#{session_name}\\t#{session_created}"],capture_output=True,text=True,timeout=2)
 for line in p.stdout.splitlines():
  name,ts=line.split("\\t",1); ts=int(ts)
  if s<=ts<e: out.append({"start_ts":ts,"end_ts":ts+300,"kind":"tmux_session","label":"tmux · "+name,"project":name.split("-",1)[-1],"source":"tmux:mac","confidence":2,"session_name":name})
except Exception: pass
for product,rel,kind,label in [("codex",".codex/history.jsonl","codex_session","Codex"),("claude",".claude/projects","claude_session","Claude Code")]:
 root=pathlib.Path.home()/rel
 files=[root] if root.is_file() else list(root.glob("*/*.jsonl")) if root.exists() else []
 groups={}
 for f in files:
  try:
   for line in f.open(errors="replace"):
    try:r=json.loads(line); ts=r.get("ts") or r.get("timestamp"); ts=int(ts) if isinstance(ts,(int,float)) else int(datetime.datetime.fromisoformat(str(ts).replace("Z","+00:00")).timestamp()); sid=str(r.get("session_id") or r.get("sessionId") or f.stem)
    except Exception:continue
    if s<=ts<e: groups.setdefault(sid,[]).append(ts)
  except Exception:pass
 for sid,v in groups.items(): out.append({"start_ts":min(v),"end_ts":min(max(v)+300,e),"kind":kind,"label":label+" · mac","project":"mac","source":product+":mac","confidence":2,"session_id":sid})
print(json.dumps(out))
''' % target.isoformat()
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", remote,
             f"python3 -c {shlex.quote(remote_code)}"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"Mac unreachable: {type(exc).__name__}"
    if proc.returncode != 0:
        detail = (proc.stderr or "ssh failed").strip().splitlines()[-1][:160]
        return [], f"Mac unreachable: {detail}"
    try:
        rows = json.loads(proc.stdout)
        return rows if isinstance(rows, list) else [], None
    except json.JSONDecodeError:
        return [], "Mac unreachable: invalid collector response"


def _dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result = []
    for row in sorted(rows, key=lambda item: (item["start_ts"], item["kind"], item["label"])):
        key = (row["start_ts"], row.get("end_ts"), row["kind"], row["label"], row.get("project"))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def merge_and_fill(rows: Iterable[dict[str, Any]], day: str | Date,
                   hours: tuple[int, int] = WORKING_HOURS,
                   fill_gaps: bool = True) -> list[dict[str, Any]]:
    target = parse_day(day)
    ordered = _dedupe(rows)
    merged: list[dict[str, Any]] = []
    for row in ordered:
        if (merged and row["kind"] in SESSION_KINDS and merged[-1]["kind"] in SESSION_KINDS
                and row.get("project") and row.get("project") == merged[-1].get("project")
                and row["start_ts"] <= (merged[-1].get("end_ts") or merged[-1]["start_ts"]) + 600):
            current = merged[-1]
            current["end_ts"] = max(current.get("end_ts") or current["start_ts"],
                                    row.get("end_ts") or row["start_ts"])
            sources = sorted(set(current["source"].split("+") + row["source"].split("+")))
            current["source"] = "+".join(sources)
            current["confidence"] = max(current["confidence"], row["confidence"])
            continue
        merged.append(dict(row))
    if not fill_gaps:
        return sorted(merged, key=lambda row: row["start_ts"])
    zone = datetime.now().astimezone().tzinfo
    work_start = int(datetime.combine(target, Time(hours[0]), tzinfo=zone).timestamp())
    work_end = int(datetime.combine(target, Time(hours[1]), tzinfo=zone).timestamp())
    occupied = sorted((max(work_start, row["start_ts"]), min(work_end, row.get("end_ts") or row["start_ts"]))
                      for row in merged if row["start_ts"] < work_end and (row.get("end_ts") or row["start_ts"]) >= work_start)
    cursor = work_start
    gaps = []
    for start, end in occupied:
        if start - cursor > 3600:
            gaps.append(activity(cursor, start, "gap", "Gap / no recorded activity", None, "inferred", 1))
        cursor = max(cursor, end)
    if work_end - cursor > 3600:
        gaps.append(activity(cursor, work_end, "gap", "Gap / no recorded activity", None, "inferred", 1))
    return sorted(merged + gaps, key=lambda row: (row["start_ts"], row["kind"]))


def _clock(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%-I:%M%p").lower()


def format_timeline(day: str | Date, rows: Iterable[dict[str, Any]], max_lines: int = 25) -> str:
    target = parse_day(day)
    title = target.strftime("%A %d %B")
    lines = [f"📊 Day Review — {title}", ""]
    material = list(rows)
    priority = {
        "task_completed": 0, "task_started": 1, "git_commit": 2,
        "claude_session": 3, "codex_session": 4, "tmux_session": 5,
        "task_created": 6, "cron_job": 7, "hermes_session": 8, "gap": 9,
    }
    # One scan-friendly line per working hour prevents frequent cron jobs from
    # consuming the entire 25-line Telegram budget before the real workday.
    zone = datetime.now().astimezone().tzinfo
    for hour in range(WORKING_HOURS[0], WORKING_HOURS[1]):
        hour_start = int(datetime.combine(target, Time(hour), tzinfo=zone).timestamp())
        hour_end = hour_start + 3600
        bucket = [row for row in material if hour_start <= row["start_ts"] < hour_end]
        if not bucket:
            continue
        bucket.sort(key=lambda row: (priority.get(row["kind"], 99), row["start_ts"]))
        labels: list[str] = []
        for row in bucket:
            label = row["label"]
            if label not in labels:
                labels.append(label)
            if len(labels) == 2:
                break
        suffix = f" · +{len(bucket) - len(labels)} more" if len(bucket) > len(labels) else ""
        lines.append(f"{_clock(hour_start)}  {' · '.join(labels)}{suffix}")
        if len(lines) >= max_lines:
            break
    return "\n".join(lines[:max_lines])


def _stats(rows: list[dict[str, Any]], notes: list[str]) -> dict[str, Any]:
    by_kind = Counter(row["kind"] for row in rows)
    sources = Counter(part for row in rows for part in row["source"].split("+"))
    return {
        "activity_count": len(rows),
        "by_kind": dict(sorted(by_kind.items())),
        "by_source": dict(sorted(sources.items())),
        "projects": sorted({row["project"] for row in rows if row.get("project")}),
        "notes": notes,
        "mac_unreachable": any(note.startswith("Mac unreachable") for note in notes),
    }


def collect_day_review(day: str | Date | None = None, *, raw: bool = False,
                       home: Path = DEFAULT_HOME, dev_root: Path = DEFAULT_DEV_ROOT,
                       include_mac: bool = True,
                       hours: tuple[int, int] = WORKING_HOURS) -> dict[str, Any]:
    target = parse_day(day)
    tmux_rows = collect_tmux(target)
    rows = [
        *collect_claude(target, home),
        *collect_codex(target, home, tmux_rows),
        *tmux_rows,
        *collect_kanban(target, home),
        *collect_hermes_sessions(target, home),
        *collect_git(target, dev_root),
        *collect_cron(target, home),
    ]
    notes: list[str] = []
    if include_mac:
        mac_rows, mac_note = collect_mac(target)
        rows.extend(mac_rows)
        if mac_note:
            notes.append(mac_note)
    normalized = merge_and_fill(rows, target, hours=hours, fill_gaps=not raw)
    timeline = format_timeline(target, normalized)
    return {
        "date": target.isoformat(),
        "generated_at": datetime.now().astimezone().isoformat(),
        "activities": normalized,
        "meetings": collect_meetings(target, home),
        "stats": _stats(normalized, notes),
        "timeline_text": timeline,
        "raw": raw,
    }


def persist_review(review: dict[str, Any], path: Path = DEFAULT_REVIEW_STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("date") != review.get("date"):
                existing.append(row)
    stored = {
        "date": review["date"],
        "generated_at": review["generated_at"],
        "stats": review.get("stats", {}),
        "timeline_text": review.get("timeline_text", ""),
    }
    existing.append(stored)
    existing.sort(key=lambda row: str(row.get("date", "")))
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            for row in existing:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def get_day_review(day: str | Date | None = None, raw: bool = False) -> dict[str, Any]:
    review = collect_day_review(day, raw=raw)
    if not raw:
        persist_review(review)
    return review


def send_telegram(review: dict[str, Any], target: str = DEFAULT_TELEGRAM) -> None:
    proc = subprocess.run(
        # shutil.which first: a scheduler/systemd environment may lack ~/.local/bin.
        [__import__("shutil").which("hermes") or str(Path.home() / ".local" / "bin" / "hermes"),
         "send", "--to", target, "--subject", "📊 Day Review", review["timeline_text"]],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "hermes send failed").strip())


def _hours(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
    if not match or not (0 <= int(match.group(1)) < int(match.group(2)) <= 24):
        raise argparse.ArgumentTypeError("hours must be START-END, e.g. 7-22")
    return int(match.group(1)), int(match.group(2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an hour-by-hour Hermes Day Review")
    parser.add_argument("--date", default="today", help="YYYY-MM-DD or today")
    parser.add_argument("--json", action="store_true", help="print review JSON")
    parser.add_argument("--telegram", action="store_true", help="send the timeline to Telegram")
    parser.add_argument("--raw", action="store_true", help="skip merge gaps and persistence")
    parser.add_argument("--hours", type=_hours, default=WORKING_HOURS, help="gap window, e.g. 7-22")
    parser.add_argument("--no-mac", action="store_true", help="skip optional Mac SSH collection")
    args = parser.parse_args(argv)
    review = collect_day_review(args.date, raw=args.raw, include_mac=not args.no_mac, hours=args.hours)
    if not args.raw:
        persist_review(review)
    if args.telegram:
        send_telegram(review)
    if args.json:
        print(json.dumps(review, ensure_ascii=False, sort_keys=True))
    elif not args.telegram:
        print(review["timeline_text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
