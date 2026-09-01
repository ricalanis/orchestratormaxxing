"""crm.update_account — the one validated write path for an account.

Why it exists (2026-08-10): the Fireflies matcher resolves a deal's meetings
from its contacts' emails OR the account's domain, and `accounts.domain` had
no write path at all (1 of 38 rows populated). A domain matches EVERY contact
of an account at once, so it is the robust half of the identity fix — but the
only way to set it was raw SQL, which the codebase's one-write-path doctrine
refuses.

The domain is normalized, not trusted: an operator pastes "https://Acme.com/"
or "ana@acme.com" as often as "acme.com", and the matcher compares
against the bare host, lowercased. A stored value that only sometimes matches
is worse than an empty one — it looks configured and silently misses.

Run:  python -m pytest tests/test_account_update.py -v
"""
import time
import uuid

import pytest

from dashboard import crm, db


@pytest.fixture
def account():
    aid = f"acct_upd_{uuid.uuid4().hex[:10]}"
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (aid, f"Cuenta update {aid[-6:]}", int(time.time())))
        conn.commit()
    finally:
        conn.close()
    yield aid
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM accounts WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()


def _row(aid):
    conn = db.get_conn()
    try:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()
    finally:
        conn.close()


def test_sets_the_domain(account):
    res = crm.update_account(account, domain="acme.com")
    assert res["status"] == "updated"
    assert _row(account)["domain"] == "acme.com"


@pytest.mark.parametrize("raw,expected", [
    ("https://Acme.com/", "acme.com"),
    ("http://www.orbital.example", "orbital.example"),
    ("WWW.JoinVertex.com", "joinvertex.com"),
    ("ana@acme.com", "acme.com"),
    ("  freight.example  ", "freight.example"),
    ("toolco.example/pricing", "toolco.example"),
    ("acme.com.", "acme.com"),      # trailing dot (a copied FQDN)
    (".jlb.swiss", "jlb.swiss"),            # leading dot (a copied cookie domain)
])
def test_normalizes_what_an_operator_actually_pastes(account, raw, expected):
    crm.update_account(account, domain=raw)
    assert _row(account)["domain"] == expected


def test_clearing_the_domain_is_explicit(account):
    crm.update_account(account, domain="acme.com")
    crm.update_account(account, domain="")
    assert _row(account)["domain"] is None


def test_renaming_keeps_the_name_non_empty(account):
    assert crm.update_account(account, name="Acme SA")["status"] == "updated"
    assert _row(account)["name"] == "Acme SA"
    assert crm.update_account(account, name="   ")["status"] == "error"


def test_notes_are_writable(account):
    crm.update_account(account, notes="dominio verificado en Fireflies")
    assert _row(account)["notes"] == "dominio verificado en Fireflies"


def test_typed_errors_for_nothing_and_for_unknown(account):
    assert crm.update_account(account)["status"] == "error"
    res = crm.update_account("acct_no_existe", domain="x.com")
    assert res["status"] == "error"
    assert res["error"] == "account not found"


def test_a_garbage_domain_is_refused_not_stored(account):
    # Better an empty domain than one that looks configured and never matches.
    for bad in ("no es un dominio", "http://", "@", "localhost"):
        res = crm.update_account(account, domain=bad)
        assert res["status"] == "error", f"{bad!r} must be refused"
    assert _row(account)["domain"] is None


def test_the_stored_domain_is_what_the_matcher_compares(account):
    # The contract that actually matters: whatever we store must equal the
    # host the Fireflies matcher extracts from a participant address.
    from dashboard import fireflies
    crm.update_account(account, domain="https://Orbital.example/")
    stored = _row(account)["domain"]
    participant = fireflies._participant_email("afranco@orbital.example")
    assert participant.rpartition("@")[2] == stored


class TestHTTP:
    def test_patch_endpoint_updates_and_validates(self, account):
        from starlette.testclient import TestClient
        from dashboard.api import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.patch(f"/api/crm/accounts/{account}",
                         json={"domain": "https://Freight.example"})
        assert r.status_code == 200
        assert _row(account)["domain"] == "freight.example"
        bad = client.patch(f"/api/crm/accounts/{account}", json={"domain": "@"})
        assert bad.status_code == 400
        missing = client.patch("/api/crm/accounts/acct_no_existe",
                               json={"domain": "x.com"})
        assert missing.status_code == 400
