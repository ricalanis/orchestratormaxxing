# Loop & Graph Mastery Program — task development across Hermes, the Orchestrator, and orchestratormaxxing

**Date:** 2026-08-10 · **Status: AMENDED / PARTIALLY SHIPPED 2026-08-13 — the shared practice lifecycle and enforced run envelope are live in source; remaining W2 recovery and causal routing stay open.**
**Provenance:** commissioned by the operator from the Second Movement (ch14–18); 3 evidence agents (haiku inventory, sonnet Hermes-runtime read, opus pipeline trace) + 2 cross-family critics (Codex/GPT-5.6: 12 findings; OpenCode kimi-k2.7: 10 findings) → 22 findings adjudicated, 20 accepted (2 modified), draft materially redesigned. It builds on the 2026-08-09 integration program and reuses the canonical loop, Gauntlet, memory, and Hermes primitives rather than duplicating them.
**Evidence appendices:** `knowledge/loop-graph-mastery-appendices-2026-08-10/` — `r1-tranche-inventory.md`, `r2-hermes-runtime.md`, `r3-task-pipeline.md`, `program-draft.md` (pre-critique), `critique-codex.txt`, `critique-kimi.txt`.

## The finding in one sentence

Across all three surfaces the instruments of loop and graph engineering **exist and are disconnected from the task path** — brakes built but not wired, gates built but bypassable, checkpoints that are only phase labels, routers and experience libraries called by nothing — so on every surface, in practice, the stop is still the agent's own word.

## 2026-08-13 amendment — shipped practice meta-loop

The task path now has one canonical lifecycle: expression match → deterministic
preflight → declared four-brake envelope → typed rescue → evidence receipt.
`orchestration_practices/catalog.json` holds 20 practices across prompt, context,
harness, loop, and graph engineering; `bin/orchestration-practice` is the bounded
adapter. Matching remains advisory and carries no write, retry, acceptance, or
external-action authority.

Migration m33 adds the durable sidecar `task_run_envelopes`. A governed task
cannot be claimed while its receipt is pending/blocked; claim enforces iteration
and deadline brakes; progress enforces no-progress; objective completion remains
the existing independent contract runner. The three-completion auto-accept
shortcut was removed. Legacy tasks without a sidecar remain compatible during
rollout. `M1` envelope coverage and `C2` typed brake events are now reported in
`kpi-brief`; routing stays shadow-only until live hold-out evidence exists.

The same skill ships to Hermes, Claude, Codex, and OpenCode; Open Design receives
a capability-aware visual adapter and abstains outside that boundary. The graph
derives practice Concepts from the canonical catalog and no longer projects the
retired Initiative layer. Still open: real resumable checkpoints, orphan-run
scheduling, remaining human/legacy acceptance provenance, Hermes hard-stop
configuration, and causal route/win-log activation.

## Evidence (instrument · state · source)

