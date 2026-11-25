#!/usr/bin/env bash
# Contract for bin/slop-check: pinned rules, changed-lines scope, and no false "clean".
set -euo pipefail
# Isolation: the caller's git environment and config never reach the fixtures or the tool.
unset "${!GIT_@}"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/slop-check"
RULES="$ROOT/deploy/slop-rules"
. "$ROOT/tests/lib/precondition.sh"

fail() { printf 'slop-check contract: FAIL: %s\n' "$*" >&2; exit 1; }
[ -x "$TOOL" ] || fail 'bin/slop-check is missing or not executable'
TMP="$(cd "$(mktemp -d)" && pwd -P)" || exit 2
trap 'rm -rf -- "$TMP"' EXIT

# S1 (no venv needed): every default rule id exists in the vendored rules, and the lock pins hashes.
python3 - "$RULES" <<'PY' || fail 'S1 vendored rules or lock are inconsistent'
import re, sys
from pathlib import Path
rules = Path(sys.argv[1])
ids = re.findall(r"^id:\s*(\S+)", "".join(p.read_text() for p in rules.glob("*.yaml")), re.M)
wanted = (rules / "default-rules.txt").read_text().split()
assert len(ids) == len(set(ids)) == 197, len(ids)
assert len(wanted) == len(set(wanted)) == 25, sorted(wanted)
assert set(wanted) <= set(ids), set(wanted) - set(ids)
lock = (rules / "requirements.lock").read_text()
assert lock.startswith("ast-grep-cli==0.42.1") and lock.count("--hash=sha256:") >= 4
assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in re.findall(r"--hash=sha256:(\S*)", lock)), lock
PY

# S2 (no venv needed): a missing venv is "cannot scan" (exit 2), never clean.
printf 'try:\n    x = 1\nexcept ValueError:\n    pass\n' > "$TMP/slop.py"
rc=0; SLOP_CHECK_VENV="$TMP/absent" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "S2 missing venv exited $rc, wanted 2"
grep -q 'slop-check --install' "$TMP/err" || fail 'S2 missing venv did not name the fix'

