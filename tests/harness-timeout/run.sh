#!/usr/bin/env bash
# Contract: tests/lib/precondition.sh :: harness_timeout is a REAL bound on a
# host with no timeout(1).
#
# Why this exists (lq-4b8954b1, measured 2026-08-31 on the Mac): macOS ships no
# timeout(1) and no gtimeout, so every contract calling it bare got exit 127 —
# which is not inert. It read two opposite ways, both wrong: oll-stdin-guard
# turned it into a HARD harness-verify error blaming bin/oll (green on Ubuntu),
# and harness-reentrancy C5 turned it into a FALSE GREEN, because a probe that
# never ran leaves an empty spawn log and the "no live agent" assertion passes.
#
# The bound is exercised through the REAL fallback path (Tier-1c): the cases
# below put a stub PATH in front that resolves neither timeout nor gtimeout, so
# on BOTH machines the python3 implementation is the code under test — a Linux
# run cannot pass by silently delegating to coreutils.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$ROOT/tests/lib/precondition.sh"
[ -f "$LIB" ] || { printf 'harness-timeout: %s missing\n' "$LIB" >&2; exit 1; }
# Sourced in this shell too: C13 needs pgrep, and an absent pgrep is a
# precondition (SKIP), not a failure — the same rule this helper enforces.
. "$LIB"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "harness-timeout contract: FAIL — $*" >&2; exit 1; }

# A PATH with the interpreters the fallback needs and NOTHING named timeout.
STUB="$TMP/bin"; mkdir -p "$STUB"
for t in python3 sh bash sleep cat env printf touch; do
  p="$(command -v "$t" 2>/dev/null)" || continue
  ln -sf "$p" "$STUB/$t"
done
[ -x "$STUB/python3" ] || fail "setup: python3 not resolvable"
PATH="$STUB" command -v timeout >/dev/null 2>&1 && fail "setup: stub PATH still resolves timeout"
PATH="$STUB" command -v gtimeout >/dev/null 2>&1 && fail "setup: stub PATH still resolves gtimeout"

# run_fallback <script-body> — evaluate <script-body> with harness_timeout
# sourced and ONLY the stub PATH, so the python3 implementation is what runs.
run_fallback() {
  env -i HOME="$HOME" PATH="$STUB" HARNESS_PRECONDITION_TEST_FALLBACK_DIRS="" \
    "$STUB/bash" -c ". '$LIB'
harness_need_bounded_run
$1"
}

# ── C1: the bound actually fires, with timeout(1) exit code 124, on time ─────
start=$(date +%s)
set +e
run_fallback 'harness_timeout 1 sleep 30' >/dev/null 2>&1
rc=$?
set -e
elapsed=$(( $(date +%s) - start ))
[ "$rc" -eq 124 ] || fail "C1: expiry must exit 124 (timeout(1) semantics), got $rc"
[ "$elapsed" -lt 15 ] || fail "C1: bound did not fire — ${elapsed}s for a 1s bound"

# ── C2: stdin belongs to the CALLER, not to the implementation ───────────────
# The load-bearing case. A heredoc-fed python3 hands the child the SCRIPT as
# stdin; the caller's pipe vanishes and oll-stdin-guard would measure nothing
# while still reporting a verdict.
printf 'caller-payload' > "$TMP/in"
got="$(run_fallback 'harness_timeout 5 cat' < "$TMP/in")" || fail "C2: bounded cat failed"
[ "$got" = "caller-payload" ] || fail "C2: caller stdin was replaced by the implementation: got '$got'"

# ── C3: a command that finishes keeps its own exit status, both ways ─────────
set +e
run_fallback 'harness_timeout 10 sh -c "exit 0"' >/dev/null 2>&1; rc0=$?
run_fallback 'harness_timeout 10 sh -c "exit 7"' >/dev/null 2>&1; rc7=$?
set -e
[ "$rc0" -eq 0 ] || fail "C3: success must stay 0, got $rc0"
[ "$rc7" -eq 7 ] || fail "C3: child status must pass through, got $rc7 (not 7)"

