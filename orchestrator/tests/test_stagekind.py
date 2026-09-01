"""Contract for `dashboard/stagekind.py` — the derived stage of a task.

Directiva ADICIÓN 9 gives a task a SEVENTH dimension (contacto · formalización ·
ejecución · entrega · facturación · cobranza) and Ley 1 forbids asking for it.
So the whole feature is a pure function over facts the caller already holds, and
this file is the table that defines it.

What it tests HARDEST, and why:

  * **The two stages the rule must REFUSE.** `facturacion` and `cobranza` are
    minted-only: a won deal on a delivered project is indistinguishable from
    "invoice this" and from "chase that invoice". A rule that guessed would chip
    every delivered project's tasks as billing work. The refusal is asserted
    exhaustively — over the whole cross-product of deal stages × project
    statuses — rather than by one example, because "never" is the claim.
  * **An explicit value always wins.** Two writers set the column (the step-5
    materializer, and the operator correcting the chip); a derivation that
    overrode either would make the chip un-correctable and the materializer's
    stamp meaningless. Asserted against a derivation that would otherwise
    disagree — an explicit value that MATCHES the rule proves nothing.
  * **The vocabulary cannot drift from the storage engine.** The migration
    carries its own literal (a migration is a historical record) and the CHECK
    constraint carries a third copy. All three are asserted equal here, so a
    rule that returns a value the DB would reject fails at test time instead of
    at write time.
  * **Absence is an answer.** Lost/stalled deals, unknown stages, archived
    projects and bare tasks all yield None. The caller renders no chip — an
    honest blank beats a plausible guess, and this wave's chip is display-only,
    so a wrong chip is a lie with nothing to correct it.

Pure module: no DB, no clock, no fixtures. Stdlib unittest, pytest-discoverable.
Run: .venv/bin/python -m pytest tests/test_stagekind.py    # from orchestrator/
"""
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Imported outside any availability guard: these are the modules under test, so
# swallowing an ImportError would turn a deleted feature into a green run.
from dashboard import stagekind
from dashboard.migrations import m06_task_deal as _m06


ALL_DEAL_STAGES = ("lead", "engaged", "qualified", "demo", "proposal",
                   "won", "lost", "stalled")
ALL_PROJECT_STATUSES = ("planned", "active", "delivering", "delivered",
                        "archived", None)


@dataclass
class _TaskObj:
    """A task as an OBJECT (what `db.Task` is) — one of the three shapes."""
    id: str = "t_x"
    deal_id: Optional[str] = None
    stage_kind: Optional[str] = None


class Vocabulary(unittest.TestCase):
    """The three copies of the stage list must say the same thing."""

    def test_runtime_and_migration_vocabularies_agree(self):
        self.assertEqual(stagekind.STAGE_KINDS, _m06.STAGE_KINDS)

    def test_the_check_constraint_carries_the_same_six_values(self):
        # The DDL string is the third copy, and the one the storage engine
        # actually enforces. If the rule can return a value this CHECK rejects,
        # the materializer's write fails in production, not here.
        ddl = _m06._ADD_STAGE_KIND
        for kind in stagekind.STAGE_KINDS:
            self.assertIn(f"'{kind}'", ddl, f"{kind} missing from the CHECK")
        self.assertEqual(ddl.count("'"), len(stagekind.STAGE_KINDS) * 2,
                         "the CHECK lists a value the runtime vocabulary lacks")

    def test_every_kind_has_a_display_label(self):
        for kind in stagekind.STAGE_KINDS:
            self.assertTrue(stagekind.label(kind), f"{kind} has no label")
        self.assertIsNone(stagekind.label(None))
        self.assertIsNone(stagekind.label("not_a_stage"))


