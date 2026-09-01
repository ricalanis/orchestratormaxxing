"""Readiness scoring model — regression guard.

Pins the multi-dimensional readiness layer (dashboard/readiness.py) added on
top of lead_score:

  1. Pure scoring — bucket boundaries, decay steps, the three dimension
     scorers (buyer/product/market) and the weighted+decayed composite pin
     EXACT numbers (doctrine: pure functions so the contract is deterministic).
  2. Fireflies extraction — Spanish-first markers (presupuesto, urgente,
     dueño, arquitectura…) parsed out of stored meeting rows.
  3. Persistence — readiness_score + readiness_dimensions (JSON) written on
     the deal, readiness_scored event logged, updated_at NOT bumped (a derived
     score write must not reset the decay clock).
  4. API — GET /api/readiness (bucketed overview) and
     POST /api/crm/deals/{id}/readiness (per-deal recompute), 404 on unknown.
  5. MCP parity — the three new tools registered AND handler-wired.

Isolation: same pattern as test_crm_growth.py — the DB layer is pointed at a
COPY of ~/.hermes/kanban.db BEFORE dashboard.api is imported; skip if absent.

Run:  python -m pytest tests/test_readiness.py -v
      python -m unittest tests.test_readiness
"""
import atexit
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import readiness as rd  # pure functions — no import side effects

_READY = False
_CLIENT = None
_TMP_DB = None
try:
    from dashboard import db as _db, crm as _crm

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_readiness_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB

        from dashboard import growth as _growth
        from dashboard.api import app            # ensure_schema() runs on the copy
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


@atexit.register
def _cleanup_tmp_db():  # pragma: no cover
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


def _today(days: int = 0) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


