"""Aprobar en bloque tiene que ser SEGURO, no nada más rápido.

Lo que se prueba aquí no es que la pantalla funcione, sino que las tres formas
conocidas de autorizar de más estén cerradas: aprobar una inferencia en bloque,
confirmar algo distinto de lo que se enseñó, y decidir a partir del contenido que
justamente se está protegiendo.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import db  # noqa: E402
from dashboard.migrations import m15_differential_capture as m15  # noqa: E402
from dashboard.migrations import m20_whatsapp_allowlist as m20  # noqa: E402
from dashboard.migrations import m21_whatsapp_verdicts as m21  # noqa: E402

NOW = 1785900000
SECRETO = "Los resultados del laboratorio salieron bien"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mirror_path = root / "wacli.db"
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("CREATE TABLE messages (chat_jid TEXT, ts INTEGER, from_me INTEGER, "
                   "text TEXT)")
        self.b2b = [f"g{i}@g.us" for i in range(30)]
        for jid in self.b2b:
            for k in range(20):
                mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                           (jid, NOW - 86400 * k, k % 2, "hola"))
        # Un chat con contenido privado: nada de esto puede salir en la revisión.
        for k in range(30):
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                       ("fam@g.us", NOW - 86400 * k, 0, SECRETO))
        mm.commit(); mm.close()

        path = root / "kanban.db"
        conn = sqlite3.connect(str(path))
        for t in ("deals", "projects", "accounts"):
            conn.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE contacts (id TEXT PRIMARY KEY, name TEXT, account_id TEXT)")
        m15.apply(conn); m20.apply(conn); m21.apply(conn)
        for i, jid in enumerate(self.b2b):
            conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                         "chat_name, verdict, verdict_source, verdict_reason) "
                         "VALUES (?,0,1,?,?,'negocio','patron_b2b',?)",
                         (jid, NOW, f"Cliente{i} <> Hacsys", "patrón «A <> B»"))
        conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                     "chat_name, verdict, verdict_source, verdict_reason) "
                     "VALUES ('inf@g.us',0,1,?,'Equipo Marketing','negocio','modelo','suena a trabajo')",
                     (NOW,))
        conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                     "chat_name, verdict, verdict_source, verdict_reason) "
                     "VALUES ('fam@g.us',0,1,?,'Familia','personal','modelo','familiar')", (NOW,))
        conn.commit(); conn.close()

        self._saved_db, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.whatsapp as wa
        import dashboard.whatsapp_review as rv
        self.wa, self.rv = wa, rv
        self._saved_mirror, wa.WACLI_DB = wa.WACLI_DB, self.mirror_path
        self._saved_now, rv._now = rv._now, lambda: NOW
        self._saved_wnow, wa._now = wa._now, lambda: NOW
        rv._lotes.clear()
        self.conn = sqlite3.connect(str(path))

    def tearDown(self):
        self.conn.close()
        self.wa.WACLI_DB, self.wa._now = self._saved_mirror, self._saved_wnow
        self.rv._now = self._saved_now
        db.KANBAN_DB = self._saved_db
        self.tmp.cleanup()

    def permitidos(self):
        return self.conn.execute(
            "SELECT count(*) FROM whatsapp_chats WHERE allowed = 1").fetchone()[0]


class OnlyAVerifiableMatchCanBeBulkApproved(Base):
    def test_an_inference_lane_refuses_to_stage(self):
        """Un modelo que leyó un nombre y le pareció de trabajo no alcanza para
        autorizar treinta conversaciones de un clic."""
        r = self.rv.stage("modelo", conn=self.conn)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"], "carril_no_masivo")
        self.assertEqual(self.permitidos(), 0)

    def test_the_deterministic_lane_stages(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["regla"])

    def test_the_inference_lane_still_appears_for_one_by_one(self):
        """Negarle el bloque no es esconderlo: sigue en la cola, de uno en uno."""
        ids = [c["id"] for c in self.rv.review(conn=self.conn)["carriles"]]
        self.assertIn("modelo", ids)


class StagingGrantsNothing(Base):
    def test_staging_authorizes_nobody(self):
        self.rv.stage("patron_b2b", conn=self.conn)
        self.assertEqual(self.permitidos(), 0, "fijar un lote no es autorizarlo")

    def test_only_commit_writes_permission(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertEqual(self.permitidos(), r["cuantos"])


class WhatYouSawIsWhatYouAuthorize(Base):
    def test_the_commit_ignores_anything_the_client_did_not_stage(self):
        """La confirmación manda un identificador, no una lista. Un navegador que
        agregue un chat al arreglo no puede autorizarlo."""
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv._lotes[r["batch_id"]]  # el lote vive en el servidor
        self.rv.commit(r["batch_id"], conn=self.conn)
        fam = self.conn.execute(
            "SELECT allowed FROM whatsapp_chats WHERE jid='fam@g.us'").fetchone()[0]
        self.assertEqual(fam, 0)

    def test_removing_from_the_batch_removes_it_from_the_commit(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        fuera = r["chats"][0]["jid"]
        self.rv.unstage(r["batch_id"], fuera)
        hecho = self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertNotIn(fuera, hecho["permitidos"])
        self.assertEqual(self.conn.execute(
            "SELECT allowed FROM whatsapp_chats WHERE jid=?", (fuera,)).fetchone()[0], 0)

    def test_a_batch_cannot_be_committed_twice(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.commit(r["batch_id"], conn=self.conn)
        again = self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertEqual(again["status"], "error")

    def test_an_expired_batch_is_refused(self):
        """Un lote viejo describe un mundo que ya cambió."""
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv._lotes[r["batch_id"]]["creado"] = NOW - self.rv.LOTE_TTL - 1
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["error"],
                         "lote_expirado")
        self.assertEqual(self.permitidos(), 0)

    def test_an_unknown_batch_grants_nothing(self):
        self.assertEqual(self.rv.commit("lote_inventado", conn=self.conn)["status"], "error")
        self.assertEqual(self.permitidos(), 0)


class TheVerbsWorkTheWayProductionCallsThem(Base):
    """Las rutas HTTP no pasan conexión: la abren los verbos. Un test que siempre
    inyecta `conn` prueba una ruta que nadie ejecuta, y deja sin cubrir justo la
    que sí corre."""

    def test_the_whole_flow_runs_without_an_injected_connection(self):
        r = self.rv.review()
        self.assertEqual(r["resumen"]["total"], 32)
        lote = self.rv.stage("patron_b2b")
        self.assertEqual(lote["status"], "ok")
        self.assertEqual(self.rv.commit(lote["batch_id"])["cuantos"], self.rv.MAX_LOTE)
        self.assertEqual(len(self.rv.allowed_chats()), self.rv.MAX_LOTE)

    def test_deciding_works_without_an_injected_connection(self):
        self.rv.decide(self.b2b[0], True)
        self.assertEqual(len(self.rv.allowed_chats()), 1)
        self.rv.decide(self.b2b[0], False)
        self.assertEqual(self.rv.allowed_chats(), [])

    def test_an_empty_shape_query_returns_a_mapping(self):
        """`review` sobre un carril vacío llama a `_forma` sin jids; devolver
        `None` ahí revienta el `.get` de quien lo consume."""
        mc = self.rv._mirror()
        try:
            self.assertEqual(self.rv._forma(mc, []), {})
        finally:
            mc.close()


class TheLaneCarriesItsOwnPermission(Base):
    """El flag `determinista` es lo que la pantalla lee para decidir si pinta el
    botón de aprobar en bloque. Si se invierte, el carril de inferencia se vuelve
    masivo — el servidor lo seguiría rechazando, pero la pantalla ya habría
    ofrecido lo que no debe."""

    def test_exactly_the_verifiable_lanes_are_marked_bulk(self):
        for via in self.rv.review(conn=self.conn)["carriles"]:
            self.assertEqual(via["determinista"], via["id"] in self.rv.CARRILES_DETERMINISTAS,
                             f"el carril {via['id']} está marcado al revés")

    def test_the_inference_lane_is_not_marked_bulk(self):
        via = next(v for v in self.rv.review(conn=self.conn)["carriles"] if v["id"] == "modelo")
        self.assertFalse(via["determinista"])


class TheNumbersAreTheOnesHeDecidesWith(Base):
    """La forma es todo lo que el operador tiene para decidir, porque el contenido no
    se le enseña. Un número mal calculado ahí no es cosmético: es la única
    evidencia sobre la que está decidiendo."""

    def forma_de(self, jid):
        mc = self.rv._mirror()
        try:
            return self.rv._forma(mc, [jid]).get(jid, {})
        finally:
            mc.close()

    def test_the_shape_is_computed_from_the_real_messages(self):
        """Con números elegidos para que ninguna cuenta salga bien por accidente:
        un cero se multiplica y se divide igual, así que el fixture no puede
        tener ceros donde se está probando la aritmética."""
        mm = sqlite3.connect(str(self.mirror_path))
        # 8 mensajes: 2 míos, 4 en fin de semana, el último hace 5 días.
        # NOW es martes 2026-08-04, así que k=9,10 son domingo y sábado.
        for k, mio in [(5, 1), (6, 1), (9, 0), (10, 0), (16, 0), (17, 0), (11, 0), (12, 0)]:
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                       ("medido@g.us", NOW - 86400 * k, mio, "x"))
        mm.commit(); mm.close()
        f = self.forma_de("medido@g.us")
        self.assertEqual(f["mensajes"], 8)
        self.assertEqual(f["mios_pct"], 25)          # 2 de 8
        self.assertEqual(f["dias_sin"], 5)           # ≠ 0: la división se nota
        self.assertEqual(f["antiguedad_dias"], 17)
        self.assertEqual(f["finde_pct"], 50)         # 4 de 8 caen en fin de semana

    def test_a_chat_with_no_mirror_rows_has_no_shape_at_all(self):
        self.assertEqual(self.forma_de("nadie@g.us"), {})

    def test_the_silent_denials_are_exactly_the_undecided(self):
        r = self.rv.review(conn=self.conn)["resumen"]
        self.assertEqual(r["denegados_en_silencio"], r["total"] - r["decididos"])

    def test_the_count_moves_when_he_decides(self):
        antes = self.rv.review(conn=self.conn)["resumen"]["denegados_en_silencio"]
        self.rv.decide(self.b2b[0], False, conn=self.conn)
        self.assertEqual(self.rv.review(conn=self.conn)["resumen"]["denegados_en_silencio"],
                         antes - 1)


class TheGuardsHoldWhenCalledDirectly(Base):
    """Los verbos son la API: no basta con que la pantalla no los llame mal."""

    def test_an_unknown_batch_cannot_be_edited(self):
        self.assertEqual(self.rv.unstage("lote_inventado", "x")["error"], "lote_desconocido")

    def test_an_oversized_batch_is_refused_at_commit(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv._lotes[r["batch_id"]]["jids"] = list(self.b2b)      # 30 > el tope
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["error"], "lote_excedido")
        self.assertEqual(self.permitidos(), 0)

    def test_a_spent_batch_is_gone_not_merely_stale(self):
        """Confirmado el lote, su identificador deja de existir. Que el segundo
        intento falle por «desfasado» sería fallar por la razón equivocada."""
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["error"],
                         "lote_desconocido")

    def test_the_commit_names_what_it_authorized(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        hecho = self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertEqual(sorted(hecho["permitidos"]), sorted(c["jid"] for c in r["chats"]))


class TheBatchIsRederivedBeforeApplying(Base):
    """Entre fijar el lote y confirmarlo el mundo se mueve. Aplicar la foto vieja
    autorizaría algo que entre tanto ya se había decidido que no."""

    def test_a_chat_denied_meanwhile_blocks_the_whole_commit(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        arrepentido = r["chats"][0]["jid"]
        self.rv.decide(arrepentido, False, conn=self.conn)      # otra pestaña, otro momento
        out = self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertEqual(out["error"], "lote_desfasado")
        self.assertIn(arrepentido, out["cambiaron"])
        self.assertEqual(self.permitidos(), 0, "no se aplica nada: se vuelve a enseñar")

    def test_a_batch_nobody_touched_still_applies(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["status"], "ok")


class RevokingAlsoTakesBackTheText(Base):
    """Quitar el permiso conservando el verbatim es apagar la llave solo hacia
    adelante. Lo crudo existe para ser digerido; sin permiso deja de tener razón
    de estar guardado."""

    def seed_capture(self, jid):
        self.conn.execute(
            "INSERT INTO capture_events (event_id, source_kind, source_ref, payload, "
            "captured_at, digest_status) VALUES (?,?,?,?,?,'pending')",
            (f"ev_{jid}", "whatsapp", jid, '{"sentences":[{"text":"%s"}]}' % SECRETO, NOW))
        self.conn.commit()

    def test_revoking_empties_the_raw_payload(self):
        jid = self.b2b[0]
        self.seed_capture(jid)
        self.rv.decide(jid, True, conn=self.conn)
        out = self.rv.decide(jid, False, conn=self.conn)
        self.assertEqual(out["crudo_purgado"], 1)
        payload, purgado = self.conn.execute(
            "SELECT payload, payload_purged_at FROM capture_events WHERE source_ref = ?",
            (jid,)).fetchone()
        self.assertIsNone(payload)
        self.assertIsNotNone(purgado, "queda constancia de que se purgó")

    def test_the_purge_survives_the_connection(self):
        """Si la purga no se persiste, la revocación se ve bien en pantalla y el
        texto sigue en disco — el peor de los dos mundos."""
        jid = self.b2b[5]
        self.seed_capture(jid)
        self.rv.decide(jid, False, conn=self.conn)
        otra = sqlite3.connect(str(db.KANBAN_DB))
        try:
            payload = otra.execute(
                "SELECT payload FROM capture_events WHERE source_ref = ?", (jid,)).fetchone()[0]
        finally:
            otra.close()
        self.assertIsNone(payload, "la purga no quedó escrita en disco")

    def test_the_row_survives_so_the_revocation_is_auditable(self):
        jid = self.b2b[1]
        self.seed_capture(jid)
        self.rv.decide(jid, False, conn=self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events WHERE source_ref = ?", (jid,)).fetchone()[0], 1)

    def test_another_chats_capture_is_untouched(self):
        mio, ajeno = self.b2b[2], self.b2b[3]
        self.seed_capture(mio); self.seed_capture(ajeno)
        self.rv.decide(mio, False, conn=self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT payload FROM capture_events WHERE source_ref = ?", (ajeno,)).fetchone()[0])

    def test_allowing_purges_nothing(self):
        jid = self.b2b[4]
        self.seed_capture(jid)
        self.rv.decide(jid, True, conn=self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT payload FROM capture_events WHERE source_ref = ?", (jid,)).fetchone()[0])


class TheBatchIsBounded(Base):
    def test_thirty_candidates_stage_at_most_the_cap(self):
        """Treinta decisiones seguidas agotan la atención, y ahí es donde alguien
        aprueba de corrido. Se revisa en tandas."""
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.assertEqual(r["cuantos"], self.rv.MAX_LOTE)
        self.assertEqual(r["restantes"], 30 - self.rv.MAX_LOTE)

    def test_a_lane_that_fits_reports_nothing_left_over(self):
        """«Quedan 1 para la siguiente tanda» cuando no queda ninguno manda al
        humano a buscar una tanda que no existe."""
        r = self.rv.stage("crm", conn=self.conn) if False else None
        self.conn.execute("UPDATE whatsapp_chats SET decided_at = ? WHERE verdict_source = "
                          "'patron_b2b' AND jid NOT IN (?,?)", (NOW, self.b2b[0], self.b2b[1]))
        self.conn.commit()
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.assertEqual(r["cuantos"], 2)
        self.assertEqual(r["restantes"], 0)

    def test_a_stale_batch_is_thrown_away_not_left_lying_around(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.decide(r["chats"][0]["jid"], False, conn=self.conn)
        self.rv.commit(r["batch_id"], conn=self.conn)          # → desfasado
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["error"],
                         "lote_desconocido")

    def test_the_rest_survive_for_the_next_round(self):
        r1 = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.commit(r1["batch_id"], conn=self.conn)
        r2 = self.rv.stage("patron_b2b", conn=self.conn)
        self.assertEqual(r2["cuantos"], 30 - self.rv.MAX_LOTE)


class TheDecisionIsMadeWithoutTheContent(Base):
    def test_no_message_text_reaches_the_review_surface(self):
        """La prueba central: si para decidir hay que leer, ya se leyó."""
        blob = repr(self.rv.review(conn=self.conn))
        self.assertNotIn(SECRETO, blob)
        self.assertNotIn("hola", blob)

    def test_no_message_text_reaches_the_staging_preview(self):
        blob = repr(self.rv.stage("patron_b2b", conn=self.conn))
        self.assertNotIn(SECRETO, blob)
        self.assertNotIn("hola", blob)

    def test_the_shape_is_there_instead(self):
        c = self.rv.review(conn=self.conn)["carriles"][0]["chats"][0]
        self.assertIn("mensajes", c["forma"])
        self.assertIn("mios_pct", c["forma"])
        self.assertIn("dias_sin", c["forma"])


class ThePreviewArguesBothWays(Base):
    def test_a_dormant_chat_says_so(self):
        """Una vista previa que solo acumula razones para decir que sí entrena el
        sello automático."""
        self.conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                          "chat_name, verdict, verdict_source, verdict_reason) VALUES "
                          "('viejo@g.us',0,1,?,'Zeta <> Hacsys','negocio','patron_b2b','x')",
                          (NOW,))
        self.conn.commit()
        mm = sqlite3.connect(str(self.mirror_path))
        for k in range(10):
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                       ("viejo@g.us", NOW - 86400 * (400 + k), 0, "x"))
        mm.commit(); mm.close()
        chats = {c["jid"]: c for c in self.rv.review(conn=self.conn)["carriles"][0]["chats"]}
        self.assertTrue(any("sin actividad" in a for a in chats["viejo@g.us"]["contra"]))

    def test_a_barely_mirrored_chat_says_how_little_there_is(self):
        """Tres mensajes bajados no son un chat de tres mensajes: es un espejo que
        apenas empezó. Confundirlos hace que el operador descarte relaciones reales
        por parecer vacías."""
        self.conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                          "chat_name, verdict, verdict_source, verdict_reason) VALUES "
                          "('poco@g.us',0,1,?,'Poco <> Hacsys','negocio','patron_b2b','x')",
                          (NOW,))
        self.conn.commit()
        mm = sqlite3.connect(str(self.mirror_path))
        for k in range(3):
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)", ("poco@g.us", NOW - k * 60, 1, "x"))
        mm.commit(); mm.close()
        c = next(c for via in self.rv.review(conn=self.conn)["carriles"]
                 for c in via["chats"] if c["jid"] == "poco@g.us")
        self.assertTrue(any("solo 3 mensajes" in a for a in c["contra"]), c["contra"])

    def test_a_chat_with_no_mirrored_messages_admits_it(self):
        """Sin datos no se juzga la forma; decirlo es más honesto que un renglón
        que parece completo."""
        self.conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                          "chat_name, verdict, verdict_source, verdict_reason) VALUES "
                          "('vacio@g.us',0,1,?,'Nuevo <> Hacsys','negocio','patron_b2b','x')",
                          (NOW,))
        self.conn.commit()
        chats = {c["jid"]: c for c in self.rv.review(conn=self.conn)["carriles"][0]["chats"]}
        self.assertTrue(any("sin mensajes" in a for a in chats["vacio@g.us"]["contra"]))

    def test_a_healthy_recent_chat_raises_no_objection(self):
        """La contra-evidencia tiene que ser escasa para que signifique algo. Si
        cada renglón trae un ⚠, el ⚠ deja de leerse — que es exactamente cómo se
        vuelve a aprobar de corrido."""
        c = next(c for via in self.rv.review(conn=self.conn)["carriles"]
                 for c in via["chats"] if c["jid"] == self.b2b[0])
        self.assertEqual(c["contra"], [], f"objeciones de más: {c['contra']}")

    def test_the_queue_leads_with_what_is_alive(self):
        """Lo más reciente arriba: es donde una decisión cambia algo hoy. El
        fixture les da fechas distintas a propósito — con todos iguales, el orden
        de inserción pasaría por orden correcto."""
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("DELETE FROM messages WHERE chat_jid IN (?,?,?)", tuple(self.b2b[:3]))
        for jid, dias in [(self.b2b[0], 30), (self.b2b[1], 2), (self.b2b[2], 12)]:
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                       (jid, NOW - 86400 * dias, 1, "x"))
        mm.commit(); mm.close()
        chats = self.rv.review(conn=self.conn)["carriles"][0]["chats"]
        orden = [c["forma"].get("dias_sin", 9999) for c in chats]
        self.assertEqual(orden, sorted(orden))
        pos = {c["jid"]: i for i, c in enumerate(chats)}
        self.assertLess(pos[self.b2b[1]], pos[self.b2b[2]])   # 2 días antes que 12
        self.assertLess(pos[self.b2b[2]], pos[self.b2b[0]])   # 12 antes que 30

    def test_a_weekend_heavy_chat_says_so(self):
        """Un chat que vive en sábado y domingo puede ser de negocio, pero esa
        duda tiene que aparecer en el mismo renglón, no en la letra chica."""
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("DELETE FROM messages WHERE chat_jid = ?", (self.b2b[6],))
        for k in (9, 10, 16, 17, 5):        # cuatro de cinco caen en fin de semana
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                       (self.b2b[6], NOW - 86400 * k, 1, "x"))
        mm.commit(); mm.close()
        c = next(c for via in self.rv.review(conn=self.conn)["carriles"]
                 for c in via["chats"] if c["jid"] == self.b2b[6])
        self.assertTrue(any("fin de semana" in a for a in c["contra"]), c["contra"])

    def test_a_chat_he_never_answers_says_so(self):
        """Los proveedores transmiten hacia ti. Que casi no contestes es la señal
        de que ahí no hay compromisos tuyos que minar."""
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("DELETE FROM messages WHERE chat_jid = ?", (self.b2b[7],))
        for k in range(25):
            mm.execute("INSERT INTO messages VALUES (?,?,?,?)",
                       (self.b2b[7], NOW - 3600 * k, 1 if k == 0 else 0, "x"))
        mm.commit(); mm.close()
        c = next(c for via in self.rv.review(conn=self.conn)["carriles"]
                 for c in via["chats"] if c["jid"] == self.b2b[7])
        self.assertTrue(any("no respondes" in a for a in c["contra"]), c["contra"])

    def test_an_empty_lane_cannot_be_staged(self):
        self.conn.execute("UPDATE whatsapp_chats SET decided_at = ? WHERE verdict_source = "
                          "'patron_b2b'", (NOW,))
        self.conn.commit()
        self.assertEqual(self.rv.stage("patron_b2b", conn=self.conn)["error"], "carril_vacio")

    def test_unstaging_reports_what_is_left(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        out = self.rv.unstage(r["batch_id"], r["chats"][0]["jid"])
        self.assertEqual(out["cuantos"], r["cuantos"] - 1)

    def test_a_batch_exactly_at_the_ttl_still_applies(self):
        """El corte es «más viejo que», no «tan viejo como»: expirar un lote que
        acaba de cumplir el plazo obliga a rearmarlo sin razón."""
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv._lotes[r["batch_id"]]["creado"] = NOW - self.rv.LOTE_TTL
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["status"], "ok")

    def test_an_expired_batch_is_discarded_not_left_around(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv._lotes[r["batch_id"]]["creado"] = NOW - self.rv.LOTE_TTL - 1
        self.rv.commit(r["batch_id"], conn=self.conn)
        self.assertEqual(self.rv.commit(r["batch_id"], conn=self.conn)["error"],
                         "lote_desconocido")

    def test_the_sample_is_drawn_fresh_each_time(self):
        """Un diálogo que siempre enseña lo mismo se contesta sin leerlo."""
        vistas = {tuple(c["jid"] for c in self.rv.stage("patron_b2b", conn=self.conn)["muestra"])
                  for _ in range(12)}
        self.assertGreater(len(vistas), 1)


class DecidingOnceIsEnough(Base):
    def test_a_denied_chat_leaves_the_queue(self):
        r = self.rv.review(conn=self.conn)
        jid = r["carriles"][0]["chats"][0]["jid"]
        self.rv.decide(jid, False, conn=self.conn)
        quedan = {c["jid"] for via in self.rv.review(conn=self.conn)["carriles"]
                  for c in via["chats"]}
        self.assertNotIn(jid, quedan, "decir que no una vez basta")

    def test_the_silent_denials_are_counted_not_queued(self):
        """Lo denegado se cuenta, no se encola: así esto no se vuelve una tarea
        eterna."""
        res = self.rv.review(conn=self.conn)["resumen"]
        self.assertEqual(res["permitidos"], 0)
        self.assertGreater(res["denegados_en_silencio"], 0)
        encolados = sum(len(v["chats"]) for v in self.rv.review(conn=self.conn)["carriles"])
        self.assertLess(encolados, res["total"])


class PermissionIsVisibleAndReversible(Base):
    def test_what_is_allowed_can_be_listed(self):
        """Una lista de permisos que no se puede ver completa es una que nadie
        revoca."""
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.commit(r["batch_id"], conn=self.conn)
        vivos = self.rv.allowed_chats(conn=self.conn)
        self.assertEqual(len(vivos), self.rv.MAX_LOTE)
        self.assertTrue(all(v["por"].startswith("regla:") for v in vivos),
                        "la autorización trae consigo por qué se dio")

    def test_listing_does_not_close_a_connection_it_was_handed(self):
        """Un verbo que cierra la conexión de quien lo llamó rompe al siguiente
        verbo de la misma petición, y el síntoma aparece lejos de la causa."""
        self.rv.decide(self.b2b[0], True, conn=self.conn)
        self.rv.allowed_chats(conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM whatsapp_chats").fetchone()[0],
                         32, "la conexión del llamador sigue viva")

    def test_permission_can_be_taken_back(self):
        r = self.rv.stage("patron_b2b", conn=self.conn)
        self.rv.commit(r["batch_id"], conn=self.conn)
        jid = r["chats"][0]["jid"]
        self.rv.decide(jid, False, conn=self.conn)
        self.assertNotIn(jid, [v["jid"] for v in self.rv.allowed_chats(conn=self.conn)])


class BothSidesAreWalkable(Base):
    """Un «no» que no se puede encontrar entre mil es un «no» definitivo. Las dos
    listas tienen que ser buscables o la decisión no es reversible de verdad."""

    def setUp(self):
        super().setUp()
        self.rv.decide(self.b2b[0], True, conn=self.conn)     # Cliente0
        self.rv.decide(self.b2b[1], False, conn=self.conn)    # Cliente1
        self.rv.decide("fam@g.us", False, conn=self.conn)     # Familia

    def test_each_side_lists_only_its_own(self):
        dentro = self.rv.listed_chats(True, conn=self.conn)
        fuera = self.rv.listed_chats(False, conn=self.conn)
        self.assertEqual([c["nombre"] for c in dentro["chats"]], ["Cliente0 <> Hacsys"])
        self.assertEqual(sorted(c["nombre"] for c in fuera["chats"]),
                         ["Cliente1 <> Hacsys", "Familia"])

    def test_the_undecided_appear_on_neither_side(self):
        """Lo que sigue en la cola vive en los carriles; mezclarlo aquí lo haría
        parecer resuelto."""
        jids = {c["jid"] for lado in (True, False)
                for c in self.rv.listed_chats(lado, limit=999, conn=self.conn)["chats"]}
        self.assertNotIn(self.b2b[5], jids)

    def test_a_denied_chat_can_be_found_by_name(self):
        out = self.rv.listed_chats(False, q="famil", conn=self.conn)
        self.assertEqual([c["nombre"] for c in out["chats"]], ["Familia"])
        self.assertEqual(out["total"], 1)

    def test_the_search_ignores_case(self):
        self.assertEqual(self.rv.listed_chats(False, q="FAMILIA", conn=self.conn)["total"], 1)

    def test_searching_one_side_does_not_reach_the_other(self):
        self.assertEqual(self.rv.listed_chats(True, q="famil", conn=self.conn)["total"], 0)

    def test_a_denied_chat_can_be_brought_back(self):
        self.rv.decide("fam@g.us", True, conn=self.conn)
        self.assertIn("Familia", [c["nombre"] for c in
                                  self.rv.listed_chats(True, conn=self.conn)["chats"]])
        self.assertEqual(self.rv.listed_chats(False, q="famil", conn=self.conn)["total"], 0)

    def test_bringing_it_back_is_forward_only(self):
        """Regresar un chat no resucita su historial: sigue valiendo el piso, y
        `decided_at` se refresca al momento de regresarlo."""
        antes = self.conn.execute(
            "SELECT decided_at FROM whatsapp_chats WHERE jid='fam@g.us'").fetchone()[0]
        self.rv._now = lambda: NOW + 500
        self.wa._now = lambda: NOW + 500
        self.rv.decide("fam@g.us", True, conn=self.conn)
        despues = self.conn.execute(
            "SELECT decided_at FROM whatsapp_chats WHERE jid='fam@g.us'").fetchone()[0]
        self.assertGreater(despues, antes)

    def test_a_truncated_list_says_how_many_it_hid(self):
        """Una lista recortada en silencio se lee como completa, y ahí es donde
        alguien concluye que su chat no está."""
        self.rv.sweep_pending(conn=self.conn)
        out = self.rv.listed_chats(False, limit=5, conn=self.conn)
        self.assertEqual(out["mostrados"], 5)
        self.assertGreater(out["total"], 5)

    def test_the_newest_decision_leads(self):
        orden = [c["desde"] for c in self.rv.listed_chats(False, conn=self.conn)["chats"]]
        self.assertEqual(orden, sorted(orden, reverse=True))


class TheSweepOnlyGoesTheCheapWay(Base):
    """Sacar del tracker lo pendiente es la dirección barata: denegar no lee nada
    y se deshace. Por eso este verbo existe y su gemelo —autorizar todo lo
    pendiente— no existe ni va a existir."""

    def test_it_clears_the_queue(self):
        antes = sum(len(v["chats"]) for v in self.rv.review(conn=self.conn)["carriles"])
        self.assertGreater(antes, 0)
        out = self.rv.sweep_pending(conn=self.conn)
        # +1: el chat 'personal' nunca estuvo en la cola, pero también sale.
        self.assertEqual(out["fuera"], antes + 1)
        self.assertEqual(self.rv.review(conn=self.conn)["carriles"], [])

    def test_it_authorizes_nobody(self):
        self.rv.sweep_pending(conn=self.conn)
        self.assertEqual(self.permitidos(), 0, "barrer nunca puede autorizar")

    def test_it_leaves_the_already_authorized_alone(self):
        self.rv.decide(self.b2b[0], True, conn=self.conn)
        out = self.rv.sweep_pending(conn=self.conn)
        self.assertEqual(out["siguen_dentro"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT allowed FROM whatsapp_chats WHERE jid = ?", (self.b2b[0],)).fetchone()[0], 1)

    def test_it_says_who_took_the_chat_out(self):
        """Para distinguir «lo miré y dije que no» de «se fue en el barrido»."""
        self.rv.sweep_pending(conn=self.conn)
        por = self.conn.execute(
            "SELECT DISTINCT decided_by FROM whatsapp_chats WHERE allowed = 0").fetchall()
        self.assertEqual([r[0] for r in por], ["barrido"])

    def test_a_second_sweep_finds_nothing(self):
        self.rv.sweep_pending(conn=self.conn)
        self.assertEqual(self.rv.sweep_pending(conn=self.conn)["fuera"], 0)

    def test_a_chat_that_arrives_later_is_not_predenied(self):
        """El barrido saca lo que hay hoy, no compromete el futuro: un chat nuevo
        entra a la cola normal."""
        self.rv.sweep_pending(conn=self.conn)
        self.conn.execute("INSERT INTO whatsapp_chats (jid, allowed, is_group, created_at, "
                          "chat_name, verdict, verdict_source, verdict_reason) VALUES "
                          "('nuevo@g.us',0,1,?,'Nuevo <> Hacsys','negocio','patron_b2b','x')",
                          (NOW,))
        self.conn.commit()
        colas = [c["jid"] for v in self.rv.review(conn=self.conn)["carriles"] for c in v["chats"]]
        self.assertEqual(colas, ["nuevo@g.us"])


class BackfillIsADifferentDecision(Base):
    """Autorizar es «de aquí en adelante». Bajar lo anterior es la operación menos
    reversible del sistema, así que no se hereda del permiso: se pide aparte, un
    chat a la vez, y se enseña el número antes de leer nada."""

    def setUp(self):
        super().setUp()
        from dashboard.migrations import m22_whatsapp_backfill as m22
        m22.apply(self.conn); self.conn.commit()
        self.jid = self.b2b[0]
        mm = sqlite3.connect(str(self.mirror_path))
        # El espejo REAL de wacli v0.15.2 — con las columnas que el backfill usa
        # para no resucitar lo borrado. Un fixture más pobre haría pasar los tests
        # contra un esquema que no existe.
        mm.execute("DROP TABLE messages")
        mm.execute("CREATE TABLE messages (chat_jid TEXT, ts INTEGER, sender_jid TEXT, "
                   "sender_name TEXT, from_me INTEGER, text TEXT, display_text TEXT, "
                   "revoked INTEGER DEFAULT 0, deleted_for_me INTEGER DEFAULT 0)")
        mm.execute("CREATE TABLE IF NOT EXISTS chats (jid TEXT PRIMARY KEY, name TEXT)")
        mm.execute("INSERT OR IGNORE INTO chats VALUES (?,?)", (self.jid, "Cliente0 <> Hacsys"))
        # Dos conversaciones separadas por silencio, hace 10 y hace 40 días.
        for dia in (10, 40):
            for k in range(6):
                mm.execute("INSERT INTO messages (chat_jid, ts, sender_jid, sender_name, "
                           "from_me, text) VALUES (?,?,?,?,?,?)",
                           (self.jid, NOW - 86400 * dia + k * 60, self.jid, "Ana",
                            k % 2, f"hola {k}"))
        mm.commit(); mm.close()
        # Autorizado HOY: todo ese historial queda debajo del piso.
        self.rv.decide(self.jid, True, conn=self.conn)

    def test_it_refuses_a_chat_nobody_authorized(self):
        """Bajar el pasado no puede ser la puerta de entrada al presente."""
        out = self.rv.backfill(self.b2b[1], dias=90, confirmar=True, conn=self.conn)
        self.assertEqual(out["error"], "chat_no_autorizado")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events").fetchone()[0], 0)

    def test_the_preview_reads_nothing(self):
        out = self.rv.backfill(self.jid, dias=90, conn=self.conn)
        self.assertFalse(out["confirmado"])
        self.assertEqual(out["mensajes"], 12)
        self.assertEqual(out["ventanas"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events").fetchone()[0], 0,
            "previsualizar no es leer")

    def test_confirming_creates_one_event_per_conversation(self):
        out = self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        self.assertTrue(out["confirmado"])
        self.assertEqual(out["eventos"], 2)
        filas = self.conn.execute(
            "SELECT source_kind, source_ref FROM capture_events").fetchall()
        self.assertEqual(len(filas), 2)
        self.assertTrue(all(f[0] == "whatsapp" and f[1] == self.jid for f in filas))

    def test_the_window_is_cut_by_silence_like_the_live_one(self):
        """Trocear por día partiría una conversación a mitad y el turno perdería
        el contexto que hace reconocible un compromiso."""
        self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        for ini, fin in self.conn.execute(
                "SELECT window_start, window_end FROM capture_events"):
            self.assertLess(fin - ini, self.wa.WINDOW_GAP_SECONDS,
                            "una ventana no puede contener un silencio")

    def test_the_range_asked_for_is_the_range_read(self):
        """Pedir 30 días no puede traer los de hace 40."""
        out = self.rv.backfill(self.jid, dias=30, confirmar=True, conn=self.conn)
        self.assertEqual(out["eventos"], 1)
        ini = self.conn.execute("SELECT min(window_start) FROM capture_events").fetchone()[0]
        self.assertGreaterEqual(ini, NOW - 30 * 86400)

    def test_it_never_reaches_above_the_permission(self):
        """De la decisión en adelante manda la captura viva; si el backfill
        pisara ahí, el mismo mensaje entraría por dos caminos.

        El permiso se fecha ANTES de ahora a propósito: si ambos instantes
        coincidieran, quitar el techo no cambiaría nada y el test pasaría con el
        bug puesto."""
        ayer = NOW - 2 * 86400
        self.conn.execute("UPDATE whatsapp_chats SET decided_at = ? WHERE jid = ?",
                          (ayer, self.jid))
        self.conn.commit()
        mm = sqlite3.connect(str(self.mirror_path))
        for k in range(6):        # conversación de AYER: posterior al permiso
            mm.execute("INSERT INTO messages (chat_jid, ts, sender_jid, sender_name, "
                       "from_me, text) VALUES (?,?,?,?,?,?)",
                       (self.jid, NOW - 86400 + k * 60, self.jid, "Ana", 0, "posterior"))
        mm.commit(); mm.close()
        self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        tope = self.conn.execute("SELECT max(window_end) FROM capture_events").fetchone()[0]
        self.assertLessEqual(tope, ayer, "el backfill no puede pisar la captura viva")
        cuerpos = " ".join(r[0] or "" for r in self.conn.execute(
            "SELECT payload FROM capture_events"))
        self.assertNotIn("posterior", cuerpos)

    def test_running_it_twice_reads_nothing_new(self):
        self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        antes = self.conn.execute("SELECT count(*) FROM capture_events").fetchone()[0]
        segunda = self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        self.assertTrue(segunda.get("nada_que_bajar"))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events").fetchone()[0], antes)

    def test_it_leaves_a_record_of_how_far_back_it_went(self):
        """Para poder responder «¿por qué el sistema tiene este mensaje de hace
        dos meses?» con una fecha, no con una suposición."""
        self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        desde, cuando = self.conn.execute(
            "SELECT backfill_from, backfill_at FROM whatsapp_chats WHERE jid = ?",
            (self.jid,)).fetchone()
        self.assertIsNotNone(cuando)
        self.assertLessEqual(desde, NOW - 40 * 86400)

    def test_an_unreadable_mirror_says_so_instead_of_saying_zero(self):
        """El fallo que este repo ya cometió tres veces: «no pude medir» leído
        como «no hay nada». Un cero inventado aquí haría que el operador concluya que
        ese chat no tiene historial que decidir."""
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("DROP TABLE messages"); mm.commit(); mm.close()
        out = self.rv.backfill(self.jid, dias=90, conn=self.conn)
        self.assertEqual(out["error"], "espejo_ilegible")
        self.assertNotIn("mensajes", out, "no puede reportar un conteo que no midió")

    def test_an_unknown_chat_is_refused(self):
        out = self.rv.backfill("nadie@g.us", dias=30, confirmar=True, conn=self.conn)
        self.assertEqual(out["error"], "chat_desconocido")

    def test_what_it_brought_is_actually_on_disk(self):
        """Si no se persiste, la pantalla dice que bajó el historial y el disco no
        lo tiene — se descubriría hasta el siguiente tick."""
        self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        otra = sqlite3.connect(str(db.KANBAN_DB))
        try:
            n = otra.execute("SELECT count(*) FROM capture_events").fetchone()[0]
            marca = otra.execute("SELECT backfill_at FROM whatsapp_chats WHERE jid = ?",
                                 (self.jid,)).fetchone()[0]
        finally:
            otra.close()
        self.assertEqual(n, 2)
        self.assertIsNotNone(marca)

    def test_a_conversation_at_exactly_the_minimum_still_counts(self):
        """El mínimo es «alcanza», no «hay que pasarlo»: descartar una charla de
        exactamente tres mensajes tiraría compromisos reales y cortos."""
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("DELETE FROM messages")
        for k in range(self.wa.MIN_MESSAGES_PER_WINDOW):
            mm.execute("INSERT INTO messages (chat_jid, ts, sender_jid, sender_name, "
                       "from_me, text) VALUES (?,?,?,?,?,?)",
                       (self.jid, NOW - 86400 * 5 + k * 60, self.jid, "Ana", k % 2, "hola"))
        mm.commit(); mm.close()
        out = self.rv.backfill(self.jid, dias=90, conn=self.conn)
        self.assertEqual(out["ventanas"], 1)
        self.assertEqual(out["mensajes"], self.wa.MIN_MESSAGES_PER_WINDOW)

    def test_the_reach_back_is_capped(self):
        out = self.rv.backfill(self.jid, dias=9999, conn=self.conn)
        self.assertEqual(out["dias"], self.rv.BACKFILL_MAX_DIAS)

    def test_a_deleted_message_does_not_come_back(self):
        """Lo que alguien borró no resucita como evidencia de una tarjeta."""
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("UPDATE messages SET revoked = 1 WHERE chat_jid = ?", (self.jid,))
        mm.commit(); mm.close()
        out = self.rv.backfill(self.jid, dias=90, conn=self.conn)
        self.assertEqual(out["mensajes"], 0)

    def test_revoking_purges_what_the_backfill_brought(self):
        """La revocación tiene que alcanzar también a lo que se bajó a mano."""
        self.rv.backfill(self.jid, dias=90, confirmar=True, conn=self.conn)
        out = self.rv.decide(self.jid, False, conn=self.conn)
        self.assertEqual(out["crudo_purgado"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events WHERE payload IS NOT NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
