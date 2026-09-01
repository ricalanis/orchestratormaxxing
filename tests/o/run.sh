#!/usr/bin/env bash
# Real-path contract for the public `o` worker runtime. It uses a private tmux
# server and a fake OpenCode TUI, but the real o/tmux-send transport.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
O="$ROOT/bin/o"
[[ -x "$O" ]] || { printf 'o-runtime: public bin/o missing\n' >&2; exit 1; }
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "o-runtime: tmux"
TMUX_BIN="$(command -v tmux)"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/o-runtime.XXXXXX")"
STUBS="$SCRATCH/bin"
RUN_DIR="$ROOT/.results/delegation/o-runtime-$$"
PROFILE_RUN_DIR="$ROOT/.results/delegation/o-runtime-profile-$$"
DEFERRED_RUN_DIR="$ROOT/.results/delegation/o-runtime-deferred-$$"
ENGLISH_RUN_DIR="$ROOT/.results/delegation/o-runtime-english-deferred-$$"
MALFORMED_RUN_DIR="$ROOT/.results/delegation/o-runtime-malformed-$$"
MARKED_RUN_DIR="$ROOT/.results/delegation/o-runtime-marked-$$"
mkdir -p "$STUBS" "$RUN_DIR" "$PROFILE_RUN_DIR" "$DEFERRED_RUN_DIR" \
  "$ENGLISH_RUN_DIR" "$MALFORMED_RUN_DIR" "$MARKED_RUN_DIR"
export TMUX_TMPDIR="$SCRATCH/tmux"
mkdir -p "$TMUX_TMPDIR"
unset TMUX TMUX_PANE

cleanup() {
  while IFS= read -r session; do
    [[ -n "$session" ]] && tmux kill-session -t "=$session" 2>/dev/null || true
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
  rm -rf "$SCRATCH" "$RUN_DIR" "$PROFILE_RUN_DIR" "$DEFERRED_RUN_DIR" \
    "$ENGLISH_RUN_DIR" "$MALFORMED_RUN_DIR" "$MARKED_RUN_DIR"
}
trap cleanup EXIT
fail() { printf 'o-runtime: %s\n' "$*" >&2; exit 1; }

ln -s "$ROOT/tests/o/fake-opencode.py" "$STUBS/opencode"
ln -s "$TMUX_BIN" "$STUBS/tmux"
cat > "$STUBS/agent-tab-status" <<'SH'
#!/bin/sh
if [ -n "${OPENCODE_STARTUP_MARKER:-}" ]; then
  i=0
  while [ ! -f "$OPENCODE_STARTUP_MARKER" ] && [ "$i" -lt 100 ]; do
    sleep 0.05
    i=$((i + 1))
  done
fi
exit 0
SH
chmod +x "$STUBS/agent-tab-status"

export PATH="$STUBS:$ROOT/bin:/usr/bin:/bin"
export CLAUDEMAXXING_O_SHELL="${O_SHELL_UNDER_TEST:-$ROOT/shell/opencode-o.sh}"
export O_READY_TIMEOUT_SECONDS=5
export OPENCODE_ARGS_LOG="$SCRATCH/opencode-args.log"

# OpenTUI queries terminal capabilities during process birth. Starting it in a
# detached tmux pane loses those replies before the human client attaches and
# leaves a permanently blank screen. Exercise the real tmux boundary through a
# real PTY: the fake exits red unless a client already exists at process birth.
startup_capture="$SCRATCH/startup-capture.log"
export OPENCODE_STARTUP_MARKER="$SCRATCH/startup-born"
if ! python3 - "$CLAUDEMAXXING_O_SHELL" "$startup_capture" <<'PY'
import errno
import os
import pty
import sys

launcher, capture = sys.argv[1:]
pid, fd = pty.fork()
if pid == 0:
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    os.execve(
        "/bin/bash",
        [
            "bash",
            "-c",
            'source "$1"; o startup-probe -- --startup-probe',
            "_",
            launcher,
        ],
        env,
    )

