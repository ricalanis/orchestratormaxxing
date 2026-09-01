#!/usr/bin/env bash
# Contract for bin/agent-done-notify — the deterministic done/needs-input
# Telegram notifier (Chunk C of the Hermes↔c/g bridge). Authored before the
# tool (Tier-0). The tool's whole job: hook JSON on stdin → one bounded,
# sanitized message via `hermes send` — never breaking the session it serves.
# Fleet identity (target, relay, dashboard base, server hostname) comes from ONE
# user-owned fleet.env; the fixture below uses neutral values and every argv
# shape asserted here is relative to them. No fleet.env == standalone client.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/agent-done-notify"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'agent-done-notify contract: %s\n' "$*" >&2; exit 1; }
[[ -x "$TOOL" ]] || fail 'tool missing or not executable'

# BSD wc left-pads redirected counts (for example, "       3"). Several
# assertions use the count as a fixture filename, so normalize numeric output
# without changing the production notifier.
wc() { command wc "$@" | tr -d '[:space:]'; }

mkdir -p "$TMP/bin" "$TMP/home/.config/claudemaxxing"
export HOME="$TMP/home"
# fleet.env fixture — exercises the parser's whole grammar (comment, bare
# KEY=VALUE, `export ` prefix, double quotes) with tenant-neutral values.
FLEET_ENV="$HOME/.config/claudemaxxing/fleet.env"
cat > "$FLEET_ENV" <<'ENV'
# claudemaxxing fleet identity (contract fixture)
CLAUDEMAXXING_NOTIFY_TARGET=telegram:100:4242
export CLAUDEMAXXING_NOTIFY_RELAY="user@fleet-server"
CLAUDEMAXXING_DASHBOARD_URL=https://fleet-server.example:5555
CLAUDEMAXXING_SERVER_HOSTNAME=fleet-server
ENV
# The real machine's fleet identity must never leak into the fixture.
unset CLAUDEMAXXING_FLEET_ENV CLAUDEMAXXING_NOTIFY_TARGET CLAUDEMAXXING_NOTIFY_RELAY \
      CLAUDEMAXXING_DASHBOARD_URL CLAUDEMAXXING_SERVER_HOSTNAME 2>/dev/null || true
# The tool asks tmux for the session name only when $TMUX is set; keep the
# baseline cases tmux-free so the test is hermetic inside a real tmux session.
unset TMUX 2>/dev/null || true
# Link-building must resolve from the test's HOME/env, never the real machine's.
unset DASHBOARD_URL 2>/dev/null || true
CALLS="$TMP/calls"; : > "$CALLS"
MSGDIR="$TMP/msgs"; mkdir -p "$MSGDIR"

# hermes shim: records argv and captures the stdin message body.
cat > "$TMP/bin/hermes" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
n=$(awk 'END { print NR }' "$CALLS")
cat > "$MSGDIR/msg.$n"
SH
chmod +x "$TMP/bin/hermes"
export CALLS MSGDIR
BASEPATH="$TMP/bin:/usr/bin:/bin"

# A realistic Claude transcript: earlier turns, then a final assistant text
# with ANSI noise and a credential-shaped token that must never reach Telegram.
TRANSCRIPT="$TMP/transcript.jsonl"
python3 - "$TRANSCRIPT" <<'PY'
import json, sys
lines = [
    {"type": "user", "message": {"content": "arregla el bug"}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text",
         "text": "Terminé el fix de sessions.py: [31mtests verdes[0m.\nToken usado: sk-AbCdEf1234567890XyZ9 (interno).\nSiguiente paso: deploy."}]}},  # gitleaks:allow
]
with open(sys.argv[1], "w") as f:
    for entry in lines:
        f.write(json.dumps(entry) + "\n")
PY

# --- 1. Claude Stop → extract of the final assistant message, sanitized ---
payload=$(python3 -c "import json;print(json.dumps({
  'hook_event_name':'Stop','session_id':'abc-123','cwd':'/tmp/miproyecto',
  'transcript_path':'$TRANSCRIPT'}))")
