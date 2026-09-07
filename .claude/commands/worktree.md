---
description: Pick the workspace mode for a task — SHARED tree, an isolated WORKTREE under ~/dev/.worktrees, or a CONTAINER — honoring a named mode, checking `task-workspace check`, and asking with AskUserQuestion when it is undecided. Merges only through `task-workspace merge`.
argument-hint: <task or branch slug> [shared|worktree|container]
---

The task: **$ARGUMENTS**

You hold `task-workspace` on PATH. It wraps `wt ` (worktrunk) and is the one resolver every launcher (`c`, `g`, `o`) and the dashboard dispatcher use. You are choosing where this task's edits happen; nothing else.

## Three modes

| | **SHARED** | **WORKTREE** | **CONTAINER** |
|---|---|---|---|
| Where | the checkout `~/dev/<slug>` | `~/dev/.worktrees/<repo>.<branch>` (Mac `~/Dev/.worktrees/`) | a Coder workspace, project mounted at the same path |
| Who else is in the tree | every other session; governance rule 3 on every commit | nobody | nobody on the host |
| Cost | zero setup, staging discipline | seconds, plus one `merge` | minutes, a running container |
| When | one hunk you finish this session | multi-file, parallel, risky, or abandonable work | foreign toolchain or untrusted tooling → `/coder-workspace` |

## Procedure — in order

**1. Did Ricardo NAME a mode? Honor it, do not ask.** A second argument `shared|worktree|container` is a named mode. So are "aquí mismo" / "en el checkout" (SHARED), "worktree" / "wt" / "rama aislada" / "aísla" (WORKTREE), "contenedor" / "coder" / "docker" (CONTAINER → run `/coder-workspace` and stop here).

**2. Run `task-workspace check`.** Read-only. Exit 2 → `wt` missing → `task-workspace install-wt` (pinned, sha256-verified from `deploy/worktrunk.lock`). Never substitute a raw `git worktree add`.

**3. Undecided? Ask with `AskUserQuestion`: two options, one question, recommendation first with the real reason** (e.g. *"Worktree — toca tres módulos y hay otra sesión en este proyecto"*). Then do what he picks. Urgency and "es chiquito" shape the recommendation; they are not a stated mode. Only a mode named as *where the edits happen* counts: "vamos directo al worktree del dashboard" describes a place he is already in, not a choice — ask.

**The exception — a mechanical one-liner** ("cambia el puerto a 8081") in a project with no other live session is SHARED without a question. Say so in one line.

## Commands

```
task-workspace resolve --mode shared    --project <path>
task-workspace resolve --mode worktree  --project <path> --branch task/<slug>   # prints the worktree path from `wt list`
task-workspace resolve --mode container --project <path> [--slug <s>]          # prints `coder-ws ssh <slug> --` + path
task-workspace list    --project <path> [--json]
task-workspace merge   --project <path> --branch task/<slug>                   # squash + rebase + ff by wt; removes the worktree
task-workspace remove  --project <path> --branch task/<slug> --confirm         # SELECT — exit 3 without --confirm
task-workspace install-wt
```

Exit codes: `0` ok · `1` error · `2` missing prerequisite (message names the fix) · `3` refused. Launchers: `c -wt`, `g -wt`, `o -wt` (= `-W worktree`) and `-c` (= `-W container`) — they call `resolve` and open the session in the printed path.

Branch: `task/<slug>` from the argument. Root: always `~/dev/.worktrees/<repo>.<branch>`; never a sibling folder in `~/dev`, which the dashboard would read as a project.

## Staying SHARED

Governance rule 3 applies verbatim: preserve others' changes, reread the diff immediately before staging, and stage only owned hunks; if edits interleave in one function, reconstruct HEAD plus only your edits in an isolated copy and apply that patch to the index; inspect the staged blob; check HEAD for your markers before assuming your work disappeared. If you cannot stage only your own hunks, say so and stop — never `git add -A`.

## Rules that hold

1. Never switch modes mid-task; report and let him choose again.
2. A worktree comes back only through `task-workspace merge` — never by hand-copying files.
3. `remove` is SELECT: never on your own initiative.
4. After `resolve`, state the mode and the absolute path in one line.
