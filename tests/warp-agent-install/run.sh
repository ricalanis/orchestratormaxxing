#!/usr/bin/env bash
set -euo pipefail
# Exercise non-login/container environments too; installer derives the account.
unset USER

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HOME_DIR="$TMP/home"
STUBS="$TMP/stubs"
mkdir -p "$HOME_DIR/.codex" "$HOME_DIR/.claude" "$HOME_DIR/.config/opencode" "$STUBS"
PYTHON3_BIN="$(command -v python3)"
ln -s "$PYTHON3_BIN" "$STUBS/python3"

fail() { printf 'warp-agent-install contract: %s\n' "$*" >&2; exit 1; }

cat > "$HOME_DIR/.codex/config.toml" <<'TOML'
model = "keep-me"

[tui]
notification_condition = "never" # preserve this comment
raw_output_mode = true

[unrelated]
value = 42
TOML
cat > "$HOME_DIR/.claude/settings.json" <<'JSON'
{
  "unrelated": {"keep": true},
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "printf unrelated"}]},
      {"hooks": [{"type": "command", "command": "bash /old/repo/orchestrator/hooks/tmux-stamp.sh"}]}
    ],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 /old/repo/orchestrator/hooks/orch-notify.py"}]}],
    "Notification": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 /old/repo/orchestrator/hooks/orch-notify.py"}]}],
    "PreCompact": [{"hooks": [{"type": "command", "command": "python3 /old/repo/orchestrator/hooks/orch-notify.py"}]}]
  }
}
JSON
cat > "$HOME_DIR/.codex/hooks.json" <<'JSON'
{
  "keep_top_level": true,
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "printf codex-unrelated"}]},
      {"hooks": [{"type": "command", "command": "o=\"$(loop-tick --gate --quiet)\"; true"}]},
      {"hooks": [
        {"type": "command", "command": "session-log tail 2>/dev/null; true"},
        {"type": "command", "command": "printf mixed-unrelated"}
      ]},
      {"hooks": [{"type": "command", "command": "warp-agent-event claude session_start; true"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "mem-audit; true"}]},
      {"hooks": [{"type": "command", "command": "printf codex-stop-unrelated"}]}
    ]
  }
}
JSON
printf 'set-option -g status on\n' > "$HOME_DIR/.tmux.conf"
: > "$HOME_DIR/.bashrc"
: > "$HOME_DIR/.zshrc"

cat > "$STUBS/codex" <<'SH'
#!/bin/sh
case "$*" in
  'plugin marketplace list --json')
    printf '{"marketplaces":[{"root":"%s"}]}\n' "$STUB_REPO" ;;
  'plugin list --json')
    printf '{"installed":[{"pluginId":"orchestratormaxxing@personal","installed":true,"enabled":true,"version":"%s"}]}\n' "$STUB_VERSION" ;;
  *) printf '{}\n' ;;
esac
SH
cat > "$STUBS/tmux" <<'SH'
#!/bin/sh
case "$1" in list-sessions) exit 1 ;; *) exit 0 ;; esac
SH
cat > "$STUBS/systemctl" <<'SH'
#!/bin/sh
case "$*" in *is-enabled*) exit 1 ;; *) exit 0 ;; esac
SH
cat > "$STUBS/launchctl" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LAUNCHCTL_LOG"
case "$1" in
  list)
    printf '%s\n' '- 0 com.orchestratormaxxing.loop'
    ;;
  print)
    # Emulates a LOADED agent. install.sh re-syncs only an agent whose plist path is the
    # file it just wrote under $HOME (ownership guard), so the path we report decides
    # whether bootout/bootstrap may happen: STUB_LAUNCHCTL_HOME is the fixture home by
    # default and is pointed at a foreign home in the negative case below.
    case "${2:-}" in
      gui/*/com.orchestratormaxxing.loop|gui/*/com.orchestratormaxxing.cogload-nightly|gui/*/com.orchestratormaxxing.cogload-catchup)
        _label="${2##*/}"
        printf '\tpath = %s/Library/LaunchAgents/%s.plist\n\tstate = not running\n' "${STUB_LAUNCHCTL_HOME:?}" "$_label"
        exit 0
        ;;
      *) exit 1 ;;
    esac
    ;;
  bootout|bootstrap) exit 0 ;;
  *) exit 1 ;;
esac
SH
chmod +x "$STUBS/codex" "$STUBS/tmux" "$STUBS/systemctl" "$STUBS/launchctl"

# install.sh reloads already-armed schedulers on Darwin.  The contract must
# never let its disposable HOME reach the operator's real launchd domain.
test -x "$STUBS/launchctl" \
  || fail 'launchctl must be fixture-isolated before install.sh runs'

export STUB_REPO="$ROOT"
export STUB_VERSION
export STUB_LAUNCHCTL_LOG="$TMP/launchctl.log"
export STUB_LAUNCHCTL_HOME="$HOME_DIR"
STUB_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "$ROOT/plugins/orchestratormaxxing/.codex-plugin/plugin.json")"

