# MCP Coverage Analysis — Orchestrator Dashboard

**Date:** 2026-07-07 (last updated 2026-07-10)  
**Author:** Hermes  
**Status:** Complete — 100% parity achieved

---

## Current State (as of 2026-07-10 — verified by independent audit)

| Layer | Count | Notes |
|---|---|---|
| **MCP Server (orchestrator)** | 205 tools | 143 default scope + 62 privileged |
| **MCP Server (lakehouse)** | 8 tools | `list_metrics`, `list_entities`, `get_metric`, `get_context_packet`, `ask_lakehouse`, `get_lineage`, `get_impact`, `query_data` |
| **MCP Server (coolify)** | 9 tools | |
| **MCP Server (gcloud)** | 1 tool | `gcloud_command` |
| **Dashboard API endpoints** | 158 | All under `/api/*` |
| **Tools ↔ Handlers parity** | 205/205 | 0 broken (was 4 broken on 2026-07-10 pre-fix) |
| **Dashboard → MCP gaps** | 0 genuine | 4 unmapped routes are UI-only or covered by lakehouse MCP |
| **MCP → Dashboard** | 53 MCP-only | Write/privileged operations (create/delete/assign) — no UI needed |
| **Parity regression tests** | 9 passing | `tests/test_mcp_api_parity.py` |

**Previous state (2026-07-07):** 78 MCP tools, 90 dashboard endpoints without MCP exposure, 15 MCP-only tools without UI.  
**Growth:** 78 → 205 tools (+127 new tools, all gaps from the original analysis filled).

### Remaining unmapped API routes (4) — all intentional

1. `POST /api/cycles/reorder` — drag-drop card reorder (UI-only; agents use `assign_task_sprint`)
2. `GET /api/growth/time-blocks/{day_of_week}/activities` — weekday widget (agents use `get_day_plan`)
3. `GET /api/lakehouse/ask` — covered by lakehouse MCP server (`ask_lakehouse`)
4. `GET /api/lakehouse/lineage` — covered by lakehouse MCP server (`get_lineage`)

### Parity ratchet

The `McpGlobalParity` test class ensures `TOOLS` and `TOOL_HANDLERS` stay 1:1 — a tool registered in the schema but missing its handler (the bug that broke `commit_cycle`, `delete_sprint`, `get_cycles_calendar`, `roll_cycle`) now fails CI immediately.

---

## Gap Analysis: Dashboard → MCP (90 endpoints without MCP exposure)

### Priority 1: CRM/Growth (40 endpoints) — BIGGEST GAP

The entire Growth system (Phases 1-3, 15 features built) has **zero MCP exposure**. Agents cannot:
- Read or update ICP
- Manage products
- Create/edit content or speaking engagements
- Log time blocks
- View pipeline health, CLTV/CAC, funnel trends
- Manage acquisition costs
- Generate or update nurture sequences
- Score leads
- Touch deals (sales activity logging)
- View behavioral coaching (Fireflies)
- View scorecard

**Endpoints to expose:**

