"""P0-2 — Unify Sprint/Cycle (revised-final-plan §2.2).

One-off DATA migration (not schema): close the legacy 14-day project sprint
`spr_083acc33`, migrate its 3 tasks to the current weekly cycle `cyc_27dd8d89`,
and make that cycle the sole active one.

Idempotent — safe to re-run. Guards on current state at every step, so a second
run is a no-op that just re-prints the (already-correct) report.

Two subtleties this script gets right that a naive `UPDATE sprints SET
status='closed'` would not:

  1. **Canonical closed status is `completed`, not `closed`.** The plan's SQL
     literal said `status='closed'`, but nothing in the codebase recognizes
     that value — `close_sprint`, `get_delivered_sprints`, `roll_cycle`, and
     `get_active_sprint` all use `active`/`planning`/`completed`. Using `closed`
     would make the sprint vanish from active queries *without* landing in the
     delivered/history views. We use `completed` (+ `closed_at`) to match.

  2. **The dual store stays consistent.** `tasks.sprint_id` and the append-only
     `task_sprints` commit-ledger are HEAD-vs-reflog (§2.4). The healthz
     invariant (§2.3) requires every task whose `sprint_id` is set to have a
     matching open (`outcome IS NULL`) ledger row. So we: stamp the old sprint's
     open rows with a terminal outcome (delivered for done+reviewed, else
     carried — mirroring close_sprint), and open a fresh `cyc_27dd8d89` ledger
     row for each moved task. After the run the invariant holds (verified below).

Run:  python -m dashboard.migrations.p0_2_unify_cycle
"""
import time

from .. import db

OLD_SPRINT = "spr_083acc33"
NEW_CYCLE = "cyc_27dd8d89"


def run() -> dict:
    conn = db.get_conn()
    try:
        now = int(time.time())

        old = conn.execute("SELECT id, status FROM sprints WHERE id = ?", (OLD_SPRINT,)).fetchone()
        new = conn.execute("SELECT id, status FROM sprints WHERE id = ?", (NEW_CYCLE,)).fetchone()
        if old is None:
            return {"status": "error", "error": f"old sprint {OLD_SPRINT} not found"}
        if new is None:
            return {"status": "error", "error": f"target cycle {NEW_CYCLE} not found"}

        moving = [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE sprint_id = ?", (OLD_SPRINT,)).fetchall()]

        # 1. Stamp the old sprint's still-open ledger rows (mirror close_sprint):
        #    delivered = done + accepted, else carried. Do this BEFORE moving the
        #    tasks' pointers so the "done" test still sees them under the sprint.
        delivered = conn.execute(
            "UPDATE task_sprints SET outcome = 'delivered' WHERE sprint_id = ? "
            "AND outcome IS NULL AND task_id IN "
            "(SELECT id FROM tasks WHERE status = 'done' AND reviewed_at IS NOT NULL)",
            (OLD_SPRINT,)).rowcount
        carried = conn.execute(
            "UPDATE task_sprints SET outcome = 'carried' WHERE sprint_id = ? "
            "AND outcome IS NULL", (OLD_SPRINT,)).rowcount

        # 2. Close the old sprint (canonical 'completed'; keep first closed_at).
        conn.execute(
            "UPDATE sprints SET status = 'completed', "
            "closed_at = COALESCE(closed_at, ?) WHERE id = ?", (now, OLD_SPRINT))

        # 3. Migrate the tasks' pointers to the weekly cycle.
        conn.execute(
            "UPDATE tasks SET sprint_id = ? WHERE sprint_id = ?", (NEW_CYCLE, OLD_SPRINT))

        # 4. Open a fresh commit-ledger row per moved task on the new cycle, and
        #    reopen any that were previously stamped — the dual-store invariant
        #    needs exactly one open (outcome IS NULL) row per current sprint_id.
        for tid in moving:
            conn.execute(
                "INSERT OR IGNORE INTO task_sprints (task_id, sprint_id, committed_at) "
                "VALUES (?,?,?)", (tid, NEW_CYCLE, now))
            conn.execute(
                "UPDATE task_sprints SET outcome = NULL "
                "WHERE task_id = ? AND sprint_id = ?", (tid, NEW_CYCLE))

        # 5. Make the weekly cycle the sole active one.
        conn.execute("UPDATE sprints SET status = 'active' WHERE id = ?", (NEW_CYCLE,))
        # Any OTHER active sprint/cycle would break "one active cycle" — none
        # exists today, but demote defensively so the migration is authoritative.
        demoted = conn.execute(
            "UPDATE sprints SET status = 'completed', closed_at = COALESCE(closed_at, ?) "
            "WHERE status = 'active' AND id != ?", (now, NEW_CYCLE)).rowcount

        conn.commit()

        # --- report + invariant check (§2.3) ---
        active = [dict(r) for r in conn.execute(
            "SELECT id, name, status FROM sprints WHERE status = 'active'").fetchall()]
        drift = conn.execute(
            "SELECT COUNT(*) FROM tasks t WHERE t.sprint_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM task_sprints ts WHERE ts.task_id = t.id "
            "AND ts.sprint_id = t.sprint_id AND ts.outcome IS NULL)").fetchone()[0]
        return {
            "status": "ok",
            "tasks_migrated": moving,
            "ledger_delivered": delivered,
            "ledger_carried": carried,
            "other_active_demoted": demoted,
            "active_cycles": active,
            "sprint_ledger_drift": drift,  # must be 0
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, ensure_ascii=False))