out="$(printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL")" || fail 'Stop run failed'
[[ -z "$out" ]] || fail 'stdout must stay empty (hook safety)'
grep -q -- '--to telegram:100:4242' "$CALLS" || fail 'target must come from fleet.env CLAUDEMAXXING_NOTIFY_TARGET'
msg="$(cat "$MSGDIR"/msg.1)"
printf '%s' "$msg" | grep 'miproyecto' >/dev/null || fail 'message must name the project'
printf '%s' "$msg" | grep 'Terminé el fix de sessions.py' >/dev/null || fail 'message must carry the summary extract'
printf '%s' "$msg" | grep 'sk-AbCdEf1234567890XyZ9' >/dev/null && fail 'credential-shaped token must be redacted'
printf '%s' "$msg" | grep $'\x1b' >/dev/null && fail 'ANSI escapes must be stripped'
[[ "$(printf '%s' "$msg" | wc -c)" -le 600 ]] || fail 'message must stay bounded'

# --- 2. notify-target file overrides the destination ---
printf 'telegram:100:9999\n' > "$HOME/.config/claudemaxxing/notify-target"
printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL" || fail 'override run failed'
grep -q -- '--to telegram:100:9999' "$CALLS" || fail 'notify-target file must override the destination'
rm -f "$HOME/.config/claudemaxxing/notify-target"

# --- 3. Notification → needs-input message, then a cooldown swallows repeats ---
note=$(python3 -c "import json;print(json.dumps({
  'hook_event_name':'Notification','session_id':'abc-123','cwd':'/tmp/miproyecto',
  'message':'Claude needs your permission to use Bash'}))")
before=$(wc -l < "$CALLS")
printf '%s' "$note" | PATH="$BASEPATH" "$TOOL" || fail 'Notification run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'needs-input notification not sent'
msg="$(cat "$MSGDIR"/msg.$after)"
printf '%s' "$msg" | grep -i 'necesita input' >/dev/null || fail 'needs-input message must say so'
printf '%s' "$msg" | grep 'permission to use Bash' >/dev/null || fail 'needs-input message must carry the reason'
printf '%s' "$note" | PATH="$BASEPATH" "$TOOL" || fail 'cooldown run failed'
[[ "$(wc -l < "$CALLS")" -eq "$after" ]] || fail 'repeated needs-input within cooldown must not re-send'

# --- 4. Missing transcript → generic done message still goes out ---
broken=$(python3 -c "import json;print(json.dumps({
  'hook_event_name':'Stop','session_id':'zzz','cwd':'/tmp/miproyecto',
  'transcript_path':'$TMP/no-such-file.jsonl'}))")
before=$(wc -l < "$CALLS")
printf '%s' "$broken" | PATH="$BASEPATH" "$TOOL" || fail 'degraded run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'degraded run must still notify'
msg="$(cat "$MSGDIR"/msg.$after)"
printf '%s' "$msg" | grep 'terminó' >/dev/null || fail 'degraded message must still say done'
printf '%s' "$msg" | grep 'miproyecto' >/dev/null || fail 'degraded message must still name the project'

# --- 5. Codex Stop → summary from last_assistant_message ---
codex=$(python3 -c "import json;print(json.dumps({
  'hook_event_name':'Stop','session_id':'c1','cwd':'/tmp/otroproyecto',
  'last_assistant_message':'Refactor listo; corre pytest para confirmar.'}))")
before=$(wc -l < "$CALLS")
printf '%s' "$codex" | PATH="$BASEPATH" "$TOOL" --agent codex || fail 'codex run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'codex stop must notify'
msg="$(cat "$MSGDIR"/msg.$after)"
printf '%s' "$msg" | grep 'Refactor listo' >/dev/null || fail 'codex message must carry last_assistant_message'
printf '%s' "$msg" | grep 'otroproyecto' >/dev/null || fail 'codex message must name the project'

