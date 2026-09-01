"""crm_proposals — the propose-only CRM correction inbox (motor caliente, m27).

Why: Hermes sees meetings and deals, not Gmail/WhatsApp, so on 2026-08-09 the
CRM accused the operator falsely (a deal recorded far below the amount actually
quoted; another reported cold during its busiest week). This module turns
external signals into *proposals* a human approves — it NEVER mutates the CRM
on its own, and approval applies only through the existing audited writers
(growth.record_touch / growth.update_deal_growth / crm.update_deal).

Flow (v1): the Thursday session calls fetch_fireflies_for_deal on the live
deals (existing tool — the fireflies_meetings cache is empty until fetched),
then derive() turns cache-vs-touch-clock gaps into touch proposals, and the
interactive Gmail pass files amount/next_touch corrections via create().
Friday, the operator approves or dismisses each card.

Invariants (tested in tests/test_crm_proposals.py):
  * derive() is read-only on deals and idempotent (UNIQUE evidence key).
  * dismissed is STICKY — a rejected (deal, kind, evidence) never re-appears.
  * approve() is a conditional-claim saga (proposed → applying → approved)
    with a deal_events marker for crash reconciliation, so a retry or a
    double click can never double-apply (mirrors digestion.accept_suggestion).
"""
import datetime
import json
import time
import uuid
from typing import Optional

from . import db
from . import crm
from . import growth

KINDS = ("touch", "next_touch", "amount")
EVIDENCE_KINDS = ("fireflies", "whatsapp", "manual")
_OPEN_STAGES = crm.OPEN_STAGES
_MARKER_EVENT = "proposal_applied"


def _now() -> int:
    return int(time.time())


def _validate_payload(kind: str, payload: dict) -> Optional[str]:
    """Per-kind payload contract; returns an error string or None."""
    if kind == "next_touch":
        date = payload.get("date")
        try:
            datetime.date.fromisoformat(str(date))
        except (TypeError, ValueError):
            return "next_touch payload needs date YYYY-MM-DD"
    elif kind == "amount":
        value = payload.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return "amount payload needs a non-negative numeric value"
    elif kind == "touch":
        date = payload.get("evidence_date")
        if date is not None:
            try:
                datetime.date.fromisoformat(str(date)[:10])
            except (TypeError, ValueError):
                return "touch evidence_date must be YYYY-MM-DD when present"
    return None


def create(deal_id: str, kind: str, payload: dict, evidence_kind: str,
           evidence_ref: str, conn=None) -> dict:
    """File one proposal. Idempotent on (deal_id, kind, evidence_ref); a row in
    ANY status (including dismissed — sticky) short-circuits to that row."""
    if kind not in KINDS:
        return {"status": "error", "error": f"kind must be one of {KINDS}"}
    if evidence_kind not in EVIDENCE_KINDS:
        return {"status": "error", "error": f"evidence_kind must be one of {EVIDENCE_KINDS}"}
    if not evidence_ref:
        return {"status": "error", "error": "evidence_ref is required"}
    err = _validate_payload(kind, payload or {})
    if err:
        return {"status": "error", "error": err}
    own = conn is None
    conn = conn or db.get_conn()
    try:
        deal = conn.execute("SELECT id FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            return {"status": "error", "error": "deal not found"}
        existing = conn.execute(
            "SELECT id, status FROM crm_proposals WHERE deal_id = ? AND kind = ? "
            "AND evidence_ref = ?", (deal_id, kind, evidence_ref)).fetchone()
        if existing is not None:
            return {"status": "exists", "id": existing["id"],
                    "proposal_status": existing["status"], "created": False}
        pid = f"cprop_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO crm_proposals (id, deal_id, kind, payload, evidence_kind, "
            "evidence_ref, status, created_at) VALUES (?,?,?,?,?,?, 'proposed', ?)",
            (pid, deal_id, kind, json.dumps(payload or {}), evidence_kind,
             evidence_ref, _now()))
        if own:
            conn.commit()
        return {"status": "ok", "id": pid, "created": True}
    finally:
        if own:
            conn.close()


