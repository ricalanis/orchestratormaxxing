---
name: plan-to-repo
description: "Persist a finalized deep plan to a durable local location (default: the current project's docs/plans/topic.md) with explicit status and author, updating the same topic in place. Use when a plan becomes the plan of record — an approved plan output, an architecture or design document, a multi-day phased implementation plan. Do not use for session task lists, scratchpad exploration, or a rejected design."
---

Fire on a **plan of record**, not on every plan.

| Fire | Do not fire |
|---|---|
| An approved plan output, at approval time | The 3–5 step task list for this session's edits (that is wrap-up WIP state) |
| An architecture/design doc guiding multi-day work (migration design, entity+flow model) | A one-off diagnosis, scratchpad exploration, or a rejected design |
| A phased implementation plan with acceptance criteria (a rollout sequence) | |

Test: if someone must re-read it next week to know what was decided, persist it.

1. Resolve the durable location. Default to the current project's own tree:
   `docs/plans/<topic>.md` (repo root first, else the working directory). Use a
   different location only when the operator explicitly names one. Never invent a
   project slug or a personal repository path.
2. Write or UPDATE `docs/plans/<topic>.md`. Front-matter: `status: draft|approved|superseded`
   and `author:` set to the **actual** author (the operator or the real named author —
   never a fabricated model name). Body = the approved plan verbatim, in the language it
   was written; only `<topic>` is ASCII kebab-case.
3. **Update, do not duplicate.** One file per topic. If the topic already has a file, edit
   it in place and keep its original filename and date — git carries history, so a second
   dated file is a duplicate, not a version. Retire a plan with `status: superseded`;
   leave it on disk.
4. Commit only if the operator asked for a commit and the location is a git repo:
   `git add <file> && git commit -m "plan(<topic>): <summary>"`. Otherwise leave the file
   as a working-tree change. Never force a commit or an approval flow beyond the operator's
   stated scope.
5. Optional attachments/indexers. If the project has a plan index or dashboard attachment
   mechanism, register the plan there. The durable local file is the source of truth and
   must survive the absence or failure of any indexer — a failed registration never
   deletes or rewrites the file. Report the exact failure and the file path.

**If registration fails** (indexer down, auth error, or a typed rejection): keep the file —
it is the durable artifact, the attachment is only an index over it. Report the exact
filled-in registration call for the operator. One attempt, no retry loop.
