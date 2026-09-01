# Growth Operating Framework — Implementation Tasks

**Role: Development Lead.** Concrete code breakdown for
`growth-operating-framework.md` (framework) + `growth-dashboard-design.md` (UX).
Design docs are the spec; this file is the work queue. No code here — tasks only.

**Conventions (from the existing codebase):**
- Schema: idempotent `ensure_*_schema()` in `dashboard/db.py`, additive `ALTER TABLE`
  only, called from `growth.ensure_schema()`; indexes for every FK/lookup.
- Writes: one validated write path per mutation in `crm.py`/`growth.py`; every mutation
  emits a `deal_events` row; closed vocabularies as module tuples validated at write.
- API: FastAPI routes in `dashboard/api.py`, thin — parse/validate → module fn →
  error envelope. MCP: entry in `TOOLS` list + dispatcher case in `mcp_server.py`.
- UI: section `<div id>` + `loadX()` in `templates/index.html`; Tailwind zinc idiom;
  after UI changes restart `hermes-dashboard.service` (templates are fresh, Python
  isn't). Verify: `py_compile`, dashboard serves, new endpoints respond.

Batches are dependency-ordered; tasks within a batch are parallelizable.
Sizes: S ≤1h · M ≤3h · L ≤1 day.

---

## Batch 0 — Schema (db.py + crm.py/growth.py migrations)

**T0.1 (M) — `events` + `event_attendance` tables.**
`db.ensure_events_schema()`:
```
events(id TEXT PK 'ev_…', name TEXT NOT NULL, event_date TEXT,  -- ISO date
       kind TEXT,            -- conference|meetup|online|clinic|other
       location TEXT, cta TEXT, prep TEXT,   -- prep = JSON {targets[],relationships[],connectors[]}
       notes TEXT, created_at INTEGER)
event_attendance(id TEXT PK 'ea_…', event_id TEXT NOT NULL REF events,
       contact_id TEXT NOT NULL REF contacts, deal_id TEXT,
       captured_at INTEGER)   -- + UNIQUE(event_id, contact_id)
```
Indexes: `event_attendance(event_id)`, `(contact_id)`. Accessors: `events_all`,
`event_get/insert/update/delete`, `attendance_insert`, `attendance_for_event`.
*Accept:* py_compile; double `ensure` idempotent; UNIQUE dedupes re-capture.

**T0.2 (S) — contact capture columns.** In `crm.ensure_schema()` additive block:
`contacts.tier TEXT` (validate `hot|warm|long_term` at write), `contacts.problem_statement
TEXT`. Extend `create_contact`/`update_contact` signatures + validation tuple
`CONTACT_TIER`. *Accept:* create/update round-trips both fields; invalid tier → error.

**T0.3 (S) — nurture sequence typing.** `db.ensure_nurture_schema()` additive:
`sequence_type TEXT DEFAULT 'hook'` (`hook|cadence_3_10_30_90|teaching`),
`channel TEXT`. `nurture_for_deal` returns them; new `db.nurture_steps_due(date)` →
pending steps with `scheduled_date <= date` joined to deals/contacts (for C2).
*Accept:* existing hook rows read back as `'hook'`; due query returns joined rows.

**T0.4 (M) — `proposals` table.**
```
proposals(id TEXT PK 'prp_…', deal_id TEXT NOT NULL REF deals,
  status TEXT DEFAULT 'draft',   -- draft|sent|accepted|rejected
  option_bueno TEXT, option_mejor TEXT, option_optimo TEXT,  -- each JSON {product_id,price,scope}
  accepted_option TEXT, reason TEXT,
  created_at INTEGER, sent_at INTEGER, decided_at INTEGER)
```
Index `proposals(deal_id)`. Accessors + `proposal_for_deal` (latest). *Accept:*
lifecycle draft→sent→accepted persists; one active proposal per deal enforced in code.

**T0.5 (S) — `channels` table + seed.**
`channels(id TEXT PK 'ch_…', key TEXT UNIQUE, label TEXT, kind TEXT, active INTEGER
DEFAULT 1, notes TEXT, created_at INTEGER)`; seed 5 rows matching `LEAD_SOURCE`
(linkedin/evento/referral/cold_email/inbound) + whatsapp/email as touch channels.
*Accept:* seed idempotent (INSERT OR IGNORE by key).

**T0.6 (S) — `monthly_reviews` table.**
`monthly_reviews(month TEXT PK 'YYYY-MM', metrics TEXT NOT NULL /*JSON*/, decisions
TEXT, created_at INTEGER, closed_at INTEGER)` + upsert/get accessors. *Accept:*
close→reopen forbidden (closed row immutable except decisions until closed).

---

## Batch 1 — Backend logic (growth.py / crm.py)

**T1.1 (L) — quick capture write path.** `growth.quick_capture(name, company, email,
linkedin_url, problem_statement, event_id, tier, channel)`: find-or-create account →
create contact (tier/problem/source) → attendance row (if event) → create deal
(`lead`, `lead_source=channel`, growth_loop heuristic) → generate cadence (T1.2)
unless tier=hot → emit `deal_events('captured_at_event')`. Returns the C1 contract
shape incl. `next_action`. Builds on existing `quick_add_lead` (extend, don't fork).
*Accept:* one call creates all rows atomically; duplicate email/linkedin returns
`{"duplicate": contact}` without writes; hot skips cadence.

**T1.2 (M) — 3-10-30-90 cadence generator.** `growth.generate_cadence(deal_id,
start_date=None)`: 4 `nurture_sequences` rows, `sequence_type='cadence_3_10_30_90'`,
scheduled at +2/+10/+30/+90 days, step labels + default channels per framework §Phase 2;
long-term tier → day-90 step only. Reuses `_render_hook_template`-style placeholder
rendering with contact problem/event name. *Accept:* dates correct across month
boundaries; regenerating wipes only pending cadence steps (sent history kept).

**T1.3 (M) — teaching funnel generator.** `growth.generate_teaching_funnel(deal_id)`:
5 weekly steps, `sequence_type='teaching'`, Spanish templates from framework §Phase 2
(Problema/Lente/Método/Casos/Decisión) rendered with `{name},{industria},{problema},
{evento},{producto}` from deal/contact/ICP/products. *Accept:* placeholders all
resolve or fall back to `[…]`; templates match the framework doc copy.

**T1.4 (M) — follow-up queue read model.** `growth.followups_today(date=None)`: due +
overdue pending steps (T0.3 query) + hot-uncontacted-72h alerts (tier=hot, deal
touch_count=0, created>72h) → C2 contract, sorted overdue→today→alerts. Marking sent
stays the existing nurture PATCH + `record_touch` (compose in the API layer, one
transaction). *Accept:* overdue days computed vs scheduled_date; empty → `items: []`.

**T1.5 (S) — cadence compliance.** `growth.cadence_compliance(deal_id=None)`:
per-sequence % sent within ±2d of schedule (elapsed steps only) + aggregate; wire the
🔴 >7d-overdue flag into `pipeline_health` reds. *Accept:* matches hand-computed
fixture; deal with no cadence → None (not 0%).

**T1.6 (M) — discovery composite + auto-fetch trigger.** `growth.discovery_state
(deal_id)`: scheduled event, latest fireflies signals, score before/after (persist
`score_before` in the recompute event payload), routing verdict (>60/30–60/<30) → C5
contract. *Accept:* each call-state (none/scheduled/fetched) renders a distinct shape.

**T1.7 (M) — proposals logic.** `growth.create_proposal(deal_id, options)` /
`decide_proposal(pid, accepted_option|rejected, reason)`: validate 3 options against
product catalog; `sent` sets deal stage→`proposal` (scorecard KPI fires via existing
stage_changed event); `accepted` sets deal value + `value_ladder_stage` from option's
product rung and spawns next-rung child deal per framework §Phase 4. Profile→track
suggestion helper `suggest_options(deal_id)` for the builder prefill. *Accept:* accept
flow updates deal + creates child deal (parent_deal_id set) + events logged.

**T1.8 (M) — conversion path read model.** `growth.conversion_path()`: rung counts +
won value from `value_ladder_stage`; edge probabilities measured from parent→child won
pairs, priors 0.4/0.5/0.6 with `measured:false` until n≥5 → C7 contract. *Accept:*
prior/measured switch at n=5; unassigned rung deals excluded but counted in a note.

**T1.9 (M) — channel + event attribution metrics.** `growth.channel_metrics(days=30)`:
per channel — touches (deal_events payload channel), leads (lead_source), won +
value, CAC (acquisition_costs by source), CLTV:CAC reusing `cltv_cac` internals.
`growth.event_attribution(event_id)`: captured → discovery → deals → won funnel via
`event_attendance`→deals. *Accept:* totals reconcile with `cltv_cac()` and scorecard
counts on a fixture DB.

**T1.10 (L) — monthly review read model + snapshot.** `growth.monthly_review(month)`:
live compose of 8 blocks (pipeline_math, revenue mix vs 4F+2B+3S target from won
deals by rung, growth_loops, channel_metrics, scorecard rollup of the month's weeks,
scoring accuracy = won/lost by score band, plan_milestones progress) → C9 contract;
`close_monthly_review(month, decisions)` freezes to `monthly_reviews`; closed months
always read the snapshot. *Accept:* closed month returns byte-identical metrics after
underlying data changes; live month reflects new deals.

---

## Batch 2 — API endpoints (api.py)

All thin wrappers; standard error envelope. **(S each unless noted)**

| # | Route | → |
|---|---|---|
| T2.1 | `POST /api/crm/quick-capture` | T1.1 (M — composes duplicate handling) |
| T2.2 | `GET /api/growth/followups-today` | T1.4 |
| T2.3 | `POST /api/growth/nurture/{deal_id}/cadence` | T1.2 regenerate |
| T2.4 | `POST /api/growth/nurture/{deal_id}/teaching` | T1.3 |
| T2.5 | `PATCH /api/growth/nurture/{step_id}` | extend: `sent` also records touch w/ channel (one transaction) |
| T2.6 | `GET /api/crm/deals/{id}/discovery` | T1.6 |
| T2.7 | `GET/POST /api/crm/deals/{id}/proposal` + `PATCH /api/crm/proposals/{pid}` | T1.7 (M) |
| T2.8 | `GET /api/growth/conversion-path` | T1.8 |
| T2.9 | `GET/POST /api/growth/events` + `PATCH/DELETE /api/growth/events/{id}` + `GET …/{id}/attribution` | T0.1/T1.9 (M) |
| T2.10 | `GET /api/growth/channels` | T1.9 |
| T2.11 | `GET /api/growth/monthly-review?month=` + `POST …/monthly-review/close` | T1.10 |
| T2.12 | `PATCH /api/crm/contacts/{id}` | extend for tier/problem_statement (T0.2) |

*Accept (batch):* every route in the C1–C9 data contracts responds with the documented
shape; 404/422 paths covered; `/api/health` still green.

## Batch 3 — MCP verbs (mcp_server.py)

Mirror the operational surface for agents (TOOLS entry + dispatcher case). **(S each)**

- T3.1 `quick_capture_contact` → T1.1 (the agent-side 30-second add)
- T3.2 `get_followups_today` → T1.4 (agents can brief Ricardo each morning)
- T3.3 `generate_cadence` / T3.4 `generate_teaching_funnel` → T1.2/T1.3
- T3.5 `get_discovery_state` → T1.6 · T3.6 `create_proposal` + `decide_proposal` → T1.7
- T3.7 `get_conversion_path` → T1.8 · T3.8 `list_events` + `get_event_attribution` → T2.9
- T3.9 `get_channel_metrics` → T1.9 · T3.10 `get_monthly_review` / `close_monthly_review`
  (close = PRIVILEGED_TOOLS — it freezes a strategic record)

*Accept:* `get_mcp_manifest` lists all; each verb round-trips against a live server.

## Batch 4 — UI (templates/index.html)

- **T4.1 (M) — nav wiring:** `mensual` in `WS_SUBS.strategy` / `TAB_WORKSPACE` /
  `ROUTE_TABS`; `content-mensual` container; header `+ Contacto` button + `c` key.
- **T4.2 (L) — C1 quick-add modal** incl. `?quickadd=1` deep link, sticky
  event/tier on “Guardar y otro”, duplicate inline link, 390px layout.
- **T4.3 (M) — C2 “Hoy toca” card** on Today (`loadFollowupsToday()`), optimistic
  ✓/⏭, overdue badge on Pipeline header.
- **T4.4 (L) — C3+C4 cadence timeline + nurture manager** in the deal drawer
  (extend existing nurture block): sequence rows, dot timeline, compliance %, template
  textarea + Copiar, generate-teaching button.
- **T4.5 (M) — C5 discovery panel** in drawer: pre-call script state, signals +
  score-delta state, background auto-fetch chip, routing CTA.
- **T4.6 (M) — C6 proposal builder + strip:** 3-option grid, product dropdowns from
  catalog, suggestion prefill, send/decide flows; strip under `#crm-pipeline`.
- **T4.7 (M) — C7 conversion path** SVG in Growth tab (replaces `#crm-ladder`
  rendering; keep div id), node click → filter all-deals.
- **T4.8 (M) — C8 events + channels sections** in Growth tab (`#growth-events`,
  `#growth-channels`): event cards w/ prep expander + attribution funnel, channels table.
- **T4.9 (L) — C9 Mensual view:** 8 blocks, month pager, close/frozen states,
  decisions textarea, per-block lazy load.
- **T4.10 (S) — dimension chips** (`op·táct·estrat`) on all new section headers.

*Accept (batch):* headless sweep — all tabs load, 0 console errors, every new section
renders its empty state on a blank DB; existing tabs unaffected.

## Batch 5 — Verification & docs

- **T5.1 (S)** `python -m py_compile` all touched files; restart
  `hermes-dashboard.service`; `curl` every new endpoint (spec @verification).
- **T5.2 (M)** Fixture-based tests for T1.2 dates, T1.5 compliance, T1.7 accept-flow
  child-deal spawn, T1.10 frozen-snapshot immutability (the 4 highest-regression-risk
  paths).
- **T5.3 (S)** Update `docs/changelog.md` + spec `@implementation` section; framework
  doc already at `knowledge/growth-operating-framework.md`.

---

## Dependency graph & suggested order

```
B0 (T0.1–0.6, parallel) → B1: T1.1←T1.2 · T1.3 · T1.4←T0.3 · T1.5 · T1.6 · T1.7←T0.4 ·
T1.8 · T1.9←T0.1/0.5 · T1.10←T0.6+T1.8/1.9  →  B2 (per-row deps as listed) →
B3 (after B2 shapes settle) ∥ B4 (T4.1 first; T4.2/4.3 after T2.1/2.2; rest after B2) → B5
```
**Milestone 1 (operational core, ship first):** T0.1–0.3 · T1.1/1.2/1.4 · T2.1/2.2/2.5 ·
T4.1–4.3 — Ricardo can capture at an event and work “Hoy toca” end-to-end.
**Milestone 2 (tactical):** cadence/teaching/discovery/proposals (rest of B1/B2 + T4.4–4.6).
**Milestone 3 (strategic):** conversion path, events/channels, Mensual (T1.8–1.10, T4.7–4.9).

Delegation note (orchestrator doctrine): B0 and B2 tasks are well-contracted,
independent chunks — good `ollama-worker`/`kimi-coder` candidates with the accept
criteria above as the Tier-1a contract. T1.1, T1.7, T1.10 and all drawer UI (T4.4–4.6)
touch cross-file behavior — keep in Opus.
