# plan-to-repo — worked example

Synthetic scenario: a team approves a migration plan for moving a service from a
monolithic config file to a split per-environment layout. The plan is the plan of record.

## Initial write

`docs/plans/config-migration.md`:

```markdown
---
status: draft
author: Ana (platform lead)
---

# Config migration: split per-environment layout

Phase 1: introduce `config/<env>.yaml` with a shared base.
Phase 2: move secrets to an ignored file, keep a template.
Phase 3: update the loader and CI to select by environment.

Acceptance: `make test` green on all three environments with the new layout.
```

## Revised write (same topic, same filename)

A week later the team revises the plan. The skill **updates the same file in place**,
keeping the original filename and date; git carries the history. It does not create a
second dated file.

```markdown
---
status: approved
author: Ana (platform lead)
---

# Config migration: split per-environment layout

Phase 1: introduce `config/<env>.yaml` with a shared base.
Phase 2: move secrets to an ignored file, keep a template.
Phase 3: update the loader and CI to select by environment.
Phase 4 (added): add a `config validate` command that fails on unknown keys.

Acceptance: `make test` green on all three environments with the new layout,
plus `config validate` failing on a deliberately bad key.
```

Decision scenario: if the indexer/dashboard is down, the file and any commit still stand;
the skill reports the failed registration and the file path, and does not retry or delete
the plan. If the operator later decides the migration is abandoned, the same file is
flipped to `status: superseded` and left on disk.
