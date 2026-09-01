#!/usr/bin/env bash
# SOURCE OF TRUTH: repo template deploy/cogload-nightly.sh. install.sh copies this to
# ~/.config/claudemaxxing/cogload-nightly.sh (copy, NOT symlink). Edit here, re-run ./install.sh.
#
# Nightly maintenance so continuous cogload data survives without manual verbs.
# Order: cogload digest -> cogload transcripts --since 45 -> rsync the Claude transcript
# mirror -> cogload rotate.
#
# THE KILL SWITCH IS LOAD-BEARING AND IS THE MAIN POINT OF THIS CHUNK.
# `cogload off` writes a sentinel at $COGLOAD_DIR/DISABLED and disables the cogload-keys
# unit — but it does NOT stop this timer. If this script ran while disabled it would keep
# READING ~/.claude/projects and keep WRITING a ~1.7GB verbatim mirror, so "off" would not
# mean off. Therefore we check the sentinel FIRST and, if present, exit 0 immediately
# WITHOUT reading ~/.claude/projects and WITHOUT any rsync.
set -uo pipefail

COGLOAD_DIR="${COGLOAD_DIR:-$HOME/.local/share/cogload}"

# Kill switch — must be the very first thing. No reads of ~/.claude/projects, no rsync.
if [[ -f "$COGLOAD_DIR/DISABLED" ]]; then
  echo "cogload-nightly: kill switch on (DISABLED sentinel present) — skipping all maintenance."
  exit 0
fi

# systemd --user starts this with a minimal PATH that does NOT include ~/.local/bin, where
# install.sh deploys the `cogload` bridge. Without this every step would fail 127 nightly and
# the timer would look armed while silently doing nothing. Same discipline as the sibling
# hermes-strategic-brief.sh. Deliberately AFTER the kill switch: the sentinel check stays the
# first thing that can gate any work.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STORE="$COGLOAD_DIR"
PROJECTS_DIR="$HOME/.claude/projects"
MIRROR="$STORE/transcripts-mirror"

# Each step failing must not abort the remaining steps silently. Track per-step exit codes
# and exit non-zero if any failed. set -uo pipefail stays on for the rest of the script, but
# we deliberately do NOT use `set -e` — we want to run all steps and report.
declare -i rc_overall=0
run_step() {
  local label="$1"; shift
  # mktemp, not a PID-predictable path: a nightly root-adjacent job writing to a guessable
  # /tmp name is a symlink-clobber surface for anything else on the box.
  local out
  out="$(mktemp "${TMPDIR:-/tmp}/cogload-nightly.XXXXXX")" || {
    rc_overall=1
    echo "cogload-nightly: $label FAILED (could not create temp file)"
    return 0
  }
  "$@" >"$out" 2>&1
  local rc=$?
  if (( rc != 0 )); then
    rc_overall=1
    echo "cogload-nightly: $label FAILED (exit $rc):"
    sed 's/^/    /' "$out" 2>/dev/null || true
  else
    echo "cogload-nightly: $label OK (exit 0)"
  fi
  rm -f "$out"
  return 0
}

# 1. digest — fold per-minute rows into durable day rows.
run_step "digest" cogload digest

# 2. rsync the Claude transcript mirror FIRST. Ordering is load-bearing: step 3
#    reads a 45-day window, but upstream ~/.claude/projects only retains 30 days,
#    so anything older can ONLY come from the mirror. Aggregating before
#    refreshing would systematically read a stale mirror every night.
#    We deliberately do NOT pass the rsync delete flag: upstream's 30-day rolling
#    cleanup must never propagate into the mirror.
#    --update = skip files newer on the receiver (idempotent, non-destructive).
if [[ -d "$PROJECTS_DIR" ]]; then
  # 700, not the ambient umask: this mirror is a VERBATIM copy of agent
  # transcripts including prompt bodies. mkdir -p also creates the store root,
  # so an inherited 0755 would widen the whole behavioural store.
  (umask 077; mkdir -p "$MIRROR")
  # --exclude: on 2026-08-16 ~/.claude/projects/<slug>/memory became a SYMLINK
  # into the governed memory store, while this mirror still held the real
  # directory from an earlier sync. rsync cannot replace a non-empty directory
  # with a symlink, so it exited 23 and failed the whole nightly three nights
  # running (2026-08-16/17/18) while the diagnosis stayed invisible under
  # `journalctl --user -u cogload-nightly.service`. That path is git-tracked
  # governed state, not transcript data, so it does not belong in a behavioural
  # mirror at all — excluding it is the correct fix, not a workaround.
  # QUOTED deliberately: unquoted, /*/memory is a glob bash would expand against
  # the filesystem root the day something like /srv/memory exists, silently
  # turning the exclude into one wrong literal path.
  run_step "rsync-mirror" rsync -a --update --exclude='/*/memory' "$PROJECTS_DIR/" "$MIRROR/"
else
  # NOT a silent skip: a missing/unmounted projects dir is a real fault, and
  # reporting "all steps OK" for it is exactly the silent zero this harness
  # exists to prevent. Fail the run so the timer log shows it.
  echo "cogload-nightly: rsync-mirror FAILED — $PROJECTS_DIR does not exist" >&2
  rc_overall=1
fi

# 3. transcripts --since 45 — aggregate the agent corpus (45-day window).
run_step "transcripts" cogload transcripts --since 45

# 4. rotate — gzip old months, prune past keep-days. Refuses to prune any day that was not
#    digested, so this is lossless by construction.
run_step "rotate" cogload rotate

# 5. fleet push — spokes ship their DIGEST ROWS (never raw minutes, never the
#    transcript mirror) to the hub. On a hub or a solo box this prints
#    "not a spoke" and exits 0, so the script stays role-agnostic. The kill
#    switch already exited at the top, and push re-checks it — double-gated,
#    because `off` must stop the device TALKING about you, not just watching.
run_step "fleet-push" cogload fleet push

if (( rc_overall != 0 )); then
  echo "cogload-nightly: one or more steps failed (see above)."
  exit 1
fi
echo "cogload-nightly: all steps OK."
exit 0