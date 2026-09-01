"""P3 — Add missing performance indexes.

Five tables have zero indexes (beyond auto PK) and are queried by FK or
filtered columns on every dashboard load / MCP call:

  session_events   — queried by session_key, resolved_at (unresolved filter)
  deal_events       — queried by deal_id, created_at (deal chain)
  task_ledger       — queried by task_id, created_at DESC (review drawer)
  initiative_events — queried by initiative_id, created_at (audit spine)
  contacts          — FK account_id (list_contacts by account)
  deals             — FKs account_id, contact_id; filter by stage (pipeline)

This migration creates the indexes idempotently (CREATE INDEX IF NOT EXISTS)
and also patches the ensure_schema() functions so fresh DBs get them too.

Run:  python -m dashboard.migrations.p3_indexes
"""
import sqlite3

from .. import db

# (table, index_name, create_sql) — ordered by dependency (parents first).
INDEXES = [
    # session_events — the lifecycle event log. Every dashboard load queries
    # unresolved events (resolved_at IS NULL) and lists by session_key.
    ("session_events", "idx_session_events_session",
     "CREATE INDEX IF NOT EXISTS idx_session_events_session "
     "ON session_events(session_key, created_at DESC)"),
    ("session_events", "idx_session_events_unresolved",
     "CREATE INDEX IF NOT EXISTS idx_session_events_unresolved "
     "ON session_events(resolved_at) WHERE resolved_at IS NULL"),

    # deal_events — the CRM audit spine. get_deal_chain queries by deal_id
    # ORDER BY created_at; growth.get_scorecard scans by created_at range.
    ("deal_events", "idx_deal_events_deal",
     "CREATE INDEX IF NOT EXISTS idx_deal_events_deal "
     "ON deal_events(deal_id, created_at)"),
    ("deal_events", "idx_deal_events_created",
     "CREATE INDEX IF NOT EXISTS idx_deal_events_created "
     "ON deal_events(created_at)"),

    # task_ledger — the verification record. db.get_task_ledger queries by
    # task_id ORDER BY created_at DESC; governance checks role='verification'.
    ("task_ledger", "idx_ledger_task",
     "CREATE INDEX IF NOT EXISTS idx_ledger_task "
     "ON task_ledger(task_id, created_at DESC, id DESC)"),
    ("task_ledger", "idx_ledger_role",
     "CREATE INDEX IF NOT EXISTS idx_ledger_role "
     "ON task_ledger(role)"),

    # initiative_events — the strategy audit spine. Queried by initiative_id
    # ORDER BY created_at (get_initiative_events).
    ("initiative_events", "idx_initiative_events_init",
     "CREATE INDEX IF NOT EXISTS idx_initiative_events_init "
     "ON initiative_events(initiative_id, created_at)"),

    # contacts — FK to accounts. crm.list_contacts queries by account_id.
    ("contacts", "idx_contacts_account",
     "CREATE INDEX IF NOT EXISTS idx_contacts_account "
     "ON contacts(account_id)"),

    # deals — FKs to accounts + contacts; pipeline filters by stage.
    # crm.list_deals / get_pipeline query by stage; get_account_chain joins
    # on account_id; deal detail lookups join on contact_id.
    ("deals", "idx_deals_account",
     "CREATE INDEX IF NOT EXISTS idx_deals_account "
     "ON deals(account_id)"),
    ("deals", "idx_deals_contact",
     "CREATE INDEX IF NOT EXISTS idx_deals_contact "
     "ON deals(contact_id)"),
    ("deals", "idx_deals_stage",
     "CREATE INDEX IF NOT EXISTS idx_deals_stage "
     "ON deals(stage)"),

    # tasks — declared FK to initiatives with no covering index (audit gap).
    # The roadmap drilldown + revenue→strategy joins filter tasks by
    # initiative_id. Unlike the tables above, `tasks` is owned by the hermes
    # CLI (not a dashboard ensure_schema), so this index can only be created
    # by running this migration — which is why the migration is now invoked at
    # dashboard startup. Guarded below in case a fresh DB has no tasks table yet.
    ("tasks", "idx_tasks_initiative",
     "CREATE INDEX IF NOT EXISTS idx_tasks_initiative "
     "ON tasks(initiative_id)"),
]


def run() -> dict:
    """Create all missing indexes. Idempotent — safe to re-run.

    Resilient to missing tables (a fresh DB before the hermes CLI has created
    the ``tasks`` table): a ``no such table`` error skips that one index rather
    than aborting the whole migration.
    """
    conn = db.get_conn()
    created = []
    already = []
    skipped = []
    try:
        # Collect existing index names for the target tables.
        existing = set()
        for table, _, _ in INDEXES:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (table,)
            ).fetchall()
            existing.update(r[0] for r in rows)

        for table, idx_name, sql in INDEXES:
            if idx_name in existing:
                already.append(idx_name)
                continue
            try:
                conn.execute(sql)
                created.append(idx_name)
            except sqlite3.OperationalError as e:
                # Target table doesn't exist yet on this DB — skip, don't abort.
                if "no such table" in str(e).lower():
                    skipped.append(idx_name)
                else:
                    raise

        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "created": created,
        "already_existed": already,
        "skipped": skipped,
        "total_indexes": len(INDEXES),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, ensure_ascii=False))