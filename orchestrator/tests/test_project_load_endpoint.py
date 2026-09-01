"""GET /api/projects/load contract — la carga de la cartera, declarada vs medida.

Lo que congela, en orden de importancia:

  - HONESTIDAD CON CERO DATOS: el día que esto se despliega las filas activas
    están en NULL. La respuesta debe seguir siendo cierta y útil — utilization
    None, banda `unknown`, y el conteo de activos leído de la tabla, no fijado.

  - PROCEDENCIA DE LA CAPACIDAD: el denominador se MIDE de `time_blocks`.
    El test alarga el bloque de consultoría y exige que el número suba solo.
    Un 9 o un 23 escritos a mano lo reprueban.

  - INCOMPLETO NUNCA EN VERDE: con un solo proyecto sin dimensionar la suma
    declarada es un piso. La banda hace piso en ámbar aunque el porcentaje sea
    ridículamente bajo. Este es el RED-PROOF (ver test_red_proof_*).

  - EL CRÉDITO A AGENTES NO SE PUEDE FALSIFICAR CON UN FLAG: poner
    `autonomy='auto'` en TODAS las tareas no puede mover `credited_h` de 0.0.
    El permiso no es evidencia.

  - NADIE ACUSA DE INACTIVIDAD: el lector sólo ve `tasks`, y las juntas con el
    cliente viven en el calendario, fuera de esta base. `days_since_task` es un
    hecho sobre tareas y se llama así; no existe un flag que lo convierta en
    juicio sobre el proyecto.

Aislamiento: el sandbox de sesión de conftest. Este módulo NO abre copia propia
y NO muta ningún global compartido — ver el comentario del bloque de import.
"""
import datetime
import os
import sys
import time
import unittest
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
try:
    # Sin copia privada de la DB y sin tocar NINGÚN global compartido. La
    # primera versión de este módulo se hizo su propio temporal, repuntó
    # `dashboard.db.KANBAN_DB` y lo borró en el teardown: eso tumbó 73 tests
    # de otros seis módulos con FileNotFoundError, porque pytest importa todo
    # en un solo proceso. El sandbox de sesión de conftest YA es una copia — y
    # además re-apunta esos globals en `pytest_collection_finish`, así que una
    # asignación local ni siquiera sobrevive. Se usa el sandbox y punto.
    from dashboard import db as _db
    from dashboard import sprints as _sprints  # noqa: F401  (conftest lo repunta)
    from dashboard.api import app
    from dashboard import capacity as _capacity
    from dashboard.migrations.m23_project_weekly_hours import (
        m23_project_weekly_hours as _m23)
    from dashboard.migrations.m24_weekly_hours_set_at import (
        m24_weekly_hours_set_at as _m24)
    from starlette.testclient import TestClient

    _CLIENT = TestClient(app, raise_server_exceptions=False)
    _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _conn():
    """Siempre por `db.get_conn()`: resuelve el sandbox vigente y dispara el
    tripwire de `assert_not_live_db` si alguna vez volviera a la DB real."""
    return _db.get_conn()


def _assert_sandbox(conn):
    """Rehusarse a destruir nada si la conexión resolvió la DB VIVA.

    El 2026-08-05 la tabla `tasks` del operador quedó en CERO y aparecieron 54
    proyectos con slug `s-proj_*` y nombres `P`/`solo`/`comercial-encendido`
    — las fixtures de ESTE módulo. `_wipe` fue la mano que borró.

    No pude atribuir cómo la conexión llegó a la viva: el sandbox de conftest
    funciona (probado con sonda en los dos órdenes de colección), la suite
    completa no la toca, Playwright tampoco, y el servidor e2e repunta antes de
    importar la app. Sin causa raíz, la única respuesta honesta es que el código
    que DESTRUYE se niegue por sí mismo en vez de confiar en que alguien más
    apuntó bien. `db.assert_not_live_db` ya existe pero está condicionado a que
    el entorno traiga marca de test; esta comprobación no depende de nada del
    entorno: pregunta a la conexión qué archivo abrió y lo compara.
    """
    fila = conn.execute("PRAGMA database_list").fetchone()
    abierto = os.path.realpath(fila[2]) if fila and fila[2] else ""
    viva = os.path.realpath(os.path.expanduser("~/.hermes/kanban.db"))
    if abierto == viva:
        raise RuntimeError(
            f"REHUSADO: este módulo iba a borrar tareas de la DB VIVA ({viva}). "
            "Pasó de verdad el 2026-08-05 y costó 320 tareas. Apunta "
            "db.KANBAN_DB al sandbox de tests/conftest.py."
        )


def _wipe(conn):
    """Dejar el sandbox sin carga visible para que las aserciones de frontera no
    choquen con los datos reales copiados.

    Los proyectos se ARCHIVAN, no se borran: sprints/epics/deals los referencian
    y un DELETE revienta la FK. Archivar es además exactamente lo que el lector
    filtra (`status='active' AND archived_at IS NULL`), así que prueba el where
    de verdad en vez de esquivarlo."""
    _assert_sandbox(conn)
    conn.execute("DELETE FROM tasks")
    conn.execute("DELETE FROM time_blocks")
    conn.execute("UPDATE projects SET archived_at = ? WHERE archived_at IS NULL",
                 (int(time.time()),))


def _mkproj(conn, name, weekly_hours=None, status="active"):
    pid = f"proj_{_uuid.uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO projects (id, slug, name, color, icon, created_at, status, "
        "kind, weekly_hours) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, f"s-{pid}", name, "#3b82f6", "📦", int(time.time()), status,
         "product", weekly_hours))
    return pid


def _mkblock(conn, role, start, end):
    conn.execute(
        "INSERT INTO time_blocks (id, day_of_week, start_time, end_time, role, "
        "label, active, created_at) VALUES (?,?,?,?,?,?,1,?)",
        (f"tb_{_uuid.uuid4().hex[:8]}", 1, start, end, role, role, int(time.time())))


def _mktask(conn, pid, status="ready", completed_at=None, executor_kind=None,
            autonomy=None):
    tid = f"t_{_uuid.uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, workspace_kind, "
        "project_id, completed_at, executor_kind, autonomy) "
        "VALUES (?,?,?,?,'scratch',?,?,?,?)",
        (tid, "t", status, int(time.time()), pid, completed_at, executor_kind,
         autonomy))
    return tid


