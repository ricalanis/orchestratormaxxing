"""
The push/pull loop core (PRD Phase 3 — the MCP v1 wiring, agent-side).

This is the shared engine behind BOTH the dashboard's operator UI and the
`hermes-orchestrator` MCP server, so a human clicking "Send to pool" and a
remote Claude Code calling `claim_task` drive the exact same state machine
(PRD §8: "backed by the same core + the internal MCP").

The loop, in one breath:

    list_pool → claim_task (Pool→Working, atomic, returns the acceptance
    contract) → report_progress / heartbeat (live) → report_result
    | report_blocked | escalate_discovery → route (auto-accept ▶ Fleet·Done,
    else ▶ your Inbox).

The one decision that lets a single operator scale to many agents lives in
`route_result` (the §7 auto-accept-vs-escalate rule): a result auto-accepts
ONLY when the agent passed its acceptance contract AND has earned HIGH trust
on this class of work AND the task is marked `autonomy='auto'` and not
sensitive. Everything else escalates to the operator's Inbox for review.
Nothing an agent does is invisible — every transition writes a task_event.

No load-bearing auto-commit: agents propose and report; they never mutate the
plan structure (roadmap/sprints/trust) — that stays operator-only (§8 safety).
"""
import json
import subprocess
import time
from typing import Optional

from . import db, governance
from . import object_graph as graph

# How long a claim holds before it's considered stale and reclaimable. An agent
# keeps it fresh with report_progress/heartbeat; if it dies, the task frees up.
CLAIM_TTL_SECONDS = 30 * 60

# Statuses a task can be claimed FROM (i.e. it's waiting for a worker).
_CLAIMABLE_STATUSES = ("ready", "backlog")

# Human owners — work assigned to these never sits in the agent pool.
_HUMAN = {"ricardo", "user"}


def _now() -> int:
    return int(time.time())


def _log(conn, task_id: str, kind: str, payload: dict) -> None:
    """Append to the shared task_events audit log (best-effort). Every agent
    action in the loop is audited and reviewable (PRD §8 safety model)."""
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, kind, json.dumps(payload), _now()),
        )
    except Exception:
        pass


def _extract_acceptance(body: Optional[str]) -> Optional[str]:
    """Pull the acceptance contract out of a task body. Creation folds it in
    under a `## Acceptance` heading (see api.create_task), so a claiming agent
    gets the contract it will be graded against — the Tier-0 spec-gate, handed
    to the worker at claim time."""
    if not body:
        return None
    marker = "## Acceptance"
    idx = body.find(marker)
    if idx == -1:
        return None
    return body[idx + len(marker):].strip() or None


def _check_owner(row, agent: Optional[str]) -> Optional[dict]:
    """Ownership guard for push-up reports (Phase 0, item 4). Once a task carries
    a LIVE claim, only the agent holding that claim may push progress / heartbeat /
    result to it — the SQL equivalent of `AND claim_lock = ?`. Without it, any
    caller could clobber another agent's in-flight task (wrong pct, a bogus
    'done', a stolen heartbeat).

    Returns an error dict to short-circuit the caller, or None to proceed:
      • no live claim (lock NULL, or expired → reclaimable) → allow (nothing to
        protect; operator/legacy direct writes still work).
      • live claim, no agent id given → reject (identify yourself).
      • live claim, agent != holder → reject (not your task)."""
    keys = row.keys()
    lock = row["claim_lock"] if "claim_lock" in keys else None
    if not lock:
        return None
    expires = row["claim_expires"] if "claim_expires" in keys else None
    if expires is not None and expires < _now():
        return None  # stale claim → task is free, don't gate the report
    if not agent:
        return {"status": "error", "error": f"ownership required: task is claimed by '{lock}' — pass your agent id"}
    if agent != lock:
        return {"status": "error", "error": f"not the claim owner (held by '{lock}')"}
    return None


def _task_public(row) -> dict:
    """The task shape returned to a claiming/reading agent: identity + the
    contract + the workspace it should work in."""
    d = dict(row)
    d["acceptance"] = _extract_acceptance(d.get("body"))
    # Phase 4: the RUNNABLE contract (contract_cmd / ## Contract fence) rides
    # along too — the claiming agent knows the exact command it will be graded
    # by, and the VALIDATE session runs the same one.
    d["contract"] = governance.extract_contract(d)
    d["workspace"] = {
        "kind": d.get("workspace_kind"),
        "path": d.get("workspace_path"),
        "branch": d.get("branch_name"),
    }
    return d


# ---------------------------------------------------------------- Pull

