"""Commercial proposal ledger: agent-safe preparation, human-only sending.

The filesystem manifest and package receipt are produced by
`proposal-workspace`; this module records their identity.  It deliberately does
not infer "sent" from a file existing: only `record_send`, called by the human
dashboard route, can cross that boundary.
"""
from __future__ import annotations

import json
import time
import uuid

from . import crm, db


def _now() -> int:
    return int(time.time())


def _error(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "error": message}


def _packet(row, sends=()) -> dict:
    item = dict(row)
    item["sends"] = [dict(s) for s in sends]
    item["proposal_state"] = (
        "sent" if item["sends"] else
        "verified" if item.get("verified_at") else "draft"
    )
    return item


def list_for_deal(deal_id: str) -> dict:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM commercial_proposal_packets WHERE deal_id=? "
            "ORDER BY revision DESC", (deal_id,)).fetchall()
        proposals = []
        for row in rows:
            sends = conn.execute(
                "SELECT * FROM commercial_proposal_sends WHERE packet_id=? "
                "ORDER BY sent_at DESC", (row["id"],)).fetchall()
            proposals.append(_packet(row, sends))
        return {"status": "ok", "deal_id": deal_id, "proposals": proposals}
    finally:
        conn.close()


def register_packet(deal_id: str, revision, workspace_path: str,
                    manifest_path: str, proposal_path: str,
                    prototype_path: str | None = None, *, workspace_schema_version=1,
                    evidence_manifest_path: str | None = None,
                    checker_report_path: str | None = None,
                    quality_report_path: str | None = None) -> dict:
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        return _error("invalid_revision", "revision must be a positive integer")
    if revision < 1:
        return _error("invalid_revision", "revision must be a positive integer")
    try:
        workspace_schema_version = int(workspace_schema_version)
    except (TypeError, ValueError):
        return _error("invalid_schema", "workspace_schema_version must be 1 or 2")
    if workspace_schema_version not in (1, 2):
        return _error("invalid_schema", "workspace_schema_version must be 1 or 2")
    required = {"workspace_path": workspace_path, "manifest_path": manifest_path,
                "proposal_path": proposal_path}
    if any(not str(value or "").strip() for value in required.values()):
        return _error("artifact_required", "workspace, manifest and proposal paths are required")
    v2_paths = (evidence_manifest_path, checker_report_path, quality_report_path)
    if workspace_schema_version == 2 and any(not str(value or "").strip() for value in v2_paths):
        return _error("v2_artifact_required", "v2 evidence, checker and quality report paths are required")
    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM deals WHERE id=?", (deal_id,)).fetchone() is None:
            return _error("not_found", "deal not found")
        existing = conn.execute(
            "SELECT * FROM commercial_proposal_packets WHERE deal_id=? AND revision=?",
            (deal_id, revision)).fetchone()
        identity = (workspace_path, manifest_path, proposal_path, prototype_path,
                    workspace_schema_version, *v2_paths)
        if existing:
            stored = tuple(existing[k] for k in
                           ("workspace_path", "manifest_path", "proposal_path", "prototype_path",
                            "workspace_schema_version", "evidence_manifest_path",
                            "checker_report_path", "quality_report_path"))
            if stored != identity:
                return _error("revision_conflict", "this deal revision already names other artifacts")
            return {"status": "exists", "created": False,
                    "proposal": _packet(existing)}
        now = _now()
        packet_id = f"prop_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO commercial_proposal_packets "
            "(id,deal_id,revision,workspace_path,manifest_path,proposal_path,prototype_path,"
            "workspace_schema_version,evidence_manifest_path,checker_report_path,quality_report_path,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (packet_id, deal_id, revision, *identity, now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM commercial_proposal_packets WHERE id=?",
                           (packet_id,)).fetchone()
        return {"status": "ok", "created": True, "proposal": _packet(row)}
    finally:
        conn.close()


