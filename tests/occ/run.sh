#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OCC="${OCC_TOOL:-$ROOT/bin/occ}"
FAKE="$ROOT/tests/occ/fake-opencode.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0

ok(){ PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$PASS" "$1"; }
fail(){ printf 'not ok %d - %s\n' "$((PASS + 1))" "$1" >&2; exit 1; }
run_final(){ OPENCODE_BIN="$FAKE" OCC_FAKE_ARGS="$TMP/args" "$OCC" "$1" --agent glm-coder --timeout "${2:-5}" --retries "${3:-0}" --final-output; }

chmod +x "$FAKE"

# C1: reproduce the original signal. Default formatted output is not a strict
# eight-row JSONL artifact; ANSI narration and the plugin's trailing {} remain.
OPENCODE_BIN="$FAKE" "$OCC" default-noisy --agent glm-coder --timeout 5 --retries 0 >"$TMP/default"
python3 - "$TMP/default" 2>/dev/null <<'PY' && fail "known-bad default transcript passed strict JSONL"
import json, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
rows = [json.loads(line) for line in lines]
assert len(rows) == 8 and [row["id"] for row in rows] == [f"A{i}" for i in range(1, 9)]
PY
ok "known-bad formatted transcript fails the strict artifact contract"

# C2: structured mode uses official isolation/events and emits only the final text.
run_final good >"$TMP/good" || fail "final-output good fixture"
test "$(cat "$TMP/good")" = $'FINAL_ONE\nFINAL_TWO' || fail "final assistant text not byte-clean"
grep -qx -- '--pure' "$TMP/args" || fail "--pure not passed"
grep -qx -- '--format' "$TMP/args" || fail "--format not passed"
grep -qx -- 'json' "$TMP/args" || fail "json format not passed"
grep -qE 'EARLY_PROGRESS|HIDDEN_REASONING|tool_use|step_start|\\033' "$TMP/good" && fail "transport/tool/reasoning leaked"
ok "final-output extracts one terminal assistant text structurally"

# C3: default mode remains unchanged and requests no structured flags.
OPENCODE_BIN="$FAKE" OCC_FAKE_ARGS="$TMP/default-args" "$OCC" default-noisy --agent glm-coder --timeout 5 --retries 0 >"$TMP/default-2"
cmp -s "$TMP/default" "$TMP/default-2" || fail "default output changed"
grep -qE '^--pure$|^--format$' "$TMP/default-args" && fail "default mode gained structured flags"
ok "default occ mode is byte-compatible"

# C4: parser failures are fail-closed and emit no artifact.
for mode in malformed unknown-event mixed-session error-event missing-text ambiguous oversized-final oversized-stream; do
  set +e
  run_final "$mode" >"$TMP/$mode.out" 2>"$TMP/$mode.err"
  rc=$?
  set -e
  test "$rc" -ne 0 || fail "$mode unexpectedly passed"
  test ! -s "$TMP/$mode.out" || fail "$mode leaked an artifact"
done
ok "malformed, drifted, mixed, missing, ambiguous and oversized events fail closed"

# C5: extraction never repairs assistant-authored content.
run_final assistant-bad-jsonl >"$TMP/bad-assistant" || fail "assistant bad JSONL transport"
test "$(cat "$TMP/bad-assistant")" = $'{"id":1}\n{}' || fail "assistant content was altered"
python3 - "$TMP/bad-assistant" 2>/dev/null <<'PY' && fail "assistant's malformed artifact passed caller contract"
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert len(rows) == 1 and rows[0]["id"] == 1
PY
ok "transport extraction preserves assistant defects for caller rejection"

# C6: real failures are returned once; only timeout retries.
set +e
run_final nonzero >"$TMP/nonzero.out" 2>"$TMP/nonzero.err"
rc=$?
set -e
test "$rc" -eq 7 || fail "nonzero child exit not preserved ($rc)"
test ! -s "$TMP/nonzero.out" || fail "nonzero child emitted artifact"
grep -q 'model failed' "$TMP/nonzero.err" && fail "raw child stderr leaked"

set +e
run_final timeout 1 1 >"$TMP/timeout.out" 2>"$TMP/timeout.err"
rc=$?
set -e
test "$rc" -eq 124 || fail "timeout did not exhaust as 124 ($rc)"
grep -q 'attempt 1 timed out' "$TMP/timeout.err" || fail "timeout retry missing"
test ! -s "$TMP/timeout.out" || fail "timeout emitted artifact"
ok "nonzero exits surface once and timeout alone retries"

printf '1..%d\n' "$PASS"
