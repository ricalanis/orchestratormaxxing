"""m17 cash-flow contract: the payment PLAN lands on the deal, audited.

Pins the five surfaces of Fase 1 (cobro first-class):
  1. m17_cash_flow — three additive columns, idempotent, zero backfill
  2. crm.set_payment_promise — the ONE audited write path (promise/repromise/
     freeze-after-paid), evented
  3. crm.mark_deal_invoiced — optional explicit date validated BEFORE the
     stamp; derivation from payment_terms_days only when the date is NULL
  4. crm.mark_deal_paid — reconciliation delta COMPUTED (event + response),
     never a stored column
  5. The side doors stay shut: generic PATCH refuses the audited field
     loudly, and every money-write verb stays absent from mcp_server.py

Isolation: dashboard.api runs ensure_schema() at import, so the DB layers are
pointed at a COPY of ~/.hermes/kanban.db BEFORE the import — the real DB is
never touched. If there's no kanban.db to copy, the whole module skips.

Run:  .venv/bin/python -m pytest tests/test_m17_cash_flow.py -q
      .venv/bin/python -m unittest tests.test_m17_cash_flow
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
_DASH_TOKEN = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_m17_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB

        _TOKEN_FILE = Path.home() / ".config" / "orchestratormaxxing" / "dashboard-token"
        try:
            _DASH_TOKEN = _TOKEN_FILE.read_text().strip()
        except Exception:
            _DASH_TOKEN = os.environ.get("HERMES_DASHBOARD_TOKEN", "")

        from dashboard.api import app  # ensure_schema() runs here, on the copy
        from dashboard import crm as _crm
        from dashboard.migrations import runner as _runner
        from dashboard.migrations.m17_cash_flow import m17_cash_flow as _m17
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _auth():
    if _DASH_TOKEN:
        return {"Authorization": f"Bearer {_DASH_TOKEN}"}
    return {}


def _conn():
    """Always resolve through db.get_conn(): under pytest the conftest's
    `pytest_collection_finish` re-points `db.KANBAN_DB` at the SESSION sandbox
    after this module's import-time redirect, so a private sqlite3.connect to
    `_TMP_DB` would seed a DB the code under test never reads. Sharing the
    resolution makes the seeder and the verbs agree in both runners."""
    return _db.get_conn()


def _mk_deal(stage="won", **cols):
    """Seed a minimal deal (with its NOT-NULL account) straight into the COPY."""
    did = f"deal_{_uuid.uuid4().hex[:8]}"
    aid = f"acct_{_uuid.uuid4().hex[:8]}"
    now = 1754000000
    base = {"id": did, "title": f"t-{did}", "stage": stage,
            "account_id": aid, "value": 10000.0, "currency": "MXN",
            "created_at": now, "updated_at": now}
    if stage in ("won", "lost"):
        base["closed_at"] = now
    base.update(cols)
    c = _conn()
    try:
        c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?, ?, ?)",
                  (aid, f"a-{aid}", now))
        keys = ", ".join(base)
        ph = ", ".join("?" * len(base))
        c.execute(f"INSERT INTO deals ({keys}) VALUES ({ph})",
                  tuple(base.values()))
        c.commit()
    finally:
        c.close()
    return did


def _deal_row(deal_id):
    c = _conn()
    try:
        return dict(c.execute("SELECT * FROM deals WHERE id = ?",
                              (deal_id,)).fetchone())
    finally:
        c.close()


def _events(deal_id, kind=None):
    c = _conn()
    try:
        sql = "SELECT kind, payload FROM deal_events WHERE deal_id = ?"
        args = [deal_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [(r["kind"], json.loads(r["payload"] or "{}"))
                for r in c.execute(sql + " ORDER BY rowid", args)]
    finally:
        c.close()


class _Base(unittest.TestCase):
    """Every case re-ensures m17 on the CURRENT DB resolution in setUp.

    Under pytest the conftest's session sandbox is a pre-m17 copy of the live
    DB and `pytest_collection_finish` re-points `db.KANBAN_DB` at it AFTER
    this module's import-time `ensure_schema()` ran on the private copy — so
    the columns must be (idempotently) ensured again at test time, through the
    same resolution the verbs use. `_m17` directly rather than
    `runner.run_versioned()`: the runner's backup gate is a side effect a
    sandbox run must not pay."""

    def setUp(self):
        c = _conn()
        try:
            _m17(c)
            c.commit()
        finally:
            c.close()


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Migration(_Base):

    def test_m17_adds_three_typed_columns_and_two_runs_are_one_schema(self):
        c = _conn()
        try:
            before = [(r["name"], r["type"]) for r in
                      c.execute("PRAGMA table_info(deals)")]
            self.assertIn(("payment_terms_days", "INTEGER"), before)
            self.assertIn(("expected_payment_date", "TEXT"), before)
            self.assertIn(("expected_payment_date_original", "TEXT"), before)
            # Idempotence: a second run adds nothing and changes nothing.
            res = _m17(c)
            after = [(r["name"], r["type"]) for r in
                     c.execute("PRAGMA table_info(deals)")]
            self.assertEqual(res["columns"], [])
            self.assertEqual(before, after)
        finally:
            c.close()

    def test_m17_is_registered_immediately_after_m16(self):
        names = [n for n, _ in _runner.MIGRATIONS]
        self.assertIn("m17_cash_flow", names)
        self.assertEqual(names.index("m17_cash_flow"),
                         names.index("m16_capture_receipts") + 1)

    def test_m17_backfills_nothing(self):
        did = _mk_deal()
        row = _deal_row(did)
        self.assertIsNone(row["payment_terms_days"])
        self.assertIsNone(row["expected_payment_date"])
        self.assertIsNone(row["expected_payment_date_original"])


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Promise(_Base):

    def test_first_promise_writes_original_once_and_events_promised(self):
        did = _mk_deal()
        res = _crm.set_payment_promise(did, "2026-09-05")
        self.assertEqual(res["status"], "payment_promised")
        row = _deal_row(did)
        self.assertEqual(row["expected_payment_date"], "2026-09-05")
        self.assertEqual(row["expected_payment_date_original"], "2026-09-05")
        self.assertEqual(_events(did, "payment_promised"),
                         [("payment_promised", {"to": "2026-09-05"})])

    def test_repromise_requires_reason_keeps_original_and_events_from_to(self):
        did = _mk_deal()
        _crm.set_payment_promise(did, "2026-09-05")
        bare = _crm.set_payment_promise(did, "2026-09-12")
        self.assertEqual(bare.get("code"), "reason_required")
        res = _crm.set_payment_promise(did, "2026-09-12",
                                       reason="cliente pidió mover al corte")
        self.assertEqual(res["status"], "payment_repromised")
        row = _deal_row(did)
        self.assertEqual(row["expected_payment_date"], "2026-09-12")
        self.assertEqual(row["expected_payment_date_original"], "2026-09-05")
        kinds = _events(did, "payment_repromised")
        self.assertEqual(len(kinds), 1)
        self.assertEqual(kinds[0][1]["from"], "2026-09-05")
        self.assertEqual(kinds[0][1]["to"], "2026-09-12")
        self.assertTrue(kinds[0][1]["reason"])

    def test_same_value_is_unchanged_and_unevented(self):
        did = _mk_deal()
        _crm.set_payment_promise(did, "2026-09-05")
        res = _crm.set_payment_promise(did, "2026-09-05")
        self.assertEqual(res["status"], "unchanged")
        self.assertEqual(len(_events(did, "payment_promised")), 1)
        self.assertEqual(_events(did, "payment_repromised"), [])

    def test_promise_after_paid_is_frozen_already_paid(self):
        did = _mk_deal(invoiced_at=1754000100, paid_at=1754000200,
                       expected_payment_date="2026-09-05",
                       expected_payment_date_original="2026-09-05")
        res = _crm.set_payment_promise(did, "2026-09-20", reason="x")
        self.assertEqual(res.get("code"), "already_paid")
        self.assertEqual(_deal_row(did)["expected_payment_date"], "2026-09-05")

    def test_an_epoch_or_malformed_date_is_refused(self):
        did = _mk_deal()
        for bad in (1754000000, "05/09/2026", "2026-9-5", "2026-13-01", "", None):
            res = _crm.set_payment_promise(did, bad)
            self.assertEqual(res.get("code"), "bad_date", f"accepted: {bad!r}")
        self.assertIsNone(_deal_row(did)["expected_payment_date"])

    def test_only_a_won_deal_carries_a_promise(self):
        did = _mk_deal(stage="proposal")
        res = _crm.set_payment_promise(did, "2026-09-05")
        self.assertEqual(res.get("code"), "not_won")


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class InvoiceDerivation(_Base):

    def test_invoiced_with_terms_derives_expected_only_when_null(self):
        did = _mk_deal(payment_terms_days=30)
        res = _crm.mark_deal_invoiced(did)
        self.assertEqual(res["status"], "deal_invoiced")
        want = (datetime.date.fromtimestamp(res["invoiced_at"])
                + datetime.timedelta(days=30)).isoformat()
        self.assertEqual(res["expected_payment_date"], want)
        self.assertTrue(res["expected_derived"])
        row = _deal_row(did)
        self.assertEqual(row["expected_payment_date"], want)
        self.assertEqual(row["expected_payment_date_original"], want)
        ev = _events(did, "payment_promised")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0][1]["terms"], 30)
        self.assertTrue(ev[0][1]["derived"])

    def test_invoiced_never_overwrites_a_manual_expected(self):
        did = _mk_deal(payment_terms_days=30,
                       expected_payment_date="2026-12-01",
                       expected_payment_date_original="2026-12-01")
        res = _crm.mark_deal_invoiced(did)
        self.assertEqual(res["status"], "deal_invoiced")
        self.assertEqual(res["expected_kept"], "2026-12-01")
        self.assertEqual(_deal_row(did)["expected_payment_date"], "2026-12-01")

    def test_invoiced_accepts_an_explicit_date_over_derivation(self):
        did = _mk_deal(payment_terms_days=30)
        res = _crm.mark_deal_invoiced(did, expected_payment_date="2026-10-10")
        self.assertEqual(res["expected_payment_date"], "2026-10-10")
        self.assertFalse(res["expected_derived"])

    def test_an_epoch_or_malformed_date_is_refused_before_the_stamp(self):
        did = _mk_deal()
        for bad in (1754000000, "10/10/2026", "2026-10-40"):
            res = _crm.mark_deal_invoiced(did, expected_payment_date=bad)
            self.assertEqual(res.get("code"), "bad_date", f"accepted: {bad!r}")
        # The refusal happened BEFORE the stamp: the verb did not half-land.
        self.assertIsNone(_deal_row(did)["invoiced_at"])

    def test_no_terms_and_no_date_leaves_null_honest(self):
        did = _mk_deal()
        res = _crm.mark_deal_invoiced(did)
        self.assertEqual(res["status"], "deal_invoiced")
        self.assertNotIn("expected_payment_date", res)
        self.assertIsNone(_deal_row(did)["expected_payment_date"])


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class PaidDelta(_Base):

    def test_paid_event_and_response_carry_delta_days_computed_not_stored(self):
        expected = (datetime.date.today() - datetime.timedelta(days=4)).isoformat()
        original = (datetime.date.today() - datetime.timedelta(days=9)).isoformat()
        did = _mk_deal(invoiced_at=1754000100,
                       expected_payment_date=expected,
                       expected_payment_date_original=original)
        res = _crm.mark_deal_paid(did)
        self.assertEqual(res["status"], "deal_paid")
        self.assertEqual(res["delta_days"], 4)
        self.assertEqual(res["delta_original_days"], 9)
        ev = _events(did, "deal_paid")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0][1]["delta_days"], 4)
        self.assertEqual(ev[0][1]["delta_original_days"], 9)
        # The delta is a reading, not a column.
        c = _conn()
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(deals)")}
        finally:
            c.close()
        self.assertFalse({"delta_days", "delta_original_days"} & cols)

    def test_paid_without_a_promise_carries_no_delta(self):
        did = _mk_deal(invoiced_at=1754000100)
        res = _crm.mark_deal_paid(did)
        self.assertEqual(res["status"], "deal_paid")
        self.assertNotIn("delta_days", res)

    def test_a_malformed_stored_date_cannot_crash_the_paid_tap(self):
        # Defense in depth: the writers validate, but a row seeded by an older
        # path must degrade to "no delta", never to a 500 on the money tap.
        did = _mk_deal(invoiced_at=1754000100, expected_payment_date="garbage")
        res = _crm.mark_deal_paid(did)
        self.assertEqual(res["status"], "deal_paid")
        self.assertNotIn("delta_days", res)


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Terms(_Base):

    def test_terms_set_via_update_deal_is_validated_and_evented(self):
        did = _mk_deal()
        res = _crm.update_deal(did, payment_terms_days=30)
        self.assertEqual(res["status"], "updated")
        self.assertEqual(_deal_row(did)["payment_terms_days"], 30)
        self.assertEqual(_events(did, "payment_terms_set"),
                         [("payment_terms_set", {"from": None, "to": 30})])

    def test_terms_rejects_out_of_range_and_bool(self):
        did = _mk_deal()
        for bad in (-1, 366, "abc", True):
            res = _crm.update_deal(did, payment_terms_days=bad)
            self.assertEqual(res["status"], "error", f"accepted: {bad!r}")
        self.assertIsNone(_deal_row(did)["payment_terms_days"])

    def test_empty_string_clears_terms(self):
        did = _mk_deal(payment_terms_days=30)
        res = _crm.update_deal(did, payment_terms_days="")
        self.assertEqual(res["status"], "updated")
        self.assertIsNone(_deal_row(did)["payment_terms_days"])

    def test_contado_zero_and_the_365_boundary_are_valid_terms(self):
        # 0 = contado is the operator's real case, not an edge: the range is
        # inclusive on both ends.
        did = _mk_deal()
        self.assertEqual(_crm.update_deal(did, payment_terms_days=0)["status"],
                         "updated")
        self.assertEqual(_deal_row(did)["payment_terms_days"], 0)
        self.assertEqual(_crm.update_deal(did, payment_terms_days=365)["status"],
                         "updated")
        self.assertEqual(_deal_row(did)["payment_terms_days"], 365)

    def test_an_update_without_terms_still_updates(self):
        # The terms branch must not leak into unrelated updates.
        did = _mk_deal()
        res = _crm.update_deal(did, notes="solo notas")
        self.assertEqual(res["status"], "updated")
        self.assertEqual(_deal_row(did)["notes"], "solo notas")


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class PatchRejection(_Base):

    def test_generic_patch_refuses_expected_payment_date_loudly(self):
        """Proven RED against the pre-guard code: PATCH returned 200, applied
        `notes`, and silently DROPPED the date — the quiet side door this
        guard closes. Green = typed 400 and nothing applied."""
        did = _mk_deal(notes="before")
        r = _CLIENT.patch(f"/api/crm/deals/{did}",
                          json={"expected_payment_date": "2026-09-05",
                                "notes": "after"},
                          headers=_auth())
        self.assertEqual(r.status_code, 400)
        self.assertIn("payment-promise", r.json().get("detail", ""))
        row = _deal_row(did)
        self.assertIsNone(row["expected_payment_date"])
        self.assertEqual(row["notes"], "before")

    def test_generic_patch_refuses_the_original_anchor_too(self):
        did = _mk_deal()
        r = _CLIENT.patch(f"/api/crm/deals/{did}",
                          json={"expected_payment_date_original": "2026-09-05"},
                          headers=_auth())
        self.assertEqual(r.status_code, 400)

    def test_payment_promise_endpoint_maps_typed_errors(self):
        did = _mk_deal(stage="proposal")
        r = _CLIENT.post(f"/api/crm/deals/{did}/payment-promise",
                         json={"expected_payment_date": "2026-09-05"},
                         headers=_auth())
        self.assertEqual(r.status_code, 400)
        r = _CLIENT.post("/api/crm/deals/deal_nope/payment-promise",
                         json={"expected_payment_date": "2026-09-05"},
                         headers=_auth())
        self.assertEqual(r.status_code, 404)

    def test_invoiced_endpoint_accepts_the_optional_body(self):
        did = _mk_deal()
        r = _CLIENT.post(f"/api/crm/deals/{did}/invoiced",
                         json={"expected_payment_date": "2026-10-10"},
                         headers=_auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["expected_payment_date"], "2026-10-10")


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class McpAbsence(_Base):
    """Money WRITE verbs are human-only: their absence from mcp_server.py IS
    the guard (m11 doctrine, extended to the m17 promise verb)."""

    def test_money_write_verbs_are_absent_from_mcp(self):
        src = (Path(__file__).resolve().parent.parent / "mcp_server.py").read_text()
        for needle in ("mark_deal_invoiced", "mark_deal_paid",
                       "set_payment_promise", "payment-promise",
                       "payment_promise"):
            self.assertNotIn(needle, src,
                             f"mcp_server.py must not reach {needle} — the "
                             f"absence is the guard, do not add MCP parity")


if __name__ == "__main__":
    unittest.main()
