"""Compose the Motor Caliente commercial-journey radar.

The rings are the real deal journey, while radius is the touch clock: nothing
moves inward because of a record edit or a signal source, only because of a
real touch.  This composer is deliberately a pure read of those facts.
"""
import datetime
import time

from . import db, crm, commercial_proposals


COOLING_DAYS = 4
WARM_DAYS = 7

_RING_STAGES = {
    "seguimiento": ("lead", "engaged"),
    "oportunidad": ("qualified", "demo"),
    "propuesta": ("proposal",),
}
_STAGE_RING = {
    stage: ring
    for ring, stages in _RING_STAGES.items()
    for stage in stages
}


def _item(row, now, today, proposal=None):
    rec = dict(row)
    days_idle, basis = crm._touch_idle(rec, now, today)
    if days_idle < COOLING_DAYS:
        warmth = "warm"
    elif days_idle < WARM_DAYS:
        warmth = "cooling"
    else:
        warmth = "stale"
    return {
        "deal_id": rec["id"],
        "title": rec["title"],
        "account_name": rec["account_name"],
        "value": rec["value"],
        "currency": rec["currency"],
        "days_idle": days_idle,
        "basis": basis,
        "warmth": warmth,
        "growth_loop": rec["growth_loop"],
        "project_id": rec["project_id"],
        "project_status": rec["project_status"],
        "proposal_revision": proposal.get("revision") if proposal else None,
        "proposal_state": proposal.get("proposal_state") if proposal else None,
    }


def compose() -> dict:
    """Return the current commercial journey, coldest relationships first."""
    now = int(time.time())
    today = datetime.date.today()
    visible_stages = (*crm.OPEN_STAGES, "won")
    marks = ",".join("?" for _ in visible_stages)

    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT d.id, d.title, d.stage, d.value, d.currency, "
            "d.last_touch_date, d.updated_at, d.growth_loop, d.project_id, "
            "a.name AS account_name, p.status AS project_status "
            "FROM deals d "
            "JOIN accounts a ON a.id = d.account_id "
            "LEFT JOIN projects p ON p.id = d.project_id "
            f"WHERE d.stage IN ({marks})",
            visible_stages,
        ).fetchall()
    finally:
        conn.close()

    rings = {name: [] for name in _RING_STAGES}
    centro = []
    orbita_fria = []
    won_sin_proyecto = []

    proposal_state = commercial_proposals.latest_for_deals([row["id"] for row in rows])
    for row in rows:
        stage = row["stage"]
        item = _item(row, now, today, proposal_state.get(row["id"]))
        if stage in crm.ACTIVE_PIPELINE_STAGES:
            ring = _STAGE_RING.get(stage)
            if ring is not None:
                rings[ring].append(item)
        elif stage == "stalled":
            orbita_fria.append(item)
        elif stage == "won" and row["project_id"] is None:
            won_sin_proyecto.append(item)
        elif stage == "won" and row["project_status"] == "active":
            centro.append(item)

    sections = {
        **rings,
        "centro": centro,
        "orbita_fria": orbita_fria,
        "won_sin_proyecto": won_sin_proyecto,
    }
    for items in sections.values():
        items.sort(key=lambda item: (-item["days_idle"], item["deal_id"]))

    return {
        "rings": rings,
        "centro": centro,
        "orbita_fria": orbita_fria,
        "won_sin_proyecto": won_sin_proyecto,
        "meta": {
            "warm_days": WARM_DAYS,
            "counts": {name: len(items) for name, items in sections.items()},
        },
    }
