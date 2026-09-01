"""The Fireflies fetch WINDOW — honoring the caller, and reporting it.

Regression (2026-08-10): fetch_and_store_for_deal hardcoded
fetch_transcripts(limit=25) and silently ignored the caller's `limit`, so a
deal whose meetings sit outside the 25 most recent transcripts returned
`stored: 0` — indistinguishable from "this deal has no meetings". With ~62
meetings in 30 days that window is ~12 days: fine for the weekly cadence,
useless for backfill (a client's Jul 22-23 meetings stayed invisible even
after its contact emails were populated).

Two fixes, both contracted here:
  1. `limit` (and an optional `since` ISO date floor) reach fetch_transcripts.
  2. The result reports the window it actually looked at — `scanned` — so a
     caller can tell "0 matched out of 40 scanned" from "0 scanned". A count
     you cannot falsify is a claim, not a measurement.

Run:  python -m pytest tests/test_fireflies_window.py -v
"""
import time
import uuid
from unittest.mock import patch

import pytest

from dashboard import db, fireflies


def _seed_deal(contact_email):
    suffix = uuid.uuid4().hex[:12]
    account_id = f"acct_ff_win_{suffix}"
    contact_id = f"cont_ff_win_{suffix}"
    deal_id = f"deal_ff_win_{suffix}"
    now = int(time.time())
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (account_id, f"Ventana {suffix}", now))
        conn.execute("INSERT INTO contacts (id, account_id, name, email, created_at) "
                     "VALUES (?,?,?,?,?)",
                     (contact_id, account_id, "Contacto", contact_email, now))
        conn.execute(
            "INSERT INTO deals (id, account_id, contact_id, title, stage, value, "
            "currency, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (deal_id, account_id, contact_id, f"Deal ventana {suffix}", "proposal",
             1000.0, "MXN", now, now))
        conn.commit()
    finally:
        conn.close()
    return deal_id


def _cleanup(deal_id):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT account_id FROM deals WHERE id = ?", (deal_id,)).fetchone()
        conn.execute("DELETE FROM fireflies_meetings WHERE deal_id = ?", (deal_id,))
        conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        if row:
            conn.execute("DELETE FROM contacts WHERE account_id = ?", (row["account_id"],))
            conn.execute("DELETE FROM accounts WHERE id = ?", (row["account_id"],))
        conn.commit()
    finally:
        conn.close()


def _transcript(tid, date, email):
    return {
        "id": tid, "title": f"Junta {tid}", "date": date,
        "participants": [email],
        "summary": {"overview": "", "action_items": "", "keywords": []},
        "sentences": [{"speaker_name": "Op", "text": "hola"}],
    }


@pytest.fixture
def deal():
    did = _seed_deal("contacto@ventana.test")
    yield did
    _cleanup(did)


def test_caller_limit_reaches_the_query(deal):
    seen = {}

    def fake_fetch(limit=25, after_date=None):
        seen["limit"] = limit
        seen["after_date"] = after_date
        return []

    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", side_effect=fake_fetch):
        fireflies.fetch_and_store_for_deal(deal, limit=80)
    assert seen["limit"] == 80, "the caller's window must reach fetch_transcripts"


def test_a_window_wider_than_the_api_allows_is_clamped_not_rejected(deal):
    # Measured 2026-08-10: the Fireflies API serves limit=50 and rejects 60/100
    # with invalid_arguments. A caller asking for backfill must get the widest
    # LEGAL look, not an error — the old clamp of 100 produced the error.
    sent = {}

    def fake_graphql(query, variables=None):
        sent["limit"] = (variables or {}).get("limit")
        return {"transcripts": []}

    with patch.object(fireflies, "_graphql", side_effect=fake_graphql):
        fireflies.fetch_transcripts(limit=500)
    assert sent["limit"] == fireflies.MAX_TRANSCRIPT_WINDOW == 50

    with patch.object(fireflies, "_graphql", side_effect=fake_graphql):
        fireflies.fetch_transcripts(limit=0)
    assert sent["limit"] == 1, "a floor of 1 keeps a bad ask from becoming a bad query"


