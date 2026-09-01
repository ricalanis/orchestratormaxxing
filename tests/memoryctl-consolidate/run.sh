#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEMORYCTL="$ROOT/bin/memoryctl"
MEMAUDIT="$ROOT/bin/mem-audit"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/memoryctl-consolidate.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok() { printf '  ok  %s  %s\n' "$1" "$2"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s  %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }
check() { if [ "$1" = "$2" ]; then ok "$3" "$4"; else bad "$3" "$4 (want '$2', got '$1')"; fi; }

new_store() {
  local dir="$TMP/$1"
  mkdir -p "$dir"
  HARNESS_MEMORY_DIR="$dir" "$MEMORYCTL" init >/dev/null
  printf '%s\n' "$dir"
}

add_fact() {
  local dir="$1" name="$2" sensitivity="${3:-normal}"
  HARNESS_MEMORY_DIR="$dir" "$MEMORYCTL" add "$name" \
    --description "$name fact" --type project --source contract \
    --sensitivity "$sensitivity" --body "$name body" >/dev/null
}

snapshot() {
  local dir="$1"
  find "$dir" -maxdepth 1 -type f ! -name '.memory.lock' -print0 |
    sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
}

consolidate() {
  local dir="$1"; shift
  HARNESS_MEMORY_DIR="$dir" "$MEMORYCTL" consolidate "$@" \
    --into merged --description 'merged facts' --type project \
    --source contract --sensitivity normal \
    --resolution-rule evidence-merge --body 'merged body' --json
}

S="$(new_store happy)"
add_fact "$S" gamma
add_fact "$S" alpha
add_fact "$S" beta
add_fact "$S" keep
OUT="$TMP/happy.json"
if consolidate "$S" gamma alpha beta >"$OUT" 2>"$TMP/happy.err"; then
  ok C1 "three active facts consolidate into one successor"
else
  bad C1 "three active facts consolidate into one successor"
fi

python3 - "$S" "$OUT" <<'PY' >/dev/null 2>&1
import json, pathlib, sys
store, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
receipt = json.loads(out.read_text())
assert set(receipt) == {"schema", "target", "sources", "changed", "hashes"}
assert receipt["schema"] == 1 and receipt["target"] == "merged"
assert receipt["sources"] == ["alpha", "beta", "gamma"]
assert set(receipt["changed"]) == {"alpha.md", "beta.md", "gamma.md", "merged.md", "MEMORY.md"}
assert set(receipt["hashes"]) == set(receipt["changed"])
for old in ("alpha", "beta", "gamma"):
    text = (store / f"{old}.md").read_text()
    assert "status: superseded" in text
    assert "superseded_by: merged" in text
    assert "resolution_rule: evidence-merge" in text
target = (store / "merged.md").read_text()
assert 'supersedes: ["alpha","beta","gamma"]' in target
index = (store / "MEMORY.md").read_text()
assert "(merged.md)" in index
assert "(keep.md)" in index and "— keep fact" in index
assert "— merged facts" in index and '— "merged facts"' not in index
assert all(f"({old}.md)" not in index for old in ("alpha", "beta", "gamma"))
PY
if [ "$?" -eq 0 ]; then ok C2 "receipt, backlinks and live index are exact"; else bad C2 "receipt, backlinks and live index are exact"; fi

HARNESS_MEMORY_DIR="$S" "$MEMAUDIT" --json >"$TMP/audit.json" 2>/dev/null
python3 - "$TMP/audit.json" <<'PY' >/dev/null 2>&1
import json, sys
d=json.load(open(sys.argv[1]))
assert d["issues"] == []
assert d["summary"]["active_count"] == 2
assert d["summary"]["indexed_active_count"] == 2
assert d["summary"]["duplicate_index_targets"] == 0
PY
if [ "$?" -eq 0 ]; then ok C3 "mem-audit validates N-to-1 and reports active/index counts"; else bad C3 "mem-audit validates N-to-1 and reports active/index counts"; fi

invalid_case() {
  local id="$1" label="$2" setup="$3"; shift 3
  local dir before after
  dir="$(new_store "$id")"
  add_fact "$dir" alpha
  add_fact "$dir" beta
  eval "$setup"
  before="$(snapshot "$dir")"
  if HARNESS_MEMORY_DIR="$dir" "$MEMORYCTL" consolidate "$@" \
      --into merged --description merged --type project --source contract \
      --sensitivity normal --resolution-rule evidence-merge --body body \
      --json >/dev/null 2>&1; then
    bad "$id" "$label is rejected"
    return
  fi
  after="$(snapshot "$dir")"
  check "$after" "$before" "$id" "$label is rejected before any write"
}

