#!/usr/bin/env bash
set -o pipefail   # a piped stage's failure must not be hidden by its last stage
# funnel-snapshot.sh — weekly conversion-funnel snapshot (Mon 9am).
#
# Captures the current lead→discovery→proposal→won funnel (counts + conversion
# rates, derived from the live deals) into conversion_snapshots, one row per ISO
# week. Re-running the same week overwrites — so a missed/caught-up run is safe.
# The Today-tab sparkline reads these snapshots as its time series.
#
# Driven by deploy/hermes-funnel-snapshot.timer (systemd --user, Mon 09:00).
# Also runnable by hand:  bin/funnel-snapshot.sh
#
# Env: HERMES_KANBAN_DB (source DB) · HERMES_FUNNEL_LOG (log path override).

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${HERMES_FUNNEL_LOG:-$HOME/.hermes/memories/funnel-snapshot.log}"
mkdir -p "$(dirname "$LOG")"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

TS="$(date '+%Y-%m-%d %H:%M:%S')"
cd "$REPO" || { echo "$TS [error] cannot cd $REPO" >>"$LOG"; exit 1; }

OUT="$("$PY" - 2>&1 <<'PYEOF'
from dashboard import growth  # safe: no import side effects (see tests/*)

r = growth.snapshot_funnel()          # current ISO week, from live deals
s = r["snapshot"]
overall = round((s["overall_rate"] or 0) * 100, 1)
print(f"week={s['week_start']} lead={s['lead_count']} disc={s['discovery_count']} "
      f"prop={s['proposal_count']} won={s['won_count']} overall={overall}%")
PYEOF
)"
RC=$?

if [ $RC -eq 0 ]; then
    echo "$TS [ok] $OUT" >>"$LOG"
    echo "📈 Funnel snapshot (lunes) — $OUT"
else
    echo "$TS [error] $OUT" >>"$LOG"
    echo "funnel-snapshot failed: $OUT" >&2
    exit 1
fi