def derive() -> dict:
    """Fireflies arm (v1): a cached meeting newer than the deal's touch clock
    becomes a touch proposal. Read-only on deals; idempotent; never-touched
    deals (last_touch_date NULL) are included. Returns counts, never pads."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT d.id AS deal_id, d.last_touch_date, m.transcript_id, "
            " m.meeting_date, m.title "
            "FROM deals d JOIN fireflies_meetings m ON m.deal_id = d.id "
            f"WHERE d.stage IN ({','.join('?' * len(_OPEN_STAGES))}) "
            "AND m.meeting_date IS NOT NULL",
            _OPEN_STAGES).fetchall()
        created = skipped = 0
        for r in rows:
            meeting_day = str(r["meeting_date"])[:10]
            touch = r["last_touch_date"]
            if touch is not None and str(touch)[:10] >= meeting_day:
                skipped += 1
                continue
            res = create(
                r["deal_id"], "touch",
                {"evidence_date": meeting_day,
                 "note": f"Reunión Fireflies: {r['title'] or r['transcript_id']}"},
                "fireflies", r["transcript_id"], conn=conn)
            if res.get("created"):
                created += 1
            else:
                skipped += 1
        conn.commit()
        return {"status": "ok", "created": created, "skipped": skipped}
    finally:
        conn.close()


def list_proposals(status: Optional[str] = "proposed") -> list:
    conn = db.get_conn()
    try:
        q = ("SELECT p.id, p.deal_id, d.title AS deal_title, "
             "d.value AS deal_value, d.currency AS deal_currency, "
             "p.kind, p.payload, "
             "p.evidence_kind, p.evidence_ref, p.status, p.created_at, "
             "p.decided_at, p.decided_via, p.applied_ref "
             "FROM crm_proposals p JOIN deals d ON d.id = p.deal_id ")
        args: tuple = ()
        if status:
            q += "WHERE p.status = ? "
            args = (status,)
        q += "ORDER BY p.created_at DESC"
        out = []
        for r in conn.execute(q, args).fetchall():
            rec = dict(r)
            try:
                rec["payload"] = json.loads(rec["payload"] or "{}")
            except ValueError:
                rec["payload"] = {}
            out.append(rec)
        return out
    finally:
        conn.close()


def _marker_exists(conn, pid: str, deal_id: str) -> bool:
    rows = conn.execute(
        "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = ?",
        (deal_id, _MARKER_EVENT)).fetchall()
    for r in rows:
        try:
            if json.loads(r["payload"] or "{}").get("proposal_id") == pid:
                return True
        except ValueError:
            continue
    return False


def approve(pid: str, via: str = "dashboard") -> dict:
    """The human gate. Conditional-claim saga, safe to retry:
    1. claim proposed → applying (a lost claim mutates nothing);
    2. apply through the audited writer for the kind;
    3. write the deal_events marker, then mark approved.
    A crashed attempt that already applied is ADOPTED via the marker instead
    of applied twice (digestion.accept_suggestion's reconciliation rule)."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM crm_proposals WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return {"status": "error", "error": "proposal not found"}
        now = _now()
        claimed = conn.execute(
            "UPDATE crm_proposals SET status = 'applying', decided_at = ?, "
            "decided_via = ? WHERE id = ? AND status = 'proposed'",
            (now, via, pid))
        conn.commit()
        if claimed.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM crm_proposals WHERE id = ?", (pid,)).fetchone()
            if row["status"] == "applying" and _marker_exists(conn, pid, row["deal_id"]):
                conn.execute(
                    "UPDATE crm_proposals SET status = 'approved' "
                    "WHERE id = ? AND status = 'applying'", (pid,))
                conn.commit()
                return {"status": "ok", "id": pid, "adopted": True}
            return {"status": "error",
                    "error": f"proposal already {row['status']} — approve applies once"}
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        deal_id = row["deal_id"]
        kind = row["kind"]
        try:
            if kind == "touch":
                note = payload.get("note") or f"Toque (evidencia {row['evidence_kind']})"
                if payload.get("evidence_date"):
                    note += f" · fecha real: {payload['evidence_date']}"
                # Stamp the day the interaction HAPPENED, not the approval day:
                # approving a July meeting must not report the deal as touched
                # today (the touch clock lying in the other direction).
                applied = growth.record_touch(
                    deal_id, note=note, on_date=payload.get("evidence_date"))
            elif kind == "next_touch":
                applied = growth.update_deal_growth(
                    deal_id, next_touch_date=payload["date"])
            else:  # amount — validated at create()
                applied = crm.update_deal(deal_id, value=float(payload["value"]))
            if not isinstance(applied, dict) or applied.get("status") == "error":
                raise RuntimeError(f"writer rejected: {applied}")
        except Exception as exc:
            conn.execute(
                "UPDATE crm_proposals SET status = 'proposed', decided_at = NULL, "
                "decided_via = NULL WHERE id = ? AND status = 'applying'", (pid,))
            conn.commit()
            return {"status": "error", "error": f"apply failed, proposal released: {exc}"}
        applied_ref = f"{kind}:{deal_id}:{now}"
        crm._log(conn, deal_id, _MARKER_EVENT,
                 {"proposal_id": pid, "kind": kind,
                  "evidence_kind": row["evidence_kind"],
                  "evidence_ref": row["evidence_ref"]},
                 source="proposal")
        conn.execute(
            "UPDATE crm_proposals SET status = 'approved', applied_ref = ? "
            "WHERE id = ? AND status = 'applying'", (applied_ref, pid))
        conn.commit()
        return {"status": "ok", "id": pid, "applied_ref": applied_ref}
    finally:
        conn.close()


def dismiss(pid: str, via: str = "dashboard") -> dict:
    """Sticky rejection: the row stays forever, so derive() can never re-nag."""
    conn = db.get_conn()
    try:
        res = conn.execute(
            "UPDATE crm_proposals SET status = 'dismissed', decided_at = ?, "
            "decided_via = ? WHERE id = ? AND status = 'proposed'",
            (_now(), via, pid))
        conn.commit()
        if res.rowcount == 0:
            row = conn.execute(
                "SELECT status FROM crm_proposals WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return {"status": "error", "error": "proposal not found"}
            return {"status": "error",
                    "error": f"proposal already {row['status']}"}
        return {"status": "ok", "id": pid}
    finally:
        conn.close()
