#!/usr/bin/env bash
# Contract: mem-audit decides MEMORY.md linkage by EXACT link target, never substring.
#
# Proven red against the pre-fix `linked = (f in index_text) or (name in index_text)`:
# C1 failed (a supersede whose successor EXTENDS the old name read as still-indexed —
# an error-severity false positive that redded the governance gate forever, lq-edd3cada)
# and C3 failed (a genuinely unindexed fact was masked by a neighbouring longer slug).
# C2/C6 are the negative fixtures: they prove the gate still fires on the real drift it
# exists to catch, so C1/C3 cannot be satisfied by an always-clean auditor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/mem-audit"
MEMORYCTL="$ROOT/bin/memoryctl"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mem-audit.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok()   { printf '  ok  %s  %s\n' "$1" "$2"; PASS=$((PASS + 1)); }
bad()  { printf '  FAIL %s  %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }
check() { if [ "$1" = "$2" ]; then ok "$3" "$4"; else bad "$3" "$4 (want '$2', got '$1')"; fi; }

# write_fact <dir> <filename-stem> <name:> <status> [superseded_by] [source]
write_fact() {
  local dir="$1" stem="$2" name="$3" status="$4" sb="${5:-}" source="${6:-}"
  { printf '%s\n' '---' "name: $name" 'description: fixture fact' "status: $status"
    [ -n "$sb" ] && printf 'superseded_by: %s\nresolution_rule: last-writer-wins\n' "$sb"
    [ -n "$source" ] && printf 'source: %s\n' "$source"
    printf '%s\n' 'metadata:' '  type: project' "created: $(date +%Y-%m-%d)" \
      "last_verified: $(date +%Y-%m-%d)" '---' 'Fixture body.'
  } > "$dir/$stem.md"
}
index_line() { printf -- '- [%s](%s.md) — hook\n' "$2" "$2" >> "$1/MEMORY.md"; }
new_store()  { local d="$TMP/$1"; mkdir -p "$d"; printf '# Shared project memory\n\n' > "$d/MEMORY.md"; echo "$d"; }

# jq-free readers over --json
sumval() { python3 -c 'import json,sys;print(json.load(sys.stdin)["summary"].get(sys.argv[1],0))' "$2" < "$1"; }
has_issue() { # <json> <severity> <file> <substring>
  python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("yes" if any(i["severity"]==sys.argv[2] and i["file"]==sys.argv[3] and sys.argv[4] in i["message"] for i in d["issues"]) else "no")' "$@"
}
run() { "$TOOL" --dir "$1" --json > "$2" 2>/dev/null || true; }
replace_once() {
  python3 - "$1" "$2" "$3" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
old, new = sys.argv[2], sys.argv[3]
text = p.read_text()
assert old in text, (p, old)
p.write_text(text.replace(old, new, 1))
PY
}

# ── C1  supersede whose successor EXTENDS the old name is NOT "still indexed" ──
S="$(new_store extends)"
write_fact "$S" platform-vision platform-vision superseded platform-vision-crm-build
write_fact "$S" platform-vision-crm-build platform-vision-crm-build active
index_line "$S" platform-vision-crm-build
run "$S" "$TMP/c1.json"
check "$(sumval "$TMP/c1.json" superseded_indexed)" "0" "C1" "superseded fact removed from the index reads as unlinked even when its successor extends its name"
check "$(has_issue "$TMP/c1.json" error platform-vision.md 'still in MEMORY.md index')" "no" "C1b" "no false-positive error on the prefix-named supersede"

# ── C2  NEGATIVE: a supersede that IS still indexed must still be caught ──
S="$(new_store stillindexed)"
write_fact "$S" platform-vision platform-vision superseded platform-vision-crm-build
write_fact "$S" platform-vision-crm-build platform-vision-crm-build active
index_line "$S" platform-vision          # the stale pointer the gate exists to catch
index_line "$S" platform-vision-crm-build
run "$S" "$TMP/c2.json"
check "$(sumval "$TMP/c2.json" superseded_indexed)" "1" "C2" "a genuinely still-indexed superseded fact is still an error (gate discriminates)"
if "$TOOL" --dir "$S" >/dev/null 2>&1; then bad "C2b" "still-indexed supersede must exit non-zero"; else ok "C2b" "error severity exits non-zero"; fi

# ── C3  active fact with NO pointer is not masked by a neighbouring longer slug ──
S="$(new_store masked)"
write_fact "$S" unrelated unrelated active
write_fact "$S" unrelated-fact unrelated-fact active
index_line "$S" unrelated-fact           # only the LONGER slug is indexed
run "$S" "$TMP/c3.json"
check "$(sumval "$TMP/c3.json" unindexed)" "1" "C3" "unindexed active fact is reported even when another slug contains its name"
check "$(has_issue "$TMP/c3.json" warn unrelated.md 'no MEMORY.md index pointer')" "yes" "C3b" "the warning names the actually-unindexed file"
check "$(has_issue "$TMP/c3.json" warn unrelated-fact.md 'no MEMORY.md index pointer')" "no" "C3c" "the properly indexed neighbour is not warned"

# ── C5  linkage follows the link TARGET, so name != filename still resolves ──
S="$(new_store renamed)"
write_fact "$S" file-slug display-name active
printf -- '- [display-name](file-slug.md) — hook\n' >> "$S/MEMORY.md"
run "$S" "$TMP/c5.json"
check "$(sumval "$TMP/c5.json" unindexed)" "0" "C5" "a fact indexed by filename under a different display name counts as linked"

# ── C6  NEGATIVE: the shared parse still catches an index pointing at nothing ──
S="$(new_store orphan)"
write_fact "$S" present present active
index_line "$S" present
index_line "$S" vanished                 # no such file
run "$S" "$TMP/c6.json"
check "$(sumval "$TMP/c6.json" orphan_index)" "1" "C6" "an index pointer to a missing file is still an error (orphan check unchanged by this round)"

# ── C7  INVARIANT: a fact's verdict must not depend on OTHER slugs in the index ──
S="$(new_store invariant)"
write_fact "$S" alpha alpha active        # deliberately NOT indexed, so the verdict is "warn"
run "$S" "$TMP/c7a.json"
index_line "$S" alpha-extended-neighbour  # unrelated longer slug appears in the index
write_fact "$S" alpha-extended-neighbour alpha-extended-neighbour active
run "$S" "$TMP/c7b.json"
check "$(has_issue "$TMP/c7b.json" warn alpha.md 'no MEMORY.md index pointer')" \
      "$(has_issue "$TMP/c7a.json" warn alpha.md 'no MEMORY.md index pointer')" \
      "C7" "adding an unrelated longer slug does not change alpha's linkage verdict"

# ── C8  NEGATIVE: exactness must not cost coverage — an ANCHORED pointer to a
#       superseded fact is still caught. Constructed by a refuting critic against
#       a target-only regex anchored to `.md)`, which silently dropped this case.
S="$(new_store anchored)"
write_fact "$S" old-fact old-fact superseded new-fact
write_fact "$S" new-fact new-fact active
printf -- '- [old-fact](old-fact.md#history) — stale pointer\n' >> "$S/MEMORY.md"
index_line "$S" new-fact
run "$S" "$TMP/c8.json"
check "$(sumval "$TMP/c8.json" superseded_indexed)" "1" "C8" "an anchored index pointer to a superseded fact is still an error"

# ── C9  …and an anchored pointer still counts as a pointer for an ACTIVE fact ──
S="$(new_store anchored-active)"
write_fact "$S" beta beta active
printf -- '- [beta](beta.md#section) — hook\n' >> "$S/MEMORY.md"
run "$S" "$TMP/c9.json"
check "$(sumval "$TMP/c9.json" unindexed)" "0" "C9" "an anchored pointer is a valid index pointer, not a missing one"

# ── C10  consolidation review is snapshot-bound, not a permanent waiver ──
seed_active_store() {
  local dir="$1" count="$2" i=1 stem
  while [ "$i" -le "$count" ]; do
    stem="$(printf 'fact-%02d' "$i")"
    write_fact "$dir" "$stem" "$stem" active
    index_line "$dir" "$stem"
    i=$((i + 1))
  done
}

S="$(new_store consolidation-review)"
seed_active_store "$S" 26
run "$S" "$TMP/c10-before.json"
check "$(has_issue "$TMP/c10-before.json" warn MEMORY.md 'active memories')" "yes" \
      "C10a" "more than 25 active facts warn before a consolidation review"
if HARNESS_MEMORY_DIR="$S" "$MEMORYCTL" review-consolidation \
     --decision no-safe-merge >/dev/null 2>&1; then
  ok "C10b" "memoryctl records a governed no-safe-merge review"
else
  bad "C10b" "memoryctl records a governed no-safe-merge review"
fi
run "$S" "$TMP/c10-after.json"
check "$(has_issue "$TMP/c10-after.json" warn MEMORY.md 'active memories')" "no" \
      "C10c" "a matching review suppresses only the consolidation advisory"
python3 - "$S/.consolidation-review.json" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text())
assert set(d) == {"schema", "decision", "active_count", "active_set_sha256", "reviewed_at"}, d
assert d["schema"] == 1 and d["decision"] == "no-safe-merge" and d["active_count"] == 26, d
assert len(d["active_set_sha256"]) == 64, d
text = p.read_text()
assert "Fixture body" not in text and "fact-01.md" not in text, text
PY
ok "C10d" "receipt contains no fact names, descriptions, bodies, or paths"

