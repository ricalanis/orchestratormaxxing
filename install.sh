#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# claudemaxxing install — deploy the orchestrator harness GLOBALLY (standalone).
#
# Makes the agentic layer available in EVERY Claude Code and Codex session, with no
# references back to this repo's path:
#   • bridges  → ~/.local/bin/{oll,oll-council,oll-sync,occ,worker-path-bench,ticket-route,provider-ask,multi-council,cross-review,xsearch,mem-audit,memory-bridge-hermes.sh,loop-queue,loop-tick,intent-queue,hermes-watch,kpi-brief,capacity,token-ledger,model-catalog,model-bench,model-eval,win-log,delegate-ledger,warp-ollama,warp-model-pin,zed-setup,session-log,task-plan,project-new,drive,gpu-desktop,tmux-send,harness-agent-run,opencode-browser-mcp,gcloud,coolify,gauntlet-judge}  (on PATH)
#   • agents   → ~/.claude/agents/{ollama-worker,product-manager,fable-planner}.md
#   • commands → ~/.claude/commands/{fanout,ideas,self-improve,wrap-up,fableplan,plan-to-repo,gauntlet}.md (bare `oll`/`session-log`)
#   • codex    → repo marketplace plugin + ~/.codex/agents/*.toml
#   • shell    → ~/.config/claudemaxxing/{claude-c,codex-g}.sh + tmux.conf
#   • doctrine → ~/.claude/CLAUDE.md + ~/.codex/AGENTS.md (compact marked block)
#   • xAI key  → ~/.config/claudemaxxing/.env   (standalone key source for xsearch)
#
# Keys (ollama key lives in OpenCode's auth store, already global):
#   export XAI_API_KEY=...   # optional; falls back to repo .env if present
#
# Idempotent — safe to re-run after editing the repo to re-sync global.
#   Run from inside the repo:  ./install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
say(){ printf "\n\033[1;36m== %s\033[0m\n" "$*"; }
# 0 iff launchd agent $1 is loaded AND its plist is exactly $2 (i.e. owned by THIS $HOME).
_launchd_owned_here(){
  local _p
  _p="$(launchctl print "gui/$(id -u)/$1" 2>/dev/null | awk -F' = ' '/^[[:space:]]*path = /{print $2; exit}')"
  [ -n "$_p" ] && [ "$_p" = "$2" ]
}

BIN_DST="$HOME/.local/bin"
CLAUDE_DIR="$HOME/.claude"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CFG_DIR="$HOME/.config/claudemaxxing"
SHARE_DIR="$HOME/.local/share/claudemaxxing"

say "Retired Claude → Codex control plane cleanup"
# These exact artifacts belonged to the retired custom orch-* layer. Pruning is
# deliberately narrow: c/g, tmux-send, and the Hermes dashboard remain untouched.
rm -f -- "$BIN_DST/orch-ledger" "$BIN_DST/orch-dispatch" \
  "$BIN_DST/orch-monitor" "$BIN_DST/orch-escalate" \
  "$CLAUDE_DIR/commands/orchestrate.md"
python3 - "$CODEX_DIR/plugins/cache/personal/claudemaxxing" <<'PY'
import os, shutil, sys

root = os.path.realpath(sys.argv[1])
if os.path.isdir(root):
    for version in os.scandir(root):
        if not version.is_dir(follow_symlinks=False):
            continue
        target = os.path.join(version.path, "skills", "execute")
        resolved = os.path.realpath(target)
        expected_parent = os.path.realpath(os.path.join(version.path, "skills"))
        if (os.path.isdir(target) and not os.path.islink(target)
                and os.path.dirname(resolved) == expected_parent
                and os.path.commonpath((root, resolved)) == root):
            shutil.rmtree(target)
PY

say "Bridges → $BIN_DST (on PATH)"
mkdir -p "$BIN_DST"
cp "$REPO_DIR/bin/oll"         "$BIN_DST/oll"
cp "$REPO_DIR/bin/oll-council" "$BIN_DST/oll-council"
cp "$REPO_DIR/bin/oll-sync"    "$BIN_DST/oll-sync"
cp "$REPO_DIR/bin/occ"         "$BIN_DST/occ"
cp "$REPO_DIR/bin/o"           "$BIN_DST/o"
cp "$REPO_DIR/bin/agent-tab-status" "$BIN_DST/agent-tab-status"
cp "$REPO_DIR/bin/warp-agent-event" "$BIN_DST/warp-agent-event"
cp "$REPO_DIR/bin/warp-agent-recovery" "$BIN_DST/warp-agent-recovery"
cp "$REPO_DIR/bin/codex-stop-hook" "$BIN_DST/codex-stop-hook"
cp "$REPO_DIR/bin/agent-done-notify" "$BIN_DST/agent-done-notify"
cp "$REPO_DIR/bin/ticket-route" "$BIN_DST/ticket-route"
cp "$REPO_DIR/bin/provider-ask"  "$BIN_DST/provider-ask"
cp "$REPO_DIR/bin/multi-council"  "$BIN_DST/multi-council"
cp "$REPO_DIR/bin/cross-review"   "$BIN_DST/cross-review"
cp "$REPO_DIR/bin/mem-audit"   "$BIN_DST/mem-audit"
cp "$REPO_DIR/bin/memoryctl"   "$BIN_DST/memoryctl"
cp "$REPO_DIR/bin/harness-sync" "$BIN_DST/harness-sync"
cp "$REPO_DIR/bin/core-export" "$BIN_DST/core-export"
cp "$REPO_DIR/bin/mut"         "$BIN_DST/mut"
cp "$REPO_DIR/bin/harness-verify" "$BIN_DST/harness-verify"
cp "$REPO_DIR/bin/harness-scan"   "$BIN_DST/harness-scan"
cp "$REPO_DIR/bin/loop-queue"     "$BIN_DST/loop-queue"
cp "$REPO_DIR/bin/loop-tick"      "$BIN_DST/loop-tick"
cp "$REPO_DIR/bin/token-ledger"   "$BIN_DST/token-ledger"
cp "$REPO_DIR/bin/model-catalog"  "$BIN_DST/model-catalog"
cp "$REPO_DIR/bin/model-bench"    "$BIN_DST/model-bench"
cp "$REPO_DIR/bin/model-eval"     "$BIN_DST/model-eval"
cp "$REPO_DIR/bin/win-log"        "$BIN_DST/win-log"
cp "$REPO_DIR/bin/delegate-ledger" "$BIN_DST/delegate-ledger"
cp "$REPO_DIR/bin/warp-ollama"    "$BIN_DST/warp-ollama"
cp "$REPO_DIR/bin/zed-setup"      "$BIN_DST/zed-setup"
cp "$REPO_DIR/bin/warp-model-pin" "$BIN_DST/warp-model-pin"
cp "$REPO_DIR/bin/session-log"    "$BIN_DST/session-log"
cp "$REPO_DIR/bin/sync-agent-skills" "$BIN_DST/sync-agent-skills"
cp "$REPO_DIR/bin/orchestration-practice" "$BIN_DST/orchestration-practice"
cp "$REPO_DIR/bin/cogload"        "$BIN_DST/cogload"
# Global accident barrier: normal tmux commands pass through, while the one
# server-wide destructive command cannot erase every c/g/o session at once.
cp "$REPO_DIR/bin/tmux-guard"    "$BIN_DST/tmux"
cp "$REPO_DIR/bin/tmux-send"     "$BIN_DST/tmux-send"
cp "$REPO_DIR/bin/harness-agent-run" "$BIN_DST/harness-agent-run"
cp "$REPO_DIR/bin/opencode-browser-mcp" "$BIN_DST/opencode-browser-mcp"
cp "$REPO_DIR/bin/browser-mcp-contract" "$BIN_DST/browser-mcp-contract"
cp "$REPO_DIR/bin/gauntlet-judge" "$BIN_DST/gauntlet-judge"

cp "$REPO_DIR/xsearch.py"      "$BIN_DST/xsearch"
mkdir -p "$SHARE_DIR"
chmod +x "$BIN_DST/o"
rm -rf -- "$SHARE_DIR/orchestration_practices"
cp -R "$REPO_DIR/orchestration_practices" "$SHARE_DIR/orchestration_practices"
cp "$REPO_DIR/prompts/context/compact.md" "$SHARE_DIR/context-compact.md"
chmod +x "$BIN_DST/oll" "$BIN_DST/oll-council" "$BIN_DST/oll-sync" "$BIN_DST/occ" "$BIN_DST/agent-tab-status" "$BIN_DST/warp-agent-event" "$BIN_DST/warp-agent-recovery" "$BIN_DST/codex-stop-hook" "$BIN_DST/agent-done-notify" "$BIN_DST/ticket-route" "$BIN_DST/provider-ask" "$BIN_DST/multi-council" "$BIN_DST/cross-review" "$BIN_DST/mem-audit" "$BIN_DST/memoryctl" "$BIN_DST/harness-sync" "$BIN_DST/core-export" "$BIN_DST/mut" "$BIN_DST/harness-verify" "$BIN_DST/harness-scan" "$BIN_DST/loop-queue" "$BIN_DST/loop-tick" "$BIN_DST/token-ledger" "$BIN_DST/model-catalog" "$BIN_DST/model-bench" "$BIN_DST/model-eval" "$BIN_DST/win-log" "$BIN_DST/delegate-ledger" "$BIN_DST/warp-ollama" "$BIN_DST/warp-model-pin" "$BIN_DST/zed-setup" "$BIN_DST/session-log" "$BIN_DST/sync-agent-skills" "$BIN_DST/orchestration-practice" "$BIN_DST/cogload" "$BIN_DST/tmux" "$BIN_DST/tmux-send" "$BIN_DST/harness-agent-run" "$BIN_DST/opencode-browser-mcp" "$BIN_DST/browser-mcp-contract" "$BIN_DST/gauntlet-judge" "$BIN_DST/xsearch"

