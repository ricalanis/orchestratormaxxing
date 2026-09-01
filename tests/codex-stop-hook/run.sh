#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/codex-stop-hook"
HOOKS="$ROOT/plugins/claudemaxxing/hooks/hooks.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'codex-stop-hook contract: %s\n' "$*" >&2; exit 1; }
[[ -x "$TOOL" ]] || fail 'helper missing or not executable'
mkdir -p "$TMP/bin" "$TMP/home"
# Hermetic HOME: the real ~/.hermes/scripts/notify-code-thread.sh must never
# fire from a test run.
export HOME="$TMP/home"

for name in warp-agent-event agent-tab-status; do
  printf '#!/bin/sh\nprintf "%%s\\n" "$0 $*" >> "$HOOK_CALLS"\n' > "$TMP/bin/$name"
  chmod +x "$TMP/bin/$name"
done
cat > "$TMP/bin/mem-audit" <<'SH'
#!/bin/sh
printf '⚠ stale project memory\n'
SH
cat > "$TMP/bin/session-log" <<'SH'
#!/bin/sh
printf 'Run /wrap-up before leaving this project.\n'
SH
chmod +x "$TMP/bin/mem-audit" "$TMP/bin/session-log"

export HOOK_CALLS="$TMP/calls"
payload='{"session_id":"s","turn_id":"t","cwd":"/tmp","hook_event_name":"Stop","model":"gpt","permission_mode":"default","stop_hook_active":false,"transcript_path":null,"last_assistant_message":"SECRET RESPONSE"}'
out="$(printf '%s' "$payload" | PATH="$TMP/bin:/usr/bin:/bin" "$TOOL")" || fail 'hook failed'
python3 - "$out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1])
assert set(obj) <= {"systemMessage"}
assert "stale project memory" in obj.get("systemMessage", "")
assert "/wrap-up" in obj.get("systemMessage", "")
assert "SECRET RESPONSE" not in json.dumps(obj)
PY
grep -Fq 'warp-agent-event codex stop' "$HOOK_CALLS" || fail 'completion event not emitted'
grep -Fq 'agent-tab-status attention' "$HOOK_CALLS" || fail 'tab status not updated'

: > "$HOOK_CALLS"
child="$(printf '%s' "$payload" | SOLPLAN_CHILD=1 PATH="$TMP/bin:/usr/bin:/bin" "$TOOL")" || \
  fail 'Solplan child path failed'
[[ "$child" == '{}' ]] || fail 'Solplan child did not return one empty JSON object'
[[ ! -s "$HOOK_CALLS" ]] || fail 'Solplan child performed lifecycle actions'

# --- enriched notify path: agent-done-notify gets the payload, and it is the
# ONLY notifier that fires (the legacy generic script must stay quiet) ---
NOTIFY_IN="$TMP/notify-stdin"
cat > "$TMP/bin/agent-done-notify" <<'SH'
#!/bin/sh
printf 'agent-done-notify %s\n' "$*" >> "$HOOK_CALLS"
cat > "$NOTIFY_IN"
SH
chmod +x "$TMP/bin/agent-done-notify"
export NOTIFY_IN
mkdir -p "$HOME/.hermes/scripts"
cat > "$HOME/.hermes/scripts/notify-code-thread.sh" <<'SH'
#!/bin/sh
printf 'notify-code-thread %s\n' "$*" >> "$HOOK_CALLS"
SH
chmod +x "$HOME/.hermes/scripts/notify-code-thread.sh"

: > "$HOOK_CALLS"
out="$(printf '%s' "$payload" | PATH="$TMP/bin:/usr/bin:/bin" "$TOOL")" || fail 'enriched hook failed'
python3 - "$out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1])
assert set(obj) <= {"systemMessage"}
assert "SECRET RESPONSE" not in json.dumps(obj)
PY
grep -Fq 'agent-done-notify --agent codex' "$HOOK_CALLS" || \
  fail 'enriched notifier not invoked with --agent codex'
grep -Fq 'notify-code-thread' "$HOOK_CALLS" && \
  fail 'legacy notifier must not fire when agent-done-notify is available'
grep -Fq 'SECRET RESPONSE' "$NOTIFY_IN" || \
  fail 'payload must reach agent-done-notify on stdin'

# --- fallback: without agent-done-notify the legacy script still notifies ---
rm -f "$TMP/bin/agent-done-notify"
: > "$HOOK_CALLS"
printf '%s' "$payload" | PATH="$TMP/bin:/usr/bin:/bin" "$TOOL" > /dev/null || fail 'fallback hook failed'
grep -Fq 'notify-code-thread' "$HOOK_CALLS" || fail 'legacy fallback notifier not invoked'

python3 - "$HOOKS" <<'PY'
import json, sys
hooks = json.load(open(sys.argv[1]))["hooks"]
stop = hooks.get("Stop", [])
commands = [h.get("command", "") for group in stop for h in group.get("hooks", [])]
assert len(commands) == 1, commands
assert "codex-stop-hook" in commands[0]
PY

printf 'codex-stop-hook contract: PASS\n'
