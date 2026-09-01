#!/usr/bin/env bash
# Contract: bin/oll never blocks unbounded on an open-but-silent stdin.
#
# Why this exists (lq-e1fa0a69, attributed 2026-08-30): a worker dispatch died
# at an external 240s timeout with exit 124, ZERO stdout and ZERO stderr —
# indistinguishable from a dead provider. The filed theory was "large argv
# prompts break the lane", but the exact 4KB repro passes in a clean
# environment, and argv/stdin build a byte-identical payload — the ONLY code
# that behaves differently between the two invocations is the unbounded
# sys.stdin.read(). Reproduced deterministically with a TINY argv prompt plus
# a FIFO held open by a writer that never writes: infinite block, silent
# death. The guard bounds the wait for stdin's FIRST byte and fails LOUDLY
# with the remedy in the message.
#
# Offline by construction: every case either trips the stdin guard or the
# oversize gate (OLL_MAX_ESTIMATED_INPUT_TOKENS=1), both of which fire before
# any auth read or network call. The oversize diagnostic prints token counts,
# which is how a success case PROVES what was actually read from stdin
# without touching a provider.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/tests/lib/precondition.sh"
# Declared HERE, in the main shell: the bounded runs below happen inside
# subshells and $( ), where an exit 77 would end only the subshell and leave
# the parent asserting against output no probe produced.
harness_need_bounded_run
OLL="$ROOT/bin/oll"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "oll-stdin-guard contract: FAIL — $*" >&2; exit 1; }

# Token math for the proof cases, computed with the tool's own estimator so
# the contract cannot drift from the implementation's divisor.
user_tokens() { # user_tokens <prompt> <stdin-data|"">
  python3 - "$OLL" "$1" "$2" <<'PY'
import runpy, sys
mod = runpy.run_path(sys.argv[1])
prompt, data = sys.argv[2], sys.argv[3]
user = f"{prompt}\n\n--- INPUT ---\n{data}" if data else prompt
print(mod["estimate_input_tokens"](user))
PY
}

# C1 — the flaw itself: argv prompt + open-but-silent stdin must die BOUNDED
# and LOUD, never hang. (Pre-guard: blocks forever; timeout 8 kills it with
# exit 124 and empty stderr — the incident signature.)
fifo="$TMP/silent.fifo"; mkfifo "$fifo"
( exec 3>"$fifo"; sleep 15 ) & holder=$!
set +e
out=$(harness_timeout 8 env OLL_STDIN_FIRST_BYTE_TIMEOUT=1 "$OLL" "argv prompt" <"$fifo" 2>"$TMP/c1.err")
rc=$?
set -e
kill "$holder" 2>/dev/null || true
[ "$rc" -eq 2 ] || fail "C1: expected bounded loud exit 2, got $rc (124 = the old unbounded hang)"
[ -z "$out" ] || fail "C1: stdout must stay empty on refusal"
grep -qi "stdin" "$TMP/c1.err" || fail "C1: error does not name stdin"
grep -q "/dev/null" "$TMP/c1.err" || fail "C1: error does not carry the no-stdin remedy"
grep -q "OLL_STDIN_FIRST_BYTE_TIMEOUT" "$TMP/c1.err" || fail "C1: error does not name the slow-producer knob"

# C2 — the combine feature survives byte-for-byte: piped data joins the argv
# prompt under the --- INPUT --- separator. Proven by the oversize
# diagnostic's user-token count matching the estimator on the COMBINED text.
data="0123456789"
want="$(user_tokens "p" "$data")"
set +e
printf %s "$data" | env OLL_MAX_ESTIMATED_INPUT_TOKENS=1 "$OLL" "p" >/dev/null 2>"$TMP/c2.err"
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "C2: oversize gate should refuse with 2, got $rc"
grep -q "user ${want})" "$TMP/c2.err" || fail "C2: stdin was not combined (wanted user ${want}): $(cat "$TMP/c2.err")"

