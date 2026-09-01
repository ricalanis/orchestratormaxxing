#!/usr/bin/env bash
# Daily Reflection — morning Telegram prompt (7:15, no-agent cron).
# Prints the deterministic prompt from /api/reflection/generate-morning, or
# NOTHING when today's morning reflection is already saved or the dashboard is
# down (empty stdout = the gateway stays silent). Source of truth lives in the
# repo at orchestrator/scripts/; installed copy: ~/.hermes/scripts/.
set -uo pipefail

TOKEN_FILE="$HOME/.config/orchestratormaxxing/dashboard-token"
[ -f "$TOKEN_FILE" ] || exit 0
RESP=$(curl -s -m 10 -X POST -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
    http://127.0.0.1:3000/api/reflection/generate-morning 2>/dev/null) || exit 0

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
