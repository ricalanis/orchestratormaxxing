# Private weekly state

Keep this state outside the outgoing payload. Validate schema and repository identities
before resuming. Missing or corrupt state means reconstruction and partial coverage;
never invent a previous completion watermark.

Use one versioned JSON document so candidate decisions and the checkpoint change together:

```json
{
  "schema_version": 1,
  "source_identity": "operator-verified source",
  "public_identity": "operator-verified destination",
  "run_id": "2026-09-07-review",
  "window": {"start_inclusive": "2026-08-31T00:00:00Z", "end_exclusive": "2026-09-07T00:00:00Z"},
  "source_refs": ["actual frozen commit"],
  "public_base": "actual public commit",
  "public_head": null,
  "review_status": "partial",
  "last_completed_watermark": null,
  "phase": "accounting",
  "coverage": [{"domain": "planning", "evidence": "private evidence reference", "last_deep_review": null, "limits": "delta only"}],
  "candidates": [{"id": "candidate-1", "disposition": "deferred", "evidence": "private evidence reference", "reason": "dependency closure pending", "reentry_condition": "runner available", "repair_count": 0, "public_pr": null}],
  "missing_evidence": [],
  "next_action": "inspect unresolved dependency"
}
```

Candidate dispositions: selected, already-present, deferred, excluded, unverified.
Review status: partial, complete-with-candidates, full-scope-no-work. Publication is a
separate phase and PR field, not the definition of review completion. Account for every
frozen item and record coverage limits; inaccessible required evidence keeps the review
partial. Advance the watermark only across contiguous fully accounted intervals.
Maintain stable IDs and prior decisions, including candidates not selected this week.

## Exclusive writer and atomic save

1. Acquire an exclusive lock on a stable sibling lockfile (Python `fcntl.flock` on
   macOS/Linux is suitable). Do not lock the data inode that will be replaced.
2. Under the lock, validate and read the latest state; reconcile interrupted or uncertain
   writes with actual branch/PR state before choosing the next action.
3. Create a private temporary file in the same directory, write the entire updated
   document, flush and `fsync`, then use atomic replace. Sync the parent directory where
   supported. Release the lock in a finally block and clean only the owned temporary file.
4. A failed save preserves the previous valid state. A fully written document may truthfully
   say partial: file atomicity never certifies review completion. Conflicting writers wait
   or report lock contention; they do not overwrite each other's decisions.

Recover missing/corrupt state from surviving frozen evidence and checkpoints; preserve the
corrupt file for private diagnosis. Re-run missing accounting and contiguous missed weeks.
If refs or evidence cannot be recovered, retain a partial result with the exact gap.
