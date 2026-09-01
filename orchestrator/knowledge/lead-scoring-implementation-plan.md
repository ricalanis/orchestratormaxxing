# Lead Scoring + Fireflies Integration — Implementation Plan

## Source docs
- `~/.hermes/orchestration/specs/crm-lead-scoring/spec.md`
- `knowledge/lead-scoring-model.md`
- `knowledge/lead-scoring-design.md`
- `knowledge/lead-scoring-tasks.md`

## Goal
Ship a working lead scoring + Fireflies integration in the dashboard:
- Fireflies API client (`dashboard/fireflies.py`)
- Rich 4-category scoring engine (`dashboard/growth.py`)
- Schema additions (`dashboard/db.py`)
- CRM reads enriched with scores + Fireflies signals (`dashboard/crm.py`)
- Dashboard UI: badges, breakdown modal, Fireflies panel, Growth strategy section (`dashboard/templates/index.html`)
- New API endpoints (`dashboard/api.py`)
- New MCP verbs (`mcp_server.py`)

## Execution order
1. Schema (db.py)
2. Fireflies client (fireflies.py)
3. Lead scoring engine (growth.py)
4. CRM query enrichment (crm.py)
5. API endpoints (api.py)
6. MCP verbs (mcp_server.py)
7. Dashboard UI (index.html)
8. py_compile + restart dashboard + verify on localhost:3000

## Key decisions
- Keep additive DB changes only (ALTER + new table); never drop columns.
- Fireflies client must gracefully return `no_api_key` when key missing.
- Score output shape follows `lead_score_details` JSON spec from model doc.
- Badge colors: red 0-30, yellow 31-60, green 61-100.
- Growth strategy section is a new Strategy sub-view.

## Verification
- `python -m py_compile` on all modified `.py` files.
- Restart dashboard: kill old `dashboard.api`, start with `DASHBOARD_BIND=0.0.0.0 .venv/bin/python -m dashboard.api`.
- `curl -s http://localhost:3000/api/health` (or available health endpoint) returns 200.
- Open dashboard and confirm CRM deal cards show scores and Growth section renders.
