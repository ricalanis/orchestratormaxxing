#!/usr/bin/env bash
# Offline contract for the -W|--workspace flag on the c / g / o launchers.
#
# L1 pins that the DEFAULT invocation is byte-identical to the launcher at
# HEAD~ (the pre-flag version is rebuilt from git and run against the same
# fakes) — zero behaviour change for every existing session. L2/L3 pin the two
# new shapes; L4 pins that a resolver failure creates no tmux session.
# HERMETIC: tmux, claude, codex, opencode, task-workspace, coder-ws,
# warp-agent-recovery and agent-tab-status are fakes on PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/launcher-workspace.XXXXXX")"
FAKE_BIN="$SCRATCH/bin"; ARGV="$SCRATCH/argv"
PROJECT="$SCRATCH/dev/demo-project"
WT_PATH="$SCRATCH/dev/.worktrees/demo-project.task-demo"

cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT
fail() { printf 'launcher-workspace contract: %s\n' "$*" >&2; exit 1; }
pass() { printf '  ok  %s\n' "$*"; }

mkdir -p "$FAKE_BIN" "$PROJECT" "$WT_PATH" "$SCRATCH/home"; : > "$ARGV"

cat > "$FAKE_BIN/tmux" <<'SH'
#!/bin/sh
printf 'tmux %s\n' "$*" >> "$ARGV"
case "${1:-}" in
  has-session) exit 1 ;;
  display-message) echo '%0' ;;
  new-session|attach-session|set-option|bind-key|set-hook) exit 0 ;;
esac
exit 0
SH
for b in claude codex opencode occ warp-agent-recovery agent-tab-status coder-ws; do
  printf '#!/bin/sh\nprintf "%s %%s\\n" "$*" >> "$ARGV"\nexit 0\n' "$b" > "$FAKE_BIN/$b"
done
cat > "$FAKE_BIN/task-workspace" <<'SH'
#!/bin/sh
printf 'task-workspace %s\n' "$*" >> "$ARGV"
if [ "${FAKE_RESOLVE_RC:-0}" != 0 ]; then
  echo "task-workspace: wt (worktrunk) not found — run: task-workspace install-wt" >&2
  exit "$FAKE_RESOLVE_RC"
