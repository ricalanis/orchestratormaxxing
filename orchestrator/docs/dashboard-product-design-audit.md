# Dashboard Product & Design Audit — Hermes Orchestrator

**Date:** 2026-07-07  
**Author:** Hermes (Product + Design audit)  
**Goal:** Feature completeness, user journey inicio a fin, UX polish en cada tab. Loop de validacion producto/diseno/desarrollo.

---

## Current Architecture

### Navigation Structure (6 workspaces, 14 tabs)

| Workspace | Tabs | Status |
|---|---|---|
| **Today** | Today | Funcional pero incompleto (ver abajo) |
| **Board** | Board, Cycle, My Tasks, Agent Tasks, Archive | Funcional, falta pulido |
| **Strategy** | Roadmap, CRM (Pipeline), Growth | CRM y Growth partially built, Roadmap basic |
| **Sessions** | Sessions | Funcional, cards grid fixed |
| **Knowledge** | Memory, Lakehouse | Memory OK, Lakehouse placeholder |
| **Ops** | Usage, Health, Coordinators | Usage basic, Health OK, Coordinators basic |

### Stats
- **8,160 lines** of HTML/JS/CSS in single file
- **302 JS functions**
- **154 API endpoints** (134 exposed via MCP after Phase 1-3)
- **6 workspaces, 14 tabs**

---

## Feature Completeness Audit (by tab)

### 1. Today Tab — STATUS: 70% complete

**What works:**
- Day plan loading (Do/Review/Needs You lanes)
- Bulk accept review tasks
- Today plan creation
- Task done/plan/unplan actions

**What's missing (user journey gaps):**
- [ ] **No morning ritual onboarding** — first open of the day should show "Buenos dias" prompt with yesterday's wrap-up summary + today's plan candidates
- [ ] **No day wrap-up flow** — `wrap_day` MCP tool exists but no UI button to trigger end-of-day review
- [ ] **No time-block integration** — Growth time-blocks exist in DB but Today tab doesn't show them alongside tasks
- [ ] **No coaching tip card** — Fireflies behavioral coaching data exists but isn't shown on Today tab as a daily nudge
- [ ] **Review lane doesn't show WHAT was done** — just task title, no diff/summary of what the agent actually produced
- [ ] **No "Needs You" action clarity** — shows tasks but doesn't explain WHAT action is needed (approve? edit? decide?)

**User Journey should be:**
```
Open dashboard → See morning summary (yesterday wrap + today plan)
→ Review agent work (see what was done, approve/reject with context)
→ Plan today (drag tasks into Do lane, see time-blocks)
→ End of day → Wrap-up button (what got done, what slipped, tomorrow's candidates)
```

### 2. Board Tab — STATUS: 80% complete

**What works:**
- Kanban columns (backlog, ready, in_progress, done, blocked)
- Task cards with priority, assignee, project
- Drag and drop between columns
- Task creation modal
- Board lens (all/my/agent)

**What's missing:**
- [ ] **No WIP limit visualization** — columns don't show count limits
- [ ] **No blocked reason display** — blocked tasks show red but don't show WHY
- [ ] **No task dependencies visualization** — task_links exist in DB but no UI
- [ ] **No bulk actions** — can't select multiple tasks to move/assign
- [ ] **No swimlane view** — can't group by project or assignee
- [ ] **Card menu incomplete** — can dispatch to agent, but can't set autonomy or contract from card

### 3. Cycle Tab — STATUS: 75% complete

**What works:**
- Sprint/cycle board with columns
- Cycle creation modal
- Commit/uncommit tasks to cycle
- Cycle search/filter
- Delete cycle
- Burndown chart

**What's missing:**
- [ ] **No cycle roll UI** — `roll_cycle` MCP tool exists but no button to roll to next cycle
- [ ] **No calendar view** — `get_cycles_calendar` endpoint exists but no calendar UI
- [ ] **No capacity planning** — no story points or effort estimation on tasks
- [ ] **No cycle goal display** — cycle has a `goal` field but it's not shown prominently
- [ ] **No velocity trend** — `get_velocity` exists but no chart in cycle view

### 4. My Tasks Tab — STATUS: 85% complete

**What works:**
- Task list with filters
- Task creation
- Status management

**What's missing:**
- [ ] **No grouping by project** — flat list, hard to scan with many tasks
- [ ] **No quick actions** — can't change status from list view without opening card
- [ ] **No deadline/due date field** — no concept of due dates

### 5. Agent Tasks Tab — STATUS: 75% complete

**What works:**
- Agent task list
- Accept/reject with reason modal
- Trust grade badge

**What's missing:**
- [ ] **No agent filter** — can't filter by which agent (claude-code vs opencode vs hermes)
- [ ] **No run history inline** — runs show in task drawer but not in list view
- [ ] **No "what the agent did" summary** — just task title, no execution summary
- [ ] **No re-dispatch button** — can't re-send a failed task to an agent from this view

### 6. Sessions Tab — STATUS: 70% complete

