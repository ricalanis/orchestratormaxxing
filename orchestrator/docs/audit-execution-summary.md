# Audit Execution Summary — 2026-07-09

Execution pass over the three multi-expert audits ([design](design-audit.md) ·
[product](product-audit.md) · [dev](dev-audit.md)) and the
[improvement plan](improvement-plan.md).

## Headline

**The audit was already ~90% executed by prior sessions/workers.** Every Top-3
finding across all three audits was verified *landed* before this pass (see the
"Already done — verified" table). This session completed the **genuinely
remaining clean work**: three commits closing the last open perf item, locking
in a load-bearing but untested fix, and finishing an event-loop-hygiene sweep.

Per the harness anti-reward-hacking doctrine (a round must resolve a *real*
flaw, not manufacture marginal ones), this pass did **not** pad to an arbitrary
count of five — it shipped the three real improvements that existed and stopped.

Test floor held throughout: **416 → 422 passing** (+6 new tests), 13 skipped.

## Constraint honored: shared working tree

A parallel session was mid-flight on a Growth/CRM feature (Revenue-mix, Client
profiles, ICP positioning, products `track` column) with uncommitted hunks in
`db.py`, `growth.py`, `roadmap.json`, `index.html`. Per the
`orchestrator-shared-tree-multisession` rule, **only my own hunks were staged**
(explicit `git add <file>`, never `-A`); the parallel session's files were left
untouched and it committed its own work (`1f526f6`) independently.

## What this session shipped

| # | Commit | Improvement | Why it mattered |
|---|--------|-------------|-----------------|
| 1 | `dbfb728` | **Complete + extend the P3 index migration** (`t_effbfdb9`) | The perf-index work was half-landed: `crm.py`'s `ensure_schema()` wiring was committed, but the `orchestration.py`+`strategy.py` wiring, the standalone migration, and its test were uncommitted — and **the migration was dead code (nothing ran it)**. Wired `ensure_schema()` index creation for `session_events`/`task_ledger`/`initiative_events`; added the audit-flagged missing `tasks(initiative_id)` index (hermes-owned table, so it lands via the migration); invoked the migration at startup (idempotent, resilient to missing tables). |
| 2 | `4c8720e` | **GZip regression test** (`t_38ff5eaf`) | The 641KB→~150KB compression fix was **untested** — a middleware-ordering regression could silently disable it on the tailnet/mobile dashboard. Added wire-level assertions (gzip applied when advertised, compressed <50% of decoded body, identity honored). |
| 3 | `bde60b3` | **`to_thread` the last 2 blocking async endpoints** | dev-audit P1: the prior sweep covered hot paths but missed `api_memory_metabolism` and `api_memory_contradiction` — both `async def` calling `gmem.*` (blocking sqlite/analysis) inline on the event loop. Wrapped in `asyncio.to_thread`, matching the adjacent lakehouse routes. |

New tests: `+2` (p3 indexes: tasks index + missing-table resilience), `+4` (gzip).

## Already done — verified (no action needed)

Each was confirmed *landed in the codebase*, not just planned:

| Audit item | Status | Evidence |
|---|---|---|
| P0 Auth on 93 mutating endpoints + real token gate (`t_b3448002`) | ✅ done | `MutatingAuthMiddleware` (api.py:344), `test_auth_middleware.py` covers destructive `DELETE/POST/PATCH` with/without/wrong token |
| P0 Silent task/deal mutation failures (`t_58d0bc71`) | ✅ done | `updateTaskStatus` (index.html:11172) + `crmTouch` (:4262) now `throw` on `!ok`, `toast('…','err')`, and revert via reload |
| P0 PATCH projects + contacts + `update_contact` (`t_6b274b33`) | ✅ done | commit `5ea7e6b`; `crm.update_contact` (crm.py) |
| P0 Contacts view (`t_32d469b1`) | ✅ done | `renderContacts` + `/api/crm/contacts` wired in `loadCRM` |
| P1 Deal product+initiative selectors + `+Deal` (`t_e4fe3b88`) | ✅ done | commit `f374c64` |
| P1 GZipMiddleware + cache headers (`t_38ff5eaf`) | ✅ done | api.py:332 `GZipMiddleware`; `CachedStaticFiles` (api.py:214) stamps `max-age=3600` (this session added the missing *test*) |
| P1 `to_thread` blocking subprocess in `api_create_task` (`t_d3159902`) | ✅ done | api.py:827 `await asyncio.to_thread(subprocess.run, …)` |
| P2 toast `'error'` alias (`t_bf118ea9`) | ✅ done | `toast()` normalizes `'error'→'err'` (index.html:7539); zero stray `'error'` call sites |
| P2 Deprecated `@app.on_event` → lifespan | ✅ done | commit `3524d7f`; only a comment reference remains |
| P2 Drag-to-stage + scorecard targets/deltas (`t_4003d61f`) | ✅ done | commit `bfdf72b` |
| P3 Nav cleanup + color tokens + `to_thread` sweep (`t_5d6c23f6`) | ✅ done | commit `3524d7f` |

## Deliberately not done (with reasons)

- **P0 Tech Event Scout cron fix (`t_70d8b6b3`)** — the broken job lives in
  `~/.hermes/cron/jobs.json`, **outside** the repo. Task scope is the
  orchestrator tree only; a config edit there is out of bounds this pass.
- **P2 Seed growth data (`t_de62325f`)** — assignee is the operator (needs their ICP
  positioning / real deal tagging); it's data-seeding, not code. The parallel
  session's `1f526f6` addresses part of this.
- **dev-audit P2 crm.py f-string SQL "hardening"** — investigated and rejected
  as artificial churn: the interpolated fragments are hardcoded source literals
  (`"name = ?"`), never user-supplied, so there is no injection surface to guard
  and an `assert` would be theater on a security-sensitive file.
- **emptyState routing / WCAG contrast (design P1)** — heavy `index.html` edits
  (173+ occurrences) that directly collide with the active parallel session's
  edits to the same file; deferred to avoid clobbering in-flight work.

## Verification

- `python -m pytest tests/ -q --tb=line` → **422 passed, 13 skipped** (from a
  416-passing floor).
- Each commit staged only its own files; the parallel session's uncommitted
  Growth work was never touched.
