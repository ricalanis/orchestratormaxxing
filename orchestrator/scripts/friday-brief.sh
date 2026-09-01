#!/usr/bin/env bash
# Practice-block brief — Thursday-17:30 Telegram delivery of the Motor
# Caliente pre-block brief (radar chunk 5), 30 min before the advisory-era
# Jueves 18:00–20:30 block (nueva etapa 2026-08-21; the Friday daytime block
# it originally briefed no longer exists). READ/NUDGE ONLY: the text carries deep
# links to the authenticated dashboard; no mutation ever rides this channel.
# ALWAYS exits 0 with EMPTY stdout (cron delivery must carry nothing).
# Idempotency: a per-date stamp file — at-most-once per Thursday; a crash
# between send and stamp yields at most one duplicate nudge (acceptable for
# a nudge, per the radar design: inaceptable en mutación, y aquí no hay).
# Slot-time FLOOR (inherited from brief-morning.sh, 2026-07-29 lesson): a
# catch-up fire >45min early does nothing and leaves the slot to its real run.
set -uo pipefail
SLOT_HHMM=1730
GRACE_MIN=45
_now_min=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
_slot_min=$(( 10#${SLOT_HHMM:0:2} * 60 + 10#${SLOT_HHMM:2:2} ))
[ "$_now_min" -lt $(( _slot_min - GRACE_MIN )) ] && exit 0
STAMP_DIR="$HOME/.hermes/memories"
mkdir -p "$STAMP_DIR" 2>/dev/null || true
STAMP="$STAMP_DIR/friday-brief-$(date +%F).sent"
[ -f "$STAMP" ] && exit 0
# Fleet identity (env > this file > empty). A machine without it is standalone.
_fe="${ORCHESTRATORMAXXING_FLEET_ENV:-$HOME/.config/orchestratormaxxing/fleet.env}"
[ -f "$_fe" ] && . "$_fe" 2>/dev/null || true
HOY_TARGET="${ORCHESTRATORMAXXING_BRIEF_TARGET:-}"
[ -n "$HOY_TARGET" ] || exit 0  # unconfigured machine sends nothing
TEXT=$(curl -s -m 20 "http://127.0.0.1:3000/api/friday-brief?format=md") || exit 0
[ -n "$TEXT" ] || exit 0
printf '%s' "$TEXT" | hermes send --to "$HOY_TARGET" -q || exit 0
touch "$STAMP" 2>/dev/null || true
exit 0
