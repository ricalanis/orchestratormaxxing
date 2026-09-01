# Gauntlet Loop — design + research trail (2026-08-01)

Ricardo's spec: *"The agent (not you!!) breaks the goal into parts, gives each part a
specialist builder and a ruthless blind critic sub-agent, with a mandate to only pass if
the generated artifact is better than some real-world equivalent."* Implemented in both
hosts: `/gauntlet` (Claude) · `$orchestratormaxxing:gauntlet` (Codex) · `bin/gauntlet-judge`
(the shared deterministic gate). Pipeline: 2 parallel arXiv deep-research agents + repo
conventions map → fableplan (Fable planned, Opus built) → Sol adversarial critique
(2 BLOCKERs + 3 MAJORs, all adjudicated below) → OPUS-DIRECT implementation.

## The loop

1. **Decompose in a sub-agent** (`ollama-worker` → `deepseek-v4-pro`; direct `oll` on
   Codex): proposes 3–7 parts, each with artifact spec, builder specialty, candidate
   real-world reference R + provenance, and candidate rubric criteria.
2. **Orchestrator ratifies before any builder runs** (Tier-0 preserved: workers
   *propose*, the orchestrator *authors*): fixes each rubric (marks load-bearing),
   authors the deterministic acceptance contract.
3. **Specialist builders** in parallel (heavy frontier pool, biased by `win-log match`).
4. **Deterministic acceptance** (Tier 1a → `bin/mut` for risky parts) with **one shared
   repair budget of 2 rounds per part** across contract failures *and* gauntlet
   refutations (Sol BLOCKER-2 fix — never 2+1).
5. **Promotion gate**: `gauntlet-judge` runs the blind pairwise panel. Exit 1 →
   refutations are *claims* to adjudicate deterministically (CriticGPT 2407.00215);
   at most one targeted-assertion repair from the shared budget; never re-roll judges
   seeking a pass (Goodhart 2210.10760).
6. **Report + record**: `win-log add --shape gauntlet-N` only when 1b actually ran
   clean (Sol MAJOR-5 fix — an unmutated part never claims `--survivors 0`).

**Invariant: the gauntlet verdict promotes, never accepts.** Enforced by a
harness-verify token check on both host surfaces.

## The gate protocol (bin/gauntlet-judge)

- **Rubric-first** (2507.17746): 3–10 boolean criteria with evidence, authored before
  the artifact exists; ≥1 load-bearing. Judges see id+text only (not load-bearing marks).
- **Blind** (2510.07517): both bodies stripped of provenance, labels "Artifact 1/2" by
  SystemRandom coin flip; the orchestrator never composes the judge prompt, so blinding
  is structural, not disciplinary.
- **Cross-family** (2404.13076, 2410.21819, 2502.01534): 2 judges, families pairwise
  distinct and disjoint from the builder's and the reference author's; collisions
  refused pre-network (exit 2). Prefix→family map in the tool; default pool
  qwen/mistral/deepseek/kimi/glm/minimax.
- **Both orders** (2305.17926): each judge reads A/B and B/A; an order-flipped winner
  is noise → fail closed.
- **Anti-sycophancy** (2509.23055): per-artifact refutation or the literal
  "cannot refute"; no graded praise channel exists in the schema.
- **Pass rule**: all 4 readings valid ∧ order-stable ∧ G wins ≥3/4 ∧ G ties-or-wins
  every load-bearing criterion in all 4 grids. `length_flag` when G > 1.5×R
  (2404.04475) — reported, never verdict-flipping.
- **Canary** (2410.07137): `--canary` is atomic per invocation (no persistent state —
  Sol MAJOR-3 fix): R-vs-R must not produce a ≥3/4 winner; a null artifact must lose
  4/4. Run on first use of a judge pair per task class or on a surprising verdict;
  fail → the gate is unusable for the task, parts report "ungated-vs-baseline".
- **Applicability guard** (2210.12563): no scope-matched R → no pairwise gate; the
  part runs contract-only. Reference-free "quality" verdicts are never gates.
- **Exit codes** (signal-vs-artifact doctrine): 0 better · 1 not-shown-better
  (fail-closed) · 2 refused pre-network · 3 gate-unavailable (invalid readings) —
  "measured worse" ≠ "couldn't measure".

## Sol critique adjudication

1. **BLOCKER — reference provenance optional** → adopted modified: `--reference-author`
   is now *required* (`family|human|unknown`); judge↔known-family collision refuses
   pre-network. Deviation: `unknown` runs with a `reference_provenance_unknown` flag
   instead of failing closed — reference-side familiarity bias always favors R
   (2410.21819, 2603.16197), i.e. only produces false *negatives*, the fail-safe
   direction; refusing would make the gate unusable for most real-world references.
