"""GET /api/crm/cash-flow contract — the deterministic read behind 💰 Cobro.

Freezes the clock via `?date=` (2030-06-05, a Wednesday; week = Mon 06-03 …
Sun 06-09) and seeds deals in that far-future month so the live-copy sandbox's
real 2026 data cannot collide with a single boundary assertion. What it pins:

  - week boundaries are exact (Sunday in, next Monday out)
  - month never sums a non-won stage or another month's payment
  - days_late is measured against the promised date (1d and 4d rows)
  - leaks use the HONEST query (won + uninvoiced; delivered is a badge, not a
    filter) and carry first-deal ids for deep links
  - narrative severity ranks vencido above ciego
  - slippage reports only at >= 3 reconciled collections
  - the month target is read from icp_config, never invented

Isolation: same convention as test_m17_cash_flow — resolve every read/write
through db.get_conn() so the conftest session sandbox and this module agree.
"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_cashflow_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB

        from dashboard.api import app  # ensure_schema() runs here, on the copy
        from dashboard import crm as _crm
        from dashboard.migrations.m17_cash_flow import m17_cash_flow as _m17
        from dashboard.migrations.m18_invoice_launch import m18_invoice_launch as _m18
        from dashboard.migrations.m19_paid_amount import m19_paid_amount as _m19
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover
    _READY = False

# The frozen clock: Wednesday. Week = Mon 2030-06-03 … Sun 2030-06-09.
_DATE = "2030-06-05"
# Epochs inside 2030-06 / 2030-05 / 2030-04 (12:00 local avoids DST edges).
_EPOCH_JUN_3 = int(datetime.datetime(2030, 6, 3, 12).timestamp())
_EPOCH_MAY_20 = int(datetime.datetime(2030, 5, 20, 12).timestamp())
_EPOCH_APR_10 = int(datetime.datetime(2030, 4, 10, 12).timestamp())


def _conn():
    return _db.get_conn()


def _seed(stage="won", project=None, **cols):
    """One deal (+ its account, + optionally its project) via db.get_conn()."""
    did = f"deal_{_uuid.uuid4().hex[:8]}"
    aid = f"acct_{_uuid.uuid4().hex[:8]}"
    now = 1754000000
    base = {"id": did, "title": f"t-{did[-4:]}", "stage": stage,
            "account_id": aid, "value": 1000.0, "currency": "MXN",
            "created_at": now, "updated_at": now}
    if stage in ("won", "lost"):
        base["closed_at"] = now
    base.update(cols)
    c = _conn()
    try:
        c.execute("INSERT INTO accounts (id, name, created_at) VALUES (?, ?, ?)",
                  (aid, f"a-{aid[-4:]}", now))
        if project:
            pid = f"proj_{_uuid.uuid4().hex[:8]}"
            c.execute(
                "INSERT INTO projects (id, slug, name, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, f"s-{pid[-6:]}", f"p-{pid[-4:]}", project, now))
            base["project_id"] = pid
        keys = ", ".join(base)
        ph = ", ".join("?" * len(base))
        c.execute(f"INSERT INTO deals ({keys}) VALUES ({ph})",
                  tuple(base.values()))
        c.commit()
    finally:
        c.close()
    return did


def _get(date=_DATE):
    r = _CLIENT.get(f"/api/crm/cash-flow?date={date}")
    assert r.status_code == 200, r.text
    return r.json()


def _row_ids(rows):
    return {r["deal_id"] for r in rows}


class _Base(unittest.TestCase):
    """Re-ensure m17 on the current DB resolution (same reason as
    test_m17_cash_flow._Base: the conftest re-points the global after this
    module's import-time redirect)."""

    def setUp(self):
        c = _conn()
        try:
            _m17(c)
            _m18(c)
            _m19(c)
            c.commit()
        finally:
            c.close()


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class WeekWindow(_Base):

    def test_sunday_is_in_and_next_monday_is_out(self):
        d_sun = _seed(invoiced_at=_EPOCH_JUN_3,
                      expected_payment_date="2030-06-09", value=111.0)
        d_mon = _seed(invoiced_at=_EPOCH_JUN_3,
                      expected_payment_date="2030-06-10", value=222.0)
        res = _get()
        self.assertEqual(res["week"]["start"], "2030-06-03")
        self.assertEqual(res["week"]["end"], "2030-06-09")
        rows = {r["deal_id"]: r for r in res["week"]["rows"]}
        self.assertIn(d_sun, rows)
        self.assertNotIn(d_mon, rows)
        # A future promise inside the window is ON TIME: days_late exactly 0.
        self.assertEqual(rows[d_sun]["days_late"], 0)
        self.assertEqual(
            sum(r["value"] for r in res["week"]["rows"]
                if r["kind"] == "payment" and not r["paid"]),
            res["week"]["total"])

    def test_a_payment_landed_this_week_shows_as_a_paid_row(self):
        # Deliberately promise-free: a reconciled (paid+promised) seed here
        # would poison the Slippage class's baseline probe.
        did = _seed(invoiced_at=_EPOCH_MAY_20, paid_at=_EPOCH_JUN_3)
        res = _get()
        row = [r for r in res["week"]["rows"] if r["deal_id"] == did]
        self.assertEqual(len(row), 1)
        self.assertTrue(row[0]["paid"])
        self.assertEqual(row[0]["days_late"], 0)   # settled money is never late
        # Paid rows never inflate the pending total.
        self.assertNotIn(did, {r["deal_id"] for r in res["overdue"]})


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class DaysLate(_Base):

    def test_days_late_measures_against_the_promise(self):
        d1 = _seed(invoiced_at=_EPOCH_JUN_3,
                   expected_payment_date="2030-06-04")   # 1d late
        d4 = _seed(invoiced_at=_EPOCH_MAY_20,
                   expected_payment_date="2030-06-01")   # 4d late
        res = _get()
        by_id = {r["deal_id"]: r for r in res["overdue"]}
        self.assertEqual(by_id[d1]["days_late"], 1)
        self.assertEqual(by_id[d4]["days_late"], 4)
        # Sorted worst-first, and the 1d row also sits in the week strip
        # (its promise falls inside the window) while the 4d row does not.
        late = [r["days_late"] for r in res["overdue"]]
        self.assertEqual(late, sorted(late, reverse=True))
        self.assertIn(d1, _row_ids(res["week"]["rows"]))
        self.assertNotIn(d4, _row_ids(res["week"]["rows"]))


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class MonthStanding(_Base):

    def test_month_counts_only_won_money_of_this_month(self):
        # A month of its own (2031-03): every class shares ONE sandbox, so an
        # absolute sum is only deterministic in a window no other case seeds.
        mar_3 = int(datetime.datetime(2031, 3, 3, 12).timestamp())
        feb_20 = int(datetime.datetime(2031, 2, 20, 12).timestamp())
        d_in = _seed(invoiced_at=feb_20, paid_at=mar_3, value=500.0)
        d_prev = _seed(invoiced_at=feb_20, paid_at=feb_20, value=900.0)
        d_pend = _seed(invoiced_at=mar_3,
                       expected_payment_date="2031-03-20", value=300.0)
        d_prop = _seed(stage="proposal",
                       expected_payment_date="2031-03-15", value=7777.0)
        res = _get(date="2031-03-05")
        self.assertEqual(res["month"]["label"], "MAR")
        self.assertEqual(res["month"]["collected"], 500.0)
        self.assertEqual(res["month"]["expected"], 800.0)
        self.assertNotIn(d_prop, _row_ids(res["week"]["rows"]))
        self.assertNotIn(d_prop, _row_ids(res["overdue"]))
        self.assertNotIn(d_prev, _row_ids(res["week"]["rows"]))
        self.assertIn(d_in, _row_ids(res["week"]["rows"]))

    def test_the_target_is_read_from_icp_config_never_invented(self):
        c = _conn()
        try:
            c.execute("INSERT OR REPLACE INTO icp_config (key, value, updated_at) "
                      "VALUES ('target_revenue', '424242', 1754000000)")
            c.commit()
        finally:
            c.close()
        self.assertEqual(_get()["month"]["target"], 424242.0)


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Leaks(_Base):

    def test_leaks_use_the_honest_query_delivered_is_a_badge_not_a_filter(self):
        # Delivered project, still uninvoiced → the canonical m11 queue would
        # show it, but so must the honest one; and an ORPHAN won (no project at
        # all) must leak too — that is the $200,500 blind spot this view exists
        # to expose.
        d_delivered = _seed(project="delivered")
        d_orphan = _seed()
        res = _get()
        leaks = res["leaks"]
        self.assertGreaterEqual(leaks["uninvoiced_count"], 2)
        self.assertIsNotNone(leaks["first_uninvoiced_deal_id"])
        self.assertIsNotNone(leaks["first_no_project_deal_id"])
        self.assertGreaterEqual(leaks["no_project_count"], 1)
        row = [r for r in res["no_expected"] if r["deal_id"] == d_delivered]
        self.assertEqual(len(row), 1)
        self.assertTrue(row[0]["delivered"])
        self.assertIn(d_orphan, {r["deal_id"] for r in res["no_expected"]})

    def test_a_paid_deal_never_counts_as_an_invoicing_leak(self):
        did = _seed(invoiced_at=_EPOCH_MAY_20, paid_at=_EPOCH_JUN_3)
        res = _get()
        self.assertNotIn(did, {res["leaks"]["first_uninvoiced_deal_id"]})
        self.assertNotIn(did, {r["deal_id"] for r in res["no_expected"]})


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Narrative(_Base):

    def test_vencido_outranks_ciego(self):
        # The sandbox's live won deals are already "blind" (no dates); an
        # overdue promise must still win the single narrative slot. The claim
        # is the RANKING, not the champion: other modules share this sandbox
        # and may have seeded older promises, so assert severity + shape, and
        # that our seed is present among the overdue rows.
        did = _seed(invoiced_at=_EPOCH_APR_10, expected_payment_date="2030-05-20",
                    title="Vencidísimo", value=18500.0)
        res = _get()
        self.assertEqual(res["narrative"]["severity"], "overdue")
        self.assertIn("venció hace", res["narrative"]["text"])
        self.assertIn(did, {r["deal_id"] for r in res["overdue"]})

    def test_without_overdue_the_blind_spot_speaks(self):
        # A clock BEFORE any promise any module ever seeds (2020): promises
        # age into overdue, so the clean window is deep in the past. Seed our
        # OWN blind deal (won, uninvoiced, dateless) so the case stays true
        # even after the operator's real backfill cleans the live copy.
        _seed(title="Ciego propio")
        res = _get(date="2020-01-08")
        self.assertEqual(res["overdue"], [])
        self.assertEqual(res["narrative"]["severity"], "blind")


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Slippage(_Base):

    def test_slippage_reports_only_from_three_reconciled(self):
        # Probe the RECONCILED count directly (the API hides it below the
        # gate, so `slippage: None` cannot distinguish 0 from 2).
        c = _conn()
        try:
            already = c.execute(
                "SELECT COUNT(*) FROM deals WHERE LOWER(stage)='won' "
                "AND paid_at IS NOT NULL "
                "AND expected_payment_date IS NOT NULL").fetchone()[0]
        finally:
            c.close()
        if already:
            self.skipTest("sandbox already carries reconciled collections")
        # Two reconciled → still silent.
        for _ in range(2):
            _seed(invoiced_at=_EPOCH_MAY_20, paid_at=_EPOCH_JUN_3,
                  expected_payment_date="2030-05-30")
        self.assertIsNone(_crm.cash_flow(date=_DATE)["slippage"])
        # The third unlocks it: paid 2030-06-03 vs promised 05-30 → +4d each.
        _seed(invoiced_at=_EPOCH_MAY_20, paid_at=_EPOCH_JUN_3,
              expected_payment_date="2030-05-30")
        s = _crm.cash_flow(date=_DATE)["slippage"]
        self.assertIsNotNone(s)
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["median_days"], 4)

    def test_a_bad_date_param_is_a_400(self):
        r = _CLIENT.get("/api/crm/cash-flow?date=garbage")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class Launches(_Base):
    """m18 — 🧾 launch rows: their own kind on the calendar, never money."""

    def test_launch_rows_are_their_own_kind_and_never_inflate_the_total(self):
        # Clock 2033-05-04 (Wed); week Mon 05-02 … Sun 05-08 — its own window.
        may2 = int(datetime.datetime(2033, 5, 2, 12).timestamp())
        d_l = _seed(expected_invoice_date="2033-05-06", value=999.0)
        d_p = _seed(invoiced_at=may2, expected_payment_date="2033-05-06",
                    value=400.0)
        res = _get(date="2033-05-04")
        rows = {r["deal_id"]: r for r in res["week"]["rows"]}
        self.assertEqual(rows[d_l]["kind"], "launch")
        self.assertEqual(rows[d_l]["date"], "2033-05-06")
        self.assertEqual(rows[d_p]["kind"], "payment")
        self.assertEqual(res["week"]["total"], 400.0,
                         "a launch is an action, not incoming money")

    def test_an_invoiced_deal_stops_launching(self):
        may2 = int(datetime.datetime(2033, 5, 2, 12).timestamp())
        did = _seed(invoiced_at=may2, expected_invoice_date="2033-05-06")
        res = _get(date="2033-05-04")
        self.assertNotIn(did, {r["deal_id"] for r in res["week"]["rows"]
                               if r["kind"] == "launch"})

    def test_an_overdue_launch_leaks_with_its_days_late(self):
        did = _seed(expected_invoice_date="2033-04-28")   # 6d late at 05-04
        res = _get(date="2033-05-04")
        row = [r for r in res["launch_overdue"] if r["deal_id"] == did]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["days_late"], 6)
        self.assertGreaterEqual(res["leaks"]["launch_overdue_count"], 1)
        self.assertIsNotNone(res["leaks"]["first_launch_overdue_deal_id"])

    def test_launch_window_boundaries_and_the_one_day_edge(self):
        # Its own week (2035-07-04, a Wednesday; Mon 07-02 … Sun 07-08).
        d_mon = _seed(expected_invoice_date="2035-07-02")   # Monday: in
        d_sun = _seed(expected_invoice_date="2035-07-08")   # Sunday: in
        d_next = _seed(expected_invoice_date="2035-07-09")  # next Monday: out
        d_1d = _seed(expected_invoice_date="2035-07-03")    # exactly 1d late
        res = _get(date="2035-07-04")
        launches = {r["deal_id"]: r for r in res["week"]["rows"]
                    if r["kind"] == "launch"}
        self.assertIn(d_mon, launches)
        self.assertIn(d_sun, launches)
        self.assertNotIn(d_next, launches)
        # On-time rows carry days_late 0; the 1-day slip carries exactly 1
        # and already sits in launch_overdue.
        self.assertEqual(launches[d_sun]["days_late"], 0)
        self.assertEqual(launches[d_1d]["days_late"], 1)
        late = {r["deal_id"]: r for r in res["launch_overdue"]}
        self.assertIn(d_1d, late)
        self.assertEqual(late[d_1d]["days_late"], 1)
        self.assertNotIn(d_sun, late)

    def test_launch_overdue_sorts_worst_first_and_names_the_worst(self):
        # Its own clock (2036-03-05): the only overdue launches are these
        # two, seeded NEWEST-FIRST so raw insertion order is the wrong order
        # and only the days_late sort can produce worst-first.
        d_new = _seed(expected_invoice_date="2036-03-03")
        d_old = _seed(expected_invoice_date="2036-02-20", title="Viejísimo")
        res = _get(date="2036-03-05")
        mine = [r for r in res["launch_overdue"]
                if r["deal_id"] in (d_old, d_new)]
        self.assertEqual([r["deal_id"] for r in mine], [d_old, d_new])
        late = [r["days_late"] for r in res["launch_overdue"]]
        self.assertEqual(late, sorted(late, reverse=True))
        self.assertEqual(res["leaks"]["first_launch_overdue_deal_id"],
                         res["launch_overdue"][0]["deal_id"])

    def test_the_week_calendar_sorts_by_date_with_paid_last(self):
        # Its own week (2037-09-08 Wed; Mon 09-06 … Sun 09-12).
        sep6 = int(datetime.datetime(2037, 9, 6, 12).timestamp())
        sep7 = int(datetime.datetime(2037, 9, 7, 12).timestamp())
        d_late_pay = _seed(invoiced_at=sep6, expected_payment_date="2037-09-11")
        d_early_launch = _seed(expected_invoice_date="2037-09-07")
        d_paid = _seed(invoiced_at=sep6, paid_at=sep7)
        res = _get(date="2037-09-08")
        rows = [r for r in res["week"]["rows"]
                if r["deal_id"] in (d_late_pay, d_early_launch, d_paid)]
        self.assertEqual([r["deal_id"] for r in rows],
                         [d_early_launch, d_late_pay, d_paid],
                         "calendar order, with settled money at the end")

    def test_a_malformed_stored_launch_date_cannot_crash_the_read(self):
        _seed(expected_invoice_date="garbage")
        res = _get(date="2035-07-04")
        self.assertEqual(res["status"], "ok")


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class MonthMoney(_Base):
    """m19 — facturado del mes + efectivo REAL (COALESCE(paid_amount, value))."""

    def test_month_shows_facturado_and_real_cash(self):
        feb1 = int(datetime.datetime(2034, 2, 1, 12).timestamp())
        feb7 = int(datetime.datetime(2034, 2, 7, 12).timestamp())  # in-week
        _seed(invoiced_at=feb1, value=800.0)                       # unpaid
        _seed(invoiced_at=feb1, paid_at=feb7, value=1000.0,
              paid_amount=900.0)                                   # real 900
        res = _get(date="2034-02-08")
        self.assertEqual(res["month"]["invoiced"], 1800.0)
        self.assertEqual(res["month"]["collected"], 900.0)
        row = [r for r in res["week"]["rows"] if r["paid"]][0]
        self.assertEqual(row["cash"], 900.0)


@unittest.skipUnless(_READY, "no live kanban.db to copy — module skipped")
class LaunchNarrative(_Base):

    def test_a_slipped_launch_speaks_when_no_client_money_is_late(self):
        # Clock 2021-01-07 — before every payment promise any module seeds
        # (2026+), so the only overdue thing is OUR slipped launch.
        _seed(expected_invoice_date="2021-01-04", title="Lanzamiento propio")
        res = _get(date="2021-01-07")
        self.assertEqual(res["overdue"], [])
        self.assertEqual(res["narrative"]["severity"], "launch")
        self.assertIn("Lanzamiento propio", res["narrative"]["text"])
