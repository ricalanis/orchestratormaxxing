#!/usr/bin/env bash
# Real-boundary contract: an inherited TMUX socket must never let an improvised
# diagnostic kill the shared server. Every server here uses a private -S path.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "tmux-guard: tmux"

TOOL="${TMUX_GUARD_UNDER_TEST:-$ROOT/bin/tmux-guard}"
REAL_TMUX="${TMUX_GUARD_REAL_UNDER_TEST:-}"
if [[ -z "$REAL_TMUX" ]]; then
  for candidate in /opt/homebrew/bin/tmux /usr/local/bin/tmux \
      /home/linuxbrew/.linuxbrew/bin/tmux /usr/bin/tmux /bin/tmux; do
    if [[ -x "$candidate" && ! "$candidate" -ef "$TOOL" ]]; then
      REAL_TMUX="$candidate"
      break
    fi
  done
fi
[[ -n "$REAL_TMUX" ]] || { printf 'tmux-guard: real tmux unavailable\n' >&2; exit 77; }
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/tmux-guard.XXXXXX")"
OUTER="$SCRATCH/outer.sock"
OTHER="$SCRATCH/other"

fail() { printf 'tmux-guard: %s\n' "$*" >&2; exit 1; }
server_up() { "$REAL_TMUX" -S "$OUTER" has-session -t '=sentinel' 2>/dev/null; }
stop_outer() { "$REAL_TMUX" -S "$OUTER" kill-session -t '=sentinel' 2>/dev/null || true; }
cleanup() { stop_outer; rm -rf "$SCRATCH"; }
trap cleanup EXIT

start_outer() {
  mkdir -p "$OTHER"
  "$REAL_TMUX" -S "$OUTER" new-session -d -s sentinel 'sleep 300'
  server_up || fail 'private sentinel did not start'
}

# C0: prove the exact pre-fix failure against a sacrificial server. TMUXTMPDIR
# differs, but inherited TMUX wins and the real client kills OUTER.
start_outer
set +e
TMUX="$OUTER,999999,0" TMUX_PANE='%0' TMUX_TMPDIR="$OTHER" \
  "$REAL_TMUX" kill-server >/dev/null 2>&1
control_rc=$?
set -e
[[ "$control_rc" -eq 0 ]] || fail "C0 unsafe control exited $control_rc"
server_up && fail 'C0 unsafe control did not reproduce the inherited-TMUX failure'

# C1: the guarded candidate must refuse the same argv and preserve OUTER.
start_outer
set +e
guard_err="$(TMUX="$OUTER,999999,0" TMUX_PANE='%0' TMUX_TMPDIR="$OTHER" \
  TMUX_GUARD_REAL="$REAL_TMUX" "$TOOL" kill-server 2>&1)"
guard_rc=$?
set -e
[[ "$guard_rc" -ne 0 ]] || fail 'C1 inherited-TMUX kill-server was accepted'
[[ "$guard_err" == *'refused kill-server'* ]] || fail "C1 refusal is not typed: $guard_err"
server_up || fail 'C1 guarded command killed the sentinel'

# C2: a chained command is equally dangerous and must be rejected before any
# part executes. An ordinary command with a kill-server argument is not one.
set +e
TMUX_GUARD_REAL="$REAL_TMUX" "$TOOL" -S "$OUTER" display-message -p safe \; kill-server \
  >/dev/null 2>&1
chain_rc=$?
set -e
[[ "$chain_rc" -ne 0 ]] || fail 'C2 chained kill-server was accepted'
server_up || fail 'C2 chained command killed the sentinel'
TMUX_GUARD_REAL="$REAL_TMUX" "$TOOL" -S "$OUTER" has-session -t '=sentinel' \
  || fail 'C2 ordinary tmux command did not pass through'

# C3: stdout and status from harmless commands pass through unchanged.
guard_version="$(TMUX_GUARD_REAL="$REAL_TMUX" "$TOOL" -V)"
real_version="$($REAL_TMUX -V)"
[[ "$guard_version" == "$real_version" ]] || fail 'C3 tmux -V changed in transit'

# C4: an explicit bad real-binary pin is authoritative and fails closed rather
# than recursively resolving this shim from PATH.
set +e
bad_err="$(TMUX_GUARD_REAL="$SCRATCH/missing-tmux" "$TOOL" -V 2>&1)"
bad_rc=$?
set -e
[[ "$bad_rc" -eq 127 && "$bad_err" == *'real tmux unavailable'* ]] \
  || fail "C4 bad real pin was not typed (rc=$bad_rc): $bad_err"

printf 'tmux-guard: C0-C4 PASS\n'
