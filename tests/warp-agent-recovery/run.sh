#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/warp-agent-recovery"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STATE="$TMP/state"
STUBS="$TMP/stubs"
mkdir -p "$STATE" "$STUBS"

fail() { printf 'warp-agent-recovery contract: %s\n' "$*" >&2; exit 1; }
[[ -x "$TOOL" ]] || fail 'missing executable bin/warp-agent-recovery'

# A valid Warp chain remains usable when the app's own parent is root or is
# otherwise unreadable to the user. Both Linux and macOS have this shape.
python3 - "$TOOL" <<'PY'
import importlib.machinery, importlib.util, os, sys
loader = importlib.machinery.SourceFileLoader("warp_recovery_contract", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mod.os.getppid = lambda: 10
linux = {
    10: (20, "shell-start", "/bin/bash", "bash"),
    20: (30, "server-start", "/opt/warp/warp", "/opt/warp/warp terminal-server --parent-pid=30"),
    30: (1, "main-start", "/opt/warp/warp", "/opt/warp/warp --finish-update"),
}
mod.linux_process = lambda pid, uid: linux.get(pid)
mod.linux_boot_id = lambda: "boot-a"
assert mod.linux_warp_generation() == "linux:boot-a:30:main-start"
mac = {
    10: (20, "shell-start", "-zsh"),
    20: (30, "server-start", "/Applications/Warp.app/Contents/MacOS/stable terminal-server --parent-pid=30"),
    30: (1, "main-start", "/Applications/Warp.app/Contents/MacOS/stable"),
}
mod.mac_process = lambda pid, uid: mac.get(pid)
assert mod.mac_warp_generation() == "macos:30:main-start"
PY

cat > "$STUBS/tmux" <<'PY'
#!/usr/bin/env python3
import json, os, sys

path = os.environ["FAKE_TMUX_STATE"]
try:
    with open(path) as f:
        state = json.load(f)
except FileNotFoundError:
    state = {"sessions": {}, "panes": {}}

def save():
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)

args = sys.argv[1:]
cmd = args[0] if args else ""
target = args[args.index("-t") + 1] if "-t" in args else ""
target = target.lstrip("=").split(":", 1)[0]

if cmd == "has-session":
    raise SystemExit(0 if target in state["sessions"] else 1)
if cmd == "display-message":
    template = args[-1]
    if target.startswith("%"):
        session = state["panes"].get(target, "")
        row = state["sessions"].get(session, {})
    else:
        session = target
        row = state["sessions"].get(session, {})
    values = {
        "#{session_name}": session,
        "#{session_attached}": str(row.get("attached", 0)),
        "#{pane_current_path}": row.get("cwd", ""),
    }
    value = values.get(template, "")
    if value:
        print(value)
        raise SystemExit(0)
    raise SystemExit(1)
if cmd == "show-options":
    key = args[-1]
    row = state["sessions"].get(target, {})
    value = row.get("options", {}).get(key)
    if value is None:
        raise SystemExit(1)
    print(value)
    raise SystemExit(0)
if cmd == "set-option":
    if os.environ.get("FAKE_TMUX_SET_FAIL") == "1":
        raise SystemExit(1)
    key, value = args[-2:]
    state["sessions"].setdefault(target, {}).setdefault("options", {})[key] = value
    save()
    raise SystemExit(0)
if cmd == "kill-session":
    state["sessions"].pop(target, None)
    state["panes"] = {pane: session for pane, session in state["panes"].items() if session != target}
    save()
    raise SystemExit(0)
if cmd == "new-session":
    session = args[args.index("-s") + 1]
    cwd = args[args.index("-c") + 1]
    command = args[args.index("-c") + 2:]
    state["sessions"][session] = {
        "attached": 0, "cwd": cwd, "options": {}, "command": command
    }
    state["panes"]["%" + str(len(state["panes"]) + 1)] = session
    save()
    raise SystemExit(0)
