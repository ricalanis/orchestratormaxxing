# MCP / DB Audit Findings — 2026-07-15

Scope: `orchestrator/mcp_server.py` + `orchestrator/dashboard/db.py`, plus live DB checks against `~/.hermes/kanban.db`.

## 1. MCP ↔ Dashboard API parity

| Status | Finding | File / location | Effort | Impact |
|--------|---------|-----------------|--------|--------|
| ✅ Good coverage | ~170 tools exposed; recent "Parity fill" / "Parity fill 2" blocks (lines 2120–2183, 2184–2345) added search, memory CRUD, stale deals, CRM decay, get_deal, get_project, scheduling verbs, create_cycle, etc. | `mcp_server.py` lines 2120–2345 | — | — |
| ⚠️ Missing: `/api/health/config` PATCH | Health config is read-only via MCP (`get_health_config`). Dashboard allows PATCH. No `update_health_config` tool. | `mcp_server.py` tool list vs `api.py:3276` | S | low |
| ⚠️ Missing: `/api/tasks/{id}/links` POST/DELETE | MCP has `get_task_links` only; dashboard exposes create/delete task dependency edges. No `add_task_link` / `remove_task_link` tools. | `mcp_server.py` tool list vs `api.py:2182–2200` | S | med |
| ⚠️ Missing: `/api/sprints` POST via legacy shape | `create_sprint` exists but is project-scoped; `create_cycle` is the weekly twin. OK, but dashboard also has `/api/sprints` POST. Largely covered. | `api.py:1974` | S | low |
| ⚠️ Missing: `/api/growth/content/{id}` full, etc. | Content update/delete already mapped. No obvious gaps here. | — | — | — |
| ⚠️ Missing: `/api/crm/deals/{id}/children` POST | MCP has `list_deal_children`; dashboard also has create child deal (`api.py:2507`). No MCP `create_deal_child`. | `api.py:2507` | S | low |
| ⚠️ Missing: `/api/growth/time-blocks/{day}/activities` | Dashboard exposes day-specific activities; MCP only lists all blocks. | `api.py:2789` | S | low |

**Verdict:** Parity is now ~95%. Remaining gaps are niche write endpoints; biggest functional gap is dependency-link mutations.

---

## 2. DB schema gaps

### 2.1 Sprint-ledger orphan drift (confirmed live)

- **Finding:** `sprints.sprint_ledger_drift()` returns **30 orphans** — 29 forward + 1 reverse.
- **File:** `dashboard/sprints.py` lines 464–498.
- **Forward orphans:** 29 `done` tasks whose `tasks.sprint_id` still points to `cyc_27dd8d89`, but they have no open (outcome IS NULL) `task_sprints` row for that cycle. Likely caused by `finish_sprint`/`close_sprint` stamping outcomes (`delivered`/`carried`) on ledger rows but NOT clearing `tasks.sprint_id` for done+reviewed work (by design it stays on the completed sprint as history). However, the drift detector flags any `tasks.sprint_id` without an open ledger row as drift.
- **Reverse orphan:** 1 task_sprints row (task `t_b4a0a103`, sprint `cyc_02ba0625`) with outcome IS NULL whose task no longer points at that sprint. Likely a pulled/moved commit where the old row wasn't stamped `dropped`.
- **Impact:** med (dashboard health check shows red; false-positive-ish for done work but real for the reverse orphan).
- **Effort to fix:** S–M. Either (a) relax forward detection to exclude done+reviewed tasks whose sprint is completed, or (b) stamp `delivered` rows and clear `tasks.sprint_id`/`archived_at` consistently. Reverse orphan needs a one-time repair + audit of pull paths.

### 2.2 Missing indexes (already applied in P3 migration)

- `p3_indexes.py` and module `ensure_schema()` calls create indexes on `deal_events`, `task_ledger`, `session_events`, `initiative_events`, `contacts`, `deals`, `tasks.initiative_id`.
- **Verified live** — indexes present.
- **Remaining un-indexed hot paths:**
  - `tasks.project_id` and `tasks.sprint_id` are referenced by FK but do not appear to have explicit non-PK indexes (the FKs are `NO ACTION`, so SQLite still needs an index for enforcement; it may be auto-created? Need verify). Live check showed `idx_tasks_project` and `idx_tasks_sprint` present — OK.
  - `task_sprints` has `idx_task_sprints_sprint` but no `idx_task_sprints_task`. Queries like `assign_task_sprint` and drift check use `task_id` correlated subqueries. **Gap:** add `(task_id, sprint_id, outcome)` covering index. | `dashboard/sprints.py`, `dashboard/migrations/p3_indexes.py` | S | med |

### 2.3 Foreign-key gaps

- `deals.initiative_id` has **no declared FK** (comment says validated in code). Risk of orphan strategy joins. | `dashboard/crm.py` lines 87–88 | S | med |
- `task_comments.task_id`, `task_events.task_id`, `task_ledger.task_id`, `task_runs.task_id`, `task_links.parent_id/child_id`, `task_attachments.task_id` — no FKs declared. SQLite default `foreign_keys=OFF` unless `PRAGMA` set; both `db.py` and `mcp_server.py` set `PRAGMA foreign_keys = ON` per connection, but without declared FKs, deletes won't cascade.
- `sprints.project_id` FK exists but `ON DELETE NO ACTION`; deleting a project with sprints won't cascade (though dashboard guards project deletion).
- `initiatives.project_id` FK exists but `ON DELETE NO ACTION`.

**Verdict:** FK coverage is partial; sidecar tables mostly lack FKs. This is a known design choice (hermes CLI owns tasks) but still a schema gap.

