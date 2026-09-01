# Product audit: automation gaps & built-but-unwired in orchestrator/dashboard

Scope: triage, day planning, wrap-up, cycle rolling, deal decay, funnel snapshots, lead scoring, nurture sequences.
Files reviewed:
- `orchestrator/dashboard/canvas.py` (day-plan/wrap-up)
- `orchestrator/dashboard/api.py` (HTTP routes)
- `orchestrator/dashboard/growth.py` (lead scoring, nurture, funnel, scorecard)
- `orchestrator/dashboard/crm.py` (deals, contacts, accounts, decay)
- `orchestrator/dashboard/sprints.py` (cycles/rolling/ledger)
- `orchestrator/mcp_server.py` (MCP tool registry)
- `orchestrator/dashboard/orchestration.py` (sweeper)
- `orchestrator/deploy/*.{service,timer}` and `orchestrator/bin/*.{sh,py}` (automation triggers)

---

## 1. Triaging / day-planning candidates — wired but not automated

**What's built:**
- `canvas.py:269 get_day_plan()` returns a `do` / `needs_you` / `review` / `later` / `carried` composition.
- `canvas.py:159 plan_day()` lets a human (or agent) commit a set of tasks for a date.
- `canvas.py:466 wrap_day()` stamps `carried_over` events for tasks not completed today.
- MCP tools registered at `mcp_server.py:4607-4610`: `get_day_plan`, `plan_day`, `plan_task`, `wrap_day`.
- API routes at `api.py:1045-1073`: `POST /api/day-plan/wrap`, `POST /api/day-plan`, `PATCH /api/tasks/{task_id}/plan`.

**What's missing / manual:**
- No scheduler/timer calls `plan_day()` automatically in the morning. The dashboard shows candidates, but a human must click to commit.
- No automatic wrap-up at 19:00. `api.py:1045` exists, but there is no systemd timer or cron job that calls it daily.
- `wrap_day()` only stamps events; it does **not** advance `planned_for` to tomorrow for incomplete tasks, so the next day's `do` list still shows stale `planned_for` dates unless the user replans.

**File:line refs:**
- `canvas.py:466-490` wrap_day only logs; no date advancement.
- `api.py:1045-1051` route has no automated caller.
- `deploy/` has no `hermes-wrap-day.{timer,service}`.

**Recommendation:**
Add `deploy/hermes-wrap-day.timer` + `service` that `POST`s `/api/day-plan/wrap` (or calls `canvas.wrap_day()` directly). Consider extending `wrap_day()` to either (a) re-plan remaining tasks to the next business day, or (b) expose a `carry_forward=true` option that updates `planned_for` for the operator.

---

## 2. Cycle rolling — mostly wired, auto-roll is implicit via close_sprint

**What's built:**
- `sprints.py:1078 roll_cycle()` closes the active cycle when its end date has passed and creates/starts the next one.
- `sprints.py:1099 finish_sprint()` is the explicit operator roll-forward; it archives accepted/rejected work, carries unfinished tasks into the next cycle, and guarantees a +2 planning slot.
- `api.py:2044` `POST /api/cycles/roll` exposes `roll_cycle()`.
- MCP `roll_cycle` at `mcp_server.py:4193`.

**What's missing / manual:**
- There is **no systemd timer** that periodically calls `/api/cycles/roll`. The sweeper (`orchestration.py:927 sweep()`) does **not** invoke `roll_cycle()`, so the active cycle only advances when an operator or agent explicitly triggers it.
- `roll_cycle()` is idempotent but relies on an external trigger; if the operator forgets, the active cycle stays expired.

**File:line refs:**
- `sprints.py:1078-1096` roll_cycle implementation.
- `orchestration.py:927-1008` sweep() does not call roll_cycle.
- `deploy/` has no `hermes-roll-cycle.{timer,service}` (only `promote-weekly.sh` for backlog promotion).

**Recommendation:**
Add a `deploy/hermes-roll-cycle.timer` + `service` running Monday 00:05 (or a nightly 00:05 timer) that calls `roll_cycle()`. Alternatively, extend `sweep()` to call `roll_cycle()` once per day.

