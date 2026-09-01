"""CRM Growth System — API + logic regression guard (all 4 phases).

Pins the growth layer added on top of the Phase-6 CRM:
  Phase 1 — deal growth fields + PATCH /growth + GET /api/pipeline-math
  Phase 2 — POST /api/crm/leads (quick-add) + lead scoring + POST /touch
  Phase 3 — GET /api/growth/loops + content_log (GET/POST /api/growth/content)
  Phase 4 — GET /api/scorecard (auto-derived weekly 5)

Isolation: dashboard.api runs ensure_schema() at import, so the DB layers are
pointed at a COPY of ~/.hermes/kanban.db BEFORE the import — the real DB is never
touched. If there's no kanban.db to copy, the whole case skips.

Run:  python -m pytest tests/test_crm_growth.py -v
      python -m unittest tests.test_crm_growth
"""
import atexit
import json
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
_growth = None
try:
    from dashboard import db as _db, crm as _crm  # no import side effects

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_growth_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB

        from dashboard import growth as _growth  # imports db → picks up KANBAN_DB
        from dashboard.api import app            # ensure_schema() runs here, on the copy
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


def _mk_lead(name="Ada Lovelace", company="Analytical Engine Co",
             source="referral", loop="referido", industry="saas",
             engagement_score=30):
    return _CLIENT.post("/api/crm/leads", json={
        "name": name, "company": company, "source": source, "loop": loop,
        "industry": industry, "engagement_score": engagement_score})


# ------------------------------------------------------------- Phase 1: schema
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class SchemaMigration(unittest.TestCase):
    def setUp(self):
        # dashboard.db.KANBAN_DB is a shared module global that other test
        # modules repoint to their own tmp copy; re-assert the growth schema on
        # whatever DB is active so these checks are order-independent.
        _growth.ensure_schema()

    def _deal_cols(self):
        conn = _db.get_conn()
        try:
            return [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        finally:
            conn.close()

    def test_deal_growth_columns_exist(self):
        cols = self._deal_cols()
        for c in ("value_ladder_stage", "growth_loop", "lead_source", "lead_score",
                  "touch_count", "last_touch_date", "next_touch_date"):
            self.assertIn(c, cols)

    def test_growth_tables_exist(self):
        conn = _db.get_conn()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        self.assertIn("lead_scoring_features", tables)
        self.assertIn("content_log", tables)

    def test_migration_is_idempotent(self):
        from dashboard.migrations import crm_growth as m
        r1 = m.run()
        r2 = m.run()
        self.assertEqual(r1["status"], "ok")
        self.assertTrue(r2["deal_cols_present"])
        # Second run adds nothing (columns already present).
        self.assertEqual(r2["deal_cols_added"], [])


# ------------------------------------------------------ Phase 1: pipeline math
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class PipelineMath(unittest.TestCase):
    def test_backward_funnel_monotonic(self):
        j = _CLIENT.get("/api/pipeline-math?revenue_goal=120000&avg_ticket=40000").json()
        need = {f["key"]: f["need"] for f in j["funnel"]}
        # clients=ceil(120000/40000)=3; then each earlier stage needs MORE.
        self.assertEqual(need["clients"], 3)
        self.assertGreaterEqual(need["proposals"], need["clients"])
        self.assertGreaterEqual(need["discovery"], need["proposals"])
        self.assertGreaterEqual(need["leads"], need["discovery"])
        self.assertGreaterEqual(need["touches"], need["leads"])

    def test_coverage_reported(self):
        j = _CLIENT.get("/api/pipeline-math").json()
        self.assertIn("coverage", j["pipeline"])
        self.assertIn("open_value", j["pipeline"])


# --------------------------------------------------- Phase 2: capture + score
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class LeadCapture(unittest.TestCase):
    def test_quick_add_creates_chain(self):
        r = _mk_lead()
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["status"], "created")
        self.assertTrue(j["account_id"] and j["contact_id"] and j["deal_id"])
        # The deal lands at value-ladder 'iman' / stage 'lead'.
        deal = _CLIENT.get(f"/api/crm/deals/{j['deal_id']}/drilldown").json()["deal"]
        self.assertEqual(deal["stage"], "lead")
        self.assertEqual(deal["value_ladder_stage"], "iman")
        self.assertEqual(deal["growth_loop"], "referido")

    def test_lead_score_is_rule_based(self):
        # 4-category model: firmographic = referral(12) + ICP industry(12),
        # product_fit = ladder fit from quick-add's 'iman' stage(5) → 29.
        # (engagement_score is stored but no longer moves the score; behavioral
        # points come from touches/events, which a fresh lead has none of.)
        # Pin the ICP so the score is hermetic, not coupled to live-DB config.
        _growth.set_icp({"industries": ["saas"]})
        r = _mk_lead(engagement_score=30, source="referral", industry="saas")
        self.assertEqual(r.json()["lead_score"], 29)

    def test_score_pure_function(self):
        # Pin the ICP so the industry-fit floor is hermetic.
        _growth.set_icp({"industries": ["saas"]})
        s = _growth.score_features(source="cold_email", engagement_score=100,
                                   industry="unknown_vertical")
        # cold_email(2) + non-ICP industry floor(3); no profile, no behavior,
        # no fireflies signals, no product fit = 5.
        self.assertEqual(s["score"], 5)

    def test_quick_add_rejects_bad_loop(self):
        r = _CLIENT.post("/api/crm/leads", json={"name": "X", "loop": "nope"})
        self.assertEqual(r.status_code, 400)