# --- 5b. OpenCode stop (claudemaxxing-notify plugin payload) → notifies ---
# Same shape as Codex: the host hands the final text directly.
oc=$(python3 -c "import json;print(json.dumps({
  'hook_event_name':'Stop','session_id':'oc1','cwd':'/tmp/otroproyecto',
  'last_assistant_message':'Investigación terminada; digest en el hilo.'}))")
before=$(wc -l < "$CALLS")
printf '%s' "$oc" | PATH="$BASEPATH" "$TOOL" --agent opencode || fail 'opencode run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'opencode stop must notify'
msg="$(cat "$MSGDIR"/msg.$after)"
printf '%s' "$msg" | grep 'Investigación terminada' >/dev/null || fail 'opencode message must carry last_assistant_message'
printf '%s' "$msg" | grep 'OpenCode' >/dev/null || fail 'opencode message must name the OpenCode host'

# --- 6. No hermes → relays over `tailscale ssh <CLAUDEMAXXING_NOTIFY_RELAY>`; message intact ---
rm -f "$TMP/bin/hermes"
cat > "$TMP/bin/tailscale" <<'SH'
#!/bin/sh
printf 'tailscale %s\n' "$*" >> "$CALLS"
n=$(awk 'END { print NR }' "$CALLS")
cat > "$MSGDIR/msg.$n"
SH
chmod +x "$TMP/bin/tailscale"
before=$(wc -l < "$CALLS")
printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL" || fail 'ssh fallback run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'ssh fallback must still notify'
tail -1 "$CALLS" | grep 'ssh user@fleet-server' >/dev/null || fail 'fallback must relay via the fleet.env relay'
tail -1 "$CALLS" | grep 'hermes' >/dev/null || fail 'fallback must invoke hermes remotely'
msg="$(cat "$MSGDIR"/msg.$after)"
printf '%s' "$msg" | grep 'Terminé el fix de sessions.py' >/dev/null || fail 'relayed message must carry the extract'

# --- 6b. NOT configured (the standalone-client invariant): no fleet.env, no
# notify-target, hermes absent, a fake tailscale on PATH → the tool spawns NO
# transport: the tailscale log stays EMPTY, exit 0, stdout silent, under 1 s.
# Proven red against the pre-fleet.env tool, which relayed to a built-in
# tenant target through tailscale whenever hermes was missing.
mv "$FLEET_ENV" "$FLEET_ENV.off"
rm -f "$TMP/bin/hermes" "$HOME/.config/claudemaxxing/notify-target"
export TSLOG="$TMP/tailscale.log"; : > "$TSLOG"
cat > "$TMP/bin/tailscale" <<'SH'
#!/bin/sh
printf 'tailscale %s\n' "$*" >> "$TSLOG"
cat > /dev/null
SH
chmod +x "$TMP/bin/tailscale"
t0=$(python3 -c 'import time;print(int(time.time()*1000))')
out="$(printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL")" || fail 'unconfigured run must exit 0'
t1=$(python3 -c 'import time;print(int(time.time()*1000))')
[[ -z "$out" ]] || fail 'unconfigured run must stay silent'
[[ ! -s "$TSLOG" ]] || fail "unconfigured machine must spawn no transport (tailscale got: $(head -1 "$TSLOG"))"
[[ $((t1 - t0)) -lt 1000 ]] || fail "unconfigured run must return in < 1 s (took $((t1 - t0)) ms)"
# control: the same run with CLAUDEMAXXING_FLEET_ENV naming the moved file
# relays again — the empty log above came from the absent file, not a dead
# shim; and an unreadable fleet.env (a directory) degrades to not configured.
: > "$TSLOG"
printf '%s' "$payload" | CLAUDEMAXXING_FLEET_ENV="$FLEET_ENV.off" PATH="$BASEPATH" "$TOOL" || fail 'env-path run failed'
grep -q 'ssh user@fleet-server' "$TSLOG" || fail 'CLAUDEMAXXING_FLEET_ENV must select the fleet.env path'
: > "$TSLOG"
printf '%s' "$payload" | CLAUDEMAXXING_FLEET_ENV="$TMP" PATH="$BASEPATH" "$TOOL" || fail 'unreadable fleet.env run failed'
[[ ! -s "$TSLOG" ]] || fail 'an unreadable fleet.env must degrade to not configured'
rm -f "$TMP/bin/tailscale"
mv "$FLEET_ENV.off" "$FLEET_ENV"

