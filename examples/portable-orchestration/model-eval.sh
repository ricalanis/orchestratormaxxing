#!/usr/bin/env bash
# model-eval.sh — self-contained offline model-eval example using bin/model-eval.
#
# Preregisters a tiny evaluation spec, runs the golden gate (contracts only, no
# providers), reports from hand-written synthetic rows, then cleans up. Does NOT
# call any provider or network endpoint. Model labels are harmless synthetic
# placeholders; no real ranking is implied.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODELEVAL="$ROOT/bin/model-eval"

TMP="$(mktemp -d -t portable-model-eval-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# Disposable HOME and cleared inherited state paths.
export HOME="$TMP/home"
mkdir -p "$HOME"
unset XDG_DATA_HOME XDG_CONFIG_HOME

SPEC="$TMP/spec.json"
ROWS="$TMP/rows.jsonl"

echo "=== preregistering a tiny offline spec"
# Neutral synthetic model labels accepted by the CLI; no real provider meaning.
"$MODELEVAL" --spec "$SPEC" preregister \
  --models "neutral-a,neutral-b" \
  --classes "reason,extract" \
  --n 2 \
  --seed "portable-example" \
  --incumbent "neutral-a"

echo
HASH="$(python3 - "$SPEC" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["spec_hash"])
PY
)"
echo "spec hash: $HASH"

echo
"$MODELEVAL" --spec "$SPEC" golden

echo
# Add deterministic synthetic rows for both arms so the offline report works.
python3 - "$SPEC" "$ROWS" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1]))
out = open(sys.argv[2], "w")
for kind in spec["classes"]:
    for i in range(spec["n_per_cell"]):
        for model in spec["models"]:
            out.write(json.dumps({
                "spec_hash": spec["spec_hash"], "model": model, "class": kind,
                "instance": i, "effort": spec["effort"],
                "outcome": "pass" if (model == spec["incumbent"] or i % 2 == 0) else "fail",
                "detail": "synthetic", "tok": 120, "wall": 1.2,
                "ttft": 0.3, "decode_tps": 100.0,
            }) + "\n")
PY

echo "=== offline report from synthetic rows"
"$MODELEVAL" --spec "$SPEC" --out "$ROWS" report

# The trap cleans $TMP, including the spec/rows.
