#!/usr/bin/env bash
# Worktrunk must be first-class on ALL SIX surfaces — Claude Code, Codex,
# OpenCode, Hermes, Warp, Zed. One grep-able presence check per surface, run
# against WORKTREE_SURFACES_ROOT (default: this repo). The negative fixture
# copies the surfaces into a scratch root, strips the literal from one of
# them, and asserts the runner exits 1 naming that surface.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${WORKTREE_SURFACES_ROOT:-$ROOT}"
fail() { printf 'worktree-surfaces contract: %s\n' "$*" >&2; exit 1; }
pass() { printf '  ok  %s\n' "$*"; }

# surface label | file | required literal(s), '|'-separated
SURFACES=(
  "Claude command|.claude/commands/worktree.md|task-workspace resolve|wt "
  "Claude/Codex skill|plugins/orchestratormaxxing/skills/worktree/SKILL.md|task-workspace resolve|--mode shared|stage only owned hunks|wt "
  "Codex plugin prompt|plugins/orchestratormaxxing/skills/worktree/agents/openai.yaml|\$orchestratormaxxing:worktree"
  "Codex plugin manifest|plugins/orchestratormaxxing/.codex-plugin/plugin.json|\$orchestratormaxxing:worktree"
  "OpenCode command|opencode/commands/worktree.md|task-workspace resolve|wt "
  "OpenCode+Hermes stack|skills/external-stack.json|plugins/orchestratormaxxing/skills/worktree|Hermes|OpenCode"
  "Hermes+all doctrine block|CLAUDE.md|task-workspace|workspace:"
  "Warp workflow|deploy/warp/orchestratormaxxing-worktree.yaml|task-workspace resolve|name:|command:"
  "Warp workflow (list)|deploy/warp/orchestratormaxxing-worktree-list.yaml|wt |name:|command:"
  "Warp in-repo|.warp/workflows/orchestratormaxxing-worktree.yaml|task-workspace resolve"
  "Zed tasks|.zed/tasks.json|task-workspace resolve|wt "
  "Installer (Claude command)|install.sh|commands/worktree.md"
  "Installer (bin)|install.sh|bin/task-workspace"
  "Installer (lock)|install.sh|worktrunk.lock"
  "Installer (Warp workflows)|install.sh|deploy/warp"
)

check_root() {
  local root="$1" entry label file lits lit
  for entry in "${SURFACES[@]}"; do
    IFS='|' read -r label file lits <<< "$entry"
    [ -f "$root/$file" ] || { printf 'missing surface [%s]: %s\n' "$label" "$file" >&2; return 1; }
    # everything after the second '|' is a '|'-separated list of literals
    lits="${entry#*|}"; lits="${lits#*|}"
    while IFS= read -r -d '|' lit || [ -n "$lit" ]; do
      grep -qF -- "$lit" "$root/$file" || { printf 'surface [%s] %s lacks %q\n' "$label" "$file" "$lit" >&2; return 1; }
    done <<< "$lits|"
  done
  python3 -m json.tool "$root/.zed/tasks.json" >/dev/null 2>&1 || { echo ".zed/tasks.json is not valid JSON" >&2; return 1; }
  return 0
}

if [ -n "${WORKTREE_SURFACES_ROOT:-}" ]; then
  # Fixture mode: just report on the given root.
  check_root "$SRC"
  exit $?
fi

check_root "$ROOT" || fail "a surface is missing its worktrunk wiring (see above)"
pass "all ${#SURFACES[@]} surfaces carry the worktrunk wiring (Claude, Codex, OpenCode, Hermes, Warp, Zed, installer)"

# --- negative fixture: strip the literal from one surface, expect exit 1 ------
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/worktree-surfaces.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
for entry in "${SURFACES[@]}"; do
  IFS='|' read -r _ file _ <<< "$entry"
  mkdir -p "$SCRATCH/$(dirname "$file")"; cp "$ROOT/$file" "$SCRATCH/$file"
done
python3 - "$SCRATCH/opencode/commands/worktree.md" <<'PY2'
import sys
p = sys.argv[1]; t = open(p).read().replace("task-workspace resolve", "task-workspace resolv3")
open(p, "w").write(t)
PY2
set +e
out="$(WORKTREE_SURFACES_ROOT="$SCRATCH" bash "$0" 2>&1)"; rc=$?
set -e
[ "$rc" -eq 1 ] || fail "negative fixture exited $rc, expected 1"
printf '%s' "$out" | grep 'OpenCode command' >/dev/null || fail "negative fixture did not name the stripped surface — $out"
pass "negative fixture: a stripped OpenCode surface is named and fails the gate"

printf 'worktree-surfaces contract: 2/2 PASS\n'