# --- 7. No transport at all → silent success (a hook must never fail) ---
rm -f "$TMP/bin/tailscale"
out="$(printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL")" || fail 'no-transport run must exit 0'
[[ -z "$out" ]] || fail 'no-transport run must stay silent'

# --- 8. Garbage stdin → exit 0, silent ---
out="$(printf 'not json' | PATH="$BASEPATH" "$TOOL")" || fail 'garbage stdin must exit 0'
[[ -z "$out" ]] || fail 'garbage stdin must stay silent'

# --- 9. Inside tmux, the message names the session (fail-open without it) ---
cat > "$TMP/bin/hermes" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
n=$(awk 'END { print NR }' "$CALLS")
cat > "$MSGDIR/msg.$n"
SH
chmod +x "$TMP/bin/hermes"
cat > "$TMP/bin/tmux" <<'SH'
#!/bin/sh
printf 'claude-sesiontest\n'
SH
chmod +x "$TMP/bin/tmux"
before=$(wc -l < "$CALLS")
printf '%s' "$payload" | TMUX=dummy PATH="$BASEPATH" "$TOOL" || fail 'tmux-name run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'tmux-name run must notify'
msg="$(cat "$MSGDIR"/msg.$after)"
printf '%s' "$msg" | grep 'claude-sesiontest' >/dev/null || fail 'message must name the tmux session'
rm -f "$TMP/bin/tmux"

# --- 10. Deep-link: the message ends with a tappable dashboard URL that
# opens THIS session's live modal (?tab=sessions&open=<host>/<session>) ---
BASE_URL='https://fleet-server.example:5555'
cat > "$TMP/bin/tmux" <<'SH'
#!/bin/sh
printf 'claude-sesiontest\n'
SH
chmod +x "$TMP/bin/tmux"
printf 'local\n' > "$HOME/.config/claudemaxxing/notify-host-key"
before=$(wc -l < "$CALLS")
printf '%s' "$payload" | TMUX=dummy PATH="$BASEPATH" "$TOOL" || fail 'link run failed'
after=$(wc -l < "$CALLS")
msg="$(cat "$MSGDIR"/msg.$after)"
[[ "$(printf '%s' "$msg" | tail -1)" == "$BASE_URL/?tab=sessions&open=local/claude-sesiontest" ]] || \
  fail "message must end with the session deep-link (got: $(printf '%s' "$msg" | tail -1))"
[[ "$(printf '%s' "$msg" | wc -c)" -le 600 ]] || fail 'message with link must stay within 600 bytes'

# needs-input also carries the link (fresh session_id to dodge the cooldown)
note2=$(python3 -c "import json;print(json.dumps({
  'hook_event_name':'Notification','session_id':'link-note-1','cwd':'/tmp/miproyecto',
  'message':'Claude needs your permission to use Bash'}))")
printf '%s' "$note2" | TMUX=dummy PATH="$BASEPATH" "$TOOL" || fail 'link note run failed'
after=$(wc -l < "$CALLS")
tail -1 "$MSGDIR/msg.$after" | grep "open=local/claude-sesiontest" >/dev/null || \
  fail 'needs-input message must carry the deep-link'

