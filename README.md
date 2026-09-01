# orchestratormaxxing

A verifier-gated orchestration harness for Claude Code, Codex, OpenCode, Zed and Warp. It is for developers who want a deterministic, self-improving setup that protects frontier-model tokens. The public project is a graduation of a private lab.

## Quick start

```bash
git clone https://github.com/ricalanis/orchestratormaxxing.git
cd orchestratormaxxing
export OLLAMA_API_KEY=…        # required
export XAI_API_KEY=…           # optional
./bootstrap.sh
./install.sh
harness-verify
```

Expected result on a standalone earth: 0 errors, 15 rows skipped as fleet-only.

## What gets installed

| Host surface | What is installed |
|---|---|
| orchestratormaxxing bridges | `~/.local/bin` |
| Claude Code | hooks, agents, commands |
| Codex | plugin + agents |
| OpenCode | agents, commands, plugins, Ollama Cloud provider |
| Zed | `zed-setup` |
| Warp | `warp-model-pin`, rules paste source |
| tmux | `c`/`g`/`o`, `tmux.conf` |
| shared memory | `memoryctl` |
| session log | `session-log` |

## What it never touches

Nothing outside your home. No network calls from hooks without `fleet.env`. Keys are read only from OpenCode's auth store or env. Arming schedulers is opt-in.

## Topology: sun, earth, moons

- **sun** — the fleet server (a Linux box): hub of the fleet, runs the Hermes dashboard, the daily loop, fleet services. `ORCHESTRATORMAXXING_SERVER_*` in `fleet.env` names the sun.
- **earth** — the human's laptop client (macOS today): Warp, Zed, tmux `c`/`g`/`o` sessions, Claude Code, Codex, OpenCode. In fleet mode an earth reaches its sun over a private network; the public default is a **standalone earth** (no sun: no `fleet.env`, hooks silent, nothing reaches out).
- **moons** — containers orbiting the sun (Semantica, Open Design, Firecrawl…): fleet services, private half.

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

## Verification policy

Two tiers, applied so that Opus never re-derives a worker's output:

- **Tier 0 — spec-gate before dispatch.** If you can't state the acceptance contract for a chunk, it's underspecified (arXiv:2606.05920) → refine it or keep it in Opus.
- **Tier 1a — correctness.** Opus authors the tests (never the worker alone — a worker that writes its own tests can bake the same bug into both); a deterministic runner executes them; Opus reads pass/fail + diffs.
- **Tier 1b — contract adequacy (gated).** For high-value / risky / new-worker chunks, `bin/mut` checks whether the contract is *tight*: each surviving mutant is either a real hole (add one assertion, re-run) or an equivalent mutant (accept). Cost scales with the number of holes, not the size of the solution.
- **Tier 2 — non-testable work.** Boolean checklist scan with a token cap, or a second *different-model* critic framed as "another agent produced this — try to refute it."
- **On failure — bounded repair.** Feed the failure diff back to the same cheap worker for ≤2 rounds before escalating. A repaired chunk must clear Tier-1b before acceptance — the anti-gaming gate against workers overfitting a visible contract.

Workers earn trust over time from their mutation scores: a model that consistently leaves tight contracts with zero real survivors gets the light-check path.

## Memory governance

Project memory is **shared by Claude and Codex, governed, and not append-only**. The private repository carries `.agents/memory/` between machines; both hosts load the same bounded index, and Claude's legacy memory path points to that store. The protocol, specified in [`knowledge/memory-protocol-upgrade.md`](knowledge/memory-protocol-upgrade.md):

- **Supersede, don't append** — conflicts are resolved by a named operator from a typed algebra (`last-writer-wins | evidence-merge | await-confirm | per-rule`, per TOKI arXiv:2606.06240), with the loser kept as a back-linked audit row.
- **Bitemporal clocks** — `created`/`last_verified` (when recorded) vs `valid_from`/`valid_to` (when true in the world); decay off whichever bites first. TTLs: reference 30d · project 14d · feedback 180d · user 365d.
- **Critic gate** on belief-changing writes only (cost scales with stakes).
- **No stored credentials, ever** (write-time rule; `mem-audit` is a pattern+entropy backstop, not the control). PII gets `sensitivity: sensitive` + least-disclosure, because agents over-surface sensitive memory +51–83% (arXiv:2606.06055).
- **Deterministic enforcement** — `mem-audit` computes staleness and drift from dates and the filesystem at every SessionStart; nothing re-reads the vault to "figure out" what's stale.
- **No memory server** — private Git handles cross-machine transport and normal conflict detection; Tailscale SSH is for authenticated remote commands only.

