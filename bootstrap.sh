#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# orchestratormaxxing bootstrap — port the orchestrator harness to a new machine.
#
# Installs/configures: bun · Codex · OpenCode · gstack (Claude + Codex + OpenCode)
# · Ollama Cloud provider (key + live model sync) · xAI key (optional) · this repo's bin/ tools.
# Existing Codex auth/config/history are always preserved.
#
# Prereqs: macOS with Homebrew, git, python3. Provide your keys via env:
#   export OLLAMA_API_KEY=...   # from https://ollama.com  (Settings → Keys)
#   export XAI_API_KEY=...      # optional, from https://console.x.ai (enables xsearch)
# Optional: ORCHESTRATORMAXXING_SKIP_GSTACK=1 skips the gstack clone/setup (default installs it).
#
# Run from inside the repo:  ./bootstrap.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
say(){ printf "\n\033[1;36m== %s\033[0m\n" "$*"; }
need(){ command -v "$1" >/dev/null 2>&1; }

: "${OLLAMA_API_KEY:?set OLLAMA_API_KEY before running (export OLLAMA_API_KEY=...)}"
XAI_API_KEY="${XAI_API_KEY:-}"   # optional; empty → no .env, no xsearch

say "Base tools (brew, git, python3, jq, node)"
need brew || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
need git    || brew install git
need python3|| brew install python
need jq     || brew install jq
need node   || brew install node
python3 -m pip install --user --quiet certifi 2>/dev/null \
  || python3 -m pip install --user --break-system-packages --quiet certifi 2>/dev/null || true

say "bun"
need bun || curl -fsSL https://bun.sh/install | bash
export BUN_INSTALL="$HOME/.bun"; export PATH="$BUN_INSTALL/bin:$PATH"

say "OpenCode"
need opencode || curl -fsSL https://opencode.ai/install | bash
export PATH="$HOME/.opencode/bin:/opt/homebrew/bin:$PATH"

say "OpenAI Codex (preserve existing auth/config)"
need codex || npm install -g @openai/codex
codex --version

if [ "${ORCHESTRATORMAXXING_SKIP_GSTACK:-0}" = "1" ]; then
say "gstack — skipped (ORCHESTRATORMAXXING_SKIP_GSTACK=1)"
else
say "gstack (Claude + Codex + OpenCode hosts)"
rm -rf "$HOME/.claude/skills/gstack"
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git "$HOME/.claude/skills/gstack"
( cd "$HOME/.claude/skills/gstack" && \
  ./setup --host claude && ./setup --host codex && ./setup --host opencode )

say "Global ~/.claude/CLAUDE.md (gstack section)"
if [ ! -f "$HOME/.claude/CLAUDE.md" ]; then
cat > "$HOME/.claude/CLAUDE.md" <<'MD'
# Global Claude Code instructions

## gstack
gstack (Garry Tan's stack) is installed at `~/.claude/skills/gstack`.
- **Use the `/browse` skill from gstack for ALL web browsing.**
- **NEVER use `mcp__claude-in-chrome__*` tools.**
MD
fi
fi

say "Ollama Cloud → OpenCode auth + provider"
mkdir -p "$HOME/.config/opencode" "$HOME/.local/share/opencode"
OLLAMA_API_KEY="$OLLAMA_API_KEY" python3 - <<'PY'
import json, os
auth_p = os.path.expanduser("~/.local/share/opencode/auth.json")
auth = json.load(open(auth_p)) if os.path.exists(auth_p) else {}
auth["ollama-cloud"] = {"type": "api", "key": os.environ["OLLAMA_API_KEY"]}
json.dump(auth, open(auth_p, "w"), indent=2); os.chmod(auth_p, 0o600)

cfg_p = os.path.expanduser("~/.config/opencode/opencode.json")
cfg = json.load(open(cfg_p)) if os.path.exists(cfg_p) else {"$schema": "https://opencode.ai/config.json"}
oc = cfg.setdefault("provider", {}).setdefault("ollama-cloud", {})
oc.setdefault("npm", "@ai-sdk/openai-compatible")
oc.setdefault("name", "Ollama Cloud")
oc.setdefault("options", {})["baseURL"] = "https://ollama.com/v1"
oc.setdefault("models", {})

# NOTE: the two coding agents (kimi-coder/glm-coder) are ensured by install.sh — the single
# source of truth for the agent spec — which this script runs at the end. We set up auth +
# the provider stub here (needed before the oll-sync model sync below).
json.dump(cfg, open(cfg_p, "w"), indent=2)
print("opencode auth + provider 'ollama-cloud' ensured (agents via install.sh)")
PY

say "Repo tools + .env + live model sync"
chmod +x "$REPO_DIR"/bin/* 2>/dev/null || true
if [ -n "$XAI_API_KEY" ]; then
printf 'XAI_API_KEY=%s\n' "$XAI_API_KEY" > "$REPO_DIR/.env"
else
echo "  (no XAI_API_KEY — skipping .env; xsearch stays unconfigured until you set one)"
fi
"$REPO_DIR/bin/oll-sync" || true

say "Deploy orchestrator harness globally (standalone)"
# Installs bridges→~/.local/bin, Claude commands/agents, the Codex plugin/agents,
# shell helpers, xAI key, and global doctrine pointers. Idempotent; re-run to re-sync.
"$REPO_DIR/install.sh" || true

say "DONE — verify:"
echo "  opencode run -m ollama-cloud/glm-5.3 'say hi'"
echo "  (cd /tmp && oll-council 'pick a sort algorithm')   # bare name, on PATH, any dir"
if [ -n "$XAI_API_KEY" ]; then echo "  (cd /tmp && xsearch 'test query' --days 3)"; fi
echo "  (new Claude Code session) /browse · /fanout · /ideas"
echo '  (new Codex thread) $orchestratormaxxing:fanout · $orchestratormaxxing:ideas · $orchestratormaxxing:memory; shell: g / g ls'
echo '  fleet machines: see install-fleet.sh (private)'
