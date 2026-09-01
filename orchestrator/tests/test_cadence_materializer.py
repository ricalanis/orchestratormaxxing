"""Contract for the cadence materializer — journey fase 1, step 5 + ADICIÓN 8.

`dashboard/cadence.py` turns commercial facts (nurture steps, won orphans,
invoices) into TASKS, and closes the loop when those tasks are finished. It runs
unattended, so almost every assertion here is about it doing NOTHING when it
should: not minting twice, not talking over a human's card, not re-minting what
the operator rejected, not nagging a lost deal.

Four of these are RED against pre-step-5 code by construction, and each one is
red for a reason that was measured, not assumed:

  * **the loop closure** — a done deal-task touched nothing. The step stayed
    `pending` forever, `sent_at` did not exist, `touch_count` never moved. So
    `crm.get_cadence_status`'s compliance arithmetic (`sent AND sent_at`) was
    incapable of a nonzero answer on any deal, ever.
  * **`record_touch`'s flat +7d** — `growth.py:1477` stamped
    `today + next_in_days` unconditionally, over the sequence's own next date.
    Since `set_nurture_status` calls it on every sent step, the deal card and
    the nurture panel disagreed about the next touch from step 2 onward.
  * **closed-deal hygiene** — nothing cancelled a lost deal's pending steps, so
    the materializer's first morning would have minted a "Romper el hielo con …"
    card for each of the 12 lost deals on the board.
  * **`plan_candidates`' fourth source** — a task that named a deal was
    invisible to the day planner: the composer had three sources and none of
    them was commercial.

DB isolation: a COPY of the session sandbox per test with `runner.run()` on top,
so the REAL migrated shape (m06 + m07-m11) is exercised rather than a hand-rolled
one. `runner.run_backup` is stubbed; the hermes CLI is never invoked (the
materializer writes tasks directly — it is one of the three sanctioned
`tasks.deal_id` writers, ruling 5). The operator's live DB is never opened.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_cadence_materializer.py  # from orchestrator/
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

_READY = False
_IMPORT_DB = None
try:
    from dashboard import db as _db, sprints as _sprints, crm as _crm
    from dashboard import cadence as _cadence, canvas as _canvas, growth as _growth
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ the per-session sandbox copy tests/conftest.py exports, never the live DB.
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_cadence_import_", suffix=".db")
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

ACCOUNT = "acct_cad"
DEAL_OPEN = "deal_cad_open"       # engaged, has a nurture sequence
DEAL_WON_ORPHAN = "deal_cad_won"  # won, no delivering project
DEAL_BILL = "deal_cad_bill"       # won, delivered project — the ADICIÓN 8 case
PROJECT = "proj_cad_delivery"


def _iso(d):
    return d.isoformat()


@unittest.skipUnless(_READY, "dashboard modules or the sandbox DB are unavailable")
class _CadenceCase(unittest.TestCase):

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_cadence_test_", suffix=".db")
        os.close(fd)
        shutil.copy(_REAL_DB, tmp)
        self.tmp = Path(tmp)
        self._orig_db, self._orig_sprints = _db.KANBAN_DB, _sprints.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp
        self._orig_backup = runner.run_backup
        runner.run_backup = lambda: None
        runner.run()
        self._seed()

    def tearDown(self):
        runner.run_backup = self._orig_backup
        _db.KANBAN_DB, _sprints.KANBAN_DB = self._orig_db, self._orig_sprints
        try:
            self.tmp.unlink()
        except Exception:
            pass

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        c = sqlite3.connect(str(self.tmp))
        c.row_factory = sqlite3.Row
        return c

    def _seed(self):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO accounts (id, name, created_at) VALUES (?,?,?)",
                  (ACCOUNT, "Cadencia Co", NOW))
        c.execute("INSERT INTO projects (id, slug, name, status, account_id, kind, created_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (PROJECT, "cad-delivery", "Cadencia Delivery", "delivered",
                   ACCOUNT, "product", NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, currency, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (DEAL_OPEN, ACCOUNT, "Cadencia — piloto", "engaged", 50000.0,
                   "MXN", NOW, NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, currency, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (DEAL_WON_ORPHAN, ACCOUNT, "Cadencia — huérfano", "won", 194500.0,
                   "MXN", NOW, NOW))
        c.execute("INSERT INTO deals (id, account_id, title, stage, value, currency, "
                  "project_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (DEAL_BILL, ACCOUNT, "Cadencia — entregado", "won", 120000.0,
                   "MXN", PROJECT, NOW, NOW))
        c.commit()
        c.close()

    def _add_step(self, nid, deal_id, number, touch_type, offset_days,
                  status="pending"):
        c = self._conn()
        c.execute(
            "INSERT INTO nurture_sequences (id, deal_id, step_number, touch_type, "
            "template_text, scheduled_date, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (nid, deal_id, number, touch_type, f"Plantilla {number}",
             _iso(TODAY + datetime.timedelta(days=offset_days)), status, NOW))
        c.commit()
        c.close()

    def _tasks(self, deal_id, open_only=True):
        c = self._conn()
        try:
            sql = ("SELECT id, title, status, body, stage_kind, due_date, project_id, "
                   "created_by, autonomy FROM tasks WHERE deal_id = ?")
            if open_only:
                sql += " AND status NOT IN ('done','rejected','cancelled')"
            return [dict(r) for r in c.execute(sql, (deal_id,))]
        finally:
            c.close()

    def _cadence_tasks(self, deal_id, open_only=True):
        return [t for t in self._tasks(deal_id, open_only)
                if t["created_by"] == "cadence"]

    def _deal(self, deal_id):
        c = self._conn()
        try:
            return dict(c.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())
        finally:
            c.close()

    def _steps(self, deal_id):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM nurture_sequences WHERE deal_id = ? ORDER BY step_number",
                (deal_id,))]
        finally:
            c.close()

    def _deal_events(self, deal_id, kind=None):
        c = self._conn()
        try:
            sql = "SELECT * FROM deal_events WHERE deal_id = ?"
            params = [deal_id]
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            return [dict(r) for r in c.execute(sql + " ORDER BY id", params)]
        finally:
            c.close()


# ===========================================================================
# 1. The migrations this step stands on
# ===========================================================================

class MigrationFloor(_CadenceCase):

    def test_m07_adds_the_ledger_columns_and_the_unique_backref(self):
        c = self._conn()
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(nurture_sequences)")}
            self.assertIn("sent_at", cols)
            self.assertIn("task_id", cols)
            idx = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='nurture_sequences'")}
            self.assertIn("idx_nurture_task", idx)
        finally:
            c.close()

    def test_two_steps_cannot_claim_one_task(self):
        """m07's UNIQUE partial index, in the storage engine (ruling 7)."""
        self._add_step("nur_a", DEAL_OPEN, 1, "trigger", 0)
        self._add_step("nur_b", DEAL_OPEN, 2, "action", 2)
        c = self._conn()
        try:
            c.execute("INSERT INTO tasks (id, title, status, created_at, created_by, "
                      "project_id, deal_id) VALUES ('t_claim','x','backlog',?,?,?,?)",
                      (NOW, "cadence", "proj_ventas", DEAL_OPEN))
            c.execute("UPDATE nurture_sequences SET task_id = 't_claim' WHERE id = 'nur_a'")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE nurture_sequences SET task_id = 't_claim' "
                          "WHERE id = 'nur_b'")
        finally:
            c.close()

    def test_m08_adds_provenance_and_log_writes_it(self):
        c = self._conn()
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(deal_events)")}
            self.assertIn("source", cols)
            self.assertIn("channel", cols)
        finally:
            c.close()
        conn = _db.get_conn()
        try:
            _crm._log(conn, DEAL_OPEN, "touch", {"note": "x"},
                      source="cadence", channel="whatsapp")
            conn.commit()
        finally:
            conn.close()
        ev = self._deal_events(DEAL_OPEN, "touch")[-1]
        self.assertEqual(ev["source"], "cadence")
        self.assertEqual(ev["channel"], "whatsapp")

    def test_m09_creates_the_hidden_sales_lane(self):
        c = self._conn()
        try:
            row = c.execute(
                "SELECT id, kind FROM projects WHERE slug = 'ventas'").fetchone()
            self.assertIsNotNone(row, "proj_ventas must exist after m09")
            self.assertEqual(row["kind"], "sales")
        finally:
            c.close()
        # The VERIFIED hiding mechanism (sprints._week_bucket_tasks) must exclude
        # it, or cadence cards leak into the delivery backlog.
        self.assertNotIn("sales", _sprints.get_icebox_tasks.__doc__ or "")  # doc-free check
        c = self._conn()
        try:
            c.execute("INSERT INTO tasks (id, title, status, created_at, created_by, "
                      "project_id) VALUES ('t_lane','lane card','backlog',?,?, "
                      "(SELECT id FROM projects WHERE slug='ventas'))",
                      (NOW, "cadence"))
            c.commit()
        finally:
            c.close()
        ids = {t["id"] for t in _sprints.get_icebox_tasks()}
        self.assertNotIn("t_lane", ids,
                         "a proj_ventas card must not appear in the delivery backlog")

    def test_m11_adds_the_billing_timestamps(self):
        c = self._conn()
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(deals)")}
            self.assertIn("invoiced_at", cols)
            self.assertIn("paid_at", cols)
        finally:
            c.close()


