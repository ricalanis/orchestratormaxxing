#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Contract: the daily cross-machine claim in deploy/loop-cron.sh is a REAL
# optimistic lock — including on the dirty-tree path, where the round runs on a
# detached HEAD inside a worktree.
#
# REAL BOUNDARY (Tier 1c): this runs the actual deploy/loop-cron.sh against real
# bare git remotes and real `git push`. Only the agent-side commands the wrapper
# shells out to (loop-tick / harness-agent-run / harness-verify / loop-queue) and
# `hostname` are faked, on the wrapper's own hardcoded PATH ($HOME/.local/bin).
# Every claim/push/rebase/worktree operation asserted below is genuine git.
#
# Proven red against the pre-fix wrapper (lq-10d88328): with the round on a
# detached worktree HEAD, `git push origin main` pushed the *branch* main — which
# the claim commit was never on — so the push trivially succeeded, the wrapper
# logged "won the day", and the claim never reached origin. C1 and C2 both fail.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WRAPPER_SRC="$ROOT/deploy/loop-cron.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TODAY="$(date -u +%F)"
ROUNDS="$TMP/rounds.log"
: > "$ROUNDS"

pass=0; fail=0
ok()   { pass=$((pass+1)); printf '  ok  %s  %s\n' "$1" "$2"; }
bad()  { fail=$((fail+1)); printf '  FAIL %s  %s\n' "$1" "$2" >&2; }

[ -f "$WRAPPER_SRC" ] || { printf 'loop-claim contract: missing %s\n' "$WRAPPER_SRC" >&2; exit 1; }

git_q() { git "$@" >/dev/null 2>&1; }

# A bare origin seeded with one commit, a stale claim marker, and a tracked file
# the "human" will leave dirty.
mk_origin() {
  local origin="$TMP/$1.git" seed="$TMP/$1-seed"
  git init --bare -b main -q "$origin"
  # A real remote that can start refusing pushes mid-run (offline / auth lost / lost
  # race), so the "push did not land" branch is exercised for real, not simulated.
  cat > "$origin/hooks/pre-receive" <<SH
#!/bin/sh
[ -f "$TMP/deny-$1" ] && { echo "test: push denied"; exit 1; }
exit 0
SH
  chmod +x "$origin/hooks/pre-receive"
  git init -b main -q "$seed"
  git -C "$seed" config user.email loop@test
  git -C "$seed" config user.name  loop
  git -C "$seed" config commit.gpgsign false
  mkdir -p "$seed/knowledge"
  printf '2020-01-01 seedhost 0\n' > "$seed/knowledge/loop-claim"
  # watcher-owned state: rewritten by loop-tick on essentially every tick, so it is
  # what the teardown rescue must NOT treat as human work worth alarming about.
  printf '{"id":"lq-seed","status":"open"}\n' > "$seed/knowledge/loop-queue.jsonl"
  printf 'pristine\n' > "$seed/tracked.txt"
  git -C "$seed" add -A
  git_q -C "$seed" commit -m seed
  git_q -C "$seed" remote add origin "$origin"
  git_q -C "$seed" push origin main
  printf '%s' "$origin"
}

