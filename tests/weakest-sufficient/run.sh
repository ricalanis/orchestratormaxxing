#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD='VALIDITY-FIRST / WEAKEST-SUFFICIENT'

for rel in \
  .claude/commands/self-improve.md \
  plugins/claudemaxxing/skills/self-improve/SKILL.md \
  bin/win-log \
  knowledge/hermes-strategic-evolution-plan.md
do
  grep -Fq "$GUARD" "$ROOT/$rel" || {
    echo "missing weakest-sufficient guard: $rel" >&2
    exit 1
  }
done

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
export WIN_LOG_DIR="$FIXTURE_DIR"

add_win() {
  "$ROOT/bin/win-log" add --class review --workers "$1" --shape "$2" \
    --survivors 0 --delivered --cost "$3" --contract 'same review contract' \
    --specificity-complete "${@:4}" >/dev/null
}

# The broad candidate costs more but adds fewer commitments and supports more cases.
add_win broad broad 2.00 \
  --assumption 'repository exists' \
  --supported-case api --supported-case web
add_win narrow narrow 0.01 \
  --assumption 'repository exists' --assumption 'host is linux' \
  --exception 'skip non-linux hosts' \
  --supported-case api

RESULT_JSON="$($ROOT/bin/win-log match --class review --contract 'same review contract' --json)"
RESULT_JSON="$RESULT_JSON" python3 - <<'PY'
import json, os

rows = json.loads(os.environ["RESULT_JSON"])
assert [r["shape"] for r in rows] == ["broad", "narrow"], rows
assert rows[0]["selection_status"] == "preferred", rows
assert rows[1]["selection_status"] == "dominated", rows
assert rows[1]["dominated_by"] == [rows[0]["id"]], rows
PY

# Incomparable or incomplete evidence abstains; cheapness must not invent an order.
FIXTURE_DIR_2="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR" "$FIXTURE_DIR_2"' EXIT
export WIN_LOG_DIR="$FIXTURE_DIR_2"
add_win api api 5.00 --supported-case api
add_win cli cli 0.01 --supported-case cli
"$ROOT/bin/win-log" add --class review --workers legacy --shape legacy \
  --survivors 0 --delivered --cost 0.001 --contract 'same review contract' >/dev/null

RESULT_JSON="$($ROOT/bin/win-log match --class review --contract 'same review contract' --json)"
RESULT_JSON="$RESULT_JSON" python3 - <<'PY'
import json, os

rows = json.loads(os.environ["RESULT_JSON"])
by_shape = {r["shape"]: r for r in rows}
assert by_shape["api"]["selection_status"] == "incomparable", rows
assert by_shape["cli"]["selection_status"] == "incomparable", rows
assert by_shape["legacy"]["selection_status"] == "review-required", rows
assert rows[-1]["shape"] == "legacy", rows
PY

# Differently contracted candidates are never ranked together, and forced invalid rows never enter.
"$ROOT/bin/win-log" add --class review --workers other --shape other \
  --survivors 0 --delivered --cost 0.0001 --contract 'different contract' \
  --specificity-complete --supported-case api >/dev/null
"$ROOT/bin/win-log" add --class review --workers invalid --shape invalid \
  --survivors 1 --cost 0.00001 --contract 'same review contract' --force >/dev/null
RESULT_JSON="$($ROOT/bin/win-log match --class review --json)"
RESULT_JSON="$RESULT_JSON" python3 - <<'PY'
import json, os

rows = json.loads(os.environ["RESULT_JSON"])
assert "invalid" not in {r["shape"] for r in rows}, rows
assert {r["selection_status"] for r in rows} == {"review-required"}, rows
assert all("multiple contracts" in r["selection_reason"] for r in rows), rows
PY

