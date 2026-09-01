#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Resolution FIRST, then a NAMED skip. A launchd/minimal-PATH run hides an
# installed Homebrew tmux, so the helper widens PATH before concluding
# absence; only genuine absence exits 77. Before this, a tmux-less host got
# a hard red ("real tmux is required...") that loop-tick enqueued as a
# harness flaw the other machine could never reproduce (lq-6cc03e4c).
# Placed before mktemp so a skip cannot leak the scratch dir.
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "warp-recovery-shell: tmux"
TMP="$(mktemp -d)"
REAL_TMUX="$(command -v tmux || true)"
# The private server's socket dir lives INSIDE $TMP, so the EXIT trap's rm -rf
# removes the socket on pass and fail alike. Before this, every run left a dead
# socket in the global /tmp/tmux-$UID (114 counted on the Linux box, 23 on the
# Mac — lq-58557e2e): tmux never unlinks its socket file on server exit.
export TMUX_TMPDIR="$TMP"
# Short name on purpose: macOS mktemp dirs run ~64 chars deep and sun_path
# caps a socket path at 104 bytes there — the old warp-recovery-focus-$$-$RANDOM
# name could overflow it once relocated under $TMP. Uniqueness now comes from
# $TMP itself; $$ only keeps the global-dir regression guard collision-free.
# If some tmux ever ignored TMUX_TMPDIR, the socket would land in the global
# dir and the end-of-run guard reds loudly — never a silent re-leak.
PTY_TMUX_SOCKET="wrf-$$"
cleanup() {
  if [[ -n "$REAL_TMUX" ]]; then
    while IFS= read -r session; do
      [[ -n "$session" ]] && "$REAL_TMUX" -L "$PTY_TMUX_SOCKET" \
        kill-session -t "=$session" >/dev/null 2>&1 || true
    done < <("$REAL_TMUX" -L "$PTY_TMUX_SOCKET" \
      list-sessions -F '#{session_name}' 2>/dev/null || true)
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
STUBS="$TMP/stubs"
LOG="$TMP/log"
mkdir -p "$STUBS"
: > "$LOG"

fail() { printf 'warp-recovery-shell contract: %s\n' "$*" >&2; exit 1; }
[[ -f "$ROOT/shell/warp-recovery.sh" ]] || fail 'missing shell/warp-recovery.sh'

cat > "$STUBS/warp-agent-recovery" <<'SH'
#!/bin/sh
printf 'recovery %s\n' "$*" >> "$CALL_LOG"
if [ "${1:-}" = claim ] && [ -n "${CLAIM_RESULT:-}" ]; then
  printf '%s\n' "$CLAIM_RESULT"
fi
SH
cat > "$STUBS/tmux" <<'SH'
#!/bin/sh
printf 'tmux %s\n' "$*" >> "$CALL_LOG"
case "${1:-}" in
  has-session) exit 1 ;;
  display-message) printf '%%1\n' ;;
esac
exit 0
SH
for name in claude codex opencode occ agent-tab-status curl gpu-agent; do
  cat > "$STUBS/$name" <<'SH'
