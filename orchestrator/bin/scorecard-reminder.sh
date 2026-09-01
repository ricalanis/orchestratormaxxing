#!/usr/bin/env bash
set -o pipefail   # a piped stage's failure must not be hidden by its last stage
# scorecard-reminder.sh — Phase 4 weekly scorecard reminder (Fri 5pm).
#
# The one-person growth ritual (playbook Cap. 6): every Friday, look at the week's
# 5 numbers — leads, touches, discovery calls, content, proposals. This job
# computes them (auto-derived from the week's deal/content events — never typed)
# and writes a one-line summary to the scorecard log so the Friday review has the
# numbers ready. Read-only against the DB; safe to run any time.
#
# Driven by deploy/hermes-scorecard.timer (systemd --user, Fri 17:00). Also
# runnable by hand:  bin/scorecard-reminder.sh
#
# Env: HERMES_KANBAN_DB (source DB) · HERMES_SCORECARD_LOG (log path override).

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${HERMES_SCORECARD_LOG:-$HOME/.hermes/memories/weekly-scorecard.log}"
mkdir -p "$(dirname "$LOG")"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

TS="$(date '+%Y-%m-%d %H:%M:%S')"
cd "$REPO" || { echo "$TS [error] cannot cd $REPO" >>"$LOG"; exit 1; }

OUT="$("$PY" - 2>&1 <<'PYEOF'
from dashboard import growth  # safe: no import side effects (see tests/*)

s = growth.scorecard()          # current ISO week, auto-derived
parts = " · ".join(f"{k['label']}={k['value']}" for k in s["kpis"])
print(f"week={s['week']} {parts} (total_activity={s['total_activity']})")
PYEOF
)"
RC=$?

if [ $RC -eq 0 ]; then
    echo "$TS [ok] $OUT" >>"$LOG"
    # A concise reminder to stdout so an interactive/cron mail run surfaces it.
    echo "📊 Scorecard semanal (viernes) — $OUT"
else
    echo "$TS [error] $OUT" >>"$LOG"
    echo "scorecard-reminder failed: $OUT" >&2
    exit 1
fi
