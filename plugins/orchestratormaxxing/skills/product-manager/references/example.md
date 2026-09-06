# product-manager — worked example

## Invented small example

A team supplies authorized evidence: "We want to let customers export their data as CSV
and JSON." The skill returns a proposal-only tree with explicit joins and boolean
acceptance, using provisional IDs because the source gave none.

```
Initiative provisional-1: Customer data export
  Epic provisional-1.1 (initiative_id=provisional-1): Export API
    Task provisional-1.1.1 (epic_id=provisional-1.1): Add GET /export endpoint
      context: authenticated users export their own data
      intent: expose a stable export endpoint
      acceptance: GET /export returns only the fixture user's rows; unauthenticated requests return 401
      project: platform-api | priority: high | assignee: (proposed) backend
    Task provisional-1.1.2 (epic_id=provisional-1.1): Add JSON format option
      context: customers need a machine-readable export
      intent: support the same authorized records in JSON
      acceptance: ?format=json returns only the authenticated fixture user's records
      project: platform-api | priority: high | assignee: (proposed) backend
      depends_on: provisional-1.1.1
  Epic provisional-1.2 (initiative_id=provisional-1): Export UI
    Task provisional-1.2.1 (epic_id=provisional-1.2): Add export button on settings page
      context: customers use their account settings to manage data
      intent: expose the authorized export action
      acceptance: clicking export downloads the fixture user's CSV without a page reload
      project: web-app | priority: medium | assignee: (proposed) frontend
      depends_on: provisional-1.1.1
```

Every task carries `project` and joins to its epic by `provisional-1.1` / `provisional-1.2`;
no orphan work. Each leaf has a boolean acceptance criterion. Dependencies are surfaced
(e.g. `1.1.2` depends on `1.1.1`). The output ends with `Changed / Proposed` — here
everything is `Proposed`; nothing is written to a live CRM or task system.

## Underspecified / orphan decision cases

- **Underspecified leaf.** A task "improve search" has no boolean acceptance. The skill
  marks it `underspecified` and asks for a measurable criterion (e.g. "p95 search latency
  under 300ms on the fixture corpus") rather than inventing one silently.
- **Orphan task.** A task references `epic-9` that does not exist in the supplied evidence.
  The skill flags the orphan with its location and proposes either a join to a real epic
  or a new epic, and does not silently drop the task.
- **Contradictory status.** Two rows mark the same task both `in_progress` and `done`.
  The coherence audit returns one row naming the contradiction and the proposed fix
  (confirm which status is live), without guessing.
