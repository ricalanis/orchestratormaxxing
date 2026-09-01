#!/usr/bin/env python3
"""
Hermes Orchestrator MCP Server
==============================
An MCP (Model Context Protocol) server that exposes the Hermes kanban dashboard
as tools for any MCP-compatible client (Claude Code, Claude Desktop, Cursor, etc).

This allows all your Claude instances to:
- View and create tasks
- Update task status (move between kanban columns)
- Assign tasks to agents or yourself
- List active sessions
- Manage sprints and projects
- Get activity feed

Transport: stdio (standard MCP mode)
Port: none (runs as subprocess, spawned by the MCP client)

Usage in Claude Code:
    claude mcp add hermes-orchestrator -- python3 /path/to/orchestrator/mcp_server.py

Usage in Claude Desktop (config.json):
    {
      "mcpServers": {
        "hermes-orchestrator": {
          "command": "python3",
          "args": ["/path/to/orchestrator/mcp_server.py"]
        }
      }
    }

Usage in Cursor/other MCP clients:
    Same pattern — point to this script with python3.
"""
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

# MCP protocol uses JSON-RPC over stdio
# We implement a minimal MCP server that responds to:
# - initialize
# - tools/list
# - tools/call

KANBAN_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
    else Path.home() / ".hermes" / "kanban.db"

# The dashboard package lives beside this file; the path insert used to happen
# further down, next to the loop-core import, but the URL resolver is needed
# here. Idempotent and harmless to do early — `dashboard.db` imports nothing but
# the stdlib.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard.db import dashboard_url as _dashboard_url  # noqa: E402

# ONE address for the whole system — see `dashboard/db.py::dashboard_url`. The
# module constant is what human-facing links carry; `tool_get_dashboard_url`
# calls the resolver live, so an operator who exports DASHBOARD_URL is never
# told a stale address.
DASHBOARD_URL = _dashboard_url()

# `_dash()` talks to the dashboard on the SAME machine — an internal hop that
# must not depend on tailscale being up (and must not gain its latency). Links
# for humans use DASHBOARD_URL; the proxy uses loopback unless overridden.
DASHBOARD_INTERNAL_URL = os.environ.get(
    "DASHBOARD_INTERNAL_URL", "http://127.0.0.1:3000")

# Dashboard auth token — same source as the dashboard's own middleware:
# env HERMES_DASHBOARD_TOKEN, else ~/.config/orchestratormaxxing/dashboard-token file.
# Used by _dash() to authenticate mutating proxy calls (POST/PATCH/DELETE/PUT).
_DASH_TOKEN_FILE = Path.home() / ".config" / "orchestratormaxxing" / "dashboard-token"


def _configured_dash_token() -> str | None:
    # An explicitly-empty env var means "disable auth" (test/CI mode).
    if "HERMES_DASHBOARD_TOKEN" in os.environ:
        tok = os.environ["HERMES_DASHBOARD_TOKEN"].strip()
        return tok or None  # "" → None → no auth header
    try:
        return _DASH_TOKEN_FILE.read_text().strip() or None
    except Exception:
        return None


_DASH_TOKEN = _configured_dash_token()
_DASH_MUTATING_METHODS = frozenset(("POST", "PATCH", "DELETE", "PUT"))

# The push/pull loop core (PRD Phase 3) lives in the dashboard package. Import it
# so the MCP server and the dashboard UI drive the exact same state machine
# ("backed by the same core", PRD §8). Lightweight — pulls in sqlite-only modules,
# not FastAPI. (The sys.path insert this block used to carry now happens above,
# next to DASHBOARD_URL, which needs it first.)
try:
    from dashboard import loop as _loop
    from dashboard import object_graph as _graph
    from dashboard import db as _db
    from dashboard import sprints as _sprints
    from dashboard import orchestration as _orch
    from dashboard import identity as _identity
    from dashboard import canvas as _canvas
    from dashboard import governance as _gov
    from dashboard import strategy as _strategy
    # The schema floor (m00) — ONE chain, shared with dashboard/api.py. This
    # block used to carry its own hand-maintained ensure_schema() list and it
    # had drifted into a strict SUBSET of the dashboard's: a DB bootstrapped by
    # the MCP server came up without the fireflies / nurture / events /
    # task-flag / comments / consulting-time schema or the P3 indexes. Both
    # processes now call the same runner, so they cannot drift again.
    from dashboard.migrations import runner as _runner
    _runner.run()  # idempotent: legacy ensures + versioned orch_migrations
    _LOOP_OK = True
except Exception as _e:  # pragma: no cover - the read/CRUD tools still work without it
    _LOOP_OK = False
    _LOOP_ERR = str(_e)

# Phase 6: the roadmap lives in the initiatives TABLE (dashboard/strategy.py);
# dashboard/roadmap.json is a generated export nothing reads anymore.

# ---------------------------------------------------------------------------
# PRD Phase 4 — the least-authority scope model (this is what makes the MCP
# publishable). Two scopes:
#   default    — orient (read) + pull (claim) + report + declare. What any
#                external fleet agent gets. Cannot restructure the plan.
#   privileged — default + orchestration: dispatch, trust, sprint/roadmap edits.
#                Granted ONLY to the operator, explicitly.
# The default scope is the safe fallback; privileged requires an explicit opt-in
# AND (if configured) a matching token, so a client can't self-elevate.
# ---------------------------------------------------------------------------
PRIV_TOKEN_FILE = Path.home() / ".config" / "orchestratormaxxing" / "mcp-privileged-token"

# Load-bearing tools an external agent must NEVER reach on the default scope
# (PRD §8). Everything not listed here is default (orient/pull/report/declare).
PRIVILEGED_TOOLS = {
    # Personal health/behavioral state. Default scope is "what any external
    # fleet agent gets" — so leaving these out would let any fleet agent read
    # the operator's cognitive-load state and WRITE stress scores and free-text
    # notes into it. Operator-only, deliberately.
    "get_cogload_status",
    "save_cogload_label",
    "update_task_status",   # arbitrary status writes bypass the loop's gate
    "assign_task",          # reassigning work = dispatch
    "dispatch_to_agent",    # RETIRED tombstone — kept listed so the verb stays
                            # OUT of the default toolset (a default-scope agent
                            # never saw it and gains nothing from a gravestone)
    "set_pool",             # put/pull a task in the open pool
    "set_autonomy",         # allow/deny auto-accept
    "change_trust_grade",   # the trust dial is operator-only (anti self-preference)
    "create_sprint",        # plan structure
    "start_sprint",         # plan structure
    "assign_task_sprint",   # cycle commitment (the commit-ledger write)
    "close_sprint",         # plan structure
    "finish_sprint",        # 🏁 roll-forward: archive + carry + activate next
    "create_project",       # plan structure
    "create_epic",          # RETIRED tombstone (epics folded into projects)
    "assign_task_epic",     # RETIRED tombstone (epics folded into projects)
    "edit_roadmap",         # RETIRED tombstone (initiatives folded into projects)
    "set_contract",         # the operator authors contracts, never the worker
    "set_run_envelope",     # operator declares brakes/checks before execution
    "create_initiative",    # RETIRED tombstone (initiatives folded into projects)
    "add_deal_event",       # commercial writes (Tier 2)
    "update_epic",          # RETIRED tombstone (epics folded into projects)
    "update_task",          # card edits are operator-only (Tier 2)
    "bulk_accept",          # mass human-gate — operator/Hermes only (Tier 2)
    "reconcile_sprint_ledger",  # repair sprint ledger drift (data integrity)
    "delete_task",          # destructive (Tier 3)
    "archive_project",      # plan structure (Tier 3)
    "register_agent",       # fleet membership (Tier 3)
    "rebuild_graph",        # heavy re-ingest (Tier 3)
    "evolve_node",          # mutates graph-node properties (memory evolution)
    "archive_stale",        # bulk decay/archive sweep across the graph
    "update_memory",        # flat-memory edit (write to the on-disk store)
    "delete_memory",        # flat-memory delete (destructive)
    "crm_decay",            # bulk commercial-structure sweep (deals → stalled/lost)
    "approve_crm_proposal", # the human gate on a CRM correction (applies a write)
    "dismiss_crm_proposal", # sticky rejection is a human decision too (Sol F1)
    "create_account",       # CRM writes = commercial structure (operator/Hermes)
    "create_contact",
    "create_deal",
    "update_deal",
    "update_contact",       # CRM writes = commercial structure (operator/Hermes)
    "plan_day",             # the day plan is the operator's commitment device —
    "plan_task",            # only the operator (or his Hermes session, granted
    "wrap_day",             # privileged explicitly) writes it; fleets read it
    "get_personal_okrs",    # the operator's sensitive personal objectives/check-ins
    # Phase 2 — Session Control writes: direct remote control of live Claude Code
    # sessions. Arbitrary command injection / termination is orchestration
    # authority, never the default fleet scope. (Reads + agent self-reported
    # events — get_session_*, list_*, create_session_event, resolve_session_event
    # — stay default, mirroring report_progress / resolve_input.)
    "send_to_session",      # inject an arbitrary prompt into a live session
    "resend_last",          # re-inject the last instruction
    "revive_session",       # wake/respawn a session on its origin machine
    "kill_session",         # terminate a live tmux session (destructive)
    "compact_session",      # push /compact into a session
    "prune_transcripts",    # operator display-hygiene sweep across sessions
    "link_task_session",    # bind a task to a session (graph structure write)
    # Phase 3 — Task Lifecycle writes: operator human-gate + destructive actions.
    # (heartbeat + context/icebox/delivered/detail/events/candidates reads stay
    # default, mirroring report_progress / the other orient verbs.)
    "accept_task",          # the positive human gate (done+reviewed)
    "reject_task",          # the negative human gate
    "abort_task",           # abort in-progress work (releases claim, kills session)
    "fail_task",            # record a failed attempt (3-strike rule)
    "delete_comment",       # destructive — remove a comment
    # Phase 4 — Sprint/Cycle + Specs + Orchestration writes: plan-structure /
    # destructive / load-bearing. (Reads — get_sprint_tasks, get_cycle_board,
    # get_cycles_calendar, get_lakehouse_overview, get_mcp_manifest, get_ledger —
    # stay default, mirroring the other orient verbs.)
    "roll_cycle",           # roll the active cycle → next (plan structure)
    "delete_sprint",        # destructive — hard-delete a sprint/cycle
    "commit_cycle",         # cycle commitment (the commit-ledger write)
    "write_spec",           # the operator authors the source-of-truth spec
    "orchestration_sweep",  # trigger the sweeper (auto-compact + auto-abort)
    # Parity audit 2026-07 — sidecar task/plan writes + derived export
    "set_task_assignee",    # reassign = dispatch (audited sidecar twin of assign_task)
    "set_scheduled_week",   # plan structure (week bucket ⇄ cycle sync)
    "assign_task_project",  # re-home a task (plan structure)
    "set_auto_cycle",       # auto-commit flag (plan structure)
    "update_project",       # plan structure (Tier 2)
    "create_cycle",         # plan structure (weekly twin of legacy create_sprint)
    "export_roadmap",       # writes roadmap.json (derived export, still a file write)
}


def _configured_priv_token():
    tok = os.environ.get("HERMES_MCP_PRIVILEGED_TOKEN")
    if tok:
        return tok.strip()
    try:
        return PRIV_TOKEN_FILE.read_text().strip() or None
    except Exception:
        return None


def _resolve_scope() -> str:
    """Decide the active scope at startup. Default unless privileged is both
    requested (HERMES_MCP_SCOPE=privileged) AND authorized (token matches, or no
    token is configured → local-operator convenience with a stderr warning)."""
    want = (os.environ.get("HERMES_MCP_SCOPE") or "default").strip().lower()
    if want != "privileged":
        return "default"
    configured = _configured_priv_token()
    if configured:
        presented = os.environ.get("HERMES_MCP_TOKEN", "").strip()
        if not presented:
            # HERMES_MCP_TOKEN_FILE lets a spawning config (e.g. the Hermes
            # gateway's mcp_servers entry) carry a *path* instead of the secret
            # itself — config files stay credential-free.
            path = os.environ.get("HERMES_MCP_TOKEN_FILE", "").strip()
            if path:
                try:
                    presented = Path(path).read_text().strip()
                except Exception:
                    presented = ""
        if presented and secrets.compare_digest(presented, configured):
            return "privileged"
        print("hermes-mcp: privileged scope requested but token missing/mismatch "
              "→ falling back to DEFAULT scope", file=sys.stderr)
        return "default"
    print("hermes-mcp: privileged scope granted with NO configured token — set "
          "HERMES_MCP_PRIVILEGED_TOKEN (or ~/.config/orchestratormaxxing/mcp-privileged-token) "
          "to lock it down before exposing this beyond localhost", file=sys.stderr)
    return "privileged"


ACTIVE_SCOPE = _resolve_scope()


def _tool_scope(name: str) -> str:
    return "privileged" if name in PRIVILEGED_TOOLS else "default"


def _scope_allows(name: str) -> bool:
    return ACTIVE_SCOPE == "privileged" or _tool_scope(name) == "default"

# --- Tool definitions ---

