"""Scorecard WoW deltas + targets — regression guard for P2 CRM polish.

Covers growth.scorecard() enhancements:
  - Each KPI now carries: value, target, wow_delta, wow_pct, prev_value
  - WoW delta = current_week - prior_week for each KPI
  - wow_pct = round(delta/prev * 100), or 100 when going from 0→N
  - Targets are defined in SCORECARD_TARGETS for all 5 KPIs
  - API endpoint /api/scorecard returns the enriched KPIs

Also covers the drag-to-stage PATCH endpoint:
  - PATCH /api/crm/deals/{id} with {stage: X} changes the deal stage
  - Stage change emits a stage_changed deal_event (for scorecard proposals count)

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes the CRM
tables on the copy for a deterministic deal/event set.

Run:  python -m pytest tests/test_scorecard_wow.py -v
"""
import atexit
import datetime
import json
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
_DASH_TOKEN = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_scwow_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)

        _ORIG_KDB = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard import crm as _crm, growth as _growth
            _growth.ensure_schema()
            from dashboard.api import app, _DASH_TOKEN as _tok
            from starlette.testclient import TestClient
            _DASH_TOKEN = _tok
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


def _now_epoch() -> int:
    return int(datetime.datetime.now().timestamp())


def _auth_headers():
    """Bearer auth headers for mutating endpoints."""
    if _DASH_TOKEN:
        return {"Authorization": f"Bearer {_DASH_TOKEN}"}
    return {}


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class ScorecardWoWBase(unittest.TestCase):
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
                "DELETE FROM accounts;"
                "DELETE FROM content_pieces;")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved

    def _create_deal(self, title, stage="lead"):
        """Create a deal (auto-emits deal_created event at current time)."""
        aid = _crm.create_account(title + " Co")["account_id"]
        did = _crm.create_deal(aid, title, stage=stage)["deal_id"]
        return did

    def _shift_deal_event_to(self, deal_id, kind, when):
        """Move a deal_event's created_at to a specific epoch."""
        conn = _db.get_conn()
        try:
            # SQLite UPDATE doesn't support ORDER BY; subquery to get the latest id.
            conn.execute(
                "UPDATE deal_events SET created_at=? WHERE id IN ("
                "  SELECT id FROM deal_events WHERE deal_id=? AND kind=? "
                "  ORDER BY created_at DESC LIMIT 1)",
                (when, deal_id, kind))
            conn.commit()
        finally:
            conn.close()


