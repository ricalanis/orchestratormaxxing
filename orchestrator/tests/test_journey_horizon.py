"""Contract for la línea de horizonte — Visión V1.

`pulse.horizon()` is the whole of what the three deleted Today blocks (💰 Dinero
· 🤖 Agentes · 🧭 Último brief) were trying to be: six numbers, one line, each
one tap from the surface that can act on it. What it must get right is not "does
it return six integers" but four properties, each of which is a rule the rest of
the system already lives by:

  * **Every count is a RULE, referenced live — never a second opinion.**
    `oportunidades_trabadas` is `pulse.stopper()` applied to the open pipeline
    (the same ladder the pulse and the deal drawer speak); the three
    `entregables` mirror `cadence._precondition_holds`; `hoy_pendientes` mirrors
    `canvas.get_day_plan`'s own `do` predicate. Each is seeded on its own row so
    a failing assertion names the rule that broke.
  * **The six PARTITION.** No fact is counted twice. The two post-win rungs of
    the stopper ladder (`sin entregar`, `factura sin pago`) belong to
    `entregables` and to nothing else, so a won orphan must move `entregables`
    and leave `oportunidades_trabadas` alone — asserted directly, because "the
    line does not add up" is the failure that makes it stop being read.
  * **Fixed structure, at zero too.** An empty world answers with six segments
    that are all zero, in `HORIZON_ORDER`, each still carrying its label, hint
    and deep-link target. A segment that vanished at zero would change the shape
    of the line every morning — the exact cost the line exists to remove.
  * **Every segment carries its target.** The line is only glanceable if a
    number is one tap from acting on it; a count with no destination is a
    statistic. The four target values are pinned to the routes the client
    actually speaks (`?tab=…` for a tab, `#…` for an anchor).

DB isolation: a COPY of the session sandbox per test with `runner.run()` on top
(so the real migrated shape is exercised), then **wiped** to an empty world
before seeding. The wipe is what makes the counts ABSOLUTE rather than deltas
against whatever the operator's CRM happens to hold — and it is what makes the
zero-state test possible at all. `runner.run_backup` is stubbed. The operator's
live DB is never opened.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_journey_horizon.py   # from orchestrator/
"""
import atexit
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Imported outside the availability guard: a missing sandbox DB is a legitimate
# skip, a missing module is the thing under test.
from dashboard import pulse as _pulse

_READY = False
_IMPORT_DB = None
_CLIENT = None
try:
    from dashboard import canvas as _canvas, db as _db, sprints as _sprints
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ the per-session sandbox copy tests/conftest.py exports, never the live DB.
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_horizon_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        from dashboard.api import app
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _REAL_DB
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


@atexit.register
def _cleanup_import_db():  # pragma: no cover
    try:
        if _IMPORT_DB and _IMPORT_DB.exists():
            _IMPORT_DB.unlink()
    except Exception:
        pass


NOW = int(time.time())
DAY = 86400
TODAY = datetime.date.today()

# Three accounts, chosen so `clientes_activos` has to DISCRIMINATE rather than
# count rows: one qualifies through a deal, one through a project, and one
# through neither (its deals are all closed and its projects all delivered).
ACCT_DEAL = "acct_hz_deal"
ACCT_PROJECT = "acct_hz_project"
ACCT_CLOSED = "acct_hz_closed"

PROJ_ACTIVE = "proj_hz_active"
PROJ_ARCHIVED = "proj_hz_archived"     # status active, archived → NOT alive
PROJ_PLANNED = "proj_hz_planned"
PROJ_DELIVERED = "proj_hz_delivered"
PROJ_DELIVERED2 = "proj_hz_delivered2"
PERSONAL = "proj_personal"             # canvas._PERSONAL_PROJECT — excluded from Hoy

