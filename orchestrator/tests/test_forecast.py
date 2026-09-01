"""30/60/90-day revenue forecast (CRM tab) — regression guard.

Covers growth.forecast() + GET /api/growth/forecast and the expected_close_date
write paths (crm.update_deal / growth.update_deal_growth):
  overdue — expected_close_date in the past
  30d/60d/90d — within 0–30 / 31–60 / 61–90 days
  beyond  — >90 days or undetermined
  auto-estimate from stage when no explicit date; weighted = value × STAGE_PROB;
  closed (won/lost) + inactive (stalled) deals excluded.

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes the CRM
tables on the copy for a deterministic active-deal set.

Run:  python -m pytest tests/test_forecast.py -v
"""
import atexit
import datetime
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_forecast_", suffix=".db")
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


def _iso(days_from_today: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days_from_today)).isoformat()


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class ForecastBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        conn = _db.get_conn()
        try:
            conn.executescript(
                "DELETE FROM deal_events;"
                "DELETE FROM lead_scoring_features;"
                "DELETE FROM deals;"
                "DELETE FROM contacts;"
                "DELETE FROM accounts;")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved

    def _deal(self, title, stage="qualified", value=None, expected_close=None):
        aid = _crm.create_account(title + " Co")["account_id"]
        did = _crm.create_deal(aid, title, stage=stage, value=value,
                               expected_close_date=expected_close)["deal_id"]
        return did


class Forecast(ForecastBase):
    def test_empty_all_buckets_zero(self):
        f = _growth.forecast()
        self.assertEqual(f["totals"]["all_active"], 0)
        self.assertEqual(f["totals"]["total_value"], 0)
        self.assertEqual(f["totals"]["total_weighted"], 0)
        for k in ("overdue", "30d", "60d", "90d", "beyond"):
            self.assertEqual(f["buckets"][k]["count"], 0)
            self.assertEqual(f["buckets"][k]["value"], 0)
            self.assertEqual(f["buckets"][k]["deals"], [])

    def test_explicit_past_is_overdue(self):
        did = self._deal("Late", stage="proposal", value=1000, expected_close=_iso(-5))
        f = _growth.forecast()
        b = f["buckets"]["overdue"]
        self.assertEqual(b["count"], 1)
        self.assertEqual(b["deals"][0]["id"], did)
        self.assertEqual(b["deals"][0]["days_until_close"], -5)
        self.assertEqual(f["totals"]["with_expected_date"], 1)
        self.assertEqual(f["totals"]["auto_estimated"], 0)

    def test_explicit_within_30_is_30d(self):
        self._deal("Soon", stage="proposal", value=1000, expected_close=_iso(15))
        f = _growth.forecast()
        self.assertEqual(f["buckets"]["30d"]["count"], 1)
        self.assertEqual(f["buckets"]["60d"]["count"], 0)

    def test_explicit_within_60_is_60d(self):
        self._deal("Mid", stage="demo", value=1000, expected_close=_iso(45))
        f = _growth.forecast()
        self.assertEqual(f["buckets"]["60d"]["count"], 1)
        self.assertEqual(f["buckets"]["30d"]["count"], 0)

    def test_explicit_within_90_is_90d(self):
        self._deal("Far", stage="qualified", value=1000, expected_close=_iso(75))
        f = _growth.forecast()
        self.assertEqual(f["buckets"]["90d"]["count"], 1)
        self.assertEqual(f["buckets"]["60d"]["count"], 0)

    def test_explicit_beyond_90(self):
        self._deal("Way out", stage="lead", value=1000, expected_close=_iso(200))
        f = _growth.forecast()
        self.assertEqual(f["buckets"]["beyond"]["count"], 1)

    def test_auto_estimate_from_stage(self):
        # No explicit date → proposal auto-estimates ~30 days → 30d bucket.
        self._deal("Auto proposal", stage="proposal", value=1000)
        # qualified auto-estimates ~60 days → 60d bucket.
        self._deal("Auto qualified", stage="qualified", value=1000)
        f = _growth.forecast()
        self.assertEqual(f["buckets"]["30d"]["count"], 1)
        self.assertEqual(f["buckets"]["60d"]["count"], 1)
        self.assertEqual(f["totals"]["with_expected_date"], 0)
        self.assertEqual(f["totals"]["auto_estimated"], 2)

    def test_closed_and_inactive_excluded(self):
        self._deal("Won", stage="won", value=1000, expected_close=_iso(10))
        self._deal("Lost", stage="lost", value=1000, expected_close=_iso(10))
        self._deal("Stalled", stage="stalled", value=1000, expected_close=_iso(10))
        f = _growth.forecast()
        self.assertEqual(f["totals"]["all_active"], 0)
        for k in ("overdue", "30d", "60d", "90d", "beyond"):
            self.assertEqual(f["buckets"][k]["count"], 0)

    def test_weighted_value_uses_stage_prob(self):
        # proposal STAGE_PROB = 0.75 → 100000 * 0.75 = 75000
        self._deal("Weighted", stage="proposal", value=100000, expected_close=_iso(10))
        f = _growth.forecast()
        b = f["buckets"]["30d"]
        self.assertEqual(b["value"], 100000)
        self.assertEqual(b["weighted"], 75000)
        self.assertEqual(f["totals"]["total_value"], 100000)
        self.assertEqual(f["totals"]["total_weighted"], 75000)

    def test_deal_shape(self):
        did = self._deal("Shape", stage="demo", value=500, expected_close=_iso(20))
        f = _growth.forecast()
        d = f["buckets"]["30d"]["deals"][0]
        for k in ("id", "title", "account_name", "stage", "value",
                  "expected_close_date", "days_until_close"):
            self.assertIn(k, d)
        self.assertEqual(d["id"], did)
        self.assertEqual(d["expected_close_date"], _iso(20))


class Endpoint(ForecastBase):
    def test_endpoint_shape(self):
        self._deal("E", stage="proposal", value=1000, expected_close=_iso(10))
        r = _CLIENT.get("/api/growth/forecast")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for k in ("today", "buckets", "totals"):
            self.assertIn(k, body)
        self.assertEqual(body["buckets"]["30d"]["count"], 1)

    def test_endpoint_empty_ok(self):
        r = _CLIENT.get("/api/growth/forecast")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["totals"]["all_active"], 0)


class WritePaths(ForecastBase):
    def test_patch_growth_sets_expected_close(self):
        did = self._deal("G", stage="qualified", value=1000)
        r = _CLIENT.patch(f"/api/crm/deals/{did}/growth",
                          json={"expected_close_date": "2026-09-01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_crm.get_deal(did)["expected_close_date"], "2026-09-01")

    def test_patch_growth_rejects_bad_date(self):
        did = self._deal("Bad", stage="qualified", value=1000)
        r = _CLIENT.patch(f"/api/crm/deals/{did}/growth",
                          json={"expected_close_date": "not-a-date"})
        self.assertEqual(r.status_code, 400)

    def test_patch_growth_clears_expected_close(self):
        did = self._deal("Clr", stage="qualified", value=1000, expected_close="2026-09-01")
        r = _CLIENT.patch(f"/api/crm/deals/{did}/growth",
                          json={"expected_close_date": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(_crm.get_deal(did)["expected_close_date"])

    def test_patch_deal_sets_expected_close(self):
        did = self._deal("D", stage="qualified", value=1000)
        r = _CLIENT.patch(f"/api/crm/deals/{did}",
                          json={"expected_close_date": "2026-08-15"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_crm.get_deal(did)["expected_close_date"], "2026-08-15")


if __name__ == "__main__":
    unittest.main()
