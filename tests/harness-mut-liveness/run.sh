#!/usr/bin/env bash
# Contract for the mut-lock liveness skip on BEHAVIORAL contracts (lq-f8612b51).
#
# Why this exists: harness-verify's lock-liveness skip covered only the SYNTAX
# check (the tool loop), so a live bin/mut run on a tool still ran that tool's
# behavioral contract against whatever mutant was on disk. Observed 2026-08-19
# with an orphaned mut (pid 3381500) cycling bin/delegate-ledger: one full
# harness-verify pass was green (44/44) and the next red — the contract crashed
# inside the live mutant's traceback while the syntax check was correctly
# skipped. A verifier that flips per mutant manufactures phantom flaws, because
# loop-tick auto-enqueues its reds (signal-vs-artifact).
#
# It drives the REAL run_contract_entry / mut_lock_live_pid against fixture
# tools and contracts, with REAL pids (a live spawned process named `mut`, a
# spawned-and-reaped dead one, our own live non-mut python), so liveness AND
# identity cross the actual os.kill/ps boundaries. C2 is proven red against the
# pre-fix verifier (no run_contract_entry — legible C0 fails — and the pre-fix
# runner measured the real live-locked bin/delegate-ledger mutant as failed).
# C1/C3/C7 are the negative fixtures: an always-skip guard fails all three,
# and C7 (a live NON-mut pid) passed as `skipped` on the pre-hardening build —
# the recorded red proof for the critics' pid-recycling/forgery finding.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HV="${HARNESS_VERIFY_UNDER_TEST:-$ROOT/bin/harness-verify}"

[[ -f "$HV" ]] || { printf 'mut-liveness: harness-verify missing\n' >&2; exit 1; }

python3 - "$HV" <<'PY'
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile

hv_path = sys.argv[1]
spec = importlib.util.spec_from_loader(
    "hv_under_test", importlib.machinery.SourceFileLoader("hv_under_test", hv_path))
hv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hv)

fails = []
def check(cid, cond, msg):
    print(("  ok   " if cond else "  FAIL ") + f"{cid}  {msg}")
    if not cond:
        fails.append(f"{cid}: {msg}")

# Resolved defensively so the pre-fix verifier produces legible FAILs instead of
# an AttributeError crash — that is what makes the red proof readable.
live_pid_of = getattr(hv, "mut_lock_live_pid", None)
entry = getattr(hv, "run_contract_entry", None)
check("C0", callable(live_pid_of), "harness-verify exposes mut_lock_live_pid()")
check("C0b", callable(entry), "harness-verify exposes run_contract_entry()")
if not (callable(live_pid_of) and callable(entry)):
    print(f"mut-liveness: FAILED ({len(fails)} failure(s))")
    sys.exit(1)

root = tempfile.mkdtemp(prefix="mut-liveness-")
os.makedirs(os.path.join(root, "bin"))
os.makedirs(os.path.join(root, "tests", "fake"))
tool = os.path.join(root, "bin", "fake-tool")
open(tool, "w").write("#!/usr/bin/env python3\nprint('ok')\n")
contract = os.path.join(root, "tests", "fake", "run.sh")
# A RED contract on purpose: the mutant-crash shape this guard exists for.
open(contract, "w").write("#!/usr/bin/env bash\necho 'traceback inside a live mutant' >&2\nexit 1\n")
os.chmod(contract, os.stat(contract).st_mode | stat.S_IEXEC)
lock = tool + ".mut-lock"

# A REAL live process whose argv names `mut` (basename-exact): liveness AND
# identity both cross the real boundary — os.kill and ps, no monkeypatching.
# Own session + DEVNULL stdio: bash may FORK its sleep rather than exec it, and
# a forked sleep inheriting this contract's pipes outlives its parent's
# terminate() and holds run_behavioral_contract's communicate() open past the
# deadline — measured as `inconclusive` inside a full pass while green
# standalone (where no pipes are captured). No `exec sleep` instead: that
# would rewrite the argv this fixture exists to have ps observe.
fake_mut = os.path.join(root, "mut")
open(fake_mut, "w").write("#!/bin/bash\nsleep 300\n")
os.chmod(fake_mut, os.stat(fake_mut).st_mode | stat.S_IEXEC)
mut_proc = subprocess.Popen([fake_mut], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)

def dispatch():
    errs, warns = [], []
    result = entry(root, "fake", "tests/fake/run.sh", "bin/fake-tool", 10,
                   lambda w, m: errs.append((w, m)),
                   lambda w, m: warns.append((w, m)))
    return result, errs, warns

