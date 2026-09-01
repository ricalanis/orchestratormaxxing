"""P2-4 — backlog cleanup (revised-final-plan Phase 3): triage the inbox, close
stale-but-shipped tasks, and add a missing initiative. Opus-confirmed, idempotent
(safe to re-run; each step guards on current state).

Decisions:
  triage — the three non-triaged inbox tasks move to their real projects:
      t_d8d36e4d (MCP SSE security) → orchestrator,
      t_e9a1732d (containerize+Coolify, rejected) → orchestrator,
      t_c708a7c4 (provision GCloud, rejected) → gpu_ops.
  close  — two tasks marked ready but actually SHIPPED are marked done:
      t_e9aa613d (Redesign Day 1 = P0-1/2/3, this session),
      t_d8d36e4d (MCP SSE hardening, commit b3b5e73).
      (t_207b87b7 UX research is left in_progress — no recorded result, not
      confidently complete; closing on a guess would be worse than a stale card.)
  attribute — give the newly-cleaned work an initiative:
      t_d8d36e4d → MCP Server Expansion (init_7e753620),
      and a NEW "Data Lakehouse — Agent Operating Layer" initiative (explore) for
      the two currently-unattributed lakehouse research tasks (t_b276e3e3,
      t_1a4e0988), so the roadmap covers that direction.

Run:  python -m dashboard.migrations.p2_4_backlog_cleanup
"""
from .. import db
from .. import sprints
from .. import strategy
from .. import object_graph as graph

TRIAGE = {
    "t_d8d36e4d": "proj_orchestrator",
    "t_e9a1732d": "proj_orchestrator",
    "t_c708a7c4": "proj_gpu_ops",
}
CLOSE_DONE = ["t_e9aa613d", "t_d8d36e4d"]
ATTRIBUTE = {"t_d8d36e4d": "init_7e753620"}
LAKEHOUSE_TITLE = "Data Lakehouse — Agent Operating Layer"
LAKEHOUSE_TASKS = ["t_b276e3e3", "t_1a4e0988"]


def _exists(conn, table, id_):
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (id_,)).fetchone() is not None


def run() -> dict:
    conn = db.get_conn()
    try:
        present = {r[0]: dict(r) for r in conn.execute(
            "SELECT id, status, project_id, initiative_id FROM tasks")}
    finally:
        conn.close()
    out = {"triaged": [], "closed": [], "attributed": [], "initiative": None, "missing": []}

    # 1. Triage inbox → real project.
    for tid, pid in TRIAGE.items():
        if tid not in present:
            out["missing"].append(tid); continue
        if present[tid]["project_id"] != pid:
            sprints.assign_task_project(tid, pid)
            out["triaged"].append({tid: pid})

    # 2. New Data Lakehouse initiative (idempotent by title) + attribute its tasks.
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT id FROM initiatives WHERE title = ?", (LAKEHOUSE_TITLE,)).fetchone()
        lake_id = row[0] if row else None
    finally:
        conn.close()
    if not lake_id:
        strategy.create_initiative(
            LAKEHOUSE_TITLE, "proj_orchestrator", tier="explore",
            why="Treat memory/context as a governed data product on a lakehouse — "
                "the agent operating layer and long-term moat.",
            success_check="One recall() path backed by governed, queryable tables.")
        # Re-query by title (create_initiative returns {"initiative": {...}}); this
        # keeps the migration single-run complete + idempotent regardless of shape.
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT id FROM initiatives WHERE title = ?", (LAKEHOUSE_TITLE,)).fetchone()
            lake_id = row[0] if row else None
        finally:
            conn.close()
    out["initiative"] = {"id": lake_id, "title": LAKEHOUSE_TITLE}
    if lake_id:
        for tid in LAKEHOUSE_TASKS:
            if tid in present and present[tid]["initiative_id"] != lake_id:
                graph.set_task_initiative(tid, lake_id)
                out["attributed"].append({tid: lake_id})

    # 3. Direct attributions (e.g. the MCP task, now in orchestrator).
    for tid, iid in ATTRIBUTE.items():
        if tid in present and present[tid]["initiative_id"] != iid:
            graph.set_task_initiative(tid, iid)
            out["attributed"].append({tid: iid})

    # 4. Close the shipped-but-stale tasks (operator →done = accepted).
    for tid in CLOSE_DONE:
        if tid not in present:
            out["missing"].append(tid); continue
        if present[tid]["status"] != "done":
            sprints.set_task_status(tid, "done")
            out["closed"].append(tid)

    return {"status": "ok", **out}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, ensure_ascii=False))
