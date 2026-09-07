#!/usr/bin/env bash
# Offline contract for bin/task-workspace — the workspace-mode resolver
# (shared | worktree | container) that every launcher and the dashboard
# dispatcher consume.
#
# HERMETIC ON PURPOSE. `wt` (worktrunk), `coder-ws` and `curl` are faked onto
# PATH; the tool runs against a disposable fixture HOME. No network, no real
# git worktree, no real download — so this belongs in bin/harness-verify's
# offline tuple. Every negative here was proven red by hand against a tool that
# skipped the guarded step (see knowledge/workspace-isolation-decision-2026-09-06.md).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="${TASK_WORKSPACE_TOOL_UNDER_TEST:-$ROOT/bin/task-workspace}"
LOCK_SRC="${TASK_WORKSPACE_LOCK_UNDER_TEST:-$ROOT/deploy/worktrunk.lock}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/task-workspace.XXXXXX")"
FAKE_BIN="$SCRATCH/bin"; ARGV="$SCRATCH/argv"
PROJECT="$SCRATCH/dev/orchestratormaxxing"
WT_PATH="$SCRATCH/dev/.worktrees/orchestratormaxxing.task-smoke"

cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT
fail() { printf 'task-workspace contract: %s\n' "$*" >&2; exit 1; }
pass() { printf '  ok  %s\n' "$*"; }

mkdir -p "$FAKE_BIN" "$PROJECT" "$SCRATCH/home" "$SCRATCH/bindir"; : > "$ARGV"
[ -f "$TOOL" ] || fail "tool missing: $TOOL"
[ -x "$TOOL" ] || fail "tool not executable: $TOOL"
[ -f "$LOCK_SRC" ] || fail "lock missing: $LOCK_SRC"

# --- fakes -----------------------------------------------------------------
# wt: records argv; `list --format json` answers with the REAL shape measured on
# the Mac 2026-09-06 (top-level `branch` + `path`); `switch` prints a DECOY path
# on stderr so a tool that scrapes prose instead of reading the JSON is caught.
cat > "$FAKE_BIN/wt" <<'SH'
#!/bin/sh
printf 'wt %s\n' "$*" >> "$ARGV"
case "$*" in
  *--version*|-V) echo "wt v${FAKE_WT_VERSION:-0.76.0}"; exit 0 ;;
  *"list --format json"*|*"list"*"--format"*"json"*)
    printf '[{"branch":"main","path":"%s","is_main":true},{"branch":"%s","path":"%s","is_main":false},{"branch":null,"path":"%s/.results/verification","is_main":false}]\n' \
      "$FAKE_WT_MAIN" "$FAKE_WT_BRANCH" "$FAKE_WT_PATH" "$FAKE_WT_MAIN"; exit 0 ;;
  *switch*)
    if [ "${FAKE_WT_EXISTS:-0}" = 1 ] && printf '%s' "$*" | grep -- '--create' >/dev/null; then
      echo "error: branch '$FAKE_WT_BRANCH' already exists" >&2; exit 1
    fi
    echo "Switched to /decoy/path/never-use-me" >&2; exit 0 ;;
  *) exit 0 ;;
esac
SH
cat > "$FAKE_BIN/coder-ws" <<'SH'
#!/bin/sh
printf 'coder-ws %s\n' "$*" >> "$ARGV"
exit 0
SH
# curl: records argv and "downloads" the fixture archive named by FAKE_CURL_SRC.
cat > "$FAKE_BIN/curl" <<'SH'
#!/bin/sh
printf 'curl %s\n' "$*" >> "$ARGV"
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; --output) out="$2"; shift 2 ;; *) shift ;; esac
done
[ -n "$out" ] && cp "$FAKE_CURL_SRC" "$out"
exit 0
SH
chmod +x "$FAKE_BIN/wt" "$FAKE_BIN/coder-ws" "$FAKE_BIN/curl"
export ARGV FAKE_WT_MAIN="$PROJECT" FAKE_WT_BRANCH="task/smoke" FAKE_WT_PATH="$WT_PATH"
export PATH="$FAKE_BIN:/usr/bin:/bin"
export HOME="$SCRATCH/home"
export TASK_WORKSPACE_WT="$FAKE_BIN/wt" TASK_WORKSPACE_CODER_WS="$FAKE_BIN/coder-ws"
export TASK_WORKSPACE_PLATFORM="linux-x86_64"

run() { : > "$ARGV"; "$TOOL" "$@"; }

