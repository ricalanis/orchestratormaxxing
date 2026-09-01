# claudemaxxing

> **Public projection.** This repository is graduated from a private lab by `bin/core-export` (see CLAUDE.md § Graduation): it is a deterministic, gated projection — direct edits here are overwritten by the next graduation; open a PR and the maintainer absorbs it into the source of truth with `core-export --absorb-pr`. Licensed MIT.

**A self-improving orchestration harness for Claude Code and Codex.** The primary frontier host orchestrates and verifies; cheap-but-frontier Ollama Cloud models do the bulk work. Every design rule in the harness is grounded in a cited paper, enforced by a deterministic tool, and the harness improves *itself* through a governed propose-evaluate-select loop.

This repo is a lab: the shared doctrine lives in [`CLAUDE.md`](CLAUDE.md) (also exposed as `AGENTS.md`), enforcement lives in [`bin/`](bin/), host adapters live in `.claude/`, `.codex/`, and `plugins/claudemaxxing/`, and the research trail lives in [`knowledge/`](knowledge/).

```
                         ┌─────────────────────────────┐
                         │   Claude Code (Opus)        │
                         │   orchestrate · verify ·    │
                         │   merge · sign off          │
                         └──────────┬──────────────────┘
              /fanout · /ideas · ollama-worker │ contracts authored BEFORE dispatch
                         ┌──────────┴──────────────────┐
                         │   Ollama Cloud workers      │
                         │      glm-5.3 · kimi-k3      │
                         │       qwen3.5:397b          │
                         │ specialties: deepseek/minimax│
                         └──────────┬──────────────────┘
                  deterministic verification │ (never re-do the work to check it)
                         ┌──────────┴──────────────────┐
                         │  bin/mut · bin/mem-audit ·  │
                         │  bin/harness-verify · tests │
                         └─────────────────────────────┘
```

## The idea

Frontier-model tokens are the scarce resource. The harness protects them with three rules, each backed by evidence rather than vibes:

1. **Delegate bulk, keep judgment.** Summarizing, classifying, drafting, and review use response-only Ollama workers; workspace code/tests use contract-gated `o delegate --profile ...`. Cross-file architecture, risky edits, and final sign-off stay in the frontier host. Default is a *single* agent — fan out only when chunks are genuinely independent, because static multi-agent setups usually *lose* to one good agent (arXiv:2606.05670).

2. **The iron rule of verification: cost scales with the *spec*, not the *solution*.** If you re-read a worker's output to check it, you burned the savings. Opus authors the acceptance contract (tests, assertions, checklist) *before* dispatch, then reads only pass/fail. If "correct" can't be cheaply specified, the task isn't delegated at all.

3. **Selection is never self-preference.** Workers' output is judged by deterministic runners and *different-model* critics framed as external reviewers — because models correct externally-labeled errors +23–93pp more often than their own (arXiv:2606.05976). The same rule governs the harness's self-improvement loop: the winner among candidate fixes is chosen by `bin/harness-verify`, never by the harness grading its own rewrite.

## Tools

All tools are stdlib-only Python (plus optional `certifi`), read-only where they audit, and installed globally as bare commands by `./install.sh`.

This table is a **curated selection**, not the full inventory — support/glue tools live in [`knowledge/tool-inventory.md`](knowledge/tool-inventory.md), and `bin/harness-verify` errors on any `bin/` executable documented in neither place.