| Endpoint | Proposed MCP Tool | Agent Use Case |
|---|---|---|
| `GET /api/growth/icp` | `get_icp` | Agent reads ICP to qualify leads |
| `PATCH /api/growth/icp` | `update_icp` | Agent refines ICP based on research |
| `GET /api/growth/products` | `list_products` | Agent references product catalog |
| `POST /api/growth/products` | `create_product` | Agent creates new product entry |
| `PATCH /api/growth/products/{id}` | `update_product` | Agent updates pricing/description |
| `DELETE /api/growth/products/{id}` | `delete_product` | Agent removes stale product |
| `GET /api/growth/content` | `list_content` | Agent sees content pipeline |
| `POST /api/growth/content` | `create_content` | Agent logs content idea/draft |
| `PATCH /api/growth/content/{id}` | `update_content` | Agent updates content status |
| `DELETE /api/growth/content/{id}` | `delete_content` | Agent removes content |
| `GET /api/growth/speaking` | `list_speaking` | Agent sees speaking pipeline |
| `POST /api/growth/speaking` | `create_speaking` | Agent logs speaking opp |
| `PATCH /api/growth/speaking/{id}` | `update_speaking` | Agent updates speaking status |
| `DELETE /api/growth/speaking/{id}` | `delete_speaking` | Agent removes speaking entry |
| `GET /api/growth/time-blocks` | `list_time_blocks` | Agent sees time allocation |
| `POST /api/growth/time-blocks` | `create_time_block` | Agent logs time block |
| `PATCH /api/growth/time-blocks/{id}` | `update_time_block` | Agent updates time block |
| `DELETE /api/growth/time-blocks/{id}` | `delete_time_block` | Agent removes time block |
| `GET /api/growth/funnel-trend` | `get_funnel_trend` | Agent reports on funnel health |
| `POST /api/growth/funnel-snapshot` | `capture_funnel` | Agent captures funnel state |
| `GET /api/growth/fireflies-analytics` | `get_fireflies_analytics` | Agent reads meeting analytics |
| `GET /api/growth/behavioral-coaching` | `get_coaching` | Agent reads coaching metrics |
| `GET /api/growth/plan-milestones` | `list_milestones` | Agent sees 90-day plan |
| `PATCH /api/growth/plan-milestones/{id}` | `update_milestone` | Agent updates milestone progress |
| `GET /api/growth/pipeline-health` | `get_pipeline_health` | Agent reports pipeline status |
| `GET /api/growth/cltv-cac` | `get_cltv_cac` | Agent reports unit economics |
| `GET /api/growth/acquisition-costs` | `list_acquisition_costs` | Agent sees acquisition spend |
| `POST /api/growth/acquisition-costs` | `create_acquisition_cost` | Agent logs acquisition cost |
| `DELETE /api/growth/acquisition-costs/{id}` | `delete_acquisition_cost` | Agent removes cost entry |
| `GET /api/growth/nurture/{deal_id}` | `get_nurture` | Agent reads nurture sequence |
| `POST /api/growth/nurture/{deal_id}/generate` | `generate_nurture` | Agent creates nurture steps |
| `PATCH /api/growth/nurture/{step_id}` | `update_nurture` | Agent updates nurture step |
| `GET /api/growth/loops` | `get_growth_loops` | Agent sees growth loop status |
| `GET /api/scorecard` | `get_scorecard` | Agent reads weekly scorecard |
| `GET /api/crm/pipeline` | `get_pipeline` | Agent views full pipeline |
| `POST /api/crm/leads` | `create_lead` | Agent creates new lead |
| `POST /api/crm/deals/{id}/touch` | `touch_deal` | Agent logs sales touch |
| `POST /api/crm/deals/{id}/score` | `score_deal` | Agent scores a deal |
| `PATCH /api/crm/deals/{id}/growth` | `update_deal_growth` | Agent updates deal growth metrics |
| `GET /api/pipeline-math` | `get_pipeline_math` | Agent gets pipeline calculations |

### Priority 2: Sessions/Agents Control (16 endpoints)

Agents and Hermes cannot interact with Claude Code sessions programmatically via MCP. This limits remote orchestration.

| Endpoint | Proposed MCP Tool | Use Case |
|---|---|---|
| `GET /api/sessions/{host}/{name}/history` | `get_session_history` | Read what a session has done |
| `GET /api/sessions/{host}/{name}/output` | `get_session_output` | Read current session output |
| `POST /api/sessions/{host}/{name}/send` | `send_to_session` | Send command to Claude Code |
| `POST /api/sessions/{host}/{name}/resend-last` | `resend_last` | Retry last command |
| `POST /api/sessions/{host}/{name}/revive` | `revive_session` | Wake up idle session |
| `POST /api/sessions/{host}/{name}/kill` | `kill_session` | Terminate session |
| `POST /api/sessions/prune-transcripts` | `prune_transcripts` | Clean old transcripts |
| `GET /api/coordinators` | `list_coordinators` | See active coordinator sessions |
| `GET /api/sessions/{host}/{name}/tasks` | `get_session_tasks` | Tasks linked to session |
| `PATCH /api/tasks/{task_id}/session` | `link_task_session` | Bind task to session |
| `POST /api/sessions/{host}/{name}/compact` | `compact_session` | Trigger context compaction |
| `GET /api/compact-candidates` | `list_compact_candidates` | Sessions needing compaction |

### Priority 3: Task Lifecycle (12 endpoints)

Missing accept/reject/abort/fail operations and context queries.