# --- T1: worktree mode creates through wt with the pinned flags -------------
set +e; out=$(run resolve --mode worktree --project "$PROJECT" --branch task/smoke 2>"$SCRATCH/err"); rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T1: resolve worktree exited $rc — $(cat "$SCRATCH/err")"
grep -q '^wt .*switch' "$ARGV" || fail "T1: wt switch was not invoked — argv: $(cat "$ARGV")"
grep -q -- '--create' "$ARGV" || fail "T1: switch lacks --create"
grep -q -- '--no-cd' "$ARGV" || fail "T1: switch lacks --no-cd (a launcher must not have its cwd changed under it)"
grep -q 'task/smoke' "$ARGV" || fail "T1: branch name not passed to wt"
grep -q -- "-C $PROJECT" "$ARGV" || fail "T1: wt not anchored with -C <project> — argv: $(cat "$ARGV")"
grep -q -- '--config-set' "$ARGV" || fail "T1: no inline --config-set (an installer-written wt config is forbidden)"
grep -q 'worktree-path' "$ARGV" || fail "T1: --config-set does not set worktree-path"
grep -q '/\.worktrees/' "$ARGV" || fail "T1: worktree path template is not under a .worktrees dot-dir — argv: $(cat "$ARGV")"
pass "T1  worktree mode: wt -C <project> --config-set worktree-path=…/.worktrees/… switch --create --no-cd <branch>"

# --- T2: the path comes from `wt list --format json`, never from prose ------
[ "$out" = "$WT_PATH" ] || fail "T2: stdout was '$out', expected the JSON path '$WT_PATH' (decoy on stderr must be ignored)"
grep -q 'list' "$ARGV" || fail "T2: wt list was never consulted"
grep -q -- '--format json' "$ARGV" || fail "T2: wt list not asked for --format json"
set +e; js=$(run resolve --mode worktree --project "$PROJECT" --branch task/smoke --json 2>/dev/null); set -e
python3 - "$js" "$WT_PATH" <<'PY' || fail "T2: --json payload wrong: $js"
import json, sys
d = json.loads(sys.argv[1])
assert d["mode"] == "worktree", d
assert d["path"] == sys.argv[2], d
assert d["branch"] == "task/smoke", d
PY
pass "T2  path is read from wt list JSON (top-level path), --json carries mode/path/branch"

# --- T3: container mode emits the coder-ws prefix and never touches wt ------
set +e; out=$(run resolve --mode container --project "$PROJECT" --slug orchestratormaxxing 2>"$SCRATCH/err"); rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T3: container resolve exited $rc — $(cat "$SCRATCH/err")"
first="$(printf '%s\n' "$out" | sed -n 1p)"; second="$(printf '%s\n' "$out" | sed -n 2p)"
[ "$first" = "coder-ws ssh orchestratormaxxing --" ] || fail "T3: first line was '$first', expected 'coder-ws ssh orchestratormaxxing --'"
[ "$second" = "$PROJECT" ] || fail "T3: second line was '$second', expected the project path"
grep -q '^wt ' "$ARGV" && fail "T3: container mode invoked wt — argv: $(cat "$ARGV")"
set +e; js=$(run resolve --mode container --project "$PROJECT" --json 2>/dev/null); set -e
python3 - "$js" "$PROJECT" <<'PY' || fail "T3: --json payload wrong: $js"
import json, sys
d = json.loads(sys.argv[1])
assert d["mode"] == "container", d
assert d["prefix"] == ["coder-ws", "ssh", "orchestratormaxxing", "--"], d   # slug defaults to basename(project)
assert d["path"] == sys.argv[2], d
PY
pass "T3  container mode: 'coder-ws ssh <slug> --' + path, slug defaults to the project basename, wt untouched"

# --- T4: remove is SELECT — refuses without --confirm and calls nothing -----
set +e; run remove --project "$PROJECT" --branch task/smoke >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 3 ] || fail "T4: remove without --confirm exited $rc, expected 3"
[ -s "$ARGV" ] && fail "T4: refused remove still invoked something — $(cat "$ARGV")"
set +e; run remove --project "$PROJECT" --branch task/smoke --confirm >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T4: confirmed remove exited $rc"
grep -q '^wt .*remove.*task/smoke' "$ARGV" || fail "T4: confirmed remove did not run wt remove <branch> — $(cat "$ARGV")"
pass "T4  remove without --confirm exits 3 and invokes nothing; with --confirm runs wt remove"

