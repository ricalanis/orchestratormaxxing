#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# orchestratormaxxing — autonomous self-improve heartbeat (daily; invoked by launchd on macOS,
# systemd --user timer on Linux — same wrapper, scheduler differs per OS).
#
# SOURCE OF TRUTH: this file is a TEMPLATE in the repo (deploy/loop-cron.sh). The live
# copy at $HOME/.config/orchestratormaxxing/loop-cron.sh is deployed by install.sh (copy, NOT
# symlink — __REPO__ is substituted at deploy time). Edit here, then re-run ./install.sh.
#
# BOTH computers can arm this loop — it's idempotent across machines via a per-day git claim,
# so there's no "single writer" requirement. Each daily tick:
#   1. sync DOWN  — pull-rebase origin/main, so this machine + the round start current.
#   2. loop-tick --intake --x --gate : watchers + research intake → queue → GATE (0 iff actionable).
#   3. CLAIM the day — if actionable, write a `loop-claim` marker, commit, and PUSH it. This is
#      an optimistic lock: whoever's push lands first wins the day; a machine that finds today
#      already claimed (or loses the push race) backs off WITHOUT running a round. So at most one
#      machine owns the day's drain sequence, and the simultaneous-07:00 case can't double-run.
#   4. fire ONE /self-improve round for that gate trigger, then re-gate. Continue while
#      actionable work remains; stop immediately when the verifier is RED or the queue is dry.
#   5. sync UP after each round so improvements propagate to the other computer.
#
# Safety posture ("fully autonomous, synced, multi-machine"):
#   • NEVER operates on a dirty tree — skips entirely if you have uncommitted edits here.
#   • Fires ONLY when the queue is actionable AND this machine won the day's claim.
#   • Single-flight lock — a slow round never overlaps the next day's tick.
#   • --max-turns cap — bounds the round's length / spend.
#   • Doctrine + install.sh changes are queued PROPOSED by /self-improve (not committed),
#     so they NEVER auto-push — only verifier-green + critic-approved changes propagate.
#   • Sync is SAFE: pull is --rebase, push is never forced; a conflict aborts and is logged
#     for manual reconcile, and an un-pushed commit is retried on the next tick. No lost work.
#   • Auth: push uses your git credential helper (macOS osxkeychain under launchd; on Linux,
#     SSH-key/agent or a stored helper — the systemd --user unit inherits your user environment).
#
# Toggles:  LOOP_SYNC=0 → local-only (no pull/push/claim; old single-machine behavior).
#           LOOP_DRY=1  → watch+gate only; never fire, never sync/claim (plumbing test).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO="__REPO__"
CFG="$HOME/.config/orchestratormaxxing"
# Export keys (XAI_API_KEY, …) from the standalone .env into this process so the
# headless `claude` round and every Bash shell / workflow agent it spawns inherit
# them. Without this the loop's children have no $XAI_API_KEY in-env — xsearch still
# works (it reads this .env directly) but raw-API/[ -n "$XAI_API_KEY" ] checks fail.
if [ -f "$CFG/.env" ]; then set -a; . "$CFG/.env"; set +a; fi
LOG="$CFG/loop-cron.log"
LOCK="$CFG/loop-cron.lock"
CLAIM="knowledge/loop-claim"          # repo-relative; the per-day cross-machine lock
# Which LOCAL ref carries this round's commits. On the clean-tree path that's the
# branch `main`; on the dirty-tree path the round runs on a DETACHED HEAD inside a
# worktree, where `main` is a different (stale) commit that the claim was never
# written on. Every push/ahead-count below goes through this ref, so the wrapper
# always addresses what it actually committed. Naming `main` there pushed a branch
# with nothing new on it: the push succeeded trivially, the claim never reached
# origin, and the optimistic lock silently became a no-op (lq-10d88328, observed
# live 2026-08-08 and 2026-08-09 — both machines could fire the same day).
SYNC_REF="main"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MAX_TURNS=60
LOOP_SYNC="${LOOP_SYNC:-1}"           # 1 = pull/push/claim (multi-machine); 0 = local-only
LOOP_GRADUATE="${LOOP_GRADUATE:-1}"   # 1 = after a green sync, graduate the public projection as a rolling PR; 0 = never
HOST="$(hostname -s 2>/dev/null || hostname)"

