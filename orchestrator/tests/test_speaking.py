"""Speaking pipeline (talks as attraction-loop generators) — regression guard.

Covers:
  1. dashboard/db.py     — speaking_events table + CRUD helpers
  2. dashboard/growth.py — create/update/delete + validation, deal linkage
  3. GET/POST/PATCH/DELETE /api/growth/speaking

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes
speaking_events (and the CRM tables, so a linked deal is deterministic).

Run:  python -m pytest tests/test_speaking.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_speaking_", suffix=".db")
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
class SpeakingBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_speaking_schema()
        conn = _db.get_conn()
        try:
            conn.executescript(
                "DELETE FROM speaking_events;"
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

    def _deal(self):
        aid = _crm.create_account("Acme")["account_id"]
        return _crm.create_deal(aid, "Acme deal")["deal_id"]


class Create(SpeakingBase):
    def test_create_defaults(self):
        res = _growth.create_speaking(title="Agentic dev talk")
        self.assertEqual(res["status"], "created")
        ev = res["event"]
        self.assertTrue(ev["id"].startswith("talk_"))
        self.assertEqual(ev["status"], "proposed")                 # default
        self.assertEqual(ev["attraction_loop_status"], "none")     # default
        self.assertIsNone(ev["deal_id"])

    def test_create_full(self):
        ev = _growth.create_speaking(
            title="MVP with agents", event_name="PyData", event_date="2026-09-20",
            status="accepted", attraction_loop_status="pre")["event"]
        self.assertEqual(ev["event_name"], "PyData")
        self.assertEqual(ev["event_date"], "2026-09-20")
        self.assertEqual(ev["status"], "accepted")
        self.assertEqual(ev["attraction_loop_status"], "pre")

    def test_create_validation(self):
        self.assertEqual(_growth.create_speaking(title="")["status"], "error")
        self.assertEqual(_growth.create_speaking(title="x", status="keynote")["status"], "error")
        self.assertEqual(_growth.create_speaking(title="x", attraction_loop_status="after")["status"], "error")
        self.assertEqual(_growth.create_speaking(title="x", event_date="Sept 1")["status"], "error")

    def test_create_with_valid_deal(self):
        did = self._deal()
        ev = _growth.create_speaking(title="Converted talk", deal_id=did)["event"]
        self.assertEqual(ev["deal_id"], did)

    def test_create_with_bad_deal_is_error(self):
        self.assertEqual(_growth.create_speaking(title="x", deal_id="deal_nope")["status"], "error")


class Update(SpeakingBase):
    def _talk(self, **kw):
        kw.setdefault("title", "T")
        return _growth.create_speaking(**kw)["event"]["id"]

    def test_update_status_and_loop(self):
        sid = self._talk()
        res = _growth.update_speaking(sid, {"status": "delivered", "attraction_loop_status": "post"})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["event"]["status"], "delivered")
        self.assertEqual(res["event"]["attraction_loop_status"], "post")

    def test_update_link_deal(self):
        sid = self._talk()
        did = self._deal()
        res = _growth.update_speaking(sid, {"deal_id": did})
        self.assertEqual(res["event"]["deal_id"], did)

    def test_update_validation(self):
        sid = self._talk()
        self.assertEqual(_growth.update_speaking(sid, {"status": "nope"})["status"], "error")
        self.assertEqual(_growth.update_speaking(sid, {"attraction_loop_status": "x"})["status"], "error")
        self.assertEqual(_growth.update_speaking(sid, {"title": " "})["status"], "error")
        self.assertEqual(_growth.update_speaking(sid, {"deal_id": "deal_missing"})["status"], "error")

    def test_update_missing(self):
        self.assertEqual(_growth.update_speaking("talk_nope", {"status": "accepted"})["status"], "error")

    def test_update_ignores_unknown_keys(self):
        sid = self._talk()
        res = _growth.update_speaking(sid, {"id": "hacked", "created_at": 0})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["event"]["id"], sid)

    def test_delete(self):
        sid = self._talk()
        self.assertEqual(_growth.delete_speaking(sid)["status"], "ok")
        self.assertIsNone(_db.speaking_event_get(sid))
        self.assertEqual(_growth.delete_speaking(sid)["status"], "error")


class Endpoints(SpeakingBase):
    def test_get_list(self):
        _growth.create_speaking(title="A")
        r = _CLIENT.get("/api/growth/speaking")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["events"]), 1)

    def test_post_creates(self):
        r = _CLIENT.post("/api/growth/speaking", json={
            "title": "Keynote", "event_name": "Conf", "status": "scheduled",
            "attraction_loop_status": "during"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["event"]["status"], "scheduled")

    def test_post_invalid_400(self):
        self.assertEqual(_CLIENT.post("/api/growth/speaking", json={"title": ""}).status_code, 400)
        self.assertEqual(
            _CLIENT.post("/api/growth/speaking", json={"title": "x", "status": "bad"}).status_code, 400)

    def test_patch_and_delete(self):
        sid = _CLIENT.post("/api/growth/speaking", json={"title": "Editable"}).json()["event"]["id"]
        r = _CLIENT.patch(f"/api/growth/speaking/{sid}", json={"status": "delivered"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["event"]["status"], "delivered")
        self.assertEqual(_CLIENT.patch(f"/api/growth/speaking/{sid}", json={"status": "bad"}).status_code, 400)
        self.assertEqual(_CLIENT.delete(f"/api/growth/speaking/{sid}").status_code, 200)
        self.assertEqual(_CLIENT.delete(f"/api/growth/speaking/{sid}").status_code, 404)

    def test_patch_missing_404(self):
        self.assertEqual(
            _CLIENT.patch("/api/growth/speaking/talk_missing", json={"status": "accepted"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
