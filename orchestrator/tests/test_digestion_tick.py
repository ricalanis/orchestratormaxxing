"""Captura por dos caminos y el tick que los compone.

El webhook y el poll existen los dos a propósito y hacen cosas distintas:

  * el **webhook** avisa rápido pero no trae contenido, así que solo deja un
    acuse durable; si el proceso muere después del 200, el acuse sobrevive;
  * el **poll** es la red: levanta lo que el webhook nunca entregó, y **se
    detiene en la primera junta sin resumen** porque `action_items` es toda
    nuestra evidencia — avanzar el watermark por encima de una junta a medio
    procesar la perdería para siempre.

Ninguno de estos tests toca la red: tanto el fetch como el modelo son seams.
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
from dashboard.migrations import m16_capture_receipts as m16  # noqa: E402

NOW = 1785900000
QUOTE = "Te mando hoy mismo el resumen."


def transcript(tid, ts, summarized=True, title="Nortex <> Ric"):
    return {
        "id": tid, "title": title, "date": ts * 1000,
        "summary": {"action_items": "**Dora**\nEnviar resumen (00:05)" if summarized else "",
                    "overview": "ok"},
        "sentences": [{"index": 1, "speaker_name": "Dora", "text": QUOTE, "start_time": 5.0}],
    }


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
        conn.execute("INSERT INTO deals VALUES ('deal_ens','Nortex')")
        m15.apply(conn); m16.apply(conn)
        conn.commit(); conn.close()
        self._saved, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.digestion as dg
        self.dg = dg
        self._saved_now, dg._now = dg._now, lambda: NOW
        self._saved_worker, self._saved_cli = dg._run_worker, dg._run_cli
        dg._run_worker = lambda argv, stdin_text, timeout: (0, '{"ops": []}', "")
        dg._run_cli = lambda argv, timeout=None: (0, "", "")
        # El espaciado entre tarjetas es real en producción (tope de 20 msg/min
        # por grupo); aquí solo alargaría la suite.
        self._saved_sleep = dg.time.sleep
        dg.time.sleep = lambda s: None
        # `tick()` resuelve los fetchers de forma perezosa, así que sin esto la
        # suite golpearía la API REAL de Fireflies. Un test que sale a la red no
        # solo es lento: es un test cuyo resultado depende de un tercero.
        from dashboard import fireflies as _ff
        self._ff = _ff
        self._saved_fetch = (_ff.fetch_transcripts_rich, _ff.fetch_transcript_rich)
        self.network_calls = []
        _ff.fetch_transcripts_rich = lambda **kw: self.network_calls.append("list") or []
        _ff.fetch_transcript_rich = lambda ref: self.network_calls.append(ref) or None
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.conn.close()
        self.dg._now, self.dg._run_worker = self._saved_now, self._saved_worker
        self.dg._run_cli = self._saved_cli
        self.dg.time.sleep = self._saved_sleep
        self._ff.fetch_transcripts_rich, self._ff.fetch_transcript_rich = self._saved_fetch
        db.KANBAN_DB = self._saved
        self.tmp.cleanup()

    def watermark(self):
        return self.conn.execute(
            "SELECT last_seen_ts, last_seen_id FROM capture_watermarks WHERE source_kind='fireflies'"
        ).fetchone()


class PollCompletenessBoundary(Base):
    def test_the_walk_stops_at_the_first_unsummarized_meeting(self):
        """Saltarse una junta sin resumen la perdería para siempre: el watermark
        nunca volvería atrás a buscarla."""
        feed = [transcript("tr_1", NOW - 300), transcript("tr_2", NOW - 200, summarized=False),
                transcript("tr_3", NOW - 100)]
        res = self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        self.assertEqual(len(res["ingested"]), 1)
        self.assertEqual(res["stopped_at_unsummarized"], "tr_2")
        self.assertEqual(self.watermark()[1], "tr_1")

    def test_the_next_run_picks_it_up_once_summarized(self):
        feed = [transcript("tr_1", NOW - 300), transcript("tr_2", NOW - 200, summarized=False)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        ready = [transcript("tr_1", NOW - 300), transcript("tr_2", NOW - 200)]
        res = self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: ready)
        self.assertEqual(len(res["ingested"]), 1)
        self.assertEqual(self.watermark()[1], "tr_2")

    def test_a_second_poll_over_the_same_feed_is_a_no_op(self):
        feed = [transcript("tr_1", NOW - 300)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        res = self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        self.assertEqual(res["ingested"], [])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events").fetchone()[0], 1)

    def test_same_second_meetings_are_ordered_by_id(self):
        """Sin desempate por id, el watermark podría saltarse una de dos juntas
        que caen en el mismo segundo."""
        feed = [transcript("tr_b", NOW - 100), transcript("tr_a", NOW - 100)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        self.assertEqual(self.watermark(), (NOW - 100, "tr_b"))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_events").fetchone()[0], 2)

    def test_a_failing_fetch_is_reported_not_raised(self):
        def boom(limit=25):
            raise RuntimeError("fireflies caído")
        res = self.dg.poll_fireflies(conn=self.conn, fetch=boom)
        self.assertEqual(res["code"], "poll_failed")

    def test_last_run_advances_even_with_nothing_new(self):
        """La antigüedad del watermark es la alarma de captura parada, así que
        tiene que moverse aunque no haya juntas nuevas."""
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: [])
        row = self.conn.execute("SELECT last_run_at FROM capture_watermarks").fetchone()
        self.assertEqual(row[0], NOW)


class ReceiptDrain(Base):
    def test_a_receipt_becomes_an_event(self):
        self.dg.record_receipt("tr_hook", conn=self.conn)
        res = self.dg.drain_receipts(conn=self.conn,
                                     fetch=lambda ref: transcript(ref, NOW - 50))
        self.assertEqual(res["fetched"], ["tr_hook"])
        row = self.conn.execute("SELECT status, event_id FROM capture_receipts "
                                "WHERE source_ref='tr_hook'").fetchone()
        self.assertEqual(row[0], "fetched")
        self.assertIsNotNone(row[1])

    def test_a_failed_fetch_retries_then_gives_up_visibly(self):
        self.dg.record_receipt("tr_bad", conn=self.conn)
        for _ in range(m16.MAX_RECEIPT_ATTEMPTS):
            self.dg.drain_receipts(conn=self.conn, fetch=lambda ref: None)
        row = self.conn.execute("SELECT status, attempts FROM capture_receipts "
                                "WHERE source_ref='tr_bad'").fetchone()
        self.assertEqual(row, ("failed", m16.MAX_RECEIPT_ATTEMPTS))

    def test_a_fetch_that_raises_is_recorded_not_propagated(self):
        self.dg.record_receipt("tr_boom", conn=self.conn)
        def boom(ref):
            raise RuntimeError("timeout")
        res = self.dg.drain_receipts(conn=self.conn, fetch=boom)
        self.assertEqual(res["failed"], ["tr_boom"])
        err = self.conn.execute("SELECT last_error FROM capture_receipts "
                                "WHERE source_ref='tr_boom'").fetchone()[0]
        self.assertIn("timeout", err)

    def test_a_repeated_webhook_does_not_duplicate_the_receipt(self):
        self.dg.record_receipt("tr_x", conn=self.conn)
        self.dg.record_receipt("tr_x", conn=self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM capture_receipts").fetchone()[0], 1)

    def test_a_fetched_receipt_is_not_re_fetched(self):
        self.dg.record_receipt("tr_once", conn=self.conn)
        self.dg.drain_receipts(conn=self.conn, fetch=lambda ref: transcript(ref, NOW - 50))
        calls = []
        self.dg.drain_receipts(conn=self.conn,
                              fetch=lambda ref: calls.append(ref) or transcript(ref, NOW))
        self.assertEqual(calls, [])


class TickComposition(Base):
    def test_tick_runs_every_stage_and_reports(self):
        self.dg.record_receipt("tr_hook", conn=self.conn)
        out = self.dg.tick(conn=self.conn,
                           budget=60)
        for stage in ("receipts", "poll", "decay", "cards"):
            self.assertIn(stage, out)
        self.assertEqual(out["status"], "ok")

    def test_tick_digests_what_it_captured(self):
        feed = [transcript("tr_1", NOW - 300)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        out = self.dg.tick(conn=self.conn, budget=60)
        self.assertEqual(len(out["digested"]), 1)
        self.assertEqual(self.conn.execute(
            "SELECT digest_status FROM capture_events").fetchone()[0], "digested")

    def test_a_zero_budget_digests_nothing_but_still_completes(self):
        """El presupuesto existe para terminar ANTES de que el worker mate el
        proceso, no para fallar."""
        feed = [transcript("tr_1", NOW - 300)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        out = self.dg.tick(conn=self.conn, budget=0)
        self.assertEqual(out["digested"], [])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.conn.execute(
            "SELECT digest_status FROM capture_events").fetchone()[0], "pending")

    def test_no_event_may_outlive_the_remaining_budget(self):
        """El presupuesto se comprueba antes de cada evento, así que sin acotar
        la llamada al remanente el tick se pasa del techo del worker y lo matan
        a media digestión. Medido en vivo: 758 s con presupuesto de 480 s."""
        seen = []
        self.dg._run_worker = lambda argv, stdin_text, timeout: (
            seen.append(timeout) or (0, '{"ops": []}', ""))
        feed = [transcript("tr_1", NOW - 300), transcript("tr_2", NOW - 200)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        self.dg.tick(conn=self.conn, budget=90)
        self.assertTrue(seen)
        self.assertTrue(all(t <= 90 for t in seen),
                        f"una llamada excedió el presupuesto del tick: {seen}")

    def test_a_sliver_of_budget_does_not_start_an_event(self):
        feed = [transcript("tr_1", NOW - 300)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        out = self.dg.tick(conn=self.conn, budget=self.dg.MIN_DIGEST_SECONDS - 1)
        self.assertEqual(out["digested"], [])
        self.assertTrue(out["budget_exhausted"])

    def test_a_worker_outage_does_not_stop_the_other_stages(self):
        feed = [transcript("tr_1", NOW - 300)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        self.dg._run_worker = lambda argv, stdin_text, timeout: (1, "", "connection refused")
        out = self.dg.tick(conn=self.conn, budget=60)
        self.assertTrue(out["digest_errors"])
        self.assertTrue(out["worker_down"], "un worker caído corta la etapa, no gira en caliente")
        self.assertIn("cards", out)
        self.assertEqual(self.conn.execute(
            "SELECT digest_status FROM capture_events").fetchone()[0], "pending")


class CaptureStatusIsNumbersOnly(Base):
    def test_status_never_leaks_titles_or_quotes(self):
        """Esta es la superficie que puede vivir en scope default, o sea
        alcanzable desde internet: tiene que decir si el loop se atoró sin
        revelar una palabra de una conversación."""
        feed = [transcript("tr_1", NOW - 300, title="Nortex <> Ric")]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        blob = json.dumps(self.dg.capture_status(conn=self.conn), ensure_ascii=False)
        self.assertNotIn("Nortex", blob)
        self.assertNotIn(QUOTE, blob)
        self.assertNotIn("Dora", blob)

    def test_status_reports_the_shape_of_the_queue(self):
        feed = [transcript("tr_1", NOW - 300)]
        self.dg.poll_fireflies(conn=self.conn, fetch=lambda limit=25: feed)
        st = self.dg.capture_status(conn=self.conn)
        self.assertEqual(st["events_by_status"].get("pending"), 1)
        self.assertEqual(st["dead_letter"], 0)
        self.assertEqual(st["watermark_age_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