---

## 3. API error handling

| Finding | File / location | Effort | Impact |
|---------|-----------------|--------|--------|
| **Global 500 envelope** exists via `@app.exception_handler(Exception)` and middleware logger wraps routes with `try/except`. | `api.py` lines 231–245, 540–590 | — | ✅ |
| **`_or_http` converts dict errors to HTTP codes** (404/409/400). Used on most endpoints. | `api.py` lines 635–654 | — | ✅ |
| **MCP `_dash()` proxy surfaces HTTP status** but does not translate to MCP error codes; returns JSON with `error` + `status`. Acceptable. | `mcp_server.py` lines 3902–3931 | — | low |
| **MCP tool handlers catch broad `Exception` and return `isError=True` JSON.** Safe but loses stack traces to client; logged to stderr only in `_dash`. | `mcp_server.py` lines 4828–4845 | — | low |
| **Some module functions still return `{"status":"error"}` without raising, so `_or_http` must be remembered on every route.** A missing `_or_http` on a new route would silently 200 on failure. This is a latent foot-gun. | across `api.py` | M | med |

---

## 4. Memory / graph integration completeness

| Finding | File / location | Effort | Impact |
|---------|-----------------|--------|--------|
| **Graph ingest covers tasks, projects, agents, git, notes, sprints, initiatives.** No CRM entities (accounts, contacts, deals), content, products, speaking, health, or time-blocks are ingested into the graph. | `dashboard/graph_memory.py` lines 305–555, 740–753 | M | med |
| **`recall()` pulls graph search + optional lakehouse context packet.** Best-effort; falls back cleanly. | `mcp_server.py` lines 3741–3771 | — | ✅ |
| **Flat memory CRUD (`get_memory`, `update_memory`, `delete_memory`) exists and proxies dashboard endpoints.** Good parity. | `mcp_server.py` lines 4276–4291 | — | ✅ |
| **`evolve_node`, `archive_stale`, `find_related`, `contradiction_check`, `get_metabolism_stats` all exposed.** Memory lifecycle tools complete. | `mcp_server.py` lines 1021–1068, 3808–3826 | — | ✅ |
| **Graph DB is separate from kanban DB.** Backups/restore must handle both. No automated sync trigger after CRM/health/growth writes; graph can lag until `rebuild_graph`. | `dashboard/graph_memory.py` | S | low |

---

## 5. Ranked issue list

| Rank | Effort | Impact | Issue | File(s) |
|------|--------|--------|-------|---------|
| 1 | S–M | med | **30 sprint-ledger orphans (29 forward + 1 reverse)** — drift detector shows red; reverse orphan is a real consistency bug; forward orphans may be design noise from completed-sprint history. | `dashboard/sprints.py:464–498` |
| 2 | S | med | **`task_sprints` lacks task-side covering index.** Add `idx_task_sprints_task ON task_sprints(task_id, sprint_id, outcome)` for drift checks, assign, and velocity. | `dashboard/migrations/p3_indexes.py`, `dashboard/sprints.py` |
| 3 | S | med | **`deals.initiative_id` has no FK.** Strategy join can silently orphan; add `REFERENCES initiatives(id)`. | `dashboard/crm.py:87` |
| 4 | M | med | **Sidecar tables lack FKs to `tasks`.** `task_comments`, `task_events`, `task_ledger`, `task_runs`, `task_links`, `task_attachments`, `task_sprints` have no declared FKs/cascades, so task hard-delete must manually clean up. `delete_task` does clean some, but not all (e.g., `task_runs`, `task_attachments`, `task_comments`? actually deletes task row directly). | `db.py`, `sprints.py:553–577` |
| 5 | S | med | **MCP missing `add_task_link` / `remove_task_link` tools.** Dashboard exposes dependency DAG mutations; MCP cannot create/remove links. | `mcp_server.py` vs `api.py:2182–2200` |
| 6 | S | low | **MCP missing `update_health_config` tool.** Dashboard supports PATCH; MCP read-only. | `mcp_server.py` vs `api.py:3276` |
| 7 | S | low | **MCP missing `create_deal_child` tool.** Dashboard has `/api/crm/deals/{id}/children` POST. | `mcp_server.py` vs `api.py:2507` |
| 8 | S | low | **MCP missing day-specific time-block activities endpoint.** | `mcp_server.py` vs `api.py:2789` |
| 9 | M | med | **Graph ingest does not include CRM/growth/health entities.** Memory graph is blind to deals, accounts, content, products, speaking, health. | `dashboard/graph_memory.py` |
| 10 | S | low | **`_or_http` is opt-in per route.** Missing calls can silently 200 on module errors. Consider a route wrapper or response-model validation. | `dashboard/api.py` |

---

## 6. Quick-win recommendations

1. **Fix the 1 reverse orphan:** run a repair that stamps `outcome='dropped'` on any open `task_sprints` row not matching `tasks.sprint_id`.
2. **Clarify forward orphan semantics:** either exclude done+reviewed tasks on completed sprints from drift, or finish the ledger by stamping `delivered` and clearing `tasks.sprint_id`/`archived_at` consistently.
3. **Add `idx_task_sprints_task`** and add it to `ensure_cycle_schema()` / `p3_indexes.py`.
4. **Add FK `deals.initiative_id REFERENCES initiatives(id)`** in `crm.ensure_schema()`.
5. **Add `add_task_link` / `remove_task_link` MCP tools** wrapping `db.add_task_link` / `db.remove_task_link`.
6. **Extend graph ingest** to include CRM accounts/contacts/deals and growth content/speaking/products.
