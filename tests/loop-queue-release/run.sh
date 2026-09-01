#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Contract: the watcher RELEASES machine-enqueued reds that are gone — and only those.
#
# WHY (observed 2026-08-14). bin/loop-tick's watcher was one-directional: it
# enqueued every persistent harness-verify red and never released one. Reds fixed
# by ordinary work stayed open forever — 5 of 51 open items claimed reds that
# harness-verify no longer reported (orchestrator/deploy/cogload-ask.* had been
# deleted, cogload-weekly.* was wired at install.sh:990, tests/capacity/run.sh
# existed). That is not cosmetic: `loop-queue status --gate` counts open items, so
# a queue that can only grow makes the loop's "loop-until-dry, then back off" stop
# condition unreachable, and points every round's "pick the highest-leverage open
# item" at phantom flaws.
#
# The releaser is the dangerous direction — it DESTROYS signal — so this contract
# is mostly negative fixtures. Auto-release is allowed ONLY when all of these hold:
#   ABSENT-TWICE   two consecutive parseable runs, the same 2-sample gate the
#                  enqueue side uses. A transient GREEN misleads exactly as much
#                  as a transient RED.
#   MACHINE-OWNED  source=harness-verify AND the machine-rendered flaw prefix. A
#                  human's flaw is resolved by judgement, never by a watcher.
#   OPEN           a CLAIMED item belongs to a round in flight.
#   SAME HOST      a red can be host-specific (bin/memory-bridge-hermes.sh's
#                  contract fails on macOS BSD sed, passes on Linux). A green run
#                  HERE is not evidence about THERE, and an unstamped legacy item
#                  is nobody's to refute.
#   MEASURED       an unreadable confirming run releases nothing. "I could not
#                  measure it" is not "it is fixed"
#                  (knowledge/signal-vs-artifact-2026-07-19.md).
#
# REAL BOUNDARY (Tier 1c): every case runs the real bin/loop-tick as a real
# subprocess against a real temp git repo, and the queue it mutates is the REAL
# bin/loop-queue writing a real knowledge/loop-queue.jsonl. Only harness-verify
# and mem-audit are stubs — they are what we need to drive to a chosen colour.
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

HOST="contract-host"
OTHER_HOST="some-other-machine"
WHERE="bin/fixture-tool"
MSG="behavioral contract missing: tests/fixture/run.sh"
FLAW="harness-verify red @ $WHERE: $MSG"

# ── fixture ──────────────────────────────────────────────────────────────────
# $1 = name. The harness-verify stub reads $repo/hv-plan (one word per line, one
# per invocation: "green" | "red" | "garbage") and logs every invocation.
mk_repo() {
  local repo="$TMP/$1"
  mkdir -p "$repo/bin" "$repo/knowledge" "$repo/.results"
  git -C "$repo" init -q 2>/dev/null
  git -C "$repo" config user.email t@t 2>/dev/null
  git -C "$repo" config user.name t 2>/dev/null
  cat > "$repo/bin/harness-verify" <<PY
#!/usr/bin/env python3
import json, sys
log = "$repo/hv.log"
with open(log, "a") as fh:
    fh.write("invoked\n")
n = sum(1 for _ in open(log))
plan = [l.strip() for l in open("$repo/hv-plan") if l.strip()]
mode = plan[n - 1] if n <= len(plan) else plan[-1]
if mode == "garbage":
    print("not json at all")
    sys.exit(0)
issues = []
if mode == "red":
    issues = [{"severity": "error", "key": "fixture:red",
               "where": "$WHERE", "message": "$MSG"}]
print(json.dumps({"errors": len(issues), "warnings": 0, "inconclusive": 0,
                  "issues": issues, "contract_results": []}))
PY
  cat > "$repo/bin/mem-audit" <<'SH'
#!/bin/sh
printf '{"files":0,"stale":0,"issues":[]}\n'
SH
  # the REAL queue tool — this contract must cross the real write boundary
  cp -p "$ROOT/bin/loop-queue" "$repo/bin/loop-queue"
  chmod +x "$repo/bin/harness-verify" "$repo/bin/mem-audit" "$repo/bin/loop-queue"
  : > "$repo/hv.log"
  printf 'green\n' > "$repo/hv-plan"
  printf '%s' "$repo"
}

