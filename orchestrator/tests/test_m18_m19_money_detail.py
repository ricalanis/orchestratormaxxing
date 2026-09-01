"""m18 + m19 contract: the launch plan and the real cash, each with its writer.

What it pins:
  1. m18_invoice_launch / m19_paid_amount — additive, idempotent, registered
     in order after m17, zero backfill.
  2. `expected_invoice_date` (🧾 lanzamiento) — LIGHT governance by design:
     the generic PATCH writes it (evented `invoice_launch_planned`), no
     reason required — but it refuses once `invoiced_at` is stamped (moot)
     and refuses non-ISO values. The asymmetry vs. the audited payment
     promise is deliberate and documented.
  3. `paid_amount` (dinero recibido) — captured on the ✅ tap body, carried
     in the `deal_paid` event + response (with `amount_diff` when it differs
     from `value`); bad amounts refuse BEFORE the stamp. Correction is a
     PATCH allowed only while paid, evented `paid_amount_set`.
  4. "Facturado" the boolean stays DERIVED (`invoiced_at IS NOT NULL`) —
     asserted here as schema shape: no `invoiced` boolean column exists.
  5. The drawer context carries the new fields, and the ACCOUNT context
     carries the per-client money rollup (facturado / cobrado real /
     pendiente / próximo cobro).

Isolation: identical convention to test_m17_cash_flow (db.get_conn()
resolution + per-test idempotent migration ensure).
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_m1819_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB

        from dashboard.api import app  # ensure_schema() runs here, on the copy
        from dashboard import crm as _crm
        from dashboard import context as _context
        from dashboard.migrations import runner as _runner
        from dashboard.migrations.m17_cash_flow import m17_cash_flow as _m17
        from dashboard.migrations.m18_invoice_launch import m18_invoice_launch as _m18
        from dashboard.migrations.m19_paid_amount import m19_paid_amount as _m19
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _conn():
    return _db.get_conn()


def _mk_deal(stage="won", **cols):
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
    return did, aid


def _deal_row(deal_id):
    c = _conn()
    try:
        return dict(c.execute("SELECT * FROM deals WHERE id = ?",
                              (deal_id,)).fetchone())
    finally:
        c.close()


def _events(deal_id, kind):
    c = _conn()
    try:
        return [json.loads(r["payload"] or "{}") for r in c.execute(
            "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = ? "
            "ORDER BY rowid", (deal_id, kind))]
    finally:
        c.close()


class _Base(unittest.TestCase):
    def setUp(self):
        c = _conn()
        try:
            _m17(c)
            _m18(c)
            _m19(c)
            c.commit()
        finally:
            c.close()


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Migrations(_Base):

    def test_both_registered_in_order_after_m17(self):
        names = [n for n, _ in _runner.MIGRATIONS]
        i17, i18, i19 = (names.index(n) for n in
                         ("m17_cash_flow", "m18_invoice_launch", "m19_paid_amount"))
        self.assertEqual((i18, i19), (i17 + 1, i17 + 2))

    def test_columns_exist_and_reruns_are_noops(self):
        c = _conn()
        try:
            cols = {r["name"]: r["type"] for r in c.execute("PRAGMA table_info(deals)")}
            self.assertEqual(cols.get("expected_invoice_date"), "TEXT")
            self.assertEqual(cols.get("paid_amount"), "REAL")
            self.assertEqual(_m18(c)["columns"], [])
            self.assertEqual(_m19(c)["columns"], [])
        finally:
            c.close()

    def test_facturado_the_boolean_stays_derived_not_a_column(self):
        c = _conn()
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(deals)")}
        finally:
            c.close()
        # invoiced_at IS the boolean; a second column would be a second truth.
        self.assertIn("invoiced_at", cols)
        self.assertNotIn("invoiced", cols)
        self.assertNotIn("facturado", cols)

    def test_no_backfill(self):
        did, _ = _mk_deal()
        row = _deal_row(did)
        self.assertIsNone(row["expected_invoice_date"])
        self.assertIsNone(row["paid_amount"])


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class LaunchWriter(_Base):

    def test_patch_sets_the_launch_and_events_it(self):
        did, _ = _mk_deal()
        res = _crm.update_deal(did, expected_invoice_date="2026-08-15")
        self.assertEqual(res["status"], "updated")
        self.assertEqual(_deal_row(did)["expected_invoice_date"], "2026-08-15")
        ev = _events(did, "invoice_launch_planned")
        self.assertEqual(ev, [{"from": None, "to": "2026-08-15"}])
        # Moving it needs no reason (own-action plan) but still leaves a trail.
        _crm.update_deal(did, expected_invoice_date="2026-08-20")
        self.assertEqual(len(_events(did, "invoice_launch_planned")), 2)

    def test_empty_string_clears(self):
        did, _ = _mk_deal(expected_invoice_date="2026-08-15")
        res = _crm.update_deal(did, expected_invoice_date="")
        self.assertEqual(res["status"], "updated")
        self.assertIsNone(_deal_row(did)["expected_invoice_date"])

    def test_non_iso_is_refused(self):
        did, _ = _mk_deal()
        for bad in ("15/08/2026", "2026-8-5", 1754000000):
            res = _crm.update_deal(did, expected_invoice_date=bad)
            self.assertEqual(res["status"], "error", f"accepted: {bad!r}")
        self.assertIsNone(_deal_row(did)["expected_invoice_date"])

    def test_planning_a_launch_after_the_invoice_is_moot(self):
        did, _ = _mk_deal(invoiced_at=1754000100)
        res = _crm.update_deal(did, expected_invoice_date="2026-08-15")
        self.assertEqual(res["status"], "error")
        self.assertIn("moot", res["error"])
        self.assertIsNone(_deal_row(did)["expected_invoice_date"])


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class ReceivedAmount(_Base):

    def test_the_tap_carries_the_real_cash_and_the_diff(self):
        did, _ = _mk_deal(invoiced_at=1754000100, value=10000.0)
        res = _crm.mark_deal_paid(did, paid_amount=9000)
        self.assertEqual(res["status"], "deal_paid")
        self.assertEqual(res["paid_amount"], 9000.0)
        self.assertEqual(res["amount_diff"], -1000.0)
        self.assertEqual(_deal_row(did)["paid_amount"], 9000.0)
        ev = _events(did, "deal_paid")
        self.assertEqual(ev[0]["paid_amount"], 9000.0)
        self.assertEqual(ev[0]["amount_diff"], -1000.0)

    def test_no_amount_keeps_null_meaning_equal_to_value(self):
        did, _ = _mk_deal(invoiced_at=1754000100)
        res = _crm.mark_deal_paid(did)
        self.assertEqual(res["status"], "deal_paid")
        self.assertIsNone(_deal_row(did)["paid_amount"])

    def test_a_bad_amount_refuses_before_the_stamp(self):
        did, _ = _mk_deal(invoiced_at=1754000100)
        for bad in ("abc", 0, -5, True):
            res = _crm.mark_deal_paid(did, paid_amount=bad)
            self.assertEqual(res.get("code"), "bad_amount", f"accepted: {bad!r}")
        self.assertIsNone(_deal_row(did)["paid_at"])

    def test_correction_patch_only_while_paid_and_evented(self):
        did, _ = _mk_deal(invoiced_at=1754000100)
        res = _crm.update_deal(did, paid_amount=9500)
        self.assertEqual(res["status"], "error")   # unpaid → capture error
        _crm.mark_deal_paid(did, paid_amount=9000)
        res = _crm.update_deal(did, paid_amount=9500)
        self.assertEqual(res["status"], "updated")
        self.assertEqual(_deal_row(did)["paid_amount"], 9500.0)
        self.assertEqual(_events(did, "paid_amount_set"),
                         [{"from": 9000.0, "to": 9500.0}])

    def test_one_peso_is_a_valid_deposit(self):
        # The bound is exactly zero, not "small": micro-payments are real.
        did, _ = _mk_deal(invoiced_at=1754000100)
        res = _crm.mark_deal_paid(did, paid_amount=1)
        self.assertEqual(res["status"], "deal_paid")
        self.assertEqual(_deal_row(did)["paid_amount"], 1.0)

    def test_correction_patch_validates_and_clears(self):
        did, _ = _mk_deal(invoiced_at=1754000100)
        _crm.mark_deal_paid(did, paid_amount=9000)
        for bad in ("abc", 0, -5, True):
            res = _crm.update_deal(did, paid_amount=bad)
            self.assertEqual(res["status"], "error", f"accepted: {bad!r}")
        self.assertEqual(_deal_row(did)["paid_amount"], 9000.0)
        res = _crm.update_deal(did, paid_amount=1)
        self.assertEqual(res["status"], "updated")   # bound is 0, not "small"
        self.assertEqual(_deal_row(did)["paid_amount"], 1.0)
        res = _crm.update_deal(did, paid_amount="")
        self.assertEqual(res["status"], "updated")
        self.assertIsNone(_deal_row(did)["paid_amount"],
                          "'' clears back to NULL — meaning '= value'")

    def test_paid_endpoint_accepts_the_body(self):
        did, _ = _mk_deal(invoiced_at=1754000100)
        r = _CLIENT.post(f"/api/crm/deals/{did}/paid",
                         json={"paid_amount": 7500})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["paid_amount"], 7500.0)


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class DrawerContext(_Base):

    def test_the_deal_entity_carries_the_new_fields(self):
        did, _ = _mk_deal(expected_invoice_date="2026-08-15")
        e = _context.build_context("deal", did)["entity"]
        self.assertEqual(e["expected_invoice_date"], "2026-08-15")
        self.assertIsNone(e["paid_amount"])

    def test_the_account_carries_the_money_rollup(self):
        did, aid = _mk_deal(value=10000.0, invoiced_at=1754000100,
                            expected_payment_date="2026-09-05")
        _mk_deal_extra = _mk_deal(value=5000.0, invoiced_at=1754000100,
                                  paid_at=1754000200, paid_amount=4500.0)
        # Second deal belongs to another account — must not leak in.
        e = _context.build_context("account", aid)["entity"]
        m = e["money"]
        self.assertEqual(m["invoiced_value"], 10000.0)
        self.assertEqual(m["collected_cash"], 0.0)
        self.assertEqual(m["pending_value"], 10000.0)
        self.assertEqual(m["next_expected"], "2026-09-05")

    def test_collected_cash_reads_the_real_deposit(self):
        did, aid = _mk_deal(value=5000.0, invoiced_at=1754000100,
                            paid_at=1754000200, paid_amount=4500.0)
        m = _context.build_context("account", aid)["entity"]["money"]
        self.assertEqual(m["collected_cash"], 4500.0)
        self.assertEqual(m["pending_value"], 0.0)


if __name__ == "__main__":
    unittest.main()
