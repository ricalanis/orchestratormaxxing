"""m31 — versioned, auditable commercial proposal packets.

`crm_proposals` is the correction inbox; these tables describe the artifact a
client can actually receive.  Draft revisions remain editable outside Hermes,
but a verified packet is immutable and every send is an append-only receipt.
"""


def m31_commercial_proposals(conn) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS commercial_proposal_packets (
            id TEXT PRIMARY KEY,
            deal_id TEXT NOT NULL REFERENCES deals(id),
            revision INTEGER NOT NULL CHECK (revision > 0),
            workspace_path TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            proposal_path TEXT NOT NULL,
            prototype_path TEXT,
            package_path TEXT,
            manifest_sha256 TEXT,
            package_sha256 TEXT,
            verification_receipt TEXT,
            verified_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE (deal_id, revision)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_commercial_packets_deal
            ON commercial_proposal_packets(deal_id, revision DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS commercial_proposal_sends (
            id TEXT PRIMARY KEY,
            packet_id TEXT NOT NULL REFERENCES commercial_proposal_packets(id),
            channel TEXT NOT NULL,
            recipient TEXT,
            evidence_ref TEXT NOT NULL,
            sent_at INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_commercial_sends_packet
            ON commercial_proposal_sends(packet_id, sent_at DESC)
        """,
    )
    for statement in statements:
        conn.execute(statement)
