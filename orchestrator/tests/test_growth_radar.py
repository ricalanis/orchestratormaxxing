"""growth_radar.compose() — the Motor Caliente radar contract.

Authored by the orchestrator BEFORE the implementation was dispatched (Tier-0):
this file is the authoritative spec for dashboard/growth_radar.py.

The radar is the commercial JOURNEY made visible (design decision 2026-08-10,
memory: radar-journey-not-sources): rings derive from deal stages, radius from
the touch clock, and nothing moves inward except a real touch. Signal sources
(WhatsApp/Fireflies) never define rings.

compose() -> dict, PURE READ, shape:
  rings: {
    "seguimiento":  deals with stage IN ('lead','engaged'),
    "oportunidad":  deals with stage IN ('qualified','demo'),
    "propuesta":    deals with stage = 'proposal',
  }
  centro:           deals won AND project_id NOT NULL AND project.status='active'
                    (items additionally carry project_id + project_status)
  orbita_fria:      deals with stage = 'stalled'
  won_sin_proyecto: won deals with project_id IS NULL (ganado, proyecto por
                    nacer — must be visible, never lost)
  meta:             {"warm_days": 7, "counts": {ring: n, ...}}

Excluded everywhere: stage='lost', and won deals whose project is delivered/
archived (they left the radar into "casos").

Each deal item: {deal_id, title, account_name, value, currency, days_idle
(int), basis ('last_touch_date'|'updated_at'), warmth, growth_loop}.
Warmth from the touch clock: days_idle < 4 → 'warm' · 4-6 → 'cooling' ·
>= 7 → 'stale'. Touch clock = last_touch_date when present else updated_at
(crm._touch_idle semantics).
Ordering inside every ring/list: days_idle DESC, deal_id ASC (coldest first).

Run:  python -m pytest tests/test_growth_radar.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_radar_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _epoch(days_ago: int) -> int:
    return int(time.time()) - days_ago * 86400


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class RadarContract(unittest.TestCase):

    def setUp(self):
        conn = _db.get_conn()
        # Park pre-existing open/stalled deals so sandbox content can't leak
        # into ring/count assertions (same rule as test_friday_prep).
        from dashboard import crm
        marks = ",".join("?" * len(crm.OPEN_STAGES))
        conn.execute(f"UPDATE deals SET stage = 'lost' WHERE stage IN ({marks})",
                     crm.OPEN_STAGES)
        conn.execute("UPDATE deals SET project_id = NULL, updated_at = ? "
                     "WHERE stage = 'won'", (_epoch(120),))
        conn.execute("UPDATE deals SET stage = 'lost' WHERE stage = 'won'")
        self.account_id = f"acc_radar_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (self.account_id, "Cuenta radar", _epoch(90)))

        def mk_project(status):
            pid = f"proj_radar_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO projects (id, slug, name, status, created_at) "
                "VALUES (?,?,?,?,?)",
                (pid, pid, f"Proyecto {status}", status, _epoch(30)))
            return pid

        def mk(title, stage, last_touch, project_id=None, value=10000.0,
               loop=None):
            did = f"deal_radar_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "last_touch_date, touch_count, growth_loop, project_id, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, self.account_id, title, stage, value, "MXN", last_touch,
                 1, loop, project_id, _epoch(60), _epoch(60)))
            return did

        self.p_active = mk_project("active")
        self.p_delivered = mk_project("delivered")

        # Ring fixtures — one per stage, warmth spread across boundaries:
        self.d_lead = mk("Lead frío", "lead", _days_ago(10))          # stale
        self.d_engaged = mk("Engaged tibio", "engaged", _days_ago(5)) # cooling
        self.d_qualified = mk("Qualified cálido", "qualified", _days_ago(3))  # warm
        self.d_demo = mk("Demo borde", "demo", _days_ago(4))          # cooling (boundary)
        self.d_proposal = mk("Propuesta borde", "proposal", _days_ago(7))     # stale (boundary)
        self.d_stalled = mk("Congelado", "stalled", _days_ago(20))
        self.d_won_active = mk("Ganado con proyecto", "won", _days_ago(2),
                               project_id=self.p_active)
        self.d_won_delivered = mk("Ganado entregado", "won", _days_ago(2),
                                  project_id=self.p_delivered)
        self.d_won_orphan = mk("Ganado sin proyecto", "won", _days_ago(1))
        self.d_lost = mk("Perdido", "lost", _days_ago(2))
        # Never touched: falls back to the edit clock (updated_at 60d → stale).
        self.d_never = mk("Nunca tocado", "lead", None)
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = _db.get_conn()
        conn.execute("DELETE FROM deals WHERE account_id = ?", (self.account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (self.account_id,))
        conn.execute("DELETE FROM projects WHERE slug LIKE 'proj_radar_%'")
        conn.commit()
        conn.close()

    def _radar(self):
        from dashboard import growth_radar
        return growth_radar.compose()

    def _ids(self, items):
        return [d["deal_id"] for d in items]

    # -- ring membership -----------------------------------------------------

    def test_rings_map_the_real_journey(self):
        r = self._radar()
        self.assertEqual(set(self._ids(r["rings"]["seguimiento"])),
                         {self.d_lead, self.d_engaged, self.d_never})
        self.assertEqual(set(self._ids(r["rings"]["oportunidad"])),
                         {self.d_qualified, self.d_demo})
        self.assertEqual(self._ids(r["rings"]["propuesta"]), [self.d_proposal])

    def test_centro_is_won_with_active_project_only(self):
        r = self._radar()
        self.assertEqual(self._ids(r["centro"]), [self.d_won_active])
        item = r["centro"][0]
        self.assertEqual(item["project_id"], self.p_active)
        self.assertEqual(item["project_status"], "active")

    def test_delivered_project_leaves_the_radar(self):
        r = self._radar()
        everywhere = (self._ids(r["centro"]) + self._ids(r["orbita_fria"])
                      + self._ids(r["won_sin_proyecto"])
                      + [i for ring in r["rings"].values() for i in self._ids(ring)])
        self.assertNotIn(self.d_won_delivered, everywhere)

    def test_won_without_project_is_visible_not_lost(self):
        r = self._radar()
        self.assertEqual(self._ids(r["won_sin_proyecto"]), [self.d_won_orphan])

    def test_stalled_is_the_cold_orbit_and_lost_is_gone(self):
        r = self._radar()
        self.assertEqual(self._ids(r["orbita_fria"]), [self.d_stalled])
        everywhere = (self._ids(r["centro"]) + self._ids(r["orbita_fria"])
                      + self._ids(r["won_sin_proyecto"])
                      + [i for ring in r["rings"].values() for i in self._ids(ring)])
        self.assertNotIn(self.d_lost, everywhere)

    # -- warmth (the touch clock) --------------------------------------------

    def test_warmth_boundaries_3_4_6_7(self):
        r = self._radar()
        by_id = {d["deal_id"]: d for ring in r["rings"].values() for d in ring}
        self.assertEqual(by_id[self.d_qualified]["warmth"], "warm")    # 3d
        self.assertEqual(by_id[self.d_demo]["warmth"], "cooling")      # 4d
        self.assertEqual(by_id[self.d_engaged]["warmth"], "cooling")   # 5d
        self.assertEqual(by_id[self.d_proposal]["warmth"], "stale")    # 7d

    def test_never_touched_uses_edit_clock_and_reports_basis(self):
        r = self._radar()
        by_id = {d["deal_id"]: d for d in r["rings"]["seguimiento"]}
        never = by_id[self.d_never]
        self.assertEqual(never["basis"], "updated_at")
        self.assertEqual(never["days_idle"], 60)
        self.assertEqual(never["warmth"], "stale")
        touched = by_id[self.d_lead]
        self.assertEqual(touched["basis"], "last_touch_date")

    # -- ordering, shape, meta ----------------------------------------------

    def test_coldest_first_inside_a_ring(self):
        r = self._radar()
        idles = [d["days_idle"] for d in r["rings"]["seguimiento"]]
        self.assertEqual(idles, sorted(idles, reverse=True))

    def test_item_shape_is_complete(self):
        r = self._radar()
        item = r["rings"]["seguimiento"][0]
        for key in ("deal_id", "title", "account_name", "value", "currency",
                    "days_idle", "basis", "warmth", "growth_loop"):
            self.assertIn(key, item)
        self.assertIsInstance(item["days_idle"], int)

    def test_meta_counts_match(self):
        r = self._radar()
        self.assertEqual(r["meta"]["warm_days"], 7)
        self.assertEqual(r["meta"]["counts"]["seguimiento"], 3)
        self.assertEqual(r["meta"]["counts"]["oportunidad"], 2)
        self.assertEqual(r["meta"]["counts"]["propuesta"], 1)
        self.assertEqual(r["meta"]["counts"]["centro"], 1)
        self.assertEqual(r["meta"]["counts"]["orbita_fria"], 1)

    def test_compose_is_a_pure_read(self):
        conn = _db.get_conn()
        before = conn.execute("PRAGMA data_version").fetchone()[0]
        rows_before = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        conn.close()
        self._radar()
        conn = _db.get_conn()
        after = conn.execute("PRAGMA data_version").fetchone()[0]
        rows_after = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        conn.close()
        self.assertEqual(before, after, "compose() must not write the DB")
        self.assertEqual(rows_before, rows_after)

    def test_empty_radar_is_honest_not_an_error(self):
        conn = _db.get_conn()
        conn.execute("UPDATE deals SET stage = 'lost' WHERE account_id = ?",
                     (self.account_id,))
        conn.commit()
        conn.close()
        r = self._radar()
        for ring in r["rings"].values():
            self.assertEqual(ring, [])
        self.assertEqual(r["centro"], [])
        self.assertEqual(r["orbita_fria"], [])


if __name__ == "__main__":
    unittest.main()
