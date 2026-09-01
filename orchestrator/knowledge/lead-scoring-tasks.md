# Lead Scoring + Fireflies Integration — Code Task Breakdown

## A. Fireflies API client — new file `dashboard/fireflies.py`
Functions:
- `_api_key() -> str | None`: read `FIREFLIES_API_KEY` from env or `~/.hermes/.env` manually (per skill pitfall).
- `_graphql(query, variables) -> dict`: POST to `https://api.fireflies.ai/graphql` with `Authorization: <key>`.
- `fetch_transcripts(limit=25, after_date=None) -> list[dict]`: run the list query; return lightweight transcript summaries.
- `extract_signals(transcript: dict) -> dict`: compute talk_ratio, questions, filler_density, action_items, topics, sentiment.
- `meetings_for_deal(deal_id: str) -> list[dict]`: match transcripts by contact email / account domain.
- `latest_signals_for_deal(deal_id: str) -> dict | None`: latest meeting signals or `None`.

GraphQL query shape (list, reliable):
```graphql
query Transcripts($limit: Int) {
  transcripts(limit: $limit) {
    id title date
    participants { displayName email }
    summary { overview action_items key_points }
    sentences { speaker_name text duration }
  }
}
```

Acceptance: `python -m py_compile dashboard/fireflies.py` passes; calling without an API key returns `{"status":"no_api_key"}` gracefully.

## B. Lead scoring engine — `dashboard/growth.py`
Extend existing scoring:
- Add `deals.client_profile` enum (A/B/C) via additive ALTER.
- Add helper `_profile_fit(profile: str) -> int`.
- Add helper `_behavioral_score(deal: dict, events: list) -> dict` using `touch_count`, `last_touch_date`, discovery events.
- Add helper `_fireflies_score(signals: dict | None) -> dict`.
- Add helper `_product_fit_score(deal: dict, product: dict | None) -> dict`.
- Rewrite `score_features(account_type, source, engagement_score, industry, deal=None, signals=None, product=None) -> dict` to return the full 4-category breakdown.
- Update `set_lead_features(...)` signature to accept optional `client_profile`, `product_id`, and `fireflies_signals`, persist `lead_score_details` JSON, and call new scoring.
- Add `score_deal(deal_id: str) -> dict`: load deal + latest Fireflies signals + product + events, compute and persist score.
- Add `score_all_leads() -> dict`: iterate all non-closed deals, call `score_deal`, return counts.

Acceptance: pure-function scoring returns expected shape; existing tests/usage still import `growth.score_features` without breaking.

## C. DB schema changes — `dashboard/db.py`
Exact SQL to add in `ensure_schema()` or a new `ensure_fireflies_schema()`:
```sql
ALTER TABLE deals ADD COLUMN client_profile TEXT;
ALTER TABLE deals ADD COLUMN lead_score_details TEXT;  -- JSON

CREATE TABLE IF NOT EXISTS fireflies_meetings (
    id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    transcript_id TEXT NOT NULL,
    title TEXT,
    meeting_date TEXT,
    duration_seconds INTEGER,
    signals TEXT,  -- JSON
    raw_summary TEXT,
    fetched_at INTEGER,
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fireflies_deal ON fireflies_meetings(deal_id, meeting_date DESC);
CREATE INDEX IF NOT EXISTS idx_fireflies_transcript ON fireflies_meetings(transcript_id);
```
Add helper functions:
- `ensure_fireflies_schema()`
- `fireflies_meeting_insert(row)`
- `fireflies_meetings_for_deal(deal_id)`
- `fireflies_latest_for_deal(deal_id)`

## D. CRM API changes — `dashboard/crm.py`
- `list_deals()` SELECT to include `lead_score`, `lead_score_details`, `client_profile`.
- `get_deal()` to include `lead_score_details`, `client_profile`, `fireflies_signals`, `fireflies_meetings`.
- Add `get_deal_fireflies(deal_id: str) -> dict` returning latest signals + meetings list.

## E. Dashboard UI changes — `dashboard/templates/index.html`
- In the deal card renderer and All Deals table: add score badge span with color class and `onclick="openLeadScoreModal('${d.id}')"`.
- Add modal HTML near other modals.
- Add `openLeadScoreModal(dealId)` JS function that fetches `/api/crm/deals/${dealId}` and renders breakdown bars.
- Add Fireflies panel render function `renderFirefliesPanel(signals)` and insert it into deal drilldown + CRM tab.

## F. Growth strategy section
- In Strategy workspace subnav builder, add "Growth" button that switches `strategySubView` to `growth`.
- Add `renderStrategyGrowth()` JS function calling `/api/growth/lead-scores` and `/api/growth/dashboard` (existing) and drawing the layout described in design spec.

## G. MCP verbs — `mcp_server.py`
Add / extend:
- `score_deal(deal_id, ...)` — existing but route through new `growth.score_deal`.
- `score_all_leads()` → calls `growth.score_all_leads()`.
- `get_fireflies_signals(deal_id)` → calls `crm.get_deal_fireflies(deal_id)` or `fireflies.latest_signals_for_deal(deal_id)`.
- `fetch_fireflies_for_deal(deal_id)` → calls Fireflies API and stores meetings.

## H. API endpoints — `dashboard/api.py`
Add FastAPI routes:
- `GET /api/crm/deals/{deal_id}/fireflies`
- `POST /api/crm/deals/{deal_id}/score`
- `POST /api/fireflies/fetch/{deal_id}`
- `GET /api/growth/lead-scores`

Wire into existing `dashboard.api` patterns and CORS.

## Execution order
1. D (schema) → 2. A (Fireflies client) → 3. B (scoring engine) → 4. D (CRM queries) → 5. H (API) → 6. G (MCP) → 7. E+F (UI) → 8. py_compile + restart + verify.