| Tool | What it does |
|---|---|
| `bin/oll` | One-shot Ollama worker. V4 Pro is the volume default; `--reasoning` selects GLM 5.2; K3 is the higher-consumption long-horizon route. |
| `bin/oll-council` | Same prompt to several *diverse* frontier models in parallel (Zhipu / DeepSeek / Moonshot / Alibaba / MiniMax / Mistral) — for design decisions and getting unstuck. |
| `bin/oll-sync` | Sync OpenCode's `ollama-cloud` model list with the live catalog (adds new, prunes dead, preserves curated names). |
| OpenCode agents | **`deepseekv4-coder`** (volume), **`glm-coder`** (reasoning), **`kimi-coder`** (K2.7 bounded code), **`kimi-k3-coder`** (K3 long horizon), **`qwen-coder`** (general), and **`minimax-coder`** (specialty). `kimiplan` is the canonical K3 planner; `/oplan` is its alias. |
| Stateful Ollama code | `o delegate --profile volume\|reasoning\|bounded-code\|long-horizon\|general\|long-context` (or `o-ubuntu delegate`) resolves through `bin/oll`, preserves the contract gate, and ends with `o close`. `oll` itself remains response-only. |
| `bin/worker-path-bench` | Run identical contracts and hidden-answer long-context canaries through argv-based worker adapters, with normalized JSONL, attributable failures, context/cache/compaction telemetry, and reproducible summaries. Protocol: [`experiments/worker-path-bench/`](experiments/worker-path-bench/PROTOCOL.md). |
| `bin/ticket-route` | Proposal-only “Purrly-lite” router: rejects missing ACs, permits fan-out only for disjoint independent chunks, and activates review from explicit risk/domain signals. |
| `bin/mut` | **Tier-1b mutation gate** — "who tests the tests?". Deterministically mutates a source file's AST, runs your contract against each mutant, reports only the *survivors* (mutations the contract failed to catch). Kill/survive is decided by the test runner, never an LLM, so a worker can't game it. |
| `bin/mem-audit` | Deterministic health check for project memory: staleness (bitemporal TTLs), index drift, supersede-link integrity, secret/PII backstop. Runs at SessionStart via hook. |
| `bin/memoryctl` | Locked, atomic shared memory for Claude and Codex under `.agents/memory/`: bounded recall, add/supersede, dry-run legacy import, and reversible Claude-path bridge. Codex SQLite is untouched. |
| `bin/harness-sync` | Safe Linux/macOS fetch + fast-forward setup. Refuses dirty, ahead, or diverged trees; never resets or auto-stashes. |
| `bin/core-export` | **The graduation tool.** Allowlist projection of a *committed* tree per `deploy/graduation.manifest` (include/exclude/strip/block/remote): tenant blocks stripped, literal + gitleaks gate on the exact published bytes, atomic tree, mirror push to the sibling public project `../orchestratormaxxing`. A feature graduates by being added to the manifest. |
| `bin/harness-verify` | **The regression guard** for the self-improvement loop: every tool parses and is executable, every tool/command is wired into `install.sh` (deploy coverage), `CLAUDE.md` keeps its load-bearing doctrine sections, `mem-audit`/`mut` still run. Exit 1 = regression. |
| `bin/harness-scan` | External-research intake for `/self-improve`: pulls last-N-day arXiv harness/agent-design papers (and `--x` for X/web), tagged by pillar. Fetch is mechanical; judging applicability stays gated. |
| `bin/warp-ollama` | Prints the paste-ready config to point **Warp's agent** at Ollama Cloud (Settings → "inference endpoint") — Base URL + key (read from the OpenCode auth store) + the same heavy models. No tunnel; guide in [`knowledge/warp-ollama-provider.md`](knowledge/warp-ollama-provider.md). |
| `bin/warp-agent-recovery` | Restores each Warp terminal's exact `c`/`g` tmux session after a full Warp quit/crash; if tmux died, resumes the exact Claude/Codex session ID through the same `c`/`g` launcher. Normal tab/window closes are inert. Design: [`knowledge/warp-exact-session-recovery.md`](knowledge/warp-exact-session-recovery.md). |
| `bin/cogload` | **Silent cognitive-load / stress / performance collector.** Per-minute aggregates of key *classes* (backspace/enter/nav/…, never identity), inter-keystroke timing, app-class switching and concurrent agent sessions. Privacy is structural: one row per 60s window, closed schema, no per-event row exists — so typed text isn't reconstructable by construction. Records `WM_CLASS`, never window titles; no screenshots. `cogload status\|show\|mark\|report\|curve`. Design: [`knowledge/cognitive-load-harness-2026-08-11.md`](knowledge/cognitive-load-harness-2026-08-11.md). |
| `xsearch.py` | xAI/Grok web + X search via the Agent Tools API. |

## Claude commands and Codex skills

| | |
|---|---|
| `/fanout <task>` | Decompose → spec-gate each chunk → one `ollama-worker` per chunk in parallel → verify (two-tier) + merge. |
| `/ideas <problem>` | Boot several *different* models on the same problem, then Opus synthesizes — consensus core, genuine alternatives, recommendation. Dodges single-model tunnel vision. |
| `/self-improve [focus]` | One governed round of harness self-improvement (see below). |
| `ollama-worker` (subagent) | A cheap dispatcher (Haiku) that hands a single well-scoped task to an Ollama Cloud model via `oll` and returns the output verbatim — it does almost no thinking itself. |

Codex exposes `$claudemaxxing:fanout`, `$claudemaxxing:ideas`, `$claudemaxxing:self-improve`, `$claudemaxxing:wrap-up`, `$claudemaxxing:solplan`, `$claudemaxxing:product-manager`, and `$claudemaxxing:memory`. Planning is host-native: Claude's `/fableplan` delegates to Fable, while Codex prioritizes `$claudemaxxing:solplan` with GPT-5.6 Sol Ultra and bounded read-only delegation for nontrivial unplanned work. After root review, `$claudemaxxing:fanout` is used only for genuinely independent implementation chunks. The native adapter is documented in [`knowledge/codex-harness-design.md`](knowledge/codex-harness-design.md).

