"""CLTV:CAC unit economics (Growth tab) — regression guard.

Covers:
  1. dashboard/db.py     — acquisition_costs table + helpers
  2. dashboard/growth.py — add/list/delete cost, cltv_cac() math + rating
  3. GET /api/growth/cltv-cac + acquisition-costs CRUD endpoints

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes the CRM
tables + acquisition_costs on the copy so CLTV:CAC math is deterministic.

Run:  python -m pytest tests/test_cltv_cac.py -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
_db = _crm = _growth = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_cltv_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)

        _ORIG_KDB = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard import crm as _crm, growth as _growth
            _growth.ensure_schema()
            from dashboard.api import app
            from starlette.testclient import TestClient
            _CLIENT = TestClient(app, raise_server_exceptions=False)
            _READY = True
        finally:
            _db.KANBAN_DB = _ORIG_KDB
except Exception:  # pragma: no cover
    _READY = False


@atexit.register
def _cleanup_tmp_db():  # pragma: no cover
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class CltvBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_acquisition_schema()
        conn = _db.get_conn()
        try:
            conn.executescript(
                "DELETE FROM acquisition_costs;"
                "DELETE FROM deal_events;"
                "DELETE FROM lead_scoring_features;"
                "DELETE FROM deals;"
                "DELETE FROM contacts;"
                "DELETE FROM accounts;")
            conn.commit()
        finally:
            conn.close()
        # a known ICP → deterministic CLTV: 40000 * 1.5 * 12 = 720000
        _growth.set_icp({"avg_ticket": 40000})

    def tearDown(self):
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM icp_config")
            conn.commit()
        finally:
            conn.close()
        _db.KANBAN_DB = self._saved

    def _won_deal(self, source, title="Deal"):
        aid = _crm.create_account(title + " Co")["account_id"]
        did = _crm.create_deal(aid, title, stage="won")["deal_id"]
        conn = _db.get_conn()
        try:
            conn.execute("UPDATE deals SET lead_source=? WHERE id=?", (source, did))
            conn.commit()
        finally:
            conn.close()
        return did


class CostCrud(CltvBase):
    def test_add_and_list(self):
        res = _growth.add_acquisition_cost("linkedin", 10000, "2026-07")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["cost"]["id"].startswith("acq_"))
        costs = _growth.list_acquisition_costs()["costs"]
        self.assertEqual(len(costs), 1)
        self.assertEqual(costs[0]["source"], "linkedin")
        self.assertEqual(costs[0]["cost_mxn"], 10000.0)

    def test_add_validation(self):
        self.assertEqual(_growth.add_acquisition_cost("myspace", 100)["status"], "error")  # bad source
        self.assertEqual(_growth.add_acquisition_cost("linkedin", "free")["status"], "error")
        self.assertEqual(_growth.add_acquisition_cost("linkedin", -5)["status"], "error")
        self.assertEqual(_growth.add_acquisition_cost("linkedin", 5, "July")["status"], "error")

    def test_totals_sum_across_months(self):
        _growth.add_acquisition_cost("linkedin", 10000, "2026-06")
        _growth.add_acquisition_cost("linkedin", 15000, "2026-07")
        self.assertEqual(_db.acquisition_cost_totals_by_source()["linkedin"], 25000.0)

    def test_delete(self):
        cid = _growth.add_acquisition_cost("evento", 5000)["cost"]["id"]
        self.assertEqual(_growth.delete_acquisition_cost(cid)["status"], "ok")
        self.assertEqual(_growth.list_acquisition_costs()["costs"], [])
        self.assertEqual(_growth.delete_acquisition_cost(cid)["status"], "error")


class CltvCacMath(CltvBase):
    def test_cltv_value(self):
        # avg_ticket 40000 (set in setUp) * repeat 1.5 * lifespan 12 = 720000
        c = _growth.cltv_cac()
        self.assertEqual(c["cltv"], 720000.0)
        self.assertEqual(c["params"]["avg_ticket"], 40000.0)

    def test_cac_and_ratio_green(self):
        # 2 won customers from linkedin, spend 20000 → CAC 10000 → 720000/10000 = 72 (>3)
        self._won_deal("linkedin", "A")
        self._won_deal("linkedin", "B")
        _growth.add_acquisition_cost("linkedin", 20000, "2026-07")
        row = next(s for s in _growth.cltv_cac()["sources"] if s["source"] == "linkedin")
        self.assertEqual(row["customers"], 2)
        self.assertEqual(row["cost_mxn"], 20000.0)
        self.assertEqual(row["cac"], 10000.0)
        self.assertEqual(row["ratio"], 72.0)
        self.assertEqual(row["rating"], "green")

    def test_delivered_customer_remains_in_cac_denominator(self):
        """REPLACED CONTRACT (journey fase 1, ruling 2). It used to move the
        deal to `stage='delivered'` to prove a delivered customer still counted
        as an acquired one. The stage is retired — a won deal stays won — so the
        delivery is expressed where it now lives, on the project, and the deal
        stays in the denominator by construction rather than by remembering to
        write `IN ('won','delivered')`."""
        did = self._won_deal("referral", "Delivered customer")
        conn = _db.get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, slug, name, created_at, status) "
                "VALUES (?,?,?,?,?)",
                ("proj_cltv_delivered", "cltv-delivered", "CLTV Delivery", 1,
                 "delivered"))
            conn.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                         ("proj_cltv_delivered", did))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(_crm.get_deal(did)["stage"], "won")
        _growth.add_acquisition_cost("referral", 12000)
        row = next(s for s in _growth.cltv_cac()["sources"]
                   if s["source"] == "referral")
        self.assertEqual(row["customers"], 1)
        self.assertEqual(row["cac"], 12000.0)

    def test_rating_thresholds(self):
        # craft a red source: CAC just above CLTV → ratio <1
        self._won_deal("cold_email")
        _growth.add_acquisition_cost("cold_email", 800000)   # CAC 800000 > CLTV 720000
        red = next(s for s in _growth.cltv_cac()["sources"] if s["source"] == "cold_email")
        self.assertEqual(red["rating"], "red")
        self.assertLess(red["ratio"], 1)
        # a yellow source: ratio between 1 and 3 → CAC 300000 → 720000/300000 = 2.4
        self._won_deal("evento")
        _growth.add_acquisition_cost("evento", 300000)
        yellow = next(s for s in _growth.cltv_cac()["sources"] if s["source"] == "evento")
        self.assertEqual(yellow["rating"], "yellow")

    def test_cost_without_customers_has_no_ratio(self):
        _growth.add_acquisition_cost("linkedin", 5000)       # spend but 0 customers
        row = next(s for s in _growth.cltv_cac()["sources"] if s["source"] == "linkedin")
        self.assertEqual(row["customers"], 0)
        self.assertIsNone(row["cac"])
        self.assertIsNone(row["ratio"])
        self.assertEqual(row["rating"], "na")

    def test_totals(self):
        self._won_deal("linkedin")
        self._won_deal("evento")
        _growth.add_acquisition_cost("linkedin", 30000)
        _growth.add_acquisition_cost("evento", 30000)
        totals = _growth.cltv_cac()["totals"]
        self.assertEqual(totals["customers"], 2)
        self.assertEqual(totals["cost_mxn"], 60000.0)
        self.assertEqual(totals["cac"], 30000.0)             # 60000 / 2

    def test_empty_is_safe(self):
        c = _growth.cltv_cac()
        self.assertEqual(c["sources"], [])
        self.assertIsNone(c["totals"]["cac"])


class Endpoints(CltvBase):
    def test_get_cltv_cac(self):
        self._won_deal("referral")
        _growth.add_acquisition_cost("referral", 12000)
        r = _CLIENT.get("/api/growth/cltv-cac")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("cltv", body)
        self.assertEqual(len(body["sources"]), 1)

    def test_post_and_get_costs(self):
        r = _CLIENT.post("/api/growth/acquisition-costs",
                         json={"source": "inbound", "cost_mxn": 8000, "month": "2026-07"})
        self.assertEqual(r.status_code, 200)
        costs = _CLIENT.get("/api/growth/acquisition-costs").json()["costs"]
        self.assertEqual(len(costs), 1)

    def test_post_invalid_is_400(self):
        r = _CLIENT.post("/api/growth/acquisition-costs", json={"source": "nope", "cost_mxn": 1})
        self.assertEqual(r.status_code, 400)

    def test_delete_endpoint(self):
        cid = _CLIENT.post("/api/growth/acquisition-costs",
                           json={"source": "evento", "cost_mxn": 100}).json()["cost"]["id"]
        self.assertEqual(_CLIENT.delete(f"/api/growth/acquisition-costs/{cid}").status_code, 200)
        self.assertEqual(_CLIENT.delete(f"/api/growth/acquisition-costs/{cid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