# ── C4: unrunnable commands keep timeout(1) codes, never 124 ────────────────
# 127 must stay reserved for "no such command" — the reading that made the
# original bug legible only in hindsight.
printf '#!/bin/sh\nexit 0\n' > "$TMP/notexec"; chmod 644 "$TMP/notexec"
set +e
run_fallback 'harness_timeout 5 definitely-not-a-real-command-xyz' >/dev/null 2>&1; rcnf=$?
run_fallback "harness_timeout 5 '$TMP/notexec'" >/dev/null 2>&1; rcne=$?
set -e
[ "$rcnf" -eq 127 ] || fail "C4: missing command must exit 127, got $rcnf"
[ "$rcne" -eq 126 ] || fail "C4: non-executable must exit 126, got $rcne"

# ── C5: expiry kills the whole TREE ─────────────────────────────────────────
# A bound whose grandchild outlives it is not a bound: the probe returns, the
# contract moves on, and the escapee keeps running against the next case.
mark="$TMP/escapee"
set +e
run_fallback "harness_timeout 1 sh -c 'sh -c \"sleep 6; touch $mark\" & wait'" >/dev/null 2>&1
set -e
"$STUB/python3" -c "import time; time.sleep(9)"
[ ! -e "$mark" ] || fail "C5: grandchild survived the bound — the tree was not killed"

# ── C6: a real timeout(1) is PREFERRED, not shadowed by the fallback ────────
# The fallback is a compatibility floor. Where coreutils exists it stays the
# implementation, so Linux keeps measuring the same binary it always did.
SHIM="$TMP/shim"; mkdir -p "$SHIM"
printf '#!/bin/sh\necho SHIM-USED\nexit 0\n' > "$SHIM/timeout"; chmod +x "$SHIM/timeout"
out="$(env -i HOME="$HOME" PATH="$SHIM:$STUB" HARNESS_PRECONDITION_TEST_FALLBACK_DIRS="" \
  "$STUB/bash" -c ". '$LIB'; harness_need_bounded_run; harness_timeout 5 sh -c 'exit 3'" 2>&1)" || true
case "$out" in
  *SHIM-USED*) : ;;
  *) fail "C6: an installed timeout(1) was bypassed for the fallback: '$out'" ;;
esac

# ── C7: a host with NO bound at all cannot produce a green ─────────────────
# Silence is not blindness, and the skip must land where exiting still ends the
# contract. Both cross-family critics of this round independently found that an
# exit 77 raised from inside a caller subshell terminates only the subshell:
# the parent walks on and grades an assertion against output the probe never
# produced — the same false green the helper exists to remove.
BARE="$TMP/bare"; mkdir -p "$BARE"
for t in sh bash sleep cat env; do
  p="$(command -v "$t" 2>/dev/null)" || continue
  ln -sf "$p" "$BARE/$t"
done
bare_run() {  # bare_run <script-body> — no timeout, no gtimeout, no python3
  env -i HOME="$HOME" PATH="$BARE" HARNESS_PRECONDITION_TEST_FALLBACK_DIRS="" \
    "$BARE/bash" -c ". '$LIB'
$1"
}

# C7a — the top-level precondition skips the whole contract, and names what is
# missing rather than saying only "unmeasurable".
set +e
bare_run 'harness_need_bounded_run; echo REACHED-THE-CASES' >"$TMP/c7a.out" 2>"$TMP/c7a.err"
rc7a=$?
set -e
[ "$rc7a" -eq 77 ] || fail "C7a: a host with no bound must SKIP (77), got $rc7a"
grep -q "REACHED-THE-CASES" "$TMP/c7a.out" && fail "C7a: skip did not stop the contract"
grep -qi "bounded run" "$TMP/c7a.err" || fail "C7a: skip does not name what is missing: $(cat "$TMP/c7a.err")"

