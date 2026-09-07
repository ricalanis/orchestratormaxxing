"""m34 — `tasks.workspace_mode`: where a dispatched task RUNS.

A task already says WHERE its code is (`workspace_path`, else the project's
`repo_path`). It could not say HOW that place should be entered: in the shared
tree everyone else is editing, in an isolated git worktree, or inside a Coder
container. `workspace_mode` is that one word, with `shared` as the default so
every existing row and every existing dispatch keeps today's exact behaviour.

Values (CHECK-constrained): `shared` · `worktree` · `container`. The resolver
that turns the word into a path or a command prefix is `bin/task-workspace`;
`dashboard/dispatch.py` consumes it only on the Codex lane (the Claude lane
never spawns — red line 10 — so it has nothing to resolve).

Purely additive; position after m33 is append-only hygiene (the ledger records
what ran by NAME). Idempotent: a second run on a DB that already has the column
is a no-op.
"""


def m34_task_workspace_mode(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "workspace_mode" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN workspace_mode TEXT NOT NULL DEFAULT 'shared' "
            "CHECK(workspace_mode IN ('shared','worktree','container'))"
        )