# ============================================================ pure: buckets
class Buckets(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(rd.bucket_for(0), "nurture")
        self.assertEqual(rd.bucket_for(30), "nurture")
        self.assertEqual(rd.bucket_for(31), "qualified")
        self.assertEqual(rd.bucket_for(60), "qualified")
        self.assertEqual(rd.bucket_for(61), "sales_ready")
        self.assertEqual(rd.bucket_for(85), "sales_ready")
        self.assertEqual(rd.bucket_for(86), "hot")
        self.assertEqual(rd.bucket_for(100), "hot")

    def test_clamps_and_garbage(self):
        self.assertEqual(rd.bucket_for(-5), "nurture")
        self.assertEqual(rd.bucket_for(140), "hot")
        self.assertEqual(rd.bucket_for(None), "nurture")
        self.assertEqual(rd.bucket_for("x"), "nurture")


# ============================================================ pure: decay
class Decay(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(rd.decay_factor(0), 1.0)
        self.assertEqual(rd.decay_factor(7), 1.0)
        self.assertEqual(rd.decay_factor(8), 0.9)
        self.assertEqual(rd.decay_factor(14), 0.9)
        self.assertEqual(rd.decay_factor(30), 0.75)
        self.assertEqual(rd.decay_factor(60), 0.55)
        self.assertEqual(rd.decay_factor(61), 0.4)
        self.assertEqual(rd.decay_factor(365), 0.4)

    def test_never_touched_is_cold(self):
        self.assertEqual(rd.decay_factor(None), 0.4)


# ============================================================ pure: buyer
class BuyerReadiness(unittest.TestCase):
    def test_full_bant_pins_exact(self):
        """proposal + CEO + $85k + close in 14d = 30+20+25+18 = 93."""
        deal = {"stage": "proposal", "value": 85000,
                "expected_close_date": _today(14)}
        r = rd._buyer_readiness(deal, [], None, contact_role="CEO")
        self.assertEqual(r["sub"], {"intent": 30, "authority": 20,
                                    "budget": 25, "urgency": 18})
        self.assertEqual(r["value"], 93)

    def test_bare_lead_is_low(self):
        r = rd._buyer_readiness({"stage": "lead"}, [], None)
        self.assertEqual(r["value"], 5)   # intent only

    def test_fireflies_budget_and_urgency_boost(self):
        deal = {"stage": "qualified"}
        ff = {"budget_mentions": 2, "urgency_mentions": 1}
        r = rd._buyer_readiness(deal, [], ff)
        self.assertEqual(r["sub"]["budget"], 8)    # no value; 2 mentions → +8
        self.assertEqual(r["sub"]["urgency"], 4)   # no date; 1 mention → +4

    def test_decision_maker_in_meeting_beats_no_role(self):
        with_dm = rd._buyer_readiness({"stage": "lead"}, [],
                                      {"decision_maker_present": True})
        without = rd._buyer_readiness({"stage": "lead"}, [], {})
        self.assertEqual(with_dm["sub"]["authority"], 15)
        self.assertEqual(without["sub"]["authority"], 0)

    def test_spanish_decision_role(self):
        r = rd._buyer_readiness({"stage": "lead"}, [], None,
                                contact_role="Dueño y fundador")
        self.assertEqual(r["sub"]["authority"], 20)

    def test_non_dm_role_partial(self):
        r = rd._buyer_readiness({"stage": "lead"}, [], None,
                                contact_role="Analista de datos")
        self.assertEqual(r["sub"]["authority"], 8)


# ============================================================ pure: product
class ProductReadiness(unittest.TestCase):
    def test_packaged_aligned_deep_is_100(self):
        deal = {"stage": "proposal", "product_id": "prod_x",
                "value_ladder_stage": "core"}
        r = rd._product_readiness(deal, {"technical_depth": 5})
        self.assertEqual(r["sub"], {"packaged": 40, "ladder_fit": 30,
                                    "technical_depth": 30})
        self.assertEqual(r["value"], 100)

    def test_unpackaged_is_zero(self):
        r = rd._product_readiness({"stage": "lead"}, None)
        self.assertEqual(r["value"], 0)

    def test_misaligned_ladder_half_credit(self):
        # recurrente offer pitched at a raw lead → repackage before pitching
        deal = {"stage": "lead", "value_ladder_stage": "recurrente"}
        r = rd._product_readiness(deal, None)
        self.assertEqual(r["sub"]["packaged"], 15)
        self.assertEqual(r["sub"]["ladder_fit"], 15)

    def test_questions_fallback_when_no_marker_depth(self):
        r = rd._product_readiness({"stage": "lead"}, {"questions": 6})
        self.assertEqual(r["sub"]["technical_depth"], 15)


# ============================================================ pure: market
class MarketReadiness(unittest.TestCase):
    def test_velocity_accelerating(self):
        now = int(time.time())
        events = [{"created_at": now - 86400 * d} for d in (1, 2, 3)]
        r = rd._market_readiness({"stage": "lead"}, events, None)
        self.assertEqual(r["sub"]["velocity"], 25)

    def test_velocity_cooling_vs_silent(self):
        now = int(time.time())
        old = [{"created_at": now - 86400 * 20}]
        self.assertEqual(
            rd._market_readiness({}, old, None)["sub"]["velocity"], 5)
        self.assertEqual(
            rd._market_readiness({}, [], None)["sub"]["velocity"], 0)

    def test_sentiment_tiers(self):
        pos = rd._market_readiness({}, [], {"sentiment": "positive"})
        neu = rd._market_readiness({}, [], {"sentiment": "neutral"})
        neg = rd._market_readiness({}, [], {"sentiment": "negative"})
        none = rd._market_readiness({}, [], {})
        self.assertEqual(pos["sub"]["sentiment"], 15)
        self.assertEqual(neu["sub"]["sentiment"], 8)
        self.assertEqual(neg["sub"]["sentiment"], 0)
        self.assertEqual(none["sub"]["sentiment"], 0)


# ============================================================ pure: composite
class Composite(unittest.TestCase):
    def test_hot_deal_pins_exact(self):
        """Fully-loaded deal, touched today → all dimensions 100, no decay."""
        deal = {
            "stage": "proposal", "value": 85000, "product_id": "prod_x",
            "value_ladder_stage": "core", "lead_source": "referral",
            "industry": "fintech", "expected_close_date": _today(14),
            "last_touch_date": _today(0),
        }
        ff = {"budget_mentions": 3, "urgency_mentions": 2, "technical_depth": 6,
              "decision_maker_present": True, "sentiment": "positive"}
        now = int(time.time())
        events = [{"created_at": now - 86400 * d} for d in (1, 2)]
        # icp_industries() reads live DB/env config — pin it so the exact
        # score is order-independent across the suite.
        with mock.patch("dashboard.growth.icp_industries",
                        return_value={"fintech"}):
            r = rd.compute_readiness(deal, events=events, ff_signals=ff,
                                     contact_role="CEO")
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["bucket"], "hot")
        self.assertEqual(r["decay"]["factor"], 1.0)

    def test_decay_drags_a_stale_deal_down(self):
        deal = {"stage": "proposal", "value": 85000, "product_id": "p",
                "value_ladder_stage": "core", "lead_source": "referral",
                "industry": "fintech", "expected_close_date": _today(14),
                "last_touch_date": _today(-90)}
        fresh = rd.compute_readiness({**deal, "last_touch_date": _today(0)},
                                     contact_role="CEO")
        stale = rd.compute_readiness(deal, contact_role="CEO")
        self.assertEqual(stale["decay"]["factor"], 0.4)
        self.assertEqual(stale["raw_score"], fresh["raw_score"])
        self.assertEqual(stale["score"], round(stale["raw_score"] * 0.4))
        self.assertLess(stale["score"], fresh["score"])

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(rd.DIMENSION_WEIGHTS.values()), 1.0)

    def test_result_shape(self):
        r = rd.compute_readiness({"stage": "lead"})
        for key in ("score", "bucket", "raw_score", "decay", "dimensions",
                    "weights", "next_best_action", "computed_at"):
            self.assertIn(key, r)
        self.assertEqual(set(r["dimensions"]), {"buyer", "product", "market"})


