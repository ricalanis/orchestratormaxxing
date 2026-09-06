#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The expected answer belongs to the verifier, never the adapter/model payload.
python3 - "$repo/bin/worker-path-bench" <<'PY'
import json, runpy, sys
mod = runpy.run_path(sys.argv[1])
case = {"id":"hidden", "prompt":"say x", "answer":"SECRET_ANSWER", "contract":{"type":"exact", "expected":"SECRET_EXPECTED"}}
payload = mod["adapter_payload"](case)
assert "contract" not in payload
assert "answer" not in payload
assert "SECRET_EXPECTED" not in json.dumps(payload)
assert "SECRET_ANSWER" not in json.dumps(payload)
PY

python3 "$repo/experiments/worker-path-bench/generate_context_cases.py" \
  --sizes 16000,32000 --output "$tmp/context-cases.jsonl"
python3 - "$tmp/context-cases.jsonl" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
assert len(rows) == 2 * 3 * 3  # sizes × canary kinds × positions
assert {r["canary_kind"] for r in rows} == {"instruction", "retrieval", "code_state"}
assert {r["needle_position"] for r in rows} == {"beginning", "middle", "end"}
assert {r["target_input_tokens"] for r in rows} == {16000, 32000}
assert all(r["protected_fact"] is True for r in rows)
assert all(abs(r["estimated_input_tokens"] - r["target_input_tokens"]) <= 128 for r in rows)
PY

python3 "$repo/bin/worker-path-bench" \
  --cases "$repo/experiments/worker-path-bench/cases.jsonl" \
  --adapter "pass=python3 $repo/experiments/worker-path-bench/fixtures/pass_adapter.py" \
  --adapter "fail=python3 $repo/experiments/worker-path-bench/fixtures/fail_adapter.py" \
  --output "$tmp/results.jsonl" --summary "$tmp/summary.json"

python3 - "$tmp/results.jsonl" "$tmp/summary.json" <<'PY'
import json, sys
results = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
summary = json.load(open(sys.argv[2]))
assert len(results) == 4
assert [(r["adapter"], r["case_id"]) for r in results] == [
    ("fail", "exact-ping"), ("fail", "json-fields"),
    ("pass", "exact-ping"), ("pass", "json-fields"),
]
assert all(r["schema_valid"] for r in results)
assert [r["contract_pass"] for r in results if r["adapter"] == "pass"] == [True, True]
assert [r["contract_pass"] for r in results if r["adapter"] == "fail"] == [False, False]
assert summary["adapters"]["pass"]["passed"] == 2
assert summary["adapters"]["fail"]["contract_failures"] == 2
assert summary["adapters"]["pass"]["infrastructure_failures"] == 0
assert 1 <= summary["adapters"]["pass"]["latency_ms_p50"] < 1000
assert 1 <= summary["adapters"]["pass"]["latency_ms_p95"] < 1000
assert open(sys.argv[1], "rb").read().endswith(b"\n")
assert open(sys.argv[2], "rb").read().endswith(b"\n")
PY

cat >"$tmp/context.jsonl" <<'EOF'
{"id":"context","prompt":"retain this: PROTECTED FACT: NEEDLE","timeout_seconds":10,"target_input_tokens":32000,"estimated_input_tokens":31990,"canary_kind":"retrieval","needle_position":"middle","protected_fact":true,"contract":{"type":"exact","expected":"NEEDLE"}}
EOF
python3 "$repo/bin/worker-path-bench" \
  --cases "$tmp/context.jsonl" \
  --adapter "context=python3 $repo/experiments/worker-path-bench/fixtures/context_adapter.py" \
  --output "$tmp/context-results.jsonl" --summary "$tmp/context-summary.json"
python3 - "$tmp/context-results.jsonl" "$tmp/context-summary.json" <<'PY'
import json, sys
r = json.loads(open(sys.argv[1]).read())
s = json.load(open(sys.argv[2]))["adapters"]["context"]
assert r["contract_pass"] is True
assert r["target_input_tokens"] == 32000
assert r["estimated_input_tokens"] == 31990
assert r["canary_kind"] == "retrieval"
assert r["needle_position"] == "middle"
assert r["protected_fact"] is True
assert r["protected_fact_pass"] is True
assert r["input_tokens_proxy"] == 32000
assert r["input_tokens_actual"] == 1234
assert r["compaction_count"] == 1
assert r["cache_read_tokens"] == 600
assert s["protected_fact_losses"] == 0
assert s["compaction_count"] == 1
assert s["input_tokens_proxy_total"] == 32000
assert s["input_tokens_actual_total"] == 1234
assert s["cache_read_tokens_total"] == 600
PY


