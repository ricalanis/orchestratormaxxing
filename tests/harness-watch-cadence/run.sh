#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Contract: the SessionStart watcher is CHEAP, SINGLE-FLIGHT and HONEST.
#
# WHY (incident 2026-08-12, second half). Cutting the recursion stopped the fork
# bomb but left the underlying Lifecycle flaw: every session start still ran the
# FULL contract suite synchronously in the user's critical path. Measured 71s
# before this round, ~100s after this round's own contract was added — against a
# hardcoded 75s budget in loop-tick. A verifier slower than its own budget is a
# trap that re-arms itself: every tick times out, the persistence re-check runs
# it a SECOND time, and each timeout is misreported as "observer unavailable"
# (an unreadable observer) when the truth is "I could not measure it in time".
#
# Three properties, none of which the old design had:
#   FRESH        — a watch that ran recently is not re-run for every new session.
#                  The queue is the durable state; the watcher refreshes it on a
#                  cadence, it is not a per-session tax.
#   SINGLE-FLIGHT— N sessions starting at once produce at most ONE watch, not N
#                  concurrent 100s verifier runs (that was the CPU storm).
#   HONEST       — skipping is visible, never silent; a timeout is reported as a
#                  timeout and never fabricates a harness-regression flaw.
#
# "Silence is not blindness": a missing or corrupt stamp must WATCH, never skip.
# A watcher that fails to zero would read as "all clear", the worst failure this
# design can have.
#
# REAL BOUNDARY (Tier 1c): every check runs the real bin/loop-tick as a real
# subprocess against a real temp git repo; only the tools it shells out to are
# recording stubs on its own resolution path.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok  %s  %s\n' "$1" "$2"; }
bad() { fail=$((fail+1)); printf '  FAIL %s  %s\n' "$1" "$2" >&2; }

[ -f "$ROOT/bin/loop-tick" ] || { echo "missing bin/loop-tick" >&2; exit 1; }

# ── fixture ──────────────────────────────────────────────────────────────────
# $1 = fixture name, $2 = actionable count reported by the queue stub,
# $3 = seconds the harness-verify stub sleeps before answering.
mk_repo() {
  local repo="$TMP/$1" actionable="$2" sleep_s="$3"
  mkdir -p "$repo/bin" "$repo/knowledge" "$repo/.results"
  git -C "$repo" init -q 2>/dev/null
  git -C "$repo" config user.email t@t 2>/dev/null
  git -C "$repo" config user.name t 2>/dev/null
  # Python: loop-tick invokes the verifier as `sys.executable <path>`.
  cat > "$repo/bin/harness-verify" <<PY
#!/usr/bin/env python3
import json, os, time
with open("$repo/hv.log", "a") as fh:
    fh.write("invoked marker=%s\n" % os.environ.get("CLAUDEMAXXING_HARNESS_CHILD", "unset"))
time.sleep($sleep_s)
print(json.dumps({"errors": 0, "warnings": 0, "inconclusive": 0,
                  "issues": [], "contract_results": []}))
PY
  cat > "$repo/bin/mem-audit" <<SH
#!/bin/sh
printf '{"files":0,"stale":0}\n'
SH
  cat > "$repo/bin/loop-queue" <<SH
#!/bin/sh
case "\$1" in
  status) printf '{"actionable": $actionable, "open": $actionable}\n' ;;
  add) printf '%s\n' "\$*" >> "$repo/enqueued.log" ;;
  *) : ;;
esac
exit 0
SH
  chmod +x "$repo/bin/"*
  : > "$repo/hv.log"; : > "$repo/enqueued.log"
  printf '%s' "$repo"
}
hv_count() { wc -l < "$1/hv.log" | tr -d ' '; }
tick() { ( cd "$1" && shift && env -u CLAUDEMAXXING_HARNESS_CHILD "$@" \
             python3 "$ROOT/bin/loop-tick" --gate --quiet >/dev/null 2>&1; echo $? ); }

# ── D1: a cold tick watches ──────────────────────────────────────────────────
repo="$(mk_repo d1 0 0)"
tick "$repo" >/dev/null
if [ "$(hv_count "$repo")" -ge 1 ]; then
  ok D1 "cold tick ran the watcher"
else
  bad D1 "cold tick did not watch at all — the loop is blind"
fi
if grep -q 'marker=1' "$repo/hv.log"; then
  ok D1b "cold tick marked the verifier subtree"
else
  bad D1b "cold tick left its verifier subtree unmarked"
fi

# ── D2: an immediately-following tick does NOT re-watch ──────────────────────
# This is the entire point: session #2 must not pay for the verifier again.
before="$(hv_count "$repo")"
start=$(date +%s)
tick "$repo" >/dev/null
elapsed=$(( $(date +%s) - start ))
if [ "$(hv_count "$repo")" -eq "$before" ]; then
  ok D2 "second tick reused the fresh watch (no re-run)"
else
  bad D2 "second tick re-ran the watcher — every session still pays the verifier"
fi
if [ "$elapsed" -le 10 ]; then
  ok D2b "fresh tick returned in ${elapsed}s"
