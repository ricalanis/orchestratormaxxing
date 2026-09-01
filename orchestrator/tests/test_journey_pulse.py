"""Contract for the journey pulse — fase 1 step 7 + directiva ADICIÓN 9.

`dashboard/pulse.py` is the composer behind BOTH new frontends: the dashboard's
`GET /api/journey/pulse?ref=` and the three MCP verbs that make this server the
contextual layer for the four hosts. What it must get right is not "does it
return data" but four properties, each of which is a rule the rest of the system
already lives by:

  * **It never guesses which client you meant** (ruling 1). Two substring
    matches are a typed `ambiguous` carrying candidates, never a best guess. The
    ONE tie-break is match strength (an exact id/name beats a substring), and
    that is asserted in both directions: exact wins, two non-exact refuse.
  * **Every stopper is a rule, not a judgement.** The four cases are table-
    driven against a pure function, including the two orderings that deviate
    from the literal spec (invoiced-before-no-touch, and no-touch only in the
    open pipeline) — both of which are *unreachability* fixes, so a test that
    did not pin them would let the bug back in silently.
  * **A task's stage is DERIVED when NULL** (Ley 1 — nothing to fill in) and an
    explicit `stage_kind` still wins. Both are asserted on the same seeded
    spine, along with the tasks the rule cannot place, which must land in
    `unstaged_tasks` rather than being dropped: a hidden task makes the count
    lie.
  * **`propose_deliver` writes NOTHING** (ruling 3). Asserted as a sha256 of the
    whole database file before and after the call (with a WAL checkpoint on both
    sides so the digest is a fact about the data, not about when SQLite chose to
    flush), plus an independent row census. "Read-only" is a property that must
    be measured; a comment saying so is not evidence.

DB isolation: a COPY of the session sandbox per test with `runner.run()` on top,
so the real migrated shape (m06 stage_kind + m10 attachments + m11 billing) is
exercised rather than a hand-rolled one. `runner.run_backup` is stubbed. The
operator's live DB is never opened.

Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_journey_pulse.py   # from orchestrator/
"""
import atexit
import datetime
import hashlib
import json
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
_MCP = None
try:
    from dashboard import db as _db, sprints as _sprints, attachments as _att
    from dashboard.migrations import runner

    _REAL_DB = Path(os.environ["HERMES_KANBAN_DB"]) if os.environ.get("HERMES_KANBAN_DB") \
        else Path.home() / ".hermes" / "kanban.db"
    # ^ the per-session sandbox copy tests/conftest.py exports, never the live DB.
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_pulse_import_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _IMPORT_DB = Path(_tmp)
        _db.KANBAN_DB = _sprints.KANBAN_DB = _IMPORT_DB
        runner.run_backup = lambda: None
        from dashboard.api import app
        from starlette.testclient import TestClient
        import mcp_server as _MCP

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

ACCOUNT = "acct_pulse"
ACCOUNT_NAME = "PulseNet Co"
# The four stopper cases, one deal each — so a failing assertion names the rule.
DEAL_ORPHAN = "deal_pulse_orphan"     # won, no delivering project
DEAL_PROPOSAL = "deal_pulse_proposal"  # proposal, untouched 12d
DEAL_NOTOUCH = "deal_pulse_notouch"   # engaged, no next_touch_date
DEAL_BILLED = "deal_pulse_billed"     # won + delivered, invoiced 12d, unpaid
DEAL_LOST = "deal_pulse_lost"         # an exit from the cycle — never a stopper
PROJECT_ACTIVE = "proj_pulse_active"
PROJECT_SHIPPED = "proj_pulse_shipped"


def _iso(days: int = 0) -> str:
    return (TODAY + datetime.timedelta(days=days)).isoformat()


