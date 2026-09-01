"""Time-block calendar (the operator's week as role-specialized blocks) — regression guard.

Covers:
  1. dashboard/db.py     — time_blocks table + CRUD helpers
  2. dashboard/growth.py — seed / create / update / delete + validation, done-for-week
  3. GET/POST/PATCH/DELETE /api/growth/time-blocks

Isolation: point KANBAN_DB at a copy, restore after import; setUp wipes
time_blocks so seeding + counts are deterministic.

Run:  python -m pytest tests/test_time_blocks.py -v
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
_db = _growth = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_tblk_", suffix=".db")
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
class TimeBlockBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_time_blocks_schema()
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM time_blocks;")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved

    def _block(self, **kw):
        kw.setdefault("day_of_week", 3)
        kw.setdefault("start_time", "10:00")
        kw.setdefault("end_time", "11:00")
        kw.setdefault("role", "sdr")
        kw.setdefault("label", "Custom")
        return _growth.create_time_block(**kw)["block"]["id"]


class Seed(TimeBlockBase):
    def test_seeds_five_defaults_on_first_call(self):
        self.assertEqual(_db.time_blocks_count(), 0)
        res = _growth.list_time_blocks()
        self.assertEqual(len(res["blocks"]), 5)
        self.assertEqual(_db.time_blocks_count(), 5)
        roles = {b["role"] for b in res["blocks"]}
        self.assertEqual(roles, {"sdr", "ae", "marketer", "consultant", "analyst"})

    def test_seed_is_idempotent(self):
        _growth.list_time_blocks()
        _growth.list_time_blocks()
        self.assertEqual(_db.time_blocks_count(), 5)

    def test_seed_does_not_refill_after_manual_delete(self):
        blocks = _growth.list_time_blocks()["blocks"]
        _growth.delete_time_block(blocks[0]["id"])
        # A non-empty table is never re-seeded.
        self.assertEqual(len(_growth.list_time_blocks()["blocks"]), 4)

    def test_blocks_ordered_by_weekday_then_start(self):
        _growth.list_time_blocks()
        dows = [b["day_of_week"] for b in _growth.list_time_blocks()["blocks"]]
        self.assertEqual(dows, sorted(dows))

    def test_response_shape(self):
        res = _growth.list_time_blocks()
        self.assertIn("week", res)
        self.assertEqual(res["roles"], ["sdr", "ae", "marketer", "consultant", "analyst",
                                        "employment", "study"])


class Create(TimeBlockBase):
    def test_create_ok(self):
        res = _growth.create_time_block(
            day_of_week=5, start_time="08:30", end_time="09:45",
            role="analyst", label="Weekend math")
        self.assertEqual(res["status"], "created")
        b = res["block"]
        self.assertTrue(b["id"].startswith("tblk_"))
        self.assertEqual(b["day_of_week"], 5)
        self.assertEqual(b["role"], "analyst")
        self.assertTrue(b["active"])
        self.assertFalse(b["done"])

    def test_create_inactive(self):
        b = _growth.create_time_block(
            day_of_week=1, start_time="09:00", end_time="10:00",
            role="ae", label="x", active=False)["block"]
        self.assertFalse(b["active"])

    def test_bad_day_of_week(self):
        self.assertEqual(_growth.create_time_block(
            day_of_week=7, start_time="09:00", end_time="10:00",
            role="sdr", label="x")["status"], "error")
        self.assertEqual(_growth.create_time_block(
            day_of_week="mon", start_time="09:00", end_time="10:00",
            role="sdr", label="x")["status"], "error")

    def test_bad_role(self):
        self.assertEqual(_growth.create_time_block(
            day_of_week=0, start_time="09:00", end_time="10:00",
            role="ceo", label="x")["status"], "error")

    def test_bad_times(self):
        # not HH:MM
        self.assertEqual(_growth.create_time_block(
            day_of_week=0, start_time="9am", end_time="10:00",
            role="sdr", label="x")["status"], "error")
        # out of range
        self.assertEqual(_growth.create_time_block(
            day_of_week=0, start_time="09:00", end_time="25:00",
            role="sdr", label="x")["status"], "error")
        # end <= start
        self.assertEqual(_growth.create_time_block(
            day_of_week=0, start_time="10:00", end_time="10:00",
            role="sdr", label="x")["status"], "error")

    def test_missing_label(self):
        self.assertEqual(_growth.create_time_block(
            day_of_week=0, start_time="09:00", end_time="10:00",
            role="sdr", label="  ")["status"], "error")


class Update(TimeBlockBase):
    def test_toggle_active(self):
        bid = self._block()
        res = _growth.update_time_block(bid, {"active": False})
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["block"]["active"])
        self.assertTrue(_growth.update_time_block(bid, {"active": True})["block"]["active"])

    def test_update_times(self):
        bid = self._block()
        res = _growth.update_time_block(bid, {"start_time": "14:00", "end_time": "16:30"})
        self.assertEqual(res["block"]["start_time"], "14:00")
        self.assertEqual(res["block"]["end_time"], "16:30")

    def test_update_time_crossfield_uses_existing(self):
        # existing 10:00–11:00; moving only start to 12:00 must fail (>= end).
        bid = self._block()
        self.assertEqual(_growth.update_time_block(bid, {"start_time": "12:00"})["status"], "error")

    def test_mark_done_for_week(self):
        bid = self._block()
        res = _growth.update_time_block(bid, {"done": True})
        self.assertTrue(res["block"]["done"])
        raw = _db.time_block_get(bid)
        self.assertEqual(raw["done_week"], _growth.iso_week())
        # clear it
        self.assertFalse(_growth.update_time_block(bid, {"done": False})["block"]["done"])
        self.assertIsNone(_db.time_block_get(bid)["done_week"])

    def test_done_auto_resets_next_week(self):
        bid = self._block()
        # Stamp a stale (past) week directly → derived done must be False.
        _db.time_block_update(bid, {"done_week": "2000-W01"})
        self.assertFalse(_growth._read_block(bid)["done"])

    def test_update_validation(self):
        bid = self._block()
        self.assertEqual(_growth.update_time_block(bid, {"role": "nope"})["status"], "error")
        self.assertEqual(_growth.update_time_block(bid, {"day_of_week": 9})["status"], "error")
        self.assertEqual(_growth.update_time_block(bid, {"start_time": "bad"})["status"], "error")
        self.assertEqual(_growth.update_time_block(bid, {"label": " "})["status"], "error")

    def test_update_missing(self):
        self.assertEqual(_growth.update_time_block("tblk_nope", {"active": False})["status"], "error")

    def test_update_ignores_unknown_keys(self):
        bid = self._block()
        res = _growth.update_time_block(bid, {"id": "hacked", "created_at": 0})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["block"]["id"], bid)

    def test_delete(self):
        bid = self._block()
        self.assertEqual(_growth.delete_time_block(bid)["status"], "ok")
        self.assertIsNone(_db.time_block_get(bid))
        self.assertEqual(_growth.delete_time_block(bid)["status"], "error")


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class Api(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_time_blocks_schema()
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM time_blocks;")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved

    def test_get_seeds_and_returns(self):
        r = _CLIENT.get("/api/growth/time-blocks")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["blocks"]), 5)

    def test_post_create(self):
        r = _CLIENT.post("/api/growth/time-blocks", json={
            "day_of_week": 4, "start_time": "16:00", "end_time": "17:00",
            "role": "marketer", "label": "Newsletter"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "created")

    def test_post_bad_is_400(self):
        r = _CLIENT.post("/api/growth/time-blocks", json={
            "day_of_week": 4, "start_time": "16:00", "end_time": "17:00",
            "role": "bogus", "label": "x"})
        self.assertEqual(r.status_code, 400)

    def test_patch_toggle(self):
        bid = _CLIENT.get("/api/growth/time-blocks").json()["blocks"][0]["id"]
        r = _CLIENT.patch(f"/api/growth/time-blocks/{bid}", json={"done": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["block"]["done"])

    def test_patch_missing_is_404(self):
        r = _CLIENT.patch("/api/growth/time-blocks/tblk_nope", json={"active": False})
        self.assertEqual(r.status_code, 404)

    def test_patch_bad_is_400(self):
        bid = _CLIENT.get("/api/growth/time-blocks").json()["blocks"][0]["id"]
        r = _CLIENT.patch(f"/api/growth/time-blocks/{bid}", json={"role": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_delete(self):
        bid = _CLIENT.get("/api/growth/time-blocks").json()["blocks"][0]["id"]
        self.assertEqual(_CLIENT.delete(f"/api/growth/time-blocks/{bid}").status_code, 200)
        self.assertEqual(_CLIENT.delete(f"/api/growth/time-blocks/{bid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
