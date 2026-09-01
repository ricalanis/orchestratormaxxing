"""Nurture sequences (per-deal Hook cadence) — regression guard.

Covers:
  1. dashboard/db.py     — nurture_sequences table + helpers
  2. dashboard/growth.py — generate (5-step Hook), get, status transitions
  3. GET /api/growth/nurture/{deal_id} · POST .../generate · PATCH .../{id}

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes the CRM
tables + nurture_sequences on the copy.

Run:  python -m pytest tests/test_nurture.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_nurture_", suffix=".db")
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
class NurtureBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_nurture_schema()
        conn = _db.get_conn()
        try:
            conn.executescript(
                "DELETE FROM nurture_sequences;"
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

    def _deal(self, title="Acme deal", stage="lead", source="referral"):
        aid = _crm.create_account("Acme")["account_id"]
        did = _crm.create_deal(aid, title, stage=stage)["deal_id"]
        conn = _db.get_conn()
        try:
            conn.execute("UPDATE deals SET lead_source=? WHERE id=?", (source, did))
            conn.commit()
        finally:
            conn.close()
        return did


class Generate(NurtureBase):
    def test_generate_creates_5_steps(self):
        did = self._deal()
        res = _growth.generate_nurture(did)
        self.assertEqual(res["status"], "ok")
        steps = res["sequence"]["steps"]
        self.assertEqual(len(steps), 5)
        self.assertEqual([s["step_number"] for s in steps], [1, 2, 3, 4, 5])
        self.assertEqual([s["touch_type"] for s in steps],
                         ["trigger", "action", "variable_reward", "investment", "re_trigger"])
        self.assertTrue(all(s["status"] == "pending" for s in steps))

    def test_scheduled_dates_follow_cadence(self):
        did = self._deal()
        steps = _growth.generate_nurture(did)["sequence"]["steps"]
        today = datetime.date.today()
        expected = [(today + datetime.timedelta(days=d)).isoformat() for d in (0, 2, 5, 9, 14)]
        self.assertEqual([s["scheduled_date"] for s in steps], expected)

    def test_template_uses_deal_data(self):
        did = self._deal(title="Beta deal", source="linkedin")
        steps = _growth.generate_nurture(did)["sequence"]["steps"]
        # account name "Acme" flows into the copy; linkedin picks its opener
        self.assertIn("Acme", steps[0]["template_text"])
        self.assertIn("LinkedIn", steps[0]["template_text"])

    def test_regenerate_replaces_not_appends(self):
        did = self._deal()
        _growth.generate_nurture(did)
        _growth.generate_nurture(did)
        self.assertEqual(len(_db.nurture_for_deal(did)), 5)

    def test_generate_missing_deal_is_error(self):
        self.assertEqual(_growth.generate_nurture("deal_nope")["status"], "error")


class GetAndStatus(NurtureBase):
    def test_get_empty_when_none(self):
        did = self._deal()
        seq = _growth.get_nurture(did)
        self.assertEqual(seq["steps"], [])
        self.assertEqual(seq["total"], 0)
        self.assertIsNone(seq["next_suggested_date"])

    def test_next_suggested_is_earliest_pending(self):
        did = self._deal()
        _growth.generate_nurture(did)
        today = datetime.date.today().isoformat()
        self.assertEqual(_growth.get_nurture(did)["next_suggested_date"], today)
        # mark step 1 sent → next moves to step 2 (today+2)
        step1 = _db.nurture_for_deal(did)[0]
        _growth.set_nurture_status(step1["id"], "sent")
        seq = _growth.get_nurture(did)
        self.assertEqual(seq["next_suggested_date"],
                         (datetime.date.today() + datetime.timedelta(days=2)).isoformat())
        self.assertEqual(seq["completed"], 1)
        self.assertEqual(seq["counts"]["sent"], 1)

    def test_skipped_counts_as_completed_not_next(self):
        did = self._deal()
        _growth.generate_nurture(did)
        s1 = _db.nurture_for_deal(did)[0]
        _growth.set_nurture_status(s1["id"], "skipped")
        seq = _growth.get_nurture(did)
        self.assertEqual(seq["counts"]["skipped"], 1)
        self.assertEqual(seq["completed"], 1)
        # skipped step no longer the next suggested
        self.assertNotEqual(seq["next_suggested_date"], s1["scheduled_date"])

    def test_status_validation(self):
        did = self._deal()
        nid = _growth.generate_nurture(did)["sequence"]["steps"][0]["id"]
        self.assertEqual(_growth.set_nurture_status(nid, "done")["status"], "error")  # bad status
        self.assertEqual(_growth.set_nurture_status("nur_nope", "sent")["status"], "error")

    def test_all_done_next_is_none(self):
        did = self._deal()
        _growth.generate_nurture(did)
        for s in _db.nurture_for_deal(did):
            _growth.set_nurture_status(s["id"], "sent")
        self.assertIsNone(_growth.get_nurture(did)["next_suggested_date"])


class Endpoints(NurtureBase):
    def test_generate_and_get(self):
        did = self._deal()
        r = _CLIENT.post(f"/api/growth/nurture/{did}/generate")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["sequence"]["steps"]), 5)
        g = _CLIENT.get(f"/api/growth/nurture/{did}")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["total"], 5)

    def test_generate_missing_deal_404(self):
        self.assertEqual(_CLIENT.post("/api/growth/nurture/deal_missing/generate").status_code, 404)

    def test_patch_status(self):
        did = self._deal()
        nid = _CLIENT.post(f"/api/growth/nurture/{did}/generate").json()["sequence"]["steps"][0]["id"]
        r = _CLIENT.patch(f"/api/growth/nurture/{nid}", json={"status": "sent"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["step"]["status"], "sent")

    def test_patch_invalid_status_400(self):
        did = self._deal()
        nid = _CLIENT.post(f"/api/growth/nurture/{did}/generate").json()["sequence"]["steps"][0]["id"]
        self.assertEqual(_CLIENT.patch(f"/api/growth/nurture/{nid}", json={"status": "nope"}).status_code, 400)

    def test_patch_missing_step_404(self):
        self.assertEqual(
            _CLIENT.patch("/api/growth/nurture/nur_missing", json={"status": "sent"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
