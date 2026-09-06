---
name: gauntlet
description: Gauntlet Loop — incremental division of a broad request, one step BEFORE planning. A worker divides the goal into incremental, independently-shippable parts, each with a ratified rubric, a candidate real-world reference, and a blind critic mandate at delivery. It divides the WHAT; astraplan designs the HOW of each part — it never plans implementation. Use for broad/multi-deliverable requests; single-deliverable work goes straight to astraplan.
---

Root Codex divides a broad request into incremental parts — **one step BEFORE planning**; `$orchestratormaxxing:astraplan` still designs each nontrivial part, unchanged. Invariant: **the gauntlet verdict promotes, never accepts** — deterministic contracts (tests, `mut`) are the only acceptance gate. Protocol + research: `knowledge/gauntlet-loop-design.md`.

1. **Fire or skip.** Broad/multi-deliverable request → divide; single-deliverable or trivial → skip to normal routing, saying so in one line.
2. **Divide via a worker, not inline:** `oll "<divide brief>" --model deepseek-v4-pro` → 3–7 incremental parts, each independently shippable, ordered so every increment lands value on its own; per part: artifact spec, candidate real-world reference R + provenance, 5–8 candidate boolean criteria.
3. **Ratify (Tier-0):** root Codex fixes order + rubric (marks load-bearing, writes JSON) + deterministic contract per part. No scope-matched R → the part is "ungated-vs-baseline" at delivery but keeps rubric + critic mandate.
4. **Hand off unchanged:** nontrivial part → `$orchestratormaxxing:astraplan` then build. A small response-only artifact may use `oll`; workspace code uses `$orchestratormaxxing:cheap-delegate` or `o delegate <run-id> --profile bounded-code|reasoning|long-horizon|general --run-dir .results/delegation/<run-id> --json`, followed by `o close <session>` on every terminal path. Land increments in order.
5. **Accept deterministically** (contract, `mut` for risky parts). One shared repair budget of 2 rounds per part across contract failures and gauntlet refutations.
6. **Gate delivery:** where R exists, `gauntlet-judge --artifact <G> --reference <R> --rubric <rubric.json> --builder <model> --reference-author <family|human|unknown> --json` — read only the JSON; exit 1 → refutations are claims: adjudicate deterministically, at most one targeted-assertion repair from the shared budget; never re-roll judges seeking a pass; exit 3 → report unavailable; first use of a judge pair or surprising verdict → `--canary` first. No R → Tier-2 cross-family critic (external framing), never the panel reference-free.
7. **Report** per increment: contract, mut score, verdict + flags, R provenance, repair rounds. `win-log add --shape gauntlet-N` only when mutation actually ran clean.