def list_pool(agent: Optional[str] = None, skills: Optional[str] = None,
              limit: int = 50) -> dict:
    """What's claimable right now: the OPEN pool (pool=1) plus, if `agent` is
    given, that agent's own assigned queue. Only tasks actually waiting for a
    worker (ready/backlog) and not already held by a live claim are returned,
    highest priority first (PRD §8: list_pool)."""
    conn = db.get_conn()
    try:
        now = _now()
        status_ph = ",".join("?" for _ in _CLAIMABLE_STATUSES)
        # Open pool + (optionally) my assigned queue; exclude live claims held by
        # someone else, and never surface human-owned work into the agent pool.
        sql = (
            f"SELECT * FROM tasks "
            f"WHERE status IN ({status_ph}) "
            f"AND (assignee IS NULL OR assignee NOT IN ('ricardo','user')) "
            f"AND (claim_lock IS NULL OR claim_expires < ? OR claim_lock = ?) "
        )
        params = list(_CLAIMABLE_STATUSES) + [now, agent or ""]
        if agent:
            sql += "AND (pool = 1 OR assignee = ?) "
            params.append(agent)
        else:
            sql += "AND pool = 1 "
        if skills:
            # Coarse skills filter: substring match on the JSON skills column.
            sql += "AND (skills IS NULL OR skills LIKE ?) "
            params.append(f"%{skills}%")
        sql += "ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return {"count": len(rows), "tasks": [_task_public(r) for r in rows]}
    finally:
        conn.close()


def claim_task(task_id: str, agent: str, session_id: Optional[str] = None) -> dict:
    """Atomically move a task Pool→Working for `agent`. Sets the claim lock,
    assignee, in_progress status, and links the agent's session, then returns
    the task + its acceptance contract + workspace. Atomic: the UPDATE only
    succeeds if the task is claimable and unheld, so two agents can never claim
    the same task (PRD §8: claim_task, "no double-claim")."""
    if not agent:
        return {"status": "error", "error": "agent required"}
    conn = db.get_conn()
    try:
        envelope_block = governance.envelope_claim_gate(task_id, conn)
        if envelope_block:
            conn.commit()
            return envelope_block
        now = _now()
        expires = now + CLAIM_TTL_SECONDS
        status_ph = ",".join("?" for _ in _CLAIMABLE_STATUSES)
        cur = conn.execute(
            f"UPDATE tasks SET claim_lock = ?, claim_expires = ?, assignee = ?, "
            f"status = 'in_progress', started_at = COALESCE(started_at, ?), "
            f"session_id = COALESCE(?, session_id), last_heartbeat_at = ? "
            f"WHERE id = ? AND status IN ({status_ph}) "
            f"AND (claim_lock IS NULL OR claim_expires < ?)",
            [agent, expires, agent, now, session_id, now, task_id, *_CLAIMABLE_STATUSES, now],
        )
        if cur.rowcount != 1:
            # Nothing changed → either it doesn't exist or it's already claimed.
            row = conn.execute("SELECT id, status, claim_lock FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.commit()
            if row is None:
                return {"status": "error", "error": "task not found"}
            return {
                "status": "unavailable",
                "task_id": task_id,
                "reason": "already claimed or not in the pool",
                "current_status": row["status"],
                "held_by": row["claim_lock"],
            }
        # Phase 4 (item 3): the claim OPENS a task_run — the first-class run
        # record (§6.4). step_key starts at 'plan'; task_runs owns liveness
        # from here (item 4) and tasks.current_run_id is the cheap pointer.
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, claim_lock, "
            "claim_expires, last_heartbeat_at, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, agent, "plan", "running", agent, expires, now, now),
        )
        run_id = cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ?, current_step_key = 'plan', "
            "workflow_template_id = COALESCE(workflow_template_id, ?) WHERE id = ?",
            (run_id, governance.WORKFLOW_TEMPLATE, task_id),
        )
        _log(conn, task_id, "claimed", {"agent": agent, "session_id": session_id,
                                        "run_id": run_id, "via": "loop"})
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = {"status": "claimed", "run_id": run_id, "task": _task_public(row)}
    finally:
        conn.close()
    # P0-7 (§6): a claim lands the task in_progress, but a fleet agent can't
    # self-commit to a cycle (privileged) — so the server does it here, closing
    # the "agent work never reaches the cycle" hole. Best-effort, post-commit
    # (its own connection); respects the per-task auto_cycle opt-out.
    try:
        from . import sprints as _sprints
        cyc = _sprints.auto_commit_to_active_cycle(task_id)
        if cyc:
            result["auto_committed_cycle"] = cyc
    except Exception:
        pass
    return result


