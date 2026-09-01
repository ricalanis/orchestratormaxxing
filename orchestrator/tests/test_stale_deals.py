"""Stale-deal truthfulness — the touch clock, not the edit clock.

Regression guard for the 2026-08-09 finding: a real deal
sat 23 days without a real contact while record edits kept bumping updated_at,
so detect_stale_deals(30) reported an empty list and no alert ever fired.
Idle must be measured on last_touch_date (a real contact) when present, and
'stalled' deals — where $208K sat invisible — must be included in the sweep.
auto_stale_decay shares the same clock: an edited-but-untouched deal must
still decay, and a recently-touched deal must never be auto-stalled.

Isolation: same pattern as test_crm_growth.py — dashboard.db is pointed at a
COPY of ~/.hermes/kanban.db before any dashboard import; skips without one.

Run:  python -m pytest tests/test_stale_deals.py -v
"""
import datetime
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_TMP_DB = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_stale_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB
        from dashboard import crm as _crm
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _iso_days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _epoch_days_ago(days: int) -> int:
    return int(time.time()) - days * 86400


def _mk_deal(conn, *, title: str, stage: str, last_touch_date, updated_at: int,
             account_id: str, value: float = 1000.0) -> str:
    deal_id = f"deal_test_{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO deals (id, account_id, title, stage, value, currency, "
        "last_touch_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (deal_id, account_id, title, stage, value, "MXN",
         last_touch_date, updated_at, updated_at))
    return deal_id


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class StaleDetection(unittest.TestCase):
    """detect_stale_deals measures idleness on the touch clock."""

    @classmethod
    def setUpClass(cls):
        conn = _db.get_conn()
        cls.account_id = f"acc_test_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
            (cls.account_id, "Cuenta de prueba stale", _epoch_days_ago(100)))
        # The original shape: touched 23d ago, record edited today.
        cls.d_edited_not_touched = _mk_deal(
            conn, title="touched 23d ago, edited today", stage="proposal",
            last_touch_date=_iso_days_ago(23), updated_at=_epoch_days_ago(0),
            account_id=cls.account_id)
        # The inverse: touched 2d ago, record untouched for 20d.
        cls.d_touched_not_edited = _mk_deal(
            conn, title="touched 2d ago, edited 20d ago", stage="proposal",
            last_touch_date=_iso_days_ago(2), updated_at=_epoch_days_ago(20),
            account_id=cls.account_id)
        # Stalled bucket must be visible.
        cls.d_stalled = _mk_deal(
            conn, title="stalled, touched 10d ago", stage="stalled",
            last_touch_date=_iso_days_ago(10), updated_at=_epoch_days_ago(10),
            account_id=cls.account_id)
        # Sales-closed stages stay excluded.
        cls.d_won = _mk_deal(
            conn, title="won long ago", stage="won",
            last_touch_date=_iso_days_ago(60), updated_at=_epoch_days_ago(60),
            account_id=cls.account_id)
        # Never touched: falls back to the edit clock.
        cls.d_never_touched = _mk_deal(
            conn, title="never touched, edited 10d ago", stage="lead",
            last_touch_date=None, updated_at=_epoch_days_ago(10),
            account_id=cls.account_id)
        conn.commit()
        conn.close()

    def _stale_ids(self, **kw):
        return {d["id"] for d in _crm.detect_stale_deals(**kw)}

    def test_edited_today_but_untouched_23d_is_stale(self):
        # THE regression: an edit must not refresh a cold deal.
        self.assertIn(self.d_edited_not_touched, self._stale_ids(days_idle=7))

    def test_touched_recently_is_not_stale_even_if_unedited(self):
        self.assertNotIn(self.d_touched_not_edited, self._stale_ids(days_idle=7))

    def test_stalled_deals_are_included_and_flagged(self):
        rows = _crm.detect_stale_deals(days_idle=7, include_stalled=True)
        by_id = {d["id"]: d for d in rows}
        self.assertIn(self.d_stalled, by_id)
        self.assertEqual(by_id[self.d_stalled]["stage"], "stalled")

    def test_include_stalled_false_preserves_old_exclusion(self):
        self.assertNotIn(
            self.d_stalled, self._stale_ids(days_idle=7, include_stalled=False))

    def test_won_is_never_stale(self):
        self.assertNotIn(self.d_won, self._stale_ids(days_idle=7))

    def test_never_touched_falls_back_to_updated_at(self):
        rows = _crm.detect_stale_deals(days_idle=7)
        by_id = {d["id"]: d for d in rows}
        self.assertIn(self.d_never_touched, by_id)
        self.assertEqual(by_id[self.d_never_touched]["basis"], "updated_at")

    def test_days_param_is_still_honored(self):
        # 23 days idle is not stale at a 30-day threshold.
        self.assertNotIn(self.d_edited_not_touched, self._stale_ids(days_idle=30))

    def test_rows_carry_days_idle_and_basis(self):
        rows = _crm.detect_stale_deals(days_idle=7)
        by_id = {d["id"]: d for d in rows}
        row = by_id[self.d_edited_not_touched]
        self.assertEqual(row["basis"], "last_touch_date")
        self.assertGreaterEqual(row["days_idle"], 22)


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class DecayClock(unittest.TestCase):
    """auto_stale_decay shares the touch clock (approved 2026-08-09)."""

    @classmethod
    def setUpClass(cls):
        conn = _db.get_conn()
        cls.account_id = f"acc_test_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
            (cls.account_id, "Cuenta de prueba decay", _epoch_days_ago(200)))
        # Touched 5d ago but record idle 40d: must NOT be auto-stalled.
        cls.d_recently_touched = _mk_deal(
            conn, title="touched 5d ago, edited 40d ago", stage="proposal",
            last_touch_date=_iso_days_ago(5), updated_at=_epoch_days_ago(40),
            account_id=cls.account_id)
        # Touched 35d ago but edited today: MUST be auto-stalled.
        cls.d_cold_but_edited = _mk_deal(
            conn, title="touched 35d ago, edited today", stage="proposal",
            last_touch_date=_iso_days_ago(35), updated_at=_epoch_days_ago(0),
            account_id=cls.account_id)
        conn.commit()
        conn.close()
        _crm.auto_stale_decay()

    def _stage(self, deal_id):
        conn = _db.get_conn()
        try:
            return conn.execute(
                "SELECT stage FROM deals WHERE id = ?", (deal_id,)).fetchone()["stage"]
        finally:
            conn.close()

    def test_recently_touched_deal_survives_decay(self):
        self.assertEqual(self._stage(self.d_recently_touched), "proposal")

    def test_cold_but_edited_deal_decays_to_stalled(self):
        self.assertEqual(self._stage(self.d_cold_but_edited), "stalled")


if __name__ == "__main__":
    unittest.main()