DEAL_STUCK_PROPOSAL = "deal_hz_proposal"   # trabada — propuesta sin respuesta
DEAL_STUCK_NOTOUCH = "deal_hz_notouch"     # trabada — sin siguiente toque
DEAL_CLEAN = "deal_hz_clean"               # open, no stopper
DEAL_WON_ORPHAN = "deal_hz_orphan"         # entregable — por entregar
DEAL_WON_UNBILLED = "deal_hz_unbilled"     # entregable — por facturar
DEAL_WON_UNPAID = "deal_hz_unpaid"         # entregable — por cobrar
DEAL_LOST = "deal_hz_lost"                 # an exit — invisible everywhere

# The tables an empty world has to be empty of. Ordered children-first so the
# wipe holds even with foreign keys on.
_WIPE = ("task_events", "task_dispatches", "nurture_sequences", "deal_events",
         "tasks", "deals", "projects", "accounts")


def _iso(days: int = 0) -> str:
    return (TODAY + datetime.timedelta(days=days)).isoformat()


@unittest.skipUnless(_READY, "dashboard modules or the sandbox DB are unavailable")
class _HorizonCase(unittest.TestCase):
    """An EMPTY world per test, migrated to the real shape. Subclasses seed."""

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_horizon_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        runner.run()
        self._wipe()

    def tearDown(self):
        runner.run_backup = self._orig_backup
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.tmp) + suffix).unlink()
            except Exception:
                pass

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        c = sqlite3.connect(str(self.tmp))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = OFF")
        return c

    def _wipe(self):
        c = self._conn()
        try:
            for table in _WIPE:
                try:
                    c.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass          # a table this schema does not have
            c.commit()
        finally:
            c.close()

    def counts(self) -> dict:
        out = _pulse.horizon(TODAY)
        return {k: out[k]["count"] for k in _pulse.HORIZON_ORDER}

    def _seed_world(self):
        """The controlled world every seeded assertion below reads."""
        c = self._conn()
        c.executemany(
            "INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
            [(ACCT_DEAL, "Horizonte Deals SA", NOW),
             (ACCT_PROJECT, "Horizonte Proyecto SA", NOW),
             (ACCT_CLOSED, "Horizonte Cerrada SA", NOW)])

        # id, slug, name, status, account_id, archived_at
        c.executemany(
            "INSERT INTO projects (id, slug, name, status, account_id, kind, "
            "created_at, archived_at) VALUES (?,?,?,?,?,'product',?,?)",
            [(PROJ_ACTIVE, "hz-active", "Plataforma", "active", ACCT_PROJECT, NOW, None),
             (PROJ_ARCHIVED, "hz-archived", "Vieja", "active", ACCT_PROJECT, NOW, NOW),
             (PROJ_PLANNED, "hz-planned", "Idea", "planned", ACCT_PROJECT, NOW, None),
             (PROJ_DELIVERED, "hz-delivered", "Onboarding", "delivered", ACCT_CLOSED, NOW, None),
             (PROJ_DELIVERED2, "hz-delivered2", "Rollout", "delivered", ACCT_CLOSED, NOW, None),
             # The personal bucket exists here only so the Hoy exclusion has a
             # real row to point at; `planned` keeps it out of the proyectos
             # assertions, which are about the status rule and nothing else.
             (PERSONAL, "personal", "Personal", "planned", None, NOW, None)])

        # id, account, stage, project_id, last_touch, next_touch, invoiced, paid
        deals = [
            (DEAL_STUCK_PROPOSAL, ACCT_DEAL, "proposal", None, _iso(-12), _iso(3), None, None),
            (DEAL_STUCK_NOTOUCH, ACCT_DEAL, "engaged", None, _iso(-1), None, None, None),
            (DEAL_CLEAN, ACCT_DEAL, "engaged", None, _iso(-1), _iso(4), None, None),
            (DEAL_WON_ORPHAN, ACCT_CLOSED, "won", None, None, None, None, None),
            (DEAL_WON_UNBILLED, ACCT_CLOSED, "won", PROJ_DELIVERED, None, None, None, None),
            (DEAL_WON_UNPAID, ACCT_CLOSED, "won", PROJ_DELIVERED2, None, None,
             NOW - 20 * DAY, None),
            (DEAL_LOST, ACCT_CLOSED, "lost", None, None, None, None, None),
        ]
        for did, acct, stage, pid, last_t, next_t, inv, paid in deals:
            c.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "created_at, updated_at, closed_at, project_id, last_touch_date, "
                "next_touch_date, invoiced_at, paid_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, acct, f"{did} — trato", stage, 1000.0, "MXN", NOW - 90 * DAY,
                 NOW - DAY, NOW - 5 * DAY, pid, last_t, next_t, inv, paid))

        # id, status, assignee, executor_kind, project_id, planned_for, archived_at
        tasks = [
            # delegando — an agent is holding OPEN work
            ("t_hz_deleg_hermes", "backlog", "hermes", "hermes", PROJ_ACTIVE, None, None),
            ("t_hz_deleg_claude", "in_progress", "claude-code", "claude", PROJ_ACTIVE, None, None),
            # …and the four rows that must NOT count as delegation
            ("t_hz_deleg_done", "done", "claude-code", "claude", PROJ_ACTIVE, None, None),
            ("t_hz_deleg_archived", "backlog", "hermes", "hermes", PROJ_ACTIVE, None, NOW),
            ("t_hz_human", "backlog", "ricardo", "human", PROJ_ACTIVE, None, None),
            ("t_hz_unstamped", "backlog", "ricardo", None, PROJ_ACTIVE, None, None),
            # hoy — open cards on TODAY's plan, human-actor, real project
            ("t_hz_hoy_a", "backlog", "ricardo", None, PROJ_ACTIVE, _iso(0), None),
            ("t_hz_hoy_b", "in_progress", None, None, PROJ_ACTIVE, _iso(0), None),
            # …and the five that must NOT count as today's open plan
            ("t_hz_hoy_done", "done", "ricardo", None, PROJ_ACTIVE, _iso(0), None),
            ("t_hz_hoy_cancelled", "cancelled", "ricardo", None, PROJ_ACTIVE, _iso(0), None),
            ("t_hz_hoy_personal", "backlog", "ricardo", None, PERSONAL, _iso(0), None),
            ("t_hz_hoy_agent", "backlog", "hermes", None, PROJ_ACTIVE, _iso(0), None),
            ("t_hz_hoy_tomorrow", "backlog", "ricardo", None, PROJ_ACTIVE, _iso(1), None),
        ]
        for tid, status, assignee, executor, pid, planned, archived in tasks:
            c.execute(
                "INSERT INTO tasks (id, title, status, created_at, created_by, assignee, "
                "executor_kind, project_id, planned_for, archived_at) "
                "VALUES (?,?,?,?,'ricardo',?,?,?,?,?)",
                (tid, f"{tid} — tarea", status, NOW, assignee, executor, pid,
                 planned, archived))
        c.commit()
        c.close()