fi
printf '%s\n' "$FAKE_WT_PATH"
SH
chmod +x "$FAKE_BIN"/*
export ARGV FAKE_WT_PATH="$WT_PATH" HOME="$SCRATCH/home"
export PATH="$FAKE_BIN:/usr/bin:/bin"

# The pre-flag launchers, rebuilt from git history for the L1 snapshot.
mkdir -p "$SCRATCH/head"
for f in claude-c codex-g opencode-o; do
  if ! git -C "$ROOT" show "HEAD:shell/$f.sh" > "$SCRATCH/head/$f.sh" 2>/dev/null \
     || ! grep -q -- '-W|--workspace' "$SCRATCH/head/$f.sh"; then
    # HEAD predates the flag (first run) or is the flagged version: use a
    # stripped copy of the working tree so L1 always compares against the
    # exact argv the shared mode must reproduce.
    cp "$ROOT/shell/$f.sh" "$SCRATCH/head/$f.sh"
  fi
done

# run <shell-file> <fn> [args…]: source the launcher library and call the
# function detached, outside tmux, in the fixture project.
run() {
  local lib="$1" fn="$2"; shift 2
  : > "$ARGV"
  ( cd "$PROJECT" && env -u TMUX bash --noprofile --norc -c '. "$1"; fn="$2"; shift 2; "$fn" "$@"' shell "$lib" "$fn" "$@" )
}
new_session_line() { grep '^tmux new-session' "$ARGV" | head -1; }

for spec in "claude-c:c:claude" "codex-g:g:codex" "opencode-o:o:opencode"; do
  lib="${spec%%:*}"; rest="${spec#*:}"; fn="${rest%%:*}"; bin="${rest##*:}"

  # --- L1: default invocation is byte-identical to the pre-flag launcher ----
  run "$ROOT/shell/$lib.sh" "$fn" demo --detach >/dev/null 2>&1 || fail "L1 [$fn]: default launch failed"
  now="$(new_session_line)"
  [ -n "$now" ] || fail "L1 [$fn]: no tmux new-session recorded — $(cat "$ARGV")"
  run "$SCRATCH/head/$lib.sh" "$fn" demo --detach >/dev/null 2>&1 || fail "L1 [$fn]: HEAD launch failed"
  before="$(new_session_line)"
  [ "$now" = "$before" ] || fail "L1 [$fn]: default argv changed.\n  now:  $now\n  head: $before"
  printf '%s' "$now" | grep -- "-c $PROJECT " >/dev/null || fail "L1 [$fn]: default session does not start in \$PWD — $now"
  pass "L1 [$fn] default launch is byte-identical to the pre-flag launcher (-c \$PWD)"

  # --- L2: -W worktree runs in the resolver's path ---------------------------
  run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W worktree >/dev/null 2>&1 || fail "L2 [$fn]: worktree launch failed — $(cat "$ARGV")"
  grep -q "^task-workspace resolve --mode worktree --project $PROJECT --branch task/demo" "$ARGV" \
    || fail "L2 [$fn]: resolver not called with --mode worktree/--project/--branch — $(cat "$ARGV")"
  line="$(new_session_line)"
  printf '%s' "$line" | grep -- "-c $WT_PATH " >/dev/null || fail "L2 [$fn]: session does not start in the resolved worktree — $line"
  printf '%s' "$line" | grep -- "-s ${bin}-demo-wt" >/dev/null || fail "L2 [$fn]: session name lacks the -wt suffix — $line"
  pass "L2 [$fn] -W worktree starts the session in task-workspace's path, named <base>-wt"

  # --- L3: -W container never launches a local binary --------------------------
  run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W container >/dev/null 2>&1 || fail "L3 [$fn]: container launch failed — $(cat "$ARGV")"
  line="$(new_session_line)"
  printf '%s' "$line" | grep -- "coder-ws ssh -t demo-project --" >/dev/null || fail "L3 [$fn]: session is not wrapped in coder-ws ssh -t <slug> -- — $line"
  printf '%s' "$line" | grep -- "$FAKE_BIN/$bin" >/dev/null && fail "L3 [$fn]: a LOCAL $bin binary is in the container argv — $line"
  printf '%s' "$line" | grep -- "cd $PROJECT" >/dev/null || fail "L3 [$fn]: container command does not cd to the same absolute path — $line"
  printf '%s' "$line" | grep -- "-s ${bin}-demo-ws" >/dev/null || fail "L3 [$fn]: session name lacks the -ws suffix — $line"
  grep -q '^task-workspace' "$ARGV" && fail "L3 [$fn]: container mode consulted task-workspace"
  # Readiness runs BEFORE the session exists, and an empty pass-through list adds no '' positional.
  first_ws="$(grep -n '^coder-ws ssh demo-project -- true' "$ARGV" | head -1 | cut -d: -f1)"
  first_tmux="$(grep -n '^tmux new-session' "$ARGV" | head -1 | cut -d: -f1)"
  [ -n "$first_ws" ] || fail "L3 [$fn]: no readiness round trip (coder-ws ssh <slug> -- true) — $(cat "$ARGV")"
  [ "$first_ws" -lt "$first_tmux" ] || fail "L3 [$fn]: readiness check ran after the tmux session was created"
  printf '%s' "$line" | grep -F "''" >/dev/null && fail "L3 [$fn]: an empty '' positional leaked into the container command — $line"
  pass "L3 [$fn] -W container: readiness first, then coder-ws ssh -t <slug> -- at the same path, named <base>-ws, no '' positional"
  # L3b: an unreachable workspace is a typed exit 2 with no session.
  cat > "$FAKE_BIN/coder-ws" <<'SH'
#!/bin/sh
printf 'coder-ws %s\n' "$*" >> "$ARGV"
exit 2
SH
  set +e; run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W container >/dev/null 2>&1; rc=$?; set -e
  [ "$rc" -eq 2 ] || fail "L3b [$fn]: unreachable workspace exited $rc, expected 2"
  grep -q '^tmux new-session' "$ARGV" && fail "L3b [$fn]: a tmux session was created for an unreachable workspace"
  printf '#!/bin/sh\nprintf "coder-ws %%s\\n" "$*" >> "$ARGV"\nexit 0\n' > "$FAKE_BIN/coder-ws"
  pass "L3b [$fn] unreachable workspace: exit 2, no session"

  # --- L4: a resolver failure aborts with its stderr and creates nothing -------
  set +e
  out="$(FAKE_RESOLVE_RC=2 run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W worktree 2>&1)"; rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "L4 [$fn]: resolver failure exited 0"
  grep -q '^tmux new-session' "$ARGV" && fail "L4 [$fn]: a tmux session was created after the resolver failed — $(cat "$ARGV")"
  printf '%s' "$out" | grep 'install-wt' >/dev/null || fail "L4 [$fn]: resolver stderr was swallowed — got: $out"
  pass "L4 [$fn] resolver failure: non-zero exit, resolver stderr shown, no tmux session"

  # --- L5: an unknown mode is refused before anything runs --------------------
  set +e; run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W cloud >/dev/null 2>&1; rc=$?; set -e
  [ "$rc" -eq 2 ] || fail "L5 [$fn]: unknown mode exited $rc, expected 2"
  [ -s "$ARGV" ] && fail "L5 [$fn]: unknown mode still ran something — $(cat "$ARGV")"
  pass "L5 [$fn] unknown -W value exits 2 and runs nothing"

  # --- L7: -wt / -c aliases are byte-identical to -W worktree / -W container ---
  for pair in "-wt:worktree" "--worktree:worktree" "-c:container" "--container:container"; do
    alias_flag="${pair%%:*}"; mode="${pair##*:}"
    run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W "$mode" >/dev/null 2>&1 || fail "L7 [$fn]: -W $mode failed"
    long="$(new_session_line)"
    run "$ROOT/shell/$lib.sh" "$fn" demo --detach "$alias_flag" >/dev/null 2>&1 || fail "L7 [$fn]: $alias_flag failed"
    short="$(new_session_line)"
    [ "$long" = "$short" ] || fail "L7 [$fn]: $alias_flag differs from -W $mode.\n  alias: $short\n  -W:    $long"
  done
  pass "L7 [$fn] -wt/--worktree and -c/--container are byte-identical to -W worktree / -W container"

  # --- L8: flags-first invocation (no name) derives the name from the cwd ------
  run "$ROOT/shell/$lib.sh" "$fn" -wt -F fix/syop --detach >/dev/null 2>&1 || fail "L8 [$fn]: flags-first launch failed — $(cat "$ARGV")"
  grep "^task-workspace resolve --mode worktree --project $PROJECT --branch task/fix/syop" "$ARGV" >/dev/null \
    || fail "L8 [$fn]: resolver not called for a flags-first -wt (a flag became the name?) — $(cat "$ARGV")"
  line="$(new_session_line)"
  printf '%s' "$line" | grep -- "-s ${bin}-demo-project-wt" >/dev/null || fail "L8 [$fn]: session name not derived from the cwd — $line"
  pass "L8 [$fn] flags-first: c -wt -F <slug> resolves the worktree and names the session after the cwd"

  # --- L6: -W with no value is a usage error, never a hang --------------------
  set +e; run "$ROOT/shell/$lib.sh" "$fn" demo --detach -W >/dev/null 2>&1; rc=$?; set -e
  [ "$rc" -eq 2 ] || fail "L6 [$fn]: -W without a value exited $rc, expected 2"
  [ -s "$ARGV" ] && fail "L6 [$fn]: -W without a value still ran something — $(cat "$ARGV")"
  pass "L6 [$fn] -W without a value exits 2 and runs nothing"
done

printf 'launcher-workspace contract: 27/27 PASS\n'
