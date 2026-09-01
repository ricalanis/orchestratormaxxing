#!/usr/bin/env bash
set -o pipefail   # a piped stage's failure must not be hidden by its last stage
# promote-weekly.sh — Backlog Phase 1 auto-promotion (backlog-planning-ux-research §5.D).
#
# Runs Monday 00:00 (cron/systemd). Promotes tasks that were scheduled for the
# week that has now arrived onto the active cycle board:
#   - relabels their scheduled_week to the CURRENT ISO week, and
#   - commits them to the active cycle (via the audited assign_task_sprint ledger).
#
# Timing note: at Monday 00:00 the ISO week has already rolled, so a task created
# last week as "next week" now carries THIS week's ISO string — that's the set we
# pull in (scheduled_week <= current). We also include the literal `next_week`
# label so the job is still correct if it runs just BEFORE the boundary (Sunday
# night). Idempotent: a task already on a cycle (sprint_id set) is skipped, so a
# re-run is a no-op.
#
# Logs one line per run to ~/.hermes/memories/weekly-promotion.log.
# Env: HERMES_KANBAN_DB (source DB) · HERMES_PROMOTE_LOG (log path override).

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${HERMES_PROMOTE_LOG:-$HOME/.hermes/memories/weekly-promotion.log}"
mkdir -p "$(dirname "$LOG")"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

TS="$(date '+%Y-%m-%d %H:%M:%S')"
cd "$REPO" || { echo "$TS [error] cannot cd $REPO" >>"$LOG"; exit 1; }

OUT="$("$PY" - 2>&1 <<'PYEOF'
import datetime
from dashboard import sprints  # safe: no import side effects (see tests/*)

def iso(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"

today = datetime.date.today()
current = iso(today)
nxt = iso(today + datetime.timedelta(days=7))

conn = sprints.get_conn()
try:
    rows = conn.execute(
        "SELECT id FROM tasks "
        "WHERE scheduled_week IS NOT NULL AND sprint_id IS NULL "
        "AND (scheduled_week <= ? OR scheduled_week = ?)",
        (current, nxt)).fetchall()
    ids = [r[0] for r in rows]
    for tid in ids:
        conn.execute("UPDATE tasks SET scheduled_week = ? WHERE id = ?", (current, tid))
    conn.commit()
finally:
    conn.close()

active = sprints.get_active_sprint()
assigned = 0
if active:
    for tid in ids:
        if sprints.assign_task_sprint(tid, active["id"]).get("status") == "assigned":
            assigned += 1

print(f"promoted={len(ids)} assigned_to_cycle={assigned} "
      f"cycle={active['id'] if active else 'none'} week={current}")
PYEOF
)"
RC=$?

if [ $RC -eq 0 ]; then
    echo "$TS [ok] $OUT" >>"$LOG"
    echo "$OUT"
else
    echo "$TS [error] $OUT" >>"$LOG"
    echo "promote-weekly failed: $OUT" >&2
    exit 1
fi