chunks = []
while True:
    try:
        chunk = os.read(fd, 65536)
    except OSError as exc:
        if exc.errno == errno.EIO:
            break
        raise
    if not chunk:
        break
    chunks.append(chunk)
_, status = os.waitpid(pid, 0)
with open(capture, "wb") as stream:
    stream.write(b"".join(chunks))
if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
    raise SystemExit(1)
PY
then
  fail 'interactive OpenCode process was born without an attached tmux client'
fi
grep -Fq 'STARTUP-ATTACHED:1' "$startup_capture" \
  || fail 'interactive OpenCode started before the tmux client attached'
if [[ "${O_STARTUP_ONLY:-0}" == "1" ]]; then
  printf 'o-startup: PASS\n'
  exit 0
fi

# A human-started interactive session keeps OpenCode's normal permission mode.
# Auto-approval belongs only to sessions born through `o delegate`.
bash -c 'source "$1"; CLAUDEMAXXING_HARNESS_CHILD=1 TMUX=1 o interactive-control --agent glm-coder </dev/null' \
  _ "$ROOT/shell/opencode-o.sh" >/dev/null
grep -Fxq -- '--agent glm-coder' "$OPENCODE_ARGS_LOG" \
  || fail 'ordinary interactive OpenCode launch did not preserve normal permission mode'
! grep -Fxq -- '--agent glm-coder --auto' "$OPENCODE_ARGS_LOG" \
  || fail 'ordinary interactive OpenCode launch gained delegation-only auto mode'

# Contract-at-birth: no session may be created while either immutable input is
# missing. This ordering is the regression target, not just the final error.
set +e
missing_json="$($O delegate contract-first --agent glm-coder --run-dir "$RUN_DIR" --json 2>/dev/null)"
missing_rc=$?
set -e
[[ "$missing_rc" -eq 2 ]] || fail "missing contract returned rc=$missing_rc, wanted 2"
[[ "$missing_json" == *'"status":"invalid_contract"'* ]] || fail "missing contract lacks typed state"
! tmux has-session -t '=opencode-contract-first' 2>/dev/null || fail 'session existed before contract-at-birth cleared'

printf '# Acceptance\nA1 must pass.\n' > "$RUN_DIR/contract.md"
printf '# Brief\nWork only in the current project.\n' > "$RUN_DIR/brief.md"
chmod a-w "$RUN_DIR/contract.md" "$RUN_DIR/brief.md"
cp "$RUN_DIR/contract.md" "$PROFILE_RUN_DIR/contract.md"
cp "$RUN_DIR/brief.md" "$PROFILE_RUN_DIR/brief.md"
chmod a-w "$PROFILE_RUN_DIR/contract.md" "$PROFILE_RUN_DIR/brief.md"

# The exact failed Acme pattern is not a task: Root expected delegate turn 1
# to read a project-wide brief and wait for a later module assignment. That
# must be rejected before OpenCode/tmux exists, otherwise the model can choose
# arbitrary work from the broad context and stay pending for an hour.
printf '# Acceptance\nA1 must pass.\n' > "$DEFERRED_RUN_DIR/contract.md"
cat > "$DEFERRED_RUN_DIR/brief.md" <<'EOF'
# Brief — implementación R0

## Protocolo de sesión
Tu PRIMER turno termina en cuanto hayas leído brief.md y contract.md: responde
únicamente LISTO; espera la tarea concreta en el SIGUIENTE mensaje.