raise SystemExit(2)
PY
chmod +x "$STUBS/tmux"

export PATH="$STUBS:/usr/bin:/bin"
export FAKE_TMUX_STATE="$TMP/tmux.json"
export WARP_RECOVERY_STATE_DIR="$STATE"
export WARP_RECOVERY_CONFIG_DIR="$ROOT/shell"
export WARP_RECOVERY_TESTING=1
export WARP_IS_LOCAL_SHELL_SESSION=1
export WARP_TERMINAL_SESSION_UUID=11111111111111111111111111111111
export WARP_RECOVERY_TEST_GENERATION=warp-a
unset TMUX TMUX_PANE SOLPLAN_CHILD

write_tmux() {
  python3 - "$FAKE_TMUX_STATE" "$1" "$2" "$3" <<'PY'
import json, sys
path, name, attached, uuid = sys.argv[1:]
data = {"sessions": {name: {"attached": int(attached), "cwd": "/work", "options": {"@warp_terminal_uuid": uuid}}}, "panes": {"%1": name}}
with open(path, "w") as f:
    json.dump(data, f)
PY
}

register_launch() {
  "$TOOL" register-launch "$1" "$2" "$3"
}

# Same Warp generation means a tab/shell close, never an app restart.
write_tmux codex-app 0 "$WARP_TERMINAL_SESSION_UUID"
register_launch codex codex-app /work
"$TOOL" bind-tmux codex codex-app
python3 - "$FAKE_TMUX_STATE" "$WARP_TERMINAL_SESSION_UUID" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))["sessions"]["codex-app"]
assert row["options"].get("@warp_terminal_uuid") == sys.argv[2], row
PY
[[ -z "$("$TOOL" claim)" ]] || fail 'claimed during the same Warp app generation'

# A tmux name has one graphical owner. Rebinding it from another restored Warp
# UUID removes the stale alias instead of leaving recovery order ambiguous.
export WARP_TERMINAL_SESSION_UUID=22222222222222222222222222222222
register_launch codex codex-app /work
python3 - "$TOOL" <<'PY'
import json, subprocess, sys
data = json.loads(subprocess.check_output([sys.argv[1], "status", "--json"], text=True))
owners = [uuid for uuid, row in data["terminals"].items()
          if row["agent"] == "codex" and row["tmux_name"] == "codex-app"]
assert owners == ["22222222222222222222222222222222"], owners
PY
rm -f "$STATE/warp-agent-recovery.json"
export WARP_TERMINAL_SESSION_UUID=11111111111111111111111111111111
write_tmux codex-app 0 "$WARP_TERMINAL_SESSION_UUID"
register_launch codex codex-app /work
"$TOOL" bind-tmux codex codex-app

# A new Warp generation reattaches the exact detached tmux session once.
export WARP_RECOVERY_TEST_GENERATION=warp-b
[[ "$("$TOOL" claim)" == $'attach\tcodex-app' ]] || fail 'did not claim exact detached tmux session'
[[ -z "$("$TOOL" claim)" ]] || fail 'same recovery record was claimed twice'

# An exact UUID owner may co-attach without detaching or modifying an existing
# client. This is required when a dead SSH transport has not timed out yet.
export WARP_RECOVERY_TEST_GENERATION=warp-c
write_tmux codex-app 1 "$WARP_TERMINAL_SESSION_UUID"
[[ "$("$TOOL" claim)" == $'attach\tcodex-app' ]] || fail 'stale attached status blocked exact owner'
python3 - "$FAKE_TMUX_STATE" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))["sessions"]["codex-app"]
assert row["attached"] == 1, row
PY
# UUID mismatches still fail closed regardless of attached status.
export WARP_RECOVERY_TEST_GENERATION=warp-d
write_tmux codex-app 0 22222222222222222222222222222222
[[ -z "$("$TOOL" claim)" ]] || fail 'attached a tmux session with mismatched Warp UUID'