ts(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
log(){ printf '%s | %s\n' "$(ts)" "$*" >> "$LOG"; }

cd "$REPO" || { log "FATAL cannot cd $REPO"; exit 1; }
# Git reports worktree paths physically (`/private/var/...` on macOS), while
# launchd/test substitutions may name the same repo through a logical alias
# (`/var/...` or case-only ~/dev). Canonicalize once so safety matching neither
# skips real leaks nor mistakes an outside path for one of ours.
REPO="$(pwd -P)"

# Single-flight: bail if a previous round is still alive.
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  log "SKIP — previous round still running (pid $(cat "$LOCK"))"
  exit 0
fi
echo $$ > "$LOCK"

# A round's commit lives ONLY on the worktree's detached HEAD. `git worktree remove`
# drops that ref, so a commit that neither reached origin nor cherry-picked back to
# main becomes unreachable and GC-able — and the "manual reconcile" we log would be a
# false promise (lq-cb30d5d6). So tag it in the shared repo FIRST: a tag is a real ref,
# so the commit survives cleanup and `git tag --list 'loop-rescue/*'` finds it. No-op
# once the commits are on origin/main (the common case) — a healthy tick tags nothing.
WORKTREE_DIR=""
# Returns 0 when nothing is at risk (already on origin/main, or successfully tagged),
# 1 when a commit is at risk and could NOT be tagged — the caller must then keep the
# worktree, because removing it is what would destroy the commit. The tag is named by
# the commit itself, so re-entry is idempotent and no two rescues can ever collide.
rescue_worktree_commits(){
  [ -n "${WORKTREE_DIR:-}" ] && [ -d "$WORKTREE_DIR" ] || return 0
  local head tag
  head="$(git -C "$WORKTREE_DIR" rev-parse --verify HEAD 2>/dev/null)" || return 0
  [ -n "$head" ] || return 0
  git -C "$REPO" merge-base --is-ancestor "$head" origin/main 2>/dev/null && return 0
  tag="loop-rescue/$(date -u +%F)-$head"   # full sha: two rescues can never collide
  if git -C "$REPO" tag -f "$tag" "$head" >/dev/null 2>>"$LOG"; then
    log "RESCUE — unpushed worktree commit $head kept reachable as tag $tag (recover: git cherry-pick $tag)"
    return 0
  fi
  log "WARN — could not tag unpushed worktree commit $head"
  return 1
}

# A round that stops at SELECT leaves its load-bearing change UNCOMMITTED on purpose
# — that is the human gate this wrapper's own header promises ("queued PROPOSED by
# /self-improve (not committed), so they NEVER auto-push"). But rescue above covers
# COMMITS only, and `git worktree remove --force` deletes exactly the uncommitted
# half while logging "worktree cleaned up" as a success. On the dirty-tree path —
# the COMMON path, since the watcher rewrites the queue file nearly every tick — that
# silently destroyed every proposal an unattended round produced (lq-35967534). So
# commit the leftovers onto the worktree's own HEAD first and let the tag rescue keep
# them reachable. This is PRESERVATION on a side ref, not delivery: cleanup runs after
# the cherry-pick/push block, so a PROPOSED diff still never lands on main and is
# still never pushed. Recover with `git cherry-pick`/`git show` on the rescue tag.
#
# EXACTLY ONE path is excluded from the TRIGGER: knowledge/loop-queue.jsonl, which
# loop-tick rewrites on essentially every tick. Rescuing on any dirt at all would tag
# every single day and train the operator to ignore RESCUE lines — alarm fatigue would
# cost more than the loss it warns about — and that file is re-derived by the next
# watcher run, so dropping it costs at most a re-mine (the status quo). The list stops
# there deliberately: knowledge/intent-queue.jsonl is NOT written by loop-tick (checked,
# not assumed) and carries sticky human `dismiss` decisions that nothing re-derives, so
# excluding it would have bought silence at the price of real loss. Anything not proven
# watcher-owned gets rescued. (Once something else IS dirty, `add -A` takes the queue
# file along; the exclusion decides WHETHER to rescue, never what to keep.)
#
# Returns 1 when work is at risk and could NOT be preserved — the caller must then keep
# the worktree, exactly as it does for an untaggable commit. `git add -A` honours
# .gitignore, so ignored material (.env, .results/) is never swept into the commit.
rescue_worktree_wip(){
  [ -n "${WORKTREE_DIR:-}" ] && [ -d "$WORKTREE_DIR" ] || return 0
  local dirty rc
  dirty="$(git -C "$WORKTREE_DIR" status --porcelain --untracked-files=all -- \
             ':(exclude)knowledge/loop-queue.jsonl' 2>>"$LOG")"; rc=$?
  if [ "$rc" -ne 0 ]; then
    # "couldn't measure" must never read as "clean" — a swallowed pathspec/repo error
    # reading as an empty status is the same false success this fix exists to remove.
    log "WARN — could not read the worktree's uncommitted state (rc=$rc); assuming there is work to rescue"
    dirty="unreadable"
  fi
  if [ -z "$dirty" ]; then
    # The one narrowing this function admits to: queue-only dirt is dropped, not saved.
    # Record it anyway — "deliberately not rescued" and "nobody noticed" must not look
    # the same in the log. An informational line, NOT a RESCUE alarm.
    git -C "$WORKTREE_DIR" diff --quiet -- knowledge/loop-queue.jsonl 2>/dev/null \
      || log "note — dropping uncommitted watcher queue state at cleanup (re-derived next tick; nothing else was dirty)"
    return 0
  fi
  if ! git -C "$WORKTREE_DIR" add -A >>"$LOG" 2>&1; then
    log "WARN — could not stage uncommitted round work for rescue"
    return 1
  fi
  # The probe can over-report (the unreadable branch above); staging nothing means
  # there was genuinely nothing at risk, so stay silent rather than tag an empty commit.
  git -C "$WORKTREE_DIR" diff --cached --quiet 2>/dev/null && return 0
  # --no-verify on purpose: a hook that BLOCKS this commit does not protect anything,
  # it destroys the proposal, because being blocked here means being deleted below.
  if ! git -C "$WORKTREE_DIR" commit -q --no-verify \
         -m "loop: PROPOSED — uncommitted round work rescued (NOT applied; review before merging)" >>"$LOG" 2>&1; then
    log "WARN — could not commit uncommitted round work for rescue"
    return 1
  fi
  log "RESCUE — uncommitted round work committed on the worktree HEAD for the human gate (PROPOSED: review it, do not assume it was applied)"
  return 0
}

# The ONLY place the worktree is removed. Cleanup must never cost the round its last
# ref, so an untaggable commit keeps its worktree instead: a stale directory is a
# cheap, visible, reversible problem; an unreachable commit is silent and permanent.
cleanup_worktree(){
  [ -n "${WORKTREE_DIR:-}" ] && [ -d "$WORKTREE_DIR" ] || return 0
  if ! rescue_worktree_wip; then
    log "KEEPING worktree $WORKTREE_DIR — uncommitted round work could not be rescued; removing it would destroy the proposal (manual reconcile)"
    return 0
  fi
  if rescue_worktree_commits; then
    git worktree remove --force "$WORKTREE_DIR" 2>>"$LOG" || true
    log "worktree cleaned up"
  else
    log "KEEPING worktree $WORKTREE_DIR — its commit is on no other ref and could not be tagged; removing it would destroy the round (manual reconcile)"
  fi
}

trap 'rm -f "$LOCK"; cleanup_worktree' EXIT

# Leaked loop worktrees from dead ticks (a crash before cleanup, an old wrapper,
# or the deliberate KEEPING path above once its commit has been reconciled) would
# otherwise accumulate in the repo root forever (lq-03823dd6). Prune only what is
# provably safe: the pid embedded in the name must be dead, and the worktree's
# HEAD must be on origin/main or preserved by a loop-rescue tag — the same "a
# commit must survive on some ref" rule cleanup_worktree enforces. Never a blind
# `git worktree prune`. Runs before the dirty check so a pruned leak can return
# the main tree to the clean (non-worktree) path.
prune_dead_worktrees(){
  git -C "$REPO" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' \
  | while IFS= read -r wt; do
    case "$wt" in "$REPO"/.loop-worktree-*) ;; *) continue ;; esac
    pid="${wt##*.loop-worktree-}"
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    [ "$pid" = "$$" ] && continue
    kill -0 "$pid" 2>/dev/null && continue      # still alive (or reused): leave it
    head="$(git -C "$wt" rev-parse --verify HEAD 2>/dev/null)" || head=""
    if [ -z "$head" ]; then
      # "Couldn't read it" must never become "safe to delete": an unreadable HEAD
      # may still be the only ref holding a commit (signal-vs-artifact doctrine).
      log "KEEPING dead worktree $wt — HEAD unreadable; cannot prove its commit safe (manual reconcile)"
      continue
    fi
    if ! git -C "$REPO" merge-base --is-ancestor "$head" origin/main 2>/dev/null \
       && [ -z "$(git -C "$REPO" tag --points-at "$head" --list 'loop-rescue/*' 2>/dev/null)" ]; then
      log "KEEPING dead worktree $wt — HEAD $head is on no safe ref (not on origin/main, no rescue tag); manual reconcile"
      continue
    fi
    if git -C "$REPO" worktree remove --force "$wt" 2>>"$LOG"; then
      log "PRUNED dead loop worktree $wt (pid $pid gone; commits safe on another ref)"
    else
      log "WARN — could not remove dead worktree $wt"
    fi
  done
}
prune_dead_worktrees

