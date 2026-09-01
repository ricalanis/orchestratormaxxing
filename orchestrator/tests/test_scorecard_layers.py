"""Scorecard layers — the motor-caliente split contract (2026-08-09).

Pins three things:
  1. K7 COMPATIBILITY — the legacy `kpis` list keeps exactly 5 entries, each
     with the full field set bin/kpi-brief and bin/hermes-watch read
     (key/label/value/icon/target/wow_delta/wow_pct/prev_value). `layers` is
     strictly additive.
  2. THE IDENTITY — fleet.touches_raw + ricardo.toques_calientes +
     ricardo.referral_asks == the legacy Toques KPI: a warm touch is never
     lost from K7, only attributed to the human layer.
  3. MRR — read from won deals with recurrence_type='monthly' (the WePort
     retainer was invisible while the tablero reported MRR $0).

Run:  python -m pytest tests/test_scorecard_layers.py -v
"""
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_layers_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB
        _READY = True
except Exception:  # pragma: no cover
    _READY = False

_KPI_FIELDS = {"key", "label", "value", "icon", "target",
               "wow_delta", "wow_pct", "prev_value"}


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class ScorecardLayers(unittest.TestCase):

    def setUp(self):
        conn = _db.get_conn()
        self.account_id = f"acc_layers_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (self.account_id, "Cuenta capas", int(time.time())))
        self.deal_id = f"deal_layers_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "touch_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.deal_id, self.account_id, "deal capas", "proposal", 1000.0,
             "MXN", 0, int(time.time()), int(time.time())))
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = _db.get_conn()
        conn.execute("DELETE FROM deal_events WHERE deal_id IN "
                     "(SELECT id FROM deals WHERE account_id = ?)", (self.account_id,))
        conn.execute("DELETE FROM deals WHERE account_id = ?", (self.account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (self.account_id,))
        conn.commit()
        conn.close()

    def _score(self):
        from dashboard import growth
        return growth.scorecard()

    def test_k7_compat_five_full_kpis_and_additive_layers(self):
        sc = self._score()
        self.assertEqual(len(sc["kpis"]), 5)
        self.assertEqual([k["key"] for k in sc["kpis"]],
                         ["leads", "touches", "discovery", "content", "proposals"])
        for k in sc["kpis"]:
            self.assertEqual(set(k.keys()), _KPI_FIELDS,
                             "legacy KPI schema must stay byte-shape stable")
        self.assertIn("layers", sc)
        self.assertIn("total_activity", sc)

    def test_warm_kinds_count_as_touches_and_land_in_ricardos_layer(self):
        from dashboard import growth
        before = self._score()
        b_touches = next(k for k in before["kpis"] if k["key"] == "touches")["value"]
        b_warm = before["layers"]["ricardo"]["toques_calientes"]
        b_ask = before["layers"]["ricardo"]["referral_asks"]
        growth.record_touch(self.deal_id, note="toque generoso", kind="warm_touch")
        growth.record_touch(self.deal_id, note="pedido de referido",
                            kind="referral_ask")
        growth.record_touch(self.deal_id, note="toque normal", kind="touch")
        after = self._score()
        a_touches = next(k for k in after["kpis"] if k["key"] == "touches")["value"]
        self.assertEqual(a_touches, b_touches + 3,
                         "all three kinds count for the legacy Toques KPI")
        self.assertEqual(after["layers"]["ricardo"]["toques_calientes"], b_warm + 1)
        self.assertEqual(after["layers"]["ricardo"]["referral_asks"], b_ask + 1)

    def test_the_identity_holds(self):
        from dashboard import growth
        growth.record_touch(self.deal_id, kind="warm_touch")
        growth.record_touch(self.deal_id, kind="touch")
        sc = self._score()
        touches_kpi = next(k for k in sc["kpis"] if k["key"] == "touches")["value"]
        self.assertEqual(
            sc["layers"]["fleet"]["touches_raw"]
            + sc["layers"]["ricardo"]["toques_calientes"]
            + sc["layers"]["ricardo"]["referral_asks"],
            touches_kpi)

    def test_mrr_reads_won_monthly_deals(self):
        conn = _db.get_conn()
        expected = conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM deals WHERE stage = 'won' "
            "AND recurrence_type = 'monthly'").fetchone()[0]
        conn.close()
        sc = self._score()
        self.assertEqual(sc["layers"]["mrr"], expected)
        # The retainer regression: a new won monthly deal moves MRR.
        conn = _db.get_conn()
        did = f"deal_layers_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "recurrence_type, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, self.account_id, "retainer fixture", "won", 20000.0, "MXN",
             "monthly", 0, int(time.time()), int(time.time())))
        conn.commit()
        conn.close()
        self.assertEqual(self._score()["layers"]["mrr"], expected + 20000.0)

    def test_sync_block_reports_backlog_not_just_success(self):
        sc = self._score()
        sync = sc["layers"]["fleet"]["sync"]
        for key in ("digested", "failed", "pending", "fidelity"):
            self.assertIn(key, sync)

    def test_unknown_kind_still_folds_to_plain_touch(self):
        from dashboard import growth
        before = self._score()
        b_touches = next(k for k in before["kpis"] if k["key"] == "touches")["value"]
        b_warm = before["layers"]["ricardo"]["toques_calientes"]
        growth.record_touch(self.deal_id, kind="telepatia")
        after = self._score()
        a_touches = next(k for k in after["kpis"] if k["key"] == "touches")["value"]
        self.assertEqual(a_touches, b_touches + 1)
        self.assertEqual(after["layers"]["ricardo"]["toques_calientes"], b_warm)


if __name__ == "__main__":
    unittest.main()