## Misión
Construir el producto completo.
EOF
chmod a-w "$DEFERRED_RUN_DIR/contract.md" "$DEFERRED_RUN_DIR/brief.md"
args_before="$(wc -l < "$OPENCODE_ARGS_LOG")"
set +e
deferred="$($O delegate deferred-turn1 --agent glm-coder --run-dir "$DEFERRED_RUN_DIR" --json 2>/dev/null)"
deferred_rc=$?
set -e
[[ "$deferred_rc" -eq 2 ]] || fail "deferred turn-1 brief returned rc=$deferred_rc, wanted 2"
[[ "$deferred" == *'"status":"invalid_turn1_task"'* ]] \
  || fail 'deferred turn-1 brief lacks typed refusal'
! tmux has-session -t '=opencode-deferred-turn1' 2>/dev/null \
  || fail 'deferred turn-1 brief created a worker before refusal'
args_after="$(wc -l < "$OPENCODE_ARGS_LOG")"
[[ "$args_before" == "$args_after" ]] \
  || fail 'deferred turn-1 brief invoked OpenCode before refusal'

# The compatibility detector covers the same bootstrap misuse in English.
printf '# Acceptance\nA1 must pass.\n' > "$ENGLISH_RUN_DIR/contract.md"
cat > "$ENGLISH_RUN_DIR/brief.md" <<'EOF'
# Context
Your first turn only reads brief.md and contract.md. Wait for the concrete task
in the next message.
EOF
chmod a-w "$ENGLISH_RUN_DIR/contract.md" "$ENGLISH_RUN_DIR/brief.md"
set +e
english="$($O delegate english-deferred --agent glm-coder --run-dir "$ENGLISH_RUN_DIR" --json 2>/dev/null)"
english_rc=$?
set -e
[[ "$english_rc" -eq 2 && "$english" == *'"status":"invalid_turn1_task"'* ]] \
  || fail 'English delayed assignment was accepted'
! tmux has-session -t '=opencode-english-deferred' 2>/dev/null \
  || fail 'English delayed assignment created a worker'

# Markers are a physical schema, so duplicates/misordering fail closed before
# OpenCode starts rather than letting a model guess which assignment is real.
printf '# Acceptance\nA1 must pass.\n' > "$MALFORMED_RUN_DIR/contract.md"
cat > "$MALFORMED_RUN_DIR/brief.md" <<'EOF'
<!-- o-delegate-turn-1:begin -->
Task A.
<!-- o-delegate-turn-1:begin -->
Task B.
<!-- o-delegate-turn-1:end -->
EOF
chmod a-w "$MALFORMED_RUN_DIR/contract.md" "$MALFORMED_RUN_DIR/brief.md"
set +e
malformed="$($O delegate malformed-turn1 --agent glm-coder --run-dir "$MALFORMED_RUN_DIR" --json 2>/dev/null)"
malformed_rc=$?
set -e
[[ "$malformed_rc" -eq 2 && "$malformed" == *'"status":"invalid_turn1_task"'* ]] \
  || fail 'duplicate turn-1 marker was accepted'
! tmux has-session -t '=opencode-malformed-turn1' 2>/dev/null \
  || fail 'malformed marker created a worker'

# A broad context is safe when the exact immediate assignment is physically
# delimited. The receipt attests that mode, and turn 1 completes normally.
printf '# Acceptance\nA1 must pass.\n' > "$MARKED_RUN_DIR/contract.md"
cat > "$MARKED_RUN_DIR/brief.md" <<'EOF'
# Project context
The repository contains many independent modules.

<!-- o-delegate-turn-1:begin -->
Inspect only the current project and return one bounded handoff. Do not wait for
another task or choose another module.
<!-- o-delegate-turn-1:end -->
EOF
chmod a-w "$MARKED_RUN_DIR/contract.md" "$MARKED_RUN_DIR/brief.md"
marked="$($O delegate marked-turn1 --agent glm-coder --run-dir "$MARKED_RUN_DIR" --json)"
python3 - "$marked" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == "sent" and row["turn1_mode"] == "marked", row
PY
sleep 1
marked_handoff="$($O handoff opencode-marked-turn1 --timeout 5 --json)"
[[ "$marked_handoff" == *'"status":"completed_retrievable"'* ]] \
  || fail 'valid marked turn-1 task did not complete'
