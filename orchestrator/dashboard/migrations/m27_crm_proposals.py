"""m27 — crm_proposals: the propose-only CRM correction inbox.

Born 2026-08-09: the CRM accused the operator falsely (one account recorded a
fraction of the real emailed quote; another looked weeks cold while email showed
handoff+invoice that week) because Hermes sees meetings and deals,
not Gmail/WhatsApp. This table holds *proposed* CRM field corrections derived
from external signals (Fireflies cache, the interactive Thursday session's
Gmail pass); a human approves or dismisses each one. Nothing here mutates the
CRM — approval applies through the existing audited writers.

Lifecycle: proposed → applying (conditional-claim lease, so a retry or a
second click can never double-apply) → approved | dismissed. `dismissed` is
sticky: the UNIQUE(deal_id, kind, evidence_ref) row stays, so derive() can
never re-nag a rejected proposal (same rule as intent-queue).

Additive only. Runs inside the runner's transaction: no commit/close here.
"""


def m27_crm_proposals(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS crm_proposals ("
        " id TEXT PRIMARY KEY,"
        " deal_id TEXT NOT NULL,"
        " kind TEXT NOT NULL CHECK (kind IN ('touch','next_touch','amount')),"
        " payload TEXT NOT NULL DEFAULT '{}',"
        " evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('fireflies','whatsapp','manual')),"
        " evidence_ref TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'proposed'"
        "   CHECK (status IN ('proposed','applying','approved','dismissed','expired')),"
        " created_at INTEGER NOT NULL,"
        " decided_at INTEGER,"
        " decided_via TEXT,"
        " applied_ref TEXT,"
        " UNIQUE (deal_id, kind, evidence_ref))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crm_proposals_status "
        "ON crm_proposals(status, created_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crm_proposals_deal "
        "ON crm_proposals(deal_id)")
