# Shared contract preconditions — source this, do not execute it.
#
# A contract that could not RUN is not a contract that FAILED. harness-verify
# reserves exit 77 (the GNU/autotools SKIP convention) for "precondition unmet
# on this host": it lands as a WARNING naming the missing dependency, never as
# an error, so loop-tick does not enqueue a machine-local environment gap as a
# harness flaw the other machine can never reproduce. Four of the queue's open
# harness-verify reds were exactly that — lq-ffc3c61b / lq-b73b6fd7 /
# lq-c3e6c297, all Mac-only, all saying "tmux missing" — against an Ubuntu
# baseline of 0 errors. Same rule the loop already applies to timeouts: report
# the inability to measure AS an inability to measure.
#
# Resolution comes FIRST. harness-verify runs contracts under minimal-PATH
# environments (launchd loop-cron, sandboxed sessions) that omit Homebrew, so
# an INSTALLED tool can look absent (lq-450528ba, fixed per-contract in
# 2026-07 and promptly regressed into three other contracts — hence this
# single shared copy). Only genuine absence skips.
#
# The widened PATH only ever applies in the case that previously produced a
# hard red: if the command was already resolvable this function returns before
# touching PATH. So the widening replaces "no measurement at all", never a
# correct measurement — but it IS a different environment, so keep the list
# narrow.
#
# The fallback list is deliberately NARROW — only the directories a minimal
# environment genuinely omits. /usr/bin and /bin are excluded on purpose: a
# contract that sets a deliberately minimal PATH (tests/context-stateless) must
# keep getting the environment it asked for, not one this helper widened.
#
# Silence is not blindness: a skip is counted and printed by harness-verify,
# so an environment that quietly stops measuring is visible, not green.

HARNESS_SKIP_EXIT=77

# harness_need_cmd <command> [label]
#   Resolves <command>, prepending the directory to PATH when it is only
#   reachable outside the inherited one. Exits 77 with a named reason when the
#   command genuinely is not installed on this host.
harness_need_cmd() {
  local name="$1" label="${2:-$1}" d fallback_dirs
  command -v "$name" >/dev/null 2>&1 && return 0

  # Contracts that prove the genuine-absence path need to hide installed host
  # tools without changing the production fallback policy.  An explicitly set
  # test override (including the empty string) supplies that seam; ordinary
  # callers always get the same narrow three-directory fallback as before.
  if [ "${HARNESS_PRECONDITION_TEST_FALLBACK_DIRS+x}" = x ]; then
    fallback_dirs="$HARNESS_PRECONDITION_TEST_FALLBACK_DIRS"
  else
    fallback_dirs="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
  fi

  local IFS=:
  for d in $fallback_dirs; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then
      PATH="$d:$PATH"
      export PATH
      return 0
    fi
  done
  # The receipt is what AUTHENTICATES the skip. harness-verify hands each
  # attempt a private path it just invented; only this function writes it, so a
  # nested tool that merely exits 77 with a SKIP:-shaped last line cannot be
  # mistaken for this contract's own precondition gate. Stderr stays for the
  # human and for a standalone run, where no receipt path exists.
  if [ -n "${HARNESS_SKIP_RECEIPT:-}" ]; then
    printf '%s not installed — contract cannot be measured on this host\n' \
      "$label" > "$HARNESS_SKIP_RECEIPT" 2>/dev/null || true
  fi
  printf 'SKIP: %s not installed — contract cannot be measured on this host\n' "$label" >&2
  exit "$HARNESS_SKIP_EXIT"
}

# ── Bounded execution ────────────────────────────────────────────────────────
# The fallback script is a plain single-quoted string, NOT a heredoc, for two
# independent reasons:
#   * A heredoc at CALL time becomes python3's stdin and the child inherits it,
#     silently replacing whatever the caller piped in — which is the entire
#     subject of tests/oll-stdin-guard.
#   * A heredoc inside $( ) at SOURCE time is mis-parsed by bash 3.2 (the
#     /bin/bash macOS still ships) as soon as the body contains an apostrophe.
# So the Python below uses double quotes throughout, deliberately. The bound is
# argv[1], not an environment variable: an env knob leaks into the bounded
# child (which GNU timeout never does) and turns a caller mistake into a
# KeyError instead of a usage error.
_HARNESS_TIMEOUT_PY='
import os, signal, subprocess, sys

try:
    # The shell-side check is a fast path, not the guarantee: it admits strings
    # like "1.2.3" and "." that float() still rejects. GNU timeout answers an
    # unusable bound (or a missing command) with 125; a traceback and exit 1
    # would be indistinguishable from a command that legitimately failed.
    secs = float(sys.argv[1])
    argv = sys.argv[2:]
    if not argv:
        raise ValueError("no command given")
except (ValueError, IndexError) as exc:
    sys.stderr.write("harness_timeout: unusable bound or command: %s\n" % exc)
    sys.exit(125)

proc = None

def signal_group(sig):
    # Never raises: this runs from a signal handler, where an escaping OSError
    # would replace the 128+N status with a traceback and exit 1.
    if proc is None:
        return
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass

def relay(sig, _frame):
    # GNU timeout forwards the signals it receives. Armed BEFORE Popen: between
    # spawning and arming, a TERM would kill only the wrapper and ORPHAN the
    # child — in a re-entrancy probe that is a live unattended process of
    # exactly the kind the probe exists to catch. This shrinks that window to
    # the microseconds between fork and the assignment to proc; it does not
    # eliminate it (GNU timeout blocks signals around the fork, this does not),
    # so a signal landing inside it still leaves an orphan. Either way the
    # wrapper exits 128+N, so no verdict flips on it.
    signal_group(sig)
    sys.exit(128 + sig)

