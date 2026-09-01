"""Decaimiento, render de tarjeta y despacho.

Dos garantías con dientes aquí:

  * **La purga E0 no puede romper la cita.** Es la razón por la que la evidencia
    se copia en `objective_evidence` en vez de apuntar al payload: la tarjeta
    tiene que seguir diciendo *por qué* meses después de que el habla cruda se
    borró.
  * **At-most-once en el envío.** `hermes send` no devuelve id de mensaje, así
    que un timeout es genuinamente ambiguo: pudo aterrizar. Reintentar
    duplicaría en el chat del operador, así que se marca y se deja.
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
DAY = 86400
QUOTE = "Te mando hoy mismo el resumen."
PAYLOAD = {"sentences": [{"index": 1, "speaker": "Dora", "text": QUOTE, "start_time": 5.0}],
           "action_items": [], "overview": None, "title": "Junta demo"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "kanban.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE deals (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE threads (thread_id INTEGER PRIMARY KEY, chat_id TEXT, "
                     "name TEXT, role TEXT, status TEXT)")
        conn.execute("INSERT INTO threads VALUES (8037,'655','Tasks','ops','active')")
        conn.execute("INSERT INTO threads VALUES (15185,'655','📅 Hoy','ops','active')")
        conn.execute("INSERT INTO deals VALUES ('deal_1','Demo')")
        m15.apply(conn)
        conn.commit()
        conn.close()
        self._saved, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.digestion as dg
        self.dg = dg
        self._saved_now, dg._now = dg._now, lambda: NOW
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.calls = []
        self._saved_cli = dg._run_cli
        dg._run_cli = self.fake_cli
        self.cli_result = (0, "", "")

    def tearDown(self):
        self.conn.close()
        self.dg._now, self.dg._run_cli = self._saved_now, self._saved_cli
        db.KANBAN_DB = self._saved
        self.tmp.cleanup()

    def fake_cli(self, argv, timeout=None):
        self.calls.append(argv)
        return self.cli_result

    def seed(self, sid="sug_1", kind="create_task", status="open", card="unsent",
             created=NOW, oid="obj_1", with_evidence=True):
        self.conn.execute("INSERT OR IGNORE INTO capture_events (event_id, source_kind, "
                          "source_ref, captured_at, payload, title) "
                          "VALUES ('ev_1','fireflies','tr_1',?,?,'Junta demo')",
                          (NOW, json.dumps(PAYLOAD)))
        self.conn.execute("INSERT OR IGNORE INTO objectives (id, title, owner, opened_at, "
                          "updated_at, last_evidence_ts) VALUES (?,?,?,?,?,?)",
                          (oid, "Enviar cotización", "Ric", NOW, NOW, NOW))
        if with_evidence:
            self.conn.execute("INSERT OR IGNORE INTO objective_evidence (objective_id, event_id, "
                              "anchor, quote, speaker, ts, op, created_at) "
                              "VALUES (?,'ev_1','1',?,'Dora',?,'objective.add',?)",
                              (oid, QUOTE, NOW, NOW))
        self.conn.execute("INSERT INTO suggestions (id, objective_id, kind, status, card_status, "
                          "title, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (sid, oid, kind, status, card, "Enviar cotización", 0.82, created, NOW))
        self.conn.commit()
        return sid


class Decay(Base):
    def test_silence_decays_prominence_and_archives(self):
        self.conn.execute("INSERT INTO objectives (id, title, opened_at, updated_at, "
                          "last_evidence_ts) VALUES ('obj_old','viejo',?,?,?)",
                          (NOW - 200 * DAY, NOW - 200 * DAY, NOW - 200 * DAY))
        self.conn.commit()
        res = self.dg.decay_and_excrete(conn=self.conn)
        self.assertEqual(res["archived"], 1)
        st = self.conn.execute("SELECT status FROM objectives WHERE id='obj_old'").fetchone()[0]
        self.assertEqual(st, "archived")

    def test_a_fresh_objective_survives(self):
        self.seed()
        self.dg.decay_and_excrete(conn=self.conn)
        st = self.conn.execute("SELECT status FROM objectives WHERE id='obj_1'").fetchone()[0]
        self.assertEqual(st, "open")

    def test_old_open_suggestions_expire(self):
        self.seed(created=NOW - 20 * DAY)
        res = self.dg.decay_and_excrete(conn=self.conn)
        self.assertEqual(res["expired"], 1)

    def test_expired_leases_return_to_the_queue(self):
        self.conn.execute("INSERT INTO capture_events (event_id, source_kind, source_ref, "
                          "captured_at, digest_status, lease_token, lease_expires_at) "
                          "VALUES ('ev_stuck','fireflies','tr_9',?,'leased','tok',?)",
                          (NOW, NOW - 10))
        self.conn.commit()
        res = self.dg.decay_and_excrete(conn=self.conn)
        self.assertEqual(res["leases_released"], 1)
        st = self.conn.execute("SELECT digest_status FROM capture_events WHERE "
                               "event_id='ev_stuck'").fetchone()[0]
        self.assertEqual(st, "pending")


class PurgeKeepsTheCitation(Base):
    def test_purge_nulls_payload_but_the_card_still_cites(self):
        self.seed()
        self.conn.execute("UPDATE capture_events SET digest_status='digested', digested_at=? "
                          "WHERE event_id='ev_1'", (NOW - 90 * DAY,))
        self.conn.commit()
        res = self.dg.decay_and_excrete(conn=self.conn)
        self.assertEqual(res["purged"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT payload FROM capture_events WHERE event_id='ev_1'").fetchone()[0])
        card = self.dg.render_card(self.conn, "sug_1")
        self.assertIn(QUOTE, card, "la cita debe sobrevivir a la purga del verbatim")

    def test_undigested_events_are_never_purged(self):
        self.seed()
        self.conn.execute("UPDATE capture_events SET digested_at=? WHERE event_id='ev_1'",
                          (NOW - 90 * DAY,))
        self.conn.commit()
        res = self.dg.decay_and_excrete(conn=self.conn)
        self.assertEqual(res["purged"], 0)


class CardRendering(Base):
    def test_card_carries_evidence_owner_and_link(self):
        self.seed()
        card = self.dg.render_card(self.conn, "sug_1")
        self.assertIn("Enviar cotización", card)
        self.assertIn(QUOTE, card)
        self.assertIn("Dora", card)
        self.assertIn("Junta demo", card)
        self.assertIn("dueño: Ric", card)
        self.assertIn("conf 0.82", card)
        self.assertIn("tab=suggestions&sug=sug_1", card)

    def test_kinds_read_differently(self):
        self.seed("sug_c", kind="close_task")
        self.assertIn("¿cierro la tarea?", self.dg.render_card(self.conn, "sug_c"))

    def test_missing_evidence_does_not_crash(self):
        self.seed("sug_n", oid="obj_n", with_evidence=False)
        self.assertIsNotNone(self.dg.render_card(self.conn, "sug_n"))

    def test_unknown_suggestion_returns_none(self):
        self.assertIsNone(self.dg.render_card(self.conn, "sug_ghost"))


class Dispatch(Base):
    def test_sends_to_the_configured_thread(self):
        self.seed()
        res = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(res["sent"], ["sug_1"])
        self.assertFalse(res["thread"]["fallback"])
        self.assertIn("telegram:655:8037", self.calls[0])

    def test_falls_back_and_says_so_when_the_topic_is_gone(self):
        self.seed()
        self.conn.execute("UPDATE threads SET status='archived' WHERE thread_id=8037")
        self.conn.commit()
        res = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertTrue(res["thread"]["fallback"], "una caída silenciosa mentiría sobre el destino")
        self.assertIn("telegram:655:15185", self.calls[0])

    def test_only_open_unsent_cards_go_out(self):
        self.seed("sug_sent", card="sent")
        self.seed("sug_dis", kind="close_task", status="dismissed")
        res = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(res["sent"], [])
        self.assertEqual(self.calls, [])

    def test_batch_is_capped(self):
        for i in range(12):
            self.conn.execute("INSERT INTO objectives (id, title, opened_at, updated_at) "
                              "VALUES (?,?,?,?)", (f"obj_{i}", f"t{i}", NOW, NOW))
            self.conn.execute("INSERT INTO suggestions (id, objective_id, kind, title, "
                              "created_at, updated_at) VALUES (?,?,'create_task',?,?,?)",
                              (f"sug_{i}", f"obj_{i}", f"t{i}", NOW + i, NOW))
        self.conn.commit()
        res = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(len(res["sent"]), 10)

    def test_a_timeout_is_ambiguous_and_never_retried(self):
        """El envío pudo aterrizar. Reintentar duplicaría en el chat."""
        self.seed()
        self.cli_result = (124, "", "timed out")
        res = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(res["ambiguous"], ["sug_1"])
        self.calls.clear()
        again = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(again["sent"], [])
        self.assertEqual(self.calls, [], "una tarjeta ambigua no se reenvía")

    def test_a_hard_failure_is_retried_next_run(self):
        """Un fallo con código conocido (no timeout) sí puede reintentarse: ahí
        sabemos que no aterrizó."""
        self.seed()
        self.cli_result = (1, "", "boom")
        res = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(len(res["failed"]), 1)
        self.cli_result = (0, "", "")
        again = self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(again["sent"], ["sug_1"])

    def test_sending_state_is_claimed_before_the_effect(self):
        """Reclamar ANTES de enviar es lo que impide que un crash a media
        llamada reenvíe al reiniciar."""
        seen = {}

        def spy(argv, timeout=None):
            seen["card_status"] = self.conn.execute(
                "SELECT card_status FROM suggestions WHERE id='sug_1'").fetchone()[0]
            return (0, "", "")

        self.seed()
        self.dg._run_cli = spy
        self.dg.dispatch_cards(conn=self.conn, sleep=lambda s: None)
        self.assertEqual(seen["card_status"], "sending")


if __name__ == "__main__":
    unittest.main()
