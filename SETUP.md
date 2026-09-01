# Set up the orchestrator harness on a new machine

## Topology

This guide describes a **standalone earth** — a human's laptop client with no sun. In the orchestratormaxxing topology, the **sun** is the fleet server (a Linux box) that runs the Hermes dashboard and fleet services; **moons** are containers orbiting the sun (Semantica, Open Design, Firecrawl…). A standalone earth has no `fleet.env`, so hooks stay silent and nothing reaches out. Fleet mode is opt-in: create `fleet.env` to connect an earth to its sun over a private network.

Public project: https://github.com/ricalanis/orchestratormaxxing. A standalone client needs one
Ollama Cloud key and nothing else. Fleet features (server relay, dashboard, notifications) stay
off until you create one file — see **Fleet mode** at the end.

## 1. Prerequisites

macOS with Homebrew is the scripted path: `bootstrap.sh` installs whatever is missing through
`brew` (git, python3, jq, node) plus bun, OpenCode and Codex. You install the frontier hosts.

| Need | Why | Install |
|---|---|---|
| Homebrew | package source for everything below | https://brew.sh |
| git, python3 ≥ 3.11 | the tools in `bin/` are stdlib Python + bash | `brew install git python` |
| node (npm) | Codex CLI | `brew install node` |
| tmux | the `c` / `g` / `o` session helpers | `brew install tmux` |
| Claude Code | frontier host #1 | `curl -fsSL https://claude.com/install.sh \| bash` |
| Codex CLI | frontier host #2 | `npm install -g @openai/codex` (bootstrap does this if missing) |

Linux: `install.sh` is OS-aware (systemd `--user` units instead of launchd) and
`bin/harness-sync setup` works on either OS, but `bootstrap.sh` assumes Homebrew. Install the
prerequisites with your package manager, put the Ollama key in OpenCode's auth store yourself
(`~/.local/share/opencode/auth.json`, provider `ollama-cloud`), then run `./install.sh`.

## 2. Clone

```bash
git clone https://github.com/ricalanis/orchestratormaxxing.git ~/Dev/orchestratormaxxing
cd ~/Dev/orchestratormaxxing      # any path works — the checkout is the source of truth
```

## 3. Keys

- `OLLAMA_API_KEY` — **required**. https://ollama.com → Settings → Keys. Every worker lane
  (`oll`, `oll-council`, the OpenCode agents, Zed, Warp) runs on Ollama Cloud.
- `XAI_API_KEY` — optional. https://console.x.ai. Enables `xsearch` (web + X search). When it
  is unset, bootstrap skips the repo `.env` and the xsearch verification line.

```bash
export OLLAMA_API_KEY=...   # required
export XAI_API_KEY=...      # optional
```

## 4. The two commands

```bash
./bootstrap.sh   # prereqs · bun · OpenCode · Codex · gstack · Ollama Cloud provider · model sync
./install.sh     # deploy the harness globally — idempotent, re-run after every git pull
```

`ORCHESTRATORMAXXING_SKIP_GSTACK=1 ./bootstrap.sh` skips the gstack clone/setup (the default installs
it). Existing Codex auth/config/history are always preserved. `bootstrap.sh` already calls
`install.sh` once at the end; running it again is harmless.

## 5. What gets installed, per host

| Host | What `install.sh` puts there |
|---|---|
| Claude Code | hooks (SessionStart / Stop / Notification) in `~/.claude/settings.json`; agents + commands in `~/.claude/{agents,commands}`; doctrine pointer in `~/.claude/CLAUDE.md` |
| Codex | the `orchestratormaxxing` plugin (skills + hooks) registered from the repo marketplace; agents in `~/.codex/agents`; doctrine pointer in `~/.codex/AGENTS.md` |
| OpenCode | coding agents (`kimi-coder`, `glm-coder`, …), the `ollama-cloud` provider, commands and plugins in `~/.config/opencode` |
| Zed | `zed-setup` merges the Ollama Cloud `api_url` + default model into `~/.config/zed/settings.json` (only when Zed is present); one-time key paste: `zed-setup --key-hint` |
| Warp | `warp-model-pin` pins the agent profile to the harness default model in Warp's `settings.toml`; `warp-ollama` prints the paste-ready inference-endpoint config (Base URL `https://ollama.com/v1`) |
| tmux | `c` (Claude), `g` (Codex), `o` (OpenCode) session helpers sourced from `~/.config/orchestratormaxxing/*.sh`, plus a `tmux.conf` snippet included from `~/.tmux.conf` |
| PATH | bridges in `~/.local/bin`: `oll`, `oll-council`, `provider-ask`, `harness-verify`, `mut`, `memoryctl`, `session-log`, … |

Everything is a copy, never a symlink: edit in the repo, re-run `./install.sh`.

## 6. Verify

```bash
harness-verify            # deterministic self-check of the installed harness (exit 0)
oll "say hi"              # one worker call on Ollama Cloud (default deepseek-v4-flash:0731)
oll "say hi" --reasoning  # same call on glm-5.3
zed-setup --check         # exit 0 when Zed is configured (or absent)
warp-model-pin --check    # optional; refuses until the endpoint model was selected once in Warp
xsearch "test" --days 3   # only with XAI_API_KEY
```

Then open a new session: Claude Code shows `/fanout`, `/ideas`, `/fableplan`, `/self-improve`;
Codex shows `$orchestratormaxxing:fanout`, `$orchestratormaxxing:ideas`, `$orchestratormaxxing:memory`;
`c`, `g`, `o` each create a tmux session and `c ls` / `g ls` / `o ls` list them;
`memoryctl path` resolves to `<repo>/.agents/memory` and `mem-audit --json` is green.

## Fleet mode (optional)

A fresh client is standalone: no server, no dashboard, no notifications, and nothing reaches out
over the network for them. Fleet features activate **only** when
`~/.config/orchestratormaxxing/fleet.env` exists (`KEY=VALUE` per line, `#` comments allowed, no shell
expansion; an explicit environment variable of the same name wins over the file):

```bash
# ~/.config/orchestratormaxxing/fleet.env — placeholders; every key is optional
ORCHESTRATORMAXXING_SERVER_SSH=you@fleet-server                      # SSH target of the fleet server
ORCHESTRATORMAXXING_SERVER_HOSTNAME=fleet-server                     # its short hostname (sessions there read "local")
ORCHESTRATORMAXXING_DASHBOARD_URL=https://fleet-server.example:5555  # orchestrator dashboard base URL
ORCHESTRATORMAXXING_NOTIFY_TARGET=telegram:<chat>:<thread>           # `hermes send --to` target for done / needs-input
ORCHESTRATORMAXXING_NOTIFY_RELAY=you@fleet-server                    # `tailscale ssh <relay>` when hermes is not local
ORCHESTRATORMAXXING_LAN_PEER=fleet-server.example                    # peer for `harness-sync lan-check`
```

Without the file: the Stop/Notification notifier exits 0 silently, `c` / `g` / `o` skip the
dashboard registration, and `harness-sync lan-check` reports that `ORCHESTRATORMAXXING_LAN_PEER` is not
configured. Provisioning the fleet machines themselves is done by `install-fleet.sh` (private).
