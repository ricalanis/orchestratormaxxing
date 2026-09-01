#!/usr/bin/env python3
"""queue-concurrency — consume-once contract for loop-queue AND intent-queue.

Hermetic (throwaway LOOP_QUEUE_DIR/INTENT_QUEUE_DIR, never the real queues),
black-box through each tool's CLI so the main() lock path is what's exercised.

Contracts (all deterministic under the fix — the flock serializes ANY schedule):
  C1  8 concurrent adds with distinct flaws → exactly 8 items survive
  C2  claim is CAS: second sequential claim exits nonzero; 4 concurrent claims
      on one open item → exactly one exit 0
  C3  reopen stamps reopened_at + reopen_count; status --json exposes totals
  C4  add --repro round-trips through list --json (provenance string, and the
      tool never executes it — asserted by using a command that would leave a
      marker file if run)
  C5  bounded lock wait: with the sidecar lock held and LOCK_TIMEOUT=1, a
      mutating verb exits 1 within the deadline instead of hanging
  C6  intent-queue: C1 + C2-sequential + C5 (same consume-once contract)

Red evidence (2026-08-09, scratchpad red_proof_t1.py against the pre-fix
`git show HEAD:bin/loop-queue`): barrier-synchronized adds lost 1 of 2 items,
a second claim exited 0, reopen left no trace — all three deterministic, per
Sol finding 2 (never launch-and-hope).
"""
import concurrent.futures as cf
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LQ = os.path.join(ROOT, "bin", "loop-queue")
IQ = os.path.join(ROOT, "bin", "intent-queue")
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILS.append(f"{name}: {detail}")


def run(tool, env, *args, timeout=30):
    return subprocess.run([sys.executable, tool, *args], capture_output=True,
                          text=True, env=env, timeout=timeout)


def items_of(tool, env, status="all"):
    return json.loads(run(tool, env, "list", "--status", status, "--json").stdout or "[]")


def lq_env(tmp):
    return dict(os.environ, LOOP_QUEUE_DIR=tmp, LOOP_QUEUE_TS="2026-01-01T00:00:00Z")


def iq_env(tmp):
    return dict(os.environ, INTENT_QUEUE_DIR=tmp, INTENT_QUEUE_TS="2026-01-01T00:00:00Z")


def c1_concurrent_adds(tool, envf, addargs):
    # deliberately a NONEXISTENT subdir: the lock path must create the queue
    # dir itself (fresh-clone case), not assume a prior writer did
    tmp = os.path.join(tempfile.mkdtemp(prefix="qc-c1-"), "fresh")
    env = envf(tmp)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        rcs = list(ex.map(
            lambda i: run(tool, env, "add", *addargs, f"concurrent flaw {i}").returncode,
            range(8)))
    n = len(items_of(tool, env))
    check(f"C1 {os.path.basename(tool)}: 8 concurrent adds all survive",
          rcs == [0] * 8 and n == 8, f"rcs={rcs} survived={n}/8")


def c2_claim_cas(tool, envf, addargs):
    tmp = tempfile.mkdtemp(prefix="qc-c2-")
    env = envf(tmp)
    run(tool, env, "add", *addargs, "cas target")
    iid = items_of(tool, env, "open")[0]["id"]
    first = run(tool, env, "claim", iid).returncode
    second = run(tool, env, "claim", iid).returncode
    check(f"C2 {os.path.basename(tool)}: sequential double-claim has one winner",
          first == 0 and second != 0, f"first={first} second={second}")
    ghost = run(tool, env, "claim", "no-such-id")
    check(f"C2 {os.path.basename(tool)}: claim on a missing id refuses cleanly",
          ghost.returncode != 0 and "Traceback" not in ghost.stderr,
          f"rc={ghost.returncode} stderr={ghost.stderr.strip()[:80]}")
    if tool != LQ:
        return
    tmp = tempfile.mkdtemp(prefix="qc-c2b-")
    env = envf(tmp)
    run(tool, env, "add", *addargs, "concurrent cas target")
    iid = items_of(tool, env, "open")[0]["id"]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        rcs = list(ex.map(lambda _: run(tool, env, "claim", iid).returncode, range(4)))
    check("C2 loop-queue: 4 concurrent claims → exactly one winner",
          rcs.count(0) == 1, f"rcs={rcs}")