@unittest.skipUnless(_READY, "dashboard modules or the sandbox DB are unavailable")
class _PulseCase(unittest.TestCase):

    def setUp(self):
        fd, tmp = tempfile.mkstemp(prefix="kanban_pulse_test_", suffix=".db")
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
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.tmp) + suffix).unlink()
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
                  (ACCOUNT, ACCOUNT_NAME, NOW))
        # Two accounts that share a prefix and NOTHING else: the ambiguity case.
        c.executemany("INSERT OR REPLACE INTO accounts (id, name, created_at) VALUES (?,?,?)",
                      [("acct_pulse_twin_n", "PulseTwin North", NOW),
                       ("acct_pulse_twin_s", "PulseTwin South", NOW),
                       ("acct_pulse_zeta", "Zeta Pulse Holdings", NOW)])
        c.executemany(
            "INSERT INTO projects (id, slug, name, status, account_id, kind, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(PROJECT_ACTIVE, "pulse-active", "PulseNet Plataforma", "active",
              ACCOUNT, "product", NOW),
             (PROJECT_SHIPPED, "pulse-shipped", "PulseNet Onboarding", "delivered",
              ACCOUNT, "product", NOW)])

        deals = [
            # id, title, stage, value, project_id, closed_at, last_touch,
            # next_touch, invoiced_at, paid_at
            (DEAL_ORPHAN, "PulseNet — retainer", "won", 194500.0, None,
             NOW - 5 * DAY, None, None, None, None),
            (DEAL_PROPOSAL, "PulseNet — propuesta anual", "proposal", 120000.0, None,
             None, _iso(-12), _iso(3), None, None),
            (DEAL_NOTOUCH, "PulseNet — piloto", "engaged", 40000.0, None,
             None, _iso(-2), None, None, None),
            (DEAL_BILLED, "PulseNet — onboarding", "won", 80000.0, PROJECT_SHIPPED,
             NOW - 30 * DAY, None, None, NOW - 12 * DAY, None),
            (DEAL_LOST, "PulseNet — extensión", "lost", 10000.0, None,
             NOW - 60 * DAY, None, None, None, None),
            # Same account, a deal whose title CONTAINS the account name — the
            # reason the exact-match tie-break exists at all.
            ("deal_pulse_zeta", "Zeta Pulse Holdings retainer", "engaged", 1000.0,
             None, None, _iso(-1), _iso(4), None, None),
        ]
        for (did, title, stage, value, pid, closed, last_touch, next_touch,
             invoiced, paid) in deals:
            account = "acct_pulse_zeta" if did == "deal_pulse_zeta" else ACCOUNT
            c.execute(
                "INSERT INTO deals (id, account_id, title, stage, value, currency, "
                "created_at, updated_at, closed_at, project_id, last_touch_date, "
                "next_touch_date, invoiced_at, paid_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, account, title, stage, value, "MXN", NOW - 90 * DAY,
                 NOW - 1 * DAY, closed, pid, last_touch, next_touch, invoiced, paid))

        tasks = [
            # id, title, status, project_id, deal_id, stage_kind, planned_for
            ("t_pulse_contacto", "Romper el hielo con PulseNet", "backlog",
             None, DEAL_NOTOUCH, None, None),
            ("t_pulse_formal", "Mandar la propuesta anual", "in_progress",
             None, DEAL_PROPOSAL, None, TODAY.isoformat()),
            ("t_pulse_ejecucion", "Construir el panel de PulseNet", "backlog",
             PROJECT_ACTIVE, None, None, None),
            ("t_pulse_entrega", "Entregar el onboarding", "backlog",
             PROJECT_SHIPPED, None, None, None),
            ("t_pulse_factura", "Facturar el onboarding", "backlog",
             None, DEAL_BILLED, "facturacion", None),
            ("t_pulse_cobranza", "Cobrar la factura del onboarding", "backlog",
             None, DEAL_BILLED, "cobranza", None),
            # Derives to None (a lost deal is an exit, not a position) → unstaged.
            ("t_pulse_unstaged", "Archivar material de la extensión", "backlog",
             None, DEAL_LOST, None, None),
            # Must NEVER appear: settled work and parked work.
            ("t_pulse_done", "Kickoff (hecho)", "done", PROJECT_ACTIVE, None, None, None),
            ("t_pulse_cancelled", "Card retirada", "cancelled", PROJECT_ACTIVE,
             None, None, None),
        ]
        for tid, title, status, pid, did, kind, planned in tasks:
            c.execute(
                "INSERT INTO tasks (id, title, status, created_at, created_by, "
                "project_id, deal_id, stage_kind, planned_for) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, title, status, NOW, "ricardo", pid, did, kind, planned))

        # Human vs machine events on the same deal — the whitelist's whole point.
        c.executemany(
            "INSERT INTO deal_events (deal_id, kind, payload, created_at) VALUES (?,?,?,?)",
            [(DEAL_PROPOSAL, "meeting", "{}", NOW - 3 * DAY),
             (DEAL_PROPOSAL, "touch", "{}", NOW - 2 * DAY),
             (DEAL_PROPOSAL, "scored", "{}", NOW - 1 * DAY),
             (DEAL_BILLED, "deal_invoiced", "{}", NOW - 12 * DAY),
             (DEAL_BILLED, "delivered_link", "{}", NOW - 29 * DAY)])
        c.commit()
        c.close()

        _att.add_attachment("project", PROJECT_SHIPPED, "plan", "Plan profundo",
                            path="/home/operator/dev/planning/pulse/plan.md")
        _att.add_attachment("project", PROJECT_SHIPPED, "resource", "Carpeta Drive",
                            url="https://drive.example/pulse")
        _att.add_attachment("project", PROJECT_ACTIVE, "plan", "Plan de plataforma",
                            path="/home/operator/dev/planning/pulse/plataforma.md")

    def _digest(self) -> str:
        """sha256 of the DB file, after flushing WAL frames into it.

        The checkpoint runs on BOTH sides so the digest measures the data, not
        the moment SQLite happened to flush — without it a read-only call could
        'change' the file simply by being the last connection to close.
        """
        c = sqlite3.connect(str(self.tmp))
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        return hashlib.sha256(self.tmp.read_bytes()).hexdigest()

    def _census(self) -> tuple:
        c = self._conn()
        try:
            return tuple(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("deals", "tasks", "projects", "deal_events",
                                   "attachments"))
        finally:
            c.close()


# ---------------------------------------------------------------- stoppers

class StopperRules(unittest.TestCase):
    """The ladder, table-driven against the pure function. No DB, no clock."""

    CASES = [
        ("won-orphan",
         {"stage": "won", "project_id": None, "closed_at": NOW - 5 * DAY},
         "sin entregar 5d"),
        ("won-orphan-uses-updated-when-never-closed",
         {"stage": "won", "project_id": None, "updated_at": NOW - 3 * DAY},
         "sin entregar 3d"),
        ("proposal-cold",
         {"stage": "proposal", "last_touch_date": _iso(-12), "next_touch_date": _iso(2)},
         "propuesta sin respuesta 12d"),
        ("proposal-never-touched-falls-back-to-created",
         {"stage": "proposal", "created_at": NOW - 20 * DAY, "next_touch_date": _iso(1)},
         "propuesta sin respuesta 20d"),
        ("proposal-touched-recently-is-clean",
         {"stage": "proposal", "last_touch_date": _iso(-2), "next_touch_date": _iso(5)},
         None),
        ("proposal-at-the-boundary-is-clean",   # >7d, not >=7d
         {"stage": "proposal", "last_touch_date": _iso(-7), "next_touch_date": _iso(1)},
         None),
        ("invoiced-unpaid",
         {"stage": "won", "project_id": "p", "invoiced_at": NOW - 12 * DAY},
         "factura sin pago 12d"),
        ("invoiced-unpaid-boundary-is-clean",
         {"stage": "won", "project_id": "p", "invoiced_at": NOW - 10 * DAY},
         None),
        ("invoiced-and-paid-is-clean",
         {"stage": "won", "project_id": "p", "invoiced_at": NOW - 40 * DAY,
          "paid_at": NOW - 30 * DAY},
         None),
        ("no-next-touch",
         {"stage": "engaged", "next_touch_date": None},
         "sin siguiente toque"),
        ("has-next-touch-is-clean",
         {"stage": "engaged", "next_touch_date": _iso(3)},
         None),
        ("lost-is-an-exit-not-a-stopper",
         {"stage": "lost", "next_touch_date": None},
         None),
        ("stalled-is-an-exit-not-a-stopper",
         {"stage": "stalled", "next_touch_date": None},
         None),
        # The two deviations, pinned. Both are UNREACHABILITY fixes: in the
        # literal spec order every invoiced deal matched "sin siguiente toque"
        # first (only a won deal can be invoiced, and winning clears the touch
        # clock), so no unpaid invoice could ever surface — and a delivered,
        # paid, closed deal was permanently nagged for a next touch it must not
        # have.
        ("won-and-unpaid-beats-no-next-touch",
         {"stage": "won", "project_id": "p", "next_touch_date": None,
          "invoiced_at": NOW - 30 * DAY},
         "factura sin pago 30d"),
        ("won-delivered-and-paid-has-no-stopper",
         {"stage": "won", "project_id": "p", "next_touch_date": None,
          "invoiced_at": NOW - 40 * DAY, "paid_at": NOW - 20 * DAY},
         None),
    ]

    def test_ladder(self):
        for name, deal, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(_pulse.stopper(deal, TODAY), expected)


# ---------------------------------------------------------------- compose

class ComposeSpine(_PulseCase):
    """The payload, over a seeded spine."""

    def test_account_pulse_groups_open_tasks_by_stage(self):
        out = _pulse.compose(ACCOUNT_NAME)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["entity"], {"kind": "account", "id": ACCOUNT,
                                         "name": ACCOUNT_NAME})
        by = out["tasks_by_stage"]
        # Every stage key is always present — an empty stage is [], not missing.
        self.assertEqual(set(by), {"contacto", "formalizacion", "ejecucion",
                                   "entrega", "facturacion", "cobranza"})
        titles = {k: [t["title"] for t in v] for k, v in by.items()}
        self.assertIn("Romper el hielo con PulseNet", titles["contacto"])
        self.assertIn("Mandar la propuesta anual", titles["formalizacion"])
        self.assertIn("Construir el panel de PulseNet", titles["ejecucion"])
        self.assertIn("Entregar el onboarding", titles["entrega"])
        # Minted-only stages: present ONLY because a writer stamped them.
        self.assertIn("Facturar el onboarding", titles["facturacion"])
        self.assertIn("Cobrar la factura del onboarding", titles["cobranza"])

    def test_stage_is_derived_when_stored_stage_kind_is_null(self):
        """Ley 1: the four derived stages above are stored as NULL. If the
        composer were reading the column instead of deriving, they would all
        fall into `unstaged_tasks`."""
        c = self._conn()
        try:
            stored = {r["id"]: r["stage_kind"] for r in c.execute(
                "SELECT id, stage_kind FROM tasks WHERE id LIKE 't_pulse_%'")}
        finally:
            c.close()
        for tid in ("t_pulse_contacto", "t_pulse_formal", "t_pulse_ejecucion",
                    "t_pulse_entrega"):
            self.assertIsNone(stored[tid], f"{tid} must be seeded with a NULL stage")
        out = _pulse.compose(ACCOUNT_NAME)
        placed = {t["id"] for items in out["tasks_by_stage"].values() for t in items}
        self.assertTrue({"t_pulse_contacto", "t_pulse_formal", "t_pulse_ejecucion",
                         "t_pulse_entrega"} <= placed)

    def test_unplaceable_task_is_kept_not_dropped(self):
        out = _pulse.compose(ACCOUNT_NAME)
        self.assertIn("t_pulse_unstaged", {t["id"] for t in out["unstaged_tasks"]})

    def test_settled_and_parked_work_is_absent(self):
        out = _pulse.compose(ACCOUNT_NAME)
        every = {t["id"] for items in out["tasks_by_stage"].values() for t in items}
        every |= {t["id"] for t in out["unstaged_tasks"]}
        self.assertNotIn("t_pulse_done", every)
        self.assertNotIn("t_pulse_cancelled", every)

    def test_today_lists_only_what_is_planned_for_today(self):
        out = _pulse.compose(ACCOUNT_NAME)
        self.assertEqual(out["today"], ["Mandar la propuesta anual"])
        formal = out["tasks_by_stage"]["formalizacion"]
        self.assertTrue(any(t["planned_today"] for t in formal))
        contacto = out["tasks_by_stage"]["contacto"]
        self.assertFalse(any(t["planned_today"] for t in contacto))

    def test_every_stopper_rule_surfaces_on_its_deal(self):
        out = _pulse.compose(ACCOUNT_NAME)
        stoppers = {d["id"]: d["stopper"] for d in out["deals"]}
        self.assertEqual(stoppers[DEAL_ORPHAN], "sin entregar 5d")
        self.assertEqual(stoppers[DEAL_PROPOSAL], "propuesta sin respuesta 12d")
        self.assertEqual(stoppers[DEAL_NOTOUCH], "sin siguiente toque")
        self.assertEqual(stoppers[DEAL_BILLED], "factura sin pago 12d")
        self.assertIsNone(stoppers[DEAL_LOST])

    def test_recent_events_are_human_only_and_capped(self):
        out = _pulse.compose(ACCOUNT_NAME)
        kinds = [e["kind"] for e in out["recent_events"]]
        self.assertLessEqual(len(kinds), 5)
        self.assertIn("touch", kinds)
        self.assertIn("meeting", kinds)
        self.assertNotIn("scored", kinds)          # machine chatter
        self.assertNotIn("deal_invoiced", kinds)   # machine chatter

    def test_attachment_counts_sum_over_the_projects_in_scope(self):
        out = _pulse.compose(ACCOUNT_NAME)
        self.assertEqual(out["attachments_summary"]["plans"], 2)
        self.assertEqual(out["attachments_summary"]["resources"], 1)
        self.assertEqual(out["attachments_summary"]["conversations"], 0)

    def test_account_with_two_projects_has_no_singular_project(self):
        out = _pulse.compose(ACCOUNT_NAME)
        self.assertIsNone(out["project"], "picking one of two would be a guess")
        self.assertEqual({p["id"] for p in out["projects"]},
                         {PROJECT_ACTIVE, PROJECT_SHIPPED})

    def test_deal_pulse_scopes_to_that_deal_and_its_project(self):
        out = _pulse.compose(DEAL_BILLED)
        self.assertEqual(out["entity"]["kind"], "deal")
        self.assertEqual([d["id"] for d in out["deals"]], [DEAL_BILLED])
        self.assertEqual(out["project"]["id"], PROJECT_SHIPPED)
        self.assertEqual(out["project"]["status"], "delivered")
        self.assertIsInstance(out["project"]["progress"], int)
        self.assertEqual(out["account"]["name"], ACCOUNT_NAME)

    def test_project_pulse_finds_the_deals_it_delivers(self):
        out = _pulse.compose("pulse-shipped")       # by slug
        self.assertEqual(out["entity"]["kind"], "project")
        self.assertEqual([d["id"] for d in out["deals"]], [DEAL_BILLED])
        titles = [t["title"] for t in out["tasks_by_stage"]["entrega"]]
        self.assertIn("Entregar el onboarding", titles)

    def test_url_is_the_canonical_deep_link(self):
        out = _pulse.compose(ACCOUNT_NAME)
        self.assertEqual(out["url"], f"{_db.dashboard_url()}/?entity=account:{ACCOUNT}")