# C3 — a slow-but-writing producer still works: first byte inside the window,
# then EOF. Guard must WAIT, not peek-and-give-up.
fifo2="$TMP/slow.fifo"; mkfifo "$fifo2"
( exec 3>"$fifo2"; sleep 0.5; printf %s "$data" >&3; exec 3>&- ) &
set +e
harness_timeout 8 env OLL_STDIN_FIRST_BYTE_TIMEOUT=3 OLL_MAX_ESTIMATED_INPUT_TOKENS=1 "$OLL" "p" <"$fifo2" >/dev/null 2>"$TMP/c3.err"
rc=$?
set -e
wait
[ "$rc" -eq 2 ] || fail "C3: slow producer should reach the oversize gate (2), got $rc"
grep -q "user ${want})" "$TMP/c3.err" || fail "C3: slow producer's data was dropped: $(cat "$TMP/c3.err")"

# C4 — an explicit no-stdin invocation is untouched: /dev/null reads as
# instant EOF, argv-only estimate, no guard trip.
wantp="$(user_tokens "p" "")"
set +e
env OLL_MAX_ESTIMATED_INPUT_TOKENS=1 "$OLL" "p" </dev/null >/dev/null 2>"$TMP/c4.err"
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "C4: /dev/null path should hit the oversize gate (2), got $rc"
grep -q "user ${wantp})" "$TMP/c4.err" || fail "C4: argv-only estimate drifted: $(cat "$TMP/c4.err")"

# C5 — prompt-less mode (stdin IS the prompt) gets the same bound: silence is
# a caller error either way, and either way it must be loud, not a hang.
fifo3="$TMP/silent2.fifo"; mkfifo "$fifo3"
( exec 3>"$fifo3"; sleep 15 ) & holder3=$!
set +e
harness_timeout 8 env OLL_STDIN_FIRST_BYTE_TIMEOUT=1 "$OLL" <"$fifo3" >"$TMP/c5.out" 2>"$TMP/c5.err"
rc=$?
set -e
kill "$holder3" 2>/dev/null || true
[ "$rc" -eq 2 ] || fail "C5: prompt-less silent stdin should refuse with 2, got $rc"
grep -qi "stdin" "$TMP/c5.err" || fail "C5: refusal is not loud"

# C6 — the knob is fail-safe: garbage or non-positive values fall back to the
# 30s default (guard stays ARMED), never to "fire instantly" (which would
# race a normal `producer | oll` pipeline) and never to a crash.
python3 - "$OLL" <<'PY' || exit 1
import os, runpy, sys
mod = runpy.run_path(sys.argv[1])
t = mod["stdin_first_byte_timeout"]
os.environ.pop("OLL_STDIN_FIRST_BYTE_TIMEOUT", None)
assert t() == 30.0, f"C6: default drifted: {t()!r}"
os.environ["OLL_STDIN_FIRST_BYTE_TIMEOUT"] = "5"
assert t() == 5.0, f"C6: valid override ignored: {t()!r}"
for bad in ("banana", "", "0", "-3"):
    os.environ["OLL_STDIN_FIRST_BYTE_TIMEOUT"] = bad
    assert t() == 30.0, f"C6: {bad!r} must fall back to the default, got {t()!r}"
PY

# C7 — the two paths that never touch select() still carry their data
# faithfully (both were mutation survivors of the first pass: a deleted tty
# branch and a deleted fd-less read both stayed green).
python3 - "$OLL" <<'PY' || exit 1
import io, runpy, sys
mod = runpy.run_path(sys.argv[1])

# C7a — an fd-less in-process stdin (a contract's StringIO) is read PLAIN,
# never dropped: returning nothing here would silently amputate piped input.
sys.stdin = io.StringIO("in-process data")
got = mod["read_stdin_all_bounded"](1)
assert got == "in-process data", f"C7a: fd-less stdin dropped: {got!r}"

# C7b — a tty stdin is never read at all: the branch must yield "" without
# calling read() (a deleted assignment turns every interactive call into a
# NameError; a read() would block an interactive terminal).
class FakeTty:
    def isatty(self): return True
    def read(self): raise AssertionError("C7b: tty stdin must not be read")
    def fileno(self): raise AssertionError("C7b: tty stdin must not be selected")
sys.stdin = FakeTty()
sys.argv = ["oll", "p"]
import os
os.environ["OLL_MAX_ESTIMATED_INPUT_TOKENS"] = "1"
try:
    rc = mod["main"]()
except SystemExit as ex:
    rc = ex.code
assert rc == 2, f"C7b: tty path should hit the oversize gate with argv only, got {rc!r}"
PY

echo "oll-stdin-guard contract: OK"
