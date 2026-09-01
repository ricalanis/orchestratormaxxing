#!/usr/bin/env bash
set -euo pipefail

. "$(cd "$(dirname "$0")/../.." && pwd)/tests/lib/precondition.sh"
harness_need_cmd tmux "agent-tab-status contract: tmux"

# The private tmux server must run under a UTF-8 locale or it renders the
# Unicode status markers (… ○) as '_' — minimal environments ship no LANG.
# No grep -q on pipes here: under pipefail an early grep exit turns a match
# into SIGPIPE/141 (bitten once already).
case "$(locale 2>/dev/null)" in
  *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;;
  *)
    avail="$(locale -a 2>/dev/null || true)"
    for l in C.UTF-8 en_US.UTF-8; do
      case "$avail" in *"$l"*) export LC_ALL="$l"; break ;; esac
    done
    ;;
esac

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/agent-tab-status"
SOCKET="agent-tab-status-$$"

cleanup() {
  while IFS= read -r session; do
    [[ -n "$session" ]] && tmux -L "$SOCKET" kill-session -t "=$session" 2>/dev/null || true
  done < <(tmux -L "$SOCKET" list-sessions -F '#{session_name}' 2>/dev/null || true)
  rm -f "/tmp/tmux-$(id -u)/$SOCKET" 2>/dev/null || true
}
trap cleanup EXIT

fail() { printf 'agent-tab-status contract: %s\n' "$*" >&2; exit 1; }
window_name() { tmux -L "$SOCKET" display-message -p -t "=$1:" '#W'; }
pane_id() { tmux -L "$SOCKET" display-message -p -t "=$1:" '#{pane_id}'; }

# A hook outside tmux is an entirely silent successful no-op.
out="$(env -u TMUX -u TMUX_PANE "$TOOL" working 2>&1)" || fail 'non-tmux call failed'
[[ -z "$out" ]] || fail 'non-tmux call emitted output'

tmux -L "$SOCKET" new-session -d -s claude-alpha 'sleep 30'
tmux -L "$SOCKET" new-session -d -s codex-beta 'sleep 30'
tmux -L "$SOCKET" new-session -d -s shell-gamma 'sleep 30'
tmux -L "$SOCKET" new-session -d -s 'claude-weird;name' 'sleep 30'
tmux_env="$(tmux -L "$SOCKET" display-message -p -t =claude-alpha: '#{socket_path},#{pid},0')"
alpha="$(pane_id claude-alpha)"
beta="$(pane_id codex-beta)"
gamma="$(pane_id shell-gamma)"
weird="$(pane_id 'claude-weird;name')"

TMUX="$tmux_env" "$TOOL" working "$alpha"
[[ "$(window_name claude-alpha)" == '… alpha' ]] || fail 'Claude working title mismatch'

TMUX="$tmux_env" "$TOOL" attention "$beta"
[[ "$(window_name codex-beta)" == '!!! beta' ]] || fail 'Codex attention title mismatch'
[[ "$(window_name claude-alpha)" == '… alpha' ]] || fail 'cross-session title contamination'

TMUX="$tmux_env" "$TOOL" idle "$alpha"
[[ "$(window_name claude-alpha)" == '○ alpha' ]] || fail 'idle title mismatch'
[[ "$(tmux -L "$SOCKET" show-options -wv -t "$alpha" automatic-rename)" == 'off' ]] || fail 'automatic rename remains enabled'
[[ "$(tmux -L "$SOCKET" show-options -wv -t "$alpha" allow-rename)" == 'off' ]] || fail 'application rename remains enabled'

before="$(window_name shell-gamma)"
TMUX="$tmux_env" "$TOOL" attention "$gamma"
[[ "$(window_name shell-gamma)" == "$before" ]] || fail 'non-agent tmux window was renamed'

TMUX="$tmux_env" "$TOOL" working "$weird"
[[ "$(window_name 'claude-weird;name')" == '… weird name' ]] || fail 'odd session name was not sanitized'

if "$TOOL" impossible >/dev/null 2>&1; then
  fail 'invalid state was accepted'
fi

python3 - "$ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
hooks = json.loads((root / "plugins/claudemaxxing/hooks/hooks.json").read_text())["hooks"]
for event in ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PreToolUse", "PostToolUse", "Stop"):
    assert hooks.get(event), f"Codex hook event missing: {event}"
commands = [h.get("command", "") for groups in hooks.values() for group in groups for h in group.get("hooks", [])]
assert any("agent-tab-status" in c and " working" in c for c in commands)
assert any("agent-tab-status" in c and " attention" in c for c in commands)

install = (root / "install.sh").read_text()
for token in (
    'bin/agent-tab-status', 'UserPromptSubmit', 'PermissionRequest',
    'AskUserQuestion|ExitPlanMode', 'agent-tab-status',
    ' attention 2>/dev/null', ' working 2>/dev/null',
):
    assert token in install, f"install wiring missing: {token}"

for launcher in ("shell/claude-c.sh", "shell/codex-g.sh"):
    text = (root / launcher).read_text()
    assert "WARP_DISABLE_AUTO_TITLE=true" in text, f"Warp title ownership missing: {launcher}"
    assert "agent-tab-status idle" in text, f"initial tab state missing: {launcher}"

tmux_conf = (root / "shell/tmux.conf").read_text()
assert "set-option -g set-titles on" in tmux_conf
assert "set-option -g set-titles-string '#W'" in tmux_conf
PY

printf 'agent-tab-status contract: PASS\n'
