"""m13 — persistent completion for the 1–3 morning reflection goals.

Goal text stays canonical in ``morning_intentions``.  This additive JSON vector
stores only the matching boolean completion state, date-scoped by the existing
``daily_reflections.date`` row.
"""


def m13_reflection_goals(conn) -> None:
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(daily_reflections)").fetchall()}
    if not cols:
        raise RuntimeError("m13 requires daily_reflections")
    if "morning_completed" not in cols:
        conn.execute(
            "ALTER TABLE daily_reflections ADD COLUMN morning_completed TEXT")
