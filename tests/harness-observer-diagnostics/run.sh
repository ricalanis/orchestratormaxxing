#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Contract: an UNREADABLE OBSERVER queues an ATTRIBUTABLE flaw.
#
# WHY (measured 2026-08-17). loop-tick's unparseable-twice path enqueued
# "harness observer unavailable: harness-verify emitted no parseable JSON on 2
# attempts" and recorded {"attempts": 2} — no exit code, no stdout, no stderr.
# The Mac's harness-observations.jsonl held ~20 such rows and the flaw synced
# here as lq-0fafcc65 with nothing to diagnose: .results/ is gitignored, so the
# QUEUE is the only artifact that crosses machines. A round draining it could
# not tell a crashed verifier from a hung one from one printing non-JSON — the
# signal-vs-artifact trap with the evidence deliberately thrown away.
#
# Five properties:
#   ATTRIBUTABLE — exit code, stdout size + head, and the stderr TAIL (a
#                  traceback's cause is its last line, so head-truncation
#                  attributes nothing). The three failure shapes must be
#                  DISTINGUISHABLE, which is the whole point.
#   IDENTITY-SAFE— diag is NOT in the content hash. The id stays the historical
#                  lq-0fafcc65, and two adds with different diagnostics stay ONE
#                  idempotent item — else every run mints a new flaw, which is
#                  the 611-fabrications failure mode with extra steps.
#   BOUNDED      — a 5KB stderr does not become a 5KB row in a committed,
#                  git-synced JSONL, and a ZERO budget suppresses rather than
#                  (via text[-0:]) returning everything.
#   REDACTED     — known credential formats never reach shared state. The
#                  credentials sit inside the RETAINED tail on purpose: in the
#                  discarded head, C4 would pass because truncation ate them.
#   CLEAN        — one line, no ANSI, no control bytes in the committed row.
#
# REAL BOUNDARY (Tier 1c): runs the real bin/loop-tick as a real subprocess in a
# real temp git repo, writing through the real bin/loop-queue to a real JSONL.
# Only harness-verify/mem-audit are stubs — we must drive the observer's colour.
#
# Proven red 5/8 against the pre-diag tools; C4 proven red against each dropped
# credential pattern; C8/C9/C10 added from bin/mut survivors (score 0.42 → the
# ANSI strip, the control strip, the zero-limit clamp and the `r.stdout or ""`
# read were all deletable with the suite still green).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok  %s  %s\n' "$1" "$2"; }
bad() { fail=$((fail+1)); printf '  FAIL %s  %s\n' "$1" "$2" >&2; }

[ -f "$ROOT/bin/loop-tick" ]  || { echo "missing bin/loop-tick" >&2; exit 1; }
[ -f "$ROOT/bin/loop-queue" ] || { echo "missing bin/loop-queue" >&2; exit 1; }

SECRET="sk-abcdef0123456789DEADBEEF"
JWT="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"  # gitleaks:allow
MARKER="OBSERVER_CAUSE_MARKER_9F2A"
OUTMARK="OBSERVER_STDOUT_MARKER_3B7D"

# $1 = repo name. Builds a repo whose observer is unreadable in the nastiest
# realistic way: non-JSON on stdout, a 5KB stderr, ANSI + control bytes, leaked
# credentials in the retained tail, and a distinctive exit code.
mk_repo() {
  local repo="$TMP/$1"
  mkdir -p "$repo/bin" "$repo/knowledge" "$repo/.results"
  git -C "$repo" init -q 2>/dev/null
  git -C "$repo" config user.email t@t 2>/dev/null
  git -C "$repo" config user.name t 2>/dev/null
  cat > "$repo/bin/harness-verify" <<PY
#!/usr/bin/env python3
import sys
sys.stdout.write("not json at all: $OUTMARK\n")
sys.stderr.write("Traceback (most recent call last):\n")
sys.stderr.write("PADDING " * 700 + "\n")
sys.stderr.write("noise line with a leaked key $SECRET in it\n")
sys.stderr.write("and a session jwt $JWT here\n")
sys.stderr.write("\x1b[31mRuntimeError\x1b[0m:\x00 $MARKER\n")
sys.exit(3)
PY
  cat > "$repo/bin/mem-audit" <<'SH'
#!/bin/sh
printf '{"files":0,"stale":0}\n'
SH
  cp "$ROOT/bin/loop-queue" "$repo/bin/loop-queue"
  chmod +x "$repo/bin/"*
  printf '%s' "$repo"
}

# $1 = repo, rest = extra env assignments for the tick
tick() {
  local repo="$1"; shift
  ( cd "$repo" && env -u ORCHESTRATORMAXXING_HARNESS_CHILD \
      HARNESS_VERIFY_TIMEOUT_SECONDS=30 LOOP_QUEUE_HOST=testhost "$@" \
      python3 "$ROOT/bin/loop-tick" --quiet >/dev/null 2>&1 )
}

REPO="$(mk_repo main)"
QUEUE="$REPO/knowledge/loop-queue.jsonl"
tick "$REPO"

if [ ! -s "$QUEUE" ]; then
  bad C0 "the unreadable observer queued nothing at all — fixture broken"
  printf 'harness-observer-diagnostics: %d/%d PASS\n' "$pass" "$((pass+fail))"
  exit 1
fi
ok C0 "unreadable observer still queues an observer-health flaw"