**What works:**
- Session cards in grid
- Session output viewer
- Send command to session
- Kill/revive sessions
- Remote host detection (GCP VM)

**What's missing:**
- [ ] **No session detail modal** — can't see full session history in a modal
- [ ] **No prompt history viewer** — can't see what was sent to each session
- [ ] **No compact trigger from UI** — `compact_session` MCP exists but no button
- [ ] **No session health indicator** — idle time, context %, last activity
- [ ] **No session-to-task link display** — can't see which tasks are assigned to which session
- [ ] **No "new session" creation flow** — can't spin up a new Claude Code session from UI

### 7. Roadmap Tab — STATUS: 60% complete

**What works:**
- Initiative list
- Initiative drilldown
- Add initiative modal
- Progress bars

**What's missing:**
- [ ] **No epic display** — `list_epics` and `create_epic` MCP tools exist but no UI
- [ ] **No initiative events timeline** — `get_initiative_events` exists but no UI
- [ ] **No quarter/milestone view** — no time-based roadmap view (quarterly, monthly)
- [ ] **No drag-to-reorder** — can't reorder initiatives by priority
- [ ] **No epic-to-task tree** — can't see epic → task hierarchy
- [ ] **No edit initiative** — can create but can't edit existing initiative details

### 8. CRM/Pipeline Tab — STATUS: 65% complete

**What works:**
- Pipeline kanban (stages)
- Deal cards
- Deal drilldown (account chain, events)
- Quick lead capture modal
- ICP editor modal
- Product creation modal
- Touch deal button
- Score deal button
- Nurture sequence display

**What's missing:**
- [ ] **No deal edit modal** — can update deal via API but no edit form in UI
- [ ] **No account detail view** — accounts exist but no detail/contacts view
- [ ] **No contact management** — contacts can be created but not listed/managed in UI
- [ ] **No pipeline math display** — `get_pipeline_math` exists but not shown
- [ ] **No funnel trend chart** — `get_funnel_trend` exists but no chart
- [ ] **No CLTV/CAC display** — `get_cltv_cac` exists but not shown
- [ ] **No acquisition cost tracker** — `list_acquisition_costs` exists but no UI
- [ ] **No pipeline health indicator** — `get_pipeline_health` exists but no badge/display
- [ ] **No deal growth metrics** — `update_deal_growth` exists but no form
- [ ] **No lead scoring display** — leads captured but scoring not shown

### 9. Growth Tab — STATUS: 55% complete

**What works:**
- Growth loops display
- Content pipeline
- Speaking pipeline
- Time blocks
- Behavioral coaching (Fireflies)
- Plan milestones
- Scorecard

**What's missing:**
- [ ] **No content edit** — can create content but can't edit/delete from UI (only via API)
- [ ] **No speaking edit** — same, no edit/delete in UI
- [ ] **No time block edit** — can create and toggle but can't edit text/time
- [ ] **No milestone progress bar** — milestones shown but no progress visualization
- [ ] **No growth loop detail** — loops shown as cards but no drilldown
- [ ] **No funnel snapshot capture** — `capture_funnel` exists but no button
- [ ] **No nurture generation** — `generate_nurture` exists but no UI button
- [ ] **No ICP fit indicator on leads** — can't see if a lead matches ICP

### 10. Memory Tab — STATUS: 80% complete

**What works:**
- Memory stores display (agent + user)
- Memory search
- Graph visualization (vis.js)
- Graph rebuild
- Sub-tabs (stores/graph)

**What's missing:**
- [ ] **No memory edit** — can view but can't edit/delete from UI
- [ ] **No graph node search** — can't search for specific nodes
- [ ] **No graph edge labels** — edges shown but relationship type not labeled
- [ ] **No memory export** — can't export memory as JSON/markdown

### 11. Usage Tab — STATUS: 50% complete

**What works:**
- Usage overview (Ollama + Claude)
- Provider breakdown
- Refresh buttons

**What's missing:**
- [ ] **No cost projection** — can't see projected monthly cost
- [ ] **No usage chart over time** — just current numbers, no trend
- [ ] **No per-session usage** — can't see which sessions use most tokens
- [ ] **No budget alerts** — no threshold setting or alert display
- [ ] **No per-model breakdown** — can't see which models cost most

### 12. Health Tab — STATUS: 85% complete

**What works:**
- Service status (dashboard, MCP SSE)
- Recent errors
- Ops status (VMs)
- Health check

**What's missing:**
- [ ] **No auto-refresh** — must manually click refresh
- [ ] **No error detail** — errors listed but can't expand for stack trace
- [ ] **No service restart** — can see status but can't restart from UI

### 13. Lakehouse Tab — STATUS: 30% complete

**What works:**
- Overview display
- Ask lakehouse (NL query)
- Metric display

**What's missing:**
- [ ] **No data freshness indicator** — `as_of` not shown
- [ ] **No lineage view** — `get_lineage` exists in lakehouse MCP but no UI
- [ ] **No metric trend chart** — just current values, no historical trend
- [ ] **No entity browser** — can't browse tables/columns
- [ ] **No context packet viewer** — `get_context_packet` exists but no UI