| Surface | Instrument | State today |
|---|---|---|
| Orchestrator | `verification_gate` (independent agent, exit-code verdict; `governance.py:118-140`) | Correct, **bypassed by 3 paths**: `review_accept` default-scope accepts on the executor's own claim (`mcp_server.py:4609-4636`); anti-respawn shortcut auto-accepts on 3 self-written events above the gate (`loop.py:368-380`); human accept manufactures the verification row (`sprints.py:417-425`). Live: 218/218 verification rows manual; 0 `contract_run` events ever |
| Orchestrator | Contract-at-birth | **Structurally impossible**: `create_task` has no contract field; `acceptance_criteria` is prose; `set_contract` privileged + post-hoc — deliberately (worker-authored contracts can encode the worker's own defect, `governance.py:164-167`). K10 = 0/112 is architecture |
| Orchestrator | Recovery | `reclaim_orphan_runs` correct but **unscheduled** (`ORCH_SWEEPER` exists only in ORCHESTRATION.md:85); the "checkpoint" is a phase label (`plan|code|validate`) whose live pointers are then cleared (`orchestration.py:861-865`) → 125/308 runs crashed and replayed from zero. Advancement is agent-self-reported (`report_progress`) |
| Orchestrator ↔ trio | Routing + experience | `bin/ticket-route` (returns **topology + review tier**, not runtime) and `win-log match/add` called by **zero** pipeline code; runtime is a human parameter at `dispatch_task`; win-log corpus: 5 hand-written records |
| Hermes agent | Completion gate | `verification_stop.py` never blocks; the goal loop (`hermes_cli/goals.py`) is the **one** place with a separate judge; minimum missing gate is `kanban_complete` |
| Hermes agent | No-progress brake | Present, **disabled**: `tool_loop_guardrails.hard_stop_enabled: false` |
| Watcher chain | intent flow | `hermes-strategic-brief.service` lacks `WorkingDirectory` → `intent-queue` path resolution falls back to process CWD; **nothing consumes intents** (found live by the Codex critic) |
| All | Measurement layer | **Working** (last night's tranches): K9 = 0.97 cache-read share; K10 = 0/112; review-queue λ/W; `gate_cleared` tiers; win-log liveness decay; exactly-once CAS; cost ledger |

Honest-signal caveat: the orchestrator loop is dormant (last `result_reported` 2026-07-28) — volumes are historical; re-validate every rate on live traffic before ratcheting on it.

## Mastery model v2 — five gates per task, metrics redesigned after critique

- **M1 · Contract at birth, and adequate.** K10a: contract attached *at creation* by the task's **creator, never its executor** (privilege boundary stays — Codex BLOCKER #2). K10b: adequacy — sampled `bin/mut` runs plus a **two-sided anchored canary**: a `true` contract must score 0 (negative half) AND an anchored reference set (~10 sampled real tasks with human-fixed pass/fail verdicts, half each way) must be judged correctly, fail-closed (positive half). Downstream metric movement is never evidence the gate is valid — gate validity comes only from the anchor set (arXiv:2607.12790).
- **M2 · Routing followed, not filed — demoted to process metric.** Adoption (% dispatches where a `ticket-route` verdict existed and the chosen shape/review tier matched it, with recorded runtime rationale at `dispatch_task`) is *reported*, not a mastery gate: agreement between a static recommendation and a choice proves nothing about contribution (static vs causal utility near-independent, ρ=−0.026 — arXiv:2607.15253). The real gate, once live traffic resumes: a **randomized hold-out** — on a sampled fraction of dispatches, withhold the route/win-log verdict and compare delivered-on-contract rate, survivors, and cost against the routed arm.
- **M3 · Braked, resumable execution — three numbers, defined.** Crash rate (125/308 baseline); resume rate (meaningful only after real checkpoints exist — see W2); **live-stagnation rate** (leases renewing while no non-self-reported progress event lands). The Codex arm currently has *no* iteration/timeout/no-progress brake — in scope.
- **M4 · Policy-conformant acceptance.** Metric = % closures whose acceptance path **matches the `gate_cleared`-tier policy** — deterministic gate where the tier requires it, human review where policy demands a human. A required human gate is *conformance*, never "bypass" (both critics; my draft had this wrong).
- **M5 · Learning that gets reused — reuse-hit scoring demoted.** Failure half: a **task-scoped regression register** — `loop-queue` is harness-scoped by design and never sees `t_*` IDs (Kimi), so failures convert via the breaker into runnable regression entries on the task's own contract, each carrying MemOps-style trace fields (trigger, target, scope, state transition, evidence — arXiv:2607.12893) rather than prose. Success half: `win-log add` on accepted delivery. Scoring: match-hit counts prove nothing (random rules matched expert-curated at +13.8pp — arXiv:2604.11088; and static utility ≠ causal utility); the gate is **outcome differential** — win-log-biased dispatches vs the M2 hold-out arm. Never respond to low reuse by growing the corpus.

## The program — four waves (resequenced per critics: contracts BEFORE bypass-closure)

**Red-proof rule, clarified (Kimi #10):** Wave red-proofs are *harness-level test fixtures* (tests/, proven red against pre-fix code) — task-level contracts don't exist until W1 delivers them.

### W0 · REPAIR — preconditions the critics surfaced *(mostly AUTO: internal, reversible, red-proofable)*
- **R1 — ✅ DONE 2026-08-10 (commit 54fc8db).** Root cause was *not* the unit: `deploy/hermes-strategic-brief.sh` never did `cd "$REPO"` (its sibling `loop-cron.sh` does, line 66), so systemd started it in `$HOME` and `intent-queue`'s cwd fallback wrote the queue outside the repo — the repo's own `knowledge/intent-queue.jsonl` had never existed. Fixed at the wrapper (fail loud, not open), plus `WorkingDirectory=__REPO__` on the unit, plus the `__REPO__` substitution `install.sh` was missing for that template (without which the unit would deploy a literal token and fail to start), plus a `harness-verify` assertion — proven red — that any `deploy/*.sh` wrapper invoking a repo-scoped tool must cd. Verified live: the armed unit now enqueues into the repo. Incident + archived evidence: `knowledge/incidents/stranded-intent-queue-2026-08-10.md`.
  **Consumer wired 2026-08-11 (the operator chose the daily brief).** Open SELECT intents now surface in the 3×-daily ritual's "⚠️ Needs you" block (`orchestrator/dashboard/brief.py`): the count on the summary line, plus one bullet for the *oldest* carrying its age. AUTO intents are deliberately excluded — they are, by definition, work an unattended round may take, and putting them in a human's needs-you block trains him to skim the one block that must never be skimmed. Because `intent-queue add` is idempotent while an item is open, that age is genuinely first-seen, which is what stops a chronic signal from re-reading as new every Monday. Queue path is a repointable module attribute (`brief.INTENT_QUEUE_DIR`), never cwd — the same class of bug as the incident itself, and every brief test is now isolated against it. Contract: 11 assertions proven red pre-change in an isolated worktree; `bin/mut` 0.91 (thr 0.85). The evidenced first-seen date of `iq-97c28640` was restored from the archived queue, so the brief reports 21d rather than the 0d the bug's data loss would have implied.
- **R2** Repair the respawn guard properly (3,950 guarded respawns) — precondition for touching the anti-respawn shortcut (Codex #4).
- ~~**R3** Fix the corrupted `trust_grade_for` signal.~~ **WITHDRAWN 2026-08-10 — this item was wrong.** Verification found no local defect: `trust_grade_for` (`object_graph.py:586-605`) computes `done`/`failed` identically to `get_agents`. The real finding is that trust is *derived from* `status='done'` counts which the three acceptance bypasses inflate — a downstream consequence of C2, with no independent fix ("Fix #1/#2 first; this gap's value depends on the `done` signal meaning something" — r3:359). Trust integrity is now listed as a C2 benefit, not a W0 repair. Filed-then-corrected: `lq-dd6d56c6`.

### W1 · CONTRACT SPINE — the structural build, now first *(SELECT: schema + acceptance semantics)*
- **C5a** Creation verbs (`create_task`, TaskCreate path) accept a typed runnable contract **authored by the creator** (human / orchestrator / router). `set_contract` stays privileged; executors can never author their own gate.
- **C5b** Contract **execution**: the review path invokes `run_contract`; executor instructions teach the verbs (`claim_next`, `report_result` + contract semantics) — today no executor instruction mentions them (Codex BLOCKER #1). K10a/K10b go live.

### W2 · CONNECT — activation, in dependency order
- **C1a** Schedule `reclaim_orphan_runs` (systemd user timer per `deploy/` convention — external unit ⇒ **SELECT** to arm, files AUTO to stage).
- **C1b** Real checkpoints: enrich `task_runs` so a reclaimed run can *resume* instead of replay-from-`plan` — the current phase label is not a checkpoint (Codex #5). Adopt the six-property machine-checked resume contract from arXiv:2608.03836 as the spec (the paper shows LangGraph/CrewAI/pydantic-graph all violate their own resume claims — hand-rolled prose contracts fail here). Structural; red-proof: kill → resume, not replay, checked property-by-property.
- **C2** Close the three acceptance bypasses — **after C5b exists** so the gate has something deterministic to run (Kimi BLOCKER #2), and after R2. Human accept records `human_override` provenance. SELECT. **Second-order benefit (ex-R3):** `trust_grade_for` feeds the auto-accept dial from `status='done'` counts that these same bypasses inflate, so the one live feedback loop is currently trained on unverified claims — closing the bypasses is what makes the trust signal mean something. No separate fix exists or should be attempted.
- **C3** Hermes agent: blocking gate on **`kanban_complete`** (the minimum missing gate — the goal loop already has a judge; Codex #6) + enable `tool_loop_guardrails.hard_stop` — both only via `hermes config set` with the config-guard live. SELECT.
- **C4** Wire the orphans at the right hop: `dispatch_task` consults `ticket-route` (shape + review tier) and `win-log match` (evidence bias), records verdict + rationale; accepted deliveries call `win-log add`. Adoption measured per M2. Recording AUTO; acting on verdicts SELECT.

### W3 · RATCHET — aim the loop at the factory
- **C6** M1–M5 land beside K9/K10 in `kpi-brief`; `hermes-watch` watches them (viable only after R1); monthly DAKI reflect routes recurring friction to product/system/harness; ratchet rule — a strategic round must move an M-number, green alone is nothing. **Warrant-aware crediting (arXiv:2607.13083):** an accepted round — and specifically any *new assertion* it adds — must cite the specific observed failure it removes (an lq-ID, a `t_*` regression entry, or a proven-red repro), verified against that evidence; an add-only accept-if-not-worse loop otherwise accumulates fixes for failures that never happened (15/60 fabricated in the paper's benchmark; 0/60 with warrants). This tightening applies to the existing `/self-improve` ratchet too — SELECT, since it edits ratchet semantics.
- **Named-gaps register** (so nothing drops silently — Kimi #7): Codex-arm brakes (unbounded time today) · occ/OpenCode ↔ kanban wiring (occ is currently outside the task pipeline entirely) · Hermes skill-curation/GEPA scoring (dormant on this install) · typed graph payloads for Kanban `task_links` (the environment's nearest real DAG) · **per-task gates are provably blind to compositional outcomes** (under ε-local indistinguishability any single-observation rule has detection advantage ≤ ε — arXiv:2607.11751; per-step detectors fall to chance under splitting): when C4 lands, `dispatch_task` records each task's link-set so an assembled-object view stays possible later; no compositional monitor is built now.

## Per-surface mastery meaning
- **Hermes agent** — a well-contracted **node**: brakes on (C3), clean inputs/exit codes; the goal loop is the in-house template for "done is not my word."
- **Orchestrator** — owner of the **typed state** (the task object) and of gates M1/M2/M4 + all telemetry; the graph lives here (`task_links` + dispatch topology).
- **orchestratormaxxing trio** — Claude orchestrates and signs off; Codex and OpenCode become routable executors whose dispatches quote the task contract verbatim (occ/codex-rescue), with the trio's instruments (fanout, Workflow, gauntlet, mut) as the shape-and-verification vocabulary `ticket-route` selects among.

## Adjudication record
Codex 12 findings: 11 accepted (3 BLOCKER, 8 MAJOR), 1 modified (M2 metric redefined rather than dropped). Kimi 10: 9 accepted, 1 modified (M5 kept, rescoped to reuse-scoring). Convergent independent catches (sequencing inversion, C1 misclassification, ticket-route placement, all four M-metric Goodhart holes) were treated as corroboration and re-verified against r2/r3 evidence — never as proof by agreement.

## External evidence pass (2026-08-10)

An arXiv sweep (20 verified entries: 15 enrich-side, 9 combat-side, overlap 4; 6 full-text reads; full verdict table in appendix `r4-arxiv-pass.md`) was run against the program with a symmetric enrich/combat mandate. The x.ai API was re-tested the same day: still 403 (credits/spending cap, unchanged since 2026-07-03) — no X-native pass possible.

Six changes it forced or supplied, all folded into the sections above:
1. **W3/C6 — warrant-aware crediting** (arXiv:2607.13083 "Phantom Guardrails", COMBATS): add-only accept-if-not-worse ratchets fabricate fixes for failures that never happened (15/60 → 0/60 with warrants). New assertions must cite the observed failure they remove. Applies to the existing `/self-improve` ratchet as well.
2. **M1b — two-sided anchored canary** (arXiv:2607.12790 "Who Grades the Grader?", ENRICH+COMBAT): a collapsed always-pass gate trains skills just as well, so downstream score can never validate a gate; validity needs an anchored reference set, fail-closed, plus the negative canary.
3. **M2 — demoted to process metric** (arXiv:2607.15253 "Bridge Evidence", COMBATS): static and causal utility are near-independent (ρ=−0.026); the outcome gate becomes a randomized hold-out arm on live traffic.
4. **M5 — reuse-hit scoring dropped** (arXiv:2604.11088 random-rules parity +13.8pp, COMBATS; plus #3): outcome differential vs the hold-out arm instead; regression entries carry MemOps trace fields (arXiv:2607.12893).
5. **C1b — formal resume contract** (arXiv:2608.03836, ENRICH): six machine-checked resume properties as the checkpoint spec; major frameworks violate their own resume claims, so prose contracts are insufficient here.
6. **Named gap — compositional blindness bound** (arXiv:2607.11751, COMBATS-scope): per-task gates cannot see harm that exists only in composition (advantage ≤ ε); record link-sets at dispatch, build nothing more now.

Queue-lead closures recommended by the pass (research trail in `r4-arxiv-pass.md`): lq-880b8d09, lq-bb67c677, lq-e37e484d, lq-0837e2d0 — all four answered relative to this program; one new independent item queued (stale-after-supersede recall probe for `mem-audit`/`harness-verify`, from the MemOps analysis).

## Not redone here
The 2026-08-09 tranches (K9/K10, review-queue module, `gate_cleared`, win-log decay, consume-once, CAS, cost ledger) and their 4 PENDING-operator SELECT items stand untouched.

## Dissent to hold
Activating gates on a dormant loop can manufacture friction with no traffic to justify it — W0/W1 are safe to build dry, but ratchet numbers (W3) wait for live traffic. New metrics invite Goodhart even after redesign — the counter-metric pairing and reported-only-until-ratified rule apply to M1–M5 exactly as they did to K-metrics. And the program's own premise should be re-tested after W1: if contract-carrying tasks don't change outcomes on live traffic, the connect wave gets re-scoped before anyone builds W2.