TOOLS = [
    {
        "name": "list_tasks",
        "description": "List kanban tasks. Filter by assignee, status, project, or free-text q (title/body substring).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "assignee": {
                    "type": "string",
                    "description": "Filter by assignee (ricardo, default, claude-code, opencode)"
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status (backlog, ready, in_progress, blocked, review, done)"
                },
                "project_id": {"type": "string", "description": "Filter by project id/slug."},
                "q": {"type": "string", "description": "Substring match on title/body."}
            }
        }
    },
    {
        "name": "get_task",
        "description": "Get detailed info about a specific task including comments and events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (e.g. t_991050b5)"}
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "create_task",
        "description": "Create a new kanban task. Returns the task ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "body": {"type": "string", "description": "Optional task body."},
                "acceptance_criteria": {"type": "string", "description": "Observable acceptance criteria."},
                "assignee": {
                    "type": "string",
                    "description": "Who should do this: ricardo (human), default (Hermes), claude-code, opencode",
                    "default": "default"
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional project id/slug. If omitted, defaults from your session's project, else the Inbox."
                },
                "session_key": {
                    "type": "string",
                    "description": "Your session key (from set_session_role) — sets the default project + stamps origin."
                },
                "origin": {
                    "type": "string",
                    "description": "Who this originates from: ricardo|hermes|agent|decomposed. Usually inferred from your session."
                },
                "sprint_id": {
                    "type": "string",
                    "description": "Optional sprint/cycle id to commit the task to on creation. If omitted, the task is unassigned until auto-committed."
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional deadline in strict YYYY-MM-DD format."
                },
                "contract_cmd": {
                    "type": "string",
                    "description": "Optional runnable contract; requires practice_text and run_context."
                },
                "practice_text": {"type": "string"},
                "practice_host": {
                    "type": "string", "default": "hermes",
                    "enum": ["hermes", "orchestrator", "claude", "codex", "opencode", "open_design"]
                },
                "run_context": {
                    "type": "object",
                    "description": "Declared dependencies, checkpoint, exact four brakes, progress scale, writers, objective completion and evidence."
                }
            },
            "required": ["title"]
        }
    },
    {
        "name": "update_task_status",
        "description": "Move a task to a different kanban column (change its status).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "status": {
                    "type": "string",
                    "enum": ["backlog", "ready", "in_progress", "blocked", "review", "done"],
                    "description": "New status (review = finished, awaiting the operator's accept; done = accepted)"
                }
            },
            "required": ["task_id", "status"]
        }
    },
    {
        "name": "assign_task",
        "description": "Assign a task to a specific agent or person.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "assignee": {
                    "type": "string",
                    "description": "Assignee: ricardo, default, claude-code, opencode"
                }
            },
            "required": ["task_id", "assignee"]
        }
    },
    {
        "name": "comment_task",
        "description": "Add a comment to a task (useful for reporting results or progress).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "comment": {"type": "string", "description": "Comment text"}
            },
            "required": ["task_id", "comment"]
        }
    },
    {
        "name": "list_projects",
        "description": "List all projects with task counts.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "list_sprints",
        "description": "List all sprints, optionally filtered by project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project ID"}
            }
        }
    },
    {
        "name": "create_sprint",
        "description": "Create a new sprint for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {"type": "string", "description": "Sprint name (e.g. 'Sprint 1')"},
                "goal": {"type": "string", "description": "Sprint goal"}
            },
            "required": ["project_id", "name"]
        }
    },
    {
        "name": "get_activity",
        "description": "Get recent activity feed (task events, completions, etc).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of events to return", "default": 20}
            }
        }
    },
    {
        "name": "get_sessions",
        "description": "List active Claude Code and OpenCode sessions across all machines. `terminals` names the live c/g/o tmux sessions (claude-*/codex-*/opencode-*) per host — host is 'local' or any remote host configured in dashboard/sessions.py; use those names with get_session_output / send_to_session.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_dashboard_url",
        "description": "Get the dashboard URL for viewing the kanban board in a browser.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    # --- PRD Phase 3: the push/pull loop (agent-side). This is how a remote
    # Claude Code fleet pulls work, reports live, and escalates to the operator. ---
    {
        "name": "list_pool",
        "description": "PULL: list tasks you can claim right now — the open pool plus your own assigned queue (highest priority first). Pass your agent name to include work assigned to you.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Your agent name (to include your assigned queue)."},
                "skills": {"type": "string", "description": "Optional skill filter."},
            },
        },
    },
    {
        "name": "claim_task",
        "description": "PULL: atomically claim a task (Pool→Working). Links your session and returns the task + its ACCEPTANCE CONTRACT + workspace. Fails cleanly if already claimed (no double-claim). Work to the acceptance contract you get back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent": {"type": "string", "description": "Your agent name."},
                "session_id": {"type": "string", "description": "Your session id (optional, links the run)."},
            },
            "required": ["task_id", "agent"],
        },
    },
    {
        "name": "claim_next",
        "description": "PULL: claim the single highest-priority claimable task for you in one call (race-safe). Returns the claimed task or an empty result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Your agent name."},
                "skills": {"type": "string", "description": "Optional skill filter."},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "report_progress",
        "description": "PUSH UP: report what you're doing on a claimed task (note + optional %). Refreshes your heartbeat. Call it as you work — it shows live on the Fleet board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "note": {"type": "string", "description": "Short progress note."},
                "pct": {"type": "integer", "description": "Optional percent complete (0-100)."},
                "step": {"type": "string", "enum": ["plan", "code", "validate"],
                         "description": "Advance the run pipeline stage (plan → code → validate)."},
                "agent": {"type": "string"},
            },
            "required": ["task_id", "note"],
        },
    },
    {
        "name": "report_result",
        "description": "PUSH UP: report you FINISHED a task. Set passed=true only if you met the acceptance contract. Routes by the auto-accept-vs-escalate rule: a high-trust agent on an auto task auto-accepts; everything else escalates to the operator's review Inbox. Never claim passed unless you actually met the contract.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "result": {"type": "string", "description": "Summary of what you did / the outcome."},
                "passed": {"type": "boolean", "description": "Did you meet the acceptance contract?", "default": True},
                "artifacts": {"type": "array", "items": {"type": "string"}, "description": "Paths/URLs/PR links produced."},
                "agent": {"type": "string"},
            },
            "required": ["task_id", "result"],
        },
    },
    {
        "name": "report_blocked",
        "description": "PUSH UP: you hit a wall and can't proceed. ALWAYS escalates to the operator's Inbox with your reason. Never silently drop a task — report it blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why you're blocked / what you need."},
                "agent": {"type": "string"},
            },
            "required": ["task_id", "reason"],
        },
    },
    {
        "name": "escalate_discovery",
        "description": "PUSH UP: you found something that needs the operator (a bug, a risk, out-of-scope work). Creates a NEW task directly in the operator's Inbox. Use this instead of silently expanding scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Details of what you found."},
                "reason": {"type": "string", "description": "Why it needs the operator."},
                "related_task": {"type": "string", "description": "Optional task id this relates to."},
                "agent": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    # --- PRD Phase 4: complete the orient/read surface (default scope) ---
    {
        "name": "get_roadmap",
        "description": "ORIENT: the roadmap — initiatives with DERIVED progress (from each project's tasks). Use it to see the big picture before pulling work.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_initiatives",
        "description": "ORIENT: list roadmap initiatives (id, title, project, status, progress).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_epics",
        "description": "RETIRED: epics were folded into projects (m03). Returns {error: epics_folded}. Use list_projects / get_project instead; tasks.epic_id is frozen audit.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "Optional project id to scope to."}},
        },
    },
    {
        "name": "get_active_sprint",
        "description": "ORIENT: the currently active sprint (optionally for a project).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "Optional project id."}},
        },
    },
    {
        "name": "get_task_history",
        "description": "ORIENT: the full status/audit trail for a task (oldest→newest).",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "get_archive",
        "description": "ORIENT: completed tasks bucketed by completion date (day|week|month).",
        "inputSchema": {
            "type": "object",
            "properties": {"group": {"type": "string", "enum": ["day", "week", "month"], "default": "day"}},
        },
    },
    # --- PRD Phase 4: privileged orchestration (operator scope only) ---
    {
        "name": "dispatch_to_agent",
        "description": "RETIRED: it wrote tasks.assignee and spawned nothing. Returns {error: retired}. Dispatch is human-initiated from the dashboard; agents pull work with claim_task/claim_next and report via report_progress/report_result.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "agent": {"type": "string"}},
            "required": ["task_id", "agent"],
        },
    },
    {
        "name": "set_pool",
        "description": "PRIVILEGED: put a task into (pool=true) or pull it from (false) the OPEN pool. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "pool": {"type": "boolean", "default": True}},
            "required": ["task_id"],
        },
    },
    {
        "name": "set_autonomy",
        "description": "PRIVILEGED: set a task's autonomy: 'auto' (auto-accept eligible) | 'dispatch' (always review). Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "autonomy": {"type": "string", "enum": ["auto", "dispatch"]}},
            "required": ["task_id", "autonomy"],
        },
    },
    {
        "name": "pin_task_bottom",
        "description": "Park a task at the bottom of its kanban column (pinned=true) or restore it (false). Positional only — the task KEEPS its status. Use it for work that's waiting on someone else; use report_blocked instead when an agent genuinely cannot proceed.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "pinned": {"type": "boolean", "default": True}},
            "required": ["task_id"],
        },
    },
    {
        "name": "change_trust_grade",
        "description": "PRIVILEGED: override an agent's trust grade (new|low|medium|high). The trust dial is operator-only — an agent can never raise its own. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent": {"type": "string"}, "grade": {"type": "string", "enum": ["new", "low", "medium", "high"]}},
            "required": ["agent", "grade"],
        },
    },
    {
        "name": "start_sprint",
        "description": "PRIVILEGED: activate a sprint/cycle. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"sprint_id": {"type": "string"}},
            "required": ["sprint_id"],
        },
    },
    {
        "name": "assign_task_sprint",
        "description": "PRIVILEGED: commit a task to a cycle (writes the append-only commit-ledger; omit sprint_id to pull it — the ledger row is stamped 'dropped', never deleted). Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "sprint_id": {"type": "string", "description": "Cycle/sprint id; omit to drop the task from its cycle."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "close_sprint",
        "description": "PRIVILEGED: close a sprint/cycle — stamps each committed task's outcome (delivered | carried | dropped) in the commit-ledger; unfinished tasks move to the next sprint or the icebox (carry-overs re-compete at standup, they never auto-roll). Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"sprint_id": {"type": "string"}, "next_sprint_id": {"type": "string"}},
            "required": ["sprint_id"],
        },
    },
    {
        "name": "finish_sprint",
        "description": "PRIVILEGED: the 🏁 Finish-Sprint roll-forward on the ACTIVE cycle — archives accepted (done+reviewed) and rejected work, carries the unfinished pile (pending + done-unreviewed + review) into the next cycle, closes the active one, activates next, and guarantees a +2 planning slot. Distinct from the auto roll_cycle sweeper (which empties to the icebox). No args — acts on the active sprint. Operator scope only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": "PRIVILEGED: create a project. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "slug": {"type": "string"}, "description": {"type": "string"}},
            "required": ["name", "slug"],
        },
    },
    {
        "name": "create_epic",
        "description": "RETIRED: epics were folded into projects (m03). Returns {error: epics_folded}. Create a project (create_project) or a task under one instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project id or slug."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "initiative_id": {"type": "string", "description": "Optional roadmap initiative this epic rolls up into."},
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "assign_task_epic",
        "description": "RETIRED: epics were folded into projects (m03). Returns {error: epics_folded}. Re-home a task with update_task/assign_task_project; tasks.epic_id is frozen audit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "epic_id": {"type": "string", "description": "Epic id; omit to clear the task's epic."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "edit_roadmap",
        "description": "RETIRED: initiatives were folded into projects (m03). Returns {error: initiatives_folded}. The roadmap fields (tier, quarter, health, confidence, why, success_check) live on the PROJECT now — use update_project; list_initiatives/get_initiative stay readable as archive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "initiative_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "owner": {"type": "string"},
                "project_id": {"type": "string"},
                "status": {"type": "string"},
                "progress": {"type": "integer", "description": "Rejected for an initiative with >=1 epic (derived)."},
                "tier": {"type": "string", "enum": ["commit", "bet", "explore"]},
                "quarter": {"type": "string", "description": "Calendar quarter, YYYY-Q[1-4] (e.g. 2026-Q3)."},
                "confidence": {"type": "string"},
                "health": {"type": "string", "enum": ["on-track", "at-risk", "off-track"]},
                "why": {"type": "string"},
                "success_check": {"type": "string"},
            },
            "required": ["initiative_id"],
        },
    },
    # --- Parallel orchestration (role / spec / ledger — default scope) ---
    {
        "name": "get_spec_slice",
        "description": "ORIENT: pull ONLY your role's slice of a feature's shared spec (the `## @all` shared section + your `## @<role>` section). Call this at the start of a task instead of asking for the whole spec — keeps your context lean and aligned with the operator's source of truth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "The feature/spec key (see list_specs)."},
                "role": {"type": "string", "description": "Your role: implementation|verification|docs|planning|review."},
            },
            "required": ["feature"],
        },
    },
    {
        "name": "list_specs",
        "description": "ORIENT: list the feature specs the operator controls (feature key, roles covered, last updated).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_ledger",
        "description": "VALIDATE ONLY: record the VERIFICATION sign-off to the task ledger — {summary, files_modified, risks, status}, role='verification' required. Rejected for the implementing agent/session (never self-review); implementers report via report_result instead. status='passed' routes the task through the accept/escalate rule; status='failed' counts a strike and auto-aborts after 3.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "summary": {"type": "string", "description": "What you did / the outcome."},
                "files_modified": {"type": "array", "items": {"type": "string"}, "description": "Paths you changed."},
                "risks": {"type": "array", "items": {"type": "string"}, "description": "Known risks / things to verify."},
                "status": {"type": "string", "enum": ["passed", "failed", "blocked", "partial"], "default": "passed"},
                "agent": {"type": "string"},
                "session_key": {"type": "string", "description": "Your session id/name, if known."},
                "role": {"type": "string"},
            },
            "required": ["task_id", "summary"],
        },
    },
    # --- Verb-audit Tier 3: completeness + complements ---
    {
        "name": "get_account_chain",
        "description": "ORIENT: the LATERAL CRM view — one account with all its contacts and deals (+ open/won value). get_deal_chain goes down the spine; this answers 'what is our whole relationship here?'.",
        "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}},
                        "required": ["account_id"]},
    },
    {
        "name": "delete_task",
        "description": "PRIVILEGED: hard-delete a mistake/duplicate/test task. REFUSES accepted work (done+reviewed is history); the event spine keeps a task_deleted tombstone. Operator scope only.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"]},
    },
    {
        "name": "list_deal_events",
        "description": "ORIENT: a deal's commercial-interaction history (stage changes, touches, growth updates, notes) — the deal_events spine, newest first.",
        "inputSchema": {"type": "object",
                        "properties": {"deal_id": {"type": "string"},
                                       "limit": {"type": "integer", "description": "Max events (default 50, cap 500)."}},
                        "required": ["deal_id"]},
    },
    {
        "name": "delete_deal",
        "description": "PRIVILEGED: hard-delete a mistake/duplicate/test deal. REFUSES won deals (closed revenue is history) and deals with sub-deals; cleans sidecar rows. Operator scope only.",
        "inputSchema": {"type": "object", "properties": {"deal_id": {"type": "string"}},
                        "required": ["deal_id"]},
    },
    {
        "name": "delete_project",
        "description": "PRIVILEGED: hard-delete an EMPTY project. REFUSES if it still owns tasks (re-home them first) and refuses proj_inbox. Operator scope only.",
        "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}},
                        "required": ["project_id"]},
    },
    {
        "name": "get_sprint",
        "description": "ORIENT: one sprint/cycle with its tasks (status/priority ordered).",
        "inputSchema": {"type": "object", "properties": {"sprint_id": {"type": "string"}},
                        "required": ["sprint_id"]},
    },
    {
        "name": "get_velocity",
        "description": "ORIENT: per-cycle committed vs velocity (accepted human-intent tasks — the cycle_velocity VIEW). Throughput trend.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_graph",
        "description": "ORIENT: browse the join-index graph — stats (default), label search (q), or subgraph expansion (node + hops). recall() searches; this shows STRUCTURE.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "node": {"type": "string"},
                           "hops": {"type": "integer", "default": 2}},
        },
    },
    {
        "name": "rebuild_graph",
        "description": "PRIVILEGED: re-ingest the join-index from all six stores (after schema changes / bulk imports). Operator scope only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_usage",
        "description": "ORIENT: token/cost budget — unified cross-provider usage (Claude Max + Ollama Cloud + future providers). Returns per-provider breakdowns + a combined roll-up (total tokens, cost, per-provider % share). Check before starting a long task.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "archive_project",
        "description": "PRIVILEGED: retire a project (stamps archived_at; open-task count surfaced, not blocked). Operator scope only.",
        "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}},
                        "required": ["project_id"]},
    },
    {
        "name": "register_agent",
        "description": "PRIVILEGED: onboard a fleet agent into the registry (idempotent on name; trust stays EARNED — registration grants no grade). Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                           "host": {"type": "string"}, "skills": {"type": "string"},
                           "notes": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "resolve_input",
        "description": "DECLARE: mark an input_needed session event answered/obsolete — clears it from the Needs-you queue.",
        "inputSchema": {"type": "object", "properties": {"event_id": {"type": "integer"}},
                        "required": ["event_id"]},
    },
    {
        "name": "get_health",
        "description": "ORIENT: platform vitals in one call — DB reachable, task counts by status, inbox backlog, active cycle, review queue depth.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Verb-audit Tier 2: the UX-friction gaps ---
    {
        "name": "get_initiative",
        "description": "ORIENT: one initiative in full detail — why, success_check, health, quarter, tier + its event history.",
        "inputSchema": {"type": "object", "properties": {"initiative_id": {"type": "string"}},
                        "required": ["initiative_id"]},
    },
    {
        "name": "get_initiative_drilldown",
        "description": "ORIENT: the Initiative→Epics→Cycles→Tasks tree — what's under a bet before you claim work on it.",
        "inputSchema": {"type": "object", "properties": {"initiative_id": {"type": "string"}},
                        "required": ["initiative_id"]},
    },
    {
        "name": "add_deal_event",
        "description": "PRIVILEGED: log a free-form commercial interaction on a deal (kind: call|meeting|email|note|…, plus a note). The deal history between stage changes. Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}, "kind": {"type": "string"},
                           "note": {"type": "string"}, "agent": {"type": "string"}},
            "required": ["deal_id", "kind"],
        },
    },
    {
        "name": "update_epic",
        "description": "RETIRED: epics were folded into projects (m03). Returns {error: epics_folded}. Edit the project (update_project) instead.",
        "inputSchema": {
            "type": "object",
            "properties": {"epic_id": {"type": "string"}, "title": {"type": "string"},
                           "description": {"type": "string"},
                           "status": {"type": "string", "enum": ["open", "closed"]}},
            "required": ["epic_id"],
        },
    },
    {
        "name": "update_task",
        "description": "PRIVILEGED: edit the card itself — title, body, priority, due_date, project_id (status still moves via update_task_status). Setting project_id re-homes the task (triage) — FK-validated. Changes land as an audited task_updated event. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "title": {"type": "string"},
                           "body": {"type": "string"}, "priority": {"type": "integer"},
                           "due_date": {"type": "string", "description": "YYYY-MM-DD."},
                           "project_id": {"type": "string", "description": "Re-home the task to this project (triage)."}},
            "required": ["task_id"],
        },
    },
    {
        "name": "list_agents",
        "description": "ORIENT: the fleet registry — every agent with earned trust grade, outcome history, and overrides.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_agent_status",
        "description": "ORIENT: live fleet status — who's busy/idle, last activity, current tasks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_task_links",
        "description": "ORIENT: a task's dependency links (parents/children in the task_links DAG) — a decomposed task can see its siblings.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"]},
    },
    {
        "name": "bulk_accept",
        "description": "PRIVILEGED: accept MANY finished tasks in one sanctioned call — each routes through accept_task, so every accept stamps done+reviewed AND the human-gate verification row (the raw-SQL bulk path that bypassed the ratchet is exactly what this replaces). Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["task_ids"],
        },
    },
    {
        "name": "reconcile_sprint_ledger",
        "description": "PRIVILEGED: repair sprint ledger drift — insert missing task_sprints rows for forward orphans. Idempotent. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # --- Verb-audit Tier 1: the critical read/create gaps ---
    {
        "name": "create_initiative",
        "description": "RETIRED: initiatives were folded into projects (m03). Returns {error: initiatives_folded}. Originate a quarterly bet with create_project, then update_project(quarter=YYYY-Q[1-4], tier=commit|bet|explore).",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "project_id": {"type": "string"},
                           "tier": {"type": "string", "enum": ["commit", "bet", "explore"]},
                           "quarter": {"type": "string"}, "health": {"type": "string"},
                           "confidence": {"type": "string"}, "why": {"type": "string"},
                           "success_check": {"type": "string"}, "description": {"type": "string"}},
            "required": ["title", "project_id"],
        },
    },
    {
        "name": "list_accounts",
        "description": "ORIENT: the account portfolio (with contact/deal counts). The CRM read the audit found missing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_contacts",
        "description": "ORIENT: contacts, optionally scoped to one account — who we know and how to reach them (phone/whatsapp/linkedin + source).",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
        },
    },
    {
        "name": "get_task_runs",
        "description": "ORIENT: a task's execution history — every run attempt with step (plan/code/validate), outcome (completed/crashed/reclaimed/…), and error. Debugging stops being blind.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_ledger",
        "description": "ORIENT: a task's verification records — who verified, passed (contract exit code), files touched, risks noted. The governance read-back.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    # --- Phase 6: CRM (the top of the spine) ---
    {
        "name": "list_deals",
        "description": "ORIENT: the commercial pipeline — deals by stage with account, contact (+ its source), and linked initiative (+ its DERIVED progress via get_deal_chain). A won deal stays 'won' forever; 'delivered' is legacy READ vocabulary (0 rows) — delivered work is projects.status='delivered', reachable via get_pipeline's derived history bucket.",
        "inputSchema": {
            "type": "object",
            "properties": {"stage": {"type": "string", "enum": ["lead", "engaged", "qualified", "demo", "proposal", "stalled", "won", "delivered", "lost"]}},
        },
    },
    {
        "name": "get_deal_chain",
        "description": "ORIENT: the FULL spine for one deal — Deal→Initiative→Epics→Tasks→Runs(+verification)→Commits. The end-to-end trace from customer signal to the agent run that shipped it.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "create_account",
        "description": "PRIVILEGED: create a CRM account (idempotent on name). Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "domain": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_contact",
        "description": "PRIVILEGED: create a CRM contact under an account, with reachability (phone/whatsapp/linkedin_url) and signal provenance (source ∈ linkedin|whatsapp|referral|event|website|other + source_notes). Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}, "name": {"type": "string"},
                           "email": {"type": "string"}, "role": {"type": "string"},
                           "phone": {"type": "string"}, "whatsapp": {"type": "string"},
                           "linkedin_url": {"type": "string"},
                           "source": {"type": "string", "enum": ["linkedin", "whatsapp", "referral", "event", "website", "other"]},
                           "source_notes": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["account_id", "name"],
        },
    },
    {
        "name": "create_deal",
        "description": "PRIVILEGED: create a deal (customer signal). Link initiative_id to close the strategy join at birth; source records where the signal entered. Stage ∈ lead|engaged|qualified|demo|proposal|stalled|won|lost (stalled = icebox; won = terminal commercial success, and stays won). 'delivered' is RETIRED as a stage — delivery is projects.status, so it is refused server-side. Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}, "title": {"type": "string"},
                           "stage": {"type": "string", "enum": ["lead", "engaged", "qualified", "demo", "proposal", "stalled", "won", "lost"]},
                           "value": {"type": "number"}, "currency": {"type": "string"},
                           "contact_id": {"type": "string"}, "initiative_id": {"type": "string"},
                           "source": {"type": "string", "enum": ["linkedin", "whatsapp", "referral", "event", "website", "other"]},
                           "notes": {"type": "string"}, "product_id": {"type": "string"}},
            "required": ["account_id", "title"],
        },
    },
    {
        "name": "update_deal",
        "description": "PRIVILEGED: move a deal's stage (first lost entry stamps closed_at; reopening clears it), set value/notes, or link/clear its initiative. Every change is a deal_event. Two stages are NOT reachable from here and are refused server-side: 'delivered' is retired (delivery is projects.status), and 'won' is a human conversion — propose it and let the operator tap Entregar in the dashboard. Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"},
                           "stage": {"type": "string", "enum": ["lead", "engaged", "qualified", "demo", "proposal", "stalled", "lost"]},
                           "value": {"type": "number"}, "initiative_id": {"type": "string"},
                           "notes": {"type": "string"}, "clear_initiative": {"type": "boolean"},
                           "product_id": {"type": "string"}, "clear_product": {"type": "boolean"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "update_contact",
        "description": "PRIVILEGED: update a CRM contact's fields (name, email, role, phone, whatsapp, linkedin_url, source, source_notes, notes) or reassign to a different account (account_id, FK-validated). PATCH semantics — only supplied fields are written. Operator/Hermes scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {"contact_id": {"type": "string"},
                           "name": {"type": "string"}, "email": {"type": "string"},
                           "role": {"type": "string"}, "notes": {"type": "string"},
                           "phone": {"type": "string"}, "whatsapp": {"type": "string"},
                           "linkedin_url": {"type": "string"},
                           "source": {"type": "string", "enum": ["linkedin", "whatsapp", "referral", "event", "website", "other"]},
                           "source_notes": {"type": "string"},
                           "account_id": {"type": "string"}},
            "required": ["contact_id"],
        },
    },
    # --- Phase 5: unified memory recall ---
    {
        "name": "recall",
        "description": "ORIENT: the ONE memory read path across all six stores (kanban, roadmap, cycles, verification ledger, knowledge notes, CC memory, changelog, Obsidian titles). Graph search → authoritative source → {fact, source, ref, staleness}. Trust 'fresh' facts; re-verify 'stale' ones before acting. Pass task_id to prepend that task's context neighborhood.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you're trying to remember/find."},
                "project_id": {"type": "string", "description": "Optional project to prefer."},
                "task_id": {"type": "string", "description": "Optional task whose neighborhood to prepend."},
                "k": {"type": "integer", "description": "Max results (default 8).", "default": 8},
            },
            "required": ["query"],
        },
    },
    # --- Memory upgrade (Phase 2/3): contradiction gate, evolution, decay, metabolism ---
    {
        "name": "contradiction_check",
        "description": "ORIENT: keyword contradiction gate (MemClaw) — does new_fact negate/supersede any of existing_facts? Returns {contradicts, which, reason}. Deterministic (no LLM). Run BEFORE superseding a memory to avoid silently dropping a real contradictory write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "new_fact": {"type": "string", "description": "The candidate fact you're about to write."},
                "existing_facts": {"type": "array", "items": {"type": "string"},
                                   "description": "The active facts to check against."},
            },
            "required": ["new_fact", "existing_facts"],
        },
    },
    {
        "name": "find_related",
        "description": "ORIENT: graph nodes related to a query by label word-overlap (A-MEM neighbour search). Use to find the existing facts that a new write might evolve or supersede.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_metabolism_stats",
        "description": "ORIENT: memory metabolism metrics (arXiv:2604.12034) — last-24h inputs_processed / facts_distilled / memories_evicted / decay_triggered plus total_active / total_archived. The digestive-system view of the graph's health.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "evolve_node",
        "description": "PRIVILEGED: merge properties into a graph node (A-MEM memory evolution) and re-stamp last_verified. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "properties": {"type": "object", "description": "Property keys to merge into the node."},
            },
            "required": ["node_id", "properties"],
        },
    },
    {
        "name": "archive_stale",
        "description": "PRIVILEGED: decay activation — archive every node past its type-specific TTL (status=archived; kept in DB, never deleted). Returns the count archived. Operator scope only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 4: runnable acceptance (the VALIDATE step) ---
    {
        "name": "run_contract",
        "description": "VALIDATE: run the task's operator-authored acceptance contract (tasks.contract_cmd or the ## Contract fence) with the deterministic runner. passed = exit code 0; writes THE authoritative role='verification' ledger row. Refused for the implementing agent (never self-review) — run this from a separate verification-role session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent": {"type": "string", "description": "Your agent name (must differ from the implementer)."},
                "session_key": {"type": "string", "description": "Your session id (proves a separate session)."},
            },
            "required": ["task_id", "agent"],
        },
    },
    {
        "name": "set_contract",
        "description": "PRIVILEGED: author a task's runnable acceptance contract (a shell command whose exit code IS pass/fail). The operator writes the contract BEFORE the work is graded (Tier-0 spec gate) — a worker never writes its own. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "contract_cmd": {"type": "string", "description": "Shell command; empty clears."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_run_envelope",
        "description": "ORIENT: read a task's durable four-brake execution envelope and evidence receipt.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "set_run_envelope",
        "description": "PRIVILEGED: declare contract, matched practice, deterministic preflight context and four brakes before execution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "contract_cmd": {"type": "string"},
                "practice_text": {"type": "string"},
                "host": {"type": "string", "enum": ["hermes", "orchestrator", "claude", "codex", "opencode", "open_design"]},
                "context": {"type": "object"}
            },
            "required": ["task_id", "contract_cmd", "practice_text", "host", "context"]
        },
    },
    # --- Phase 3: the Canvas / Today (plan verbs) ---
    {
        "name": "get_day_plan",
        "description": "ORIENT: the Today canvas, composed server-side — do (planned tasks), review (awaiting accept), needs_you (blocked + input asks), later (unplanned drawer), overdue. The same view the dashboard's Today tab renders. Pass candidates=true at standup to also get the proposed plan (overdue + carry-overs + cycle tasks + review/blocked counts).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
                "candidates": {"type": "boolean", "description": "Include the standup's candidate plan."},
            },
        },
    },
    {
        "name": "wrap_day",
        "description": "PRIVILEGED: the 19:00 wrap-up write — stamps a carried_over event on every unfinished task in the day's plan (idempotent) and returns the digest (done/carried/review/blocked) to deliver. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
            },
        },
    },
    {
        "name": "plan_day",
        "description": "PRIVILEGED: commit the day's plan (the morning-standup confirm). Sets planned_for + plan_order for the given tasks; tasks previously planned that day but absent are unplanned. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"},
                             "description": "Task ids in plan order."},
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "plan_task",
        "description": "PRIVILEGED: plan/unplan ONE task — set planned_for (YYYY-MM-DD), plan_order, or due_date; clear=true unplans. Operator scope only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "planned_for": {"type": "string", "description": "YYYY-MM-DD."},
                "plan_order": {"type": "integer"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD ('' clears)."},
                "clear": {"type": "boolean", "description": "Unplan the task."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "set_session_role",
        "description": "DECLARE: register this session's role + the feature it's working on, so the fleet console shows what you are (implementation/verification/docs/planning/review). Also lets you opt into auto_compact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_key": {"type": "string", "description": "Your tmux session name or jsonl session id."},
                "role": {"type": "string", "description": "implementation|verification|docs|planning|review"},
                "feature": {"type": "string", "description": "The feature/spec key you're serving."},
                "project": {"type": "string"},
                "auto_compact": {"type": "boolean", "description": "Let the orchestrator auto-/compact you when large."},
            },
            "required": ["session_key"],
        },
    },
    # ===== CRM Growth Phase 1 — 40 tools (thin wrappers over dashboard/growth.py) =====
    # --- Group A: ICP + Products + Pipeline + Scorecard ---
    {
        "name": "get_icp",
        "description": "Get the effective Ideal Customer Profile config (industries, positioning, target revenue, avg ticket, close rate). Drives lead scoring + pipeline math.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_icp",
        "description": "Update ICP config. Accepts industries (list or comma-separated string), positioning, target_revenue, avg_ticket, close_rate (fraction in [0,1]).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "industries": {"type": "array", "items": {"type": "string"}, "description": "Target industries (list or comma-separated string)."},
                "positioning": {"type": "string"},
                "target_revenue": {"type": "number"},
                "avg_ticket": {"type": "number"},
                "close_rate": {"type": "number", "description": "Fraction in [0,1] (>1 treated as a percentage)."},
            },
        },
    },
    {
        "name": "list_products",
        "description": "List all productized offers (seeds the 3 default offers on first call of an empty catalog).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_product",
        "description": "Create a product/offer in the catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "value_ladder_stage": {"type": "string", "description": "Value-ladder stage (e.g. iman, low, mid, high)."},
                "fixed_price_mxn": {"type": "number", "description": "Fixed price in MXN."},
                "ficha_html": {"type": "string", "description": "Product sheet HTML."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_product",
        "description": "Patch whitelisted fields on a product (name, description, value_ladder_stage, fixed_price_mxn, ficha_html).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "value_ladder_stage": {"type": "string"},
                "fixed_price_mxn": {"type": "number"},
                "ficha_html": {"type": "string"},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "delete_product",
        "description": "Delete a product from the catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "get_pipeline",
        "description": "Get the full CRM pipeline: deals grouped by stage, each carrying its initiative's derived progress.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_pipeline_math",
        "description": "Backward funnel math: revenue goal → clients → proposals → discovery calls → leads → touches, plus current pipeline coverage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "revenue_goal": {"type": "number"},
                "avg_ticket": {"type": "number"},
            },
        },
    },
    {
        "name": "get_pipeline_health",
        "description": "Touch-cadence alert triage over active deals: red (overdue next touch), yellow (cold 7+ days), blue (no next touch scheduled).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_forecast",
        "description": "30/60/90-day revenue forecast over active deals, bucketed by expected_close_date (explicit or auto-estimated from stage): overdue / 30d / 60d / 90d / beyond, with count, total value and weighted value per bucket.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cltv_cac",
        "description": "CLTV (avg_ticket × repeat × lifespan), CAC, and CLTV:CAC ratio per lead source, with a green/yellow/red rating.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_scorecard",
        "description": "Weekly scorecard — the weekly 5 (leads, touches, discovery calls, content, proposals) auto-derived from the week's events.",
        "inputSchema": {
            "type": "object",
            "properties": {"week": {"type": "string", "description": "ISO week (e.g. 2026-W28). Defaults to current week."}},
        },
    },
    {
        "name": "get_growth_loops",
        "description": "The three growth flywheels plus per-loop leads / conversion / ratio.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_lead",
        "description": "Quick-add a lead: creates an account + contact + deal (value-ladder 'iman' / stage 'lead'), rule-scored 0-100.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Contact name (required)."},
                "company": {"type": "string"},
                "source": {"type": "string", "description": "Lead source enum."},
                "loop": {"type": "string", "description": "Growth loop."},
                "notes": {"type": "string"},
                "engagement_score": {"type": "integer"},
                "industry": {"type": "string"},
                "value": {"type": "number"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "touch_deal",
        "description": "Log a sales touch on a deal: +1 touch count, stamp last_touch_date=today, suggest next_touch_date (+N days).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "note": {"type": "string"},
                "kind": {"type": "string", "description": "Touch kind (default 'touch'). 'warm_touch' = toque de generosidad and 'referral_ask' = pedido de referido — both count as touches AND land in the operator's scorecard layer.", "enum": ["touch", "warm_touch", "referral_ask", "discovery_call", "discovery", "call", "meeting"]},
                "next_in_days": {"type": "integer", "description": "Days until next suggested touch (default 7)."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "score_deal",
        "description": "Upsert a deal's lead-scoring features and recompute deals.lead_score (0-100).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "account_type": {"type": "string"},
                "source": {"type": "string"},
                "product_interest": {"type": "string"},
                "engagement_score": {"type": "integer"},
                "industry": {"type": "string"},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "score_all_leads",
        "description": "Recompute lead scores for all non-closed deals in one batch. Returns counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_fireflies_signals",
        "description": "Return cached Fireflies meeting signals (talk ratio, questions, filler words, action items, sentiment) for a deal.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "fetch_fireflies_for_deal",
        "description": "Trigger a Fireflies data fetch for a deal via the dashboard API. Requires a configured FIREFLIES_API_KEY. The response reports `scanned` (transcripts actually examined) next to `stored`, so 0-matched-of-40 reads differently from a blind 0-of-0.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Transcript window to scan (default 25 ≈ the last ~12 days at the operator's meeting rate; widen for backfill — clamped to the API's real ceiling of 50)."},
                "since": {"type": "string", "description": "Optional ISO date floor (YYYY-MM-DD) for the scan."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "get_readiness",
        "description": "Readiness dashboard view: all active deals with multi-dimensional readiness scores (buyer/product/market, 0-100 each), bucketed nurture (0-30) / qualified (31-60) / sales_ready (61-85) / hot (86-100), each with its next best action on the sprint ladder.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "score_readiness",
        "description": "Compute + persist a deal's multi-dimensional readiness score: buyer (intent/authority/budget/urgency), product (offer packaged for this prospect), market (ICP fit/source/velocity/sentiment), time-decayed. Writes deals.readiness_score + readiness_dimensions.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "score_readiness_from_fireflies",
        "description": "Parse a deal's STORED Fireflies meetings for readiness signals (budget mentions, urgency/timeline language, technical question depth, decision-maker presence, sentiment — Spanish + English markers) and update its readiness dimensions. Run fetch_fireflies_for_deal first to pull fresh transcripts.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "update_deal_growth",
        "description": "Set a deal's growth attributes (value_ladder_stage / growth_loop / lead_source / next_touch_date / product_id) — validated enums. Passing product_id auto-derives value_ladder_stage from the product's rung unless value_ladder_stage is also given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "value_ladder_stage": {"type": "string"},
                "growth_loop": {"type": "string"},
                "lead_source": {"type": "string"},
                "next_touch_date": {"type": "string", "description": "YYYY-MM-DD."},
                "product_id": {"type": "string", "description": "Catalog product id ('' to clear); auto-syncs the value-ladder rung."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "get_funnel_trend",
        "description": "Last N weeks of lead→discovery→proposal→won conversion snapshots (seeds the first from current deals if none exist).",
        "inputSchema": {
            "type": "object",
            "properties": {"weeks": {"type": "integer", "description": "Number of weeks (default 12)."}},
        },
    },
    {
        "name": "get_pipeline_temporal",
        "description": "Month-by-month pipeline flow: new deals, stage movements, won/lost counts and revenue closed. The temporal view of the pipeline.",
        "inputSchema": {
            "type": "object",
            "properties": {"months": {"type": "integer", "description": "Number of months (default 12, max 60)."}},
        },
    },
    # --- Group B: Content + Speaking + Time-blocks + Acquisition Costs ---
    {
        "name": "list_content",
        "description": "Content cadence (pieces per week + publishing streak) plus the raw pieces list for the content calendar.",
        "inputSchema": {
            "type": "object",
            "properties": {"weeks": {"type": "integer", "description": "Weeks of cadence history (default 8)."}},
        },
    },
    {
        "name": "create_content",
        "description": "Create a content piece on the calendar. publish_date defaults to today, status to 'idea'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "topic": {"type": "string"},
                "channel": {"type": "string", "description": "Content channel enum."},
                "growth_loop": {"type": "string"},
                "hook": {"type": "string"},
                "publish_date": {"type": "string", "description": "YYYY-MM-DD."},
                "status": {"type": "string", "description": "idea|draft|scheduled|published."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_content",
        "description": "Patch whitelisted fields on a content piece (title, topic, channel, growth_loop, hook, publish_date, status).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string"},
                "title": {"type": "string"},
                "topic": {"type": "string"},
                "channel": {"type": "string"},
                "growth_loop": {"type": "string"},
                "hook": {"type": "string"},
                "publish_date": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["content_id"],
        },
    },
    {
        "name": "delete_content",
        "description": "Delete a content piece.",
        "inputSchema": {
            "type": "object",
            "properties": {"content_id": {"type": "string"}},
            "required": ["content_id"],
        },
    },
    {
        "name": "list_speaking",
        "description": "List all speaking engagements (talks as attraction-loop generators).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_speaking",
        "description": "Create a speaking engagement.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "event_name": {"type": "string"},
                "event_date": {"type": "string", "description": "YYYY-MM-DD."},
                "status": {"type": "string"},
                "attraction_loop_status": {"type": "string"},
                "deal_id": {"type": "string", "description": "Optional linked deal."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_speaking",
        "description": "Patch whitelisted fields on a speaking engagement.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "speaking_id": {"type": "string"},
                "title": {"type": "string"},
                "event_name": {"type": "string"},
                "event_date": {"type": "string"},
                "status": {"type": "string"},
                "attraction_loop_status": {"type": "string"},
                "deal_id": {"type": "string"},
            },
            "required": ["speaking_id"],
        },
    },
    {
        "name": "delete_speaking",
        "description": "Delete a speaking engagement.",
        "inputSchema": {
            "type": "object",
            "properties": {"speaking_id": {"type": "string"}},
            "required": ["speaking_id"],
        },
    },
    {
        "name": "list_time_blocks",
        "description": "The weekly schedule of role-specialized time blocks (seeds 5 defaults on first call). Each carries a derived `done` flag for the current week.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_time_block",
        "description": "Create a weekly time block. day_of_week 0-6 (0=Mon), start/end as HH:MM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "day_of_week": {"type": "integer", "description": "0-6 (0=Mon)."},
                "start_time": {"type": "string", "description": "HH:MM."},
                "end_time": {"type": "string", "description": "HH:MM."},
                "role": {"type": "string"},
                "label": {"type": "string"},
                "active": {"type": "boolean"},
            },
            "required": ["day_of_week"],
        },
    },
    {
        "name": "update_time_block",
        "description": "Patch a time block. Special key `done` (bool) marks/unmarks the block done for the CURRENT week.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "block_id": {"type": "string"},
                "day_of_week": {"type": "integer"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "role": {"type": "string"},
                "label": {"type": "string"},
                "active": {"type": "boolean"},
                "done": {"type": "boolean", "description": "Mark/unmark done for the current week."},
            },
            "required": ["block_id"],
        },
    },
    {
        "name": "delete_time_block",
        "description": "Delete a time block.",
        "inputSchema": {
            "type": "object",
            "properties": {"block_id": {"type": "string"}},
            "required": ["block_id"],
        },
    },
    {
        "name": "list_acquisition_costs",
        "description": "List monthly acquisition costs per lead source.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_acquisition_cost",
        "description": "Record a monthly acquisition cost for a lead source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Lead source enum."},
                "cost_mxn": {"type": "number"},
                "month": {"type": "string", "description": "YYYY-MM."},
                "notes": {"type": "string"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "delete_acquisition_cost",
        "description": "Delete an acquisition cost entry.",
        "inputSchema": {
            "type": "object",
            "properties": {"cost_id": {"type": "string"}},
            "required": ["cost_id"],
        },
    },
    # --- Group C: Nurture + Fireflies + Milestones + Funnel Snapshot ---
    {
        "name": "get_nurture",
        "description": "A deal's 5-touch nurture sequence plus the next suggested touch date.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "quick_add_contact",
        "description": "Growth Operating Framework — fastest capture: name + company → account + contact + lead-stage deal. Optional email, phone, whatsapp, linkedin_url, source, notes, loop, lead_source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Contact name (required)."},
                "company": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "whatsapp": {"type": "string"},
                "linkedin_url": {"type": "string"},
                "source": {"type": "string"},
                "notes": {"type": "string"},
                "loop": {"type": "string", "description": "Growth loop (autoridad|referido|producto)."},
                "lead_source": {"type": "string", "description": "Lead source enum (linkedin|evento|referral|cold_email|inbound)."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_cadence_status",
        "description": "Per-deal nurture cadence: steps, next due date, overdue count, and compliance %.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "get_monthly_strategic_view",
        "description": "C9 strategic monthly view: pipeline math, revenue mix, growth loops, channel metrics, scorecard rollup, scoring accuracy, and plan milestones progress.",
        "inputSchema": {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM. Defaults to current month."}},
        },
    },
    {
        "name": "get_conversion_path",
        "description": "Where a deal sits on the value ladder and the measured probability of moving to the next rung.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "generate_nurture",
        "description": "(Re)generate a 5-touch Hook nurture sequence from the deal's name/source/stage.",
        "inputSchema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
        },
    },
    {
        "name": "update_nurture",
        "description": "Update a nurture step's status (pending/sent/skipped).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "status": {"type": "string", "description": "pending|sent|skipped."},
            },
            "required": ["step_id"],
        },
    },
    {
        "name": "get_fireflies_analytics",
        "description": "Behavioral coaching: talk-listen ratio over the last N Fireflies meetings + coaching summary. Fail-soft (available:false without Fireflies).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Meetings to analyze (default 10)."}},
        },
    },
    {
        "name": "get_coaching",
        "description": "Behavioral coaching metrics: last N meetings with talk%, filler words, longest monologue vs targets, trend + sparkline + rotating tip. Fail-soft.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Meetings to include (default 10)."}},
        },
    },
    {
        "name": "get_meeting_feedback",
        "description": "Get feedback for a specific Fireflies meeting by transcript ID, or search recent meetings by participant name/title. Returns talk ratio, summary, action items, sentences, and coaching signals. Use when user asks 'give me feedback on my call with X'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript_id": {"type": "string", "description": "Fireflies transcript ID (e.g. 01KXRQ63K6VX61NKGBT37AJD5G). If provided, fetches that specific meeting."},
                "search": {"type": "string", "description": "Search string to filter by title or participant (case-insensitive). Used when transcript_id is not provided."},
                "limit": {"type": "integer", "description": "Max meetings to list when searching (default 20)."},
            },
        },
    },
    {
        "name": "list_milestones",
        "description": "All 90-day plan milestones grouped by phase plus per-phase/overall progress (seeds the 3-phase plan on first call).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_milestone",
        "description": "Toggle (or set explicitly with `completed`) a plan milestone's completed flag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "milestone_id": {"type": "string"},
                "completed": {"type": "boolean", "description": "Set explicitly; omit to toggle."},
            },
            "required": ["milestone_id"],
        },
    },
    {
        "name": "capture_funnel",
        "description": "Manually capture a conversion-funnel snapshot for the current (or given) week.",
        "inputSchema": {
            "type": "object",
            "properties": {"week_start": {"type": "string", "description": "YYYY-MM-DD Monday. Defaults to current week."}},
        },
    },
    # --- Phase 2: Session Control. Programmatic control of Claude Code sessions
    # (read history/output, send/resend commands, revive/kill/compact, discover
    # and link tasks, session events). Proxies the live dashboard API. ---
    {
        "name": "get_session_history",
        "description": "Get the recent prompt/instruction history recorded for a Claude Code session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on (e.g. 'local', a Tailscale hostname)."},
                "session_name": {"type": "string", "description": "Session name or id (tmux name / UUID)."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "get_session_output",
        "description": "Get recent output from a session — a live terminal capture or parsed Claude Code transcript. Host and session name come from get_sessions' `terminals`; output is bounded (`lines` clamped to 1-500).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on ('local' or any remote host configured in dashboard/sessions.py)."},
                "session_name": {"type": "string", "description": "Session name or id (tmux name from get_sessions.terminals / UUID)."},
                "lines": {"type": "integer", "description": "How many recent lines to return (default 50, clamped to 1-500)."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "send_to_session",
        "description": "Send a prompt/command to an interactive Claude/Codex session on any machine (records it in the session's prompt history; delivery is validated by tmux-send and fails closed). PRIVILEGED — direct remote control.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on ('local' or any remote host configured in dashboard/sessions.py)."},
                "session_name": {"type": "string", "description": "Session name or id (tmux name from get_sessions.terminals / UUID)."},
                "text": {"type": "string", "description": "The prompt/command to inject."},
            },
            "required": ["host", "session_name", "text"],
        },
    },
    {
        "name": "resend_last",
        "description": "Re-send the last recorded instruction to an idle session (the supervisor's nudge). PRIVILEGED.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "revive_session",
        "description": "Revive a session on its origin machine. PRIVILEGED.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "kill_session",
        "description": "Kill a live tmux session (idempotent: already-gone is not an error). PRIVILEGED — destructive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "prune_transcripts",
        "description": "Hide transcript-only sessions idle beyond a threshold from the listing (display hygiene; files untouched, a pruned session that wakes auto-unhides). PRIVILEGED.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "older_than_days": {"type": "number", "description": "Prune transcript sessions idle longer than this many days (default 2 days / 48h)."},
            },
        },
    },
    {
        "name": "compact_session",
        "description": "Send /compact to a session to trigger context compaction. PRIVILEGED.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "list_coordinators",
        "description": "Derived Coordinators view — Code / Research / Commercial sub-agents, each with the tasks it's handling, last activity, and a green/yellow/red signal.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_session_tasks",
        "description": "List the kanban tasks hard-linked to a session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id."},
            },
            "required": ["host", "session_name"],
        },
    },
    {
        "name": "link_task_session",
        "description": "Bind a task to a Claude Code session (sets task.session_id). PRIVILEGED — graph structure write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to link."},
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id to link the task to."},
            },
            "required": ["task_id", "session_name"],
        },
    },
    {
        "name": "list_compact_candidates",
        "description": "List live sessions whose context is large enough to warrant /compact.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_session_meta",
        "description": "Get every registered session's role/feature/policy (+ role counts and tag summary). Optionally filter by host and/or session_name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional: only sessions on this host."},
                "session_name": {"type": "string", "description": "Optional: only sessions whose key matches this name."},
            },
        },
    },
    {
        "name": "create_session_event",
        "description": "Log a lifecycle event for a session (e.g. input_needed / note). The agent-side hook for a session to push its status up.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id (used as the event's session_key)."},
                "event_type": {"type": "string", "description": "Event kind (e.g. 'note', 'input_needed', 'stop')."},
                "message": {"type": "string", "description": "Human-readable event message / payload."},
            },
            "required": ["host", "session_name", "event_type", "message"],
        },
    },
    {
        "name": "list_session_events",
        "description": "List recent session lifecycle events (+ pending input). Optionally filter by session and unresolved-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional host filter (informational)."},
                "session_name": {"type": "string", "description": "Optional: only events for this session (session_key)."},
                "unresolved_only": {"type": "boolean", "description": "Only return unresolved events."},
            },
        },
    },
    {
        "name": "resolve_session_event",
        "description": "Mark a session event as resolved (clears a pending input/notification).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event id to resolve."},
            },
            "required": ["event_id"],
        },
    },
    # --- Phase 3: Task Lifecycle · Group A (accept/reject/abort/fail) ---
    {
        "name": "accept_task",
        "description": "Operator accepts an agent's completion — the human gate. Stamps done+reviewed so the card leaves the Inbox. PRIVILEGED — operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to accept."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "reject_task",
        "description": "Operator rejects a task — the negative human gate. Sets status='rejected' with an optional reason. PRIVILEGED — operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to reject."},
                "reason": {"type": "string", "description": "Optional reason for the rejection."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "review_accept",
        "description": "DEFAULT SCOPE: accept a task that has passed automated review. Only works on tasks in 'review' status that have a result_reported event with passed=true. This lets the auto-review sweep close the loop without operator intervention. Routes through the same accept_task endpoint (stamps done+reviewed). NOT privileged — any agent can call it, but only on tasks already in review with a passing result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to accept from review."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "abort_task",
        "description": "Abort an in-progress task (releases the claim, optionally kills its session). PRIVILEGED — operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to abort."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "fail_task",
        "description": "Record a failed attempt on a task (increments strikes; auto-aborts on the 3rd — the 3-strike rule). PRIVILEGED — operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id that failed."},
                "reason": {"type": "string", "description": "Optional error/reason for the failure."},
            },
            "required": ["task_id"],
        },
    },
    # --- Phase 3: Task Lifecycle · Group B (heartbeat + context reads) ---
    {
        "name": "heartbeat",
        "description": "Refresh a task's heartbeat (proves the claim holder is alive; the ownership guard only accepts the owner's beat).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to beat."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_context",
        "description": "One-call context packet for an entity drawer — ancestors + entity + children for a task | project | initiative | deal | session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "description": "One of task | project | initiative | deal | session."},
                "entity_id": {"type": "string", "description": "The entity's id."},
            },
            "required": ["entity_type", "entity_id"],
        },
    },
    {
        "name": "list_icebox",
        "description": "List icebox tasks — parked work not committed to any cycle.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_delivered",
        "description": "List delivered sprints/cycles (closed work, the shipped record).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 3: Task Lifecycle · Group C (project/roadmap/plan reads + comment delete) ---
    {
        "name": "get_project_detail",
        "description": "Full project detail — project + tasks-by-column + stats + initiatives + active-cycle id. Accepts an id or slug.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project id or slug."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_initiative_events",
        "description": "An initiative's audit spine — its roadmap event timeline (mirrors task history).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "initiative_id": {"type": "string", "description": "The initiative id."},
            },
            "required": ["initiative_id"],
        },
    },
    {
        "name": "get_day_plan_candidates",
        "description": "The standup's candidate plan — overdue + carry-overs + cycle tasks eligible for today.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_comment",
        "description": "Delete one comment by id. PRIVILEGED — operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer", "description": "The comment id to delete."},
            },
            "required": ["comment_id"],
        },
    },
    # --- Phase 4: Sprint/Cycle · Group A ---
    {
        "name": "get_sprint_tasks",
        "description": "List the tasks in a sprint/cycle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sprint_id": {"type": "string", "description": "The sprint/cycle id."},
            },
            "required": ["sprint_id"],
        },
    },
    {
        "name": "get_cycle_board",
        "description": "The active cycle's kanban board — committed tasks grouped by column, in board order.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cycles_calendar",
        "description": "The cycle calendar — past/active/upcoming cycles laid out by date.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "roll_cycle",
        "description": "Roll the active cycle to the next one — closes the expired cycle with outcomes and opens the next empty. PRIVILEGED — operator action.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_sprint",
        "description": "Hard-delete a sprint/cycle. PRIVILEGED — destructive operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sprint_id": {"type": "string", "description": "The sprint/cycle id to delete."},
            },
            "required": ["sprint_id"],
        },
    },
    {
        "name": "commit_cycle",
        "description": "Commit a set of tasks to a cycle at once (each routes through assign_task_sprint — one commit-ledger row per task). Pass sprint_id='icebox' to pull them all to the icebox. PRIVILEGED — operator action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sprint_id": {"type": "string", "description": "The cycle id to commit to (or 'icebox' to pull)."},
                "task_ids": {"type": "array", "items": {"type": "string"}, "description": "The task ids to commit."},
            },
            "required": ["sprint_id"],
        },
    },
    # --- Phase 4: Lakehouse · Group B ---
    {
        "name": "get_lakehouse_overview",
        "description": "Data-lakehouse overview — the agent operating layer's metrics/entities snapshot.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_mcp_manifest",
        "description": "Introspection — list every MCP tool this server exposes (names + descriptions).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 4: Specs + Ledger · Group C ---
    {
        "name": "write_spec",
        "description": "Write/overwrite the source-of-truth spec for a feature. PRIVILEGED — the operator authors specs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "The feature name (spec key)."},
                "content": {"type": "string", "description": "The full spec content (markdown)."},
            },
            "required": ["feature", "content"],
        },
    },
    {
        "name": "get_ledger",
        "description": "The global task ledger — the append-only orchestration event log (optionally filtered to one task).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Optional — restrict the ledger to one task."},
            },
        },
    },
    # --- Phase 4: Orchestration · Group D ---
    {
        "name": "orchestration_sweep",
        "description": "Run one sweeper pass on demand (auto-compact large sessions + auto-abort tasks past the failure limit). PRIVILEGED — operator action.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 5: Usage/Ops + Session Meta · Group A (usage) ---
    {
        "name": "list_usage_providers",
        "description": "Unified cross-provider usage summary — combined token totals, estimated cost, and per-provider share (Ollama + Claude, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_ollama_usage",
        "description": "Re-scrape ollama.com/settings on demand and return fresh usage (drives the browse daemon). PRIVILEGED — operator action.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_claude_usage",
        "description": "Force a cache-bust + re-fetch of the live Claude Max limits. PRIVILEGED — operator action.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_ollama_completion",
        "description": "Log an Ollama Cloud chat-completion usage event (model + optional prompt metadata). PRIVILEGED — usage write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "The Ollama Cloud model used."},
                "prompt": {"type": "string", "description": "Optional prompt text (stored as metadata)."},
            },
            "required": ["model"],
        },
    },
    {
        "name": "get_stats",
        "description": "Top-level orchestration stats snapshot (counts across tasks/sessions/etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 5: Ops/Health + Session Meta · Group B ---
    {
        "name": "get_recent_errors",
        "description": "Recent orchestration errors — the ops error feed.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_ops_status",
        "description": "Operational status snapshot — the health/ops overview for the orchestrator.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_session_meta",
        "description": "Set one metadata field on a session (role/feature/tag/notes/project/auto_compact/auto_abort) by key. PRIVILEGED — session config write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Host the session runs on."},
                "session_name": {"type": "string", "description": "Session name or id (used as session_key)."},
                "key": {"type": "string", "description": "The meta field to set (e.g. 'role', 'feature', 'tag', 'notes')."},
                "value": {"type": "string", "description": "The value to set for that field."},
            },
            "required": ["session_name", "key", "value"],
        },
    },
    # --- Parity fill: features the dashboard API/UI had but MCP did not ---
    {
        "name": "search",
        "description": "ORIENT: global omnibar search across tasks, deals, sessions, and flat memory (substring, case-insensitive). Each hit carries {type, id, title, subtitle, tab, entity}. The fast 'find any entity by a word' path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query."},
                "limit": {"type": "integer", "description": "Max hits per type (default 8, max 20).", "default": 8},
            },
            "required": ["q"],
        },
    },
    {
        "name": "get_memory",
        "description": "ORIENT: the flat agent/user memory stores (memory_view), categorized by content with capacity stats. Pure read of the on-disk memory files — the source behind the dashboard Memory tab. (Graph recall is `recall`; this is the flat index.)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_memory",
        "description": "PRIVILEGED: edit one flat memory entry in place, addressed by {source: agent|user, index, content}. The MCP twin of the Memory-tab inline editor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["agent", "user"], "description": "Which store (default agent)."},
                "index": {"type": "integer", "description": "Entry index within the store."},
                "content": {"type": "string", "description": "New content for the entry."},
            },
            "required": ["index", "content"],
        },
    },
    {
        "name": "delete_memory",
        "description": "PRIVILEGED: delete one flat memory entry (backs up the store first), addressed by {source, index}. The MCP twin of the Memory-tab delete.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["agent", "user"], "description": "Which store (default agent)."},
                "index": {"type": "integer", "description": "Entry index within the store."},
            },
            "required": ["index"],
        },
    },
    {
        "name": "get_stale_deals",
        "description": "ORIENT: CRM deals with no REAL touch inside `days` (default 7) — idle is measured on last_touch_date (record edits don't refresh it) and 'stalled' deals are included. The raw stale list behind the pipeline-health cadence alerts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Staleness threshold in days (default 7), measured on the touch clock.", "default": 7},
            },
        },
    },
    {
        "name": "list_crm_proposals",
        "description": "ORIENT: the propose-only CRM correction inbox (m27) — proposed touch/next_touch/amount updates derived from external evidence, awaiting the human gate. status='' lists all states.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "proposed (default) · applying · approved · dismissed · '' for all.", "default": "proposed"},
            },
        },
    },
    {
        "name": "propose_crm_update",
        "description": "DECLARE: file ONE CRM correction proposal from external evidence (e.g. the Thursday Gmail pass: a quote amount, a real touch, a committed next step). NEVER mutates the CRM — a human approves it in the dashboard. kind=touch payload {note?, evidence_date?} · next_touch {date} · amount {value}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["touch", "next_touch", "amount"]},
                "payload": {"type": "object", "description": "Per-kind payload (see description)."},
                "evidence_kind": {"type": "string", "enum": ["fireflies", "whatsapp", "manual"], "default": "manual"},
                "evidence_ref": {"type": "string", "description": "Stable evidence id (transcript id, gmail:<slug>, …) — the idempotency + sticky-dismiss key."},
            },
            "required": ["deal_id", "kind", "evidence_ref"],
        },
    },
    {
        "name": "derive_crm_proposals",
        "description": "DECLARE: sweep the Fireflies cache for meetings newer than each open deal's touch clock and file touch proposals (idempotent; dismissed is sticky; read-only on deals). Run fetch_fireflies_for_deal first — the cache only holds what was fetched.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_commercial_proposals",
        "description": "ORIENT: list the immutable revision ledger and send receipts for one deal. This is the client-facing proposal lifecycle, not the CRM correction inbox.",
        "inputSchema": {"type": "object", "properties": {"deal_id": {"type": "string"}}, "required": ["deal_id"]},
    },
    {
        "name": "register_commercial_proposal",
        "description": "DECLARE: register one proposal-workspace revision. Exact replay is idempotent; changing the artifacts requires a new revision. Does not send or move the deal.",
        "inputSchema": {"type": "object", "properties": {
            "deal_id": {"type": "string"}, "revision": {"type": "integer", "minimum": 1},
            "workspace_path": {"type": "string"}, "manifest_path": {"type": "string"},
            "proposal_path": {"type": "string"}, "prototype_path": {"type": "string"},
            "workspace_schema_version": {"type": "integer", "enum": [1, 2]},
            "evidence_manifest_path": {"type": "string"},
            "checker_report_path": {"type": "string"}, "quality_report_path": {"type": "string"}
        }, "required": ["deal_id", "revision", "workspace_path", "manifest_path", "proposal_path"]},
    },
    {
        "name": "verify_commercial_proposal",
        "description": "DECLARE: freeze the exact packaged revision using proposal-workspace's receipt and SHA-256 hashes. A verified revision cannot be overwritten.",
        "inputSchema": {"type": "object", "properties": {
            "packet_id": {"type": "string"}, "package_path": {"type": "string"},
            "manifest_sha256": {"type": "string"}, "package_sha256": {"type": "string"},
            "verification_receipt": {"type": "string"}, "receipt_sha256": {"type": "string"},
            "evidence_manifest_sha256": {"type": "string"},
            "checker_report_sha256": {"type": "string"},
            "quality_report_sha256": {"type": "string"},
            "quality_status": {"type": "string", "enum": ["pass", "fail"]}
        }, "required": ["packet_id", "package_path", "manifest_sha256", "package_sha256", "verification_receipt"]},
    },
    {
        "name": "approve_crm_proposal",
        "description": "HUMAN GATE (privileged): apply one proposal exactly once through the audited CRM writers.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "dismiss_crm_proposal",
        "description": "HUMAN GATE (privileged): reject one proposal — sticky, it will never be re-proposed for the same evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_growth_radar",
        "description": "ORIENT: the Motor Caliente radar — deals as rings of the commercial journey (seguimiento: lead+engaged · oportunidad: qualified+demo · propuesta · centro: won with active project), warmth from the touch clock, stalled as the cold orbit, won-without-project flagged. Pure read.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_friday_brief",
        "description": "ORIENT: the Thursday pre-block brief for the Friday growth ritual — gates (scorecard pass/fail), tablero, stale deals (7d touch clock incl. stalled), pending CRM proposals, ≤3 generosity-touch candidates with drafts, and the referral momento-alto. Pure read.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_weekly_reflection",
        "description": "ORIENT: the Friday 5-questions log (qué regalé / qué declaré / a quién pedí referido / qué propuesta avancé / qué aprendí). Optional week YYYY-Www (default current); n_history to include the trend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "week": {"type": "string", "description": "ISO week YYYY-Www (default: current)."},
                "n_history": {"type": "integer", "description": "Also return the last N weeks (default 0 = none)."},
            },
        },
    },
    {
        "name": "save_weekly_reflection",
        "description": "DECLARE: save/upsert THE CURRENT week's 5-questions answers (keys q_regale, q_declare, q_referido, q_propuesta, q_aprendi — all optional strings). MCP writes only the current week; backdating is dashboard-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q_regale": {"type": "string"},
                "q_declare": {"type": "string"},
                "q_referido": {"type": "string"},
                "q_propuesta": {"type": "string"},
                "q_aprendi": {"type": "string"},
            },
        },
    },
    {
        "name": "crm_decay",
        "description": "PRIVILEGED: run the CRM auto-decay sweep — move idle deals to 'stalled' (default 30d) or 'lost' (default 90d). Commercial-structure write; distinct from `archive_stale` (which decays the memory graph).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days_to_stalled": {"type": "integer", "description": "Idle days before → stalled (default 30).", "default": 30},
                "days_to_lost": {"type": "integer", "description": "Idle days before → lost (default 90).", "default": 90},
            },
        },
    },
    # --- Parity fill 2 (2026-07 audit): backend verbs that lacked an MCP tool ---
    {
        "name": "get_deal",
        "description": "ORIENT: one CRM deal by id — the raw deal row (stage, value, contact/account links, growth + lead-score fields). Lighter than get_deal_chain (no joined contact/account/events).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string", "description": "The deal id."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "get_deal_fireflies",
        "description": "ORIENT: every stored Fireflies meeting linked to a deal, newest first. (get_deal_fireflies_latest returns just the most recent one.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string", "description": "The deal id."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "get_deal_fireflies_latest",
        "description": "ORIENT: the most recent stored Fireflies meeting for a deal (empty object if none). Stored rows only — fetch_fireflies_for_deal pulls fresh ones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string", "description": "The deal id."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "list_deal_children",
        "description": "ORIENT: the child deals of a parent deal (expansion/renewal chains hang off an original deal).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string", "description": "The PARENT deal id."},
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "compute_funnel",
        "description": "ORIENT: compute the CURRENT acquisition funnel from live deals (stage counts + conversion rates). Pure read — capture_funnel is the verb that persists a weekly snapshot.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_project",
        "description": "ORIENT: one project's raw row by id or slug. Lighter than get_project_detail (no tasks/stats/initiatives join).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project id or slug."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_next_week_tasks",
        "description": "ORIENT: tasks scheduled for NEXT week (the next-week drawer of the cycle board). Optionally filtered to one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Optional project id filter."},
            },
        },
    },
    {
        "name": "get_future_tasks",
        "description": "ORIENT: tasks scheduled 2+ weeks out, or with a non-ISO tag like 'someday' (the future drawer of the cycle board). Optionally filtered to one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Optional project id filter."},
            },
        },
    },
    {
        "name": "set_task_assignee",
        "description": "PRIVILEGED: reassign a task via the audited sidecar write (logs a dispatched/reclaimed/reassigned event). Works for any agent/human name — the manual stand-in for the claim/dispatch loop; distinct from assign_task (hermes CLI, validates profiles).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id."},
                "assignee": {"type": "string", "description": "New assignee (agent or human name)."},
            },
            "required": ["task_id", "assignee"],
        },
    },
    {
        "name": "set_scheduled_week",
        "description": "PRIVILEGED: set (or clear) a task's planned week bucket, e.g. '2026-W28' (omit week to clear → back to the backlog lens). Scheduling a FUTURE week auto-drops the task from the active cycle (sprint ⇄ week sync); audited with a task_updated event.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id."},
                "week": {"type": "string", "description": "ISO week 'YYYY-Www' (or a tag like 'someday'). Omit to clear."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "assign_task_project",
        "description": "PRIVILEGED: re-home a task to a project (id or slug). The dedicated triage verb (update_task can also patch project_id alongside other fields).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id."},
                "project_id": {"type": "string", "description": "Target project id or slug."},
            },
            "required": ["task_id", "project_id"],
        },
    },
    {
        "name": "set_auto_cycle",
        "description": "PRIVILEGED: per-task opt-in/out of the weekly auto-commit (with auto_cycle on, a task whose scheduled_week matches a newly created cycle is pulled in automatically).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id."},
                "enabled": {"type": "boolean", "description": "true = auto-commit on, false = off."},
            },
            "required": ["task_id", "enabled"],
        },
    },
    {
        "name": "update_project",
        "description": "PRIVILEGED: PATCH a project's name/slug/description/color/icon — only supplied fields are written. Refuses to archive (that stays archive_project's job).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project id or slug."},
                "name": {"type": "string"},
                "slug": {"type": "string"},
                "description": {"type": "string"},
                "color": {"type": "string", "description": "Hex color, e.g. '#3b82f6'."},
                "icon": {"type": "string", "description": "Emoji icon."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "create_cycle",
        "description": "PRIVILEGED: create a weekly cross-project cycle (Mon-Sun snap; named after the ISO week by default; auto-commits matching scheduled_week tasks). Distinct from the legacy project-scoped create_sprint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional name (defaults to the ISO week, e.g. 'Cycle 2026-W28')."},
                "goal": {"type": "string", "description": "Optional cycle goal."},
                "start_date": {"type": "integer", "description": "Optional epoch seconds — snapped to that week's Monday."},
                "end_date": {"type": "integer", "description": "Optional epoch seconds (requires start_date)."},
            },
        },
    },
    {
        "name": "export_roadmap",
        "description": "PRIVILEGED: regenerate roadmap.json from the initiatives table (the DB is the source of truth; the file is a derived, _generated-marked export).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Personal Health (daily ritual timeline) ---
    {
        "name": "get_health_today",
        "description": "Today's health canvas: daily routines grouped by time_block (morning/midday/evening/night) with done status, streak, and progress.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_health_routines",
        "description": "All active health routines (exercise, meditation, meals, supplements, sleep, devocional) ordered by time_block.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_health_routine",
        "description": "Mark a health routine done for today (idempotent). Pass routine_id and optional note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "routine_id": {"type": "integer", "description": "The routine id to check off."},
                "note": {"type": "string", "description": "Optional free-text note."},
            },
            "required": ["routine_id"],
        },
    },
    {
        "name": "uncheck_health_routine",
        "description": "Remove done status for a health routine today.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "routine_id": {"type": "integer", "description": "The routine id to uncheck."},
            },
            "required": ["routine_id"],
        },
    },
    {
        "name": "get_health_config",
        "description": "Health config key-value store (wake_time, sleep_target, exercise_window, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_health_plate",
        "description": "The Balanced plate reference data: segments, portions, supplements, shopping lists, alternatives (from the mi_plato_balanced artifact).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Personal OKRs (privileged sensitive read) ---
    {
        "name": "get_personal_okrs",
        "description": "PRIVILEGED: the operator's personal OKRs — objectives, key results, current state, and bounded immutable check-in history. Excludes transcripts, other participants, and business content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Objective year; defaults to 2026."},
                "history_limit": {"type": "integer", "minimum": 0, "maximum": 50,
                                  "description": "Newest check-ins to include; defaults to 5."},
            },
        },
    },
    # --- Daily Reflection (Reflection–Action Loop — morning/evening ritual) ---
    {
        "name": "get_reflection_today",
        "description": "Daily reflection (Reflection–Action Loop, Harvard 15-min debrief): today's morning intentions + evening wins/misses/adjustments + 7-day history.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_reflection_morning",
        "description": "Save TODAY's morning reflection when the operator answers the 7:15 Telegram prompt (source is recorded as telegram): 1-3 things he wants to ACHIEVE today, PLUS the cognitive-load check-in that same prompt asks for. ONE call saves both — do not call save_cogload_label for a reply to this prompt. Today-only and create-guarded: if a morning entry already exists it is returned untouched unless overwrite=true, but the check-in is saved either way (it has its own guard). A reply with only intentions, or only numbers, is fine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intentions": {"type": "array", "items": {"type": "string"},
                               "description": "1-3 things the operator wants to achieve today (concrete outcomes, not qualities)."},
                "carga": {
                    "type": "object",
                    "description": "The morning check-in, ONLY if he explicitly gave the numbers ('2 3' = ansiedad, estres). NEVER infer them from his prose, his tone or his intentions — an inferred number is a fabricated measurement. If they are absent, omit this and do NOT ask a follow-up: a missing check-in is completely acceptable, and nagging adds to the load being measured.",
                    "properties": {
                        "anx": {"type": "integer", "minimum": 1, "maximum": 5,
                                "description": "Ansiedad AHORA 1-5 (5=mucha) — the first number."},
                        "stress": {"type": "integer", "minimum": 1, "maximum": 5,
                                   "description": "Estres AHORA 1-5 — the second number."},
                    },
                },
                "overwrite": {"type": "boolean", "description": "Explicitly replace an already-saved morning entry. Default false."},
            },
            "required": ["intentions"],
        },
    },
    {
        "name": "save_cogload_label",
        "description": (
            "CORRECTION PATH ONLY. The check-in normally rides the reflection he is "
            "already answering: use save_reflection_morning (ansiedad AHORA + estres "
            "AHORA) or save_reflection_evening (ansiedad ahora, ansiedad de todo el dia, "
            "estres ahora, efectividad) — one input, one call. Use THIS tool only when "
            "numbers arrive detached from either prompt. Momentary anxiety and recalled "
            "whole-day anxiety are DIFFERENT measurements; never copy one into the "
            "other. Call it ONLY when he explicitly gives numbers 1-5. NEVER infer these "
            "from his prose, his tone, or the content of his wins/misses — an inferred "
            "number is a fabricated measurement and silently corrupts the calibration "
            "this exists for. If the numbers are absent, do NOT ask a follow-up and do "
            "NOT mention it: a missing label is completely acceptable, and nagging about "
            "stress adds to the load being measured. Confirm back with the numbers only, "
            "no commentary about what they mean or any pattern across days. One reading "
            "per moment per day; a repeat returns status 'exists' and changes nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slot": {"type": "string", "enum": ["morning", "evening"],
                         "description": "Which prompt he is answering."},
                "anx": {"type": "integer", "minimum": 1, "maximum": 5,
                        "description": "Ansiedad AHORA 1-5 (5=mucha). Both slots."},
                "anx_day": {"type": "integer", "minimum": 1, "maximum": 5,
                            "description": "Ansiedad recordada de TODO EL DIA 1-5. Evening only."},
                "stress": {"type": "integer", "minimum": 1, "maximum": 5,
                           "description": "Estres 1-5, exactly as the operator stated it."},
                "eff": {"type": "integer", "minimum": 1, "maximum": 5,
                        "description": "Efectividad 1-5, if he gave it."},
            },
            "required": ["slot"],
        },
    },
    {
        "name": "get_cogload_status",
        "description": (
            "Instrument health for the cognitive-load collector: is it capturing, are "
            "its subsystems alive, how many days carry a label. Returns NO behavioural "
            "metrics for today by design — showing the operator his own numbers before he "
            "answers PARTE 4 would anchor the answer and contaminate the measurement."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_reflection_evening",
        "description": "Save TODAY's Reflection–Action Loop when the operator answers the 18:45 Telegram prompt (source is recorded as telegram). Harvard 3-part format: wins = what went well and WHY it worked (1-3); misses = what didn't go as planned, what happened, why (0-2); adjustments = what he'll do differently tomorrow, as concrete next steps (0-2); PLUS the PARTE 4 check-in in the same call. A partial reply is fine — save what he gave, never invent the rest. ONE call saves both — do not call save_cogload_label for a reply to this prompt. Today-only and create-guarded: an existing evening entry is returned untouched unless overwrite=true, but the check-in is saved either way (it has its own guard).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "carga": {
                    "type": "object",
                    "description": "PARTE 4, ONLY if he explicitly gave the numbers, in the order the prompt asks ('2 3 4 3' = ansiedad ahora, ansiedad del dia, estres ahora, efectividad). Momentary anxiety and recalled whole-day anxiety are DIFFERENT measurements; never copy one into the other. NEVER infer any of them from his prose, his tone, or the content of his wins/misses — an inferred number is a fabricated measurement and silently corrupts the calibration this exists for. If they are absent, omit this and do NOT ask a follow-up.",
                    "properties": {
                        "anx": {"type": "integer", "minimum": 1, "maximum": 5,
                                "description": "Ansiedad AHORA 1-5 — first number."},
                        "anx_day": {"type": "integer", "minimum": 1, "maximum": 5,
                                    "description": "Ansiedad recordada de TODO EL DIA 1-5 — second number. Evening only."},
                        "stress": {"type": "integer", "minimum": 1, "maximum": 5,
                                   "description": "Estres AHORA 1-5 — third number."},
                        "eff": {"type": "integer", "minimum": 1, "maximum": 5,
                                "description": "Efectividad del dia 1-5 — fourth number."},
                    },
                },
                "wins": {"type": "array",
                         "items": {"type": "object", "properties": {
                             "what": {"type": "string", "description": "The win / effective action."},
                             "why": {"type": "string", "description": "Why it worked."}},
                             "required": ["what"]},
                         "description": "1-3 wins {what, why}."},
                "misses": {"type": "array",
                           "items": {"type": "object", "properties": {
                               "what": {"type": "string", "description": "The moment that didn't meet expectations."},
                               "what_happened": {"type": "string", "description": "What happened."},
                               "why": {"type": "string", "description": "Why it may have gone that way."}},
                               "required": ["what"]},
                           "description": "0-2 misses {what, what_happened, why}. Matter-of-fact, no self-punishment."},
                "adjustments": {"type": "array",
                                "items": {"type": "object", "properties": {
                                    "action": {"type": "string", "description": "The concrete adjustment, as a specific next step."},
                                    "when": {"type": "string", "description": "When tomorrow (e.g. '7am', 'antes de comer')."}},
                                    "required": ["action"]},
                                "description": "0-2 adjustments {action, when}. Close with an action, not an abstraction."},
                "overwrite": {"type": "boolean", "description": "Explicitly replace an already-saved evening entry. Default false."},
            },
            "required": ["wins"],
        },
    },
    # --- Journey: THE contextual layer (fase 1 step 7 + directiva ADICIÓN 9) ---
    # These three are what make this MCP the single contextual interface for the
    # four hosts (Hermes · Claude · Codex · OpenCode): ask where a client is, get
    # the whole cycle with its tasks typed by stage; ask for a project's hub, get
    # its five facets; propose a delivery, get a deep link — never a write.
    {
        "name": "get_journey_pulse",
        "description": (
            "¿Dónde está <cliente>? The WHOLE client cycle for one reference — an "
            "account, a deal or a project, given as an id, a slug or a name. Returns "
            "the open TASKS grouped by journey stage (contacto → formalización → "
            "ejecución → entrega → facturación → cobranza), what is planned for today, "
            "the deals with a deterministic `stopper` each (sin entregar / propuesta "
            "sin respuesta / factura sin pago / sin siguiente toque), the delivering "
            "project with its progress, the last human touches and the attachment "
            "counts — plus `text`, a compact Spanish block meant to be relayed "
            "verbatim. Read-only. An ambiguous reference is REFUSED with candidates "
            "(it never guesses which client you meant): re-ask with an exact name or id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "Account / deal / project — id, slug or name "
                                       "(e.g. 'Acme', 'd_1a2b3c4d', 'acme-delivery')."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "get_project_hub",
        "description": (
            "The five facets of one project: conversations (Fireflies) · resources "
            "(Drive) · code (repo) · plans (~/dev/planning) · tasks (open/total). The "
            "read behind 'what does this project already have attached'; register new "
            "pointers through the dashboard's POST /api/attachments. Accepts an id, a "
            "slug or a name (resolved server-side, never guessed). Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_ref": {"type": "string",
                                "description": "Project id, slug or name."},
            },
            "required": ["project_ref"],
        },
    },
    {
        "name": "propose_deliver",
        "description": (
            "PROPOSE delivering a won deal — read-only, and deliberately so. Returns "
            "the proposal text plus a deep link to the web modal where the operator "
            "confirms in two taps; it writes NOTHING (creating the project, linking "
            "it and stamping the delivery is a human-only gesture on the panel). "
            "Typed refusals: not_won, already_delivered, ambiguous (with candidates), "
            "not_found. Use it when a chat says 'ya entregamos X' — you propose, he taps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deal_ref": {"type": "string",
                             "description": "Deal id or title (exact or unambiguous)."},
            },
            "required": ["deal_ref"],
        },
    },
]