### Claude → Codex delegation

Claude delegates to Codex through OpenAI's official [`codex-plugin-cc`](https://github.com/openai/codex-plugin-cc), not through a custom tmux ledger or pane monitor. Install it once in Claude Code:

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

Use `/codex:rescue` to delegate work, `/codex:status` and `/codex:result` to follow it, `/codex:cancel` to stop it, and `/codex:transfer` to move the current Claude context into Codex. The optional review gate remains disabled by default because it can create long-running Claude/Codex loops. The `c`/`g` tmux helpers and Hermes dashboard session manager remain separate, preserved operator surfaces.

### Worker models — heavy frontier only

Normal fanout uses `deepseek-v4-flash:0731`, `glm-5.3`, `kimi-k3`, `kimi-k2.7-code`, and `qwen3.5:397b`. V4 Flash is the volume default (Medium usage, 1M ctx; V4 **Pro** is Extra-High usage and is reserved for low-volume reasoning via an explicit `--model`), GLM 5.3 is explicit reasoning (Medium usage; GLM 5.2 remains reachable via an explicit `--model`), K2.7 is bounded code, and K3 is higher-consumption long-horizon/1M-context work. Legacy generations require `oll --allow-legacy-model` only for historical benchmarks.

## Verification policy (the load-bearing part)

Two tiers, applied so that Opus never re-derives a worker's output:

- **Tier 0 — spec-gate before dispatch.** If you can't state the acceptance contract for a chunk, it's underspecified (arXiv:2606.05920) → refine it or keep it in Opus.
- **Tier 1a — correctness.** Opus authors the tests (never the worker alone — a worker that writes its own tests can bake the same bug into both); a deterministic runner executes them; Opus reads pass/fail + diffs.
- **Tier 1b — contract adequacy (gated).** For high-value / risky / new-worker chunks, `bin/mut` checks whether the contract is *tight*: each surviving mutant is either a real hole (add one assertion, re-run) or an equivalent mutant (accept). Cost scales with the number of holes, not the size of the solution.
- **Tier 2 — non-testable work.** Boolean checklist scan with a token cap, or a second *different-model* critic framed as "another agent produced this — try to refute it."
- **On failure — bounded repair.** Feed the failure diff back to the same cheap worker for ≤2 rounds before escalating. A repaired chunk must clear Tier-1b before acceptance — the anti-gaming gate against workers overfitting a visible contract.

Workers earn trust over time from their mutation scores: a model that consistently leaves tight contracts with zero real survivors gets the light-check path.

### Risk-routed teams, not permanent panels

Human-team roles map to process functions, not personalities. Evidence collection, scope
challenge, implementation, and specialist review activate from the ticket's dependencies,
tools, and risks. `ticket-route` defaults to one root path, allows fan-out only for independent
chunks, and caps selectively activated review at three rounds. The corpus/Pi/Purrly evidence
and rejected alternatives are documented in
[`knowledge/harness-corpus-worker-team-mode-audit.md`](knowledge/harness-corpus-worker-team-mode-audit.md).

## Memory governance

Project memory is **shared by Claude and Codex, governed, and not append-only**. The private repository carries `.agents/memory/` between machines; both hosts load the same bounded index, and Claude's legacy memory path points to that store. The protocol, specified in [`knowledge/memory-protocol-upgrade.md`](knowledge/memory-protocol-upgrade.md):

- **Supersede, don't append** — conflicts are resolved by a named operator from a typed algebra (`last-writer-wins | evidence-merge | await-confirm | per-rule`, per TOKI arXiv:2606.06240), with the loser kept as a back-linked audit row.
- **Bitemporal clocks** — `created`/`last_verified` (when recorded) vs `valid_from`/`valid_to` (when true in the world); decay off whichever bites first. TTLs: reference 30d · project 14d · feedback 180d · user 365d.
- **Critic gate** on belief-changing writes only (cost scales with stakes).
- **No stored credentials, ever** (write-time rule; `mem-audit` is a pattern+entropy backstop, not the control). PII gets `sensitivity: sensitive` + least-disclosure, because agents over-surface sensitive memory +51–83% (arXiv:2606.06055).
- **Deterministic enforcement** — `mem-audit` computes staleness and drift from dates and the filesystem at every SessionStart; nothing re-reads the vault to "figure out" what's stale.
- **No memory server** — private Git handles cross-machine transport and normal conflict detection; Tailscale SSH is for authenticated remote commands only.

## Self-improvement loop