| Endpoint | Proposed MCP Tool | Use Case |
|---|---|---|
| `POST /api/tasks/{id}/accept` | `accept_task` | Operator approves agent work |
| `POST /api/tasks/{id}/reject` | `reject_task` | Operator rejects agent work |
| `POST /api/tasks/{id}/abort` | `abort_task` | Abort in-progress task |
| `POST /api/tasks/{id}/fail` | `fail_task` | Mark task as failed (3-strike rule) |
| `POST /api/tasks/{id}/heartbeat` | `heartbeat` | Refresh task heartbeat |
| `DELETE /api/comments/{id}` | `delete_comment` | Remove a comment |
| `GET /api/context/{type}/{id}` | `get_context` | Get context packet for entity |
| `GET /api/icebox` | `list_icebox` | View icebox tasks |
| `GET /api/delivered` | `list_delivered` | View delivered tasks |
| `GET /api/projects/{id}/detail` | `get_project_detail` | Full project with tasks/epics |
| `GET /api/roadmap/{id}/events` | `get_initiative_events` | Roadmap event timeline |
| `GET /api/day-plan/candidates` | `get_day_plan_candidates` | Candidates for day planning |

### Priority 4: Sprint/Cycle Management (6 endpoints)

| Endpoint | Proposed MCP Tool | Use Case |
|---|---|---|
| `GET /api/sprints/{id}/tasks` | `get_sprint_tasks` | Tasks in a sprint |
| `GET /api/cycle/active/board` | `get_cycle_board` | Active cycle kanban board |
| `GET /api/cycles/calendar` | `get_cycles_calendar` | Cycle calendar view |
| `POST /api/cycles/roll` | `roll_cycle` | Roll to next cycle |
| `DELETE /api/sprints/{id}` | `delete_sprint` | Delete a sprint |
| `POST /api/cycles/{id}/commit` | `commit_cycle` | Commit cycle plan |

### Priority 5: Lakehouse & Specs (5 endpoints)

| Endpoint | Proposed MCP Tool | Use Case |
|---|---|---|
| `GET /api/lakehouse/overview` | `get_lakehouse_overview` | Data warehouse overview |
| `GET /api/lakehouse/ask` | `ask_lakehouse` | NL query to lakehouse (already in lakehouse MCP) |
| `GET /api/mcp/manifest` | `get_mcp_manifest` | List all MCP tools (introspection) |
| `PUT /api/specs/{feature}` | `write_spec` | Write/update a feature spec |
| `GET /api/ledger` | `get_ledger` | Global task ledger |

### Priority 6: Usage/Ops (9 endpoints)

| Endpoint | Proposed MCP Tool | Use Case |
|---|---|---|
| `GET /api/stats` | `get_stats` | Dashboard aggregate stats |
| `GET /api/errors/recent` | `get_recent_errors` | Recent error log |
| `GET /api/ops-status` | `get_ops_status` | Ops status (VMs, services) |
| `GET /api/usage/providers` | `list_usage_providers` | Provider breakdown |
| `POST /api/usage/refresh-ollama` | `refresh_ollama_usage` | Sync Ollama usage |
| `POST /api/usage/refresh-claude` | `refresh_claude_usage` | Sync Claude usage |
| `GET /healthz` / `GET /metrics` | — | Health checks, skip MCP (infra) |

### Priority 7: Session Meta/Events (7 endpoints)

| Endpoint | Proposed MCP Tool | Use Case |
|---|---|---|
| `GET /api/session-meta` | `get_session_meta` | Session metadata |
| `POST /api/session-meta` | `set_session_meta` | Update session metadata |
| `POST /api/session-events` | `create_session_event` | Log session event |
| `GET /api/session-events` | `list_session_events` | View session events |
| `POST /api/session-events/{id}/resolve` | `resolve_session_event` | Resolve an event |
| `POST /api/orchestration/sweep` | `orchestration_sweep` | Trigger orchestration sweep |

---

## Gap Analysis: MCP → Dashboard (15 tools without UI)

These MCP tools exist but have no dashboard visualization:

