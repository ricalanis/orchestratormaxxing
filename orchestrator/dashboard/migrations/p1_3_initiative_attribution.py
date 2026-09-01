"""P1-3 — initiative attribution pass (revised-final-plan §8).

Backfill tasks.initiative_id (the P0-1 column) for the unattributed tasks of the
one SHARED project (proj_orchestrator: 3 initiatives), so the roadmap roll-up
becomes injectively attributed instead of showing "unattributed".

Method (the orchestrator doctrine — worker proposes, Opus confirms): an
`ollama-worker` (glm-5.1) first-pass-classified all 33 unattributed tasks into
the 3 initiatives (or "unclear"); Opus then reviewed and OVERRODE where the
worker read a phase's surface keyword instead of the whole:
  - the interaction-layers Phase 0-6 + the redesign sprint + the deep-design
    analysis + the dashboard knowledge-graph memory are ALL the "Dashboard v2"
    BUILD → init_9995ca2e (the worker had split Phase 4→coord, Phase 5→MCP on
    surface keywords);
  - the two explicitly-MCP tasks (scope hardening, Obsidian MCP server) →
    init_7e753620;
  - research/business + a borderline agent-access-infra task are left honestly
    UNATTRIBUTED (t_476af1fc, t_85ba07e5, t_b276e3e3, t_1a4e0988) — no clean fit,
    and P0-1 embraces "unattributed" over a forced guess;
  - Multi-Agent Coordination (init_52c9d667, a *planned* initiative) gets 0 — no
    current task is multi-machine-coordination work, which is the honest signal.

Conservative + idempotent: only sets a task whose initiative_id is currently
NULL (never clobbers a manual attribution), and re-running is a no-op. Applies
through graph.set_task_initiative (FK-validated).

Run:  python -m dashboard.migrations.p1_3_initiative_attribution
"""
from .. import db
from .. import object_graph as graph

# Opus-confirmed attribution (task_id → initiative_id). Tasks omitted here stay
# unattributed by design (see the module docstring).
ATTRIBUTION = {
    't_80f66db2': 'init_9995ca2e',
    't_b436ad0c': 'init_9995ca2e',
    't_b4c60cee': 'init_9995ca2e',
    't_fed889fb': 'init_9995ca2e',
    't_c4179cb2': 'init_9995ca2e',
    't_cca2da33': 'init_9995ca2e',
    't_390e588c': 'init_9995ca2e',
    't_9cf0abfd': 'init_9995ca2e',
    't_f6c74333': 'init_9995ca2e',
    't_2ca0e18d': 'init_9995ca2e',
    't_e55c209a': 'init_9995ca2e',
    't_89fcaba3': 'init_9995ca2e',
    't_448efdbc': 'init_7e753620',
    't_75ed8944': 'init_9995ca2e',
    't_80947045': 'init_9995ca2e',
    't_8a53852b': 'init_9995ca2e',
    't_2a2db9ab': 'init_9995ca2e',
    't_c7ab4210': 'init_9995ca2e',
    't_c1d3945d': 'init_9995ca2e',
    't_4c07143d': 'init_9995ca2e',
    't_95a1b8cb': 'init_9995ca2e',
    't_d24384c9': 'init_9995ca2e',
    't_625a6e11': 'init_9995ca2e',
    't_f8edc22f': 'init_9995ca2e',
    't_6b9183d3': 'init_9995ca2e',
    't_55d91e1c': 'init_9995ca2e',
    't_85a6f338': 'init_7e753620',
    't_207b87b7': 'init_9995ca2e',
    't_e9aa613d': 'init_9995ca2e',
}


def run() -> dict:
    conn = db.get_conn()
    try:
        cur = {r["id"]: r["initiative_id"] for r in conn.execute(
            "SELECT id, initiative_id FROM tasks WHERE id IN (%s)"
            % ",".join("?" * len(ATTRIBUTION)), list(ATTRIBUTION))}
    finally:
        conn.close()

    applied, skipped, missing = [], [], []
    for tid, iid in ATTRIBUTION.items():
        if tid not in cur:
            missing.append(tid)
            continue
        if cur[tid]:                              # already attributed → don't clobber
            skipped.append(tid)
            continue
        res = graph.set_task_initiative(tid, iid)
        (applied if not res.get("error") else missing).append(tid)

    return {"status": "ok", "applied": len(applied), "skipped_existing": len(skipped),
            "missing": missing, "total_in_map": len(ATTRIBUTION)}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