# JSON-quoted frontmatter is semantic text, not its serialized escape form.
# This matches memoryctl's writer for sources/descriptions containing quotes.
S_ORDER="$(new_store consolidation-order)"
i=26
while [ "$i" -ge 1 ]; do
  stem="$(printf 'fact-%02d' "$i")"
  if [ "$i" -eq 26 ]; then
    write_fact "$S_ORDER" "$stem" "$stem" active "" '"meeting \"quoted\" note"'
  elif [ "$i" -eq 25 ]; then
    write_fact "$S_ORDER" "$stem" "$stem" active "" '"unterminated'
  elif [ "$i" -eq 24 ]; then
    write_fact "$S_ORDER" "$stem" "$stem" active "" "'single quoted'"
  elif [ "$i" -eq 23 ]; then
    write_fact "$S_ORDER" "$stem" "$stem" active "" '"bad\q"'
  else
    write_fact "$S_ORDER" "$stem" "$stem" active
  fi
  index_line "$S_ORDER" "$stem"
  i=$((i - 1))
done
HARNESS_MEMORY_DIR="$S_ORDER" "$MEMORYCTL" review-consolidation \
  --decision no-safe-merge >/dev/null 2>&1 || true
run "$S_ORDER" "$TMP/c10-order.json"
check "$(has_issue "$TMP/c10-order.json" warn MEMORY.md 'active memories')" "no" \
      "C10g" "JSON-escaped semantic fields do not invalidate a matching review"

