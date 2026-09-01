"""A touch is stamped when it HAPPENED, not when it was approved.

Found 2026-08-11 approving the first Fireflies-derived proposals: a client
meeting from weeks earlier was filed as a touch, and the CRM recorded it as touched
TODAY — the radar flipped a 24-day-cold deal to "warm, 0d". The touch clock
lied again, in the opposite direction from the bug the whole inbox exists to
fix. (Sol's original critique flagged exactly this: record_touch stamps the
approval date rather than the external signal's date.)

Two rules, both contracted here:
  1. `on_date` stamps the real date of the interaction — on the deal AND on
     the audit event, so a later reader sees when it happened, not when it
     was entered.
  2. A BACKDATED touch never rewinds a fresher clock and never rewrites the
     forward plan: recording a July meeting today must not move a deal whose
     last touch is August, nor wipe a next_touch_date the operator set. A
     future date is refused — a touch that has not happened is not a touch.

Run:  python -m pytest tests/test_touch_backdating.py -v
"""
import datetime
import time
import uuid

import pytest

from dashboard import db, growth


def _day(offset: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


@pytest.fixture
def deal():
    suffix = uuid.uuid4().hex[:10]
    account_id, deal_id = f"acct_bd_{suffix}", f"deal_bd_{suffix}"
    now = int(time.time())
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (account_id, f"Backdate {suffix}", now))
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "touch_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (deal_id, account_id, f"Deal backdate {suffix}", "proposal", 1000.0,
             "MXN", 0, now, now))
        conn.commit()
    finally:
        conn.close()
    yield deal_id
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM deal_events WHERE deal_id = ?", (deal_id,))
        conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def _deal(deal_id):
    conn = db.get_conn()
    try:
        return conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    finally:
        conn.close()


def _set(deal_id, **cols):
    conn = db.get_conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE deals SET {sets} WHERE id = ?",
                     (*cols.values(), deal_id))
        conn.commit()
    finally:
        conn.close()


def test_on_date_stamps_the_real_day(deal):
    res = growth.record_touch(deal, note="junta real", on_date=_day(-20))
    assert res["status"] == "ok"
    assert _deal(deal)["last_touch_date"] == _day(-20)
    assert res["last_touch_date"] == _day(-20)


def test_the_audit_event_carries_the_real_day(deal):
    import json
    growth.record_touch(deal, note="junta real", on_date=_day(-20))
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = 'touch' "
            "ORDER BY rowid DESC LIMIT 1", (deal,)).fetchone()
    finally:
        conn.close()
    assert json.loads(row["payload"])["occurred_on"] == _day(-20)


def test_a_backdated_touch_never_rewinds_a_fresher_clock(deal):
    _set(deal, last_touch_date=_day(-2))
    growth.record_touch(deal, on_date=_day(-20))
    row = _deal(deal)
    assert row["last_touch_date"] == _day(-2), "the freshest real contact wins"
    assert row["touch_count"] == 1, "but the touch still counts — it happened"


def test_a_backdated_touch_does_not_rewrite_the_forward_plan(deal):
    _set(deal, next_touch_date=_day(3))
    growth.record_touch(deal, on_date=_day(-20))
    assert _deal(deal)["next_touch_date"] == _day(3), \
        "recording history must not clobber the operator's plan"


def test_a_touch_today_still_sets_the_next_touch(deal):
    growth.record_touch(deal, next_in_days=7)
    row = _deal(deal)
    assert row["last_touch_date"] == _day(0)
    assert row["next_touch_date"] == _day(7)


def test_todays_date_is_a_valid_touch(deal):
    # The boundary: today happened; only the future has not.
    res = growth.record_touch(deal, on_date=_day(0))
    assert res["status"] == "ok"
    assert _deal(deal)["last_touch_date"] == _day(0)


def test_the_default_cadence_is_seven_days(deal):
    growth.record_touch(deal)          # no next_in_days — exercise the default
    assert _deal(deal)["next_touch_date"] == _day(7)


def test_an_absurd_cadence_still_schedules_tomorrow(deal):
    # next_in_days=0 would mean "next touch today", which the stale sweep would
    # read as already-due the moment it is written.
    growth.record_touch(deal, next_in_days=0)
    assert _deal(deal)["next_touch_date"] == _day(1)


def test_warm_kinds_keep_their_own_name_in_the_ledger(deal):
    growth.record_touch(deal, kind="warm_touch")
    growth.record_touch(deal, kind="referral_ask")
    conn = db.get_conn()
    try:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM deal_events WHERE deal_id = ? ORDER BY rowid", (deal,))]
    finally:
        conn.close()
    assert "warm_touch" in kinds and "referral_ask" in kinds


def test_each_touch_increments_the_count(deal):
    growth.record_touch(deal)
    growth.record_touch(deal)
    assert _deal(deal)["touch_count"] == 2


def test_the_event_records_the_channel(deal):
    import json
    _set(deal, lead_source="referral")
    growth.record_touch(deal, note="con canal")
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = 'touch' "
            "ORDER BY rowid DESC LIMIT 1", (deal,)).fetchone()
    finally:
        conn.close()
    assert json.loads(row["payload"])["channel"] == "referral"


def test_an_unknown_deal_is_a_typed_error():
    res = growth.record_touch("deal_no_existe")
    assert res["status"] == "error"
    assert res["error"] == "deal not found"


def test_a_future_touch_is_refused(deal):
    res = growth.record_touch(deal, on_date=_day(1))
    assert res["status"] == "error"
    assert _deal(deal)["touch_count"] == 0, "nothing recorded"


def test_a_malformed_date_is_refused(deal):
    assert growth.record_touch(deal, on_date="mañana")["status"] == "error"
    assert _deal(deal)["touch_count"] == 0


def test_approving_a_dated_proposal_stamps_the_evidence_date(deal):
    from dashboard import crm_proposals
    from dashboard.migrations.m27_crm_proposals import m27_crm_proposals
    conn = db.get_conn()
    try:
        m27_crm_proposals(conn)
        conn.commit()
    finally:
        conn.close()
    res = crm_proposals.create(
        deal, "touch", {"evidence_date": _day(-20), "note": "junta Fireflies"},
        "fireflies", f"T-BD-{uuid.uuid4().hex[:6]}")
    assert crm_proposals.approve(res["id"])["status"] == "ok"
    assert _deal(deal)["last_touch_date"] == _day(-20), \
        "the inbox must record when the meeting happened, not when it was approved"
