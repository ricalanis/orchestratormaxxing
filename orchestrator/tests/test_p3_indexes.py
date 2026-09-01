"""Tests for P3 index migration — verifies all 12 indexes are created and
idempotent, and that the ensure_schema() functions include them for fresh DBs."""
import sqlite3
import tempfile
import os
from pathlib import Path

import pytest


# All 12 indexes the migration should create.
EXPECTED_INDEXES = [
    "idx_session_events_session",
    "idx_session_events_unresolved",
    "idx_deal_events_deal",
    "idx_deal_events_created",
    "idx_ledger_task",
    "idx_ledger_role",
    "idx_initiative_events_init",
    "idx_contacts_account",
    "idx_deals_account",
    "idx_deals_contact",
    "idx_deals_stage",
    "idx_tasks_initiative",
]


def _get_index_names(conn, table):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (table,)
    ).fetchall()
    return {r[0] for r in rows}


class TestP3Indexes:
    """Test the migration against the live kanban DB (indexes are idempotent)."""

    def test_migration_creates_all_indexes(self):
        from dashboard.migrations import p3_indexes
        result = p3_indexes.run()
        assert result["status"] == "ok"
        # All 12 should now exist (created, already there, or skipped-missing-table).
        total = (len(result["created"]) + len(result["already_existed"])
                 + len(result.get("skipped", [])))
        assert total == 12

    def test_migration_is_idempotent(self):
        from dashboard.migrations import p3_indexes
        first = p3_indexes.run()
        second = p3_indexes.run()
        # Second run should create nothing — all already exist (the live test DB
        # has every target table, so nothing is skipped).
        assert second["created"] == []
        assert len(second["already_existed"]) == 12

    def test_tasks_initiative_index_created(self):
        """The hermes-owned tasks.initiative_id FK gets a covering index."""
        from dashboard.migrations import p3_indexes
        from dashboard import db
        p3_indexes.run()
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_tasks_initiative'"
            ).fetchone()
            assert row is not None, "idx_tasks_initiative not created"
            assert "initiative_id" in row[0]
        finally:
            conn.close()

    def test_migration_resilient_to_missing_table(self):
        """A missing target table is skipped, not fatal."""
        import sqlite3
        import tempfile
        from pathlib import Path
        import dashboard.db as dbmod
        with tempfile.TemporaryDirectory() as td:
            dbpath = Path(td) / "empty.db"
            sqlite3.connect(str(dbpath)).close()  # empty DB, zero tables
            original = dbmod.KANBAN_DB
            dbmod.KANBAN_DB = dbpath
            try:
                from dashboard.migrations import p3_indexes
                result = p3_indexes.run()
                # No tables exist → every index is skipped, run still succeeds.
                assert result["status"] == "ok"
                assert result["created"] == []
                assert len(result["skipped"]) == 12
            finally:
                dbmod.KANBAN_DB = original

    def test_all_expected_indexes_exist_in_db(self):
        from dashboard import db
        conn = db.get_conn()
        try:
            for idx_name in EXPECTED_INDEXES:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                    (idx_name,)
                ).fetchone()
                assert row is not None, f"Index {idx_name} not found in DB"
        finally:
            conn.close()

    def test_partial_index_for_unresolved(self):
        """The unresolved-events index should be a partial index (WHERE clause)."""
        from dashboard import db
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_session_events_unresolved'"
            ).fetchone()
            assert row is not None
            assert "WHERE resolved_at IS NULL" in row[0]
        finally:
            conn.close()

    def test_query_uses_index(self):
        """Verify EXPLAIN QUERY PLAN uses the new indexes, not full scans."""
        from dashboard import db
        conn = db.get_conn()
        try:
            # session_events by session_key
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM session_events "
                "WHERE session_key = 'test' ORDER BY created_at DESC"
            ).fetchall()
            assert any("idx_session_events_session" in r[3] for r in plan), \
                f"session_events query not using index: {plan}"

            # task_ledger by task_id
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM task_ledger "
                "WHERE task_id = 't_test' ORDER BY created_at DESC, id DESC"
            ).fetchall()
            assert any("idx_ledger_task" in r[3] for r in plan), \
                f"task_ledger query not using index: {plan}"

            # deal_events by deal_id
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM deal_events "
                "WHERE deal_id = 'd_test' ORDER BY created_at"
            ).fetchall()
            assert any("idx_deal_events_deal" in r[3] for r in plan), \
                f"deal_events query not using index: {plan}"

            # contacts by account_id
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM contacts WHERE account_id = 'a_test'"
            ).fetchall()
            assert any("idx_contacts_account" in r[3] for r in plan), \
                f"contacts query not using index: {plan}"

            # deals by stage
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM deals WHERE stage = 'lead'"
            ).fetchall()
            assert any("idx_deals_stage" in r[3] for r in plan), \
                f"deals query not using index: {plan}"
        finally:
            conn.close()