# ----------------------------------------------------------- Phase 2: touches
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class TouchTracking(unittest.TestCase):
    def test_touch_increments_and_dates(self):
        did = _mk_lead().json()["deal_id"]
        r = _CLIENT.post(f"/api/crm/deals/{did}/touch", json={"note": "called"})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["touch_count"], 1)
        self.assertTrue(j["last_touch_date"] and j["next_touch_date"])
        # REPLACED CONTRACT (journey fase 1, step 5). This used to assert
        # `next_touch > last_touch` — which was not a requirement, it was the
        # flat `today + 7d` implementation frozen into an assertion (the
        # Tier-1c "true vs required" trap). `record_touch` now DERIVES the date
        # from the cadence ledger (`cadence.recompute` → MIN pending
        # scheduled_date), and `quick_add_lead` auto-generates a sequence whose
        # first step is scheduled for TODAY — so "next touch: today" is the
        # correct answer here, and the old assertion was demanding that the deal
        # card contradict its own nurture panel.
        #
        # What IS required: a date exists, it is never in the past, and it is the
        # ledger's own next pending step rather than a number this verb invented.
        self.assertGreaterEqual(j["next_touch_date"], j["last_touch_date"])
        nxt = _CLIENT.get(f"/api/growth/nurture/{did}").json()
        self.assertEqual(j["next_touch_date"], nxt["next_suggested_date"],
                         "one cadence, one date — never a second clock")
        # Second touch bumps to 2.
        r2 = _CLIENT.post(f"/api/crm/deals/{did}/touch", json={})
        self.assertEqual(r2.json()["touch_count"], 2)

    def test_touch_unknown_deal_404(self):
        r = _CLIENT.post("/api/crm/deals/deal_missing/touch", json={})
        self.assertEqual(r.status_code, 404)


# ------------------------------------------------------- Phase 3: loops + content
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class GrowthLoops(unittest.TestCase):
    def test_loops_shape(self):
        _mk_lead(loop="autoridad")
        j = _CLIENT.get("/api/growth/loops").json()
        keys = {l["key"] for l in j["loops"]}
        self.assertEqual(keys, {"autoridad", "referido", "producto"})
        for l in j["loops"]:
            self.assertIn("leads", l)
            self.assertIn("conversion", l)
            self.assertIn("ratio", l)

    def test_content_add_and_cadence(self):
        before = _CLIENT.get("/api/growth/content").json()["this_week"]
        r = _CLIENT.post("/api/growth/content", json={
            "title": "How we shipped an MVP in 2 weeks", "channel": "blog",
            "loop": "autoridad"})
        self.assertEqual(r.status_code, 200, r.text)
        j = _CLIENT.get("/api/growth/content").json()
        self.assertEqual(j["this_week"], before + 1)
        self.assertGreaterEqual(j["streak"], 1)
        self.assertTrue(len(j["weeks"]) >= 1)

    def test_content_rejects_bad_channel(self):
        r = _CLIENT.post("/api/growth/content", json={"title": "x", "channel": "myspace"})
        self.assertEqual(r.status_code, 400)