---

## 3. Sprint ledger drift — built but not auto-reconciled

**What's built:**
- `sprints.py:464 sprint_ledger_drift()` detects forward/reverse orphans between `tasks.sprint_id` and `task_sprints`.
- `orch-verify` and tests reference it.

**What's missing / manual:**
- `sprint_ledger_drift()` is read-only. There is no `reconcile_ledger()` or `repair_ledger()` that fixes the 30 drift instances (29 forward orphans reported in the context).
- The sweeper does not repair drift automatically.

**File:line refs:**
- `sprints.py:464-497` read-only drift check.
- No `def repair_ledger` anywhere in `dashboard/`.

**Recommendation:**
Add `sprints.repair_ledger()` that resolves forward orphans by inserting missing open `task_sprints` rows and reverse orphans by stamping `outcome='carried'` on orphaned ledger rows (or linking them to the current sprint_id). Wire it into `sweep()` or a weekly repair timer.

---

## 4. Deal decay (auto stale → stalled → lost) — built but not scheduled

**What's built:**
- `crm.py:834 auto_stale_decay(days_to_stalled=30, days_to_lost=90)` moves idle active deals to `stalled` and idle stalled deals to `lost`, logging `auto_stalled` / `auto_lost` events.
- `api.py:2394` `POST /api/crm/decay` exposes it.
- MCP `crm_decay` at `mcp_server.py:4299-4306`.

**What's missing / manual:**
- No cron/timer runs `auto_stale_decay()`. The function only runs when an agent/operator invokes it.
- The sweeper does not include CRM decay.

**File:line refs:**
- `crm.py:834-871` auto_stale_decay.
- `api.py:2394-2399` route.
- `deploy/` has no `hermes-crm-decay.{timer,service}`.

**Recommendation:**
Add a weekly `deploy/hermes-crm-decay.timer` (e.g., Sunday 23:00) or extend `sweep()` to call `crm.auto_stale_decay()`. Include a report in the operator digest.

---

## 5. Funnel snapshots — built and scheduled, but snapshot function naming mismatch

**What's built:**
- `growth.py:1957 compute_funnel()` computes stage counts + conversion rates.
- `growth.py:1988 snapshot_funnel()` upserts a weekly snapshot.
- `bin/funnel-snapshot.sh` calls `growth.snapshot_funnel()`.
- `deploy/hermes-funnel-snapshot.{service,timer}` runs Monday 09:00.
- MCP `capture_funnel` at `mcp_server.py:3730`.

**What's missing / wiring issue:**
- The shell script references `growth.snapshot_funnel()` (`bin/funnel-snapshot.sh:27`), which exists. Good.
- There is no HTTP route `/api/capture-funnel` separate from the timer; the timer is the only automation caller. That's acceptable, but the MCP `capture_funnel` tool directly imports `growth` instead of calling the API, which is inconsistent with the “thin proxy” pattern used for cycle tools.
- `growth.py:2001 funnel_trend()` auto-seeds from current deals if the table is empty, which can mask the fact that the timer failed (you get a snapshot but it may be stale).

**File:line refs:**
- `growth.py:1988-1999` snapshot_funnel.
- `bin/funnel-snapshot.sh:27`.
- `mcp_server.py:3730` capture_funnel (direct import, not API proxy).

**Recommendation:**
- Keep the timer.
- Optionally add an HTTP route `POST /api/funnel/capture` and make `tool_capture_funnel` proxy it for consistency.
- Add alerting when `snapshot_funnel()` is run but no prior snapshot exists (seed path) so the operator knows historical data is missing.

---

## 6. Lead scoring — built but not auto-refreshed

**What's built:**
- `growth.py:1375 score_all_leads()` recomputes scores for all non-closed deals.
- `growth.py:1343 score_deal()` recomputes one deal using latest Fireflies signals.
- `crm.py:237 _autoscore()` calls `growth.score_deal()` on deal create/update (best-effort).
- MCP `score_all_leads` at `mcp_server.py:4238` and `score_deal` at `mcp_server.py:4627`.

