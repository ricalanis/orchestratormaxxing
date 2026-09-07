---
name: worktree
description: "Trigger: worktree, isolate this task, wt, rama aislada, workspace mode, aísla esta tarea, en su propia rama. Pick the workspace mode for a task — SHARED tree, an isolated WORKTREE under ~/dev/.worktrees, or a CONTAINER — honoring a named mode, checking `task-workspace check`, and ASKING when it is undecided; a worktree comes back only through `task-workspace merge`."
---

You hold `task-workspace` on PATH: the one resolver every launcher and the dashboard dispatcher use. A task declares `workspace: shared | worktree | container` and the tool turns it into a place to run. It wraps `wt ` (worktrunk), adopted verbatim; the local code is routing plus the pinned-install gate.

## There are THREE modes. Picking one is the first thing you do.

| | **SHARED** | **WORKTREE** | **CONTAINER** |
|---|---|---|---|
| Where | the project checkout, `~/dev/<slug>` | `~/dev/.worktrees/<repo>.<branch>` (Mac `~/Dev/.worktrees/`) | a Coder workspace, project mounted at the same path |
| Who else is in the tree | every other session on this project — governance rule 3 on every commit | nobody: one branch, one task | nobody on the host |
| Cost | zero setup; staging discipline on every commit | seconds to create, one `merge` at the end | minutes; a running container |
| When | one small single-session edit | parallel or risky edits, multi-file work, anything another session might touch | foreign toolchains, dependency installs, untrusted tooling → `$orchestratormaxxing:coder-workspace` |

None is the "safe" one. They cost different things.

## The routing procedure — follow it in order

**1. Did Ricardo NAME a mode? Then honor it and do not ask.**

- SHARED: "shared", "aquí mismo", "en el árbol", "en el checkout", "sin worktree"
- WORKTREE: "worktree", "wt", "rama aislada", "aísla esta tarea", "isolate", "en su propia rama"
- CONTAINER: "container", "contenedor", "coder", "en docker" → `$orchestratormaxxing:coder-workspace`; the rest of this skill does not apply.

**2. Run `task-workspace check`.** Read-only, one call: the `wt` path and version, and whether `coder-ws` is present. Exit 2 means `wt` is missing — say so and run `task-workspace install-wt` (pinned, sha256-verified; nothing enters `~/.local/bin` on a mismatch).

**3. Still not stated? ASK, then STOP and wait.** An undecided mode gets asked, never guessed. Print exactly two options, recommendation first with its real reason, and end your turn.

Recommend **WORKTREE** when the change spans files, when another session is or may be in this project, when a verifier will run, or when the task might be abandoned. Recommend **SHARED** when the edit is one hunk you can stage alone and finish this session.

## Then do it

```
task-workspace resolve --mode worktree --project <path> --branch task/<slug>   # prints the worktree path (from `wt list --format json`)
task-workspace resolve --mode shared   --project <path>                       # the project path, no subprocess
task-workspace merge   --project <path> --branch task/<slug>                  # the only way a worktree comes back
```

`c/g/o -wt` (= `-W worktree`) or `-c` (= `-W container`) do the resolve and start the session there. The full verb table, exit codes, the fixed root, and the six rules (never switch modes mid-task; merge only with `task-workspace merge`; `remove` is SELECT; one branch, one task; report the path once; never a raw `git worktree add`) live in **`references/commands.md`** — read it before your first `merge` or `remove`.

## Staying SHARED

Governance rule 3, verbatim — it is the price of the mode:

> Concurrent agent sessions can share the same worktree. Preserve others' changes, reread the diff immediately before staging, and stage only owned hunks. If edits interleave in one function, reconstruct HEAD plus only your edits in an isolated copy using unique anchors, compare it to HEAD, and apply that patch to the index. Inspect the staged blob. A sibling commit can absorb your work: check HEAD for your markers before assuming that a clean file means your work disappeared. Validate reconstructed changes in isolation and prevent tests from writing production DBs or shared derived artifacts.

If you cannot stage only your own hunks, you chose the wrong mode — say so and stop; do not `git add -A`.
