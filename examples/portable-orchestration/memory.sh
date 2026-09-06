#!/usr/bin/env bash
# memory.sh — self-contained shared-memory example using bin/memoryctl.
#
# Creates a disposable git repo, runs add -> supersede -> consolidate with safe
# invented facts, prints evidence, then cleans up. Never touches the caller's
# real project memory.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEMORYCTL="$ROOT/bin/memoryctl"

TMP="$(mktemp -d -t portable-memory-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# Build an isolated git project so memoryctl scopes to a fake repo.
REPO="$TMP/demo-repo"
mkdir -p "$REPO"
git -C "$REPO" init -q

# Disposable HOME and cleared inherited state paths so only owned temp dirs change.
export HOME="$TMP/home"
mkdir -p "$HOME"
export HARNESS_MEMORY_DIR="$REPO/.agents/memory"
unset XDG_DATA_HOME XDG_CONFIG_HOME

cd "$REPO"

echo "=== isolated memory store: $HARNESS_MEMORY_DIR"

"$MEMORYCTL" init

printf 'Body for the first synthetic fact.' \
  | "$MEMORYCTL" add demo-fact-1 \
      --description "first synthetic memory" \
      --type project --source "portable-orchestration-example"

printf 'Updated body for the revised synthetic fact.' \
  | "$MEMORYCTL" supersede demo-fact-1 demo-fact-2 \
      --resolution-rule last-writer-wins \
      --description "revised synthetic memory" \
      --type project --source "portable-orchestration-example"

printf 'First of two mergeable notes.' \
  | "$MEMORYCTL" add demo-fact-a \
      --description "mergeable note A" \
      --type project --source "portable-orchestration-example"

printf 'Second of two mergeable notes.' \
  | "$MEMORYCTL" add demo-fact-b \
      --description "mergeable note B" \
      --type project --source "portable-orchestration-example"

printf 'Consolidated body replacing both notes.' \
  | "$MEMORYCTL" consolidate demo-fact-a demo-fact-b \
      --into demo-fact-combined \
      --resolution-rule evidence-merge \
      --description "consolidated synthetic memory" \
      --type project --source "portable-orchestration-example" \
      --sensitivity normal --json

echo
STATE="$TMP/state.json"
"$MEMORYCTL" list --json > "$STATE"

# Emit concise, machine-readable evidence lines the test can inspect.
echo
python3 - "$STATE" <<'PY'
import json, sys
mem = json.load(open(sys.argv[1]))["memories"]
status = {m["name"]: m["status"] for m in mem}
assert status.get("demo-fact-1") == "superseded", status
assert status.get("demo-fact-2") == "active", status
assert status.get("demo-fact-a") == "superseded", status
assert status.get("demo-fact-b") == "superseded", status
assert status.get("demo-fact-combined") == "active", status
print("evidence: add -> supersede -> consolidate transitions verified")
PY

# On exit the trap removes $TMP, including the isolated repo and fake HOME.
