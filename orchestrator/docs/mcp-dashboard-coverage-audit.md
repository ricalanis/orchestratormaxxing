# MCP ↔ Dashboard Feature-Parity Audit

**Date:** 2026-07-09
**Goal:** every feature reachable through the MCP server (`mcp_server.py`) is also
reachable through the dashboard API (`dashboard/api.py`) **and** visible in the
dashboard UI (`dashboard/templates/*.html`), and vice-versa.

## Architecture note (why "gap" is a real feature question)

The MCP server and the dashboard API are **two parallel frontends over one shared
backend** (`dashboard/db.py`, `sprints.py`, `crm.py`, `growth.py`,
`graph_memory.py`, `memory_view.py`, `sessions.py`).

- ~44 MCP tools proxy to the HTTP API via `_dash(method, path)`.
- ~130 MCP tools call the backend modules / DB / `hermes` CLI **directly**.

So a tool having no matching API route (or a route having no matching tool) is a
genuine capability asymmetry, not just missing plumbing.

## Inventory totals

| Surface | Count |
|---|---|
| Registered MCP tools (`TOOLS` list, `mcp_server.py`) | 174 |
| Dashboard API routes (`@app.*`, `dashboard/api.py`) | 189 |
| Distinct `/api/*` paths referenced by the UI templates | 101 |

Reconciliation was done mechanically (verb + singularized-noun feature keys) and
every flagged item was then hand-verified against source (the matcher
false-positives on shared verbs like *archive*).

---

## Gap set A — MCP tool exists, no API endpoint

| MCP tool | Backend it calls | Status | Fix |
|---|---|---|---|
| `evolve_node` | `graph_memory.evolve_node()` | **REAL GAP** — no route | add `POST /api/graph/evolve` |
| `find_related` | `graph_memory.find_related()` | **REAL GAP** — `/api/graph?q=` is adjacent but not the same read | add `GET /api/graph/related` |
| `get_dashboard_url` | returns config URL string | not a gap (meta helper, nothing to serve) | — |
| `get_archive` | `db.get_archive()` | not a gap — `/api/archive` exists (matcher false-positive) | — |
| `contradiction_check` | `graph_memory.contradiction_check()` | not a gap — `/api/memory/contradiction` exists | — |

## Gap set B — API endpoint exists, no MCP tool

| API route | Backend it calls | Status | Fix |
|---|---|---|---|
| `GET /api/search` | omnibar over tasks/deals/sessions/memory | **REAL GAP** | add `tool_search` (`_dash` proxy) |
| `POST /api/crm/decay` | `crm.auto_stale_decay()` | **REAL GAP** (`archive_stale` is *graph* decay, different) | add `tool_crm_decay` |
| `GET /api/crm/stale` | `crm` stale-deal list | **REAL GAP** (partly covered by `get_pipeline_health`) | add `tool_get_stale_deals` |
| `GET /api/memory` | `memory_view.build()` (flat agent/user index) | **GAP** — MCP has graph `recall` but no flat-memory read | add `tool_get_memory` |
| `PATCH /api/memory` / `DELETE /api/memory` | edit/delete flat memory entry | **GAP** — no MCP write path | add `tool_update_memory` / `tool_delete_memory` |
| `GET /api/archive` | `db.get_archive()` | not a gap — `get_archive` tool exists | — |

## Gap set C — MCP tool + API endpoint exist, but no UI element

20 API routes have no UI reference. Most are **machine-facing by design** (an agent
loop calls them; a human never would) and are correctly UI-less:

`GET /api/pool`, `POST /api/claim-next`, `POST /api/discoveries`,
`GET /api/agent-status`, `GET /api/recall`, `GET /api/mcp/manifest`,
`GET /api/pipeline` (alias of `/api/crm/pipeline`),
`GET /api/cycle-board` (alias), `GET /api/icebox`, `GET /api/delivered`.

Genuine **UI gaps** (a human would want to see/trigger these). Re-verified against
the templates — several first-pass "gaps" already have UI and were struck out:

| Route | Tool | Verified UI status |
|---|---|---|
| ~~`GET /api/ledger`~~ | `get_ledger` | **has UI** — verification ledger renders per-task in the drawer (index.html:8958). Only a *global* ledger view is missing (low value). |
| `GET /api/compact-candidates` | `list_compact_candidates` | **UI gap** — no compaction-candidates widget in Sessions. |
| `POST /api/crm/decay` | `crm_decay` (new) | **UI gap** — a manual "run decay" trigger button (auto-decay still runs server-side). |
| `POST /api/growth/funnel-snapshot` | `capture_funnel` | **UI gap** — no manual snapshot button (snapshots are captured on a schedule). |
| `GET /api/errors/recent` | `get_recent_errors` | partial — surfaced via `/api/ops-status`; no dedicated feed. |
| `GET /api/stats` / `GET /api/activity` / `GET /api/health` | `get_stats`/`get_activity`/`get_health` | partial — folded into the ops-status widget. |

Each remaining item is a **convenience trigger/view for a feature already reachable
via both the API and (post-Batch-1) MCP** — not a capability an agent or the API
lacks. They are deferred as follow-up because `index.html` is under active
concurrent edit by parallel sessions in exactly these tab regions (the repo's
shared-tree doctrine warns against bundling into contended files), and JS UI
additions can't be end-to-end verified in this headless session.

---

## Fix log

- **Batch 1** ✅ (Gap set B — commit `0bbb9f3`): added MCP tools `search`,
  `get_memory`, `update_memory`, `delete_memory`, `get_stale_deals`, `crm_decay`
  (writes marked privileged). 174 → 180 registered tools.
- **Batch 2** ✅ (Gap set A — commit `cc0d765`): added `GET /api/graph/related`
  and `POST /api/graph/evolve`, plus `tests/test_mcp_api_parity.py` pinning both
  parity directions.
- **Batch 3** ⏸ (Gap set C — deferred): the remaining items are convenience
  UI triggers/views for features already reachable via API **and** MCP. Deferred
  because `index.html` is under active concurrent edit by parallel sessions in the
  target tab regions and JS UI can't be verified headless here. See the table above.

**Outcome:** bidirectional **MCP ↔ API parity is now complete** — every registered
MCP tool has an API path and every API endpoint has an MCP tool (verified
mechanically + by hand). The only residual asymmetry is a handful of
already-reachable features lacking a dashboard *button/view*.

Baseline before work: **408 passed, 14 pre-existing failures** (fireflies API-key,
icp-config, crm lead-scoring — all unrelated to parity). After Batches 1-2:
**413 passed, same 14 pre-existing failures, 0 new** (the +5 are the new parity tests).
