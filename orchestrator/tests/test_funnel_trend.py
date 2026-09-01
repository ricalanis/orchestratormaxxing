"""Conversion funnel over time (weekly snapshots) — regression guard.

Covers:
  1. dashboard/db.py     — conversion_snapshots table + upsert helper
  2. dashboard/growth.py — compute_funnel math, snapshot_funnel (one/week),
                           funnel_trend (seeds + last N)
  3. GET /api/growth/funnel-trend + POST /api/growth/funnel-snapshot

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes the CRM
tables + conversion_snapshots so counts/rates are deterministic.

Run:  python -m pytest tests/test_funnel_trend.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_funnel_", suffix=".db")
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
class FunnelBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_conversion_schema()
        conn = _db.get_conn()
        try:
            conn.executescript(
                "DELETE FROM conversion_snapshots;"
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

    def _deal(self, stage):
        aid = _crm.create_account(stage + " Co")["account_id"]
        return _crm.create_deal(aid, stage + " deal", stage=stage)["deal_id"]


class Compute(FunnelBase):
    def test_cumulative_counts_and_rates(self):
        # one deal at each of lead / qualified / proposal / won, plus a lost
        for st in ("lead", "qualified", "proposal", "won", "lost"):
            self._deal(st)
        f = _growth.compute_funnel()
        # cumulative: won counts in every step, lost excluded
        self.assertEqual(f["lead_count"], 4)       # lead,qualified,proposal,won
        self.assertEqual(f["discovery_count"], 3)  # qualified,proposal,won
        self.assertEqual(f["proposal_count"], 2)   # proposal,won
        self.assertEqual(f["won_count"], 1)        # won
        self.assertEqual(f["lead_to_discovery_rate"], 0.75)     # 3/4
        self.assertEqual(f["discovery_to_proposal_rate"], round(2 / 3, 4))
        self.assertEqual(f["proposal_to_won_rate"], 0.5)        # 1/2
        self.assertEqual(f["overall_rate"], 0.25)              # 1/4

    def test_empty_is_zero_not_divide_error(self):
        f = _growth.compute_funnel([])
        self.assertEqual(f["lead_count"], 0)
        self.assertEqual(f["overall_rate"], 0.0)
        self.assertEqual(f["lead_to_discovery_rate"], 0.0)

    def test_monotonic_funnel(self):
        self._deal("won"); self._deal("qualified"); self._deal("lead")
        f = _growth.compute_funnel()
        self.assertGreaterEqual(f["lead_count"], f["discovery_count"])
        self.assertGreaterEqual(f["discovery_count"], f["proposal_count"])
        self.assertGreaterEqual(f["proposal_count"], f["won_count"])

    def test_a_delivered_deal_remains_a_won_conversion(self):
        """REPLACED CONTRACT (journey fase 1, ruling 2). This used to create a
        deal at `stage='delivered'` and assert the funnel still counted it as a
        won conversion — a guard against the old bug where delivery silently
        dropped a deal out of the funnel. `delivered` is retired as a stage now,
        so the same question is asked of the shape that ships: a won deal whose
        project has been delivered. It cannot fall out, because it never leaves
        `won` in the first place."""
        did = self._deal("won")
        conn = _db.get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, slug, name, created_at, status) "
                "VALUES (?,?,?,?,?)",
                ("proj_funnel_delivered", "funnel-delivered", "Funnel Delivery",
                 1, "delivered"))
            conn.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                         ("proj_funnel_delivered", did))
            conn.commit()
        finally:
            conn.close()
        f = _growth.compute_funnel()
        self.assertEqual(f["lead_count"], 1)
        self.assertEqual(f["proposal_count"], 1)
        self.assertEqual(f["won_count"], 1)
        self.assertEqual(f["overall_rate"], 1.0)


class Snapshot(FunnelBase):
    def test_snapshot_current_week(self):
        self._deal("won"); self._deal("proposal")
        res = _growth.snapshot_funnel()
        self.assertEqual(res["status"], "ok")
        s = res["snapshot"]
        monday = (datetime.date.today()
                  - datetime.timedelta(days=datetime.date.today().weekday())).isoformat()
        self.assertEqual(s["week_start"], monday)
        self.assertEqual(s["lead_count"], 2)
        self.assertEqual(s["won_count"], 1)

    def test_snapshot_is_one_per_week(self):
        self._deal("lead")
        _growth.snapshot_funnel()
        self._deal("won")               # deals change…
        _growth.snapshot_funnel()       # …but same week overwrites, not appends
        rows = _db.conversion_snapshots_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["won_count"], 1)   # reflects the latest compute

    def test_snapshot_specific_week(self):
        _growth.snapshot_funnel(week_start="2026-01-05")
        self.assertIsNotNone(_db.conversion_snapshot_get_week("2026-01-05"))


class Trend(FunnelBase):
    def test_trend_seeds_when_empty(self):
        t = _growth.funnel_trend()
        self.assertEqual(len(t["snapshots"]), 1)     # seeded from current deals
        self.assertIsNotNone(t["latest"])

    def test_trend_returns_last_n_ordered(self):
        for wk in ("2026-01-05", "2026-01-12", "2026-01-19"):
            _growth.snapshot_funnel(week_start=wk)
        t = _growth.funnel_trend(weeks=2)
        self.assertEqual(len(t["snapshots"]), 2)
        # oldest→newest, last two of the three
        self.assertEqual([s["week_start"] for s in t["snapshots"]],
                         ["2026-01-12", "2026-01-19"])


class Endpoints(FunnelBase):
    def test_get_funnel_trend(self):
        self._deal("won")
        r = _CLIENT.get("/api/growth/funnel-trend?weeks=12")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("snapshots", body)
        self.assertGreaterEqual(len(body["snapshots"]), 1)   # seeded
        self.assertIsNotNone(body["latest"])

    def test_post_snapshot(self):
        self._deal("proposal")
        r = _CLIENT.post("/api/growth/funnel-snapshot", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["snapshot"]["proposal_count"], 1)


if __name__ == "__main__":
    unittest.main()
