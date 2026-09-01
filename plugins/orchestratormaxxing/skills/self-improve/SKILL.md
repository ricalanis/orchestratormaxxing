---
name: self-improve
description: Run exactly one governed, verifier-selected self-improvement round on the orchestratormaxxing harness. Use when loop-queue has an actionable flaw or the user explicitly requests a harness round; never fabricate work merely because the queue is empty.
---

Run one propose -> evaluate -> select round. The primary Codex thread owns decomposition and sign-off; LLM preference never selects its own rewrite.

1. MINE
   - Read `bin/loop-queue list` first. Recurrences outrank new items. Claim exactly one item.
   - If the queue is empty, inspect `bin/harness-verify --json`, `bin/mem-audit --json`, the user's explicit focus, and optionally `bin/harness-scan --days 7`. Enqueue only a concrete finding supported by one of those signals; an empty result means stop without a round.
   - Attribute the flaw to one ETCLOVG layer and state it in one line. If `harness-verify` is already red for an unrelated reason, halt and report it; never auto-repair the guard.
2. PROPOSE
   - For a bounded non-doctrine flaw, ask 2-3 different heavy Ollama families for scoped candidate diffs in parallel. Direct `oll` calls are preferred; each prompt includes the flaw-specific contract and a commitment ledger: added assumptions, exceptions/special cases, and narrowed supported states/inputs. Diff size limits blast radius but is not selection evidence.
   - Candidate diffs are response-only proposals and never edit a worktree. Any explicitly delegated workspace implementation uses `o delegate <run-id> --profile bounded-code|reasoning|long-horizon --run-dir .results/delegation/<run-id> --json` in its isolated worktree and always ends with `o close <session>`.
   - For tiny or load-bearing changes, the root thread may draft one proposal directly.
3. EVALUATE
   - Apply competing code candidates in isolated temporary worktrees so a red candidate cannot contaminate the current tree.
   - Require `bin/harness-verify` green plus a flaw-specific assertion. Use `bin/mut` for changed Python tools when a false green is expensive.
   - Discard red candidates and remove their temporary worktrees after capturing the failure evidence. Read only pass/fail, failure diffs, and mutation survivors until final sign-off.
   - If no candidate survives, archive the rejected round, keep the queue item unresolved, report the failure evidence, and stop.
4. SELECT
   - **VALIDITY-FIRST / WEAKEST-SUFFICIENT:** Compare only candidates sufficient for the same deterministic contracts. Prefer A over B only when A adds no more assumptions, exceptions/special cases, or narrowing of the supported state/input set, and strictly less in at least one; otherwise abstain and escalate. Never substitute diff/line count, description length, cost, or a fabricated numeric weakness score. Bennett's theorem assumes a finite enactive formalism and uniform task distribution; here this is only a qualitative tie-break after validity.
   - Send surviving work to two different-family critics, framed as another agent's proposal to refute. Agreement is corroboration; disagreement or doubt escalates to the root thread.
5. SIGN-OFF
   - The root Codex thread reads the winning diff once and owns the merge decision.
   - Changes to `AGENTS.md`/`CLAUDE.md`, memory doctrine, `.claude/`, `.codex/`, this plugin, provider routing, or `install.sh` are load-bearing: ask the user before committing. In unattended runs, leave them `PROPOSED` and uncommitted.
   - Non-load-bearing fixes may commit directly on the current branch only when the invoking policy explicitly authorizes it. Never push; the loop wrapper owns synchronization.
6. ARCHIVE
   - Append the round to `knowledge/self-improve-log.md`, including rejected alternatives and verifier delta.
   - Resolve the claimed queue item, or leave it claimed with a `PROPOSED` note when waiting for human sign-off.

Ratchet: an accepted round must resolve one real queue item or add one deterministic verifier assertion. Strategic rounds additionally owe a measured KPI improvement with a paired guard KPI. One trigger means one round; bounded spend is 2-3 proposers and 2 critics.
