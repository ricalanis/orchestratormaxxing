#!/usr/bin/env bash
# Contract: bin/mut can never leave a mutated source behind, and concurrent
# runs against the same file are refused (lq-cd123ac1).
#   C1  normal run restores the source byte-for-byte and leaves no sidecars
#   C2  an interrupted run's leftover backup is auto-restored by the next run
#   C3  a live lock refuses a second run without touching the source
#   C4  a stale lock (dead pid) does not block, and is cleaned up
#   C5  an executable source keeps its mode across a run
#   C6  a mutant that escapes its sandbox and writes into the repo is REPORTED
#   C7  a run whose mutants write nothing reports no debris (no false positive)
#   C8  outside a git worktree the guard says so — unknown, never "clean"
#   C10 the on-disk mutant declares itself: shebang kept + live-pid banner (lq-bc7ccd55)
#   C11 contract process groups cannot survive timeout, normal exit, or interruption
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

src="$scratch/target.py"
cat > "$src" <<'PY'
def f(x):
    return x > 2
PY
orig_hash="$(shasum "$src" | cut -d' ' -f1)"
test_cmd="python3 -c \"import sys; sys.path.insert(0,'$scratch'); import target; assert target.f(3) and not target.f(1)\""

# C1 — normal run: source restored, no lock/backup left behind
"$repo/bin/mut" --src "$src" --test "$test_cmd" --threshold 0 >/dev/null 2>&1
[ "$(shasum "$src" | cut -d' ' -f1)" = "$orig_hash" ] || { echo "C1: source not restored after normal run"; exit 1; }
[ ! -e "$src.mut-lock" ] || { echo "C1: lockfile left behind"; exit 1; }
[ ! -e "$src.mut-orig" ] || { echo "C1: backup left behind"; exit 1; }

# C2 — interrupted run simulation: backup holds the original, on-disk file is a
# mutant, no live lock. The next run must restore the original first.
cp "$src" "$src.mut-orig"
cat > "$src" <<'PY'
def f(x):
    return x >= 2
PY
"$repo/bin/mut" --src "$src" --test "$test_cmd" --threshold 0 >/dev/null 2>&1
[ "$(shasum "$src" | cut -d' ' -f1)" = "$orig_hash" ] || { echo "C2: interrupted-run backup not recovered"; exit 1; }
[ ! -e "$src.mut-orig" ] || { echo "C2: backup not cleaned up"; exit 1; }

# C3 — live lock: a second run is refused and the source is untouched
sleep 30 & holder=$!
disown "$holder"
echo "$holder" > "$src.mut-lock"
if "$repo/bin/mut" --src "$src" --test "$test_cmd" --threshold 0 >/dev/null 2>&1; then
    kill "$holder" 2>/dev/null || true
    echo "C3: concurrent run was not refused"; exit 1
fi
[ "$(shasum "$src" | cut -d' ' -f1)" = "$orig_hash" ] || { kill "$holder" 2>/dev/null || true; echo "C3: refused run still touched the source"; exit 1; }
kill "$holder" 2>/dev/null || true
rm -f "$src.mut-lock"

# C4 — stale lock (dead pid): run proceeds and cleans the lock
sleep 0.01 & stale=$!
wait "$stale" 2>/dev/null || true
echo "$stale" > "$src.mut-lock"
"$repo/bin/mut" --src "$src" --test "$test_cmd" --threshold 0 >/dev/null 2>&1
[ "$(shasum "$src" | cut -d' ' -f1)" = "$orig_hash" ] || { echo "C4: source not restored after stale-lock run"; exit 1; }
[ ! -e "$src.mut-lock" ] || { echo "C4: stale lock not cleaned up"; exit 1; }

# C5 — an executable source keeps its MODE across a run. Every restore path is an
# os.replace of the backup inode over the source, so a backup written with open()'s
# default 0o644 silently strips the execute bit from bin/ tools (lq-8070472f:
# ./bin/harness-verify went permission-denied immediately after a mut run).
chmod 755 "$src"
"$repo/bin/mut" --src "$src" --test "$test_cmd" --threshold 0 >/dev/null 2>&1
mode="$(stat -c '%a' "$src" 2>/dev/null || stat -f '%Lp' "$src")"
[ "$mode" = "755" ] || { echo "C5: source mode not preserved across run (got $mode, want 755)"; exit 1; }
[ "$(shasum "$src" | cut -d' ' -f1)" = "$orig_hash" ] || { echo "C5: source content changed"; exit 1; }

# ── debris guard (lq-46634844) ───────────────────────────────────────────────
# Crosses the real boundary: a real git worktree, a real tool whose output path is
# env-resolved, and a real mutation of that resolution. The incident shape exactly —
# a mutated bin/token-ledger fell back to the REAL repo and left 3 snapshots in
# knowledge/ while its contract stayed green.
gitrepo="$scratch/repo"
mkdir -p "$gitrepo"
git -C "$gitrepo" init -q
sandbox="$scratch/sandbox"
mkdir -p "$sandbox"

cat > "$gitrepo/tool.py" <<'PY'
import os
import sys


