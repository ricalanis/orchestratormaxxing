#!/usr/bin/env bash
# cogload weekly brief — Sundays only. Sends the dashboard-composed weekly
# cogload brief (aggregates only) to the Health thread. Never fabricates a
# brief: if the dashboard endpoint is unreachable, exit non-zero without
# sending anything. Never includes free-text label content — aggregates only.
# Exit 0 on a deliberate skip (with one explanatory line); non-zero only on
# real failure.
set -uo pipefail

STORE="${COGLOAD_DIR:-$HOME/.local/share/cogload}"
# Fleet identity (env > this file > empty). A machine without it is standalone.
_fe="${ORCHESTRATORMAXXING_FLEET_ENV:-$HOME/.config/orchestratormaxxing/fleet.env}"
[ -f "$_fe" ] && . "$_fe" 2>/dev/null || true
TARGET="${ORCHESTRATORMAXXING_COGLOAD_BRIEF_TARGET:-}"
[ -n "$TARGET" ] || exit 0  # unconfigured machine sends nothing
TOKEN_FILE="$HOME/.config/orchestratormaxxing/dashboard-token"

# 1. Kill switch — same skip as the evening ask, checked before the day guard
#    so a disabled collector never sends, any day.
if [ -e "$STORE/DISABLED" ]; then
  echo "cogload-weekly-brief: kill switch on ($STORE/DISABLED) — skip"
  exit 0
fi

# 2. Sundays only (1=Mon .. 7=Sun).
if [ "$(date +%u)" != "7" ]; then
  echo "cogload-weekly-brief: not Sunday — skip"
  exit 0
fi

# 2b. At-most-once per ISO week.
WEEK_STAMP="$STORE/.weekly-stamp-$(date +%G-W%V)"
if [ -f "$WEEK_STAMP" ]; then
  echo "cogload-weekly-brief: already sent this week — skip"
  exit 0
fi

# 3. No records at all in the last 7 days — nothing to brief (covers the
#    "kill switch was on all week" case deterministically).
HAS_RECORDS=0
for i in 0 1 2 3 4 5 6; do
  D="$(date -d "$i days ago" +%F 2>/dev/null || date -v-${i}d +%F 2>/dev/null)"
  [ -z "$D" ] && continue
  M="$(printf '%s' "$D" | cut -c1-7)"
  KF="$STORE/keys/${M}/keys-${D}.jsonl"
  KG="${KF}.gz"
  if [ -f "$KF" ] || [ -f "$KG" ]; then
    HAS_RECORDS=1
    break
  fi
done
if [ "$HAS_RECORDS" -eq 0 ]; then
  echo "cogload-weekly-brief: no key records in last 7 days — skip"
  exit 0
fi

# 4. Read the brief from the dashboard endpoint (aggregates only, server-side).
if [ ! -f "$TOKEN_FILE" ]; then
  echo "cogload-weekly-brief: no dashboard token ($TOKEN_FILE) — abort" >&2
  exit 1
fi
# The token must NOT be passed via -H: curl's argv is world-readable through
# `ps` and /proc/<pid>/cmdline for the life of the request, so any local process
# could lift the dashboard bearer token. --config reads the header from a file
# instead; create it 0600 and remove it on exit.
CURLCFG=$(mktemp) && chmod 600 "$CURLCFG"
trap 'rm -f "$CURLCFG"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "$(cat "$TOKEN_FILE")" > "$CURLCFG"
BRIEF=$(curl -fsS -m 20 --config "$CURLCFG" \
    "http://127.0.0.1:3000/api/personal/cogload/weekly?format=md" 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$BRIEF" ]; then
  echo "cogload-weekly-brief: dashboard endpoint unreachable — abort without sending" >&2
  exit 1
fi

# 5. Send. The dashboard is the sole author; we forward aggregates only and
#    never extract or append free-text label content.
if printf '%s' "$BRIEF" | hermes send --to "$TARGET" -q; then
  # At-most-once per ISO week. Without this, every invocation on a Sunday that
  # clears the guards sends ANOTHER message — a retry, a manual run or a second
  # timer fire would spam the Health thread. The evening ask already stamps per
  # date; the brief had no guard at all. Stamp only AFTER a successful send, so
  # a failed send is retried rather than silently swallowed.
  touch "$STORE/.weekly-stamp-$(date +%G-W%V)" 2>/dev/null || true
  echo "cogload-weekly-brief: sent weekly brief to $TARGET"
  exit 0
else
  echo "cogload-weekly-brief: hermes send failed" >&2
  exit 1
fi