else
  bad D2b "fresh tick took ${elapsed}s — still blocking session start"
fi

# ── D3: the skip path still answers the gate question correctly ──────────────
# Skipping the WATCH must not change the VERDICT: it is read from the queue.
repo="$(mk_repo d3 3 0)"
rc1="$(tick "$repo")"                     # cold, actionable=3
rc2="$(tick "$repo")"                     # fresh-skip, same queue
if [ "$rc1" = "0" ] && [ "$rc2" = "0" ]; then
  ok D3 "skip path still reports ACT (exit 0) when the queue is actionable"
else
  bad D3 "verdict changed across the skip path (cold=$rc1 fresh=$rc2, expected 0/0)"
fi
repo="$(mk_repo d3b 0 0)"
tick "$repo" >/dev/null
rc2="$(tick "$repo")"
if [ "$rc2" = "1" ]; then
  ok D3b "skip path still reports IDLE (exit 1) on an empty queue"
else
  bad D3b "fresh tick on an empty queue exited $rc2, expected 1 (idle)"
fi

# ── D4: the freshness window EXPIRES (never permanently blind) ───────────────
repo="$(mk_repo d4 0 0)"
tick "$repo" >/dev/null
before="$(hv_count "$repo")"
# Backdate every stamp the tool may have written.
find "$repo/.results" "$repo/knowledge" -type f -exec touch -d '25 hours ago' {} + 2>/dev/null
python3 - "$repo" <<'PY'
import json, os, sys, time
old = time.time() - 25 * 3600
for base, _, files in os.walk(sys.argv[1]):
    if ".git" in base: continue
    for f in files:
        p = os.path.join(base, f)
        if not (f.endswith(".json") or f.endswith(".jsonl") or "stamp" in f or "watch" in f):
            continue
        try:
            raw = open(p).read()
        except OSError:
            continue
        if "ts" not in raw and "last" not in raw: continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict):
            for k in list(d):
                if "ts" in k or "time" in k or "last" in k:
                    if isinstance(d[k], (int, float)): d[k] = old
                    elif isinstance(d[k], str):
                        d[k] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old))
            open(p, "w").write(json.dumps(d))
PY
tick "$repo" >/dev/null
if [ "$(hv_count "$repo")" -gt "$before" ]; then
  ok D4 "watch resumed after the freshness window expired"
else
  bad D4 "watcher never ran again after expiry — permanently blind"
fi

# ── D5: a MISSING stamp must watch (fail open to watching) ───────────────────
repo="$(mk_repo d5 0 0)"
tick "$repo" >/dev/null
rm -rf "$repo/.results"
before="$(hv_count "$repo")"
tick "$repo" >/dev/null
if [ "$(hv_count "$repo")" -gt "$before" ]; then
  ok D5 "missing stamp → watched (silence is not blindness)"
else
  bad D5 "missing stamp → skipped; a wiped stamp reads as 'all clear'"
fi

# ── D6: single-flight — N concurrent ticks produce at most ONE watch ─────────
repo="$(mk_repo d6 0 6)"        # slow verifier so the ticks genuinely overlap
for _ in 1 2 3 4 5; do
  ( cd "$repo" && env -u CLAUDEMAXXING_HARNESS_CHILD \
      python3 "$ROOT/bin/loop-tick" --gate --quiet >/dev/null 2>&1 ) &
done
wait
n="$(hv_count "$repo")"
if [ "$n" -le 1 ]; then
  ok D6 "5 concurrent ticks produced $n watch (single-flight holds)"
else
  bad D6 "5 concurrent ticks produced $n concurrent verifier runs — the CPU storm is still possible"
fi

# ── D7: a timeout is a TIMEOUT, not a fabricated harness regression ──────────
# The old code mapped "I could not measure it in time" onto "the observer is
# broken" and enqueued a flaw for it, 611 times in one day.
repo="$(mk_repo d7 0 25)"
( cd "$repo" && env -u CLAUDEMAXXING_HARNESS_CHILD HARNESS_VERIFY_TIMEOUT_SECONDS=3 \
    python3 "$ROOT/bin/loop-tick" --gate >"$repo/out.txt" 2>&1 )
if grep -qi 'timed*.out\|timeout' "$repo/out.txt" "$ROOT/knowledge/loop-tick.log" 2>/dev/null; then
  ok D7 "a slow verifier is reported as a timeout"
else
  bad D7 "timeout not reported as a timeout: $(head -c 160 "$repo/out.txt")"
fi
if grep -qi 'observer unavailable\|unparseable' "$repo/enqueued.log" 2>/dev/null; then
  bad D7b "a timeout still enqueued an 'observer unavailable' flaw (phantom)"
else
  ok D7b "a timeout enqueued no phantom observer-unavailable flaw"
fi