# C7b — the load-bearing one. A caller that FORGOT the precondition and bounds
# inside a subshell must still be unable to read a pass. 125 is GNU timeout's
# own "the bound itself failed"; 0 here is the false green, and 77 is worse
# than useless because the subshell swallows it.
set +e
bare_run '( harness_timeout 1 sleep 30 ) >/dev/null 2>&1; echo "SUBSHELL-RC=$?"' >"$TMP/c7b.out" 2>&1
set -e
grep -q "SUBSHELL-RC=125" "$TMP/c7b.out" \
  || fail "C7b: an unguarded bound in a subshell must surface 125, got: $(cat "$TMP/c7b.out")"

# C7c — every real caller declares the precondition in its MAIN shell, so C7b
# stays a backstop and never the primary defence.
for c in oll-stdin-guard harness-reentrancy; do
  grep -q '^harness_need_bounded_run' "$ROOT/tests/$c/run.sh" \
    || fail "C7c: tests/$c/run.sh uses a bounded run without declaring the precondition"
done

# ── C9: a signal death reports as the shell does, 128+N, on every host ──────
# Popen.wait() reports a signal as -N; sys.exit(-9) surfaces as 247. Left
# unconverted, a killed child reads as 137 under coreutils and 247 under the
# fallback — a host-dependent verdict flip, which is this helper's own bug class.
# The self-kill lives in a script file so that $$ is expanded by the BOUNDED
# child at its own runtime; inline, the calling shell expands it first and the
# signal lands on the wrong process (measured: the case then cannot discriminate).
printf '#!/bin/sh\nkill -9 $$\n' > "$TMP/selfkill"; chmod +x "$TMP/selfkill"
set +e
run_fallback "harness_timeout 10 '$TMP/selfkill'" >/dev/null 2>&1; rcsig=$?
set -e
[ "$rcsig" -eq 137 ] || fail "C9: SIGKILL death must report 128+9=137, got $rcsig"

# ── C10: the bound leaks nothing into the child environment ────────────────
# GNU timeout exports nothing of its own. A knob passed through the environment
# would be visible to any contract that snapshots env under the bound.
leaked="$(run_fallback 'harness_timeout 5 env' 2>/dev/null \
  | grep -cE 'HARNESS_TIMEOUT|HARNESS_BOUNDED_RUN_IMPL' || true)"
[ "$leaked" -eq 0 ] || fail "C10: the bound leaked $leaked harness var(s) into the child"

# ── C11: the pinned bound cannot be re-aimed by a call-site PATH ───────────
# Callers narrow PATH mid-contract on purpose (harness-reentrancy prepends a
# shim directory to intercept agent spawns). A bound that resolves per call
# would then be whatever that directory happens to contain — so the precondition
# pins an absolute path and every later call uses it.
HIJACK="$TMP/hijack"; mkdir -p "$HIJACK"
printf '#!/bin/sh\necho HIJACKED\nexit 0\n' > "$HIJACK/timeout"; chmod +x "$HIJACK/timeout"
printf '#!/bin/sh\necho HIJACKED\nexit 0\n' > "$HIJACK/python3"; chmod +x "$HIJACK/python3"
out="$(run_fallback "( PATH=\"$HIJACK:\$PATH\"; harness_timeout 5 sh -c \"exit 4\" ); echo RC=\$?" 2>&1)"
case "$out" in
  *HIJACKED*) fail "C11: a call-site PATH re-aimed the bound at a shim: '$out'" ;;
esac
case "$out" in
  *RC=4*) : ;;
  *) fail "C11: the pinned bound did not run the command: '$out'" ;;
esac

# ── C14: an UNDECLARED caller never PATH-searches for its bound ────────────
# The complement of C11. If a caller forgets harness_need_bounded_run, the
# helper must refuse (125) rather than resolve a bound from the call site's
# PATH — which in tests/harness-reentrancy is deliberately full of shims. A
# silent PATH search there would make a shim the bound.
out="$(env -i HOME="$HOME" PATH="$HIJACK:$STUB" HARNESS_PRECONDITION_TEST_FALLBACK_DIRS="" \
  "$STUB/bash" -c ". '$LIB'; harness_timeout 5 sh -c 'exit 4'; echo RC=\$?" 2>&1)"
