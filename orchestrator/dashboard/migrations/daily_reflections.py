"""Daily Reflection — standalone idempotent migration.

Creates the `daily_reflections` table + date index (BRIEF-daily-reflection §4.1).
The schema is owned by dashboard/reflection.py (ensure_schema, all IF NOT
EXISTS); this wrapper exists so the table can be installed/verified without
booting the API.

Run:  python -m dashboard.migrations.daily_reflections
"""
from .. import db
from .. import reflection


def run() -> None:
    reflection.ensure_schema()
    conn = db.get_conn()
    try:
        cols = conn.execute("PRAGMA table_info(daily_reflections)").fetchall()
        idx = conn.execute("PRAGMA index_list(daily_reflections)").fetchall()
        print("daily_reflections columns:",
              ", ".join(f"{c['name']} {c['type']}" for c in cols))
        print("indexes:", ", ".join(i["name"] for i in idx) or "(none)")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