# One "machine": its own clone, its own $HOME, its own fake hostname, and the
# wrapper deployed exactly the way install.sh deploys it (sed __REPO__, copy).
mk_machine() {
  local name="$1" origin="$2" dirty="$3" mode="${4:-}" okey="${5:-}"
  local repo="$TMP/$name/repo" home="$TMP/$name/home"
  mkdir -p "$TMP/$name" "$home/.config/claudemaxxing" "$home/.local/bin"
  git clone -q -b main "$origin" "$repo"
  git -C "$repo" config user.email loop@test
  git -C "$repo" config user.name  loop
  git -C "$repo" config commit.gpgsign false
  [ "$dirty" = "1" ] && printf 'uncommitted human edit\n' > "$repo/tracked.txt"

  cat > "$home/.local/bin/hostname" <<SH
#!/bin/sh
printf '%s\n' "$name"
SH
  # Gate once per machine, then report idle after its round. This lets the real wrapper
  # exercise loop-until-dry without turning this claim-focused contract into an infinite loop.
  cat > "$home/.local/bin/loop-tick" <<SH
#!/bin/sh
[ -f "$TMP/$name/round-fired" ] && exit 1
exit 0
SH
  printf '#!/bin/sh\nexit 0\n'                       > "$home/.local/bin/harness-verify"
  printf '#!/bin/sh\nexit 0\n'                       > "$home/.local/bin/loop-queue"
  # the round: commits one uniquely-named artifact in whatever tree it was fired in.
  cat > "$home/.local/bin/harness-agent-run" <<SH
#!/bin/sh
printf '%s\n' "$name" >> "$ROUNDS"
touch "$TMP/$name/round-fired"
printf 'round by %s\n' "$name" > "round-$name.txt"
git add "round-$name.txt"
git commit -q -m "self-improve: round artifact ($name)"
SH
  # "lose" mode reproduces the only path that can drop a round: the round touches a
  # file the human left dirty in the main tree (so the cherry-pick back WILL be
  # refused) and the remote stops accepting pushes right after the claim landed (so
  # the commit never reaches origin either). Nothing but the worktree HEAD holds it.
  if [ "$mode" = "lose" ]; then
    cat >> "$home/.local/bin/harness-agent-run" <<SH
printf 'round by %s\n' "$name" > tracked.txt
git add tracked.txt
git commit -q -m "self-improve: round touches the file the human is editing ($name)"
touch "$TMP/deny-$okey"
SH
  fi
  # "propose" mode reproduces the unattended SELECT->PROPOSED outcome the loop is
  # DESIGNED to produce: the round commits its log line, then leaves the load-bearing
  # change UNCOMMITTED for the human gate -- a modified tracked file AND a brand-new
  # untracked one (a PROPOSED diff routinely adds files).
  if [ "$mode" = "propose" ]; then
    cat >> "$home/.local/bin/harness-agent-run" <<SH
printf 'PROPOSED doctrine edit\n' > tracked.txt
printf 'new tool body\n' > proposed-new-tool.sh
SH
  fi
  # "queuedirt" mode is the COMMON tick: the only thing left uncommitted is the
  # watcher-owned queue state that loop-tick rewrites every run.
  if [ "$mode" = "queuedirt" ]; then
    cat >> "$home/.local/bin/harness-agent-run" <<SH
printf '{"id":"lq-seed","status":"claimed"}\n' > knowledge/loop-queue.jsonl
SH
  fi
  # "intentdirt" leaves the OTHER queue file dirty. loop-tick does not write it and it
  # carries sticky human decisions, so it is not watcher-owned and must be rescued.
  if [ "$mode" = "intentdirt" ]; then
    cat >> "$home/.local/bin/harness-agent-run" <<SH
printf '{"id":"iq-1","status":"dismissed-by-human"}\n' > knowledge/intent-queue.jsonl
SH
  fi
  # "unrescuable" leaves work that CANNOT be staged (an unreadable file), so the
  # rescue itself fails and cleanup has to fall back to keeping the worktree.
  if [ "$mode" = "unrescuable" ]; then
    cat >> "$home/.local/bin/harness-agent-run" <<SH
printf 'PROPOSED but unstageable\n' > proposed-blocked.txt
chmod 000 proposed-blocked.txt
SH
  fi
  chmod +x "$home/.local/bin/"*
  sed "s#__REPO__#$repo#g" "$WRAPPER_SRC" > "$home/.config/claudemaxxing/loop-cron.sh"
}

run_machine() {
  local name="$1"
  HOME="$TMP/$name/home" bash "$TMP/$name/home/.config/claudemaxxing/loop-cron.sh" \
    >>"$TMP/$name/stdout.log" 2>&1
}

mlog()  { cat "$TMP/$1/home/.config/claudemaxxing/loop-cron.log" 2>/dev/null; }
claim_on_origin() { git -C "$1" show main:knowledge/loop-claim 2>/dev/null; }

# ── Scenario 1: dirty tree (worktree / detached HEAD) + a second machine ─────
O1="$(mk_origin o1)"
mk_machine A "$O1" 1     # dirty  -> forces the worktree path
mk_machine B "$O1" 0     # clean  -> the other computer, cloned before A ran

run_machine A

# C1 — the claim commit written on the worktree's detached HEAD actually reaches
# origin. This IS the lock: if it doesn't land, nothing coordinates the machines.
if [ "$(claim_on_origin "$O1" | cut -d' ' -f1)" = "$TODAY" ]; then
  ok C1 "dirty-tree round pushes today's claim to origin/main"
else
  bad C1 "origin/main claim is '$(claim_on_origin "$O1" | tr -d '\n')', expected $TODAY (the lock never left the machine)"
fi