class ComposeResolution(_PulseCase):
    """Ruling 1, both directions: refuse ties, honour exactness."""

    def test_ambiguous_returns_candidates_and_never_guesses(self):
        out = _pulse.compose("PulseTwin")
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["code"], "ambiguous")
        names = {c["name"] for c in out["candidates"]}
        self.assertEqual(names, {"PulseTwin North", "PulseTwin South"})
        self.assertNotIn("entity", out)

    def test_ambiguous_across_kinds_is_still_ambiguous(self):
        """'Zeta' is a substring of both an account and a deal, and neither is
        exact — so the pulse refuses rather than preferring a kind."""
        out = _pulse.compose("Zeta")
        self.assertEqual(out["code"], "ambiguous")
        self.assertEqual({c["kind"] for c in out["candidates"]}, {"account", "deal"})

    def test_exact_name_beats_a_substring_match_in_another_kind(self):
        out = _pulse.compose("Zeta Pulse Holdings")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["entity"]["kind"], "account")
        self.assertEqual(out["entity"]["id"], "acct_pulse_zeta")

    def test_not_found_is_typed(self):
        out = _pulse.compose("no-such-client-anywhere-xyz")
        self.assertEqual(out["code"], "not_found")

    def test_empty_ref_is_not_found_not_a_crash(self):
        self.assertEqual(_pulse.compose("")["code"], "not_found")