probe() {  # $1 = queue path → id len has_secret has_marker has_rc has_stdout clean
  python3 - "$1" "$SECRET" "$MARKER" "$JWT" "$OUTMARK" <<'PY'
import json, re, sys
path, secret, marker, jwt, outmark = sys.argv[1:6]
items = [json.loads(l) for l in open(path) if l.strip()]
obs = [i for i in items if i.get("layer") == "O"]
it = obs[-1] if obs else {}
diag = it.get("diag", "")
blob = json.dumps(it)
has_stdout = int(bool(re.search(r"stdout=[1-9]\d*B", diag)) and outmark in diag)
clean = int("\x1b" not in diag and "\x00" not in diag
            and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", diag)
            and "\n" not in diag and "  " not in diag
            # ESC alone is also a control byte, so stripping controls hides a
            # missing ANSI strip; the leftover "[31m" residue is what proves it ran.
            and not re.search(r"\[\d{1,2}(;\d{1,2})*m", diag))
print(it.get("id", "NONE"), len(diag),
      int(secret in blob or jwt in blob), int(marker in diag),
      int("rc=3" in diag), has_stdout, clean)
PY
}

read -r ITEM_ID ITEM_DIAG_LEN HAS_SECRET HAS_MARKER HAS_RC HAS_STDOUT CLEAN <<EOF
$(probe "$QUEUE")
EOF

[ "$ITEM_DIAG_LEN" -gt 0 ] \
  && ok   C1 "the queued observer flaw carries a diagnostic payload ($ITEM_DIAG_LEN chars)" \
  || bad  C1 "the queued observer flaw has NO diag — unattributable, the flaw this fixes"

[ "$HAS_RC" = "1" ] \
  && ok   C2 "diag names the observer's exit code (rc=3)" \
  || bad  C2 "diag does not carry the exit code — cannot tell a crash from a hang"

[ "$HAS_MARKER" = "1" ] \
  && ok   C3 "diag keeps the stderr TAIL (the cause line), not just the head" \
  || bad  C3 "diag lost the cause line — head-truncation attributes nothing"

# C4/C5 guard the payload, so they require a payload: "no diag" is an UNTESTED
# guard, not a clean one. Without this they pass vacuously against the pre-fix
# tool and the suite reads green while proving nothing.
[ "$ITEM_DIAG_LEN" -gt 0 ] && [ "$HAS_SECRET" = "0" ] \
  && ok   C4 "leaked credentials in stderr (api key + JWT) are redacted before shared state" \
  || bad  C4 "a credential reached the committed, git-synced queue (or no diag to check)"

[ "$ITEM_DIAG_LEN" -gt 0 ] && [ "$ITEM_DIAG_LEN" -le 1200 ] \
  && ok   C5 "diag is bounded despite a 5KB stderr ($ITEM_DIAG_LEN ≤ 1200)" \
  || bad  C5 "diag is not a bounded payload ($ITEM_DIAG_LEN chars) in a committed JSONL"

# Identity: the content hash is layer+flaw ONLY. This is the historical id of the
# real flaw, so the assertion pins cross-machine recurrence detection, not a
# freshly-computed self-consistent value.
[ "$ITEM_ID" = "lq-0fafcc65" ] \
  && ok   C6 "diag did not change the item's identity (still lq-0fafcc65)" \
  || bad  C6 "identity drifted to $ITEM_ID — recurrence detection broken by diagnostics"

# Idempotency under DIFFERING diagnostics: the same flaw, twice, with different
# payloads, must stay ONE open item. Anything else mints a flaw per run.
BEFORE=$(wc -l < "$QUEUE")
( cd "$REPO" && LOOP_QUEUE_HOST=testhost python3 bin/loop-queue add --layer O \
    --source harness-verify --diag "rc=99 totally different payload" \
    "harness observer unavailable: harness-verify emitted no parseable JSON on 2 attempts" \
    >/dev/null 2>&1 )
AFTER=$(wc -l < "$QUEUE")
[ "$BEFORE" = "$AFTER" ] \
  && ok   C7 "a second add with a different diag is idempotent (no duplicate item)" \
  || bad  C7 "differing diagnostics split one flaw into $AFTER items"

# The three failure shapes must be DISTINGUISHABLE. "Printed non-JSON" is not
# "printed nothing": without the stdout size + head, a truncated pipe and a
# silent crash read identically.
[ "$HAS_STDOUT" = "1" ] \
  && ok   C8 "diag reports the observer's stdout size and head (non-JSON ≠ silence)" \
  || bad  C8 "diag hides stdout — a chatty non-JSON observer looks like a silent crash"

[ "$CLEAN" = "1" ] \
  && ok   C9 "diag is one clean line: no ANSI, no control bytes, no run-on whitespace" \
  || bad  C9 "raw ANSI/control bytes reached a committed JSONL row"

# Zero budget must SUPPRESS. text[-0:] is the whole string, so the naive slice
# returns everything a zero limit asked it to drop.
REPO0="$(mk_repo zerolimit)"
tick "$REPO0" HARNESS_DIAG_LIMIT=0
read -r _ Z_LEN _ Z_MARKER _ _ _ <<EOF
$(probe "$REPO0/knowledge/loop-queue.jsonl")
EOF
[ "$Z_LEN" -gt 0 ] && [ "$Z_MARKER" = "0" ] \
  && ok   C10 "a zero stderr budget suppresses the tail instead of emitting all of it" \
  || bad  C10 "zero budget did not bound the tail (len=$Z_LEN, cause_present=$Z_MARKER)"

printf 'harness-observer-diagnostics: %d/%d PASS\n' "$pass" "$((pass+fail))"
[ "$fail" -eq 0 ] || exit 1