# C2 — and the lock has TEETH: the other machine must back off the same day.
run_machine B
if [ "$(tr -d '[:space:]' < "$ROUNDS")" = "A" ]; then
  ok C2 "second machine backs off — exactly one round fired across both"
else
  bad C2 "rounds fired: [$(tr '\n' ' ' < "$ROUNDS")] — expected only A (double-run)"
fi
if mlog B | grep 'IDLE.*already claimed' >/dev/null; then
  ok C3 "backing-off machine logs the claim it saw"
else
  bad C3 "machine B log has no 'already claimed' IDLE line: $(mlog B | tail -2 | tr '\n' ' ')"
fi

# C4 — the round's own commit still propagates (the claim fix must not strand it).
if git -C "$O1" ls-tree --name-only main | grep '^round-A.txt$' >/dev/null; then
  ok C4 "round commit from the worktree reaches origin/main"
else
  bad C4 "round-A.txt is not on origin/main — the round's work did not propagate"
fi

# C5 — the dirty main tree is left un-diverged from origin (a duplicate commit
# there cannot be pushed non-ff and cannot be rebased away while the tree is
# dirty, so it would wedge every later tick).
if git -C "$TMP/A/repo" merge-base --is-ancestor main origin/main 2>/dev/null; then
  ok C5 "main tree stays an ancestor of origin/main (no divergent duplicate)"
else
  bad C5 "$TMP/A/repo main diverged from origin/main after the worktree round"
fi

# C6 — the human's uncommitted edit is untouched. The worktree exists for this.
if [ "$(cat "$TMP/A/repo/tracked.txt")" = "uncommitted human edit" ]; then
  ok C6 "uncommitted work in the shared tree survives the round"
else
  bad C6 "tracked.txt was clobbered: $(cat "$TMP/A/repo/tracked.txt")"
fi

# C8 — the trade-off this fix makes on purpose: the dirty main tree is left
# BEHIND origin (it no longer gets a cherry-picked duplicate). Prove that costs
# nothing across ticks — a second tick must build its worktree from that stale
# main, rebase forward, see the claim, and idle cleanly instead of wedging.
run_machine A
if [ "$(tr -d '[:space:]' < "$ROUNDS")" = "A" ] \
   && mlog A | grep 'IDLE.*queue not actionable' >/dev/null \
   && git -C "$TMP/A/repo" merge-base --is-ancestor main origin/main 2>/dev/null; then
  ok C8 "a later tick off the stale main tree rebases forward and stays dry"
else
  bad C8 "second tick misbehaved: rounds=[$(tr '\n' ' ' < "$ROUNDS")] tail=$(mlog A | tail -2 | tr '\n' ' ')"
fi

# ── Scenario 2: clean tree (no worktree) — the path that already worked ──────
O2="$(mk_origin o2)"
mk_machine C "$O2" 0
run_machine C

if [ "$(claim_on_origin "$O2" | cut -d' ' -f1)" = "$TODAY" ] \
   && git -C "$O2" ls-tree --name-only main | grep '^round-C.txt$' >/dev/null; then
  ok C7 "clean-tree path still claims and pushes its round (no regression)"
else
  bad C7 "clean-tree machine: claim='$(claim_on_origin "$O2" | tr -d '\n')' tree=[$(git -C "$O2" ls-tree --name-only main | tr '\n' ' ')]"
fi

# ── Scenario 3: the round's commit can reach NEITHER origin NOR main ─────────
# Push denied after the claim landed + cherry-pick refused by the human's dirty
# tracked.txt. The wrapper logs "manual reconcile" and then removes the worktree,
# which is the only ref holding that commit. Cleanup must not destroy the round.
O3="$(mk_origin o3)"
mk_machine D "$O3" 1 lose o3
run_machine D

# C11 first — assert the PRECONDITION, so if this scenario ever stops reproducing the
# loss path (hook not firing, no cherry-pick conflict) the contract says which half
# broke instead of just reporting a missing tag.
if mlog D | grep 'push failed' >/dev/null && mlog D | grep 'cherry-pick from worktree conflicted' >/dev/null \
   && ! git -C "$O3" ls-tree --name-only main | grep '^round-D.txt$' >/dev/null; then
  ok C11 "precondition real: push was refused AND cherry-pick conflicted, so origin never got the round"
else
  bad C11 "scenario did not reproduce the loss path: $(mlog D | tail -3 | tr '\n' ' ')"
fi