def verify_packet(packet_id: str, package_path: str, manifest_sha256: str,
                  package_sha256: str, verification_receipt: str, *,
                  receipt_sha256: str | None = None,
                  evidence_manifest_sha256: str | None = None,
                  checker_report_sha256: str | None = None,
                  quality_report_sha256: str | None = None,
                  quality_status: str | None = None) -> dict:
    values = (package_path, manifest_sha256, package_sha256, verification_receipt)
    if any(not str(value or "").strip() for value in values):
        return _error("verification_required", "package path, both hashes and receipt are required")
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM commercial_proposal_packets WHERE id=?",
                           (packet_id,)).fetchone()
        if row is None:
            return _error("not_found", "proposal packet not found")
        v2_values = (receipt_sha256, evidence_manifest_sha256, checker_report_sha256,
                     quality_report_sha256, quality_status)
        if row["workspace_schema_version"] == 2:
            if any(not str(value or "").strip() for value in v2_values):
                return _error("v2_verification_required",
                              "v2 receipt, evidence, checker and quality hashes are required")
            if quality_status != "pass":
                return _error("quality_not_passed", "v2 quality_status must be pass")
        existing = tuple(row[k] for k in
                         ("package_path", "manifest_sha256", "package_sha256", "verification_receipt",
                          "receipt_sha256", "evidence_manifest_sha256", "checker_report_sha256",
                          "quality_report_sha256", "quality_status"))
        identity = (*values, *v2_values)
        if row["verified_at"]:
            if existing != identity:
                return _error("revision_frozen", "verified revisions are immutable; create a new revision")
            return {"status": "exists", "verified": True, "proposal": _packet(row)}
        now = _now()
        conn.execute(
            "UPDATE commercial_proposal_packets SET package_path=?, manifest_sha256=?, "
            "package_sha256=?, verification_receipt=?, receipt_sha256=?, "
            "evidence_manifest_sha256=?, checker_report_sha256=?, quality_report_sha256=?, "
            "quality_status=?, verified_at=?, updated_at=? WHERE id=?",
            (*identity, now, now, packet_id))
        conn.commit()
        row = conn.execute("SELECT * FROM commercial_proposal_packets WHERE id=?",
                           (packet_id,)).fetchone()
        return {"status": "ok", "verified": True, "proposal": _packet(row)}
    finally:
        conn.close()


def record_send(packet_id: str, channel: str, evidence_ref: str,
                idempotency_key: str, recipient: str | None = None,
                sent_at: int | None = None) -> dict:
    if not all(str(v or "").strip() for v in (channel, evidence_ref, idempotency_key)):
        return _error("send_evidence_required", "channel, evidence_ref and idempotency_key are required")
    conn = db.get_conn()
    try:
        packet = conn.execute("SELECT * FROM commercial_proposal_packets WHERE id=?",
                              (packet_id,)).fetchone()
        if packet is None:
            return _error("not_found", "proposal packet not found")
        if not packet["verified_at"]:
            return _error("not_verified", "verify the exact package before recording a send")
        old = conn.execute("SELECT * FROM commercial_proposal_sends WHERE idempotency_key=?",
                           (idempotency_key,)).fetchone()
        identity = (packet_id, channel, recipient, evidence_ref)
        if old:
            stored = tuple(old[k] for k in ("packet_id", "channel", "recipient", "evidence_ref"))
            if stored != identity:
                return _error("idempotency_conflict", "idempotency key already records another send")
            return {"status": "exists", "created": False, "send": dict(old)}
        now = _now()
        send_id = f"psend_{uuid.uuid4().hex[:12]}"
        effective_sent_at = int(sent_at or now)
        conn.execute(
            "INSERT INTO commercial_proposal_sends "
            "(id,packet_id,channel,recipient,evidence_ref,sent_at,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (send_id, packet_id, channel, recipient, evidence_ref,
             effective_sent_at, idempotency_key, now))
        crm._log(conn, packet["deal_id"], "proposal_sent", {
            "packet_id": packet_id, "revision": packet["revision"],
            "channel": channel, "recipient": recipient,
            "evidence_ref": evidence_ref, "sent_at": effective_sent_at,
        })
        conn.commit()
        row = conn.execute("SELECT * FROM commercial_proposal_sends WHERE id=?",
                           (send_id,)).fetchone()
        return {"status": "ok", "created": True, "send": dict(row)}
    finally:
        conn.close()


def latest_for_deals(deal_ids: list[str]) -> dict[str, dict]:
    """Batch read for radar/context surfaces; state is derived, never cached."""
    if not deal_ids:
        return {}
    conn = db.get_conn()
    try:
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='commercial_proposal_packets'"
        ).fetchone() is None:
            return {}
        marks = ",".join("?" for _ in deal_ids)
        rows = conn.execute(
            "SELECT p.*, EXISTS(SELECT 1 FROM commercial_proposal_sends s WHERE s.packet_id=p.id) AS has_send "
            "FROM commercial_proposal_packets p JOIN ("
            " SELECT deal_id, MAX(revision) revision FROM commercial_proposal_packets "
            f" WHERE deal_id IN ({marks}) GROUP BY deal_id"
            ") x ON x.deal_id=p.deal_id AND x.revision=p.revision", deal_ids).fetchall()
        return {r["deal_id"]: {**dict(r), "proposal_state":
                ("sent" if r["has_send"] else "verified" if r["verified_at"] else "draft")}
                for r in rows}
    finally:
        conn.close()
