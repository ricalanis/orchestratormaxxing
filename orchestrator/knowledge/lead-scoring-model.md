# Lead Scoring Model — CRM + Fireflies Integration

## Overview
0–100 weighted-sum score built on the existing `deals.lead_score` field.
Score is stored with a full JSON breakdown so the dashboard can render the modal.

## Category weights
| Category | Weight | Rationale |
|----------|--------|-----------|
| Firmographic | 30 | Static fit: industry + source quality + client profile |
| Behavioral | 30 | Recency/frequency: touches, engagement, meeting attendance |
| Fireflies signals | 25 | Discovery-call quality from transcripts |
| Product fit | 20 | Track / ladder / explicit product interest + expected deal value |

## Sub-features and formulas

### Firmographic (30 pts)
- **Source quality** (0–12): `referral=12, inbound=10, linkedin=8, event=6, other/website=4, cold_email=2`.
- **Industry ICP match** (0–12): exact match to `icp_config().industries` → 12; partial/known sector → 6; unknown → 3.
- **Client profile fit** (0–6): A/B/C profile from `deals.client_profile`. **Value-based inversion:** C (grande ágil, high fractional retainer probability) = 6, B = 4, A (startup) = 2, missing = 0.

### Behavioral (30 pts)
- **Touch count** (0–10): response-rate based once CRM stores prospect replies.
  - `response_rate = touches_with_response / total_touches`
  - ≥50% → 10; 25–49% → 7; 10–24% → 4; <10% → 1; 0 touches → 0
  - *Current fallback:* until explicit reply data is wired, the model keeps the legacy per-touch score (2 pts per touch, cap 10) and marks a TODO for future refinement.
- **Engagement recency** (0–10): only counts if the lead has been touched at least once.
  - `last_touch_date` ≤7d → 10; ≤14d → 7; ≤30d → 4; older → 0
  - 0 touches → 0 (a touch every 3 days on a cold lead is spam, not engagement).
- **Meeting attendance / discovery held** (0–10): any `discovery_call` event in last 14d → 10; any meeting/call event → 6; none → 0.

### Fireflies signals (25 pts)
Derived from the latest stored Fireflies meeting for the deal (see `dashboard/fireflies.py`).
- **Talk-listen ratio** (0–8): now measures **Ricardo's** talk ratio. Lower Ricardo = better discovery.
  - Ricardo talk ratio ≤45% → 8 (on target, listening well)
  - 46–55% → 5 (improving)
  - 56–64% → 2 (current baseline, needs work)
  - >64% → 0 (talking too much)
- **Questions asked** (0–6): ≥6 prospect questions → 6; 4–5 → 4; 2–3 → 2; 0–1 → 0.
- **Filler words** (0–3): low filler density (<3%) → 3; <5% → 1; ≥5% → 0.
- **Action items mentioned** (0–4): ≥3 action items → 4; 1–2 → 2; none → 0.
- **Sentiment / topics** (0–4): positive sentiment or next-step/budget/timeline topics → 4; neutral → 2; negative → 0.

### Product fit (20 pts)
- **Track match A/B** (0–5): deal has a product track or initiative track aligned → 5; partial → 2.
- **Product interest expressed** (0–5): product_id set or explicit product interest in notes/features → 5; inferred from source → 2.
- **Value-ladder stage fit** (0–5): all stages have equal potential — a lead entering through Scorecard (imán) is just earlier in the funnel. Any `value_ladder_stage`, `product_id`, or identified track → 5; no product fit identified → 0.
- **Expected deal value** (0–5): `deal.value` if set, otherwise profile estimate (A=$18K, B=$45K/mes, C=$45K/mes). Normalize: ≥$80K → 5; $40–79K → 4; $20–39K → 3; $10–19K → 2; <$10K → 1; no value → 0.

## Client profiles A/B/C
Taken from the existing `CLIENT_PROFILES` in `growth.py`. Scoring is **value-based**, not ease-of-close:
- **A — Startup sin infra** (share 40%): high build-fit, lower fractional-fit. Score bonus **+2** profile points.
- **B — Mediana con equipo** (share 35%): balanced build + fractional. Score bonus **+4** profile points.
- **C — Grande ágil** (share 25%): low build-fit, high fractional-fit, $45K/mes recurring potential. Score bonus **+6** profile points.

The profile also influences the **recommended track** shown in the dashboard: A→B (build), B→A+B, C→A (fractional/data).

## Fireflies GraphQL mapping
Use Fireflies `transcripts` list query filtered by `date` and participant email/domain:
```graphql
query {
  transcripts(limit: 10) {
    id title date
    participants { displayName email }
    summary { overview action_items key_points }
    sentences { speaker_name text duration }
  }
}
```
Signal extraction:
- `talk_ratio` → sum `duration` of non-Ricardo sentences / total duration.
- `questions` → count sentences by non-Ricardo containing `?`.
- `filler_words` → count of "um", "uh", "o sea", "entonces", "like" / total words.
- `action_items` → length of `summary.action_items` array.
- `sentiment` → keyword scan of `summary.overview` and `key_points` for positive/negative markers + next-step/budget/timeline topics.

## Rules-based baseline → GBM upgrade path
Current implementation is a transparent weighted sum.
When ~100 scored deals with outcome labels (`won`/`lost`) are accumulated, switch to a small Gradient Boosting classifier (e.g., scikit-learn `HistGradientBoostingClassifier` or `xgboost`) using the same feature vector plus won/lost label. Keep the rules-based model as fallback and as a model-interpretability baseline.

## Output shape
Stored in `deals.lead_score` (INTEGER) and `deals.lead_score_details` (JSON):
```json
{
  "score": 73,
  "categories": {
    "firmographic": {"value": 24, "max": 30, "sub": {"source": 10, "industry": 12, "profile": 2}},
    "behavioral": {"value": 22, "max": 30, "sub": {"touches": 8, "recency": 7, "meeting": 7}},
    "fireflies": {"value": 18, "max": 25, "sub": {"talk_ratio": 5, "questions": 4, "filler": 2, "actions": 4, "sentiment": 3}},
    "product_fit": {"value": 9, "max": 20, "sub": {"track": 5, "interest": 2, "ladder": 2, "expected_value": 0}},
  },
  "profile": "B",
  "profile_note": "Mediana con equipo — recomendado: A+B"
}
```