RESCUE_TAG="$(git -C "$TMP/D/repo" tag --list 'loop-rescue/*' | head -1)"
if [ -n "$RESCUE_TAG" ] \
   && git -C "$TMP/D/repo" ls-tree --name-only "$RESCUE_TAG" | grep '^round-D.txt$' >/dev/null; then
  ok C9 "an unpushed, un-cherry-pickable round survives worktree cleanup (tag $RESCUE_TAG)"
else
  bad C9 "round-D.txt is on no surviving ref after cleanup — the round was LOST (tags=[$(git -C "$TMP/D/repo" tag --list | tr '\n' ' ')])"
fi

# C10 — and the rescue must not be bought by leaking the worktree: cleanup still
# happens, so worktrees can't pile up tick after tick.
if [ "$(git -C "$TMP/D/repo" worktree list --porcelain | grep -c '^worktree ')" = "1" ]; then
  ok C10 "the worktree is still cleaned up (rescue is a ref, not a leaked directory)"
else
  bad C10 "worktree left behind: $(git -C "$TMP/D/repo" worktree list | tr '\n' ' ')"
fi

# ── Scenario 4: the rescue tag itself cannot be written ─────────────────────
# Same loss path, but a D/F ref conflict (an existing `loop-rescue` tag blocks the
# nested name) makes tagging impossible. Cleanup must then REFUSE to remove the
# worktree: a stale directory is visible and reversible, an unreachable commit is
# neither. Without this branch the fix would only shrink the loss window.
O4="$(mk_origin o4)"
mk_machine E "$O4" 1 lose o4
git -C "$TMP/E/repo" tag loop-rescue main      # blocks every refs/tags/loop-rescue/* below it
run_machine E

WT_E="$(git -C "$TMP/E/repo" worktree list --porcelain | awk '/^worktree /{print $2}' | grep loop-worktree | head -1)"
if [ -n "$WT_E" ] && [ -d "$WT_E" ] \
   && git -C "$WT_E" ls-tree --name-only HEAD | grep '^round-E.txt$' >/dev/null \
   && mlog E | grep 'KEEPING worktree' >/dev/null; then
  ok C12 "an untaggable commit keeps its worktree rather than being destroyed"
else
  bad C12 "untaggable commit was not preserved: worktree='$WT_E' log=$(mlog E | tail -2 | tr '\n' ' ')"
fi

# PID reuse is intentionally treated as "still alive" by the production
# guard. macOS recycles short-lived fixture PIDs aggressively, so move the
# preserved worktree to a deterministic impossible PID before testing the
# genuinely-dead branch.
DEAD_PID_E=99999993
WT_E_DEAD="$TMP/E/repo/.loop-worktree-$DEAD_PID_E"
kill -0 "$DEAD_PID_E" 2>/dev/null && { printf 'loop-claim fixture PID unexpectedly alive: %s\n' "$DEAD_PID_E" >&2; exit 1; }
git_q -C "$TMP/E/repo" worktree move "$WT_E" "$WT_E_DEAD" \
  || { printf 'loop-claim fixture could not move preserved worktree\n' >&2; exit 1; }
WT_E="$WT_E_DEAD"

# ── Scenario 5: dead-tick worktrees — prune when safe, keep when at risk ─────
# (lq-03823dd6) Leaked worktrees from dead ticks used to accumulate forever.

# C13 — the observed leak shape: a registered .loop-worktree-<pid> whose pid is
# dead and whose HEAD is already on origin/main (nothing at risk). The next tick
# must prune it at start.
DEAD_PID=99999991                           # outside Darwin/Linux PID ranges
kill -0 "$DEAD_PID" 2>/dev/null && { printf 'loop-claim fixture PID unexpectedly alive: %s\n' "$DEAD_PID" >&2; exit 1; }
LEAK_A="$TMP/A/repo/.loop-worktree-$DEAD_PID"
git_q -C "$TMP/A/repo" worktree add --detach "$LEAK_A" origin/main
run_machine A
if [ ! -d "$LEAK_A" ] && mlog A | grep 'PRUNED dead loop worktree' >/dev/null; then
  ok C13 "a dead tick's worktree with its commits on origin/main is pruned at tick start"
else
  bad C13 "leaked worktree survived the tick: $(git -C "$TMP/A/repo" worktree list | tr '\n' ' ')"
fi