run_install() {
  HOME="$HOME_DIR" CODEX_HOME="$HOME_DIR/.codex" \
    ORCHESTRATORMAXXING_SKIP_EXTERNAL_SKILLS=1 \
    PATH="$STUBS:/usr/bin:/bin" bash "$ROOT/install.sh" >"$TMP/install.out" 2>"$TMP/install.err" || {
      tail -n 30 "$TMP/install.err" >&2
      fail 'install.sh failed in disposable home'
    }
}

run_install
mkdir -p "$TMP/first"
for path in \
  "$HOME_DIR/.codex/config.toml" \
  "$HOME_DIR/.codex/hooks.json" \
  "$HOME_DIR/.claude/settings.json" \
  "$HOME_DIR/.tmux.conf" \
  "$HOME_DIR/.bashrc" \
  "$HOME_DIR/.zshrc"; do
  sha256sum "$path"
  cp "$path" "$TMP/first/$(basename "$path").snapshot"
done > "$TMP/first.sha"
run_install
for path in \
  "$HOME_DIR/.codex/config.toml" \
  "$HOME_DIR/.codex/hooks.json" \
  "$HOME_DIR/.claude/settings.json" \
  "$HOME_DIR/.tmux.conf" \
  "$HOME_DIR/.bashrc" \
  "$HOME_DIR/.zshrc"; do
  sha256sum "$path"
done > "$TMP/second.sha"
if ! cmp -s "$TMP/first.sha" "$TMP/second.sha"; then
  for path in \
    "$HOME_DIR/.codex/config.toml" \
    "$HOME_DIR/.codex/hooks.json" \
    "$HOME_DIR/.claude/settings.json" \
    "$HOME_DIR/.tmux.conf" \
    "$HOME_DIR/.bashrc" \
    "$HOME_DIR/.zshrc"; do
    diff -u "$TMP/first/$(basename "$path").snapshot" "$path" || true
  done >&2
  fail 'second install changed managed configuration'
fi

if [ "$(uname -s)" = Darwin ]; then
  for label in com.orchestratormaxxing.loop \
               com.orchestratormaxxing.cogload-nightly \
               com.orchestratormaxxing.cogload-catchup; do
    grep -Fq "bootout gui/$(id -u)/$label" "$STUB_LAUNCHCTL_LOG" \
      || fail "Darwin install did not exercise bootout for $label"
    grep -Fq "bootstrap gui/$(id -u) $HOME_DIR/Library/LaunchAgents/$label.plist" \
      "$STUB_LAUNCHCTL_LOG" \
      || fail "Darwin install did not exercise fixture bootstrap for $label"
  done
  if grep -Fq "$HOME/Library/LaunchAgents" "$STUB_LAUNCHCTL_LOG"; then
    fail 'disposable install touched the operator LaunchAgents path'
  fi
  # Ownership guard (negative): the loaded agents belong to ANOTHER home → the installer
  # must write its plists but never bootout/bootstrap them (a fixture, a proof, or a second
  # account must not hijack the real agents). Proven red against the pre-guard installer.
  : > "$STUB_LAUNCHCTL_LOG"
  STUB_LAUNCHCTL_HOME="/Users/someone-else" HOME="$HOME_DIR" CODEX_HOME="$HOME_DIR/.codex" \
    PATH="$STUBS:$PATH" ORCHESTRATORMAXXING_SKIP_EXTERNAL_SKILLS=1 bash "$ROOT/install.sh" >/dev/null 2>&1 || true
  if grep -Eq "bootout|bootstrap" "$STUB_LAUNCHCTL_LOG"; then
    fail "install re-armed a launch agent owned by another home: $(grep -E 'bootout|bootstrap' "$STUB_LAUNCHCTL_LOG" | head -2)"
  fi
  grep -Fq "another home" "$TMP/install-foreign.log" 2>/dev/null || true
fi

python3 - "$HOME_DIR" "$ROOT" <<'PY'
import json, pathlib, sys, tomllib
home = pathlib.Path(sys.argv[1])
cfg_text = (home / ".codex/config.toml").read_text()
cfg = tomllib.loads(cfg_text)
assert cfg["model"] == "keep-me"
assert cfg["unrelated"]["value"] == 42
assert cfg["tui"]["notification_condition"] == "always"
assert "raw_output_mode" not in cfg["tui"]
assert "# preserve this comment" in cfg_text

settings = json.loads((home / ".claude/settings.json").read_text())
assert settings["unrelated"] == {"keep": True}
commands = [
    hook.get("command", "")
    for groups in settings["hooks"].values()
    for group in groups
    for hook in group.get("hooks", [])
]
assert commands.count('printf unrelated') == 1
assert not any("orchestrator/hooks/orch-notify.py" in c or
               "orchestrator/hooks/tmux-stamp.sh" in c for c in commands), commands
for event in ("session_start", "prompt_submit", "stop", "permission_request"):
    matches = [c for c in commands if "warp-agent-event" in c and f"claude {event}" in c]
    assert len(matches) == 1, (event, matches)
