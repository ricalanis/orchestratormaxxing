"""
Phase 4 — Activate governance (trust the fleet).

The trust machinery has existed since PRD Phase 3/4 but never *fired*:
task_ledger had 0 rows, route_result never auto-accepted, the run state machine
was NULL everywhere. This module is the wiring that makes it fire — under the
harness iron rule imported into the platform:

  • the VALIDATE step is a SEPARATE verification-role session (never
    self-review — arXiv 2606.05976) and the ONLY task_ledger writer;
  • `ledger.passed` is the EXIT CODE of a runnable contract (tasks.contract_cmd
    or a ```contract fenced block), never an LLM's self-report;
  • route_result requires that independent, passing verification row before
    `done` is reachable for an agent task (loop.route_result reads
    `verification_gate` below).

Sidecar on the kanban DB, same pattern as identity/canvas.
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import db

# Where a verification-run executes and for how long. The contract is
# operator-authored (create path / privileged set_contract), so running it is
# the VALIDATE session's job, not a privilege escalation.
CONTRACT_TIMEOUT = 300  # seconds
_OUTPUT_TAIL = 2000     # chars of contract output kept in the ledger summary

_HUMAN = ("ricardo", "user")

# The default workflow template stamped on a claimed run (§6.4): the
# PLAN → CODE → VALIDATE pipeline.
WORKFLOW_TEMPLATE = "plan-code-validate-v1"
RUN_STEPS = ("plan", "code", "validate")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import orchestration_practices as _practices  # noqa: E402


def _now() -> int:
    return int(time.time())


def _envelope_row(task_id: str, conn=None):
    own = conn is None
    conn = conn or db.get_conn()
    try:
        try:
            return conn.execute(
                "SELECT * FROM task_run_envelopes WHERE task_id = ?", (task_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: task_run_envelopes" not in str(exc):
                raise
            return None
    finally:
        if own:
            conn.close()


def require_run_envelope(task_id: str) -> dict:
    """Mark a task as governed. A pending row blocks claim until configured."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not exists:
            return {"status": "error", "error": "task not found"}
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO task_run_envelopes "
            "(task_id,status,created_at,updated_at) VALUES (?,'pending',?,?)",
            (task_id, now, now),
        )
        conn.commit()
        return {"status": "pending", "task_id": task_id}
    finally:
        conn.close()


def set_run_envelope(task_id: str, practice_text: str, host: str,
                     context: dict) -> dict:
    """Evaluate and persist one declared envelope; advisory matching has no authority."""
    conn = db.get_conn()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return {"status": "error", "error": "task not found"}
        result = _practices.evaluate(practice_text, host, context)
        runnable = extract_contract(task)
        if result.get("status") == "ready" and context.get("contract") != runnable:
            result = dict(result)
            result["status"] = "blocked"
            result["reason"] = "declared contract differs from task contract"
            result["rescue_policy_ids"] = ["rescue.missing-contract"]
        brakes = context.get("brakes", {}) if isinstance(context, dict) else {}
        budget = brakes.get("budget_or_deadline", {}) if isinstance(brakes, dict) else {}
        no_progress = brakes.get("no_progress", {}) if isinstance(brakes, dict) else {}
        max_iterations = brakes.get("max_iterations") if isinstance(brakes, dict) else None
        deadline = budget.get("deadline_at") if isinstance(budget, dict) else None
        if deadline is None and isinstance(budget, dict) and budget.get("max_seconds") is not None:
            try:
                deadline = _now() + int(budget["max_seconds"])
            except (TypeError, ValueError):
                deadline = None
        stalled = no_progress.get("max_stalled_steps") if isinstance(no_progress, dict) else None
        now = _now()
        stored_status = "ready" if result["status"] == "ready" else "blocked"
        conn.execute(
            "INSERT INTO task_run_envelopes "
            "(task_id,practice_text,host,context_json,receipt_json,status,reason,"
            "max_iterations,deadline_at,max_stalled_steps,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET practice_text=excluded.practice_text,"
            "host=excluded.host,context_json=excluded.context_json,"
            "receipt_json=excluded.receipt_json,status=excluded.status,reason=excluded.reason,"
            "max_iterations=excluded.max_iterations,deadline_at=excluded.deadline_at,"
            "max_stalled_steps=excluded.max_stalled_steps,attempts=0,last_progress=NULL,"
            "stalled_steps=0,updated_at=excluded.updated_at",
            (task_id, practice_text, host, json.dumps(context, sort_keys=True),
             json.dumps(result.get("receipt"), sort_keys=True), stored_status,
             result.get("reason"), max_iterations, deadline, stalled, now, now),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
            (task_id, "envelope_configured", json.dumps({
                "status": result["status"],
                "practice_ids": (result.get("receipt") or {}).get("practice_ids", []),
                "rescue_policy_ids": result.get("rescue_policy_ids", []),
            }), now),
        )
        conn.commit()
        return {**result, "task_id": task_id}
    finally:
        conn.close()


