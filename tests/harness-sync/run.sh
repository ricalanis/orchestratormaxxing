#!/usr/bin/env bash
# Real-path contract for harness-sync's queue-state autocommit. A throwaway repo
# with a local bare origin — real git, no network, no touching the real harness.
#
# The regression target is a human losing `harness-sync pull` to a file no human
# wrote (knowledge/*.jsonl, appended by the watchers), while every guard that
# protects HAND edits stays exactly as strict as before.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SYNC="$ROOT/bin/harness-sync"
[[ -x "$SYNC" ]] || { printf 'harness-sync: bin/harness-sync missing\n' >&2; exit 1; }

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/harness-sync.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
fail() { printf 'harness-sync contract: %s\n' "$*" >&2; exit 1; }

ORIGIN="$SCRATCH/origin.git"
REPO="$SCRATCH/repo"
git init -q --bare -b main "$ORIGIN"
git init -q -b main "$REPO"
cd "$REPO"
git config user.email harness@test.local
git config user.name 'Harness Test'
git config commit.gpgsign false
mkdir -p knowledge bin
printf '{"id":"lq-seed","flaw":"seed"}\n' > knowledge/loop-queue.jsonl
printf '{"id":"iq-seed"}\n' > knowledge/intent-queue.jsonl
printf 'echo baseline\n' > bin/tool.sh
git add -A
git commit -q -m 'baseline'
git remote add origin "$ORIGIN"
git push -q -u origin main

# C1: queue-only dirt is committed, published, and the pull completes.
printf '{"id":"lq-new","flaw":"watcher wrote this"}\n' >> knowledge/loop-queue.jsonl
out="$("$SYNC" pull 2>&1)" || fail "C1: pull refused over machine-written state:\n$out"
[[ "$out" == *"autocommitted machine state"* ]] || fail "C1: pull did not report the autocommit:\n$out"
[[ "$out" == *"knowledge/loop-queue.jsonl"* ]] || fail "C1: autocommit did not name the queue file"
[[ -z "$(git status --porcelain)" ]] || fail 'C1: tree still dirty after autocommit'
[[ "$(git rev-list --count origin/main..HEAD)" == "0" ]] || fail 'C1: autocommit was not published to origin'
git log -1 --format=%s | grep -F 'chore(state): autocommit' >/dev/null || fail 'C1: unexpected autocommit subject'

# C2: a hand edit to source still refuses, and is NOT swept into a commit.
printf 'echo edited by a human\n' >> bin/tool.sh
set +e
out="$("$SYNC" pull 2>&1)"; rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail 'C2: pull accepted a dirty source file'
[[ "$out" == *'working tree is dirty'* ]] || fail "C2: wrong refusal for a dirty source file:\n$out"
git status --porcelain | grep '^ M bin/tool.sh' >/dev/null || fail 'C2: the hand edit was committed or lost'

# C3: mixed dirt refuses too — the queue may be autocommitted, the source may not.
printf '{"id":"lq-mixed"}\n' >> knowledge/loop-queue.jsonl
set +e
out="$("$SYNC" pull 2>&1)"; rc=$?
set -e
[[ "$rc" -ne 0 && "$out" == *'working tree is dirty'* ]] || fail "C3: mixed dirt did not refuse:\n$out"
git status --porcelain | grep '^ M bin/tool.sh' >/dev/null || fail 'C3: hand edit lost while handling mixed dirt'
git checkout -q -- bin/tool.sh

# C4: a local SOURCE commit is still the operator's problem, never auto-published.
printf 'echo committed by hand\n' >> bin/tool.sh
git commit -q -am 'hand commit'
set +e
out="$("$SYNC" pull 2>&1)"; rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail 'C4: pull auto-published a source commit'
[[ "$out" == *'not on origin/main'* ]] || fail "C4: wrong refusal for an un-pushed source commit:\n$out"
git reset -q --hard origin/main

# C5: state-only ahead + behind → rebase and publish rather than call it diverged.
CLONE="$SCRATCH/clone"
git clone -q "$ORIGIN" "$CLONE"
git -C "$CLONE" config user.email other@test.local
git -C "$CLONE" config user.name 'Other Machine'
git -C "$CLONE" config commit.gpgsign false
printf 'echo from the other machine\n' >> "$CLONE/bin/tool.sh"
git -C "$CLONE" commit -q -am 'other machine work'
git -C "$CLONE" push -q origin main
printf '{"id":"lq-diverged"}\n' >> knowledge/loop-queue.jsonl
out="$("$SYNC" pull 2>&1)" || fail "C5: diverged queue state was not reconciled:\n$out"
[[ "$out" == *'synced main with origin/main'* ]] || fail "C5: pull did not report a completed sync:\n$out"
grep -q 'from the other machine' bin/tool.sh || fail 'C5: the other machine work never arrived'
[[ "$(git rev-list --count origin/main..HEAD)" == "0" ]] || fail 'C5: local queue commit was not published'