# Transaction-time refresh is deliberately excluded from the semantic digest.
HARNESS_MEMORY_DIR="$S" "$MEMORYCTL" reverify fact-01 >/dev/null
run "$S" "$TMP/c10-reverify.json"
check "$(has_issue "$TMP/c10-reverify.json" warn MEMORY.md 'active memories')" "no" \
      "C10e" "reverify does not invalidate a current consolidation review"

# Meaning changes do invalidate the receipt.
printf '\nChanged claim.\n' >> "$S/fact-01.md"
run "$S" "$TMP/c10-edit.json"
check "$(has_issue "$TMP/c10-edit.json" warn MEMORY.md 'active memories')" "yes" \
      "C10f" "editing an active fact reopens the consolidation advisory"

# ── C11  membership changes and malformed receipts fail open ──
HARNESS_MEMORY_DIR="$S" "$MEMORYCTL" review-consolidation \
  --decision no-safe-merge >/dev/null 2>&1 || true
write_fact "$S" fact-27 fact-27 active
index_line "$S" fact-27
run "$S" "$TMP/c11-add.json"
check "$(has_issue "$TMP/c11-add.json" warn MEMORY.md 'active memories')" "yes" \
      "C11a" "adding an active fact invalidates the review receipt"
printf '{not json\n' > "$S/.consolidation-review.json"
run "$S" "$TMP/c11-malformed.json"
check "$(has_issue "$TMP/c11-malformed.json" warn MEMORY.md 'active memories')" "yes" \
      "C11b" "a malformed receipt cannot suppress the advisory"

S2="$(new_store consolidation-supersede)"
seed_active_store "$S2" 26
HARNESS_MEMORY_DIR="$S2" "$MEMORYCTL" review-consolidation \
  --decision no-safe-merge >/dev/null 2>&1 || true
HARNESS_MEMORY_DIR="$S2" "$MEMORYCTL" supersede fact-26 fact-26-new \
  --description replacement --type project --source contract \
  --resolution-rule evidence-merge --body 'replacement body' >/dev/null 2>&1 || true
