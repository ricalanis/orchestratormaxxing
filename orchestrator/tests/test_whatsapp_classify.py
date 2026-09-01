"""El clasificador propone; el operador decide.

La garantía que no puede romperse: **ninguna ruta del clasificador escribe
`allowed`**. Un clasificador que se auto-otorga permiso de leer conversaciones
es el fallo del que no se vuelve — marcar de más significa leer algo privado, y
leer no se deshace.

La segunda: ante la duda, `incierto`. Un falso positivo cuesta privacidad; un
falso negativo cuesta dos clics.
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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mirror_path = root / "wacli.db"
        mm = sqlite3.connect(str(self.mirror_path))
        mm.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, kind TEXT)")
        for jid, name, kind in [
                ("g1@g.us", "Nowports <> Hacsys", "group"),
                ("g2@g.us", "boda (Sarahi && JP)", "group"),
                ("p1@s.whatsapp.net", "Poncho Garciga", "dm"),
                ("p2@s.whatsapp.net", "Zambo", "dm"),
                ("p3@s.whatsapp.net", "5218110000000", "dm"),
                ("g3@g.us", "Proyecto Nortex", "group"),
                ("g4@g.us", "San Antonio Team", "group"),
                ("g5@g.us", "Ruben M. Poncho Garciga y equipo", "group")]:
            mm.execute("INSERT INTO chats VALUES (?,?,?)", (jid, name, kind))
        mm.commit(); mm.close()

        path = root / "kanban.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE deals (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE contacts (id TEXT PRIMARY KEY, name TEXT, account_id TEXT)")
        conn.execute("INSERT INTO accounts VALUES ('acc_vertex','Vertex')")
        conn.execute("INSERT INTO contacts VALUES ('c1','Poncho Garciga','acc_vertex')")
        conn.execute("INSERT INTO contacts VALUES ('c2','Antonio','acc_vertex')")
        conn.execute("INSERT INTO projects VALUES ('proj_ens','Nortex')")
        m15.apply(conn); m20.apply(conn); m21.apply(conn)
        conn.commit(); conn.close()

        self._saved_db, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.whatsapp as wa
        import dashboard.whatsapp_classify as cl
        self.wa, self.cl = wa, cl
        self._saved_mirror, wa.WACLI_DB = wa.WACLI_DB, self.mirror_path
        self._saved_now, cl._now = cl._now, lambda: NOW
        self.conn = sqlite3.connect(str(path))
        self.model_calls = []

    def tearDown(self):
        self.conn.close()
        self.wa.WACLI_DB, self.cl._now = self._saved_mirror, self._saved_now
        db.KANBAN_DB = self._saved_db
        self.tmp.cleanup()

    def fake_model(self, veredicto="negocio"):
        def f(names):
            self.model_calls.append(list(names))
            return {n: (veredicto, "motivo de prueba") for n in names}
        return f

    def sync_and_classify(self, classifier=None):
        self.cl.sync_chats_from_mirror(conn=self.conn)
        return self.cl.classify_pending(conn=self.conn,
                                        classifier=classifier or self.fake_model())

    def row(self, jid):
        return self.conn.execute(
            "SELECT verdict, verdict_source, allowed, verdict_reason FROM whatsapp_chats "
            "WHERE jid = ?", (jid,)).fetchone()


class TheVerdictNeverGrantsAccess(unittest.TestCase):
    """Se comprueba por LECTURA DEL CÓDIGO además de por comportamiento: un
    UPDATE de `allowed` dentro del clasificador sería invisible a un test que
    solo mire el resultado de un caso feliz."""

    def test_no_classifier_code_path_writes_allowed(self):
        src = (Path(__file__).resolve().parents[1] /
               "dashboard" / "whatsapp_classify.py").read_text()
        ofensas = [ln.strip() for ln in src.splitlines()
                   if "allowed" in ln.lower() and ("update" in ln.lower()
                                                   or "insert" in ln.lower())
                   and "allowed, is_group" not in ln and not ln.strip().startswith("#")]
        # El único INSERT que menciona `allowed` es el alta con 0 fijo.
        for ln in ofensas:
            self.assertIn("0", ln, f"el clasificador no puede escribir permisos: {ln}")


class SignalPriority(Base):
    def test_a_crm_contact_wins_and_never_reaches_the_model(self):
        """Un empate con el CRM es un hecho verificable; gastarle una llamada al
        modelo, o dejar que una inferencia lo pise, sería peor en ambos sentidos."""
        self.sync_and_classify()
        v, src, allowed, reason = self.row("p1@s.whatsapp.net")
        self.assertEqual((v, src), ("negocio", "crm"))
        self.assertIn("Poncho Garciga", reason)
        self.assertNotIn("Poncho Garciga [1a1]", sum(self.model_calls, []))

    def test_the_b2b_pattern_is_deterministic(self):
        self.sync_and_classify()
        v, src, _, _ = self.row("g1@g.us")
        self.assertEqual((v, src), ("negocio", "patron_b2b"))

    def test_an_entity_name_is_recognised_without_the_model(self):
        self.sync_and_classify()
        v, src, _, reason = self.row("g3@g.us")
        self.assertEqual((v, src), ("negocio", "nombre_entidad"))
        self.assertIn("Nortex", reason)

    def test_only_the_leftovers_reach_the_model(self):
        self.sync_and_classify()
        enviados = sum(self.model_calls, [])
        self.assertIn("boda (Sarahi && JP) [grupo]", enviados)
        self.assertIn("Zambo [1a1]", enviados)
        self.assertNotIn("Nowports <> Hacsys [grupo]", enviados)

    def test_a_phone_number_name_is_never_sent_anywhere(self):
        """Sin nombre no hay señal, y mandar un número al modelo solo expone su
        agenda a cambio de nada."""
        self.sync_and_classify()
        v, _, allowed, _ = self.row("p3@s.whatsapp.net")
        self.assertEqual(v, "incierto")
        self.assertEqual(allowed, 0)
        self.assertNotIn("5218110000000 [1a1]", sum(self.model_calls, []))


class AFirstNameIsNotAnIdentity(Base):
    """Medido en la corrida real: el contacto «Antonio» empató dentro de «San
    Antonio Team». Este carril es el único aprobable en bloque, así que una
    conjetura aquí cuesta privacidad de golpe."""

    def test_a_bare_first_name_does_not_claim_a_chat(self):
        self.sync_and_classify()
        v, src, _, _ = self.row("g4@g.us")
        self.assertNotEqual(src, "crm", "«Antonio» dentro de «San Antonio Team» no es identidad")

    def test_a_full_name_contained_in_a_longer_chat_name_does(self):
        self.sync_and_classify()
        v, src, _, reason = self.row("g5@g.us")
        self.assertEqual((v, src), ("negocio", "crm"))
        self.assertIn("Poncho Garciga", reason)

    def test_an_entity_inside_another_word_does_not_match(self):
        """«Coppel» dentro de «BanCoppel» es otra empresa."""
        self.conn.execute("INSERT INTO accounts VALUES ('a2','Coppel')")
        self.conn.commit()
        import dashboard.whatsapp_classify as cl
        self.assertIsNone(cl.entity_match(self.conn, "BanCoppel- Participantes"))
        self.assertIsNotNone(cl.entity_match(self.conn, "Coppel - instructores"))


class NothingIsEverAutoAllowed(Base):
    def test_every_chat_stays_denied_after_classification(self):
        self.sync_and_classify()
        allowed = self.conn.execute(
            "SELECT count(*) FROM whatsapp_chats WHERE allowed = 1").fetchone()[0]
        self.assertEqual(allowed, 0, "clasificar no es autorizar")

    def test_even_the_strongest_verdict_grants_nothing(self):
        self.sync_and_classify()
        v, src, allowed, _ = self.row("p1@s.whatsapp.net")
        self.assertEqual(v, "negocio")
        self.assertEqual(allowed, 0)

    def test_the_human_decision_survives_a_reclassification(self):
        """El operador autoriza algo que el clasificador cree personal: su decisión
        no puede ser revertida por una corrida posterior."""
        self.sync_and_classify(self.fake_model("personal"))
        self.wa.set_chat_allowed("g2@g.us", True, conn=self.conn)
        self.conn.execute("UPDATE whatsapp_chats SET verdict = NULL")
        self.conn.commit()
        self.cl.classify_pending(conn=self.conn, classifier=self.fake_model("personal"))
        self.assertEqual(self.row("g2@g.us")[2], 1, "el permiso humano manda")


class ModelFailuresDoNotInvent(Base):
    def test_a_dead_worker_leaves_the_verdict_empty(self):
        """Sin veredicto es honesto; inventar uno sería peor que no tenerlo."""
        res = self.sync_and_classify(lambda names: None)
        self.assertGreater(res["fallos"], 0)
        self.assertIsNone(self.row("p2@s.whatsapp.net")[0])

    def test_an_out_of_vocabulary_class_is_discarded(self):
        """Aproximar 'quizá-negocio' a la clase más cercana sería adivinar sobre
        privacidad."""
        self.sync_and_classify(lambda names: {n: ("quizá", "x") for n in names})
        self.assertIsNone(self.row("p2@s.whatsapp.net")[0])

    def test_a_classified_chat_is_not_reclassified(self):
        self.sync_and_classify()
        antes = len(self.model_calls)
        self.cl.classify_pending(conn=self.conn, classifier=self.fake_model())
        self.assertEqual(len(self.model_calls), antes, "no se re-gasta en lo ya decidido")


class TheProposalIsReviewable(Base):
    def test_hard_signals_come_first(self):
        self.sync_and_classify()
        props = self.cl.proposals(conn=self.conn)
        self.assertEqual(props[0]["source"], "crm")
        negocios = [p for p in props if p["verdict"] == "negocio"]
        self.assertEqual(props[:len(negocios)], negocios, "lo accionable arriba")

    def test_every_proposal_carries_its_reason(self):
        """Sin motivo, revisar 1143 filas se vuelve dar clic — y ahí es donde
        alguien autoriza sin querer sus conversaciones familiares."""
        self.sync_and_classify()
        for p in self.cl.proposals(conn=self.conn):
            self.assertTrue(p["reason"], f"{p['jid']} sin motivo")
            self.assertIn(p["source"], ("crm", "patron_b2b", "nombre_entidad", "modelo"))


class SyncRegistersWithoutReading(Base):
    def test_registering_a_chat_stores_only_its_name(self):
        self.cl.sync_chats_from_mirror(conn=self.conn)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(whatsapp_chats)")]
        self.assertIn("chat_name", cols)
        self.assertNotIn("text", cols)
        self.assertNotIn("message", cols)

    def test_a_second_sync_adds_nothing(self):
        a = self.cl.sync_chats_from_mirror(conn=self.conn)
        b = self.cl.sync_chats_from_mirror(conn=self.conn)
        self.assertEqual(a["nuevos"], 8)
        self.assertEqual(b["nuevos"], 0)


if __name__ == "__main__":
    unittest.main()