# C14 — the deliberately KEPT worktree from C12 (commit on no other ref, rescue
# tag blocked) must NOT be pruned even though its tick's pid is dead: pruning it
# is what would destroy the round. The prune must say why it kept it.
run_machine E
if [ -n "$WT_E" ] && [ -d "$WT_E" ] \
   && git -C "$WT_E" ls-tree --name-only HEAD | grep '^round-E.txt$' >/dev/null \
   && mlog E | grep 'KEEPING dead worktree' >/dev/null; then
  ok C14 "a dead worktree whose commit is on no safe ref survives the prune"
else
  bad C14 "at-risk worktree was pruned or not reported: worktree='$WT_E' log=$(mlog E | tail -3 | tr '\n' ' ')"
fi

# C15 — once the stranded commit is reconciled (reaches origin) and the machine
# has synced, the same worktree is pruned: the KEEPING path is a deferral, not a
# permanent leak.
rm -f "$TMP/deny-o4"
git_q -C "$WT_E" push origin HEAD:main
git_q -C "$TMP/E/repo" fetch origin
run_machine E
if [ ! -d "$WT_E" ] && mlog E | grep 'PRUNED dead loop worktree' >/dev/null; then
  ok C15 "a reconciled kept worktree is pruned on the next synced tick"
else
  bad C15 "reconciled worktree not pruned: $(git -C "$TMP/E/repo" worktree list | tr '\n' ' ') log=$(mlog E | tail -3 | tr '\n' ' ')"
fi

# C16 — critic-sourced negative case (deepseek, 2026-08-14): a dead worktree
# whose HEAD cannot even be read must be KEPT, not pruned — "couldn't measure"
# is not "safe". Corrupt the worktree's HEAD metadata and prove the prune
# refuses it.
DEAD_PID2=99999992
kill -0 "$DEAD_PID2" 2>/dev/null && { printf 'loop-claim fixture PID unexpectedly alive: %s\n' "$DEAD_PID2" >&2; exit 1; }
LEAK_BAD="$TMP/A/repo/.loop-worktree-$DEAD_PID2"
git_q -C "$TMP/A/repo" worktree add --detach "$LEAK_BAD" origin/main
printf 'not a ref\n' > "$(git -C "$LEAK_BAD" rev-parse --git-dir)/HEAD"
run_machine A
if [ -d "$LEAK_BAD" ] && mlog A | grep 'HEAD unreadable' >/dev/null; then
  ok C16 "a dead worktree with an unreadable HEAD is kept, not destroyed"
else
  bad C16 "unreadable-HEAD worktree mishandled: exists=$([ -d "$LEAK_BAD" ] && echo yes || echo no) log=$(mlog A | tail -3 | tr '\n' ' ')"
fi

# ── Scenario 6: the unattended PROPOSED diff (lq-35967534) ──────────────────
# Doctrine (/self-improve, and this wrapper's own header) says a load-bearing change
# found by an unattended round is left UNCOMMITTED for the human gate. The dirty-tree
# path is the COMMON path (the watcher rewrites the queue file every tick), and there
# teardown used to rescue COMMITS only and then `git worktree remove --force` --
# silently destroying the proposal while logging "worktree cleaned up" as a success.
# Proven red against the pre-fix wrapper: C17/C18 fail (no ref holds the diff).
rescue_holds() {   # repo path want -> 0 if any loop-rescue ref carries that content
  local repo="$1" path="$2" want="$3" t
  for t in $(git -C "$repo" tag --list 'loop-rescue/*'); do
    git -C "$repo" show "$t:$path" 2>/dev/null | grep "$want" >/dev/null && return 0
  done
  return 1
}

O5="$(mk_origin o5)"
mk_machine F "$O5" 1 propose
run_machine F

# C17 — the proposal survives teardown, INCLUDING the file git was not tracking.
# An untracked new file is the half a `git stash`-shaped rescue would silently drop.
if rescue_holds "$TMP/F/repo" tracked.txt 'PROPOSED doctrine edit' \
   && rescue_holds "$TMP/F/repo" proposed-new-tool.sh 'new tool body'; then
  ok C17 "an unattended PROPOSED diff (tracked + untracked) survives worktree teardown"
else
  bad C17 "PROPOSED work was DESTROYED by cleanup — tags=[$(git -C "$TMP/F/repo" tag --list | tr '\n' ' ')] log=$(mlog F | tail -3 | tr '\n' ' ')"
fi

# C18 — and it is rescued LOUDLY. A proposal the human never hears about is the
# same no-op as one that was deleted; "worktree cleaned up" must not be the whole story.
if mlog F | grep 'RESCUE — uncommitted' >/dev/null; then
  ok C18 "the uncommitted rescue is logged for the human gate"
