# OpenCode as a first-class harness citizen — 2026-08-11

> **Superseded policy note — 2026-08-20:** Kimi K3 is now available under
> included-first Pro/Max usage, with published overage rates after included
> limits. The active harness uses K3 for `kimi-coder`, exposes `/kimiplan`, and
> no longer excludes K3 from `oll-sync`. The dated evidence below is preserved
> as the original 2026-08-11 decision record.

**Adopt/extend/build decision (plugin-first gate):** ADOPT OpenCode's native surfaces —
markdown agents (`~/.config/opencode/agents/`), markdown commands (`~/.config/opencode/commands/`),
JS plugins (`~/.config/opencode/plugins/`, `session.idle` hook), `opencode run`/`--prompt`/`--agent`
CLI — and EXTEND them with orchestratormaxxing policy (doctrine prompts, Hermes bridge, done-notify,
provider-ask delegation). BUILD only what upstream cannot know: the `o`/`o-ubuntu` tmux launcher
and the fleet/bridge edits. No custom control plane, no OpenCode fork, no session driver.

## Goal
`opencode` + `oll` reach the same integration level as Claude (`c`) and Codex (`g`) in the
orchestratormaxxing environment: launcher + fleet, Hermes visibility, lifecycle notifications,
host-native planning and deep research, and cross-host delegation.

## Load-bearing constraints (verified 2026-08-11)
1. **Anthropic prohibits Claude Pro/Max OAuth inside OpenCode** (opencode.ai/docs/providers;
   OpenCode removed those plugins in 1.3.0). Delegation to Opus/Sonnet therefore shells out to
   the Claude Code CLI (`claude -p`, subscription auth) via `provider-ask anthropic` — never an
   in-OpenCode `anthropic` provider. Codex delegation stays on `codex exec` (`provider-ask openai`).
2. **kimi-k3 is the only extra-usage-only cloud model** ("requires a Pro or Max subscription, and
   consumes extra usage credits" — ollama.com/library/kimi-k3). Every other cloud model is
   included in Pro at a usage *level* (Light→Extra-High burn rate, levels 1–4). `oll-sync`
   excludes kimi-k3 so the TUI can never burn extra credits by accident.
3. **Top included models for agentic coding** (ranked): glm-5.2 (~1M ctx, project-level
   engineering), kimi-k2.7-code (256K, the only coding-specialized model), minimax-m3 (512K–1M,
   agentic+multimodal). deepseek-v4-flash:0731 is the newest/cheapest frontier alternate (Medium
   usage, 1M ctx); deepseek-v4-pro is included but Extra-High usage → reserved for low-volume
   reasoning (the oplanner), never routine fanout.
4. **opencode 1.17.x startup-hang class (lq-6e4c38c5)** — every one-shot goes through `occ`
   (bounded timeout + retry + orphan reap), including the `o` helper's `--prompt`/`--headless`.

## The pieces
- `shell/opencode-o.sh` — `o`/`o-ubuntu`, sibling of `c`/`g`: `opencode-<name>[-role][-N]` tmux
  sessions, exact `=` targets, auto-number, `o ls`, `--detach`, `--headless --prompt` (via occ),
  `--agent` passthrough, dashboard role/feature registration, Warp recovery registration.
- `bin/gpu-agent` mode `o` + `bin/harness-remote` `o)` case (+ `ls` covers all three hosts).
  Folder aliasing reuses the hardened no-tools Claude picker (opencode has no no-tools mode and
  the Mac may not have opencode installed).
- `bin/warp-agent-recovery` — `opencode` joins the AGENTS set; wrapper map → `opencode-o.sh`/`o`.
- OpenCode assets (repo `opencode/`, deployed by install.sh):
  - agents: `oplanner.md` (deepseek-v4-pro, read-only — the host-native planner; Claude has
    /fableplan, Codex has solplan, OpenCode has /oplan), `deep-researcher.md` (minimax-m3,
    webfetch allowed, edit/bash denied).
  - commands: `/oplan`, `/research`, `/opus`, `/sonnet`, `/codex` (delegation via provider-ask).
  - plugins: `orchestratormaxxing-notify.js` — `session.idle` → `agent-done-notify --agent opencode`
    (same Telegram done-notification path as Claude/Codex Stop hooks).
- Third primary coding agent `minimax-coder` (ollama-cloud/minimax-m3) beside kimi-coder/glm-coder
  in the install.sh JSON block.
- `bin/provider-ask` gains the `anthropic` backend (`claude -p --model opus|sonnet`); multi-council
  naturally widens.
- `bin/oll-sync`: kimi-k3 exclusion + fresh ctx defaults; run prunes retired kimi-k2.5/minimax-m2.5.
- Hermes: `mcp_server.py` terminals accept `opencode-*` (agent: "opencode"); `bin/task-plan`
  planner `oplan` → detached `o --agent oplanner --prompt <brief>` session. Dashboard
  `sessions.py` already classifies opencode sessions.
- Gates: `harness-verify` asserts the new deploys + an opencode-o behavioral contract;
  `tests/ubuntu-launchers` and `tests/task-plan` extended.

## Status
Implementation in progress this session; results + verification evidence recorded in
docs/changelog.md at wrap-up.
