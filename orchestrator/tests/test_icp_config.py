"""ICP config (Strategy → ICP Editor) — regression guard.

Covers the whole feature:
  1. dashboard/db.py       — icp_config table + get/set helpers
  2. dashboard/growth.py   — icp_config()/growth_config()/set_icp() (DB → env)
  3. GET/PATCH /api/growth/icp endpoints

Isolation (same pattern as test_crm_growth): point the DB layer at a COPY of the
real kanban.db, ensure the growth+icp schema on that copy, build the client, then
RESTORE the shared _db.KANBAN_DB global. Each test re-points to the copy in setUp
and restores in tearDown, so this module never hijacks KANBAN_DB for other test
modules in the run. Skips wholesale if there's no kanban.db to copy.

Run:  python -m pytest tests/test_icp_config.py -v
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
_ORIG_KDB = None
_db = None
_growth = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_icp_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)

        _ORIG_KDB = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard import growth as _growth
            _growth.ensure_schema()            # crm + growth + icp tables on the copy
            from dashboard.api import app
            from starlette.testclient import TestClient
            _CLIENT = TestClient(app, raise_server_exceptions=False)
            _READY = True
        finally:
            _db.KANBAN_DB = _ORIG_KDB           # good citizen: don't leak our copy
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
class IcpBase(unittest.TestCase):
    """Point KANBAN_DB at our copy for the duration of each test; wipe icp_config."""

    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM icp_config")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved


class DbLayer(IcpBase):
    def test_schema_and_roundtrip(self):
        _db.ensure_icp_schema()
        cols = [r[1] for r in _db.get_conn().execute("PRAGMA table_info(icp_config)").fetchall()]
        self.assertEqual(set(cols), {"key", "value", "updated_at"})
        self.assertEqual(_db.get_icp_config(), {})           # wiped in setUp
        _db.set_icp_config({"industries": "saas,fintech", "avg_ticket": "50000"})
        got = _db.get_icp_config()
        self.assertEqual(got["industries"], "saas,fintech")
        self.assertEqual(got["avg_ticket"], "50000")

    def test_set_is_upsert(self):
        _db.set_icp_config({"avg_ticket": "10"})
        _db.set_icp_config({"avg_ticket": "20"})            # overwrite, not dup
        self.assertEqual(_db.get_icp_config()["avg_ticket"], "20")
        n = _db.get_conn().execute(
            "SELECT COUNT(*) FROM icp_config WHERE key='avg_ticket'").fetchone()[0]
        self.assertEqual(n, 1)


class GrowthConfig(IcpBase):
    def test_defaults_from_env_when_db_empty(self):
        c = _growth.icp_config()
        # env fallback: industries default set + GROWTH_CONFIG numbers
        self.assertIn("saas", c["industries"])
        self.assertEqual(c["target_revenue"], _growth.GROWTH_CONFIG["revenue_goal"])
        self.assertEqual(c["avg_ticket"], _growth.GROWTH_CONFIG["avg_ticket"])
        self.assertEqual(c["close_rate"], _growth.GROWTH_CONFIG["close_rate"])

    def test_set_icp_string_and_persist(self):
        res = _growth.set_icp({
            "industries": "SaaS, Fintech , ai",       # mixed case + spaces
            "positioning_statement": "We ship agents.",
            "target_revenue": 200000,
            "avg_ticket": 25000,
            "close_rate": 0.4,
        })
        self.assertEqual(res["status"], "ok")
        c = _growth.icp_config()
        self.assertEqual(c["industries"], ["saas", "fintech", "ai"])   # normalized
        self.assertEqual(c["positioning_statement"], "We ship agents.")
        self.assertEqual(c["target_revenue"], 200000.0)
        self.assertEqual(c["avg_ticket"], 25000.0)
        self.assertEqual(c["close_rate"], 0.4)

    def test_set_icp_accepts_list_for_industries(self):
        _growth.set_icp({"industries": ["Logistics", "retail"]})
        self.assertEqual(_growth.icp_config()["industries"], ["logistics", "retail"])

    def test_close_rate_percentage_is_normalized(self):
        _growth.set_icp({"close_rate": 30})               # >1 → treated as %
        self.assertEqual(_growth.icp_config()["close_rate"], 0.3)

    def test_validation_errors(self):
        self.assertEqual(_growth.set_icp({"target_revenue": "abc"})["status"], "error")
        self.assertEqual(_growth.set_icp({"avg_ticket": -5})["status"], "error")
        self.assertEqual(_growth.set_icp({"close_rate": "nope"})["status"], "error")

    def test_growth_config_overlays_db(self):
        _growth.set_icp({"target_revenue": 90000, "avg_ticket": 30000, "close_rate": 0.5})
        cfg = _growth.growth_config()
        self.assertEqual(cfg["revenue_goal"], 90000.0)
        self.assertEqual(cfg["avg_ticket"], 30000.0)
        self.assertEqual(cfg["close_rate"], 0.5)
        # non-ICP fields still come from env defaults
        self.assertEqual(cfg["proposal_rate"], _growth.GROWTH_CONFIG["proposal_rate"])

    def test_pipeline_math_uses_db_config(self):
        _growth.set_icp({"target_revenue": 100000, "avg_ticket": 25000})
        m = _growth.pipeline_math()
        self.assertEqual(m["goal"], 100000.0)
        self.assertEqual(m["avg_ticket"], 25000.0)
        clients = next(f for f in m["funnel"] if f["key"] == "clients")
        self.assertEqual(clients["need"], 4)              # ceil(100000/25000)

    def test_icp_industries_drives_scoring(self):
        _growth.set_icp({"industries": ["spacetech"]})

        def _industry_pts(industry):
            return _growth.score_features(industry=industry)[
                "categories"]["firmographic"]["sub"]["industry"]

        # a matching industry earns the full firmographic industry weight (12)
        self.assertEqual(_industry_pts("spacetech"), 12)
        # a substring overlap with an ICP industry earns the partial 6
        self.assertEqual(_industry_pts("spacetech & robotics"), 6)
        # an unrelated industry earns only the floor 3
        self.assertEqual(_industry_pts("saas"), 3)


class Endpoints(IcpBase):
    def test_get_returns_shape(self):
        r = _CLIENT.get("/api/growth/icp")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for k in ("industries", "positioning_statement", "target_revenue",
                  "avg_ticket", "close_rate"):
            self.assertIn(k, body)
        self.assertIsInstance(body["industries"], list)

    def test_patch_updates_and_get_reflects(self):
        r = _CLIENT.patch("/api/growth/icp", json={
            "industries": "biotech, climate",
            "positioning_statement": "Data science for hard problems.",
            "target_revenue": 150000, "avg_ticket": 50000, "close_rate": 0.35})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        got = _CLIENT.get("/api/growth/icp").json()
        self.assertEqual(got["industries"], ["biotech", "climate"])
        self.assertEqual(got["target_revenue"], 150000.0)
        self.assertEqual(got["close_rate"], 0.35)

    def test_patch_invalid_is_400(self):
        r = _CLIENT.patch("/api/growth/icp", json={"target_revenue": "not-a-number"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