| MCP Tool | What It Does | UI Needed? |
|---|---|---|
| `assign_task` | Assign task to agent | Yes — in task drawer dropdown |
| `assign_task_epic` | Link task to epic | Yes — in task drawer |
| `create_epic` | Create new epic | Yes — in roadmap view |
| `create_initiative` | Create new initiative | Yes — in roadmap view |
| `dispatch_to_agent` | Send task to specific agent | Yes — in task drawer |
| `get_active_sprint` | Get current sprint | Yes — already shown indirectly |
| `get_initiative` | Get single initiative | Yes — in roadmap |
| `get_sprint` | Get single sprint | Yes — in sprint view |
| `get_task_links` | Get task dependencies | Yes — in task drawer |
| `list_deals` | List all deals | Yes — in CRM tab |
| `list_epics` | List all epics | Yes — in roadmap |
| `list_initiatives` | List all initiatives | Yes — in roadmap |
| `set_session_role` | Set session role | No — internal, dashboard shows it |
| `update_epic` | Update epic | Yes — in roadmap |
| `update_task_status` | Update task status | Yes — already in task drawer |

**Most of these are read operations that should surface in the Roadmap and CRM tabs.**

---

## Cross-MCP Server Analysis

| Server | Tools | Dashboard Exposure | Gap |
|---|---|---|---|
| **orchestrator** | 78 | 64 covered | 90 dashboard endpoints not exposed |
| **lakehouse** | 7 | 2 endpoints proxy to it | Dashboard has `/api/lakehouse/*` but MCP lakehouse has `get_lineage`, `get_impact` not exposed |
| **coolify** | 9 | 0 (BROKEN) | Fix Cloudflare UA, then expose in Ops tab |
| **gcloud** | 1 | 0 | Expose VM status/control in Ops tab |

---

## Recommended Implementation Plan

### Phase 1: Growth MCP (40 new tools) — HIGHEST IMPACT
The entire Growth system is agent-invisible. Agents can't read ICP, manage products, log time, score leads, or view coaching metrics.

- **Effort:** ~2 Claude Code sessions
- **Approach:** Wrapper functions calling existing dashboard API logic (reuse DB layer, don't duplicate)
- **Priority:** ICP + Products + Pipeline + Scorecard first (17 tools), Content/Speaking/Time-blocks second (13 tools), Nurture/Fireflies/Analytics last (10 tools)

### Phase 2: Session Control MCP (16 new tools)
Enables Hermes to programmatically interact with Claude Code sessions (send commands, read output, compact, kill, revive).

- **Effort:** 1 Claude Code session
- **Risk:** High — direct session manipulation. Need scope guards (only orchestrator role)

### Phase 3: Task Lifecycle MCP (12 new tools)
Accept/reject/abort/fail/heartbeat + context queries.

- **Effort:** 1 session
- **Priority:** accept/reject/abort first (operator actions), context/icebox/delivered second

### Phase 4: Sprint/Cycle + Lakehouse + Specs (11 new tools)
Sprint management, lakehouse overview, spec writing, global ledger.

- **Effort:** 1 session

### Phase 5: Usage/Ops + Session Meta (16 new tools)
Usage refresh, error log, session events, orchestration sweep.

- **Effort:** 1 session
- **Lower priority:** Most of these are dashboard-internal (health checks, metrics)

### Phase 6: Dashboard UI for MCP-only tools (15 tools)
Surface `list_deals`, `list_epics`, `list_initiatives`, `assign_task_epic`, `create_epic`, `create_initiative` in the dashboard UI.

- **Effort:** 1 session

---

## Summary

| Metric | Current | Target |
|---|---|---|
| MCP tools (orchestrator) | 78 | ~163 |
| MCP tools (all servers) | 95 | ~180 |
| Dashboard → MCP coverage | 41% (64/154) | ~100% |
| MCP → Dashboard coverage | 81% (63/78) | ~100% |
| Gap (dashboard-only) | 90 | 0 |
| Gap (MCP-only) | 15 | 0 |

**Total new MCP tools to build: ~85**  
**Estimated effort: 6-7 Claude Code sessions**  
**Priority order: Growth → Sessions → Task Lifecycle → Sprint/Lakehouse → Usage/Ops → Dashboard UI**

---

## Architecture Note

All new MCP tools should be **thin wrappers** over existing dashboard API logic. The dashboard's `api.py` already has the DB connections, validation, and business logic. The MCP server should import and call those functions directly, not duplicate SQL queries. This means:

1. Refactor `api.py` endpoints to extract logic into reusable functions (if not already)
2. MCP tool functions call those functions
3. Dashboard endpoints and MCP tools share the same code path

This prevents drift between what the dashboard shows and what agents see.