# ---------------------------------------------------------------- zero state

class HorizonZeroState(_HorizonCase):
    """An empty world answers with seven zeros — not with six segments."""

    def test_every_segment_is_present_and_zero(self):
        out = _pulse.horizon(TODAY)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["order"], list(_pulse.HORIZON_ORDER))
        # 6 → 7 (m17): the pin was true-not-required; `cobro` is the documented
        # calendar-money exception to the six-action partition (see pulse.py).
        self.assertEqual(len(_pulse.HORIZON_ORDER), 7)
        for key in _pulse.HORIZON_ORDER:
            with self.subTest(key):
                self.assertIn(key, out, "a segment may never be omitted — zero is dimmed, "
                                        "never hidden (the fixed structure IS the point)")
                self.assertEqual(out[key]["count"], 0)

    def test_zero_segments_still_carry_their_label_hint_and_target(self):
        out = _pulse.horizon(TODAY)
        for key in _pulse.HORIZON_ORDER:
            with self.subTest(key):
                seg = out[key]
                self.assertEqual(seg["key"], key)
                self.assertTrue(seg["label"], "a zero segment still names itself")
                self.assertTrue(seg["hint"])
                self.assertTrue(seg["target"], "a count with no destination is a statistic")

    def test_entregables_breaks_down_to_three_named_zeros(self):
        parts = _pulse.horizon(TODAY)["entregables"]["parts"]
        self.assertEqual(parts, {"entregar": 0, "facturar": 0, "cobrar": 0})

    def test_the_date_is_the_day_asked_for(self):
        self.assertEqual(_pulse.horizon(TODAY)["date"], TODAY.isoformat())