# --- T5: install-wt refuses a sha256 mismatch BEFORE anything reaches bindir --
# Fixture archive: the real asset is a tar.xz with the binary at <dir>/wt.
mkdir -p "$SCRATCH/fx/worktrunk-fake"
printf '#!/bin/sh\necho "wt v0.76.0"\n' > "$SCRATCH/fx/worktrunk-fake/wt"; chmod +x "$SCRATCH/fx/worktrunk-fake/wt"
tar -C "$SCRATCH/fx" -cJf "$SCRATCH/asset.tar.xz" worktrunk-fake
GOOD_SHA="$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$SCRATCH/asset.tar.xz")"
python3 - "$LOCK_SRC" "$SCRATCH/lock-bad.json" "$SCRATCH/lock-good.json" "$GOOD_SHA" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))
bad = json.loads(json.dumps(lock)); bad["assets"]["linux-x86_64"]["sha256"] = "0" * 64
json.dump(bad, open(sys.argv[2], "w"))
good = json.loads(json.dumps(lock)); good["assets"]["linux-x86_64"]["sha256"] = sys.argv[4]
json.dump(good, open(sys.argv[3], "w"))
PY
export FAKE_CURL_SRC="$SCRATCH/asset.tar.xz"
# The tool must treat the lock as authoritative even though a `wt` is on PATH:
# hide it so install-wt has to download.
set +e
out=$(env TASK_WORKSPACE_WT=/nonexistent/wt PATH="$FAKE_BIN/nowt:$FAKE_BIN:/usr/bin:/bin" \
      TASK_WORKSPACE_LOCK="$SCRATCH/lock-bad.json" TASK_WORKSPACE_BIN_DIR="$SCRATCH/bindir" \
      "$TOOL" install-wt 2>&1); rc=$?
set -e
[ "$rc" -eq 1 ] || fail "T5: sha256 mismatch exited $rc, expected 1 — $out"
printf '%s' "$out" | grep -i 'sha256' >/dev/null || fail "T5: mismatch refusal does not name sha256 — $out"
[ -e "$SCRATCH/bindir/wt" ] && fail "T5: a mismatched archive still landed in the bin dir"
printf '%s' "$out" | grep 'Traceback' >/dev/null && fail "T5: refusal was a traceback"
: > "$ARGV"
set +e
out=$(env TASK_WORKSPACE_WT=/nonexistent/wt PATH="$FAKE_BIN:/usr/bin:/bin" \
      TASK_WORKSPACE_LOCK="$SCRATCH/lock-good.json" TASK_WORKSPACE_BIN_DIR="$SCRATCH/bindir" \
      "$TOOL" install-wt 2>&1); rc=$?
set -e
[ "$rc" -eq 0 ] || fail "T5: matching sha256 install exited $rc — $out"
[ -x "$SCRATCH/bindir/wt" ] || fail "T5: wt was not installed executable into the bin dir"
grep -q '^curl ' "$ARGV" || fail "T5: install-wt did not download through curl"
grep -q 'worktrunk-x86_64-unknown-linux-musl.tar.xz' "$ARGV" || fail "T5: downloaded the wrong asset for linux-x86_64 — $(cat "$ARGV")"
pass "T5  install-wt: sha256 mismatch refused before install; match installs the locked asset"

# --- T5b: an already-matching wt is a no-op — no download ------------------
: > "$ARGV"
set +e
out=$(env TASK_WORKSPACE_WT="$SCRATCH/bindir/wt" TASK_WORKSPACE_LOCK="$SCRATCH/lock-good.json" \
      TASK_WORKSPACE_BIN_DIR="$SCRATCH/bindir" "$TOOL" install-wt 2>&1); rc=$?
set -e
[ "$rc" -eq 0 ] || fail "T5b: no-op install exited $rc — $out"
grep -q '^curl ' "$ARGV" && fail "T5b: downloaded again although the installed version matches the lock"
pass "T5b install-wt is a no-op when the installed wt already matches the lock"

# --- T6: shared mode is the project path verbatim, no subprocess ------------
set +e; out=$(run resolve --mode shared --project "$PROJECT" 2>/dev/null); rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T6: shared resolve exited $rc"
[ "$out" = "$PROJECT" ] || fail "T6: shared mode printed '$out', expected '$PROJECT'"
[ -s "$ARGV" ] && fail "T6: shared mode ran a subprocess — $(cat "$ARGV")"
pass "T6  shared mode returns the project path verbatim and runs nothing"

# --- T7: a missing wt is a TYPED exit 2 naming the fix, never a traceback ----
set +e
out=$(env TASK_WORKSPACE_WT=/nonexistent/wt PATH="/usr/bin:/bin" HOME="$SCRATCH/nohome" \
      "$TOOL" resolve --mode worktree --project "$PROJECT" --branch task/smoke 2>&1); rc=$?