def claim_next(agent: str, skills: Optional[str] = None) -> dict:
    """Claim the single highest-priority claimable task for `agent`. Retries
    down the pool if a race loses the top pick, so a caller gets *a* task or a
    clean empty (PRD §8: claim_next(filter))."""
    pool = list_pool(agent=agent, skills=skills, limit=10)
    for t in pool["tasks"]:
        res = claim_task(t["id"], agent, session_id=None)
        if res.get("status") == "claimed":
            return res
        # lost the race → try the next one
    return {"status": "empty", "reason": "nothing claimable in the pool"}


# ---------------------------------------------------------------- Push up (report)

def report_progress(task_id: str, note: str, pct: Optional[int] = None,
                    agent: Optional[str] = None, step: Optional[str] = None) -> dict:
    """The live "what I'm doing" push. Sets progress_note/pct + refreshes the
    heartbeat (and extends the claim), so the Fleet board's Working lane shows
    real-time progress (PRD §8: report_progress). Call it as you work.
    Phase 4 (item 3): pass step ('plan'|'code'|'validate') to advance the run
    state machine — task_runs.step_key + tasks.current_step_key move together."""
    if step is not None and step not in governance.RUN_STEPS:
        return {"status": "error",
                "error": f"unknown step '{step}' (want one of {governance.RUN_STEPS})"}
    conn = db.get_conn()
    try:
        now = _now()
        row = conn.execute(
            "SELECT claim_lock, claim_expires, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        owner_err = _check_owner(row, agent)  # item 4: only the claim holder may push
        if owner_err:
            return owner_err
        envelope_block = governance.record_envelope_progress(task_id, pct, conn)
        if envelope_block:
            conn.execute(
                "UPDATE tasks SET status='blocked',claim_lock=NULL,claim_expires=NULL WHERE id=?",
                (task_id,),
            )
            close_run(conn, task_id, "blocked", "no_progress",
                      error=envelope_block["reason"])
            conn.commit()
            return envelope_block
        conn.execute(
            "UPDATE tasks SET progress_note = ?, progress_pct = ?, "
            "last_heartbeat_at = ?, claim_expires = ? WHERE id = ?",
            (note, pct, now, now + CLAIM_TTL_SECONDS, task_id),
        )
        rid = row["current_run_id"] if "current_run_id" in row.keys() else None
        if rid:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ?, claim_expires = ? WHERE id = ?",
                (now, now + CLAIM_TTL_SECONDS, rid))
        if step:
            conn.execute("UPDATE tasks SET current_step_key = ? WHERE id = ?", (step, task_id))
            if rid:
                conn.execute("UPDATE task_runs SET step_key = ? WHERE id = ?", (step, rid))
            _log(conn, task_id, "step_advanced", {"step": step, "agent": agent, "run_id": rid})
        _log(conn, task_id, "progress", {"note": note, "pct": pct, "agent": agent, "step": step})
        conn.commit()
        return {"status": "ok", "task_id": task_id, "note": note, "pct": pct,
                **({"step": step} if step else {})}
    finally:
        conn.close()


def heartbeat(task_id: str, agent: Optional[str] = None) -> dict:
    """Liveness only — refresh the heartbeat + extend the claim (PRD §8).

    Phase 0 (item 2): a heartbeat is pure liveness state, so it lives in the
    `last_heartbeat_at` COLUMN — it must NEVER append a `task_events` row. High-
    frequency beats in the event log drown the real audit trail (they were 132 of
    426 rows). Only meaningful transitions (claimed/progress/result/…) get events.
    orch-verify asserts no code path inserts a 'heartbeat' event.

    Phase 0 (item 4): only the claim holder may refresh the beat."""
    conn = db.get_conn()
    try:
        now = _now()
        row = conn.execute(
            "SELECT claim_lock, claim_expires FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        owner_err = _check_owner(row, agent)
        if owner_err:
            return owner_err
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ?, claim_expires = ? WHERE id = ?",
            (now, now + CLAIM_TTL_SECONDS, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET last_heartbeat_at = ?, claim_expires = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (now, now + CLAIM_TTL_SECONDS, task_id),
        )
        conn.commit()
        return {"status": "ok", "task_id": task_id, "beat_at": now}
    finally:
        conn.close()


def close_run(conn, task_id: str, status: str, outcome: str,
              summary: Optional[str] = None, error: Optional[str] = None) -> Optional[int]:
    """Close the task's current run (Phase 4 item 3/4): stamp the task_runs row
    terminal and clear the task's run pointers. task_runs is the single record
    of what actually happened; tasks.* only points at the live one. Uses the
    caller's open connection (one transaction with the task transition)."""
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    rid = row["current_run_id"] if row and "current_run_id" in row.keys() else None
    now = _now()
    if rid:
        conn.execute(
            "UPDATE task_runs SET status = ?, outcome = ?, ended_at = ?, "
            "summary = COALESCE(?, summary), error = COALESCE(?, error) "
            "WHERE id = ? AND ended_at IS NULL",
            (status, outcome, now, summary, error, rid))
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL, current_step_key = NULL WHERE id = ?",
        (task_id,))
    return rid


