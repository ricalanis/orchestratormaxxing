#!/usr/bin/env bash
# cadence-reconcile.sh — one deterministic materializer pass. Silent unless cards moved.
set -uo pipefail
T="${HERMES_DASHBOARD_TOKEN:-}"
[ -z "$T" ] && [ -f "$HOME/.config/orchestratormaxxing/dashboard-token" ] && T=$(cat "$HOME/.config/orchestratormaxxing/dashboard-token")
A=(); [ -n "$T" ] && A=(-H "Authorization: Bearer ${T}")
R=$(curl -s -X POST "${A[@]}" -H 'Content-Type: application/json' -d '{}' \
      http://127.0.0.1:3000/api/cadence/reconcile || echo '{}')
python3 -c '
import json,sys
d=json.loads(sys.argv[1] or "{}"); c=d.get("counts") or {}
if c.get("minted") or c.get("closed"):
    print(f"🗓️ Cadencia: {c.get(\"minted\",0)} nueva(s), {c.get(\"closed\",0)} cerrada(s)")
    for m in (d.get("minted") or [])[:5]: print("  ·", m["title"])
' "$R"
exit 0
