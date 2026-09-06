#!/usr/bin/env bash
# session-log.sh — self-contained session persistence example using bin/session-log.
#
# Creates a disposable git repo, writes durable changelog entries, overwrites
# the sticky WIP twice, shows the tail/recovery view, runs the staleness check,
# then cleans up. Never touches the caller's real project docs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_LOG="$ROOT/bin/session-log"

TMP="$(mktemp -d -t portable-session-log-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

REPO="$TMP/demo-repo"
mkdir -p "$REPO"
git -C "$REPO" init -q

# Use a disposable HOME and clear inherited state paths so only owned temp
# directories are mutated.
export HOME="$TMP/home"
mkdir -p "$HOME"
export SESSION_LOG_DIR="$REPO/docs"
export SESSION_LOG_TS="2026-09-05 12:00"
unset XDG_DATA_HOME XDG_CONFIG_HOME

cd "$REPO"

echo "=== isolated docs dir: $SESSION_LOG_DIR"

# Two durable changelog entries survive.
"$SESSION_LOG" changelog "first synthetic session entry" --tag feat
"$SESSION_LOG" changelog "second synthetic session entry" --tag docs

# First WIP state.
printf 'First WIP: plan the next feature.' | "$SESSION_LOG" wip set
FIRST_WIP_FILE="$SESSION_LOG_DIR/WIP.md"

# Overwrite with a second, distinct WIP state.
printf 'Second WIP: verify overwrite replaced the first state.' | "$SESSION_LOG" wip set

# Negative control: the first WIP text must be gone.
if grep -q 'First WIP:' "$FIRST_WIP_FILE"; then
    echo "FAIL: first WIP text survived overwrite" >&2
    exit 1
fi

echo
"$SESSION_LOG" tail --n 5

echo
"$SESSION_LOG" check --json > "$TMP/check.json"

python3 - "$TMP/check.json" "$SESSION_LOG_DIR/changelog.md" "$SESSION_LOG_DIR/WIP.md" <<'PY'
import json, sys, os
result = json.load(open(sys.argv[1]))
# The stale flag is about logging relative to source changes; the example has no
# source changes, so stale must be false.
assert result["stale"] is False, result
assert os.path.isfile(sys.argv[2]), "changelog.md missing"
assert os.path.isfile(sys.argv[3]), "WIP.md missing"
changelog = open(sys.argv[2]).read()
wip = open(sys.argv[3]).read()
assert "first synthetic session entry" in changelog, changelog
assert "second synthetic session entry" in changelog, changelog
assert "First WIP:" not in wip, "first WIP text survived overwrite"
assert "Second WIP:" in wip, wip
print("evidence: durable changelog entries survive, WIP overwritten, tail/check verified")
PY