## Self-improvement loop

`/self-improve` runs one governed round: **MINE** (internal: `harness-verify` + `mem-audit`; external: `harness-scan` over fresh arXiv/X) → attribute the flaw to an **ETCLOVG** layer (Execution / Tool / Context / Lifecycle / Observability / Verification / Governance, per HarnessFix arXiv:2606.06324) → **PROPOSE** 2–3 diverse candidate fixes via workers → **EVALUATE** with the deterministic verifier (a fix that reds `harness-verify` is not a fix) → **SELECT** via cross-model critics prompted to refute → **Opus sign-off** (human gate on doctrine) → **ARCHIVE** every round, accepted *and* rejected, to `knowledge/self-improve-log.md` (created by the loop's first round).

It's propose-evaluate-select (ADAS / Darwin-Gödel Machine), with the one substitution that makes it safe: selection is a verifier, never the harness's preference for its own rewrite.

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

## Fleet mode (private half)

These tools ship only from the private installer and are not part of this public projection: `folder-sync`, `gpu-agent`, `gpu-desktop`, `harness-remote`, `project-new`, `semantica`, `firecrawl`, `opendesign`, `drive`, `design-eval`, `worker-path-bench`, `transcription-fix`, and the proposal toolchain. The maintainer's graduation workflow (`/graduate`) is also private; `bin/core-export` itself ships so you can run your own projection.

## Graduation

This repo is produced by `bin/core-export` from a private source of truth. Direct edits are overwritten by the next graduation. Contributions go through PRs that the maintainer absorbs with `core-export --absorb-pr <n>`. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licensed MIT.

## Tools

Tools are dependency-free (Python stdlib or plain bash), read-only where they audit, and installed globally as bare commands by `./install.sh`. The curated set below is what an orchestrator session routes work with; the exhaustive complement lives in `knowledge/tool-inventory.md`.

| Tool | What it does |
|---|---|
| `bin/oll` | One-shot Ollama worker. `deepseek-v4-flash:0731` is the volume default; `glm-5.3` is explicit reasoning; `kimi-k3` the higher-consumption long-horizon route. |
| `bin/oll-council` | Same prompt to several *diverse* frontier models in parallel (Zhipu / DeepSeek / Moonshot / Alibaba / MiniMax / Mistral) — for design decisions and getting unstuck. |
| `bin/oll-sync` | Sync OpenCode's `ollama-cloud` model list with the live catalog (adds new, prunes dead, preserves curated names). |
| `bin/ticket-route` | Proposal-only "Purrly-lite" router: rejects missing ACs, permits fan-out only for disjoint independent chunks, and activates review from explicit risk/domain signals. |
| `bin/mut` | **Tier-1b mutation gate** — "who tests the tests?". Deterministically mutates a source file's AST, runs your contract against each mutant, reports only the *survivors* (mutations the contract failed to catch). |
| `bin/mem-audit` | Deterministic health check for project memory: staleness (bitemporal TTLs), index drift, supersede-link integrity, secret/PII backstop. Runs at SessionStart via hook. |
| `bin/memoryctl` | Locked, atomic shared memory for Claude and Codex under `.agents/memory/`: bounded recall, add/supersede, dry-run legacy import, and reversible Claude-path bridge. |
| `bin/harness-sync` | Safe Linux/macOS fetch + fast-forward setup. Refuses dirty, ahead, or diverged trees; never resets or auto-stashes. |
| `bin/harness-verify` | **The regression guard** for the self-improvement loop: every tool parses and is executable, every tool/command is wired into `install.sh` (deploy coverage), `CLAUDE.md` keeps its load-bearing doctrine sections, `mem-audit`/`mut` still run. Exit 1 = regression. |
| `bin/harness-scan` | External-research intake for `/self-improve`: pulls last-N-day arXiv harness/agent-design papers (and `--x` for X/web), tagged by pillar. |
| `bin/warp-ollama` | Prints the paste-ready config to point **Warp's agent** at Ollama Cloud (Settings → "inference endpoint") — Base URL + key (read from the OpenCode auth store) + the same heavy models. |
| `bin/warp-agent-recovery` | Restores each Warp terminal's exact `c`/`g` tmux session after a full Warp quit/crash; if tmux died, resumes the exact Claude/Codex session ID through the same `c`/`g` launcher. |
| `bin/cogload` | **Silent cognitive-load / stress / performance collector.** Per-minute aggregates of key *classes* (backspace/enter/nav/…, never identity), inter-keystroke timing, app-class switching and concurrent agent sessions. |
| `bin/core-export` | **The graduation tool.** Allowlist projection of a *committed* tree per `deploy/graduation.manifest` (include/exclude/strip/block/remote): tenant blocks stripped, literal + gitleaks gate on the exact published bytes, atomic tree, mirror push to the sibling public project. |
| `bin/tmux-send` | Deterministic tmux sender with execution validation — resolved pane ids, C-m submit, retries; never bare `send-keys`. |
| `bin/xsearch` | xAI/Grok web + X search via the Agent Tools API. |

## Claude commands and Codex skills

| | |
|---|---|
| `/fanout <task>` | Decompose → spec-gate each chunk → one `ollama-worker` per chunk in parallel → verify (two-tier) + merge. |
| `/ideas <problem>` | Boot several *different* models on the same problem, then Opus synthesizes — consensus core, genuine alternatives, recommendation. |
| `/self-improve [focus]` | One governed round of harness self-improvement (see below). |
| `ollama-worker` (subagent) | A cheap dispatcher (Haiku) that hands a single well-scoped task to an Ollama Cloud model via `oll` and returns the output verbatim. |

Codex exposes `$orchestratormaxxing:fanout`, `$orchestratormaxxing:ideas`, `$orchestratormaxxing:self-improve`, `$orchestratormaxxing:wrap-up`, `$orchestratormaxxing:solplan`, and `$orchestratormaxxing:memory`. Planning is host-native: Claude's `/fableplan` delegates to Fable, while Codex prioritizes `$orchestratormaxxing:solplan` with GPT-5.6 Sol Ultra and bounded read-only delegation for nontrivial unplanned work. After root review, `$orchestratormaxxing:fanout` is used only for genuinely independent implementation chunks.

### Worker models — heavy frontier only

Normal fanout uses `deepseek-v4-flash:0731`, `glm-5.3`, `kimi-k3`, `kimi-k2.7-code`, and `qwen3.5:397b`. V4 Flash is the volume default (Medium usage, 1M ctx; V4 **Pro** is Extra-High usage and is reserved for low-volume reasoning via an explicit `--model`), GLM 5.3 is explicit reasoning (High usage, thinking always on), K2.7 is bounded code, and K3 is higher-consumption long-horizon/1M-context work. Legacy generations require `oll --allow-legacy-model` only for historical benchmarks.

## Install

**Port to a new machine:** follow [`SETUP.md`](SETUP.md). On an existing clean checkout, `bin/harness-sync setup` safely fast-forwards, installs both hosts, and connects shared memory.

**Deploy the harness globally** (Claude Code, Codex, and OpenCode):

```bash
./install.sh
```

This copies bridges to `~/.local/bin`, Claude agents/commands to `~/.claude`, Codex agents to `~/.codex/agents`, registers the repo's Codex plugin marketplace, installs plugin skills/hooks, deploys exact Warp recovery plus the `c` and `g` tmux helpers, and merges compact global doctrine pointers without replacing auth or unrelated settings. The repo is the source of truth — re-run `./install.sh` after editing here.

Keys: the Ollama key lives in OpenCode's auth store (`~/.local/share/opencode/auth.json`) — no secret in any script; the xAI key lives in `.env` (gitignored).

## Repo layout

```
CLAUDE.md          # the doctrine — orchestrator pattern, verification policy, memory governance
SETUP.md           # port the harness to a new machine
bootstrap.sh       # scripted version of SETUP.md
install.sh         # deploy the harness globally (idempotent)
bin/               # workers · verification · memoryctl · harness-sync · core-export
.agents/memory/    # governed project facts shared by Claude/Codex and private Git
.claude/
  agents/ollama-worker.md          # the cheap dispatcher subagent
  commands/{fanout,ideas,self-improve}.md
.codex/
  config.toml                      # repo-local limits; no model/auth pin
  agents/*.toml                    # Codex role adapters
plugins/orchestratormaxxing/             # Codex skills + lifecycle hooks
knowledge/         # research notes, protocol specs, self-improve log, transcripts
```

---

*The name is the method: take Claude Code, and max it out.*