class DerivationTable(unittest.TestCase):
    """The rule itself, as a table. Each row: (deal_stage, project_status) → kind."""

    CASES = [
        # --- commercial lineage: the deal answers -------------------------
        ("lead",      None,          "contacto"),
        ("lead",      "active",      "contacto"),
        ("engaged",   None,          "contacto"),
        ("qualified", None,          "formalizacion"),
        ("demo",      None,          "formalizacion"),
        ("proposal",  "active",      "formalizacion"),
        # --- won: the delivery leg says where we are ----------------------
        ("won",       None,          "ejecucion"),
        ("won",       "planned",     "ejecucion"),
        ("won",       "active",      "ejecucion"),
        ("won",       "delivering",  "entrega"),
        ("won",       "delivered",   "entrega"),
        ("won",       "archived",    None),
        # --- exits from the cycle are not positions in it -----------------
        ("lost",      "active",      None),
        ("stalled",   "active",      None),
        # --- a vocabulary that grew without this table --------------------
        ("negotiating", "active",    None),
    ]

    def test_the_table(self):
        for stage, status, expected in self.CASES:
            with self.subTest(deal_stage=stage, project_status=status):
                got = stagekind.derive({"deal_id": "d_1"}, deal_stage=stage,
                                       project_status=status)
                self.assertEqual(got, expected)

    def test_a_dealless_project_task_is_execution(self):
        # The commonest task in the system: no deal, a live project. The
        # delivery cycle is the only cycle it is in.
        self.assertEqual(
            stagekind.derive({"project_id": "proj_x"}, project_status="active"),
            "ejecucion")
        self.assertEqual(
            stagekind.derive({"project_id": "proj_x"}, project_status="planned"),
            "ejecucion")

    def test_a_dealless_delivered_project_task_is_delivery(self):
        self.assertEqual(
            stagekind.derive({"project_id": "proj_x"}, project_status="delivered"),
            "entrega")
        self.assertEqual(
            stagekind.derive({"project_id": "proj_x"}, project_status="delivering"),
            "entrega")

    def test_an_archived_project_places_nothing(self):
        self.assertIsNone(
            stagekind.derive({"project_id": "proj_x"}, project_status="archived"))

    def test_a_task_with_neither_places_nothing(self):
        self.assertIsNone(stagekind.derive({}))
        self.assertIsNone(stagekind.derive(None))

    def test_a_deal_id_whose_stage_did_not_resolve_places_nothing(self):
        # The join was not made (a pre-m06 read path, a deleted deal). Refusing
        # is the point: guessing a position in a cycle we cannot see would put a
        # confident chip on a task nobody can trace.
        self.assertIsNone(stagekind.derive({"deal_id": "d_gone"},
                                           project_status="active"))


class MintedOnly(unittest.TestCase):
    """`facturacion` / `cobranza` are asserted by a writer or they are not true."""

    def test_no_combination_of_facts_ever_derives_them(self):
        for stage in ALL_DEAL_STAGES + (None, "", "unknown"):
            for status in ALL_PROJECT_STATUSES + ("", "unknown"):
                for task in ({"deal_id": "d_1"}, {"project_id": "p_1"}, {}):
                    got = stagekind.derive(task, deal_stage=stage,
                                           project_status=status)
                    self.assertNotIn(
                        got, stagekind.MINTED_ONLY,
                        f"derived {got} from stage={stage!r} status={status!r} "
                        f"task={task!r} — billing stages are minted-only")

    def test_but_an_explicit_billing_stamp_survives(self):
        for kind in stagekind.MINTED_ONLY:
            self.assertEqual(
                stagekind.derive({"deal_id": "d_1", "stage_kind": kind},
                                 deal_stage="won", project_status="delivered"),
                kind)

    def test_derive_never_returns_a_value_outside_the_vocabulary(self):
        for stage in ALL_DEAL_STAGES + (None, "unknown"):
            for status in ALL_PROJECT_STATUSES + ("unknown",):
                got = stagekind.derive({"deal_id": "d_1"}, deal_stage=stage,
                                       project_status=status)
                self.assertTrue(got is None or got in stagekind.STAGE_KINDS)


class ExplicitWins(unittest.TestCase):

    def test_an_explicit_value_overrides_a_rule_that_disagrees(self):
        # lead + explicit 'entrega' — the rule would say contacto. If the
        # override did not win, the operator's correction would be erased on
        # every read.
        self.assertEqual(
            stagekind.derive({"deal_id": "d_1", "stage_kind": "entrega"},
                             deal_stage="lead", project_status="active"),
            "entrega")

    def test_a_garbage_explicit_value_falls_back_to_the_rule(self):
        # The CHECK makes this unreachable through a sanctioned writer, so this
        # is about a hand-edited row: an unrecognised string must not become a
        # chip, and must not suppress the honest derived answer either.
        self.assertEqual(
            stagekind.derive({"deal_id": "d_1", "stage_kind": "no_such_stage"},
                             deal_stage="lead"),
            "contacto")

    def test_explicit_values_are_case_and_space_insensitive(self):
        self.assertEqual(
            stagekind.derive({"stage_kind": "  Entrega "}), "entrega")


class RowShapes(unittest.TestCase):
    """Dicts, sqlite3.Rows and dataclasses all reach this function."""

    def test_an_object_task(self):
        self.assertEqual(
            stagekind.derive(_TaskObj(deal_id="d_1"), deal_stage="proposal"),
            "formalizacion")
        self.assertEqual(
            stagekind.derive(_TaskObj(stage_kind="cobranza"), deal_stage="won"),
            "cobranza")

    def test_a_sqlite_row(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id TEXT, deal_id TEXT, stage_kind TEXT)")
        conn.execute("INSERT INTO t VALUES ('t1', 'd_1', NULL)")
        row = conn.execute("SELECT * FROM t").fetchone()
        self.assertEqual(stagekind.derive(row, deal_stage="engaged"), "contacto")

    def test_a_row_that_predates_m06_has_neither_column(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id TEXT, project_id TEXT)")
        conn.execute("INSERT INTO t VALUES ('t1', 'p_1')")
        row = conn.execute("SELECT * FROM t").fetchone()
        # Must not raise on the missing columns — the fallback read path in
        # `db._select_tasks` hands exactly this shape through.
        self.assertEqual(stagekind.derive(row, project_status="active"), "ejecucion")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