# Cost is allowed only after specificity evidence is literally identical.
FIXTURE_DIR_3="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR" "$FIXTURE_DIR_2" "$FIXTURE_DIR_3"' EXIT
export WIN_LOG_DIR="$FIXTURE_DIR_3"
add_win expensive expensive 4.00 --supported-case api
add_win cheap cheap 0.02 --supported-case api
RESULT_JSON="$($ROOT/bin/win-log match --class review --contract 'same review contract' --json)"
RESULT_JSON="$RESULT_JSON" python3 - <<'PY'
import json, os

rows = json.loads(os.environ["RESULT_JSON"])
assert [r["shape"] for r in rows] == ["cheap", "expensive"], rows
assert [r["selection_status"] for r in rows] == ["preferred", "equivalent"], rows
PY

if "$ROOT/bin/win-log" add --class review --workers empty --shape empty \
  --survivors 0 --delivered --contract contract --specificity-complete >/dev/null 2>&1
then
  echo "specificity-complete accepted an empty supported-case ledger" >&2
  exit 1
fi

echo "weakest-sufficient contract: PASS"

# --- liveness demotion (2026-08-09): stale / retired-worker wins never compete ---
# Red evidence: pre-fix (git show HEAD:bin/win-log) this exact fixture ranked the
# RETIRED-worker win `preferred`. Post-fix it is review-required with the reason.
FIXTURE_DIR_4="$(mktemp -d)"
export WIN_LOG_DIR="$FIXTURE_DIR_4"
cat > "$FIXTURE_DIR_4/orchestration-wins.jsonl" <<'JSONL'
{"id":"w1","ts":"2026-06-01T00:00:00Z","task_class":"tests","shape":"single","workers":["glm-5.2"],"contract":"pytest -q","delivered":true,"survivors":0,"cost_usd":1,"status":"active","specificity_complete":true,"supported_cases":["case-a"],"assumptions":[],"exceptions":[]}
{"id":"w2","ts":"2026-08-08T00:00:00Z","task_class":"tests","shape":"single","workers":["qwen3-coder:480b"],"contract":"pytest -q","delivered":true,"survivors":0,"cost_usd":1,"status":"active","specificity_complete":true,"supported_cases":["case-a"],"assumptions":[],"exceptions":[]}
{"id":"w3","ts":"2026-08-08T00:00:00Z","task_class":"tests","shape":"single","workers":["glm-5.2"],"contract":"pytest -q","delivered":true,"survivors":0,"cost_usd":2,"status":"active","specificity_complete":true,"supported_cases":["case-a"],"assumptions":[],"exceptions":[]}
JSONL
RESULT_JSON="$(WIN_LOG_TS=2026-08-09T00:00:00Z $ROOT/bin/win-log match --class tests --json)"
RESULT_JSON="$RESULT_JSON" ROOT="$ROOT" python3 - <<'PY'
import json, os, re

rows = {r["id"]: r for r in json.loads(os.environ["RESULT_JSON"])}
assert rows["w1"]["selection_status"] == "review-required" and "stale" in rows["w1"]["selection_reason"], rows["w1"]
assert rows["w2"]["selection_status"] == "review-required" and "retired" in rows["w2"]["selection_reason"], rows["w2"]
assert rows["w3"]["selection_status"] == "preferred", rows["w3"]

# RETIRED_WORKERS must stay a superset of harness-verify's RETIRED_MODELS
# (no import edge — the sync lives here, in the contract)
root = os.environ["ROOT"]
wl = open(f"{root}/bin/win-log").read()
hv = open(f"{root}/bin/harness-verify").read()
wl_set = set(re.findall(r'"([^"]+)"', re.search(r"RETIRED_WORKERS = \{(.*?)\}", wl, re.S).group(1)))
hv_set = set(re.findall(r'"([^"]+)"', re.search(r"RETIRED_MODELS = \[(.*?)\]", hv, re.S).group(1)))
assert hv_set <= wl_set, f"win-log RETIRED_WORKERS missing {hv_set - wl_set}"
PY

echo "liveness-demotion contract: PASS"