# seed one queue item: $1=repo $2=layer $3=source $4=flaw $5=host ("" = unstamped)
seed() {
  local repo="$1" layer="$2" source="$3" flaw="$4" host="$5"
  ( cd "$repo" && LOOP_QUEUE_HOST="${host:-placeholder}" \
      ./bin/loop-queue add --layer "$layer" --source "$source" "$flaw" >/dev/null )
  if [ -z "$host" ]; then
    # legacy items predate the host stamp — strip it to reproduce that state exactly
    python3 - "$repo/knowledge/loop-queue.jsonl" <<'PY'
import json, sys
p = sys.argv[1]
rows = [json.loads(l) for l in open(p) if l.strip()]
for r in rows:
    r.pop("host", None)
open(p, "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  fi
}

status_of() {  # $1=repo $2=flaw  → status, or "MISSING"
  python3 - "$1/knowledge/loop-queue.jsonl" "$2" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
m = [r for r in rows if r.get("flaw") == sys.argv[2]]
print(m[-1].get("status") if m else "MISSING")
PY
}
note_of() {
  python3 - "$1/knowledge/loop-queue.jsonl" "$2" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
m = [r for r in rows if r.get("flaw") == sys.argv[2]]
print((m[-1].get("note") or "") if m else "")
PY
}
hv_count() { wc -l < "$1/hv.log" | tr -d ' '; }
tick() { ( cd "$1" && rm -f "$1/.results/watch-stamp.json" \
             && env -u CLAUDEMAXXING_HARNESS_CHILD LOOP_QUEUE_HOST="$HOST" \
                  python3 "$ROOT/bin/loop-tick" --gate --quiet >/dev/null 2>&1; echo $? ); }

# ── C1: a this-host machine red that is gone gets released ───────────────────
repo="$(mk_repo c1)"
seed "$repo" V harness-verify "$FLAW" "$HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "resolved" ]; then
  ok C1 "a fixed machine-enqueued red left the queue"
else
  bad C1 "fixed red still $(status_of "$repo" "$FLAW") — the queue can still only grow"
fi
case "$(note_of "$repo" "$FLAW")" in
  *auto-released*2\ consecutive*) ok C1b "the release is auditable (note records the evidence)" ;;
  *) bad C1b "released without recording why: '$(note_of "$repo" "$FLAW")'" ;;
esac

# ── C2: release costs a SECOND sample — one green run is not enough ──────────
if [ "$(hv_count "$repo")" -eq 2 ]; then
  ok C2 "release took two consecutive parseable runs"
else
  bad C2 "expected 2 harness-verify runs for a release, saw $(hv_count "$repo")"
fi

# ── C3: with nothing releasable, the confirming run is NOT paid ──────────────
repo="$(mk_repo c3)"
printf 'green\ngreen\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(hv_count "$repo")" -eq 1 ]; then
  ok C3 "clean queue costs exactly one verifier run (release is pay-per-use)"
else
  bad C3 "clean queue ran harness-verify $(hv_count "$repo")x — release taxes every watch"
fi

# ── C4 (discrimination): a red that is STILL red is never released ───────────
repo="$(mk_repo c4)"
seed "$repo" V harness-verify "$FLAW" "$HOST"
printf 'red\nred\nred\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok C4 "a live red stays open (the gate discriminates, it does not always-release)"
else
  bad C4 "released a red that is still red — signal destroyed"
fi

# ── C5: a red stamped to ANOTHER host is not refuted by a green run here ─────
repo="$(mk_repo c5)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok C5 "another host's red survives a local green (host-specific reds are real)"
else
  bad C5 "a green Linux run erased a red only the Mac can see"
fi

# ── C6: an UNSTAMPED legacy item is not this host's to refute ────────────────
repo="$(mk_repo c6)"
seed "$repo" V harness-verify "$FLAW" ""
printf 'green\ngreen\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok C6 "an unstamped legacy red stays open for a human"
else
  bad C6 "auto-released an item whose observing host is unknown"