# bin/chrome-debug-wayland-shim → shadows google-chrome(-stable) on PATH (Linux only).
# Any launch carrying --remote-debugging-port under a Wayland session gets
# --ozone-platform=wayland injected — X11 Chromium is keyboard-dead for humans on the
# GNOME-Wayland stack (measured 2026-07-13; knowledge/opencode-browser-connector.md).
# Passthrough otherwise; absolute-path launches (/opt/google/chrome/chrome) bypass it.
if [ "$(uname -s)" = "Linux" ]; then
  cp "$REPO_DIR/bin/chrome-debug-wayland-shim" "$BIN_DST/google-chrome"
  cp "$REPO_DIR/bin/chrome-debug-wayland-shim" "$BIN_DST/google-chrome-stable"
  chmod +x "$BIN_DST/google-chrome" "$BIN_DST/google-chrome-stable"
  # bin/chrome-debug → one entry point for the account-integrated CDP Chrome (:18800).
  # Idempotent: no-ops if the endpoint is up, else restarts chrome-cdp.service, else
  # manual launch on the canonical clawdbot profile. All harnesses reference it.
  cp "$REPO_DIR/bin/chrome-debug" "$BIN_DST/chrome-debug"
  chmod +x "$BIN_DST/chrome-debug"
fi
case ":$PATH:" in
  *":$BIN_DST:"*) : ;;
  *) printf '  \033[1;33mNOTE\033[0m %s is not on PATH — add it to your shell profile.\n' "$BIN_DST" ;;
esac

say "Agent + commands → $CLAUDE_DIR"
mkdir -p "$CLAUDE_DIR/agents" "$CLAUDE_DIR/commands"
cp "$REPO_DIR/.claude/agents/ollama-worker.md" "$CLAUDE_DIR/agents/ollama-worker.md"
cp "$REPO_DIR/.claude/agents/fable-planner.md" "$CLAUDE_DIR/agents/fable-planner.md"
cp "$REPO_DIR/.claude/commands/fanout.md"      "$CLAUDE_DIR/commands/fanout.md"
cp "$REPO_DIR/.claude/commands/ideas.md"       "$CLAUDE_DIR/commands/ideas.md"
cp "$REPO_DIR/.claude/commands/self-improve.md" "$CLAUDE_DIR/commands/self-improve.md"
cp "$REPO_DIR/.claude/commands/wrap-up.md"     "$CLAUDE_DIR/commands/wrap-up.md"
cp "$REPO_DIR/.claude/commands/fableplan.md"   "$CLAUDE_DIR/commands/fableplan.md"
cp "$REPO_DIR/.claude/commands/gauntlet.md"    "$CLAUDE_DIR/commands/gauntlet.md"
cp "$REPO_DIR/.claude/commands/cheap-delegate.md" "$CLAUDE_DIR/commands/cheap-delegate.md"
cp "$REPO_DIR/.claude/commands/graduate.md"    "$CLAUDE_DIR/commands/graduate.md"

say "Governed skill stack → Claude, Codex, OpenCode, and production Hermes"
# ADOPT + EXTEND: upstream payloads stay pinned in skills/external-stack.json;
# this repo supplies the four-host lifecycle and the automatic anti-slop router.
# The explicit skip is for offline installer contracts/recovery, not normal setup.
if [ "${CLAUDEMAXXING_SKIP_EXTERNAL_SKILLS:-0}" = "1" ]; then
  echo "  skipped pinned skill stack (CLAUDEMAXXING_SKIP_EXTERNAL_SKILLS=1)"
else
  # Internal skills (skills/anti-slop-design, skills/orchestration-practices) and the
  # core claudemaxxing workflow skills are entries in the same governed manifest;
  # one sync installs them on all four hosts. The explicit source keeps global
  # installs bound to this checkout instead of ~/.local/bin.
  "$BIN_DST/sync-agent-skills" \
    --manifest "$REPO_DIR/skills/external-stack.json" \
    --source "local://claudemaxxing=$REPO_DIR" || {
    echo "  ERROR: pinned skill stack installation failed" >&2
    exit 1
  }
  if [ -f "$HOME/.hermes/kanban.db" ]; then
    echo "  Hermes: new sessions discover the skills; active sessions run /reload-skills"
  fi
fi

