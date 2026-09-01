# Hermes: Operative → Tactical → Strategic — Evolution Plan

**Date:** 2026-07-10
**Source:** 4-expert council (strategy / proactivity / KPI / memory) synthesizing
`knowledge/research-agent-orchestration-proactivity-2025.md` (20+ arXiv papers) against Hermes'
*actual* infrastructure. Council specs: `scratchpad/council-{strategy,proactivity,kpi,memory}.md`.
**Governing rule:** *extend what exists, never invent a parallel system.* Every component below
maps to a real file / endpoint / verb. Doctrine (CLAUDE.md) is binding: deterministic gates,
selection-never-self-preference, unattended stops at SELECT, minimal fan-out, governed memory.

---

## The one idea

The three layers are **not three agent teams**. They are **one orchestrator reasoning at three
decision frequencies over three state scopes** — temporal abstraction (arXiv 2606.20014). This is
what keeps the evolution inside "default single-agent; fan out only on independent chunks."

| Layer | Frequency | State scope | Implemented by (exists) | New |
|---|---|---|---|---|
| **Operative** | per-task | one file/chunk | `ollama-worker`, kimi/glm-coder, Claude Code; `claim_next→report_result`, `accept_task` | — (complete) |
| **Tactical** | daily tick + weekly roll | active cycle | today: 4 uncoordinated crons (standup/wrap-up/decay/roll) | `bin/hermes-tactical` sequencer + 1 jobs.json job |
| **Strategic** | weekly + monthly | global (all KPIs, roadmap) | today: **missing**; data exists (scorecard/velocity/pipeline-math/cltv-cac/funnel/icp/lakehouse) | a jobs.json "Strategic Review" prompt over `bin/kpi-brief` |

**Signals compress UP** (task `report_result` → cycle `wrap_day`/`finish_sprint` → `get_velocity`/
`scorecard` → strategic reads them). **Directives expand DOWN** (`cycle.goal` + active initiatives →
biased `get_day_plan_candidates` → `claim_next`/delegate). The two loops never share a decision
step (state asymmetry) — the safety property from 2606.20014 / 2601.09295.

**Crucially, the strategic layer sets direction, never executes.** Its durable output is
`update_initiative`/`create_initiative` mutations (audited in the existing `initiative_events`
spine) and the `cycle.goal` string that `create_cycle` already accepts — **no new objective store.**

---

## The four subsystems (from the council)

### 1. Proactive initiator (proactivity spec)
- **Sibling queue, not loop-queue.** `bin/intent-queue` over `knowledge/intent-queue.jsonl` reuses
  loop-queue's *mechanism* (atomic JSONL, content-hash idempotency, gate exit-code) but stays
  business-scoped — because loop-queue's `status --gate` fires `/self-improve`, and a business
  intent must fire a *proactive* round; and ETCLOVG layers don't describe a stale deal.
- **Watchers** are pure reads of already-computed signals (pipeline-health red count, stale deals,
  unscored leads, velocity gap, funnel-snapshot-missing, touch overdue, approaching Claude limit).
  On-demand perception (2512.06721), never busy-poll.
- **Gate:** v1 deterministic heuristic `U = urgency + recency − fatigue`; v2 a reward model trained
  on Ricardo's accept/dismiss history (the queue is the training log) — 2410.12361. Selection stays
  the gate, never self-preference.
- **Load-bearing split (the safety boundary, set deterministically by the watcher, never LLM-inferred):**
  - **AUTO** (internal derived state, idempotent, reversible): `score_all_leads`, `capture_funnel`, `crm/decay`.
  - **STOP AT SELECT** (external / irreversible / human-authored): touch a deal / set `next_touch_date`,
    *send* nurture (drafting is fine), sprint re-scope, roadmap change, spend/routing throttle.
- **Idle-time compute** (2605.25971): when no user message + no active task — pre-score leads,
  pre-capture funnel, pre-triage, pre-draft nurture, daily long-term-intent recheck. All compute-only, never send.

### 2. KPI closed-loop (KPI spec)
- **10 KPIs, every one grounded** in an existing source (see the KPI table in `council-kpi.md`):
  K1 velocity, K2 delivery-rate (`cycle_velocity` VIEW), K3 CLTV:CAC, K4 pipeline coverage,
  K5 lead-score coverage, K6 touch-cadence, K7 scorecard hit-rate, K8 cost-per-task, K9 tokens/session,
  K10 worker contract-adequacy (mutation-kill rate).
- **The loop is the existing `/self-improve` round at a strategic cadence** with a KPI gate on EVALUATE
  — not a new loop. KPI-green = the *targeted* KPI beats a **frozen trailing-window baseline** AND
  `harness-verify` stays green.
- **Ratchet extension (third axis):** a *strategic* round must move a business KPI or be rejected
  (green ≠ progress). Anti-gaming: a **deterministic query** measures (never the model), baseline
  snapshotted pre-round, credit **deferred to the next window** (defeats Goodhart), each target
  ships a **paired guard KPI** (velocity↔delivery, cost↔delivery, coverage↔contract-adequacy).
- **KPI → lever → file** (TPGO 2604.20714): flagship is **K10 mutation-scores → `provider-routing.md`**
  (numeric feedback rewriting a text table; `bin/mut` is the deterministic gate). Load-bearing levers
  (routing, agent prompts, scorecard targets) stop at SELECT → PROPOSED.

### 3. Experience library (KPI + memory specs)
- Mine **wins**, not just failures. The substrate **already exists in the graph** (ledger `passed`,
  sprint `outcome`). `knowledge/orchestration-wins.jsonl` + `bin/win-log add|match` records
  KPI-gated accepted trajectories ("`fanout-2` on {glm-5.1, qwen3-coder} → 0 survivors at $0.10").
  Surfaced at PROPOSE to bias fan-out shape + worker choice **from evidence** (SiriuS 2502.04780) —
  a learned delegate/keep table, not a static one. Entry gated by the deterministic mut/KPI result,
  never self-report; a win whose KPI later regresses is demoted.