for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, relay)

try:
    # A new process GROUP, matching what GNU timeout does, so expiry kills the
    # whole tree: a bounded probe whose grandchild outlives it is not a bound.
    # Deliberately NOT a new session (os.setsid): that additionally drops the
    # controlling terminal, which GNU timeout leaves alone, and would change
    # isatty() under the bound on one host only.
    proc = subprocess.Popen(argv, preexec_fn=os.setpgrp)
except FileNotFoundError:
    sys.exit(127)   # timeout(1): command not found
except OSError:
    sys.exit(126)   # timeout(1): found but not executable

def as_shell_status(rc):
    # Popen.wait() reports a signal death as -N; every shell reports it as
    # 128+N, and so does GNU timeout. Returning -9 here would surface as exit
    # 247 on the fallback host only — a host-dependent verdict flip, which is
    # the exact bug class this helper exists to remove.
    return rc if rc >= 0 else 128 - rc

try:
    sys.exit(as_shell_status(proc.wait(timeout=secs)))
except subprocess.TimeoutExpired:
    signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        proc.wait()
    sys.exit(124)   # timeout(1): the bound fired
'

# harness_bounded_run_impl
#   Prints how a bounded run would be performed on this host: "timeout",
#   "gtimeout", "python3", or nothing at all (return 1).
harness_bounded_run_impl() {
  local bin path
  for bin in timeout gtimeout python3; do
    # An ABSOLUTE path, not a bare name: a bounded run must not be re-resolved
    # against whatever PATH the call site happens to have. Callers legitimately
    # narrow PATH mid-contract (tests/harness-reentrancy prepends a shim
    # directory), and a bound that changes identity with the call site is not
    # a bound. Raised by a cross-family critic of the 2026-08-31 round.
    if path="$(command -v "$bin" 2>/dev/null)" && [ -n "$path" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

# harness_need_bounded_run
#   Top-level precondition: call this in the caller's MAIN shell, beside the
#   other harness_need_cmd declarations, before any case runs.
#
#   Why it is separate from harness_timeout: harness_need_cmd reports an unmet
#   precondition by exiting 77, and a bounded run is usually invoked inside a
#   subshell or a $( ). exit 77 there terminates only the subshell — the parent
#   walks on and evaluates an assertion against output the probe never
#   produced. That is precisely the false green this whole helper exists to
#   remove (both cross-family critics of the 2026-08-31 round found it
#   independently), so the skip decision is hoisted to a place where exiting
#   actually ends the contract.
harness_need_bounded_run() {
  # PIN the implementation here, in the ambient environment: the host the
  # precondition inspected is then the host every later call uses.
  # Deliberately NOT exported. Subshells and $( ) inherit a plain shell
  # variable, which is every way these contracts invoke a bounded run, while an
  # export would inject a harness knob into the environment of every tool under
  # test — the same leak the argv-not-env decision above rejected.
  if HARNESS_BOUNDED_RUN_IMPL="$(harness_bounded_run_impl)"; then
    return 0
  fi
  unset HARNESS_BOUNDED_RUN_IMPL
  harness_need_cmd python3 "timeout(1)/gtimeout or python3 (for a bounded run)"
}

# harness_timeout <seconds> <command> [args...]
#   Run <command> under a wall-clock bound with GNU timeout(1) exit codes
#   (124 expired, 126 not executable, 127 not found), on every host in the
#   fleet.
#
#   Why this exists: macOS ships no timeout(1), and coreutils gtimeout is not
#   installed on this Mac, so a bare "timeout 8 cmd" exits 127 — and 127 is not
#   inert. It read TWO opposite ways on 2026-08-31, both wrong:
#     * tests/oll-stdin-guard C1 compared 127 against the expected 2 and
#       reported a HARD harness-verify ERROR against bin/oll, a tool that is
#       fine (Ubuntu: 0 errors). A host gap masqueraded as a code regression.
#     * tests/harness-reentrancy C5 runs the cheap-delegate contract under a
#       bound and asserts no live agent was spawned. At 127 the contract never
#       ran, the log stayed empty, and the assertion PASSED — a false green on
#       a guard whose whole job is catching recursive agent spawns.
#   Skipping alone would have silenced the Mac on exactly the guards it needs,
#   so the bound is made portable rather than made optional.
harness_timeout() {
  local secs="${1:-}" impl; shift || true
  case "$secs" in
    ''|*[!0-9.]*)
      # GNU timeout also accepts s/m/h/d suffixes; this helper deliberately
      # does not, and says so with GNU's own "the bound itself failed" code
      # rather than pretending to support a form it would mis-parse.
      printf 'harness_timeout: bound must be a plain number of seconds, got %s\n' "${secs:-<missing>}" >&2
      return 125 ;;
  esac
  # ONLY the pin. Re-resolving here would search the CALL SITE's PATH, which
  # tests/harness-reentrancy deliberately fills with shims — a shim named
  # timeout or python3 would then become the bound. Declaring the precondition
  # is mandatory, and forgetting it is loud rather than silently PATH-dependent.
  impl="${HARNESS_BOUNDED_RUN_IMPL:-}"
  [ -n "$impl" ] || {
    # Unreachable when the caller declared harness_need_bounded_run. If one
    # forgot, fail with GNU timeout's own "the bound itself failed" code —
    # never 0, so an unguarded caller still cannot read this as a pass.
    printf 'harness_timeout: no pinned bound — call harness_need_bounded_run in the main shell first\n' >&2
    return 125
  }
  case "$impl" in
    */python3) "$impl" -c "$_HARNESS_TIMEOUT_PY" "$secs" "$@" ;;
    *)         "$impl" "$secs" "$@" ;;
  esac
}