def test_default_window_stays_twenty_five(deal):
    seen = {}

    def fake_fetch(limit=25, after_date=None):
        seen["limit"] = limit
        return []

    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", side_effect=fake_fetch):
        fireflies.fetch_and_store_for_deal(deal)
    assert seen["limit"] == 25, "back-compat: the default window is unchanged"


def test_since_floor_is_passed_through(deal):
    seen = {}

    def fake_fetch(limit=25, after_date=None):
        seen["after_date"] = after_date
        return []

    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", side_effect=fake_fetch):
        fireflies.fetch_and_store_for_deal(deal, since="2026-07-01")
    assert seen["after_date"] == "2026-07-01"


def test_result_reports_the_window_it_scanned(deal):
    # THE regression: stored=0 must be distinguishable from scanned=0.
    others = [_transcript(f"T{i}", "2026-08-0" + str(i % 9 + 1), "ajeno@otra.test")
              for i in range(4)]
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=others):
        res = fireflies.fetch_and_store_for_deal(deal, limit=50)
    assert res["stored"] == 0
    assert res["scanned"] == 4, "the caller must see how wide the look actually was"
    assert res["window"]["limit"] == 50

    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=[]):
        empty = fireflies.fetch_and_store_for_deal(deal)
    assert empty["stored"] == 0 and empty["scanned"] == 0


def test_a_matching_transcript_inside_the_window_is_stored(deal):
    rows = [
        _transcript("T-OLD", "2026-07-22", "contacto@ventana.test"),
        _transcript("T-OTHER", "2026-08-05", "ajeno@otra.test"),
    ]
    rows[0]["summary"]["overview"] = "Resumen real de la junta"
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=rows):
        res = fireflies.fetch_and_store_for_deal(deal, limit=100)
    assert res["stored"] == 1
    assert res["scanned"] == 2
    conn = db.get_conn()
    try:
        stored = conn.execute(
            "SELECT transcript_id, meeting_date, duration_seconds, raw_summary "
            "FROM fireflies_meetings WHERE deal_id = ?", (deal,)).fetchall()
    finally:
        conn.close()
    assert [r["transcript_id"] for r in stored] == ["T-OLD"]
    assert stored[0]["meeting_date"] == "2026-07-22"
    # The stored row must carry the transcript's own payload, not a husk:
    expected_duration = int(
        fireflies.extract_signals(rows[0]).get("total_duration") or 0)
    assert stored[0]["duration_seconds"] == expected_duration
    assert "Resumen real de la junta" in stored[0]["raw_summary"]


def test_an_empty_transcript_still_stores_what_signals_report(deal):
    # NOT a pinned constant: extract_signals floors total_duration at 1 as a
    # divide-by-zero guard for talk_ratio, so the storage step's `or 0`
    # fallback is unreachable. What IS required is that storage reports what
    # signals computed — it must never invent a number of its own.
    bare = _transcript("T-BARE", "2026-07-30", "contacto@ventana.test")
    bare["sentences"] = []
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=[bare]):
        res = fireflies.fetch_and_store_for_deal(deal)
    assert res["stored"] == 1
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT duration_seconds FROM fireflies_meetings WHERE transcript_id = 'T-BARE'"
        ).fetchone()
    finally:
        conn.close()
    assert row["duration_seconds"] == int(fireflies.extract_signals(bare)["total_duration"])


def test_no_api_key_shape_is_unchanged(deal):
    with patch.object(fireflies, "_api_key", return_value=None):
        res = fireflies.fetch_and_store_for_deal(deal)
    assert res == {"status": "no_api_key"}


def test_unknown_deal_says_deal_not_found(deal):
    # Not just "an error" — the RIGHT error. A generic status=error would also
    # pass if the deal check fell through and the API call blew up instead.
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=[]):
        res = fireflies.fetch_and_store_for_deal("deal_no_existe")
    assert res["status"] == "error"
    assert res["error"] == "deal not found"


def test_a_failing_fetch_is_a_typed_error_not_a_crash(deal):
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts",
                      side_effect=RuntimeError("fireflies 503")):
        res = fireflies.fetch_and_store_for_deal(deal)
    assert res["status"] == "error"
    assert "fireflies 503" in res["error"]