class TestEnsureSchemaIncludesIndexes:
    """Verify ensure_schema() creates indexes on a fresh DB."""

    def test_orchestration_ensure_schema_creates_indexes(self):
        with tempfile.TemporaryDirectory() as td:
            dbpath = Path(td) / "test.db"
            conn = sqlite3.connect(str(dbpath))
            conn.execute("PRAGMA foreign_keys = ON")
            conn.close()

            # Monkeypatch KANBAN_DB to point at our temp DB.
            import dashboard.db as dbmod
            original = dbmod.KANBAN_DB
            dbmod.KANBAN_DB = dbpath
            try:
                from dashboard import orchestration
                orchestration.ensure_schema()

                conn = sqlite3.connect(str(dbpath))
                conn.row_factory = sqlite3.Row
                # session_events indexes
                idxs = _get_index_names(conn, "session_events")
                assert "idx_session_events_session" in idxs
                assert "idx_session_events_unresolved" in idxs
                # task_ledger indexes
                idxs = _get_index_names(conn, "task_ledger")
                assert "idx_ledger_task" in idxs
                assert "idx_ledger_role" in idxs
                conn.close()
            finally:
                dbmod.KANBAN_DB = original

    def test_crm_ensure_schema_creates_indexes(self):
        with tempfile.TemporaryDirectory() as td:
            dbpath = Path(td) / "test.db"
            conn = sqlite3.connect(str(dbpath))
            conn.execute("PRAGMA foreign_keys = ON")
            conn.close()

            import dashboard.db as dbmod
            original = dbmod.KANBAN_DB
            dbmod.KANBAN_DB = dbpath
            try:
                from dashboard import crm
                crm.ensure_schema()

                conn = sqlite3.connect(str(dbpath))
                conn.row_factory = sqlite3.Row
                idxs = _get_index_names(conn, "deal_events")
                assert "idx_deal_events_deal" in idxs
                assert "idx_deal_events_created" in idxs
                idxs = _get_index_names(conn, "contacts")
                assert "idx_contacts_account" in idxs
                idxs = _get_index_names(conn, "deals")
                assert "idx_deals_account" in idxs
                assert "idx_deals_contact" in idxs
                assert "idx_deals_stage" in idxs
                conn.close()
            finally:
                dbmod.KANBAN_DB = original

    def test_strategy_ensure_schema_creates_indexes(self):
        with tempfile.TemporaryDirectory() as td:
            dbpath = Path(td) / "test.db"
            conn = sqlite3.connect(str(dbpath))
            conn.execute("PRAGMA foreign_keys = ON")
            # strategy.ensure_schema calls _migrate_roadmap_json which needs
            # the projects table to exist (FK from initiatives.project_id).
            conn.execute(
                "CREATE TABLE IF NOT EXISTS projects ("
                "  id TEXT PRIMARY KEY, slug TEXT UNIQUE, name TEXT,"
                "  description TEXT, color TEXT, icon TEXT, created_at INTEGER,"
                "  archived_at INTEGER, kind TEXT)")
            conn.execute(
                "INSERT OR IGNORE INTO projects (id, slug, name, created_at) "
                "VALUES ('proj_test', 'test', 'Test', 0)")
            conn.commit()
            conn.close()

            import dashboard.db as dbmod
            original = dbmod.KANBAN_DB
            dbmod.KANBAN_DB = dbpath
            try:
                from dashboard import strategy
                # Prevent the roadmap JSON migration from running — it reads
                # the real roadmap.json and inserts initiatives with project_ids
                # that don't exist in our temp DB (FK violation).
                original_export = strategy.ROADMAP_EXPORT
                strategy.ROADMAP_EXPORT = Path(td) / "nonexistent.json"
                try:
                    strategy.ensure_schema()
                finally:
                    strategy.ROADMAP_EXPORT = original_export

                conn = sqlite3.connect(str(dbpath))
                conn.row_factory = sqlite3.Row
                idxs = _get_index_names(conn, "initiative_events")
                assert "idx_initiative_events_init" in idxs
                conn.close()
            finally:
                dbmod.KANBAN_DB = original