def _is_sensitive(row) -> bool:
    """A task is sensitive (never auto-accepts) if the body flags it. Kept as a
    simple, explicit marker in v1 — the operator writes `[sensitive]` in the
    body for work that must always be reviewed regardless of trust."""
    body = (row["body"] or "") if "body" in row.keys() else ""
    return "[sensitive]" in body.lower()


def route_result(task_row, agent: str, passed: bool) -> dict:
    """The auto-accept-vs-escalate decision (PRD §7 + Phase 4 §6.4). Returns
    the routing verdict; the caller applies it. Auto-accept requires ALL of:
      • the agent passed the acceptance contract, AND
      • the agent has earned HIGH trust, AND
      • the task is autonomy='auto' and not flagged sensitive, AND
      • an INDEPENDENT role='verification' ledger row has passed (Phase 4:
        the VALIDATE session signed off — never the implementer's self-report).
    Anything else escalates to the operator's Inbox (with the result on record).
    trust_grade is coarse (per-agent, not yet per-task-class) in v1 — noted."""
    if not passed:
        return {"decision": "escalate", "reason": "acceptance contract not passed"}
    grade = graph.trust_grade_for(agent)
    if grade != "high":
        return {"decision": "escalate", "reason": f"agent trust '{grade}' < high"}
    autonomy = (task_row["autonomy"] if "autonomy" in task_row.keys() else None) or "dispatch"
    if autonomy != "auto":
        return {"decision": "escalate", "reason": f"task autonomy '{autonomy}' (not auto)"}
    if _is_sensitive(task_row):
        return {"decision": "escalate", "reason": "task flagged sensitive"}
    gate = governance.verification_gate(task_row, agent)
    if gate:
        return {"decision": "escalate", "reason": gate}
    return {"decision": "auto_accept",
            "reason": "high-trust agent passed an auto task with independent verification"}


def report_result(task_id: str, result: str, passed: bool = True,
                  artifacts: Optional[list] = None, agent: Optional[str] = None) -> dict:
    """An agent reports it's finished. Runs the §7 rule and routes:
      • auto_accept → status=done, reviewed_at=now → settles into Fleet·Done.
      • escalate   → status=REVIEW (Phase 3 item 2: the real queryable state,
                     not the old invisible done+unreviewed predicate) → surfaces
                     in your Inbox / the Today Review zone (the human gate).
    Either way the claim is released and the result is recorded (PRD §8)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        who = agent or row["assignee"]
        owner_err = _check_owner(row, who)  # item 4: only the claim holder may report a result
        if owner_err:
            return owner_err
        # Phase 0 (item 3): a FAILED result is not a completion. Previously
        # passed=False still fell through to status='done', pct=100, and
        # consecutive_failures=0 — silently marking failed work done AND resetting
        # the strike counter, defeating the 3-strike auto-abort. Route failures to
        # record_failed_attempt (counts the strike, frees the claim for a bounded
        # retry, auto-aborts on the 3rd) instead. Close our read conn first — that
        # path opens its own connection.
        if not passed:
            conn.close()
            from . import orchestration as _orch
            return _orch.record_failed_attempt(task_id, error=result or "", agent=who)
        verdict = route_result(row, who, passed)
        now = _now()
        auto = verdict["decision"] == "auto_accept"
        new_status = "done" if auto else "review"
        reviewed_at = now if auto else None
        conn.execute(
            "UPDATE tasks SET status = ?, result = ?, completed_at = ?, "
            "reviewed_at = ?, claim_lock = NULL, claim_expires = NULL, "
            "progress_pct = 100, consecutive_failures = 0 WHERE id = ?",
            (new_status, result, now, reviewed_at, task_id),
        )
        close_run(conn, task_id, "done", "completed", summary=result)
        if auto:
            governance.complete_run_envelope(task_id, conn)
        _log(conn, task_id, "result_reported",
             {"agent": who, "passed": passed, "artifacts": artifacts or [],
              "decision": verdict["decision"], "reason": verdict["reason"]})
        if verdict["decision"] == "auto_accept":
            _log(conn, task_id, "auto_accepted", {"agent": who, "via": "loop"})
        else:
            _log(conn, task_id, "escalated_review", {"agent": who, "reason": verdict["reason"]})
        conn.commit()
        return {
            "status": new_status,
            "task_id": task_id,
            "routed": verdict["decision"],
            "reason": verdict["reason"],
            "reviewed": reviewed_at is not None,
        }
    finally:
        conn.close()


def report_blocked(task_id: str, reason: str, agent: Optional[str] = None) -> dict:
    """An agent hits a wall. ALWAYS escalates to the operator's Inbox — a
    blocked task (any owner) routes to My Work / Inbox in the board. Releases
    the claim so the work isn't stuck behind a dead lock (PRD §7/§8)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        conn.execute(
            "UPDATE tasks SET status = 'blocked', last_failure_error = ?, "
            "claim_lock = NULL, claim_expires = NULL WHERE id = ?",
            (reason[:500], task_id),
        )
        close_run(conn, task_id, "blocked", "blocked", error=reason[:500])
        _log(conn, task_id, "blocked", {"reason": reason, "agent": agent or row["assignee"], "via": "loop"})
        conn.commit()
        return {"status": "blocked", "task_id": task_id, "escalated": True}
    finally:
        conn.close()


