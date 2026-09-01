# ETCLOVG — harness-flaw failure-attribution checklist

Source: **HarnessFix — Diagnosing and Repairing LLM Agent Harness Flaws** (arXiv:2606.06324,
2026-06-04). When a delegated task or agent run fails, attribute the failure to a **harness
layer** *before* blaming the model — most "the model is dumb" failures are actually defects in
the support layers around it. HarnessFix reports +15.2–50.0% on SWE-Bench / Terminal-Bench /
GAIA / AppWorld purely by fixing these layers. This is the taxonomy `bin/harness-verify` lints
and the `/self-improve` loop mines (step 1, MINE).

## The seven layers (E-T-C-L-O-V-G)
| Layer | What it covers | Smells in *this* repo | Where we enforce |
|---|---|---|---|
| **E**xecution | sandbox, runners, exit codes, timeouts | a `bin/` tool crashes / wrong exit code; non-idempotent `install.sh` | `harness-verify` syntax+run checks |
| **T**ool | tool/skill interfaces, arg contracts, too-many-tools | wrong-tool calls; bloated tool menus (ToolChoiceConfusion 2606.06284); `oll` model not in catalog | `oll-sync`; minimal-frontier rule in `ollama-worker.md` |
| **C**ontext | what's loaded, compaction, CLAUDE.md hierarchy | stale/missing doctrine section; context rot; over-stuffed prompt | `harness-verify` doctrine-section check |
| **L**ifecycle | orchestration, spawn/merge, deploy, idempotency | new tool not wired into `install.sh`; fan-out where single-agent wins (2606.05670) | `harness-verify` deploy-coverage check |
| **O**bservability | traces, cost notes, what-ran logs | no cost/token note; can't tell which worker produced what | `/fanout` cost-note step; `self-improve-log.md` |
| **V**erification | the checks themselves; contract adequacy | worker grades itself; loose contract (mutation survivors); self-preference selection | two-tier policy; `bin/mut`; cross-model critic |
| **G**overnance | memory rules, write-gates, supersession, sign-off | append-only memory drift; ungated belief change; no human gate on doctrine | memory governance protocol; `bin/mem-audit` |

## How to use it
1. **On any failure:** name the layer first. "The worker hallucinated a flag" is usually a **T**ool
   contract gap, not a model failure — fix the contract, not the prompt.
2. **In `/self-improve` step 1:** run `harness-verify --json` + `mem-audit --json`, bucket each
   flag by layer, fix the one with best impact ÷ blast-radius.
3. **Targeted patch, not broad rewrite:** HarnessFix's win is *step-level* attribution → a
   flaw-specific patch under a repair spec, validated against a regression guard. Mirror that:
   one flaw, one layer, one diff, verifier stays green.

## Why attribution beats outcome-only fixing
Fixing on final outcome ("it failed, try harder") churns the prompt and regresses elsewhere.
Attributing to a layer makes the fix *local and verifiable* — which is exactly what lets the
self-improvement loop stay safe (small reversible changes guarded by `harness-verify`).