# ----------------------------------------------------- Phase 4: weekly scorecard
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class Scorecard(unittest.TestCase):
    def test_scorecard_auto_derives(self):
        base = _CLIENT.get("/api/scorecard").json()
        base_kpi = {k["key"]: k["value"] for k in base["kpis"]}
        # A new lead + a touch + a content piece should bump the live counters.
        did = _mk_lead().json()["deal_id"]
        _CLIENT.post(f"/api/crm/deals/{did}/touch", json={})
        _CLIENT.post("/api/growth/content", json={"title": "weekly note"})
        after = _CLIENT.get("/api/scorecard").json()
        after_kpi = {k["key"]: k["value"] for k in after["kpis"]}
        self.assertGreaterEqual(after_kpi["leads"], base_kpi["leads"] + 1)
        self.assertGreaterEqual(after_kpi["touches"], base_kpi["touches"] + 1)
        self.assertGreaterEqual(after_kpi["content"], base_kpi["content"] + 1)

    def test_proposal_counted_on_stage_change(self):
        did = _mk_lead().json()["deal_id"]
        base = {k["key"]: k["value"] for k in _CLIENT.get("/api/scorecard").json()["kpis"]}
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "proposal"})
        after = {k["key"]: k["value"] for k in _CLIENT.get("/api/scorecard").json()["kpis"]}
        self.assertEqual(after["proposals"], base["proposals"] + 1)

    def test_scorecard_has_five_kpis(self):
        j = _CLIENT.get("/api/scorecard").json()
        self.assertEqual(len(j["kpis"]), 5)
        self.assertEqual({k["key"] for k in j["kpis"]},
                         {"leads", "touches", "discovery", "content", "proposals"})


# ------------------------------------------------ Stalled stage (icebox) + decay
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class StalledStage(unittest.TestCase):
    def _pipeline(self):
        return _CLIENT.get("/api/crm/pipeline").json()

    def test_stalled_in_stages(self):
        self.assertIn("stalled", self._pipeline()["stages"])

    def test_stale_and_decay_endpoints(self):
        stale = _CLIENT.get("/api/crm/stale?days=30").json()
        self.assertIn("stale_deals", stale)
        # Huge thresholds → nothing eligible → zero, but keys present.
        decay = _CLIENT.post("/api/crm/decay?days_to_stalled=99999&days_to_lost=99999").json()
        for k in ("stalled_count", "lost_count", "stalled_deals", "lost_deals"):
            self.assertIn(k, decay)
        self.assertEqual(decay["stalled_count"], 0)
        self.assertEqual(decay["lost_count"], 0)

    def test_stalled_excluded_from_open_value(self):
        did = _mk_lead(company="Stall Co A").json()["deal_id"]
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"value": 5000})
        before = self._pipeline()
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "stalled"})
        after = self._pipeline()
        # Value leaves open_value and shows up under stalled_value.
        self.assertEqual(after["open_value"], before["open_value"] - 5000)
        self.assertGreaterEqual(after["stalled_value"], 5000)

    def test_lost_to_stalled_clears_closed_at(self):
        did = _mk_lead(company="Stall Co B").json()["deal_id"]
        _crm.update_deal(did, stage="lost")
        self.assertIsNotNone(_crm.get_deal(did)["closed_at"])
        _crm.update_deal(did, stage="stalled")
        # Reopening out of a closed stage must clear closed_at — stalled is open.
        self.assertIsNone(_crm.get_deal(did)["closed_at"])

    def test_pipeline_health_excludes_stalled(self):
        did = _mk_lead(company="Stall Co C").json()["deal_id"]
        _crm.update_deal(did, stage="stalled")
        ph = _growth.pipeline_health()
        for lvl in ("red", "yellow", "blue"):
            ids = {d["id"] for d in ph["levels"][lvl]["deals"]}
            self.assertNotIn(did, ids)

    def test_decay_moves_backdated_deal_to_stalled(self):
        did = _mk_lead(company="Stall Co D").json()["deal_id"]
        # Backdate updated_at to 60 days ago so it is decay-eligible.
        conn = _db.get_conn()
        try:
            conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?",
                         (_crm._now() - 60 * 86400, did))
            conn.commit()
        finally:
            conn.close()
        out = _CLIENT.post("/api/crm/decay?days_to_stalled=30&days_to_lost=90").json()
        self.assertGreaterEqual(out["stalled_count"], 1)
        self.assertEqual(_crm.get_deal(did)["stage"], "stalled")
        # Now stalled with a fresh updated_at → NOT immediately eligible for lost.
        self.assertIsNone(_crm.get_deal(did)["closed_at"])


