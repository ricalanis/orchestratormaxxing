"""weekly_reflection — the Friday 5-questions log contract (m28).

Authored by the orchestrator BEFORE the implementation was dispatched (Tier-0):
this file is the authoritative spec for dashboard/weekly_reflection.py and
dashboard/migrations/m28_weekly_reflections.py.

API (mirrors reflection.py's conventions: conn-per-call, typed error dicts):
  save_week(answers: dict, week: str = None) -> dict
      answers uses the five keys q_regale, q_declare, q_referido, q_propuesta,
      q_aprendi (all optional strings; unknown keys are a typed error).
      week defaults to the CURRENT ISO week 'YYYY-Www'; malformed week strings
      are a typed error {"status": "error", ...}. Same-week saves UPSERT (one
      row per week, updated_at bumps, provided keys overwrite, omitted keys
      survive).
  get_week(week: str = None) -> dict | None   (None when the week has no row)
  history(n: int = 8) -> list  — newest week first, at most n rows, each row
      carrying week + the five answers + "answered" (count of non-empty).

Migration m28 creates weekly_reflections(week TEXT UNIQUE, q_regale, q_declare,
q_referido, q_propuesta, q_aprendi, created_at, updated_at) — additive, no
commit inside (the runner owns the transaction).

Run:  python -m pytest tests/test_weekly_reflection.py -v
"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_TMP_DB = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_wref_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _week_of(days_ago: int) -> str:
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class WeeklyReflection(unittest.TestCase):

    def setUp(self):
        from dashboard.migrations.m28_weekly_reflections import (
            m28_weekly_reflections)
        conn = _db.get_conn()
        m28_weekly_reflections(conn)
        conn.execute("DELETE FROM weekly_reflections")
        conn.commit()
        conn.close()

    def _wr(self):
        from dashboard import weekly_reflection
        return weekly_reflection

    def test_save_and_get_roundtrip_defaults_to_current_week(self):
        wr = self._wr()
        res = wr.save_week({"q_regale": "dictamen Acme",
                            "q_declare": "caso Yazaki avanzado"})
        self.assertEqual(res["status"], "ok")
        row = wr.get_week()
        self.assertEqual(row["q_regale"], "dictamen Acme")
        self.assertEqual(row["q_declare"], "caso Yazaki avanzado")
        self.assertEqual(row["week"], _week_of(0))

    def test_same_week_save_upserts_one_row(self):
        wr = self._wr()
        wr.save_week({"q_regale": "v1"})
        first = wr.get_week()
        wr.save_week({"q_regale": "v2", "q_aprendi": "Mom Test aplicado"})
        second = wr.get_week()
        conn = _db.get_conn()
        count = conn.execute("SELECT COUNT(*) FROM weekly_reflections "
                             "WHERE week = ?", (_week_of(0),)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "one row per week — upsert, not append")
        self.assertEqual(second["q_regale"], "v2")
        self.assertEqual(second["q_aprendi"], "Mom Test aplicado")
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])

    def test_partial_update_preserves_other_answers(self):
        wr = self._wr()
        wr.save_week({"q_referido": "Aza — 2 nombres"})
        wr.save_week({"q_propuesta": "Acme avanzada"})
        row = wr.get_week()
        self.assertEqual(row["q_referido"], "Aza — 2 nombres")
        self.assertEqual(row["q_propuesta"], "Acme avanzada")

    def test_get_week_without_row_is_none(self):
        self.assertIsNone(self._wr().get_week(_week_of(70)))

    def test_history_newest_first_capped_with_answered_counts(self):
        wr = self._wr()
        wr.save_week({"q_regale": "a"}, week=_week_of(14))
        wr.save_week({"q_regale": "b", "q_declare": "c"}, week=_week_of(7))
        wr.save_week({"q_regale": "d", "q_declare": "e", "q_aprendi": "f"})
        hist = wr.history(2)
        self.assertEqual([h["week"] for h in hist],
                         [_week_of(0), _week_of(7)])
        self.assertEqual(hist[0]["answered"], 3)
        self.assertEqual(hist[1]["answered"], 2)

    def test_malformed_week_and_unknown_keys_are_typed_errors(self):
        wr = self._wr()
        self.assertEqual(wr.save_week({"q_regale": "x"}, week="viernes")["status"],
                         "error")
        self.assertEqual(wr.save_week({"q_bogus": "x"})["status"], "error")
        self.assertEqual(wr.save_week({})["status"], "error")

    def test_migration_is_idempotent(self):
        from dashboard.migrations.m28_weekly_reflections import (
            m28_weekly_reflections)
        conn = _db.get_conn()
        m28_weekly_reflections(conn)
        m28_weekly_reflections(conn)
        conn.commit()
        conn.close()
        self.assertEqual(self._wr().save_week({"q_regale": "ok"})["status"], "ok")


if __name__ == "__main__":
    unittest.main()
