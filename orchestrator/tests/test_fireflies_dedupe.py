"""One cached row per (deal, transcript) — re-fetch REFRESHES, never duplicates.

Found 2026-08-10 running the first full backfill: fetching a deal twice left
two rows for the same meeting. `fireflies_meeting_insert` uses INSERT OR
REPLACE, but the conflict target is the PRIMARY KEY `id` and the caller minted
a fresh uuid on every write, so the REPLACE could never fire. The natural key
is (deal_id, transcript_id) and nothing enforced it.

Two layers, both contracted here:
  1. The row id is DERIVED from (deal_id, transcript_id), so the existing
     INSERT OR REPLACE collapses a re-fetch onto the same row.
  2. Migration m30 dedupes what already leaked and adds the UNIQUE index, so
     the guarantee is structural rather than a convention a future caller can
     forget. (A duplicated cache silently inflates meetings_analyzed, the
     readiness signals and the coaching metrics that read it.)

Run:  python -m pytest tests/test_fireflies_dedupe.py -v
"""
import time
import uuid
from unittest.mock import patch

import pytest

from dashboard import db, fireflies


def _seed(contact_email):
    suffix = uuid.uuid4().hex[:12]
    account_id, contact_id = f"acct_ff_dup_{suffix}", f"cont_ff_dup_{suffix}"
    deal_id = f"deal_ff_dup_{suffix}"
    now = int(time.time())
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (account_id, f"Dedupe {suffix}", now))
        conn.execute("INSERT INTO contacts (id, account_id, name, email, created_at) "
                     "VALUES (?,?,?,?,?)",
                     (contact_id, account_id, "Contacto", contact_email, now))
        conn.execute(
            "INSERT INTO deals (id, account_id, contact_id, title, stage, value, "
            "currency, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (deal_id, account_id, contact_id, f"Deal dedupe {suffix}", "proposal",
             1000.0, "MXN", now, now))
        conn.commit()
    finally:
        conn.close()
    return deal_id, account_id


@pytest.fixture
def deal():
    deal_id, account_id = _seed("dup@dedupe.test")
    yield deal_id
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM fireflies_meetings WHERE deal_id = ?", (deal_id,))
        conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        conn.execute("DELETE FROM contacts WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def _transcript(tid, date, email, title="Junta"):
    return {
        "id": tid, "title": title, "date": date, "participants": [email],
        "summary": {"overview": "", "action_items": "", "keywords": []},
        "sentences": [{"speaker_name": "Op", "text": "hola"}],
    }


def _rows(deal_id):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT id, transcript_id, title, fetched_at FROM fireflies_meetings "
            "WHERE deal_id = ? ORDER BY transcript_id", (deal_id,)).fetchall()
    finally:
        conn.close()


def test_refetching_the_same_meeting_keeps_one_row(deal):
    t = [_transcript("T-DUP", "2026-08-01", "dup@dedupe.test")]
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=t):
        first = fireflies.fetch_and_store_for_deal(deal)
        second = fireflies.fetch_and_store_for_deal(deal)
    assert first["stored"] == 1 and second["stored"] == 1
    rows = _rows(deal)
    assert len(rows) == 1, "a re-fetch must refresh the row, not duplicate it"


def test_a_refetch_refreshes_the_stored_payload(deal):
    old = [_transcript("T-DUP", "2026-08-01", "dup@dedupe.test", title="Título viejo")]
    new = [_transcript("T-DUP", "2026-08-01", "dup@dedupe.test", title="Título nuevo")]
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=old):
        fireflies.fetch_and_store_for_deal(deal)
    before = _rows(deal)[0]
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=new):
        fireflies.fetch_and_store_for_deal(deal)
    after = _rows(deal)[0]
    assert after["title"] == "Título nuevo", "the refresh must win, not be dropped"
    assert after["id"] == before["id"], "same natural key → same row id"


def test_distinct_meetings_still_get_their_own_rows(deal):
    ts = [_transcript("T-A", "2026-08-01", "dup@dedupe.test"),
          _transcript("T-B", "2026-08-02", "dup@dedupe.test")]
    with patch.object(fireflies, "_api_key", return_value="k"), \
         patch.object(fireflies, "fetch_transcripts", return_value=ts):
        fireflies.fetch_and_store_for_deal(deal)
    assert [r["transcript_id"] for r in _rows(deal)] == ["T-A", "T-B"]


def test_the_same_transcript_on_two_deals_is_not_collapsed():
    # Two deals can legitimately share one meeting (a joint call): the natural
    # key is the PAIR, so neither may overwrite the other.
    d1, a1 = _seed("shared@dedupe.test")
    d2, a2 = _seed("shared@dedupe.test")
    try:
        t = [_transcript("T-SHARED", "2026-08-01", "shared@dedupe.test")]
        with patch.object(fireflies, "_api_key", return_value="k"), \
             patch.object(fireflies, "fetch_transcripts", return_value=t):
            fireflies.fetch_and_store_for_deal(d1)
            fireflies.fetch_and_store_for_deal(d2)
        assert len(_rows(d1)) == 1 and len(_rows(d2)) == 1
        assert _rows(d1)[0]["id"] != _rows(d2)[0]["id"]
    finally:
        conn = db.get_conn()
        try:
            for did, aid in ((d1, a1), (d2, a2)):
                conn.execute("DELETE FROM fireflies_meetings WHERE deal_id = ?", (did,))
                conn.execute("DELETE FROM deals WHERE id = ?", (did,))
                conn.execute("DELETE FROM contacts WHERE account_id = ?", (aid,))
                conn.execute("DELETE FROM accounts WHERE id = ?", (aid,))
            conn.commit()
        finally:
            conn.close()


def test_migration_dedupes_and_locks_the_natural_key(deal):
    from dashboard.migrations.m30_fireflies_dedupe import m30_fireflies_dedupe
    conn = db.get_conn()
    try:
        # Reconstruct the PRE-migration world: another module importing
        # dashboard.api runs the migration chain against the shared session
        # sandbox, so by the time this test runs the index may already exist —
        # and then the duplicates it needs to create are unrepresentable.
        conn.execute("DROP INDEX IF EXISTS idx_fireflies_deal_transcript")
        # Two legacy rows for one meeting, exactly as the pre-fix writer left them.
        for n, when in ((1, 100), (2, 200)):
            conn.execute(
                "INSERT INTO fireflies_meetings (id, deal_id, transcript_id, title, "
                "meeting_date, fetched_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (f"ffm_legacy_{n}_{uuid.uuid4().hex[:6]}", deal, "T-LEGACY",
                 f"copia {n}", "2026-07-01", when, when))
        conn.commit()
        assert len(_rows(deal)) == 2
        m30_fireflies_dedupe(conn)
        conn.commit()
        rows = _rows(deal)
        assert len(rows) == 1, "the migration collapses the duplicates"
        assert rows[0]["fetched_at"] == 200, "the NEWEST row survives"
        # And the key is now enforced by the schema, not by convention:
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO fireflies_meetings (id, deal_id, transcript_id, "
                "created_at) VALUES (?,?,?,?)",
                (f"ffm_x_{uuid.uuid4().hex[:6]}", deal, "T-LEGACY", 300))
            conn.commit()
        conn.rollback()
        # Idempotent: running it again on a clean table is a no-op.
        m30_fireflies_dedupe(conn)
        conn.commit()
        assert len(_rows(deal)) == 1
    finally:
        conn.close()