run "$S2" "$TMP/c11-supersede.json"
check "$(has_issue "$TMP/c11-supersede.json" warn MEMORY.md 'active memories')" "yes" \
      "C11c" "superseding an active fact invalidates the review receipt"

# ── C12  the receipt never suppresses independent memory-health findings ──
S="$(new_store consolidation-independent)"
seed_active_store "$S" 26
HARNESS_MEMORY_DIR="$S" "$MEMORYCTL" review-consolidation \
  --decision no-safe-merge >/dev/null 2>&1 || true
python3 - "$S/fact-01.md" "$S/MEMORY.md" <<'PY'
import pathlib, re, sys
fact = pathlib.Path(sys.argv[1])
text = re.sub(r"(?m)^last_verified:.*$", "last_verified: 2000-01-01",
              fact.read_text(), count=1)
fact.write_text(text)
index = pathlib.Path(sys.argv[2])
index.write_text("\n".join(line for line in index.read_text().splitlines()
                           if "(fact-02.md)" not in line) + "\n")
PY
run "$S" "$TMP/c12.json"
check "$(has_issue "$TMP/c12.json" warn MEMORY.md 'active memories')" "no" \
      "C12a" "transaction-only and index drift do not stale the semantic receipt"
check "$(has_issue "$TMP/c12.json" warn fact-01.md 'STALE:')" "yes" \
      "C12b" "a matching receipt does not suppress staleness"
check "$(has_issue "$TMP/c12.json" warn fact-02.md 'no MEMORY.md index pointer')" "yes" \
      "C12c" "a matching receipt does not suppress index drift"

# ── C13  the original threshold remains exact ──
S="$(new_store consolidation-threshold)"
seed_active_store "$S" 25
run "$S" "$TMP/c13.json"
check "$(has_issue "$TMP/c13.json" warn MEMORY.md 'active memories')" "no" \
      "C13" "25 active facts require no consolidation receipt"

# ── C14  verification dates gate ACTIVE beliefs, not superseded history ──
S="$(new_store legacy-dates)"
cat > "$S/legacy-old.md" <<'OLD'
---
name: legacy-old
description: superseded legacy fact without dates
status: superseded
superseded_by: current-fact
resolution_rule: evidence-merge
metadata:
  type: project
---
Historical body.
OLD
cat > "$S/current-fact.md" <<'CURRENT'
---
name: current-fact
description: current verified fact
metadata:
  type: project
  status: active
  created: 2026-08-16
  last_verified: 2026-08-16
  supersedes: legacy-old
---
Current body.
CURRENT
cat > "$S/legacy-active.md" <<'ACTIVE'
---
name: legacy-active
description: active legacy fact without dates
metadata:
  type: project
  status: active
---
Active body.
ACTIVE
index_line "$S" current-fact
index_line "$S" legacy-active
run "$S" "$TMP/c14.json"
check "$(sumval "$TMP/c14.json" missing_dates)" "1" "C14a" \
  "only the active legacy fact contributes a missing-date warning"
check "$(has_issue "$TMP/c14.json" warn legacy-old.md 'no created/last_verified date')" \
  "no" "C14b" "superseded history is exempt from active-belief staleness metadata"
check "$(has_issue "$TMP/c14.json" warn legacy-active.md 'no created/last_verified date')" \
  "yes" "C14c" "active legacy facts still require a verification date"

# ── C15  N→1 backlinks accept a JSON array and remain exact in both directions ──
S="$(new_store multi-backlink)"
write_fact "$S" old-a old-a superseded merged
write_fact "$S" old-b old-b superseded merged
write_fact "$S" merged merged active
replace_once "$S/merged.md" $'metadata:\n' $'supersedes: ["old-a","old-b"]\nmetadata:\n'
index_line "$S" merged
run "$S" "$TMP/c15-valid.json"
check "$(sumval "$TMP/c15-valid.json" broken_link)" "0" \
      "C15a" "JSON-array supersedes validates two exact scalar backlinks"

replace_once "$S/old-b.md" 'superseded_by: merged' 'superseded_by: elsewhere'
run "$S" "$TMP/c15-wrong-back.json"
check "$(has_issue "$TMP/c15-wrong-back.json" error merged.md 'does not point back')" "yes" \
      "C15b" "a source pointing elsewhere invalidates the N-to-1 target"
