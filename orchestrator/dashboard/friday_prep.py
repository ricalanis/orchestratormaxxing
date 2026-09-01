"""Deterministic Thursday preparation for the Friday growth block.

A pure read: it reports the growth gates and the few CRM records worth acting
on without narrating or changing their state. Velocity intentionally lives in
its own verb (get_velocity) — the brief carries only what the ritual reads.
Contract: tests/test_friday_prep.py (authored before this module).
"""
import datetime
import time

from . import db, crm, growth, crm_proposals


_OPEN_STAGES = crm.OPEN_STAGES
_PIPELINE_STAGES = crm.ACTIVE_PIPELINE_STAGES


def _days_idle(row, reference_date, reference_epoch):
    """Touch clock when present, edit clock as fallback (never-touched deals
    still rank — they are the coldest relationships of all)."""
    touch = row["last_touch_date"]
    if touch:
        try:
            touched = datetime.date.fromisoformat(str(touch)[:10])
            return max(0, (reference_date - touched).days)
        except ValueError:
            pass
    updated_at = row["updated_at"]
    if updated_at is None:
        return 0
    return max(0, int((reference_epoch - int(updated_at)) // 86400))


def _crm_blocks(conn, reference_date, reference_epoch):
    marks = ",".join("?" for _ in _OPEN_STAGES)
    rows = conn.execute(
        "SELECT id, title, last_touch_date, updated_at FROM deals "
        f"WHERE stage IN ({marks})",
        _OPEN_STAGES,
    ).fetchall()

    ranked = sorted(
        ((row, _days_idle(row, reference_date, reference_epoch)) for row in rows),
        key=lambda item: (-item[1], str(item[0]["id"])),
    )[:3]
    generosity = []
    for row, idle in ranked:
        generosity.append({
            "deal_id": row["id"],
            "deal_title": row["title"],
            "days_idle": idle,
            "reason": f"Lleva {idle} días sin un toque; conviene aportar valor primero.",
            "draft": (
                f"Hola, encontré algo que te puede servir sobre {row['title']}. "
                "Te lo comparto por si te ayuda; sin prisa por responder."
            ),
        })

    cutoff = reference_epoch - 14 * 86400
    won = conn.execute(
        "SELECT id, title, COALESCE(closed_at, updated_at) AS moment "
        "FROM deals WHERE stage = 'won' "
        "AND COALESCE(closed_at, updated_at) >= ? "
        "AND COALESCE(closed_at, updated_at) <= ? "
        "ORDER BY moment DESC, id ASC LIMIT 1",
        (cutoff, reference_epoch),
    ).fetchone()
    referral = None
    if won is not None:
        referral = {
            "deal_id": won["id"],
            "deal_title": won["title"],
            "reason": "Victoria reciente: es un momento natural para pedir una referencia.",
            "draft": (
                f"Me dio mucho gusto el resultado de {won['title']}. "
                "Si conoces a alguien a quien algo así le pueda servir, "
                "con gusto primero le comparto una idea útil."
            ),
        }
    return generosity, referral


def render_md(brief=None) -> str:
    """Telegram rendering of the brief — ≤25 lines, deep links on the canonical
    URL (loopback links die inside Telegram), read/nudge only: every action the
    text invites happens in the authenticated dashboard."""
    b = brief if brief is not None else compose()
    base = db.dashboard_url()
    today = datetime.date.today().isoformat()
    gates = {g["key"]: g for g in b.get("gates", [])}
    lines = [f"🔥 Brief del viernes — {today}"]
    row = []
    for key, label in (("content", "📣"), ("touches", "🤝"), ("proposals", "📄")):
        g = gates.get(key)
        if g:
            row.append(f"{label} {g['value']}/{g['target'] if g['target'] is not None else '—'}")
    if row:
        lines.append("Semana: " + " · ".join(row))
    pend = b.get("pending_proposals", [])
    if pend:
        lines.append(f"🗂 {len(pend)} correcciones por decidir → {base}/?tab=crm")
    stale = b.get("stale", [])
    if stale:
        lines.append(f"❄ Fríos (≥7d): {len(stale)}")
        for d in stale[:3]:
            lines.append(f"  · {d['title']} — {d['days_idle']}d")
    for c in b.get("generosity_candidates", [])[:3]:
        lines.append(f"🎁 {c['deal_title']} ({c['days_idle']}d)")
    ref = b.get("referral_candidate")
    if ref:
        lines.append(f"🙏 Momento alto: {ref['deal_title']}")
    lines.append(f"Bloque viernes 10:00 — radar y 5 preguntas → {base}/?tab=crm")
    return "\n".join(lines[:25])


def compose(today=None) -> dict:
    """Compose the pure-read preparation brief for Friday's growth ritual.
    `today` is accepted for API stability but the brief always composes NOW —
    a backdated brief would be a narrated one."""
    reference_date, reference_epoch = datetime.date.today(), int(time.time())
    conn = db.get_conn()
    try:
        pipeline_marks = ",".join("?" for _ in _PIPELINE_STAGES)
        pipeline = conn.execute(
            "SELECT COALESCE(SUM(value), 0) AS total FROM deals "
            f"WHERE stage IN ({pipeline_marks})",
            _PIPELINE_STAGES,
        ).fetchone()["total"]
        generosity, referral = _crm_blocks(
            conn, reference_date, reference_epoch
        )
    finally:
        conn.close()

    return {
        "gates": growth.scorecard()["kpis"],
        "tablero": {"pipeline_value": pipeline},
        "stale": crm.detect_stale_deals(7, include_stalled=True),
        "pending_proposals": crm_proposals.list_proposals("proposed"),
        "generosity_candidates": generosity,
        "referral_candidate": referral,
    }
