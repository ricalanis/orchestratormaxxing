"""m32 — freeze proposal v2 evidence and quality identity in the ledger."""


def m32_commercial_proposal_quality(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(commercial_proposal_packets)")}
    additions = (
        ("workspace_schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ("receipt_sha256", "TEXT"),
        ("evidence_manifest_path", "TEXT"),
        ("evidence_manifest_sha256", "TEXT"),
        ("checker_report_path", "TEXT"),
        ("checker_report_sha256", "TEXT"),
        ("quality_report_path", "TEXT"),
        ("quality_report_sha256", "TEXT"),
        ("quality_status", "TEXT"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE commercial_proposal_packets ADD COLUMN {name} {definition}")
    triggers = (
        """
        CREATE TRIGGER IF NOT EXISTS commercial_packet_verified_immutable
        BEFORE UPDATE ON commercial_proposal_packets
        WHEN OLD.verified_at IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'verified commercial proposal is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS commercial_packet_verified_no_delete
        BEFORE DELETE ON commercial_proposal_packets
        WHEN OLD.verified_at IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'verified commercial proposal cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS commercial_send_no_update
        BEFORE UPDATE ON commercial_proposal_sends
        BEGIN
            SELECT RAISE(ABORT, 'commercial proposal send is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS commercial_send_no_delete
        BEFORE DELETE ON commercial_proposal_sends
        BEGIN
            SELECT RAISE(ABORT, 'commercial proposal send is append-only');
        END
        """,
    )
    for statement in triggers:
        conn.execute(statement)