class _DeliveredBase(unittest.TestCase):
    """Fixture shared by the retirement matrix and the derived-history reads."""

    def _deal(self, company: str, stage: str = "lead", value: float = 5000,
              expected_close_date: str = None):
        aid = _crm.create_account(company)["account_id"]
        return _crm.create_deal(aid, f"{company} engagement", stage=stage,
                                value=value,
                                expected_close_date=expected_close_date)["deal_id"]

    def _delivered_project(self, name: str, deal_id: str = None) -> str:
        """A project whose delivery leg is CLOSED — the fact "delivered" now
        lives on. Written directly (not through the verb) so the read under test
        is the derivation, not `mark_project_delivered`'s bookkeeping."""
        pid = "proj_" + name.lower().replace(" ", "_")
        conn = _db.get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, slug, name, created_at, status, "
                "delivered_at) VALUES (?,?,?,?,?,?)",
                (pid, pid.replace("proj_", ""), name, 1, "delivered",
                 "2026-08-01T12:00:00-06:00"))
            if deal_id:
                conn.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                             (pid, deal_id))
            conn.commit()
        finally:
            conn.close()
        return pid


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class DeliveredStageRetired(_DeliveredBase):
    """REPLACES `test_delivered_is_a_terminal_pipeline_stage` — the negative
    matrix per ruling 4.

    The test this replaces asserted that `stage='delivered'` was writable end to
    end, i.e. it *proved the gates were open*. That was the CRITICAL-2 shape: a
    single column carrying both "the money landed" and "the work shipped", so a
    delivery deleted the commercial fact. A won deal now stays `won` forever and
    delivery is `projects.status`.

    A retirement is only real if EVERY writer refuses, so the gates are
    inventoried per entity + verb and asserted one by one:
        REST      POST /api/crm/deals · PATCH /api/crm/deals/{id} · POST …/children
        verb      crm.create_deal / crm.update_deal (typed `stage_retired`)
        MCP       tool_create_deal / tool_update_deal (server-side, enums are advisory)
        engine    the m05 trigger pair — `tests/test_m05_stage_guard.py`, which
                  owns a per-test migrated copy so the DDL is provably present
    Each row is red by construction against pre-change code.
    """

    def test_the_rest_create_path_refuses_the_retired_stage(self):
        aid = _crm.create_account("Retired REST Create Co")["account_id"]
        r = _CLIENT.post("/api/crm/deals", json={
            "account_id": aid, "title": "should never exist", "stage": "delivered"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("retired", r.text)
        self.assertEqual(_crm.list_deals(stage="delivered"), [])

    def test_the_rest_update_path_refuses_the_retired_stage(self):
        did = self._deal("Retired REST Update Co", stage="won")
        r = _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "delivered"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(_crm.get_deal(did)["stage"], "won")

    def test_the_child_deal_path_refuses_the_retired_stage(self):
        parent = self._deal("Retired Child Co", stage="won")
        r = _CLIENT.post(f"/api/crm/deals/{parent}/children",
                         json={"title": "sub-deal", "stage": "delivered"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(_crm.list_deal_children(parent), [])

    def test_the_verb_layer_types_its_refusal(self):
        """A typed code, not a prose 400: the UI and the MCP surface both switch
        on it, and it names the replacement so the caller stops retrying."""
        aid = _crm.create_account("Retired Verb Co")["account_id"]
        created = _crm.create_deal(aid, "nope", stage="delivered")
        self.assertEqual(created["code"], "stage_retired")
        self.assertIn("projects.status", created["error"])

        did = _crm.create_deal(aid, "real one", stage="won")["deal_id"]
        updated = _crm.update_deal(did, stage="delivered")
        self.assertEqual(updated["code"], "stage_retired")
        self.assertEqual(_crm.get_deal(did)["stage"], "won")

    def test_won_stays_writable_on_every_path(self):
        """The retirement is surgical. `won` is the web pipeline's terminal
        column — banning it too would have made the board unusable."""
        aid = _crm.create_account("Won Still Writable Co")["account_id"]
        born_won = _CLIENT.post("/api/crm/deals", json={
            "account_id": aid, "title": "born won", "stage": "won", "value": 100})
        self.assertEqual(born_won.status_code, 200, born_won.text)
        did = self._deal("Won Still Writable Two Co")
        moved = _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "won"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(_crm.get_deal(did)["stage"], "won")
        self.assertIsNotNone(_crm.get_deal(did)["closed_at"])
        # ...and reopening still clears the sales close.
        _crm.update_deal(did, stage="engaged")
        self.assertIsNone(_crm.get_deal(did)["closed_at"])

    def test_the_mcp_handlers_refuse_server_side(self):
        """The enum is a HINT — a hand-rolled JSON-RPC call never sees it. Both
        gates therefore live in the handler, and both are typed."""
        import mcp_server
        aid = _crm.create_account("Retired MCP Co")["account_id"]
        created = json.loads(mcp_server.tool_create_deal(
            {"account_id": aid, "title": "nope", "stage": "delivered"}))
        self.assertEqual(created["code"], "stage_retired")

        did = self._deal("Retired MCP Update Co")
        for stage, code in (("delivered", "stage_retired"), ("won", "stage_human_only")):
            out = json.loads(mcp_server.tool_update_deal(
                {"deal_id": did, "stage": stage}))
            self.assertEqual(out["code"], code, out)
        # Neither write happened.
        self.assertEqual(_crm.get_deal(did)["stage"], "lead")
        # `won` is refused as HUMAN-ONLY, not as retired: the deep link is the
        # path (ruling 3), so the refusal has to point at it.
        self.assertIn("deliver", json.loads(mcp_server.tool_update_deal(
            {"deal_id": did, "stage": "won"}))["error"])

    def test_the_mcp_enums_no_longer_offer_what_the_handlers_refuse(self):
        """REPLACES `test_mcp_stage_enums_include_stalled_and_delivered`. An
        enum that offers a value the handler rejects teaches the model to retry
        a refused call; `list_deals` keeps it because READING the legacy
        vocabulary is still legal (ruling 2)."""
        import mcp_server
        tools = {t["name"]: t for t in mcp_server.TOOLS}
        enums = {name: tools[name]["inputSchema"]["properties"]["stage"]["enum"]
                 for name in ("create_deal", "update_deal", "list_deals")}
        for name, enum in enums.items():
            self.assertIn("stalled", enum, f"{name} stage enum missing 'stalled'")
        self.assertNotIn("delivered", enums["create_deal"])
        self.assertNotIn("delivered", enums["update_deal"])
        self.assertNotIn("won", enums["update_deal"])
        self.assertIn("won", enums["create_deal"])       # recording a closed deal
        self.assertIn("delivered", enums["list_deals"])  # legacy READ vocabulary

    def test_the_speaking_pipeline_is_preserved(self):
        """Ruling 4: only the two DEAL controls die. A talk's `delivered` means
        the conference happened — a different entity with no double meaning, and
        the retirement stops at the CRM."""
        self.assertIn("delivered", _growth.SPEAKING_STATUS)
        talk = _growth.create_speaking("Retirement-proof keynote", status="proposed")
        self.assertEqual(talk.get("status"), "created", talk)
        moved = _growth.update_speaking(talk["speaking_id"], {"status": "delivered"})
        self.assertNotEqual(moved.get("status"), "error", moved)
        self.assertEqual(
            next(t for t in _growth.list_speaking()["events"]
                 if t["id"] == talk["speaking_id"])["status"], "delivered")


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class DeliveredHistoryIsDerived(_DeliveredBase):
    """The delivered COLUMN survives; it is now a read over the join.

    These replace the four value/history assertions that used to reach it by
    writing `stage='delivered'`. Same questions — does delivery keep won_value,
    does it stay out of open pipeline, is closed revenue protected — asked of
    the shape that actually ships.
    """

    def test_a_won_deal_on_a_delivered_project_is_history_not_pipeline(self):
        did = self._deal("Derived History Co", value=7300)
        _crm.update_deal(did, stage="won")
        pipe = _CLIENT.get("/api/crm/pipeline").json()
        self.assertIn(did, {d["id"] for d in pipe["by_stage"]["won"]})

        self._delivered_project("Derived History Delivery", deal_id=did)
        pipe = _CLIENT.get("/api/crm/pipeline").json()
        self.assertIn(did, {d["id"] for d in pipe["by_stage"]["delivered"]})
        self.assertNotIn(did, {d["id"] for d in pipe["by_stage"]["won"]})
        self.assertEqual(pipe["counts"]["delivered"],
                         len(pipe["by_stage"]["delivered"]))
        # The stage itself never changed — that is the whole point.
        self.assertEqual(_crm.get_deal(did)["stage"], "won")

    def test_delivery_keeps_won_value_and_never_becomes_open_value(self):
        before = _CLIENT.get("/api/crm/pipeline").json()
        did = self._deal("Derived Value Co", value=7300)
        _crm.update_deal(did, stage="won")
        self._delivered_project("Derived Value Delivery", deal_id=did)
        after = _CLIENT.get("/api/crm/pipeline").json()
        self.assertEqual(after["open_value"], before["open_value"])
        self.assertEqual(after["won_value"], before["won_value"] + 7300)

    def test_delivered_revenue_is_protected_history_and_account_won_value(self):
        did = self._deal("Derived Protected Co", value=9100)
        _crm.update_deal(did, stage="won")
        self._delivered_project("Derived Protected Delivery", deal_id=did)
        deal = _crm.get_deal(did)
        chain = _crm.account_chain(deal["account_id"])
        self.assertEqual(chain["open_value"], 0)
        self.assertEqual(chain["won_value"], 9100)
        refused = _crm.delete_deal(did)
        self.assertEqual(refused["status"], "error")
        self.assertIn("closed revenue is history", refused["error"])
        self.assertIsNotNone(_crm.get_deal(did))

    def test_a_null_value_delivery_does_not_inflate_won_value(self):
        before = _CLIENT.get("/api/crm/pipeline").json()["won_value"]
        did = self._deal("Derived Null Value Co", value=None)
        _crm.update_deal(did, stage="won")
        deal = _crm.get_deal(did)
        self._delivered_project("Derived Null Value Delivery", deal_id=did)
        after = _CLIENT.get("/api/crm/pipeline").json()["won_value"]
        self.assertEqual(after, before)
        self.assertEqual(_crm.account_chain(deal["account_id"])["won_value"], 0)

    def test_an_undelivered_project_leaves_the_deal_in_the_won_column(self):
        """The derivation is the PROJECT's status, not the mere existence of a
        link — a won deal in flight must stay visible as work in progress."""
        did = self._deal("Derived Active Co", value=4200)
        _crm.update_deal(did, stage="won")
        conn = _db.get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, slug, name, created_at, status) "
                "VALUES (?,?,?,?,?)",
                ("proj_derived_active", "derived-active", "Derived Active", 1, "active"))
            conn.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                         ("proj_derived_active", did))
            conn.commit()
        finally:
            conn.close()
        pipe = _CLIENT.get("/api/crm/pipeline").json()
        self.assertIn(did, {d["id"] for d in pipe["by_stage"]["won"]})
        self.assertNotIn(did, {d["id"] for d in pipe["by_stage"]["delivered"]})


# ------------------------------------------------------------- Phase: temporal
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class PipelineTemporal(unittest.TestCase):
    """GET /api/growth/pipeline-temporal — month-by-month deal flow + revenue."""

    def setUp(self):
        _growth.ensure_schema()

    def test_endpoint_returns_buckets(self):
        r = _CLIENT.get("/api/growth/pipeline-temporal?months=6")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("buckets", data)
        self.assertIn("totals", data)
        self.assertIn("currency", data)
        self.assertEqual(data["months"], 6)
        self.assertEqual(len(data["buckets"]), 6)

    def test_bucket_shape(self):
        r = _CLIENT.get("/api/growth/pipeline-temporal?months=3")
        b = r.json()["buckets"][0]
        for key in ("month", "label", "new_deals", "stage_moves",
                     "won_count", "won_value", "lost_count",
                     "active_end", "active_value"):
            self.assertIn(key, b, f"bucket missing key: {key}")
        self.assertRegex(b["month"], r"^\d{4}-\d{2}$")
        self.assertIsInstance(b["new_deals"], int)
        self.assertIsInstance(b["stage_moves"], int)
        self.assertIsInstance(b["won_count"], int)
        self.assertIsInstance(b["lost_count"], int)
        self.assertIsInstance(b["active_end"], int)

    def test_totals_match_bucket_sums(self):
        r = _CLIENT.get("/api/growth/pipeline-temporal?months=12")
        data = r.json()
        t = data["totals"]
        buckets = data["buckets"]
        self.assertEqual(t["new_deals"], sum(b["new_deals"] for b in buckets))
        self.assertEqual(t["stage_moves"], sum(b["stage_moves"] for b in buckets))
        self.assertEqual(t["won_count"], sum(b["won_count"] for b in buckets))
        self.assertEqual(t["lost_count"], sum(b["lost_count"] for b in buckets))

    def test_months_clamped(self):
        r = _CLIENT.get("/api/growth/pipeline-temporal?months=999")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertLessEqual(len(data["buckets"]), 60)

    def test_direct_function(self):
        """Call growth.pipeline_temporal directly (no HTTP layer)."""
        result = _growth.pipeline_temporal(months=12)
        self.assertEqual(len(result["buckets"]), 12)
        self.assertIn("totals", result)
        # Buckets should be oldest→newest.
        months = [b["month"] for b in result["buckets"]]
        self.assertEqual(months, sorted(months))

    def test_mcp_tool_registered(self):
        import mcp_server
        tools = {t["name"]: t for t in mcp_server.TOOLS}
        self.assertIn("get_pipeline_temporal", tools)
        self.assertIn("months", tools["get_pipeline_temporal"]["inputSchema"]["properties"])


# ------------------------------------------------- Win/loss: the lost reason
@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class LostReason(unittest.TestCase):
    """A loss is only useful if you can count WHY. Pins: the category is
    validated, it only sticks to a lost deal, leaving 'lost' clears it, the
    stage event carries it, and the report counts uncategorised honestly."""

    def _deal(self):
        j = _mk_lead(name="Loss Case", company="Lost Co").json()
        return j["deal_id"]

    def test_valid_category_persists_with_notes(self):
        did = self._deal()
        r = _CLIENT.patch(f"/api/crm/deals/{did}", json={
            "stage": "lost", "lost_reason": "competitor", "lost_notes": "went with Acme"})
        self.assertEqual(r.status_code, 200)
        d = _CLIENT.get(f"/api/crm/deals/{did}/drilldown").json()["deal"]
        self.assertEqual(d["lost_reason"], "competitor")
        self.assertEqual(d["lost_notes"], "went with Acme")
        self.assertTrue(d["closed_at"], "a lost deal must be stamped closed")

    def test_unknown_category_is_refused(self):
        did = self._deal()
        r = _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "lost", "lost_reason": "vibes"})
        self.assertEqual(r.status_code, 400)
        d = _CLIENT.get(f"/api/crm/deals/{did}/drilldown").json()["deal"]
        self.assertIsNone(d["lost_reason"], "a refused write must not partially apply")

    def test_reason_does_not_stick_to_a_live_deal(self):
        did = self._deal()
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"lost_reason": "price"})
        d = _CLIENT.get(f"/api/crm/deals/{did}/drilldown").json()["deal"]
        self.assertIsNone(d["lost_reason"], "only a lost deal carries a loss reason")

    def test_reopening_clears_the_reason(self):
        did = self._deal()
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "lost", "lost_reason": "timing"})
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "qualified"})
        d = _CLIENT.get(f"/api/crm/deals/{did}/drilldown").json()["deal"]
        self.assertIsNone(d["lost_reason"], "a live deal must not keep a loss category")
        self.assertIsNone(d["lost_notes"])

    def test_stage_event_records_the_reason(self):
        did = self._deal()
        _CLIENT.patch(f"/api/crm/deals/{did}", json={
            "stage": "lost", "lost_reason": "no_budget" if False else "price", "lost_notes": "over budget"})
        events = _CLIENT.get(f"/api/crm/deals/{did}/drilldown").json()
        blob = str(events)
        self.assertIn("price", blob, "the audit trail must carry the loss category")

    def test_report_counts_uncategorised_honestly(self):
        did = self._deal()
        _CLIENT.patch(f"/api/crm/deals/{did}", json={"stage": "lost"})   # no reason
        j = _CLIENT.get("/api/crm/loss-reasons").json()
        self.assertGreaterEqual(j["total"], 1)
        self.assertLessEqual(j["categorised"], j["total"])
        self.assertIn("vocabulary", j)
        buckets = {b["reason"]: b for b in j["buckets"]}
        self.assertIn(None, buckets, "uncategorised losses must be reported, not dropped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
