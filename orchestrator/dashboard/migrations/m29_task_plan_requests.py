"""m29 — honest outbox for task-to-planning-session launches.

Additive only. This records requests and their observed spawn result; it does
not populate or modify any project's ``repo_path`` (that backfill is SELECT).
"""


def m29_task_plan_requests(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_plan_requests ("
        " id TEXT PRIMARY KEY,"
        " task_id TEXT NOT NULL,"
        " planner TEXT NOT NULL CHECK (planner IN ('fable','opus1m','sol')),"
        " state TEXT NOT NULL CHECK (state IN ('requested','created','spawn_failed')),"
        " session TEXT,"
        " attach_hint TEXT,"
        " folder TEXT,"
        " note TEXT,"
        " exit_code INTEGER,"
        " created_at INTEGER NOT NULL,"
        " updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_plan_requests_task "
        "ON task_plan_requests(task_id, created_at DESC)"
    )