# S3 (no venv needed): rules without their hash lock are "cannot scan" (exit 2), not a traceback.
mkdir -p "$TMP/nolock" "$TMP/fake-venv/bin"
cp "$RULES"/*.yaml "$RULES/default-rules.txt" "$TMP/nolock/"
: > "$TMP/fake-venv/bin/ast-grep"; echo stamp > "$TMP/fake-venv/.lock-sha256"
rc=0; SLOP_CHECK_RULES="$TMP/nolock" SLOP_CHECK_VENV="$TMP/fake-venv" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "S3 rules without a lock exited $rc, wanted 2: $(tail -1 "$TMP/err")"
grep -q 'requirements.lock' "$TMP/err" || fail 'S3 did not name the missing lock'

# S4 (no venv needed): --install never clears a directory that is not a virtualenv.
mkdir "$TMP/precious"; echo keep > "$TMP/precious/notes.txt"
rc=0; SLOP_CHECK_VENV="$TMP/precious" "$TOOL" --install >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "S4 --install into a non-venv directory exited $rc, wanted 2"
[ -f "$TMP/precious/notes.txt" ] || fail 'S4 --install deleted files in a non-venv directory'
# A pyvenv.cfg alone does not make a directory ours: only the tool's own stamp does.
mkdir "$TMP/other-venv"; : > "$TMP/other-venv/pyvenv.cfg"; echo keep > "$TMP/other-venv/notes.txt"
rc=0; SLOP_CHECK_VENV="$TMP/other-venv" "$TOOL" --install >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "S4 --install into an unstamped virtualenv exited $rc, wanted 2"
[ -f "$TMP/other-venv/notes.txt" ] || fail 'S4 --install deleted files in an unstamped virtualenv'
grep -q 'choose another path' "$TMP/err" || fail "S4 refusal did not name the fix: $(cat "$TMP/err")"

# S5 (no venv needed): a scanner that cannot run is "cannot scan" (exit 2), and --check says DRIFT.
mkdir -p "$TMP/noexec-venv/bin"; : > "$TMP/noexec-venv/bin/ast-grep"
python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
  "$RULES/requirements.lock" > "$TMP/noexec-venv/.lock-sha256"
rc=0; SLOP_CHECK_VENV="$TMP/noexec-venv" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "S5 non-executable scanner exited $rc, wanted 2: $(tail -1 "$TMP/err")"
rc=0; SLOP_CHECK_VENV="$TMP/noexec-venv" "$TOOL" --install --check >/dev/null 2>&1 || rc=$?
[ "$rc" = 1 ] || fail "S5 --install --check accepted a venv without a runnable scanner (exit $rc)"

# S6 (no venv needed): a scanner record that is malformed or names a file the tool did not pass
# is "cannot scan" (exit 2), not a traceback and not a finding.
mkdir -p "$TMP/stub-venv/bin"; cp "$TMP/noexec-venv/.lock-sha256" "$TMP/stub-venv/"
printf '#!/bin/sh\ncat "%s"\n' "$TMP/stub-out" > "$TMP/stub-venv/bin/ast-grep"; chmod +x "$TMP/stub-venv/bin/ast-grep"
for record in '{}' '{"file":"/elsewhere.py","range":{"start":{"line":0},"end":{"line":0}},"ruleId":"r","message":"m"}'; do
  printf '%s\n' "$record" > "$TMP/stub-out"
  rc=0; SLOP_CHECK_VENV="$TMP/stub-venv" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
  [ "$rc" = 2 ] || fail "S6 scanner record $record exited $rc, wanted 2: $(tail -1 "$TMP/err")"
  ! grep -q Traceback "$TMP/err" || fail "S6 scanner record $record raised a traceback"
done

# S7-S9 (no venv, no network): --install runs a stub uv placed first on PATH. The stub records its
# arguments and working directory, fakes `venv` (the directory and bin/) and `pip install` (a
# stub ast-grep that reports the pinned version), and fails `pip install` while $TMP/uv-fail exists.
STUB="$TMP/stub-bin"; mkdir "$STUB" "$TMP/caller" "$TMP/inst"
{ printf '#!/bin/sh\nLOG=%s\n' "$TMP"; cat <<'SH'; } > "$STUB/uv"; chmod +x "$STUB/uv"
printf '%s\n' "$*" >> "$LOG/uv-args"; pwd -P >> "$LOG/uv-cwd"
for arg; do case $arg in -*) ;; *) cmd=$arg; break ;; esac; done
case $cmd in
  venv) for arg; do dir=$arg; done; mkdir -p "$dir/bin" ;;
  pip)
    [ ! -e "$LOG/uv-fail" ] || exit 1
    prev=; for arg; do [ "$prev" != --python ] || py=$arg; [ "$prev" != -r ] || req=$arg; prev=$arg; done
    [ -f "$req" ] || exit 1  # like the real builder: the lock must be readable from here
    printf '#!/bin/sh\necho "ast-grep 0.42.1"\n' > "$(dirname "$py")/ast-grep"; chmod +x "$(dirname "$py")/ast-grep" ;;
  *) exit 1 ;;
esac
SH
no_leftovers() { [ -z "$(ls -d "$TMP/inst"/*.new.* 2>/dev/null)" ]; }

# S7: install builds beside the target and swaps it in, from the venv's parent, without uv config.
rc=0; (cd "$TMP/caller" && PATH="$STUB:$PATH" SLOP_CHECK_VENV="$TMP/inst/venv" "$TOOL" --install >/dev/null 2>"$TMP/err") || rc=$?
[ "$rc" = 0 ] || fail "S7 install with a stub uv exited $rc, wanted 0: $(tail -1 "$TMP/err")"
cmp -s "$TMP/inst/venv/.lock-sha256" "$TMP/noexec-venv/.lock-sha256" || fail 'S7 install left no matching stamp'
no_leftovers || fail "S7 install left a build directory: $(ls "$TMP/inst")"
[ "$(grep -c -- '--no-config' "$TMP/uv-args")" = 2 ] || fail "S7 uv ran without --no-config: $(cat "$TMP/uv-args")"
[ "$(sort -u "$TMP/uv-cwd")" = "$(cd "$TMP/inst" && pwd -P)" ] \
  || fail "S7 builders ran from $(sort -u "$TMP/uv-cwd"), wanted the venv's parent"
rc=0; SLOP_CHECK_VENV="$TMP/inst/venv" "$TOOL" --install --check >/dev/null 2>&1 || rc=$?
[ "$rc" = 0 ] || fail "S7 --install --check after install exited $rc, wanted 0"

# S7b: a relative SLOP_CHECK_RULES still installs (the builders run from another directory).
ln -s "$RULES" "$TMP/caller/rules"
rc=0; (cd "$TMP/caller" && PATH="$STUB:$PATH" SLOP_CHECK_RULES=rules SLOP_CHECK_VENV="$TMP/inst/venv" "$TOOL" --install >/dev/null 2>"$TMP/err") || rc=$?
[ "$rc" = 0 ] || fail "S7b install with a relative SLOP_CHECK_RULES exited $rc, wanted 0: $(tail -1 "$TMP/err")"

# S8: a failed build leaves the existing venv exactly as it was, and nothing beside it.
cp -R "$TMP/inst/venv" "$TMP/inst-before"; : > "$TMP/uv-fail"
rc=0; PATH="$STUB:$PATH" SLOP_CHECK_VENV="$TMP/inst/venv" "$TOOL" --install >/dev/null 2>"$TMP/err" || rc=$?
rm "$TMP/uv-fail"
[ "$rc" = 2 ] || fail "S8 failed build exited $rc, wanted 2"
diff -r "$TMP/inst-before" "$TMP/inst/venv" >/dev/null || fail 'S8 a failed build changed the existing venv'
no_leftovers || fail "S8 failed build left a build directory: $(ls "$TMP/inst")"
grep -q 'removing uv from PATH' "$TMP/err" || fail "S8 uv failure did not name the pip builder: $(cat "$TMP/err")"

# S9: --install --check runs the scanner: a stamped venv whose ast-grep reports another version is drift.
cp -R "$TMP/inst-before" "$TMP/oldver-venv"; printf '#!/bin/sh\necho "ast-grep 0.41.0"\n' > "$TMP/oldver-venv/bin/ast-grep"
rc=0; SLOP_CHECK_VENV="$TMP/oldver-venv" "$TOOL" --install --check >/dev/null 2>&1 || rc=$?
[ "$rc" = 1 ] || fail "S9 --install --check accepted a scanner of another version (exit $rc)"

# S10: argument combinations that make no sense are usage errors (exit 2), and nothing is built.
for args in '--check' "--install $TMP/slop.py" '--install --diff HEAD' '--install --all-rules' "--max 0 $TMP/slop.py"; do
  # shellcheck disable=SC2086 # one word per argument; $TMP has no spaces
  rc=0; (cd "$TMP/caller" && PATH="$STUB:$PATH" SLOP_CHECK_VENV="$TMP/args-venv" "$TOOL" $args >/dev/null 2>"$TMP/err") || rc=$?
  [ "$rc" = 2 ] && grep -q 'usage:' "$TMP/err" || fail "S10 '$args' exited $rc without a usage error: $(tail -1 "$TMP/err")"
done
[ ! -e "$TMP/args-venv" ] || fail 'S10 a refused --install still built a venv'

# S11: an unknown default rule id, or SLOP_CHECK_RULES naming no rules directory, is "cannot scan".
mkdir "$TMP/badids"; cp "$RULES"/* "$TMP/badids/"; echo no-such-rule >> "$TMP/badids/default-rules.txt"
: > "$TMP/stub-out"
rc=0; SLOP_CHECK_RULES="$TMP/badids" SLOP_CHECK_VENV="$TMP/stub-venv" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] && grep -q no-such-rule "$TMP/err" || fail "S11 unknown default rule id exited $rc: $(tail -1 "$TMP/err")"
rc=0; SLOP_CHECK_RULES="$TMP/nowhere" SLOP_CHECK_VENV="$TMP/stub-venv" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] && grep -q SLOP_CHECK_RULES "$TMP/err" || fail "S11 missing SLOP_CHECK_RULES directory exited $rc: $(tail -1 "$TMP/err")"

# S12: a crash is "cannot scan" (exit 2), never exit 1 like findings. Rule ids of mixed types
# cannot be sorted, which nothing in the tool expects.
printf '{"file":"%s","range":{"start":{"line":0},"end":{"line":0}},"ruleId":%s,"message":"m"}\n' \
  "$TMP/slop.py" 1 "$TMP/slop.py" '"a"' > "$TMP/stub-out"
rc=0; SLOP_CHECK_VENV="$TMP/stub-venv" "$TOOL" "$TMP/slop.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "S12 a crash exited $rc, wanted 2"
grep -q 'cannot scan: internal error: TypeError' "$TMP/err" || fail "S12 crash not reported as internal error: $(tail -1 "$TMP/err")"

# S13: a reader that closes the pipe early (slop-check | head) gets the normal code and no traceback.
python3 -c 'import json, sys
for n in range(5000):
    print(json.dumps({"file": sys.argv[1], "range": {"start": {"line": n}, "end": {"line": n}}, "ruleId": "r", "message": "m" * 50}))' \
  "$TMP/slop.py" > "$TMP/stub-out"
rc=0; SLOP_CHECK_VENV="$TMP/stub-venv" "$TOOL" --max 5000 "$TMP/slop.py" 2>"$TMP/err" | head -1 >/dev/null || rc=${PIPESTATUS[0]}
[ "$rc" = 1 ] || fail "S13 closed pipe exited $rc, wanted 1: $(tail -1 "$TMP/err")"
[ ! -s "$TMP/err" ] || fail "S13 closed pipe was not quiet: $(tail -1 "$TMP/err")"

# Everything below needs the pinned ast-grep venv: only drift (exit 1) skips; "cannot check" fails.
rc=0; "$TOOL" --install --check >/dev/null 2>"$TMP/err" || rc=$?
case "$rc" in
  0) ;;
  1) echo "slop-check contract: SKIP: pinned ast-grep venv not installed (run: slop-check --install)"
     exit "$HARNESS_SKIP_EXIT" ;;
  *) fail "venv gate: --install --check exited $rc: $(tail -1 "$TMP/err")" ;;
esac

scan_json() { local rc=0; "$TOOL" "$@" --json > "$TMP/out.json" || rc=$?; echo "$rc"; }
field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$TMP/out.json" "$1"; }

# C1: a known slop pattern is found at its line, exit 1.
[ "$(scan_json "$TMP/slop.py")" = 1 ] || fail 'C1 slop file did not exit 1'
[ "$(field '[(f["rule"], f["line"]) for f in d["findings"]]')" = "[('except-pass-silence', 3)]" ] \
  || fail "C1 wrong findings: $(cat "$TMP/out.json")"

# C2: clean code exits 0 and says clean.
printf 'def double(x: int) -> int:\n    return 2 * x\n' > "$TMP/clean.py"
out="$("$TOOL" "$TMP/clean.py")" || fail 'C2 clean file did not exit 0'
case "$out" in 'slop-check: clean'*) ;; *) fail "C2 wrong text: $out" ;; esac

# C3: an extensionless python script is scanned and reported under its own name.
{ printf '#!/usr/bin/env python3\n'; cat "$TMP/slop.py"; } > "$TMP/tool"
[ "$(scan_json "$TMP/tool")" = 1 ] || fail 'C3 extensionless script not scanned'
[ "$(field 'd["findings"][0]["file"]')" = "$TMP/tool" ] || fail "C3 wrong path: $(cat "$TMP/out.json")"

# C4: diff mode reports only changed lines and untracked files, not legacy slop.
REPO="$TMP/repo"; mkdir "$REPO"; cd "$REPO"
git init -q; git config user.email t@t; git config user.name t
cp "$TMP/slop.py" legacy.py; git add legacy.py; git commit -qm legacy
printf 'try:\n    y = 2\nexcept KeyError:\n    pass\n' >> legacy.py
cp "$TMP/slop.py" fresh.py
[ "$(scan_json)" = 1 ] || fail 'C4 diff mode did not exit 1'
got="$(field 'sorted((f["file"].rsplit("/",1)[1], f["line"]) for f in d["findings"])')"
[ "$got" = "[('fresh.py', 3), ('legacy.py', 7)]" ] || fail "C4 wrong scope: $got"

# C5: no changed Python is a legitimate clean (exit 0), not an error.
git add -A; git commit -qm more; printf 'notes\n' > README
out="$("$TOOL")" || fail 'C5 no-python diff did not exit 0'
case "$out" in *'no Python files'*) ;; *) fail "C5 wrong text: $out" ;; esac
cd "$TMP"

# C8: changed files whose names git would quote or pad (space, non-ASCII) are still scanned.
cd "$REPO"
cp "$TMP/clean.py" 'two words.py'; cp "$TMP/clean.py" 'café.py'; git add -A; git commit -qm names
cat "$TMP/slop.py" >> 'two words.py'; cat "$TMP/slop.py" >> 'café.py'
[ "$(scan_json)" = 1 ] || fail 'C8 odd file names did not exit 1'
got="$(field 'sorted(f["file"].rsplit("/",1)[1] for f in d["findings"])')"
[ "$got" = "['café.py', 'two words.py']" ] || fail "C8 odd file names skipped: $got"

# C9: an option-like --diff value is refused (exit 2) and never reaches git as an option.
rc=0; "$TOOL" --diff="--output=$TMP/injected" >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] || fail "C9 option-like --diff exited $rc, wanted 2"
[ ! -e "$TMP/injected" ] || fail 'C9 option-like --diff wrote a file through git'

# C10: a repo that marks Python as binary (.gitattributes -diff) cannot hide changed lines.
git add -A; git commit -qm odd-names
printf '*.py -diff\n' > .gitattributes; git add .gitattributes; git commit -qm attrs
cat "$TMP/slop.py" >> legacy.py
[ "$(scan_json)" = 1 ] || fail 'C10 -diff attribute hid a changed file (false clean)'

# C11: a repo-configured textconv helper is never executed by a scan.
printf '#!/bin/sh\ntouch "%s"\ncat "$1"\n' "$TMP/textconv-ran" > "$TMP/conv.sh"; chmod +x "$TMP/conv.sh"
printf '*.py diff=evil\n' > .gitattributes; git config diff.evil.textconv "$TMP/conv.sh"
[ "$(scan_json)" = 1 ] || fail 'C11 textconv attribute hid a changed file'
[ ! -e "$TMP/textconv-ran" ] || fail 'C11 scan executed a repo-configured textconv helper'

# Each case below starts from a committed tree, so only its own change is in scope.
files_found() { field 'sorted({f["file"].rsplit("/",1)[1] for f in d["findings"]})'; }

# C12: a file name that looks like pathspec magic cannot exclude itself from the scan.
cp "$TMP/clean.py" ':(exclude)*.py'; git add -A; git commit -qm magic
cat "$TMP/slop.py" >> ':(exclude)*.py'
[ "$(scan_json)" = 1 ] || fail 'C12 pathspec-magic file name hid a changed file (false clean)'
[ "$(files_found)" = "[':(exclude)*.py']" ] || fail "C12 wrong files: $(files_found)"

# C13: a repo-configured clean or process filter is never executed and cannot hide a changed file.
git add -A; git commit -qm c12
printf '#!/bin/sh\ntouch "%s"\ncat\n' "$TMP/filter-ran" > "$TMP/clean.sh"
printf '#!/bin/sh\ntouch "%s"\n' "$TMP/filter-ran" > "$TMP/process.sh"; chmod +x "$TMP/clean.sh" "$TMP/process.sh"
printf '*.py filter=evil\n' > .gitattributes
git config filter.evil.clean "$TMP/clean.sh"; git config filter.evil.process "$TMP/process.sh"
cat "$TMP/slop.py" >> legacy.py
[ "$(scan_json)" = 1 ] || fail 'C13 filter attribute hid a changed file'
[ "$(files_found)" = "['legacy.py']" ] || fail "C13 wrong files: $(files_found)"
[ ! -e "$TMP/filter-ran" ] || fail 'C13 scan executed a repo-configured filter'
git config --unset filter.evil.clean; git config --unset filter.evil.process; git checkout -q -- .gitattributes

# C14: a new file that is staged but not committed is reported on every line.
git add -A; git commit -qm c13
{ cat "$TMP/slop.py"; cat "$TMP/slop.py"; } > staged.py; git add staged.py
[ "$(scan_json)" = 1 ] || fail 'C14 staged new file not reported'
[ "$(field '[(f["file"].rsplit("/",1)[1], f["line"]) for f in d["findings"]]')" = "[('staged.py', 3), ('staged.py', 7)]" ] \
  || fail "C14 staged file not reported on all lines: $(cat "$TMP/out.json")"

# C15: rewriting a file with CRLF line endings adds no lines; only the appended block is reported.
git commit -qm c14; cp "$TMP/slop.py" crlf.py; git add crlf.py; git commit -qm crlf
{ cat "$TMP/slop.py"; cat "$TMP/slop.py"; } | awk '{ printf "%s\r\n", $0 }' > crlf.py
[ "$(scan_json)" = 1 ] || fail 'C15 appended block in a CRLF rewrite not reported'
[ "$(field '[(f["file"].rsplit("/",1)[1], f["line"]) for f in d["findings"]]')" = "[('crlf.py', 7)]" ] \
  || fail "C15 CRLF rewrite reported old lines: $(cat "$TMP/out.json")"

# C16: a symlink is never followed (it could point outside the repository), and a deleted
# tracked file is skipped, not an error.
git commit -qam c15; ln -s "$TMP/slop.py" outside.py; rm staged.py
[ "$(scan_json)" = 0 ] || fail "C16 symlink followed or deleted file broke the scan: $(cat "$TMP/out.json")"
rm outside.py; git checkout -q -- staged.py
# C17: an sgconfig.yml in the scanned repository is never loaded (it can name a library to load).
printf 'ruleDirs: []\ncustomLanguages:\n  evil:\n    libraryPath: %s/evil.so\n    extensions: [evil]\n    expandoChar: _\n' "$TMP" > sgconfig.yml
cp "$TMP/slop.py" config-probe.py
[ "$(scan_json)" = 1 ] || fail "C17 repository sgconfig.yml reached the scanner: $(cat "$TMP/out.json")"
[ "$(scan_json "$REPO/config-probe.py")" = 1 ] || fail 'C17 repository sgconfig.yml reached a whole-file scan'
rm -f sgconfig.yml config-probe.py

# C21: diff mode counts lines by "\n" like the scanner, so a lone carriage return in an earlier
# line does not shift added lines. The added except is the first new line: off by one, it is missed.
printf 'x = 1\r# note\ntry:\n    x = 1\n' > cr.py; git add cr.py; git commit -qm cr
printf 'except ValueError: pass\n' >> cr.py
[ "$(scan_json)" = 1 ] || fail "C21 lone carriage return shifted added lines (false clean): $(cat "$TMP/out.json")"
[ "$(field '[(f["file"].rsplit("/",1)[1], f["line"]) for f in d["findings"]]')" = "[('cr.py', 4)]" ] \
  || fail "C21 wrong line: $(cat "$TMP/out.json")"

# C31: submodule entries in the compared tree are noted on stderr; the exit code does not change.
git update-index --add --cacheinfo "160000,$(git rev-parse HEAD),sub"; git commit -qm sub
rc=0; "$TOOL" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 1 ] || fail "C31 a submodule entry changed the exit code to $rc"
grep -q 'note: 1 submodule path(s) not scanned' "$TMP/err" || fail "C31 skipped submodule not noted: $(cat "$TMP/err")"
cd "$TMP"

# C6: --all-rules really scans with the rules outside the default set.
printf 'def f(x):\n    if isinstance(x, bool):\n        return 1\n    return 2\n' > "$TMP/nondefault.py"
[ "$(scan_json "$TMP/nondefault.py")" = 0 ] || fail 'C6 default set flagged a non-default pattern'
[ "$(scan_json --all-rules "$TMP/nondefault.py")" = 1 ] || fail 'C6 --all-rules missed a non-default rule'
[ "$(field '(d["rules"], d["findings"][0]["rule"])')" = "(197, 'isinstance-bool-exclusion')" ] \
  || fail "C6 wrong --all-rules result: $(cat "$TMP/out.json")"

# C7: a venv built from another lock is refused (exit 2).
DRIFT="$TMP/drift-venv"; cp -R "$("$TOOL" --install --check >/dev/null; python3 -c 'import os,pathlib;print(os.environ.get("SLOP_CHECK_VENV") or pathlib.Path.home()/".local/share/orchestratormaxxing/slop-check/venv")')" "$DRIFT"
echo stale > "$DRIFT/.lock-sha256"
rc=0; SLOP_CHECK_VENV="$DRIFT" "$TOOL" "$TMP/slop.py" >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] || fail "C7 drifted venv exited $rc, wanted 2"

# C18: a directory scan includes extensionless Python scripts.
mkdir "$TMP/scripts"; cp "$TMP/tool" "$TMP/scripts/tool"
[ "$(scan_json "$TMP/scripts")" = 1 ] || fail "C18 extensionless script in a directory not scanned: $(cat "$TMP/out.json")"
[ "$(files_found)" = "['tool']" ] || fail "C18 wrong files: $(files_found)"

# C19: a .venv above the scan root hides nothing; a .venv below it is pruned.
mkdir -p "$TMP/.venv/proj/.venv"; cp "$TMP/slop.py" "$TMP/.venv/proj/app.py"; cp "$TMP/slop.py" "$TMP/.venv/proj/.venv/dep.py"
[ "$(scan_json "$TMP/.venv/proj")" = 1 ] || fail "C19 .venv above the scan root hid files: $(cat "$TMP/out.json")"
[ "$(files_found)" = "['app.py']" ] || fail "C19 .venv below the scan root not pruned: $(files_found)"

# C20: symlinks are never followed in whole-path mode; an explicit one (or a loop) is exit 2.
mkdir "$TMP/linked"; cp "$TMP/clean.py" "$TMP/linked/clean.py"; ln -s "$TMP/slop.py" "$TMP/linked/link.py"
[ "$(scan_json "$TMP/linked")" = 0 ] || fail "C20 symlink inside a directory was followed: $(cat "$TMP/out.json")"
ln -s loop.py "$TMP/loop.py"
for link in "$TMP/linked/link.py" "$TMP/loop.py"; do
  rc=0; "$TOOL" "$link" >/dev/null 2>"$TMP/err" || rc=$?
  [ "$rc" = 2 ] || fail "C20 explicit symlink $link exited $rc, wanted 2"
  grep -q "$link" "$TMP/err" || fail "C20 refusal did not name $link: $(cat "$TMP/err")"
  ! grep -q Traceback "$TMP/err" || fail "C20 explicit symlink $link raised a traceback"
done

# C22: a directory that cannot be read is "cannot scan" (exit 2), not "no Python files".
SKIPPED=''
if [ "$(id -u)" = 0 ]; then
  SKIPPED='; C22 skipped: root reads any directory'
else
  mkdir -p "$TMP/locked/inner"; cp "$TMP/slop.py" "$TMP/locked/inner/a.py"; chmod 000 "$TMP/locked/inner"
  rc=0; "$TOOL" "$TMP/locked" >/dev/null 2>&1 || rc=$?
  chmod 755 "$TMP/locked/inner"
  [ "$rc" = 2 ] || fail "C22 unreadable directory exited $rc, wanted 2"
fi

# C23: a source file that is not UTF-8 is "cannot scan" (the scanner would skip it silently).
printf '# caf\351\n' > "$TMP/latin1.py"; cat "$TMP/slop.py" >> "$TMP/latin1.py"
rc=0; "$TOOL" "$TMP/latin1.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "C23 non-UTF-8 source exited $rc, wanted 2"
grep -q 'latin1.py' "$TMP/err" || fail "C23 refusal did not name the file: $(cat "$TMP/err")"

# C24: a Unicode line separator inside matched text does not break the scanner record.
printf 'try:\n    x = 1\nexcept ValueError:\n    # sep\342\200\250here\n    pass\n' > "$TMP/u2028.py"
[ "$(scan_json "$TMP/u2028.py")" = 1 ] || fail "C24 U+2028 in matched text exited $(scan_json "$TMP/u2028.py"), wanted 1"

# C25: a relative SLOP_CHECK_VENV works (the scanner runs from another directory).
REAL_VENV="$(python3 -c 'import os,pathlib;print(os.environ.get("SLOP_CHECK_VENV") or pathlib.Path.home()/".local/share/orchestratormaxxing/slop-check/venv")')"
ln -s "$REAL_VENV" "$TMP/rel-venv"; mkdir "$TMP/empty-home"
# An empty HOME: were the relative path ignored, the default venv under HOME would not exist either.
rc=0; (cd "$TMP" && HOME="$TMP/empty-home" SLOP_CHECK_VENV=rel-venv "$TOOL" "$TMP/slop.py" >/dev/null 2>&1) || rc=$?
[ "$rc" = 1 ] || fail "C25 relative SLOP_CHECK_VENV exited $rc, wanted 1"

# C26: a file past the scanner's size limit is "cannot scan" (the scanner would skip it silently).
python3 -c 'import sys; open(sys.argv[1], "w").write("# filler 1234567890\n" * 200001 + open(sys.argv[2]).read())' "$TMP/huge.py" "$TMP/slop.py"
rc=0; "$TOOL" "$TMP/huge.py" >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] || fail "C26 oversized file exited $rc, wanted 2"
grep -q 'huge.py' "$TMP/err" || fail "C26 refusal did not name the file: $(cat "$TMP/err")"
python3 -c 'import sys; open(sys.argv[1], "w").write("# filler 1234567890\n" * 150000 + open(sys.argv[2]).read())' "$TMP/big.py" "$TMP/slop.py"
[ "$(scan_json "$TMP/big.py")" = 1 ] || fail 'C26 a large file under the limit was not scanned'
rm -f "$TMP/huge.py" "$TMP/big.py"

# C27: any file whose first line is a Python shebang is Python, whatever its suffix; a .pyi
# without one is not.
mkdir "$TMP/shebang"; cp "$TMP/tool" "$TMP/shebang/tool.v2"; cp "$TMP/slop.py" "$TMP/shebang/stub.pyi"
[ "$(scan_json "$TMP/shebang")" = 1 ] || fail "C27 tool.v2 with a Python shebang not scanned: $(cat "$TMP/out.json")"
[ "$(files_found)" = "['tool.v2']" ] || fail "C27 wrong files: $(files_found)"

# C28: in a repository with no commits, default diff mode scans every Python file as new;
# an explicit --diff that does not resolve is "not a revision".
mkdir "$TMP/unborn"; cd "$TMP/unborn"; git init -q; cp "$TMP/slop.py" new.py
[ "$(scan_json)" = 1 ] || fail "C28 repository with no commits not scanned: $(cat "$TMP/out.json")"
[ "$(field '[(f["file"].rsplit("/",1)[1], f["line"]) for f in d["findings"]]')" = "[('new.py', 3)]" ] \
  || fail "C28 wrong findings: $(cat "$TMP/out.json")"
rc=0; "$TOOL" --diff nosuch >/dev/null 2>"$TMP/err" || rc=$?
[ "$rc" = 2 ] && grep -q 'not a revision: nosuch' "$TMP/err" || fail "C28 --diff nosuch exited $rc: $(cat "$TMP/err")"
cd "$TMP"

# C29: control characters in a printed file name are shown escaped, never sent to the terminal raw.
mkdir "$TMP/esc"; cp "$TMP/slop.py" "$TMP/esc/a"$'\033'"[31mb.py"
out="$("$TOOL" "$TMP/esc" || true)"
case "$out" in *$'\033'*) fail 'C29 printed a raw escape character' ;; esac
case "$out" in *'esc/a\x1b[31mb.py:3 '*) ;; *) fail "C29 name not shown escaped: $out" ;; esac

# C30: a directory holding pyvenv.cfg is a virtualenv under any name and is pruned below the scan root.
mkdir -p "$TMP/venvs/proj/env/lib"; : > "$TMP/venvs/proj/env/pyvenv.cfg"
cp "$TMP/slop.py" "$TMP/venvs/proj/app.py"; cp "$TMP/slop.py" "$TMP/venvs/proj/env/lib/dep.py"
[ "$(scan_json "$TMP/venvs/proj")" = 1 ] && [ "$(files_found)" = "['app.py']" ] \
  || fail "C30 virtualenv below the scan root not pruned: $(files_found)"
[ "$(scan_json "$TMP/venvs/proj/env")" = 1 ] && [ "$(files_found)" = "['dep.py']" ] \
  || fail "C30 a scan root holding pyvenv.cfg was pruned: $(files_found)"

echo "slop-check contract: OK (S1-S13, C1-C31$SKIPPED)"
