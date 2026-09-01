"""review_queue — Little's-Law instrumentation for the human review gate.

The verification-tax counter-metric (DORA 2026 "Balancing AI tensions"; Second
Movement ch18: once the inner loop is cheap the review queue is the bottleneck,
and Little's law L = λ·W only describes a queue that is keeping up). Read-only
by contract: this module NEVER writes to the DB — the byte-identical sha256
test is the enforcement, not this docstring.

PINNED METRIC DEFINITIONS (the test's golden values encode exactly these; if a
definition changes, change it here AND there or the drift is a bug):

  window        trailing `days` (default 7) ending at `now`; timestamps are
                task_events.created_at (unix seconds).
  WIP           snapshot: COUNT of tasks with status='review' at read time.
  arrivals      distinct tasks whose FIRST-EVER `escalated_review` event falls
                inside the window. A task escalated 13 times counts as ONE
                arrival (its other 12 events are rework, not arrivals).
                `respawn_guarded` events play no part in anything here.
  lambda        arrivals / days  (per-day arrival rate).
  W (wait)      per arrival task with a terminal review event (`accepted` or
                `rejected`) at-or-after its first escalation: hours from first
                escalation to FIRST such terminal. Median AND mean over
                completed waits (mean feeds Little's law; median is the
                human-facing number). Censored work (arrivals with no terminal
                yet) is counted separately, never silently dropped.
  rework        escalated_review events in the window MINUS arrivals in the
                window — i.e. every window escalation that is not some task's
                first-ever escalation (repeat escalations of old tasks land
                here too, deliberately).
  predicted WIP lambda_per_day × mean_wait_days, only when both exist —
                reported beside observed WIP, no verdict (advisory: a large gap
                means the queue is not in steady state, which is the signal).
  explainer     for each task currently in review: loop.route_result(row,
                agent=assignee, passed=True) called READ-ONLY. passed=True is a
                hypothetical, so the field is `would_auto_accept_if_passed` —
                "even if its contract had passed, would this task still need
                the operator?" — and `blocking_reason` is route_result's own reason.

Grounded: knowledge/second-movement-integration-research-2026-08-09.md
candidates 5+6 (kept by the cross-family critic); measured 2026-08-09: 234
escalated_review vs 98 accepted events, three tasks escalated 13x each.
"""
import time
from statistics import median

from dashboard import db


def _fetch_events(conn, now, days):
    """One pass over the review-relevant events; returns (first_esc, terminals,
    window_esc_count) where first_esc maps task_id -> first-ever escalation ts,
    terminals maps task_id -> list of terminal (accepted/rejected) ts sorted,
    and window_esc_count is total escalated_review events inside the window."""
    since = now - days * 86400
    first_esc = {}
    for tid, ts in conn.execute(
            "SELECT task_id, MIN(created_at) FROM task_events "
            "WHERE kind = 'escalated_review' GROUP BY task_id"):
        first_esc[tid] = ts
    terminals = {}
    for tid, ts in conn.execute(
            "SELECT task_id, created_at FROM task_events "
            "WHERE kind IN ('accepted', 'rejected') ORDER BY created_at"):
        terminals.setdefault(tid, []).append(ts)
    window_esc = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'escalated_review' "
        "AND created_at >= ? AND created_at <= ?", (since, now)).fetchone()[0]
    return first_esc, terminals, window_esc


def review_queue_summary(days: int = 7, now: int = None) -> dict:
    """The full read-only summary. `now` is injectable for deterministic tests."""
    now = int(now if now is not None else time.time())
    days = max(1, int(days))
    since = now - days * 86400
    conn = db.get_conn()
    try:
        first_esc, terminals, window_esc = _fetch_events(conn, now, days)

        arrivals = {tid: ts for tid, ts in first_esc.items() if since <= ts <= now}
        waits = []
        censored = 0
        for tid, esc_ts in arrivals.items():
            term = next((t for t in terminals.get(tid, []) if t >= esc_ts), None)
            if term is None:
                censored += 1
            else:
                waits.append((term - esc_ts) / 3600.0)

        wip_rows = conn.execute("SELECT * FROM tasks WHERE status = 'review'").fetchall()
        oldest_open_hours = None
        queue = []
        # import here so a broken loop/graph import degrades the explainer,
        # never the counters
        try:
            from dashboard import loop as _loop
        except Exception:
            _loop = None
        for row in wip_rows:
            tid = row["id"]
            waiting_since = first_esc.get(tid, row["created_at"])
            waiting_hours = round((now - waiting_since) / 3600.0, 2)
            if oldest_open_hours is None or waiting_hours > oldest_open_hours:
                oldest_open_hours = waiting_hours
            entry = {"task_id": tid, "title": row["title"],
                     "assignee": row["assignee"], "waiting_hours": waiting_hours,
                     "would_auto_accept_if_passed": None, "blocking_reason": None}
            if _loop is not None:
                try:
                    verdict = _loop.route_result(row, agent=row["assignee"] or "?",
                                                 passed=True)
                    entry["would_auto_accept_if_passed"] = (
                        verdict.get("decision") == "auto_accept")
                    entry["blocking_reason"] = verdict.get("reason")
                except Exception as ex:
                    entry["blocking_reason"] = f"explainer unavailable: {ex}"
            queue.append(entry)
        queue.sort(key=lambda e: -e["waiting_hours"])

        lam = round(len(arrivals) / days, 3)
        mean_wait = round(sum(waits) / len(waits), 2) if waits else None
        predicted = (round(lam * (mean_wait / 24.0), 2)
                     if waits and lam is not None else None)
        return {
            "window_days": days,
            "now": now,
            "wip": len(wip_rows),
            "arrivals": len(arrivals),
            "arrivals_per_day": lam,
            "wait_hours": {
                "median": round(median(waits), 2) if waits else None,
                "mean": mean_wait,
                "completed": len(waits),
                "censored": censored,
                "oldest_open_hours": oldest_open_hours,
            },
            "rework_events": window_esc - len(arrivals),
            "littles_law_predicted_wip": predicted,
            "queue": queue,
        }
    finally:
        conn.close()
