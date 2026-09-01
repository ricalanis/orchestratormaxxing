"""CRM Growth System — schema migration (idempotent).

Adds the growth layer on top of the Phase-6 CRM:
  • deals: value_ladder_stage, growth_loop, lead_source, lead_score,
           touch_count, last_touch_date, next_touch_date  (additive ALTERs)
  • lead_scoring_features  (one row per lead — the 0–100 score inputs)
  • content_log            (content-cadence tracker)

Guards on PRAGMA table_info / CREATE TABLE IF NOT EXISTS, so a second run is a
no-op. The same install is wired into `growth.ensure_schema()` (run at every app
boot); this standalone script is the explicit, runnable migration for the live
DB — same convention as p0_2 / p1_3 / p2_4 / phase1_backlog_scheduling.

Run:  python -m dashboard.migrations.crm_growth
"""
from .. import db
from .. import growth


_DEAL_COLS = ("value_ladder_stage", "growth_loop", "lead_source", "lead_score",
              "touch_count", "last_touch_date", "next_touch_date")


def run() -> dict:
    conn = db.get_conn()
    try:
        before = {r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()}
    finally:
        conn.close()

    # ensure_schema is the single source of truth for the DDL (additive + tables).
    growth.ensure_schema()

    conn = db.get_conn()
    try:
        after = {r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()}
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()

    return {
        "status": "ok",
        "deal_cols_added": sorted(after - before),
        "deal_cols_present": all(c in after for c in _DEAL_COLS),
        "lead_scoring_features": "lead_scoring_features" in tables,
        "content_log": "content_log" in tables,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, ensure_ascii=False))
