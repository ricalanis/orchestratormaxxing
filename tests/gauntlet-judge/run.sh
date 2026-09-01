#!/usr/bin/env bash
# Offline behavioral contract for bin/gauntlet-judge. No network: the judge is
# a stub (GAUNTLET_OLL) that answers in blinded Artifact-1/2 terms, so every
# green case also exercises the unblinding bookkeeping. Proven red against a
# seeded accept-at-2/4 pass-rule bug (Tier 1c).
set -u
set -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${GAUNTLET_TOOL:-$HERE/../../bin/gauntlet-judge}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export GAUNTLET_OLL="$HERE/judge-stub.py"
export GAUNTLET_TEST_MARKER="$WORK/marker"

cat > "$WORK/rubric.json" <<'EOF'
[{"id":"lb1","text":"names every flag exactly","load_bearing":true},
 {"id":"c2","text":"has a runnable example","load_bearing":false},
 {"id":"c3","text":"states the exit codes","load_bearing":false}]
EOF
printf 'SENTINEL_G generated summary\nflags listed here\n' > "$WORK/g.txt"
printf 'SENTINEL_R reference docstring\nolder wording\n'   > "$WORK/r.txt"

FAILS=0
check() { # check <name> <expected_exit> <actual_exit>
  if [ "$3" -eq "$2" ]; then echo "ok   $1"; else echo "FAIL $1: exit $3 (want $2)"; FAILS=$((FAILS+1)); fi
}
run() { # run <scenario> -> sets OUT/RC
  local scenario="$1"; shift
  OUT="$(GAUNTLET_TEST_SCENARIO="$scenario" "$TOOL" --rubric "$WORK/rubric.json" \
        --builder glm-5.2 --reference-author human --json "$@" 2>"$WORK/err")"
  RC=$?
}
json_has() { # json_has <name> <python-expr over d>
  if python3 -c "import json,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if ($2) else 1)" <<<"$OUT"
  then echo "ok   $1"; else echo "FAIL $1"; FAILS=$((FAILS+1)); fi
}

# 1. clean win -> promotion (exit 0), order-stable, load-bearing ok
run g_better --artifact "$WORK/g.txt" --reference "$WORK/r.txt"
check "g_better exit0" 0 "$RC"
json_has "g_better verdict" "d['verdict']=='better' and d['order_stable'] and d['load_bearing_ok']"
json_has "g_better refutations carried" "len(d['refutations_of_artifact'])==4"

# 2. length flag on an oversized win (reported, not verdict-flipping)
python3 -c "open('$WORK/g-long.txt','w').write('SENTINEL_G '+'padding '*200)"
run g_better --artifact "$WORK/g-long.txt" --reference "$WORK/r.txt"
check "length_flag exit0" 0 "$RC"
json_has "length_flag present" "'length_flag' in d['flags']"

# 3. position bias -> order flip -> fail closed
run position_bias --artifact "$WORK/g.txt" --reference "$WORK/r.txt"
check "position_bias exit1" 1 "$RC"
json_has "position_bias unstable" "d['order_stable']==False"

# 4. G loses a load-bearing criterion -> fail even while winning pairwise
run load_bearing_loss --artifact "$WORK/g.txt" --reference "$WORK/r.txt"
check "load_bearing exit1" 1 "$RC"
json_has "load_bearing false" "d['load_bearing_ok']==False"

# 5. 2-2 judge split, order-stable -> fail closed
run split --artifact "$WORK/g.txt" --reference "$WORK/r.txt"
check "split exit1" 1 "$RC"
json_has "split stable but 2-2" "d['order_stable'] and d['pairwise']['g_wins']==2"

# 6. judges can't emit valid JSON after retry -> gate unavailable, not a verdict
run invalid --artifact "$WORK/g.txt" --reference "$WORK/r.txt"
check "invalid exit3" 3 "$RC"

