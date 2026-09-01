#!/usr/bin/env bash
# Real-path contract for tmux-send. It drives a private tmux server so a fake
# sender cannot mask a broken pane target or submit key.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "tmux-send: tmux"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/tmux-send.XXXXXX")"
TARGET="tmux-send-$$"

export TMUX_TMPDIR="$SCRATCH"
unset TMUX

cleanup() {
  while IFS= read -r session; do
    [[ -n "$session" ]] && tmux kill-session -t "=$session" 2>/dev/null || true
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pane_text() { tmux capture-pane -p -t "=$TARGET:" 2>/dev/null || true; }

# A missing session must fail loudly. Some pane-target errors can otherwise be
# lost on stderr even when tmux reports a successful process exit.
set +e
"$ROOT/bin/tmux-send" "$TARGET-nope" "echo hi" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ] || fail "send to a nonexistent session exited 0"

# The text must physically land in a pane. The banner carries a Codex activity
# marker so tmux-send can confirm on its first observation.
tmux new-session -d -s "$TARGET" -c "$ROOT" \
  sh -c 'printf "Working (1s • esc to interrupt)\n"; exec cat'
sleep 1
tmux has-session -t "=$TARGET" 2>/dev/null || fail "could not create test session"

MARKER="TMUX-SEND-DELIVERED-$$"
set +e
"$ROOT/bin/tmux-send" "$TARGET" "$MARKER" >/dev/null 2>&1
send_rc=$?
set -e
sleep 1
got="$(pane_text)"
case "$got" in
  *"$MARKER"*) ;;
  *) fail "sent text never reached the pane (rc=$send_rc)" ;;
esac
[ "$send_rc" -eq 0 ] || fail "text arrived but sender reported failure (rc=$send_rc)"

# An OpenCode TUI pane must also be recognized on first observation. The banner
# is the empirically captured live executing state of opencode 1.17.9
# (⬝-spinner + "esc interrupt" — note: NOT Codex's "esc to interrupt"); an
# unrecognized state would fall into the C-m re-submit branch and exit 1
# (double-submit risk, lq-ab9f9bb5).
OC_TARGET="$TARGET-oc"
tmux new-session -d -s "$OC_TARGET" -c "$ROOT" \
  sh -c 'printf " %s  esc interrupt   tab agents  ctrl+p commands\n" "⬝⬝⬝⬝⬝⬝⬝⬝"; exec cat'
sleep 1
tmux has-session -t "=$OC_TARGET" 2>/dev/null || fail "could not create opencode test session"
OC_MARKER="TMUX-SEND-OPENCODE-$$"
set +e
"$ROOT/bin/tmux-send" "$OC_TARGET" "$OC_MARKER" >/dev/null 2>&1
oc_rc=$?
set -e
sleep 1
oc_got="$(tmux capture-pane -p -t "=$OC_TARGET:")"
case "$oc_got" in
  *"$OC_MARKER"*) ;;
  *) fail "opencode-marker pane never received the text (rc=$oc_rc)" ;;
esac
[ "$oc_rc" -eq 0 ] || fail "opencode executing marker not recognized (rc=$oc_rc)"
tmux kill-session -t "=$OC_TARGET" 2>/dev/null || true

printf 'tmux-send: real tmux transport verified\n'
