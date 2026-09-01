# Science backing for cheap-delegate — arXiv sweep 2026-08-12

Produced by a real `/research` run (deep-researcher, deepseek-v4-flash:0731); the six
load-bearing IDs below were independently spot-verified against arxiv.org/abs the same
day (titles matched claims). Structural conclusions are each corroborated by ≥2
independent papers. No dedicated 2024–2026 survey of LLM routing/cascades exists —
genuine gap, revisit periodically.

## What the literature CONFIRMS about the design
- **Cheap-first cascades with detectable failure** — FrugalGPT 2305.05176 (up to 98%
  cost cut); Conformal Cascade 2607.25018 (escalation can carry provable bounds);
  Hybrid LLM 2404.14618 (difficulty-aware cheapest-adequate).
- **Contract-first + bounded repair** — VeriHarness **2607.14167** (structured
  feedback: location + observed + admissible alternatives → +44pp; external validator
  owns acceptance/budget — maps almost exactly onto our Tier-0/repair mechanism);
  "Don't Regenerate, Debug" 2608.02712 (bounded same-lane repair beats regeneration,
  −92.8% tokens); TDD+interpreter **2511.12823** (smallest same-family model + test
  scaffold ≈98% of the largest — the cheap-lane-with-contract bet, confirmed).
- **Route on attested outcomes, never self-reports** — Provenance Paradox
  **2603.18043** (routing on self-reported quality is worse than random; attested
  outcomes work) + ACRouter 2606.22902 (execution-grounded feedback). This is
  win-log's design, vindicated: the corpus records *verified* wins only.

## What it CONTRADICTS or bounds
- **"Escalate exactly one rung" is structurally suboptimal** — **2605.06350**:
  direct-to-large beats paying the cheap lane first on hard task classes.
- **Anchoring contamination** — Pyramid MoA **2602.19509**: feeding the cheap lane's
  output to the escalated model costs up to −18pp; pass the *contract failure diff*,
  never the cheap attempt.
- **Repair games loose contracts** — Self-Repair Trap **2608.05917** + SecTDD
  2608.09740: repair optimizes the contract proxy; without mutation-tight contracts
  the ≤2-repair cap is a cost control, not a correctness control (our `bin/mut` gate
  is the countermeasure — apply it to repaired chunks, which CLAUDE.md already
  mandates).
- **Learned-router ceiling** — 2608.08265: deployable routers realize only ~7.5–14.4%
  of the oracle gap. Don't over-invest in routing cleverness; invest in contracts.

## Adopted into the playbook (2026-08-12, each pending its first local outcome)
1. Repair feedback must be structured (location + observed + admissible alternatives).
2. Escalation passes ONLY the failure diff, never the cheap lane's output.
3. Known-hard task classes may go direct-to-strong, skipping the ladder.
4. High-value repaired chunks clear `bin/mut` before acceptance (already doctrine).

## Recommended experiment (queued, not run)
Measure locally: (1) are our acceptance contracts mutation-tight (2608.05917)?
(2) what routing gain is realizable on our task distribution (2608.08265 predicts
~10–15% of oracle)? Tight + real gain → design sound; either failing → tighten
contracts / widen direct-to-strong rather than adding repair rounds.