def c3_reopen_stamps():
    tmp = tempfile.mkdtemp(prefix="qc-c3-")
    env = lq_env(tmp)
    run(LQ, env, "add", "--layer", "V", "--source", "manual", "reopen stamp target")
    iid = items_of(LQ, env, "open")[0]["id"]
    run(LQ, env, "resolve", iid)
    run(LQ, env, "reopen", iid)
    run(LQ, env, "resolve", iid)
    run(LQ, env, "reopen", iid)
    # a second, never-reopened item must contribute ZERO to the totals
    run(LQ, env, "add", "--layer", "V", "--source", "manual", "never reopened bystander")
    items = items_of(LQ, env, "open")
    it = next(x for x in items if x.get("reopen_count"))
    st_run = run(LQ, env, "status", "--json")
    st = json.loads(st_run.stdout)
    check("C3 reopen stamps reopened_at + reopen_count",
          it.get("reopened_at") == "2026-01-01T00:00:00Z" and it.get("reopen_count") == 2,
          f"item={it}")
    check("C3 status --json exposes reopen totals (bystanders contribute zero)",
          st.get("reopened_items") == 1 and st.get("reopen_events") == 2
          and st_run.returncode == 0, f"status={st} rc={st_run.returncode}")


def c4_repro_provenance():
    tmp = tempfile.mkdtemp(prefix="qc-c4-")
    env = lq_env(tmp)
    marker = os.path.join(tmp, "EXECUTED")
    run(LQ, env, "add", "--layer", "V", "--source", "manual",
        "repro target", "--repro", f"touch {marker}")
    it = items_of(LQ, env, "open")[0]
    check("C4 --repro round-trips through list --json",
          it.get("repro") == f"touch {marker}", f"item={it}")
    check("C4 repro is NEVER executed by the tool", not os.path.exists(marker))


def c5_bounded_lock(tool, envf, addargs, timeout_var):
    tmp = tempfile.mkdtemp(prefix="qc-c5-")
    env = envf(tmp)
    run(tool, env, "add", *addargs, "seed so the dir exists")
    qfile = [f for f in os.listdir(tmp) if f.endswith(".jsonl")][0]
    holder = open(os.path.join(tmp, qfile + ".lock"), "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    env[timeout_var] = "1"
    t0 = time.monotonic()
    r = run(tool, env, "add", *addargs, "should be refused while locked", timeout=15)
    dt = time.monotonic() - t0
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    check(f"C5 {os.path.basename(tool)}: lock-busy mutating verb exits 1 within deadline",
          r.returncode == 1 and dt < 10 and "lock busy" in r.stderr
          and "Traceback" not in r.stderr,  # a CLEAN refusal, not a crash that
          f"rc={r.returncode} dt={dt:.1f}s stderr={r.stderr.strip()[:80]}")  # happens to exit 1


def main():
    lq_args = ("--layer", "V", "--source", "manual")
    iq_args = ("--kind", "manual", "--source", "manual")
    print("loop-queue:")
    c1_concurrent_adds(LQ, lq_env, lq_args)
    c2_claim_cas(LQ, lq_env, lq_args)
    c3_reopen_stamps()
    c4_repro_provenance()
    c5_bounded_lock(LQ, lq_env, lq_args, "LOOP_QUEUE_LOCK_TIMEOUT")
    print("intent-queue:")
    c1_concurrent_adds(IQ, iq_env, iq_args)
    c2_claim_cas(IQ, iq_env, iq_args)
    c5_bounded_lock(IQ, iq_env, iq_args, "INTENT_QUEUE_LOCK_TIMEOUT")
    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S)")
        for f in FAILS:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("\nall queue-concurrency contracts PASS")


if __name__ == "__main__":
    main()