class RenderText(_PulseCase):
    """The Spanish block agents relay: task-first, stages as sections."""

    def test_task_first_stage_sections_and_links(self):
        text = _pulse.render(_pulse.compose(ACCOUNT_NAME))
        self.assertLess(text.index("TAREAS"), text.index("DEALS"),
                        "the pulse must lead with the work, not the money")
        for label in ("contacto", "formalización", "ejecución", "entrega",
                      "facturación", "cobranza"):
            self.assertIn(label, text)
        self.assertIn("⛔ sin entregar 5d", text)
        self.assertIn("← hoy", text)
        self.assertIn(f"{_db.dashboard_url()}/?entity=deal:{DEAL_ORPHAN}", text)
        # Spanish agreement: a human reads this on a phone ("1 recursos" is a tell
        # that a machine wrote it and nobody looked).
        self.assertIn("ADJUNTOS: 2 planes · 1 recurso · 0 conversaciones", text)

    def test_ambiguous_renders_as_a_question_not_a_choice(self):
        text = _pulse.render(_pulse.compose("PulseTwin"))
        self.assertIn("Ambiguo", text)
        self.assertIn("PulseTwin North", text)
        self.assertIn("PulseTwin South", text)


# ---------------------------------------------------------------- endpoint

class PulseEndpoint(_PulseCase):

    def test_ok(self):
        r = _CLIENT.get("/api/journey/pulse", params={"ref": ACCOUNT_NAME})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["entity"]["id"], ACCOUNT)
        self.assertIn("tasks_by_stage", body)

    def test_ambiguous_is_400_with_candidates(self):
        r = _CLIENT.get("/api/journey/pulse", params={"ref": "PulseTwin"})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "ambiguous")
        self.assertEqual(len(detail["candidates"]), 2)

    def test_unknown_ref_is_404(self):
        r = _CLIENT.get("/api/journey/pulse", params={"ref": "no-such-client-xyz"})
        self.assertEqual(r.status_code, 404)

    def test_missing_ref_is_422(self):
        self.assertEqual(_CLIENT.get("/api/journey/pulse").status_code, 422)


