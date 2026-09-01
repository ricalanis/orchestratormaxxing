# Model-eval doctrine — derived from refuting our own eval, 2026-08-27

The 2026-08-26 model comparison picked a winner. Asked whether that winner was a
property of the models or of the evaluator, the honest answer was **partly the
evaluator**. This note records the critique, what survived adjudication, and the
mechanism that now prevents each failure. Memory: `model-eval-design-doctrine`.

Method: the full methodology was handed to a **different model family** (Sonnet,
via `provider-ask anthropic`) framed as *another engineer's* work with a mandate
to refute — the harness's own anti-self-review rule. Its statistics were then
verified independently rather than accepted.

## What was actually wrong, adjudicated

| # | Critique | Verdict |
|---|---|---|
| 1 | **Post-hoc task exclusion.** The code-review class was dropped *after* seeing scores, with a rationale never stated before the run. | **Upheld, and worse than I framed it.** Sonnet caught what I missed: the excluded class is *exactly* the one whose contract was known-broken and whose scores a config artifact had already muddied. Including it flipped the winner (92% vs 83%). Defensible-in-isolation does not save a criterion applied after looking at which arm it helps. |
| 2 | **Effort pinned on the wrong evidence.** `low` was chosen from the invented tasks; the later sweep on the *real* task showed correctness flat across low/medium/high/max. | **Upheld.** The pin isn't wrong, it was unjustified by the evidence gathered for it. |
| 3 | **Latency contaminated.** A ~20 s TTFT stall hit all arms, yet "2.8 s vs 3.6 s median" still fed the decision. | **Partly upheld.** Decode rate *was* TTFT-stripped, but the wall-clock medians in the decision table were not. |
| 4 | **Contracts authored and graded by the decision-maker**, with a demonstrated false negative. | **Upheld.** |
| 5 | **The one real-task eval had no discriminating power** — every model dodged the trap. | **Upheld; I had said so myself.** |
| 6 | **Selective retry.** Two 429s were retried rather than scored. | **Partly upheld.** Retrying infra is right (a transport failure must not demote a lane), but at n=3 it must be retried to a *fixed count of decided trials* and the infra rate reported per arm. |
| 7 | **All non-production tasks invented**, not sampled from real traffic. | **Upheld.** |

### Self-refutation found independently

I reported **min tokens per cell** in the effort table. On n=2 that flatters any
trend. Re-checked against non-overlapping ranges: the headline survives
(`v4-flash` `none` 215–279 vs `low` 1570–2360 — 6–11×, disjoint; `glm-5.2` `none`
146–148 vs `max` 1082–1155 — disjoint), but the implied *fine ordering* does not
(`glm-5.2` `low` [1216, 252] and `medium` [256, 676] overlap completely).

## The numbers that make n=3 indefensible

Verified independently, not taken on the critic's word:

- Clopper–Pearson 95% CI: **9/9 → [66.4%, 100%]**, **8/9 → [51.8%, 99.7%]**.
  Nearly total overlap; Fisher exact p ≈ 1.0. One flipped trial swings a 3-rep
  cell by 33 points.
- Separating a true 98% from a true 89% at α=0.05, power 0.8 needs
  **n ≈ 117 per arm** — about 35× what was run.
- 3 reps of one prompt at temperature 0 are not 3 draws of capability; they
  mostly measure decode stability. Effective sample size is smaller than it looks.

## The mechanism: `bin/model-eval`

Each rule is enforced by the tool, not by remembering to be careful.

| Rule | Mechanism |
|---|---|
| Pre-register before the first call | `preregister` writes + hashes the spec; the hash rides in every result row. **There is no exclusion flag.** |
| Golden-test contracts first | `golden` scores hand-written correct-but-differently-worded answers; any failure **aborts before a single model call** (exit 2). |
| Distinct instances, not reps | Generators emit N *different* problems per class. Reason puzzles are brute-forced here, so only unique-solution instances ship and ground truth is derived, never assumed. |
| Paired + interleaved | All models see the same instances; job order puts models adjacent in time so a load spike is spread across arms. |
| Exact statistics | Clopper–Pearson CIs (normal approximations are invalid at k=n, which is where routing evals live) and exact McNemar on discordant pairs. |
| Ties go to the incumbent | The verdict prints `KEEP INCUMBENT` when CIs overlap or McNemar p ≥ 0.05. A larger point estimate is not a win. |
| Infra never silently dropped | Retried to a fixed count of decided trials; infra count reported per arm. |
| TTFT separate from decode | Reported as distinct metrics, with the note that a stall shared by all arms is provider load. |
| Quota per unit of *useful* work | Reports **tokens per decided pass**, not tokens per call — the scarce resource on a flat subscription. |

**The golden gate was proven red**: reintroducing the historical single-literal
`"retry"` contract makes it reject all five hand-written correct answers and exit
2 — it would have refused to spend a single call on the broken checker that
corrupted the original eval.

## Still not done (honest limits of the current tool)

- **Instances are synthetic-but-varied, not sampled from production logs.** The
  harness logs token usage (`~/.local/share/orchestratormaxxing/oll-usage.jsonl`) but
  not prompts, so there is no corpus to sample. Real-traffic sampling needs a
  prompt log first.
- **No shadow-traffic soak.** A days-old model still should not enter the
  digestion pipeline on synthetic evidence; that requires routing a slice of real
  transcripts and comparing validator pass rates (n ≥ 100 real events).
- **Not tested:** long-context degradation for the roles named for it,
  concurrent-load contention, refusal/safety false positives on real transcript
  content, and a frozen golden set re-scored each cycle to detect silent
  provider-side model drift.
- **n = 20** is a budget compromise, adequate to reject small differences but
  not to *establish* one; the stopping rule (ties → incumbent) is what makes that
  safe rather than merely cheap.
