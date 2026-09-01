#!/usr/bin/env python3
"""round-usage-ledger — contract for harness-agent-run's measured cost ledger.

Hermetic: CLAUDE_BIN points at stubs, HARNESS_RESULTS_DIR at a temp dir. The
ledger is ADVISORY by contract — the load-bearing assertions are that the
wrapper's exit code is ALWAYS the round's exit code (never the parser's), the
narrative always reaches stdout, and a row is written only when the payload
parses with all four token fields as integers.

  L1  well-formed payload → exactly one JSONL row, four int token fields,
      cost + turns recorded, narrative on stdout
  L2  garbage (non-JSON) output → NO row, raw output surfaced, exit unchanged
  L3  exit-code preservation: a failing round (rc=3) keeps rc=3 AND still
      records its usage row (a failed round's spend is still spend)

Red-first is structural: pre-change, harness-agent-run exec'd the host with no
--output-format json, so L1's row assertion fails against `git show HEAD:`.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, "bin", "harness-agent-run")
FAILS = []

PAYLOAD = json.dumps({
    "type": "result", "subtype": "success", "result": "round narrative here",
    "total_cost_usd": 1.23, "num_turns": 7,
    "usage": {"input_tokens": 100, "output_tokens": 200,
              "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 40},
})


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILS.append(f"{name}: {detail}")


def make_stub(tmp, body):
    path = os.path.join(tmp, "claude-stub")
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env bash\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def run_tool(tmp, stub):
    env = dict(os.environ, CLAUDE_BIN=stub, HARNESS_RESULTS_DIR=tmp,
               HARNESS_TIMEOUT_SECONDS="30")
    return subprocess.run(["bash", TOOL, "self-improve"], capture_output=True,
                          text=True, env=env, timeout=60)


def rows(tmp):
    p = os.path.join(tmp, "round-usage.jsonl")
    if not os.path.isfile(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def l1_well_formed():
    tmp = tempfile.mkdtemp(prefix="rul-l1-")
    stub = make_stub(tmp, f"printf '%s' '{PAYLOAD}'")
    r = run_tool(tmp, stub)
    rws = rows(tmp)
    ok_row = (len(rws) == 1
              and all(isinstance(rws[0].get(k), int)
                      for k in ("input", "output", "cache_read", "cache_creation"))
              and rws[0]["cache_read"] == 3000 and rws[0]["cost_usd"] == 1.23
              and rws[0]["num_turns"] == 7 and rws[0]["rc"] == 0)
    check("L1 one well-formed row with four int token fields",
          r.returncode == 0 and ok_row, f"rc={r.returncode} rows={rws}")
    check("L1 narrative reaches stdout",
          "round narrative here" in r.stdout and "[round-usage]" in r.stdout,
          f"stdout={r.stdout[:120]}")


def l2_garbage():
    tmp = tempfile.mkdtemp(prefix="rul-l2-")
    stub = make_stub(tmp, "echo 'this is not json at all'")
    r = run_tool(tmp, stub)
    check("L2 garbage output → no row, raw surfaced, exit unchanged",
          r.returncode == 0 and rows(tmp) == [] and "not json at all" in r.stdout,
          f"rc={r.returncode} rows={rows(tmp)} stdout={r.stdout[:100]}")


def l3_exit_preserved():
    tmp = tempfile.mkdtemp(prefix="rul-l3-")
    stub = make_stub(tmp, f"printf '%s' '{PAYLOAD}'; exit 3")
    r = run_tool(tmp, stub)
    rws = rows(tmp)
    check("L3 failing round keeps rc=3 AND records its spend",
          r.returncode == 3 and len(rws) == 1 and rws[0]["rc"] == 3,
          f"rc={r.returncode} rows={rws}")


if __name__ == "__main__":
    print("round-usage ledger:")
    l1_well_formed()
    l2_garbage()
    l3_exit_preserved()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S)")
        sys.exit(1)
    print("\nall round-usage-ledger contracts PASS")