# host label: a session on the fleet server (CLAUDEMAXXING_SERVER_HOSTNAME,
# env override > fleet.env) is labelled "local"; any other machine keeps its
# own short hostname; the notify-host-key file still wins over both.
rm -f "$HOME/.config/claudemaxxing/notify-host-key"
me="$(python3 -c 'import socket;print(socket.gethostname().lower().split(".",1)[0])')"
printf '%s' "$payload" | TMUX=dummy CLAUDEMAXXING_SERVER_HOSTNAME="$me" PATH="$BASEPATH" "$TOOL" || \
  fail 'server-host run failed'
after=$(wc -l < "$CALLS")
tail -1 "$MSGDIR/msg.$after" | grep 'open=local/claude-sesiontest' >/dev/null || \
  fail "a session on the fleet server must be labelled local (got: $(tail -1 "$MSGDIR/msg.$after"))"
printf '%s' "$payload" | TMUX=dummy PATH="$BASEPATH" "$TOOL" || fail 'other-host run failed'
after=$(wc -l < "$CALLS")
me_q="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$me")"
tail -1 "$MSGDIR/msg.$after" | grep "open=$me_q/claude-sesiontest" >/dev/null || \
  fail "a machine that is not the fleet server keeps its hostname (got: $(tail -1 "$MSGDIR/msg.$after"))"
printf 'local\n' > "$HOME/.config/claudemaxxing/notify-host-key"

# a session name needing quoting is percent-encoded
cat > "$TMP/bin/tmux" <<'SH'
#!/bin/sh
printf 'claude-con espacio\n'
SH
printf '%s' "$payload" | TMUX=dummy PATH="$BASEPATH" "$TOOL" || fail 'quoted-name run failed'
after=$(wc -l < "$CALLS")
tail -1 "$MSGDIR/msg.$after" | grep 'open=local/claude-con%20espacio' >/dev/null || \
  fail 'session name must be percent-encoded in the link'
rm -f "$TMP/bin/tmux"

# no tmux → link still present, but without &open=
printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL" || fail 'linkless-tmux run failed'
after=$(wc -l < "$CALLS")
last="$(tail -1 "$MSGDIR/msg.$after")"
[[ "$last" == "$BASE_URL/?tab=sessions" ]] || \
  fail "without tmux the link must point at the sessions tab (got: $last)"

# DASHBOARD_URL env overrides the base
printf '%s' "$payload" | DASHBOARD_URL='https://otra.base:9999' PATH="$BASEPATH" "$TOOL" || \
  fail 'env-override run failed'
after=$(wc -l < "$CALLS")
tail -1 "$MSGDIR/msg.$after" | grep '^https://otra.base:9999/?tab=sessions' >/dev/null || \
  fail 'DASHBOARD_URL env must override the link base'

# dashboard-url file set to off → no link at all
printf 'off\n' > "$HOME/.config/claudemaxxing/dashboard-url"
printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL" || fail 'link-off run failed'
after=$(wc -l < "$CALLS")
grep -q 'https://' "$MSGDIR/msg.$after" && fail 'dashboard-url=off must suppress the link'
rm -f "$HOME/.config/claudemaxxing/dashboard-url" "$HOME/.config/claudemaxxing/notify-host-key"

# no built-in tenant default: with fleet.env absent and only the legacy
# notify-target file set, the message still goes out (the legacy override
# wins) but carries NO dashboard link — there is no default base to fall to.
mv "$FLEET_ENV" "$FLEET_ENV.off"
printf 'telegram:100:4242\n' > "$HOME/.config/claudemaxxing/notify-target"
before=$(wc -l < "$CALLS")
printf '%s' "$payload" | PATH="$BASEPATH" "$TOOL" || fail 'legacy-target run failed'
after=$(wc -l < "$CALLS")
[[ "$after" -eq $((before + 1)) ]] || fail 'legacy notify-target must still deliver without fleet.env'
grep -q 'https://' "$MSGDIR/msg.$after" && fail 'without fleet.env there must be no default dashboard link'
rm -f "$HOME/.config/claudemaxxing/notify-target"
mv "$FLEET_ENV.off" "$FLEET_ENV"