$O close opencode-marked-turn1 --json >/dev/null

delegated="$($O delegate contract-first --agent glm-coder --run-dir "$RUN_DIR" --json)"
python3 - "$delegated" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == "sent", row
assert row["session"] == "opencode-contract-first", row
assert row["attach"] == "o ls opencode-contract-first", row
assert row["handoff"] == "o handoff opencode-contract-first", row
assert row["turn1_mode"] == "legacy", row
assert len(row["contract_sha256"]) == 64, row
assert len(row["brief_sha256"]) == 64, row
PY
grep -Fxq -- '--agent glm-coder --auto' "$OPENCODE_ARGS_LOG" \
  || fail 'delegated OpenCode worker did not launch in auto permission mode'

# Canonical profiles are resolved by the shared Ollama policy before OpenCode
# starts. The receipt surface attests both the selected agent and exact model.
profiled="$($O delegate profile-route --profile bounded-code --run-dir "$PROFILE_RUN_DIR" --json)"
python3 - "$profiled" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == "sent", row
assert row["profile"] == "bounded-code", row
assert row["agent"] == "kimi-coder", row
assert row["model"] == "kimi-k2.7-code", row
assert row["selection_source"] == "profile", row
PY
sleep 1
grep -Fxq -- '--agent kimi-coder --auto' "$OPENCODE_ARGS_LOG" \
  || fail 'profile selected worker did not launch in auto permission mode'
$O close opencode-profile-route --json >/dev/null

# A caller can still name a custom installed agent, but cannot combine that
# override with a canonical profile or invoke a legacy model-shaped agent.
set +e
collision="$($O delegate collision --profile reasoning --agent custom-coder --run-dir "$RUN_DIR" --json 2>/dev/null)"
collision_rc=$?
legacy="$($O delegate legacy --agent kimi-k2.6-coder --run-dir "$RUN_DIR" --json 2>/dev/null)"
legacy_rc=$?
set -e
[[ "$collision_rc" -eq 2 && "$collision" == *'"status":"invalid_route"'* ]] || fail 'profile/agent collision was accepted'
[[ "$legacy_rc" -eq 2 && "$legacy" == *'"status":"legacy_model"'* ]] || fail 'legacy stateful agent was accepted'
! tmux has-session -t '=opencode-collision' 2>/dev/null || fail 'collision created a worker'
! tmux has-session -t '=opencode-legacy' 2>/dev/null || fail 'legacy route created a worker'
tmux has-session -t '=opencode-contract-first' 2>/dev/null || fail 'delegated session is not attachable'
sleep 1
pane="$(tmux capture-pane -p -t '=opencode-contract-first:')"
pane_compact="$(printf '%s' "$pane" | tr -d '\n')"
[[ "$pane_compact" == *'TURN-1-IDLE'* ]] || fail 'initial turn never reached the durable idle event'

# Durable handoff is event-bound, not reconstructed from alternate-screen
# pixels. The fake emits no post-turn "Ask anything", so this is a negative
# fixture for the literal-placeholder readiness bug.
handoff="$($O handoff opencode-contract-first --timeout 5 --json)"
python3 - "$handoff" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == "completed_retrievable", row
assert row["turn"] == 1 and row["text"] == "FINAL-TURN-1", row
assert row["opencode_session"].startswith("ses_fake"), row
PY