def outdir():
    env = os.environ.get("TOOL_OUT")
    if env:
        return env
    return os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(outdir(), "artifact.txt"), "w") as fh:
        fh.write("snapshot\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY

# The escape is produced by a mutation the operator table generates deterministically:
# `return env` is an ast.Return, and Return is in DELETABLE, so `return env → pass`
# always exists. If that stops being generated this case goes red on purpose.
escape_cmd="TOOL_OUT='$sandbox' python3 '$gitrepo/tool.py' && test -f '$sandbox/artifact.txt'"
out="$("$repo/bin/mut" --src "$gitrepo/tool.py" --test "$escape_cmd" --threshold 0 2>/dev/null)"

# C6 — the escaped write is surfaced, attributed, and NOT silently swallowed
grep -q "^DEBRIS" <<<"$out" || { echo "C6: sandbox escape produced no DEBRIS report"; echo "$out"; exit 1; }
# Anchored on the DEBRIS line itself: mut echoes the test command, which contains the
# word artifact.txt, so a bare substring match would pass without any detection at all.
grep -qE '^- artifact\.txt ' <<<"$out" || { echo "C6: DEBRIS report does not name the escaped path"; echo "$out"; exit 1; }
grep -q "while mutating" <<<"$out" || { echo "C6: debris not tied to the mutant that was live"; echo "$out"; exit 1; }
# and it reports rather than deletes — the tree is shared, an unwarranted rm costs more
[ -f "$gitrepo/artifact.txt" ] || { echo "C6: mut deleted the debris instead of reporting it"; exit 1; }
rm -f "$gitrepo/artifact.txt"

# C7 — no false positive: mutants that write nothing must not manufacture debris
cat > "$gitrepo/pure.py" <<'PY'
def f(x):
    return x > 2
PY
pure_cmd="python3 -c \"import sys; sys.path.insert(0,'$gitrepo'); import pure; assert pure.f(3) and not pure.f(1)\""
out="$("$repo/bin/mut" --src "$gitrepo/pure.py" --test "$pure_cmd" --threshold 0 2>/dev/null)"
grep -q "DEBRIS" <<<"$out" && { echo "C7: clean run reported debris (false positive)"; echo "$out"; exit 1; }

# C9 — the escaped write lands on a file the tree was ALREADY dirty on. A path-only
# snapshot misses this entirely, and a shared tree is dirty most of the time, so the
# hole would sit exactly where the repo normally lives. Detection must key off the
# file's stamp, not its mere existence.
git -C "$gitrepo" add tool.py && git -C "$gitrepo" -c user.email=t@t -c user.name=t commit -qm base
printf 'stale\n' > "$gitrepo/artifact.txt"          # untracked and present BEFORE the run
printf 'dirty\n' >> "$gitrepo/tool_notes.md"
git -C "$gitrepo" add tool_notes.md && git -C "$gitrepo" -c user.email=t@t -c user.name=t commit -qm notes
printf 'locally-modified\n' >> "$gitrepo/tool_notes.md"   # tracked AND already dirty
overwrite_cmd="TOOL_OUT='$sandbox' python3 '$gitrepo/tool.py' && test -f '$sandbox/artifact.txt'"
out="$("$repo/bin/mut" --src "$gitrepo/tool.py" --test "$overwrite_cmd" --threshold 0 2>/dev/null)"
grep -qE '^- artifact\.txt ' <<<"$out" || { echo "C9: overwrite of an already-present path went undetected"; echo "$out"; exit 1; }
rm -f "$gitrepo/artifact.txt"

# C8 — outside a git worktree the guard must SAY it did not check. An unreadable
# observer degrades to "unknown", never to "clean" (signal-vs-artifact doctrine).
out="$("$repo/bin/mut" --src "$src" --test "$test_cmd" --threshold 0 2>/dev/null)"
grep -q "guard off" <<<"$out" || { echo "C8: non-git run did not declare the guard off"; echo "$out"; exit 1; }

# C10 — the on-disk mutant DECLARES ITSELF (lq-bc7ccd55). The lock/backup sidecars
# are gitignored, so the one evidence stream a concurrent session reliably reads is
# the mutated file/diff itself: on 2026-08-19 a successor read a live mutant's diff
# as debris ("shebang stripped"), `git checkout`ed it mid-run, and falsified that
# mutant's verdict. Every mutant written to disk must keep the original shebang and
# carry a banner naming the live pid and the .mut-orig backup. The capture crosses
# the real boundary: the contract command copies the src WHILE the mutant is live.
decl="$scratch/decl.py"
cat > "$decl" <<'PY'
#!/usr/bin/env python3
def f(x):
    return x > 2
PY
cap="$scratch/captured-mutant.py"
"$repo/bin/mut" --src "$decl" --test "cp '$decl' '$cap'; exit 1" --threshold 0 >/dev/null 2>&1
[ -f "$cap" ] || { echo "C10: contract never ran (no mutant captured)"; exit 1; }
# Exact-line equality, not a '^#!' prefix: a shebang whose trailing newline is lost
# merges with the banner into one line that still greps as '#!' but breaks kernel
# exec (env gets 'python3# !! …' as its argument) — a real mut survivor until pinned.
[ "$(head -n1 "$cap")" = "#!/usr/bin/env python3" ] || { echo "C10: mutant first line is not the original shebang"; exit 1; }
grep -q "UNDER MUTATION by bin/mut" "$cap" || { echo "C10: on-disk mutant does not declare itself"; exit 1; }
grep -qE "pid [0-9]+" "$cap" || { echo "C10: mutant banner names no live pid"; exit 1; }
grep -q "mut-orig" "$cap" || { echo "C10: banner does not point at the pristine backup"; exit 1; }
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$cap" || { echo "C10: banner broke the mutant's syntax"; exit 1; }
grep -q "UNDER MUTATION" "$decl" && { echo "C10: banner leaked into the restored source"; exit 1; }

# C11 — the process-containment sibling crosses the real OS boundary. Keep it
# called from this already-registered contract so the shared-tree verifier does
# not need an overlapping edit merely to learn one more contract path.
MUT_UNDER_TEST="$repo/bin/mut" bash "$repo/tests/mut-process-cleanup/run.sh" >/dev/null \
  || { echo "C11: mut contract process containment failed"; exit 1; }

echo "mut-safety: all contracts green"
