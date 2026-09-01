#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# orchestratormaxxing — monthly DOCTRINE CONTRADICTION AUDIT seeder.
#
# SOURCE OF TRUTH: this file is a TEMPLATE in the repo
# (deploy/orchestratormaxxing-doctrine-audit.sh). The live copy at
# $HOME/.config/orchestratormaxxing/orchestratormaxxing-doctrine-audit.sh is deployed by
# install.sh (copy, NOT symlink — __REPO__ substituted at deploy time). Edit
# here, then re-run ./install.sh.
#
# WHY THIS EXISTS. The instruction surfaces of this harness — the repo
# CLAUDE.md, the global ~/.claude/CLAUDE.md, .claude/commands/*.md,
# .claude/agents/*.md and skills/*/SKILL.md — are written at different times by
# different rounds, and nothing checks them against each other. Anthropic's own
# July 2026 finding was that layered instructions had become "minefields of
# conflicting guidance"; the Second Movement integration research (2026-08-09)
# deferred a periodic contradiction audit as a candidate for a later
# /self-improve round. This is that seeder.
#
# WHAT IT DOES: enqueues ONE loop-queue item and exits. That is the whole job.
#
# WHAT IT DELIBERATELY DOES NOT DO: it never invokes an agent, never reads a
# transcript, never judges a contradiction, never edits doctrine. Chapter 18 of
# Orchestra of One says to name the cap on every armed loop before arming it, so
# this one's caps are physical rather than prose:
#
#   • FIRING CAP — OnCalendar=monthly in the sibling .timer. Once a month.
#   • QUEUE CAP  — at most ONE open audit item at a time. loop-queue add is
#                  idempotent by content hash, but idempotency alone would
#                  re-stamp a RESOLVED item as `recurred` and let it jump the
#                  queue on false evidence, so this refuses explicitly when an
#                  audit item is already open.
#   • ROUND CAP  — the round it eventually triggers runs under harness-agent-run
#                  (MAX_TURNS, HARNESS_TIMEOUT_SECONDS), like every other round.
#   • FINDINGS CAP — the MINE procedure caps enqueued contradictions at
#                  DOCTRINE_AUDIT_MAX_FINDINGS (3) per round, so one audit can
#                  never flood the daily drain.
#   • GATE       — loop-queue status --gate, then harness-verify. Unattended
#                  rounds stop at SELECT for doctrine, so no contradiction is
#                  ever "fixed" without Ricardo.
#
# Deploying this file is always safe; ARMING the monthly timer is opt-in
# (SELECT), same convention as the daily loop.
set -uo pipefail

REPO="__REPO__"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
LOG_DIR="$HOME/.config/orchestratormaxxing"
LOG="$LOG_DIR/doctrine-audit.log"
mkdir -p "$LOG_DIR"

# The findings ceiling the queued round must honour. Kept here, beside the other
# caps, so the whole cap set for this loop reads in one place.
DOCTRINE_AUDIT_MAX_FINDINGS="${DOCTRINE_AUDIT_MAX_FINDINGS:-3}"

# Repo-scoped tools resolve their queue path from the working directory and fail
# OPEN when it isn't the repo: systemd --user starts this in $HOME, `git
# rev-parse` fails there, and loop-queue's cwd fallback writes a
# plausible-looking queue OUTSIDE the repo that no gate or human ever reads
# (observed live 2026-08-10 — three load-bearing SELECT intents stranded for
# three weeks). Same discipline as the sibling loop-cron.sh: cd first, refuse
# otherwise.
cd "$REPO" || { echo "FATAL cannot cd $REPO — refusing to run (the queue would land outside the repo)" >> "$LOG"; exit 1; }

LOOP_QUEUE="$(command -v loop-queue || echo "$REPO/bin/loop-queue")"

FLAW="doctrine contradiction audit (monthly): reconcile CLAUDE.md, ~/.claude/CLAUDE.md, .claude/commands/*.md, .claude/agents/*.md and skills/*/SKILL.md against each other. Procedure + caps: knowledge/doctrine-audit-2026-08-16.md. Enqueue at most ${DOCTRINE_AUDIT_MAX_FINDINGS} contradictions; doctrine edits stop at SELECT."

{
  echo "===== doctrine audit seeder $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="

  # QUEUE CAP, enforced before the add rather than trusted to idempotency.
  if "$LOOP_QUEUE" list --status open --json 2>/dev/null \
     | grep -q '"source": *"doctrine-audit"'; then
    echo "SKIP — an audit item is already open; one at a time is the cap."
  else
    "$LOOP_QUEUE" add "$FLAW" --layer G --source doctrine-audit 2>&1 \
      || echo "(loop-queue add failed)"
  fi
  echo
} >> "$LOG" 2>&1

# Keep the log bounded — a monthly cadence, small forever.
if [ -f "$LOG" ]; then
  tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