python3 - "$repo/experiments/worker-path-bench/score.py" <<'PY'
import runpy, sys
m = runpy.run_path(sys.argv[1])
rows = [{
    "adapter":"a", "contract_pass":False, "infrastructure_failure":False,
    "schema_valid":True, "latency_ms":10, "protected_fact":True,
    "input_tokens_proxy":32000, "input_tokens_actual":30000,
    "compaction_count":2, "cache_read_tokens":100,
}]
s = m["compute_summary"](rows)["adapters"]["a"]
assert s["protected_fact_losses"] == 1
assert s["compaction_count"] == 2
assert s["input_tokens_proxy_total"] == 32000
assert s["input_tokens_actual_total"] == 30000
assert s["cache_read_tokens_total"] == 100
PY

cat >"$tmp/timeout.jsonl" <<'EOF'
{"id":"timeout","prompt":"wait","timeout_seconds":0.05,"contract":{"type":"exact","expected":"PONG"}}
EOF
python3 "$repo/bin/worker-path-bench" \
  --cases "$tmp/timeout.jsonl" \
  --adapter "sleep=python3 $repo/experiments/worker-path-bench/fixtures/sleep_adapter.py" \
  --output "$tmp/timeout-results.jsonl" --summary "$tmp/timeout-summary.json"
python3 - "$tmp/timeout-results.jsonl" <<'PY'
import json, sys
r = json.loads(open(sys.argv[1]).read())
assert r["timed_out"] is True
assert r["infrastructure_failure"] is True
assert r["contract_pass"] is False
assert 40 <= r["latency_ms"] < 1000
assert r["exit_code"] is not None
PY

for mode in bad_json bad_usage bad_events nonzero; do
  cat >"$tmp/$mode.jsonl" <<EOF
{"id":"$mode","prompt":"x","timeout_seconds":1,"fixture_mode":"$mode","contract":{"type":"exact","expected":"PONG"}}
EOF
  python3 "$repo/bin/worker-path-bench" \
    --cases "$tmp/$mode.jsonl" \
    --adapter "broken=python3 $repo/experiments/worker-path-bench/fixtures/malformed_adapter.py" \
    --output "$tmp/$mode-results.jsonl" --summary "$tmp/$mode-summary.json"
  python3 - "$tmp/$mode-results.jsonl" <<'PY'
import json, sys
r = json.loads(open(sys.argv[1]).read())
assert r["infrastructure_failure"] is True
assert r["contract_pass"] is False
PY
done

marker="$tmp/orphan-marker"
pidfile="$tmp/spawn-pid"
cat >"$tmp/orphan.jsonl" <<EOF
{"id":"orphan","prompt":"x","marker":"$marker","pidfile":"$pidfile","timeout_seconds":0.05,"contract":{"type":"exact","expected":"PONG"}}
EOF
python3 "$repo/bin/worker-path-bench" \
  --cases "$tmp/orphan.jsonl" \
  --adapter "spawn=python3 $repo/experiments/worker-path-bench/fixtures/spawn_adapter.py" \
  --output "$tmp/orphan-results.jsonl" --summary "$tmp/orphan-summary.json"
sleep 0.6
[ ! -e "$marker" ]
! kill -0 "$(cat "$pidfile")" 2>/dev/null

set +e
for omitted in cases output summary; do
  args=(--cases "$repo/experiments/worker-path-bench/cases.jsonl" \
    --adapter "pass=python3 $repo/experiments/worker-path-bench/fixtures/pass_adapter.py" \
    --output "$tmp/required-output" --summary "$tmp/required-summary")
  case "$omitted" in
    cases) args=("${args[@]:2}") ;;
    output) args=("${args[@]:0:4}" "${args[@]:6}") ;;
    summary) args=("${args[@]:0:6}") ;;
  esac
  python3 "$repo/bin/worker-path-bench" "${args[@]}" >/dev/null 2>&1
  [ "$?" -eq 2 ] || exit 1
done
python3 "$repo/bin/worker-path-bench" --cases "$repo/experiments/worker-path-bench/cases.jsonl" \
  --adapter "invalid" --output "$tmp/x" --summary "$tmp/y" >/dev/null 2>&1
[ "$?" -eq 2 ] || exit 1
python3 "$repo/bin/worker-path-bench" --cases "$repo/experiments/worker-path-bench/cases.jsonl" \
  --adapter "=python3 -c pass" --output "$tmp/x" --summary "$tmp/y" >/dev/null 2>&1
[ "$?" -eq 2 ] || exit 1
python3 "$repo/bin/worker-path-bench" --cases "$repo/experiments/worker-path-bench/cases.jsonl" \
  --adapter "empty=" --output "$tmp/x" --summary "$tmp/y" >/dev/null 2>&1
