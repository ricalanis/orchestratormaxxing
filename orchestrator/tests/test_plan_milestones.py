"""90-Day Plan tracker (Strategy → playbook execution) — regression guard.

Covers:
  1. dashboard/db.py     — plan_milestones table + raw helpers
  2. dashboard/growth.py — seed defaults, grouping + progress, toggle
  3. GET /api/growth/plan-milestones + PATCH /api/growth/plan-milestones/{id}

Isolation: the good-citizen pattern (point KANBAN_DB at a copy, restore after
import; re-point + wipe the table in setUp).

Run:  python -m pytest tests/test_plan_milestones.py -v
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
_db = None
_growth = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_plan_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)

        _ORIG_KDB = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard import growth as _growth
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
class PlanBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_plan_schema()
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM plan_milestones")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved


class Seeding(PlanBase):
    def test_seeds_all_13_milestones_across_3_phases(self):
        out = _growth.list_plan_milestones()
        self.assertEqual(len(out["milestones"]), 13)
        self.assertEqual(len(out["phases"]), 3)
        counts = {p["key"]: p["total"] for p in out["phases"]}
        self.assertEqual(counts, {"fundaciones": 4, "motor": 4, "optimizacion": 5})

    def test_phase_order_and_days(self):
        phases = _growth.list_plan_milestones()["phases"]
        self.assertEqual([p["key"] for p in phases],
                         ["fundaciones", "motor", "optimizacion"])
        self.assertEqual(phases[0]["days"], "Días 1–30")
        self.assertEqual(phases[2]["days"], "Días 61–90")

    def test_known_titles_present(self):
        titles = {m["title"] for m in _growth.list_plan_milestones()["milestones"]}
        for t in ("Write positioning statement",
                  "Publish weekly insight-led content 4 weeks",
                  "Review talk-listen ratio"):
            self.assertIn(t, titles)

    def test_seed_is_idempotent(self):
        _growth.list_plan_milestones()
        _growth.list_plan_milestones()
        self.assertEqual(len(_db.plan_milestones_all()), 13)

    def test_all_start_incomplete(self):
        out = _growth.list_plan_milestones()
        self.assertEqual(out["overall"]["done"], 0)
        self.assertEqual(out["overall"]["pct"], 0)


class Progress(PlanBase):
    def test_toggle_updates_progress(self):
        ms = _growth.list_plan_milestones()["milestones"]
        fund = [m for m in ms if m["phase"] == "fundaciones"]
        # complete 2 of 4 in phase 1
        for m in fund[:2]:
            res = _growth.set_milestone_completed(m["id"], True)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["milestone"]["completed"], 1)
            self.assertIsNotNone(res["milestone"]["completed_at"])
        out = _growth.list_plan_milestones()
        p1 = next(p for p in out["phases"] if p["key"] == "fundaciones")
        self.assertEqual(p1["done"], 2)
        self.assertEqual(p1["pct"], 50)                 # 2/4
        self.assertEqual(out["overall"]["done"], 2)
        self.assertEqual(out["overall"]["pct"], round(2 / 13 * 100))

    def test_toggle_without_arg_flips(self):
        mid = _growth.list_plan_milestones()["milestones"][0]["id"]
        self.assertEqual(_growth.set_milestone_completed(mid)["milestone"]["completed"], 1)
        r = _growth.set_milestone_completed(mid)                      # flip back
        self.assertEqual(r["milestone"]["completed"], 0)
        self.assertIsNone(r["milestone"]["completed_at"])            # cleared

    def test_explicit_false_uncompletes(self):
        mid = _growth.list_plan_milestones()["milestones"][0]["id"]
        _growth.set_milestone_completed(mid, True)
        r = _growth.set_milestone_completed(mid, False)
        self.assertEqual(r["milestone"]["completed"], 0)

    def test_toggle_missing_is_error(self):
        self.assertEqual(
            _growth.set_milestone_completed("mile_nope", True)["status"], "error")


class Endpoints(PlanBase):
    def test_get_seeds_and_returns(self):
        r = _CLIENT.get("/api/growth/plan-milestones")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["milestones"]), 13)
        self.assertEqual(body["overall"]["total"], 13)

    def test_patch_toggles_completed(self):
        mid = _CLIENT.get("/api/growth/plan-milestones").json()["milestones"][0]["id"]
        r = _CLIENT.patch(f"/api/growth/plan-milestones/{mid}", json={"completed": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["milestone"]["completed"], 1)
        # reflected in the aggregate
        self.assertEqual(_CLIENT.get("/api/growth/plan-milestones").json()["overall"]["done"], 1)

    def test_patch_no_body_flips(self):
        mid = _CLIENT.get("/api/growth/plan-milestones").json()["milestones"][0]["id"]
        r = _CLIENT.patch(f"/api/growth/plan-milestones/{mid}")     # no body → toggle
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["milestone"]["completed"], 1)

    def test_patch_missing_is_404(self):
        r = _CLIENT.patch("/api/growth/plan-milestones/mile_missing", json={"completed": True})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