# ── C1: NEGATIVE FIXTURE — with no lock, the red contract stays a measured red ─
# An always-skip guard passes C2 while hiding every real red; this catches it.
r, errs, warns = dispatch()
check("C1", r is not None and r["outcome"] == "failed" and len(errs) == 1 and not warns,
      f"no lock -> contract runs and errors (outcome={r and r['outcome']}, err={errs}, warn={warns})")

# ── C2: a LIVE mut lock skips the behavioral contract — warn, never err ──────
# Proven red pre-fix: the dispatch ran the contract and recorded the error.
open(lock, "w").write(str(mut_proc.pid))
r, errs, warns = dispatch()
check("C2", r is not None and r["outcome"] == "skipped",
      f"live mut lock -> outcome skipped (got {r and r['outcome']})")
check("C2b", not errs and len(warns) == 1,
      f"live mut lock -> warn, never err (err={errs}, warn={warns})")
check("C2c", warns and "mut run in progress" in warns[0][1] and str(mut_proc.pid) in warns[0][1],
      f"skip names the live mut pid (got {warns})")
check("C2d", warns and "bin/fake-tool" in warns[0][1],
      f"skip names the tool under mutation (got {warns})")

# ── C2e: the skip is COUNTED — a contract that stopped being measured is visible ─
counts = hv.contract_outcome_counts([r])
check("C2e", counts.get("skipped") == 1,
      f"mut-liveness skip lands in the skipped count (got {counts})")

# ── C2f: the result stays ADDRESSABLE — where/path ride every outcome ─────────
# The JSON surface keys on these; a skip result missing them is anonymous.
# Real mut survivors until pinned (both `result[...] = ...` lines deleted clean).
check("C2f", r.get("where") == "bin/fake-tool" and r.get("path") == "tests/fake/run.sh",
      f"skip result carries where+path (got where={r.get('where')!r}, path={r.get('path')!r})")

# ── C3: NEGATIVE — a DEAD pid never excuses measurement ──────────────────────
# mut cleans stale locks itself; honouring one would let a crashed run blind
# the verifier forever. Real boundary: a real spawned-and-reaped child pid.
child = subprocess.Popen(["true"])
child.wait()
open(lock, "w").write(str(child.pid))
r, errs, warns = dispatch()
check("C3", r is not None and r["outcome"] == "failed" and len(errs) == 1,
      f"dead-pid lock -> contract still measured (outcome={r and r['outcome']}, err={errs})")
check("C3b", live_pid_of(tool) == 0, "helper reports 0 for a dead pid")

# ── C4: garbage lock content never excuses measurement ───────────────────────
open(lock, "w").write("not-a-pid\n")
r, errs, warns = dispatch()
check("C4", r is not None and r["outcome"] == "failed",
      f"garbage lock -> contract still measured (got {r and r['outcome']})")

# ── C7: NEGATIVE — a live pid that is NOT mut never excuses measurement ──────
# Raised independently by BOTH cross-family critics (deepseek-v4-pro,
# mistral-large-3): os.kill(pid,0) proves A process lives, not that MUT lives,
# so a recycled or forged pid would blind the verifier indefinitely. This exact
# fixture (our own python pid, argv `python3 -`) was honoured as `skipped` by
# the pre-hardening build — that pass is the recorded red proof.
open(lock, "w").write(str(os.getpid()))
r, errs, warns = dispatch()
check("C7", r is not None and r["outcome"] == "failed" and len(errs) == 1,
      f"live NON-mut pid -> contract still measured (outcome={r and r['outcome']}, err={errs})")
check("C7b", live_pid_of(tool) == 0, "helper reports 0 for a live non-mut pid")

# ── C5: a missing contract FILE stays an error even under a live mut lock ────
# A structural gap must not be masked by a transient one.
open(lock, "w").write(str(mut_proc.pid))
errs, warns = [], []
r = entry(root, "fake", "tests/does-not-exist/run.sh", "bin/fake-tool", 10,
          lambda w, m: errs.append((w, m)), lambda w, m: warns.append((w, m)))
check("C5", r is None and len(errs) == 1 and "missing" in errs[0][1],
      f"missing contract under live lock still errors (r={r}, err={errs})")

# ── C6: the helper's own edges ────────────────────────────────────────────────
os.remove(lock)
check("C6", live_pid_of(tool) == 0, "no lock -> 0")
open(lock, "w").write(str(mut_proc.pid))
check("C6b", live_pid_of(tool) == mut_proc.pid, "live mut pid -> that pid")
open(lock, "w").write("")
check("C6c", live_pid_of(tool) == 0, "empty lock -> 0")

