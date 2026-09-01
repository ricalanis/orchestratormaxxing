#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "agent-scrollback: tmux"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/agent-scrollback.XXXXXX")"
REAL_TMUX="$(command -v tmux)"
export TMUX_TMPDIR="$TMP/tmux"
SOCKET="as-$$"
STUBS="$TMP/stubs"
UNAME_FILE="$TMP/uname"
mkdir -p "$TMUX_TMPDIR" "$STUBS"

cleanup() {
  while IFS= read -r session; do
    [[ -n "$session" ]] && "$REAL_TMUX" -L "$SOCKET" kill-session -t "=$session" \
      >/dev/null 2>&1 || true
  done < <("$REAL_TMUX" -L "$SOCKET" list-sessions -F '#{session_name}' 2>/dev/null || true)
  rm -rf "$TMP"
}
trap cleanup EXIT
fail() { printf 'agent-scrollback contract: %s\n' "$*" >&2; exit 1; }

cat > "$STUBS/tmux" <<'SH'
#!/bin/sh
exec "$REAL_TMUX" -L "$SOCKET" "$@"
SH
cat > "$STUBS/uname" <<'SH'
#!/bin/sh
cat "$UNAME_FILE"
SH
cat > "$STUBS/claude" <<'SH'
#!/bin/sh
if [ "${CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN:-}" != 1 ]; then
  printf '\033[?1049h'
fi
python3 - <<'PY'
for i in range(7000):
    print(f"scrollback-{i:05d}-abcdefghijklmnopqrstuvwxyz-0123456789", flush=True)
PY
sleep 30
SH
cat > "$STUBS/codex" <<'SH'
#!/bin/sh
normal=0
for arg in "$@"; do
  [ "$arg" = --no-alt-screen ] && normal=1
done
if [ "$normal" != 1 ]; then
  printf '\033[?1049h'
fi
python3 - <<'PY'
for i in range(7000):
    print(f"scrollback-{i:05d}-abcdefghijklmnopqrstuvwxyz-0123456789", flush=True)
PY
sleep 30
SH
for name in agent-tab-status warp-agent-recovery curl gpu-agent; do
  cat > "$STUBS/$name" <<'SH'