def get_run_envelope(task_id: str) -> Optional[dict]:
    row = _envelope_row(task_id)
    if row is None:
        return None
    out = dict(row)
    out["context"] = json.loads(out.pop("context_json") or "{}")
    out["receipt"] = json.loads(out.pop("receipt_json") or "null")
    return out


def _block_envelope(conn, task_id: str, rescue_policy_id: str, reason: str) -> dict:
    now = _now()
    conn.execute(
        "UPDATE task_run_envelopes SET status='blocked',reason=?,updated_at=? WHERE task_id=?",
        (reason, now, task_id),
    )
    conn.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
        (task_id, "envelope_blocked", json.dumps({
            "rescue_policy_id": rescue_policy_id, "reason": reason,
        }), now),
    )
    return {"status": "blocked", "task_id": task_id,
            "rescue_policy_id": rescue_policy_id, "reason": reason}


def envelope_claim_gate(task_id: str, conn) -> Optional[dict]:
    """Apply iteration/deadline brakes. No row means rollout-compatible legacy."""
    row = _envelope_row(task_id, conn)
    if row is None:
        return None
    if row["status"] != "ready":
        rescue = "rescue.missing-contract" if row["status"] == "pending" else "rescue.envelope-blocked"
        return _block_envelope(conn, task_id, rescue,
                               row["reason"] or f"envelope is {row['status']}")
    if row["max_iterations"] is None or row["attempts"] >= row["max_iterations"]:
        return _block_envelope(conn, task_id, "rescue.iteration-budget",
                               "maximum iterations reached")
    if row["deadline_at"] is None or row["deadline_at"] <= _now():
        return _block_envelope(conn, task_id, "rescue.deadline-exceeded",
                               "deadline reached")
    conn.execute(
        "UPDATE task_run_envelopes SET attempts=attempts+1,updated_at=? WHERE task_id=?",
        (_now(), task_id),
    )
    return None


def record_envelope_progress(task_id: str, pct: Optional[int], conn) -> Optional[dict]:
    row = _envelope_row(task_id, conn)
    if row is None or pct is None:
        return None
    previous = row["last_progress"]
    stalled = 0 if previous is None or pct > previous else row["stalled_steps"] + 1
    conn.execute(
        "UPDATE task_run_envelopes SET last_progress=?,stalled_steps=?,updated_at=? "
        "WHERE task_id=?", (pct, stalled, _now(), task_id),
    )
    if row["max_stalled_steps"] is None or stalled >= row["max_stalled_steps"]:
        return _block_envelope(conn, task_id, "rescue.no-progress",
                               "progress did not advance")
    return None


def complete_run_envelope(task_id: str, conn) -> None:
    """Mark the declared envelope terminal in the same transaction as the run."""
    conn.execute(
        "UPDATE task_run_envelopes SET status='completed',updated_at=? "
        "WHERE task_id=? AND status='ready'", (_now(), task_id))


