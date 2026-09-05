#!/usr/bin/env python3
"""Contract for bin/delegate-ledger — the evidence substrate for /cheap-delegate routing.

Root authored this BEFORE the tool existed (Tier 0 spec gate). Every check here is a
statement about what "correct" means; the tool is judged against it, never the reverse.

Two checks are the load-bearing ones and both were proven RED against a deliberately
guard-less implementation before the guard landed (Tier 1c):
  C5 — a low-N cell must not emit a preferred_lane AT ALL (structural, not prose)
  C6 — infrastructure failures must never demote a lane's pass_rate

Hermetic: DELEGATE_LEDGER_DIR (store), DELEGATE_LEDGER_NOW (clock). No network, no agents.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, "bin", "delegate-ledger")

FAILS = []
CHECKS = 0

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(args, store, now=None, expect=0):
    env = dict(os.environ)
    env["DELEGATE_LEDGER_DIR"] = store
    env["DELEGATE_LEDGER_NOW"] = iso(now or NOW)
    env.pop("ORCHESTRATORMAXXING_HARNESS_CHILD", None)
    p = subprocess.run([sys.executable, TOOL] + args, env=env,
                       capture_output=True, text=True)
    if expect is not None and p.returncode != expect:
        raise AssertionError(
            f"exit {p.returncode} != {expect} for {' '.join(args)}\n"
            f"  stdout: {p.stdout.strip()[:400]}\n  stderr: {p.stderr.strip()[:400]}")
    return p


def rows(store):
    path = os.path.join(store, "delegation-ledger.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def check(name, fn):
    global CHECKS
    CHECKS += 1
    try:
        fn()
        print(f"  PASS {name}")
    except AssertionError as exc:
        FAILS.append((name, str(exc)))
        print(f"  FAIL {name}: {exc}")
    except Exception as exc:  # a crash is a failure, never a skip
        FAILS.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"  FAIL {name}: {type(exc).__name__}: {exc}")


def seed(store, task_class, lane, verdict, n, model="glm-5.2", now=None, run_prefix="r"):
    """Record n rows in one (class, lane) cell."""
    for i in range(n):
        run([
            "record", "--run-id", f"{run_prefix}-{task_class}-{lane}-{verdict}-{i}",
            "--class", task_class, "--lane", lane, "--verdict", verdict,
            "--model", model, "--duration-s", "12",
        ], store, now=now)


# ── C1 — happy path, normalized row, idempotent ───────────────────────────────

def c1():
    with tempfile.TemporaryDirectory() as store:
        run(["record", "--run-id", "run-a", "--class", "tool-implementation",
             "--lane", "o", "--verdict", "pass", "--model", "glm-coder",
             "--repair-rounds", "1", "--duration-s", "368"], store)
        rs = rows(store)
        assert len(rs) == 1, f"expected 1 row, got {len(rs)}"
        r = rs[0]
        for field in ("id", "ts", "run_id", "task_class", "lane", "model",
                      "attempt", "verdict", "cost_class", "host", "source"):
            assert field in r, f"row missing required field {field!r}"
        assert r["lane"] == "o", f"lane not normalized: {r['lane']!r}"
        assert r["verdict"] == "pass"
        assert r["attempt"] == 1, "attempt must default to 1"
        assert r["cost_class"] == "included", \
            f"cost_class must be DERIVED from lane, got {r['cost_class']!r}"
        assert r["ts"] == iso(NOW), "ts must come from the injected clock"
        assert isinstance(r["duration_s"], (int, float)), "duration_s must be numeric"

        # identical re-record is a no-op (idempotent by run_id+attempt+lane)
        run(["record", "--run-id", "run-a", "--class", "tool-implementation",
             "--lane", "o", "--verdict", "pass", "--model", "glm-coder",
             "--repair-rounds", "1", "--duration-s", "368"], store)
        assert len(rows(store)) == 1, "re-recording the same attempt must not append"

        # a second ATTEMPT on the same run is a distinct row (escalation)
        run(["record", "--run-id", "run-a", "--class", "tool-implementation",
             "--lane", "codex", "--verdict", "pass", "--model", "gpt-5.6-luna",
             "--attempt", "2"], store)
        rs = rows(store)
        assert len(rs) == 2, "an escalation attempt must add a row"
        assert rs[1]["cost_class"] == "subscription", \
            f"codex lane must derive subscription, got {rs[1]['cost_class']!r}"


# ── C2 — validation: closed enums, required flags ─────────────────────────────

def c2():
    with tempfile.TemporaryDirectory() as store:
        run(["record", "--run-id", "x", "--class", "c", "--lane", "o",
             "--verdict", "mostly-worked"], store, expect=2)
        run(["record", "--run-id", "x", "--class", "c", "--lane",
             "1 — oll one-shot (glm-5.2), response-only", "--verdict", "pass"],
            store, expect=2)
        run(["record", "--run-id", "x", "--class", "c", "--verdict", "pass"],
            store, expect=2)
        run(["record", "--class", "c", "--lane", "o", "--verdict", "pass"],
            store, expect=2)
        assert rows(store) == [], "a rejected record must not write anything"


# ── C3 — receipt cross-check: contradiction refuses, unparsed is honest ───────

def c3():
    with tempfile.TemporaryDirectory() as store:
        rundir = os.path.join(store, "run-b")
        os.makedirs(rundir)
        with open(os.path.join(rundir, "receipt.json"), "w", encoding="utf-8") as fh:
            json.dump({"run_id": "run-b", "lane": "opencode-worker/glm-coder",
                       "contract_verdict": "PASS — 8/8 checks",
                       "output_sha256": "a" * 64}, fh)
        # Root claims fail, receipt says pass -> integrity refusal, no row
        run(["record", "--run-id", "run-b", "--class", "c", "--lane", "o",
             "--verdict", "fail", "--run-dir", rundir], store, expect=3)
        assert rows(store) == [], "an integrity refusal must not write a row"
        # agreeing verdict is accepted and binds the output hash
        run(["record", "--run-id", "run-b", "--class", "c", "--lane", "o",
             "--verdict", "pass", "--run-dir", rundir], store)
        r = rows(store)[0]
        assert r.get("output_sha256") == "a" * 64, "must bind the receipt's output hash"
        assert r.get("receipt") == "checked", f"receipt state {r.get('receipt')!r}"

    with tempfile.TemporaryDirectory() as store:
        rundir = os.path.join(store, "run-c")
        os.makedirs(rundir)
        with open(os.path.join(rundir, "receipt.json"), "w", encoding="utf-8") as fh:
            json.dump({"c8_note": "transport ok, artifact judged separately"}, fh)
        run(["record", "--run-id", "run-c", "--class", "c", "--lane", "oll",
             "--verdict", "pass", "--run-dir", rundir], store)
        r = rows(store)[0]
        assert r.get("receipt") == "unparsed", \
            f"an unmappable receipt verdict must be declared, got {r.get('receipt')!r}"


# ── C4 — the row never carries a secret or an unbounded note ──────────────────

def c4():
    secret = "sk-live-AAAABBBBCCCCDDDDEEEEFFFF0123456789"  # gitleaks:allow
    with tempfile.TemporaryDirectory() as store:
        run(["record", "--run-id", "run-d", "--class", "c", "--lane", "oll",
             "--verdict", "pass", "--note", f"used key {secret}"], store, expect=2)
        run(["record", "--run-id", "run-e", "--class", "c", "--lane", "oll",
             "--verdict", "pass", "--note", "x" * 400], store, expect=2)
        assert rows(store) == [], "a rejected note must not write a row"
        path = os.path.join(store, "delegation-ledger.jsonl")
        blob = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        assert secret not in blob, "the seeded secret reached the store"


# ── C5 — LOW-N MUST NOT RANK (proven red against threshold=0) ────────────────

def c5():
    with tempfile.TemporaryDirectory() as store:
        seed(store, "bulk-read", "oll", "pass", 3)
        out = json.loads(run(["stats", "--class", "bulk-read", "--json"], store).stdout)
        assert out.get("authority") == "advisory", \
            f"3 rows must be advisory, got {out.get('authority')!r}"
        assert "preferred_lane" not in out, \
            "an advisory stat must OMIT preferred_lane entirely, not merely caveat it"
        cell = out["lanes"]["oll"]
        assert cell["n"] == 3 and cell["pass"] == 3

    with tempfile.TemporaryDirectory() as store:
        seed(store, "bulk-read", "oll", "pass", 5)
        out = json.loads(run(["stats", "--class", "bulk-read", "--json"], store).stdout)
        assert out.get("authority") == "sufficient", \
            f"5 fresh decided rows must be sufficient, got {out.get('authority')!r}"
        assert out.get("preferred_lane") == "oll", \
            f"a sufficient stat must rank, got {out.get('preferred_lane')!r}"


# ── C6 — INFRA MUST NOT DEMOTE A LANE (proven red against naive pass_rate) ───

def c6():
    with tempfile.TemporaryDirectory() as store:
        seed(store, "review", "oll", "infra", 3, run_prefix="i")
        seed(store, "review", "oll", "pass", 1, run_prefix="p")
        out = json.loads(run(["stats", "--class", "review", "--json"], store).stdout)
        cell = out["lanes"]["oll"]
        assert cell["infra"] == 3, f"infra must be counted separately, got {cell}"
        assert cell["pass_rate"] == 1.0, \
            f"pass_rate must exclude infra from the denominator, got {cell['pass_rate']}"
        assert cell["n"] == 4, "n counts every attempt including infra"


# ── C7 — stale evidence does not confer authority ─────────────────────────────

def c7():
    with tempfile.TemporaryDirectory() as store:
        old = NOW - timedelta(days=45)
        seed(store, "drafting", "oll", "pass", 6, now=old)
        out = json.loads(run(["stats", "--class", "drafting", "--json"], store).stdout)
        assert out.get("authority") == "advisory", \
            "rows older than the 30d TTL must not confer authority"
        assert "preferred_lane" not in out


# ── C8 — import over the REAL observed receipt drift, skipping honestly ───────

def c8():
    with tempfile.TemporaryDirectory() as store:
        base = os.path.join(store, "delegation")
        # mappable: a known lane spelling + a mappable verdict + a task class
        for name, receipt in (
            ("run-ok-1", {"run_id": "run-ok-1", "lane": "opencode-worker/glm-coder",
                          "contract_verdict": "pass", "task_class": "tool-implementation"}),
            ("run-ok-2", {"run_id": "run-ok-2", "lane": "ollama/deepseek-v4-pro",
                          "verdict": "FAIL_OUTPUT_FORMAT", "task_class": "review"}),
            # unmappable: no task_class anywhere -> must be SKIPPED, never guessed
            ("run-skip", {"run_id": "run-skip", "lane": "o/glm-coder",
                          "contract_verdict": "pass"}),
        ):
            d = os.path.join(base, name)
            os.makedirs(d)
            with open(os.path.join(d, "receipt.json"), "w", encoding="utf-8") as fh:
                json.dump(receipt, fh)

        p = run(["import", "--dir", base], store)
        rs = rows(store)
        assert len(rs) == 2, f"expected 2 imported rows, got {len(rs)}"
        assert all(r["source"] == "import" for r in rs), "imported rows must be tagged"
        by_run = {r["run_id"]: r for r in rs}
        assert by_run["run-ok-1"]["lane"] == "o", "lane spelling must normalize to the enum"
        assert by_run["run-ok-2"]["lane"] == "oll"
        assert by_run["run-ok-2"]["verdict"] == "fail", "FAIL_* must map to fail"
        assert "run-skip" in p.stdout, "a skipped receipt must be named in the output"
        assert "task_class" in p.stdout, "the skip must state WHY it was skipped"

        run(["import", "--dir", base], store)
        assert len(rows(store)) == 2, "re-import must add zero rows"


# ── C9 — concurrent writers do not tear a line ───────────────────────────────

def c9():
    with tempfile.TemporaryDirectory() as store:
        env = dict(os.environ)
        env["DELEGATE_LEDGER_DIR"] = store
        env["DELEGATE_LEDGER_NOW"] = iso(NOW)
        env.pop("ORCHESTRATORMAXXING_HARNESS_CHILD", None)
        procs = [
            subprocess.Popen(
                [sys.executable, TOOL, "record", "--run-id", f"conc-{i}",
                 "--class", "c", "--lane", "oll", "--verdict", "pass"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for i in range(2)
        ]
        for p in procs:
            p.wait()
            assert p.returncode == 0, f"concurrent record failed: {p.stderr.read()[:200]}"
        rs = rows(store)  # json.loads on every line: a torn line raises
        assert len(rs) == 2, f"expected 2 intact lines, got {len(rs)}"


# ── C10 — the override flag exists and is readable (sigil bookkeeping) ────────

def c10():
    with tempfile.TemporaryDirectory() as store:
        run(["record", "--run-id", "run-f", "--class", "c", "--lane", "codex",
             "--verdict", "pass", "--model", "gpt-5.6-sol", "--override"], store)
        r = rows(store)[0]
        assert r.get("override") is True, \
            "a sigil-forced run must be marked so stats never read it as 'cheap lane failed'"


# ── C11 — boundaries and the human-facing surface ────────────────────────────
# Added after a mutation run scored 0.54: the guards were tested far from their
# edges (C7 used 45 days against a 30-day TTL, C4 used 400 chars against a 200
# cap), so an off-by-one in either constant survived. The human-readable `stats`
# output was untested entirely, so every print statement in it could be deleted
# unnoticed — and that output is what a person reads to make a routing call.

def c11():
    # TTL boundary: exactly 30 days old still counts, 31 does not.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "edge", "oll", "pass", 5, now=NOW - timedelta(days=30))
        out = json.loads(run(["stats", "--class", "edge", "--json"], store).stdout)
        assert out["authority"] == "sufficient", \
            "a row exactly at the TTL edge must still be fresh"
    with tempfile.TemporaryDirectory() as store:
        seed(store, "edge", "oll", "pass", 5, now=NOW - timedelta(days=31))
        out = json.loads(run(["stats", "--class", "edge", "--json"], store).stdout)
        assert out["authority"] == "advisory", "a row one day past the TTL must be stale"

    # Sufficiency boundary: exactly 5 decided is enough, 4 is not.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "edge2", "oll", "pass", 4)
        out = json.loads(run(["stats", "--class", "edge2", "--json"], store).stdout)
        assert out["authority"] == "advisory", "4 decided attempts must not confer authority"

    # Note cap boundary: exactly 200 accepted, 201 refused.
    with tempfile.TemporaryDirectory() as store:
        run(["record", "--run-id", "n200", "--class", "c", "--lane", "oll",
             "--verdict", "pass", "--note", "x" * 200], store)
        run(["record", "--run-id", "n201", "--class", "c", "--lane", "oll",
             "--verdict", "pass", "--note", "x" * 201], store, expect=2)
        assert len(rows(store)) == 1, "the 200-char note must be kept and 201 refused"

    # The store is created even when its parent directory does not exist yet.
    with tempfile.TemporaryDirectory() as tmp:
        nested = os.path.join(tmp, "does", "not", "exist")
        run(["record", "--run-id", "fresh", "--class", "c", "--lane", "oll",
             "--verdict", "pass"], nested)
        assert len(rows(nested)) == 1, "a nested store path must be created on demand"

    # Even-numbered duration sets take the mean of the middle pair.
    with tempfile.TemporaryDirectory() as store:
        for i, d in enumerate((10, 20, 30, 40)):
            run(["record", "--run-id", f"med-{i}", "--class", "med", "--lane", "oll",
                 "--verdict", "pass", "--duration-s", str(d)], store)
        out = json.loads(run(["stats", "--class", "med", "--json"], store).stdout)
        assert out["lanes"]["oll"]["median_duration_s"] == 25, \
            f"even-length median must average the middle pair, got {out['lanes']['oll']['median_duration_s']}"

    # The human-readable surface must state the class and the authority, because
    # that is the output a person actually reads before routing.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "human", "oll", "pass", 3)
        text = run(["stats", "--class", "human"], store).stdout
        assert "human" in text, "plain output must name the class"
        assert "advisory" in text, "plain output must state the authority"
        assert "oll" in text, "plain output must name the lane it counted"
        assert "preferred" not in text.lower(), \
            "an advisory plain output must not name a preferred lane either"


# ── C12–C16 — the receipt WRITER (flaw lq-e9b5da07) ──────────────────────────
# Measured 2026-08-17: 10 hand-authored receipts carried 41 distinct keys, with
# task_class present in 1 of 10 and the repair count spelled 4 different ways.
# A receipt corpus in that state cannot be aggregated, so the routing evidence
# it was supposed to supply does not exist. These checks specify the writer that
# replaces prose: a CLOSED field set, a REQUIRED task_class, ONE repair-count
# name, and hashes the tool MEASURES rather than accepts.
#
# Each kills a specific plausible-wrong implementation:
#   C12  a writer that passes arbitrary extra keys through   (the drift itself)
#   C13  a writer that leaves task_class optional            (the 9/10 hole)
#   C14  a writer whose output the ledger cannot read back   (a private schema)
#   C15  a writer that fabricates or silently overwrites     (evidence loss)
#   C16  a writer that ACCEPTS a declared sha instead of measuring one

RECEIPT_SCHEMA = {
    "schema_version", "ts", "run_id", "task_class", "lane", "model", "attempt",
    "contract_verdict", "cost_class", "host", "exit_status", "repair_rounds",
    "duration_s", "survivors", "cost_usd", "override",
    "output_sha256", "contract_sha256", "brief_sha256", "note",
}


def make_run_dir(tmp, name="run-x", output="worker output\n",
                 contract="the contract\n", brief="the brief\n"):
    d = os.path.join(tmp, name)
    os.makedirs(d)
    for fname, body in (("output.md", output), ("contract.md", contract),
                        ("brief.md", brief)):
        if body is not None:
            with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                fh.write(body)
    return d


def read_receipt(run_dir):
    with open(os.path.join(run_dir, "receipt.json"), encoding="utf-8") as fh:
        return json.load(fh)


def c12():
    """The emitted key set is exactly the schema — no more, no fewer."""
    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        d = make_run_dir(tmp)
        # Every optional flag supplied: the maximal receipt must still be closed.
        run(["receipt", "--run-dir", d, "--run-id", "run-x", "--class", "tool-implementation",
             "--lane", "o", "--verdict", "pass", "--model", "glm-coder", "--host", "ubuntu",
             "--attempt", "2", "--repair-rounds", "1", "--exit-status", "0",
             "--duration-s", "42", "--survivors", "0", "--cost-usd", "0.0",
             "--override", "--note", "routed by sigil"], store)
        r = read_receipt(d)
        assert set(r) == RECEIPT_SCHEMA, (
            f"key set drifted: extra={sorted(set(r) - RECEIPT_SCHEMA)} "
            f"missing={sorted(RECEIPT_SCHEMA - set(r))}")

        # A minimal receipt carries the SAME keys, with nulls — an absent field must
        # be a readable null, never an omission a reader has to guess about.
        d2 = make_run_dir(tmp, "run-min")
        run(["receipt", "--run-dir", d2, "--run-id", "run-min", "--class", "review",
             "--lane", "oll", "--verdict", "fail"], store)
        r2 = read_receipt(d2)
        assert set(r2) == RECEIPT_SCHEMA, "a minimal receipt must carry the full key set"
        assert r2["model"] is None and r2["duration_s"] is None, \
            "unsupplied optional fields must be explicit nulls"
        assert r2["override"] is False, "override must default to a readable false"
        assert r2["cost_class"] == "included", "cost_class is derived from the lane, not declared"
        assert r2["schema_version"] == 1, \
            "the schema version is the reader's contract — a silent bump breaks aggregation"
        assert r2["attempt"] == 1, "an unspecified attempt is the first one"

        # The stdout line is what a session reads to confirm the receipt landed, and
        # which artifacts were MISSING from the run dir. Untested, every print in the
        # writer could be deleted unnoticed (the lesson C11 already paid for).
        d3 = make_run_dir(tmp, "run-surface", output=None)
        out = run(["receipt", "--run-dir", d3, "--run-id", "run-surface", "--class", "review",
                   "--lane", "oll", "--verdict", "pass"], store).stdout
        assert os.path.join(d3, "receipt.json") in out, "the writer must name the file it wrote"
        assert "output.md" in out, "an absent artifact must be named, not silently nulled"
        assert "contract.md" not in out, "a present artifact must not be reported absent"

        # The drift the flaw measured must be unreachable: no free-form key channel.
        p = run(["receipt", "--run-dir", d2, "--run-id", "run-min", "--class", "review",
                 "--lane", "oll", "--verdict", "fail", "--repairs", "3"], store, expect=2)
        assert "repairs" in (p.stderr + p.stdout), \
            "an unknown field flag must be rejected by name"


def c13():
    """task_class is required, and there is exactly one repair-count spelling."""
    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        d = make_run_dir(tmp, "run-noclass")
        run(["receipt", "--run-dir", d, "--run-id", "r", "--lane", "oll",
             "--verdict", "pass"], store, expect=2)
        assert not os.path.exists(os.path.join(d, "receipt.json")), \
            "a refused receipt must write no file at all"

        # Each required flag, at its own absence. A receipt missing run_id or lane is
        # exactly the unaggregatable row the flaw is about — `import` skips both.
        base_args = {"--run-dir": None, "--run-id": "r", "--class": "c",
                     "--lane": "oll", "--verdict": "pass"}
        for omit in ("--run-dir", "--run-id", "--class", "--lane", "--verdict"):
            d_i = make_run_dir(tmp, f"run-omit{omit.strip('-')}")
            argv = ["receipt"]
            for flag, val in base_args.items():
                if flag == omit:
                    continue
                argv += [flag, d_i if flag == "--run-dir" else val]
            run(argv, store, expect=2)
            assert not os.path.exists(os.path.join(d_i, "receipt.json")), \
                f"omitting {omit} must write no receipt"

        repair_keys = {k for k in RECEIPT_SCHEMA if "repair" in k}
        assert repair_keys == {"repair_rounds"}, \
            f"exactly one repair-count spelling may exist, found {sorted(repair_keys)}"

        # The closed enums apply here too — a lane or verdict outside the enum is
        # what let free-form spellings into the corpus in the first place.
        d2 = make_run_dir(tmp, "run-badlane")
        run(["receipt", "--run-dir", d2, "--run-id", "r2", "--class", "c",
             "--lane", "opencode", "--verdict", "pass"], store, expect=2)
        run(["receipt", "--run-dir", d2, "--run-id", "r2", "--class", "c",
             "--lane", "o", "--verdict", "GREEN"], store, expect=2)


def c14():
    """A written receipt round-trips: `record --run-dir` checks it and `import` reads it.

    This is the check that closes the flaw. The corpus was unaggregatable; a writer
    whose output the ledger still cannot read would be a new private schema, not a fix.
    """
    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "delegation")
        os.makedirs(base)
        d = make_run_dir(base, "run-rt")
        run(["receipt", "--run-dir", d, "--run-id", "run-rt", "--class", "tool-implementation",
             "--lane", "o", "--verdict", "pass", "--model", "glm-coder",
             "--duration-s", "30"], store)

        # record must find the receipt and mark it checked, carrying the measured shas.
        run(["record", "--run-id", "run-rt", "--class", "tool-implementation",
             "--lane", "o", "--verdict", "pass", "--run-dir", d], store)
        row = rows(store)[0]
        assert row["receipt"] == "checked", \
            f"a tool-written receipt must cross-check, got {row.get('receipt')!r}"
        assert row["output_sha256"] == read_receipt(d)["output_sha256"], \
            "the row must carry the receipt's measured output hash"

        # and a contradicting verdict must still be refused (the cross-check is real).
        run(["record", "--run-id", "run-rt", "--class", "tool-implementation",
             "--lane", "o", "--verdict", "fail", "--attempt", "2", "--run-dir", d],
            store, expect=3)

        # import must ingest it with ZERO skips — the aggregation the flaw said was
        # impossible, on a corpus the tool wrote.
        with tempfile.TemporaryDirectory() as store2:
            p = run(["import", "--dir", base], store2)
            assert "skipped 0" in p.stdout, f"a tool-written corpus must skip nothing: {p.stdout}"
            assert len(rows(store2)) == 1, "the written receipt must import as one row"
            assert rows(store2)[0]["task_class"] == "tool-implementation", \
                "task_class must survive the round trip (it was present in 1/10 hand-written)"


def c15():
    """Refusals: no run dir, no clobber, no credentials — and no partial file."""
    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        # A receipt for a run directory that does not exist would be evidence for a
        # delegation that never had a gate.
        missing = os.path.join(tmp, "never-dispatched")
        p = run(["receipt", "--run-dir", missing, "--run-id", "r", "--class", "c",
                 "--lane", "oll", "--verdict", "pass"], store, expect=2)
        assert not os.path.exists(missing), "a refused receipt must not create the run dir"
        assert missing in p.stderr, "the refusal must name the run dir it could not find"

        d = make_run_dir(tmp, "run-clobber")
        run(["receipt", "--run-dir", d, "--run-id", "r1", "--class", "c",
             "--lane", "oll", "--verdict", "pass"], store)
        first = read_receipt(d)
        # Overwriting silently would destroy the evidence of the earlier attempt.
        p = run(["receipt", "--run-dir", d, "--run-id", "r1", "--class", "c",
                 "--lane", "oll", "--verdict", "fail"], store, expect=3)
        assert read_receipt(d) == first, "a refused overwrite must leave the receipt untouched"
        assert "--force" in p.stderr, "the clobber refusal must state the way out"
        run(["receipt", "--run-dir", d, "--run-id", "r1", "--class", "c",
             "--lane", "oll", "--verdict", "fail", "--force"], store)
        assert read_receipt(d)["contract_verdict"] == "fail", "--force must rewrite"

        d2 = make_run_dir(tmp, "run-secret")
        p = run(["receipt", "--run-dir", d2, "--run-id", "r2", "--class", "c",
                 "--lane", "oll", "--verdict", "pass", "--note", "key sk-abc123"],
                store, expect=2)
        assert "credential" in p.stderr, "the refusal must say the note looked like a credential"
        assert not os.path.exists(os.path.join(d2, "receipt.json")), \
            "a credential-bearing receipt must write nothing"


def c16():
    """Hashes are MEASURED from the run dir, never declared by the caller."""
    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        d = make_run_dir(tmp, "run-h", output="alpha\n")
        run(["receipt", "--run-dir", d, "--run-id", "rh", "--class", "c",
             "--lane", "o", "--verdict", "pass"], store)
        first = read_receipt(d)
        expected = hashlib.sha256(b"alpha\n").hexdigest()
        assert first["output_sha256"] == expected, \
            f"output_sha256 must be sha256 of output.md, got {first['output_sha256']}"

        with open(os.path.join(d, "output.md"), "w", encoding="utf-8") as fh:
            fh.write("beta\n")
        run(["receipt", "--run-dir", d, "--run-id", "rh", "--class", "c",
             "--lane", "o", "--verdict", "pass", "--force"], store)
        assert read_receipt(d)["output_sha256"] != first["output_sha256"], \
            "a changed output must change the measured hash"

        # A caller-declared hash would let Root certify a payload it never read.
        p = run(["receipt", "--run-dir", d, "--run-id", "rh", "--class", "c",
                 "--lane", "o", "--verdict", "pass", "--force",
                 "--output-sha256", "deadbeef"], store, expect=2)
        assert "output-sha256" in (p.stderr + p.stdout), \
            "a declared hash flag must be rejected by name"

        # An absent artifact is a readable null, never a fabricated or omitted hash.
        d2 = make_run_dir(tmp, "run-noout", output=None)
        run(["receipt", "--run-dir", d2, "--run-id", "rn", "--class", "c",
             "--lane", "oll", "--verdict", "infra"], store)
        r2 = read_receipt(d2)
        assert "output_sha256" in r2 and r2["output_sha256"] is None, \
            "a missing output.md must hash to an explicit null"
        assert r2["contract_sha256"] is not None, "the contract that WAS there must hash"


# ── C17 — the ledger boundary can TELL a hand-authored receipt from a written one ──
# Both cross-family critics of the writer independently made the same point: a closed
# schema nobody is obliged to use changes nothing, because the playbook sentence saying
# "never by hand" is prose and no tool can read it. So `record --run-dir` reports which
# kind it found. A warning, never a refusal — the legacy corpus must stay ingestible,
# which is exactly what C8 (import drifted receipts) depends on.

def c17():
    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        written = make_run_dir(tmp, "run-written")
        run(["receipt", "--run-dir", written, "--run-id", "run-written", "--class", "c",
             "--lane", "o", "--verdict", "pass"], store)
        p = run(["record", "--run-id", "run-written", "--class", "c", "--lane", "o",
                 "--verdict", "pass", "--run-dir", written], store)
        assert rows(store)[0].get("receipt_schema") == "closed", \
            f"a tool-written receipt must read as closed, got {rows(store)[0].get('receipt_schema')!r}"
        assert "hand-authored" not in p.stderr, "a tool-written receipt must not be warned about"

    with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as tmp:
        hand = make_run_dir(tmp, "run-hand")
        with open(os.path.join(hand, "receipt.json"), "w", encoding="utf-8") as fh:
            json.dump({"run_id": "run-hand", "lane": "opencode", "verdict": "PASS",
                       "repairs": 2, "notes": "whatever the session felt like"}, fh)
        p = run(["record", "--run-id", "run-hand", "--class", "c", "--lane", "o",
                 "--verdict", "pass", "--run-dir", hand], store)
        row = rows(store)[0]
        assert row.get("receipt") == "checked", "a legacy receipt must still cross-check"
        assert row.get("receipt_schema") == "legacy", \
            f"a hand-authored receipt must be flagged, got {row.get('receipt_schema')!r}"
        assert "delegate-ledger receipt" in p.stderr, \
            "the warning must name the tool that would have written it correctly"


# ── C18–C20 — added after the 2026-08-17 mutation run shipped at 0.62 with 125
# survivors unreviewed (lq-b74a8154). The three biggest uncovered surfaces, each
# a REAL hole (a mutated line changes what a routing reader sees, unnoticed):
#   C18  the ranking tiebreaker chain — the routing recommendation itself —
#        was never exercised with more than one sufficient lane
#   C19  the `list` command was untested entirely
#   C20  normalize_verdict / map_lane_import variants and the import skip
#        branches only C8's three receipts touched

def c18():
    # pass_rate dominates: a lane with a better rate wins regardless of order.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "rank", "oll", "pass", 4)
        seed(store, "rank", "oll", "fail", 2)    # oll: 4/6
        seed(store, "rank", "codex", "pass", 5)  # codex: 5/5
        out = json.loads(run(["stats", "--class", "rank", "--json"], store).stdout)
        assert out["authority"] == "sufficient"
        assert out["preferred_lane"] == "codex", \
            f"higher pass_rate must win, got {out['preferred_lane']!r}"

    # equal pass_rate: more decided evidence wins.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "rank2", "oll", "pass", 5)
        seed(store, "rank2", "codex", "pass", 9)
        out = json.loads(run(["stats", "--class", "rank2", "--json"], store).stdout)
        assert out["preferred_lane"] == "codex", \
            "at equal pass_rate the lane with more decided attempts must win"

    # equal rate and volume: the faster median wins.
    with tempfile.TemporaryDirectory() as store:
        for i in range(5):
            run(["record", "--run-id", f"slow-{i}", "--class", "rank3", "--lane", "oll",
                 "--verdict", "pass", "--duration-s", "100"], store)
            run(["record", "--run-id", f"fast-{i}", "--class", "rank3", "--lane", "codex",
                 "--verdict", "pass", "--duration-s", "10"], store)
        out = json.loads(run(["stats", "--class", "rank3", "--json"], store).stdout)
        assert out["preferred_lane"] == "codex", \
            "at equal rate and volume the faster median must win"

    # a lane with no duration data must not beat a measured one on a tie.
    with tempfile.TemporaryDirectory() as store:
        for i in range(5):
            run(["record", "--run-id", f"m-{i}", "--class", "rank4", "--lane", "codex",
                 "--verdict", "pass", "--duration-s", "50"], store)
            run(["record", "--run-id", f"nd-{i}", "--class", "rank4", "--lane", "oll",
                 "--verdict", "pass"], store)
        out = json.loads(run(["stats", "--class", "rank4", "--json"], store).stdout)
        assert out["preferred_lane"] == "codex", \
            "a lane with measured duration must outrank an unmeasured tie"

    # an insufficient cell never becomes preferred, whatever its rate.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "rank5", "oll", "pass", 1)    # 1.0 but low-N
        seed(store, "rank5", "codex", "pass", 4)
        seed(store, "rank5", "codex", "fail", 1)  # 0.8, sufficient
        out = json.loads(run(["stats", "--class", "rank5", "--json"], store).stdout)
        assert out["preferred_lane"] == "codex", \
            "an insufficient cell must never be the preferred lane"

    # the sufficient header names the preferred lane on the human surface too.
    with tempfile.TemporaryDirectory() as store:
        seed(store, "rank6", "oll", "pass", 5)
        text = run(["stats", "--class", "rank6"], store).stdout
        assert "preferred: oll" in text, \
            "a sufficient plain output must name the preferred lane"
        assert "sufficient" in text


def c19():
    with tempfile.TemporaryDirectory() as store:
        run(["record", "--run-id", "l1", "--class", "alpha", "--lane", "oll",
             "--verdict", "pass"], store, now=NOW - timedelta(days=2))
        run(["record", "--run-id", "l2", "--class", "beta", "--lane", "codex",
             "--verdict", "fail"], store, now=NOW)
        run(["record", "--run-id", "l3", "--class", "alpha", "--lane", "o",
             "--verdict", "infra"], store, now=NOW - timedelta(days=1))

        out = json.loads(run(["list", "--json"], store).stdout)
        assert [r["run_id"] for r in out] == ["l1", "l3", "l2"], \
            f"list must sort by ts newest-last, got {[r['run_id'] for r in out]}"

        out = json.loads(run(["list", "--class", "alpha", "--json"], store).stdout)
        assert {r["run_id"] for r in out} == {"l1", "l3"}, \
            "list --class must return only that class"

        # the plain surface names class, lane and verdict — what a human scans.
        text = run(["list", "--class", "beta"], store).stdout
        assert "beta" in text and "codex" in text and "fail" in text, \
            f"plain list must show class, lane and verdict: {text!r}"

    with tempfile.TemporaryDirectory() as store:
        text = run(["list"], store).stdout
        assert "no rows" in text, "an empty ledger must say so, not print nothing"


def c20():
    with tempfile.TemporaryDirectory() as store:
        base = os.path.join(store, "delegation")
        cases = (
            ("v-green", {"run_id": "v-green", "lane": "oll", "task_class": "c",
                         "contract_verdict": "GREEN 8/8"}, "oll", "pass"),
            ("v-ok", {"run_id": "v-ok", "lane": "oll", "task_class": "c",
                      "contract_verdict": "ok"}, "oll", "pass"),
            ("v-timeout", {"run_id": "v-timeout", "lane": "oll", "task_class": "c",
                           "contract_verdict": "timeout after 300s"}, "oll", "infra"),
            ("v-transport", {"run_id": "v-transport", "lane": "oll", "task_class": "c",
                             "contract_verdict": "transport error"}, "oll", "infra"),
            ("l-codex", {"run_id": "l-codex", "lane": "codex gpt-5.6-luna",
                         "task_class": "c", "contract_verdict": "pass"}, "codex", "pass"),
            ("l-claude", {"run_id": "l-claude", "lane": "claude sonnet via provider-ask",
                          "task_class": "c", "contract_verdict": "pass"}, "anthropic", "pass"),
            ("l-oslash", {"run_id": "l-oslash", "lane": "o/glm-coder",
                          "task_class": "c", "contract_verdict": "pass"}, "o", "pass"),
        )
        for name, receipt, _, _ in cases:
            d = os.path.join(base, name)
            os.makedirs(d)
            with open(os.path.join(d, "receipt.json"), "w", encoding="utf-8") as fh:
                json.dump(receipt, fh)
        # skip branches: missing run_id, unmappable lane, unparseable file
        d = os.path.join(base, "s-norunid"); os.makedirs(d)
        with open(os.path.join(d, "receipt.json"), "w", encoding="utf-8") as fh:
            json.dump({"lane": "oll", "task_class": "c", "contract_verdict": "pass"}, fh)
        d = os.path.join(base, "s-badlane"); os.makedirs(d)
        with open(os.path.join(d, "receipt.json"), "w", encoding="utf-8") as fh:
            json.dump({"run_id": "s-badlane", "lane": "carrier-pigeon",
                       "task_class": "c", "contract_verdict": "pass"}, fh)
        d = os.path.join(base, "s-corrupt"); os.makedirs(d)
        with open(os.path.join(d, "receipt.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")

        p = run(["import", "--dir", base], store)
        rs = {r["run_id"]: r for r in rows(store)}
        for name, _, want_lane, want_verdict in cases:
            assert name in rs, f"{name} must import"
            assert rs[name]["lane"] == want_lane, \
                f"{name}: lane {rs[name]['lane']!r} != {want_lane!r}"
            assert rs[name]["verdict"] == want_verdict, \
                f"{name}: verdict {rs[name]['verdict']!r} != {want_verdict!r}"
        assert len(rs) == len(cases), f"exactly the mappable receipts import, got {sorted(rs)}"
        assert "s-norunid" in p.stdout and "run_id" in p.stdout, \
            "the run_id skip must be named with its reason"
        assert "s-badlane" in p.stdout and "carrier-pigeon" in p.stdout, \
            "the lane skip must quote the unmappable value"
        assert "s-corrupt" in p.stdout and "unparseable" in p.stdout, \
            "the corrupt receipt must be reported unparseable"
        assert "imported 7, skipped 3" in p.stdout, \
            f"the tally must count both sides: {p.stdout.splitlines()[-1]!r}"

    # record --run-dir with NO receipt.json: declared absent, still recorded.
    with tempfile.TemporaryDirectory() as store:
        d = os.path.join(store, "run-noreceipt")
        os.makedirs(d)
        run(["record", "--run-id", "nr", "--class", "c", "--lane", "oll",
             "--verdict", "pass", "--run-dir", d], store)
        assert rows(store)[0].get("receipt") == "absent", \
            "a run dir without a receipt must be declared absent"

    # a malformed ts in the store must read as stale, never crash or count fresh.
    with tempfile.TemporaryDirectory() as store:
        path = os.path.join(store, "delegation-ledger.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(5):
                fh.write(json.dumps({"id": f"bad-{i}", "ts": "yesterday-ish",
                                     "run_id": f"b{i}", "task_class": "badts",
                                     "lane": "oll", "verdict": "pass"}) + "\n")
        out = json.loads(run(["stats", "--class", "badts", "--json"], store).stdout)
        assert out["authority"] == "advisory", \
            "an unparseable newest_ts must not confer authority"


def c21():
    """Different models retain their verdicts; retries remain idempotent."""
    with tempfile.TemporaryDirectory() as store:
        base = ["record", "--run-id", "models", "--class", "review", "--lane", "oll"]
        for model, verdict in (("model-a", "pass"), ("model-b", "fail"),
                               (None, "infra"), ("None", "pass"), ("model-ñ", "pass")):
            args = base + ["--verdict", verdict]
            if model is not None:
                args += ["--model", model]
            run(args, store)
            run(args, store)
        rs = rows(store)
        assert len(rs) == 5, f"distinct models must retain five rows, got {len(rs)}"
        assert {r["model"]: r["verdict"] for r in rs} == {
            "model-a": "pass", "model-b": "fail", None: "infra",
            "None": "pass", "model-ñ": "pass"}
        assert len({r["id"] for r in rs}) == 5


def c22():
    """Historical IDs remain readable; equal labels do not erase new evidence."""
    with tempfile.TemporaryDirectory() as store:
        base = ["record", "--run-id", "legacy", "--class", "review", "--lane", "oll",
                "--verdict", "pass"]
        run(base + ["--model", "model-a"], store)
        legacy = rows(store)[0]
        legacy["id"] = "dl-" + hashlib.sha1(b"legacy\x001\x00oll").hexdigest()[:8]
        path = os.path.join(store, "delegation-ledger.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy) + "\n")
        with open(path, "rb") as fh:
            before = fh.read()
        run(base + ["--model", "model-a"], store)
        with open(path, "rb") as fh:
            assert fh.read() == before, "legacy retry must not rewrite or append"
        run(base + ["--model", "model-b"], store)
        assert len(rows(store)) == 2, "a new model on a historical run must append"

        # Simulate a short-hash collision using a different identity's derived label.
        other = rows(store)[1]
        legacy["id"] = other["id"]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy) + "\n")
        run(base + ["--model", "model-b"], store)
        assert len(rows(store)) == 2, "a matching label must not suppress a distinct identity"

        # Older rows may omit model entirely; missing and explicit null agree.
        legacy.pop("model")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy) + "\n")
        run(base, store)
        assert rows(store) == [legacy], "an absent legacy model must match an unmodelled retry"


def c23():
    """Receipt imports and direct records use the same per-model identity."""
    with tempfile.TemporaryDirectory() as store:
        rdir = os.path.join(store, "receipts")
        run(["record", "--run-id", "import-models", "--class", "review", "--lane", "oll",
             "--model", "model-a", "--verdict", "pass"], store)
        for name, model, verdict in (("a", "model-a", "pass"),
                                     ("b", "model-b", "fail"),
                                     ("c", None, "infra"), ("d", "None", "pass")):
            directory = os.path.join(rdir, name)
            os.makedirs(directory)
            with open(os.path.join(directory, "receipt.json"), "w", encoding="utf-8") as fh:
                json.dump({"run_id": "import-models", "task_class": "review",
                           "lane": "oll", "model": model, "contract_verdict": verdict}, fh)
        run(["import", "--dir", rdir], store)
        imported = rows(store)
        assert len(imported) == 4, f"import must preserve each model, got {len(imported)}"
        assert {r["model"]: r["verdict"] for r in imported} == {
            "model-a": "pass", "model-b": "fail", None: "infra", "None": "pass"}
        assert len({r["id"] for r in imported}) == 4
        run(["import", "--dir", rdir], store)
        for row in imported:
            args = ["record", "--run-id", "import-models", "--class", "review",
                    "--lane", "oll", "--verdict", row["verdict"]]
            if row["model"] is not None:
                args += ["--model", row["model"]]
            run(args, store)
        assert rows(store) == imported, "reimport and direct retry must both be no-ops"


def c24():
    """Import keeps accepting JSON-valued models and unambiguous field boundaries."""
    with tempfile.TemporaryDirectory() as store:
        rdir = os.path.join(store, "receipts")
        fixtures = [("json-models", {"name": "model-a", "options": [1, 2]}),
                    ("json-models", {"options": [1, 2], "name": "model-a"}),
                    ("json-models", ["model-a"]),
                    ("embedded\u0000field", "model-a"),
                    ("embedded", "field\u0000model-a")]
        for index, (run_id, model) in enumerate(fixtures):
            directory = os.path.join(rdir, str(index))
            os.makedirs(directory)
            with open(os.path.join(directory, "receipt.json"), "w", encoding="utf-8") as fh:
                json.dump({"run_id": run_id, "task_class": "review", "lane": "oll",
                           "model": model, "contract_verdict": "pass"}, fh)
        run(["import", "--dir", rdir], store)
        imported = rows(store)
        assert len(imported) == 4, "JSON models and field boundaries must have stable identities"
        run(["import", "--dir", rdir], store)
        assert rows(store) == imported


def c25():
    """The lock protects deduplication for matching and distinct model identities."""
    with tempfile.TemporaryDirectory() as store:
        env = dict(os.environ, DELEGATE_LEDGER_DIR=store, DELEGATE_LEDGER_NOW=iso(NOW))
        env.pop("ORCHESTRATORMAXXING_HARNESS_CHILD", None)
        children = []
        for model in ["model-a", "model-b"] * 4:
            children.append(subprocess.Popen(
                [sys.executable, TOOL, "record", "--run-id", "concurrent-models",
                 "--class", "review", "--lane", "oll", "--model", model,
                 "--verdict", "pass"], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True))
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, (stdout, stderr)
        rs = rows(store)
        assert len(rs) == 2, f"concurrent retries must produce two complete rows, got {len(rs)}"
        assert {r["model"] for r in rs} == {"model-a", "model-b"}


def main():
    if not os.path.exists(TOOL):
        print(f"FAIL: {TOOL} does not exist")
        return 1
    print("# delegate-ledger contract")
    for name, fn in (("C1 record/normalize/idempotent", c1),
                     ("C2 closed enums + required flags", c2),
                     ("C3 receipt cross-check", c3),
                     ("C4 no secrets, bounded note", c4),
                     ("C5 low-N omits preferred_lane", c5),
                     ("C6 infra never demotes", c6),
                     ("C7 stale confers no authority", c7),
                     ("C8 import drifted receipts, skip honestly", c8),
                     ("C9 concurrent append intact", c9),
                     ("C10 override recorded", c10),
                     ("C11 boundaries + human surface", c11),
                     ("C12 receipt schema is closed", c12),
                     ("C13 task_class required, one repair name", c13),
                     ("C14 written receipt round-trips into the ledger", c14),
                     ("C15 refuses fabrication, clobber, credentials", c15),
                     ("C16 hashes measured, not declared", c16),
                     ("C17 hand-authored receipts flagged at the ledger", c17),
                     ("C18 ranking tiebreakers among competing lanes", c18),
                     ("C19 list surface", c19),
                     ("C20 normalization variants + import skips", c20),
                     ("C21 per-model records and retries", c21),
                     ("C22 legacy identity and label collisions", c22),
                     ("C23 per-model import and cross-writer retries", c23),
                     ("C24 JSON model compatibility", c24),
                     ("C25 concurrent per-model deduplication", c25)):
        check(name, fn)
    print(f"# {CHECKS - len(FAILS)}/{CHECKS} checks passed")
    if FAILS:
        for name, why in FAILS:
            print(f"  - {name}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