say "Codex agent roles + claudemaxxing plugin"
mkdir -p "$CODEX_DIR/agents"
# Remove the superseded Codex-only planner copy. Claude's fable-planner remains
# installed separately under ~/.claude/agents.
rm -f "$CODEX_DIR/agents/fable-planner.toml"
for agent in "$REPO_DIR"/.codex/agents/*.toml; do
  [ -f "$agent" ] || continue
  cp "$agent" "$CODEX_DIR/agents/$(basename "$agent")"
done

# The repo marketplace keeps the plugin version-controlled. Register it once,
# then install/reinstall the plugin so Codex loads its skills and hooks from the
# cache. Preserve all unrelated Codex config and auth.
if command -v codex >/dev/null 2>&1; then
  if ! codex plugin marketplace list --json 2>/dev/null | \
      python3 -c 'import json, os, sys; root=os.path.realpath(sys.argv[1]); data=json.load(sys.stdin); raise SystemExit(0 if any(os.path.realpath(m.get("root", "")) == root for m in data.get("marketplaces", [])) else 1)' "$REPO_DIR"; then
    codex plugin marketplace add "$REPO_DIR" >/dev/null 2>&1 || {
      echo "  ERROR: could not register repo Codex marketplace" >&2
      exit 1
    }
  fi
  codex plugin add claudemaxxing@personal --json >/dev/null 2>&1 || {
    echo "  ERROR: could not install claudemaxxing@personal" >&2
    exit 1
  }
  EXPECTED_PLUGIN_VERSION=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' \
    "$REPO_DIR/plugins/claudemaxxing/.codex-plugin/plugin.json")
  codex plugin list --json 2>/dev/null | python3 -c \
    'import json, sys; expected=sys.argv[1]; data=json.load(sys.stdin); raise SystemExit(0 if any(p.get("pluginId") == "claudemaxxing@personal" and p.get("installed") is True and p.get("enabled") is True and p.get("version") == expected for p in data.get("installed", [])) else 1)' \
    "$EXPECTED_PLUGIN_VERSION" || {
      echo "  ERROR: claudemaxxing@personal is not installed+enabled at $EXPECTED_PLUGIN_VERSION after setup" >&2
      exit 1
    }
  echo "  installed and verified claudemaxxing Codex plugin"
else
  echo "  (Codex not installed — plugin registration skipped; bootstrap.sh can install it)"
fi

# Keep Codex's native OSC 9 fallback active even while Warp is focused. Rich
# OSC 777 events from warp-agent-event take precedence once Warp sees them.
# Remove the legacy raw_output_mode key previously managed here so Codex uses
# its normal full-screen TUI; `codex --no-alt-screen` remains an explicit opt-in.
# Change only these TUI keys and preserve all unrelated bytes.
CODEX_CONFIG="$CODEX_DIR/config.toml"
python3 - "$CODEX_CONFIG" <<'PY'
import os, re, sys, tempfile
try:
    import tomllib
except ImportError:  # Python 3.10 on an older Mac: leave valid user config untouched.
    print("  warning: Python tomllib unavailable; Codex notification fallback not changed")
    raise SystemExit(0)

path = sys.argv[1]
try:
    original = open(path, encoding="utf-8").read()
except FileNotFoundError:
    original = ""
if original.strip():
    tomllib.loads(original)

lines = original.splitlines(keepends=True)
header = re.compile(r"^\s*\[([^]]+)\]\s*(?:#.*)?(?:\r?\n)?$")
key = re.compile(r'^(\s*)(notification_condition|raw_output_mode)(\s*=\s*)([^#\r\n]*)(.*?)(\r?\n)?$')
values = {"notification_condition": '"always"'}
section = None
found_tui = False
found_keys = set()
out = []

def missing_setting_lines(newline="\n"):
    return [f"{name} = {value}{newline}" for name, value in values.items()
            if name not in found_keys]

for line in lines:
    match = header.match(line)
    next_section = match.group(1).strip() if match else None
    if match and section == "tui":
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        out.extend(missing_setting_lines(newline))
        found_keys.update(values)
    if match:
        section = next_section
        if section == "tui":
            found_tui = True
    match_key = key.match(line) if section == "tui" else None
    if match_key:
        name = match_key.group(2)
        if name in values:
            newline = match_key.group(6) or ""
            comment = match_key.group(5)
            out.append(match_key.group(1) + name + match_key.group(3) +
                       values[name] + comment + newline)
            found_keys.add(name)
    else:
        out.append(line)

if found_tui and found_keys != set(values):
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += "\n"
    out.extend(missing_setting_lines())
elif not found_tui:
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += "\n"
    if out and "".join(out).strip():
        out.append("\n")
    out.append("[tui]\n")
    out.extend(missing_setting_lines())

updated = "".join(out)
parsed = tomllib.loads(updated)
assert parsed.get("tui", {}).get("notification_condition") == "always"
assert "raw_output_mode" not in parsed.get("tui", {})
if updated != original:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = (os.stat(path).st_mode & 0o777) if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(prefix=".config.toml.warp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(updated)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
print("  Codex notifications enabled; normal full-screen TUI restored")
PY

say "Shell session helpers → $CFG_DIR/{warp-recovery,claude-c,codex-g,opencode-o}.sh (sourced by ~/.bashrc + ~/.zshrc)"
# The `c` / `cs` tmux session helpers. Repo file is the source of truth; deploy a COPY (the same
# re-sync barrier as the bin/ tools) and make each present rc file source it via a marked block.
# We manage ONLY our marked block — idempotent strip-then-append, so re-running keeps rc files in
# sync without duplicating. Works in bash and zsh (the snippet avoids zsh-reserved vars).
mkdir -p "$CFG_DIR"
WARP_RECOVERY_SNIPPET="$CFG_DIR/warp-recovery.sh"
cp "$REPO_DIR/shell/warp-recovery.sh" "$WARP_RECOVERY_SNIPPET"
echo "  wrote $WARP_RECOVERY_SNIPPET"
SHELL_SNIPPET="$CFG_DIR/claude-c.sh"
cp "$REPO_DIR/shell/claude-c.sh" "$SHELL_SNIPPET"
echo "  wrote $SHELL_SNIPPET"
CODEX_SHELL_SNIPPET="$CFG_DIR/codex-g.sh"
cp "$REPO_DIR/shell/codex-g.sh" "$CODEX_SHELL_SNIPPET"
echo "  wrote $CODEX_SHELL_SNIPPET"
OPENCODE_SHELL_SNIPPET="$CFG_DIR/opencode-o.sh"
cp "$REPO_DIR/shell/opencode-o.sh" "$OPENCODE_SHELL_SNIPPET"
echo "  wrote $OPENCODE_SHELL_SNIPPET"
C_BEGIN="# >>> claudemaxxing:c-command >>>"
C_END="# <<< claudemaxxing:c-command <<<"
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -e "$rc" ] || continue
  if grep -qF "$C_BEGIN" "$rc"; then          # idempotent: drop any prior marked block first
    python3 - "$rc" "$C_BEGIN" "$C_END" <<'PY'
import sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(path).read()
i, j = t.find(begin), t.find(end)
if i != -1 and j != -1:
    before = t[:i].rstrip()
    after = t[j + len(end):].lstrip()
    t = before + ("\n" if before and after else "") + after
open(path, "w").write(t)
PY
  fi
  python3 - "$rc" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read().rstrip("\r\n")
open(path, "w").write(text)
PY
  printf '\n%s\nexport PATH="%s:$PATH"\n[ -f "%s" ] && . "%s"\n[ -f "%s" ] && . "%s"\n[ -f "%s" ] && . "%s"\n[ -f "%s" ] && . "%s"\n%s\n' \
    "$C_BEGIN" "$BIN_DST" "$WARP_RECOVERY_SNIPPET" "$WARP_RECOVERY_SNIPPET" "$SHELL_SNIPPET" "$SHELL_SNIPPET" \
    "$CODEX_SHELL_SNIPPET" "$CODEX_SHELL_SNIPPET" "$OPENCODE_SHELL_SNIPPET" "$OPENCODE_SHELL_SNIPPET" "$C_END" >> "$rc"
  echo "  sourced from $rc"
done

# Trackpad/wheel scrolling for full-screen Claude/Codex TUIs. Keep the actual
# settings in a copied harness fragment and manage only one source-file line in
# the user's ~/.tmux.conf, preserving every unrelated tmux preference.
TMUX_SNIPPET="$CFG_DIR/tmux.conf"
cp "$REPO_DIR/shell/tmux.conf" "$TMUX_SNIPPET"
TMUX_USER_CONF="$HOME/.tmux.conf"
TMUX_BEGIN="# >>> claudemaxxing:tmux >>>"
TMUX_END="# <<< claudemaxxing:tmux <<<"
touch "$TMUX_USER_CONF"
if grep -qF "$TMUX_BEGIN" "$TMUX_USER_CONF"; then
  python3 - "$TMUX_USER_CONF" "$TMUX_BEGIN" "$TMUX_END" <<'PY'
import sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(path).read()
i, j = t.find(begin), t.find(end)
if i != -1 and j != -1:
    before = t[:i].rstrip()
    after = t[j + len(end):].lstrip()
    t = before + ("\n" if before and after else "") + after
open(path, "w").write(t)
PY
fi
python3 - "$TMUX_USER_CONF" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read().rstrip("\r\n")
open(path, "w").write(text)
PY
printf '\n%s\nsource-file "%s"\n%s\n' \
  "$TMUX_BEGIN" "$TMUX_SNIPPET" "$TMUX_END" >> "$TMUX_USER_CONF"
echo "  sourced $TMUX_SNIPPET from $TMUX_USER_CONF"
if command -v tmux >/dev/null 2>&1 && tmux list-sessions >/dev/null 2>&1; then
  tmux source-file "$TMUX_SNIPPET"
  echo "  reloaded tmux scrolling defaults in the running server"
fi

say "OpenCode coding agents → ~/.config/opencode/opencode.json"
# Ship the Tab-selectable primary coding agents on HEAVY frontier
# Ollama Cloud code models — same doctrine as the ollama-worker subagent. This is the SINGLE
# source of truth for the agent spec; bootstrap.sh relies on this step (via its install.sh call).
# Idempotent; touches only the provider stub + agent block — never the ollama-cloud API key (that
# lives in OpenCode's auth store, configured by bootstrap.sh). The canonical coding agents own
# their model and allow-all permission policy so `o delegate --auto` is prompt-free even when a
# global config would otherwise ask. Skipped if OpenCode was never set up.
OPENCODE_CFG_DIR="$HOME/.config/opencode"
if [ -d "$OPENCODE_CFG_DIR" ] || command -v opencode >/dev/null 2>&1; then
  mkdir -p "$OPENCODE_CFG_DIR"
  python3 - "$OPENCODE_CFG_DIR/opencode.json" "$REPO_DIR" <<'PY'
import json, os, sys
cfg_p, repo = sys.argv[1], sys.argv[2]
cfg = json.load(open(cfg_p)) if os.path.exists(cfg_p) else {"$schema": "https://opencode.ai/config.json"}

# provider stub (no key — auth.json holds the ollama-cloud key, set by bootstrap.sh)
oc = cfg.setdefault("provider", {}).setdefault("ollama-cloud", {})
oc.setdefault("npm", "@ai-sdk/openai-compatible")
oc.setdefault("name", "Ollama Cloud")
oc.setdefault("options", {})["baseURL"] = "https://ollama.com/v1"
models = oc.setdefault("models", {})
models.setdefault("kimi-k3", {
    "name": "Kimi K3",
    "limit": {"context": 1_000_000, "output": 32768},
})
# Migrate only the stale catalog entry shipped during the 256k K3 transition;
# keep every unrelated/custom model field intact.
k3 = models["kimi-k3"]
if (k3.get("name") == "Kimi K3"
        and k3.get("limit") == {"context": 256_000, "output": 32768}):
    k3["limit"] = {"context": 1_000_000, "output": 32768}

# Canonical coding agents — Tab-selectable in the OpenCode TUI, or `opencode run --agent
# kimi-coder ...`. They ride HEAVY frontier Ollama Cloud code models. The `model` pointer is
# kept current across version bumps; descriptive fields use setdefault so user edits survive.
# Keep a short always-on prompt even though OpenCode also discovers the governed
# skill stack. The prompt carries only invariants that must fire before skill
# selection; detailed workflows stay in skills so they are loaded on demand.
PRE_ANTI_SLOP_PROMPT = (
    "You are a focused coding agent. Write correct, idiomatic code that matches the "
    "surrounding style. Make minimal, verifiable changes; run or reason through a "
    "verification before claiming done; never invent APIs. State your assumptions and "
    "anything you did not verify."
)
# The PLAN TO REPO paragraph (dashboard registration of deep plans) is a FLEET workflow —
# it now lives in the private plan-to-repo skill (skills/fleet-stack.json). The literal is
# kept ONLY so a prompt this installer shipped before can still be refreshed.
LEGACY_PLAN_TO_REPO = (
    "\n\n"
    "PLAN TO REPO — when you finalize a deep plan (an approved architecture/design doc or a "
    "multi-day phased implementation plan; NOT a session task list or a scratchpad "
    "exploration), persist it before coding. Write or UPDATE "
    "~/dev/planning/<project-slug>/<YYYY-MM-DD>_<tema>.md — front-matter project/agent/status "
    "with agent: opencode, one file per topic, edit the existing file in place rather than "
    "adding a second dated one (git carries history). Commit in that repo only, as "
    "'plan(<slug>): <tema>'. Then register it: resolve the project id with `curl -s "
    "http://127.0.0.1:3000/api/projects/<slug>/hub | jq -r .project.id` (the POST does not "
    "resolve slugs) and POST {\"node_kind\":\"project\",\"node_id\":\"<proj_id>\","
    "\"kind\":\"plan\",\"title\":\"<title>\",\"path\":\"<slug>/<file>.md\","
    "\"source_agent\":\"opencode\"} to http://127.0.0.1:3000/api/attachments with header "
    "'Authorization: Bearer $(cat ~/.config/claudemaxxing/dashboard-token)'. Re-posting the "
    "same path upserts, so re-running is safe. If registration fails, KEEP the file and the "
    "commit and report the exact POST for a human to run — never skip writing the plan "
    "because the POST failed. Full convention: ~/dev/planning/README.md."
)
ANTI_SLOP_SUFFIX = (
    "\n\nANTI-SLOP UI — before generating or materially restyling any frontend UI, "
    "load the anti-slop-design skill automatically. Follow its route and required "
    "generation gate before delivery; do not wait for the user to request a cleanup pass."
)
CODING_PROMPT = PRE_ANTI_SLOP_PROMPT + ANTI_SLOP_SUFFIX
# Prompts now carry doctrine, so a re-run must be able to REFRESH one this installer shipped
# before — plain `setdefault` would freeze the first version forever on every existing install
# and the plan-to-repo rule would never reach an already-configured machine. Refresh only an
# exact match of a previously shipped string; a hand-edited prompt is a user edit and survives.
SHIPPED_PROMPTS = {
    # v1 — pre plan-to-repo
    "You are a focused coding agent. Write correct, idiomatic code that matches the "
    "surrounding style. Make minimal, verifiable changes; run or reason through a "
    "verification before claiming done; never invent APIs. State your assumptions and "
    "anything you did not verify.",
    PRE_ANTI_SLOP_PROMPT,
    CODING_PROMPT,
    PRE_ANTI_SLOP_PROMPT + LEGACY_PLAN_TO_REPO,
    PRE_ANTI_SLOP_PROMPT + LEGACY_PLAN_TO_REPO + ANTI_SLOP_SUFFIX,
}
AGENTS = {
    "deepseekv4-coder": {"model": "ollama-cloud/deepseek-v4-flash:0731",
                         "description": "Heavy coding agent on DeepSeek V4 Pro — dialogue, execution, and volume."},
    "kimi-coder": {"model": "ollama-cloud/kimi-k2.7-code",
                   "description": "Heavy coding agent on Kimi K2.7 Code (256k ctx) — bounded implementation, refactors, tests."},
    "kimi-k3-coder": {"model": "ollama-cloud/kimi-k3",
                      "description": "Heavy coding agent on Kimi K3 (1M ctx, multimodal) — long-horizon, multi-phase execution."},
    "glm-coder":  {"model": "ollama-cloud/glm-5.3",
                   "description": "Heavy coding agent on GLM-5.3 — hard problems and second-opinion implementations."},
    "qwen-coder": {"model": "ollama-cloud/qwen3.5:397b",
                   "description": "Heavy coding agent on Qwen 3.5 — general and broad-knowledge implementation."},
    # The Tab-selectable coders use normal Ollama Cloud plan capacity. K3 is
    # heavier, so routing selects it by capability rather than as the default.
    "minimax-coder": {"model": "ollama-cloud/minimax-m3",
                      "description": "Heavy coding agent on MiniMax M3 (512K–1M ctx, multimodal) — agentic/long-context work and cross-family third opinion."},
}
agents = cfg.setdefault("agent", {})
# Rename the installer-owned volume agent without discarding user-tuned fields.
# If both names exist, the explicit new name wins and the stale old selector is
# removed so OpenCode never presents duplicate DeepSeek V4 agents.
if "deepseekv4-coder" not in agents and isinstance(agents.get("v4-coder"), dict):
    agents["deepseekv4-coder"] = agents["v4-coder"]
agents.pop("v4-coder", None)
SHIPPED_DESCRIPTIONS = {
    "kimi-coder": {
        "Heavy coding agent on Kimi K2.7 Code (256k ctx) — implementation, refactors, tests.",
        "Heavy coding agent on Kimi K3 (1M ctx, multimodal) — implementation, refactors, tests.",
        AGENTS["kimi-coder"]["description"],
    },
    "glm-coder": {
        "Heavy coding agent on GLM-5.2 — hard problems and second-opinion implementations.",
        AGENTS["glm-coder"]["description"],
    },
}
for name, spec in AGENTS.items():
    a = agents.setdefault(name, {})
    a["model"] = spec["model"]            # keep the model pointer current across version bumps
    a["permission"] = "allow"             # canonical coders are prompt-free delegated executors
    a.setdefault("mode", "primary")
    if not a.get("description") or a["description"] in SHIPPED_DESCRIPTIONS.get(name, set()):
        a["description"] = spec["description"]
    a.setdefault("temperature", 0.2)
    if not a.get("prompt") or a["prompt"] in SHIPPED_PROMPTS:
        a["prompt"] = CODING_PROMPT   # doctrine must reach the agent; user edits survive

# MCP registrations. Fleet servers (hermes-orchestrator, open-design) are added by
# install-fleet.sh on fleet machines; the public core registers only the browser bridge.
mcp = cfg.setdefault("mcp", {})

# MCP: the CDP browser connector (bin/opencode-browser-mcp) — gives the primary coders
# the 8 browser_* tools over Chrome DevTools Protocol. Unlike hermes it IS a copied bridge
# (single self-contained file, no repo imports), so it runs from ~/.local/bin like every
# other tool. CDP endpoint defaults to the shared localhost:18800 Chrome; users point it at a remote Chrome
# via `environment: {"CDP_URL": ...}` (setdefault — a user override survives re-runs).
browser = mcp.setdefault("browser", {})
browser["type"] = "local"
browser["command"] = ["node", os.path.join(os.path.expanduser("~"), ".local", "bin", "opencode-browser-mcp")]
browser.setdefault("enabled", True)
browser.setdefault("environment", {}).setdefault("CDP_URL", "http://127.0.0.1:18800")

json.dump(cfg, open(cfg_p, "w"), indent=2)
print("  ensured OpenCode agents deepseekv4/glm/kimi-k2.7/kimi-k3/qwen/minimax + browser MCP in", cfg_p)
PY
  # Native OpenCode surfaces (markdown agents/commands, JS plugin) — repo is the
  # source of truth, deploy COPIES like every other harness asset. kimiplan is
  # canonical; oplanner and /oplan remain compatibility aliases on the same K3
  # model. Also ships deep-researcher and /research /opus /sonnet /codex,
  # and the done-notify plugin (session.idle → agent-done-notify --agent opencode).
  for sub in agents commands plugins; do
    if [ -d "$REPO_DIR/opencode/$sub" ]; then
      mkdir -p "$OPENCODE_CFG_DIR/$sub"
      cp "$REPO_DIR/opencode/$sub/"* "$OPENCODE_CFG_DIR/$sub/"
    fi
  done
  echo "  deployed opencode/{agents,commands,plugins} → $OPENCODE_CFG_DIR"
else
  echo "  (OpenCode not configured here — skipped; run ./bootstrap.sh to set it up)"
fi

# NOTE: the installer deliberately writes NO context/compaction config. Every host ships its
# own calibrated compaction and we settle on it. A tool that wrote our own thresholds here was
# removed on 2026-07-18 after it degraded Claude badly; harness-verify now fails if anything in
# this file touches those keys again. Post-mortem: knowledge/context-lifecycle.md.

say "xAI key → $CFG_DIR/.env (standalone source for xsearch)"
mkdir -p "$CFG_DIR"
KEY="${XAI_API_KEY:-}"
if [ -z "$KEY" ] && [ -f "$REPO_DIR/.env" ]; then
  KEY="$(grep '^XAI_API_KEY=' "$REPO_DIR/.env" | cut -d= -f2-)"
fi
if [ -n "$KEY" ]; then
  printf 'XAI_API_KEY=%s\n' "$KEY" > "$CFG_DIR/.env"
  chmod 600 "$CFG_DIR/.env"
  echo "  wrote $CFG_DIR/.env"
else
  echo "  (no XAI_API_KEY and no repo .env — xsearch will need the key set later)"
fi

say "Zed editor → Ollama Cloud provider config (interactive surface, never a lane)"
# Idempotent additive merge of language_models.ollama.api_url into
# ~/.config/zed/settings.json; silent no-op on a Zed-less machine (the tool
# gates itself — never creates ~/.config/zed where Zed is absent).
if [ -d "$HOME/.config/zed" ] || command -v zed >/dev/null 2>&1; then
  "$BIN_DST/zed-setup" || echo "  (zed-setup could not merge — run 'zed-setup --print' and paste manually)"
  echo "  one-time auth: 'zed-setup --key-hint' → paste in Zed Agent Panel → LLM Providers → Ollama"
else
  echo "  (Zed not installed — 'curl -f https://zed.dev/install.sh | sh' on Linux, 'brew install --cask zed' on macOS)"
fi

say "Orchestrator pointer → Claude, Codex, OpenCode, and Zed global instructions"
BEGIN="<!-- claudemaxxing:orchestrator:begin -->"
END="<!-- claudemaxxing:orchestrator:end -->"
# Same marked doctrine block into every harness-aware agent's global context file:
# Claude Code reads ~/.claude/CLAUDE.md; Codex reads ~/.codex/AGENTS.md;
# OpenCode reads ~/.config/opencode/AGENTS.md; Zed's agent reads the always-on
# ~/.config/zed/AGENTS.md (its "Instructions" file). Warp has no file-based
# global rules (Warp Drive is cloud-only) — in-repo it reads the project
# AGENTS.md → CLAUDE.md symlink natively, like Zed does.
mkdir -p "$CODEX_DIR"
POINTER_TARGETS=("$CLAUDE_DIR/CLAUDE.md" "$CODEX_DIR/AGENTS.md")
if [ -d "$OPENCODE_CFG_DIR" ] || command -v opencode >/dev/null 2>&1; then
  mkdir -p "$OPENCODE_CFG_DIR"
  POINTER_TARGETS+=("$OPENCODE_CFG_DIR/AGENTS.md")
fi
if [ -d "$HOME/.config/zed" ] || command -v zed >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/zed"
  POINTER_TARGETS+=("$HOME/.config/zed/AGENTS.md")
fi
# Warp has no file-based global rules (Warp Drive is cloud-only), so the
# directive cannot be installed — but its paste source can. This file always
# carries the CURRENT doctrine block; the human pastes it once into
# Warp → Warp Drive → Rules, and re-pastes after doctrine changes.
WARP_RULES_MD="$CFG_DIR/warp-global-rules.md"
if [ ! -f "$WARP_RULES_MD" ] || ! grep -qF "$BEGIN" "$WARP_RULES_MD"; then
  cat > "$WARP_RULES_MD" <<'MD'
# Warp Drive Global Rules — paste source (generated by install.sh; do not hand-edit)

Open Warp → Warp Drive → Rules → add (or replace) the claudemaxxing global rule
with everything BETWEEN the markers below. Re-paste after doctrine changes —
install.sh refreshes this file on every run.
MD
fi
POINTER_TARGETS+=("$WARP_RULES_MD")
for GLOBAL_MD in "${POINTER_TARGETS[@]}"; do
touch "$GLOBAL_MD"
# strip any previous block (idempotent), then append fresh
if grep -qF "$BEGIN" "$GLOBAL_MD"; then
  python3 - "$GLOBAL_MD" "$BEGIN" "$END" <<'PY'
import sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(path).read()
i, j = t.find(begin), t.find(end)
if i != -1 and j != -1:
    t = t[:i].rstrip() + "\n" + t[j+len(end):].lstrip()
open(path, "w").write(t)
PY
fi
HOST_ROUTING=""
if [ "$GLOBAL_MD" = "$CODEX_DIR/AGENTS.md" ]; then
  HOST_ROUTING='- **Codex plan-first routing:** use the namespaced `$claudemaxxing:*` skills; nontrivial unplanned work → `$claudemaxxing:solplan` → root review → `$claudemaxxing:fanout` only for independent chunks.'
elif [ "$GLOBAL_MD" = "$OPENCODE_CFG_DIR/AGENTS.md" ]; then
  HOST_ROUTING='- **OpenCode routing:** the claudemaxxing workflow skills are installed natively. Use `/kimiplan` for read-only planning, then `fanout` only for independent chunks; `o delegate/send/handoff/close` remains the stateful worker lifecycle.'
elif [ "$GLOBAL_MD" = "$HOME/.hermes/AGENTS.md" ]; then
  HOST_ROUTING='- **Hermes routing:** the claudemaxxing workflow skills are installed natively; Hermes keeps its richer native `plan-to-repo`. Use `solplan` for read-only planning and the same `oll`/`o` deterministic worker lifecycle.'
fi
cat >> "$GLOBAL_MD" <<MD

$BEGIN
## Frontier orchestrator workers (Ollama Cloud)

The primary host (Claude, Codex, OpenCode, or Hermes) orchestrates + verifies; heavy Ollama Cloud models do bounded bulk work.
- **Delegate** bulk/parallel/low-stakes work (summarize, classify, draft, first-pass code/tests/review). **Keep in the primary frontier thread:** cross-file architecture, risky edits, final sign-off/merge.
- **Iron rule of verification:** verification cost scales with the *spec*, not the *solution* — never re-do a worker's work to check it. Author the contract (tests/checklist) first; read only pass/fail. If you can't cheaply specify "correct," keep it in the frontier thread.
- **Physical delegation gate:** Root persists read-only \`.results/delegation/<run-id>/contract.md\` + \`brief.md\` before dispatch; workers never author/certify their gate. New OpenCode briefs mark exactly one complete bounded turn-1 assignment with \`<!-- o-delegate-turn-1:begin -->\` / \`<!-- o-delegate-turn-1:end -->\`: \`o delegate\` executes it immediately, while \`o send\` is repair-only and never the initial task (unmarked legacy bounded briefs execute as a whole). Direct \`oll\` is response-only (no files/shell/tests); workspace reads/edits, commands/tests, persistent artifacts, and repairs route through the public \`o\` worker runtime (\`o delegate/send/handoff\`; \`o output\` is pane diagnostics; then \`o close\` — an unclosed worker leaks a process tree); \`occ\` is internal one-shot transport.
- Tools (on PATH): \`oll "<task>" --model <m>\` · \`oll-council "<q>"\` · \`memoryctl\` · \`mem-audit\`. Workflows: Claude \`/gauntlet\` (divide a broad request into gated increments, one step before planning; promotes, never accepts) + \`/fanout\` + \`/ideas\`; Codex \`\$claudemaxxing:gauntlet\` + \`\$claudemaxxing:fanout\` + \`\$claudemaxxing:ideas\` + \`\$claudemaxxing:memory\`. Optional agent: \`ollama-worker\`.
$HOST_ROUTING
- **Shared-skill vocabulary:** inside an OpenCode or Hermes session, "root Codex" in a shared workflow means the current primary host. Keep the invariant and use the host routing above; do not launch Codex merely to rename the orchestrator.
- **Memory governance:** Claude and Codex share the file-backed \`<git-root>/.agents/memory/\` store. Use \`memoryctl show/add/supersede/consolidate\`; never scrape Codex SQLite. Supersede (don't append) on conflict; stamp \`created\`/\`last_verified\` + decay (reference 30d / project 14d → re-verify when stale); gate only belief-changing writes with a different-family critic; keep \`MEMORY.md\` one line per active fact. Above 25 facts, merge safe groups with atomic \`memoryctl consolidate\` or bind a no-safe-merge review to the exact semantic set with \`memoryctl review-consolidation --decision no-safe-merge\`; any meaning or membership change reopens it. Run \`mem-audit\` and act on its flags — never re-read the vault to re-derive staleness.

### Compact Instructions
Preserve the objective/current dispatch, acceptance criteria, constraints, decisions and evidence, modified paths, exact verification state, unresolved questions, and next steps. Drop repeated tool output, superseded reasoning, and repository text that can be read again. Never claim an unrun gate passed. After compaction, use reloaded shared memory and the session log as bounded external state.
$END
MD
echo "  wrote doctrine block into $GLOBAL_MD"
done

say "Hooks → $CLAUDE_DIR/settings.json (global SessionStart: shared memory, loop-tick, session-log; Stop: memory/log audit)"
GLOBAL_SETTINGS="$CLAUDE_DIR/settings.json"
python3 - "$GLOBAL_SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except (FileNotFoundError, ValueError):
    data = {}  # missing or empty/garbled -> start fresh (never clobber valid JSON)

hooks = data.setdefault("hooks", {})

def is_agenttab(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and "agent-tab-status" in (h.get("command") or "")
               for h in entry.get("hooks", []))

def is_legacy_notify(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict)
               and ("claude-hook-notify" in (h.get("command") or "")
                    or "notify-code-thread.sh" in (h.get("command") or ""))
               for h in entry.get("hooks", []))

def is_agentnotify(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and "agent-done-notify" in (h.get("command") or "")
               for h in entry.get("hooks", []))

def is_retired_orchestrator_hook(entry):
    if not isinstance(entry, dict):
        return False
    retired = ("orchestrator/hooks/orch-notify.py",
               "orchestrator/hooks/tmux-stamp.sh")
    return any(isinstance(h, dict)
               and any(marker in (h.get("command") or "") for marker in retired)
               for h in entry.get("hooks", []))

# The retired custom orch-* control plane registered project-specific scripts
# as global hooks. Remove only those exact paths; unrelated user hooks survive.
for event, groups in list(hooks.items()):
    if isinstance(groups, list):
        hooks[event] = [entry for entry in groups
                        if not is_retired_orchestrator_hook(entry)]

agenttab = '"$HOME/.local/bin/agent-tab-status"'
warpevent = '"$HOME/.local/bin/warp-agent-event"'
warprecovery = '"$HOME/.local/bin/warp-agent-recovery"'
ss = hooks.get("SessionStart")
if not isinstance(ss, list):
    ss = []

# idempotent: drop prior shared-memory entries, then re-add fresh (lets commands evolve)
def is_memaudit(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and "mem-audit" in (h.get("command") or "")
               for h in entry.get("hooks", []))
ss = [e for e in ss if not is_memaudit(e)]

def is_memorybrief(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and "memoryctl" in (h.get("command") or "")
               and " brief " in (h.get("command") or "") for h in entry.get("hooks", []))
ss = [e for e in ss if not is_memorybrief(e)]

# Both Claude and Codex receive the same bounded active-memory index. Sensitive
# bodies are omitted; an agent explicitly reads one only when the task needs it.
ss.append({"hooks": [{"type": "command",
                      "command": '"$HOME/.local/bin/memoryctl" brief --max-bytes 12000 2>/dev/null; true',
                      "timeout": 15, "statusMessage": "Loading shared Claude/Codex memory…"}]})

# bare `mem-audit` (on PATH); silent when clean, surfaces flags into context otherwise
cmd = ('o="$("$HOME/.local/bin/mem-audit" 2>/dev/null)"; echo "$o" | grep -qE "⚠|✗" && '
       'printf "Memory audit flags (re-verify stale/superseded memory before trusting it):'
       '\\n%s\\n" "$o"; true')
ss.append({"hooks": [{"type": "command", "command": cmd,
                      "timeout": 15, "statusMessage": "Auditing project memory…"}]})

# --- continuous watcher (loop engineering): capture harness flaws into the forward queue ---
# loop-tick self-scopes to the claudemaxxing harness (silent IDLE elsewhere), so this is safe
# as a GLOBAL SessionStart hook. It enqueues harness-verify reds / mem-audit drift the moment
# they appear — the "continuous watching" half; rounds are still fired event-driven off the
# queue, never on a clock. Idempotent (loop-queue dedupes), silent unless it captures something.
def is_looptick(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and "loop-tick" in (h.get("command") or "")
               for h in entry.get("hooks", []))
ss = [e for e in ss if not is_looptick(e)]
ltcmd = ('loop-tick --kick --quiet >/dev/null 2>&1; n="$(loop-queue status --json 2>/dev/null)"; '
         'echo "$n" | grep -q \'"actionable": 0\' || '
         'printf "Self-improve queue has open flaws — run /self-improve to drain:\\n%s\\n" '
         '"$(loop-queue list 2>/dev/null)"; true')
ss.append({"hooks": [{"type": "command", "command": ltcmd,
                      "timeout": 5, "statusMessage": "Watching harness health…"}]})

# --- session-log: per-project changelog + WIP (fires in EVERY project — NOT self-scoped) ---
# Unlike mem-audit/loop-tick (which self-scope to this harness), the session-log hooks are
# meant to work everywhere: SessionStart loads the recent changelog tail + current WIP so a new
# session resumes with prior state. Silent in projects that haven't adopted session-log (no
# docs/ log yet). The Stop half (below) reminds — never blocks — when an adopted project's log
# went stale this session. Both are deterministic (no LLM) and use bare `session-log` on PATH.
def is_sessionlog(entry):
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and "session-log" in (h.get("command") or "")
               for h in entry.get("hooks", []))
ss = [e for e in ss if not is_sessionlog(e)]
ss.append({"hooks": [{"type": "command", "command": "session-log tail 2>/dev/null; true",
                      "timeout": 15, "statusMessage": "Loading project session log…"}]})

# --- Warp/tmux attention state: one shared helper, host lifecycle as the signal ---
ss = [e for e in ss if not is_agenttab(e)]
ss.append({"hooks": [{"type": "command", "command": f'{warprecovery} register-agent claude 2>/dev/null; {agenttab} idle 2>/dev/null; {warpevent} claude session_start 2>/dev/null; true',
                      "timeout": 5}]})

hooks["SessionStart"] = ss

ups = hooks.get("UserPromptSubmit")
if not isinstance(ups, list):
    ups = []
ups = [e for e in ups if not is_agenttab(e)]
ups.append({"hooks": [{"type": "command", "command": f'{agenttab} working 2>/dev/null; {warpevent} claude prompt_submit 2>/dev/null; true',
                       "timeout": 5}]})
hooks["UserPromptSubmit"] = ups

# AskUserQuestion/ExitPlanMode pause the agent until the human responds. Once
# the tool returns, PostToolUse restores the working indicator.
for event, state, warp_event in (("PreToolUse", "attention", "question_asked"),
                                 ("PostToolUse", "working", "permission_replied")):
    groups = hooks.get(event)
    if not isinstance(groups, list):
        groups = []
    groups = [e for e in groups if not is_agenttab(e)]
    groups.append({"matcher": "AskUserQuestion|ExitPlanMode",
                   "hooks": [{"type": "command",
                              "command": f'{agenttab} {state} 2>/dev/null; {warpevent} claude {warp_event} 2>/dev/null; true',
                              "timeout": 5}]})
    hooks[event] = groups

pr = hooks.get("PermissionRequest")
if not isinstance(pr, list):
    pr = []
pr = [e for e in pr if not is_agenttab(e)]
pr.append({"hooks": [{"type": "command", "command": f'{agenttab} attention 2>/dev/null; {warpevent} claude permission_request 2>/dev/null; true',
                      "timeout": 5}]})
hooks["PermissionRequest"] = pr

notifications = hooks.get("Notification")
if not isinstance(notifications, list):
    notifications = []
notifications = [e for e in notifications
                 if not is_agenttab(e) and not is_legacy_notify(e) and not is_agentnotify(e)]
for notification_type in ("permission_prompt", "idle_prompt", "elicitation_dialog"):
    notifications.append({"matcher": notification_type,
                          "hooks": [{"type": "command",
                                     "command": f'{agenttab} attention 2>/dev/null; {warpevent} claude question_asked 2>/dev/null; true',
                                     "timeout": 5}]})
notifications.append({"matcher": "permission_prompt|idle_prompt|elicitation_dialog",
                      "hooks": [{"type": "command",
                                 "command": '"$HOME/.local/bin/agent-done-notify" 2>/dev/null; true',
                                 "timeout": 15,
                                 "statusMessage": "Notificando a Proyectos…"}]})
hooks["Notification"] = notifications

subagent_stop = hooks.get("SubagentStop")
if isinstance(subagent_stop, list):
    hooks["SubagentStop"] = [e for e in subagent_stop
                             if not is_legacy_notify(e) and not is_agentnotify(e)]

# Stop hook: nudge /wrap-up if this session changed source but didn't log it. `session-log check`
# is silent unless an *adopted* project's log is stale, so this never nags throwaway/cloned repos.
st = hooks.get("Stop")
if not isinstance(st, list):
    st = []
st = [e for e in st if not is_sessionlog(e) and not is_memaudit(e)
      and not is_legacy_notify(e) and not is_agentnotify(e)]
st = [e for e in st if not is_agenttab(e)]
st.append({"hooks": [{"type": "command",
                      "command": f'{agenttab} attention 2>/dev/null; {warpevent} claude stop 2>/dev/null; true',
                      "timeout": 5}]})
st.append({"hooks": [{"type": "command",
                      "command": ('o="$("$HOME/.local/bin/mem-audit" 2>/dev/null)"; echo "$o" | grep -qE "⚠|✗" && '
                                  'printf "Shared memory needs attention:\\n%s\\n" "$o"; true'),
                      "timeout": 15, "statusMessage": "Checking shared memory…"}]})
st.append({"hooks": [{"type": "command",
                      "command": 'o="$(session-log check 2>/dev/null)"; [ -n "$o" ] && printf "%s\\n" "$o"; true',
                      "timeout": 15, "statusMessage": "Checking session log…"}]})
st.append({"hooks": [{"type": "command",
                      "command": '"$HOME/.local/bin/agent-done-notify" 2>/dev/null; true',
                      "timeout": 15, "statusMessage": "Notificando a Proyectos…"}]})
hooks["Stop"] = st

data["hooks"] = hooks

with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("  merged SessionStart (shared memory, loop-tick, session-log) + Stop audit/log hooks into", path)
PY

# Codex now owns these lifecycle hooks through the installed claudemaxxing
# plugin. Older Mac installs copied Claude's hook set into ~/.codex/hooks.json;
# Codex loads BOTH sources, so every lifecycle action ran twice and the copied
# Warp hook even identified Codex as Claude. Remove only our owned commands,
# preserve mixed/unrelated groups, and keep one recoverable pre-migration copy.
python3 - "$CODEX_DIR/hooks.json" <<'PY'
import json, os, shutil, sys, tempfile

path = sys.argv[1]
if not os.path.isfile(path):
    raise SystemExit(0)
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError) as ex:
    print(f"  warning: Codex hooks cleanup skipped ({type(ex).__name__})")
    raise SystemExit(0)

hooks = data.get("hooks") if isinstance(data, dict) else None
if not isinstance(hooks, dict):
    raise SystemExit(0)

def owned(command):
    if not isinstance(command, str):
        return False
    return (("memoryctl" in command and " brief " in command)
            or "mem-audit" in command
            or "loop-tick" in command
            or "session-log tail" in command
            or "session-log check" in command
            or any(marker in command for marker in (
                "agent-tab-status", "warp-agent-event", "warp-agent-recovery",
                "agent-done-notify", "codex-stop-hook")))

removed = 0
for event, groups in list(hooks.items()):
    if not isinstance(groups, list):
        continue
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        before = group["hooks"]
        after = [hook for hook in before
                 if not (isinstance(hook, dict) and owned(hook.get("command")))]
        removed += len(before) - len(after)
        if after:
            updated = dict(group)
            updated["hooks"] = after
            kept_groups.append(updated)
    if kept_groups:
        hooks[event] = kept_groups
    else:
        hooks.pop(event, None)

if removed:
    backup = path + ".pre-claudemaxxing-plugin"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".hooks-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"  removed {removed} duplicated claudemaxxing hook(s) from {path}; backup: {backup}")
PY

say "Autonomous loop glue → live locations (copy, not symlink; __REPO__/__HOME__ substituted)"
# The forward-queue WATCH half ships via the SessionStart hook above (read-only, always on).
# This deploys the daily ACT half: the shared wrapper + the per-OS scheduler unit (launchd on
# macOS via com.claudemaxxing.loop.plist; systemd --user on Linux via claudemaxxing-loop.service
# + claudemaxxing-loop.timer). Deploying the FILES is always safe (version-controlled copies);
# ARMING the autonomous loop stays OPT-IN (we only re-sync a scheduler that's ALREADY armed —
# otherwise we just print how to enable it).
WRAP_DST="$CFG_DIR/loop-cron.sh"
if [ -f "$REPO_DIR/deploy/loop-cron.sh" ]; then
  sed "s#__REPO__#$REPO_DIR#g" "$REPO_DIR/deploy/loop-cron.sh" > "$WRAP_DST"
  chmod +x "$WRAP_DST"
  echo "  wrote $WRAP_DST"
fi

case "$(uname -s)" in
Darwin)
  # macOS → launchd user agent.
  LAUNCH_DST="$HOME/Library/LaunchAgents"
  PLIST_DST="$LAUNCH_DST/com.claudemaxxing.loop.plist"
  if [ -f "$REPO_DIR/deploy/com.claudemaxxing.loop.plist" ]; then
    mkdir -p "$LAUNCH_DST"
    sed "s#__HOME__#$HOME#g" "$REPO_DIR/deploy/com.claudemaxxing.loop.plist" > "$PLIST_DST"
    echo "  wrote $PLIST_DST"
    if command -v launchctl >/dev/null 2>&1; then
      # Capture first, then grep: under `set -o pipefail`, `launchctl list | grep -q` reports
      # failure even on a MATCH — grep -q closes the pipe, launchctl gets SIGPIPE (141), pipefail
      # propagates it. Decoupling (|| true) makes the grep's own exit the deciding one.
      # Re-sync ONLY an agent this HOME owns: the loaded agent's plist path must be the file
      # we just wrote. `launchctl list` can omit a loaded-but-idle agent, and an install run
      # under another HOME (a fixture, a proof, a second account) must never bootout the real
      # agent and bootstrap its own plist in its place.
      if _launchd_owned_here "com.claudemaxxing.loop" "$PLIST_DST"; then
        launchctl bootout "gui/$(id -u)/com.claudemaxxing.loop" 2>/dev/null
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null \
          && echo "  reloaded launchd agent (was already armed → kept in sync)" \
          || echo "  NOTE: could not reload launchd agent (load it manually, see below)"
      elif launchctl print "gui/$(id -u)/com.claudemaxxing.loop" >/dev/null 2>&1; then
        echo "  NOTE: com.claudemaxxing.loop is loaded from another home — left untouched"
      else
        printf '  \033[1;33mautonomous loop NOT armed\033[0m — to enable the daily 07:00 self-improve round:\n'
        printf '    launchctl bootstrap gui/$(id -u) %s\n' "$PLIST_DST"
      fi
    fi
  fi
  ;;
Linux)
  # Linux → systemd --user timer (claudemaxxing-loop.service + claudemaxxing-loop.timer).
  SD_DST="$HOME/.config/systemd/user"
  if [ -f "$REPO_DIR/deploy/claudemaxxing-loop.service" ] && command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$SD_DST"
    sed "s#__HOME__#$HOME#g" "$REPO_DIR/deploy/claudemaxxing-loop.service" > "$SD_DST/claudemaxxing-loop.service"
    cp "$REPO_DIR/deploy/claudemaxxing-loop.timer" "$SD_DST/claudemaxxing-loop.timer"
    echo "  wrote $SD_DST/claudemaxxing-loop.{service,timer}"
    systemctl --user daemon-reload 2>/dev/null || true
    if systemctl --user is-enabled --quiet claudemaxxing-loop.timer 2>/dev/null; then
      # already armed → restart so the live timer matches the repo (re-sync next-elapse)
      systemctl --user restart claudemaxxing-loop.timer 2>/dev/null \
        && echo "  reloaded systemd timer (was already armed → kept in sync)" \
        || echo "  NOTE: could not reload systemd timer (enable manually, see below)"
    else
      printf '  \033[1;33mautonomous loop NOT armed\033[0m — to enable the daily 07:00 self-improve round:\n'
      printf '    systemctl --user enable --now claudemaxxing-loop.timer\n'
      printf '    sudo loginctl enable-linger %s   # once: so it fires without an active login\n' "$USER"
    fi
  fi
  ;;
esac


# --- cogload: the cognitive-load collector (Linux/X11; deploy always, arm opt-in) ---
# Same posture as the loop and strategic-brief timers: writing the unit is safe,
# ARMING is the user's call — this one records interaction behavior, so it must
# never start collecting merely because someone re-ran install.sh. An already-armed
# unit IS restarted, so a code change reaches the running collector.
if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -f "$REPO_DIR/deploy/cogload-keys.service" ]; then
  SD_DST="$HOME/.config/systemd/user"
  mkdir -p "$SD_DST"
  sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO_DIR#g" \
    "$REPO_DIR/deploy/cogload-keys.service" > "$SD_DST/cogload-keys.service"
  echo "  wrote $SD_DST/cogload-keys.service"
  systemctl --user daemon-reload 2>/dev/null || true
  if systemctl --user is-enabled --quiet cogload-keys.service 2>/dev/null; then
    systemctl --user restart cogload-keys.service 2>/dev/null \
      && echo "  restarted cogload collector (was already armed → kept in sync)" \
      || echo "  NOTE: could not restart cogload collector"
  else
    printf '  \033[1;33mcogload NOT armed\033[0m — it needs a venv with its deps, then:\n'
    printf '    cogload on\n'
  fi

  # evdev is the Linux capture backend (compositor-independent: it survives the
  # X11 -> Wayland move that blinded the XRecord path on 2026-08-16). It was
  # previously present on this box by hand and referenced NOWHERE in the repo,
  # so a reinstall on a fresh machine would silently fall back to the X11-only
  # path. Provision it here so the backend is reproducible, not local luck.
  #
  # The venv is built `--without-pip` (uv), so `venv/bin/pip` does not exist —
  # use uv, and NEVER assume pip. Non-fatal: a box without uv still installs,
  # it just keeps the pynput fallback and says so.
  # GNOME Shell extension: the only route to the `screen` channel under
  # Wayland. Copied, never auto-enabled — enabling needs a shell restart, and
  # arming anything that observes you stays the user's call.
  COGLOAD_EXT_SRC="$REPO_DIR/deploy/gnome-cogload-extension"
  COGLOAD_EXT_DST="$HOME/.local/share/gnome-shell/extensions/cogload@claudemaxxing.local"
  if [ -d "$COGLOAD_EXT_SRC" ] && [ "$(uname -s)" = "Linux" ]; then
    mkdir -p "$COGLOAD_EXT_DST"
    cp "$COGLOAD_EXT_SRC/metadata.json" "$COGLOAD_EXT_SRC/extension.js" "$COGLOAD_EXT_DST/" 2>/dev/null || true
    echo "  wrote $COGLOAD_EXT_DST (enable with: gnome-extensions enable cogload@claudemaxxing.local)"
  fi

  COGLOAD_VENV="$HOME/.local/share/cogload/venv"
  if [ -x "$COGLOAD_VENV/bin/python3" ]; then
    if "$COGLOAD_VENV/bin/python3" -c 'import evdev' 2>/dev/null; then
      echo "  cogload: evdev present in the collector venv"
    elif command -v uv >/dev/null 2>&1; then
      if uv pip install --python "$COGLOAD_VENV/bin/python3" -q 'evdev>=1.9,<2' 2>/dev/null; then
        echo "  cogload: installed evdev into the collector venv"
      else
        printf '  \033[1;33mcogload: could not install evdev\033[0m — capture will fall back to X11-only pynput\n'
      fi
    else
      printf '  \033[1;33mcogload: evdev missing and uv not found\033[0m — capture will fall back to X11-only pynput\n'
    fi
    # The input group is a PRIVILEGE GRANT and is the user's decision, so this
    # only ever prints the command. Two traps make the naive instruction wrong:
    # the group does not apply to an existing session, and with lingering
    # enabled the `systemd --user` manager keeps its original supplementary
    # groups across logout/login — so a plain re-login leaves the collector
    # exactly as blind as before.
    if ! id -nG 2>/dev/null | tr " " "\n" | grep -qx input; then
      printf '  \033[1;33mcogload: %s is not in the `input` group\033[0m — evdev cannot read keyboards.\n' "$USER"
      printf '    sudo usermod -aG input %s\n' "$USER"
      printf '    loginctl terminate-user %s   # REQUIRED: a plain logout keeps the old groups while lingering is on\n' "$USER"
    fi
  fi
fi

# --- cogload nightly maintenance (digest → transcripts → mirror → rotate) ---
# Deploy always, arm opt-in — but unlike the collector this one is AUTO-safe to
# arm alongside it, because the script itself refuses to observe anything while
# the kill switch is set. Armed only when the collector is armed: maintenance
# for a collector that isn't running is pure noise.
if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -f "$REPO_DIR/deploy/cogload-nightly.service" ]; then
  SD_DST="$HOME/.config/systemd/user"
  CFG_DIR="${CFG_DIR:-$HOME/.config/claudemaxxing}"
  mkdir -p "$SD_DST" "$CFG_DIR"
  cp "$REPO_DIR/deploy/cogload-nightly.sh" "$CFG_DIR/cogload-nightly.sh"
  chmod +x "$CFG_DIR/cogload-nightly.sh"
  sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO_DIR#g" \
    "$REPO_DIR/deploy/cogload-nightly.service" > "$SD_DST/cogload-nightly.service"
  cp "$REPO_DIR/deploy/cogload-nightly.timer" "$SD_DST/cogload-nightly.timer"
  echo "  wrote $SD_DST/cogload-nightly.{service,timer}"
  systemctl --user daemon-reload 2>/dev/null || true
  if systemctl --user is-enabled --quiet cogload-nightly.timer 2>/dev/null; then
    systemctl --user restart cogload-nightly.timer 2>/dev/null \
      && echo "  reloaded cogload nightly timer (was already armed → kept in sync)" \
      || echo "  NOTE: could not reload cogload nightly timer"
  elif systemctl --user is-enabled --quiet cogload-keys.service 2>/dev/null; then
    printf '  \033[1;33mcogload nightly NOT armed\033[0m — collector is on; to keep its data durable:\n'
    printf '    systemctl --user enable --now cogload-nightly.timer\n'
  fi
fi

# --- cogload on macOS: deploy the launch agent + setup script, arm NEVER ---
# Arming is deliberately manual on the Mac: the collector cannot work until
# Ricardo grants Input Monitoring + Accessibility in the GUI, and TCC cannot be
# prompted over SSH. Auto-arming here would produce a launch agent that runs and
# silently captures nothing — a collector writing zeros reads as a calm day.
if [ "$(uname -s)" = "Darwin" ] && [ -f "$REPO_DIR/deploy/com.claudemaxxing.cogload-keys.plist" ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  sed "s|__HOME__|$HOME|g" "$REPO_DIR/deploy/com.claudemaxxing.cogload-keys.plist" \
    > "$HOME/Library/LaunchAgents/com.claudemaxxing.cogload-keys.plist"
  # Close-of-day maintenance + lightweight login catch-up. Both are deployed
  # always and armed only by the setup script. If already armed, reload them so
  # a schedule fix reaches the live LaunchAgents without turning collection on
  # for a new install.
  if [ -f "$REPO_DIR/deploy/com.claudemaxxing.cogload-nightly.plist" ]; then
    mkdir -p "$HOME/.config/claudemaxxing"
    install -m 0755 "$REPO_DIR/deploy/cogload-nightly.sh" \
      "$HOME/.config/claudemaxxing/cogload-nightly.sh"
    sed "s|__HOME__|$HOME|g" "$REPO_DIR/deploy/com.claudemaxxing.cogload-nightly.plist" \
      > "$HOME/Library/LaunchAgents/com.claudemaxxing.cogload-nightly.plist"
    echo "  wrote ~/Library/LaunchAgents/com.claudemaxxing.cogload-nightly.plist"
  fi
  if [ -f "$REPO_DIR/deploy/com.claudemaxxing.cogload-catchup.plist" ]; then
    mkdir -p "$HOME/.config/claudemaxxing"
    install -m 0755 "$REPO_DIR/deploy/cogload-catchup.sh" \
      "$HOME/.config/claudemaxxing/cogload-catchup.sh"
    sed "s|__HOME__|$HOME|g" "$REPO_DIR/deploy/com.claudemaxxing.cogload-catchup.plist" \
      > "$HOME/Library/LaunchAgents/com.claudemaxxing.cogload-catchup.plist"
    echo "  wrote ~/Library/LaunchAgents/com.claudemaxxing.cogload-catchup.plist"
  fi
  for _cogload_job in nightly catchup; do
    _cogload_label="com.claudemaxxing.cogload-${_cogload_job}"
    _cogload_plist="$HOME/Library/LaunchAgents/${_cogload_label}.plist"
    if [ -f "$_cogload_plist" ] \
       && _launchd_owned_here "$_cogload_label" "$_cogload_plist"; then
      launchctl bootout "gui/$UID/$_cogload_label" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$UID" "$_cogload_plist" >/dev/null 2>&1 \
        && echo "  reloaded $_cogload_label (was already armed → kept in sync)" \
        || echo "  NOTE: could not reload $_cogload_label"
    fi
  done
  echo "  wrote ~/Library/LaunchAgents/com.claudemaxxing.cogload-keys.plist"
  printf '  \033[1;33mcogload NOT armed on this Mac\033[0m — run in Terminal (not SSH):\n'
  printf '    bash %s/deploy/cogload-mac-setup.sh\n' "$REPO_DIR"
fi


say "Browser MCP glue → chrome-cdp.service + weekly update script (opt-in armed)"
# Runtime glue for the browser connector: a persistent loopback-CDP Chrome (systemd --user,
# Linux-only — this rides the GPU box) + the weekly self-update script for the Hermes cron
# job. Deploying the FILES is always safe; ARMING (systemctl enable / hermes cron create /
# hermes mcp add) stays opt-in, same posture as the loop. Unlike the timers we do NOT
# restart an armed chrome-cdp on re-sync: a restart would yank the browser out from under
# any agent mid-task; the unit file rarely changes, so pick it up on the next natural restart.
if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -f "$REPO_DIR/deploy/chrome-cdp.service" ]; then
  SD_DST="$HOME/.config/systemd/user"
  mkdir -p "$SD_DST"
  cp "$REPO_DIR/deploy/chrome-cdp.service" "$SD_DST/chrome-cdp.service"
  echo "  wrote $SD_DST/chrome-cdp.service"
  systemctl --user daemon-reload 2>/dev/null || true
  if systemctl --user is-enabled --quiet chrome-cdp.service 2>/dev/null; then
    echo "  chrome-cdp armed — unit re-synced (applies on its next restart; not bounced mid-task)"
  else
    printf '  \033[1;33mchrome-cdp NOT armed\033[0m — to run the browser MCP endpoint persistently:\n'
    printf '    systemctl --user enable --now chrome-cdp.service\n'
  fi
fi

say "Doctrine audit glue → live locations (monthly loop-queue seeder; opt-in armed)"
# --- doctrine contradiction audit: the harness's slowest clock (deploy always, arm opt-in) ---
# Monthly SEEDER only — it enqueues one loop-queue item and exits; it never invokes an agent.
# Caps (firing, queue, runtime, findings) live in the wrapper and units, not in prose:
# knowledge/doctrine-audit-2026-08-16.md. Deploying is safe; ARMING is SELECT.
DA_WRAP_DST="$CFG_DIR/claudemaxxing-doctrine-audit.sh"
if [ -f "$REPO_DIR/deploy/claudemaxxing-doctrine-audit.sh" ]; then
  sed "s#__REPO__#$REPO_DIR#g" "$REPO_DIR/deploy/claudemaxxing-doctrine-audit.sh" > "$DA_WRAP_DST"
  chmod +x "$DA_WRAP_DST"
  echo "  wrote $DA_WRAP_DST"
fi
if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -f "$REPO_DIR/deploy/claudemaxxing-doctrine-audit.service" ]; then
  SD_DST="$HOME/.config/systemd/user"
  mkdir -p "$SD_DST"
  sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO_DIR#g" "$REPO_DIR/deploy/claudemaxxing-doctrine-audit.service" > "$SD_DST/claudemaxxing-doctrine-audit.service"
  cp "$REPO_DIR/deploy/claudemaxxing-doctrine-audit.timer" "$SD_DST/claudemaxxing-doctrine-audit.timer"
  echo "  wrote $SD_DST/claudemaxxing-doctrine-audit.{service,timer}"
  systemctl --user daemon-reload 2>/dev/null || true
  if systemctl --user is-enabled --quiet claudemaxxing-doctrine-audit.timer 2>/dev/null; then
    systemctl --user restart claudemaxxing-doctrine-audit.timer 2>/dev/null \
      && echo "  reloaded doctrine-audit timer (was already armed → kept in sync)" \
      || echo "  NOTE: could not reload doctrine-audit timer"
  else
    printf '  \033[1;33mdoctrine audit NOT armed\033[0m — to enable the monthly 1st-of-month 09:00 audit:\n'
    printf '    systemctl --user enable --now claudemaxxing-doctrine-audit.timer\n'
  fi
fi

# Fleet layer (private machines only). install-fleet.sh deploys the tools and services that
# reach the owner's server/tailnet/Hermes; the graduated public core ships without it, so
# on a standalone client this is a silent no-op.
if [ -x "$REPO_DIR/install-fleet.sh" ]; then
  "$REPO_DIR/install-fleet.sh"
elif [ ! -f "$CFG_DIR/fleet.env" ]; then
  echo "  standalone client: no $CFG_DIR/fleet.env — fleet features stay off (template: deploy/fleet.env.example)"
fi

say "DONE — verify:"
echo "  command -v oll oll-council oll-sync mem-audit xsearch"
echo "  (cd /tmp && oll 'say hi' --model glm-5.3)"
echo "  (cd /tmp && xsearch 'test query' --days 1)"
echo "  Claude: new session → /gauntlet (divide broad requests into increments) · /fanout · /ideas"
echo '  Codex:  new thread  → $claudemaxxing:gauntlet to divide broad requests · $claudemaxxing:solplan first for nontrivial work · $claudemaxxing:fanout after planning · $claudemaxxing:memory; shell: g / g ls'
echo '  Fleet:  c-ubuntu · g-ubuntu · o-ubuntu · harness-sync status · gpu-agent c|g|o|ls|send'