def escalate_discovery(title: str, body: str = "", reason: str = "",
                       related_task: Optional[str] = None,
                       agent: Optional[str] = None) -> dict:
    """The agent-found "this needs you" up-channel: create a NEW task directly
    in the operator's Inbox (assignee=ricardo, ready). An agent never silently
    drops or invents scope — discoveries surface to the human (PRD §7/§8)."""
    full_body = body or ""
    if reason:
        full_body = (full_body + f"\n\n_Escalated by {agent or 'an agent'}: {reason}_").strip()
    if related_task:
        full_body = (full_body + f"\n\nRelated: {related_task}").strip()
    cmd = [db.hermes_bin(), "kanban", "create", title, "--json",
           "--assignee", "ricardo", "--created-by", agent or "agent"]
    if full_body:
        cmd += ["--body", full_body]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        return {"status": "error", "error": f"create failed: {e}"}
    if r.returncode != 0:
        return {"status": "error", "error": (r.stderr or r.stdout).strip()}
    new_id = None
    try:
        payload = json.loads(r.stdout.strip())
        new_id = payload.get("id") or payload.get("task_id") or (payload.get("task") or {}).get("id")
    except Exception:
        import re as _re
        m = _re.search(r"\bt_[0-9a-f]+\b", r.stdout)
        new_id = m.group(0) if m else None
    if new_id:
        conn = db.get_conn()
        try:
            _log(conn, new_id, "discovery",
                 {"by": agent, "reason": reason, "related_task": related_task, "via": "loop"})
            if related_task:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (related_task, new_id),
                    )
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    return {"status": "created", "task_id": new_id, "in": "operator_inbox"}


# ---------------------------------------------------------------- Operator-side (dispatch)

def set_pool(task_id: str, in_pool: bool) -> dict:
    """Operator dispatch: put a task into (or pull it from) the OPEN pool so any
    trusted agent can claim it (PRD §8 operator-side: "Send to pool")."""
    conn = db.get_conn()
    try:
        cur = conn.execute("UPDATE tasks SET pool = ? WHERE id = ?", (1 if in_pool else 0, task_id))
        if cur.rowcount != 1:
            return {"status": "error", "error": "task not found"}
        _log(conn, task_id, "pooled" if in_pool else "unpooled", {"via": "dashboard"})
        conn.commit()
        return {"status": "ok", "task_id": task_id, "pool": bool(in_pool)}
    finally:
        conn.close()


def set_autonomy(task_id: str, autonomy: str) -> dict:
    """Operator sets a task's autonomy: 'auto' (eligible for auto-accept by a
    high-trust agent) or 'dispatch' (always review). Operator-only — an agent
    can't raise its own task's autonomy (mirrors the trust dial, §8)."""
    if autonomy not in ("auto", "dispatch"):
        return {"status": "error", "error": "autonomy must be 'auto' or 'dispatch'"}
    conn = db.get_conn()
    try:
        cur = conn.execute("UPDATE tasks SET autonomy = ? WHERE id = ?", (autonomy, task_id))
        if cur.rowcount != 1:
            return {"status": "error", "error": "task not found"}
        _log(conn, task_id, "autonomy_set", {"autonomy": autonomy, "via": "dashboard"})
        conn.commit()
        return {"status": "ok", "task_id": task_id, "autonomy": autonomy}
    finally:
        conn.close()