def envelope_coverage() -> dict:
    """Read-only M1/C2 ledger: adoption, readiness and typed rescue counts."""
    conn = db.get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee IS NOT NULL "
            "AND assignee NOT IN ('ricardo','user')").fetchone()[0]
        try:
            governed = conn.execute("SELECT COUNT(*) FROM task_run_envelopes").fetchone()[0]
            ready = conn.execute(
                "SELECT COUNT(*) FROM task_run_envelopes WHERE status IN ('ready','completed')"
            ).fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind='envelope_blocked'").fetchone()[0]
            payloads = conn.execute(
                "SELECT payload FROM task_events WHERE kind='envelope_blocked'").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: task_run_envelopes" not in str(exc):
                raise
            governed = ready = blocked = 0
            payloads = []
        rescues = {}
        for row in payloads:
            try:
                policy = json.loads(row["payload"] or "{}").get("rescue_policy_id")
            except (json.JSONDecodeError, TypeError):
                policy = None
            if policy:
                rescues[policy] = rescues.get(policy, 0) + 1
        return {
            "agent_tasks": total,
            "governed": governed,
            "ready_or_completed": ready,
            "coverage": round(governed / total, 3) if total else None,
            "readiness": round(ready / governed, 3) if governed else None,
            "blocked_events": blocked,
            "rescue_counts": dict(sorted(rescues.items())),
        }
    finally:
        conn.close()


def ensure_schema() -> None:
    """Idempotent Phase-4 install: contract column, the auto-pool triggers
    (+ one-time backfill), and the orch_meta epoch marker the ratchet scopes
    its no-unverified-done check to. Safe at startup."""
    conn = db.get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "contract_cmd" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN contract_cmd TEXT")
        # Tiny key/value sidecar for governance markers (NOT config — markers).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orch_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""")
        # The Phase-4 epoch: the ratchet only judges tasks completed AFTER the
        # governance gate went live (legacy done tasks predate the ledger).
        conn.execute(
            "INSERT OR IGNORE INTO orch_meta (key, value) VALUES ('phase4_epoch', ?)",
            (str(_now()),))
        # Auto-pool (Phase 4 item 5, decision: USE the pool): a `ready`
        # agent-task is claimable by construction, so it enters the open pool
        # the moment it becomes ready — no manual set_pool step. Human-owned
        # work never pools (list_pool also filters, defense in depth).
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS trg_auto_pool_update
            AFTER UPDATE OF status ON tasks
            WHEN NEW.status = 'ready' AND NEW.assignee IS NOT NULL
             AND NEW.assignee NOT IN ('ricardo', 'user') AND COALESCE(NEW.pool, 0) = 0
            BEGIN
                UPDATE tasks SET pool = 1 WHERE id = NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_auto_pool_insert
            AFTER INSERT ON tasks
            WHEN NEW.status = 'ready' AND NEW.assignee IS NOT NULL
             AND NEW.assignee NOT IN ('ricardo', 'user') AND COALESCE(NEW.pool, 0) = 0
            BEGIN
                UPDATE tasks SET pool = 1 WHERE id = NEW.id;
            END;
        """)
        conn.execute(
            "UPDATE tasks SET pool = 1 WHERE status = 'ready' AND assignee IS NOT NULL "
            "AND assignee NOT IN ('ricardo', 'user') AND COALESCE(pool, 0) = 0")
        conn.commit()
    finally:
        conn.close()


def phase4_epoch() -> int:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT value FROM orch_meta WHERE key = 'phase4_epoch'").fetchone()
        return int(row["value"]) if row else 0
    finally:
        conn.close()


# ---------------------------------------------------------------- verification gate

def verification_row(task_id: str, conn=None) -> Optional[dict]:
    """The latest role='verification' ledger row for a task, or None."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM task_ledger WHERE task_id = ? AND role = 'verification' "
            "ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def verification_gate(task_row, implementer: Optional[str]) -> Optional[str]:
    """The §6.4 accept gate: before an agent task may auto-accept, an
    INDEPENDENT verification must have passed. Returns the escalation reason,
    or None when the gate is clear:
      • no role='verification' ledger row → escalate
      • the verification failed          → escalate
      • the verifier is the implementer (same agent or same session) —
        self-review is empirically broken → escalate."""
    ver = verification_row(task_row["id"])
    if ver is None:
        return "no verification ledger row (VALIDATE session has not signed off)"
    if not ver.get("passed"):
        return f"verification failed ({ver.get('status')})"
    ver_agent = (ver.get("agent") or "").strip()
    if not ver_agent:
        return "verification row has no agent identity (cannot prove independence)"
    if implementer and ver_agent == implementer:
        return f"verification is self-review (implementer '{implementer}' verified itself)"
    keys = task_row.keys() if hasattr(task_row, "keys") else task_row
    task_session = task_row["session_id"] if "session_id" in keys else None
    if task_session and ver.get("session_key") and ver["session_key"] == task_session:
        return "verification came from the implementing session (not a separate VALIDATE session)"
    return None