case "$out" in
  *HIJACKED*) fail "C14: an undeclared caller PATH-searched and found a shim: '$out'" ;;
esac
case "$out" in
  *RC=125*) : ;;
  *) fail "C14: an undeclared caller must refuse with 125, got: '$out'" ;;
esac

# ── C12: an unusable bound is a usage error, not a traceback ───────────────
# GNU timeout accepts s/m/h/d suffixes; this helper does not. It must refuse
# loudly with 125 (timeout(1)'s own "the bound itself failed"), never crash and
# never be mistaken for the command's own status.
# "1.2.3" and "." pass a digits-and-dots character class but not float(); a
# missing command reaches Popen([]). All three must land on 125, not a traceback.
for bad in 90s "" abc 1.2.3 . ; do
  set +e
  run_fallback "harness_timeout '$bad' sh -c 'exit 0'" >/dev/null 2>"$TMP/c12.err"
  rcbad=$?
  set -e
  [ "$rcbad" -eq 125 ] || fail "C12: bound '$bad' must be refused with 125, got $rcbad"
  grep -qiE "plain number of seconds|unusable bound" "$TMP/c12.err" \
    || fail "C12: refusal of '$bad' does not say what is wrong: $(cat "$TMP/c12.err")"
  grep -qi "traceback" "$TMP/c12.err" && fail "C12: bound '$bad' produced a traceback"
done
set +e
run_fallback "harness_timeout 5" >/dev/null 2>"$TMP/c12b.err"; rcnc=$?
set -e
[ "$rcnc" -eq 125 ] || fail "C12: a bound with no command must be refused with 125, got $rcnc"

# ── C13: a signal to the bound reaches the bounded child ───────────────────
# GNU timeout relays the signals it receives. Without relay, a TERM kills only
# the wrapper and leaves the child running unattended — which in a re-entrancy
# probe is precisely the escaped process the probe exists to catch.
harness_need_cmd ps
printf '#!/bin/sh\necho $$ > "%s"\nsleep 20\n' "$TMP/child.pid" > "$TMP/longchild"
chmod +x "$TMP/longchild"
rm -f "$TMP/child.pid"
run_fallback "harness_timeout 30 '$TMP/longchild'" >/dev/null 2>&1 &
holder_sh=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$TMP/child.pid" ] && break; sleep 0.5; done
[ -s "$TMP/child.pid" ] || fail "C13: bounded child never started"
child_pid="$(cat "$TMP/child.pid")"
# The wrapper is the child's PARENT, read directly. Descending from the
# backgrounded shell instead would signal whichever intermediate process bash
# happened not to exec away — and killing THAT orphans the child by hand,
# which is the very condition under test (measured: the case then always reds).
wrapper="$(ps -o ppid= -p "$child_pid" 2>/dev/null | tr -d ' ')"
[ -n "$wrapper" ] || fail "C13: could not find the bound wrapper process"
kill -TERM "$wrapper" 2>/dev/null || fail "C13: could not signal the wrapper"
for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$child_pid" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$child_pid" 2>/dev/null; then
  kill -9 "$child_pid" 2>/dev/null || true
  fail "C13: the bounded child was ORPHANED — the signal was not relayed"
fi
wait "$holder_sh" 2>/dev/null || true

# ── C8: no contract may reintroduce a bare timeout(1) call ─────────────────
# The regression guard. This is a lint, not a proof of intent: it catches the
# command-position form that produced the incident, which is the form every
# affected caller used.
bare_re='(^|[|;&(]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+|env[[:space:]]+)*timeout[[:space:]]+[0-9]'
offenders="$(grep -rnE "$bare_re" "$ROOT"/tests/*/run.sh 2>/dev/null \
  | grep -vE ':[0-9]+:[[:space:]]*#' \
  | grep -v 'harness_timeout' \
  | grep -v '/harness-timeout/run.sh:' || true)"
[ -z "$offenders" ] || fail "C8: bare timeout(1) is not portable — use harness_timeout:
$offenders"

echo "harness-timeout contract: OK"
