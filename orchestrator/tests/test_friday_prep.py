"""friday_prep.compose() — the Thursday pre-block brief contract.

Authored by the orchestrator BEFORE the implementation was dispatched (Tier-0):
this file is the authoritative spec for dashboard/friday_prep.py.

compose(today=None) returns one dict, PURE READ (no DB writes), with keys:
  gates                — exactly growth.scorecard()'s kpis, passed through
                         (list; each row keeps key/label/value/target/status).
  tablero              — {"pipeline_value": <sum of open lead+discovery+proposal
                         deal values>}. (Velocity lives in its own verb.)
  stale                — exactly crm.detect_stale_deals(7, include_stalled=True).
  pending_proposals    — exactly crm_proposals.list_proposals("proposed").
  generosity_candidates — AT MOST 3: open-stage deals (lead/discovery/proposal/
                         stalled) ranked by days-without-touch DESC (touch clock,
                         never-touched first/oldest). Each: {"deal_id",
                         "deal_title", "days_idle", "reason", "draft"} where
                         draft is a deterministic Spanish template that names
                         the deal title. Honest empty list when no open deals.
  referral_candidate   — ONE dict or None: the most recently WON deal within 14
                         days (momento alto), {"deal_id", "deal_title",
                         "reason", "draft"}; None when there is none — never
                         padded from open deals.

Isolation: conftest's session sandbox; fixtures under one throwaway account.

Run:  python -m pytest tests/test_friday_prep.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_fprep_", suffix=".db")
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
class FridayBrief(unittest.TestCase):

    @classmethod
    def _clear_open_deals(cls, conn):
        """Park every pre-existing open/stalled deal as lost so the sandbox's
        real CRM content can't leak into candidate ranking assertions.

        Uses crm.OPEN_STAGES — this helper originally carried the same
        hand-rolled tuple (with the phantom 'discovery' and no engaged/
        qualified/demo) that the modules did, so mid-journey deals survived
        the parking and broke the emptiness assertions under a shared sandbox."""
        from dashboard import crm
        marks = ",".join("?" * len(crm.OPEN_STAGES))
        conn.execute(
            f"UPDATE deals SET stage = 'lost' WHERE stage IN ({marks})",
            crm.OPEN_STAGES)
        conn.execute("UPDATE deals SET closed_at = COALESCE(closed_at, 0) "
                     "WHERE stage = 'won'")
        # Push historic wins out of the 14d momento-alto window:
        conn.execute("UPDATE deals SET updated_at = ?, closed_at = ? "
                     "WHERE stage = 'won'", (_epoch(60), _epoch(60)))

    def setUp(self):
        from dashboard.migrations.m27_crm_proposals import m27_crm_proposals
        conn = _db.get_conn()
        m27_crm_proposals(conn)
        self._clear_open_deals(conn)
        self.account_id = f"acc_fprep_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (self.account_id, "Cuenta brief", _epoch(90)))

        def mk(title, stage, last_touch, updated_days=30, closed_days=None):
            did = f"deal_fprep_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "last_touch_date, touch_count, created_at, updated_at, closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, self.account_id, title, stage, 10000.0, "MXN", last_touch,
                 1, _epoch(90), _epoch(updated_days),
                 _epoch(closed_days) if closed_days is not None else None))
            return did

        # Open deals with distinct idleness → deterministic ranking. The edit
        # clock (updated_at) is deliberately ANTI-correlated with the touch
        # clock so a ranking accidentally computed from edits inverts and
        # fails loudly (the touch clock is the contract):
        self.d_coldest = mk("El más frío", "proposal", _days_ago(30), updated_days=1)
        self.d_cold = mk("Frío medio", "stalled", _days_ago(15), updated_days=5)
        self.d_cool = mk("Tibio", "qualified", _days_ago(9), updated_days=40)
        self.d_fresh = mk("Recién tocado", "lead", _days_ago(1), updated_days=45)
        # Momento alto: won 3 days ago.
        self.d_won_recent = mk("Ganado esta semana", "won", _days_ago(3),
                               updated_days=3, closed_days=3)
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = _db.get_conn()
        conn.execute("DELETE FROM crm_proposals WHERE deal_id IN "
                     "(SELECT id FROM deals WHERE account_id = ?)", (self.account_id,))
        conn.execute("DELETE FROM deals WHERE account_id = ?", (self.account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (self.account_id,))
        conn.commit()
        conn.close()

    def _compose(self):
        from dashboard import friday_prep
        return friday_prep.compose()

    def test_compose_is_a_pure_read(self):
        conn = _db.get_conn()
        before = conn.execute("PRAGMA data_version").fetchone()[0]
        counts_before = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        self._compose()
        after = conn.execute("PRAGMA data_version").fetchone()[0]
        counts_after = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        conn.close()
        self.assertEqual(before, after, "compose() must not write the DB")
        self.assertEqual(counts_before, counts_after)

    def test_gates_mirror_scorecard(self):
        from dashboard import growth
        brief = self._compose()
        expected = growth.scorecard()["kpis"]
        self.assertEqual(
            [(g["key"], g["value"], g["target"]) for g in brief["gates"]],
            [(g["key"], g["value"], g["target"]) for g in expected])

    def test_stale_block_equals_detector(self):
        from dashboard import crm
        brief = self._compose()
        self.assertEqual(
            {d["id"] for d in brief["stale"]},
            {d["id"] for d in crm.detect_stale_deals(7, include_stalled=True)})

    def test_pending_proposals_are_passed_through(self):
        from dashboard import crm_proposals
        crm_proposals.create(self.d_coldest, "touch", {}, "manual", "brief:e1")
        brief = self._compose()
        ids = {p["id"] for p in brief["pending_proposals"]}
        self.assertEqual(ids, {p["id"] for p in
                               crm_proposals.list_proposals("proposed")})

    def test_generosity_candidates_capped_ranked_and_drafted(self):
        brief = self._compose()
        cands = brief["generosity_candidates"]
        self.assertLessEqual(len(cands), 3)
        self.assertEqual([c["deal_id"] for c in cands],
                         [self.d_coldest, self.d_cold, self.d_cool],
                         "ranked by days-without-touch DESC, capped at 3")
        for c in cands:
            self.assertGreaterEqual(c["days_idle"], 8)
            self.assertIn(c["deal_title"], c["draft"],
                          "draft must name the deal")

    def test_never_touched_deal_is_the_coldest_candidate(self):
        conn = _db.get_conn()
        did = f"deal_fprep_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "last_touch_date, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, self.account_id, "Nunca tocado", "lead", 10000.0, "MXN",
             None, 0, _epoch(90), _epoch(45)))
        conn.commit()
        conn.close()
        cands = self._compose()["generosity_candidates"]
        self.assertEqual(cands[0]["deal_id"], did,
                         "never-touched ranks by edit clock: 45d > 30d")
        self.assertEqual(cands[0]["days_idle"], 45)

    def test_referral_candidate_is_the_recent_win(self):
        brief = self._compose()
        ref = brief["referral_candidate"]
        self.assertIsNotNone(ref)
        self.assertEqual(ref["deal_id"], self.d_won_recent)
        self.assertIn(ref["deal_title"], ref["draft"])

    def test_win_older_than_14_days_is_not_a_momento_alto(self):
        conn = _db.get_conn()
        conn.execute("UPDATE deals SET closed_at = ?, updated_at = ? WHERE id = ?",
                     (_epoch(15), _epoch(15), self.d_won_recent))
        conn.commit()
        conn.close()
        self.assertIsNone(self._compose()["referral_candidate"])

    def test_zero_idle_deals_report_integer_zero(self):
        conn = _db.get_conn()
        # Zero-idle deals are the FRESHEST, so they only reach the top-3 when
        # no colder deal competes — park the shared sandbox first.
        self._clear_open_deals(conn)
        touched_today = f"deal_fprep_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "last_touch_date, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (touched_today, self.account_id, "Tocado hoy", "lead", 1.0, "MXN",
             _days_ago(0), 1, _epoch(0), _epoch(0)))
        null_updated = f"deal_fprep_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "last_touch_date, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (null_updated, self.account_id, "Sin relojes", "lead", 1.0, "MXN",
             None, 0, _epoch(0), None))
        edited_now = f"deal_fprep_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "last_touch_date, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (edited_now, self.account_id, "Editado hoy sin toque", "lead", 1.0,
             "MXN", None, 0, _epoch(0), _epoch(0)))
        self._edited_now = edited_now
        # Clear the older fixtures so BOTH zero-idle deals make the top 3:
        conn.execute("UPDATE deals SET stage = 'lost' WHERE id IN (?,?,?,?)",
                     (self.d_coldest, self.d_cold, self.d_cool, self.d_fresh))
        conn.commit()
        conn.close()
        cands = self._compose()["generosity_candidates"]
        by_id = {c["deal_id"]: c for c in cands}
        self.assertEqual(by_id[touched_today]["days_idle"], 0)
        self.assertIn(0, (by_id.get(null_updated, {}).get("days_idle"),
                          by_id.get(self._edited_now, {}).get("days_idle")))
        edited = by_id.get(self._edited_now)
        if edited is not None:
            self.assertEqual(edited["days_idle"], 0)
            self.assertIsInstance(edited["days_idle"], int)
        for c in cands:
            self.assertIsInstance(c["days_idle"], int)

    def test_render_md_is_bounded_linked_and_pure(self):
        from dashboard import friday_prep, db as dbmod
        conn = _db.get_conn()
        before = conn.execute("PRAGMA data_version").fetchone()[0]
        conn.close()
        text = friday_prep.render_md()
        conn = _db.get_conn()
        after = conn.execute("PRAGMA data_version").fetchone()[0]
        conn.close()
        self.assertEqual(before, after, "render_md must not write")
        self.assertIsInstance(text, str)
        lines = text.split("\n")
        self.assertLessEqual(len(lines), 25, "Telegram budget: 25 lines max")
        self.assertIn(dbmod.dashboard_url(), text,
                      "deep links must use the canonical URL, never loopback")
        self.assertNotIn("127.0.0.1", text)
        # The coldest fixture must be named (the nudge earns its send).
        self.assertIn("El más frío", text)

    def test_stale_threshold_boundary_is_seven_days(self):
        conn = _db.get_conn()
        eight = f"deal_fprep_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "last_touch_date, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eight, self.account_id, "Ocho días", "proposal", 1.0, "MXN",
             _days_ago(8), 1, _epoch(8), _epoch(8)))
        conn.commit()
        conn.close()
        stale_ids = {d["id"] for d in self._compose()["stale"]}
        self.assertIn(eight, stale_ids,
                      "an 8-day-idle deal is inside the 7-day sweep")

    def test_honest_empties_when_nothing_qualifies(self):
        # conftest re-points every module at ONE shared session sandbox after
        # collection, so "nothing qualifies" has to be made true globally — an
        # assertion of emptiness that only parked its own account would be
        # pinning the run order, not the behavior.
        conn = _db.get_conn()
        self._clear_open_deals(conn)
        conn.execute("UPDATE deals SET stage = 'lost' WHERE account_id = ?",
                     (self.account_id,))
        conn.commit()
        conn.close()
        brief = self._compose()
        self.assertEqual(brief["generosity_candidates"], [])
        self.assertIsNone(brief["referral_candidate"])

    def test_tablero_pipeline_value_sums_open_deals(self):
        # Assert the ARITHMETIC against the DB, not a magic total: the sandbox
        # is shared, so a hardcoded number would test the run order instead of
        # the contract ("sum the value of deals in active pipeline stages").
        from dashboard import crm
        conn = _db.get_conn()
        try:
            marks = ",".join("?" * len(crm.ACTIVE_PIPELINE_STAGES))
            expected = conn.execute(
                f"SELECT COALESCE(SUM(value), 0) FROM deals WHERE stage IN ({marks})",
                crm.ACTIVE_PIPELINE_STAGES).fetchone()[0]
            mine = conn.execute(
                f"SELECT COALESCE(SUM(value), 0) FROM deals WHERE account_id = ? "
                f"AND stage IN ({marks})",
                (self.account_id, *crm.ACTIVE_PIPELINE_STAGES)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(self._compose()["tablero"]["pipeline_value"], expected)
        self.assertEqual(mine, 30000.0, "this fixture contributes 3 open deals")

    def test_mid_journey_stages_are_on_the_radar(self):
        # Regression (2026-08-10): 'discovery' never existed in crm.STAGES;
        # engaged/qualified/demo deals must count as candidates AND pipeline.
        conn = _db.get_conn()
        base = self._compose()["tablero"]["pipeline_value"]
        for stage in ("engaged", "demo"):
            did = f"deal_fprep_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "last_touch_date, touch_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, self.account_id, f"viaje {stage}", stage, 5000.0, "MXN",
                 _days_ago(50), 1, _epoch(50), _epoch(50)))
        conn.commit()
        conn.close()
        brief = self._compose()
        self.assertEqual(brief["tablero"]["pipeline_value"], base + 10000.0)
        titles = [c["deal_title"] for c in brief["generosity_candidates"]]
        self.assertIn("viaje engaged", titles)
        self.assertIn("viaje demo", titles)


if __name__ == "__main__":
    unittest.main()
