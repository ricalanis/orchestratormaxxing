"""El aplicador: el gate donde la salida de un modelo se vuelve estado.

Cada clase aquí corresponde a una garantía que el diseño promete y que un
refactor podría tirar sin que nada más se ponga rojo:

  * el modelo no puede inventar una cita (anti-alucinación),
  * ni reescribir el pasado (validez cronológica),
  * ni saltarse la máquina de estados (transiciones legales),
  * ni perder un evento por responder mal (failed → dead_letter, nunca digested),
  * ni escribir una sugerencia (las deriva el código, con dedup estructural).

Todo corre contra un kanban.db temporal: sin red, sin modelo, sin subprocess.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import db  # noqa: E402
from dashboard.migrations import m15_differential_capture as m15  # noqa: E402

NOW = 1785900000
QUOTE = "Te mando hoy mismo el resumen de funcionalidades."
PAYLOAD = {
    "title": "Junta demo",
    "overview": "Un resumen generado por IA que NO es citable.",
    "action_items": [{"assignee": "Dora", "text": "Enviar resumen", "at_seconds": 5}],
    "sentences": [
        {"index": 0, "speaker": "Ric", "text": "Cuéntame.", "start_time": 1.0},
        {"index": 1, "speaker": "Dora", "text": QUOTE, "start_time": 5.0},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "kanban.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE deals (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO deals VALUES ('deal_1','Demo')")
        conn.execute("INSERT INTO projects VALUES ('proj_1','Demo')")
        m15.apply(conn)
        conn.commit()
        conn.close()
        self._saved_db, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.digestion as dg
        self.dg = dg
        self._saved_now = dg._now
        dg._now = lambda: NOW
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.seed_event()

    def tearDown(self):
        self.conn.close()
        self.dg._now = self._saved_now
        db.KANBAN_DB = self._saved_db
        self.tmp.cleanup()

    def seed_event(self, event_id="ev_1", occurred=NOW, entity=("deal", "deal_1")):
        self.conn.execute(
            "INSERT INTO capture_events (event_id, source_kind, source_ref, occurred_at, "
            "captured_at, payload, entity_kind, entity_id) VALUES (?,'fireflies','tr_1',?,?,?,?,?)",
            (event_id, occurred, NOW, json.dumps(PAYLOAD), entity[0], entity[1]))
        self.conn.commit()
        return event_id

    def apply(self, ops, event_id="ev_1"):
        return self.dg.apply_state_ops(event_id, ops, conn=self.conn)

    def add(self, **over):
        # Dueño explícito: tras el gate de dueño, una alta SIN dueño no produce
        # tarjeta a propósito (solo los compromisos del operador hacen tarjeta), así que
        # el helper del caso normal declara que el compromiso es del operador.
        op = {"op": "objective.add", "title": "Enviar cotización", "owner": "Ric",
              "quote": QUOTE}
        op.update(over)
        return op

    def verdicts(self, res):
        return [o["verdict"] for o in res["ops"]]


class AntiHallucination(Base):
    def test_a_verbatim_quote_is_accepted(self):
        res = self.apply([self.add()])
        self.assertEqual(self.verdicts(res), ["applied"])

    def test_an_invented_quote_is_rejected(self):
        res = self.apply([self.add(quote="Te confirmo el pago mañana sin falta.")])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM objectives").fetchone()[0], 0)

    def test_whitespace_rewrapping_is_tolerated(self):
        res = self.apply([self.add(quote="  Te mando   hoy mismo\nel resumen de funcionalidades. ")])
        self.assertEqual(self.verdicts(res), ["applied"])

    def test_the_ai_overview_is_not_quotable(self):
        """Citar el resumen sería citar a la IA de Fireflies como si fuera
        alguien hablando — exactamente lo que el gate existe para impedir."""
        res = self.apply([self.add(quote="Un resumen generado por IA que NO es citable.")])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])

    def test_the_action_item_text_is_not_quotable(self):
        res = self.apply([self.add(quote="Enviar resumen")])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])


class OpSchema(Base):
    def test_unknown_operator_is_rejected(self):
        res = self.apply([{"op": "objective.delete", "objective_id": "x", "quote": QUOTE}])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])

    def test_unexpected_field_is_rejected(self):
        """Un campo de más es el vector por el que un patch libre se colaría."""
        res = self.apply([self.add(status="done")])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])

    def test_missing_required_field_is_rejected(self):
        res = self.apply([{"op": "objective.add", "title": "sin cita"}])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])

    def test_oversized_title_and_bad_confidence_are_rejected(self):
        # Un evento por aserción: el segundo apply sobre el mismo evento daría
        # el corto-circuito de replay (correcto) en vez de un veredicto.
        self.assertEqual(self.verdicts(self.apply([self.add(title="x" * 121)])),
                         ["rejected_validation"])
        self.seed_event("ev_conf", occurred=NOW + 5)
        self.assertEqual(self.verdicts(self.apply([self.add(confidence=1.4)], "ev_conf")),
                         ["rejected_validation"])

    def test_non_object_op_does_not_crash_the_batch(self):
        res = self.apply(["basura", self.add()])
        self.assertEqual(self.verdicts(res), ["rejected_validation", "applied"])


class EntityReferences(Base):
    def test_unknown_entity_is_rejected_not_globalised(self):
        """Silenciar la referencia a NULL convertiría un objetivo de un deal
        inexistente en un objetivo global — basura que parece dato."""
        res = self.apply([self.add(entity_kind="deal", entity_id="deal_ghost")])
        self.assertEqual(self.verdicts(res), ["rejected_unknown_ref"])

    def test_known_entity_is_accepted(self):
        res = self.apply([self.add(entity_kind="project", entity_id="proj_1")])
        self.assertEqual(self.verdicts(res), ["applied"])

    def test_half_specified_entity_is_rejected(self):
        self.assertEqual(self.verdicts(self.apply([self.add(entity_kind="deal")])),
                         ["rejected_validation"])

    def test_objective_inherits_the_event_entity(self):
        self.apply([self.add()])
        row = self.conn.execute("SELECT entity_kind, entity_id FROM objectives").fetchone()
        self.assertEqual(row, ("deal", "deal_1"))


class ChronologicalValidity(Base):
    def test_an_older_event_cannot_rewrite_a_newer_state(self):
        res = self.apply([self.add()])
        oid = res["ops"][0]["objective_id"]
        self.seed_event("ev_old", occurred=NOW - 86400)
        stale = self.dg.apply_state_ops(
            "ev_old", [{"op": "objective.complete", "objective_id": oid, "quote": QUOTE}],
            conn=self.conn)
        self.assertEqual(self.verdicts(stale), ["rejected_stale"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM objectives WHERE id = ?", (oid,)).fetchone()[0], "open")

    def test_a_newer_event_may_advance(self):
        oid = self.apply([self.add()])["ops"][0]["objective_id"]
        self.seed_event("ev_new", occurred=NOW + 3600)
        res = self.dg.apply_state_ops(
            "ev_new", [{"op": "objective.complete", "objective_id": oid, "quote": QUOTE}],
            conn=self.conn)
        self.assertEqual(self.verdicts(res), ["applied"])


class LegalTransitions(Base):
    def setUp(self):
        super().setUp()
        self.oid = self.apply([self.add()])["ops"][0]["objective_id"]
        self.seed_event("ev_2", occurred=NOW + 10)

    def op(self, name, **kw):
        base = {"op": name, "objective_id": self.oid, "quote": QUOTE}
        base.update(kw)
        return self.dg.apply_state_ops("ev_2", [base], conn=self.conn)

    def status(self):
        return self.conn.execute("SELECT status FROM objectives WHERE id = ?",
                                 (self.oid,)).fetchone()[0]

    def test_block_then_unblock(self):
        self.assertEqual(self.verdicts(self.op("objective.block", waiting_on="Dora")), ["applied"])
        self.assertEqual(self.status(), "blocked")
        self.seed_event("ev_3", occurred=NOW + 20)
        r = self.dg.apply_state_ops(
            "ev_3", [{"op": "objective.unblock", "objective_id": self.oid, "quote": QUOTE}],
            conn=self.conn)
        self.assertEqual(self.verdicts(r), ["applied"])
        self.assertEqual(self.status(), "open")

    def test_cannot_unblock_an_open_objective(self):
        self.assertEqual(self.verdicts(self.op("objective.unblock")), ["rejected_validation"])

    def test_cannot_complete_twice(self):
        self.assertEqual(self.verdicts(self.op("objective.complete")), ["applied"])
        self.seed_event("ev_4", occurred=NOW + 30)
        r = self.dg.apply_state_ops(
            "ev_4", [{"op": "objective.complete", "objective_id": self.oid, "quote": QUOTE}],
            conn=self.conn)
        self.assertEqual(self.verdicts(r), ["rejected_validation"])

    def test_update_operators_change_only_their_field(self):
        self.assertEqual(self.verdicts(self.op("objective.reassign", owner="Ric")), ["applied"])
        self.seed_event("ev_5", occurred=NOW + 40)
        self.dg.apply_state_ops("ev_5", [{"op": "objective.reschedule", "objective_id": self.oid,
                                          "due_hint": "viernes", "quote": QUOTE}], conn=self.conn)
        row = self.conn.execute("SELECT owner, due_hint, title, status FROM objectives "
                                "WHERE id = ?", (self.oid,)).fetchone()
        self.assertEqual(row, ("Ric", "viernes", "Enviar cotización", "open"))

    def test_supersede_keeps_the_audit_link(self):
        r = self.op("objective.supersede", title="Enviar cotización v2")
        new_id = r["ops"][0]["objective_id"]
        old = self.conn.execute("SELECT status, superseded_by FROM objectives WHERE id = ?",
                                (self.oid,)).fetchone()
        self.assertEqual(old, ("superseded", new_id))
        self.assertNotEqual(new_id, self.oid)

    def test_version_increments_for_compare_and_swap(self):
        self.op("objective.advance")
        v = self.conn.execute("SELECT version FROM objectives WHERE id = ?",
                              (self.oid,)).fetchone()[0]
        self.assertEqual(v, 2)


class EventIsNeverLost(Base):
    def status_of(self, eid="ev_1"):
        return self.conn.execute(
            "SELECT digest_status, attempts FROM capture_events WHERE event_id = ?",
            (eid,)).fetchone()

    def test_malformed_output_fails_it_does_not_digest(self):
        res = self.dg.apply_state_ops("ev_1", "no soy una lista", conn=self.conn)
        self.assertEqual(res["status"], "error")
        self.assertEqual(self.status_of(), ("failed", 1))

    def test_repeated_failures_dead_letter_instead_of_vanishing(self):
        for _ in range(m15.MAX_DIGEST_ATTEMPTS):
            self.dg.apply_state_ops("ev_1", None, conn=self.conn)
        st, attempts = self.status_of()
        self.assertEqual(st, "dead_letter")
        self.assertEqual(attempts, m15.MAX_DIGEST_ATTEMPTS)

    def test_too_many_ops_is_a_failure_not_a_silent_truncation(self):
        self.dg.apply_state_ops("ev_1", [self.add()] * 21, conn=self.conn)
        self.assertEqual(self.status_of()[0], "failed")

    def test_rejections_still_digest_the_event(self):
        """Un rechazo de validación es una respuesta válida del gate: el evento
        SÍ se digirió (se juzgó), a diferencia de una salida ilegible."""
        self.apply([self.add(quote="inventada")])
        self.assertEqual(self.status_of()[0], "digested")

    def test_replay_after_commit_does_not_double_apply(self):
        self.apply([self.add()])
        again = self.apply([self.add()])
        self.assertTrue(again.get("replay"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM objectives").fetchone()[0], 1)

    def test_ledger_records_every_verdict(self):
        self.apply([self.add(), self.add(quote="inventada")])
        rows = self.conn.execute("SELECT op_index, verdict FROM state_ops WHERE event_id='ev_1' "
                                 "ORDER BY op_index").fetchall()
        self.assertEqual(rows, [(0, "applied"), (1, "rejected_validation")])


class SuggestionsAreDerived(Base):
    def test_add_derives_a_create_task_card(self):
        oid = self.apply([self.add()])["ops"][0]["objective_id"]
        row = self.conn.execute("SELECT objective_id, kind, status, title FROM suggestions"
                                ).fetchone()
        self.assertEqual(row, (oid, "create_task", "open", "Enviar cotización"))

    def test_advance_does_not_nag(self):
        oid = self.apply([self.add()])["ops"][0]["objective_id"]
        self.conn.execute("DELETE FROM suggestions")
        self.seed_event("ev_2", occurred=NOW + 10)
        self.dg.apply_state_ops("ev_2", [{"op": "objective.advance", "objective_id": oid,
                                          "quote": QUOTE}], conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM suggestions").fetchone()[0], 0)

    def test_a_second_mention_bumps_seen_count_not_a_second_card(self):
        oid = self.apply([self.add()])["ops"][0]["objective_id"]
        self.seed_event("ev_2", occurred=NOW + 10)
        self.dg.apply_state_ops("ev_2", [{"op": "objective.rename", "objective_id": oid,
                                          "title": "Enviar cotización", "quote": QUOTE}],
                                conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM suggestions").fetchone()[0], 1)

    def test_dismiss_is_sticky_forever(self):
        """El camino real de reincidencia es un ciclo bloquear→desbloquear→
        bloquear: la MISMA (objetivo, kind) se vuelve a derivar. Descartar una
        tarjeta y disparar otra de kind distinto no prueba nada — nunca toca la
        rama pegajosa."""
        oid = self.apply([self.add()])["ops"][0]["objective_id"]
        self.seed_event("ev_b1", occurred=NOW + 10)
        self.dg.apply_state_ops("ev_b1", [{"op": "objective.block", "objective_id": oid,
                                           "waiting_on": "Dora", "quote": QUOTE}],
                                conn=self.conn)
        sid = self.conn.execute("SELECT id FROM suggestions WHERE objective_id = ? AND "
                                "kind = 'unblock'", (oid,)).fetchone()[0]

        # El operador la descarta.
        self.conn.execute("UPDATE suggestions SET status='dismissed' WHERE id = ?", (sid,))
        self.conn.commit()

        # El objetivo se desbloquea y vuelve a bloquearse → misma tarjeta otra vez.
        self.seed_event("ev_u", occurred=NOW + 20)
        self.dg.apply_state_ops("ev_u", [{"op": "objective.unblock", "objective_id": oid,
                                          "quote": QUOTE}], conn=self.conn)
        self.seed_event("ev_b2", occurred=NOW + 30)
        self.dg.apply_state_ops("ev_b2", [{"op": "objective.block", "objective_id": oid,
                                           "waiting_on": "Dora", "quote": QUOTE}],
                                conn=self.conn)

        st = self.conn.execute("SELECT status FROM suggestions WHERE id = ?", (sid,)).fetchone()[0]
        self.assertEqual(st, "dismissed", "una sugerencia descartada jamás revive")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM suggestions WHERE objective_id = ? AND kind = 'unblock'",
            (oid,)).fetchone()[0], 1, "ni se duplica por otro id")

    def _seed_history(self, n, owner, kind="create_task", status="dismissed"):
        for i in range(n):
            oid = f"obj_h{owner}{i}"
            self.conn.execute(
                "INSERT INTO objectives (id, title, owner, opened_at, updated_at) "
                "VALUES (?,?,?,?,?)", (oid, "hist", owner, NOW, NOW))
            self.conn.execute(
                "INSERT INTO suggestions (id, objective_id, kind, status, bucket, title, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"sug_h{owner}{i}", oid, kind, status, "fireflies:create_task:med",
                 "hist", NOW, NOW))
        self.conn.commit()

    def test_a_bucket_ricardo_keeps_rejecting_gets_suppressed(self):
        self._seed_history(8, "Ric")
        self.apply([self.add(confidence=0.6)])
        st = self.conn.execute("SELECT status FROM suggestions WHERE bucket=? AND title=?",
                               ("fireflies:create_task:med", "Enviar cotización")).fetchone()[0]
        self.assertEqual(st, "suppressed")

    def test_dismissing_other_peoples_commitments_does_not_teach_suppression(self):
        """Pasó de verdad: 11 descartes de tareas AJENAS —por un gate de dueño
        que faltaba— enseñaron "descarta todo", y dos compromisos legítimos del
        operador quedaron suprimidos sin llegarle. Descartar lo de otro responde
        "¿esto es mío?", no "¿esto vale la pena?"."""
        self._seed_history(12, "Antonio")
        self.apply([self.add(confidence=0.6)])
        st = self.conn.execute("SELECT status FROM suggestions WHERE bucket=? AND title=?",
                               ("fireflies:create_task:med", "Enviar cotización")).fetchone()[0]
        self.assertEqual(st, "open", "el gusto del operador no se infiere de tareas ajenas")


class SuggestionsCarryTheirProject(Base):
    """Una tarea bien archivada nace en su proyecto; una mal archivada se pierde
    igual que una sin archivar, pero además miente. Por eso la resolución es
    conservadora: sin ancla, Inbox."""

    def project_of(self):
        return self.conn.execute("SELECT proposed_project_id FROM suggestions").fetchone()[0]

    def test_an_objective_anchored_to_a_project_proposes_it(self):
        self.conn.execute("UPDATE capture_events SET entity_kind='project', entity_id='proj_1' "
                          "WHERE event_id='ev_1'")
        self.conn.commit()
        self.apply([self.add()])
        self.assertEqual(self.project_of(), "proj_1")

    def test_a_deal_hops_to_its_delivery_project(self):
        """La unión dinero→entrega ya existe en la espina (`deals.project_id`);
        seguirla es lo que archiva el compromiso de un trato en el proyecto que
        lo entrega."""
        self.conn.execute("ALTER TABLE deals ADD COLUMN project_id TEXT")
        self.conn.execute("UPDATE deals SET project_id='proj_1' WHERE id='deal_1'")
        self.conn.commit()
        self.apply([self.add()])          # el evento ya está anclado a deal_1
        self.assertEqual(self.project_of(), "proj_1")

    def test_a_deal_without_a_project_proposes_nothing(self):
        self.conn.execute("ALTER TABLE deals ADD COLUMN project_id TEXT")
        self.conn.commit()
        self.apply([self.add()])
        self.assertIsNone(self.project_of())

    def test_a_dead_project_reference_is_not_proposed(self):
        self.conn.execute("UPDATE capture_events SET entity_kind='project', "
                          "entity_id='proj_borrado' WHERE event_id='ev_1'")
        self.conn.commit()
        self.apply([self.add(entity_kind="project", entity_id="proj_1")])
        self.conn.execute("DELETE FROM projects WHERE id='proj_1'")
        self.conn.commit()
        self.assertIsNone(
            self.dg.resolve_project_for_objective(
                self.conn,
                self.conn.execute("SELECT id FROM objectives").fetchone()[0]))

    def test_no_anchor_means_no_project(self):
        self.conn.execute("UPDATE capture_events SET entity_kind=NULL, entity_id=NULL "
                          "WHERE event_id='ev_1'")
        self.conn.commit()
        self.apply([self.add()])
        self.assertIsNone(self.project_of())


class ProvenanceIsRecorded(Base):
    def test_every_applied_op_leaves_its_quote(self):
        oid = self.apply([self.add(anchor=1, speaker="Dora")])["ops"][0]["objective_id"]
        row = self.conn.execute("SELECT quote, speaker, anchor, op FROM objective_evidence "
                                "WHERE objective_id = ?", (oid,)).fetchone()
        self.assertEqual(row, (QUOTE, "Dora", "1", "objective.add"))

    def test_a_rejected_op_leaves_no_evidence(self):
        self.apply([self.add(quote="inventada")])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM objective_evidence").fetchone()[0], 0)


class GistIsBounded(Base):
    def test_gist_writes_to_the_events_entity(self):
        res = self.apply([{"op": "entity.set_gist", "gist": "Cotización pendiente."}])
        self.assertEqual(self.verdicts(res), ["applied"])
        row = self.conn.execute("SELECT entity_kind, entity_id, gist FROM entity_state").fetchone()
        self.assertEqual(row, ("deal", "deal_1", "Cotización pendiente."))

    def test_oversized_gist_is_rejected(self):
        res = self.apply([{"op": "entity.set_gist", "gist": "x" * 701}])
        self.assertEqual(self.verdicts(res), ["rejected_validation"])


if __name__ == "__main__":
    unittest.main()