- **VALIDITY-FIRST / WEAKEST-SUFFICIENT:** `win-log match` compares only delivered, zero-survivor
  wins for the same nonempty contract. Complete finite ledgers of assumptions, exceptions/special
  cases, and supported states/inputs form an abstaining partial order: prefer a candidate only when
  it is no more specific on every dimension and strictly less specific on one. Missing or
  incomparable evidence returns `review-required`/`incomparable`; cost breaks ties only for
  identical evidence. This does not estimate Bennett weakness: the theorem assumes a finite
  enactive formalism and uniform tasks, so Hermes uses the idea only as a post-validity review
  tie-break, never instead of KPI/contract gates or the AUTO/SELECT safety boundary.

### 4. Memory for strategy (memory spec)
- Keep the **vault (governed belief) + graph (recall index)** split; don't invent a third store.
- **Long-term intent:** a `hermes-quarterly-intent.md` vault fact (`type: project`, valid-time =
  quarter) + graph node; strategic cadence stamps `last_verified`; `mem-audit` flags it stale.
- **Strategic memory:** live ICP/roadmap/CRM stay authoritative in their DBs; only *derived beliefs*
  (competitive landscape, CLTV drivers) go to vault `reference` (30d decay), critic-gated.
- **Role memory:** per-`Agent` graph grade built *from* mutation/delivery; stable lessons promoted
  into `.claude/agents/<role>.md`, dynamic ones recall-at-dispatch (minimal frontier).
- **The memory→strategy bridge is a pure wiring gap** (highest-leverage, cheapest): `recall`/
  `contradiction_check`/`find_related` are exposed (api.py) but *not called* in the task/deal create
  paths. Wire them: task-create → recall related + winning shape + contradiction-check the premise;
  deal-stage-change → recall account/deal-type knowledge into coaching; cycle-planning → recall intent + wins.

---

## Phased rollout

### Phase 1 — quick wins (shipped in this session)
All additive, read-only or propose-only, no live mutation, no auto-execute:
1. **`bin/kpi-brief`** — deterministic strategic KPI brief (reads the dashboard endpoints; prints
   K1–K7 value-vs-target + gaps). The strategic layer's *data foundation* and the KPI loop's *measurement*.
2. **`bin/intent-queue`** — the sibling business-intent queue (add/list/status/resolve, content-hash
   idempotent, `status --gate` exit-code). Proactivity's forward state.
3. **`bin/hermes-watch`** — business watchers → `intent-queue add` (pipeline-health, stale deals,
   unscored leads, velocity gap, funnel-missing, overdue touches). Sets `load_bearing` deterministically;
   proposes only, never executes.
4. **`bin/win-log`** — the experience library (`orchestration-wins.jsonl`, add/match).
5. **`/self-improve` ratchet extension** — the third (KPI) axis documented; `kpi-brief` named as the
   deterministic measurement.
6. **`deploy/hermes-strategic-brief.{service,timer}`** — a weekly (Mon) strategic-brief unit,
   version-controlled + install.sh-wired but **opt-in armed** (per the deploy doctrine).
7. All wired into `install.sh` (global) + added to `harness-verify`'s coverage; listed in CLAUDE.md.

### Phase 2 — the Tactical Controller
- `bin/hermes-tactical tick` — a deterministic sequencer (like `loop-tick`) running the SOP
  `plan → triage → execute → review → ship → roll` over real verbs (`roll_cycle`/`create_cycle(goal)`
  → `get_stale_deals` → `claim_next`/delegate → `accept_task` → `wrap_day` → `finish_sprint`).
- Collapse the 3 time-crons (standup/wrap-up/decay) into **one** jobs.json interval job that calls it.
- Wire the memory→strategy bridge into task-create + deal-stage-change (the cheapest high-leverage win).

### Phase 3 — the Strategic Planner + full KPI loop
- A "Strategic Review" jobs.json prompt (weekly/monthly, kept in Opus/Hermes) that reads `kpi-brief`
  + lakehouse + initiatives and **emits objectives** as PROPOSED `initiative_events` + `cycle.goal`
  suggestions (never auto-committed).
- Staple the KPI gate to `/self-improve`'s EVALUATE (frozen baseline, deferred cross-window credit,
  paired guard KPI). Turn on K10 → `provider-routing.md` as the first automatable lever (PROPOSED diffs).
- Promote role-memory lessons into agent prompts; wire the intent-queue gate v2 (reward model from the log).

---

## What NOT to build (anti-goals)
- **No core-code self-rewrite.** The Darwin-Gödel end (2505.22954) is a north-star *anti-goal*: Hermes
  evolves prompts / routing / memory, **never the verifier**. `harness-verify`/`mut` stay the arbiters.
- **No self-preference grading.** Selection is always a deterministic query over data the model didn't
  author; agreeing LLM critics are corroboration, never proof.
- **No unbounded autonomy.** Load-bearing actions (send/publish, mutate business records, edit routing/
  doctrine/install.sh) always stop at SELECT → PROPOSED for human sign-off.
- **No parallel systems.** No new objective store (use initiatives + cycle.goal), no third memory system
  (use vault + graph), no second self-improve loop (extend the round). Reuse mechanism, never conflate scope.
- **No dumb-clock proliferation.** New cadences are event-gated (act on real signal), not timers that
  manufacture dry, reward-hack-prone rounds.
- **No giant tool/skill frontier per worker** (arXiv:2606.06284) — hand each role its minimal next-step tools.
