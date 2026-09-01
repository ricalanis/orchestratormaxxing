"""Content pipeline calendar (content_pieces) — regression guard.

Covers:
  1. dashboard/db.py     — content_pieces table + CRUD helpers
  2. dashboard/growth.py — create/update/delete + content_cadence reads pieces
  3. GET/POST/PATCH/DELETE /api/growth/content

Note: content_pieces SUPERSEDES content_log as the cadence source of truth; the
back-compat of GET/POST /api/growth/content (this_week/streak) is separately
pinned by test_crm_growth.py — kept green by this change.

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes
content_pieces (and content_log) on the copy.

Run:  python -m pytest tests/test_content_pipeline.py -v
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
_db = _growth = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_content_", suffix=".db")
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
class ContentBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_content_pieces_schema()
        conn = _db.get_conn()
        try:
            conn.executescript("DELETE FROM content_pieces; DELETE FROM content_log;")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved


class Create(ContentBase):
    def test_create_defaults(self):
        res = _growth.create_content_piece(title="Weekly note")
        self.assertEqual(res["status"], "created")
        p = res["piece"]
        self.assertTrue(p["id"].startswith("cnt_"))
        self.assertEqual(p["status"], "idea")                      # default
        self.assertEqual(p["publish_date"], datetime.date.today().isoformat())  # default today

    def test_create_full(self):
        p = _growth.create_content_piece(
            title="MVP in 2 weeks", topic="agentic dev", channel="blog",
            growth_loop="autoridad", hook="ship faster", publish_date="2026-08-01",
            status="scheduled")["piece"]
        self.assertEqual(p["channel"], "blog")
        self.assertEqual(p["growth_loop"], "autoridad")
        self.assertEqual(p["hook"], "ship faster")
        self.assertEqual(p["publish_date"], "2026-08-01")
        self.assertEqual(p["status"], "scheduled")

    def test_create_validation(self):
        self.assertEqual(_growth.create_content_piece(title="")["status"], "error")
        self.assertEqual(_growth.create_content_piece(title="x", channel="myspace")["status"], "error")
        self.assertEqual(_growth.create_content_piece(title="x", growth_loop="viral")["status"], "error")
        self.assertEqual(_growth.create_content_piece(title="x", status="done")["status"], "error")
        self.assertEqual(_growth.create_content_piece(title="x", publish_date="Aug 1")["status"], "error")


class Update(ContentBase):
    def _piece(self, **kw):
        kw.setdefault("title", "P")
        return _growth.create_content_piece(**kw)["piece"]["id"]

    def test_update_fields(self):
        cid = self._piece(status="idea")
        res = _growth.update_content_piece(cid, {"status": "published", "channel": "linkedin"})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["piece"]["status"], "published")
        self.assertEqual(res["piece"]["channel"], "linkedin")

    def test_update_validation(self):
        cid = self._piece()
        self.assertEqual(_growth.update_content_piece(cid, {"status": "nope"})["status"], "error")
        self.assertEqual(_growth.update_content_piece(cid, {"channel": "myspace"})["status"], "error")
        self.assertEqual(_growth.update_content_piece(cid, {"title": "  "})["status"], "error")

    def test_update_missing(self):
        self.assertEqual(_growth.update_content_piece("cnt_nope", {"status": "draft"})["status"], "error")

    def test_update_ignores_unknown_keys(self):
        cid = self._piece()
        res = _growth.update_content_piece(cid, {"id": "hacked", "created_at": 0})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["piece"]["id"], cid)

    def test_delete(self):
        cid = self._piece()
        self.assertEqual(_growth.delete_content_piece(cid)["status"], "ok")
        self.assertIsNone(_db.content_piece_get(cid))
        self.assertEqual(_growth.delete_content_piece(cid)["status"], "error")


class Cadence(ContentBase):
    def test_cadence_reads_pieces(self):
        _growth.create_content_piece(title="A")   # publish today
        _growth.create_content_piece(title="B")
        cad = _growth.content_cadence()
        self.assertEqual(cad["this_week"], 2)
        self.assertGreaterEqual(cad["streak"], 1)
        self.assertEqual(len(cad["pieces"]), 2)     # calendar payload

    def test_cadence_buckets_by_publish_date(self):
        # a piece dated 3 weeks ago should NOT count as this week
        old = (datetime.date.today() - datetime.timedelta(days=21)).isoformat()
        _growth.create_content_piece(title="Old", publish_date=old)
        cad = _growth.content_cadence()
        self.assertEqual(cad["this_week"], 0)
        self.assertEqual(cad["total"], 1)


class Endpoints(ContentBase):
    def test_get_returns_cadence_and_pieces(self):
        _CLIENT.post("/api/growth/content", json={"title": "Piece 1", "channel": "blog"})
        j = _CLIENT.get("/api/growth/content").json()
        self.assertIn("this_week", j)               # back-compat cadence shape
        self.assertIn("pieces", j)                  # new calendar payload
        self.assertEqual(len(j["pieces"]), 1)

    def test_post_creates_with_new_fields(self):
        r = _CLIENT.post("/api/growth/content", json={
            "title": "Deep dive", "topic": "rag", "channel": "youtube",
            "growth_loop": "producto", "status": "draft", "publish_date": "2026-09-10"})
        self.assertEqual(r.status_code, 200)
        p = r.json()["piece"]
        self.assertEqual(p["channel"], "youtube")
        self.assertEqual(p["status"], "draft")

    def test_post_bad_channel_400(self):
        r = _CLIENT.post("/api/growth/content", json={"title": "x", "channel": "myspace"})
        self.assertEqual(r.status_code, 400)

    def test_patch_and_delete(self):
        cid = _CLIENT.post("/api/growth/content", json={"title": "Editable"}).json()["piece"]["id"]
        r = _CLIENT.patch(f"/api/growth/content/{cid}", json={"status": "published"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["piece"]["status"], "published")
        self.assertEqual(_CLIENT.patch(f"/api/growth/content/{cid}", json={"status": "bad"}).status_code, 400)
        self.assertEqual(_CLIENT.delete(f"/api/growth/content/{cid}").status_code, 200)
        self.assertEqual(_CLIENT.delete(f"/api/growth/content/{cid}").status_code, 404)

    def test_patch_missing_404(self):
        self.assertEqual(
            _CLIENT.patch("/api/growth/content/cnt_missing", json={"status": "draft"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