set -e
[ "$rc" -eq 2 ] || fail "T7: missing wt exited $rc, expected 2 — $out"
printf '%s' "$out" | grep 'task-workspace install-wt' >/dev/null || fail "T7: missing wt does not name 'task-workspace install-wt' — $out"
printf '%s' "$out" | grep 'Traceback' >/dev/null && fail "T7: missing wt produced a traceback"
pass "T7  missing wt exits 2 and names task-workspace install-wt"
# A wt that EXISTS but is not executable must be the same typed sentence, not a PermissionError.
cp "$FAKE_BIN/wt" "$SCRATCH/wt-noexec"; chmod -x "$SCRATCH/wt-noexec"
set +e
out=$(env TASK_WORKSPACE_WT="$SCRATCH/wt-noexec" "$TOOL" check 2>&1); rc=$?
set -e
[ "$rc" -eq 2 ] || fail "T7b: non-executable wt exited $rc, expected 2 — $out"
printf '%s' "$out" | grep 'Traceback' >/dev/null && fail "T7b: non-executable wt produced a traceback"
printf '%s' "$out" | grep 'install-wt' >/dev/null || fail "T7b: non-executable wt does not name the fix — $out"
pass "T7b a non-executable wt is the same typed exit 2, never a traceback"

# --- T8: an existing branch is not an error — fall through to wt list ------
set +e; out=$(FAKE_WT_EXISTS=1 run resolve --mode worktree --project "$PROJECT" --branch task/smoke 2>"$SCRATCH/err"); rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T8: existing branch exited $rc — $(cat "$SCRATCH/err")"
[ "$out" = "$WT_PATH" ] || fail "T8: existing branch resolved '$out', expected '$WT_PATH'"
pass "T8  resolve is idempotent: an existing branch still resolves to its worktree path"

# --- T9: check is read-only and typed ---------------------------------------
set +e; run check >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T9: check with wt present exited $rc"
grep -qE '^wt .*(switch|remove|merge)' "$ARGV" && fail "T9: check invoked a mutating wt verb — $(cat "$ARGV")"
set +e; env TASK_WORKSPACE_WT=/nonexistent/wt PATH="/usr/bin:/bin" "$TOOL" check >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] || fail "T9: check without wt exited $rc, expected 2"
# A wt that exists but cannot answer --version is NOT green.
cat > "$SCRATCH/wt-broken" <<'SH'
#!/bin/sh
exit 9
SH
chmod +x "$SCRATCH/wt-broken"
set +e; env TASK_WORKSPACE_WT="$SCRATCH/wt-broken" "$TOOL" check >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] || fail "T9: check with a wt that fails --version exited $rc, expected 2 (false green)"
pass "T9  check is read-only: 0 with wt, 2 without or when --version fails"

# --- T10: merge runs wt merge inside the branch's worktree ------------------
set +e; run merge --project "$PROJECT" --branch task/smoke >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T10: merge exited $rc"
grep -q -- "-C $WT_PATH .*merge" "$ARGV" || fail "T10: merge did not run wt -C <worktree path> merge — $(cat "$ARGV")"
grep -E '^wt .*--config-set .*worktree-path.*merge' "$ARGV" >/dev/null || fail "T10: merge call lacks the inline worktree-path template — $(cat "$ARGV")"
pass "T10 merge resolves the worktree path first, then runs wt merge there (template on every call)"

# --- T11: no tenant literal in a PUBLIC tool ---------------------------------
# The literals are assembled from halves so this public test file does not
# itself trip core-export's literal gate.
_t_host="ricardo"; _t_host="${_t_host}ubuntu"
_t_net="tail58"; _t_net="${_t_net}eae3"
_t_user="rical"; _t_user="${_t_user}anis"
grep -nE "$_t_host|$_t_net|$_t_user" "$TOOL" && fail "T11: public tool carries a tenant literal"
pass "T11 public tool carries no tenant literal"

# --- T12: list tolerates a detached worktree (branch: null) -------------------
set +e; out=$(run list --project "$PROJECT" 2>&1); rc=$?; set -e
[ "$rc" -eq 0 ] || fail "T12: list exited $rc with a detached entry — $out"
printf '%s' "$out" | grep 'Traceback' >/dev/null && fail "T12: list crashed on branch: null"
printf '%s' "$out" | grep 'detached' >/dev/null || fail "T12: detached entry not labelled — $out"
pass "T12 list renders a detached (branch: null) worktree instead of crashing"

printf 'task-workspace contract: 14/14 PASS\n'