# Follow-up stays in the same session and uses the literal-safe tmux-send path.
before="$(tmux list-sessions -F '#{session_name}' | wc -l)"
sent="$($O send opencode-contract-first --prompt 'repair literal $HOME Enter' --json)"
after="$(tmux list-sessions -F '#{session_name}' | wc -l)"
[[ "$before" == "$after" ]] || fail 'repair spawned another OpenCode session'
[[ "$sent" == *'"status":"sent"'* ]] || fail 'send lacks typed success'
sleep 1
pane="$(tmux capture-pane -p -t '=opencode-contract-first:')"
[[ "$pane" == *'TURN-2-IDLE'* ]] || fail 'follow-up was not delivered to the bound worker'
handoff="$($O handoff opencode-contract-first --timeout 5 --json)"
python3 - "$handoff" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["turn"] == 2 and row["text"] == "FINAL-TURN-2", row
PY

# Terminal states stay distinct. None may degrade to a generic empty pane or
# accidentally return the previous repair's text.
for spec in \
  'EMPTY_FINAL:provider_empty' \
  'INCOMPLETE_TURN:incomplete_tool_call' \
  'PROVIDER_ERROR:provider_error' \
  'OVERSIZE_FINAL:oversize'; do
  prompt="${spec%%:*}"; want="${spec#*:}"
  "$O" send opencode-contract-first --prompt "$prompt" --json >/dev/null
  set +e
  typed="$($O handoff opencode-contract-first --timeout 5 --json)"
  typed_rc=$?
  set -e
  [[ "$typed_rc" -ne 0 ]] || fail "$want handoff returned success"
  python3 - "$typed" "$want" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == sys.argv[2], row
assert row.get("text", "") != "FINAL-TURN-2", row
PY
done

# Bounded read distinguishes captured, truly empty, missing, and unreadable.
captured="$($O output opencode-contract-first --lines 40 --json)"
python3 - "$captured" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == "captured", row
assert row["session"] == "opencode-contract-first", row
assert row["lines"] == 40, row
assert len(row["text"].encode()) <= 65536, len(row["text"].encode())
PY
[[ "$captured" != *'FINAL-TURN-2'* ]] || fail 'pane observation impersonated the durable handoff'

tmux new-session -d -s opencode-empty 'sleep 30'
empty="$($O output opencode-empty --lines 5 --json)"
[[ "$empty" == *'"status":"empty"'* ]] || fail 'successful blank capture was not typed empty'

# Name shape is not ownership. A human TUI can also be opencode-*; worker-only
# send/handoff/close must refuse it and leave the exact sentinel alive.
set +e
human_send="$($O send opencode-empty --prompt 'must not land' --json 2>/dev/null)"; human_send_rc=$?
human_handoff="$($O handoff opencode-empty --timeout 0 --json 2>/dev/null)"; human_handoff_rc=$?
human_close="$($O close opencode-empty --json 2>/dev/null)"; human_close_rc=$?
set -e
[[ "$human_send_rc" -eq 4 && "$human_send" == *'"status":"not_owned"'* ]] \
  || fail 'send accepted a human opencode-* session'
[[ "$human_handoff_rc" -eq 4 && "$human_handoff" == *'"status":"not_owned"'* ]] \
  || fail 'handoff accepted a human opencode-* session'
[[ "$human_close_rc" -eq 4 && "$human_close" == *'"status":"not_owned"'* ]] \
  || fail 'close accepted a human opencode-* session'
tmux has-session -t '=opencode-empty' 2>/dev/null \
  || fail 'worker lifecycle command killed the human sentinel'

set +e
missing="$($O output opencode-nope --json 2>/dev/null)"
missing_rc=$?
set -e
[[ "$missing_rc" -eq 3 && "$missing" == *'"status":"missing"'* ]] || fail 'missing session was not distinct'

FAKE_TMUX="$SCRATCH/fake-tmux"
cat > "$FAKE_TMUX" <<'SH'
#!/bin/sh
case "$1" in
  has-session) exit 0 ;;
  display-message) exit 1 ;;
