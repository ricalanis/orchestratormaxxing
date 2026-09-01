# Tool inventory — the doc-inventory policy (lq-1f8b2552)

Policy, decided 2026-08-14: **CLAUDE.md's "Tools in this repo" and README's table
are explicitly curated, not exhaustive.** They carry the tools an orchestrator
session must know about to route work. This file is the exhaustive complement:
every `bin/` executable *deliberately left out* of the curated surfaces gets one
line here, so nothing ships undocumented.

`bin/harness-verify` enforces the pair deterministically:
- a `bin/` executable absent from **both** CLAUDE.md and this manifest is
  undocumented drift → **error**;
- a line here naming a tool that no longer ships is manifest rot → **error**;
- a tool present in both graduated into CLAUDE.md without leaving this file →
  **warn** (delete its line here).

Entry format is load-bearing (the verifier parses it): one bullet per tool,
`` - `name` — description ``.

Uncurated tools (support/glue — invoked by hooks, launchers, or other tools,
not routed directly by an orchestrator session):

- `agent-tab-status` — sets the Warp-visible tmux window title for Claude/Codex lifecycle state; called by hooks with the current `TMUX_PANE`.
- `browser-mcp-contract` — one-command deterministic verify of the browser MCP stack (install checks + live CDP phase).
- `chrome-debug` — ONE entry point for the account-integrated CDP Chrome (port 18800); shared human+agent profile.
- `codex-stop-hook` — fail-open Codex Stop hook emitting exactly one JSON object on stdout.
- `opencode-browser-mcp` — MCP stdio server exposing Chrome CDP actions (navigate/click/type/extract/screenshot/eval) to OpenCode agents.
- `orchestration-practice` — deterministic matcher and preflight receipt generator for the shared Orchestra-of-One practice catalog.
- `sync-agent-skills` — installs the pinned external skill stack into Claude, Codex, OpenCode, and Hermes.
- `tmux-guard` — PATH shim that forwards normal tmux commands and refuses server-wide `kill-server` so one diagnostic cannot terminate every shared agent session.
- `warp-agent-event` — emits one privacy-minimal Warp CLI-agent lifecycle event through tmux.
- `capacity` — reads the fleet's live session/agent capacity signals for the dashboard and hooks; fleet identity arrives via fleet.env (ORCHESTRATORMAXXING_SERVER_SSH), standalone machines degrade to local-only.
- `memory-bridge-hermes.sh` — SessionStart glue that loads the shared governed memory brief into Hermes-launched sessions; a thin wrapper over memoryctl.
- `model-catalog` — reads the usage class, real context window and modalities the provider publishes for every live Ollama Cloud model into `knowledge/model-catalog.json`, so selection stops running on prose. An unknown fact stays UNKNOWN — the family-prefix guess it replaced had 13 of 20 windows wrong, including the default worker at a fifth of its real size.
- `model-bench` — compares candidate workers on stability, intelligence and speed with `reasoning_effort` pinned, because an unpinned run measures the token cap rather than the model. Reports decode SPREAD beside the median (a model varying 7x is not 'fast on median', it is unschedulable) and records a budget-exhausted empty answer as its own outcome, distinct from a wrong one.
- `model-eval` — pre-registered, paired model-routing eval: the spec is hashed before the first call and there is no exclusion flag, hand-written golden answers must clear the frozen contracts or the run aborts before spending a call, N *distinct* instances replace N reps, and Clopper-Pearson CIs plus exact McNemar decide the verdict — with overlapping CIs returning KEEP INCUMBENT, because a larger point estimate is not a win.

