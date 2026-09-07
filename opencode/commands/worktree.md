---
description: Pick the workspace mode for a task (shared tree, isolated worktree, or container) via task-workspace; ask when undecided
---
Task: $ARGUMENTS

Decide WHERE this task's edits happen before touching a file. `task-workspace`
(on PATH) is the resolver; it wraps `wt ` (worktrunk).

1. A named mode wins: `shared` / "aquí mismo" → SHARED; `worktree` / `wt` /
   "rama aislada" / "aísla" → WORKTREE; `container` / "contenedor" / "coder" →
   CONTAINER (use the coder-workspace skill; stop here).
2. Run `task-workspace check` (read-only). Exit 2 → `task-workspace install-wt`;
   never a raw `git worktree add`.
3. Undecided → print two options, recommendation first with the real reason,
   and END YOUR TURN. Recommend WORKTREE for multi-file, parallel, risky or
   abandonable work; SHARED for one hunk you finish this session.

Commands:
- `task-workspace resolve --mode shared --project <path>`
- `task-workspace resolve --mode worktree --project <path> --branch task/<slug>`
  (prints `~/dev/.worktrees/<repo>.<branch>`; never a sibling in `~/dev`)
- `task-workspace list --project <path>`
- `task-workspace merge --project <path> --branch task/<slug>` (squash + rebase
  + ff by wt; removes the worktree — the ONLY way back, never hand-copy files)
- `task-workspace remove --project <path> --branch <b> --confirm` (SELECT;
  exit 3 without `--confirm`; never on your own initiative)

Exit codes 0 ok · 1 error · 2 missing prerequisite (names the fix) · 3 refused.
Launchers: `o -wt`, `c -wt`, `g -wt` (= `-W worktree`); `-c` (= `-W container`).

Staying SHARED means governance rule 3: reread the diff immediately before
staging and stage only owned hunks; never `git add -A`. Never switch modes
mid-task. After `resolve`, state the mode and the absolute path in one line.