# C6: the escape hatch really disables it — proving C1 passes BECAUSE of the
# autocommit and not because the dirty guard went soft.
printf '{"id":"lq-optout"}\n' >> knowledge/loop-queue.jsonl
set +e
out="$(HARNESS_SYNC_AUTOCOMMIT=0 "$SYNC" pull 2>&1)"; rc=$?
set -e
[[ "$rc" -ne 0 && "$out" == *'working tree is dirty'* ]] || fail "C6: opt-out did not restore the strict guard:\n$out"

# C7: the standalone verb is honest about doing nothing.
"$SYNC" autocommit >/dev/null || fail 'C7: autocommit verb failed'
json="$(HARNESS_SYNC_AUTOCOMMIT=1 "$SYNC" autocommit --json)"
python3 - "$json" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
assert row["committed"] == [], row
PY
[[ -z "$(git status --porcelain)" ]] || fail 'C7: tree dirty after the verb ran'

# C8: no fleet.env, no legacy env → the standalone-client state. lan-check
# refuses (exit 2) naming the key in ONE line, status still answers, and neither
# transport is spawned — the fakes' log must stay empty.
FAKEBIN="$SCRATCH/fakebin"; EMPTYHOME="$SCRATCH/emptyhome"; CALLS="$SCRATCH/transport.log"
mkdir -p "$FAKEBIN" "$EMPTYHOME"
for t in tailscale ssh; do
  cat > "$FAKEBIN/$t" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$0 \$*" >> "$CALLS"
exit 1
EOF
  chmod +x "$FAKEBIN/$t"
done
: > "$CALLS"
fleet_env_cleared() {
  env -u HARNESS_LAN_PEER -u ORCHESTRATORMAXXING_LAN_PEER -u ORCHESTRATORMAXXING_FLEET_ENV \
      -u HARNESS_LAN_USER -u ORCHESTRATORMAXXING_SERVER_SSH -u HARNESS_LAN_IDENTITY \
      HOME="$1" PATH="$FAKEBIN:$PATH" "${@:2}"
}
set +e
out="$(fleet_env_cleared "$EMPTYHOME" "$SYNC" lan-check 2>&1)"; rc=$?
set -e
[[ "$rc" -eq 2 ]] || fail "C8: unconfigured lan-check exited $rc, want 2:\n$out"
[[ "$out" == *ORCHESTRATORMAXXING_LAN_PEER* ]] || fail "C8: refusal does not name ORCHESTRATORMAXXING_LAN_PEER:\n$out"
[[ "$out" != *$'\n'* ]] || fail "C8: refusal must be one line:\n$out"
out="$(fleet_env_cleared "$EMPTYHOME" "$SYNC" status 2>&1)" || fail "C8: unconfigured status failed:\n$out"
[[ "$out" == *'lan: not configured'* ]] || fail "C8: status did not report the LAN section as unconfigured:\n$out"
[[ ! -s "$CALLS" ]] || fail "C8: transport spawned while unconfigured:\n$(cat "$CALLS")"

# C9: fleet.env configures the peer and the fakes see exactly the old argv shape
# (user from the fleet server's user@host); legacy HARNESS_LAN_PEER still wins.
FLEETHOME="$SCRATCH/fleethome"; mkdir -p "$FLEETHOME/.config/orchestratormaxxing"
printf '# fleet identity\nexport ORCHESTRATORMAXXING_LAN_PEER="fleet-server.example"\nORCHESTRATORMAXXING_SERVER_SSH=fleet@fleet-server\n' \
  > "$FLEETHOME/.config/orchestratormaxxing/fleet.env"
: > "$CALLS"
set +e
out="$(fleet_env_cleared "$FLEETHOME" "$SYNC" lan-check 2>&1)"; rc=$?
set -e
[[ "$rc" -eq 1 ]] || fail "C9: configured lan-check with failing transports exited $rc, want 1:\n$out"
grep -q -- 'tailscale ping -c 1 fleet-server.example$' "$CALLS" || fail "C9: tailscale argv drifted:\n$(cat "$CALLS")"
grep -q -- 'ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new fleet@fleet-server.example true$' "$CALLS" \
  || fail "C9: ssh argv drifted:\n$(cat "$CALLS")"
: > "$CALLS"
set +e
fleet_env_cleared "$FLEETHOME" env HARNESS_LAN_PEER=legacy.example "$SYNC" lan-check >/dev/null 2>&1
set -e
grep -q -- ' fleet@legacy.example true$' "$CALLS" || fail "C9: legacy HARNESS_LAN_PEER did not outrank fleet.env:\n$(cat "$CALLS")"

printf 'harness-sync contract: PASS (C1-C9)\n'
