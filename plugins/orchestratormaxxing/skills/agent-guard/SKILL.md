---
name: agent-guard
description: "Trigger: command guard, dangerous command hook, PreToolUse safety, guardrails, why was a command blocked, add a blocked pattern, wire the guard into a host. Operates bin/agent-guard — the shared denylist of catastrophic shell commands plus write-content security guidance — across Claude, Codex, OpenCode, Hermes, Warp and Zed."
---
<!-- Runbook adapted from davidondrej/skills@76724e4 skills/ops-and-setup/global-agent-guardrails (MIT);
     the wiring table, gotchas and probe are rewritten for orchestratormaxxing's bin/agent-guard. -->

# agent-guard runbook

One patterns file (`deploy/agent-guard/dangerous-patterns.txt`, POSIX ERE) is the single source of truth; every host reads it through `bin/agent-guard` or a thin native adapter. It is a seatbelt against accidents, NOT a sandbox against a malicious agent.

## State check

```bash
agent-guard status --gate   # 0 iff patterns compile and every detected host has the expected configuration
agent-guard selftest        # must end "failed: 0"
agent-guard check "rm -rf /" ; echo "exit=$?"   # expect 2 + reason
```

Configuration checks do not prove live host interception or Codex hook trust; test the actual host before relying on enforcement. Hermes post-write guidance is supplied by its separately bundled `security-guidance` plugin.

Red gate → read `agent-guard status --json` `hosts.<name>.state` (`wired` / `missing` / `absent`) and rewire the `missing` host from the table.

## Add or tune a pattern

1. Edit `deploy/agent-guard/dangerous-patterns.txt` — POSIX ERE, `[[:space:]]` never `\s` (adapters convert).
2. Add a `block`/`allow` case to `deploy/agent-guard/guard-cases.txt`.
3. `bash tests/agent-guard/run.sh` → then `./install.sh` (copies the data to `~/.config/orchestratormaxxing/agent-guard/` and re-applies Warp/Zed).
4. On each independently managed installation, update the public checkout and rerun its installer and guard checks.

Block only irreversible/catastrophic commands; `rm -rf node_modules`, `git clean -fdx` stay allowed.

## Per-host wiring (this harness)

| Host | Where | Event | Blocks via |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` (`agent-guard wire claude`) | `PreToolUse` Bash · `PostToolUse` Edit/Write | exit 2 · additionalContext |
| Codex | `plugins/orchestratormaxxing/hooks/hooks.json` + `agent-guard trust-codex --apply` | `PreToolUse` Bash · `PostToolUse` apply_patch | exit 2 · additionalContext |
| OpenCode | `~/.config/opencode/plugins/orchestratormaxxing-guard.js` | `tool.execute.before/after` | throws Error · appends guidance |
| Hermes | `~/.hermes/plugins/command-guard` + bundled `security-guidance`, both in `plugins.enabled` | `pre_tool_call` (`terminal`) | `{"action":"block"}` |
| Warp | `settings.toml` `command_denylist` (`agent-guard warp --apply`) | native | **confirm**, not block |
| Zed | `settings.json` `agent.tool_permissions…terminal.always_deny` (`agent-guard zed --apply`) | native | deny |
| Hermes orchestrator | none | — | inherits Claude/Codex hooks |

Warp and Zed are pre-guard only (no post scan).

## Gotchas

- **Codex trust is hash-pinned**: any edit to a hook ENTRY silently disables it until `agent-guard trust-codex --apply` (then `--check`).
- **Warp restores account settings at startup** — re-run `agent-guard warp --check` after launch.
- **False positive**: an ARGUMENT containing a dangerous-looking string (a prompt mentioning `git push --force`) is blocked → put the text in a file and reference it.
- Missing patterns file fails OPEN at call time but reds `status --gate` — silence ≠ blindness.

## Safe E2E probe

```bash
cd "$(mktemp -d)"
# then ask the agent (Claude, Codex, OpenCode, Hermes) to run exactly:  git push --force
```

Blocked = guard works. `not a git repository` = guard failed, harmlessly — rewire that host.
