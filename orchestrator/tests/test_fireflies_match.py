"""Focused regression tests for Fireflies transcript-to-deal matching.

Lives in orchestrator/tests/, so the suite-wide conftest sandbox (DB copy +
live-DB tripwire) loads automatically — no manual bootstrap needed.
"""
import time
import uuid

import pytest

from dashboard import db, fireflies


@pytest.fixture
def deal_factory():
    created = []

    def seed(*, contact_email=None, account_domain=None, account_emails=()):
        suffix = uuid.uuid4().hex[:12]
        account_id = f"acct_ff_match_{suffix}"
        deal_id = f"deal_ff_match_{suffix}"
        contact_id = f"cont_ff_match_{suffix}" if contact_email else None
        now = int(time.time())

        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO accounts (id, name, domain, created_at) VALUES (?,?,?,?)",
                (account_id, f"Fireflies match {suffix}", account_domain, now),
            )
            if contact_id:
                conn.execute(
                    "INSERT INTO contacts "
                    "(id, account_id, name, email, created_at) VALUES (?,?,?,?,?)",
                    (contact_id, account_id, "Primary contact", contact_email, now),
                )
            for index, email in enumerate(account_emails):
                conn.execute(
                    "INSERT INTO contacts "
                    "(id, account_id, name, email, created_at) VALUES (?,?,?,?,?)",
                    (
                        f"cont_ff_match_{suffix}_{index}",
                        account_id,
                        f"Account contact {index}",
                        email,
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO deals "
                "(id, account_id, contact_id, title, stage, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    deal_id,
                    account_id,
                    contact_id,
                    f"Fireflies deal {suffix}",
                    "lead",
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        created.append((deal_id, account_id))
        return deal_id

    yield seed

    conn = db.get_conn()
    try:
        for deal_id, account_id in reversed(created):
            conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
            conn.execute("DELETE FROM contacts WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def _matches(deal_id, *participants):
    keys = fireflies._resolve_deal_match_keys(deal_id)
    transcript = {"title": "Unrelated title", "participants": list(participants)}
    return keys, fireflies._match_deal(transcript, keys)


def test_contact_email_match_uses_resolved_deal_contact(deal_factory):
    deal_id = deal_factory(contact_email="owner@contact.example")

    keys, matched = _matches(deal_id, "owner@contact.example")

    assert keys["emails"] == {"owner@contact.example"}
    assert matched is True


def test_any_contact_email_on_deal_account_matches(deal_factory):
    deal_id = deal_factory(
        contact_email="owner@contact.example",
        account_emails=("buyer@another.example",),
    )

    keys, matched = _matches(deal_id, "buyer@another.example")

    assert keys["emails"] == {
        "owner@contact.example",
        "buyer@another.example",
    }
    assert matched is True


def test_account_domain_match(deal_factory):
    deal_id = deal_factory(account_domain="company.example")

    keys, matched = _matches(deal_id, "guest@company.example")

    assert keys["account_domain"] == "company.example"
    assert matched is True


def test_no_resolvable_keys_never_guesses(deal_factory):
    deal_id = deal_factory()

    keys, matched = _matches(deal_id, "prospect@unknown.example")

    assert keys == {"emails": set(), "account_domain": ""}
    assert matched is False


def test_email_and_domain_matching_are_case_insensitive(deal_factory):
    deal_id = deal_factory(
        contact_email="Owner@Mixed.Example",
        account_domain="Mixed.Example",
    )

    _, email_matched = _matches(deal_id, "OWNER@MIXED.EXAMPLE")
    _, domain_matched = _matches(deal_id, "Guest@MIXED.EXAMPLE")

    assert email_matched is True
    assert domain_matched is True


def test_empty_participants_never_match(deal_factory):
    deal_id = deal_factory(
        contact_email="owner@contact.example",
        account_domain="contact.example",
    )

    _, matched = _matches(deal_id)

    assert matched is False