#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >> "$CALL_LOG"
exit 0
SH
done
chmod +x "$STUBS"/*

export PATH="$STUBS:/usr/bin:/bin"
export CALL_LOG="$LOG"
export WARP_IS_LOCAL_SHELL_SESSION=1
export WARP_TERMINAL_SESSION_UUID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# The claim-once latch is EXPORTED by shell/warp-recovery.sh:18, so a test run
# from inside a real Warp session inherits it and the claim path silently
# short-circuits at the `-z ${_WARP_AGENT_RECOVERY_CLAIMED}` gate (lq-8d3db194:
# "did not claim recovery" only in sessions whose ancestry ran the installed
# hook). A fixture is hermetic only if it strips the host state it asserts on.
unset TMUX TMUX_PANE _WARP_AGENT_RECOVERY_CLAIMED

run_launcher() {
  local launcher="$1" command="$2"
  : > "$LOG"
  ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/$launcher\"; $command" >/dev/null 2>&1
}

run_launcher claude-c.sh 'c app'
python3 - "$LOG" claude <<'PY'
import os, sys
# The launcher logs bash's logical $PWD — its case follows however the invoking
# session's cwd string was typed (dev vs Dev on this case-insensitive FS), while
# os.getcwd() returns the kernel-canonical spelling. Compare path IDENTITY, not
# strings: string equality made this contract fail only in sessions launched from
# the other case-spelling of the same directory (lq-edd04bfe, recurred 3x). And
# never use bare next() — a starved generator reports StopIteration instead of
# naming what was missing.
lines = open(sys.argv[1]).read().splitlines()
agent = sys.argv[2]
prefix = f"recovery register-launch {agent} {agent}-app "
def find(pred, what):
    hit = next((i for i, x in enumerate(lines) if pred(x)), None)
    if hit is None:
        sys.exit(f"warp-recovery-shell: {what} not in call log:\n" + "\n".join(lines))
    return hit
reg = find(lambda x: x.startswith(prefix) and os.path.exists(x[len(prefix):])
           and os.path.samefile(x[len(prefix):], os.getcwd()),
           f"{agent} register-launch for the current directory")
new = find(lambda x: x.startswith("tmux new-session "), "tmux new-session")
bind = find(lambda x: x == f"recovery bind-tmux {agent} {agent}-app", "bind-tmux")
assert reg < new < bind, lines
assert any("WARP_TERMINAL_SESSION_UUID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in x for x in lines if x.startswith("tmux new-session ")), lines
PY

run_launcher codex-g.sh 'g app'
python3 - "$LOG" codex <<'PY'
import os, sys
# Same path-identity + no-bare-next rules as the claude block above.
lines = open(sys.argv[1]).read().splitlines()
agent = sys.argv[2]
prefix = f"recovery register-launch {agent} {agent}-app "
def find(pred, what):
    hit = next((i for i, x in enumerate(lines) if pred(x)), None)
    if hit is None:
        sys.exit(f"warp-recovery-shell: {what} not in call log:\n" + "\n".join(lines))
    return hit
reg = find(lambda x: x.startswith(prefix) and os.path.exists(x[len(prefix):])
           and os.path.samefile(x[len(prefix):], os.getcwd()),
           f"{agent} register-launch for the current directory")
new = find(lambda x: x.startswith("tmux new-session "), "tmux new-session")
bind = find(lambda x: x == f"recovery bind-tmux {agent} {agent}-app", "bind-tmux")
assert reg < new < bind, lines
assert any("WARP_TERMINAL_SESSION_UUID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in x for x in lines if x.startswith("tmux new-session ")), lines
PY

run_launcher opencode-o.sh 'o app'
python3 - "$LOG" opencode <<'PY'
import os, sys
# Same path-identity + no-bare-next rules as the claude block above.
lines = open(sys.argv[1]).read().splitlines()
agent = sys.argv[2]
prefix = f"recovery register-launch {agent} {agent}-app "
def find(pred, what):
    hit = next((i for i, x in enumerate(lines) if pred(x)), None)
    if hit is None:
        sys.exit(f"warp-recovery-shell: {what} not in call log:\n" + "\n".join(lines))
    return hit
reg = find(lambda x: x.startswith(prefix) and os.path.exists(x[len(prefix):])
           and os.path.samefile(x[len(prefix):], os.getcwd()),
           f"{agent} register-launch for the current directory")
new = find(lambda x: x.startswith("tmux new-session "), "tmux new-session")
bind = find(lambda x: x == f"recovery bind-tmux {agent} {agent}-app", "bind-tmux")
assert reg < new < bind, lines
launch = "\n".join(lines[new:bind])
assert "WARP_TERMINAL_SESSION_UUID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in launch, lines
assert "session_attached" in launch and 'exec "$@"' in launch, lines
PY

# One-shot/headless and nested agent invocations must not become restorable tabs.
: > "$LOG"
ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/claude-c.sh\"; c app --prompt hi" >/dev/null 2>&1
! grep -q 'register-launch' "$LOG" || fail 'registered Claude one-shot mode'
: > "$LOG"
ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/codex-g.sh\"; g app --headless --prompt hi" >/dev/null 2>&1
! grep -q 'register-launch' "$LOG" || fail 'registered Codex headless mode'
: > "$LOG"
ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/opencode-o.sh\"; o app --prompt hi; o app --headless --prompt hi" >/dev/null 2>&1
! grep -q 'register-launch' "$LOG" || fail 'registered OpenCode one-shot/headless mode'
: > "$LOG"
TMUX=/tmp/tmux ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/claude-c.sh\"; . \"$ROOT/shell/codex-g.sh\"; . \"$ROOT/shell/opencode-o.sh\"; c app; g app; o app" >/dev/null 2>&1
! grep -q 'register-launch' "$LOG" || fail 'registered an invocation already inside tmux'

# Crash recovery enters through c/g inside the recreated tmux session. The
# wrappers must preserve the exact resume ID and their normal safety flags.
: > "$LOG"
TMUX=/tmp/tmux ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/claude-c.sh\"; c recovered --resume claude-exact-9" >/dev/null 2>&1
grep -qx 'claude --dangerously-skip-permissions --resume claude-exact-9' "$LOG" || fail 'Claude exact resume bypassed c behavior'
: > "$LOG"
TMUX=/tmp/tmux ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/codex-g.sh\"; g recovered -- -C /srv/api resume codex-exact-7" >/dev/null 2>&1
codex_resume='codex --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust'
if [[ "$(uname -s)" == "Linux" ]]; then
  codex_resume+=' --no-alt-screen'
fi
codex_resume+=' -C /srv/api resume codex-exact-7'
grep -qxF "$codex_resume" "$LOG" || fail 'Codex exact resume bypassed platform-specific g behavior'
: > "$LOG"
TMUX=/tmp/tmux ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/opencode-o.sh\"; o recovered -- -s ses-oc-42" >/dev/null 2>&1
grep -qx 'opencode -s ses-oc-42' "$LOG" || fail 'OpenCode exact resume bypassed o behavior'

# Startup recovery is ARMED by the rc file, but claim/attach must wait until
# Warp has installed its shell integration and reached precmd. It must also
# emit Warp's preexec hook before replacing the shell: without Preexec, Warp
# remains in its command editor and never forwards typed bytes to tmux.
for spec in \
  $'attach\tclaude-api:claude-api' \
  $'attach\tcodex-api:codex-api' \
  $'attach\topencode-api:opencode-api'; do
  : > "$LOG"
  result="${spec%%:*}"
  session="${spec#*:}"
  CLAIM_RESULT="$result" ROOT="$ROOT" \
    bash --noprofile --norc -ic \
      "warp_preexec() { printf '%s\\n' warp-preexec >>\"$LOG\"; }; PROMPT_COMMAND='printf \"%s\\n\" prompt-preserved >>\"$LOG\"'; . \"$ROOT/shell/warp-recovery.sh\"; printf '%s\\n' sourced >>\"$LOG\"; declare -p PROMPT_COMMAND >>\"$LOG\"; _warp_agent_recovery_on_prompt" \
      >/dev/null 2>&1 || true
  grep -qx 'sourced' "$LOG" || fail 'recovery preempted Bash rc sourcing'
  grep -q 'prompt-preserved' "$LOG" || fail 'recovery replaced an existing PROMPT_COMMAND'
  grep -q '_warp_agent_recovery_on_prompt' "$LOG" || fail 'recovery did not arm a Bash prompt hook'
  grep -qx 'recovery claim' "$LOG" || fail 'Bash prompt hook did not claim recovery'
  grep -qx "tmux attach-session -t =$session" "$LOG" || fail "did not attach exact claimed $session"
  python3 - "$LOG" "$session" <<'PY'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
assert lines.index("warp-preexec") < lines.index(f"tmux attach-session -t ={sys.argv[2]}"), lines
PY
done

# Zsh keeps user precmd hooks. Our hook must force Warp's own precmd byte frame
# before replacing the shell, because Warp appends warp_precmd after user hooks.
if command -v zsh >/dev/null 2>&1; then
  : > "$LOG"
  CLAIM_RESULT=$'attach\tcodex-zsh' ROOT="$ROOT" zsh --no-rcs -fic \
    "precmd_functions=(existing_precmd); warp_precmd() { print warp-precmd >>\"$LOG\"; }; warp_preexec() { print warp-preexec >>\"$LOG\"; }; . \"$ROOT/shell/warp-recovery.sh\"; print sourced >>\"$LOG\"; print -r -- \"\${precmd_functions[*]}\" >>\"$LOG\"; _warp_agent_recovery_on_prompt" \
    >/dev/null 2>&1 || true
  grep -qx 'sourced' "$LOG" || fail 'recovery preempted Zsh rc sourcing'
  grep -q 'existing_precmd' "$LOG" || fail 'recovery replaced an existing Zsh precmd hook'
  grep -q '_warp_agent_recovery_on_prompt' "$LOG" || fail 'recovery did not arm a Zsh prompt hook'
  python3 - "$LOG" <<'PY'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
assert lines.index("warp-precmd") < lines.index("recovery claim"), lines
assert lines.index("recovery claim") < lines.index("warp-preexec"), lines
assert lines.index("warp-preexec") < lines.index("tmux attach-session -t =codex-zsh"), lines
PY
  grep -qx 'tmux attach-session -t =codex-zsh' "$LOG" || fail 'did not attach exact Zsh recovery session'
fi

# A visible tmux attachment is not enough: the restored Warp terminal must
# complete its prompt handshake, enter command-execution mode via Preexec, and
# then deliver a real byte to the pane. The two seeded controls model both paid
# bugs: RC-time attachment and prompt-time attachment without Preexec.
[[ -n "$REAL_TMUX" ]] || fail 'real tmux is required for writable recovery contract'
PTY_DIR="$TMP/pty-contract"
PTY_STUBS="$PTY_DIR/stubs"
PTY_LOG="$PTY_DIR/events.log"
mkdir -p "$PTY_STUBS"
: > "$PTY_LOG"
cat > "$PTY_STUBS/warp-agent-recovery" <<'SH'
#!/bin/sh
if [ "${1:-}" = claim ]; then
  printf '%s\n' "$CLAIM_RESULT"
fi
SH
cat > "$PTY_STUBS/tmux" <<'SH'
#!/bin/sh
printf 'attach-attempt\n' >> "$PTY_LOG"
if ! grep -qx handshake "$PTY_LOG"; then
  printf 'rejected-before-handshake\n' >> "$PTY_LOG"
  exit 97
fi
if ! grep -qx preexec "$PTY_LOG"; then
  printf 'rejected-without-preexec\n' >> "$PTY_LOG"
  exit 98
fi
printf 'attach-after-handshake\n' >> "$PTY_LOG"
exec "$REAL_TMUX" -L "$PTY_TMUX_SOCKET" "$@"
SH
cat > "$PTY_DIR/receiver.sh" <<'SH'
#!/bin/sh
IFS= read -r line
printf 'received:%s\n' "$line" >> "$PTY_LOG"
SH
cat > "$PTY_DIR/bad.rc" <<'SH'
# Seeded pre-fix behavior: attaching while the rc file is still being sourced
# preempts Warp's first prompt handshake.
result="$(warp-agent-recovery claim)"
session="${result#attach$'\t'}"
exec tmux attach-session -t "=$session"
SH
cat > "$PTY_DIR/prompt-only.rc" <<SH
# Seeded current bug: Warp has completed precmd, but this prompt callback execs
# tmux without a Preexec hook, so Warp's editor keeps ownership of typed input.
prompt_only_attach() {
  result="\$(warp-agent-recovery claim)"
  session="\${result#attach\$'\\t'}"
  exec tmux attach-session -t "=\$session"
}
SH
cat > "$PTY_DIR/good.rc" <<SH
PROMPT_COMMAND='printf "handshake\\n" >> "$PTY_LOG"'
warp_preexec() { printf "preexec\\n" >> "$PTY_LOG"; }
. "$ROOT/shell/warp-recovery.sh"
SH
chmod +x "$PTY_STUBS/"* "$PTY_DIR/receiver.sh"

export REAL_TMUX PTY_TMUX_SOCKET PTY_LOG
PTY_PATH="$PTY_STUBS:/usr/bin:/bin"
export PTY_PATH

# Keeper session (lq-58557e2e, second flake shape): each phase kills its only
# session, which brings the private server down, and the next phase's
# new-session can then connect to the still-exiting server — tmux answers
# "server exited unexpectedly" and set -e turns that timing race into a
# phantom red (observed 1/12 under a concurrent harness-verify pass). The
# keeper holds the server alive across all three phases, so no phase ever
# starts against a dying server; cleanup later drains only this private server.
# Prevention,
# not retry — no timing budget to tune.
"$REAL_TMUX" -L "$PTY_TMUX_SOCKET" new-session -d -s wrf-keeper 'sleep 600'

"$REAL_TMUX" -L "$PTY_TMUX_SOCKET" new-session -d -s codex-bad \
  "$PTY_DIR/receiver.sh"
CLAIM_RESULT=$'attach\tcodex-bad' python3 - "$PTY_DIR/bad.rc" <<'PY'
import os
import pty
import sys
import time

rc = sys.argv[1]
env = os.environ.copy()
env["PATH"] = env["PTY_PATH"]
env["TERM"] = "xterm-256color"
pid, fd = pty.fork()
if pid == 0:
    os.execve("/bin/bash", ["bash", "--noprofile", "--rcfile", rc, "-i"], env)
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        done, status = pid, 0
    if done:
        break
    time.sleep(0.02)
else:
    os.kill(pid, 15)
    os.waitpid(pid, 0)
os.close(fd)
PY
grep -qx 'rejected-before-handshake' "$PTY_LOG" \
  || fail 'seeded RC-time attach was not rejected before Warp handshake'
! grep -q '^received:' "$PTY_LOG" \
  || fail 'seeded RC-time attach unexpectedly delivered pane input'
"$REAL_TMUX" -L "$PTY_TMUX_SOCKET" kill-session -t =codex-bad >/dev/null 2>&1 || true

: > "$PTY_LOG"
"$REAL_TMUX" -L "$PTY_TMUX_SOCKET" new-session -d -s codex-prompt-only \
  "$PTY_DIR/receiver.sh"
printf 'handshake\n' >> "$PTY_LOG"
set +e
CLAIM_RESULT=$'attach\tcodex-prompt-only' PATH="$PTY_PATH" \
  bash --noprofile --norc -c \
    ". \"$PTY_DIR/prompt-only.rc\"; prompt_only_attach" \
    >/dev/null 2>&1
prompt_only_rc=$?
set -e
[[ "$prompt_only_rc" == 98 ]] \
  || fail "seeded prompt-time attach returned $prompt_only_rc instead of 98"
grep -qx 'rejected-without-preexec' "$PTY_LOG" \
  || fail 'seeded prompt-time attach was not rejected without Warp Preexec'
! grep -q '^received:' "$PTY_LOG" \
  || fail 'seeded prompt-time attach unexpectedly delivered pane input'
"$REAL_TMUX" -L "$PTY_TMUX_SOCKET" kill-session -t =codex-prompt-only >/dev/null 2>&1 || true

: > "$PTY_LOG"
"$REAL_TMUX" -L "$PTY_TMUX_SOCKET" new-session -d -s codex-good \
  "$PTY_DIR/receiver.sh"
CLAIM_RESULT=$'attach\tcodex-good' python3 - "$PTY_DIR/good.rc" <<'PY'
import os
import pty
import signal
import subprocess
import sys
import time

rc = sys.argv[1]
env = os.environ.copy()
env["PATH"] = env["PTY_PATH"]
env["TERM"] = "xterm-256color"
pid, fd = pty.fork()
if pid == 0:
    os.execve("/bin/bash", ["bash", "--noprofile", "--rcfile", rc, "-i"], env)
os.set_blocking(fd, False)
pty_output = bytearray()
fd_open = True

def drain_pty():
    if not fd_open:
        return
    while True:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            return
        if not chunk:
            return
        pty_output.extend(chunk)

def shutdown_child():
    """Deterministic teardown (lq-58557e2e): close the pty master FIRST — the
    tmux client exits on its own once its terminal is gone — then escalate
    TERM→KILL with budgets sized for a loaded machine. The old shape (TERM 1s,
    KILL 1s, raise) flaked under a concurrent harness-verify pass, and because
    it ran AFTER the real assertions had passed, the red it produced was pure
    artifact — the exact phantom-flaw generator signal-vs-artifact warns about.
    Returns True once the child is reaped; False only for a child that
    survived SIGKILL for 10s — a kernel-level anomaly (D-state), not load
    noise, and the one teardown state deliberately still worth a red on the
    success path (the diagnostic paths stay best-effort so a cleanup hiccup
    can never mask a real failure message)."""
    global fd_open
    drain_pty()
    if fd_open:
        os.close(fd)
        fd_open = False
    for sig, budget in ((None, 2.0), (signal.SIGTERM, 3.0), (signal.SIGKILL, 10.0)):
        if sig is not None:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return True
        until = time.monotonic() + budget
        while time.monotonic() < until:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if done:
                return True
            time.sleep(0.02)
    return False

deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    drain_pty()
    clients = subprocess.run(
        [env["REAL_TMUX"], "-L", env["PTY_TMUX_SOCKET"], "list-clients"],
        capture_output=True,
        text=True,
    )
    if clients.returncode == 0 and clients.stdout.strip():
        break
    time.sleep(0.02)
else:
    # Diagnostic BEFORE teardown: even if an outer budget kills the contract
    # mid-teardown, the specific reason has already left the process. Teardown
    # itself stays best-effort — it must never mask this message.
    drain_pty()
    detail = bytes(pty_output[-1000:]).decode("utf-8", "replace")
    print(f"writable recovery contract: tmux client never attached: {detail!r}",
          file=sys.stderr, flush=True)
    shutdown_child()
    raise SystemExit(1)

os.write(fd, b"probe\n")
log = env["PTY_LOG"]
while time.monotonic() < deadline:
    drain_pty()
    if os.path.exists(log) and "received:probe" in open(log, encoding="utf-8").read():
        break
    time.sleep(0.02)
else:
    # Diagnostic BEFORE teardown: even if an outer budget kills the contract
    # mid-teardown, the specific reason has already left the process. Teardown
    # itself stays best-effort — it must never mask this message.
    drain_pty()
    detail = bytes(pty_output[-1000:]).decode("utf-8", "replace")
    print(f"writable recovery contract: probe did not reach pane: {detail!r}",
          file=sys.stderr, flush=True)
    shutdown_child()
    raise SystemExit(1)

if not shutdown_child():
    raise SystemExit("writable recovery contract: tmux client survived SIGKILL (unreapable)")
PY
python3 - "$PTY_LOG" <<'PY'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
assert lines.index("handshake") < lines.index("attach-attempt"), lines
assert lines.index("handshake") < lines.index("preexec"), lines
assert lines.index("preexec") < lines.index("attach-attempt"), lines
assert lines.index("attach-after-handshake") < lines.index("received:probe"), lines
PY

: > "$LOG"
CLAIM_RESULT=$'attach-ubuntu\tc\tclaude-remote-safe' ROOT="$ROOT" \
  bash --noprofile --norc -ic \
    "warp_preexec() { printf '%s\\n' warp-preexec >>\"$LOG\"; }; . \"$ROOT/shell/warp-recovery.sh\"; printf '%s\\n' sourced >>\"$LOG\"; _warp_agent_recovery_on_prompt" \
    >/dev/null 2>&1 || true
grep -qx 'sourced' "$LOG" || fail 'Ubuntu recovery preempted Bash rc sourcing'
grep -qx 'gpu-agent attach c claude-remote-safe' "$LOG" \
  || fail 'Ubuntu recovery did not use the exact gpu-agent attach path'
python3 - "$LOG" <<'PY'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
assert lines.index("warp-preexec") < lines.index("gpu-agent attach c claude-remote-safe"), lines
PY
! grep -q '^tmux attach-session' "$LOG" || fail 'Ubuntu recovery attached local tmux'

for variant in noninteractive nested nonwarp; do
  : > "$LOG"
  case "$variant" in
    noninteractive) ROOT="$ROOT" bash --noprofile --norc -c ". \"$ROOT/shell/warp-recovery.sh\"" >/dev/null 2>&1 ;;
    nested) TMUX=/tmp/tmux ROOT="$ROOT" bash --noprofile --norc -ic ". \"$ROOT/shell/warp-recovery.sh\"" >/dev/null 2>&1 ;;
    nonwarp) WARP_IS_LOCAL_SHELL_SESSION= ROOT="$ROOT" bash --noprofile --norc -ic ". \"$ROOT/shell/warp-recovery.sh\"" >/dev/null 2>&1 ;;
  esac
  ! grep -q '_warp_agent_recovery_on_prompt' "$LOG" || fail "$variant shell armed recovery"
  ! grep -q 'recovery claim' "$LOG" || fail "$variant shell attempted recovery"
done

# Socket-leak regression guard (lq-58557e2e): the private server must never
# have bound in the GLOBAL tmux dir — its socket lives under TMUX_TMPDIR
# ($TMP) and dies with the EXIT trap's rm -rf on pass and fail alike.
[[ ! -e "/tmp/tmux-$(id -u)/$PTY_TMUX_SOCKET" ]] \
  || fail "private tmux socket leaked into /tmp/tmux-$(id -u)/$PTY_TMUX_SOCKET"

printf 'warp-recovery-shell contract: PASS\n'
