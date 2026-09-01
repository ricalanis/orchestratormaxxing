"""Captura de WhatsApp: default-deny, pulso sin contenido, ventanas por silencio.

La propiedad que sostiene todo lo demás: **un chat que el operador no autorizó jamás
deja texto en nuestra base.** No porque se filtre después, sino porque nunca se
lee. Su espejo tiene 493 chats y 417 grupos — conversaciones con clientes, con su
familia, con quien sea — y el sistema solo mira los que él marcó.

Los tests usan un espejo falso con el esquema REAL de wacli v0.15.2 (`ts`, no
`timestamp`; `sender_name`; `revoked`), verificado contra el store vivo.
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
from dashboard.migrations import m20_whatsapp_allowlist as m17  # noqa: E402

NOW = 1785900000
GAP = m17.WINDOW_GAP_SECONDS
CHAT = "5215551234567@s.whatsapp.net"
GRUPO = "120363000@g.us"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        # Espejo falso con el esquema REAL de wacli.
        self.mirror_path = root / "wacli.db"
        m = sqlite3.connect(str(self.mirror_path))
        m.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT)")
        m.execute("CREATE TABLE messages (chat_jid TEXT, ts INTEGER, sender_jid TEXT, "
                  "sender_name TEXT, from_me INTEGER, text TEXT, display_text TEXT, "
                  "revoked INTEGER DEFAULT 0, deleted_for_me INTEGER DEFAULT 0)")
        m.execute("INSERT INTO chats VALUES (?,?)", (CHAT, "Cliente Demo"))
        m.execute("INSERT INTO chats VALUES (?,?)", (GRUPO, "Grupo Familia"))
        for i, (txt, mine) in enumerate([
                ("Oye Ric, ¿nos mandas la cotización?", 0),
                ("Va, te la mando el jueves.", 1),
                ("Perfecto, gracias.", 0)]):
            m.execute("INSERT INTO messages (chat_jid, ts, sender_jid, sender_name, from_me, "
                      "text) VALUES (?,?,?,?,?,?)",
                      (CHAT, NOW - 3600 + i * 60, CHAT, "Ana", mine, txt))
        for i in range(4):
            m.execute("INSERT INTO messages (chat_jid, ts, sender_jid, sender_name, from_me, "
                      "text) VALUES (?,?,?,?,?,?)",
                      (GRUPO, NOW - 3600 + i * 60, GRUPO, "Prima", 0, f"mensaje familiar {i}"))
        m.commit(); m.close()

        path = root / "kanban.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE deals (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, account_id TEXT, "
                     "status TEXT)")
        m15.apply(conn); m17.apply(conn)
        conn.commit(); conn.close()

        self._saved_db, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.whatsapp as wa
        self.wa = wa
        self._saved_mirror, wa.WACLI_DB = wa.WACLI_DB, self.mirror_path
        self._saved_now, wa._now = wa._now, lambda: NOW
        self.conn = sqlite3.connect(str(path))

    def tearDown(self):
        self.conn.close()
        self.wa.WACLI_DB, self.wa._now = self._saved_mirror, self._saved_now
        db.KANBAN_DB = self._saved_db
        self.tmp.cleanup()

    def pulse(self, jid=CHAT, when=None):
        """Un pulso = un mensaje. En producción el webhook dispara uno por
        mensaje, así que una ventana con varios los cubre a todos; pulsar una
        sola vez produce una ventana de un instante que no cubre nada."""
        if when is not None:
            self.wa._now = lambda: when
        self.wa.record_activity(jid, conn=self.conn)
        self.wa._now = lambda: NOW

    def allow(self, jid=CHAT, when=None):
        """Autoriza en el pasado. En la vida real el operador marca el chat y DESPUÉS
        llega la conversación; los fixtures deben respetar ese orden porque
        autorizar no alcanza hacia atrás."""
        self.wa._now = lambda: (NOW - 7200 if when is None else when)
        self.wa.set_chat_allowed(jid, True, conn=self.conn)
        self.wa._now = lambda: NOW

    def pulse_conversation(self, jid=CHAT, count=3, first=None):
        """Pulsa como lo haría una conversación real: uno por mensaje, en los
        mismos tiempos que tiene el espejo."""
        first = (NOW - 3600) if first is None else first
        for i in range(count):
            self.pulse(jid, when=first + i * 60)

    def events(self):
        return self.conn.execute(
            "SELECT event_id, source_ref, payload FROM capture_events").fetchall()


class DefaultDeny(Base):
    def test_a_new_chat_is_registered_denied(self):
        """Aparece en la lista para que el operador decida. Verlo no es leerlo."""
        self.pulse()
        row = self.conn.execute("SELECT allowed FROM whatsapp_chats WHERE jid=?",
                                (CHAT,)).fetchone()
        self.assertEqual(row[0], 0)
        self.assertFalse(self.wa.is_allowed(self.conn, CHAT))

    def test_an_unknown_chat_is_not_allowed(self):
        self.assertFalse(self.wa.is_allowed(self.conn, "desconocido@s.whatsapp.net"))

    def test_an_allowed_chat_reads_as_allowed(self):
        """El lado positivo del gate. Sin él, una función que SIEMPRE dijera «no»
        pasaría todos los contratos de negación — y el sistema entero se quedaría
        mudo pareciendo prudente."""
        self.allow()
        self.assertIs(self.wa.is_allowed(self.conn, CHAT), True)

    def test_a_denied_chat_never_yields_a_window(self):
        """La propiedad central: sin permiso, su texto no entra jamás."""
        self.pulse_conversation()
        self.assertEqual(self.wa.closed_windows(self.conn, now=NOW), [])
        res = self.wa.harvest_windows(conn=self.conn, now=NOW)
        self.assertEqual(res["created"], [])
        self.assertEqual(self.events(), [])

    def test_allowing_a_chat_is_what_opens_it(self):
        self.pulse_conversation()
        self.wa.set_chat_allowed(CHAT, True, conn=self.conn)
        self.assertEqual([w["jid"] for w in self.wa.closed_windows(self.conn, now=NOW)], [CHAT])

    def test_a_family_group_stays_out_unless_chosen(self):
        self.allow(CHAT)
        self.pulse_conversation(GRUPO, count=4)
        self.pulse_conversation(CHAT)
        self.wa.harvest_windows(conn=self.conn, now=NOW)
        refs = {r[1] for r in self.events()}
        self.assertIn(CHAT, refs)
        self.assertNotIn(GRUPO, refs, "un grupo no autorizado no puede colarse")

    def test_revoking_permission_stops_future_windows(self):
        self.pulse_conversation()
        self.wa.set_chat_allowed(CHAT, True, conn=self.conn)
        self.wa.set_chat_allowed(CHAT, False, conn=self.conn)
        self.assertEqual(self.wa.closed_windows(self.conn, now=NOW), [])


class ThePulseCarriesNoContent(Base):
    def test_activity_stores_no_message_text(self):
        """Un chat que nunca se permita jamás deja una palabra: el pulso es la
        única escritura que ocurre antes del permiso, y no lleva texto."""
        self.pulse()
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(whatsapp_activity)")]
        self.assertNotIn("text", cols)
        blob = json.dumps(self.conn.execute("SELECT * FROM whatsapp_activity").fetchall())
        self.assertNotIn("cotización", blob)
        self.assertNotIn("mensaje familiar", blob)

    def test_repeated_pulses_count_without_reopening_the_window(self):
        self.pulse(when=NOW - 3600)
        self.pulse(when=NOW - 3000)
        row = self.conn.execute("SELECT message_count, window_open_at FROM whatsapp_activity "
                                "WHERE jid=?", (CHAT,)).fetchone()
        self.assertEqual(row[0], 2)
        self.assertEqual(row[1], NOW - 3600, "la ventana la abre el PRIMER mensaje")


class WindowsCloseOnSilence(Base):
    def test_a_live_conversation_is_not_harvested_mid_flight(self):
        """Cortar una conversación a mitad le quita al turno el contexto que hace
        reconocible un compromiso."""
        self.pulse(when=NOW - 60)
        self.wa.set_chat_allowed(CHAT, True, conn=self.conn)
        self.assertEqual(self.wa.closed_windows(self.conn, now=NOW), [])

    def test_silence_closes_it(self):
        self.pulse_conversation()
        self.wa.set_chat_allowed(CHAT, True, conn=self.conn)
        self.assertEqual(len(self.wa.closed_windows(self.conn, now=NOW)), 1)

    def test_a_harvested_window_is_not_harvested_twice(self):
        self.allow()
        self.pulse_conversation()
        first = self.wa.harvest_windows(conn=self.conn, now=NOW)
        self.assertEqual(len(first["created"]), 1)
        again = self.wa.harvest_windows(conn=self.conn, now=NOW)
        self.assertEqual(again["created"], [])
        self.assertEqual(len(self.events()), 1)

    def test_a_too_short_window_is_marked_not_retried_forever(self):
        corto = "5215559999999@s.whatsapp.net"
        self.allow(corto)
        self.pulse(corto, when=NOW - GAP - 60)
        res = self.wa.harvest_windows(conn=self.conn, now=NOW)
        self.assertEqual(res["skipped"], [corto])
        self.assertIsNotNone(self.conn.execute(
            "SELECT harvested_at FROM whatsapp_activity WHERE jid=?", (corto,)).fetchone()[0])


class ThePulseSurvivesASilentWebhook(Base):
    """El webhook es el camino rápido, no el único.

    Se midió: 29 mensajes espejados y cero pulsos, porque un 429 nuestro tumbó
    los primeros intentos de wacli y después se quedó callado. Lo caro es que el
    síntoma de un webhook caído es **idéntico al de nadie escribiéndote** — un
    sistema que solo lo tenga a él se queda mudo y parece tranquilo. Reconciliar
    contra el espejo es lo que convierte esa falla silenciosa en recuperable.
    """

    def marca(self):
        r = self.conn.execute(
            "SELECT last_seen_ts FROM capture_watermarks WHERE source_kind='whatsapp'"
        ).fetchone()
        return r[0] if r else None

    def test_it_rebuilds_the_pulse_nobody_reported(self):
        self.wa.reconcile_activity(conn=self.conn, now=NOW - 7200)   # fija la marca
        self.wa.reconcile_activity(conn=self.conn, now=NOW)
        n, ultimo = self.conn.execute(
            "SELECT message_count, last_seen_at FROM whatsapp_activity WHERE jid=?",
            (CHAT,)).fetchone()
        self.assertEqual(n, 3)
        self.assertEqual(ultimo, NOW - 3600 + 120)

    def test_the_first_run_does_not_swallow_the_whole_archive(self):
        """Sin marca previa se arranca en AHORA: la primera corrida no puede
        convertir años de espejo en un pulso masivo."""
        res = self.wa.reconcile_activity(conn=self.conn, now=NOW)
        self.assertEqual(res["mensajes"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM whatsapp_activity").fetchone()[0], 0)

    def test_running_it_twice_does_not_double_count(self):
        self.wa.reconcile_activity(conn=self.conn, now=NOW - 7200)
        self.wa.reconcile_activity(conn=self.conn, now=NOW)
        antes = self.conn.execute(
            "SELECT message_count FROM whatsapp_activity WHERE jid=?", (CHAT,)).fetchone()[0]
        self.wa.reconcile_activity(conn=self.conn, now=NOW)
        self.assertEqual(self.conn.execute(
            "SELECT message_count FROM whatsapp_activity WHERE jid=?", (CHAT,)).fetchone()[0],
            antes, "la marca impide recontar")

    def test_an_unallowed_chat_still_leaves_only_a_pulse(self):
        """La reconciliación no es una puerta trasera al contenido: deja lo mismo
        que dejaría el webhook — un pulso y un alta denegada."""
        self.wa.reconcile_activity(conn=self.conn, now=NOW - 7200)
        self.wa.reconcile_activity(conn=self.conn, now=NOW)
        self.assertGreater(self.conn.execute(
            "SELECT message_count FROM whatsapp_activity WHERE jid=?", (GRUPO,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT allowed FROM whatsapp_chats WHERE jid=?", (GRUPO,)).fetchone()[0], 0)
        self.assertEqual(self.wa.harvest_windows(conn=self.conn, now=NOW)["created"], [])
        self.assertEqual(self.events(), [])

    def test_a_reconciled_pulse_feeds_the_harvest(self):
        """La prueba que importa: sin webhook, el circuito completo igual cierra."""
        self.allow()
        self.wa.reconcile_activity(conn=self.conn, now=NOW - 7200)
        self.wa.reconcile_activity(conn=self.conn, now=NOW)
        self.assertEqual(len(self.wa.harvest_windows(conn=self.conn, now=NOW)["created"]), 1)

    def test_an_unreadable_mirror_does_not_move_the_watermark(self):
        """Si «no pude leer» avanzara la marca, esos mensajes se perderían para
        siempre — y en silencio."""
        self.wa.reconcile_activity(conn=self.conn, now=NOW - 7200)
        antes = self.marca()
        self.wa.WACLI_DB = self.mirror_path.parent / "no-existe.db"
        res = self.wa.reconcile_activity(conn=self.conn, now=NOW)
        self.assertEqual(res["status"], "error")
        self.assertEqual(self.marca(), antes)

    def test_a_deleted_message_is_not_resurrected_by_the_reconciliation(self):
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("UPDATE messages SET revoked = 1 WHERE chat_jid = ?", (CHAT,))
        mm.commit(); mm.close()
        self.wa.reconcile_activity(conn=self.conn, now=NOW - 7200)
        self.wa.reconcile_activity(conn=self.conn, now=NOW)
        self.assertIsNone(self.conn.execute(
            "SELECT message_count FROM whatsapp_activity WHERE jid=?", (CHAT,)).fetchone())


class AllowingIsFromNowOn(Base):
    """Marcar un chat entrega lo que pase de ahí en adelante, no el historial que
    el espejo ya haya acumulado. Sin este piso, un clic sobre un chat viejo
    entrega meses de conversación que el operador nunca decidió dar — y leer no se
    deshace. Bajar historial es una decisión aparte."""

    def test_history_before_the_decision_is_not_harvested(self):
        self.pulse_conversation()                      # la conversación ya ocurrió
        self.wa.set_chat_allowed(CHAT, True, conn=self.conn)   # y recién ahora se autoriza
        res = self.wa.harvest_windows(conn=self.conn, now=NOW)
        self.assertEqual(res["created"], [])
        self.assertEqual(self.events(), [], "autorizar no alcanza hacia atrás")

    def test_the_stale_window_is_closed_not_retried_forever(self):
        self.pulse_conversation()
        self.wa.set_chat_allowed(CHAT, True, conn=self.conn)
        self.assertEqual(self.wa.harvest_windows(conn=self.conn, now=NOW)["skipped"], [CHAT])
        self.assertIsNotNone(self.conn.execute(
            "SELECT harvested_at FROM whatsapp_activity WHERE jid=?", (CHAT,)).fetchone()[0])

    def test_what_arrives_after_the_decision_flows_normally(self):
        self.allow(when=NOW - 7200)
        self.pulse_conversation()
        self.assertEqual(len(self.wa.harvest_windows(conn=self.conn, now=NOW)["created"]), 1)


class TheEventLooksLikeAnyOther(Base):
    def harvest(self):
        self.allow()
        self.pulse_conversation()
        self.wa.harvest_windows(conn=self.conn, now=NOW)
        return json.loads(self.events()[0][2])

    def test_messages_become_sentences_with_speaker_and_time(self):
        """Esa equivalencia es lo que deja reusar el turno, el gate de citas y las
        tarjetas sin una sola rama por fuente."""
        p = self.harvest()
        self.assertEqual(sorted(p["sentences"][0].keys()),
                         ["index", "speaker", "start_time", "text"])
        self.assertEqual(len(p["sentences"]), 3)
        self.assertEqual(p["title"], "Cliente Demo")

    def test_ricardos_own_messages_are_attributed_to_him(self):
        """`from_me` es la mitad difícil del modo sombra: sin ella se pierden
        justo los compromisos que él asumió."""
        p = self.harvest()
        suyas = [s for s in p["sentences"] if s["speaker"] == "operador"]
        self.assertEqual(len(suyas), 1)
        self.assertIn("te la mando", suyas[0]["text"])

    def test_the_quote_gate_can_verify_against_it(self):
        from dashboard import digestion as dg
        p = self.harvest()
        citables = dg.quotable_texts(p)
        self.assertIn("Va, te la mando el jueves.", citables)

    def test_a_second_harvest_of_the_same_window_is_the_same_event(self):
        from dashboard import digestion as dg
        a = dg.event_id_for("whatsapp", CHAT, 100, 200)
        b = dg.event_id_for("whatsapp", CHAT, 100, 200)
        self.assertEqual(a, b)


class TheMirrorIsPhysicallyReadOnly(Base):
    """El store de wacli sostiene la SESIÓN emparejada. Corromperlo obligaría al
    operador a re-emparejar — y cada vinculación nueva es exactamente el tipo de
    actividad que sube el riesgo de ban.

    Conductual, no textual: se intenta escribir de verdad. Un test que solo
    buscara "mode=ro" en el código pasaría con una conexión que resultara
    escribible por cualquier otra razón.
    """

    def test_writing_through_the_mirror_connection_fails(self):
        conn = self.wa._mirror()
        self.assertIsNotNone(conn)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO chats (jid, name) VALUES ('x@s.whatsapp.net','x')")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages")
        finally:
            conn.close()

    def test_reading_still_works(self):
        conn = self.wa._mirror()
        try:
            self.assertGreater(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        finally:
            conn.close()

    def test_a_missing_mirror_is_not_an_error(self):
        """Antes de emparejar no hay espejo; eso no puede tumbar el tick."""
        saved, self.wa.WACLI_DB = self.wa.WACLI_DB, Path("/no/existe/wacli.db")
        try:
            self.assertIsNone(self.wa._mirror())
            self.assertIsNone(self.wa.read_window(CHAT, 0, 9))
        finally:
            self.wa.WACLI_DB = saved


class DeletedMessagesDoNotResurface(Base):
    def test_a_revoked_message_is_not_evidence(self):
        """Un mensaje que alguien borró no debe reaparecer citado en una tarjeta."""
        m = sqlite3.connect(str(self.mirror_path))
        m.execute("UPDATE messages SET revoked=1 WHERE text LIKE 'Va, te la mando%'")
        m.commit(); m.close()
        w = self.wa.read_window(CHAT, 0, 9999999999)
        self.assertIsNone(w, "quedan 2 mensajes, por debajo del mínimo de ventana")


if __name__ == "__main__":
    unittest.main()