invalid_case C4 "fewer than two sources" : alpha
invalid_case C5 "duplicate source" : alpha alpha
invalid_case C6 "target also listed as source" : alpha merged
invalid_case C7 "unknown source" : alpha missing
invalid_case C8 "existing target" 'add_fact "$dir" merged' alpha beta
invalid_case C9 "superseded source" 'HARNESS_MEMORY_DIR="$dir" "$MEMORYCTL" supersede alpha alpha-new --description new --type project --source contract --resolution-rule evidence-merge --body body >/dev/null' alpha beta
invalid_case C10 "mixed sensitivity" 'add_fact "$dir" secret sensitive' alpha secret
invalid_case C12 "duplicate link metadata" 'python3 - "$dir/alpha.md" <<'"'"'PY'"'"'
import pathlib, sys
p=pathlib.Path(sys.argv[1]); text=p.read_text(); marker="  status: active\n"
assert marker in text; p.write_text(text.replace(marker, marker + marker, 1))
PY' alpha beta

S="$(new_store two-source)"
add_fact "$S" alpha
add_fact "$S" beta
if consolidate "$S" beta alpha >/dev/null 2>&1; then
  ok C13 "the minimum valid two-source consolidation succeeds"
else
  bad C13 "the minimum valid two-source consolidation succeeds"
fi

S="$(new_store secret-body)"
add_fact "$S" alpha
add_fact "$S" beta
BEFORE="$(snapshot "$S")"
if HARNESS_MEMORY_DIR="$S" "$MEMORYCTL" consolidate alpha beta --into merged \
    --description merged --type project --source contract --sensitivity normal \
    --resolution-rule evidence-merge --body 'api_key=abcdef123456' --json >/dev/null 2>&1; then  # gitleaks:allow
  bad C14 "credential-like merged content is rejected"
elif [ "$(snapshot "$S")" = "$BEFORE" ]; then
  ok C14 "credential-like merged content is rejected before any write"
else
  bad C14 "credential-like rejection leaves the vault byte-identical"
fi

missing_required() {
  local id="$1" missing="$2"
  local dir before after err rc args
  dir="$(new_store "$id")"
  add_fact "$dir" alpha
  add_fact "$dir" beta
  before="$(snapshot "$dir")"
  args=(consolidate alpha beta --into merged --type project
        --sensitivity normal --resolution-rule evidence-merge --body body --json)
  [ "$missing" = "--description" ] || args+=(--description merged)
  [ "$missing" = "--source" ] || args+=(--source contract)
  err="$TMP/$id.err"
  HARNESS_MEMORY_DIR="$dir" "$MEMORYCTL" "${args[@]}" >/dev/null 2>"$err"
  rc=$?
  after="$(snapshot "$dir")"
  if [ "$rc" -ne 0 ] && grep -q -- "$missing" "$err" && \
      grep -q 'required' "$err" && [ "$after" = "$before" ]; then
    ok "$id" "$missing is an argparse-required preflight"
  else
    bad "$id" "$missing is an argparse-required preflight"
  fi
}

missing_required C15 --description
missing_required C16 --source

# Fault injection after each replace must roll back the complete store.
S="$(new_store rollback)"
add_fact "$S" alpha
add_fact "$S" beta
add_fact "$S" gamma
BASE="$(snapshot "$S")"
ROLLBACK_OK=yes
for n in 1 2 3 4 5; do
  HARNESS_MEMORY_DIR="$S" MEMORYCTL_FAIL_AFTER_REPLACE="$n" \
    "$MEMORYCTL" consolidate alpha beta gamma --into merged \
    --description merged --type project --source contract --sensitivity normal \
    --resolution-rule evidence-merge --body body --json >/dev/null 2>&1 && ROLLBACK_OK=no
  [ "$(snapshot "$S")" = "$BASE" ] || ROLLBACK_OK=no
done
check "$ROLLBACK_OK" yes C11 "every injected replace failure rolls the vault back byte-for-byte"

printf '# memoryctl-consolidate: %d/%d checks passed\n' "$PASS" "$((PASS + FAIL))"
[ "$FAIL" -eq 0 ]