# ============================================================ pure: fireflies
class FirefliesExtraction(unittest.TestCase):
    def _meeting(self, overview="", topics=(), keywords=(), title="Reunión"):
        return {
            "title": title,
            "signals": {"topics": list(topics), "sentiment": "positive",
                        "questions": 5},
            "raw_summary": json.dumps({
                "overview": overview,
                "action_items": ["enviar propuesta"],
                "keywords": list(keywords),
            }),
        }

    def test_spanish_markers(self):
        m = self._meeting(
            overview=("El dueño preguntó por el presupuesto y pidió una "
                      "cotización; quiere empezar este mes, es urgente. "
                      "Hubo preguntas sobre arquitectura y base de datos."),
            keywords=["datos", "pipeline"],
            topics=["integración con API"])
        sig = rd.extract_fireflies_readiness([m])
        self.assertGreaterEqual(sig["budget_mentions"], 2)     # presupuesto+cotización
        self.assertGreaterEqual(sig["urgency_mentions"], 2)    # este mes+urgente
        self.assertGreaterEqual(sig["technical_depth"], 4)     # arquitectura, bd, datos…
        self.assertTrue(sig["decision_maker_present"])         # dueño
        self.assertEqual(sig["sentiment"], "positive")
        self.assertEqual(sig["questions"], 5)
        self.assertEqual(sig["meetings_analyzed"], 1)

    def test_english_markers_also_work(self):
        m = self._meeting(overview="The founder asked about budget and "
                                   "timeline; wants to start asap.")
        sig = rd.extract_fireflies_readiness([m])
        self.assertGreaterEqual(sig["budget_mentions"], 1)
        self.assertGreaterEqual(sig["urgency_mentions"], 2)
        self.assertTrue(sig["decision_maker_present"])

    def test_empty_and_garbage_meetings(self):
        empty = rd.extract_fireflies_readiness([])
        self.assertEqual(empty["meetings_analyzed"], 0)
        self.assertEqual(empty["budget_mentions"], 0)
        self.assertFalse(empty["decision_maker_present"])
        garbage = rd.extract_fireflies_readiness(
            [{"title": None, "signals": None, "raw_summary": "not json"}])
        self.assertEqual(garbage["meetings_analyzed"], 1)
        self.assertEqual(garbage["budget_mentions"], 0)

    def test_distinct_markers_not_occurrences(self):
        """'presupuesto' five times is ONE budget signal, not five."""
        m = self._meeting(overview="presupuesto " * 5)
        sig = rd.extract_fireflies_readiness([m])
        self.assertEqual(sig["budget_mentions"], 1)


# ============================================================ pure: actions
class NextBestAction(unittest.TestCase):
    def test_every_bucket_has_an_action(self):
        for bucket, _, _ in rd.BUCKETS:
            self.assertTrue(rd.next_best_action(bucket))

    def test_hot_is_ladder_aware(self):
        upgrade = rd.next_best_action("hot", {"value_ladder_stage": "core"})
        fresh = rd.next_best_action("hot", {})
        self.assertIn("Sprint 2", upgrade)
        self.assertNotEqual(upgrade, fresh)

    def test_sales_ready_pitches_sprint_1(self):
        self.assertIn("Sprint 1", rd.next_best_action("sales_ready", {}))

    def test_nurture_nurtures(self):
        self.assertIn("Nurture", rd.next_best_action("nurture", {}))