fi

# ── C7: a HUMAN-sourced flaw is never touched, whatever it says ──────────────
repo="$(mk_repo c7)"
seed "$repo" V manual "$FLAW" "$HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok C7 "a manually-filed flaw is resolved by judgement, never by the watcher"
else
  bad C7 "the watcher resolved a human's flaw"
fi

# ── C8: a CLAIMED item belongs to a round in flight ──────────────────────────
repo="$(mk_repo c8)"
seed "$repo" V harness-verify "$FLAW" "$HOST"
iid="$(python3 - "$repo/knowledge/loop-queue.jsonl" <<'PY'
import json, sys
print([json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1]["id"])
PY
)"
( cd "$repo" && ./bin/loop-queue claim "$iid" >/dev/null 2>&1 )
printf 'green\ngreen\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "claimed" ]; then
  ok C8 "a claimed item is left to its round (its ratchet stays resolvable)"
else
  bad C8 "released an item a round holds — that round can no longer resolve it"
fi

# ── C9: an unreadable confirming run releases NOTHING ────────────────────────
# "could not measure" must never be spent as "fixed" — the failure mode that
# turned an unreadable pane into a dead executor (signal-vs-artifact, 2026-07-19).
repo="$(mk_repo c9)"
seed "$repo" V harness-verify "$FLAW" "$HOST"
printf 'green\ngarbage\ngarbage\n' > "$repo/hv-plan"
tick "$repo" >/dev/null
if [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok C9 "an unreadable second sample releases nothing"
else
  bad C9 "spent an unreadable observation as evidence the red was fixed"
fi

# ── C10: loop-queue stamps the observing host (what C5/C6 ride on) ───────────
repo="$(mk_repo c10)"
seed "$repo" V harness-verify "$FLAW" "$HOST"
stamped="$(python3 - "$repo/knowledge/loop-queue.jsonl" <<'PY'
import json, sys
print([json.loads(l) for l in open(sys.argv[1]) if l.strip()][-1].get("host", ""))
PY
)"
if [ "$stamped" = "$HOST" ]; then
  ok C10 "a new flaw records which machine observed it"
else
  bad C10 "host provenance missing ('$stamped') — host-scoped release cannot work"
fi

# ═════════════════════════════════════════════════════════════════════════════
# RECONCILE — the human-gated complement of auto-release (lq-2f448edc).
#
# Auto-release is deliberately host-scoped (C5/C6), so a red stamped to a
# decommissioned/renamed host — or an unstamped legacy item — can NEVER leave the
# queue automatically. `loop-tick --reconcile` closes that class WITHOUT widening
# the automatic path: it PROPOSES foreign/unstamped machine reds absent from two
# consecutive local runs, and resolves one only on an explicit human
# `--confirm <id>`. The gate is structural: under CLAUDEMAXXING_HARNESS_CHILD
# (any harness-spawned agent) the verb refuses, so an unattended round cannot
# confirm on a human's behalf.
# ═════════════════════════════════════════════════════════════════════════════

item_id_of() {  # $1=repo $2=flaw → id
  python3 - "$1/knowledge/loop-queue.jsonl" "$2" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
m = [r for r in rows if r.get("flaw") == sys.argv[2]]
print(m[-1]["id"] if m else "")
PY
}
# run reconcile as the real subprocess; stdout+stderr to $repo/recon.out, echoes rc
recon() { local repo="$1"; shift
  ( cd "$repo" && env -u CLAUDEMAXXING_HARNESS_CHILD LOOP_QUEUE_HOST="$HOST" \
      python3 "$ROOT/bin/loop-tick" --reconcile "$@" >"$repo/recon.out" 2>&1; echo $? ); }