# ── D8: the default budget must exceed the real verifier's runtime ───────────
# The 2026-08-12 trap was a 75s budget against a 71s (and rising) verifier: the
# margin, not the mechanism, is what failed. Executed, not grepped: a verifier
# slower than the OLD cap must complete under the NEW default.
repo="$(mk_repo d8 0 0)"
budget="$(python3 - <<PY
import re
src = open("$ROOT/bin/loop-tick").read()
m = re.search(r'HARNESS_VERIFY_TIMEOUT_SECONDS[^)]*?(\d{2,4})', src)
print(m.group(1) if m else 0)
PY
)"
if [ "${budget:-0}" -ge 240 ]; then
  ok D8 "default verifier budget is ${budget}s (headroom over a ~100s suite)"
else
  bad D8 "default verifier budget is ${budget}s — too tight for a ~100s suite that keeps growing"
fi

# ── D9: SessionStart KICKS the cold watch outside the interactive path ──────
# A stale/missing stamp must still cause a watch, but the human must not wait
# behind it. The stub sleeps long enough to discriminate detachment from a
# synchronous call; then we wait for the durable stamp so a launcher that merely
# drops the work cannot pass on latency alone.
repo="$(mk_repo d9 0 3)"
start_ms="$(python3 -c 'import time; print(int(time.monotonic() * 1000))')"
( cd "$repo" && env -u CLAUDEMAXXING_HARNESS_CHILD \
    python3 "$ROOT/bin/loop-tick" --kick --quiet >/dev/null 2>&1 )
kick_rc=$?
end_ms="$(python3 -c 'import time; print(int(time.monotonic() * 1000))')"
kick_ms=$(( end_ms - start_ms ))
if [ "$kick_rc" -eq 0 ]; then
  ok D9 "cold watch kick was accepted"
else
  bad D9 "cold watch kick exited $kick_rc"
fi
if [ "$kick_ms" -le 1500 ]; then
  ok D9b "cold watch kick returned in ${kick_ms}ms instead of blocking on the verifier"
else
  bad D9b "cold watch kick took ${kick_ms}ms — SessionStart still pays the verifier"
fi
for _ in $(seq 1 100); do
  [ -f "$repo/.results/watch-stamp.json" ] && break
  sleep 0.1
done
if [ -f "$repo/.results/watch-stamp.json" ] && [ "$(hv_count "$repo")" -eq 1 ]; then
  ok D9c "detached cold watch completed once and published its stamp"
else
  bad D9c "kick returned but watch did not complete exactly once (runs=$(hv_count "$repo"))"
fi

# ── D10: detachment mechanics and failure path are contract-visible ─────────
# Import the real helper but replace only Popen: this checks the exact boundary
# arguments without creating a second background process or relying on timing.
python3 - "$ROOT/bin/loop-tick" >"$TMP/d10.out" 2>"$TMP/d10.err" <<'PY'
import contextlib, importlib.util, importlib.machinery, io, os, subprocess, sys
path = sys.argv[1]
loader = importlib.machinery.SourceFileLoader("loop_tick_probe", path)
spec = importlib.util.spec_from_loader("loop_tick_probe", loader)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

seen = {}
def capture(*args, **kwargs):
    seen["args"] = args
    seen["kwargs"] = kwargs
    return object()
mod.subprocess.Popen = capture
os.environ["CLAUDEMAXXING_HARNESS_CHILD"] = "1"
assert mod.kick_watch(False) == 0
kw = seen["kwargs"]
assert kw["stdin"] is subprocess.DEVNULL
assert kw["stdout"] is subprocess.DEVNULL
assert kw["stderr"] is subprocess.DEVNULL
assert kw["start_new_session"] is True
assert kw["close_fds"] is True
assert "CLAUDEMAXXING_HARNESS_CHILD" not in kw["env"]
assert seen["args"][0][-2:] == ["--gate", "--quiet"]

def fail(*args, **kwargs):
    raise OSError("seeded spawn refusal")
mod.subprocess.Popen = fail
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert mod.kick_watch(False) == 2
assert "could not kick watcher" in err.getvalue()
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert mod.kick_watch(True) == 2
assert err.getvalue() == ""
PY
if [ "$?" -eq 0 ]; then
  ok D10 "kick closes inherited descriptors, detaches its session, scrubs the marker and fails observably"
else
  bad D10 "kick detachment/failure mechanics violated: $(tail -c 240 "$TMP/d10.err")"
fi

repo="$(mk_repo d10guard 0 0)"
( cd "$repo" && env CLAUDEMAXXING_HARNESS_CHILD=1 \
    python3 "$ROOT/bin/loop-tick" --kick --quiet >/dev/null 2>&1 )
guard_rc=$?
sleep 0.3
if [ "$guard_rc" -eq 0 ] && [ ! -s "$repo/hv.log" ]; then
  ok D10b "marked SessionStart kick is an immediate no-op"
else
  bad D10b "marked SessionStart kick spawned work or returned $guard_rc"
fi
( cd "$repo" && env CLAUDEMAXXING_HARNESS_CHILD=1 \
    python3 "$ROOT/bin/loop-tick" --gate --quiet >/dev/null 2>&1 )
marked_gate_rc=$?
if [ "$marked_gate_rc" -eq 1 ]; then
  ok D10c "marked synchronous gate preserves the idle exit contract"
else
  bad D10c "marked synchronous gate returned $marked_gate_rc instead of idle=1"
fi

printf '\nharness-watch-cadence: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