recovery = [c for c in commands if "warp-agent-recovery" in c and "register-agent claude" in c]
assert len(recovery) == 1, recovery
watchers = [c for c in commands if "loop-tick" in c]
assert len(watchers) == 1 and "--kick" in watchers[0] and "--gate" not in watchers[0], watchers

codex_hooks = json.loads((home / ".codex/hooks.json").read_text())
assert codex_hooks["keep_top_level"] is True
codex_commands = [
    hook.get("command", "")
    for groups in codex_hooks["hooks"].values()
    for group in groups
    for hook in group.get("hooks", [])
]
assert sorted(codex_commands) == sorted([
    "printf codex-unrelated", "printf mixed-unrelated", "printf codex-stop-unrelated"
]), codex_commands
backup = home / ".codex/hooks.json.pre-orchestratormaxxing-plugin"
assert backup.is_file(), "legacy Codex hooks were removed without a recoverable backup"
backup_commands = [
    hook.get("command", "")
    for groups in json.loads(backup.read_text())["hooks"].values()
    for group in groups
    for hook in group.get("hooks", [])
]
assert any("loop-tick --gate" in c for c in backup_commands), backup_commands

plugin_hooks = json.loads((pathlib.Path(sys.argv[2]) / "plugins/orchestratormaxxing/hooks/hooks.json").read_text())
plugin_watchers = [
    hook.get("command", "")
    for group in plugin_hooks["hooks"]["SessionStart"]
    for hook in group.get("hooks", [])
    if "loop-tick" in hook.get("command", "")
]
assert len(plugin_watchers) == 1 and "--kick" in plugin_watchers[0] and "--gate" not in plugin_watchers[0], plugin_watchers

tmux = (home / ".tmux.conf").read_text()
assert tmux.count("# >>> orchestratormaxxing:tmux >>>") == 1
assert "set-option -g status on" in tmux
managed_tmux = (pathlib.Path(sys.argv[2]) / "shell/tmux.conf").read_text()
assert "set-option -s set-clipboard external" in managed_tmux
for rc in (home / ".bashrc", home / ".zshrc"):
    text = rc.read_text()
    assert text.count("# >>> orchestratormaxxing:c-command >>>") == 1
    assert text.count("warp-recovery.sh") == 2

names = ["warp-agent-event", "warp-agent-recovery", "codex-stop-hook", "tmux-send", "o", "intent-queue"]
if (pathlib.Path(sys.argv[2]) / "install-fleet.sh").is_file():   # fleet bridges ship only from the private half
    names += ["gpu-agent", "harness-remote"]
for name in names:
    deployed = home / ".local/bin" / name
    assert deployed.is_file()
    assert deployed.stat().st_mode & 0o111
    if name == "intent-queue":
        assert deployed.read_bytes() == (pathlib.Path(sys.argv[2]) / "bin" / name).read_bytes()

for name in ("warp-recovery.sh", "claude-c.sh", "codex-g.sh", "opencode-o.sh"):
    deployed = home / ".config/orchestratormaxxing" / name
    source = pathlib.Path(sys.argv[2]) / "shell" / name
    assert deployed.read_bytes() == source.read_bytes(), name
assert "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1" in (
    home / ".config/orchestratormaxxing/claude-c.sh").read_text()
installed_c = (home / ".config/orchestratormaxxing/claude-c.sh").read_text()
installed_g = (home / ".config/orchestratormaxxing/codex-g.sh").read_text()
assert "linux_normal_screen" in installed_c
assert "codex_tui_args+=(--no-alt-screen)" in installed_g
assert '"$(uname -s 2>/dev/null)" == "Linux"' in installed_c
assert '"$(uname -s 2>/dev/null)" == "Linux"' in installed_g

plugin = home / ".config/opencode/plugins/orchestratormaxxing-notify.js"
assert plugin.is_file()
plugin_text = plugin.read_text()
assert "warp-agent-recovery register-agent opencode" in plugin_text
PY

for shell_bin in bash zsh; do
  command -v "$shell_bin" >/dev/null 2>&1 || continue
  rc="$HOME_DIR/.$shell_bin"rc
  shell_flags=(--no-rcs)
  type_check='whence -w c-ubuntu | grep function >/dev/null; whence -w g-ubuntu | grep function >/dev/null; whence -w o-ubuntu | grep function >/dev/null'
  [ "$shell_bin" != bash ] || shell_flags=(--noprofile --norc)
  [ "$shell_bin" != bash ] || type_check='test "$(type -t c-ubuntu)" = function; test "$(type -t g-ubuntu)" = function; test "$(type -t o-ubuntu)" = function'
  HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$STUBS:/usr/bin:/bin" \
    "$shell_bin" "${shell_flags[@]}" -c '. "$1"; eval "$2"' \
    shell "$rc" "$type_check" || fail "$shell_bin did not load c-ubuntu/g-ubuntu/o-ubuntu from the installed rc block"
done

printf 'warp-agent-install contract: PASS\n'
