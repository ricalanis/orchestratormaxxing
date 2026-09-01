#!/usr/bin/env bash
# Contract: after a REAL `memoryctl supersede`, every recall surface returns only
# the superseding fact (lq-fa123808 — the MemOps stale-after-supersede regression
# class, arXiv:2607.12893). tests/mem-audit proves the AUDITOR discriminates over
# hand-crafted fixtures; nothing before this drove the real add -> supersede path
# and checked what recall actually surfaces. Isolated via HARNESS_MEMORY_DIR;
# never touches a real vault.
#
# Proven red against two seeded mutants of bin/memoryctl (scratch copy, 2026-08-14):
#   - supersede that no longer flips the old fact's status -> C1b/C2b/C3a/C4 red
#   - brief that stops filtering on status                 -> C1b red
# C6 is the in-contract negative fixture: a vault shaped like the broken-supersede
# outcome (old fact still active + indexed) MUST surface the old fact in the brief
# and keep its index pointer — proving C1b/C2b are status-driven, not vacuous greps.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CTL="$ROOT/bin/memoryctl"
AUDIT="$ROOT/bin/mem-audit"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/memoryctl-recall.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { printf '  ok  %s  %s\n' "$1" "$2"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s  %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }
has()   { if printf '%s' "$1" | grep -F -- "$2" >/dev/null; then ok "$3" "$4"; else bad "$3" "$4"; fi; }
lacks() { if printf '%s' "$1" | grep -F -- "$2" >/dev/null; then bad "$3" "$4"; else ok "$3" "$4"; fi; }

unset HARNESS_MEMORY_KEY 2>/dev/null || true
export HARNESS_MEMORY_DIR="$TMP/vault"

"$CTL" add old-fact --description "the pre-supersede fact" --source fixture \
       --body "Old body." >/dev/null
"$CTL" supersede old-fact new-fact --resolution-rule last-writer-wins \
       --description "the superseding fact" --source fixture \
       --body "New body." >/dev/null

# ── C1  the SessionStart brief surfaces ONLY the successor ──
BRIEF="$("$CTL" brief)"
has   "$BRIEF" "- new-fact:" C1a "brief lists the superseding fact"
lacks "$BRIEF" "- old-fact:" C1b "brief omits the superseded fact"

# ── C2  the MEMORY.md index points only at the successor ──
INDEX="$(cat "$TMP/vault/MEMORY.md")"
has   "$INDEX" "](new-fact.md)" C2a "index links the successor"
lacks "$INDEX" "](old-fact.md)" C2b "index dropped the superseded pointer"

# ── C3  the audit row is kept AND stale-marked, linked both ways ──
OLD="$("$CTL" show old-fact)"
has "$OLD" "status: superseded"      C3a "superseded fact is stale-marked, not silently active"
has "$OLD" "superseded_by: new-fact" C3b "superseded fact points at its successor"
NEW="$("$CTL" show new-fact)"
has "$NEW" "supersedes: old-fact"    C3c "successor links back to the audit row"

# ── C4  list reports the statuses the recall surfaces rely on ──
C4="$("$CTL" list --json | python3 -c '
import json, sys
d = {m["name"]: m["status"] for m in json.load(sys.stdin)["memories"]}
print("ok" if d.get("old-fact") == "superseded" and d.get("new-fact") == "active" else "no")')"
if [ "$C4" = ok ]; then ok C4 "list shows old-fact superseded, new-fact active"
else bad C4 "list statuses wrong after supersede"; fi

# ── C5  mem-audit agrees the post-supersede vault is clean ──
if "$AUDIT" --dir "$TMP/vault" --json > "$TMP/audit.json" 2>/dev/null; then
  ok C5a "mem-audit exits 0 on the post-supersede vault"
else bad C5a "mem-audit reds a vault the tool itself just wrote"; fi
C5="$(python3 -c '
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
print("ok" if s.get("superseded_indexed", 0) == 0 and s.get("unindexed", 0) == 0 else "no")' \
  "$TMP/audit.json")"
if [ "$C5" = ok ]; then ok C5b "no stale index pointer, no unindexed successor"
else bad C5b "audit summary shows recall drift after a clean supersede"; fi

# ── C6  NEGATIVE: the broken-supersede shape MUST surface the old fact ──
# Two active facts is exactly what a supersede that failed to flip status leaves
# behind. The brief must show both and the index must keep both pointers — if it
# did not, C1b/C2b above would pass even against that regression.
export HARNESS_MEMORY_DIR="$TMP/vault-broken"
"$CTL" add old-fact --description "still active by mistake" --source fixture \
       --body "Old body." >/dev/null
"$CTL" add new-fact --description "the would-be successor" --source fixture \
       --body "New body." >/dev/null
BROKEN_BRIEF="$("$CTL" brief)"
has "$BROKEN_BRIEF" "- old-fact:" C6a "an active-status old fact DOES reach the brief (C1b discriminates)"
has "$(cat "$TMP/vault-broken/MEMORY.md")" "](old-fact.md)" C6b \
    "an active-status old fact DOES keep an index pointer (C2b discriminates)"

# ── C7  a bridged legacy fact with no last_verified can be repaired ──
# `memoryctl reverify` is the governed metadata repair path. Legacy Claude
# memories predate last_verified, so requiring the field to already exist makes
# the migration impossible without a forbidden hand-edit.
export HARNESS_MEMORY_DIR="$TMP/vault-legacy"
mkdir -p "$HARNESS_MEMORY_DIR"
cat > "$HARNESS_MEMORY_DIR/legacy-fact.md" <<'LEGACY'
---
name: legacy-fact
description: legacy fixture without verification metadata
metadata:
  node_type: memory
  type: project
  status: active
---
Legacy body sentinel.
LEGACY
printf '%s\n' '# Shared project memory' '' \
  '- [legacy-fact](legacy-fact.md) — legacy fixture without verification metadata' \
  > "$HARNESS_MEMORY_DIR/MEMORY.md"
if "$CTL" reverify legacy-fact >/dev/null 2>&1; then
  ok C7a "reverify backfills a missing legacy last_verified field"
else
  bad C7a "reverify refused a bridged legacy fact with no last_verified field"
fi
LEGACY_TEXT="$(cat "$HARNESS_MEMORY_DIR/legacy-fact.md")"
if [ "$(printf '%s\n' "$LEGACY_TEXT" | grep -c '^  last_verified:')" = 1 ]; then
  ok C7b "legacy fact has exactly one last_verified field after repair"
else
  bad C7b "legacy fact does not have exactly one last_verified field"
fi
has "$LEGACY_TEXT" "Legacy body sentinel." C7c \
  "reverify preserves the legacy memory body"

printf 'memoryctl-supersede-recall contract: %d/%d PASS\n' "$PASS" "$((PASS + FAIL))"
[ "$FAIL" -eq 0 ]
