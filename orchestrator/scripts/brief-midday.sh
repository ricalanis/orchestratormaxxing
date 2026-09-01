#!/usr/bin/env bash
# Brief — midday slot of the 3x-daily ritual (Plan 08:30 / Pulse 13:30 / Close 18:30).
# no-agent hermes cron job: compose (dashboard does all writes) -> send -> sent-callback.
# ALWAYS exits 0 with EMPTY stdout (cron delivery must carry nothing; the script sends).
# Semantics: at-least-once — a crash between send and callback yields one duplicate
# on the next fire; the compose endpoint itself is idempotent per (date, slot).
# Slot-time FLOOR (added after 2026-07-29): this script does NOTHING when it fires
# more than GRACE_MIN before its own slot; it exits 0, composes nothing, sends
# nothing, and leaves the slot for its real run. Why: a cron catch-up (job
# creation, a laptop waking, a ticker restart) fired the Plan at 06:01 that day.
# Compose is idempotent per (date, slot) and `sent` was already stamped, so the
# real 08:30 run exited silent and the whole day ran on a payload frozen at
# 06:01 — which, that morning, was also composed before the m02_spine migration
# and therefore wrong. A LATE fire still composes: a late brief beats no brief.
set -uo pipefail
SLOT=midday
SLOT_HHMM=1330          # this slot's scheduled time, HH:MM as HHMM
GRACE_MIN=45            # how early a fire may still count as this slot
_now_min=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
_slot_min=$(( 10#${SLOT_HHMM:0:2} * 60 + 10#${SLOT_HHMM:2:2} ))
[ "$_now_min" -lt $(( _slot_min - GRACE_MIN )) ] && exit 0
# Fleet identity (env > this file > empty). A machine without it is standalone.
_fe="${ORCHESTRATORMAXXING_FLEET_ENV:-$HOME/.config/orchestratormaxxing/fleet.env}"
[ -f "$_fe" ] && . "$_fe" 2>/dev/null || true
HOY_TARGET="${ORCHESTRATORMAXXING_BRIEF_TARGET:-}"
[ -n "$HOY_TARGET" ] || exit 0  # unconfigured machine sends nothing
TOKEN_FILE="$HOME/.config/orchestratormaxxing/dashboard-token"; [ -f "$TOKEN_FILE" ] || exit 0
AUTH="Authorization: Bearer $(cat "$TOKEN_FILE")"
RESP=$(curl -s -m 20 -X POST -H "$AUTH" "http://127.0.0.1:3000/api/brief/$SLOT") || exit 0
TEXT=$(printf '%s' "$RESP" | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get("sent"):
    sys.exit(0)
sys.stdout.write(d.get("rendered_md", ""))
')
[ -n "$TEXT" ] || exit 0
printf '%s' "$TEXT" | hermes send --to "$HOY_TARGET" -q || exit 0
curl -s -m 10 -X POST -H "$AUTH" "http://127.0.0.1:3000/api/brief/$SLOT/sent" >/dev/null 2>&1
exit 0