esac
exit 1
SH
chmod +x "$FAKE_TMUX"
mkdir -p "$SCRATCH/unreadable-bin"
ln -s "$FAKE_TMUX" "$SCRATCH/unreadable-bin/tmux"
set +e
unreadable="$(PATH="$SCRATCH/unreadable-bin:/usr/bin:/bin" CLAUDEMAXXING_O_SHELL="$ROOT/shell/opencode-o.sh" "$O" output opencode-broken --json 2>/dev/null)"
unreadable_rc=$?
set -e
[[ "$unreadable_rc" -eq 4 && "$unreadable" == *'"status":"unreadable"'* ]] || fail 'unreadable pane collapsed into empty/missing'

# A delegated worker is closeable, and closing it takes the process with it.
# Without this the lifecycle has no terminator: every delegation leaks an
# OpenCode process plus its MCP children for the life of the tmux server.
worker_pid="$(tmux display-message -p -t '=opencode-contract-first:' '#{pane_pid}')"
[[ "$worker_pid" =~ ^[0-9]+$ ]] || fail 'could not resolve delegated worker pid'

# The janitor only proposes sessions born of a delegation; an untagged session
# (a human's own TUI, or the raw one above) must never be a candidate.
dry="$(O_REAP_IDLE_SECONDS=0 $O reap --dry-run --json)"
python3 - "$dry" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["status"] == "reaped" and row["dry_run"] is True, row
assert "opencode-contract-first" in row["sessions"], row
assert "opencode-empty" not in row["sessions"], row
PY
tmux has-session -t '=opencode-contract-first' 2>/dev/null || fail 'dry-run reap killed a session'

# A turn without a terminal plugin event remains observably pending. Handoff
# and a second send both fail with distinct typed states; neither may submit an
# overlapping task or fall back to pane lore.
$O send opencode-contract-first --prompt NO_EVENT --json >/dev/null
set +e
pending="$($O handoff opencode-contract-first --timeout 0 --json)"
pending_rc=$?
overlap="$($O send opencode-contract-first --prompt 'must not overlap' --json)"
overlap_rc=$?
set -e
[[ "$pending_rc" -eq 4 && "$pending" == *'"status":"readiness_failure"'* ]] \
  || fail 'pending turn was not a typed readiness failure'
[[ "$overlap_rc" -eq 4 && "$overlap" == *'"status":"not_ready"'* ]] \
  || fail 'overlapping send was not refused as not_ready'

closed="$($O close opencode-contract-first --json)"
[[ "$closed" == *'"status":"closed"'* ]] || fail 'close lacks typed success'
! tmux has-session -t '=opencode-contract-first' 2>/dev/null || fail 'closed session still exists'
for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$worker_pid" 2>/dev/null || break; sleep 0.2; done
! kill -0 "$worker_pid" 2>/dev/null || fail 'close left the worker process running'

set +e
gone="$($O close opencode-contract-first --json 2>/dev/null)"
gone_rc=$?
bad="$($O close not-an-opencode-session --json 2>/dev/null)"
bad_rc=$?
set -e
[[ "$gone_rc" -eq 3 && "$gone" == *'"status":"missing"'* ]] || fail 'closing an absent session was not typed missing'
[[ "$bad_rc" -eq 2 && "$bad" == *'"status":"invalid_session"'* ]] || fail 'close accepted a non-worker target'

# Real reap: a delegated worker idle past the threshold is collected, and the
# untagged session beside it survives.
second="$($O delegate contract-second --agent glm-coder --run-dir "$RUN_DIR" --json)"
[[ "$second" == *'"status":"sent"'* ]] || fail 'second delegation did not dispatch'
sleep 2
reaped="$(O_REAP_IDLE_SECONDS=1 $O reap --json)"
python3 - "$reaped" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["dry_run"] is False, row
assert "opencode-contract-second" in row["sessions"], row
PY
! tmux has-session -t '=opencode-contract-second' 2>/dev/null || fail 'reap did not close the idle delegated worker'
tmux has-session -t '=opencode-empty' 2>/dev/null || fail 'reap killed an untagged session'

printf 'o-runtime: PASS\n'
