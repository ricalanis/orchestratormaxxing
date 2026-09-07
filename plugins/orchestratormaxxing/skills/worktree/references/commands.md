# task-workspace — the commands, exit codes and rules

```
task-workspace resolve --mode shared    --project <path>                      # prints the project path; no subprocess
task-workspace resolve --mode worktree  --project <path> --branch task/<slug>  # wt switch --create; prints the worktree path
task-workspace resolve --mode container --project <path> [--slug <s>]         # prints `coder-ws ssh <slug> --` then the path
task-workspace list    --project <path>                                       # branch + path table (`--json` for the array)
task-workspace merge   --project <path> --branch task/<slug>                  # wt merge: squash + rebase + ff; removes the worktree
task-workspace remove  --project <path> --branch task/<slug> --confirm        # SELECT: exit 3 and nothing runs without --confirm
task-workspace install-wt                                                     # pinned wt from deploy/worktrunk.lock (sha256-verified)
task-workspace check                                                          # read-only preflight
```

`resolve --mode worktree` prints the path from `wt list --format json`, never
from what `wt switch` said; if the branch already exists it reuses that
worktree. Exit codes: `0` ok · `1` error · `2` missing prerequisite, the message
names the fix (`task-workspace install-wt` for `wt`, `install-fleet.sh` for
`coder-ws`) · `3` refused.

**Launchers.** `c -wt`, `g -wt`, `o -wt` (aliases of `-W worktree`; `-c`/`--container` = `-W container`) (and
`-W container`) call `resolve` and start the session inside the printed path;
the tmux session is named `<base>-wt` / `<base>-ws`. Do not `cd` into a
worktree that a launcher did not create for this task. The dashboard's Codex
lane reads `tasks.workspace_mode` the same way.

**The root is fixed.** A worktree lives at `~/dev/.worktrees/<repo>.<branch>`
(Mac `~/Dev/.worktrees/`) — never as a sibling folder in `~/dev`, because every
`~/dev/<slug>` is a project to the dashboard and a stray sibling becomes a
phantom project. Branch names are `task/<slug>`; `wt` sanitizes the slash. The
path template reaches `wt` inline (`--config-set 'worktree-path = …'`); no
installer writes a `wt` config.

## Rules that hold

1. **Never switch modes mid-task.** A task that started in a worktree finishes
   there; a shared-tree task does not sprout a worktree halfway because a diff
   got scary. Report and let him choose again.
2. **A worktree is merged with `task-workspace merge`** — squash, rebase,
   fast-forward, worktree removed. Never by hand-copying files into the
   checkout, never by `cp -r`, never by cherry-picking around it.
3. **`remove` is SELECT.** It needs `--confirm`, discards the branch's worktree,
   and you never run it on your own initiative. Stale worktrees are listed with
   `task-workspace list`, then he decides.
4. **One branch, one task.** Do not park a second task on an existing
   `task/<slug>` worktree; resolve a new one.
5. **Report the path once.** After `resolve`, state the mode and the absolute
   path in one line so the next session and the dashboard can find the work.
6. **Never fall back to a raw `git worktree add`.** The path convention and the
   merge live in `wt`; a hand-made worktree is invisible to `task-workspace list`.
