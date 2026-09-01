"""El gate humano: aceptar, descartar, editar.

Aceptar cruza a `hermes kanban create`, un efecto externo que no se deshace, así
que la saga tiene que ser segura de reintentar sin duplicar. Tres propiedades lo
sostienen y cada una se prueba contra su forma de romperse:

  * **un solo dueño** — un segundo click en vuelo recibe conflicto, no una tarea
    gemela;
  * **reconciliación por marcador con `instr`, no `LIKE`** — en SQL el guion bajo
    es comodín, así que `LIKE '%[suggestion_ab]%'` reconciliaría contra la tarea
    equivocada, que es peor que no reconciliar;
  * **el efecto externo va por el seam `_run_cli`** — ningún test toca el kanban
    real del operador.
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
QUOTE = "Te mando hoy mismo el resumen."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "kanban.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE deals (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, "
                     "status TEXT DEFAULT 'backlog')")
        conn.execute("INSERT INTO projects VALUES ('proj_1','Demo')")
        m15.apply(conn)
        conn.commit(); conn.close()
        self._saved, db.KANBAN_DB = db.KANBAN_DB, path
        import dashboard.digestion as dg
        self.dg = dg
        self._saved_now, dg._now = dg._now, lambda: NOW
        self._saved_cli = dg._run_cli
        dg._run_cli = self.fake_cli
        self.calls, self.cli_result, self.on_create = [], None, None
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.seed()

    def tearDown(self):
        self.conn.close()
        self.dg._now, self.dg._run_cli = self._saved_now, self._saved_cli
        db.KANBAN_DB = self._saved
        self.tmp.cleanup()

    def fake_cli(self, argv, timeout=None):
        """El seam: nunca corre `hermes` de verdad. Por defecto simula un create
        exitoso INSERTANDO la tarea, para que la reconciliación tenga algo real
        que encontrar."""
        self.calls.append(argv)
        if self.cli_result is not None:
            return self.cli_result
        tid = f"t_{len(self.calls):06x}"
        body = argv[argv.index("--body") + 1] if "--body" in argv else ""
        self.conn.execute("INSERT INTO tasks (id, title, body) VALUES (?,?,?)",
                          (tid, argv[3], body))
        self.conn.commit()
        if self.on_create:
            self.on_create()
        return (0, json.dumps({"id": tid}), "")

    def seed(self, sid="sug_1", kind="create_task", status="open", oid="obj_1"):
        self.conn.execute("INSERT OR IGNORE INTO capture_events (event_id, source_kind, "
                          "source_ref, title, captured_at) "
                          "VALUES ('ev_1','fireflies','tr_1','Junta demo',?)", (NOW,))
        self.conn.execute("INSERT OR IGNORE INTO objectives (id, title, owner, opened_at, "
                          "updated_at) VALUES (?,?,?,?,?)", (oid, "Enviar cotización",
                                                             "Ric", NOW, NOW))
        self.conn.execute("INSERT OR IGNORE INTO objective_evidence (objective_id, event_id, "
                          "anchor, quote, speaker, ts, op, created_at) "
                          "VALUES (?,'ev_1','1',?,'Dora',?,'objective.add',?)",
                          (oid, QUOTE, NOW, NOW))
        self.conn.execute("INSERT INTO suggestions (id, objective_id, kind, status, title, "
                          "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                          (sid, oid, kind, status, "Enviar cotización", NOW, NOW))
        self.conn.commit()
        return sid

    def suggestion(self, sid="sug_1"):
        return self.conn.execute(
            "SELECT status, task_id, decided_via, edited, final_title FROM suggestions "
            "WHERE id = ?", (sid,)).fetchone()

    def accept(self, sid="sug_1", **kw):
        return self.dg.accept_suggestion(sid, conn=self.conn, **kw)


class AcceptCreatesOnce(Base):
    def test_accept_creates_a_task_and_links_it(self):
        res = self.accept()
        self.assertEqual(res["status"], "ok")
        st, task_id, via, _, _ = self.suggestion()
        self.assertEqual(st, "accepted")
        self.assertTrue(task_id)
        self.assertEqual(via, "dashboard")
        # El objetivo guarda su tarea: es lo que deja que un `complete` posterior
        # derive un close_task que sepa qué cerrar.
        self.assertEqual(self.conn.execute(
            "SELECT task_id FROM objectives WHERE id='obj_1'").fetchone()[0], task_id)

    def test_argv_is_pinned_and_carries_the_marker(self):
        self.accept()
        argv = self.calls[0]
        self.assertEqual(argv[1:3], ["kanban", "create"])
        self.assertEqual(argv[3], "Enviar cotización")
        self.assertIn("--json", argv)
        body = argv[argv.index("--body") + 1]
        self.assertIn("[suggestion:sug_1]", body)
        self.assertIn(QUOTE, body, "la tarea debe llevar su evidencia")
        self.assertIn("Dora", body)

    def test_a_replayed_accept_does_not_create_twice(self):
        first = self.accept()
        self.calls.clear()
        again = self.accept()
        self.assertTrue(again["replay"])
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertEqual(self.calls, [])

    def test_a_crash_after_create_adopts_the_task_instead_of_duplicating(self):
        """El intento anterior murió DESPUÉS de crear la tarea. Sin
        reconciliación, el reintento crearía una gemela.

        Dos fases, porque las dos son correctas: mientras el lease sigue vivo el
        reintento se rechaza (podría ser un doble-click, no un intento muerto);
        una vez vencido, se adopta la tarea que sí quedó.
        """
        def die():
            raise RuntimeError("murió tras crear")
        self.on_create = die
        with self.assertRaises(RuntimeError):
            self.accept()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        self.on_create = None
        self.calls.clear()

        # Fase 1: lease vivo → rechazo, sin tocar el CLI.
        self.assertEqual(self.accept()["code"], "accept_in_flight")
        self.assertEqual(self.calls, [])

        # Fase 2: lease vencido → adopta en vez de duplicar.
        self.conn.execute("UPDATE suggestions SET updated_at=? WHERE id='sug_1'",
                          (NOW - self.dg.ACCEPT_LEASE_SECONDS - 1,))
        self.conn.commit()
        res = self.accept()
        self.assertTrue(res["adopted"], "debe adoptar la tarea existente")
        self.assertEqual(self.calls, [], "no debe volver a llamar al CLI")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_the_marker_match_is_exact_not_a_like_pattern(self):
        """`_` es comodín en LIKE, y el id de la sugerencia LLEVA guion bajo.

        El impostor tiene que diferir exactamente donde está ese comodín — o sea
        `sug?1` en vez de `sug_1` — porque ahí es donde `LIKE` haría el match
        falso y ligaría la sugerencia a la tarea de otro.
        """
        self.conn.execute("INSERT INTO tasks (id, title, body) VALUES "
                          "('t_impostor','Otra','[suggestion:sugZ1]')")
        self.conn.commit()
        res = self.accept()
        self.assertNotEqual(res["task_id"], "t_impostor")
        self.assertFalse(res.get("adopted"), "una tarea ajena no debe secuestrar el accept")

    def test_a_second_accept_in_flight_is_refused(self):
        self.conn.execute("UPDATE suggestions SET status='accepting', updated_at=? "
                          "WHERE id='sug_1'", (NOW,))
        self.conn.commit()
        self.assertEqual(self.accept()["code"], "accept_in_flight")
        self.assertEqual(self.calls, [])

    def test_a_stale_accepting_is_recoverable(self):
        """Un `accepting` viejo es un intento muerto, no uno en curso — si no se
        pudiera recuperar, la sugerencia quedaría trabada para siempre."""
        self.conn.execute("UPDATE suggestions SET status='accepting', updated_at=? "
                          "WHERE id='sug_1'", (NOW - self.dg.ACCEPT_LEASE_SECONDS - 1,))
        self.conn.commit()
        self.assertEqual(self.accept()["status"], "ok")

    def test_a_timeout_stays_ambiguous_and_does_not_duplicate(self):
        self.cli_result = (124, "", "timed out")
        self.assertEqual(self.accept()["code"], "accept_ambiguous")
        self.assertEqual(self.suggestion()[0], "accepting")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_a_hard_failure_returns_the_suggestion_to_open(self):
        self.cli_result = (1, "", "boom")
        self.assertEqual(self.accept()["code"], "create_failed")
        self.assertEqual(self.suggestion()[0], "open")

    def test_unblock_creates_a_destrabar_task(self):
        self.conn.execute("UPDATE objectives SET waiting_on='Dora' WHERE id='obj_1'")
        self.conn.execute("UPDATE suggestions SET kind='unblock' WHERE id='sug_1'")
        self.conn.commit()
        self.accept()
        self.assertTrue(self.calls[0][3].startswith("Destrabar:"))
        self.assertIn("Esperando a: Dora", self.calls[0][self.calls[0].index("--body") + 1])


class FilingIsReportedNotSwallowed(Base):
    """Una tarea que el operador cree archivada y no lo está es peor que una que sabe
    que quedó suelta: la primera no la vuelve a buscar.

    La tarea ya existe cuando esto corre, y eso no se deshace, así que un fallo
    de archivado no puede abortar el accept — pero sí tiene que decirse.
    """

    def with_project(self, pid="proj_1"):
        self.conn.execute("UPDATE suggestions SET proposed_project_id=? WHERE id='sug_1'", (pid,))
        self.conn.commit()

    def test_a_successful_filing_is_reported(self):
        self.with_project()
        from dashboard import sprints
        saved = sprints.assign_task_project
        sprints.assign_task_project = lambda t, p: {"status": "ok"}
        try:
            res = self.accept()
        finally:
            sprints.assign_task_project = saved
        self.assertTrue(res["project_applied"])

    def test_a_failed_filing_still_accepts_but_says_so(self):
        self.with_project()
        from dashboard import sprints
        saved = sprints.assign_task_project

        def boom(t, p):
            raise RuntimeError("proyecto archivado")

        sprints.assign_task_project = boom
        try:
            res = self.accept()
        finally:
            sprints.assign_task_project = saved
        self.assertEqual(res["status"], "ok", "la tarea existe; el accept no se aborta")
        self.assertTrue(res["task_id"])
        self.assertFalse(res["project_applied"], "el fallo debe viajar hasta el humano")
        self.assertIn("proyecto archivado", res["project_error"])

    def test_a_typed_error_dict_is_also_a_failure(self):
        """`assign_task_project` devuelve dicts de error en vez de lanzar — un
        try/except no lo vería."""
        self.with_project()
        from dashboard import sprints
        saved = sprints.assign_task_project
        sprints.assign_task_project = lambda t, p: {"status": "error", "error": "no existe"}
        try:
            res = self.accept()
        finally:
            sprints.assign_task_project = saved
        self.assertFalse(res["project_applied"])

    def test_without_a_project_nothing_is_claimed(self):
        res = self.accept()
        self.assertNotIn("project_applied", res)


class AcceptCloseTask(Base):
    def test_close_marks_the_linked_task_done_via_the_sidecar(self):
        self.accept()                                  # crea y liga la tarea
        task_id = self.suggestion()[1]
        self.seed("sug_close", kind="close_task")
        called = {}
        from dashboard import sprints
        saved = sprints.set_task_status
        sprints.set_task_status = lambda tid, st: called.update(tid=tid, st=st) or {"status": "ok"}
        try:
            res = self.dg.accept_suggestion("sug_close", conn=self.conn)
        finally:
            sprints.set_task_status = saved
        self.assertTrue(res["closed"])
        self.assertEqual(called, {"tid": task_id, "st": "done"})
        # Nunca `hermes kanban complete`: sale 0 aunque falle.
        self.assertNotIn("complete", [c[2] for c in self.calls if len(c) > 2])

    def test_close_without_a_linked_task_finalizes_and_says_so(self):
        self.seed("sug_orphan", kind="close_task", oid="obj_2")
        res = self.dg.accept_suggestion("sug_orphan", conn=self.conn)
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["closed"])
        self.assertTrue(self.suggestion("sug_orphan")[2].endswith(":sin-tarea"))


class DismissAndEdit(Base):
    def test_dismiss_is_recorded_and_idempotent(self):
        self.assertEqual(self.dg.dismiss_suggestion("sug_1", conn=self.conn)["status"], "ok")
        self.assertEqual(self.suggestion()[0], "dismissed")
        self.assertTrue(self.dg.dismiss_suggestion("sug_1", conn=self.conn)["already"])

    def test_dismiss_is_refused_while_an_accept_is_in_flight(self):
        """Descartar a media creación dejaría una tarea huérfana sin sugerencia
        que la explique."""
        self.conn.execute("UPDATE suggestions SET status='accepting', updated_at=? "
                          "WHERE id='sug_1'", (NOW,))
        self.conn.commit()
        self.assertEqual(self.dg.dismiss_suggestion("sug_1", conn=self.conn)["code"],
                         "accept_in_flight")

    def test_an_accepted_suggestion_cannot_be_dismissed(self):
        self.accept()
        self.assertEqual(self.dg.dismiss_suggestion("sug_1", conn=self.conn)["code"],
                         "already_accepted")

    def test_a_dismissed_suggestion_cannot_be_accepted(self):
        self.dg.dismiss_suggestion("sug_1", conn=self.conn)
        self.assertEqual(self.accept()["code"], "already_dismissed")
        self.assertEqual(self.calls, [])

    def test_edit_stores_the_final_title_without_losing_the_proposal(self):
        self.dg.edit_suggestion("sug_1", title="Enviar cotización v2", conn=self.conn)
        _, _, _, edited, final = self.suggestion()
        self.assertTrue(edited)
        self.assertEqual(final, "Enviar cotización v2")
        self.assertEqual(self.conn.execute(
            "SELECT title FROM suggestions WHERE id='sug_1'").fetchone()[0], "Enviar cotización")

    def test_the_edited_title_is_what_gets_created(self):
        self.accept(overrides={"title": "Enviar cotización v2"})
        self.assertEqual(self.calls[0][3], "Enviar cotización v2")

    def test_an_unknown_project_is_refused_not_silently_dropped(self):
        self.assertEqual(self.dg.edit_suggestion("sug_1", project_id="proj_ghost",
                                                 conn=self.conn)["code"], "unknown_project")

    def test_editing_a_decided_suggestion_is_refused(self):
        self.dg.dismiss_suggestion("sug_1", conn=self.conn)
        self.assertEqual(self.dg.edit_suggestion("sug_1", title="x", conn=self.conn)["code"],
                         "not_open")


class ReadModels(Base):
    def test_list_suggestions_carries_evidence_and_objective(self):
        rows = self.dg.list_suggestions(conn=self.conn)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["evidence"]["quote"], QUOTE)
        self.assertEqual(r["evidence"]["meeting"], "Junta demo")
        self.assertEqual(r["objective"]["owner"], "Ric")

    def test_edited_title_wins_in_the_read_model(self):
        self.dg.edit_suggestion("sug_1", title="Editado", conn=self.conn)
        self.assertEqual(self.dg.list_suggestions(conn=self.conn)[0]["title"], "Editado")

    def test_status_filter(self):
        self.dg.dismiss_suggestion("sug_1", conn=self.conn)
        self.assertEqual(self.dg.list_suggestions(conn=self.conn), [])
        self.assertEqual(len(self.dg.list_suggestions(status="dismissed", conn=self.conn)), 1)
        self.assertEqual(len(self.dg.list_suggestions(status="all", conn=self.conn)), 1)

    def test_list_objectives_defaults_to_the_live_ones(self):
        self.conn.execute("INSERT INTO objectives (id, title, status, opened_at, updated_at) "
                          "VALUES ('obj_done','Ya','done',?,?)", (NOW, NOW))
        self.conn.commit()
        ids = {o["id"] for o in self.dg.list_objectives(conn=self.conn)}
        self.assertEqual(ids, {"obj_1"})


if __name__ == "__main__":
    unittest.main()
