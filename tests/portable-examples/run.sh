#!/usr/bin/env bash
# tests/portable-examples/run.sh — execute the portable orchestration examples
# and verify observable final state / printed evidence. All work happens inside
# disposable temp directories; the caller's project files are never touched.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EXAMPLES="$ROOT/examples/portable-orchestration"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# The test owns a single scratch directory. Everything else lives inside it.
TEST_TMP="$(mktemp -d -t portable-examples-test-XXXXXX)"
CALLER_CWD="$TEST_TMP/caller-cwd"
mkdir -p "$CALLER_CWD"
SCRATCH="$TEST_TMP/scratch"
mkdir -p "$SCRATCH"
# Captures for cleanup via the single EXIT trap.
out1=""
out2=""
out3=""

cleanup() {
    rm -rf "$TEST_TMP"
}
trap cleanup EXIT

# Caller sentinel inside the test-owned caller-cwd; the real caller cwd is untouched.
CALLER_SENTINEL="$CALLER_CWD/.portable-examples-caller-sentinel"
echo "caller-unchanged" > "$CALLER_SENTINEL"

# Build a blocked-PATH with dummy provider/network executables that record any
# attempt to invoke them. The probe log path is passed to each dummy as an env
# var, so the path can contain spaces/apostrophes without quoting issues.
BLOCKED="$SCRATCH/blocked"
PROBE_LOG="$SCRATCH/probe.log"
mkdir -p "$BLOCKED"
for name in ssh curl wget oll provider-ask cross-review; do
    cat > "$BLOCKED/$name" <<'EOF'
#!/bin/sh
printf "%s\n" "$0" >> "$PROBE_LOG"
exit 99
EOF
    chmod +x "$BLOCKED/$name"
done

# Preserve the interpreter selected by this host, including setup-python in CI.
PYTHON3="$(command -v python3)" || fail "no usable python3 found"
PYTHON_DIR="$(dirname "$PYTHON3")"
mkdir -p "$SCRATCH/tmpdir" "$SCRATCH/fake-home"

run_example() {
    local script="$1"
    local out="$2"
    # Run from the test-owned caller-cwd with the blocked PATH prefix, disposable
    # HOME, and a scratch TMPDIR. Inherited env is stripped with env -i except
    # for the PATH/HOME/TMPDIR the test controls.
    (
        cd "$CALLER_CWD"
        env -i \
            PATH="$BLOCKED:$ROOT/bin:$PYTHON_DIR:/usr/bin:/bin:/usr/local/bin" \
            HOME="$SCRATCH/fake-home" \
            TMPDIR="$SCRATCH/tmpdir" \
            PROBE_LOG="$PROBE_LOG" \
            bash "$script" > "$out" 2>&1
    ) || fail "$(basename "$script") exited nonzero"
}

assert_scratch_empty_after() {
    local name="$1"
    python3 - "$SCRATCH/tmpdir" "$SCRATCH/fake-home" <<'CHECK'
from pathlib import Path
import sys
for root in sys.argv[1:]:
    assert not list(Path(root).iterdir()), "example left state in " + root
CHECK

}

echo "== memory.sh =="
out1="$TEST_TMP/memory.out"
run_example "$EXAMPLES/memory.sh" "$out1"
grep -q 'evidence: add -> supersede -> consolidate transitions verified' "$out1" \
    || fail "memory.sh did not print expected evidence"
grep -q '/demo-repo/.agents/memory' "$out1" \
    || fail "memory.sh did not show an isolated memory path"
assert_scratch_empty_after memory.sh

echo "== model-eval.sh =="
out2="$TEST_TMP/model-eval.out"
run_example "$EXAMPLES/model-eval.sh" "$out2"
grep -q 'pre-registered' "$out2" || fail "model-eval.sh did not preregister"
grep -q 'golden gate passed' "$out2" || fail "model-eval.sh golden gate did not pass"
grep -q 'neutral-a' "$out2" || fail "model-eval.sh report missing neutral-a"
grep -q 'neutral-b' "$out2" || fail "model-eval.sh report missing neutral-b"
grep -qE 'KEEP INCUMBENT|beats the other' "$out2" || fail "model-eval.sh did not produce a verdict"
grep -q 'synthetic' "$out2" || fail "model-eval.sh did not use synthetic rows"
assert_scratch_empty_after model-eval.sh

echo "== session-log.sh =="
out3="$TEST_TMP/session-log.out"
run_example "$EXAMPLES/session-log.sh" "$out3"
grep -q 'evidence: durable changelog entries survive, WIP overwritten, tail/check verified' "$out3" \
    || fail "session-log.sh did not print expected evidence"
grep -q 'first synthetic session entry' "$out3" || fail "first changelog entry missing"
grep -q 'second synthetic session entry' "$out3" || fail "second changelog entry missing"
grep -q 'Second WIP:' "$out3" || fail "second WIP state missing"
if grep -q 'First WIP:' "$out3"; then
    fail "session-log.sh first WIP text survived overwrite"
fi
assert_scratch_empty_after session-log.sh

# Caller preservation: the sentinel inside the test-owned caller-cwd must be unchanged.
[ "$(cat "$CALLER_SENTINEL")" = "caller-unchanged" ] \
    || fail "caller sentinel was modified"
# Network/provider sentinel must not have been invoked.
[ ! -s "$PROBE_LOG" ] \
    || fail "a blocked provider/network executable was invoked: $(cat "$PROBE_LOG")"

rm -f "$out1" "$out2" "$out3"

echo "portable-examples: all three scripts executed and evidence verified"