`/self-improve` runs one governed round: **MINE** (internal: `harness-verify` + `mem-audit`; external: `harness-scan` over fresh arXiv/X) → attribute the flaw to an **ETCLOVG** layer (Execution / Tool / Context / Lifecycle / Observability / Verification / Governance, per HarnessFix arXiv:2606.06324) → **PROPOSE** 2–3 diverse candidate fixes via workers → **EVALUATE** with the deterministic verifier (a fix that reds `harness-verify` is not a fix) → **SELECT** via cross-model critics prompted to refute → **Opus sign-off** (human gate on doctrine) → **ARCHIVE** every round, accepted *and* rejected, to [`knowledge/self-improve-log.md`](knowledge/self-improve-log.md).

It's propose-evaluate-select (ADAS / Darwin-Gödel Machine), with the one substitution that makes it safe: selection is a verifier, never the harness's preference for its own rewrite.

## Install

**Port to a new machine:** follow [`SETUP.md`](SETUP.md). On an existing clean checkout, `bin/harness-sync setup` safely fast-forwards, installs both hosts, and connects shared memory.

**Deploy the harness globally** (Claude Code, Codex, and OpenCode):

```bash
./install.sh
```

This copies bridges to `~/.local/bin`, Claude agents/commands to `~/.claude`, Codex agents to `~/.codex/agents`, registers the repo's Codex plugin marketplace, installs plugin skills/hooks, deploys exact Warp recovery plus the `c` and `g` tmux helpers, and merges compact global doctrine pointers without replacing auth or unrelated settings. The repo is the source of truth — re-run `./install.sh` after editing here.

Keys: the Ollama key lives in OpenCode's auth store (`~/.local/share/opencode/auth.json`) — no secret in any script; the xAI key lives in `.env` (gitignored).

## Graduation / public project

This repo is **tenant #1** of the harness. Its public core, [`orchestratormaxxing`](https://github.com/ricalanis/orchestratormaxxing), is a deterministic projection produced by `bin/core-export` from `deploy/graduation.manifest`: only allowlisted *committed* paths ship, `<!-- tenant:begin -->`…`<!-- tenant:end -->` blocks are stripped, and a literal + gitleaks gate runs on the exact published bytes before the mirror push. A feature graduates by being added to the manifest. Tenant identity (fleet server, dashboard URL, notify target/relay, LAN peer) lives only in `~/.config/claudemaxxing/fleet.env`, deployed by `install.sh`; a machine without that file is a standalone client — hooks stay silent and spawn no transport — and git is the only connection between tenants.

## Repo layout

```
CLAUDE.md          # the doctrine — orchestrator pattern, verification policy, memory governance
SETUP.md           # port the harness to a new machine
bootstrap.sh       # scripted version of SETUP.md
install.sh         # deploy the harness globally (idempotent)
xsearch.py         # xAI/Grok web + X search
bin/               # workers · verification · memoryctl · harness-sync · core-export
.agents/memory/    # governed project facts shared by Claude/Codex and private Git
.claude/
  agents/ollama-worker.md          # the cheap dispatcher subagent
  commands/{fanout,ideas,self-improve}.md
.codex/
  config.toml                      # repo-local limits; no model/auth pin
  agents/*.toml                    # Codex role adapters
plugins/claudemaxxing/             # Codex skills + lifecycle hooks
knowledge/         # research notes, protocol specs, self-improve log, transcripts
```

## Research foundations

The doctrine isn't folklore — each rule traces to a verified paper (full notes in [`knowledge/`](knowledge/)):

| Finding | Source | What it changed here |
|---|---|---|
| Static multi-agent usually loses to one good agent; only dynamic decomposition wins | arXiv:2606.05670 | Default single-agent; `/fanout` only on truly independent chunks |
| Self-correction is driven by role labels, not reasoning (+23–93pp when errors look external) | arXiv:2606.05976 | Critics are always different models, framed as external reviewers |
| Harness flaws decompose into ETCLOVG layers; fixing them beats blaming the model | arXiv:2606.06324 | Failure attribution + `harness-verify` lint |
| Big tool menus reduce reliability; minimal frontier ≈ 90% fewer tokens at equal success | arXiv:2606.06284 | Minimal tool/skill frontier per worker |
| Agents charge ahead on underspecified intent (best model 52% after 3 rounds) | arXiv:2606.05920 | Tier-0 spec-gate + bounded repair loop |
| Bitemporal typed-operator algebra for memory contradictions | arXiv:2606.06240 | Supersede protocol with named resolution operators |
| Agents over-surface sensitive memory +51–83% | arXiv:2606.06055 | Credentials never stored; PII least-disclosure |
| Self-referential harness optimization needs a non-self-preference guard | arXiv:2606.05922 | `harness-verify` as the deterministic selector |

---

*The name is the method: take Claude Code, and max it out.*
