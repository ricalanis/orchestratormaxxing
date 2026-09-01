#!/usr/bin/env bash
# Contract: bin/model-eval's raw rows are machine-local run evidence, never
# autocommit cargo.
#
# Why this exists: the tool appended every run's rows to git-tracked
# knowledge/model-eval.jsonl — a path the loop cron autocommits and concurrent
# sessions merge. On 2026-08-27 a completed 120-row reasoning run was silently
# reverted to the previously committed 160 rows by two merge commits from
# another session; the report survived only because it went to stdout
# (lq-d64b06f8). "Append as each lands" survives a crash but not a merge.
# Raw rows therefore live per-run in the gitignored .results/model-eval/,
# keyed by spec_hash; knowledge/ carries only rendered conclusions — the same
# split delegate-ledger already enforces for receipts.
#
# Offline by construction: every case runs against a fixture repo and a HOME
# with no credentials, so no model is ever called. MODEL_EVAL_BIN overrides the
# tool under test (how the pre-fix tool was proven red).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="${MODEL_EVAL_BIN:-$ROOT/bin/model-eval}"

TMP="$(mktemp -d /tmp/model-eval-contract.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# Fixture repo: the tool derives REPO from its own location, so a copy under
# $TMP/repo/bin/ resolves every path against the fixture, never the real tree.
mkdir -p "$TMP/repo/bin" "$TMP/repo/knowledge" "$TMP/home"
cp "$BIN" "$TMP/repo/bin/model-eval"
chmod +x "$TMP/repo/bin/model-eval"
TOOL="$TMP/repo/bin/model-eval"
SPEC="$TMP/repo/knowledge/model-eval-spec.json"
export HOME="$TMP/home"   # no OpenCode auth store -> load_key can never succeed

fail(){ echo "model-eval: $*" >&2; exit 1; }

# ---- C0: preregister still works and yields the spec_hash the run is keyed by
python3 "$TOOL" --spec "$SPEC" preregister \
  --models alpha,beta --classes extract,digest --n 3 --incumbent alpha >/dev/null \
  || fail "C0 preregister failed"
HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["spec_hash"])' "$SPEC")"
[ -n "$HASH" ] || fail "C0 spec has no spec_hash"

rows_for(){ # emit one decided row per model x class x instance for this spec
  python3 - "$1" <<'PY'
import json, sys
h = sys.argv[1]
for m in ("alpha", "beta"):
    for c in ("extract", "digest"):
        for i in range(3):
            print(json.dumps({"spec_hash": h, "model": m, "class": c, "instance": i,
                              "outcome": "pass" if (m == "alpha" or i % 2) else "fail",
                              "detail": "fixture", "tok": 100, "wall": 1.0, "ttft": 0.2}))
PY
}

# ---- C1: report reads the PER-RUN gitignored path by default ----------------
mkdir -p "$TMP/repo/.results/model-eval"
rows_for "$HASH" > "$TMP/repo/.results/model-eval/$HASH.jsonl"
out="$(python3 "$TOOL" --spec "$SPEC" report)" \
  || fail "C1 report did not read .results/model-eval/<spec_hash>.jsonl"
case "$out" in *"$HASH"*) ;; *) fail "C1 report output does not carry the spec hash";; esac

# ---- C2: rows recorded before the split stay reportable (legacy fallback) ---
rm "$TMP/repo/.results/model-eval/$HASH.jsonl"
rows_for "$HASH" > "$TMP/repo/knowledge/model-eval.jsonl"
python3 "$TOOL" --spec "$SPEC" report >/dev/null \
  || fail "C2 legacy knowledge/model-eval.jsonl rows are no longer reportable"

# ---- C2b: an EXPLICIT --out is honored on report, even inside knowledge/ ----
# Reading knowledge/ is allowed; only the write verb refuses it. This is the
# case that catches a refusal guard leaking onto the read path.
python3 "$TOOL" --spec "$SPEC" --out "$TMP/repo/knowledge/model-eval.jsonl" report >/dev/null \
  || fail "C2b explicit --out read from knowledge/ was refused or ignored"