[ "$?" -eq 2 ] || exit 1
python3 "$repo/bin/worker-path-bench" --cases "$repo/experiments/worker-path-bench/cases.jsonl" \
  --adapter "equals=python3 $repo/experiments/worker-path-bench/fixtures/pass_adapter.py --label=a=b" \
  --output "$tmp/equals-results" --summary "$tmp/equals-summary" >/dev/null 2>&1
[ "$?" -eq 0 ] || exit 1
cat >"$tmp/missing.jsonl" <<'EOF'
{"id":"missing","prompt":"x","timeout_seconds":1,"contract":{"type":"exact","expected":"PONG"}}
EOF
python3 "$repo/bin/worker-path-bench" --cases "$tmp/missing.jsonl" \
  --adapter "missing=$tmp/does-not-exist" --output "$tmp/missing-results" --summary "$tmp/missing-summary"
python3 - "$tmp/missing-results" <<'PY'
import json, sys
r = json.loads(open(sys.argv[1]).read())
assert r["infrastructure_failure"] is True
assert r["schema_valid"] is False
assert r["exit_code"] is None
assert "does-not-exist" in r["stderr"]
PY
python3 "$repo/bin/worker-path-bench" --cases "$repo/experiments/worker-path-bench/cases.jsonl" \
  --adapter "dup=python3 -c pass" --adapter "dup=python3 -c pass" \
  --output "$tmp/x" --summary "$tmp/y" >/dev/null 2>&1
[ "$?" -eq 2 ] || exit 1
: >"$tmp/empty.jsonl"
python3 "$repo/bin/worker-path-bench" --cases "$tmp/empty.jsonl" \
  --adapter "pass=python3 $repo/experiments/worker-path-bench/fixtures/pass_adapter.py" \
  --output "$tmp/x" --summary "$tmp/y" >/dev/null 2>&1
[ "$?" -eq 2 ] || exit 1
cat >"$tmp/bad-id.jsonl" <<'EOF'
{"id":7,"prompt":"x","timeout_seconds":1,"contract":{"type":"exact","expected":"PONG"}}
EOF
python3 "$repo/bin/worker-path-bench" --cases "$tmp/bad-id.jsonl" \
  --adapter "pass=python3 $repo/experiments/worker-path-bench/fixtures/pass_adapter.py" \
  --output "$tmp/x" --summary "$tmp/y" >/dev/null 2>&1
[ "$?" -eq 2 ] || exit 1
set -e

python3 - "$repo" <<'PY'
import importlib.util, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "experiments/worker-path-bench/score.py"
spec = importlib.util.spec_from_file_location("score_contract", p)
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
assert s.parse_envelope('{"output":"x","usage":{},"events":[]}')[1] is True
assert s.parse_envelope('{}')[1] is False
assert s.parse_envelope('[]')[1] is False
assert s.parse_envelope('not-json')[1] is False
assert s.parse_envelope('{"output":"x","usage":[],"events":[]}')[1] is False
assert s.parse_envelope('{"output":"x","usage":{},"events":{}}')[1] is False
assert s.score_contract({"type":"exact","expected":"x"}, "x") is True
assert s.score_contract({"type":"exact","expected":"x"}, "y") is False
assert s.score_contract({"type":"json_fields","expected":{"a":1}}, '{"a":1,"b":2}') is True
assert s.score_contract({"type":"json_fields","expected":{"a":1}}, '{"a":2}') is False
assert s.score_contract({"type":"json_fields","expected":{"a":1}}, 'not-json') is False
assert s.score_contract({"type":"unknown","expected":1}, 1) is False
assert s.percentile([], 0.5) == 0.0
assert s.percentile([7], 0.95) == 7.0
assert s.percentile([1, 3], 0.5) == 2
assert s.percentile([30, 10, 20], 0.95) == 29
rows = [
    {"adapter":"x","contract_pass":True,"infrastructure_failure":False,"schema_valid":True,"latency_ms":30},
    {"adapter":"x","contract_pass":False,"infrastructure_failure":False,"schema_valid":True,"latency_ms":10},
    {"adapter":"x","contract_pass":False,"infrastructure_failure":True,"schema_valid":False,"latency_ms":20},
]
assert s.compute_summary([]) == {"adapters": {}}
assert s.compute_summary(rows) == {"adapters":{"x":{
    "total":3,"passed":1,"contract_failures":1,"infrastructure_failures":1,
    "schema_valid":2,"latency_ms_p50":20.0,"latency_ms_p95":29.0,
    "input_tokens_proxy_observed_cases":0,"input_tokens_proxy_total":None,
    "input_tokens_actual_observed_cases":0,"input_tokens_actual_total":None,
    "cache_read_tokens_observed_cases":0,"cache_read_tokens_total":None,
    "compaction_observed_cases":0,"compaction_count":None,
}}}
PY