# ---------------------------------------------------------------- the six

class HorizonCounts(_HorizonCase):
    """Each of the six, seeded with its own discriminating rows."""

    def setUp(self):
        super().setUp()
        self._seed_world()

    def test_all_six_at_once(self):
        self.assertEqual(self.counts(), {
            "clientes_activos": 2,       # deals-account + project-account, NOT the closed one
            "oportunidades_trabadas": 2,  # proposal sin respuesta + sin siguiente toque
            "proyectos_vivos": 1,        # active; archived-active / planned / delivered are not
            "delegando": 2,              # hermes + claude, open and unarchived
            "entregables": 3,            # entregar + facturar + cobrar, one each
            "hoy_pendientes": 2,         # two open cards on today's human plan
            "cobro": 0,                  # the seeded world carries no payment dates
        })

    # --- Q1 clientes activos ---------------------------------------------
    def test_an_account_qualifies_through_an_open_deal_or_a_live_project(self):
        out = _pulse.horizon(TODAY)
        self.assertEqual(out["clientes_activos"]["count"], 2)
        # The closed account has three won deals, a lost one and two delivered
        # projects — a real client, but not a LIVE one. Counting it would make
        # "clientes activos" mean "clients we have ever had".
        c = self._conn()
        c.execute("UPDATE deals SET stage = 'engaged', next_touch_date = ? WHERE id = ?",
                  (_iso(2), DEAL_LOST))
        c.commit()
        c.close()
        self.assertEqual(_pulse.horizon(TODAY)["clientes_activos"]["count"], 3)

    def test_an_archived_project_does_not_keep_a_client_alive(self):
        c = self._conn()
        c.execute("UPDATE projects SET archived_at = ? WHERE id = ?", (NOW, PROJ_ACTIVE))
        c.commit()
        c.close()
        after = self.counts()
        self.assertEqual(after["clientes_activos"], 1)
        self.assertEqual(after["proyectos_vivos"], 0)

    # --- Q2 oportunidades trabadas ---------------------------------------
    def test_trabadas_is_stopper_applied_to_the_open_pipeline(self):
        """The count IS `pulse.stopper()` — not a second rule that resembles it.

        Giving the untouched deal a next touch removes its stopper, so the
        segment must drop by exactly one. If the horizon had its own copy of the
        ladder, this is where the two would drift apart.
        """
        deal = {"stage": "engaged", "next_touch_date": None}
        self.assertEqual(_pulse.stopper(deal, TODAY), "sin siguiente toque")
        c = self._conn()
        c.execute("UPDATE deals SET next_touch_date = ? WHERE id = ?",
                  (_iso(3), DEAL_STUCK_NOTOUCH))
        c.commit()
        c.close()
        self.assertEqual(self.counts()["oportunidades_trabadas"], 1)

    def test_a_clean_open_deal_is_not_trabada(self):
        c = self._conn()
        stoppers = {r["id"]: _pulse.stopper(dict(r), TODAY)
                    for r in c.execute("SELECT * FROM deals")}
        c.close()
        self.assertIsNone(stoppers[DEAL_CLEAN])
        self.assertEqual(stoppers[DEAL_STUCK_PROPOSAL], "propuesta sin respuesta 12d")

    def test_a_lost_deal_is_an_exit_from_every_segment(self):
        c = self._conn()
        c.execute("DELETE FROM deals WHERE id = ?", (DEAL_LOST,))
        c.commit()
        c.close()
        self.assertEqual(self.counts(), {
            "clientes_activos": 2, "oportunidades_trabadas": 2, "proyectos_vivos": 1,
            "delegando": 2, "entregables": 3, "hoy_pendientes": 2, "cobro": 0,
        })

    def test_the_six_partition_a_won_orphan_is_entregable_not_trabada(self):
        """The load-bearing invariant of a glanceable line: nothing is counted
        twice. `stopper()` fires "sin entregar" on a won orphan — that rung is
        `entregables`' job, and `oportunidades_trabadas` must not double it."""
        c = self._conn()
        row = dict(c.execute("SELECT * FROM deals WHERE id = ?",
                             (DEAL_WON_ORPHAN,)).fetchone())
        c.close()
        self.assertTrue(_pulse.stopper(row, TODAY).startswith("sin entregar"))
        before = self.counts()

        c = self._conn()
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, currency, "
                  "created_at, updated_at, closed_at, project_id) "
                  "VALUES ('deal_hz_orphan2', ?, 'otro huérfano', 'won', 1.0, 'MXN', "
                  "?, ?, ?, NULL)", (ACCT_CLOSED, NOW - DAY, NOW - DAY, NOW - DAY))
        c.commit()
        c.close()
        after = self.counts()
        self.assertEqual(after["entregables"], before["entregables"] + 1)
        self.assertEqual(after["oportunidades_trabadas"],
                         before["oportunidades_trabadas"],
                         "a won orphan belongs to entregables and to nothing else")

    # --- Q3 proyectos vivos ----------------------------------------------
    def test_only_active_and_delivering_projects_are_alive(self):
        self.assertEqual(_pulse.horizon(TODAY)["proyectos_vivos"]["count"], 1)
        c = self._conn()
        # `delivering` is handled ahead of its existence (same as stagekind).
        c.execute("UPDATE projects SET status = 'delivering' WHERE id = ?", (PROJ_PLANNED,))
        c.commit()
        c.close()
        self.assertEqual(_pulse.horizon(TODAY)["proyectos_vivos"]["count"], 2)

    # --- Q4 delegando -----------------------------------------------------
    def test_delegando_counts_open_work_in_an_agents_hands(self):
        self.assertEqual(_pulse.horizon(TODAY)["delegando"]["count"], 2)

    def test_an_unstamped_executor_is_not_a_delegation(self):
        """31 live rows carry `executor_kind IS NULL`. They are unstamped, not
        delegated — counting them would make the segment a measure of schema
        debt rather than of work in flight."""
        c = self._conn()
        c.execute("UPDATE tasks SET executor_kind = NULL WHERE id = ?",
                  ("t_hz_deleg_hermes",))
        c.commit()
        c.close()
        self.assertEqual(self.counts()["delegando"], 1)

    def test_settled_and_archived_delegations_are_not_in_flight(self):
        c = self._conn()
        c.execute("UPDATE tasks SET status = 'done' WHERE id = ?", ("t_hz_deleg_claude",))
        c.commit()
        c.close()
        self.assertEqual(self.counts()["delegando"], 1)

    # --- Q5 entregables ---------------------------------------------------
    def test_entregables_sums_the_three_named_questions(self):
        seg = _pulse.horizon(TODAY)["entregables"]
        self.assertEqual(seg["parts"], {"entregar": 1, "facturar": 1, "cobrar": 1})
        self.assertEqual(seg["count"], 3)
        self.assertEqual(seg["hint"], "por entregar/facturar/cobrar")

    def test_a_paid_invoice_leaves_the_delivery_questions(self):
        c = self._conn()
        c.execute("UPDATE deals SET paid_at = ? WHERE id = ?", (NOW, DEAL_WON_UNPAID))
        c.commit()
        c.close()
        seg = _pulse.horizon(TODAY)["entregables"]
        self.assertEqual(seg["parts"], {"entregar": 1, "facturar": 1, "cobrar": 0})
        self.assertEqual(seg["count"], 2)

    def test_invoicing_moves_a_deal_from_facturar_to_cobrar(self):
        c = self._conn()
        c.execute("UPDATE deals SET invoiced_at = ? WHERE id = ?",
                  (NOW - DAY, DEAL_WON_UNBILLED))
        c.commit()
        c.close()
        self.assertEqual(_pulse.horizon(TODAY)["entregables"]["parts"],
                         {"entregar": 1, "facturar": 0, "cobrar": 2})

    def test_delivering_a_won_orphan_clears_its_entregable(self):
        c = self._conn()
        c.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                  (PROJ_ACTIVE, DEAL_WON_ORPHAN))
        c.commit()
        c.close()
        # The project is `active`, not `delivered`, so it does not become an
        # invoice question either — the money is simply being worked.
        self.assertEqual(_pulse.horizon(TODAY)["entregables"]["parts"],
                         {"entregar": 0, "facturar": 1, "cobrar": 1})

    # --- Q6 hoy -----------------------------------------------------------
    def test_hoy_counts_the_open_cards_on_todays_human_plan(self):
        self.assertEqual(_pulse.horizon(TODAY)["hoy_pendientes"]["count"], 2)

    def test_finishing_a_card_drops_hoy(self):
        c = self._conn()
        c.execute("UPDATE tasks SET status = 'done' WHERE id = ?", ("t_hz_hoy_a",))
        c.commit()
        c.close()
        self.assertEqual(self.counts()["hoy_pendientes"], 1)

    def test_hoy_uses_the_day_plans_own_actor_and_project_predicate(self):
        """The segment sits directly above the plan pane; if it counted a
        different set than `canvas.get_day_plan`'s `do`, the number and the cards
        under it would visibly disagree."""
        plan = _canvas.get_day_plan(TODAY.isoformat())
        open_do = [t for t in plan["do"]
                   if t["status"] not in ("done", "rejected", "cancelled")]
        self.assertEqual(_pulse.horizon(TODAY)["hoy_pendientes"]["count"], len(open_do))
        self.assertEqual({t["id"] for t in open_do}, {"t_hz_hoy_a", "t_hz_hoy_b"})


