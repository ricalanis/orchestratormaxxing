# weekly-public-contribution — synthetic examples

## Interrupted run

A weekly run is interrupted during inventory accounting. Save a valid atomic checkpoint
with `review_status: partial`, frozen refs, stable candidate IDs and the next action.
Resume that state and finish the missing accounting; the completion watermark remains
unchanged until then. If interruption happens after full accounting but before security
review, the review may be complete-with-candidates while publication remains pending.
Atomic file replacement and review completion are separate properties.

## Week-empty (full-scope no-work)

A week has no eligible new capability (everything already shipped or nothing changed).
The skill returns **full-scope no-work**: the full ten-domain baseline was reviewed, the
delta was fully accounted, and the completed review is the result. It does not manufacture
a change to justify a contribution. The watermark advances because accounting completed,
even though nothing shipped.

## Complete-with-candidates

A week has full inventory accounting and two capabilities are selected and ready for the
security/review path. The skill reports **complete-with-candidates** with the stable
candidate IDs; the watermark advances on the accounting, and the candidates proceed through
the inherited five passes before any publish.

## Incomplete-evidence partial

A week's delta references a source ref that is unreachable, so some items cannot be
accounted. The skill reports **incomplete-evidence partial** with exactly what is missing
and what was accounted; the watermark does not advance. On the next run the missing
evidence is re-attempted and the missed interval is reviewed contiguously, not skipped.

## Public-drift

Between the baseline and sign-off, the public base branch moves (a changed ref). The
skill detects the drift, invalidates the prior security receipts, refreshes the base, and
renews **all** inherited required security reviews from `public-improve-security` (the
five passes: confidentiality/export, automated analysis, projection/trust boundaries,
independent adversarial review, and exact-artifact sign-off) against the new base/head
before any write. It never publishes an unverified tree against the stale base.

## Uncertain write

A push fails with an ambiguous error. The skill reconciles existing branch/PR state
before retrying, never force-pushes, and reports the concrete blocker if the state cannot
be reconciled. Authorized local writes (e.g. the register/checkpoint save) are not
re-authorized on every run — existing authorization is preserved.
