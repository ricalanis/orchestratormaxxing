#!/usr/bin/env bash
# Contract: every command driven by bin/mut is a bounded process tree.
#
# Real boundary cases:
#   C1 timeout kills a TERM-ignoring descendant, not only the shell leader
#   C2 a successful leader cannot leave a background descendant behind
#   C3 SIGTERM of mut cleans the active contract before restoring the source
#   C4 cleanup never reaches an unrelated process outside the contract group
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MUT="${MUT_UNDER_TEST:-$ROOT/bin/mut}"
SCRATCH="$(mktemp -d)"
HELPER="$SCRATCH/helper.py"
SRC="$SCRATCH/target.py"
ORIGINAL="$SCRATCH/target.original"
SENTINEL=""
MUT_PID=""
RECEIPTS=()

cleanup() {
  set +e
  if [[ -n "$MUT_PID" ]]; then
    kill -TERM "$MUT_PID" 2>/dev/null
    sleep 0.1
    kill -KILL "$MUT_PID" 2>/dev/null
    wait "$MUT_PID" 2>/dev/null
  fi
  local receipt pid pgid
  for receipt in "${RECEIPTS[@]}"; do
    if [[ -s "$receipt" ]]; then
      read -r pid pgid < "$receipt"
      kill -TERM "$pid" 2>/dev/null
      sleep 0.05
      kill -KILL "$pid" 2>/dev/null
    fi
  done
  if [[ -n "$SENTINEL" ]]; then
    kill -TERM "$SENTINEL" 2>/dev/null
    wait "$SENTINEL" 2>/dev/null
  fi
  rm -rf "$SCRATCH"
}
trap cleanup EXIT INT TERM HUP

fail() {
  printf 'mut-process-cleanup: %s\n' "$*" >&2
  exit 1
}

cat > "$SRC" <<'PY'
def enabled(value):
    return value > 2
PY
cp "$SRC" "$ORIGINAL"
chmod 755 "$SRC"

cat > "$HELPER" <<'PY'
import os
import signal
import subprocess
import sys
import time