# ---------------------------------------------------------------- targets

class CobroSegment(_HorizonCase):
    """Q7 (m17) — the calendar-money lens: promised-this-week, paid-this-week,
    and the red overdue suffix; display is the compact pending amount."""

    def _cobro_deal(self, did, *, expected=None, paid=None, value=25000.0):
        c = self._conn()
        c.execute(
            "INSERT INTO accounts (id, name, created_at) VALUES (?, ?, ?)",
            (f"acct_{did}", f"a-{did}", NOW))
        c.execute(
            "INSERT INTO deals (id, account_id, title, stage, value, currency, "
            "created_at, updated_at, closed_at, invoiced_at, paid_at, "
            "expected_payment_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, f"acct_{did}", f"{did} — trato", "won", value, "MXN",
             NOW - 90 * DAY, NOW - DAY, NOW - 5 * DAY, NOW - 10 * DAY, paid,
             expected))
        c.commit()
        c.close()

    def test_zero_state_has_no_display_and_no_alert(self):
        seg = _pulse.horizon(TODAY)["cobro"]
        self.assertEqual(seg["count"], 0)
        self.assertIsNone(seg["display"])
        self.assertIsNone(seg["alert"])
        self.assertEqual(seg["target"], "#today-cobro")

    def test_a_promise_this_week_counts_and_shows_the_compact_amount(self):
        self._cobro_deal("d_hz_cobro_wk", expected=TODAY.isoformat())
        seg = _pulse.horizon(TODAY)["cobro"]
        self.assertEqual(seg["count"], 1)
        self.assertEqual(seg["display"], "$25k")
        self.assertIsNone(seg["alert"])

    def test_a_payment_landed_this_week_counts_without_pending_amount(self):
        self._cobro_deal("d_hz_cobro_paid", paid=NOW)
        seg = _pulse.horizon(TODAY)["cobro"]
        self.assertEqual(seg["count"], 1)
        self.assertIsNone(seg["display"])

    def test_an_overdue_promise_raises_the_red_suffix(self):
        # 10 days past: always before this week's Monday, >= the 3-day gate.
        self._cobro_deal(
            "d_hz_cobro_late",
            expected=(TODAY - datetime.timedelta(days=10)).isoformat())
        seg = _pulse.horizon(TODAY)["cobro"]
        self.assertEqual(seg["alert"], "1 vencido")
        self.assertEqual(seg["count"], 0)

    def test_a_fresh_delay_stays_out_of_the_alert(self):
        # 1-2 days past the promise is bank friction, not an alarm — the
        # amber treatment lives in the block, never in the line.
        self._cobro_deal(
            "d_hz_cobro_soft",
            expected=(TODAY - datetime.timedelta(days=1)).isoformat())
        self.assertIsNone(_pulse.horizon(TODAY)["cobro"]["alert"])


