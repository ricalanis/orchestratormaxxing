---
name: weekly-public-contribution
description: Run a weekly, evidence-gated public capability contribution by wrapping the sibling omaxxing-public-improve and public-improve-security workflows. Resolves source/public repos from user context and Git, keeps evidence private, freezes a ten-domain baseline then a frozen delta, and preserves explicit publish authorization. Use for a weekly public contribution or projection audit; never auto-publishes.
---

Generic wrapper over the sibling [`omaxxing-public-improve`](../omaxxing-public-improve/SKILL.md)
and [`public-improve-security`](../public-improve-security/SKILL.md) skills. Load those for
the detailed workflow and five-pass procedure; this skill sets the weekly cadence and the
accounting/authorization boundaries.

## Resolve repos and rights

Resolve the source repository, public repository, branch and optional host locations from
the operator's request and verified Git metadata. Never infer a private source from a
sibling name and never default to a private repo. Require explicit rights to the public
repo before any write. Run full dependency and standalone checks for every selected
capability; an absent professional instance is excluded, not probed.

## Baseline, delta, backlog

1. **Full ten-domain baseline** (planning; delegation; execution/recovery;
   installation/host discovery; verification/CI; security/publication; memory/context;
   browser/design; autonomous improvement; shared-server/fleet services). Record which
   domains apply and why. This baseline is the reference for the week.
2. **Frozen delta.** Freeze the inclusive start / exclusive end timestamps and record
   source refs, statuses, host provenance, and public base. Inventory every discovered
   item and commit to it. Never use mtime as proof of completion.
3. **Backlog + oldest deeper review.** Keep a backlog of deferred/excluded items and
   select the oldest unreviewed area for deeper inspection. Review contiguous missed
   intervals before advancing the watermark.

## Accounting and checkpoints

- Keep evidence outside the public payload (ignored source-local directory or outside both
  repos). Public summaries name actual checks and limits without raw findings.
- Use stable candidate IDs across the run. Atomic checkpoints with **one writer**; a
  checkpoint is atomically replaced; its review status may remain partial. See `references/checkpoint.md` for the
  minimal checkpoint/register schema and the exclusive-writer atomic save procedure.
- **Completion means full inventory accounting, independent of shipping.** A week is
  complete when every discovered item in the frozen delta is accounted for (selected,
  already-present, deferred, excluded, or unverified-with-reason) — not when something
  shipped. Report one of three states:
  - **complete-with-candidates** — full accounting done and at least one capability is
    identified; preparation and publication may remain pending;
  - **full-scope no-work** — full accounting done and nothing is eligible to ship (the
    completed review is the result);
  - **incomplete-evidence partial** — accounting could not be completed because evidence
    is missing/inaccessible; report exactly what is missing and what was accounted.
- **Last-completed watermark.** Advance the last-completed watermark only after a week's
  full inventory accounting is done, never on shipping. Missing or corrupt state is
  reconstructed from the register; missed weeks are reviewed contiguously (each missed
  interval is itself accounted), not merely recorded as skipped.
- **Invalidation on changed refs:** a changed base/head invalidates prior security
  receipts and requires renewed review. **Uncertain-write reconciliation:** reconcile
  existing branch/PR state before retrying an uncertain write; never force-push.
- Preserve the shared repair budget (two rounds per selected capability) through
  delegation and security fixes.

## Authorization and publication

Default to review/preparation. Preserve explicit publish authorization and use the
inherited five passes from `public-improve-security` before any push or PR, including
drafts. A changed base/head or code change renews **all** inherited required security
reviews (the five passes) against the new refs before any write. Preserve existing
authorization: authorized local writes (e.g. the register/checkpoint save) are not
re-authorized on every run. No scheduler, exporter, private history, or public-tree
mirroring. Public-only additions survive; never rewrite a manifest to authorize an export.

Return the PR, truthful coverage, or a concrete blocker/no-work result supported by the
completed review.
