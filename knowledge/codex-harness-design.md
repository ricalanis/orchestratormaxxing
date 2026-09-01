# Codex harness adapter — design and rollout

Date: 2026-07-14

## Decision

Make Codex a first-class host over the existing claudemaxxing core. Do not fork the harness into "Claude logic" and "Codex logic." The durable queues, Ollama bridges, session persistence, memory auditor, mutation runner, verifier, and scheduler remain shared. Only host integration lives in adapters.

```
                    primary frontier host
                 Claude Code       Codex
                      │               │
          .claude commands/agents   plugin skills/hooks
                      │             .codex agents/config
                      └───────┬───────┘
                              │
         contracts + bin/ tools + knowledge/ + queues
                              │
             Ollama / cross-provider workers
                              │
        deterministic tests · mut · harness-verify
```

This preserves the actual doctrine: the frontier host decides what to delegate, authors the contract, reads deterministic results, and owns the merge. The worker provider is an implementation detail.

## Practices to carry into Codex

### 1. Keep one root decision-maker

Default single-agent. Fan out only independent, acceptance-gated chunks. Codex native subagents are valuable for read-heavy exploration, parallel tests, and roles that benefit from visible agent threads. They are not the default wrapper around `oll`: spending a Codex subagent just to launch an Ollama process defeats the quota-saving design.

Codex routes nontrivial unplanned work through `$claudemaxxing:solplan` first. Root Codex reviews the Sol Ultra plan and acceptance contract, then invokes `$claudemaxxing:fanout` only when the accepted execution shape contains genuinely independent implementation chunks. File count alone never justifies fanout; trivial, already-approved, and tightly coupled execution stays in root Codex.

The `$claudemaxxing:fanout` skill therefore operates after required planning and prefers bounded parallel `oll` calls. The optional `ollama-worker` custom agent exists for manual delegation and visible per-agent inspection.

### 2. Spec before dispatch

The root Codex thread writes the acceptance contract before any worker call. Code uses deterministic assertions/tests; analysis uses a boolean evidence checklist. A chunk without a cheap contract is kept in Codex. Repair gets at most two rounds with only the failure diff returned to the same worker; repaired code must pass mutation testing.

### 3. Preserve critic independence

When Codex is the orchestrator, `provider-ask openai` is not an independent critic—it is another Codex-family process. Prefer Ollama, xAI, or Claude/Fable depending on the artifact. Cross-family agreement is corroboration, never a replacement for deterministic gates.

The Codex `$claudemaxxing:self-improve` contract resolves an ambiguity in the Claude command: it requires exactly two different-family critics for surviving changes.

### 4. Use native Codex customization surfaces

- `AGENTS.md`: durable repo doctrine. This repo keeps `AGENTS.md → CLAUDE.md` for shared history and raises `project_doc_max_bytes` to 64 KiB so the global + project chain is not truncated.
- `.codex/config.toml`: repo-local multi-agent bounds and hook feature enablement; it deliberately does not pin model/provider/auth.
- `.codex/agents/*.toml`: narrow roles (`ollama-worker`, `sol-planner`, `product-manager`). `sol-planner` pins `gpt-5.6-sol` with `model_reasoning_effort = "ultra"` and read-only sandboxing. Delegation stays bounded at depth 1 and four total threads.
- `plugins/claudemaxxing/skills/*`: reusable `$claudemaxxing:fanout`, `$claudemaxxing:ideas`, `$claudemaxxing:self-improve`, `$claudemaxxing:wrap-up`, `$claudemaxxing:solplan`, `$claudemaxxing:product-manager`, and `$claudemaxxing:memory` workflows. Solplan is the first choice for nontrivial unplanned Codex work and uses GPT-5.6 Sol Ultra with up to three direct read-only exploration subagents. Its runner closes stdin, suppresses unrelated lifecycle work, captures the final answer, validates the plan contract, and times out cleanly. The root Sol planner synthesizes; root Codex reviews the plan and may fan out only independent implementation chunks. Claude's `/fableplan` remains unchanged and Claude-only.
- `plugins/claudemaxxing/hooks/hooks.json`: SessionStart/Stop integrations for `mem-audit`, `loop-tick`, and `session-log`.
- `.agents/plugins/marketplace.json`: version-controlled local distribution. `install.sh` registers and installs it without replacing unrelated Codex config or auth.

