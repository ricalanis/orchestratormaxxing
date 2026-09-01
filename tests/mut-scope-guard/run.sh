#!/usr/bin/env bash
# Contract: `bin/mut --scope changed` must distinguish "nothing changed" from
# "I cannot see this file at all" (lq-166f10d2, signal-vs-artifact doctrine).
#
# `git diff HEAD -- <untracked>` is EMPTY, so the pre-fix tool read an
# unmeasurable file as a clean scope and returned the PASS exit code having run
# zero mutants — the anti-gaming gate silently green on every brand-new tool.
#
#   C1  untracked source + --scope changed -> REFUSED (exit 2), zero mutants
#   C2  tracked + unchanged  + --scope changed -> exit 0 (guard does not over-fire)
#   C3  tracked + changed    + --scope changed -> mutants really run (happy path)
#   C4  untracked source + --scope all -> runs (the guard is scoped to `changed`)
#   C5  a refused run leaves no lock/backup and the source byte-identical
#   C6  --json refusal exits 2 and never prints a passing verdict on stdout
#   C7  a repo with NO HEAD (staged file, zero commits) refuses with 2, not 1 --
#       git tracks the file so C1's index check passes, but `git diff HEAD` then
#       fails; exiting 1 there would read as "score below threshold" to a Bash
#       caller. Found by two cross-family critics, 2026-08-27.
#   C8  git ABSENT from PATH refuses with 2 -- the observer being missing is the
#       purest "cannot measure" case, and it must not surface as a score verdict
#   C9  a refusal carries an actionable message, not raw git plumbing noise
#       (C8/C9 added to kill Tier-1b survivors: the except-branch and the
#        capture_output/text kwargs were unexercised, mut score 0.79 -> 1.00)
set -uo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
mut="$repo/bin/mut"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

cd "$scratch" || exit 1
git init -q . 2>/dev/null
git config user.email harness@local
git config user.name harness
git config commit.gpgsign false

cat > tracked.py <<'PY'
def f(x):
    return x > 2
PY
git add tracked.py
git commit -qm init

cat > untracked.py <<'PY'
def g(x):
    return x > 2
PY

fail() { echo "$1"; exit 1; }

# ---------------------------------------------------------------- C1
out="$("$mut" --src untracked.py --test true --scope changed --threshold 0 2>&1)"
rc=$?
[ "$rc" = "2" ] || fail "C1: untracked source with --scope changed exited $rc, expected 2 (refused). Output: $out"
case "$out" in
    *"mutants ·"*) fail "C1: refused run still executed mutants: $out" ;;
esac
case "$out" in
    *"git add"*) : ;;
    *) fail "C1: refusal does not name the remedy (git add): $out" ;;
esac

# ---------------------------------------------------------------- C5
[ ! -e untracked.py.mut-lock ] || fail "C5: refused run left a lockfile"
[ ! -e untracked.py.mut-orig ] || fail "C5: refused run left a backup"
grep -q 'return x > 2' untracked.py || fail "C5: refused run modified the source"

# ---------------------------------------------------------------- C6
jout="$("$mut" --src untracked.py --test true --scope changed --threshold 0 --json 2>/dev/null)"
jrc=$?
[ "$jrc" = "2" ] || fail "C6: --json refusal exited $jrc, expected 2"
case "$jout" in
    *'"passed": true'*) fail "C6: --json refusal printed a passing verdict: $jout" ;;
esac

# ---------------------------------------------------------------- C2
out2="$("$mut" --src tracked.py --test true --scope changed --threshold 0 2>&1)"
rc2=$?
[ "$rc2" = "0" ] || fail "C2: tracked+unchanged source exited $rc2, expected 0. Output: $out2"
case "$out2" in
    *"no changed lines"*) : ;;
    *) fail "C2: tracked+unchanged run lost its explanation: $out2" ;;
esac

# ---------------------------------------------------------------- C3
cat > tracked.py <<'PY'
def f(x):
    return x > 3
PY
out3="$("$mut" --src tracked.py --test true --scope changed --threshold 0 2>&1)"
rc3=$?
[ "$rc3" = "0" ] || fail "C3: tracked+changed source exited $rc3, expected 0. Output: $out3"
case "$out3" in
    *"mutants ·"*) : ;;
    *) fail "C3: tracked+changed source ran no mutants — guard over-fired: $out3" ;;
esac

# ---------------------------------------------------------------- C4
out4="$("$mut" --src untracked.py --test true --scope all --threshold 0 2>&1)"
rc4=$?
[ "$rc4" = "0" ] || fail "C4: untracked source with --scope all exited $rc4, expected 0. Output: $out4"
case "$out4" in
    *"mutants ·"*) : ;;
    *) fail "C4: --scope all on an untracked source ran no mutants: $out4" ;;
esac

# ---------------------------------------------------------------- C7
nohead="$(mktemp -d)"
cd "$nohead" || exit 1
git init -q . 2>/dev/null
git config user.email harness@local
git config user.name harness
cat > staged.py <<'SRC'
def h(x):
    return x > 2
SRC
git add staged.py          # tracked in the INDEX, but there is no HEAD to diff
out7="$("$mut" --src staged.py --test true --scope changed --threshold 0 2>&1)"
rc7=$?
cd "$scratch" || exit 1
rm -rf "$nohead"
[ "$rc7" = "2" ] || fail "C7: no-HEAD repo exited $rc7, expected 2 (unmeasurable, not a failed score). Output: $out7"
case "$out7" in
    *"mutants "*) fail "C7: refused run still executed mutants: $out7" ;;
esac

# ---------------------------------------------------------------- C8
py3="$(command -v python3)"
empty_path="$(mktemp -d)"
out8="$(PATH="$empty_path" "$py3" "$mut" --src tracked.py --test true --scope changed 2>&1)"
rc8=$?
rmdir "$empty_path" 2>/dev/null || true
[ "$rc8" = "2" ] || fail "C8: git absent from PATH exited $rc8, expected 2 (unmeasurable). Output: $out8"
case "$out8" in
    *"needs git"*) : ;;
    *) fail "C8: missing-git refusal does not say git is missing: $out8" ;;
esac

# ---------------------------------------------------------------- C9
out9="$("$mut" --src untracked.py --test true --scope changed --threshold 0 2>&1)"
case "$out9" in
    *"did not match any file"*|*"Did you forget to"*)
        fail "C9: refusal leaks raw git plumbing noise instead of an actionable message: $out9" ;;
esac

echo "mut-scope-guard: C1-C9 pass"