# ---------------------------------------------------------------- runnable contracts

_CONTRACT_FENCE_RE = re.compile(
    r"##\s*Contract[^\n]*\n(?:.*?)```(?:\w+)?\n(.*?)```", re.S | re.I)


def extract_contract(task_row) -> Optional[str]:
    """The task's runnable acceptance contract: tasks.contract_cmd wins, else
    the first fenced code block under a `## Contract` heading in the body."""
    keys = task_row.keys() if hasattr(task_row, "keys") else task_row
    cmd = task_row["contract_cmd"] if "contract_cmd" in keys else None
    if cmd and str(cmd).strip():
        return str(cmd).strip()
    body = task_row["body"] if "body" in keys else None
    if body:
        m = _CONTRACT_FENCE_RE.search(body)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def set_contract(task_id: str, contract_cmd: Optional[str]) -> dict:
    """Operator authors the runnable acceptance contract (Tier 0: the spec
    exists BEFORE the work is graded). Privileged — a worker that writes its
    own contract can bake the same bug into both code and test."""
    conn = db.get_conn()
    try:
        cur = conn.execute("UPDATE tasks SET contract_cmd = ? WHERE id = ?",
                           ((contract_cmd or "").strip() or None, task_id))
        if cur.rowcount != 1:
            return {"status": "error", "error": "task not found"}
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, "contract_set",
             json.dumps({"contract_cmd": contract_cmd, "via": "operator"}), _now()))
        conn.commit()
        return {"status": "ok", "task_id": task_id, "contract_cmd": contract_cmd}
    finally:
        conn.close()