# Dirty tree? Run the round in an isolated worktree instead of skipping.
# The shared tree is dirty almost every day from parallel sessions — skipping
# starves the self-improve loop for days. A worktree gives the round a clean
# copy that can't entangle with uncommitted edits. The round's commit is
# cherry-picked back to the main tree after it completes (if clean).
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  WORKTREE_DIR="$REPO/.loop-worktree-$$"
  log "DIRTY — working tree has uncommitted edits; running round in isolated worktree ($WORKTREE_DIR)"
  if ! git worktree add --quiet --detach "$WORKTREE_DIR" HEAD 2>>"$LOG"; then
    log "SKIP — could not create worktree; not syncing or firing (dirty tree, worktree add failed)"
    exit 0
  fi
  # Re-point subsequent operations at the worktree — including which ref we push.
  SYNC_REF="HEAD"
  cd "$WORKTREE_DIR" || { log "FATAL cannot cd $WORKTREE_DIR"; git worktree remove --force "$WORKTREE_DIR" 2>/dev/null; exit 1; }
fi

git_sync_down(){
  { [ "$LOOP_SYNC" = "1" ] && [ "${LOOP_DRY:-0}" != "1" ]; } || return 0
  if ! git fetch --quiet origin 2>>"$LOG"; then
    log "WARN — git fetch failed (offline?); proceeding without sync"; return 0
  fi
  if ! git rebase origin/main >>"$LOG" 2>&1; then
    git rebase --abort 2>/dev/null
    log "HALT — rebase onto origin/main hit conflicts; reconcile manually (nothing fired this tick)"
    return 1
  fi
  return 0
}