# ---------------------------------------------------------------- MCP verbs

class McpJourneyTools(_PulseCase):

    def test_pulse_tool_returns_json_and_the_relayable_text(self):
        out = json.loads(_MCP.TOOL_HANDLERS["get_journey_pulse"]({"ref": ACCOUNT_NAME}))
        self.assertEqual(out["status"], "ok")
        self.assertIn("tasks_by_stage", out)
        self.assertIn("TAREAS", out["text"])
        self.assertIn("Romper el hielo con PulseNet", out["text"])

    def test_pulse_tool_relays_a_typed_refusal(self):
        out = json.loads(_MCP.TOOL_HANDLERS["get_journey_pulse"]({"ref": "PulseTwin"}))
        self.assertEqual(out["code"], "ambiguous")
        self.assertIn("Ambiguo", out["text"])

    def test_project_hub_tool_resolves_a_name(self):
        out = json.loads(_MCP.TOOL_HANDLERS["get_project_hub"](
            {"project_ref": "PulseNet Onboarding"}))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["project"]["id"], PROJECT_SHIPPED)
        self.assertEqual(out["facets"]["plans"]["count"], 1)
        self.assertEqual(out["facets"]["resources"]["count"], 1)
        self.assertIn("open", out["facets"]["tasks"])

    def test_project_hub_tool_refuses_an_ambiguous_name(self):
        out = json.loads(_MCP.TOOL_HANDLERS["get_project_hub"]({"project_ref": "PulseNet"}))
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["code"], "ambiguous")
        self.assertEqual(len(out["candidates"]), 2)

    def test_propose_deliver_returns_a_proposal_and_the_two_tap_link(self):
        out = json.loads(_MCP.TOOL_HANDLERS["propose_deliver"]({"deal_ref": DEAL_ORPHAN}))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(
            out["url"],
            f"{_db.dashboard_url()}/?entity=deal:{DEAL_ORPHAN}&action=deliver")
        self.assertIn(out["url"], out["proposal"])
        self.assertIn("PulseNet", out["proposal"])

    def test_propose_deliver_typed_refusals(self):
        cases = [
            (DEAL_PROPOSAL, "not_won"),
            (DEAL_BILLED, "already_delivered"),
            ("PulseNet —", "ambiguous"),
            ("deal_that_does_not_exist", "not_found"),
        ]
        for ref, code in cases:
            with self.subTest(ref):
                out = json.loads(_MCP.TOOL_HANDLERS["propose_deliver"]({"deal_ref": ref}))
                self.assertEqual(out["status"], "error")
                self.assertEqual(out["code"], code)

    def test_propose_deliver_leaves_the_database_byte_identical(self):
        """Ruling 3 measured, not asserted in prose: a conversion from chat is a
        PROPOSAL. If this verb ever grows a write, this digest changes."""
        before, census = self._digest(), self._census()
        for ref in (DEAL_ORPHAN, DEAL_PROPOSAL, DEAL_BILLED, "nope-xyz"):
            _MCP.TOOL_HANDLERS["propose_deliver"]({"deal_ref": ref})
        self.assertEqual(self._digest(), before)
        self.assertEqual(self._census(), census)

    def test_the_pulse_read_is_also_write_free(self):
        before, census = self._digest(), self._census()
        _MCP.TOOL_HANDLERS["get_journey_pulse"]({"ref": ACCOUNT_NAME})
        _MCP.TOOL_HANDLERS["get_project_hub"]({"project_ref": PROJECT_SHIPPED})
        self.assertEqual(self._digest(), before)
        self.assertEqual(self._census(), census)


if __name__ == "__main__":
    unittest.main()
