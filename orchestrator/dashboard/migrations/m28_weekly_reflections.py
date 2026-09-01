"""m28 — weekly_reflections: the Friday five-question log.

One row stores the answers for one ISO week.  Repeated saves update that row
while leaving omitted answers intact.

Additive only. Runs inside the runner's transaction: no commit/close here.
"""


def m28_weekly_reflections(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS weekly_reflections ("
        " week TEXT UNIQUE,"
        " q_regale TEXT,"
        " q_declare TEXT,"
        " q_referido TEXT,"
        " q_propuesta TEXT,"
        " q_aprendi TEXT,"
        " created_at INTEGER,"
        " updated_at INTEGER)"
    )
