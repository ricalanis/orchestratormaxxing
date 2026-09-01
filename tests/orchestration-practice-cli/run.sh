#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="${ORCHESTRATION_PRACTICE_TOOL:-$ROOT/bin/orchestration-practice}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0

ok() { PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$PASS" "$1"; }
fail() { printf 'not ok %d - %s\n' "$((PASS + 1))" "$1" >&2; exit 1; }

python3 - "$TMP/healthy.json" "$TMP/unhealthy.json" "$TMP/spinning.json" <<'PY'
import json, sys
healthy = {
  "contract": "pytest -q", "dependencies": {"runner": True},
  "checkpoint": {"kind": "run_state", "state_ref": "docs/WIP.md"},
  "brakes": {"max_iterations": 3, "budget_or_deadline": {"max_seconds": 900},
             "no_progress": {"max_stalled_steps": 2},
             "completion_check": {"type": "exit_code", "self_report": False}},
  "progress": [0, 1], "writers": {"task.status": ["orchestrator"]},
  "completion": {"type": "exit_code", "value": 0, "self_report": False},
  "evidence_refs": ["test:cli"]
}
unhealthy = dict(healthy)
unhealthy["dependencies"] = {"runner": False}
spinning = dict(healthy)
spinning["progress"] = [0, 50, 100]
spinning["action_results"] = [
  {"action": "pytest -q", "result": {"exit_code": 1, "failures": 2}},
  {"action": "pytest -q", "result": {"exit_code": 1, "failures": 2}},
  {"action": "pytest -q", "result": {"exit_code": 1, "failures": 2}},
]
for path, value in zip(sys.argv[1:], (healthy, unhealthy, spinning)):
    open(path, "w", encoding="utf-8").write(json.dumps(value))
PY

out="$($TOOL match --host codex --text 'contract before dispatch' --json)" || fail "match ready"
python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "matched"' <<<"$out" || fail "match JSON"
ok "match returns canonical JSON"

out="$($TOOL match --host hermes --text 'contrato antes del dispatch y freno despues de 2 pasos sin progreso' --json)" || fail "Spanish match ready"
python3 -c 'import json,sys; x=json.load(sys.stdin); assert [m["practice_id"] for m in x["matches"]] == ["prompt.contract-first", "loop.four-brakes"]' <<<"$out" || fail "Spanish match JSON"
ok "Spanish request matches canonical practices"

set +e
out="$($TOOL match --host claude --text 'loop earrings' --json)"; rc=$?
set -e
test "$rc" -eq 2 || fail "no-match exit"
python3 -c 'import json,sys; x=json.load(sys.stdin); assert x == {"matches": [], "reason": "no_match", "status": "abstain"}' <<<"$out" || fail "no-match JSON"
ok "no match abstains with exit 2"

set +e
out="$($TOOL match --host hermes --text 'compre cuatro frenos para la bicicleta' --json)"; rc=$?
set -e
test "$rc" -eq 2 || fail "Spanish false-positive exit"
python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "abstain"' <<<"$out" || fail "Spanish false-positive JSON"
ok "unrelated Spanish abstains"

set +e
out="$($TOOL match --host future-host --text 'contract before dispatch' --json)"; rc=$?
set -e
test "$rc" -eq 2 || fail "unsupported-host exit"
grep -q 'unsupported_host' <<<"$out" || fail "unsupported-host JSON"
ok "unsupported host abstains"

out="$($TOOL evaluate --host codex --text 'run until done with a no-progress brake' --context "$TMP/healthy.json" --json)" || fail "ready evaluation"
python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["status"]=="ready" and x["receipt"]["authority"]["may_retry"] is False' <<<"$out" || fail "ready receipt"
ok "healthy context is ready with no authority"

set +e
out="$($TOOL evaluate --host hermes --text 'MCP tool output is untrusted input' --context "$TMP/unhealthy.json" --json)"; rc=$?
set -e
test "$rc" -eq 3 || fail "blocked exit"
python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["status"]=="blocked"; assert "rescue.dependency-unhealthy" in x["rescue_policy_ids"]' <<<"$out" || fail "blocked rescue"
ok "unhealthy dependency blocks with typed rescue"

set +e
out="$($TOOL evaluate --host codex --text 'run until done with a no-progress brake' --context "$TMP/spinning.json" --json)"; rc=$?
set -e
test "$rc" -eq 3 || fail "spinning trace blocked exit"
python3 -c 'import json,sys; x=json.load(sys.stdin); failed={c["check_id"] for c in x["checks"] if not c["passed"]}; assert x["status"]=="blocked" and "progress.not-spinning" in failed and "rescue.no-progress" in x["rescue_policy_ids"]' <<<"$out" || fail "spinning trace blocked JSON"
ok "third identical action/result pair blocks despite numeric progress"

ln -s "$TMP/healthy.json" "$TMP/context-link.json"
set +e
$TOOL evaluate --host codex --text 'run until done' --context "$TMP/context-link.json" --json >"$TMP/symlink.out" 2>"$TMP/symlink.err"; rc=$?
set -e
test "$rc" -eq 1 || fail "symlink refusal exit"
grep -qi 'symlink' "$TMP/symlink.err" || fail "symlink diagnostic"
ok "context symlinks are refused"

set +e
$TOOL match --host codex --text "$(python3 -c 'print("x"*4097)')" --json >"$TMP/long.out" 2>"$TMP/long.err"; rc=$?
set -e
test "$rc" -eq 1 || fail "text cap exit"
grep -q '4096' "$TMP/long.err" || fail "text cap diagnostic"
ok "input text is capped before matching"

out="$($TOOL catalog --json)" || fail "catalog command"
python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["schema_version"]==1 and x["practice_count"]==20 and len(x["levels"])==5' <<<"$out" || fail "catalog summary"
ok "catalog reports bounded summary"

printf '1..%d\n' "$PASS"