# ===========================================================================
# 2. Minting: once, and only the next step
# ===========================================================================

class Minting(_CadenceCase):

    def test_mints_the_next_due_human_step_once(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        self._add_step("nur_2", DEAL_OPEN, 2, "action", 3)
        res = _cadence.reconcile()
        self.assertEqual(res["status"], "ok")
        open_cards = self._cadence_tasks(DEAL_OPEN)
        self.assertEqual(len(open_cards), 1, "exactly ONE card, never the sequence")
        card = open_cards[0]
        self.assertIn("Cadencia Co", card["title"])
        self.assertEqual(card["due_date"], _iso(TODAY),
                         "due_date IS the step's scheduled_date — no third clock")
        self.assertEqual(card["autonomy"], "ask")
        self.assertEqual(card["stage_kind"], "contacto",
                         "derived from the deal's stage (engaged), not guessed")
        c = self._conn()
        try:
            pid = c.execute("SELECT id FROM projects WHERE slug='ventas'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(card["project_id"], pid)
        # The backref m07's index protects.
        step = [s for s in self._steps(DEAL_OPEN) if s["id"] == "nur_1"][0]
        self.assertEqual(step["task_id"], card["id"])

    def test_a_second_run_mints_nothing_new(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        _cadence.reconcile()
        first = {t["id"] for t in self._cadence_tasks(DEAL_OPEN)}
        _cadence.reconcile()
        second = {t["id"] for t in self._cadence_tasks(DEAL_OPEN)}
        self.assertEqual(first, second, "reconcile must be idempotent")
        self.assertEqual(len(second), 1)

    def test_a_step_scheduled_beyond_the_horizon_is_not_minted(self):
        self._add_step("nur_far", DEAL_OPEN, 1, "trigger", 9)
        _cadence.reconcile()
        self.assertEqual(self._cadence_tasks(DEAL_OPEN), [])

    def test_an_open_manual_task_blocks_minting_and_is_never_closed(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        c = self._conn()
        c.execute("INSERT INTO tasks (id, title, status, created_at, created_by, "
                  "project_id, deal_id) VALUES ('t_manual','Llamar yo','in_progress',?,?,?,?)",
                  (NOW, "ricardo", PROJECT, DEAL_OPEN))
        c.commit()
        c.close()
        res = _cadence.reconcile()
        self.assertIn(DEAL_OPEN, res["blocked_by_manual"])
        self.assertEqual(self._cadence_tasks(DEAL_OPEN), [])
        still = [t for t in self._tasks(DEAL_OPEN) if t["id"] == "t_manual"]
        self.assertEqual(len(still), 1, "a human's card is never closed by cadence")
        self.assertEqual(still[0]["status"], "in_progress")

    def test_the_storage_engine_refuses_a_second_open_cadence_card(self):
        """m06's partial UNIQUE index covers the ADICIÓN 8 cards too — they are
        `created_by='cadence'`, which is the discriminator it keys on."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        _cadence.reconcile()
        c = self._conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO tasks (id, title, status, created_at, "
                          "created_by, project_id, deal_id) VALUES "
                          "('t_dup','dup','backlog',?,?,?,?)",
                          (NOW, "cadence", "proj_ventas", DEAL_OPEN))
        finally:
            c.close()

    def test_automated_steps_log_an_event_and_mint_nothing(self):
        self._add_step("nur_auto", DEAL_OPEN, 1, "drip", 0)
        self._add_step("nur_human", DEAL_OPEN, 2, "action", 1)
        res = _cadence.reconcile()
        self.assertIn("nur_auto", res["automated_steps"])
        steps = {s["id"]: s for s in self._steps(DEAL_OPEN)}
        self.assertEqual(steps["nur_auto"]["status"], "sent")
        self.assertEqual(steps["nur_auto"]["sent_at"], _iso(TODAY))
        self.assertIsNone(steps["nur_auto"]["task_id"],
                          "an automated step never becomes a card")
        touches = [e for e in self._deal_events(DEAL_OPEN, "touch")]
        self.assertTrue(any(e["channel"] == "drip" for e in touches))
        # …and the NEXT (human) step is the one that got a card.
        cards = self._cadence_tasks(DEAL_OPEN)
        self.assertEqual(len(cards), 1)
        self.assertEqual(steps["nur_human"]["task_id"], cards[0]["id"])


# ===========================================================================
# 3. The won orphan (🚚) and sticky archive
# ===========================================================================

class WonOrphan(_CadenceCase):

    def test_a_won_deal_with_no_project_gets_one_deliver_card(self):
        _cadence.reconcile()
        cards = self._cadence_tasks(DEAL_WON_ORPHAN)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertIn("🚚 Entregar", card["title"])
        self.assertIn("$194,500", card["title"])
        self.assertEqual(card["stage_kind"], "entrega")
        self.assertIn(f"?entity=deal:{DEAL_WON_ORPHAN}&action=deliver", card["body"])

    def test_delivering_the_deal_closes_the_card(self):
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_WON_ORPHAN)[0]
        c = self._conn()
        c.execute("UPDATE deals SET project_id = ? WHERE id = ?",
                  (PROJECT, DEAL_WON_ORPHAN))
        c.commit()
        c.close()
        res = _cadence.reconcile()
        self.assertIn(card["id"], res["closed"])
        c = self._conn()
        try:
            st = c.execute("SELECT status FROM tasks WHERE id = ?",
                           (card["id"],)).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(st, "cancelled",
                         "the deliver card must not survive the delivery")
        # PROJECT is already `delivered`, so the deal legitimately moves on to
        # the next station of the cycle — one card, and it is the invoice, not a
        # second 🚚. The one-open-per-deal rule holds ACROSS kinds.
        open_cards = self._cadence_tasks(DEAL_WON_ORPHAN)
        self.assertEqual(len(open_cards), 1)
        self.assertIn("[cadence:invoice]", open_cards[0]["body"])

    def test_a_rejected_card_is_never_minted_again(self):
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_WON_ORPHAN)[0]
        c = self._conn()
        c.execute("UPDATE tasks SET status = 'rejected' WHERE id = ?", (card["id"],))
        c.commit()
        c.close()
        res = _cadence.reconcile()
        self.assertIn(DEAL_WON_ORPHAN, res["sticky_skipped"])
        self.assertEqual(self._cadence_tasks(DEAL_WON_ORPHAN), [],
                         "a rejection is a verdict, not a snooze button")

    def test_refusing_a_TOUCH_card_silences_that_step_only(self):
        """Sticky is DEAL-scoped for 🚚/💵/📩 and STEP-scoped for a nurture card.

        Rejecting "Romper el hielo con X" is a verdict on the opener, not on the
        client — the sequence must continue with step 2, and the refused step
        must stop being the deal's advertised next touch."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        self._add_step("nur_2", DEAL_OPEN, 2, "action", 1)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]
        c = self._conn()
        c.execute("UPDATE tasks SET status = 'rejected' WHERE id = ?", (card["id"],))
        c.commit()
        c.close()

        res = _cadence.reconcile()
        self.assertNotIn(DEAL_OPEN, res["sticky_skipped"],
                         "a nurture card is step-scoped, never deal-scoped")
        cards = self._cadence_tasks(DEAL_OPEN)
        self.assertEqual(len(cards), 1)
        steps = {s["id"]: s for s in self._steps(DEAL_OPEN)}
        self.assertEqual(steps["nur_1"]["status"], "skipped",
                         "the refused step stops being the advertised next touch")
        self.assertEqual(steps["nur_1"]["task_id"], card["id"],
                         "the backref survives as the audit of WHAT was refused")
        self.assertEqual(steps["nur_2"]["task_id"], cards[0]["id"])
        self.assertEqual(self._deal(DEAL_OPEN)["next_touch_date"],
                         _iso(TODAY + datetime.timedelta(days=1)))

    def test_an_archived_card_is_never_minted_again(self):
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_WON_ORPHAN)[0]
        c = self._conn()
        c.execute("UPDATE tasks SET archived_at = ? WHERE id = ?", (NOW, card["id"]))
        c.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (card["id"],))
        c.commit()
        c.close()
        _cadence.reconcile()
        self.assertEqual(self._cadence_tasks(DEAL_WON_ORPHAN), [])


# ===========================================================================
# 4. ADICIÓN 8 — facturación y cobranza
# ===========================================================================

class Billing(_CadenceCase):

    def test_a_delivered_uninvoiced_deal_gets_a_facturar_card(self):
        _cadence.reconcile()
        cards = self._cadence_tasks(DEAL_BILL)
        self.assertEqual(len(cards), 1)
        self.assertIn("Facturar Cadencia Co", cards[0]["title"])
        self.assertIn("$120,000", cards[0]["title"])
        self.assertEqual(cards[0]["stage_kind"], "facturacion",
                         "minted-only: stagekind.derive REFUSES to conclude it")

    def test_marking_invoiced_closes_facturar_and_arms_cobranza(self):
        _cadence.reconcile()
        factura = self._cadence_tasks(DEAL_BILL)[0]
        res = _crm.mark_deal_invoiced(DEAL_BILL)
        self.assertEqual(res["status"], "deal_invoiced")
        # Fresh invoice → nothing to chase yet: the card closes, none replaces it.
        _cadence.reconcile()
        self.assertEqual(self._cadence_tasks(DEAL_BILL), [])
        c = self._conn()
        try:
            st = c.execute("SELECT status FROM tasks WHERE id = ?",
                           (factura["id"],)).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(st, "cancelled")

    def test_an_invoice_older_than_the_grace_period_gets_a_cobranza_card(self):
        aged = NOW - (_cadence.COLLECTION_GRACE_DAYS + 1) * DAY
        c = self._conn()
        c.execute("UPDATE deals SET invoiced_at = ? WHERE id = ?", (aged, DEAL_BILL))
        c.commit()
        c.close()
        _cadence.reconcile()
        cards = self._cadence_tasks(DEAL_BILL)
        self.assertEqual(len(cards), 1)
        self.assertIn("Seguimiento de cobro Cadencia Co", cards[0]["title"])
        self.assertIn("factura hace", cards[0]["title"])
        self.assertEqual(cards[0]["stage_kind"], "cobranza")

    def test_paid_closes_the_cobranza_card_immediately(self):
        aged = NOW - (_cadence.COLLECTION_GRACE_DAYS + 1) * DAY
        c = self._conn()
        c.execute("UPDATE deals SET invoiced_at = ? WHERE id = ?", (aged, DEAL_BILL))
        c.commit()
        c.close()
        _cadence.reconcile()
        self.assertEqual(len(self._cadence_tasks(DEAL_BILL)), 1)
        res = _crm.mark_deal_paid(DEAL_BILL)
        self.assertEqual(res["status"], "deal_paid")
        self.assertEqual(self._cadence_tasks(DEAL_BILL), [],
                         "the card must go the instant the money is recorded")

    def test_paid_before_invoiced_is_refused(self):
        res = _crm.mark_deal_paid(DEAL_BILL)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "not_invoiced")
        self.assertIsNone(self._deal(DEAL_BILL)["paid_at"])

    def test_only_one_open_billing_card_per_deal(self):
        aged = NOW - (_cadence.COLLECTION_GRACE_DAYS + 1) * DAY
        c = self._conn()
        c.execute("UPDATE deals SET invoiced_at = ? WHERE id = ?", (aged, DEAL_BILL))
        c.commit()
        c.close()
        _cadence.reconcile()
        _cadence.reconcile()
        _cadence.reconcile()
        self.assertEqual(len(self._cadence_tasks(DEAL_BILL)), 1)

    def test_the_billing_verbs_are_idempotent(self):
        _crm.mark_deal_invoiced(DEAL_BILL)
        again = _crm.mark_deal_invoiced(DEAL_BILL)
        self.assertEqual(again["status"], "already_deal_invoiced")


# ===========================================================================
# 5. The loop closure  (RED against pre-step-5 code)
# ===========================================================================

class LoopClosure(_CadenceCase):

    def test_finishing_the_card_sends_the_step_and_moves_the_counters(self):
        """RED-PROOF: at HEAD~ a done deal-task touched nothing at all — the step
        stayed pending, `sent_at` did not exist, touch_count did not move."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        self._add_step("nur_2", DEAL_OPEN, 2, "action", 4)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]

        before = self._deal(DEAL_OPEN)["touch_count"] or 0
        out = _sprints.set_task_status(card["id"], "done")
        self.assertEqual(out.get("result"), "updated")

        steps = {s["id"]: s for s in self._steps(DEAL_OPEN)}
        self.assertEqual(steps["nur_1"]["status"], "sent")
        self.assertEqual(steps["nur_1"]["sent_at"], _iso(TODAY))

        deal = self._deal(DEAL_OPEN)
        self.assertEqual(deal["touch_count"], before + 1)
        self.assertEqual(deal["last_touch_date"], _iso(TODAY))
        self.assertEqual(deal["next_touch_date"],
                         _iso(TODAY + datetime.timedelta(days=4)),
                         "recomputed from the ledger, never invented")

        touch = [e for e in self._deal_events(DEAL_OPEN, "touch")][-1]
        self.assertEqual(touch["source"], "cadence")
        self.assertEqual(touch["channel"], "trigger")

    def test_compliance_becomes_arithmetically_possible(self):
        """`get_cadence_status` reads `sent_at`; without m07 it could only be 0."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]
        _sprints.set_task_status(card["id"], "done")
        status = _crm.get_cadence_status(DEAL_OPEN)
        self.assertEqual(status["compliance"], 1.0)

    def test_no_new_card_is_minted_mid_afternoon(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        self._add_step("nur_2", DEAL_OPEN, 2, "action", 1)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]
        _sprints.set_task_status(card["id"], "done")
        self.assertEqual(self._cadence_tasks(DEAL_OPEN), [],
                         "finishing a card must not immediately grow the board")

    def test_a_task_with_no_step_is_untouched(self):
        c = self._conn()
        c.execute("INSERT INTO tasks (id, title, status, created_at, created_by, "
                  "project_id) VALUES ('t_plainish','plain','backlog',?,?,?)",
                  (NOW, "ricardo", PROJECT))
        c.commit()
        c.close()
        out = _sprints.set_task_status("t_plainish", "done")
        self.assertNotIn("cadence", out)


# ===========================================================================
# 6. Closed-deal hygiene  (RED against pre-step-5 code)
# ===========================================================================

class ClosedDealHygiene(_CadenceCase):

    def test_losing_a_deal_cancels_its_pending_steps_and_clears_next_touch(self):
        """RED-PROOF: at HEAD~ the steps SURVIVED the close, so the materializer's
        first morning would have nagged all 12 lost deals."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        self._add_step("nur_2", DEAL_OPEN, 2, "action", 3)
        c = self._conn()
        c.execute("UPDATE deals SET next_touch_date = ? WHERE id = ?",
                  (_iso(TODAY), DEAL_OPEN))
        c.commit()
        c.close()

        res = _crm.update_deal(DEAL_OPEN, stage="lost", lost_reason="price")
        self.assertEqual(res["status"], "updated")

        self.assertTrue(all(s["status"] == "skipped" for s in self._steps(DEAL_OPEN)))
        self.assertIsNone(self._deal(DEAL_OPEN)["next_touch_date"])

    def test_winning_a_deal_also_stops_the_nurture(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        _crm.update_deal(DEAL_OPEN, stage="won")
        self.assertTrue(all(s["status"] == "skipped" for s in self._steps(DEAL_OPEN)))

    def test_closing_the_deal_closes_the_open_card_on_the_next_pass(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]
        _crm.update_deal(DEAL_OPEN, stage="lost", lost_reason="timing")
        res = _cadence.reconcile()
        self.assertIn(card["id"], res["closed"])
        self.assertEqual(self._cadence_tasks(DEAL_OPEN), [])

    def test_a_lost_deal_is_never_minted_for(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        _crm.update_deal(DEAL_OPEN, stage="lost", lost_reason="bad_fit")
        _cadence.reconcile()
        self.assertEqual(self._cadence_tasks(DEAL_OPEN), [])


# ===========================================================================
# 7. record_touch stops inventing a second clock  (RED against pre-step-5)
# ===========================================================================

class RecordTouchRecomputes(_CadenceCase):

    def test_next_touch_comes_from_the_ledger_not_from_plus_seven_days(self):
        """RED-PROOF: at HEAD~ `growth.record_touch` stamped `today + 7` over the
        sequence's own next date, so the deal card and the nurture panel
        disagreed from step 2 onward."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", -1, status="sent")
        self._add_step("nur_2", DEAL_OPEN, 2, "action", 3)
        out = _growth.record_touch(DEAL_OPEN, note="llamada")
        self.assertEqual(out["next_touch_date"],
                         _iso(TODAY + datetime.timedelta(days=3)))
        self.assertNotEqual(out["next_touch_date"],
                            _iso(TODAY + datetime.timedelta(days=7)))

    def test_a_deal_with_no_ledger_keeps_the_legacy_nudge(self):
        out = _growth.record_touch(DEAL_OPEN, note="ad hoc", next_in_days=7)
        self.assertEqual(out["next_touch_date"],
                         _iso(TODAY + datetime.timedelta(days=7)))

    def test_a_spent_ledger_yields_no_next_touch_rather_than_a_fiction(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", -2, status="sent")
        out = _growth.record_touch(DEAL_OPEN, note="última")
        self.assertIsNone(out["next_touch_date"])

    def test_reconcile_never_clears_a_hand_set_next_touch_on_a_ledgerless_deal(self):
        c = self._conn()
        c.execute("UPDATE deals SET next_touch_date = ? WHERE id = ?",
                  (_iso(TODAY + datetime.timedelta(days=5)), DEAL_OPEN))
        c.commit()
        c.close()
        _cadence.reconcile()
        self.assertEqual(self._deal(DEAL_OPEN)["next_touch_date"],
                         _iso(TODAY + datetime.timedelta(days=5)))


# ===========================================================================
# 8. The day planner's fourth source  (RED against pre-step-5)
# ===========================================================================

class PlanCandidatesClientSource(_CadenceCase):

    def test_a_deal_task_is_a_candidate_labelled_cliente(self):
        """RED-PROOF: at HEAD~ `plan_candidates` had three sources and none of
        them was commercial, so a card that named a client was invisible to the
        one surface the operator opens at 08:00."""
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 1)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]
        res = _canvas.plan_candidates(_iso(TODAY))
        mine = [c for c in res["candidates"] if c["id"] == card["id"]]
        self.assertEqual(len(mine), 1, "the client card must reach the planner")
        self.assertEqual(mine[0]["why"], "cliente")

    def test_urgency_wins_the_label_when_the_card_is_also_overdue(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", -3)
        _cadence.reconcile()
        card = self._cadence_tasks(DEAL_OPEN)[0]
        res = _canvas.plan_candidates(_iso(TODAY))
        mine = [c for c in res["candidates"] if c["id"] == card["id"]]
        self.assertEqual(len(mine), 1, "never duplicated across sources")
        self.assertEqual(mine[0]["why"], "overdue")


# ===========================================================================
# 9. The HTTP edge
# ===========================================================================

class Endpoints(_CadenceCase):

    def setUp(self):
        super().setUp()
        self._orig_import = _db.KANBAN_DB
        _db.KANBAN_DB = _sprints.KANBAN_DB = self.tmp

    def test_reconcile_endpoint_runs_a_pass(self):
        self._add_step("nur_1", DEAL_OPEN, 1, "trigger", 0)
        r = _CLIENT.post("/api/cadence/reconcile", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(len(self._cadence_tasks(DEAL_OPEN)), 1)

    def test_billing_verbs_are_reachable_and_ordered(self):
        r = _CLIENT.post(f"/api/crm/deals/{DEAL_BILL}/paid")
        self.assertEqual(r.status_code, 400)
        r = _CLIENT.post(f"/api/crm/deals/{DEAL_BILL}/invoiced")
        self.assertEqual(r.status_code, 200, r.text)
        r = _CLIENT.post(f"/api/crm/deals/{DEAL_BILL}/paid")
        self.assertEqual(r.status_code, 200, r.text)
        deal = self._deal(DEAL_BILL)
        self.assertIsNotNone(deal["invoiced_at"])
        self.assertIsNotNone(deal["paid_at"])

    def test_billing_verbs_are_absent_from_the_mcp_surface(self):
        """Human-only by structure, not by convention (spec red line 11)."""
        src = (REPO / "mcp_server.py").read_text()
        self.assertNotIn("mark_deal_invoiced", src)
        self.assertNotIn("mark_deal_paid", src)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
