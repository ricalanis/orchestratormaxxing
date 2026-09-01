---
name: wrap-up
description: Persist the current project's completed work and forward state through the deterministic session-log tool. Use at the end of a material implementation session; skip read-only or trivial sessions.
---

1. Review `git status --short` and the relevant diff/stat. Preserve unrelated user changes.
2. Write one concise changelog entry with `session-log changelog "<what changed and why>" --tag <feat|fix|docs|refactor|chore>`. Do not hand-edit the generated logs.
3. Replace the WIP note using `session-log wip set` on stdin. Include current state, decisions, unresolved questions, next steps, and the few files needed to resume. WIP is forward state, not history.
4. Run `session-log check`; it should be silent.
5. Report what was persisted in one line.
