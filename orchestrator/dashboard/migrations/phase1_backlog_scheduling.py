"""Phase 1 — Backlog + Scheduling (backlog-planning-ux-research §5).

SCHEMA migration: add the nullable `scheduled_week` column to tasks (ISO week
string like "2026-W28"). `due_date` already exists (added by canvas.ensure_schema
in Phase 3), so this only fills the one missing column.

Idempotent — guards on PRAGMA table_info, so a second run is a no-op. The same
ALTER is also wired into `canvas.ensure_schema()` so the column is guaranteed at
every app boot; this standalone script is the explicit, runnable migration for
the live DB (and mirrors the p0_2 / p1_3 / p2_4 convention).

Run:  python -m dashboard.migrations.phase1_backlog_scheduling
"""
from .. import db


def run() -> dict:
    conn = db.get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        added = []
        if "scheduled_week" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN scheduled_week TEXT")
            added.append("scheduled_week")
        # due_date is a Phase-3 column; report whether it's already present.
        due_date_present = "due_date" in cols
        conn.commit()
        return {
            "status": "ok",
            "added": added,
            "scheduled_week_present": True,
            "due_date_present": due_date_present,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, ensure_ascii=False))
