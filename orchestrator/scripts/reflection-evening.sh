#!/bin/bash
# Daily Reflection — Harvard Reflection–Action Loop (18:45, no-agent cron).
# Normal mode prints the server-composed prompt from real morning intentions and
# the persisted 18:30 day review. Already answered or dashboard unavailable means
# empty stdout, so the gateway stays silent.
set -uo pipefail

if [ "${1:-}" = "--test" ]; then
  cat <<'PROMPT'
🌙 Reflection–Action Loop — 15 minutes

1. What went well?
   Name 1–3 wins and why each one worked.

2. What didn't go as planned?
   Name 1–2 moments, what happened, and why.

3. What will I do differently?
   Choose 1–2 concrete adjustments for tomorrow.

Flow: facts → meaning → next step.
PROMPT
  exit 0
fi

[ "$#" -eq 0 ] || exit 2

TOKEN_FILE="$HOME/.config/orchestratormaxxing/dashboard-token"
[ -f "$TOKEN_FILE" ] || exit 0
RESP=$(curl -s -m 15 -X POST -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
    http://127.0.0.1:3000/api/reflection/generate-evening 2>/dev/null) || exit 0

python3 - "$RESP" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
if d.get("exists") or not d.get("prompt"):
    sys.exit(0)
print(d["prompt"])
PY