Official Codex behavior informing these choices: [custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [skills](https://learn.chatgpt.com/docs/build-skills), [hooks](https://learn.chatgpt.com/docs/hooks), and [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

Non-managed plugin hooks require one-time review in `/hooks`. The trusted `g` launcher and explicit
autonomous runner use Codex's automation-only `--dangerously-bypass-hook-trust` flag; a direct
Codex CLI/app thread stays on the normal review boundary.

### 5. Keep autonomous Codex opt-in until observed

`harness-agent-run` makes the scheduled runner host-neutral, but the default remains Claude. Set `HARNESS_HOST=codex` only after manual `$claudemaxxing:self-improve` rounds have produced a green evidence trail. Codex receives an external wall-clock bound; the skill itself bounds provider calls and stops load-bearing changes at `PROPOSED`.

Rollout sequence:

1. Manual `$claudemaxxing:fanout`, `$claudemaxxing:ideas`, `$claudemaxxing:wrap-up`.
2. Manual `$claudemaxxing:self-improve` against a synthetic or low-risk queue item.
3. Hook review/trust and SessionStart/Stop observation.
4. `LOOP_DRY=1 HARNESS_HOST=codex`.
5. `LOOP_SYNC=0 HARNESS_HOST=codex` for one local round.
6. Only then allow the existing cross-machine claim/sync wrapper to run Codex.

Never arm separate Claude and Codex timers against the same queue. One scheduler selects one host.

### 6. Share governed memory without scraping host internals

The explicit file-backed adapter now exists: adopted repositories keep one fact per file under `.agents/memory/`. Claude's legacy project-memory directory is imported, backed up, and replaced by a reversible symlink; Codex receives the same bounded index through its plugin hook and reads/writes with `$claudemaxxing:memory`/`memoryctl`. `mem-audit` resolves this canonical directory for both hosts. Codex's private SQLite remains untouched.

Because memory is versioned with the private project repository, the existing Git synchronization and conflict model carries it between Linux and macOS without a new listener or credential. Credentials remain forbidden; sensitive facts are hidden from the automatic brief.

### 7. Make tmux semantics exact

`g` mirrors the lifecycle of `c` with Codex-native commands:

- `g [name]` creates a fresh auto-numbered `codex-*` interactive TUI.
- `g [name] -A|--attach` attaches the exact base session.
- `g --prompt` runs `codex exec --json` without tmux.
- `g [name] --headless --prompt` writes JSONL under `/tmp`.
- `g ls` lists/attaches; no `gs` alias because Ghostscript owns it.
- Unknown Codex flags retain argument boundaries. `-a` remains Codex approval policy, not attach.
- Only `PATH` and an explicit `CODEX_HOME` cross into new tmux sessions; credentials are not bulk-copied.

`tmux-send` uses exact session targets and literal text insertion, then checks Claude/Codex execution markers.

## Deterministic acceptance contract

The port is ready when:

1. `bin/harness-verify` is green and checks both host adapters.
2. Every Codex agent TOML parses and contains `name`, `description`, `developer_instructions`.
3. The plugin validator and each skill validator are green.
4. The Codex plugin appears installed and its hooks are available for review/trust.
5. `g` sources under bash (and zsh where installed), creates exact auto-numbered sessions, forwards native flags, and refuses headless mode without a prompt.
6. `tmux-send` test fixtures recognize both Claude and Codex working/idle states.
7. `install.sh` is idempotent and preserves existing `~/.codex/config.toml` and auth.
8. `bootstrap.sh` never removes Codex and installs gstack's Codex host.
9. `harness-agent-run` defaults to Claude, rejects unknown hosts, and exposes Codex only through explicit `HARNESS_HOST=codex`.
10. The user-owned dirty state that predated this port remains untouched.

## Deferred deliberately

- Automatic merging of genuinely conflicting cross-machine memory edits; ordinary Git conflict handling remains the honest boundary.
- Dashboard transcript ingestion/revive for Codex sessions. Named `codex-*` panes are tmux-accessible now; first-class dashboard history is a separate cross-layer change.
- Changing the current frontier model. User-level Codex configuration remains authoritative.
- Rewriting the entire Claude-oriented doctrine into a new neutral file. The adapter section provides correct precedence with much less migration risk; a later documentation-only pass can rename the historical host cleanly.
