#!/usr/bin/env bash
# Tier 1c: real tmux transport with fake long-lived agent CLIs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "task-plan: tmux"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/task-plan.XXXXXX")"
TARGET_BIN="${TASK_PLAN_BIN_UNDER_TEST:-$ROOT/bin/task-plan}"
export TMUX_TMPDIR="$SCRATCH/tmux"
unset TMUX

cleanup() {
  while IFS= read -r session; do
    [[ -n "$session" ]] && tmux kill-session -t "=$session" 2>/dev/null || true
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
  rm -rf "$SCRATCH"
}
trap cleanup EXIT
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
mkdir -p "$TMUX_TMPDIR" "$SCRATCH/bin" "$SCRATCH/home/dev/fallback-project" \
  "$SCRATCH/repo-project" "$SCRATCH/kimi-project" "$SCRATCH/home"

export HOME="$SCRATCH/home"
export HERMES_KANBAN_DB="$SCRATCH/kanban.db"
export TASK_PLAN_AGENT_LOG="$SCRATCH/agents.jsonl"
export TASK_PLAN_SIDE_EFFECT_LOG="$SCRATCH/side-effects.log"
export HERMES_DASHBOARD_TOKEN="test-token"
export ORCH_DASHBOARD_URL="http://127.0.0.1:9"
export PATH="$SCRATCH/bin:$PATH"

cat > "$SCRATCH/bin/claude" <<'PY'
#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["TASK_PLAN_AGENT_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"agent": os.path.basename(__file__), "cwd": os.getcwd(), "argv": sys.argv[1:]}) + "\n")
time.sleep(300)
PY
cp "$SCRATCH/bin/claude" "$SCRATCH/bin/codex"
cp "$SCRATCH/bin/claude" "$SCRATCH/bin/opencode"

cat > "$SCRATCH/bin/curl" <<'SH'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$TASK_PLAN_SIDE_EFFECT_LOG"
SH

cat > "$SCRATCH/bin/hermes" <<'SH'
#!/usr/bin/env bash
printf 'hermes %s\n' "$*" >> "$TASK_PLAN_SIDE_EFFECT_LOG"
SH
chmod +x "$TARGET_BIN" "$SCRATCH/bin/claude" "$SCRATCH/bin/codex" \
  "$SCRATCH/bin/opencode" "$SCRATCH/bin/curl" "$SCRATCH/bin/hermes"