git_sync_up(){
  { [ "$LOOP_SYNC" = "1" ] && [ "${LOOP_DRY:-0}" != "1" ]; } || return 0
  [ "$(git rev-list --count "origin/main..$SYNC_REF" 2>/dev/null || echo 0)" -gt 0 ] || return 0
  if git push --quiet origin "$SYNC_REF:main" 2>>"$LOG"; then
    log "pushed to origin — both computers in sync"; return 0
  fi
  git fetch --quiet origin 2>>"$LOG" || true
  if git rebase origin/main >>"$LOG" 2>&1 && git push --quiet origin "$SYNC_REF:main" 2>>"$LOG"; then
    log "pushed after rebase-retry — both computers in sync"; return 0
  fi
  git rebase --abort 2>/dev/null
  log "WARN — push failed (diverged/auth/offline); commit stays local, retried next tick"
  return 0
}

# Per-day cross-machine claim (the optimistic lock). Returns:
#   0 → THIS machine owns today's round (proceed to fire)
#   1 → already claimed / lost the race / can't claim → back off (do NOT fire), stay synced
graduate_public(){
  # Graduation = publish the gated public projection (core-export --pr → one rolling PR on the
  # public repo). Runs ONLY on fleet machines (install-fleet.sh present), only with sync on, and
  # only when the tool is installed; never fatal — the private repo is the source of truth and a
  # failed graduation simply retries next tick. Bounded so a wedged gh/git cannot hold the tick.
  [ "$LOOP_GRADUATE" = "1" ] || return 0
  [ "$LOOP_SYNC" = "1" ] || return 0
  [ -x "$REPO/install-fleet.sh" ] || return 0
  command -v core-export >/dev/null 2>&1 || { log "graduate — skipped (core-export not on PATH)"; return 0; }
  local out rc
  out="$( (cd "$REPO" && timeout 300 core-export --repo "$REPO" --pr --json) 2>>"$LOG")"; rc=$?
  if [ "$rc" -eq 0 ]; then
    log "graduate — $(printf '%s' "$out" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); p=d.get("push") or {}
    print("pushed" if p.get("pushed") else "no changes", p.get("action",""), p.get("url") or p.get("pr") or "")
except Exception:
    print("done (summary unreadable)")')"
  else
    log "WARN — graduation exited $rc (gate red = a tenant literal would ship; see core-export output above); private repo unaffected"
  fi
  return 0
}