**What's missing / manual:**
- There is no scheduled job that runs `score_all_leads()`. Scores become stale if Fireflies signals arrive after the deal was last touched (only explicit `score_all_leads` or `score_deal` refreshes them).
- `_autoscore()` in `crm.py:237` catches create/update, but not Fireflies ingestion, touch events, or content interactions.

**File:line refs:**
- `growth.py:1375-1387` score_all_leads.
- `crm.py:237-245` _autoscore best-effort only on deal writes.
- No timer/service for scoring.

**Recommendation:**
- Add `deploy/hermes-score-leads.timer` + `service` (e.g., nightly) calling `growth.score_all_leads()`.
- OR trigger `score_deal()` from `record_touch()` and from the Fireflies fetch/store path so scores stay current.

---

## 7. Nurture sequences — built but not auto-generated or auto-advanced

**What's built:**
- `growth.py:920 generate_nurture(deal_id)` creates a 5-touch Hook cadence from deal name/source/stage.
- `growth.py:902 get_nurture()` reads the sequence + next suggested date.
- `crm.py:529 get_cadence_status()` computes compliance % and overdue steps.
- MCP tools: `generate_nurture`, `update_nurture`, `get_nurture`, `get_cadence_status` (`mcp_server.py:3626, 3653, 3677, 3685`).

**What's missing / manual:**
- Nurture sequences are **only** generated when an operator/agent calls `generate_nurture`. There is no trigger on lead creation (`quick_add_lead`/`create_deal`) that auto-generates the sequence.
- There is no daily job that sends/advances overdue nurture steps or surfaces them in the operator digest.
- `pipeline_health()` (`growth.py:588`) flags red/yellow/blue touch alerts, but it does not use nurture `next_suggested_date` to prioritize.

**File:line refs:**
- `growth.py:920-944` generate_nurture (manual call).
- `crm.py:487-527` quick_add_contact does not call generate_nurture.
- `growth.py:1487-1500` quick_add_lead does not call generate_nurture.

**Recommendation:**
- Auto-generate nurture on lead creation: add `growth.generate_nurture(deal_id)` to `quick_add_lead()` and `create_deal()` for `stage='lead'` deals.
- Add a daily `deploy/hermes-nurture-check.timer` that surfaces overdue nurture steps in the operator digest or a dedicated `/api/nurture/overdue` endpoint.
- Optionally integrate nurture next-due into `pipeline_health()` red/yellow logic.

---

## 8. Scorecard reminder — built and scheduled

**What's built:**
- `growth.py:1629 scorecard()` auto-derives the weekly 5 from deal/content events.
- `bin/scorecard-reminder.sh` computes it Friday 17:00.
- `deploy/hermes-scorecard.{service,timer}` exists.

**What's missing:**
- The scorecard is logged to a file but not surfaced as a dashboard notification or session event. No HTTP route `/api/scorecard/remind` exists.

**File:line refs:**
- `bin/scorecard-reminder.sh:28-30`.
- `api.py` has `/api/scorecard` but no reminder route.

**Recommendation:**
- Optional: expose `POST /api/scorecard/remind` and optionally create a `session_event`/`notification` so the Friday review appears in the dashboard Needs-you queue.

---

## 9. MCP/HTTP parity gaps

**What's wired:** Most rituals and CRM/growth verbs have both HTTP and MCP tools.

**What's missing from MCP registry (or inconsistent):**
- `tool_get_cadence_status` is implemented and registered at `mcp_server.py:4703`, but it is **not** in the earlier privileged/orient tool lists in the same file; only the final `TOOL_HANDLERS` map matters, so it is callable.
- No MCP tool for `get_stale_deals`? Actually `tool_get_stale_deals` exists at `mcp_server.py:4294`.
- No MCP tool for `content_cadence` or `pipeline_math`? Check registry: `get_pipeline_math` exists at `mcp_server.py:4619`; `list_content` exists. `content_cadence` is not exposed as an MCP tool, but `list_content` returns cadence data.
- No MCP tool for `auto_stale_decay`? `crm_decay` covers it.