def receipt(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {os.getpgid(0)}\n")
        handle.flush()
        os.fsync(handle.fileno())


def linger(path):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    receipt(path)
    while True:
        time.sleep(1)


def graceful(path, marker):
    def stop(_signum, _frame):
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("TERM\n")
            handle.flush()
            os.fsync(handle.fileno())
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    receipt(path)
    while True:
        time.sleep(1)


mode, path, *rest = sys.argv[1:]
if mode == "hang":
    linger(path)
elif mode == "background":
    marker = rest[0]
    child = subprocess.Popen(
        [sys.executable, __file__, "graceful", path, marker],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 2
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)
    if not os.path.exists(path):
        child.kill()
        raise SystemExit("background child never wrote its receipt")
elif mode == "linger":
    linger(path)
elif mode == "graceful":
    graceful(path, rest[0])
else:
    raise SystemExit(f"unknown mode: {mode}")
PY

wait_receipt() {
  local receipt="$1"
  local _wait_round
  for _wait_round in $(seq 1 200); do
    [[ -s "$receipt" ]] && return 0
    sleep 0.01
  done
  fail "no child receipt at $receipt"
}

pid_alive() {
  kill -0 "$1" 2>/dev/null
}

group_alive() {
  python3 - "$1" <<'PY'
import os, sys
try:
    os.killpg(int(sys.argv[1]), 0)
except ProcessLookupError:
    raise SystemExit(1)
except PermissionError:
    pass
PY
}

assert_gone() {
  local receipt="$1" label="$2"
  local pid pgid _probe_round
  read -r pid pgid < "$receipt"
  for _probe_round in $(seq 1 200); do
    if ! pid_alive "$pid" && ! group_alive "$pgid"; then
      return 0
    fi
    sleep 0.01
  done
  fail "$label leaked pid=$pid pgid=$pgid"
}

assert_sentinel() {
  pid_alive "$SENTINEL" || fail "$1 killed the unrelated sentinel"
}

# A process outside every mut-created session. If cleanup uses broad PID/name
# matching or mut's own process group, this canary dies.
sleep 120 &
SENTINEL=$!

# C1 — Timeout. The helper ignores TERM, forcing the TERM->KILL escalation.
timeout_receipt="$SCRATCH/timeout.receipt"
RECEIPTS+=("$timeout_receipt")
"$MUT" --src "$SRC" \
  --test "python3 '$HELPER' hang '$timeout_receipt'" \
  --max-mutants 1 --timeout 0.20 --threshold 0 >/dev/null 2>&1
wait_receipt "$timeout_receipt"
assert_gone "$timeout_receipt" "C1 timeout"
assert_sentinel C1

# C2 — The shell leader exits 0 after its child closes inherited stdio. The
# mutant must still be scored as survived, but its residual group must not live.
background_receipt="$SCRATCH/background.receipt"
background_term="$SCRATCH/background.term"
RECEIPTS+=("$background_receipt")
json_out="$SCRATCH/background.json"
"$MUT" --src "$SRC" \
  --test "python3 '$HELPER' background '$background_receipt' '$background_term'" \
  --max-mutants 1 --timeout 2 --threshold 0 --json > "$json_out" 2>/dev/null
wait_receipt "$background_receipt"
python3 - "$json_out" <<'PY' || fail "C2 changed the successful mutant verdict"
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["valid"] == 1, data
assert data["survived"] == 1, data
PY
if [[ "$(uname -s)" != Darwin ]]; then
  [[ -s "$background_term" ]] || fail "C2 skipped graceful TERM before escalation"
fi
assert_gone "$background_receipt" "C2 normal leader exit"
assert_sentinel C2

# C2b — Containment must not collapse the original verdict protocol. A real
# nonzero contract remains killed; only the residual group handling changed.
nonzero_json="$SCRATCH/nonzero.json"
"$MUT" --src "$SRC" --test "exit 7" \
  --max-mutants 1 --timeout 2 --threshold 0 --json > "$nonzero_json" 2>/dev/null
python3 - "$nonzero_json" <<'PY' || fail "C2b changed the nonzero mutant verdict"
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["valid"] == 1, data
assert data["killed"] == 1, data
assert data["survived"] == 0, data
PY
assert_sentinel C2b

# C3 — Interrupt mut while its contract is live. Cleanup must precede source
# restoration and sidecar removal on this path too.
interrupt_receipt="$SCRATCH/interrupt.receipt"
RECEIPTS+=("$interrupt_receipt")
"$MUT" --src "$SRC" \
  --test "python3 '$HELPER' hang '$interrupt_receipt'" \
  --max-mutants 1 --timeout 20 --threshold 0 >/dev/null 2>&1 &
MUT_PID=$!
wait_receipt "$interrupt_receipt"
[[ -e "$SRC.mut-lock" && -e "$SRC.mut-orig" ]] \
  || fail "C3 never observed an active mutation"
kill -TERM "$MUT_PID"
set +e
wait "$MUT_PID"
interrupt_rc=$?
set -e
MUT_PID=""
[[ "$interrupt_rc" -ne 0 ]] || fail "C3 interrupted mut exited successfully"
assert_gone "$interrupt_receipt" "C3 interrupted run"
cmp -s "$SRC" "$ORIGINAL" || fail "C3 did not restore source byte-for-byte"
mode="$(stat -c '%a' "$SRC" 2>/dev/null || stat -f '%Lp' "$SRC")"
[[ "$mode" == 755 ]] || fail "C3 restored source mode $mode, expected 755"
[[ ! -e "$SRC.mut-lock" && ! -e "$SRC.mut-orig" && ! -e "$SRC.mut-orig.tmp" ]] \
  || fail "C3 left mutation sidecars"
assert_sentinel C3

# C5 — An unprovable cleanup is not a score. Drive both function and main
# boundaries without manufacturing an actually-unkillable kernel process.
python3 - "$MUT" "$SCRATCH/fail-closed.py" <<'PY' \
  || fail "C5 cleanup failure did not refuse measurement"
import runpy
import sys

tool, src = sys.argv[1:]
ns = runpy.run_path(tool)
g = ns["run_test"].__globals__

class FakeProc:
    pid = 424242
    returncode = 0
    def communicate(self, timeout=None):
        return b"", b""

fake = FakeProc()
real_spawn = g["_spawn_contract"]
real_terminate = g["_terminate_contract"]
real_terminate_active = g["_terminate_active_contracts"]
g["_spawn_contract"] = lambda _cmd, _env: fake
g["_terminate_contract"] = lambda _proc: False
try:
    ns["run_test"]("true", 1)
except g["ContractCleanupError"]:
    pass
else:
    raise AssertionError("run_test accepted an unproven cleanup")

# If a handled signal lands exactly as spawn unmasks it, run_test has not yet
# received the proc local. The registry must already own the group, and the
# proc=None finally must drive registry cleanup.
g["_spawn_contract"] = lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt())
called = []
g["_terminate_active_contracts"] = lambda: called.append(True) or []
try:
    g["run_test"]("true", 1)
