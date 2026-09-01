# Connect a Claude Code fleet to Hermes (MCP)

> *"A shared brain, work queue, and human approval loop for your Claude Code fleet."*
> — PRD §8, the publishable wedge.

Any MCP client (Claude Code, Claude Desktop, Cursor) can connect to the Hermes
orchestrator and **orient itself, pull work, and report** — without being able to
freely restructure the plan. This is the *least-authority* model that makes the
MCP safe to hand to external agents.

---

## 1. Connect (least-authority, default)

From the orchestrator machine (or any tailnet machine with the repo):

```bash
./connect-fleet.sh
```

That runs, under the hood:

```bash
claude mcp add hermes-orchestrator -- python3 /path/to/orchestrator/mcp_server.py
```

Verify:

```bash
claude mcp list          # hermes-orchestrator should be listed
./connect-fleet.sh --show   # prints the live connection manifest (scopes + tools)
```

The **default scope** an external fleet gets can:

| Capability | Tools |
|---|---|
| **Orient** (read the plan) | `get_roadmap`, `list_initiatives`, `list_projects`, `list_epics`, `list_sprints`, `get_active_sprint`, `get_task`, `get_task_history`, `get_archive`, `list_tasks`, `get_activity`, `get_sessions`, `get_dashboard_url`, `get_spec_slice`, `list_specs` |
| **Pull** (take work) | `list_pool`, `claim_task`, `claim_next` |
| **Report** (push up) | `report_progress`, `report_result`, `report_blocked`, `escalate_discovery`, `comment_task`, `report_ledger` |
| **Declare** (bounded) | `create_task`, `set_session_role` |

It **cannot**: dispatch other agents, edit the roadmap/sprints/projects, or change
trust grades. Those are load-bearing and stay operator-only.

The canonical, always-current list is the manifest endpoint:

```bash
curl -s http://127.0.0.1:5555/api/mcp/manifest | jq
```

The manifest derives its tool lists directly from `mcp_server.py`, so it can never
drift from what the server actually enforces.

---

## 2. The agent-side loop (how a fleet works a task)

```
list_pool ──▶ claim_task (Pool→Working, returns the ACCEPTANCE CONTRACT)
   │
   ├─ report_progress(note, pct)   ← call as you work; shows live on the Fleet board
   │
   └─ report_result(passed=…)  ─┬─ met the contract + high trust + auto  ─▶ auto-accept ▶ Done
      report_blocked(reason)  ──┤  everything else                       ─▶ operator Inbox
      escalate_discovery(…)   ──┘  found new work / a risk               ─▶ operator Inbox (new task)
```

- **Always** `report_blocked` instead of silently dropping a task.
- **Never** claim `passed=true` unless you actually met the acceptance contract
  you got back from `claim_task`.
- Work only to the contract in the claim response — keep your context lean with
  `get_spec_slice(feature, role)` rather than pulling whole specs.

---

## 3. Privileged (operator) scope

Orchestration — `dispatch_to_agent`, `set_pool`, `set_autonomy`,
`change_trust_grade`, `create_sprint`, `close_sprint`, `create_project`,
`edit_roadmap`, `update_task_status`, `assign_task` — is a **separate scope the
operator grants explicitly**. It is not exposed to external fleets.

```bash
# operator only:
export HERMES_MCP_TOKEN="$(cat ~/.config/orchestratormaxxing/mcp-privileged-token)"
./connect-fleet.sh --privileged
```

Enforcement (`mcp_server.py`):

- Privileged scope requires **both** `HERMES_MCP_SCOPE=privileged` **and** a
  `HERMES_MCP_TOKEN` that matches the configured token
  (`~/.config/orchestratormaxxing/mcp-privileged-token` or
  `HERMES_MCP_PRIVILEGED_TOKEN`). Mismatch → **falls back to default** (fail-safe).
- Privileged tools are **hidden** from a default client's `tools/list` **and**
  re-checked on every `tools/call` (defence in depth) — a default client that
  names a privileged tool is denied with an explicit error.
- The trust dial is operator-only: **an agent can never raise its own trust
  grade** (kills the self-preference failure mode).

To lock down privileged scope before exposing beyond localhost:

```bash
mkdir -p ~/.config/orchestratormaxxing
openssl rand -hex 24 > ~/.config/orchestratormaxxing/mcp-privileged-token
chmod 600 ~/.config/orchestratormaxxing/mcp-privileged-token
```

If no token is configured, requesting privileged scope is granted **with a stderr
warning** (local-operator convenience) — set a token before any non-localhost use.

---

## 4. Transport & safety

- **Transport:** stdio — the MCP client spawns `mcp_server.py` as a subprocess.
  The dashboard it reads/writes is **tailnet-only** (`tailscale serve`, TLS +
  tailnet ACLs), never public. A public Funnel endpoint is an explicit,
  opt-in decision at publish time.
- **Everything is audited:** every agent action writes a `task_event`
  (status_changed / result / escalation), visible in the task's history and the
  Fleet board — nothing an agent does is invisible.
- **No load-bearing auto-commit:** agents propose and report; they never mutate
  plan structure unattended. Mirrors `/self-improve`'s "propose, human signs off."

---

## 5. Quick reference

```bash
./connect-fleet.sh            # add, default (least-authority) scope
./connect-fleet.sh --show     # print the live manifest, make no changes
./connect-fleet.sh --print    # print the `claude mcp add` command only
./connect-fleet.sh --name x   # custom MCP server name
./connect-fleet.sh --privileged   # operator only (needs a matching token)
```