# 7-9. policy refusals are PRE-NETWORK: marker file must stay absent
rm -f "$GAUNTLET_TEST_MARKER"
GAUNTLET_TEST_SCENARIO=g_better "$TOOL" --rubric "$WORK/rubric.json" \
  --builder glm-5.2 --reference-author human --judges glm-5.2,qwen3.5:397b \
  --artifact "$WORK/g.txt" --reference "$WORK/r.txt" >/dev/null 2>&1
check "same-family judge exit2" 2 $?
GAUNTLET_TEST_SCENARIO=g_better "$TOOL" --rubric "$WORK/rubric.json" \
  --builder glm-5.2 --reference-author qwen3.5:397b \
  --judges qwen3.5:397b,mistral-large-3:675b \
  --artifact "$WORK/g.txt" --reference "$WORK/r.txt" >/dev/null 2>&1
check "reference-author collision exit2" 2 $?
OLL_MAX_ESTIMATED_INPUT_TOKENS=10 GAUNTLET_TEST_SCENARIO=g_better "$TOOL" \
  --rubric "$WORK/rubric.json" --builder glm-5.2 --reference-author human \
  --artifact "$WORK/g.txt" --reference "$WORK/r.txt" >/dev/null 2>&1
check "oversize exit2" 2 $?
if [ -e "$GAUNTLET_TEST_MARKER" ]; then echo "FAIL refusals reached a judge"; FAILS=$((FAILS+1)); else echo "ok   refusals pre-network"; fi

# 10. unknown reference provenance runs (fail-safe direction) with a flag
run g_better --artifact "$WORK/g.txt" --reference "$WORK/r.txt" --reference-author unknown
OUT="$(GAUNTLET_TEST_SCENARIO=g_better "$TOOL" --rubric "$WORK/rubric.json" \
      --builder glm-5.2 --reference-author unknown --json \
      --artifact "$WORK/g.txt" --reference "$WORK/r.txt" 2>/dev/null)"
check "unknown provenance exit0" 0 $?
json_has "unknown provenance flagged" "'reference_provenance_unknown' in d['flags']"

# 11. canary: honest judges -> R-vs-R ties, null loses 4/4 -> pass
OUT="$(GAUNTLET_TEST_SCENARIO=canary_ok "$TOOL" --rubric "$WORK/rubric.json" \
      --builder glm-5.2 --reference-author human --json --canary \
      --reference "$WORK/r.txt" 2>/dev/null)"
check "canary pass exit0" 0 $?
json_has "canary pass json" "d['canary']=='pass'"

# 12. canary: degenerate judges let the null artifact win -> fail
OUT="$(GAUNTLET_TEST_SCENARIO=canary_null_wins "$TOOL" --rubric "$WORK/rubric.json" \
      --builder glm-5.2 --reference-author human --json --canary \
      --reference "$WORK/r.txt" 2>/dev/null)"
check "canary null-wins exit1" 1 $?
json_has "canary fail json" "d['canary']=='fail'"

# 13. rubric shape gates: too few criteria / no load-bearing -> refuse
echo '[{"id":"a","text":"x","load_bearing":true}]' > "$WORK/rubric-short.json"
"$TOOL" --rubric "$WORK/rubric-short.json" --builder glm-5.2 \
  --reference-author human --artifact "$WORK/g.txt" --reference "$WORK/r.txt" >/dev/null 2>&1
check "short rubric exit2" 2 $?
python3 - "$WORK/rubric-nolb.json" <<'EOF'
import json, sys
json.dump([{"id": f"c{i}", "text": "x", "load_bearing": False} for i in range(3)], open(sys.argv[1], "w"))
EOF
"$TOOL" --rubric "$WORK/rubric-nolb.json" --builder glm-5.2 \
  --reference-author human --artifact "$WORK/g.txt" --reference "$WORK/r.txt" >/dev/null 2>&1
check "no load-bearing rubric exit2" 2 $?

if [ "$FAILS" -gt 0 ]; then echo "gauntlet-judge contract: $FAILS failure(s)"; exit 1; fi
echo "gauntlet-judge contract: all green"
