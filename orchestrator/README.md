# Hermes Orchestrator

Turn Hermes into the central orchestrator of your one-person dev company.

## What This Is

- **Kanban Dashboard** — web UI for Hermes kanban, accessible over Tailscale
- **Agent Session Manager** — launch/monitor/stop Claude Code sessions via tmux
- **Orchestrator Skill** — teaches Hermes how to dispatch tasks to agents
- **Daily Standup** — cron job that sends a morning summary to Telegram

## Quick Start

### Start the dashboard
```bash
# Already running as a systemd service. To restart:
systemctl --user restart hermes-dashboard

# Or manually:
cd ~/dev/orchestratormaxxing/orchestrator
source .venv/bin/activate
python -m dashboard.api
```

### Access the dashboard
- From any tailnet device: `https://<orchestrator-host>.<your-tailnet>.ts.net:5555`
- On the Linux box itself: `http://127.0.0.1:3000`
- ⚠️ Intranet only — bound to loopback, exposed to the tailnet via
  `tailscale serve` (`:5555` → `127.0.0.1:3000`, TLS + tailnet ACLs), never public

### Manage agent sessions
```bash
agent-session start <task-id> <project-dir> [--interactive] [--prompt "task"]
agent-session status
agent-session logs <task-id>
agent-session send <task-id> "follow-up"
agent-session stop <task-id>
```

### SSH from Mac to Linux
```bash
ssh <user>@<orchestrator-host>
# Then: tmux attach -t task-xxx
```

## Architecture

```
Operator (Telegram / Dashboard)
  │
  ▼
Hermes (Orchestrator, Linux GPU box, <tailnet-ip>)
  ├── Kanban Dashboard (loopback :5555 → tailscale serve, tailnet only)
  ├── Claude Code (via tmux or print mode)
  ├── OpenCode (via opencode run --agent, on Mac)
  ├── Hermes Subagents (via delegate_task)
  └── Mac (<tailnet-ip>, control surface via Tailscale SSH)
```

## Files

| Path | Purpose |
|------|---------|
| `dashboard/api.py` | FastAPI backend |
| `dashboard/db.py` | SQLite read layer (Hermes kanban DB) |
| `dashboard/agent_status.py` | Agent activity monitor |
| `dashboard/templates/` | Jinja2 HTML templates |
| `bin/agent-session.sh` | Claude Code tmux session manager |
| `deploy/hermes-dashboard.service` | systemd user service |
| `.venv/` | Python virtual environment |

## Tailscale Network

| Machine | IP | MagicDNS | Role |
|---------|----|----------|------|
| <orchestrator-host> | <tailnet-ip> | <orchestrator-host> | Orchestrator + agent host |
| <control-host> | <tailnet-ip> | <control-host> | Control surface |

## Pending Setup (Requires the operator)

1. **Tailscale SSH on Linux**: `sudo tailscale set --ssh` (needs sudo password)
2. **Mac dashboard access**: Open `https://<orchestrator-host>.<your-tailnet>.ts.net:5555` in Safari on Mac
3. **Git push**: Commit and push orchestrator code to sync with Mac

## Hermes Skill

The `hermes-orchestrator` skill is installed at:
`~/.hermes/skills/productivity/hermes-orchestrator/SKILL.md`

Load it in any session: `/skill hermes-orchestrator`