except KeyboardInterrupt:
    pass
else:
    raise AssertionError("spawn interruption did not propagate")
if called != [True]:
    raise AssertionError("spawn interruption skipped active-group cleanup")

# The registration itself precedes unmasking. Simulate a pending signal being
# delivered by pthread_sigmask(SIG_SETMASK) after Popen returned.
real_popen = g["subprocess"].Popen
real_sigmask = g["signal"].pthread_sigmask
g["subprocess"].Popen = lambda *_a, **_kw: fake
def interrupt_on_unmask(how, _mask):
    if how == g["signal"].SIG_BLOCK:
        return set()
    raise KeyboardInterrupt
g["signal"].pthread_sigmask = interrupt_on_unmask
g["_ACTIVE_CONTRACTS"].clear()
try:
    real_spawn("true", {})
except KeyboardInterrupt:
    pass
else:
    raise AssertionError("unmask fixture delivered no interrupt")
finally:
    g["subprocess"].Popen = real_popen
    g["signal"].pthread_sigmask = real_sigmask
if g["_ACTIVE_CONTRACTS"].get(fake.pid) is not fake:
    raise AssertionError("spawn unmasked before registering the contract group")

# Both kinds of unreadable cleanup stay false: a direct leader that cannot be
# reaped, and a group that survives KILL. No real unkillable process is needed.
class Unreaped(FakeProc):
    def wait(self, timeout=None):
        raise g["subprocess"].TimeoutExpired("fixture", timeout)
g["_ACTIVE_CONTRACTS"].clear()
real_group_alive = g["_group_alive"]
g["_group_alive"] = lambda _pgid: False
if real_terminate(Unreaped()) is not False:
    raise AssertionError("an unreaped leader was reported clean")
g["_group_alive"] = real_group_alive

# A post-cleanup pipe that still cannot drain contradicts "group gone" and must
# refuse the score rather than carrying a plausible verdict forward.
class Undrained(FakeProc):
    calls = 0
    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            return b"", b""
        raise g["subprocess"].TimeoutExpired("fixture", timeout)
undrained = Undrained()
g["_spawn_contract"] = lambda *_a: undrained
g["_terminate_contract"] = lambda _proc: True
try:
    g["run_test"]("true", 1)
except g["ContractCleanupError"]:
    pass
else:
    raise AssertionError("an undrained contract pipe was reported clean")

g["_spawn_contract"] = real_spawn
g["_terminate_contract"] = real_terminate
g["_terminate_active_contracts"] = real_terminate_active
g["_ACTIVE_CONTRACTS"].clear()
g["_ACTIVE_CONTRACTS"][fake.pid] = fake
g["_terminate_contract"] = lambda _proc: False
if real_terminate_active() != [fake.pid]:
    raise AssertionError("active-group cleanup lost its failed PGID")
g["_ACTIVE_CONTRACTS"].clear()
g["_terminate_contract"] = real_terminate

with open(src, "w", encoding="utf-8") as handle:
    handle.write("def f(x):\n    return x > 2\n")
g["run_test"] = lambda *_a, **_kw: (_ for _ in ()).throw(
    g["ContractCleanupError"]("fixture cleanup failure")
)
sys.argv = [tool, "--src", src, "--test", "true", "--max-mutants", "1"]
try:
    ns["main"]()
except SystemExit as exc:
    if exc.code != 2:
        raise AssertionError(f"main cleanup refusal exited {exc.code}, expected 2")
else:
    raise AssertionError("main continued after cleanup failure")
PY

echo "mut-process-cleanup: C1-C5 pass"