**Minor inconsistency:**
- Some growth tools (e.g., `tool_score_all_leads`, `tool_capture_funnel`) directly import `dashboard.growth` while most Phase-4 cycle tools proxy via `_dash()` to the API. This creates two code paths and makes behavior differ if the API has middleware (auth, logging, side effects).

**File:line refs:**
- `mcp_server.py:4238` score_all_leads (direct import).
- `mcp_server.py:3730` capture_funnel (direct import).
- `mcp_server.py:4193` roll_cycle (API proxy).

**Recommendation:**
Standardize growth ritual tools to proxy via `_dash()` like cycle tools, or ensure both paths are tested. Not a critical bug, but a maintenance hazard.

---

## 10. Orchestration sweeper — built but narrow

**What's built:**
- `orchestration.py:927 sweep()` auto-compacts sessions, auto-aborts tasks over the failure limit, resolves stale/superseded input events, reclaims orphan runs, graduates autonomy, auto-tags sessions.
- `api.py:3210` `POST /api/orchestration/sweep` exposes it.
- MCP `orchestration_sweep` at `mcp_server.py:4234`.

**What's missing:**
- `sweep()` does **not** call `roll_cycle()`, `crm.auto_stale_decay()`, `score_all_leads()`, or `growth.snapshot_funnel()`. It is a session/task health sweep, not a business-ritual sweep.
- There is no evidence of an external scheduler actually invoking `sweep()` periodically. The dashboard service runs the API, but no timer/service triggers `/api/orchestration/sweep`.

**File:line refs:**
- `orchestration.py:927-1008` sweep scope.
- `deploy/hermes-dashboard.service` only starts the API; no companion timer for `/api/orchestration/sweep`.

**Recommendation:**
Add `deploy/hermes-orchestration-sweep.timer` + `service` (e.g., every 15 min) hitting `POST /api/orchestration/sweep`. Then extend `sweep()` to also call:
1. `roll_cycle()` (or keep separate Monday timer)
2. `crm.auto_stale_decay()` (or keep separate weekly timer)
3. `growth.score_all_leads()`

This centralizes ritual automation in one scheduler the operator can inspect.

---

## Summary table

| Ritual | Built? | Wired to timer/sweeper? | Gap | Priority |
|--------|--------|-------------------------|-----|----------|
| Day-plan candidate view | ✅ | ❌ No auto-commit | Morning commit is manual | Medium |
| Wrap-up | ✅ | ❌ No timer/service | `planned_for` not advanced | High |
| Cycle rolling | ✅ | ❌ No timer/service | Expired cycles stay active | High |
| Ledger drift repair | ✅ (read-only) | ❌ No repair function | 30 drift unresolved | High |
| Deal decay | ✅ | ❌ No timer/service | Stale deals not auto-staged | Medium |
| Funnel snapshots | ✅ | ✅ Monday 09:00 timer | OK; minor MCP inconsistency | Low |
| Lead scoring refresh | ✅ | ❌ No timer/service | Scores go stale | Medium |
| Nurture sequences | ✅ | ❌ No auto-gen/advance | Leads land without nurture | High |
| Scorecard reminder | ✅ | ✅ Friday 17:00 timer | OK; not surfaced as notification | Low |
| Orchestration sweep | ✅ | ❌ No known scheduler | Sessions/tasks not auto-maintained | Medium |

---

## Suggested automation backlog (in order of impact)

1. **Auto-wrap + carry-forward** (`deploy/hermes-wrap-day.timer` + extend `canvas.wrap_day()`).
2. **Auto-roll cycle** (`deploy/hermes-roll-cycle.timer` or extend `sweep()`).
3. **Repair sprint ledger drift** (`sprints.repair_ledger()` + weekly timer).
4. **Auto-generate nurture on lead creation** (`quick_add_lead`/`create_deal` hook).
5. **Auto-score leads** (nightly timer or Fireflies/touch triggers).
6. **Auto-CRM-decay** (weekly timer or extend `sweep()`).
7. **Surface scorecard as dashboard notification** (`POST /api/scorecard/remind`).
8. **Standardize MCP growth tools to API proxy** for single code path.