python3 - "$HERMES_KANBAN_DB" "$SCRATCH/repo-project" "$SCRATCH/kimi-project" <<'PY'
import sqlite3, sys
db, repo, kimi = sys.argv[1:]
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, slug TEXT, repo_path TEXT);
CREATE TABLE deals (id TEXT PRIMARY KEY, title TEXT, project_id TEXT);
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, project_id TEXT, deal_id TEXT);
""")
c.executemany("INSERT INTO projects VALUES (?,?,?,?)", [
    ("p_repo", "Repo Project", "repo-project", repo),
    ("p_kimi", "Kimi Project", "kimi-project", kimi),
    ("p_fallback", "Fallback Project", "fallback-project", None),
    ("p_missing", "Missing Project", "missing-project", None),
])
c.execute("INSERT INTO deals VALUES (?,?,?)", ("d1", "Signed deal", "p_repo"))
body = "Context line\n\n## Acceptance\n- exact task id in brief\n- detached session lives"
c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", [
    ("t_repo", "Plan repo task", body, "p_repo", "d1"),
    ("t_kimi", "Plan with Kimi", body, "p_kimi", None),
    ("t_fallback", "Plan fallback task", body, "p_fallback", None),
    ("t_missing", "Refuse missing task", body, "p_missing", None),
])
c.commit()
PY

if [ "${TASK_PLAN_REFUSAL_CANARY:-0}" = 1 ]; then
  mkdir -p "$HOME/dev/missing-project"
fi
set +e
missing_out="$($TARGET_BIN t_missing --planner fable --json 2>&1)"
missing_rc=$?
set -e
[ "$missing_rc" -ne 0 ] || fail "missing folder was accepted (red canary reached this assertion)"
case "$missing_out" in
  *"Populate projects.repo_path"*) ;;
  *) fail "missing-folder error is not actionable: $missing_out" ;;
esac

repo_json="$($TARGET_BIN t_repo --planner fable --json)"
fallback_json="$($TARGET_BIN t_fallback --planner opus1m --json)"
sol_json="$($TARGET_BIN t_repo --planner sol --json)"
oplan_json="$($TARGET_BIN t_repo --planner oplan --json)"
kimiplan_json="$($TARGET_BIN t_kimi --planner kimiplan --json)"

python3 - "$repo_json" "$fallback_json" "$sol_json" "$oplan_json" "$kimiplan_json" "$SCRATCH/repo-project" "$HOME/dev/fallback-project" "$SCRATCH/kimi-project" <<'PY'
import json, os, sys
repo, fallback, sol, oplan, kimiplan = map(json.loads, sys.argv[1:6])
repo_dir, fallback_dir, kimi_dir = map(os.path.realpath, sys.argv[6:9])
assert repo == {"session": "claude-repo-project-planning", "attach_hint": "cs 1",
                "folder": repo_dir, "planner": "fable"}, repo
assert fallback["session"] == "claude-fallback-project-planning", fallback
assert fallback["folder"] == fallback_dir, fallback
assert fallback["planner"] == "opus1m", fallback
assert sol["session"] == "codex-repo-project-planning", sol
assert sol["planner"] == "sol", sol
assert sol["attach_hint"] == "g ls 1", sol
assert oplan["session"] == "opencode-repo-project-planning", oplan
assert oplan["planner"] == "oplan", oplan
assert oplan["attach_hint"] == "o ls 1", oplan
assert kimiplan["session"] == "opencode-kimi-project-planning", kimiplan
assert kimiplan["folder"] == kimi_dir, kimiplan
assert kimiplan["planner"] == "kimiplan", kimiplan
assert kimiplan["attach_hint"] == "o ls 1", kimiplan
PY

for session in claude-repo-project-planning claude-fallback-project-planning codex-repo-project-planning opencode-repo-project-planning opencode-kimi-project-planning; do
  tmux has-session -t "=$session" 2>/dev/null || fail "$session is not alive"
  attached="$(tmux display-message -p -t "=$session:" '#{session_attached}')"
  [ "$attached" = 0 ] || fail "$session is attached"
done

repo_cwd="$(tmux display-message -p -t '=claude-repo-project-planning:' '#{pane_current_path}')"
fallback_cwd="$(tmux display-message -p -t '=claude-fallback-project-planning:' '#{pane_current_path}')"
kimi_cwd="$(tmux display-message -p -t '=opencode-kimi-project-planning:' '#{pane_current_path}')"
repo_expected="$(cd "$SCRATCH/repo-project" && pwd -P)"
fallback_expected="$(cd "$HOME/dev/fallback-project" && pwd -P)"
kimi_expected="$(cd "$SCRATCH/kimi-project" && pwd -P)"
[ "$repo_cwd" = "$repo_expected" ] || fail "repo_path cwd mismatch: $repo_cwd"
[ "$fallback_cwd" = "$fallback_expected" ] || fail "fallback cwd mismatch: $fallback_cwd"
[ "$kimi_cwd" = "$kimi_expected" ] || fail "kimi repo_path cwd mismatch: $kimi_cwd"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ "$(wc -l < "$TASK_PLAN_AGENT_LOG" 2>/dev/null || true)" -ge 5 ] && break
  sleep 0.1
done
python3 - "$TASK_PLAN_AGENT_LOG" "$SCRATCH/repo-project" "$HOME/dev/fallback-project" <<'PY'
import json, os, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert len(rows) == 5, rows
by = {(r["agent"], os.path.basename(r["cwd"])): r for r in rows}
fable = by[("claude", "repo-project")]
assert any("/fableplan" in a and "Task ID: t_repo" in a for a in fable["argv"]), fable
opus = by[("claude", "fallback-project")]
assert "--model" in opus["argv"] and "opus[1m]" in opus["argv"], opus
assert "--permission-mode" in opus["argv"] and "plan" in opus["argv"], opus
assert any("Task ID: t_fallback" in a for a in opus["argv"]), opus
sol = next(r for r in rows if r["agent"] == "codex")
assert any(a.startswith("$orchestratormaxxing:solplan") and "Task ID: t_repo" in a for a in sol["argv"]), sol
# oplan: compatibility selector resolves to the canonical K3 kimiplan agent.
oc = by[("opencode", "repo-project")]
assert "--agent" in oc["argv"] and "kimiplan" in oc["argv"], oc
assert "--prompt" in oc["argv"], oc
assert any("Task ID: t_repo" in a for a in oc["argv"]), oc
kimi = by[("opencode", "kimi-project")]
start = kimi["argv"].index("--agent")
assert kimi["argv"][start:start + 4] == ["--agent", "kimiplan", "--prompt", next(a for a in kimi["argv"] if "Task ID: t_kimi" in a)], kimi
assert any("Task ID: t_kimi" in a for a in kimi["argv"]), kimi
PY

grep -q 'hermes kanban comment t_repo' "$TASK_PLAN_SIDE_EFFECT_LOG" \
  || fail "kanban comment was not emitted"
grep -q '/api/tasks/t_repo/session' "$TASK_PLAN_SIDE_EFFECT_LOG" \
  || fail "task/session hard link was not requested"
grep -q 'hermes kanban comment t_kimi' "$TASK_PLAN_SIDE_EFFECT_LOG" \
  || fail "kimiplan kanban comment was not emitted"
grep -q '/api/tasks/t_kimi/session' "$TASK_PLAN_SIDE_EFFECT_LOG" \
  || fail "kimiplan task/session hard link was not requested"

printf 'task-plan: real tmux + folder resolution + planners verified\n'