# ── C8..C12: FUNCTION-based contracts get the same skip (lq-5b64d4bf) ────────
# run_contract_entry covers only the tuple-registered contracts; the in-process
# function contracts (loop_tools_behavior_ok and friends) hardcoded their tools
# and bypassed the skip, so a live mut run on e.g. bin/loop-queue still flipped
# their verdicts per mutant. Resolved defensively like C0 so the pre-fix
# verifier fails legibly (C8/C12 red) instead of crashing.
fnc = getattr(hv, "run_function_contract", None)
check("C8", callable(fnc), "harness-verify exposes run_function_contract()")
if callable(fnc):
    calls = []
    def fake_fn(r):
        calls.append(r)
        return ["red-msg"]

    def fdispatch(tools):
        errs, warns = [], []
        res = fnc(root, "fake-fn", fake_fn, "bin/fake-tool", tools,
                  lambda w, m: errs.append((w, m)),
                  lambda w, m: warns.append((w, m)))
        return res, errs, warns

    # C9 NEGATIVE: no lock -> the function runs and its red reaches err.
    # An always-skip dispatch passes C10 while hiding every real red.
    if os.path.exists(lock):
        os.remove(lock)
    res, errs, warns = fdispatch(("bin/fake-tool",))
    check("C9", res is None and len(calls) == 1 and len(errs) == 1 and not warns
          and errs[0][1] == "behavioral contract failed: red-msg",
          f"no lock -> function measured (calls={len(calls)}, err={errs}, warn={warns})")

    # C10: a live mut lock skips WITHOUT invoking the function — warn, never err.
    open(lock, "w").write(str(mut_proc.pid))
    calls.clear()
    res, errs, warns = fdispatch(("bin/fake-tool",))
    check("C10", res is not None and res["outcome"] == "skipped" and not calls
          and not errs and len(warns) == 1,
          f"live lock -> skipped, fn NOT invoked (calls={len(calls)}, err={errs}, warn={warns})")
    check("C10b", bool(res) and res.get("where") == "bin/fake-tool"
          and res.get("path") == "bin/fake-tool"
          and hv.contract_outcome_counts([res]).get("skipped") == 1,
          f"fn-contract skip is addressable + counted (got {res})")

    # C10c: the lock may sit on a TRANSITIVE listed tool (loop-tick shells out
    # to loop-queue) — any listed tool under live mutation skips the contract.
    calls.clear()
    res, errs, warns = fdispatch(("bin/unlocked-tool", "bin/fake-tool"))
    check("C10c", res is not None and res["outcome"] == "skipped" and not calls,
          f"live lock on a transitive listed tool skips (got {res and res['outcome']})")

    # C10d NEGATIVE (cross-family critic finding): a live lock on an UNLISTED
    # tool must NOT skip — the skip is scoped to the tools the contract drives,
    # never a repo-wide excuse. The live lock still sits on bin/fake-tool.
    calls.clear()
    res, errs, warns = fdispatch(("bin/unrelated-tool",))
    check("C10d", res is None and len(calls) == 1 and len(errs) == 1 and not warns,
          f"live lock on an UNLISTED tool -> contract still measured (calls={len(calls)}, err={errs}, warn={warns})")

    # C11 NEGATIVE: a dead pid never excuses measurement of a function contract.
    open(lock, "w").write(str(child.pid))
    calls.clear()
    res, errs, warns = fdispatch(("bin/fake-tool",))
    check("C11", res is None and len(calls) == 1 and len(errs) == 1,
          f"dead-pid lock -> function still measured (calls={len(calls)}, err={errs})")
    os.remove(lock)

# C12: the WIRING — audit() must route every function contract through the
# helper; a bare `for msg in <fn>(root)` dispatch bypasses the skip again.
src = open(hv_path).read()
bare = [name for name in (
    "loop_tools_behavior_ok", "loop_queue_ttl_ok", "intent_queue_behavior_ok",
    "session_log_behavior_ok", "loop_tick_persistence_ok",
    "loop_tick_backpressure_ok", "loop_tick_ttl_wired_ok",
    "tmux_send_transport_ok", "codex_g_behavior_ok", "opencode_o_behavior_ok",
    "shared_memory_behavior_ok") if f"for msg in {name}(" in src]
check("C12", not bare, f"no function contract is dispatched bare (bare={bare})")

import signal
try:
    os.killpg(mut_proc.pid, signal.SIGTERM)
except ProcessLookupError:
    pass
mut_proc.wait()
print(f"mut-liveness: {'FAILED' if fails else 'PASSED'} ({len(fails)} failure(s))")
sys.exit(1 if fails else 0)
PY