else
  bad C18 "no loud RESCUE line for the uncommitted proposal: $(mlog F | tail -3 | tr '\n' ' ')"
fi

# C19 — rescue must not be bought with a leaked worktree (same rule C10 pins).
if [ "$(git -C "$TMP/F/repo" worktree list --porcelain | grep -c '^worktree ')" = "1" ]; then
  ok C19 "the proposal rescue is a ref, not a leaked worktree"
else
  bad C19 "worktree left behind: $(git -C "$TMP/F/repo" worktree list | tr '\n' ' ')"
fi

# ── Scenario 7: the common tick must stay silent ────────────────────────────
# The watcher rewrites knowledge/loop-queue.jsonl on essentially every tick, so a
# rescue that fires on ANY dirt would tag every single day. Alarm fatigue defeats
# the point of C18: the human stops reading RESCUE lines. Watcher-owned queue state
# is re-derived by the next tick, so dropping it is safe and is the status quo.
O6="$(mk_origin o6)"
mk_machine G "$O6" 1 queuedirt
run_machine G

if [ -z "$(git -C "$TMP/G/repo" tag --list 'loop-rescue/*')" ] \
   && ! mlog G | grep 'RESCUE — uncommitted' >/dev/null \
   && [ "$(git -C "$TMP/G/repo" worktree list --porcelain | grep -c '^worktree ')" = "1" ]; then
  ok C20 "a tick dirty ONLY with watcher-owned queue state rescues nothing and stays quiet"
else
  bad C20 "queue-only dirt triggered a rescue (alarm fatigue): tags=[$(git -C "$TMP/G/repo" tag --list | tr '\n' ' ')] log=$(mlog G | tail -3 | tr '\n' ' ')"
fi

# ── Scenario 8: the exclusion stays as narrow as the evidence ───────────────
# Only knowledge/loop-queue.jsonl is proven watcher-rewritten every tick. Excluding
# knowledge/intent-queue.jsonl too (first draft; both cross-family critics caught it)
# would have traded silence for real loss — loop-tick never writes it and it carries
# sticky human `dismiss` decisions nothing re-derives. Anything not PROVEN watcher-owned
# is rescued.
O7="$(mk_origin o7)"
mk_machine H "$O7" 1 intentdirt
run_machine H

if rescue_holds "$TMP/H/repo" knowledge/intent-queue.jsonl 'dismissed-by-human'; then
  ok C21 "a dirty file that is not proven watcher-owned is rescued, not silently dropped"
else
  bad C21 "intent-queue work was DESTROYED — tags=[$(git -C "$TMP/H/repo" tag --list | tr '\n' ' ')] log=$(mlog H | tail -3 | tr '\n' ' ')"
fi

# ── Scenario 9: the rescue itself fails ─────────────────────────────────────
# If the leftovers cannot be staged/committed, "log a WARN and delete anyway" would
# only SHRINK the loss window (deepseek, 2026-08-25). Fall back to the rule C12 already
# pins: a stale directory is cheap, visible and reversible; destroyed work is not.
O8="$(mk_origin o8)"
mk_machine I "$O8" 1 unrescuable
run_machine I

# Precondition first (C11's discipline): if the staging failure stops reproducing —
# e.g. running as root, where mode 000 does not block a read — say which half broke.
if mlog I | grep 'could not stage uncommitted round work' >/dev/null; then
  ok C22 "precondition real: the rescue genuinely could not stage the leftover work"
else
  bad C22 "scenario did not reproduce an unrescuable leftover (running as root?): $(mlog I | tail -3 | tr '\n' ' ')"
fi

WT_I="$(git -C "$TMP/I/repo" worktree list --porcelain | awk '/^worktree /{print $2}' | grep loop-worktree | head -1)"
if [ -n "$WT_I" ] && [ -f "$WT_I/proposed-blocked.txt" ] \
   && mlog I | grep 'KEEPING worktree.*could not be rescued' >/dev/null; then
  ok C23 "unrescuable round work keeps its worktree instead of being force-removed"
else
  bad C23 "unrescuable work was destroyed: worktree='$WT_I' log=$(mlog I | tail -3 | tr '\n' ' ')"
fi

printf 'loop-claim contract: %s/%s PASS\n' "$pass" "$((pass+fail))"
[ "$fail" -eq 0 ] || exit 1