@unittest.skipUnless(_READY, "dashboard/DB unavailable")
class ProjectLoadContract(unittest.TestCase):

    def setUp(self):
        conn = _conn()
        try:
            _m23(conn)          # idempotentes: re-aplicarlas no debe fallar
            _m24(conn)
            _wipe(conn)
            conn.commit()
        finally:
            conn.close()

    def _get(self):
        r = _CLIENT.get("/api/projects/load")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # --- 1. honestidad con cero datos -----------------------------------
    def test_zero_data_is_honest_and_still_true(self):
        conn = _conn()
        try:
            for i in range(3):
                _mkproj(conn, f"P{i}")          # las tres en NULL
            _mkblock(conn, "consultant", "09:00", "18:00")
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        d = body["declared"]
        self.assertEqual(d["committed_hours"], 0.0)
        self.assertIsNone(d["utilization"])
        self.assertEqual(d["band"], "unknown")
        self.assertFalse(d["complete"])
        self.assertEqual(d["active_count"], 3)
        self.assertEqual(d["unsized_count"], 3)
        # la mitad medida existe aunque nadie haya declarado nada
        self.assertIn("touched_projects_7d", body["measured"])
        self.assertEqual(len(body["measured"]["done_by_week"]), 4)

    # --- 2. la capacidad se mide, no se codifica ------------------------
    def test_capacity_is_measured_from_time_blocks(self):
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "18:00")   # 9h entrega
            _mkblock(conn, "sdr", "09:00", "13:00")          # 4h crecimiento
            _mkblock(conn, "analyst", "14:00", "16:00")      # 2h crecimiento
            conn.commit()
        finally:
            conn.close()
        cap = self._get()["capacity"]
        self.assertEqual(cap["delivery_hours"], 9.0)
        self.assertEqual(cap["growth_reserved_hours"], 4.0)
        self.assertEqual(cap["admin_reserved_hours"], 2.0)
        self.assertEqual(cap["week_hours"], 15.0)
        self.assertEqual(cap["source"]["delivery_roles"], ["consultant"])

        conn = _conn()
        try:
            conn.execute("UPDATE time_blocks SET end_time='19:00' WHERE role='consultant'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._get()["capacity"]["delivery_hours"], 10.0)

    def test_red_proof_admin_hours_are_not_delivery(self):
        """RED-PROOF del tercer carril. Facturar y cobrar no es capacidad que un
        proyecto pueda gastar. Mientras `analyst` vivió dentro de `consultant`,
        esa hora se presentaba como entrega y el denominador salía inflado — un
        proyecto podía leerse holgado con horas que en realidad son de cobranza.
        Si `analyst` volviera al carril de entrega, `delivery_hours` diría 11 y
        `admin_reserved_hours` no existiría."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "18:00")   # 9h entrega
            _mkblock(conn, "analyst", "14:00", "16:00")      # 2h COBRANZA
            _mkproj(conn, "P", weekly_hours=8.0)
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        self.assertEqual(body["capacity"]["delivery_hours"], 9.0)
        self.assertEqual(body["capacity"]["admin_reserved_hours"], 2.0)
        self.assertEqual(body["capacity"]["source"]["admin_roles"], ["analyst"])
        self.assertNotIn("analyst", body["capacity"]["source"]["delivery_roles"])
        # 8/9 = 89% → rojo. Con las 2h de cobranza dentro sería 8/11 = 73%: ámbar.
        self.assertEqual(body["declared"]["band"], "red")

    def test_an_unmapped_role_never_becomes_delivery(self):
        """Un rol que nadie mapeó cae a crecimiento, no a entrega: inventar
        capacidad de proyecto es el error caro; reservar de más sólo es cauto."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkblock(conn, "rol-que-nadie-mapeo", "09:00", "17:00")
            conn.commit()
        finally:
            conn.close()
        cap = self._get()["capacity"]
        self.assertEqual(cap["delivery_hours"], 10.0)
        self.assertEqual(cap["growth_reserved_hours"], 8.0)
        self.assertIn("rol-que-nadie-mapeo", cap["source"]["growth_roles"])

    # --- 3. los tres estados y las bandas de Kingman --------------------
    def test_states_and_bands(self):
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")   # 10h exactas
            _mkproj(conn, "unsized")
            _mkproj(conn, "parked", weekly_hours=0.0)
            _mkproj(conn, "committed", weekly_hours=6.0)
            conn.commit()
        finally:
            conn.close()
        d = self._get()["declared"]
        self.assertEqual((d["unsized_count"], d["parked_count"], d["committed_count"]),
                         (1, 1, 1))
        self.assertEqual(d["committed_hours"], 6.0)
        self.assertEqual(d["utilization"], 0.6)

        # con todo dimensionado, las bandas caen donde Kingman las pone
        conn = _conn()
        try:
            conn.execute("UPDATE projects SET weekly_hours=0.0 WHERE weekly_hours IS NULL")
            conn.commit()
        finally:
            conn.close()
        for hours, band in ((7.0, "green"), (8.0, "amber"), (9.0, "red")):
            conn = _conn()
            try:
                conn.execute("UPDATE projects SET weekly_hours=? WHERE name='committed'",
                             (hours,))
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(self._get()["declared"]["band"], band,
                             f"{hours}h/10h debería ser {band}")

    # --- 4. RED-PROOF: incompleto jamás en verde ------------------------
    def test_red_proof_incomplete_measurement_is_never_green(self):
        """Sin el clamp de incompletitud esto devuelve `green` (2/10 = 20%) y
        el test se pone ROJO. Un clamp que nunca falló no está probado."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "sized", weekly_hours=2.0)
            _mkproj(conn, "never-sized")                     # queda en NULL
            conn.commit()
        finally:
            conn.close()
        d = self._get()["declared"]
        self.assertFalse(d["complete"])
        self.assertLess(d["utilization"], 0.70)              # 20%: "parece" verde
        self.assertNotEqual(d["band"], "green")
        self.assertEqual(d["band"], "amber")

    # --- 5. el crédito a agentes no se falsifica con un flag ------------
    def test_agent_credit_cannot_be_faked_by_flipping_autonomy(self):
        conn = _conn()
        try:
            pid = _mkproj(conn, "P", weekly_hours=4.0)
            for _ in range(5):
                _mktask(conn, pid, executor_kind="hermes", autonomy="ask")
            conn.commit()
        finally:
            conn.close()
        before = self._get()["agent_capacity"]
        self.assertEqual(before["credited_h"], 0.0)
        self.assertEqual(before["status"], "unproven")
        self.assertEqual(before["lane_counts"]["supervised"], 5)
        self.assertEqual(before["lane_counts"]["autonomous"], 0)

        conn = _conn()
        try:
            conn.execute("UPDATE tasks SET autonomy='auto'")
            conn.commit()
        finally:
            conn.close()
        after = self._get()["agent_capacity"]
        self.assertEqual(after["credited_h"], 0.0, "un permiso no es evidencia")
        self.assertEqual(after["lane_counts"]["autonomous"], 5)

    def test_unknown_executor_falls_to_ricardo_lane(self):
        conn = _conn()
        try:
            pid = _mkproj(conn, "P")
            _mktask(conn, pid, executor_kind=None, autonomy="auto")
            _mktask(conn, pid, executor_kind="zzz-nope", autonomy="auto")
            conn.commit()
        finally:
            conn.close()
        lanes = self._get()["agent_capacity"]["lane_counts"]
        self.assertEqual(lanes["ricardo"], 2)
        self.assertEqual(lanes["autonomous"], 0)
        self.assertEqual(sum(lanes.values()), 2)

    # --- 6. idle usa columnas vivas -------------------------------------

    # --- 7. round-trip por el escritor real -----------------------------
    def test_patch_round_trip_and_validation(self):
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "P")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"weekly_hours": 6}).status_code, 200)
        rows = {p["id"]: p for p in self._get()["projects"]}
        self.assertEqual(rows[pid]["weekly_hours"], 6.0)
        self.assertEqual(rows[pid]["state"], "committed")

        # Medias horas: el paso de la UI es 0.5 y la columna es REAL, así que el
        # medio tiene que sobrevivir el viaje completo — un entero silencioso
        # aquí convertiría "3.5" en "3" sin decirlo.
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"weekly_hours": 3.5}).status_code, 200)
        self.assertEqual({p["id"]: p for p in self._get()["projects"]}[pid]["weekly_hours"],
                         3.5)
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"weekly_hours": 0.5}).status_code, 200)
        d = self._get()["declared"]
        self.assertEqual(d["committed_hours"], 0.5)
        self.assertEqual(d["committed_count"], 1)

        # 0 es aparcado, no "sin dimensionar": tiene que persistir
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"weekly_hours": 0}).status_code, 200)
        self.assertEqual({p["id"]: p for p in self._get()["projects"]}[pid]["state"],
                         "parked")

        for bad in ("soon", -1, 41, True):
            r = _CLIENT.patch(f"/api/projects/{pid}", json={"weekly_hours": bad})
            self.assertEqual(r.status_code, 400, f"{bad!r} debió ser rechazado")
        self.assertEqual({p["id"]: p for p in self._get()["projects"]}[pid]["state"],
                         "parked", "un rechazo no puede dejar media escritura")

    # --- 8. fronteras que la mutación demostró sin cubrir ---------------
    def test_capacity_counts_partial_hours(self):
        """Todos los bloques reales terminan en :00, así que un contrato que
        solo los use no distingue `h*60 + m` de `h*60 - m`. Un bloque a media
        hora sí."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:30", "13:00")   # 3.5h
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._get()["capacity"]["delivery_hours"], 3.5)

    def test_band_boundaries_are_exact(self):
        """Kingman pone los codos EN 0.70 y 0.85: el borde pertenece a la banda
        buena. Sin esto, `>` y `>=` son indistinguibles."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")   # 10h
            _mkproj(conn, "solo", weekly_hours=7.0)
            conn.commit()
        finally:
            conn.close()
        for hours, band in ((7.0, "green"), (7.1, "amber"), (8.5, "amber"),
                            (8.6, "red")):
            conn = _conn()
            try:
                conn.execute("UPDATE projects SET weekly_hours=? WHERE name='solo'",
                             (hours,))
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(self._get()["declared"]["band"], band,
                             f"{hours}/10h = {hours / 10} debería ser {band}")

    def test_millisecond_timestamps_are_normalised(self):
        """kanban.db guarda epochs en segundos Y en milisegundos según quién
        escribió. Sin normalizar, un ms se lee como el año 56000 o como 1970 y
        un proyecto vivo sale inactivo."""
        conn = _conn()
        try:
            pid = _mkproj(conn, "ms", weekly_hours=2.0)
            _mktask(conn, pid, status="done",
                    completed_at=int(time.time() * 1000))     # milisegundos
            conn.commit()
        finally:
            conn.close()
        row = {p["name"]: p for p in self._get()["projects"]}["ms"]
        self.assertEqual(row["done_28d"], 1)
        self.assertEqual(row["days_since_task"], 0)


    def test_done_28d_window_boundary(self):
        """`done_28d` sigue siendo una ventana de 28 días exactos aunque el
        juicio `idle` haya muerto. Al borrar el flag se fue con él la única
        aserción que fijaba este borde, y una constante sin contrato se mueve
        sola: 27 días dentro, 29 fuera."""
        now = int(time.time())
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            dentro = _mkproj(conn, "dentro", weekly_hours=1.0)
            fuera = _mkproj(conn, "fuera", weekly_hours=1.0)
            _mktask(conn, dentro, status="done", completed_at=now - 27 * 86400)
            _mktask(conn, fuera, status="done", completed_at=now - 29 * 86400)
            conn.commit()
        finally:
            conn.close()
        rows = {p["name"]: p for p in self._get()["projects"]}
        self.assertEqual(rows["dentro"]["done_28d"], 1)
        self.assertEqual(rows["fuera"]["done_28d"], 0)
        self.assertEqual(rows["fuera"]["days_since_task"], 29)

    def test_measured_windows_separate_7d_from_28d(self):
        now = int(time.time())
        conn = _conn()
        try:
            recent = _mkproj(conn, "reciente")
            older = _mkproj(conn, "viejo")
            _mktask(conn, recent, status="done", completed_at=now - 3 * 86400)
            _mktask(conn, older, status="done", completed_at=now - 20 * 86400)
            conn.commit()
        finally:
            conn.close()
        mea = self._get()["measured"]
        self.assertEqual(mea["touched_projects_7d"], 1)
        self.assertEqual(mea["touched_projects_28d"], 2)
        # done_by_week va del más viejo al más nuevo: la de 3 días cae en el
        # último cubo, la de 20 en el tercero contando hacia atrás.
        self.assertEqual(mea["done_by_week"][-1], 1)
        self.assertEqual(sum(mea["done_by_week"]), 2)

    def test_open_tasks_excludes_terminal_statuses(self):
        conn = _conn()
        try:
            pid = _mkproj(conn, "P", weekly_hours=2.0)
            _mktask(conn, pid, status="ready")
            _mktask(conn, pid, status="in_progress")
            for dead in ("done", "cancelled", "rejected"):
                _mktask(conn, pid, status=dead)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual({p["name"]: p for p in self._get()["projects"]}["P"]["open_tasks"], 2)

    def test_only_active_unarchived_projects_are_counted(self):
        conn = _conn()
        try:
            _mkproj(conn, "activo", weekly_hours=3.0)
            _mkproj(conn, "planeado", weekly_hours=99.0, status="planned")
            _mkproj(conn, "entregado", weekly_hours=99.0, status="delivered")
            conn.commit()
        finally:
            conn.close()
        d = self._get()["declared"]
        self.assertEqual(d["active_count"], 1)
        self.assertEqual(d["committed_hours"], 3.0)

    # --- 9. la plomería que la mutación dejó al descubierto -------------
    def test_accepts_a_callers_connection_and_does_not_close_it(self):
        """El lector tiene que servir a un llamador que ya está en transacción
        (un verbo MCP), y no puede cerrarle la conexión debajo. Además pone él
        mismo el `row_factory`: recibir una conexión cruda es normal."""
        import sqlite3 as _sq
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "P", weekly_hours=2.0)
            conn.commit()
        finally:
            conn.close()
        raw = _sq.connect(str(_db.KANBAN_DB))    # SIN row_factory a propósito
        try:
            body = _capacity.project_load(raw)
            self.assertEqual(body["declared"]["committed_hours"], 2.0)
            raw.execute("SELECT 1").fetchone()   # sigue viva: no cerró lo ajeno
        finally:
            raw.close()

    def test_survives_a_db_without_task_runs(self):
        """`task_runs` es del despachador, no del lector de carga. Si no está,
        el panel informa 0 corridas verificadas en vez de reventar."""
        conn = _conn()
        try:
            _mkproj(conn, "P", weekly_hours=2.0)
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='task_runs'").fetchone()[0]
            conn.execute("DROP TABLE task_runs")
            conn.commit()
            body = _capacity.project_load(conn)
            self.assertEqual(body["agent_capacity"]["distinct_completed_runs"], 0)
            self.assertEqual(body["agent_capacity"]["credited_h"], 0.0)
            conn.execute(ddl)
            conn.commit()
        finally:
            conn.close()

    def test_malformed_blocks_degrade_instead_of_crashing(self):
        """Un bloque ilegible o invertido vale 0, no tumba la tarjeta ni suma
        horas negativas. `active_blocks` cuenta lo que se leyó de verdad."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "18:00", "09:00")   # invertido → 0
            _mkblock(conn, "sdr", "no-es-hora", "13:00")     # ilegible → 13:00
            _mkblock(conn, "sdr", "09:00", "13:00")          # rol repetido
            conn.commit()
        finally:
            conn.close()
        cap = self._get()["capacity"]
        self.assertEqual(cap["delivery_hours"], 0.0)
        self.assertEqual(cap["growth_reserved_hours"], 17.0)   # 13 + 4
        self.assertEqual(cap["source"]["active_blocks"], 3)
        self.assertEqual(cap["source"]["growth_roles"], ["sdr"])   # sin repetir

    def test_window_boundaries_for_touched_counts(self):
        """7d y 28d son ventanas distintas y sus bordes importan: una tarea a
        7d+1h NO es de esta semana, y una a 28d+1h no es del mes."""
        now = int(time.time())
        conn = _conn()
        try:
            a = _mkproj(conn, "hoy")
            b = _mkproj(conn, "semana-pasada")
            c = _mkproj(conn, "mes-pasado")
            _mktask(conn, a, status="done", completed_at=now - 3600)
            _mktask(conn, b, status="done", completed_at=now - 7 * 86400 - 3600)
            _mktask(conn, c, status="done", completed_at=now - 28 * 86400 - 3600)
            conn.commit()
        finally:
            conn.close()
        mea = self._get()["measured"]
        self.assertEqual(mea["touched_projects_7d"], 1)
        self.assertEqual(mea["touched_projects_28d"], 2)

    def test_one_hour_is_committed_not_parked(self):
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "chico", weekly_hours=1.0)
            conn.commit()
        finally:
            conn.close()
        d = self._get()["declared"]
        self.assertEqual(d["committed_count"], 1)
        self.assertEqual(d["parked_count"], 0)


    def test_an_empty_portfolio_is_not_complete(self):
        """Cero proyectos activos no es 'medición completa': es que no hay nada
        que medir. Decir `complete` ahí abriría la puerta al verde vacío."""
        d = self._get()["declared"]
        self.assertEqual(d["active_count"], 0)
        self.assertFalse(d["complete"])
        self.assertEqual(d["band"], "unknown")

    # --- 10. qué entra al presupuesto y qué no --------------------------
    def test_red_proof_commercial_and_personal_do_not_eat_delivery_hours(self):
        """RED-PROOF de la exclusión. El esfuerzo comercial y lo personal son
        trabajo, pero no se pagan del presupuesto de ENTREGA: comercial tiene su
        propio carril de horas y lo personal no es capacidad de negocio. Si
        `kind` no filtrara, las 4 h de ventas y las 2 de personal entrarían al
        numerador (14/10 = 140%, rojo) y además el ritual pediría declarar el
        Inbox. Con el filtro son 8/10 = 80%: ámbar, y sólo un proyecto que
        declarar."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")     # 10 h entrega
            _mkproj(conn, "producto", weekly_hours=8.0)
            for nombre, kind, h in (("ventas", "sales", 4.0),
                                    ("mi vida", "personal", 2.0),
                                    ("inbox", "system", 0.0)):
                pid = _mkproj(conn, nombre, weekly_hours=h)
                conn.execute("UPDATE projects SET kind = ? WHERE id = ?", (kind, pid))
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        d = body["declared"]
        self.assertEqual(d["committed_hours"], 8.0, "sólo producto suma")
        self.assertEqual(d["active_count"], 1)
        self.assertEqual(d["utilization"], 0.8)
        self.assertEqual(d["band"], "amber")
        self.assertEqual(d["pending_this_week"], 1, "no se pide declarar lo excluido")
        # Lo excluido se VE: un proyecto fuera y en silencio es como se acumulan.
        # El conteo se deriva de las filas — `excluded` como bloque murió porque
        # con el carril de automejora habría empezado a mentir.
        self.assertEqual(sum(1 for p in body["projects"] if not p["counts"]), 3)
        # `excluded` murió: el hecho vive en la fila (`counts`/`bucket`), y con
        # el carril de automejora su conteo habría empezado a mentir.
        self.assertNotIn("excluded", body)
        self.assertEqual({p["name"] for p in body["projects"]},
                         {"producto", "ventas", "mi vida", "inbox"})
        rows = {p["name"]: p for p in body["projects"]}
        self.assertTrue(rows["producto"]["in_budget"])
        self.assertEqual(rows["producto"]["bucket"], "budget")
        for n in ("ventas", "mi vida", "inbox"):
            self.assertFalse(rows[n]["counts"])
            self.assertFalse(rows[n]["in_budget"])
            self.assertEqual(rows[n]["bucket"], "outside")
        # La fila de otro carril CONSERVA sus horas declaradas: son reales, sólo
        # se pagan de otro bolsillo. Borrarlas perdería el dato al reincluirlo.
        self.assertEqual(rows["ventas"]["weekly_hours"], 4.0)
        self.assertEqual(rows["ventas"]["kind_label"], "comercial")

    def test_kind_null_counts_as_product(self):
        """Un proyecto sin clasificar es trabajo hasta que alguien diga lo
        contrario: excluir por omisión escondería carga real."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "sin-clasificar", weekly_hours=3.0)
            conn.execute("UPDATE projects SET kind = NULL WHERE id = ?", (pid,))
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        self.assertEqual(body["declared"]["committed_hours"], 3.0)
        self.assertTrue(body["projects"][0]["counts"])
        self.assertEqual(body["projects"][0]["kind"], "product")

    def test_kind_and_tier_round_trip_and_reject_junk(self):
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "P", weekly_hours=3.0)
            conn.commit()
        finally:
            conn.close()
        # Sacarlo del presupuesto y devolverlo, por el escritor real.
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"kind": "sales"}).status_code, 200)
        self.assertEqual(self._get()["declared"]["committed_hours"], 0.0)
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"kind": "product"}).status_code, 200)
        self.assertEqual(self._get()["declared"]["committed_hours"], 3.0)
        # Jerarquía: poner, y poder QUITAR (la cadena vacía es el "quítalo"
        # explícito; sin él la jerarquía sólo podría subir).
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"tier": "commit"}).status_code, 200)
        self.assertEqual(self._get()["projects"][0]["tier"], "commit")
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"tier": ""}).status_code, 200)
        self.assertIsNone(self._get()["projects"][0]["tier"])
        for bad in ({"kind": "inventado"}, {"tier": "altisimo"}):
            self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}", json=bad).status_code,
                             400, f"{bad} debió ser rechazado")
        self.assertEqual(self._get()["declared"]["committed_hours"], 3.0)

    def test_the_number_orders_the_rows_and_tier_no_longer_does(self):
        """Priorizar es darle horas y despriorizar es quitárselas, así que un
        proxy ordinal aparte (`tier`) sólo podía contradecir al cardinal que
        además entra al denominador. Sin estimar primero —son el pendiente del
        ritual—, luego lo pesado, luego nombre; y `tier` no mueve nada."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            for nombre, wh in (("b-ocho", 8.0), ("a-dos", 2.0),
                               ("c-sin", None), ("d-cero", 0.0)):
                pid = _mkproj(conn, nombre, weekly_hours=wh)
                if nombre == "b-ocho":      # el más pesado, con el tier más bajo
                    conn.execute("UPDATE projects SET tier='explore' WHERE id=?", (pid,))
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        self.assertEqual([p["name"] for p in body["projects"]],
                         ["c-sin", "b-ocho", "a-dos", "d-cero"])
        # El aparcado va al final y fuera del presupuesto, sin cambiar de tipo.
        self.assertEqual(body["projects"][-1]["bucket"], "outside")
        self.assertEqual(body["projects"][-1]["kind"], "product")
        # `sort` es un entero denso que la UI usa para no repintar saltando.
        self.assertEqual([p["sort"] for p in body["projects"]], [0, 1, 2, 3])

    def test_red_proof_a_product_at_zero_leaves_the_budget_without_relabelling(self):
        """RED-PROOF de lo que el operador pidió: sacar del presupuesto SIN apagar y
        SIN mentir sobre qué es el proyecto.

        Antes de este cambio la aserción era inexpresable: el único modo de
        estar fuera era pertenecer a la lista `excluded`, que por construcción
        significaba `kind != 'product'`. Y el único gesto para llegar ahí
        cambiaba `kind` — que además saca las tareas del proyecto del backlog de
        grooming del ciclo (`sprints.py:1521`), un efecto que nadie pidió.
        Ahora `0h` ES estar fuera, y el tipo no se toca.
        """
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")     # 10 h
            vivo = _mkproj(conn, "sigue-dentro", weekly_hours=6.0)
            fuera = _mkproj(conn, "fuera-esta-semana", weekly_hours=6.0)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._get()["declared"]["committed_hours"], 12.0)

        # El gesto: poner 0 horas. Un solo PATCH, sin tocar status ni kind.
        r = _CLIENT.patch(f"/api/projects/{fuera}", json={"weekly_hours": 0})
        self.assertEqual(r.status_code, 200, r.text)

        body = self._get()
        row = {p["id"]: p for p in body["projects"]}[fuera]
        self.assertFalse(row["in_budget"], "0h es estar fuera del presupuesto")
        self.assertEqual(row["bucket"], "outside")
        self.assertEqual(row["kind"], "product", "no se re-etiquetó")
        self.assertTrue(row["active"], "sigue prendido")
        self.assertTrue(row["counts"], "sigue siendo de carril entrega")
        self.assertEqual(body["declared"]["committed_hours"], 6.0)
        self.assertEqual(body["declared"]["parked_count"], 1)
        self.assertEqual(body["declared"]["outside_count"], 1)
        # Y en la DB el tipo sigue intacto: el backlog del ciclo no se movió.
        conn = _conn()
        try:
            self.assertEqual(conn.execute(
                "SELECT kind FROM projects WHERE id = ?", (fuera,)).fetchone()[0],
                "product")
        finally:
            conn.close()

    def test_parking_does_not_escape_the_weekly_ritual(self):
        """Aparcar es declarar, no evadir. Si un 0h quedara exento del ritual, la
        forma más barata de cerrar la semana sería aparcar todo y el ritual
        dejaría de medir nada."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "aparcado")
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(self._get()["declared"]["ritual_due"])
        _CLIENT.patch(f"/api/projects/{pid}", json={"weekly_hours": 0})
        d = self._get()["declared"]
        self.assertFalse(d["ritual_due"], "declarar 0 cierra la semana")
        self.assertEqual(d["active_count"], 1, "sigue contando como vivo")
        self.assertEqual(d["committed_count"], 0, "pero no como comprometido")
        self.assertEqual(d["declared_this_week"], 1)

    # --- 10a-bis. el carril de automejora --------------------------------
    def test_red_proof_self_hours_claim_the_same_pool(self):
        """RED-PROOF del pozo. Automejora no compite por el presupuesto de
        entrega, pero SALE DEL MISMO DÍA. Si el color de la tarjeta se pintara
        sólo de entrega, un pozo lleno de automejora se leería verde con el día
        ya desbordado — la mentira que este carril existe para matar. La
        utilización de entrega conserva su numerador puro; el color sale del
        total. Contra el código de ayer `total` ni siquiera existe."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")     # 10 h de pozo
            _mkproj(conn, "cliente", weekly_hours=4.0)
            auto = _mkproj(conn, "automejora", weekly_hours=5.0)
            conn.execute("UPDATE projects SET kind='self' WHERE id=?", (auto,))
            conn.commit()
        finally:
            conn.close()
        b = self._get()
        self.assertEqual(b["declared"]["committed_hours"], 4.0)
        self.assertEqual(b["declared"]["utilization"], 0.4)
        self.assertEqual(b["declared"]["band"], "green", "entrega sola va holgada")
        self.assertEqual(b["self_lane"]["hours"], 5.0)
        self.assertEqual(b["self_lane"]["committed_count"], 1)
        self.assertTrue(b["self_lane"]["on"])
        # `share` es la tajada del pozo que se lleva automejora: 5 de 10 = 50%.
        self.assertEqual(b["self_lane"]["share"], 0.5)
        # El POZO: 9 de 10 = 90% → rojo, con entrega en verde al mismo tiempo.
        self.assertEqual(b["total"]["claimed_hours"], 9.0)
        self.assertEqual(b["total"]["utilization"], 0.9)
        self.assertEqual(b["total"]["band"], "red")
        fila = {p["name"]: p for p in b["projects"]}["automejora"]
        self.assertEqual(fila["bucket"], "self")
        self.assertEqual(fila["kind_label"], "automejora")
        self.assertFalse(fila["counts"], "no compite por el presupuesto de entrega")

    def test_self_never_touches_the_delivery_ritual(self):
        """Automejora no pide declaración cada lunes: el instrumento es el
        contraste que ya existe (21 hechas · —h) más el ámbar que un self sin
        declarar impone al pozo entero."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            cli = _mkproj(conn, "cliente")
            auto = _mkproj(conn, "auto")                        # sin declarar
            conn.execute("UPDATE projects SET kind='self' WHERE id=?", (auto,))
            conn.commit()
        finally:
            conn.close()
        # El cliente se declara por el ESCRITOR real, que es quien sella la fecha.
        _CLIENT.patch(f"/api/projects/{cli}", json={"weekly_hours": 2})
        b = self._get()
        self.assertEqual(b["declared"]["active_count"], 1, "sólo entrega en el ritual")
        self.assertEqual(b["declared"]["pending_this_week"], 0,
                         "el self sin declarar NO agrega pendiente al ritual")
        self.assertFalse(b["declared"]["ritual_due"])
        self.assertFalse(b["total"]["complete"], "el pozo sí sabe que falta medir")
        self.assertEqual(b["self_lane"]["unsized_count"], 1)

    def test_the_self_lane_survives_being_switched_off(self):
        """Apagar el carril no borra horas ni saca las filas de su sección: si se
        fueran a APAGADOS, el encabezado y su botón se irían con ellas y prender
        sería buscarlas en un cementerio — el interruptor de un solo sentido."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            a = _mkproj(conn, "auto-a", weekly_hours=3.0)
            b_ = _mkproj(conn, "auto-b", weekly_hours=2.0)
            conn.execute("UPDATE projects SET kind='self' WHERE id IN (?,?)", (a, b_))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._get()["self_lane"]["hours"], 5.0)
        for pid in (a, b_):
            self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                           json={"status": "planned"}).status_code, 200)
        off = self._get()
        self.assertFalse(off["self_lane"]["on"])
        self.assertEqual(off["self_lane"]["hours"], 0.0, "apagado sale del pozo")
        self.assertEqual(off["self_lane"]["count"], 2, "pero sigue contándose")
        self.assertEqual(off["inactive"]["count"], 0, "NO se van a apagados")
        self.assertEqual({p["bucket"] for p in off["projects"]}, {"self"})
        for pid in (a, b_):
            _CLIENT.patch(f"/api/projects/{pid}", json={"status": "active"})
        on = self._get()
        self.assertTrue(on["self_lane"]["on"])
        self.assertEqual(on["self_lane"]["hours"], 5.0, "el reparto sobrevivió")

    def test_marking_self_keeps_the_tasks_in_the_cycle_backlog(self):
        """`sprints.py:1521` es lista NEGRA (`NOT IN personal/system/sales`), así
        que 'self' pasa igual que 'product': separar las horas no exilia el
        trabajo. Congela ese no-op — si alguien convirtiera el filtro en lista
        blanca, Hermes perdería sus 21 tareas del backlog en silencio."""
        from dashboard import sprints as _sp
        conn = _conn()
        try:
            pid = _mkproj(conn, "auto")
            for _ in range(3):
                _mktask(conn, pid, status="ready")
            conn.commit()
        finally:
            conn.close()
        antes = len([t for t in _sp.get_future_tasks() if t.get("project_id") == pid])
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"kind": "self"}).status_code, 200)
        despues = len([t for t in _sp.get_future_tasks() if t.get("project_id") == pid])
        self.assertEqual(antes, despues, "marcar automejora no toca el backlog")

    def test_the_worst_band_wins_across_the_two_claims(self):
        """El color sale de la PEOR de las dos mitades, y el orden verde<ámbar<rojo
        tiene que estar fijado: sin esto, subir un escalón el rango de `green` lo
        empataría con `amber` y una tarjeta ámbar se pintaría verde."""
        for entrega, auto, esperado in ((5.0, 3.0, "amber"),   # entrega verde, pozo 80%
                                        (8.0, 1.0, "red")):    # entrega ámbar, pozo 90%
            conn = _conn()
            try:
                _wipe(conn)
                _mkblock(conn, "consultant", "09:00", "19:00")   # 10 h
                _mkproj(conn, "cliente", weekly_hours=entrega)
                a = _mkproj(conn, "auto", weekly_hours=auto)
                conn.execute("UPDATE projects SET kind='self' WHERE id=?", (a,))
                conn.commit()
            finally:
                conn.close()
            b = self._get()
            self.assertEqual(b["total"]["band"], esperado,
                             f"entrega {entrega}h + auto {auto}h de 10h")
            self.assertNotEqual(b["declared"]["band"], "red",
                                "la mitad de entrega sigue siendo suya")

    def test_an_unmeasured_half_never_greys_out_a_measured_red(self):
        """`unknown` no gana: una mitad sin declarar no puede teñir de gris una
        mitad que sí se midió en rojo."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "cliente")                       # sin declarar → unknown
            a = _mkproj(conn, "auto", weekly_hours=9.0)    # 90% del pozo
            conn.execute("UPDATE projects SET kind='self' WHERE id=?", (a,))
            conn.commit()
        finally:
            conn.close()
        b = self._get()
        self.assertEqual(b["declared"]["band"], "unknown")
        self.assertEqual(b["total"]["band"], "red")

    def test_sections_come_in_the_order_ricardo_named(self):
        """"cosas prendidas, cosas prendidas de automejora, y luego fuera de
        presupuesto y apagadas" — ese orden es el contrato."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "z-en-presupuesto", weekly_hours=1.0)
            a = _mkproj(conn, "a-automejora", weekly_hours=4.0)   # MÁS pesado
            conn.execute("UPDATE projects SET kind='self' WHERE id=?", (a,))
            _mkproj(conn, "b-aparcado", weekly_hours=0.0)
            for nombre in ("z-apagado", "a-apagado"):
                _mkproj(conn, nombre, status="planned")
            conn.commit()
        finally:
            conn.close()
        b = self._get()
        # El bucket manda sobre las horas y sobre el nombre: el de automejora
        # pesa más (4h vs 1h) y se llama antes, y aun así va DESPUÉS.
        self.assertEqual([p["bucket"] for p in b["projects"]],
                         ["budget", "self", "outside"])
        self.assertEqual([p["name"] for p in b["projects"]],
                         ["z-en-presupuesto", "a-automejora", "b-aparcado"])
        # `outside_count` cuenta SÓLO los de afuera, no todo lo que no es afuera.
        self.assertEqual(b["declared"]["outside_count"], 1)
        # Los apagados van por nombre, para que el orden no baile entre lecturas.
        self.assertEqual([p["name"] for p in b["inactive"]["projects"]],
                         ["a-apagado", "z-apagado"])

    def test_an_empty_pool_has_no_utilization(self):
        """Sin nada reclamado no hay porcentaje que enseñar: un 0% inventado es
        tan mentira como un verde inventado."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "sin-declarar")
            conn.commit()
        finally:
            conn.close()
        b = self._get()
        self.assertEqual(b["total"]["claimed_hours"], 0.0)
        self.assertIsNone(b["total"]["utilization"])
        self.assertIsNone(b["self_lane"]["share"], "sin horas no hay tajada")

    # --- 10b. encender y apagar, en los dos sentidos --------------------
    def test_red_proof_the_switch_works_both_ways_from_one_screen(self):
        """RED-PROOF del interruptor. Si la respuesta sólo trajera los activos,
        apagar un proyecto lo borraría de la pantalla y no habría desde dónde
        volver a encenderlo: un interruptor de un solo sentido no es un
        interruptor. El apagado tiene que seguir viniendo en la MISMA lectura."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "encendido", weekly_hours=4.0)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._get()["declared"]["active_count"], 1)

        # APAGAR: sale del presupuesto, pero NO desaparece.
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"status": "planned"}).status_code, 200)
        body = self._get()
        self.assertEqual(body["declared"]["active_count"], 0)
        self.assertEqual(body["declared"]["committed_hours"], 0.0)
        self.assertEqual(body["inactive"]["count"], 1)
        off = body["inactive"]["projects"][0]
        self.assertEqual(off["name"], "encendido")
        self.assertFalse(off["active"])
        self.assertEqual(off["status"], "planned")
        self.assertEqual(off["weekly_hours"], 4.0, "apagar no borra lo declarado")

        # ENCENDER de vuelta: recupera su lugar y sus horas.
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"status": "active"}).status_code, 200)
        body = self._get()
        self.assertEqual(body["declared"]["active_count"], 1)
        self.assertEqual(body["declared"]["committed_hours"], 4.0)
        self.assertEqual(body["inactive"]["count"], 0)
        self.assertTrue(body["projects"][0]["active"])

    def test_inactive_lists_every_non_active_state_but_never_archived(self):
        """Archivar es la salida definitiva y tiene su propio verbo: un
        archivado NO puede reaparecer en el interruptor. `status` NULL sí, o
        quedaría invisible para siempre."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            _mkproj(conn, "vivo")
            for nombre, st in (("planeado", "planned"), ("entregado", "delivered")):
                pid = _mkproj(conn, nombre, status=st)
                if nombre == "planeado":
                    _mktask(conn, pid, status="ready")
                    _mktask(conn, pid, status="in_progress")
                    _mktask(conn, pid, status="done", completed_at=int(time.time()) - 86400)
            sin_estado = _mkproj(conn, "sin-estado")
            conn.execute("UPDATE projects SET status = NULL WHERE id = ?", (sin_estado,))
            muerto = _mkproj(conn, "archivado")
            conn.execute("UPDATE projects SET archived_at = ? WHERE id = ?",
                         (int(time.time()), muerto))
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        self.assertEqual(body["declared"]["active_count"], 1)
        self.assertEqual({p["name"] for p in body["inactive"]["projects"]},
                         {"planeado", "entregado", "sin-estado"})
        self.assertEqual(body["inactive"]["by_status"],
                         {"planned": 1, "delivered": 1, "sin estado": 1})
        # Un apagado sigue mostrando su trabajo: apagarlo no borra sus tareas, y
        # verlas es justo lo que dice si vale la pena volver a encenderlo. El
        # que no tiene ninguna reporta 0, no un hueco.
        off = {p["name"]: p for p in body["inactive"]["projects"]}
        self.assertEqual(off["planeado"]["open_tasks"], 2)
        self.assertEqual(off["planeado"]["done_28d"], 1)
        self.assertEqual(off["entregado"]["open_tasks"], 0)
        self.assertEqual(off["entregado"]["done_28d"], 0)
        self.assertEqual(off["sin-estado"]["kind_label"], "producto")

    def test_red_proof_one_patch_both_lights_it_and_classifies_it(self):
        """RED-PROOF del botón «+ entrega». El botón promete UNA acción, y eso
        exige que el PATCH aplique tipo Y estado en la misma llamada: si sólo
        pasara uno, un proyecto apagado y personal quedaría a medio camino —
        encendido pero sin contar, o clasificado pero apagado— y el usuario
        vería un botón que no hizo lo que dice. Esta es la única aserción que
        prueba que el camino de un tap existe de verdad."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")     # 10 h
            pid = _mkproj(conn, "apagado-y-personal", weekly_hours=4.0,
                          status="planned")
            conn.execute("UPDATE projects SET kind = 'personal' WHERE id = ?", (pid,))
            conn.commit()
        finally:
            conn.close()
        before = self._get()
        self.assertEqual(before["declared"]["active_count"], 0)
        self.assertEqual(before["inactive"]["count"], 1)

        r = _CLIENT.patch(f"/api/projects/{pid}",
                          json={"kind": "product", "status": "active"})
        self.assertEqual(r.status_code, 200, r.text)

        after = self._get()
        self.assertEqual(after["declared"]["active_count"], 1)
        self.assertEqual(after["inactive"]["count"], 0, "ni a medio camino en apagados")
        self.assertEqual(after["projects"][0]["bucket"], "budget")
        row = after["projects"][0]
        self.assertEqual(row["kind"], "product")
        self.assertTrue(row["active"])
        self.assertEqual(row["weekly_hours"], 4.0, "recupera sus horas de antes")
        self.assertEqual(after["declared"]["committed_hours"], 4.0)

    def test_an_excluded_project_enters_with_kind_alone(self):
        """Ya encendido, meterlo al presupuesto es sólo el tipo: el botón no
        debe tocar el estado de algo que ya estaba activo."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "comercial-encendido", weekly_hours=2.0)
            conn.execute("UPDATE projects SET kind = 'sales' WHERE id = ?", (pid,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._get()["projects"][0]["bucket"], "outside")
        self.assertEqual(_CLIENT.patch(f"/api/projects/{pid}",
                                       json={"kind": "product"}).status_code, 200)
        after = self._get()
        self.assertEqual(after["declared"]["committed_hours"], 2.0)
        self.assertEqual(after["projects"][0]["bucket"], "budget")
        self.assertEqual(after["inactive"]["count"], 0)

    # --- 10c. nadie acusa de inactividad --------------------------------
    def test_no_project_is_ever_flagged_idle(self):
        """El juicio `idle` se borró a propósito. Vivía de `tasks.completed_at`
        y las juntas con el cliente no están en esta base: Acme salía
        marcado teniendo juntas reales, y Vertex con ocho. El HECHO sobre tareas
        se conserva y se llama por su nombre; el juicio no vuelve."""
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "juntas-sin-tareas", weekly_hours=6.0)
            _mktask(conn, pid, status="done", completed_at=int(time.time()) - 90 * 86400)
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        row = body["projects"][0]
        self.assertNotIn("idle", row, "el juicio no vuelve por la puerta de atrás")
        self.assertNotIn("idle_count", body["declared"])
        self.assertNotIn("last_touch_days", row, "renombrado: el nombre dice el instrumento")
        # El hecho desnudo sigue disponible para que el operador juzgue él.
        self.assertEqual(row["done_28d"], 0)
        self.assertEqual(row["days_since_task"], 90)
        self.assertEqual(row["weekly_hours"], 6.0)
        self.assertEqual(body["declared"]["band"], "green")   # 6/10 = 60%, completo

    # --- 11. el ritual semanal ------------------------------------------
    def test_ritual_is_due_until_every_active_project_is_declared_this_week(self):
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            a = _mkproj(conn, "A")
            _mkproj(conn, "B")
            conn.commit()
        finally:
            conn.close()
        d = self._get()["declared"]
        self.assertTrue(d["ritual_due"])
        self.assertEqual(d["declared_this_week"], 0)
        self.assertEqual(d["pending_this_week"], 2)

        # Declarar UNO no cierra el ritual, y los pendientes bajan de 2 a 1.
        _CLIENT.patch(f"/api/projects/{a}", json={"weekly_hours": 2})
        d = self._get()["declared"]
        self.assertTrue(d["ritual_due"])
        self.assertEqual(d["declared_this_week"], 1)
        self.assertEqual(d["pending_this_week"], 1)
        for p in self._get()["projects"]:
            if p["id"] == a:
                self.assertTrue(p["set_this_week"])
                self.assertEqual(p["set_days_ago"], 0)
            else:
                self.assertFalse(p["set_this_week"])
                self.assertIsNone(p["set_days_ago"])

        # Declarar el ÚLTIMO lo cierra. Un ritual que nunca se cierra es una
        # alarma permanente, y una alarma permanente se ignora — esta aserción
        # es la que impide que el chip se quede encendido para siempre.
        b_id = [p["id"] for p in self._get()["projects"] if p["id"] != a][0]
        _CLIENT.patch(f"/api/projects/{b_id}", json={"weekly_hours": 0})
        d = self._get()["declared"]
        self.assertFalse(d["ritual_due"], "con todo declarado el ritual se cierra")
        self.assertEqual(d["declared_this_week"], 2)
        self.assertEqual(d["pending_this_week"], 0)
        # Aparcar (0h) TAMBIÉN es declarar: decir "esta semana no" es una
        # decisión, no una omisión.
        self.assertEqual(d["parked_count"], 1)

    def test_red_proof_last_sunday_is_within_7_days_but_is_not_this_week(self):
        """RED-PROOF del ritual, y tiene que DISCRIMINAR.

        Una marca de hace 30 días quedaría fuera tanto de la semana ISO como de
        una ventana móvil de 7 días: no distingue las dos reglas, así que no
        prueba nada. La instancia que sí las separa es el domingo pasado a las
        23:00 — SIEMPRE a menos de 7 días y SIEMPRE en la semana ISO anterior.
        Con semana ISO el ritual queda pendiente; con «hace menos de 7 días» el
        reparto de la semana pasada contaría como el de esta y el ritual no
        pediría nada nunca. Se calcula del lunes real, no de un literal, para
        que el contrato valga cualquier día que se corra."""
        now_dt = datetime.datetime.now()
        monday = (now_dt - datetime.timedelta(days=now_dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        last_week = int((monday - datetime.timedelta(hours=1)).timestamp())
        conn = _conn()
        try:
            _mkblock(conn, "consultant", "09:00", "19:00")
            pid = _mkproj(conn, "semana-pasada", weekly_hours=6.0)
            conn.execute("UPDATE projects SET weekly_hours_set_at = ? WHERE id = ?",
                         (last_week, pid))
            conn.commit()
        finally:
            conn.close()
        body = self._get()
        row = body["projects"][0]
        self.assertEqual(row["weekly_hours"], 6.0)      # el número sigue ahí…
        self.assertLessEqual(row["set_days_ago"], 7,
                             "la marca DEBE caer dentro de 7 días, o el test no discrimina")
        self.assertFalse(row["set_this_week"])          # …pero no es de esta semana
        self.assertNotEqual(row["set_week"], body["week"])
        self.assertTrue(body["declared"]["ritual_due"])
        self.assertEqual(body["declared"]["declared_this_week"], 0)

    def test_the_writer_stamps_the_date_with_the_number(self):
        """La marca viaja pegada al número: no hay forma de fingir el ritual sin
        mover horas, ni de mover horas sin dejar fecha."""
        conn = _conn()
        try:
            pid = _mkproj(conn, "P")
            conn.commit()
        finally:
            conn.close()
        _CLIENT.patch(f"/api/projects/{pid}", json={"weekly_hours": 4})
        conn = _conn()
        try:
            stamped = conn.execute(
                "SELECT weekly_hours_set_at FROM projects WHERE id = ?", (pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(stamped)
        self.assertLess(abs(int(time.time()) - stamped), 120)

        # Un PATCH que NO toca las horas no puede refrescar la fecha.
        _CLIENT.patch(f"/api/projects/{pid}", json={"name": "renombrado"})
        conn = _conn()
        try:
            again = conn.execute(
                "SELECT weekly_hours_set_at FROM projects WHERE id = ?", (pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(again, stamped, "renombrar no es repartir la semana")

    def test_an_empty_portfolio_has_no_ritual_pending(self):
        d = self._get()["declared"]
        self.assertEqual(d["active_count"], 0)
        self.assertFalse(d["ritual_due"])

    # --- 11. la migración es idempotente --------------------------------
    def test_migration_is_idempotent(self):
        conn = _conn()
        try:
            self.assertEqual(_m23(conn)["columns"], [])   # ya aplicadas en setUp
            self.assertEqual(_m24(conn)["columns"], [])
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