# Ubuntu wrappers persist a local Warp owner for an exact remote tmux name.
# Recovery emits a typed remote action and does not require an agent session ID
# because the remote tmux server is the persistence boundary.
rm -f "$STATE/warp-agent-recovery.json" "$FAKE_TMUX_STATE"
export WARP_RECOVERY_TEST_GENERATION=warp-ubuntu-a
register_launch claude-ubuntu claude-remote-safe /local/project
export WARP_RECOVERY_TEST_GENERATION=warp-ubuntu-b
[[ "$("$TOOL" claim)" == $'attach-ubuntu\tc\tclaude-remote-safe' ]] \
  || fail 'did not claim exact c-ubuntu session'
[[ -z "$("$TOOL" claim)" ]] || fail 'claimed c-ubuntu recovery twice'

export WARP_RECOVERY_TEST_GENERATION=warp-ubuntu-c
register_launch codex-ubuntu codex-remote-safe /local/project
export WARP_RECOVERY_TEST_GENERATION=warp-ubuntu-d
[[ "$("$TOOL" claim)" == $'attach-ubuntu\tg\tcodex-remote-safe' ]] \
  || fail 'did not claim exact g-ubuntu session'

# If a recreated tmux session cannot be tagged, it is removed immediately so a
# later shell can retry instead of inheriting an unclaimable orphan.
rm -f "$FAKE_TMUX_STATE"
export WARP_RECOVERY_TEST_GENERATION=warp-cleanup-a
register_launch codex codex-cleanup /srv/cleanup
printf '%s\n' '{"session_id":"cleanup-id","cwd":"/srv/cleanup"}' | \
  TMUX=/tmp/tmux TMUX_PANE=%6 "$TOOL" register-agent codex >/dev/null
export WARP_RECOVERY_TEST_GENERATION=warp-cleanup-b
export FAKE_TMUX_SET_FAIL=1
[[ -z "$("$TOOL" claim)" ]] || fail 'claimed an untaggable recreated session'
unset FAKE_TMUX_SET_FAIL
python3 - "$FAKE_TMUX_STATE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert "codex-cleanup" not in data["sessions"], data
PY

# A dead tmux session resumes the exact Codex ID and cwd, never --last.
rm -f "$FAKE_TMUX_STATE"
export WARP_RECOVERY_TEST_GENERATION=warp-e
register_launch codex codex-api /srv/api
TMUX=/tmp/tmux TMUX_PANE=%7 \
  printf '%s\n' '{"session_id":"019abcdef","cwd":"/srv/api"}' | \
  TMUX=/tmp/tmux TMUX_PANE=%7 "$TOOL" register-agent codex >/dev/null
export WARP_RECOVERY_TEST_GENERATION=warp-f
[[ "$("$TOOL" claim)" == $'attach\tcodex-api' ]] || fail 'did not recreate dead Codex tmux session'
python3 - "$FAKE_TMUX_STATE" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))["sessions"]["codex-api"]
cmd = row["command"]
assert row["cwd"] == "/srv/api", row
assert "resume" in cmd and "019abcdef" in cmd, cmd
assert "--last" not in cmd, cmd
assert str(__import__('pathlib').Path(__import__('os').environ["WARP_RECOVERY_CONFIG_DIR"]) / "codex-g.sh") in cmd, cmd
assert any('g "$@"' in arg for arg in cmd), cmd
assert "codex" not in cmd, cmd
assert "WARP_TERMINAL_SESSION_UUID=11111111111111111111111111111111" in cmd, cmd
assert "WARP_IS_LOCAL_SHELL_SESSION=1" in cmd, cmd
PY