### 14. Coordinators Tab — STATUS: 40% complete

**What works:**
- Coordinator list
- Refresh

**What's missing:**
- [ ] **No coordinator detail** — can't click to see what a coordinator is working on
- [ ] **No coordinator health** — no status indicator
- [ ] **No coordinator tasks** — can't see tasks assigned to each coordinator

---

## Cross-cutting UX Issues

### A. Onboarding/Empty States
- [ ] **No first-run experience** — new users see empty tabs with no guidance
- [ ] **Empty states are inconsistent** — some tabs show "No data", others show nothing
- [ ] **No contextual help** — no tooltips or "?" icons explaining features

### B. Navigation
- [ ] **Workspace bar not sticky** — scrolls away on long pages
- [ ] **No breadcrumbs** — can't tell where you are in the hierarchy
- [ ] **No global search** — can't search across tasks, deals, sessions, memory
- [ ] **Tab counts inconsistent** — some tabs show count badges, others don't

### C. Mobile
- [ ] **No responsive layout** — dashboard assumes desktop width
- [ ] **No touch gestures** — no swipe between tabs
- [ ] **No mobile-specific UI** — same layout on phone (cramped)

### D. Feedback & Toasts
- [ ] **Toasts auto-dismiss too fast** — can't read long messages
- [ ] **No undo** — destructive actions have no undo
- [ ] **No confirmation for bulk actions** — bulk accept happens silently

### E. Performance
- [ ] **No lazy loading** — all data loads on tab switch
- [ ] **No caching** — every tab switch re-fetches
- [ ] **No skeleton states** — blank screen while loading

---

## Proposed Product Pass — Prioritized

### P0: Critical User Journey Fixes (must-have for feature completeness)

1. **Today: Morning ritual** — show yesterday wrap + today plan candidates on first open
2. **Today: Day wrap-up button** — trigger end-of-day review with what got done
3. **Today: Coaching tip card** — show Fireflies coaching nudge
4. **Board: Blocked reason display** — show WHY a task is blocked
5. **Sessions: Context % + idle indicator** — show session health at a glance
6. **CRM: Pipeline math + health** — show conversion rates and pipeline status
7. **Growth: Content/speaking/time-block edit + delete** — CRUD complete
8. **Roadmap: Epic display + create** — surface epics in roadmap

### P1: Completeness Gaps (important but not blocking)

9. **CRM: Deal edit modal** — edit deal details from UI
10. **CRM: Funnel trend chart** — visualize funnel over time
11. **CRM: CLTV/CAC display** — show unit economics
12. **Growth: Milestone progress bars** — visualize milestone completion
13. **Sessions: Compact button** — trigger compaction from UI
14. **Sessions: New session creation** — spin up Claude Code from UI
15. **Roadmap: Initiative edit** — edit existing initiatives
16. **Roadmap: Initiative events timeline** — show event history
17. **Memory: Edit/delete** — manage memory entries from UI
18. **Usage: Cost chart over time** — trend visualization
19. **Lakehouse: Data freshness + lineage** — show data state

### P2: UX Polish (nice-to-have)

20. **Global search** — search across all entities
21. **Onboarding/empty states** — consistent guidance when data is empty
22. **Sticky workspace nav** — stays visible on scroll
23. **Skeleton loading states** — shimmer instead of blank
24. **Mobile responsive** — at least readable on phone
25. **Undo for destructive actions** — confirm or undo
26. **Auto-refresh health tab** — poll every 30s
27. **Board: WIP limits** — show column count limits
28. **Board: Swimlane view** — group by project/assignee
29. **Cycle: Calendar view** — monthly calendar of cycles
30. **Cycle: Velocity trend chart** — show velocity over sprints

---

## Implementation Plan (3 Claude Code sessions)

### Session 1: P0 Critical Fixes (items 1-8)
- Today tab: morning ritual, wrap-up, coaching card
- Board: blocked reason
- Sessions: health indicators
- CRM: pipeline math
- Growth: CRUD complete
- Roadmap: epics

### Session 2: P1 Completeness (items 9-19)
- CRM: deal edit, funnel chart, CLTV/CAC
- Growth: milestone progress
- Sessions: compact, new session
- Roadmap: initiative edit, events timeline
- Memory: edit/delete
- Usage: cost chart
- Lakehouse: freshness + lineage

### Session 3: P2 Polish (items 20-30)
- Global search
- Empty states
- Sticky nav
- Skeleton loading
- Mobile responsive basics
- Undo/confirm
- Auto-refresh health
- Board: WIP limits, swimlanes
- Cycle: calendar, velocity chart

### Validation Loop (after each session)

**Producto:** verify user journey completeness (can user do X end-to-end?)  
**Diseno:** verify visual consistency, empty states, feedback  
**Desarrollo:** verify code quality, no regressions, tests pass

Each validation is a Claude Code subagent review with a checklist.