def run_contract(task_id: str, agent: Optional[str] = None,
                 session_key: Optional[str] = None) -> dict:
    """The VALIDATE step: run the task's contract with a deterministic runner
    and write the ONE authoritative verification ledger row — passed IS the
    exit code, never a self-report. Refuses to run for the implementer
    (never self-review). Cost model: the runner is cheap CPU; the orchestrator
    reads only pass/fail + the output tail."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "error", "error": "task not found"}
    implementer = row["assignee"]
    if agent and implementer and agent == implementer and implementer not in _HUMAN:
        return {"status": "error",
                "error": f"self-review refused: '{agent}' implemented this task — "
                         "verification must come from a separate session/agent"}
    cmd = extract_contract(row)
    if not cmd:
        return {"status": "error",
                "error": "no runnable contract (set tasks.contract_cmd or a "
                         "```-fenced block under '## Contract' in the body)"}
    cwd = row["workspace_path"] if row["workspace_path"] else None
    started = _now()
    try:
        proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                              timeout=CONTRACT_TIMEOUT, cwd=cwd)
        rc = proc.returncode
        tail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))[-_OUTPUT_TAIL:]
    except subprocess.TimeoutExpired:
        rc, tail = 124, f"contract timed out after {CONTRACT_TIMEOUT}s"
    except Exception as e:
        rc, tail = 127, f"contract runner error: {e}"
    passed = rc == 0

    from . import orchestration as _orch
    entry = _orch.append_ledger(
        task_id,
        f"contract `{cmd}` → rc={rc}\n{tail}".strip(),
        files_modified=[], risks=[] if passed else [f"contract failed rc={rc}"],
        status="passed" if passed else "failed",
        agent=agent or "contract-runner", session_key=session_key,
        role="verification", passed=passed)
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, "contract_run",
             json.dumps({"cmd": cmd, "rc": rc, "passed": passed, "agent": agent,
                         "duration_s": _now() - started}), _now()))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "task_id": task_id, "cmd": cmd, "rc": rc,
            "passed": passed, "ledger_id": entry["id"], "output_tail": tail[-500:]}


def contract_coverage() -> dict:
    """K10 v1 — contract-adequacy + verification-provenance, READ-ONLY.

    Two deterministic ratios over existing columns (no new state, no writes):
      • agent_tasks.coverage — agent tasks (assignee set, not human) carrying a
        runnable contract per extract_contract (tasks.contract_cmd or a fenced
        `## Contract` block). The Tier-0 doctrine target is 1.0.
      • verification_rows.contract_provenance — role='verification' ledger rows
        written by the deterministic contract runner (summary starts with
        "contract `", the exact prefix run_contract writes) vs the
        `operator manual accept` fallback sprints.accept_task writes.
    contract_run_events counts the runner actually firing (task_events).
    Measured at ship time (2026-08-09): 0 contracts, 0 contract_run events,
    218/218 verification rows from manual accept — the honest number this
    surface exists to make visible. An auto-accept-reachability ratio is
    deliberately deferred: accept events don't yet record auto-vs-manual
    provenance, and guessing it would violate the no-fabricated-joins rule."""
    conn = db.get_conn()
    try:
        agent_rows = conn.execute(
            "SELECT * FROM tasks WHERE assignee IS NOT NULL "
            "AND assignee NOT IN ('ricardo', 'user')").fetchall()
        with_contract = sum(1 for r in agent_rows if extract_contract(r) is not None)
        ver_total = conn.execute(
            "SELECT COUNT(*) FROM task_ledger WHERE role = 'verification'").fetchone()[0]
        ver_contract = conn.execute(
            "SELECT COUNT(*) FROM task_ledger WHERE role = 'verification' "
            "AND summary LIKE 'contract %'").fetchone()[0]
        ver_manual = conn.execute(
            "SELECT COUNT(*) FROM task_ledger WHERE role = 'verification' "
            "AND summary LIKE 'operator manual accept%'").fetchone()[0]
        run_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'contract_run'").fetchone()[0]
    finally:
        conn.close()
    total = len(agent_rows)
    return {
        "agent_tasks": {
            "total": total,
            "with_contract": with_contract,
            "coverage": round(with_contract / total, 3) if total else None,
        },
        "verification_rows": {
            "total": ver_total,
            "from_contract_run": ver_contract,
            "manual_accept": ver_manual,
            "other": ver_total - ver_contract - ver_manual,
            "contract_provenance": round(ver_contract / ver_total, 3) if ver_total else None,
        },
        "contract_run_events": run_events,
    }


# ---------------------------------------------------------------- autonomy graduation

# Phase 4 item 5: graduate autonomy on ONE low-blast-radius class first —
# docs/research work (wrong output = a bad document, not broken prod). The
# classifier is deterministic keywords over the TITLE ONLY: a long feature
# brief casually mentioning "docs"/"research" in its body must never graduate
# (live incident 2026-07-04: the Phase-3 implementation task auto-graduated
# off a body keyword and was then auto-accepted by a stale pre-gate process).
# Conservative on miss — a real docs task the classifier skips just stays on
# the manual-review path.
_LOW_BLAST_KEYWORDS = (
    "docs", "documentation", "readme", "changelog", "write up", "writeup",
    "research", "investigate", "digest", "survey", "summarize", "summary",
)
# A title that ALSO smells like implementation never graduates, keyword or not.
_IMPLEMENTATION_MARKERS = ("implement", "fix", "refactor", "build", "deploy",
                           "migrate", "phase", "feat", "wire", "schema")


def is_low_blast_radius(title: str, body: Optional[str] = None) -> bool:
    t = (title or "").lower()
    if any(m in t for m in _IMPLEMENTATION_MARKERS):
        return False
    return any(k in t for k in _LOW_BLAST_KEYWORDS)


def graduate_autonomy() -> dict:
    """Sweep pass: agent tasks in the docs/research class that are still
    waiting (backlog/ready) graduate to autonomy='auto' — the class where
    route_result may auto-accept for the first time (still gated on HIGH trust
    + a passing independent verification; this only opens the door). Each
    graduation is an audited event."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, body FROM tasks WHERE status IN ('backlog', 'ready') "
            "AND assignee IS NOT NULL AND assignee NOT IN ('ricardo', 'user') "
            "AND COALESCE(autonomy, 'dispatch') != 'auto'").fetchall()
        graduated = []
        for r in rows:
            if not is_low_blast_radius(r["title"], r["body"]):
                continue
            conn.execute("UPDATE tasks SET autonomy = 'auto' WHERE id = ?", (r["id"],))
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (r["id"], "autonomy_set",
                 json.dumps({"autonomy": "auto", "via": "graduation",
                             "class": "docs-research"}), _now()))
            graduated.append(r["id"])
        conn.commit()
        return {"graduated": graduated, "count": len(graduated)}
    finally:
        conn.close()