# --- 11. The legacy generic project hook is gone (dedupe: ONE notifier) ---
grep -q 'Session completed' "$ROOT/.claude/settings.json" && \
  fail 'repo settings still register the generic Session completed hook'

# --- 12. OpenCode plugin: permission.asked → needs-input via the REAL tool ---
# Executes the actual claudemaxxing-notify.js under node/bun (lq-3c820901):
# the plugin must bridge OpenCode's permission.asked EVENT (verified emitted at
# runtime; the 'permission.ask' plugin HOOK is dead code and must NOT be
# registered) into a PermissionRequest payload, which then flows through the
# real agent-done-notify — so cooldown parity comes from the tool, not a copy.
# Resolve a JS runtime beyond PATH: the loop-cron wrapper's PATH omits
# /opt/homebrew/bin, where the Mac's node lives. A missing runtime is a HARD
# fail, never a silent skip — a skipped plugin case would read as a green
# contract that verified nothing (silence ≠ blindness).
JSRUN="$(command -v node || command -v bun || true)"
for cand in /opt/homebrew/bin/node /usr/local/bin/node "$HOME/.local/bin/node" \
            /opt/homebrew/bin/bun /usr/local/bin/bun "$HOME/.local/bin/bun"; do
  [[ -z "$JSRUN" && -x "$cand" ]] && JSRUN="$cand"
done
[[ -n "$JSRUN" ]] || fail 'no node/bun runtime found — cannot verify the OpenCode plugin'
{
  PLUGIN="$ROOT/opencode/plugins/claudemaxxing-notify.js"
  [[ -f "$PLUGIN" ]] || fail 'claudemaxxing-notify.js plugin missing'
  cat > "$TMP/plugin-harness.mjs" <<'JS'
import { pathToFileURL } from "node:url"
import { writeFileSync } from "node:fs"

const [pluginPath, outFile] = process.argv.slice(2)
const calls = []
// Fake Bun-$ tagged template: record the command text + interpolated values.
const $ = (strings, ...values) => {
  calls.push({ text: strings.raw.join("__V__"), values: values.map(String) })
  return Promise.resolve()
}
const client = { session: { messages: async () => ({ data: [
  { info: { id: "msg_idle_1", role: "assistant", finish: "stop" },
    parts: [{ type: "text", text: "listo el análisis" }] },
] }) } }
const mod = await import(pathToFileURL(pluginPath).href)
const hooks = await mod.ClaudemaxxingNotify({ directory: "/tmp/ocproyecto", client, $ })
if (Object.prototype.hasOwnProperty.call(hooks, "permission.ask")) {
  console.error("plugin registers the never-fired permission.ask hook (dead code)")
  process.exit(1)
}
await hooks.event({ event: { type: "permission.asked", properties: {
  id: "per_1", sessionID: "oc-perm-1", permission: "bash",
  patterns: ["rm -rf /tmp/x"], metadata: {}, always: [],
} } })
const permCalls = calls.length
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "oc-idle-1" } } })
const interactiveBindCalls = calls.filter((c) => c.text.includes("o bind-event --json")).length
process.env.CLAUDEMAXXING_O_DELEGATED = "1"
const delegatedHooks = await mod.ClaudemaxxingNotify({ directory: "/tmp/ocproyecto", client, $ })
await delegatedHooks.event({ event: { type: "session.idle", properties: { sessionID: "oc-idle-1" } } })
// Malformed events must not crash the handler (best-effort contract): a
// permission.asked with no properties still alerts with the fallback reason.
await hooks.event({ event: { type: "permission.asked" } })
await hooks.event({ event: { type: "permission.asked", properties: {
  sessionID: "oc-perm-2", permission: "webfetch", patterns: "not-an-array",
} } })
writeFileSync(outFile, JSON.stringify({ permCalls, interactiveBindCalls, calls }))
JS
  "$JSRUN" "$TMP/plugin-harness.mjs" "$PLUGIN" "$TMP/plugin-calls.json" || \
    fail 'plugin harness run failed (dead permission.ask hook registered?)'
  python3 - "$TMP/plugin-calls.json" "$TMP/perm-payload.json" <<'PY' || fail 'plugin permission.asked branch wrong'
