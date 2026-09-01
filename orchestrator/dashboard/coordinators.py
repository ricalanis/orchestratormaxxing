"""
Coordinators view — derived, no new backend state (PRD principle #4, "derived
over declared"). We model the fleet as three standing sub-agents that route work
by domain:

  - Code Coordinator       — engineering: implement / fix / refactor / test / ship
  - Research Coordinator    — investigation: research / specs / analysis / docs
  - Commercial Coordinator  — business: product / CRM / customers / pricing / GTM

Each task is classified onto exactly one coordinator using (1) its assignee, then
(2) keyword hits over title+body, with Code as the engineering-default fallback.
From the live task set we derive, per coordinator: the tasks it is currently
handling, the last activity timestamp, and a green/yellow/red health signal.

This is a pure function of the existing task rows — nothing is persisted, so the
view can never drift from task reality.
"""
import time
from typing import Any, Optional

# Recency thresholds for the health signal (seconds).
FRESH_SECONDS = 2 * 3600      # in-progress + touched within 2h → green
STALE_SECONDS = 6 * 3600      # in-progress but untouched >6h → red (stalled/dead)

COORDINATORS = [
    {
        "key": "code",
        "name": "Code Coordinator",
        "icon": "⚙️",
        "desc": "Engineering — implementation, fixes, refactors, tests, deploys",
        # assignee strings that route straight here (heavy code agents + the
        # Hermes/OpenCode coders + the generic dispatcher default).
        "assignees": ["claude-code", "opencode", "kimi-coder", "glm-coder", "default"],
        "keywords": [
            "code", "implement", "fix", "refactor", "bug", "test", "deploy",
            "api", "dashboard", "endpoint", "script", "mcp", "hook", "build",
            "ui", "frontend", "backend", "commit", "merge", "lint", "schema",
        ],
    },
    {
        "key": "research",
        "name": "Research Coordinator",
        "icon": "🔬",
        "desc": "Investigation — research digests, specs, analysis, documentation",
        "assignees": ["ollama-worker", "research"],
        "keywords": [
            "research", "investigate", "analyze", "analysis", "spec", "prd",
            "explore", "digest", "study", "audit", "summarize", "document",
            "docs", "knowledge", "compare", "evaluate", "benchmark",
        ],
    },
    {
        "key": "commercial",
        "name": "Commercial Coordinator",
        "icon": "💼",
        "desc": "Business — product, CRM, customers, pricing, go-to-market",
        "assignees": ["commercial", "sales"],
        "keywords": [
            "commercial", "sales", "crm", "customer", "client", "pricing",
            "price", "market", "revenue", "business", "gtm", "launch",
            "outreach", "lead", "invoice", "billing", "proposal",
        ],
    },
]

# Statuses that count as "currently handling" (open work), and their buckets.
# `review` (Phase 3): finished-but-unaccepted still occupies the coordinator —
# it's open until the operator accepts.
_OPEN_STATUSES = {"in_progress", "ready", "backlog", "blocked", "review"}


def classify(task: dict) -> str:
    """Route one task to a coordinator key. Assignee wins; else keyword vote;
    else fall back to Code (the engineering default)."""
    assignee = (task.get("assignee") or "").strip().lower()
    for c in COORDINATORS:
        if assignee in c["assignees"] and assignee not in ("default",):
            return c["key"]
    # 'default' (the bare Hermes dispatcher) is ambiguous — let keywords decide
    # first, only falling back to Code if nothing matches.
    text = f"{task.get('title') or ''} {task.get('body') or ''}".lower()
    best_key, best_hits = None, 0
    for c in COORDINATORS:
        hits = sum(1 for kw in c["keywords"] if kw in text)
        if hits > best_hits:
            best_key, best_hits = c["key"], hits
    if best_key:
        return best_key
    if assignee == "default":
        return "code"
    # Unknown assignee, no keyword signal → engineering default.
    return "code"


def _task_last_activity(task: dict) -> int:
    """Best-available activity timestamp for a task (unix seconds)."""
    return max(
        int(task.get("completed_at") or 0),
        int(task.get("started_at") or 0),
        int(task.get("created_at") or 0),
    )


def _health(in_progress: int, blocked: int, ready: int,
            last_activity: int, now: int) -> tuple[str, str]:
    """Green/yellow/red + a one-word label from the coordinator's task mix."""
    age = now - last_activity if last_activity else None
    if blocked > 0:
        return "red", "blocked"
    if in_progress > 0:
        if age is not None and age <= FRESH_SECONDS:
            return "green", "active"
        if age is not None and age > STALE_SECONDS:
            return "red", "stalled"
        return "yellow", "working"
    if ready > 0:
        return "yellow", "queued"
    return "gray", "idle"


def build(tasks: list[dict], now: Optional[int] = None) -> dict:
    """Derive the coordinators view from the live task list."""
    if now is None:
        now = int(time.time())

    buckets: dict[str, list[dict]] = {c["key"]: [] for c in COORDINATORS}
    done_recent: dict[str, int] = {c["key"]: 0 for c in COORDINATORS}
    week_ago = now - 7 * 24 * 3600

    for t in tasks:
        key = classify(t)
        status = (t.get("status") or "").lower()
        if status == "done":
            if int(t.get("completed_at") or 0) >= week_ago:
                done_recent[key] += 1
            continue
        if status in _OPEN_STATUSES:
            buckets[key].append(t)

    out = []
    for c in COORDINATORS:
        mine = buckets[c["key"]]
        in_progress = sum(1 for t in mine if (t.get("status") or "").lower() == "in_progress")
        ready = sum(1 for t in mine if (t.get("status") or "").lower() in ("ready", "backlog"))
        blocked = sum(1 for t in mine if (t.get("status") or "").lower() == "blocked")
        last_activity = max((_task_last_activity(t) for t in mine), default=0)
        color, label = _health(in_progress, blocked, ready, last_activity, now)
        # Tasks it's handling, most-recently-active first, capped for payload size.
        handling = sorted(mine, key=_task_last_activity, reverse=True)
        out.append({
            "key": c["key"],
            "name": c["name"],
            "icon": c["icon"],
            "desc": c["desc"],
            "color": color,
            "label": label,
            "in_progress": in_progress,
            "queued": ready,
            "blocked": blocked,
            "total_open": len(mine),
            "done_recent": done_recent[c["key"]],
            "last_activity": last_activity or None,
            "last_activity_ago": (now - last_activity) if last_activity else None,
            "tasks": [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "assignee": t.get("assignee"),
                    "priority": t.get("priority"),
                    "progress_note": t.get("progress_note"),
                    "progress_pct": t.get("progress_pct"),
                    "last_activity": _task_last_activity(t),
                }
                for t in handling[:12]
            ],
        })

    return {"coordinators": out, "generated_at": now}