# ============================================================ DB: persistence
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class Persistence(unittest.TestCase):
    def setUp(self):
        rd.ensure_schema()
        acct = _crm.create_account(f"Readiness Test Co {time.time_ns()}")
        self.account_id = acct["account_id"]
        deal = _crm.create_deal(self.account_id, "Readiness deal",
                                stage="qualified", value=45000)
        self.deal_id = deal["deal_id"]

    def test_schema_columns_exist_and_idempotent(self):
        rd.ensure_schema()   # second run must not raise
        conn = _db.get_conn()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        finally:
            conn.close()
        self.assertIn("readiness_score", cols)
        self.assertIn("readiness_dimensions", cols)

    def test_score_readiness_persists_and_events(self):
        res = rd.score_readiness(self.deal_id)
        self.assertEqual(res["status"], "ok")
        self.assertIsInstance(res["score"], int)
        deal = _crm.get_deal(self.deal_id)
        self.assertEqual(deal["readiness_score"], res["score"])
        dims = deal["readiness_dimensions"]     # parsed JSON via get_deal
        self.assertIsInstance(dims, dict)
        self.assertEqual(set(dims["dimensions"]), {"buyer", "product", "market"})
        self.assertIn(dims["bucket"], [b for b, _, _ in rd.BUCKETS])
        kinds = [e["kind"] for e in deal["events"]]
        self.assertIn("readiness_scored", kinds)

    def test_score_does_not_bump_updated_at(self):
        before = _crm.get_deal(self.deal_id)["updated_at"]
        rd.score_readiness(self.deal_id)
        after = _crm.get_deal(self.deal_id)["updated_at"]
        self.assertEqual(before, after,
                         "derived-score write must not reset the decay clock")

    def test_autoscore_on_deal_write(self):
        """crm deal writes refresh readiness best-effort (via _autoscore)."""
        _crm.update_deal(self.deal_id, stage="demo")
        deal = _crm.get_deal(self.deal_id)
        self.assertIsNotNone(deal["readiness_score"])

    def test_unknown_deal(self):
        self.assertEqual(rd.score_readiness("deal_nope")["status"], "error")

    def test_from_fireflies_requires_stored_meetings(self):
        res = rd.score_readiness_from_fireflies(self.deal_id)
        self.assertEqual(res["status"], "no_meetings")

    def test_from_fireflies_with_stored_meeting(self):
        _db.ensure_fireflies_schema()
        _db.fireflies_meeting_insert({
            "id": f"ffm_test_{time.time_ns()}",
            "deal_id": self.deal_id,
            "transcript_id": "tr_x", "title": "Discovery",
            "meeting_date": _today(0), "duration_seconds": 60,
            "signals": {"sentiment": "positive", "questions": 6,
                        "topics": ["arquitectura de datos"]},
            "raw_summary": json.dumps({
                "overview": "El fundador pidió presupuesto, urgente."}),
            "fetched_at": int(time.time()), "created_at": int(time.time()),
        })
        res = rd.score_readiness_from_fireflies(self.deal_id)
        self.assertEqual(res["status"], "ok")
        sig = res["fireflies_signals"]
        self.assertGreaterEqual(sig["budget_mentions"], 1)
        self.assertGreaterEqual(sig["urgency_mentions"], 1)
        self.assertTrue(sig["decision_maker_present"])
        # and the meeting signals moved the persisted dimensions
        deal = _crm.get_deal(self.deal_id)
        self.assertEqual(deal["readiness_score"], res["score"])


# ============================================================ API endpoints
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class ApiEndpoints(unittest.TestCase):
    def setUp(self):
        rd.ensure_schema()
        acct = _crm.create_account(f"Readiness API Co {time.time_ns()}")
        deal = _crm.create_deal(acct["account_id"], "API readiness deal",
                                stage="engaged", value=18000)
        self.deal_id = deal["deal_id"]

    def test_get_readiness_overview(self):
        r = _CLIENT.get("/api/readiness")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        keys = [b["key"] for b in body["buckets"]]
        self.assertEqual(keys, ["nurture", "qualified", "sales_ready", "hot"])
        ranges = [b["range"] for b in body["buckets"]]
        self.assertEqual(ranges, [[0, 30], [31, 60], [61, 85], [86, 100]])
        all_deals = [d for b in body["buckets"] for d in b["deals"]]
        mine = [d for d in all_deals if d["id"] == self.deal_id]
        self.assertEqual(len(mine), 1)
        for key in ("readiness_score", "dimensions", "next_best_action", "decay"):
            self.assertIn(key, mine[0])
        self.assertEqual(sum(b["count"] for b in body["buckets"]),
                         body["total_active"])

    def test_post_readiness_scores_deal(self):
        r = _CLIENT.post(f"/api/crm/deals/{self.deal_id}/readiness", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("dimensions", body)

    def test_post_readiness_404(self):
        r = _CLIENT.post("/api/crm/deals/deal_nope/readiness", json={})
        self.assertEqual(r.status_code, 404)

    def test_post_from_fireflies_flag(self):
        r = _CLIENT.post(f"/api/crm/deals/{self.deal_id}/readiness",
                         json={"from_fireflies": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "no_meetings")


# ============================================================ MCP parity
class McpWiring(unittest.TestCase):
    TOOLS = ["get_readiness", "score_readiness", "score_readiness_from_fireflies"]

    def test_registered_and_wired(self):
        import mcp_server
        registered = {t["name"] for t in mcp_server.TOOLS}
        for name in self.TOOLS:
            self.assertIn(name, registered, f"{name} missing from TOOLS")
            self.assertIn(name, mcp_server.TOOL_HANDLERS,
                          f"{name} missing from TOOL_HANDLERS")
            self.assertTrue(callable(mcp_server.TOOL_HANDLERS[name]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