import json, sys
data = json.load(open(sys.argv[1]))
calls, perm_calls = data["calls"], data["permCalls"]
assert perm_calls == 1, f"permission.asked must emit exactly one notify call (got {perm_calls})"
assert data["interactiveBindCalls"] == 0, "interactive idle event reached delegation-only bind-event"
assert "agent-done-notify --agent opencode" in calls[0]["text"], "must pipe into agent-done-notify --agent opencode"
payload = json.loads(calls[0]["values"][0])
assert payload["hook_event_name"] == "PermissionRequest", payload
assert payload["session_id"] == "oc-perm-1", payload
assert "bash" in payload["message"] and "rm -rf /tmp/x" in payload["message"], payload
notify_calls = [c for c in calls if "agent-done-notify --agent opencode" in c["text"]]
recovery_calls = [c for c in calls if "warp-agent-recovery register-agent opencode" in c["text"]]
bind_calls = [c for c in calls if "o bind-event --json" in c["text"]]
assert len(notify_calls) == 5, "interactive/delegated idle + both malformed permission events must all notify"
assert len(recovery_calls) == 2, "both idle events must bind the OpenCode recovery id"
assert len(bind_calls) == 1, "only delegated idle may bind one durable worker handoff"
assert all(">/dev/null 2>&1 || true" in c["text"] for c in calls), "best-effort plugin command can leak into TUI"
handoff = json.loads(bind_calls[0]["values"][0])
assert handoff == {
    "session_id": "oc-idle-1",
    "message_id": "msg_idle_1",
    "finish": "stop",
    "text": "listo el análisis",
    "error_code": "",
}, handoff
stop = json.loads(notify_calls[1]["values"][0])
assert stop["hook_event_name"] == "Stop" and stop["last_assistant_message"] == "listo el análisis", stop
delegated_stop = json.loads(notify_calls[2]["values"][0])
assert delegated_stop["hook_event_name"] == "Stop", delegated_stop
bare = json.loads(notify_calls[3]["values"][0])
assert bare["hook_event_name"] == "PermissionRequest", bare
assert bare["message"] == "permiso pendiente" and bare["session_id"] == "", bare
odd = json.loads(notify_calls[4]["values"][0])
assert odd["message"] == "webfetch" and odd["session_id"] == "oc-perm-2", odd
open(sys.argv[2], "w").write(calls[0]["values"][0])
PY
  # The plugin's ACTUAL payload through the REAL tool: needs-input message
  # names OpenCode + the reason, and the tool's 120s cooldown swallows repeats.
  before=$(wc -l < "$CALLS")
  PATH="$BASEPATH" "$TOOL" --agent opencode < "$TMP/perm-payload.json" || fail 'plugin-payload run failed'
  after=$(wc -l < "$CALLS")
  [[ "$after" -eq $((before + 1)) ]] || fail 'plugin permission payload must notify'
  msg="$(cat "$MSGDIR/msg.$after")"
  printf '%s' "$msg" | grep 'OpenCode' >/dev/null || fail 'permission message must name OpenCode'
  printf '%s' "$msg" | grep -i 'necesita input' >/dev/null || fail 'permission message must say needs-input'
  printf '%s' "$msg" | grep 'rm -rf /tmp/x' >/dev/null || fail 'permission message must carry the pattern'
  PATH="$BASEPATH" "$TOOL" --agent opencode < "$TMP/perm-payload.json" || fail 'cooldown rerun failed'
  [[ "$(wc -l < "$CALLS")" -eq "$after" ]] || fail 'repeated permission within cooldown must not re-send'
}

printf 'agent-done-notify contract: PASS\n'