class ScorecardFields(ScorecardWoWBase):
    """Verify the scorecard returns the new fields per KPI."""

    def test_kpis_have_target(self):
        s = _growth.scorecard()
        for k in s["kpis"]:
            self.assertIn("target", k, f"{k['key']} missing target")
            self.assertIsNotNone(k["target"], f"{k['key']} target is None")
            self.assertGreater(k["target"], 0, f"{k['key']} target should be > 0")

    def test_kpis_have_wow_delta(self):
        s = _growth.scorecard()
        for k in s["kpis"]:
            self.assertIn("wow_delta", k, f"{k['key']} missing wow_delta")
            self.assertIn("wow_pct", k, f"{k['key']} missing wow_pct")
            self.assertIn("prev_value", k, f"{k['key']} missing prev_value")

    def test_wow_delta_zero_on_empty(self):
        s = _growth.scorecard()
        for k in s["kpis"]:
            self.assertEqual(k["value"], 0)
            self.assertEqual(k["wow_delta"], 0)
            self.assertEqual(k["prev_value"], 0)

    def test_wow_delta_positive_with_events_this_week(self):
        now = _now_epoch()
        did = self._create_deal("This week lead")
        # Shift the deal_created event to now (this week)
        self._shift_deal_event_to(did, "deal_created", now)
        s = _growth.scorecard()
        leads_kpi = next(k for k in s["kpis"] if k["key"] == "leads")
        self.assertEqual(leads_kpi["value"], 1)
        self.assertEqual(leads_kpi["wow_delta"], 1)
        self.assertEqual(leads_kpi["prev_value"], 0)
        # 0→1 is 100% WoW
        self.assertEqual(leads_kpi["wow_pct"], 100)

    def test_wow_delta_negative_when_prev_higher(self):
        now = _now_epoch()
        # Create 3 deals last week, 1 this week
        last_week = now - 8 * 86400
        prev_ids = []
        for i in range(3):
            did = self._create_deal(f"Last wk {i}")
            self._shift_deal_event_to(did, "deal_created", last_week)
            prev_ids.append(did)
        did = self._create_deal("This wk")
        self._shift_deal_event_to(did, "deal_created", now)
        s = _growth.scorecard()
        leads_kpi = next(k for k in s["kpis"] if k["key"] == "leads")
        self.assertEqual(leads_kpi["value"], 1)
        self.assertEqual(leads_kpi["prev_value"], 3)
        self.assertEqual(leads_kpi["wow_delta"], -2)
        # -2/3 = -67%
        self.assertEqual(leads_kpi["wow_pct"], -67)

    def test_targets_constant_values(self):
        """All 5 KPIs carry a positive-integer target. The exact numbers are
        the operator's standing commitment (advisory era: HERMES_TARGET_<KPI>
        env-overridable), so the invariant is structure, not a frozen value —
        a target of 0 or a missing KPI would silence the amber logic."""
        self.assertEqual(
            set(_growth.SCORECARD_TARGETS),
            {"leads", "touches", "discovery", "content", "proposals"})
        for key, target in _growth.SCORECARD_TARGETS.items():
            self.assertIsInstance(target, int, key)
            self.assertGreater(target, 0, key)

    def test_api_returns_enriched_kpis(self):
        r = _CLIENT.get("/api/scorecard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("kpis", body)
        for k in body["kpis"]:
            self.assertIn("target", k)
            self.assertIn("wow_delta", k)
            self.assertIn("wow_pct", k)
            self.assertIn("prev_value", k)


class ScorecardTotalActivity(ScorecardWoWBase):
    """Total activity still sums correctly with the new structure."""

    def test_total_activity_sums_values(self):
        now = _now_epoch()
        d1 = self._create_deal("L1")
        self._shift_deal_event_to(d1, "deal_created", now)
        d2 = self._create_deal("L2")
        self._shift_deal_event_to(d2, "deal_created", now)
        s = _growth.scorecard()
        expected = sum(k["value"] for k in s["kpis"])
        self.assertEqual(s["total_activity"], expected)


class DragToStagePatch(ScorecardWoWBase):
    """Verify PATCH /api/crm/deals/{id} changes stage (the drag-to-stage backend)."""

    def _make_deal(self, stage="lead"):
        aid = _crm.create_account("Drag Co")["account_id"]
        did = _crm.create_deal(aid, "Drag me", stage=stage)["deal_id"]
        return did

    def test_patch_changes_stage(self):
        did = self._make_deal("lead")
        r = _CLIENT.patch(f"/api/crm/deals/{did}",
                          json={"stage": "qualified"}, headers=_auth_headers())
        self.assertEqual(r.status_code, 200)
        deal = _crm.get_deal(did)
        self.assertEqual(deal["stage"], "qualified")

    def test_patch_emits_stage_changed_event(self):
        did = self._make_deal("lead")
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "proposal"},
                      headers=_auth_headers())
        conn = _db.get_conn()
        try:
            rows = conn.execute(
                "SELECT kind, payload FROM deal_events WHERE deal_id=? "
                "AND kind='stage_changed' ORDER BY created_at DESC LIMIT 1",
                (did,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][1] or "{}")
        self.assertEqual(payload.get("to"), "proposal")
        self.assertEqual(payload.get("from"), "lead")

    def test_patch_to_won_stamps_closed_at(self):
        did = self._make_deal("engaged")
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "won"},
                      headers=_auth_headers())
        deal = _crm.get_deal(did)
        self.assertEqual(deal["stage"], "won")
        self.assertIsNotNone(deal.get("closed_at"))

    def test_patch_to_stalled_does_not_stamp_closed_at(self):
        did = self._make_deal("engaged")
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "stalled"},
                      headers=_auth_headers())
        deal = _crm.get_deal(did)
        self.assertEqual(deal["stage"], "stalled")
        self.assertIsNone(deal.get("closed_at"))

    def test_patch_nonexistent_deal_404(self):
        r = _CLIENT.patch("/api/crm/deals/nonexistent", json={"stage": "won"},
                          headers=_auth_headers())
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()