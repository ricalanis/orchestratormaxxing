# Evidence-gated context lifecycle — HISTORICAL POST-MORTEM

> **This document is history, not a live proposal.** The tooling it describes
> (`bin/context-lifecycle`, `config/context-lifecycle.json`, its test contract, and the Pi
> lock file) was **deleted on 2026-07-18**. The harness settles on host-native compaction and
> overrides none of it; see the "Context compaction is host-native" section of CLAUDE.md.
> Nothing below is applied, and the stage table/thresholds are retained only as the design
> that was tried. Do not resurrect them without new evidence — `harness-verify` fails if any
> host config or `install.sh` pins a compaction override.

## STATUS 2026-07-18 — REVERTED, then REMOVED.

The stage model is retained as design. Its **host numbers were rolled back** and are no longer
written anywhere. What happened, so it isn't repeated:

- Commit `189dfb8` (2026-07-17 20:29) wrote the uncalibrated treatment into **global** Claude,
  Codex, and OpenCode config, and `install.sh` re-applied it on every re-sync. The policy's own
  `promotion_gate` (10 trials/stage, ≤5pp pass-rate drop, ≤2% infra failure, 0 protected-fact
  loss) **was never run**, yet the round was recorded as "verified." That claim was wrong.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` is Claude Code's internal **test-only** `testPctOverride`
  (sibling of `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`). Setting it discards the calibrated
  per-window `precomputeBufferFraction` lookup table and applies a flat percentage; paired with
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (which "takes precedence" and blocks self-correction) the
  trigger was pinned at 128,100 tokens.
- Measured from `~/.claude/projects` transcripts (`compact_boundary` markers), split on the
  commit: **28 compactions Jun 12 → Jul 14** (~0.8/day) versus **63 on Jul 18 alone**.
- Codex showed **no** regression (353 pre vs 37 post) because scope `body_after_prefix` counts
  only post-prefix tokens — the keys were wrong but not harmful. Codex's native limit is 90% of
  the model's resolved context window (`codex-rs` `protocol/src/openai_models.rs::
  auto_compact_token_limit`), so a flat 128000 was a guess *below* the model's own calibration.
- OpenCode's block was removed with the rest; it had not been isolated as a cause.
- **Pi (`pi-ollama-context-trimmer`) was never the problem and was never installed** — only a
  lock file exists; `pi-install` is a print-only stub.

Enforcement now (2026-07-18, after removal): there is no tool left to re-apply any of this.
The planner/auditor and its policy file are deleted; what survives is a single unconditional
guard — `harness-verify` fails if any host config or `install.sh` pins a compaction override,
and naming the removed tool in executable installer code is itself an error. An earlier
revision of this paragraph described `apply-safe` refusing while provisional and `install.sh`
auditing instead of applying; those code paths no longer exist.

**Lesson (ETCLOVG: Governance).** The old contract asserted the overrides must be *present* —
a test that enshrined an unvalidated change instead of gating it. A config that declares itself
provisional must never be reachable by an installer.

## Decision

Use four semantic stages across harnesses, translated through each host's native surface:

| Stage | Initial frontier | State transition |
|---|---:|---|
| Verbatim | 0–50k estimated tokens | Keep the active working set intact. |
| Prune | 50–96k | Remove duplicate notifications, stale tool output, and completed actions whose effects are persisted. |
| Checkpoint + compact | 96–128k | Preserve the explicit work contract, then invoke the host-native compactor. |
| Recover | 128–150k | If compaction cannot restore a bounded working set, start fresh from the checkpoint and persisted repository state. |

The numbers are the first calibration treatment. A host/model/task threshold advances only
after ten trials, zero protected-fact loss, no more than a five-point pass@1 loss, and no more
than 2% infrastructure failures. Characters/3 is a conservative proxy for code and JSON; actual
provider token counts supersede it when available.

Protected state is the objective/current dispatch, acceptance criteria, constraints, decisions,
modified paths, verification state, unresolved questions, and next steps. Repository content,
persisted outputs, and completed action history are references rather than prose to carry.

## Host translation

- **Claude Code:** use its native prune-then-summary behavior. The installed initial treatment
  uses an effective 183k calculation window and 70% trigger (about 128k). Root/global
  `CLAUDE.md`, shared memory, and the bounded session log reload after compaction. Compact
  instructions preserve the work contract. We do not rewrite outbound Anthropic requests,
  because changing the repeated prefix would work against prompt caching.
- **Codex:** use remote native compaction at 128k body-after-prefix tokens and the versioned
  compact prompt. Do not read private SQLite or transcript files. Its existing SessionStart
  hooks reload shared memory and the session log after a compact lifecycle transition.
- **OpenCode/Ollama:** enable native old-tool pruning, automatic summary, five recent turns, and
  a 24,576-token recent budget. A 65,536-token reserve is applied only when every configured
  Ollama coding agent has verified context metadata of at least 196,608 tokens. OpenCode 1.17.9
  only subtracts that reserve when the provider exposes a separate input limit; with the current
  context/output-only Ollama catalog, the observed native triggers remain about 167k for GLM and
  223k for Kimi. The audit reports this explicitly. Existing user values always win, and we do not
  fabricate input-limit metadata merely to force the 128k treatment.
- **Pi/Ollama:** `pi-ollama-context-trimmer` remains commit-pinned and proposal-only until Pi
  exposes a verified isolated Ollama profile. Its request-view eviction is valuable for Ollama
  Cloud economics but must not become a global Anthropic/OpenAI extension.
- **`oll`, `provider-ask ollama`, and `oll-council`:** these are one-shot calls with no session
  history to compact. They reject inputs above 50k proxy tokens before reading authentication or
  touching the network unless the operator supplies an attributable override.

## What came from Purrly, and what changed

**[Source fact]** At commit `08f1f08c06910d07520215ee466598a84a4f71b2`, the Pi extension
protects pinned/dispatch/path-selected content, collapses redundant subagent traffic, preserves a
recent slice, detects repeated tool calls, and drops old whole turns above its configured cap. Its
320 upstream tests pass locally. The 50–100k “hold” tier performs no compression, and the
`SUMMARIZE_TIER_MAX_TOKENS` name does not correspond to a summary operation.
[Upstream repository](https://github.com/PurrlyDigital/pi-ollama-context-trimmer)

**[Inference]** Protected semantic classes and pair-atomic tool-call/result handling transfer well.
Per-request message mutation does not: Claude Code and Codex already own compaction and caching,
while direct `oll` has no accumulated history.

**[Local policy]** We therefore use native compaction plus explicit checkpoint/rehydration for
Claude/Codex, deterministic pruning before summary in OpenCode, and keep Pi's hard-drop extension
isolated to Ollama.

## Research basis and limits

- **[Source fact]** [Needle Threading](https://arxiv.org/html/2411.05000) finds task/model-specific
  effective context frontiers far below advertised limits for many models and warns that tokenizer
  token counts are not directly comparable. This rules out calling 128k universally optimal.
- **[Source fact]** [Lost in the Middle](https://arxiv.org/abs/2307.03172) finds position-sensitive
  retrieval, motivating beginning/middle/end canaries and reintroducing protected facts near the
  active edge.
- **[Source fact]** [RULER](https://arxiv.org/abs/2404.06654) shows simple needle success does not
  imply multi-hop or aggregation reliability. Calibration therefore includes instruction,
  retrieval, and code-state canaries.
- **[Source fact]** [ACON](https://arxiv.org/abs/2510.00615) learns compression guidance from
  paired full-context successes and compressed-context failures. We adopt the evaluation shape,
  not an autonomous self-optimizing compressor.
- **[Source fact]** [Active Context Compression](https://arxiv.org/abs/2601.07190) reports a 22.7%
  token reduction with identical 3/5 accuracy on only five SWE-bench Lite instances. It is useful
  evidence, but too small to set a production threshold.
- **[Source fact]** [Problems of Implicit Context Compression](https://arxiv.org/abs/2605.11051)
  reports that continuous embedding compression which works on single-shot code understanding can
  fail multi-step coding. Opaque compression is out of scope.
- **[Source fact]** [Structured Context Eviction](https://arxiv.org/abs/2606.11213) proposes typed,
  dependency-aware, deterministic eviction of recoverable action episodes. This supports our
  protected/recoverable/disposable classification, while remaining a recent preprint.

Official host surfaces used by the adapter are documented in Claude Code's
[context-window guide](https://code.claude.com/docs/en/context-window) and
[environment variables](https://code.claude.com/docs/en/env-vars), plus Codex's current config
reference/manual (`model_auto_compact_token_limit`, scope, and compact prompt).