claim_day(){
  [ "$LOOP_SYNC" = "1" ] || return 0          # local-only: no coordination needed, just fire
  local today; today="$(date -u +%F)"
  # Already claimed today in the synced state? (the common case: other machine ran earlier)
  if [ -f "$CLAIM" ] && [ "$(cut -d' ' -f1 "$CLAIM" 2>/dev/null)" = "$today" ]; then
    log "IDLE — today's round already claimed by $(cut -d' ' -f2 "$CLAIM" 2>/dev/null) (synced; skipping)"
    return 1
  fi
  # Try to claim: write marker, commit, push. The push winning IS the lock.
  _write_claim "$today"
  if git push --quiet origin "$SYNC_REF:main" 2>>"$LOG"; then
    log "claimed $today @ $HOST — won the day, firing"
    return 0
  fi
  # Push rejected: someone advanced origin between our fetch and now. Drop our claim, take theirs.
  git fetch --quiet origin 2>>"$LOG" || true
  git reset --hard origin/main >>"$LOG" 2>&1
  if [ -f "$CLAIM" ] && [ "$(cut -d' ' -f1 "$CLAIM" 2>/dev/null)" = "$today" ]; then
    log "IDLE — lost daily claim race to $(cut -d' ' -f2 "$CLAIM" 2>/dev/null); backing off (synced)"
    return 1
  fi
  # Origin advanced for an UNRELATED reason (e.g. your manual push) — retry the claim once.
  _write_claim "$today"
  if git push --quiet origin "$SYNC_REF:main" 2>>"$LOG"; then
    log "claimed $today @ $HOST after retry — firing"; return 0
  fi
  git reset --hard origin/main >>"$LOG" 2>&1
  log "WARN — could not claim the day (contention/auth/offline); skipping round, retry next tick"
  return 1
}
_write_claim(){
  printf '%s %s %s\n' "$1" "$HOST" "$(date -u +%s)" > "$CLAIM"
  git add "$CLAIM"
  git commit -q -m "loop: claim $1 @ $HOST"
}

# 1. sync DOWN first so this machine + the round start from the latest origin.
if ! git_sync_down; then exit 0; fi

# 2. watch + research intake + gate. Exit 0 = a round is warranted; 1 = idle; 2 = watcher errored.
tick_out="$(loop-tick --intake --x --gate 2>&1)"; gate=$?
log "tick (gate=$gate) :: $tick_out"