# ── R1: propose lists a foreign red absent locally; resolves NOTHING ─────────
repo="$(mk_repo r1)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
seed "$repo" V manual "human note about a harness-verify red @ thing" "$OTHER_HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
rc="$(recon "$repo")"
iid="$(item_id_of "$repo" "$FLAW")"
if [ "$rc" = "0" ] && grep -q "$iid" "$repo/recon.out" \
   && [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok R1 "propose mode surfaces the foreign red and mutates nothing"
else
  bad R1 "rc=$rc, status=$(status_of "$repo" "$FLAW") — propose must list, never resolve"
fi
if grep -q "human note" "$repo/recon.out"; then
  bad R1b "a manually-filed flaw was proposed for reconcile"
else
  ok R1b "human-sourced flaws are never reconcile candidates"
fi

# ── R2: explicit --confirm resolves the foreign red, auditable, 2 samples ────
repo="$(mk_repo r2)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
iid="$(item_id_of "$repo" "$FLAW")"
rc="$(recon "$repo" --confirm "$iid")"
if [ "$rc" = "0" ] && [ "$(status_of "$repo" "$FLAW")" = "resolved" ]; then
  ok R2 "human-confirmed reconcile resolves the stranded foreign red"
else
  bad R2 "rc=$rc, status=$(status_of "$repo" "$FLAW") — confirm did not resolve"
fi
case "$(note_of "$repo" "$FLAW")" in
  *human*confirm*"$OTHER_HOST"*"on $HOST"*) ok R2b "the reconcile is auditable (note records human gate, origin host, observing host)" ;;
  *) bad R2b "reconcile note lacks evidence: '$(note_of "$repo" "$FLAW")'" ;;
esac
if grep -q "$OTHER_HOST" "$repo/recon.out"; then
  ok R2d "the confirm output names the origin host being reconciled"
else
  bad R2d "confirm output hides which host's red was resolved"
fi
if [ "$(hv_count "$repo")" -eq 2 ]; then
  ok R2c "reconcile measured absence on two consecutive runs"
else
  bad R2c "expected 2 harness-verify samples, saw $(hv_count "$repo")"
fi