# ---- C3: the WRITE verb refuses a knowledge/ target before credentials ------
# The refusal must be the tool's own message, not load_key stumbling over the
# empty HOME — that distinction is what makes this case falsifiable.
rm "$TMP/repo/knowledge/model-eval.jsonl"
set +e
msg="$(python3 "$TOOL" --spec "$SPEC" --out "$TMP/repo/knowledge/evil.jsonl" run 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail "C3 run accepted a knowledge/ output target"
case "$msg" in *".results/model-eval"*) ;; *) fail "C3 no refusal naming .results/model-eval (got: ${msg:0:120})";; esac
case "$msg" in *"ERROR reading"*) fail "C3 refusal came AFTER the credential read";; *) ;; esac
[ ! -e "$TMP/repo/knowledge/evil.jsonl" ] || fail "C3 refused run still created the file"

# ---- C3b: a LEGITIMATE explicit --out is honored, announced, dir created ----
# Dies at load_key (empty HOME) — after announcing the explicit target and
# creating its parent at resolve time, so a bad target fails before credentials.
set +e
msg="$(python3 "$TOOL" --spec "$SPEC" --out "$TMP/fresh/rows.jsonl" run 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail "C3b run succeeded with no credentials (network reached?)"
case "$msg" in *"$TMP/fresh/rows.jsonl"*) ;; \
  *) fail "C3b explicit --out not announced (refused or replaced by the default?)";; esac
[ -d "$TMP/fresh" ] || fail "C3b parent dir of explicit --out not created at resolve time"

# ---- C3c: a symlink into knowledge/ is refused too --------------------------
# abspath alone string-matches the LINK name, not where it lands, so a
# `ln -s knowledge k; --out k/rows.jsonl` run would write through the link into
# knowledge/ — the original flaw back through a side door (both cross-family
# critics of the 2026-08-30 round found exactly this). The guard must compare
# realpaths.
ln -s knowledge "$TMP/repo/klink"
set +e
msg="$(python3 "$TOOL" --spec "$SPEC" --out "$TMP/repo/klink/evil.jsonl" run 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail "C3c run accepted a symlinked knowledge/ target"
case "$msg" in *".results/model-eval"*) ;; *) fail "C3c no refusal for a symlinked knowledge/ target (got: ${msg:0:120})";; esac
[ ! -e "$TMP/repo/knowledge/evil.jsonl" ] || fail "C3c refused run still wrote through the symlink"

# ---- C4: run's default target is the per-run path, announced pre-credential -
# With an empty HOME the run dies at load_key — AFTER announcing where rows go.
set +e
msg="$(python3 "$TOOL" --spec "$SPEC" run 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail "C4 run succeeded with no credentials (network reached?)"
case "$msg" in *".results/model-eval/$HASH.jsonl"*) ;; \
  *) fail "C4 default out is not the per-run .results path (got: ${msg:0:160})";; esac
[ ! -e "$TMP/repo/knowledge/model-eval.jsonl" ] || fail "C4 run touched knowledge/"

# ---- C5: neither file exists -> clean exit 3, never a traceback -------------
# The results dir is removed first so C5 also pins that report is READ-ONLY:
# a read verb that mkdirs its default path is a write side effect (the one
# mutant of resolve_out's `writing and` guard that survived the first sweep).
rm -rf "$TMP/repo/.results"
set +e
python3 "$TOOL" --spec "$SPEC" report >/dev/null 2>"$TMP/c5.err"
rc=$?
set -e
[ "$rc" -eq 3 ] || fail "C5 rowless report exited $rc, want 3 (stderr: $(head -c120 "$TMP/c5.err"))"
grep -q "Traceback" "$TMP/c5.err" && fail "C5 rowless report crashed with a traceback"
grep -q "no rows" "$TMP/c5.err" || fail "C5 rowless report is silent about why (want 'no rows')"
[ ! -d "$TMP/repo/.results" ] || fail "C5 report CREATED the results dir — read verbs must be read-only"

# ---- C6: the real repo still gitignores the write target --------------------
git -C "$ROOT" check-ignore -q ".results/model-eval/x.jsonl" \
  || fail "C6 .results/model-eval/ is not gitignored in the real repo"

echo "model-eval: C0-C6 pass"
