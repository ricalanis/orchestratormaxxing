"""crm_proposals — the propose-only contract (m27, motor caliente).

The invariants that make the correction inbox safe:
  * derive() NEVER mutates deals (full-table snapshot identical before/after);
  * derive() is idempotent (twice → same rows) and includes never-touched deals;
  * dismissed is sticky — a rejected (deal, kind, evidence) is never re-created;
  * approve() applies exactly once (double-approve → typed error), through the
    audited writers (touch_count bump + deal_events marker), and releases the
    proposal back to 'proposed' when the writer fails;
  * amount/next_touch payloads are validated at create time.

Isolation: same pattern as test_crm_growth.py — dashboard.db points at a COPY
of ~/.hermes/kanban.db before any dashboard import; skips without one.

Run:  python -m pytest tests/test_crm_proposals.py -v
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
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_cprop_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB
        from dashboard.migrations.runner import MIGRATIONS
        from dashboard import crm_proposals as _cp

        _conn = _db.get_conn()
        for _name, _fn in MIGRATIONS:
            if _name == "m27_crm_proposals":
                _fn(_conn)
        _conn.commit()
        _conn.close()
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _epoch(days_ago: int) -> int:
    return int(time.time()) - days_ago * 86400


def _snapshot_deals(conn):
    return conn.execute(
        "SELECT * FROM deals ORDER BY id").fetchall()


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class ProposalInbox(unittest.TestCase):

    def setUp(self):
        # conftest's pytest_collection_finish re-points _db.KANBAN_DB to the
        # session sandbox AFTER this module's import block, so the migration
        # must be (re-)applied to whatever DB is active NOW — idempotent.
        from dashboard.migrations.m27_crm_proposals import m27_crm_proposals
        conn = _db.get_conn()
        m27_crm_proposals(conn)
        conn.commit()
        self.account_id = f"acc_cprop_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (self.account_id, "Cuenta propuestas", _epoch(60)))

        def mk(title, stage, last_touch, touch_count=0):
            did = f"deal_cprop_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "last_touch_date, touch_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, self.account_id, title, stage, 1000.0, "MXN",
                 last_touch, touch_count, _epoch(60), _epoch(60)))
            return did

        # Meeting (5d ago) newer than the touch clock (12d ago) → proposal.
        self.d_behind = mk("CRM atrasado vs reunión", "proposal", _days_ago(12), 3)
        # Never touched at all → still proposed (Sol F2: NULL must not exclude).
        self.d_never = mk("nunca tocado", "lead", None, 0)
        # Touch clock ahead of the meeting → NO proposal.
        self.d_current = mk("CRM al día", "qualified", _days_ago(1), 5)

        def mk_meeting(deal_id, days_ago_n, transcript):
            conn.execute(
                "INSERT INTO fireflies_meetings (id, deal_id, transcript_id, title, "
                "meeting_date, created_at) VALUES (?,?,?,?,?,?)",
                (f"ffm_{uuid.uuid4().hex[:8]}", deal_id, transcript,
                 f"Reunión {transcript}", _days_ago(days_ago_n), _epoch(days_ago_n)))

        mk_meeting(self.d_behind, 5, "T-BEHIND")
        mk_meeting(self.d_never, 3, "T-NEVER")
        mk_meeting(self.d_current, 4, "T-CURRENT")
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = _db.get_conn()
        conn.execute("DELETE FROM crm_proposals WHERE deal_id IN "
                     "(SELECT id FROM deals WHERE account_id = ?)", (self.account_id,))
        conn.execute("DELETE FROM fireflies_meetings WHERE deal_id IN "
                     "(SELECT id FROM deals WHERE account_id = ?)", (self.account_id,))
        conn.execute("DELETE FROM deal_events WHERE deal_id IN "
                     "(SELECT id FROM deals WHERE account_id = ?)", (self.account_id,))
        conn.execute("DELETE FROM deals WHERE account_id = ?", (self.account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (self.account_id,))
        conn.commit()
        conn.close()

    def _proposals_for(self, deal_id, status=None):
        return [p for p in _cp.list_proposals(status=status)
                if p["deal_id"] == deal_id]

    # -- derive ------------------------------------------------------------

    def test_derive_proposes_when_meeting_beats_touch_clock(self):
        res = _cp.derive()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(self._proposals_for(self.d_behind)), 1)
        prop = self._proposals_for(self.d_behind)[0]
        self.assertEqual(prop["kind"], "touch")
        self.assertEqual(prop["evidence_ref"], "T-BEHIND")
        self.assertEqual(prop["payload"]["evidence_date"], _days_ago(5))

    def test_derive_includes_never_touched_deals(self):
        _cp.derive()
        self.assertEqual(len(self._proposals_for(self.d_never)), 1)

    def test_derive_skips_deals_whose_touch_clock_is_current(self):
        _cp.derive()
        self.assertEqual(self._proposals_for(self.d_current), [])

    def test_derive_never_mutates_deals(self):
        conn = _db.get_conn()
        before = _snapshot_deals(conn)
        conn.close()
        _cp.derive()
        conn = _db.get_conn()
        after = _snapshot_deals(conn)
        conn.close()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_derive_twice_is_idempotent(self):
        _cp.derive()
        count_1 = len(self._proposals_for(self.d_behind))
        _cp.derive()
        count_2 = len(self._proposals_for(self.d_behind))
        self.assertEqual((count_1, count_2), (1, 1))

    def test_dismiss_is_sticky_against_derive(self):
        _cp.derive()
        pid = self._proposals_for(self.d_behind)[0]["id"]
        self.assertEqual(_cp.dismiss(pid)["status"], "ok")
        _cp.derive()
        rows = self._proposals_for(self.d_behind)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "dismissed")

    # -- approve saga --------------------------------------------------------

    def _touch_state(self, deal_id):
        conn = _db.get_conn()
        try:
            return dict(conn.execute(
                "SELECT touch_count, last_touch_date, value, next_touch_date "
                "FROM deals WHERE id = ?", (deal_id,)).fetchone())
        finally:
            conn.close()

    def test_approve_touch_applies_once_and_audits(self):
        _cp.derive()
        pid = self._proposals_for(self.d_behind)[0]["id"]
        before = self._touch_state(self.d_behind)
        res = _cp.approve(pid, via="test")
        self.assertEqual(res["status"], "ok")
        after = self._touch_state(self.d_behind)
        self.assertEqual(after["touch_count"], before["touch_count"] + 1)
        conn = _db.get_conn()
        markers = conn.execute(
            "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = 'proposal_applied'",
            (self.d_behind,)).fetchall()
        conn.close()
        self.assertEqual(len(markers), 1)
        rows = self._proposals_for(self.d_behind)
        self.assertEqual(rows[0]["status"], "approved")
        self.assertTrue(rows[0]["applied_ref"])

    def test_double_approve_is_a_typed_error_not_a_second_touch(self):
        _cp.derive()
        pid = self._proposals_for(self.d_behind)[0]["id"]
        _cp.approve(pid)
        before = self._touch_state(self.d_behind)
        res = _cp.approve(pid)
        self.assertEqual(res["status"], "error")
        self.assertEqual(self._touch_state(self.d_behind)["touch_count"],
                         before["touch_count"])

    def test_amount_approve_updates_value_via_audited_writer(self):
        res = _cp.create(self.d_behind, "amount", {"value": 337500.0},
                         "manual", "gmail:acme-quote")
        self.assertTrue(res.get("created"))
        self.assertEqual(_cp.approve(res["id"])["status"], "ok")
        self.assertEqual(self._touch_state(self.d_behind)["value"], 337500.0)

    def test_next_touch_approve_sets_the_date(self):
        target = _days_ago(-3)  # three days out
        res = _cp.create(self.d_behind, "next_touch", {"date": target},
                         "manual", "gmail:next-step")
        self.assertTrue(res.get("created"))
        self.assertEqual(_cp.approve(res["id"])["status"], "ok")
        self.assertEqual(self._touch_state(self.d_behind)["next_touch_date"], target)

    def test_failed_apply_releases_the_claim(self):
        res = _cp.create(self.d_behind, "amount", {"value": 1.0},
                         "manual", "gmail:will-fail")
        pid = res["id"]
        conn = _db.get_conn()
        conn.execute("UPDATE crm_proposals SET payload = '{\"value\": \"boom\"}' "
                     "WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
        out = _cp.approve(pid)
        self.assertEqual(out["status"], "error")
        rows = [p for p in _cp.list_proposals(status="proposed") if p["id"] == pid]
        self.assertEqual(len(rows), 1, "failed apply must release back to proposed")

    # -- create validation ---------------------------------------------------

    def test_create_rejects_bad_payloads_and_unknown_deal(self):
        bad_amount = _cp.create(self.d_behind, "amount", {"value": "mucho"},
                                "manual", "x1")
        bad_date = _cp.create(self.d_behind, "next_touch", {"date": "pronto"},
                              "manual", "x2")
        no_deal = _cp.create("deal_missing", "touch", {}, "manual", "x3")
        for res in (bad_amount, bad_date, no_deal):
            self.assertEqual(res["status"], "error")

    def test_all_real_open_stages_derive(self):
        # Regression (2026-08-10): the module shipped naming 'discovery' — a
        # stage that does not exist in crm.STAGES — silently excluding
        # engaged/qualified/demo deals from derive().
        conn = _db.get_conn()
        mids = {}
        for stage in ("engaged", "qualified", "demo"):
            did = f"deal_cprop_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "last_touch_date, touch_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, self.account_id, f"mid-journey {stage}", stage, 1000.0,
                 "MXN", _days_ago(12), 1, _epoch(60), _epoch(60)))
            conn.execute(
                "INSERT INTO fireflies_meetings (id, deal_id, transcript_id, title, "
                "meeting_date, created_at) VALUES (?,?,?,?,?,?)",
                (f"ffm_{uuid.uuid4().hex[:8]}", did, f"T-{stage.upper()}",
                 f"Reunión {stage}", _days_ago(5), _epoch(5)))
            mids[stage] = did
        conn.commit()
        conn.close()
        _cp.derive()
        for stage, did in mids.items():
            self.assertEqual(len(self._proposals_for(did)), 1,
                             f"a '{stage}' deal must be inside derive()'s scope")

    def test_unknown_proposal_ids_are_typed_errors(self):
        self.assertEqual(_cp.approve("cprop_missing")["status"], "error")
        self.assertEqual(_cp.dismiss("cprop_missing")["status"], "error")

    def test_create_rejects_bad_kind_evidence_and_ref(self):
        cases = (
            _cp.create(self.d_behind, "bogus", {}, "manual", "e1"),
            _cp.create(self.d_behind, "touch", {}, "telepatia", "e2"),
            _cp.create(self.d_behind, "touch", {}, "manual", ""),
            _cp.create(self.d_behind, "touch", {"evidence_date": "ayer"},
                       "manual", "e3"),
        )
        for res in cases:
            self.assertEqual(res["status"], "error")

    def test_double_dismiss_is_typed_error(self):
        _cp.derive()
        pid = self._proposals_for(self.d_behind)[0]["id"]
        self.assertEqual(_cp.dismiss(pid)["status"], "ok")
        self.assertEqual(_cp.dismiss(pid)["status"], "error")

    def test_derive_reports_honest_counts(self):
        # Counts are sandbox-global (the conftest DB is a copy of the live one,
        # so real cached meetings can be present). Assert this fixture's OWN
        # share plus the global invariant that a second sweep adds nothing —
        # absolute totals would pin the environment, not the behavior.
        first = _cp.derive()
        mine = (len(self._proposals_for(self.d_behind))
                + len(self._proposals_for(self.d_never)))
        self.assertEqual(mine, 2, "d_behind + d_never — never padded")
        self.assertEqual(self._proposals_for(self.d_current), [],
                         "touch clock ahead of the meeting → no proposal")
        self.assertGreaterEqual(first["created"], 2)
        second = _cp.derive()
        self.assertEqual(second["created"], 0, "idempotent: a second sweep adds nothing")
        self.assertGreaterEqual(second["skipped"], 3)

    def test_same_day_touch_and_meeting_is_not_a_proposal(self):
        conn = _db.get_conn()
        conn.execute("UPDATE deals SET last_touch_date = ? WHERE id = ?",
                     (_days_ago(5), self.d_behind))  # same day as T-BEHIND
        conn.commit()
        conn.close()
        _cp.derive()
        self.assertEqual(self._proposals_for(self.d_behind), [])

    def test_datetime_formatted_meeting_date_still_derives(self):
        conn = _db.get_conn()
        conn.execute(
            "UPDATE fireflies_meetings SET meeting_date = ? WHERE transcript_id = 'T-BEHIND'",
            (_days_ago(5) + "T14:30:00",))
        conn.commit()
        conn.close()
        _cp.derive()
        rows = self._proposals_for(self.d_behind)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["evidence_date"], _days_ago(5))

    def test_null_meeting_title_falls_back_to_transcript_id(self):
        conn = _db.get_conn()
        conn.execute(
            "UPDATE fireflies_meetings SET title = NULL WHERE transcript_id = 'T-BEHIND'")
        conn.commit()
        conn.close()
        _cp.derive()
        note = self._proposals_for(self.d_behind)[0]["payload"]["note"]
        self.assertIn("T-BEHIND", note)

    def test_amount_zero_is_a_valid_correction(self):
        res = _cp.create(self.d_behind, "amount", {"value": 0},
                         "manual", "gmail:zero-out")
        self.assertTrue(res.get("created"))

    def test_datetime_evidence_date_is_accepted(self):
        res = _cp.create(self.d_behind, "touch",
                         {"evidence_date": "2026-08-05T10:00:00"},
                         "manual", "gmail:datetime-evidence")
        self.assertTrue(res.get("created"))

    def _latest_touch_event_note(self, deal_id):
        import json as _json
        conn = _db.get_conn()
        try:
            row = conn.execute(
                "SELECT payload FROM deal_events WHERE deal_id = ? AND kind = 'touch' "
                "ORDER BY rowid DESC LIMIT 1", (deal_id,)).fetchone()
            return _json.loads(row["payload"])["note"] if row else None
        finally:
            conn.close()

    def test_noteless_touch_gets_default_note_with_evidence_date(self):
        res = _cp.create(self.d_behind, "touch",
                         {"evidence_date": _days_ago(4)},
                         "manual", "gmail:noteless")
        self.assertEqual(_cp.approve(res["id"])["status"], "ok")
        note = self._latest_touch_event_note(self.d_behind)
        self.assertIn("Toque (evidencia manual)", note)
        self.assertIn(f"fecha real: {_days_ago(4)}", note)

    def test_malformed_stored_payload_is_tolerated(self):
        res = _cp.create(self.d_behind, "touch", {}, "manual", "gmail:mangle")
        pid = res["id"]
        conn = _db.get_conn()
        conn.execute("UPDATE crm_proposals SET payload = 'not json' WHERE id = ?",
                     (pid,))
        conn.commit()
        conn.close()
        listed = [p for p in _cp.list_proposals() if p["id"] == pid]
        self.assertEqual(listed[0]["payload"], {})
        self.assertEqual(_cp.approve(pid)["status"], "ok")

    def test_error_dict_from_writer_releases_the_claim(self):
        from unittest.mock import patch
        res = _cp.create(self.d_behind, "touch", {}, "manual", "gmail:errdict")
        with patch.object(_cp.growth, "record_touch",
                          return_value={"status": "error", "error": "nope"}):
            out = _cp.approve(res["id"])
        self.assertEqual(out["status"], "error")
        rows = [p for p in _cp.list_proposals(status="proposed")
                if p["id"] == res["id"]]
        self.assertEqual(len(rows), 1, "writer rejection must release the claim")

    def test_non_dict_writer_result_releases_the_claim(self):
        from unittest.mock import patch
        res = _cp.create(self.d_behind, "touch", {}, "manual", "gmail:nondict")
        with patch.object(_cp.growth, "record_touch", return_value=None):
            out = _cp.approve(res["id"])
        self.assertEqual(out["status"], "error")
        rows = [p for p in _cp.list_proposals(status="proposed")
                if p["id"] == res["id"]]
        self.assertEqual(len(rows), 1)

    def test_list_orders_newest_first(self):
        _cp.create(self.d_behind, "touch", {}, "manual", "old-evidence")
        _cp.create(self.d_behind, "touch", {}, "manual", "new-evidence")
        conn = _db.get_conn()
        conn.execute("UPDATE crm_proposals SET created_at = created_at - 100 "
                     "WHERE evidence_ref = 'old-evidence'")
        conn.commit()
        conn.close()
        refs = [p["evidence_ref"] for p in self._proposals_for(self.d_behind)]
        self.assertEqual(refs, ["new-evidence", "old-evidence"])
        # The UI's before→after diff reads the CURRENT deal value from here.
        row = self._proposals_for(self.d_behind)[0]
        self.assertEqual(row["deal_value"], 1000.0)
        self.assertEqual(row["deal_currency"], "MXN")

    # -- crash reconciliation (the 'applying' lease) -------------------------

    def _force_status(self, pid, status):
        conn = _db.get_conn()
        conn.execute("UPDATE crm_proposals SET status = ? WHERE id = ?",
                     (status, pid))
        conn.commit()
        conn.close()

    def test_crashed_approve_with_marker_is_adopted_not_reapplied(self):
        _cp.derive()
        pid = self._proposals_for(self.d_behind)[0]["id"]
        # Simulate a crash AFTER apply+marker but BEFORE the final mark:
        self._force_status(pid, "applying")
        conn = _db.get_conn()
        from dashboard import crm as _crm
        _crm._log(conn, self.d_behind, "proposal_applied",
                  {"proposal_id": pid, "kind": "touch"}, source="proposal")
        conn.commit()
        conn.close()
        before = self._touch_state(self.d_behind)
        res = _cp.approve(pid)
        self.assertEqual(res.get("adopted"), True)
        self.assertEqual(self._touch_state(self.d_behind)["touch_count"],
                         before["touch_count"], "adoption must not re-apply")
        self.assertEqual(self._proposals_for(self.d_behind)[0]["status"],
                         "approved")

    def test_applying_without_marker_is_in_flight_error(self):
        _cp.derive()
        pid = self._proposals_for(self.d_behind)[0]["id"]
        self._force_status(pid, "applying")
        res = _cp.approve(pid)
        self.assertEqual(res["status"], "error")
        self.assertEqual(self._proposals_for(self.d_behind)[0]["status"],
                         "applying", "no marker, no adoption: stays in flight")


@unittest.skipUnless(_READY, "needs ~/.hermes/kanban.db to copy")
class ProposalHTTPRoutes(unittest.TestCase):
    """The API wiring: the inbox driven through the HTTP surface
    (Sol F5 — module tests alone don't establish the POST paths work)."""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient
        from dashboard.api import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        from dashboard.migrations.m27_crm_proposals import m27_crm_proposals
        conn = _db.get_conn()
        m27_crm_proposals(conn)
        self.account_id = f"acc_chttp_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
                     (self.account_id, "Cuenta HTTP", _epoch(60)))
        self.deal_id = f"deal_chttp_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "last_touch_date, touch_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.deal_id, self.account_id, "deal http", "proposal", 1000.0,
             "MXN", _days_ago(12), 1, _epoch(60), _epoch(60)))
        conn.execute(
            "INSERT INTO fireflies_meetings (id, deal_id, transcript_id, title, "
            "meeting_date, created_at) VALUES (?,?,?,?,?,?)",
            (f"ffm_{uuid.uuid4().hex[:8]}", self.deal_id, "T-HTTP",
             "Reunión HTTP", _days_ago(5), _epoch(5)))
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = _db.get_conn()
        conn.execute("DELETE FROM crm_proposals WHERE deal_id = ?", (self.deal_id,))
        conn.execute("DELETE FROM fireflies_meetings WHERE deal_id = ?", (self.deal_id,))
        conn.execute("DELETE FROM deal_events WHERE deal_id = ?", (self.deal_id,))
        conn.execute("DELETE FROM deals WHERE id = ?", (self.deal_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (self.account_id,))
        conn.commit()
        conn.close()

    def _mine(self):
        return [p for p in self.client.get("/api/crm/proposals").json()["proposals"]
                if p["deal_id"] == self.deal_id]

    def test_http_roundtrip_derive_list_approve(self):
        r = self.client.post("/api/crm/proposals/derive")
        self.assertEqual(r.status_code, 200)
        mine = self._mine()
        self.assertEqual(len(mine), 1)
        pid = mine[0]["id"]
        r = self.client.post(f"/api/crm/proposals/{pid}/approve")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/crm/proposals/{pid}/approve")
        self.assertEqual(r.status_code, 409, "double approve over HTTP must 409")

    def test_http_dismiss_and_validation(self):
        r = self.client.post("/api/crm/proposals", json={
            "deal_id": self.deal_id, "kind": "amount",
            "payload": {"value": 337500}, "evidence_ref": "gmail:http-quote"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("created"))
        pid = r.json()["id"]
        r = self.client.post(f"/api/crm/proposals/{pid}/dismiss")
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/crm/proposals", json={
            "deal_id": self.deal_id, "kind": "amount",
            "payload": {"value": "mucho"}, "evidence_ref": "gmail:bad"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