# ── R3 (discrimination): a foreign red that is red HERE too is refused ───────
repo="$(mk_repo r3)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
printf 'red\nred\n' > "$repo/hv-plan"
iid="$(item_id_of "$repo" "$FLAW")"
rc="$(recon "$repo" --confirm "$iid")"
if [ "$rc" != "0" ] && [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok R3 "a red also live locally cannot be reconciled away"
else
  bad R3 "rc=$rc, status=$(status_of "$repo" "$FLAW") — reconciled a still-live red"
fi

# ── R4: an UNSTAMPED legacy item is a candidate (that is who this is for) ────
repo="$(mk_repo r4)"
seed "$repo" V harness-verify "$FLAW" ""
printf 'green\ngreen\n' > "$repo/hv-plan"
iid="$(item_id_of "$repo" "$FLAW")"
rc="$(recon "$repo" --confirm "$iid")"
if [ "$rc" = "0" ] && [ "$(status_of "$repo" "$FLAW")" = "resolved" ]; then
  ok R4 "an unstamped legacy red is human-reconcilable"
else
  bad R4 "rc=$rc, status=$(status_of "$repo" "$FLAW") — unstamped item not reconcilable"
fi

# ── R5: a THIS-host red is not reconcile's to touch (auto-release owns it) ───
repo="$(mk_repo r5)"
seed "$repo" V harness-verify "$FLAW" "$HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
iid="$(item_id_of "$repo" "$FLAW")"
rc="$(recon "$repo" --confirm "$iid")"
if [ "$rc" != "0" ] && [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok R5 "this-host reds stay on the auto-release path, not the human bypass"
else
  bad R5 "rc=$rc, status=$(status_of "$repo" "$FLAW") — reconcile bypassed the 2-tick auto gate"
fi

# ── R6: an unmeasurable local observer refuses ('could not measure' ≠ absent) ─
repo="$(mk_repo r6)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
printf 'garbage\ngarbage\ngarbage\n' > "$repo/hv-plan"
iid="$(item_id_of "$repo" "$FLAW")"
rc="$(recon "$repo" --confirm "$iid")"
if [ "$rc" != "0" ] && [ "$(status_of "$repo" "$FLAW")" = "open" ]; then
  ok R6 "an unreadable observer reconciles nothing"
else
  bad R6 "rc=$rc, status=$(status_of "$repo" "$FLAW") — spent an unreadable run as absence"
fi

# ── R8: an UNREADABLE queue proposes nothing (unreadable ≠ empty) ────────────
repo="$(mk_repo r8)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
cat > "$repo/bin/loop-queue" <<'SH'
#!/bin/sh
echo "this is not json"
SH
chmod +x "$repo/bin/loop-queue"
printf 'green\ngreen\n' > "$repo/hv-plan"
rc="$(recon "$repo")"
if [ "$rc" != "0" ] && ! grep -qi "no foreign" "$repo/recon.out"; then
  ok R8 "an unreadable queue refuses instead of reading as clean"
else
  bad R8 "rc=$rc — an unreadable queue was reported as empty/clean"
fi

# ── R9: an EMPTY candidate set in propose mode is a clean 0, not an error ────
repo="$(mk_repo r9)"
printf 'green\n' > "$repo/hv-plan"
rc="$(recon "$repo")"
if [ "$rc" = "0" ] && grep -qi "no foreign" "$repo/recon.out"; then
  ok R9 "a clean queue proposes nothing and exits 0"
else
  bad R9 "rc=$rc — a clean queue must not read as an operator error"
fi

# ── R10 (TOCTOU, critic finding): a concurrent resolve during the measurement
# window is refused — never overwritten. The confirm path spends two verifier
# runs measuring; if another actor resolves the item meanwhile, resolving again
# would clobber that actor's audit note.
repo="$(mk_repo r10)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
iid="$(item_id_of "$repo" "$FLAW")"
cat > "$repo/bin/harness-verify" <<PY
#!/usr/bin/env python3
import json, subprocess
log = "$repo/hv.log"
with open(log, "a") as fh:
    fh.write("invoked\n")
if sum(1 for _ in open(log)) == 1:
    # simulate a concurrent round resolving the item mid-measurement
    subprocess.run(["$repo/bin/loop-queue", "resolve", "$iid",
                    "--note", "resolved by concurrent round"],
                   cwd="$repo", capture_output=True)
print(json.dumps({"errors": 0, "warnings": 0, "inconclusive": 0,
                  "issues": [], "contract_results": []}))
PY
chmod +x "$repo/bin/harness-verify"
rc="$(recon "$repo" --confirm "$iid")"
if [ "$rc" != "0" ] && [ "$(note_of "$repo" "$FLAW")" = "resolved by concurrent round" ]; then
  ok R10 "a mid-measurement concurrent resolve is refused, its audit note intact"
else
  bad R10 "rc=$rc note='$(note_of "$repo" "$FLAW")' — reconcile clobbered a concurrent actor's resolution"
fi

# ── R7 (structural human gate): a harness child cannot confirm ───────────────
repo="$(mk_repo r7)"
seed "$repo" V harness-verify "$FLAW" "$OTHER_HOST"
printf 'green\ngreen\n' > "$repo/hv-plan"
iid="$(item_id_of "$repo" "$FLAW")"
rc="$( ( cd "$repo" && CLAUDEMAXXING_HARNESS_CHILD=1 LOOP_QUEUE_HOST="$HOST" \
      python3 "$ROOT/bin/loop-tick" --reconcile --confirm "$iid" >"$repo/recon.out" 2>&1; echo $? ) )"
if [ "$rc" != "0" ] && [ "$(status_of "$repo" "$FLAW")" = "open" ] \
   && grep -q "human-gated" "$repo/recon.out"; then
  ok R7 "a harness-spawned agent is structurally unable to confirm (and told why)"
else
  bad R7 "rc=$rc, status=$(status_of "$repo" "$FLAW") — an unattended round confirmed as a human, or refused silently"
fi

printf 'loop-queue-release contract: %d/%d PASS\n' "$pass" "$((pass+fail))"
[ "$fail" -eq 0 ] || exit 1