#!/bin/sh
exit 0
SH
done
chmod +x "$STUBS"/*

export REAL_TMUX SOCKET UNAME_FILE
export PATH="$STUBS:/usr/bin:/bin"
printf 'Linux\n' > "$UNAME_FILE"

# Keep the private server alive across sessions and load the production tmux
# fragment so this crosses the same history/mouse/OSC52 boundary as c/g.
"$REAL_TMUX" -L "$SOCKET" -f "$ROOT/shell/tmux.conf" \
  new-session -d -x 100 -y 20 -s keeper 'sleep 60'

wait_for_pane() {
  local session="$1" deadline=$((SECONDS + 8)) stat alt hist
  while (( SECONDS < deadline )); do
    stat="$(tmux display-message -p -t "=$session:" '#{alternate_on} #{history_size}' 2>/dev/null || true)"
    alt="${stat%% *}"
    hist="${stat##* }"
    if [[ "$alt" == 1 || "${hist:-0}" -gt 5000 ]]; then
      printf '%s\n' "$stat"
      return 0
    fi
    sleep 0.05
  done
  fail "$session did not render its deterministic transcript (last=$stat)"
}

assert_normal() {
  local session="$1" stat alt hist captured deadline
  stat="$(wait_for_pane "$session")"
  alt="${stat%% *}"; hist="${stat##* }"
  [[ "$alt" == 0 ]] || fail "$session entered alternate screen"
  [[ "$hist" -gt 5000 ]] || fail "$session retained only $hist history lines"
  # history_size crosses 5000 while the tail is still rendering on a slow runner
  # (caught live on the 2-vCPU ubuntu CI runner): poll until the last line lands.
  deadline=$((SECONDS + 10))
  while :; do
    captured="$(tmux capture-pane -p -S - -t "=$session:")"
    [[ "$captured" == *scrollback-06999-* ]] && break
    (( SECONDS < deadline )) || fail "$session lost transcript end"
    sleep 0.2
  done
  [[ "$captured" == *scrollback-00000-* ]] || fail "$session lost transcript start"
}

assert_alternate() {
  local session="$1" stat alt hist
  stat="$(wait_for_pane "$session")"
  alt="${stat%% *}"; hist="${stat##* }"
  [[ "$alt" == 1 ]] || fail "$session unexpectedly left full-screen mode"
  [[ "$hist" == 0 ]] || fail "$session alternate screen leaked $hist history lines"
}

run_fresh() {
  local launcher="$1" invocation="$2"
  env -u TMUX ROOT="$ROOT" bash --noprofile --norc -c \
    ". \"$ROOT/shell/$launcher\"; $invocation --detach" >/dev/null 2>&1
}

run_nested() {
  local session="$1" launcher="$2" invocation="$3"
  tmux new-session -d -x 100 -y 20 -s "$session" \
    bash --noprofile --norc -c ". \"$ROOT/shell/$launcher\"; $invocation"
}

# Linux is the execution host for both local c/g and Mac c-ubuntu/g-ubuntu.
run_fresh claude-c.sh 'c fresh-claude'
assert_normal claude-fresh-claude

# Cross the real tmux clipboard boundary with the complete >300 KiB transcript.
# `capture-pane` proves the source history, `load-buffer -w` emits the same OSC52
# sequence used by copy-mode, and the attached PTY is the first deterministic
# receiver outside tmux. Exact bytes at all three points prevent a viewport-only
# or payload-truncation implementation from going green.
python3 - <<'PY'
import base64
import os
import pty
import re
import select
import signal
import subprocess
import time

tmux = [os.environ["REAL_TMUX"], "-L", os.environ["SOCKET"]]
expected = subprocess.run(
    [*tmux, "capture-pane", "-p", "-S", "-", "-t", "=claude-fresh-claude:"],
    check=True, capture_output=True,
).stdout
if len(expected) < 300_000:
    raise SystemExit(f"agent-scrollback: transcript boundary too small: {len(expected)}")

pid, fd = pty.fork()
if pid == 0:
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    # This is a synthetic outer terminal client, not a tmux-inside-tmux pane.
    # Inheriting Root's TMUX/TERM_PROGRAM would make tmux wrap terminal control
    # frames for a nonexistent parent client and invalidate the wire boundary.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env.pop("TERM_PROGRAM", None)
    os.execvpe(tmux[0], [*tmux, "attach-session", "-t", "=claude-fresh-claude"], env)

client = None
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    rows = subprocess.run(
        [*tmux, "list-clients", "-F", "#{client_pid}\t#{client_name}"],
        check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    for row in rows:
        client_pid, client_name = row.split("\t", 1)
        if int(client_pid) == pid:
            client = client_name
            break
    if client:
        break
    time.sleep(0.02)
if not client:
    os.kill(pid, signal.SIGTERM)
    raise SystemExit("agent-scrollback: attached tmux client was not observable")

# Drain the initial screen draw, then request an explicit clipboard write for
# this exact client. The command also leaves a tmux buffer for byte comparison.
time.sleep(0.1)
while select.select([fd], [], [], 0)[0]:
    os.read(fd, 65536)
subprocess.run(
    [*tmux, "load-buffer", "-w", "-t", client, "-"],
    input=expected, check=True,
)
saved = subprocess.run([*tmux, "save-buffer", "-"], check=True, capture_output=True).stdout
if saved != expected:
    raise SystemExit(f"agent-scrollback: tmux buffer mismatch {len(saved)} != {len(expected)}")

wire = bytearray()
deadline = time.monotonic() + 5
pattern = re.compile(rb"\x1b\]52;[^;]*;([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)")
match = None
while time.monotonic() < deadline:
    ready, _, _ = select.select([fd], [], [], 0.1)
    if ready:
        wire.extend(os.read(fd, 1_048_576))
        match = pattern.search(wire)
        if match:
            break
if not match:
    features = subprocess.run(
        [*tmux, "display-message", "-p", "-c", client,
         "#{client_termname} #{client_termfeatures}"],
        check=False, text=True, capture_output=True,
    ).stdout.strip()
    subprocess.run([*tmux, "detach-client", "-t", client], check=False)
    raise SystemExit(
        "agent-scrollback: tmux emitted no OSC52 clipboard frame "
        f"(features={features!r}, wire={len(wire)}, marker={wire.find(b']52;')}, "
        f"head={bytes(wire[:32])!r}, tail={bytes(wire[-16:])!r})"
    )
decoded = base64.b64decode(match.group(1), validate=True)
if decoded != expected:
    raise SystemExit(f"agent-scrollback: OSC52 mismatch {len(decoded)} != {len(expected)}")

subprocess.run([*tmux, "detach-client", "-t", client], check=False)
deadline = time.monotonic() + 2
while time.monotonic() < deadline:
    done, _ = os.waitpid(pid, os.WNOHANG)
    if done:
        break
    time.sleep(0.02)
else:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(0.02)
    else:
        # Darwin's tmux client may stay blocked after TERM. Bound cleanup just
        # like the production process contracts: TERM first, then exact-child
        # KILL, never an unbounded wait or a broad process match.
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
os.close(fd)
PY

run_nested nested-claude claude-c.sh 'c nested-claude'
assert_normal nested-claude
run_fresh codex-g.sh 'g fresh-codex'
assert_normal codex-fresh-codex
run_nested nested-codex codex-g.sh 'g nested-codex'
assert_normal nested-codex

# Mac-local launch policy is deliberately unchanged: fresh Claude already used
# normal-screen output, while Codex keeps its full-screen TUI. The remote
# c-ubuntu/g-ubuntu process runs the Linux branch above on the Ubuntu host.
printf 'Darwin\n' > "$UNAME_FILE"
run_fresh claude-c.sh 'c mac-claude'
assert_normal claude-mac-claude
run_fresh codex-g.sh 'g mac-codex'
assert_alternate codex-mac-codex

printf 'agent-scrollback contract: PASS\n'