if [ "${LOOP_DRY:-0}" = "1" ]; then
  log "DRY — would $( [ $gate -eq 0 ] && echo 'try to claim + fire' || echo 'stay idle' ); not acting/syncing."
  exit 0
fi

if [ "$gate" -eq 2 ]; then
  log "HALT — a deterministic watcher errored (harness-verify/mem-audit). Not firing; needs a look."
  exit 0
fi

if [ "$gate" -ne 0 ]; then
  log "IDLE — queue not actionable, no round."
  git_sync_up   # push any straggler commits from a prior failed push; keeps machines current
  graduate_public
  exit 0
fi

# 3. claim the day across machines — back off if another machine already owns it.
if ! claim_day; then
  exit 0
fi

# 4. Drain the actionable queue with discrete triggers. Each gate authorizes exactly one
# round; after that round we re-run the deterministic watchers before authorizing another.
# This is loop-until-dry without turning loop-tick itself into an unbounded agent loop.
round_n=0
while [ "$gate" -eq 0 ]; do
  round_n=$((round_n+1))
  log "ACT — firing round $round_n (host=${HARNESS_HOST:-claude}, max-turns=$MAX_TURNS)"
  MAX_TURNS="$MAX_TURNS" harness-agent-run self-improve >> "$LOG" 2>&1
  rc=$?
  log "round $round_n finished rc=$rc"
  if [ "$rc" -ne 0 ]; then
    log "HALT — round $round_n failed rc=$rc; queue remains for the next trigger."
    break
  fi

  # Propagate each accepted round before re-gating. A later RED stops further rounds but
  # does not strand already-committed work on one machine.
  git_sync_up

  tick_out="$(loop-tick --intake --x --gate 2>&1)"; gate=$?
  log "tick after round $round_n (gate=$gate) :: $tick_out"
  if [ "$gate" -eq 2 ]; then
    log "HALT — deterministic watcher RED after round $round_n; no further rounds."
    break
  fi
  if [ "$gate" -ne 0 ]; then
    log "IDLE — queue drained after $round_n round(s)."
    break
  fi
done

# If we ran in a worktree (dirty main tree), cherry-pick the round's commit back
# to the main repo and clean up the worktree.
if [ -n "$WORKTREE_DIR" ]; then
  cd "$REPO" || { log "FATAL cannot cd back to $REPO for worktree cleanup"; exit 1; }
  SYNC_REF="main"   # back in the main tree, `main` is what's checked out again
  # The worktree now pushes its own commits (claim + round) straight to origin/main,
  # so the common case has NOTHING to rescue. Cherry-picking anyway would put a
  # duplicate commit on a `main` that is behind origin — which can then neither be
  # pushed (non-fast-forward) nor rebased away (the tree is dirty; that is why we
  # used a worktree at all), wedging every later tick. The stale `main` is harmless:
  # it fast-forwards on the next clean sync. Cherry-pick stays as the fallback for
  # when the push did NOT land (offline / lost race), so no round is ever lost.
  if git -C "$WORKTREE_DIR" merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
    log "worktree commits already on origin/main; main tree catches up on its next clean sync"
  else
    ROUND_COMMITS="$(git -C "$WORKTREE_DIR" rev-list --reverse origin/main..HEAD 2>/dev/null)"
    if [ -n "$ROUND_COMMITS" ] && git cherry-pick --quiet $ROUND_COMMITS >>"$LOG" 2>&1; then
      log "cherry-picked unpushed round commit(s) from worktree to main (origin push had not landed)"
      git_sync_up
    else
      git cherry-pick --abort 2>/dev/null
      log "WARN — cherry-pick from worktree conflicted or found no range; the round's commit is preserved by cleanup below (manual reconcile)"
    fi
  fi
  # Tags the round's commit first when it is on no other ref, and refuses to remove
  # the worktree if it cannot (lq-cb30d5d6 — this used to remove it unconditionally,
  # which made the "manual reconcile" promise above false).
  cleanup_worktree
fi

graduate_public
log "tick done — review with: git -C $REPO log --oneline -5"
exit 0