check "$(has_issue "$TMP/c15-wrong-back.json" error old-b.md 'unknown memory')" "yes" \
      "C15b2" "an unknown superseded_by target is reported on its source"
check "$(sumval "$TMP/c15-wrong-back.json" broken_link)" "2" \
      "C15b3" "broken-link count equals the two distinct failed edges"

replace_once "$S/old-b.md" 'superseded_by: elsewhere' 'superseded_by: merged'
replace_once "$S/merged.md" 'supersedes: ["old-a","old-b"]' 'supersedes: ["old-a","old-a"]'
run "$S" "$TMP/c15-duplicate.json"
check "$(has_issue "$TMP/c15-duplicate.json" error merged.md 'duplicate memory names')" "yes" \
      "C15c" "duplicate N-to-1 sources are rejected"

replace_once "$S/merged.md" 'supersedes: ["old-a","old-a"]' 'supersedes: ["old-a","old-b"]'
cp "$S/MEMORY.md" "$TMP/c15-index"
index_line "$S" merged
run "$S" "$TMP/c15-index-duplicate.json"
check "$(sumval "$TMP/c15-index-duplicate.json" duplicate_index_targets)" "1" \
      "C15d" "duplicate live-index targets are counted and rejected"
check "$(has_issue "$TMP/c15-index-duplicate.json" error MEMORY.md 'duplicate memory index targets')" "yes" \
      "C15d2" "a duplicate live-index target is an error, not only a counter"
cp "$TMP/c15-index" "$S/MEMORY.md"

replace_once "$S/merged.md" 'status: active' 'status: superseded'
run "$S" "$TMP/c15-inactive-target.json"
check "$(has_issue "$TMP/c15-inactive-target.json" error old-a.md 'is not active')" "yes" \
      "C15e" "a superseded source cannot point at an inactive target"
replace_once "$S/merged.md" 'status: superseded' 'status: active'

# A superseded target remains a valid historical hop when its own exact
# forward/back link continues the chain. This is the scalar legacy format
# already present in governed stores.
write_fact "$S" next next active
replace_once "$S/merged.md" 'status: active' $'status: superseded\nsuperseded_by: next'
replace_once "$S/next.md" $'metadata:\n' $'supersedes: merged\nmetadata:\n'
replace_once "$S/MEMORY.md" '[merged](merged.md)' '[next](next.md)'
run "$S" "$TMP/c15-chain.json"
check "$(sumval "$TMP/c15-chain.json" broken_link)" "0" \
      "C15e2" "a fully linked supersession chain preserves legacy audit history"
replace_once "$S/merged.md" $'status: superseded\nsuperseded_by: next' 'status: active'
replace_once "$S/MEMORY.md" '[next](next.md)' '[merged](merged.md)'

replace_once "$S/merged.md" 'supersedes: ["old-a","old-b"]' 'supersedes: ["merged"]'
run "$S" "$TMP/c15-self.json"
check "$(has_issue "$TMP/c15-self.json" error merged.md 'cannot supersede itself')" "yes" \
      "C15f" "a target cannot supersede itself"

replace_once "$S/merged.md" 'supersedes: ["merged"]' 'supersedes: ["ghost"]'
run "$S" "$TMP/c15-unknown.json"
check "$(has_issue "$TMP/c15-unknown.json" error merged.md 'unknown memory')" "yes" \
      "C15g" "a target cannot supersede an unknown source"

replace_once "$S/merged.md" 'supersedes: ["ghost"]' 'supersedes: [1]'
run "$S" "$TMP/c15-nonstring.json"
check "$(has_issue "$TMP/c15-nonstring.json" error merged.md 'string array')" "yes" \
      "C15h" "a target rejects non-string source entries"

replace_once "$S/merged.md" 'supersedes: [1]' 'supersedes: ["old-a"]'
replace_once "$S/old-a.md" 'status: superseded' 'status: active'
run "$S" "$TMP/c15-active-source.json"
check "$(has_issue "$TMP/c15-active-source.json" error merged.md 'not superseded')" "yes" \
      "C15i" "a target cannot name an active source"

printf 'mem-audit contract: %d/%d PASS\n' "$PASS" "$((PASS + FAIL))"
[ "$FAIL" -eq 0 ]