class HorizonTargets(_HorizonCase):
    """Every number is one tap from the surface that can act on it."""

    EXPECTED = {
        "clientes_activos": "?tab=crm",
        "oportunidades_trabadas": "?tab=crm",
        "proyectos_vivos": "?tab=projects",
        "delegando": "?tab=agent-tasks",
        "entregables": "?tab=crm",
        "hoy_pendientes": "#today-plan-wrap",
        "cobro": "#today-cobro",
    }

    def test_each_segment_deep_links_where_the_client_can_route(self):
        out = _pulse.horizon(TODAY)
        self.assertEqual({k: out[k]["target"] for k in _pulse.HORIZON_ORDER},
                         self.EXPECTED)

    def test_a_tab_target_names_a_real_route(self):
        """`?tab=<key>` is only a deep link if the template routes that key —
        ROUTE_TABS in index.html is the list that decides."""
        html = (REPO / "dashboard" / "templates" / "index.html").read_text()
        for key, target in self.EXPECTED.items():
            with self.subTest(key):
                if target.startswith("#"):
                    self.assertIn(f'id="{target[1:]}"', html)
                else:
                    tab = target.split("=", 1)[1]
                    self.assertIn(f"'{tab}'", html)


# ---------------------------------------------------------------- endpoint

class HorizonEndpoint(_HorizonCase):

    def setUp(self):
        super().setUp()
        self._seed_world()

    def test_ok_and_shaped(self):
        r = _CLIENT.get("/api/journey/horizon")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["order"], list(_pulse.HORIZON_ORDER))
        for key in _pulse.HORIZON_ORDER:
            with self.subTest(key):
                self.assertEqual(set(body[key]) >= {"key", "count", "label",
                                                    "hint", "target"}, True)

    def test_the_endpoint_serves_the_composers_own_numbers(self):
        """A wrapper, not a second composer (the same rule the pulse endpoint
        lives by): the HTTP body must equal `pulse.horizon()`."""
        body = _CLIENT.get("/api/journey/horizon").json()
        direct = _pulse.horizon()
        self.assertEqual({k: body[k]["count"] for k in _pulse.HORIZON_ORDER},
                         {k: direct[k]["count"] for k in _pulse.HORIZON_ORDER})

    def test_the_read_is_write_free(self):
        census = self._census()
        _CLIENT.get("/api/journey/horizon")
        _pulse.horizon()
        self.assertEqual(self._census(), census)

    def _census(self) -> tuple:
        c = self._conn()
        try:
            return tuple(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("deals", "tasks", "projects", "accounts"))
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
