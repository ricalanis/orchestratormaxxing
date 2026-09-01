# Lead Scoring + Growth Dashboard UX Design

## Components

### 1. Lead score badge on deal cards
- **Where**: every deal card in the CRM stage pipeline (`renderCRMDealCard`) and every row in the All Deals table.
- **Visual**: circular badge showing the integer score; color by band — red 0-30, yellow 31-60, green 61-100. Use Tailwind classes `bg-red-500/20 text-red-300`, `bg-yellow-500/20 text-yellow-300`, `bg-emerald-500/20 text-emerald-300`.
- **Interaction**: click opens the Lead Score Breakdown Modal (`openLeadScoreModal(dealId)`).
- **Data needed**: `lead_score` and `lead_score_details.score` from deal object.

### 2. Lead score breakdown modal
- **Where**: global modal area in `index.html`, reused from existing modal pattern.
- **Layout**:
  - Header: score badge large + account name + deal title.
  - 4 horizontal bars, one per category, showing `value / max` with percent width.
  - Expandable sub-bars for each sub-feature (source, industry, profile, touches, recency, meeting, talk ratio, questions, filler, action items, sentiment, track, interest, ladder).
  - Footer: profile badge + recommended next action (e.g., "Schedule discovery call", "Send proposal").
- **Data needed**: `lead_score_details.categories` + `profile` + `profile_note`.
- **Interaction**: opened from badge; close with X or Escape.

### 3. Fireflies meeting signals panel
- **Where**: inside the deal drilldown drawer (`crm-drilldown`) and as a standalone card in the CRM tab below the pipeline.
- **Layout**: compact card titled "🔥 Latest Fireflies signals" showing:
  - Talk ratio (progress bar)
  - Questions count
  - Filler word density
  - Action items count
  - Top 3 topics as chips
  - Sentiment badge
- **Data needed**: `fireflies_signals` object returned by `GET /api/crm/deals/{id}/fireflies` (or embedded in `get_deal()`).
- **Empty state**: "No Fireflies meeting linked yet. Add a meeting or connect Fireflies API key."

### 4. Growth as a first-class strategy area
- **Where**: Strategy workspace (`ws-strategy`) gets a new subnav item "Growth" alongside existing roadmap views.
- **Layout**:
  - Top row: pipeline math card, ICP card, CLTV:CAC card.
  - Middle row: growth loops (existing), client profiles (existing), value ladder (existing).
  - Bottom row: lead scoring health — distribution histogram, top scored deals, cold deals.
- **Data needed**: `/api/growth/dashboard` (existing) plus a new `/api/growth/lead-scores` endpoint returning score distribution + top deals.

### 5. Strategy section restructure
- **Where**: Strategy workspace subnav.
- **Change**: add "Growth" tab next to "Roadmap" / "ICP" / "Products". The Growth tab shows growth initiatives (deals with initiative links tagged as growth) mixed with product/agent initiatives.
- **Initiative grouping**: in the roadmap list, add a `kind` badge (`growth` | `product` | `agent-ops`). Growth initiatives are those whose project slug starts with `growth-` or whose title contains "Growth" / "Lead Gen" / "Pipeline".
- **Data needed**: same `/api/roadmap` endpoint; UI adds a filter chip for `kind:growth`.

## API contracts
- `GET /api/crm/deals` → include `lead_score`, `lead_score_details`, `fireflies_signals` (latest).
- `GET /api/crm/deals/{id}` → include same fields + `fireflies_meetings` list.
- `GET /api/crm/deals/{id}/fireflies` → latest signals + meetings list.
- `GET /api/growth/lead-scores` → `{distribution: [{band, count}], top: [...], cold: [...]}`.
- `POST /api/crm/deals/{id}/score` → trigger rescore.
- `POST /api/fireflies/fetch` → fetch latest meetings for a contact email.

## Mobile/sizing
- Badge is fixed 28px circle; modal max-w-2xl; Fireflies panel is full-width on mobile, 1/3 width on desktop.
