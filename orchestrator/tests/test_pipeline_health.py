"""Pipeline health alerts (Today tab) — regression guard.

Covers growth.pipeline_health() + GET /api/growth/pipeline-health:
  red    — next_touch_date in the past
  yellow — no touch in 7+ days
  blue   — no next_touch_date set
  priority red > yellow > blue (no double-counting); closed deals excluded.

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes the CRM
tables on the copy for a deterministic active-deal set.

Run:  python -m pytest tests/test_pipeline_health.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_health_", suffix=".db")
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
class HealthBase(unittest.TestCase):
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

    def _deal(self, title, stage="qualified", next_touch=None, last_touch=None,
              touch_count=0):
        aid = _crm.create_account(title + " Co")["account_id"]
        did = _crm.create_deal(aid, title, stage=stage)["deal_id"]
        conn = _db.get_conn()
        try:
            conn.execute(
                "UPDATE deals SET next_touch_date=?, last_touch_date=?, touch_count=? "
                "WHERE id=?", (next_touch, last_touch, touch_count, did))
            conn.commit()
        finally:
            conn.close()
        return did


class Triage(HealthBase):
    def test_empty_is_ok(self):
        h = _growth.pipeline_health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["active_total"], 0)
        self.assertEqual(h["levels"]["red"]["count"], 0)

    def test_red_when_next_touch_past(self):
        did = self._deal("Overdue", next_touch=_iso(-3), last_touch=_iso(-3), touch_count=1)
        h = _growth.pipeline_health()
        red = h["levels"]["red"]
        self.assertEqual(red["count"], 1)
        self.assertEqual(red["deals"][0]["id"], did)
        self.assertEqual(red["deals"][0]["days_overdue"], 3)
        self.assertFalse(h["ok"])

    def test_yellow_when_cold_7_days(self):
        # last touch 10 days ago, next touch scheduled in the future → not red
        did = self._deal("Cold", next_touch=_iso(5), last_touch=_iso(-10), touch_count=2)
        h = _growth.pipeline_health()
        yellow = h["levels"]["yellow"]
        self.assertEqual(yellow["count"], 1)
        self.assertEqual(yellow["deals"][0]["id"], did)
        self.assertEqual(yellow["deals"][0]["days_since_touch"], 10)

    def test_blue_when_no_next_touch(self):
        did = self._deal("Unscheduled", next_touch=None, last_touch=_iso(-2), touch_count=1)
        h = _growth.pipeline_health()
        blue = h["levels"]["blue"]
        self.assertEqual(blue["count"], 1)
        self.assertEqual(blue["deals"][0]["id"], did)

    def test_never_touched_no_next_is_blue(self):
        self._deal("Fresh lead", next_touch=None, last_touch=None, touch_count=0)
        h = _growth.pipeline_health()
        self.assertEqual(h["levels"]["blue"]["count"], 1)
        self.assertEqual(h["levels"]["yellow"]["count"], 0)  # last_touch None → not cold

    def test_priority_red_over_yellow(self):
        # past next-touch AND cold → counts ONCE, as red
        self._deal("Both", next_touch=_iso(-1), last_touch=_iso(-30), touch_count=1)
        h = _growth.pipeline_health()
        self.assertEqual(h["levels"]["red"]["count"], 1)
        self.assertEqual(h["levels"]["yellow"]["count"], 0)

    def test_priority_yellow_over_blue(self):
        # cold AND no next-touch → counts ONCE, as yellow (more urgent)
        self._deal("Cold+unsched", next_touch=None, last_touch=_iso(-14), touch_count=1)
        h = _growth.pipeline_health()
        self.assertEqual(h["levels"]["yellow"]["count"], 1)
        self.assertEqual(h["levels"]["blue"]["count"], 0)

    def test_recent_touch_with_next_is_clean(self):
        # touched 2 days ago, next touch in future → no alert at all
        self._deal("Healthy", next_touch=_iso(5), last_touch=_iso(-2), touch_count=3)
        h = _growth.pipeline_health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["active_total"], 1)

    def test_closed_deals_excluded(self):
        self._deal("Won deal", stage="won", next_touch=_iso(-10))
        self._deal("Lost deal", stage="lost", next_touch=None)
        h = _growth.pipeline_health()
        self.assertEqual(h["active_total"], 0)
        self.assertTrue(h["ok"])

    def test_counts_sum_to_active_total(self):
        self._deal("A", next_touch=_iso(-1))                 # red
        self._deal("B", next_touch=_iso(3), last_touch=_iso(-9))   # yellow
        self._deal("C", next_touch=None)                     # blue
        self._deal("D", next_touch=_iso(3), last_touch=_iso(-1))   # clean
        h = _growth.pipeline_health()
        lv = h["levels"]
        alerted = lv["red"]["count"] + lv["yellow"]["count"] + lv["blue"]["count"]
        self.assertEqual(lv["red"]["count"], 1)
        self.assertEqual(lv["yellow"]["count"], 1)
        self.assertEqual(lv["blue"]["count"], 1)
        self.assertEqual(h["active_total"], 4)
        self.assertLessEqual(alerted, h["active_total"])     # no double-count


class Endpoint(HealthBase):
    def test_endpoint_shape(self):
        self._deal("Overdue", next_touch=_iso(-2))
        r = _CLIENT.get("/api/growth/pipeline-health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for k in ("today", "active_total", "levels", "ok"):
            self.assertIn(k, body)
        self.assertEqual(body["levels"]["red"]["count"], 1)
        self.assertIn("need a touch today", body["levels"]["red"]["label"])

    def test_endpoint_ok_when_empty(self):
        r = _CLIENT.get("/api/growth/pipeline-health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