# A dead Claude session uses its exact ID and never --continue.
rm -f "$FAKE_TMUX_STATE"
export WARP_TERMINAL_SESSION_UUID=33333333333333333333333333333333
export WARP_RECOVERY_TEST_GENERATION=warp-g
register_launch claude claude-web /srv/web
printf '%s\n' '{"session_id":"claude-session-9","cwd":"/srv/web"}' | \
  TMUX=/tmp/tmux TMUX_PANE=%8 "$TOOL" register-agent claude >/dev/null
export WARP_RECOVERY_TEST_GENERATION=warp-h
[[ "$("$TOOL" claim)" == $'attach\tclaude-web' ]] || fail 'did not recreate dead Claude tmux session'
python3 - "$FAKE_TMUX_STATE" <<'PY'
import json, sys
cmd = json.load(open(sys.argv[1]))["sessions"]["claude-web"]["command"]
assert "--resume" in cmd and "claude-session-9" in cmd, cmd
assert "--continue" not in cmd, cmd
assert str(__import__('pathlib').Path(__import__('os').environ["WARP_RECOVERY_CONFIG_DIR"]) / "claude-c.sh") in cmd, cmd
assert any('c "$@"' in arg for arg in cmd), cmd
assert "claude" not in cmd, cmd
assert "WARP_TERMINAL_SESSION_UUID=33333333333333333333333333333333" in cmd, cmd
assert "WARP_IS_LOCAL_SHELL_SESSION=1" in cmd, cmd
PY

# A dead OpenCode session resumes the exact session id via `o -- -s <id>`.
rm -f "$FAKE_TMUX_STATE"
export WARP_TERMINAL_SESSION_UUID=44444444444444444444444444444444
export WARP_RECOVERY_TEST_GENERATION=warp-o1
register_launch opencode opencode-svc /srv/svc
printf '%s\n' '{"session_id":"ses_oc42","cwd":"/srv/svc"}' | \
  TMUX=/tmp/tmux TMUX_PANE=%9 "$TOOL" register-agent opencode >/dev/null
export WARP_RECOVERY_TEST_GENERATION=warp-o2
[[ "$("$TOOL" claim)" == $'attach\topencode-svc' ]] || fail 'did not recreate dead OpenCode tmux session'
python3 - "$FAKE_TMUX_STATE" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))["sessions"]["opencode-svc"]
cmd = row["command"]
assert row["cwd"] == "/srv/svc", row
assert "-s" in cmd and "ses_oc42" in cmd, cmd
assert "--continue" not in cmd and "--last" not in cmd, cmd
assert str(__import__('pathlib').Path(__import__('os').environ["WARP_RECOVERY_CONFIG_DIR"]) / "opencode-o.sh") in cmd, cmd
assert any('o "$@"' in arg for arg in cmd), cmd
assert "opencode" not in cmd, cmd
assert "WARP_TERMINAL_SESSION_UUID=44444444444444444444444444444444" in cmd, cmd
assert "WARP_IS_LOCAL_SHELL_SESSION=1" in cmd, cmd
PY

# Concurrent shells race on one claim; exactly one wins.
export WARP_RECOVERY_TEST_GENERATION=warp-i
outputs="$TMP/claims"
mkdir "$outputs"
for i in $(seq 1 20); do "$TOOL" claim >"$outputs/$i" & done
wait
claim_count="$(grep -l '^attach' "$outputs"/* | awk 'END { print NR }')"
[[ "$claim_count" == 1 ]] || fail 'concurrent claim was not one-shot'

# Malformed/non-Warp contexts are silent, and status stores no content.
export WARP_TERMINAL_SESSION_UUID=bad
[[ -z "$("$TOOL" claim)" ]] || fail 'accepted malformed Warp UUID'
unset WARP_IS_LOCAL_SHELL_SESSION
[[ -z "$("$TOOL" claim)" ]] || fail 'claimed outside a local Warp shell'
status="$("$TOOL" status --json)"
[[ "$status" != *prompt* && "$status" != *transcript* ]] || fail 'status leaked conversation content'

printf 'warp-agent-recovery contract: PASS\n'