2. **BLOCKER — 2+1 repair rounds** → adopted fully: one shared per-part budget of 2.
3. **MAJOR — canary state / tie schema** → adopted: canary is stateless+atomic; full
   JSON schema with tie semantics (deciding_criterion null iff tie; refutations always).
4. **MAJOR — `mut --scope changed` blind to new files** → adopted: the contract runs
   `--scope all` (verified: `bin/mut:331` reports "nothing to mutate" green for an
   untracked file).
5. **MAJOR — unconditional `--survivors 0` corrupts win-log** → adopted: record only
   when 1b actually ran.

## Research trail (all arXiv IDs verified live against abs pages, 2026-08-01)

**Builder/critic loops:** 2303.17651 (Self-Refine — keep the loop, swap the feedback
source) · 2310.01798 (self-correction fails without external signal) · 2305.11738
(CRITIC — ground critique in tools) · 2407.00215 (CriticGPT — critics hallucinate;
adjudicate claims) · 2511.16004 (InfCode — the strongest critic emits executable
tests; SOTA SWE-bench) · 2511.01758 (RLAC — critic proposes, validator decides) ·
2503.13657 (MAST — weak verification is a top-3 multi-agent killer) · 2509.23055
(sycophancy/disagreement-collapse) · 2510.07517 (anonymization reduces bias) ·
2306.05685 / 2404.13076 / 2410.21819 (judge biases; self-preference survives blinding).

**Baseline-anchored acceptance:** 2305.17926 (order swaps flip rankings) · 2404.04475
(length-controlled win-rates) · 2410.07137 (null models win benchmarks — canaries) ·
2502.01534 (preference leakage across model lineage) · 2210.10760 (proxy
overoptimization — cap the loop) · 2210.12563 (reference-free evaluation is gameable)
· 2602.16802 (references turn cheap judges into usable soft verifiers — the positive
case for this gate) · 2507.17746 (rubrics beat holistic) · 2501.13007 (pairwise
knockout beats absolute scores) · 2506.13131 (AlphaEvolve — loops compound only on
machine-gradeable acceptance) · adjacent: 2504.20879, 2603.16197, 2606.04923.

**Novelty:** no verified paper anticipates the full pattern (decompose → specialist
builder + blind critic per part → baseline-anchored bar). Closest composites each miss
a leg: InfCode (no blinding/baseline/decomposition), RLAC (post-training, no
decomposition), 2605.15425 (decomposition without per-part critics). The
baseline-anchored per-part bar is the novel leg — and the reason the verdict is
capped at *promotion*: an LLM-judged bar, however hardened, inherits every bias above
(2506.13131), so acceptance stays deterministic.

## Positioning v2 — incremental divider, one step BEFORE planning (same day)

Two positions were tried in one day; the second stands:

1. **v1 (rolled back): default execution shape.** Making the gauntlet the
   default *execution* wrapper after fableplan/solplan created two competing
   decomposition layers — the gauntlet's decomposer and the plan's own steps —
   i.e. it quietly became a second planning layer. Ricardo caught the
   conflation and called the rollback.
2. **v2 (current): intake divider.** The gauntlet runs **one step before
   planning**, only for broad/multi-deliverable requests: it divides the
   request into incremental, independently-shippable parts — each with a
   ratified rubric, a candidate real-world reference, and a critic mandate at
   delivery — then hands each part to the *unchanged* host-native pipeline
   (fableplan/solplan design nontrivial parts; execution routing untouched).
   Boundary in one line: **the gauntlet divides the WHAT into increments;
   fableplan/solplan design the HOW of each part.** The `gauntlet-judge`
   panel gates each increment's *delivery* where R exists (promotion); a
   Tier-2 cross-family refutation critic covers R-less parts — the panel
   never runs reference-free (2210.12563).
- Single-deliverable or trivial requests skip the divider (stated in one
  line); fanout remains the lighter path for independent chunks.
- Enforced by harness-verify: the "one step BEFORE planning" positioning
  token on the command, the skill, and CLAUDE.md, plus the divide-first line
  in the plugin defaultPrompt — the loop cannot silently drift back into a
  planning layer or an execution default.

## Verification of this feature itself

Offline behavioral contract `tests/gauntlet-judge/run.sh` (24 checks; stub judge via
`GAUNTLET_OLL` answers in blinded labels, so green exercises the unblinding
bookkeeping), proven red against two seeded pass-rule bugs (accept-at-2/4; always-pass).
`bin/mut --scope all` over the tool. Wired into harness-verify's behavioral-contract
table + a promotion-invariant token check on both host surfaces.