# --- DB helpers ---

def get_conn():
    # Same live-DB tripwire as dashboard.db.get_conn — mcp_server keeps its own
    # KANBAN_DB global and is a full write path (deal_events, tasks, projects),
    # so it is a third independent route to the operator's real file.
    from dashboard.db import assert_not_live_db
    assert_not_live_db(KANBAN_DB)
    conn = sqlite3.connect(str(KANBAN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    # Phase 1 (item 1): FK enforcement is per-connection in SQLite; set it here so
    # the MCP server's writes honour declared foreign keys like the dashboard's.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_hermes_cli(args: list[str]) -> tuple[int, str, str]:
    """Run hermes kanban CLI command and return (exit_code, stdout, stderr)."""
    cmd = ["hermes", "kanban"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# --- Tool implementations ---

def tool_list_tasks(args: dict) -> str:
    conn = get_conn()
    try:
        query = "SELECT id, title, assignee, status, priority, project_id, sprint_id, created_at, completed_at FROM tasks"
        conditions = []
        params = []
        if args.get("assignee"):
            conditions.append("assignee = ?")
            params.append(args["assignee"])
        if args.get("status"):
            conditions.append("status = ?")
            params.append(args["status"])
        # Verb-audit complement: search/scope filters — agents stop paging the
        # whole board to find one card.
        if args.get("project_id"):
            pid = _db.resolve_project(args["project_id"]) if _LOOP_OK else args["project_id"]
            conditions.append("project_id = ?")
            params.append(pid or args["project_id"])
        if args.get("q"):
            conditions.append("(title LIKE ? OR body LIKE ?)")
            params += [f"%{args['q']}%", f"%{args['q']}%"]
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT 50"

        rows = conn.execute(query, params).fetchall()
        tasks = [dict(r) for r in rows]
        return json.dumps({"count": len(tasks), "tasks": tasks}, indent=2)
    finally:
        conn.close()


def tool_get_task(args: dict) -> str:
    task_id = args["task_id"]
    conn = get_conn()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return json.dumps({"error": "Task not found"})
        comments = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC LIMIT 20", (task_id,)
        ).fetchall()
        return json.dumps({
            "task": dict(task),
            "comments": [dict(c) for c in comments],
            "events": [dict(e) for e in events],
        }, indent=2, default=str)
    finally:
        conn.close()


# Phase 1 (item 4): the MCP server's session identity, set by whoever spawns it
# (Hermes's session sets HERMES_MCP_ORIGIN=hermes + HERMES_MCP_SESSION_KEY=<key>).
# A per-call `session_key` arg overrides. This is how "a call on Hermes's MCP
# session stamps origin='hermes'" (§3.3, user decision: session identity → origin).
SERVER_SESSION_KEY = os.environ.get("HERMES_MCP_SESSION_KEY")
SERVER_ORIGIN = os.environ.get("HERMES_MCP_ORIGIN")

# --- Inbound Telegram provenance (spec §2, ruling 6) -------------------------
# A task created from a Telegram topic lands origin='hermes', session_id=NULL —
# so nothing downstream can ever say "here is what the Growth front advanced
# today", which is literally goal 3 of the consolidation. These ~10 lines stamp
# `tasks.thread_id` from the gateway's own session key.
#
# Two rules make it provenance rather than decoration:
#   1. The key comes from the ENVIRONMENT (HERMES_MCP_SESSION_KEY, set by
#      whoever spawned this server), never from `args`. A model-supplied
#      thread id is a claim, not evidence — and a caller that can pick its own
#      provenance has none.
#   2. The parsed thread must EXIST in the `threads` registry. An unregistered
#      topic stamps NULL rather than minting a phantom thread id.
# Anything else → NULL. If the live gateway config never sets the env var, this
# stays dormant and every task carries thread_id NULL: a known-partial, honestly
# empty column, never a faked one.
_TELEGRAM_SESSION_RE = re.compile(r"^agent:main:telegram:dm:(\d+):(\d+)$")


def _provenance_thread_id(conn) -> "int | None":
    key = SERVER_SESSION_KEY or ""
    mo = _TELEGRAM_SESSION_RE.match(key.strip())
    if not mo:
        return None
    thread_id = int(mo.group(2))
    try:
        row = conn.execute(
            "SELECT thread_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    except sqlite3.Error:
        return None                      # registry not migrated yet → honest NULL
    return int(row["thread_id"]) if row else None


def tool_create_task(args: dict) -> str:
    title = args["title"]
    assignee = args.get("assignee", "default")
    session_key = args.get("session_key") or SERVER_SESSION_KEY

    envelope_parts = (args.get("contract_cmd"), args.get("practice_text"),
                      args.get("run_context"))
    if any(part is not None for part in envelope_parts) and not all(
            part is not None for part in envelope_parts):
        return json.dumps({"error": "contract_cmd, practice_text and run_context must be supplied together"})

    due_date = args.get("due_date")
    if due_date is not None:
        if not _LOOP_OK:
            return json.dumps({"error": f"due date write path unavailable: {_LOOP_ERR}"})
        _, due_error = _sprints.normalize_due_date(due_date)
        if due_error:
            return json.dumps({"error": due_error})

    # The Hermes CLI creates the base row and cannot atomically attach a cycle.
    # Refuse a target that is already invalid before creating anything; the
    # writer repeats this check later to close the race where a cycle finishes
    # between preflight and sidecar assignment.
    sprint_id = args.get("sprint_id")
    if sprint_id:
        if not _LOOP_OK:
            return json.dumps({"error": f"sprint write path unavailable: {_LOOP_ERR}"})
        sprint_check = _sprints.validate_sprint_target(sprint_id)
        if sprint_check.get("status") == "error":
            return json.dumps({"error": sprint_check["error"],
                               "requested_sprint_id": sprint_id})

    create_args = ["create", title, "--json", "--assignee", assignee]
    task_body = args.get("body") or ""
    if args.get("acceptance_criteria"):
        task_body = (task_body + "\n\n## Acceptance\n" + args["acceptance_criteria"]).strip()
    if task_body:
        create_args += ["--body", task_body]
    code, stdout, stderr = run_hermes_cli(create_args)
    if code != 0:
        return json.dumps({"error": f"hermes kanban create failed: {stderr}"})

    # Robustly parse the new id (the old `split('\n')[0]` returned the whole
    # "Created t_… (ready…)" line, so it never started with t_ and the project/
    # origin stamping below was silently skipped). Prefer --json; regex-fallback.
    task_id = None
    try:
        payload = json.loads(stdout)
        task_id = payload.get("id") or payload.get("task_id") or (payload.get("task") or {}).get("id")
    except Exception:
        pass
    if not task_id:
        import re as _re
        mo = _re.search(r"\bt_[0-9a-f]+\b", stdout)
        task_id = mo.group(0) if mo else (stdout.strip().split("\n")[0] if stdout else "unknown")

    result = {"status": "created", "task_id": task_id, "title": title, "assignee": assignee}
    if _LOOP_OK and task_id.startswith("t_"):
        # Resolve where the task lands (explicit → session project → Inbox floor)
        # and its origin (explicit → server/session identity). The identity
        # trigger already set a created_by-derived origin baseline on insert; a
        # Hermes-session call OVERRIDES it to the badge rule (origin='hermes',
        # delegate='claude-code', owner='ricardo').
        project_id = _identity.resolve_create_project(args.get("project_id"), session_key)
        origin = args.get("origin") or SERVER_ORIGIN
        origin = origin if origin in _identity.ORIGINS else None
        conn = get_conn()
        try:
            conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?", (project_id, task_id))
            # Inbound Telegram provenance: env-derived + registry-verified, else NULL.
            thread_id = _provenance_thread_id(conn)
            if thread_id is not None:
                conn.execute("UPDATE tasks SET thread_id = ? WHERE id = ?",
                             (thread_id, task_id))
                result["thread_id"] = thread_id
            if origin:
                delegate = None if assignee in ("ricardo", "user") else (
                    "claude-code" if (origin == "hermes" and assignee == "default") else assignee)
                conn.execute(
                    "UPDATE tasks SET origin = ?, owner = 'ricardo', delegate = COALESCE(?, delegate) WHERE id = ?",
                    (origin, delegate, task_id),
                )
            conn.commit()
        finally:
            conn.close()
        result["project_id"] = project_id
        if origin:
            result["origin"] = origin
        # Assign to sprint if sprint_id was provided (same pattern as API api_create_task)
        if sprint_id:
            try:
                sprint_res = _sprints.assign_task_sprint(task_id, sprint_id)
                if sprint_res.get("status") == "error":
                    result["sprint_id"] = None
                    result["sprint_error"] = sprint_res.get("error", "sprint assignment failed")
                else:
                    result["sprint_id"] = sprint_res.get("sprint_id")
            except Exception as e:
                result["sprint_id"] = None
                result["sprint_error"] = str(e)
        if due_date is not None:
            due_res = _sprints.update_task_fields(task_id, due_date=due_date)
            result["due_date_set"] = due_res.get("status") != "error"
            if due_res.get("status") == "error":
                result["due_date_error"] = due_res["error"]
        if args.get("contract_cmd") is not None:
            contract_res = _gov.set_contract(task_id, args["contract_cmd"])
            if contract_res.get("status") != "ok":
                result["envelope_error"] = contract_res.get("error")
            else:
                envelope_res = _gov.set_run_envelope(
                    task_id, args["practice_text"],
                    args.get("practice_host", "hermes"), args["run_context"])
                result["envelope_status"] = envelope_res.get("status")
                if envelope_res.get("status") != "ready":
                    result["envelope_error"] = envelope_res.get("reason")
    return json.dumps(result)


def tool_update_task_status(args: dict) -> str:
    # Phase 0 (item 1): ONE validated status write path. Every board-status
    # change — from the MCP server, the dashboard UI, or the loop — goes through
    # sprints.set_task_status, which validates the status, stamps started_at/
    # completed_at, and appends a `status_changed` event. The old code forked
    # three ways (hermes CLI for done/blocked/ready + a raw-SQL fallback that
    # wrote status with NO event and NO validation); that raw fallback is deleted.
    task_id = args["task_id"]
    status = args["status"]
    if not _LOOP_OK:
        return json.dumps({"error": f"status write path unavailable: {_LOOP_ERR}"})
    res = _sprints.set_task_status(task_id, status)
    if res.get("status") == "error":
        return json.dumps({"error": res["error"]})
    return json.dumps({
        "status": "updated", "task_id": task_id,
        "new_status": status, "from": res.get("from"),
    })


def tool_assign_task(args: dict) -> str:
    task_id = args["task_id"]
    assignee = args["assignee"]
    code, stdout, stderr = run_hermes_cli(["assign", task_id, assignee])
    if code != 0:
        return json.dumps({"error": f"hermes kanban assign failed: {stderr}"})
    return json.dumps({"status": "assigned", "task_id": task_id, "assignee": assignee})


def tool_comment_task(args: dict) -> str:
    task_id = args["task_id"]
    comment = args["comment"]
    code, stdout, stderr = run_hermes_cli(["comment", task_id, comment])
    if code != 0:
        return json.dumps({"error": f"hermes kanban comment failed: {stderr}"})
    return json.dumps({"status": "commented", "task_id": task_id})


def tool_list_projects(args: dict) -> str:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM projects WHERE archived_at IS NULL ORDER BY name").fetchall()
        projects = []
        for r in rows:
            p = dict(r)
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id = ?", (p["id"],)).fetchone()[0]
            p["task_count"] = count
            projects.append(p)
        return json.dumps({"projects": projects}, indent=2)
    finally:
        conn.close()


def tool_list_sprints(args: dict) -> str:
    conn = get_conn()
    try:
        if args.get("project_id"):
            rows = conn.execute(
                "SELECT * FROM sprints WHERE project_id = ? ORDER BY start_date DESC",
                (args["project_id"],)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sprints ORDER BY start_date DESC").fetchall()
        sprints = []
        for r in rows:
            s = dict(r)
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE sprint_id = ?", (s["id"],)).fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE sprint_id = ? AND status = 'done'", (s["id"],)
            ).fetchone()[0]
            s["task_count"] = count
            s["done_count"] = done
            sprints.append(s)
        return json.dumps({"sprints": sprints}, indent=2)
    finally:
        conn.close()


def tool_create_sprint(args: dict) -> str:
    import uuid
    project_id = args["project_id"]
    name = args["name"]
    goal = args.get("goal", "")
    sid = f"spr_{uuid.uuid4().hex[:8]}"
    now = int(time.time())
    end = now + (14 * 24 * 3600)  # 2 weeks default

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO sprints (id, project_id, name, goal, start_date, end_date, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, project_id, name, goal, now, end, "planning", now)
        )
        conn.commit()
        return json.dumps({"status": "created", "sprint_id": sid, "name": name})
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


def tool_get_activity(args: dict) -> str:
    limit = args.get("limit", 20)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT te.id, te.task_id, te.kind, te.payload, te.created_at,
                      t.title, t.assignee
               FROM task_events te
               LEFT JOIN tasks t ON te.task_id = t.id
               ORDER BY te.created_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        events = [dict(r) for r in rows]
        return json.dumps({"events": events}, indent=2, default=str)
    finally:
        conn.close()


def tool_get_sessions(args: dict) -> str:
    """Session summary + the chat-facing terminal list, via the dashboard API.

    Goes through `_dash` (loopback) — the human-facing DASHBOARD_URL may
    resolve to the tailscale-serve front. `terminals` names the live c/g tmux
    sessions per host so a chat client can address them directly; capped so a
    runaway host can't flood a Telegram reply."""
    raw = _dash("GET", "/api/sessions")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict) or "error" in data:
        return raw
    terminals = []
    for s in (data.get("tmux_local") or []) + (data.get("tmux_remote") or []):
        name = s.get("name") or ""
        if not name.startswith(("claude-", "codex-", "opencode-")):
            continue
        terminals.append({
            "name": name,
            "host": s.get("host") or "local",
            "attached": bool(s.get("attached")),
            "path": s.get("path") or "",
            "agent": ("claude" if name.startswith("claude-")
                      else "opencode" if name.startswith("opencode-")
                      else "codex"),
        })
        if len(terminals) >= 40:
            break
    summary = {
        "claude_code_sessions": len(data.get("claude_code", [])),
        "opencode_sessions": len(data.get("opencode", [])),
        "tmux_local": len(data.get("tmux_local", [])),
        "hosts": data.get("hosts", {}),
        "active_now": data.get("total_active", 0),
        "terminals": terminals,
    }
    return json.dumps(summary, indent=2)


def tool_get_dashboard_url(args: dict) -> str:
    """The one address, resolved live.

    It used to answer with TWO: `url` (loopback, dead from anywhere but the
    server) and `tailscale_url` — a tailnet URL
    with the wrong protocol, since 5555 is the HTTPS terminator `tailscale serve`
    owns. An agent that picked the second field got a URL that has never worked,
    and a caller handed two addresses has to guess. One field now, from the same
    resolver every deep link in the system uses."""
    return json.dumps({
        "url": _dashboard_url(),
        "note": "Reachable from any device on the tailnet (HTTPS, tailscale serve → the "
                "dashboard's loopback port). Override with $DASHBOARD_URL."
    })


# --- PRD Phase 3 loop handlers (delegate to the shared dashboard.loop core) ---

def _need_loop() -> Optional[str]:
    if not _LOOP_OK:
        return json.dumps({"error": f"loop core unavailable: {_LOOP_ERR}"})
    return None


# --- Retired surfaces: a tombstone verb, never a removed one ------------------
# A deleted verb answers "Unknown tool" — which reads as a broken server and
# invites a retry. A registered verb that returns a TYPED explanation tells the
# caller the concept is gone and what to use instead, once. Both payloads are
# byte-identical to the dashboard's (dashboard/api.py: EPICS_GONE, and the
# retired-dispatch body); tests/test_mcp_api_parity.py gates the symmetry so the
# two frontends cannot drift into "gone here, alive there".
#
# Epics were folded into projects (spec §1, migration m03). `tasks.epic_id`
# stays in the schema as frozen audit — still read for display, never written —
# so only the write/list verbs die here.
EPICS_GONE = {
    "error": "epics_folded",
    "hint": "epics were folded into projects (m03); tasks.epic_id is frozen audit",
}

# Initiatives were folded into projects too (spec §1: "Initiative → folded into
# Project. Roadmap fields move onto projects"). quarter/tier/why/success_check/
# health/confidence live on `projects` now, so the roadmap is a read over the
# project spine. The initiative READS survive as archive — list_initiatives,
# get_initiative, get_initiative_drilldown, get_initiative_events, get_roadmap
# still answer, because history is kept, not dropped — and only the two WRITE
# verbs (create_initiative, edit_roadmap) die here. Byte-identical to
# dashboard/api.py's INITIATIVES_GONE; tests/test_initiatives_410.py is the
# ratchet.
INITIATIVES_GONE = {
    "error": "initiatives_folded",
    "hint": "initiatives were folded into projects (m03); use projects + quarter",
}

# `dispatch_to_agent` wrote `tasks.assignee` and spawned nothing — the exact lie
# the dispatch saga replaced (spec §2: "a control that lies about its effect
# teaches distrust of every other control"). Dispatch is human-initiated from
# the dashboard by design (ruling 2), so this is not "moved to another verb":
# there is deliberately no agent-reachable path into the saga.
DISPATCH_RETIRED = {
    "error": "retired",
    "hint": "use the dashboard dispatch (human-only); agents report via claim/report verbs",
}


def _gone(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def tool_list_pool(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.list_pool(agent=args.get("agent"), skills=args.get("skills")), indent=2, default=str)


def tool_claim_task(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.claim_task(args["task_id"], args["agent"], session_id=args.get("session_id")), indent=2, default=str)


def tool_claim_next(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.claim_next(args["agent"], skills=args.get("skills")), indent=2, default=str)


def tool_report_progress(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.report_progress(args["task_id"], args.get("note", ""), pct=args.get("pct"), agent=args.get("agent"), step=args.get("step")), default=str)


def tool_report_result(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.report_result(args["task_id"], args.get("result", ""), passed=args.get("passed", True), artifacts=args.get("artifacts"), agent=args.get("agent")), default=str)


def tool_report_blocked(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.report_blocked(args["task_id"], args.get("reason", ""), agent=args.get("agent")), default=str)


def tool_escalate_discovery(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.escalate_discovery(args["title"], args.get("body", ""), reason=args.get("reason", ""), related_task=args.get("related_task"), agent=args.get("agent")), default=str)


# --- PRD Phase 4: orient/read surface (default scope) ---

def tool_get_roadmap(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    # Phase 6: the initiatives TABLE is the source; roadmap.json is a generated
    # export nobody reads anymore.
    inits = _strategy.list_initiatives()
    # Accepted-done roll-up over each initiative's epics' tasks (project-wide
    # fallback when it has no epics) — mirrors the API.
    for init in inits:
        prog = _graph.initiative_progress(init)
        init["epic_count"] = prog["epic_count"]
        init["task_in_flight"] = prog["task_in_flight"]
        if prog["task_total"] > 0:
            init.update({"progress": prog["progress"], "derived": True,
                         "progress_scope": prog["scope"],
                         "task_total": prog["task_total"], "task_done": prog["task_done"]})
        else:
            init["derived"] = False
    return json.dumps({"initiatives": inits}, indent=2, default=str)


def tool_list_initiatives(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"initiatives": _strategy.list_initiatives()}, indent=2, default=str)


def tool_list_epics(args: dict) -> str:
    return _gone(EPICS_GONE)


def tool_get_active_sprint(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.get_active_sprint(args.get("project_id")), indent=2, default=str)


def tool_get_task_history(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"task_id": args["task_id"], "history": _db.get_task_history(args["task_id"])}, indent=2, default=str)


def tool_get_archive(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    group = args.get("group", "day")
    if group not in ("day", "week", "month"):
        group = "day"
    return json.dumps(_db.get_archive(group), indent=2, default=str)


# --- PRD Phase 4: privileged orchestration (operator scope only) ---

def tool_dispatch_to_agent(args: dict) -> str:
    return _gone(DISPATCH_RETIRED)


def tool_set_pool(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.set_pool(args["task_id"], bool(args.get("pool", True))), default=str)


def tool_set_autonomy(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_loop.set_autonomy(args["task_id"], args.get("autonomy", "")), default=str)


def tool_pin_task_bottom(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(
        _db.set_pinned_bottom(args["task_id"], bool(args.get("pinned", True))), default=str)


def tool_change_trust_grade(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_graph.set_agent_trust(args["agent"], args["grade"]), default=str)


def tool_start_sprint(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.start_sprint(args["sprint_id"]), default=str)


def tool_assign_task_sprint(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.assign_task_sprint(args["task_id"], args.get("sprint_id")), default=str)


def tool_close_sprint(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.close_sprint(args["sprint_id"], args.get("next_sprint_id")), default=str)


def tool_finish_sprint(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.finish_sprint(), default=str)


def tool_create_project(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.create_project(args["name"], args["slug"], args.get("description", "")), default=str)


def tool_create_epic(args: dict) -> str:
    return _gone(EPICS_GONE)


def tool_assign_task_epic(args: dict) -> str:
    return _gone(EPICS_GONE)


def tool_edit_roadmap(args: dict) -> str:
    # RETIRED: initiatives were folded into projects (m03). The roadmap fields
    # this verb edited (tier/quarter/health/confidence/why/success_check) live
    # on `projects` now — use update_project. Twin of PATCH /api/roadmap/{id}.
    return _gone(INITIATIVES_GONE)


# --- Parallel orchestration handlers ---

def tool_get_spec_slice(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_orch.spec_slice(args["feature"], args.get("role")), indent=2, default=str)


def tool_list_specs(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"specs": _orch.list_specs()}, indent=2, default=str)


def tool_report_ledger(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_orch.report_ledger(
        args["task_id"], args.get("summary", ""),
        files_modified=args.get("files_modified"), risks=args.get("risks"),
        status=args.get("status", "passed"), agent=args.get("agent"),
        session_key=args.get("session_key"), role=args.get("role")), indent=2, default=str)


# --- Verb-audit Tier 3 handlers ---

def tool_get_account_chain(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.account_chain(args["account_id"]), indent=2, default=str)


def tool_delete_task(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.delete_task(args["task_id"]), default=str)


def tool_list_deal_events(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.list_deal_events(
        args["deal_id"], limit=args.get("limit", 50)), indent=2, default=str)


def tool_delete_deal(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.delete_deal(args["deal_id"]), default=str)


def tool_delete_project(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import db as _db
    return json.dumps(_db.delete_project(args["project_id"]), default=str)


def tool_get_sprint(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    s = _sprints.get_sprint(args["sprint_id"])
    if not s:
        return json.dumps({"error": "sprint not found"})
    s["tasks"] = _sprints.get_sprint_tasks(args["sprint_id"])
    return json.dumps({"sprint": s}, indent=2, default=str)


def tool_get_velocity(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"cycles": _sprints.get_velocity()}, indent=2, default=str)


def tool_get_graph(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    store = _gmem.get_store()
    if args.get("node"):
        return json.dumps(store.expand(args["node"], hops=max(0, min(int(args.get("hops", 2)), 4))),
                          indent=2, default=str)
    if args.get("q"):
        return json.dumps({"matches": store.search(args["q"], limit=25)}, indent=2, default=str)
    return json.dumps(store.stats(), indent=2, default=str)


def tool_rebuild_graph(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    out = _gmem.ingest_all(_gmem.get_store(), rebuild=True)
    return json.dumps({"stats": out["stats"]}, indent=2, default=str)


def tool_get_usage(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import usage as _usage
    from dashboard import providers as _providers
    summary = _usage.get_usage_summary()
    summary["unified"] = _providers.get_unified_summary()
    return json.dumps(summary, indent=2, default=str)


def tool_archive_project(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.archive_project(args["project_id"]), default=str)


def tool_register_agent(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_graph.register_agent(
        args["name"], kind=args.get("kind"), host=args.get("host"),
        skills=args.get("skills"), notes=args.get("notes")), default=str)


def tool_resolve_input(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_orch.resolve_event(int(args["event_id"])), default=str)


def tool_get_health(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    stats = _db.get_stats()
    active = _sprints.get_active_sprint()
    return json.dumps({
        "db": str(KANBAN_DB), "db_exists": KANBAN_DB.exists(),
        "tasks_total": stats["total"], "by_status": stats["by_status"],
        "inbox_count": stats.get("inbox_count", 0),
        "review_queue": stats["by_status"].get("review", 0),
        "active_cycle": {"id": active["id"], "name": active["name"]} if active else None,
        "scope": ACTIVE_SCOPE,
    }, indent=2, default=str)


# --- Verb-audit Tier 2 handlers ---

def tool_get_initiative(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    init = _strategy.get_initiative(args["initiative_id"])
    if not init:
        return json.dumps({"error": "initiative not found"})
    init["events"] = _strategy.get_events(args["initiative_id"], limit=20)
    return json.dumps({"initiative": init}, indent=2, default=str)


def tool_get_initiative_drilldown(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    init = _strategy.get_initiative(args["initiative_id"])
    if not init:
        return json.dumps({"error": "initiative not found"})
    return json.dumps(_graph.initiative_drilldown(init), indent=2, default=str)


def tool_add_deal_event(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.add_deal_event(args["deal_id"], args.get("kind", ""),
                                          note=args.get("note", ""),
                                          agent=args.get("agent")), default=str)


def tool_update_epic(args: dict) -> str:
    return _gone(EPICS_GONE)


def tool_update_task(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.update_task_fields(
        args["task_id"], title=args.get("title"), body=args.get("body"),
        priority=args.get("priority"), due_date=args.get("due_date"),
        project_id=args.get("project_id")), default=str)


def tool_list_agents(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    # No live-session probe over MCP (that's the dashboard's job) — the
    # registry + earned grades are the point here.
    return json.dumps({"agents": _graph.get_agents(set())}, indent=2, default=str)


def tool_get_agent_status(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import agent_status as _astat
    return json.dumps(_astat.get_agent_status(), indent=2, default=str)


def tool_get_task_links(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"task_id": args["task_id"],
                       "links": _db.get_task_links(args["task_id"])}, indent=2, default=str)


def tool_bulk_accept(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.bulk_accept(args.get("task_ids") or []), indent=2, default=str)


def tool_reconcile_sprint_ledger(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.reconcile_sprint_ledger(), indent=2, default=str)


# --- Verb-audit Tier 1 handlers ---

def tool_create_initiative(args: dict) -> str:
    # RETIRED: initiatives were folded into projects (m03). Originating a
    # quarterly bet is now create_project + update_project(quarter=…, tier=…).
    # Twin of POST /api/roadmap.
    return _gone(INITIATIVES_GONE)


def tool_list_accounts(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps({"accounts": _crm.list_accounts()}, indent=2, default=str)


def tool_list_contacts(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps({"contacts": _crm.list_contacts(args.get("account_id"))},
                      indent=2, default=str)


def tool_get_task_runs(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"task_id": args["task_id"],
                       "runs": _db.get_task_runs(args["task_id"])}, indent=2, default=str)


def tool_get_task_ledger(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"task_id": args["task_id"],
                       "ledger": _orch.get_ledger(limit=50, task_id=args["task_id"])},
                      indent=2, default=str)


# --- Phase 6: CRM handlers ---

def tool_list_deals(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps({"deals": _crm.list_deals(stage=args.get("stage"))}, indent=2, default=str)


def tool_get_deal_chain(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.deal_drilldown(args["deal_id"]), indent=2, default=str)


def tool_create_account(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.create_account(args.get("name", ""), args.get("domain", ""),
                                          args.get("notes", "")), default=str)


def tool_create_contact(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.create_contact(
        args.get("account_id", ""), args.get("name", ""),
        email=args.get("email", ""), role=args.get("role", ""),
        notes=args.get("notes", ""), phone=args.get("phone", ""),
        whatsapp=args.get("whatsapp", ""), linkedin_url=args.get("linkedin_url", ""),
        source=args.get("source", ""), source_notes=args.get("source_notes", "")), default=str)


# --- the deal-stage gates, server-side ---------------------------------------
# An `enum` in an inputSchema is ADVISORY: it shapes what a well-behaved client
# offers and nothing more — a hand-rolled JSON-RPC call, a stale client, or a
# model that ignores the schema all land in the handler regardless. So the two
# retired/human-only stages are refused HERE, in code, and the enums above are
# the hint rather than the guard (the same reasoning that puts m05's trigger
# under crm.py's validator: each layer enforces what it can actually enforce).
STAGE_RETIRED = {
    "status": "error",
    "code": "stage_retired",
    "error": ("stage 'delivered' is retired — a won deal stays 'won'. Delivery "
              "is projects.status='delivered' (deliver the deal into a project, "
              "then mark the project delivered)."),
}

# `won` is not retired, it is HUMAN-ONLY (ruling 3): closing a deal is a
# conversion, and conversions from chat are PROPOSALS with a deep link to the
# web modal — two taps, structurally human — never a write an agent performs on
# its own reading of a conversation.
STAGE_HUMAN_ONLY = {
    "status": "error",
    "code": "stage_human_only",
    "error": ("moving a deal to 'won' is a human conversion — propose it with a "
              "deep link to the dashboard (?entity=deal:<id>&action=deliver) and "
              "let the operator tap Entregar."),
}


def tool_create_deal(args: dict) -> str:
    # Before `_need_loop`: refusing a retired stage is input validation, not
    # work, so the answer must not depend on whether the loop core imported.
    if args.get("stage") == "delivered":
        return json.dumps(STAGE_RETIRED, indent=2)
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.create_deal(
        args.get("account_id", ""), args.get("title", ""),
        stage=args.get("stage", "lead"), value=args.get("value"),
        currency=args.get("currency", "MXN"), contact_id=args.get("contact_id"),
        initiative_id=args.get("initiative_id"), notes=args.get("notes", ""),
        source=args.get("source", ""),
        product_id=args.get("product_id")), default=str)


def tool_update_deal(args: dict) -> str:
    # Same order, same reason as create_deal: the two gates are decisions about
    # the ARGUMENT, and they answer identically with or without the loop core.
    if args.get("stage") == "delivered":
        return json.dumps(STAGE_RETIRED, indent=2)
    if args.get("stage") == "won":
        return json.dumps(STAGE_HUMAN_ONLY, indent=2)
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.update_deal(
        args["deal_id"], stage=args.get("stage"), value=args.get("value"),
        initiative_id=args.get("initiative_id"), notes=args.get("notes"),
        clear_initiative=bool(args.get("clear_initiative")),
        product_id=args.get("product_id"),
        clear_product=bool(args.get("clear_product"))), default=str)


def tool_update_contact(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.update_contact(
        args["contact_id"], name=args.get("name"), email=args.get("email"),
        role=args.get("role"), notes=args.get("notes"), phone=args.get("phone"),
        whatsapp=args.get("whatsapp"), linkedin_url=args.get("linkedin_url"),
        source=args.get("source"), source_notes=args.get("source_notes"),
        account_id=args.get("account_id")), default=str)


# --- CRM Growth: thin wrappers over dashboard/growth.py (+ crm.pipeline / fireflies) ---
# Every handler below calls the SAME function the dashboard endpoint calls, so the
# MCP surface and the UI never drift. No SQL is duplicated here.

# GROUP A — ICP + Products + Pipeline + Scorecard

def tool_get_icp(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.icp_config(), indent=2, default=str)


def tool_update_icp(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    # MCP schema uses "positioning" but set_icp expects "positioning_statement"
    if "positioning" in args and "positioning_statement" not in args:
        args["positioning_statement"] = args.pop("positioning")
    return json.dumps(_growth.set_icp(args), indent=2, default=str)


def tool_list_products(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.list_products(), indent=2, default=str)


def tool_create_product(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.create_product(
        name=args.get("name", ""), description=args.get("description", ""),
        value_ladder_stage=args.get("value_ladder_stage", ""),
        fixed_price_mxn=args.get("fixed_price_mxn"),
        ficha_html=args.get("ficha_html", "")), indent=2, default=str)


def tool_update_product(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    updates = {k: v for k, v in args.items() if k != "product_id"}
    return json.dumps(_growth.update_product(args["product_id"], updates),
                      indent=2, default=str)


def tool_delete_product(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.delete_product(args["product_id"]), indent=2, default=str)


def tool_get_pipeline(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.pipeline(), indent=2, default=str)


def tool_get_pipeline_math(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.pipeline_math(
        revenue_goal=args.get("revenue_goal"),
        avg_ticket=args.get("avg_ticket")), indent=2, default=str)


def tool_get_pipeline_health(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.pipeline_health(), indent=2, default=str)


def tool_get_forecast(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.forecast(), indent=2, default=str)


def tool_get_cltv_cac(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.cltv_cac(), indent=2, default=str)


def tool_get_scorecard(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.scorecard(week=args.get("week")), indent=2, default=str)


def tool_get_growth_loops(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.growth_loops(), indent=2, default=str)


def tool_create_lead(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.quick_add_lead(
        name=args.get("name", ""), company=args.get("company", ""),
        source=args.get("source", ""), loop=args.get("loop", ""),
        notes=args.get("notes", ""),
        engagement_score=int(args.get("engagement_score", 0) or 0),
        industry=args.get("industry", ""), value=args.get("value")),
        indent=2, default=str)


def tool_touch_deal(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.record_touch(
        args["deal_id"], note=args.get("note", ""),
        kind=args.get("kind", "touch"),
        next_in_days=int(args.get("next_in_days", 7))), indent=2, default=str)


def tool_score_deal(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.set_lead_features(
        args["deal_id"], account_type=args.get("account_type", ""),
        source=args.get("source", ""),
        product_interest=args.get("product_interest", ""),
        engagement_score=int(args.get("engagement_score", 0) or 0),
        industry=args.get("industry", "")), indent=2, default=str)


def tool_update_deal_growth(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.update_deal_growth(
        args["deal_id"],
        value_ladder_stage=args.get("value_ladder_stage"),
        growth_loop=args.get("growth_loop"),
        lead_source=args.get("lead_source"),
        next_touch_date=args.get("next_touch_date"),
        product_id=args.get("product_id")), indent=2, default=str)


def tool_get_funnel_trend(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.funnel_trend(weeks=int(args.get("weeks", 12))),
                      indent=2, default=str)


def tool_get_pipeline_temporal(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.pipeline_temporal(months=int(args.get("months", 12))),
                      indent=2, default=str)


# GROUP B — Content + Speaking + Time-blocks + Acquisition Costs

def tool_list_content(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.content_cadence(weeks=int(args.get("weeks", 8))),
                      indent=2, default=str)


def tool_create_content(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.create_content_piece(
        title=args.get("title", ""), topic=args.get("topic", ""),
        channel=args.get("channel", ""),
        growth_loop=args.get("growth_loop", args.get("loop", "")),
        hook=args.get("hook", ""),
        publish_date=args.get("publish_date", args.get("published_at", "")),
        status=args.get("status", "")), indent=2, default=str)


def tool_update_content(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    updates = {k: v for k, v in args.items() if k != "content_id"}
    return json.dumps(_growth.update_content_piece(args["content_id"], updates),
                      indent=2, default=str)


def tool_delete_content(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.delete_content_piece(args["content_id"]),
                      indent=2, default=str)


def tool_list_speaking(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.list_speaking(), indent=2, default=str)


def tool_create_speaking(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.create_speaking(
        title=args.get("title", ""), event_name=args.get("event_name", ""),
        event_date=args.get("event_date", ""), status=args.get("status", ""),
        attraction_loop_status=args.get("attraction_loop_status", ""),
        deal_id=args.get("deal_id", "")), indent=2, default=str)


def tool_update_speaking(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    updates = {k: v for k, v in args.items() if k != "speaking_id"}
    return json.dumps(_growth.update_speaking(args["speaking_id"], updates),
                      indent=2, default=str)


def tool_delete_speaking(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.delete_speaking(args["speaking_id"]),
                      indent=2, default=str)


def tool_list_time_blocks(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.list_time_blocks(), indent=2, default=str)


def tool_create_time_block(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.create_time_block(
        day_of_week=args.get("day_of_week"),
        start_time=args.get("start_time", ""), end_time=args.get("end_time", ""),
        role=args.get("role", ""), label=args.get("label", ""),
        active=args.get("active", True)), indent=2, default=str)


def tool_update_time_block(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    updates = {k: v for k, v in args.items() if k != "block_id"}
    return json.dumps(_growth.update_time_block(args["block_id"], updates),
                      indent=2, default=str)


def tool_delete_time_block(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.delete_time_block(args["block_id"]),
                      indent=2, default=str)


def tool_list_acquisition_costs(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.list_acquisition_costs(), indent=2, default=str)


def tool_create_acquisition_cost(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.add_acquisition_cost(
        source=args.get("source", ""), cost_mxn=args.get("cost_mxn"),
        month=args.get("month", ""), notes=args.get("notes", "")),
        indent=2, default=str)


def tool_delete_acquisition_cost(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.delete_acquisition_cost(args["cost_id"]),
                      indent=2, default=str)


# GROUP C — Nurture + Fireflies + Milestones + Funnel Snapshot

def tool_get_nurture(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.get_nurture(args["deal_id"]), indent=2, default=str)


def tool_quick_add_contact(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    res = _crm.quick_add_contact(
        name=args.get("name", ""), company=args.get("company", ""),
        email=args.get("email", ""), phone=args.get("phone", ""),
        whatsapp=args.get("whatsapp", ""), linkedin_url=args.get("linkedin_url", ""),
        source=args.get("source", ""), notes=args.get("notes", ""))
    # Apply loop / lead_source growth fields if provided.
    if res.get("deal_id") and (args.get("loop") or args.get("lead_source")):
        from dashboard import growth as _growth
        _growth.update_deal_growth(
            res["deal_id"], growth_loop=args.get("loop"),
            lead_source=args.get("lead_source"))
    return json.dumps(res, indent=2, default=str)


def tool_get_cadence_status(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.get_cadence_status(args["deal_id"]), indent=2, default=str)


def tool_get_monthly_strategic_view(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.monthly_strategic_view(args.get("month")), indent=2, default=str)


def tool_get_conversion_path(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.conversion_path_for_deal(args["deal_id"]), indent=2, default=str)


def tool_generate_nurture(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.generate_nurture(args["deal_id"]), indent=2, default=str)


def tool_update_nurture(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.set_nurture_status(
        args["step_id"], args.get("status", "")), indent=2, default=str)


def tool_get_fireflies_analytics(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import fireflies as _fireflies
    return json.dumps(_fireflies.analytics(limit=int(args.get("limit", 10))),
                      indent=2, default=str)


def tool_get_coaching(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import fireflies as _fireflies
    return json.dumps(_fireflies.coaching(limit=int(args.get("limit", 10))),
                      indent=2, default=str)


def tool_get_meeting_feedback(args: dict) -> str:
    """Get feedback for a specific meeting or search by participant/title.

    If transcript_id is provided, fetch that meeting's full transcript + signals.
    If search is provided, list recent meetings filtered by title/participant.
    """
    from dashboard import fireflies as _ff

    transcript_id = args.get("transcript_id", "")
    search = (args.get("search") or "").lower().strip()
    limit = int(args.get("limit", 20))

    if transcript_id:
        # Fetch single transcript by ID
        query = """
        query Transcript($id: String!) {
          transcript(id: $id) {
            id title date participants
            summary { overview action_items keywords }
            sentences { speaker_name text }
          }
        }
        """
        try:
            data = _ff._graphql(query, {"id": transcript_id})
            t = data.get("transcript")
            if not t:
                return json.dumps({"error": "transcript not found", "id": transcript_id})
            signals = _ff.extract_signals(t)
            summary = t.get("summary") or {}
            return json.dumps({
                "id": t.get("id"),
                "title": t.get("title"),
                "date": _ff._parse_date(t.get("date")),
                "participants": t.get("participants") or [],
                "summary": {
                    "overview": summary.get("overview") or "",
                    "action_items": summary.get("action_items") or "",
                    "keywords": summary.get("keywords") or [],
                },
                "signals": signals,
                "sentence_count": len(t.get("sentences") or []),
                "sentences": t.get("sentences") or [],
            }, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e), "id": transcript_id})

    # Search mode: list recent meetings, filter by search term
    try:
        transcripts = _ff.fetch_transcripts(limit=limit)
        if search:
            filtered = []
            for t in transcripts:
                title = (t.get("title") or "").lower()
                participants = " ".join(str(p).lower() for p in (t.get("participants") or []))
                if search in title or search in participants:
                    filtered.append(t)
            transcripts = filtered

        results = []
        for t in transcripts:
            signals = _ff.extract_signals(t)
            results.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "date": _ff._parse_date(t.get("date")),
                "participants": t.get("participants") or [],
                "signals": signals,
            })
        return json.dumps({
            "count": len(results),
            "meetings": results,
        }, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_list_milestones(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.list_plan_milestones(), indent=2, default=str)


def tool_update_milestone(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    completed = bool(args["completed"]) if "completed" in args else None
    return json.dumps(_growth.set_milestone_completed(args["milestone_id"], completed),
                      indent=2, default=str)


def tool_capture_funnel(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.snapshot_funnel(week_start=args.get("week_start")),
                      indent=2, default=str)


# --- Phase 5: unified memory recall handler ---

def tool_recall(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    task_id = args.get("task_id")
    result = _gmem.recall(
        args["query"], project_id=args.get("project_id"),
        task_id=task_id, k=max(1, min(int(args.get("k", 8)), 25)))
    # Keep Semantica behind Hermes's existing recall tool.  This adds no MCP
    # verb or write authority: enabled success appends one bounded read-only
    # context packet, while disabled/down/stale/malformed responses preserve
    # the canonical result shape exactly.
    try:
        from dashboard import semantica_client as _semantica
        if _semantica.enabled():
            semantic = _semantica.query(
                args["query"], min(max(1, int(args.get("k", 8))), 10))
            if semantic.get("status") == "ok":
                result["semantic_context"] = semantic
    except Exception:
        pass  # projection enrichment is best-effort — never break recall
    # Optional lakehouse enrichment (§5.4): when a task is in scope, attach the
    # lakehouse's analytics context packet — reached as an MCP CLIENT of the
    # standalone lakehouse (no import, no shared DB). Feature-flagged OFF by
    # default; any failure leaves `result` exactly as the graph produced it.
    if task_id:
        try:
            from dashboard import lakehouse_client as _lh
            if _lh.enabled():
                pkt = _lh.get_context_packet(task_id)
                if pkt and pkt.get("task"):
                    t = pkt["task"]
                    result["lakehouse"] = {
                        "source": "lakehouse-mcp:get_context_packet",
                        "task": {f: t.get(f) for f in (
                            "status", "cycle_time_hours", "attempts", "crash_count",
                            "is_accepted", "verification_passed", "project_name")},
                        "project": pkt.get("project"),
                        "related_memory": (pkt.get("related_memory") or [])[:5],
                    }
        except Exception:
            pass  # enrichment is best-effort — never break recall
    return json.dumps(result, indent=2, default=str)


# --- Memory upgrade (Phase 2/3) handlers ---

def tool_contradiction_check(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    existing = args.get("existing_facts") or []
    if not isinstance(existing, list):
        existing = [existing]
    return json.dumps(
        _gmem.contradiction_check(args["new_fact"], [str(f) for f in existing]),
        indent=2, default=str)


def tool_find_related(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    limit = max(1, min(int(args.get("limit", 5)), 25))
    return json.dumps(
        {"related": _gmem.find_related(args["query"], limit=limit)},
        indent=2, default=str)


def tool_get_metabolism_stats(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    return json.dumps(_gmem.get_metabolism_stats(), indent=2, default=str)


def tool_evolve_node(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    props = args.get("properties") or {}
    if not isinstance(props, dict):
        return json.dumps({"error": "properties must be an object"})
    ok = _gmem.evolve_node(args["node_id"], props)
    return json.dumps({"evolved": ok, "node_id": args["node_id"]}, default=str)


def tool_archive_stale(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import graph_memory as _gmem
    return json.dumps({"archived": _gmem.archive_stale()}, default=str)


# --- Phase 4: runnable acceptance handlers ---

def tool_run_contract(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_gov.run_contract(
        args["task_id"], agent=args.get("agent"),
        session_key=args.get("session_key")), indent=2, default=str)


def tool_set_contract(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_gov.set_contract(args["task_id"], args.get("contract_cmd")), default=str)


def tool_get_run_envelope(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    value = _gov.get_run_envelope(args["task_id"])
    return json.dumps(value if value is not None else {"error": "run envelope not found"},
                      indent=2, default=str)


def tool_set_run_envelope(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    contract = _gov.set_contract(args["task_id"], args.get("contract_cmd"))
    if contract.get("status") != "ok":
        return json.dumps(contract, default=str)
    return json.dumps(_gov.set_run_envelope(
        args["task_id"], args["practice_text"], args["host"], args["context"]),
        indent=2, default=str)


# --- Phase 3: the Canvas / Today handlers ---

def tool_get_day_plan(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_canvas.get_day_plan(
        args.get("date"), include_candidates=bool(args.get("candidates"))),
        indent=2, default=str)


def tool_wrap_day(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_canvas.wrap_day(args.get("date")), indent=2, default=str)


def tool_plan_day(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_canvas.plan_day(args.get("task_ids") or [],
                                       date=args.get("date")), default=str)


def tool_plan_task(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_canvas.plan_task(
        args["task_id"], planned_for=args.get("planned_for"),
        plan_order=args.get("plan_order"), due_date=args.get("due_date"),
        clear_plan=bool(args.get("clear"))), default=str)


def tool_set_session_role(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_orch.set_session_role(
        args["session_key"], role=args.get("role"), feature=args.get("feature"),
        project=args.get("project"), auto_compact=args.get("auto_compact")), default=str)


# ---------------------------------------------------------------------------
# Phase 2 — Session Control (16 tools)
# ---------------------------------------------------------------------------
# These proxy the LIVE dashboard API (like tool_get_sessions) rather than
# re-deriving session state in this subprocess. Session state — the warm
# sessions cache (SSH/tmux probes), the in-memory prompt history, and the
# endpoint-level validation — all live in the running dashboard process. Going
# through HTTP is the truest "thin wrapper over existing dashboard API logic":
# zero duplicated SQL, zero cold re-probing, and one source of truth for what
# agents see vs. what the dashboard shows.

def _dash(method: str, path: str, body: Optional[dict] = None,
          timeout: int = 30) -> str:
    """Proxy a dashboard API endpoint and return its JSON as an indented string.
    Surfaces non-2xx responses as {"error": <detail>, "status": <code>} so the
    caller sees the endpoint's own 400/404 messages verbatim."""
    url = f"{DASHBOARD_INTERNAL_URL}{path}"
    cmd = ["curl", "-s", "-X", method, "-w", "\n%{http_code}", url]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    # Authenticate mutating requests (the dashboard's Bearer gate).
    if method in _DASH_MUTATING_METHODS and _DASH_TOKEN:
        cmd += ["-H", f"Authorization: Bearer {_DASH_TOKEN}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"dashboard request failed: {e}"})
    if r.returncode != 0:
        return json.dumps({"error": f"dashboard not reachable at {DASHBOARD_URL}: "
                                    f"{r.stderr.strip() or 'curl failed'}"})
    payload, _, status = r.stdout.rpartition("\n")
    if not status:  # no status line captured — treat whole output as payload
        payload, status = r.stdout, ""
    try:
        data = json.loads(payload) if payload.strip() else {}
    except json.JSONDecodeError:
        data = {"raw": payload}
    if status and not status.startswith("2"):
        detail = data.get("detail") if isinstance(data, dict) else None
        return json.dumps({"error": detail or f"HTTP {status}", "status": int(status)},
                          indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


def _hs(args: dict):
    """URL-encode host + session_name path segments (names/UUIDs may need it)."""
    return quote(str(args["host"]), safe=""), quote(str(args["session_name"]), safe="")


# --- Group A: session read/write ---

def tool_get_session_history(args: dict) -> str:
    h, s = _hs(args)
    return _dash("GET", f"/api/sessions/{h}/{s}/history")


def tool_get_session_output(args: dict) -> str:
    h, s = _hs(args)
    path = f"/api/sessions/{h}/{s}/output"
    if args.get("lines"):
        # Chat clients set this — clamp so a huge/negative value can't turn
        # into an unbounded capture.
        path += f"?lines={max(1, min(500, int(args['lines'])))}"
    return _dash("GET", path)


def tool_send_to_session(args: dict) -> str:
    h, s = _hs(args)
    # tmux-send's validated delivery can legitimately take ~35s (retries);
    # the proxy must outlive it or a landed send reads as "couldn't measure".
    return _dash("POST", f"/api/sessions/{h}/{s}/send",
                 {"text": args.get("text", "")}, timeout=45)


def tool_resend_last(args: dict) -> str:
    h, s = _hs(args)
    return _dash("POST", f"/api/sessions/{h}/{s}/resend-last")


def tool_revive_session(args: dict) -> str:
    h, s = _hs(args)
    return _dash("POST", f"/api/sessions/{h}/{s}/revive")


def tool_kill_session(args: dict) -> str:
    h, s = _hs(args)
    return _dash("POST", f"/api/sessions/{h}/{s}/kill")


def tool_prune_transcripts(args: dict) -> str:
    body: dict = {}
    if args.get("older_than_days") is not None:
        body = {"hours": float(args["older_than_days"]) * 24}
    return _dash("POST", "/api/sessions/prune-transcripts", body)


def tool_compact_session(args: dict) -> str:
    h, s = _hs(args)
    return _dash("POST", f"/api/sessions/{h}/{s}/compact")


# --- Group B: session discovery and linking ---

def tool_list_coordinators(args: dict) -> str:
    return _dash("GET", "/api/coordinators")


def tool_get_session_tasks(args: dict) -> str:
    h, s = _hs(args)
    return _dash("GET", f"/api/sessions/{h}/{s}/tasks")


def tool_link_task_session(args: dict) -> str:
    # The graph links a task to a session by session_id; the dashboard uses the
    # bare session_name as that id (see get_session_tasks), so pass it through.
    tid = quote(str(args["task_id"]), safe="")
    return _dash("PATCH", f"/api/tasks/{tid}/session",
                 {"session_id": args["session_name"]})


def tool_list_compact_candidates(args: dict) -> str:
    return _dash("GET", "/api/compact-candidates")


def tool_get_session_meta(args: dict) -> str:
    raw = _dash("GET", "/api/session-meta")
    host, sn = args.get("host"), args.get("session_name")
    if not host and not sn:
        return raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    sess = data.get("sessions")
    if isinstance(sess, dict):
        def keep(key, meta):
            if sn and sn not in str(key):
                return False
            if host and isinstance(meta, dict) and meta.get("host") not in (None, host):
                return False
            return True
        data["sessions"] = {k: m for k, m in sess.items() if keep(k, m)}
    return json.dumps(data, indent=2, default=str)


# --- Group C: session events ---

def tool_create_session_event(args: dict) -> str:
    body = {
        "session_key": args["session_name"],
        "host": args.get("host"),
        "kind": args.get("event_type", "note"),
        "message": args.get("message", ""),
    }
    return _dash("POST", "/api/session-events", body)


def tool_list_session_events(args: dict) -> str:
    path = "/api/session-events?limit=50"
    if args.get("session_name"):
        path += f"&session_key={quote(str(args['session_name']), safe='')}"
    if args.get("unresolved_only"):
        path += "&unresolved_only=true"
    return _dash("GET", path)


def tool_resolve_session_event(args: dict) -> str:
    eid = quote(str(args["event_id"]), safe="")
    return _dash("POST", f"/api/session-events/{eid}/resolve")


# --- Phase 5: Usage/Ops + Session Meta ---
# Thin proxies over the dashboard's own endpoints (via _dash).

# Group A — usage

def tool_list_usage_providers(args: dict) -> str:
    return _dash("GET", "/api/usage/providers")


def tool_refresh_ollama_usage(args: dict) -> str:
    return _dash("POST", "/api/usage/refresh-ollama")


def tool_refresh_claude_usage(args: dict) -> str:
    return _dash("POST", "/api/usage/refresh-claude")


def tool_get_ollama_completion(args: dict) -> str:
    body = {"model": args["model"], "metadata": {"prompt": args.get("prompt")}}
    return _dash("POST", "/api/usage/ollama-completion", body)


def tool_get_stats(args: dict) -> str:
    return _dash("GET", "/api/stats")


# Group B — ops/health + session meta

def tool_get_recent_errors(args: dict) -> str:
    return _dash("GET", "/api/errors/recent")


def tool_get_ops_status(args: dict) -> str:
    return _dash("GET", "/api/ops-status")


def tool_set_session_meta(args: dict) -> str:
    # The endpoint reads named fields (role/feature/tag/notes/project/…); map the
    # generic key→value onto whichever field the caller names.
    body = {"session_key": args["session_name"], args["key"]: args["value"]}
    if args.get("host"):
        body["host"] = args["host"]
    return _dash("POST", "/api/session-meta", body)


# --- Phase 3: Task Lifecycle ---
# Thin proxies over the dashboard's own endpoints (via _dash), so agents and the
# UI share one code path — no duplicated SQL.

def _tid(args: dict) -> str:
    return quote(str(args["task_id"]), safe="")


# Group A — accept/reject/abort/fail (operator actions, PRIVILEGED)

def tool_accept_task(args: dict) -> str:
    return _dash("POST", f"/api/tasks/{_tid(args)}/accept")


def tool_review_accept(args: dict) -> str:
    """DEFAULT SCOPE review-accept: any agent can call this, but only on tasks
    already in 'review' status with a passing result_reported event. This closes
    the review-sweep loop without needing the PRIVILEGED accept_task tool."""
    import sqlite3
    from dashboard.db import KANBAN_DB
    tid = args.get("task_id") or ""
    if not tid:
        return json.dumps({"error": "task_id required"})
    conn = sqlite3.connect(str(KANBAN_DB))
    try:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT id, status FROM tasks WHERE id=?", (tid,)).fetchone()
        if not task:
            return json.dumps({"error": f"task {tid} not found"})
        if task["status"] != "review":
            return json.dumps({"error": f"task {tid} is not in review (status={task['status']})"})
        # Check for a passing result_reported event
        passing = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='result_reported' "
            "AND json_extract(payload, '$.passed') = 1 LIMIT 1", (tid,)
        ).fetchone()
        if not passing:
            return json.dumps({"error": f"task {tid} has no passing result_reported event"})
    finally:
        conn.close()
    # All checks pass — route through the same accept endpoint
    return _dash("POST", f"/api/tasks/{tid}/accept")


def tool_reject_task(args: dict) -> str:
    body = {"reason": args["reason"]} if args.get("reason") else {}
    return _dash("POST", f"/api/tasks/{_tid(args)}/reject", body)


def tool_abort_task(args: dict) -> str:
    return _dash("POST", f"/api/tasks/{_tid(args)}/abort")


def tool_fail_task(args: dict) -> str:
    # The endpoint records the failure detail under 'error'.
    body = {"error": args["reason"]} if args.get("reason") else {}
    return _dash("POST", f"/api/tasks/{_tid(args)}/fail", body)


# Group B — heartbeat + context reads

def tool_heartbeat(args: dict) -> str:
    return _dash("POST", f"/api/tasks/{_tid(args)}/heartbeat")


def tool_get_context(args: dict) -> str:
    et = quote(str(args["entity_type"]), safe="")
    eid = quote(str(args["entity_id"]), safe="")
    return _dash("GET", f"/api/context/{et}/{eid}")


def tool_list_icebox(args: dict) -> str:
    return _dash("GET", "/api/icebox")


def tool_list_delivered(args: dict) -> str:
    return _dash("GET", "/api/delivered")


# Group C — project/roadmap/plan reads + comment delete

def tool_get_project_detail(args: dict) -> str:
    pid = quote(str(args["project_id"]), safe="")
    return _dash("GET", f"/api/projects/{pid}/detail")


def tool_get_initiative_events(args: dict) -> str:
    iid = quote(str(args["initiative_id"]), safe="")
    return _dash("GET", f"/api/roadmap/{iid}/events")


def tool_get_day_plan_candidates(args: dict) -> str:
    return _dash("GET", "/api/day-plan/candidates")


def tool_delete_comment(args: dict) -> str:
    cid = quote(str(args["comment_id"]), safe="")
    return _dash("DELETE", f"/api/comments/{cid}")


# --- Phase 4: Sprint/Cycle + Lakehouse + Specs ---
# Thin proxies over the dashboard's own endpoints (via _dash), so agents and the
# UI share one code path — no duplicated SQL.

def _sid(args: dict) -> str:
    return quote(str(args["sprint_id"]), safe="")


# Group A — Sprint/Cycle

def tool_get_sprint_tasks(args: dict) -> str:
    return _dash("GET", f"/api/sprints/{_sid(args)}/tasks")


def tool_get_cycle_board(args: dict) -> str:
    return _dash("GET", "/api/cycle/active/board")


def tool_get_cycles_calendar(args: dict) -> str:
    return _dash("GET", "/api/cycles/calendar")


def tool_roll_cycle(args: dict) -> str:
    return _dash("POST", "/api/cycles/roll")


def tool_delete_sprint(args: dict) -> str:
    return _dash("DELETE", f"/api/sprints/{_sid(args)}")


def tool_commit_cycle(args: dict) -> str:
    # The endpoint commits a list of tasks to the cycle (each routes through
    # assign_task_sprint). sprint_id='icebox' pulls them all to the icebox.
    body = {"task_ids": args.get("task_ids") or []}
    return _dash("POST", f"/api/cycles/{_sid(args)}/commit", body)


# Group B — Lakehouse

def tool_get_lakehouse_overview(args: dict) -> str:
    return _dash("GET", "/api/lakehouse/overview")


def tool_get_mcp_manifest(args: dict) -> str:
    return _dash("GET", "/api/mcp/manifest")


# Group C — Specs + Ledger

def tool_write_spec(args: dict) -> str:
    feature = quote(str(args["feature"]), safe="")
    return _dash("PUT", f"/api/specs/{feature}", {"content": args.get("content", "")})


def tool_get_ledger(args: dict) -> str:
    path = "/api/ledger?limit=50"
    if args.get("task_id"):
        path += f"&task_id={quote(str(args['task_id']), safe='')}"
    return _dash("GET", path)


# Group D — Orchestration

def tool_orchestration_sweep(args: dict) -> str:
    return _dash("POST", "/api/orchestration/sweep")


def tool_score_all_leads(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _g
    return json.dumps(_g.score_all_leads(), indent=2, default=str)


def tool_get_fireflies_signals(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    deal_id = args.get("deal_id", "")
    if not deal_id:
        return json.dumps({"error": "deal_id required"})
    from dashboard import fireflies as _ff
    return json.dumps(_ff.latest_signals_for_deal(deal_id) or {}, indent=2, default=str)


def tool_fetch_fireflies_for_deal(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    deal_id = args.get("deal_id", "")
    if not deal_id:
        return json.dumps({"error": "deal_id required"})
    from dashboard import fireflies as _ff
    # The caller's window is honored (it used to be silently dropped): `limit`
    # widens the transcript scan, `since` floors it by date.
    res = _ff.fetch_and_store_for_deal(
        deal_id, limit=int(args.get("limit", 25) or 25), since=args.get("since"))
    return json.dumps(res, indent=2, default=str)


def tool_get_readiness(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import readiness as _rd
    return json.dumps(_rd.readiness_overview(), indent=2, default=str)


def tool_score_readiness(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    deal_id = args.get("deal_id", "")
    if not deal_id:
        return json.dumps({"error": "deal_id required"})
    from dashboard import readiness as _rd
    return json.dumps(_rd.score_readiness(deal_id), indent=2, default=str)


def tool_score_readiness_from_fireflies(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    deal_id = args.get("deal_id", "")
    if not deal_id:
        return json.dumps({"error": "deal_id required"})
    from dashboard import readiness as _rd
    return json.dumps(_rd.score_readiness_from_fireflies(deal_id),
                      indent=2, default=str)


# --- Parity fill: proxy tools for API endpoints that lacked an MCP tool ---

def tool_search(args: dict) -> str:
    from urllib.parse import urlencode
    qs = urlencode({"q": args.get("q", ""), "limit": int(args.get("limit", 8))})
    return _dash("GET", f"/api/search?{qs}")


def tool_get_memory(args: dict) -> str:
    return _dash("GET", "/api/memory")


def tool_update_memory(args: dict) -> str:
    return _dash("PATCH", "/api/memory", {
        "source": args.get("source", "agent"),
        "index": int(args["index"]),
        "content": args.get("content", ""),
    })


def tool_delete_memory(args: dict) -> str:
    from urllib.parse import urlencode
    qs = urlencode({"source": args.get("source", "agent"), "index": int(args["index"])})
    return _dash("DELETE", f"/api/memory?{qs}")


def tool_get_stale_deals(args: dict) -> str:
    days = int(args.get("days", 7))
    return _dash("GET", f"/api/crm/stale?days={days}")


def tool_list_crm_proposals(args: dict) -> str:
    status = args.get("status", "proposed")
    return _dash("GET", f"/api/crm/proposals?status={status}")


def tool_propose_crm_update(args: dict) -> str:
    return _dash("POST", "/api/crm/proposals", {
        "deal_id": args.get("deal_id", ""),
        "kind": args.get("kind", ""),
        "payload": args.get("payload") or {},
        "evidence_kind": args.get("evidence_kind", "manual"),
        "evidence_ref": args.get("evidence_ref", ""),
    })


def tool_derive_crm_proposals(args: dict) -> str:
    return _dash("POST", "/api/crm/proposals/derive", {})


def tool_list_commercial_proposals(args: dict) -> str:
    return _dash("GET", f"/api/crm/deals/{args['deal_id']}/commercial-proposals")


def tool_register_commercial_proposal(args: dict) -> str:
    deal_id = args.get("deal_id", "")
    body = {key: args.get(key) for key in
            ("revision", "workspace_path", "manifest_path", "proposal_path", "prototype_path",
             "workspace_schema_version", "evidence_manifest_path", "checker_report_path",
             "quality_report_path")}
    return _dash("POST", f"/api/crm/deals/{deal_id}/commercial-proposals", body)


def tool_verify_commercial_proposal(args: dict) -> str:
    packet_id = args.get("packet_id", "")
    body = {key: args.get(key) for key in
            ("package_path", "manifest_sha256", "package_sha256", "verification_receipt",
             "receipt_sha256", "evidence_manifest_sha256", "checker_report_sha256",
             "quality_report_sha256", "quality_status")}
    return _dash("POST", f"/api/crm/commercial-proposals/{packet_id}/verify", body)


def tool_approve_crm_proposal(args: dict) -> str:
    return _dash("POST", f"/api/crm/proposals/{args['id']}/approve", {"via": "mcp"})


def tool_dismiss_crm_proposal(args: dict) -> str:
    return _dash("POST", f"/api/crm/proposals/{args['id']}/dismiss", {"via": "mcp"})


def tool_get_growth_radar(args: dict) -> str:
    return _dash("GET", "/api/growth/radar")


def tool_get_friday_brief(args: dict) -> str:
    return _dash("GET", "/api/friday-brief")


def tool_get_weekly_reflection(args: dict) -> str:
    week = args.get("week")
    qs = f"?week={week}" if week else ""
    out = _dash("GET", f"/api/reflection/weekly{qs}")
    n = int(args.get("n_history", 0) or 0)
    if n > 0:
        hist = _dash("GET", f"/api/reflection/weekly/history?n={n}")
        return json.dumps({"current": json.loads(out),
                           "history": json.loads(hist)}, indent=2,
                          ensure_ascii=False)
    return out


def tool_save_weekly_reflection(args: dict) -> str:
    body = {k: v for k, v in args.items()
            if k in ("q_regale", "q_declare", "q_referido", "q_propuesta",
                     "q_aprendi") and isinstance(v, str)}
    # MCP writes only the CURRENT week — no week override on this path.
    return _dash("POST", "/api/reflection/weekly", body)


def tool_crm_decay(args: dict) -> str:
    from urllib.parse import urlencode
    qs = urlencode({
        "days_to_stalled": int(args.get("days_to_stalled", 30)),
        "days_to_lost": int(args.get("days_to_lost", 90)),
    })
    return _dash("POST", f"/api/crm/decay?{qs}")


# --- Parity fill 2 (2026-07 audit): backend verbs that lacked an MCP tool ---

def tool_get_deal(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    deal = _crm.get_deal(args["deal_id"])
    if deal is None:
        return json.dumps({"error": f"deal '{args['deal_id']}' not found"})
    return json.dumps(deal, indent=2, default=str)


def tool_get_deal_fireflies(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps({"deal_id": args["deal_id"],
                       "meetings": _crm.get_deal_fireflies(args["deal_id"])},
                      indent=2, default=str)


def tool_get_deal_fireflies_latest(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps(_crm.get_deal_fireflies_latest(args["deal_id"]) or {},
                      indent=2, default=str)


def tool_list_deal_children(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import crm as _crm
    return json.dumps({"parent_deal_id": args["deal_id"],
                       "children": _crm.list_deal_children(args["deal_id"])},
                      indent=2, default=str)


def tool_compute_funnel(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import growth as _growth
    return json.dumps(_growth.compute_funnel(), indent=2, default=str)


def tool_get_project(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    pid = _db.resolve_project(args["project_id"]) or args["project_id"]
    proj = _sprints.get_project(pid)
    if proj is None:
        return json.dumps({"error": f"project '{args['project_id']}' not found"})
    return json.dumps(proj, indent=2, default=str)


def tool_get_next_week_tasks(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"tasks": _sprints.get_next_week_tasks(args.get("project_id"))},
                      indent=2, default=str)


def tool_get_future_tasks(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps({"tasks": _sprints.get_future_tasks(args.get("project_id"))},
                      indent=2, default=str)


def tool_set_task_assignee(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.set_task_assignee(args["task_id"], args["assignee"]),
                      default=str)


def tool_set_scheduled_week(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.set_scheduled_week(args["task_id"], args.get("week") or None),
                      default=str)


def tool_assign_task_project(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    pid = _db.resolve_project(args["project_id"]) or args["project_id"]
    if _sprints.get_project(pid) is None:
        return json.dumps({"error": f"project '{args['project_id']}' not found"})
    return json.dumps(_sprints.assign_task_project(args["task_id"], pid), default=str)


def tool_set_auto_cycle(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.set_auto_cycle(args["task_id"], bool(args["enabled"])),
                      default=str)


def tool_update_project(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    pid = _db.resolve_project(args["project_id"]) or args["project_id"]
    return json.dumps(_sprints.update_project(
        pid, name=args.get("name"), slug=args.get("slug"),
        description=args.get("description"), color=args.get("color"),
        icon=args.get("icon")), default=str)


def tool_create_cycle(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    return json.dumps(_sprints.create_cycle(
        name=args.get("name"), goal=args.get("goal", ""),
        start_date=args.get("start_date"), end_date=args.get("end_date")),
        indent=2, default=str)


def tool_export_roadmap(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    _strategy.export_roadmap()
    return json.dumps({"status": "exported",
                       "note": "roadmap.json regenerated from the initiatives table"})


# --- Personal Health tool implementations ---

def tool_get_health_today(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import health as _health
    return json.dumps(_health.get_today(), indent=2, default=str)


def tool_get_health_routines(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import health as _health
    return json.dumps({"routines": _health.get_routines()}, indent=2, default=str)


def tool_check_health_routine(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import health as _health
    try:
        return json.dumps(_health.check_routine(args["routine_id"], args.get("note")),
                          indent=2, default=str)
    except LookupError as e:
        return json.dumps({"error": str(e)})


def tool_uncheck_health_routine(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import health as _health
    try:
        return json.dumps(_health.uncheck_routine(args["routine_id"]),
                          indent=2, default=str)
    except LookupError as e:
        return json.dumps({"error": str(e)})


def tool_get_health_config(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import health as _health
    return json.dumps(_health.get_config(), indent=2, default=str)


def tool_get_health_plate(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import health as _health
    return json.dumps(_health.get_plate_data(), indent=2, default=str)


# --- Daily Reflection tool implementations ---
# Today-only + create-guarded on purpose: these run on the DEFAULT scope so the
# Telegram thread agent can save the operator's replies, so they must not be able to
# rewrite historical personal text. Arbitrary-date edits live behind the
# Bearer-authed dashboard API only.

def tool_get_reflection_today(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import reflection as _refl
    return json.dumps(_refl.get_today(), indent=2, default=str)


def tool_get_personal_okrs(args: dict) -> str:
    """Privileged thin read over the dashboard's canonical composer."""
    err = _need_loop()
    if err:
        return err
    from dashboard import okrs as _okrs
    return json.dumps(
        _okrs.get_okrs(args.get("year", 2026), args.get("history_limit", 5)),
        indent=2, ensure_ascii=False, default=str,
    )


def tool_save_reflection_morning(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import reflection as _refl
    today = _refl.today_str()
    cur = _refl.get_reflection(today)
    # The reflection and the check-in are ONE input with TWO independent
    # guards. An already-saved morning must not swallow the numbers (they may
    # arrive in a later message), and a refused/unavailable check-in must never
    # cost the operator his intentions.
    carga = args.get("carga")
    carga_result = _cogload_mark(carga, "morning") if carga is not None else None
    if cur.get("morning") and not args.get("overwrite"):
        out = {"status": "exists", "morning": cur["morning"],
               "note": "La reflexión matutina de hoy ya está guardada; "
                       "pasa overwrite=true solo si el operador pide reemplazarla."}
        if carga_result is not None:
            out["carga"] = carga_result
        return json.dumps(out, indent=2, default=str)
    try:
        out = _refl.save_morning(
            today, args.get("intentions") or [], source="telegram")
    except ValueError as e:
        return json.dumps({"error": str(e),
                           **({"carga": carga_result} if carga_result else {})})
    if carga_result is not None:
        out["carga"] = carga_result
    return json.dumps(out, indent=2, default=str)


def _cogload_mark(carga: dict, slot: str) -> dict:
    """Persist ONE check-in THROUGH `cogload mark` — the store's only writer.

    Shared by the two reflection tools (where the numbers actually arrive, in
    the same reply as the intentions or the wins) and by save_cogload_label
    (the correction path). One writer, one validation, one set of guarantees.

    The allowlist deliberately admits no `note` and no `force`: free text from a
    Telegram reply can therefore never reach the store, and the agent cannot
    override the one-per-(day, slot) guard. Those are type-level guarantees, not
    instructions the model has to be trusted to follow — MCP schemas are
    advertised, NOT enforced, so arguments reach handlers unvalidated and
    "the schema has no note property" is documentation, not a control.
    """
    import subprocess as _sp
    if not isinstance(carga, dict):
        return {"error": "carga must be an object"}
    unexpected = set(carga) - {"anx", "anx_day", "stress", "eff"}
    if unexpected:
        return {"error": f"unexpected fields: {sorted(unexpected)}"}
    if slot not in ("morning", "evening"):
        return {"error": "slot must be 'morning' or 'evening'"}
    cmd = ["cogload", "mark", "--slot", slot, "--src", "telegram"]
    given = {}
    for key, flag in (("anx", "--anx"), ("anx_day", "--anx-day"),
                      ("stress", "--stress"), ("eff", "--eff")):
        v = carga.get(key)
        if v is None:
            continue
        if isinstance(v, bool):
            return {"error": f"{key} must be an integer 1-5"}
        try:
            v = int(v)
        except (TypeError, ValueError):
            return {"error": f"{key} must be an integer 1-5"}
        if not 1 <= v <= 5:
            return {"error": f"{key} must be 1-5"}
        cmd += [flag, str(v)]
        given[key] = v
    if not given:
        return {"error": "at least one measure is required"}
    if "anx_day" in given and slot != "evening":
        return {"error": "anx_day is a whole-day recall; evening slot only"}
    try:
        r = _sp.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, _sp.SubprocessError) as e:
        return {"error": f"cogload unavailable: {e}"[:200]}
    if r.returncode == 3:
        return {"status": "exists",
                "note": f"el momento '{slot}' de hoy ya tiene lectura; "
                        "correccion via `cogload mark --force`"}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "cogload mark failed").strip()[:200]}
    return {"ok": True, "slot": slot, **given}


def tool_save_cogload_label(args: dict) -> str:
    """Correction path for a check-in that arrived outside its reflection.

    The normal path is save_reflection_morning / save_reflection_evening: the
    numbers ride the reply the operator already sends, so there is ONE input, not a
    second ritual bolted onto it.
    """
    if not isinstance(args, dict):
        return json.dumps({"error": "arguments must be an object"})
    slot = args.get("slot")
    carga = {k: v for k, v in args.items() if k != "slot"}
    unexpected = set(args) - {"slot", "anx", "anx_day", "stress", "eff"}
    if unexpected:
        return json.dumps({"error": f"unexpected fields: {sorted(unexpected)}"})
    return json.dumps(_cogload_mark(carga, slot))


def tool_get_cogload_status(args: dict) -> str:
    """Instrument health only — never today's behavioural numbers (anchoring)."""
    import subprocess as _sp
    out = {"available": False}
    try:
        r = _sp.run(["cogload", "status", "--json"], capture_output=True,
                    text=True, timeout=10)
        if r.returncode in (0, 1) and r.stdout.strip():
            st = json.loads(r.stdout)
            out = {"available": True, "ok": st.get("ok"),
                   "subsystems": st.get("subsystems"),
                   "session_type": st.get("session_type")}
        else:
            out = {"available": False, "reason": (r.stderr or "no output").strip()[:120]}
    except Exception as e:
        out = {"available": False, "reason": f"{type(e).__name__}: {e}"[:120]}
    try:
        from dashboard import cogload as _cg
        labs = _cg.load_labels() or []
        days = {l.get("day") for l in labs if l.get("day")}
        import datetime as _dt
        out["labeled_days"] = len(days)
        out["today_labeled"] = _dt.date.today().isoformat() in days
    except Exception:
        pass
    return json.dumps(out)


def tool_save_reflection_evening(args: dict) -> str:
    err = _need_loop()
    if err:
        return err
    from dashboard import reflection as _refl
    today = _refl.today_str()
    cur = _refl.get_reflection(today)
    # Same two-independent-guards rule as the morning (see there).
    carga = args.get("carga")
    carga_result = _cogload_mark(carga, "evening") if carga is not None else None
    if cur.get("evening") and not args.get("overwrite"):
        out = {"status": "exists", "evening": cur["evening"],
               "note": "La reflexión nocturna de hoy ya está guardada; "
                       "pasa overwrite=true solo si el operador pide reemplazarla."}
        if carga_result is not None:
            out["carga"] = carga_result
        return json.dumps(out, indent=2, default=str)
    try:
        out = _refl.save_evening(
            today, args.get("wins") or [],
            args.get("misses") or [],
            args.get("adjustments") or [],
            source="telegram")
    except ValueError as e:
        return json.dumps({"error": str(e),
                           **({"carga": carga_result} if carga_result else {})})
    if carga_result is not None:
        out["carga"] = carga_result
    return json.dumps(out, indent=2, default=str)


# --- Tool dispatch ---
#
# NOTE — there is deliberately no `dispatch_task` verb here, and adding one is a
# design change, not a parity fix.
#
# Dispatching a task spawns a process or hands work to the gateway; in phase 1
# every dispatch is human-initiated from the web UI, where **the click IS the
# approval** (ruling 2). That is what lets phase 1 ship without any ASK-queue
# mechanics at all. An MCP verb would hand agents a self-service path to spawn
# onward work with no human in the loop and no queue to bound it — so the
# ABSENCE of the verb is the guard, exactly as it is for the three conversion
# verbs (red line 11). Agent-initiated dispatch waits for the ASK
# queue (phase 2). The dashboard route `POST /api/tasks/{id}/dispatch` sits
# behind MutatingAuthMiddleware and is the only way in.
# `tests/test_dispatch.py` asserts this absence; deleting the verb from that
# test is the only honest way to add one here.

# --- Journey: the contextual layer (fase 1 step 7 + ADICIÓN 9) ---------------
# All three call the dashboard package IN-PROCESS rather than proxying through
# `_dash`: they are pure reads over the same SQLite file, so a curl hop would
# add a second failure mode (the dashboard being down) to a question that needs
# none — and it would make the verbs untestable without a live server.

def tool_get_journey_pulse(args: dict) -> str:
    """The whole cycle for one reference, JSON + the Spanish block to relay.

    `text` ships ALONGSIDE the payload, not instead of it: an agent relaying a
    pulse should quote the rendered block verbatim (it is deterministic, already
    stage-grouped and already carries the deep links), while a caller that wants
    to reason over the data still has it structured. Rendering it here rather
    than at each host is what keeps four hosts saying the same sentence.
    """
    err = _need_loop()
    if err:
        return err
    from dashboard import pulse as _pulse
    payload = _pulse.compose(args.get("ref"))
    return json.dumps({**payload, "text": _pulse.render(payload)},
                      indent=2, default=str, ensure_ascii=False)


def tool_get_project_hub(args: dict) -> str:
    """The five-facet project hub, addressable by name (the REST route takes an
    id or a slug; `refs.resolve` adds exact-name and unambiguous-prefix and
    refuses to guess between two matches)."""
    err = _need_loop()
    if err:
        return err
    from dashboard import attachments as _att, refs as _refs
    ref = args.get("project_ref") or args.get("project_id")
    res = _refs.resolve("project", ref)
    if not res.get("ok"):
        return json.dumps(
            {"status": "error", "code": res.get("code", "not_found"),
             "error": f"project '{ref}' " +
                      ("matches several projects — say which"
                       if res.get("code") == "ambiguous" else "not found"),
             "candidates": res.get("candidates", [])},
            indent=2, default=str, ensure_ascii=False)
    return json.dumps(_att.list_project_hub(res["id"]),
                      indent=2, default=str, ensure_ascii=False)


def tool_propose_deliver(args: dict) -> str:
    """A read-only delivery proposal + the two-tap deep link (ruling 3). This
    verb has no write path at all — the absence IS the guard, exactly as it is
    for the conversion verbs."""
    err = _need_loop()
    if err:
        return err
    from dashboard import pulse as _pulse
    return json.dumps(_pulse.propose_deliver(args.get("deal_ref")),
                      indent=2, default=str, ensure_ascii=False)


TOOL_HANDLERS = {
    "list_tasks": tool_list_tasks,
    "get_task": tool_get_task,
    "create_task": tool_create_task,
    "update_task_status": tool_update_task_status,
    "assign_task": tool_assign_task,
    "comment_task": tool_comment_task,
    "list_projects": tool_list_projects,
    "list_sprints": tool_list_sprints,
    "create_sprint": tool_create_sprint,
    "get_activity": tool_get_activity,
    "get_sessions": tool_get_sessions,
    "get_dashboard_url": tool_get_dashboard_url,
    # PRD Phase 3 — the push/pull loop
    "list_pool": tool_list_pool,
    "claim_task": tool_claim_task,
    "claim_next": tool_claim_next,
    "report_progress": tool_report_progress,
    "report_result": tool_report_result,
    "report_blocked": tool_report_blocked,
    "escalate_discovery": tool_escalate_discovery,
    # PRD Phase 4 — orient/read (default scope)
    "get_roadmap": tool_get_roadmap,
    "list_initiatives": tool_list_initiatives,
    "list_epics": tool_list_epics,
    "get_active_sprint": tool_get_active_sprint,
    "get_task_history": tool_get_task_history,
    "get_archive": tool_get_archive,
    # PRD Phase 4 — privileged orchestration (operator scope only)
    "dispatch_to_agent": tool_dispatch_to_agent,
    "set_pool": tool_set_pool,
    "set_autonomy": tool_set_autonomy,
    "pin_task_bottom": tool_pin_task_bottom,
    "change_trust_grade": tool_change_trust_grade,
    "start_sprint": tool_start_sprint,
    "assign_task_sprint": tool_assign_task_sprint,
    "close_sprint": tool_close_sprint,
    "finish_sprint": tool_finish_sprint,
    "create_project": tool_create_project,
    "create_epic": tool_create_epic,
    "assign_task_epic": tool_assign_task_epic,
    "edit_roadmap": tool_edit_roadmap,
    # Parallel orchestration — role / spec / ledger (default scope)
    "get_spec_slice": tool_get_spec_slice,
    "list_specs": tool_list_specs,
    "report_ledger": tool_report_ledger,
    "set_session_role": tool_set_session_role,
    # Verb audit Tier 3
    "get_account_chain": tool_get_account_chain,
    "delete_task": tool_delete_task,
    "list_deal_events": tool_list_deal_events,
    "delete_deal": tool_delete_deal,
    "delete_project": tool_delete_project,
    "get_sprint": tool_get_sprint,
    "get_velocity": tool_get_velocity,
    "get_graph": tool_get_graph,
    "rebuild_graph": tool_rebuild_graph,
    "get_usage": tool_get_usage,
    "archive_project": tool_archive_project,
    "register_agent": tool_register_agent,
    "resolve_input": tool_resolve_input,
    "get_health": tool_get_health,
    # Verb audit Tier 2
    "get_initiative": tool_get_initiative,
    "get_initiative_drilldown": tool_get_initiative_drilldown,
    "add_deal_event": tool_add_deal_event,
    "update_epic": tool_update_epic,
    "update_task": tool_update_task,
    "list_agents": tool_list_agents,
    "get_agent_status": tool_get_agent_status,
    "get_task_links": tool_get_task_links,
    "bulk_accept": tool_bulk_accept,
    "reconcile_sprint_ledger": tool_reconcile_sprint_ledger,
    # Verb audit Tier 1
    "create_initiative": tool_create_initiative,
    "list_accounts": tool_list_accounts,
    "list_contacts": tool_list_contacts,
    "get_task_runs": tool_get_task_runs,
    "get_task_ledger": tool_get_task_ledger,
    # Phase 6 — CRM
    "list_deals": tool_list_deals,
    "get_deal_chain": tool_get_deal_chain,
    "create_account": tool_create_account,
    "create_contact": tool_create_contact,
    "create_deal": tool_create_deal,
    "update_deal": tool_update_deal,
    "update_contact": tool_update_contact,
    # Phase 5 — unified memory
    "recall": tool_recall,
    # Memory upgrade Phase 2/3 — contradiction gate, evolution, decay, metabolism
    "contradiction_check": tool_contradiction_check,
    "find_related": tool_find_related,
    "get_metabolism_stats": tool_get_metabolism_stats,
    "evolve_node": tool_evolve_node,
    "archive_stale": tool_archive_stale,
    # Phase 4 — runnable acceptance
    "run_contract": tool_run_contract,
    "set_contract": tool_set_contract,
    "get_run_envelope": tool_get_run_envelope,
    "set_run_envelope": tool_set_run_envelope,
    # Phase 3 — the Canvas / Today
    "get_day_plan": tool_get_day_plan,
    "plan_day": tool_plan_day,
    "plan_task": tool_plan_task,
    "wrap_day": tool_wrap_day,
    # CRM Growth Phase 1 — Group A (ICP + Products + Pipeline + Scorecard)
    "get_icp": tool_get_icp,
    "update_icp": tool_update_icp,
    "list_products": tool_list_products,
    "create_product": tool_create_product,
    "update_product": tool_update_product,
    "delete_product": tool_delete_product,
    "get_pipeline": tool_get_pipeline,
    "get_pipeline_math": tool_get_pipeline_math,
    "get_pipeline_health": tool_get_pipeline_health,
    "get_forecast": tool_get_forecast,
    "get_cltv_cac": tool_get_cltv_cac,
    "get_scorecard": tool_get_scorecard,
    "get_growth_loops": tool_get_growth_loops,
    "create_lead": tool_create_lead,
    "touch_deal": tool_touch_deal,
    "score_deal": tool_score_deal,
    "update_deal_growth": tool_update_deal_growth,
    "get_funnel_trend": tool_get_funnel_trend,
    "get_pipeline_temporal": tool_get_pipeline_temporal,
    # CRM proposals (m27 — the propose-only correction inbox)
    "list_crm_proposals": tool_list_crm_proposals,
    "propose_crm_update": tool_propose_crm_update,
    "derive_crm_proposals": tool_derive_crm_proposals,
    "list_commercial_proposals": tool_list_commercial_proposals,
    "register_commercial_proposal": tool_register_commercial_proposal,
    "verify_commercial_proposal": tool_verify_commercial_proposal,
    "approve_crm_proposal": tool_approve_crm_proposal,
    "dismiss_crm_proposal": tool_dismiss_crm_proposal,
    "get_growth_radar": tool_get_growth_radar,
    "get_friday_brief": tool_get_friday_brief,
    "get_weekly_reflection": tool_get_weekly_reflection,
    "save_weekly_reflection": tool_save_weekly_reflection,
    # CRM Growth Phase 1 — Group B (Content + Speaking + Time-blocks + Acquisition Costs)
    "list_content": tool_list_content,
    "create_content": tool_create_content,
    "update_content": tool_update_content,
    "delete_content": tool_delete_content,
    "list_speaking": tool_list_speaking,
    "create_speaking": tool_create_speaking,
    "update_speaking": tool_update_speaking,
    "delete_speaking": tool_delete_speaking,
    "list_time_blocks": tool_list_time_blocks,
    "create_time_block": tool_create_time_block,
    "update_time_block": tool_update_time_block,
    "delete_time_block": tool_delete_time_block,
    "list_acquisition_costs": tool_list_acquisition_costs,
    "create_acquisition_cost": tool_create_acquisition_cost,
    "delete_acquisition_cost": tool_delete_acquisition_cost,
    # CRM Growth Phase 1 — Group C (Nurture + Fireflies + Milestones + Funnel Snapshot)
    "get_nurture": tool_get_nurture,
    "generate_nurture": tool_generate_nurture,
    "update_nurture": tool_update_nurture,
    "get_fireflies_analytics": tool_get_fireflies_analytics,
    "get_coaching": tool_get_coaching,
    "get_meeting_feedback": tool_get_meeting_feedback,
    "list_milestones": tool_list_milestones,
    "update_milestone": tool_update_milestone,
    "capture_funnel": tool_capture_funnel,
    # Phase 2 — Session Control · Group A (read/write)
    "get_session_history": tool_get_session_history,
    "get_session_output": tool_get_session_output,
    "send_to_session": tool_send_to_session,
    "resend_last": tool_resend_last,
    "revive_session": tool_revive_session,
    "kill_session": tool_kill_session,
    "prune_transcripts": tool_prune_transcripts,
    "compact_session": tool_compact_session,
    # Phase 2 — Session Control · Group B (discovery + linking)
    "list_coordinators": tool_list_coordinators,
    "get_session_tasks": tool_get_session_tasks,
    "link_task_session": tool_link_task_session,
    "list_compact_candidates": tool_list_compact_candidates,
    "get_session_meta": tool_get_session_meta,
    # Phase 2 — Session Control · Group C (events)
    "create_session_event": tool_create_session_event,
    "list_session_events": tool_list_session_events,
    "resolve_session_event": tool_resolve_session_event,
    # Phase 3 — Task Lifecycle · Group A (accept/reject/abort/fail — PRIVILEGED)
    "accept_task": tool_accept_task,
    "review_accept": tool_review_accept,
    "reject_task": tool_reject_task,
    "abort_task": tool_abort_task,
    "fail_task": tool_fail_task,
    # Phase 3 — Task Lifecycle · Group B (heartbeat + context reads)
    "heartbeat": tool_heartbeat,
    "get_context": tool_get_context,
    "list_icebox": tool_list_icebox,
    "list_delivered": tool_list_delivered,
    # Phase 3 — Task Lifecycle · Group C (project/roadmap/plan reads + comment delete)
    "get_project_detail": tool_get_project_detail,
    "get_initiative_events": tool_get_initiative_events,
    "get_day_plan_candidates": tool_get_day_plan_candidates,
    "delete_comment": tool_delete_comment,
    # Phase 4 — Sprint/Cycle · Group A
    "get_sprint_tasks": tool_get_sprint_tasks,
    "get_cycle_board": tool_get_cycle_board,
    "get_cycles_calendar": tool_get_cycles_calendar,
    "roll_cycle": tool_roll_cycle,
    "delete_sprint": tool_delete_sprint,
    "commit_cycle": tool_commit_cycle,
    # CRM Growth Phase 1 — Group C extension (lead scoring + Fireflies signals)
    "score_all_leads": tool_score_all_leads,
    "get_fireflies_signals": tool_get_fireflies_signals,
    "fetch_fireflies_for_deal": tool_fetch_fireflies_for_deal,
    # Readiness scoring — multi-dimensional buyer/product/market readiness
    "get_readiness": tool_get_readiness,
    "score_readiness": tool_score_readiness,
    "score_readiness_from_fireflies": tool_score_readiness_from_fireflies,
    # Growth Operating Framework — Phase 2 operational + strategic
    "quick_add_contact": tool_quick_add_contact,
    "get_cadence_status": tool_get_cadence_status,
    "get_monthly_strategic_view": tool_get_monthly_strategic_view,
    "get_conversion_path": tool_get_conversion_path,
    # Phase 4 — Lakehouse + closing markers
    "get_lakehouse_overview": tool_get_lakehouse_overview,
    "get_mcp_manifest": tool_get_mcp_manifest,
    # Phase 4 — Specs + Ledger · Group C
    "write_spec": tool_write_spec,
    "get_ledger": tool_get_ledger,
    # Phase 4 — Orchestration · Group D
    "orchestration_sweep": tool_orchestration_sweep,
    # Phase 5 — Usage · Group A
    "list_usage_providers": tool_list_usage_providers,
    "refresh_ollama_usage": tool_refresh_ollama_usage,
    "refresh_claude_usage": tool_refresh_claude_usage,
    "get_ollama_completion": tool_get_ollama_completion,
    "get_stats": tool_get_stats,
    # Phase 5 — Ops/Health + Session Meta · Group B
    "get_recent_errors": tool_get_recent_errors,
    "get_ops_status": tool_get_ops_status,
    "set_session_meta": tool_set_session_meta,
    # Parity fill — API endpoints that lacked an MCP tool
    "search": tool_search,
    "get_memory": tool_get_memory,
    "update_memory": tool_update_memory,
    "delete_memory": tool_delete_memory,
    "get_stale_deals": tool_get_stale_deals,
    "crm_decay": tool_crm_decay,
    # Parity fill 2 (2026-07 audit) — backend verbs that lacked an MCP tool
    "get_deal": tool_get_deal,
    "get_deal_fireflies": tool_get_deal_fireflies,
    "get_deal_fireflies_latest": tool_get_deal_fireflies_latest,
    "list_deal_children": tool_list_deal_children,
    "compute_funnel": tool_compute_funnel,
    "get_project": tool_get_project,
    "get_next_week_tasks": tool_get_next_week_tasks,
    "get_future_tasks": tool_get_future_tasks,
    "set_task_assignee": tool_set_task_assignee,
    "set_scheduled_week": tool_set_scheduled_week,
    "assign_task_project": tool_assign_task_project,
    "set_auto_cycle": tool_set_auto_cycle,
    "update_project": tool_update_project,
    "create_cycle": tool_create_cycle,
    "export_roadmap": tool_export_roadmap,
    # Personal Health — daily ritual timeline
    "get_health_today": tool_get_health_today,
    "get_health_routines": tool_get_health_routines,
    "check_health_routine": tool_check_health_routine,
    "uncheck_health_routine": tool_uncheck_health_routine,
    "get_health_config": tool_get_health_config,
    "get_health_plate": tool_get_health_plate,
    # Daily Reflection — morning/evening examen adaptado
    "get_personal_okrs": tool_get_personal_okrs,
    "get_reflection_today": tool_get_reflection_today,
    "save_reflection_morning": tool_save_reflection_morning,
    "save_cogload_label": tool_save_cogload_label,
    "get_cogload_status": tool_get_cogload_status,
    "save_reflection_evening": tool_save_reflection_evening,
    # Journey — the contextual layer. DEFAULT scope on purpose: all three are
    # reads (propose_deliver returns text + a link and writes nothing), and the
    # whole point of ADICIÓN 9 is that EVERY agent can ask where a client is.
    "get_journey_pulse": tool_get_journey_pulse,
    "get_project_hub": tool_get_project_hub,
    "propose_deliver": tool_propose_deliver,
}


# --- MCP protocol over stdio ---

def send_response(msg: dict):
    """Send a JSON-RPC response over stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle_request(req: dict) -> Optional[dict]:
    """Serve a single MCP JSON-RPC request."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "hermes-orchestrator",
                    "version": "2.0.0",
                    # Advertise the active least-authority scope (PRD Phase 4) so a
                    # client can see whether it holds orchestration authority.
                    "scope": ACTIVE_SCOPE,
                }
            }
        }

    if method == "initialized":
        return None  # notification, no response needed

    if method == "tools/list":
        # Least authority: a default-scope client is only SHOWN the tools it may
        # call. Privileged tools are hidden entirely unless the operator scope is
        # active. (Enforcement below is the real gate; this keeps the menu honest.)
        visible = [t for t in TOOLS if _scope_allows(t["name"])]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": visible}
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
            }

        # Defense in depth: re-check scope on every call, never trust the filtered
        # list alone. A default-scope client that names a privileged tool is denied.
        if not _scope_allows(tool_name):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": (f"Tool '{tool_name}' requires the 'privileged' "
                                f"scope; this connection is '{ACTIVE_SCOPE}'. The "
                                f"operator grants orchestration authority explicitly."),
                },
            }

        try:
            result = handler(tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result}]
                }
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"error": str(e)})}]
                }
            }

    # Unknown method
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"}
        }
    return None


def main():
    """Main loop: read JSON-RPC from stdin, write responses to stdout."""
    # Log to stderr so it doesn't interfere with stdio protocol
    _n = sum(1 for t in TOOLS if _scope_allows(t["name"]))
    print(f"Hermes Orchestrator MCP Server starting — scope={ACTIVE_SCOPE}, "
          f"{_n}/{len(TOOLS)} tools available", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(req)
        if response:
            send_response(response)


if __name__ == "__main__":
    main()
