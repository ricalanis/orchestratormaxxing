"""Contracts for the Projects > Plan weekly allocation view."""

from pathlib import Path
import time
import uuid
import pytest

from dashboard import db, plan, sprints
from dashboard.migrations.m26_project_week_plan import m26_project_week_plan


def _sandbox_conn():
    conn = db.get_conn()
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    live_path = (Path.home() / ".hermes" / "kanban.db").resolve()
    assert db_path != live_path, f"refusing to mutate live DB: {db_path}"
    m26_project_week_plan(conn)
    return conn


def _insert_project(conn, name, weekly_hours=2.0):
    pid = f"plan_{uuid.uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO projects (id, slug, name, color, icon, created_at, status, "
        "kind, weekly_hours, weekly_hours_set_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, pid, name, "#3b82f6", "📦", int(time.time()), "active",
         "product", weekly_hours, 1),
    )
    return pid


def test_semana_sin_planear_es_gris_nunca_verde():
    conn = _sandbox_conn()
    try:
        conn.execute("DELETE FROM project_week_plan")
        data = plan.week_plan(conn=conn)
        assert data["weeks"][1]["planned_count"] == 0
        assert data["weeks"][1]["band"] == "unknown"
    finally:
        conn.rollback()
        conn.close()


def test_la_semana_en_curso_no_la_escribe_el_plan():
    cur = sprints._iso_week_str()
    from dashboard.api import api_projects_plan_cell
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused_http:
        api_projects_plan_cell({
            "project_id": "does-not-matter", "iso_week": cur, "hours": 4.0,
        })
    assert refused_http.value.status_code == 409

    conn = _sandbox_conn()
    pid = _insert_project(conn, "No tocar presente", weekly_hours=2.0)
    conn.commit()
    future = sprints._iso_week_str(offset_weeks=1)
    try:
        before = conn.execute(
            "SELECT weekly_hours, weekly_hours_set_at FROM projects WHERE id=?", (pid,)
        ).fetchone()
        refused = plan.set_cell(pid, cur, 4.0)
        assert refused == {
            "status": "error",
            "error": "la semana en curso se declara en projects.weekly_hours "
                     "(PATCH /api/projects/{id})",
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM project_week_plan WHERE project_id=?", (pid,)
        ).fetchone()[0] == 0

        written = plan.set_cell(pid, future, 4.0)
        assert written["status"] == "updated"
        assert conn.execute(
            "SELECT hours FROM project_week_plan WHERE project_id=? AND iso_week=?",
            (pid, future),
        ).fetchone()[0] == 4.0
        after = conn.execute(
            "SELECT weekly_hours, weekly_hours_set_at FROM projects WHERE id=?", (pid,)
        ).fetchone()
        assert tuple(after) == tuple(before)
    finally:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
        conn.close()


def test_una_fila_vencida_no_se_promueve_ni_se_borra_en_la_lectura():
    conn = _sandbox_conn()
    pid = _insert_project(conn, "Aplicar propuesta", weekly_hours=2.0)
    cur = sprints._iso_week_str()
    conn.execute(
        "INSERT INTO project_week_plan(project_id, iso_week, hours, set_at) "
        "VALUES (?,?,9.0,?)", (pid, cur, int(time.time()))
    )
    conn.commit()
    try:
        for _ in range(2):
            data = plan.week_plan()
            project = next(p for p in data["projects"] if p["id"] == pid)
            assert project["cells"][0]["hours"] == 2.0
            assert project["cells"][0]["proposal"] == 9.0
            assert conn.execute(
                "SELECT weekly_hours FROM projects WHERE id=?", (pid,)
            ).fetchone()[0] == 2.0
            assert conn.execute(
                "SELECT COUNT(*) FROM project_week_plan WHERE project_id=?", (pid,)
            ).fetchone()[0] == 1

        applied = plan.apply_overdue(pid)
        assert applied["status"] == "updated"
        row = conn.execute(
            "SELECT weekly_hours, weekly_hours_set_at FROM projects WHERE id=?", (pid,)
        ).fetchone()
        week_start, week_end = sprints._week_window()
        assert row[0] == 9.0
        assert week_start <= row[1] <= week_end
        assert conn.execute(
            "SELECT COUNT(*) FROM project_week_plan "
            "WHERE project_id=? AND iso_week<=?", (pid, cur)
        ).fetchone()[0] == 0
    finally:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
        conn.close()


def test_la_fragmentacion_sola_enrojece_una_semana_ligera():
    conn = _sandbox_conn()
    try:
        conn.execute("UPDATE projects SET status='planned'")
        conn.execute("DELETE FROM time_blocks")
        conn.execute(
            "INSERT INTO time_blocks (id, day_of_week, start_time, end_time, role, "
            "label, active, created_at) VALUES (?,?,?,?,?,?,1,?)",
            (f"tb_{uuid.uuid4().hex[:8]}", 1, "09:00", "17:00", "consultant",
             "delivery", int(time.time())),
        )
        # Four eight-hour delivery days = 32 h.
        for day in (2, 3, 4):
            conn.execute(
                "INSERT INTO time_blocks (id, day_of_week, start_time, end_time, role, "
                "label, active, created_at) VALUES (?,?,?,?,?,?,1,?)",
                (f"tb_{uuid.uuid4().hex[:8]}", day, "09:00", "17:00",
                 "consultant", "delivery", int(time.time())),
            )
        future = sprints._iso_week_str(offset_weeks=1)
        for i in range(6):
            pid = _insert_project(conn, f"Ligero {i}", weekly_hours=0.0)
            conn.execute(
                "INSERT INTO project_week_plan(project_id, iso_week, hours, set_at) "
                "VALUES (?,?,1.0,?)", (pid, future, int(time.time()))
            )
        data = plan.week_plan(conn=conn)
        week = data["weeks"][1]
        assert week["hours"] == 6.0
        assert week["n_projects"] == 6
        assert week["band_hours"] != "red"
        assert week["band_count"] == "red"
        assert week["band"] == "red"
    finally:
        conn.rollback()
        conn.close()


def test_red_proof_una_semana_sin_capacidad_jamas_es_verde():
    """Hallazgo de la crítica con OpenCode (glm-5.2) sobre el diff real.

    Sin bloques de entrega activos el denominador es 0, y week_plan caía a
    `utilization = 0.0`; con la medición completa, `_band(0.0, True)` devuelve
    "green". Una semana con 10h planeadas se pintaba VERDE sobre capacidad cero.
    Es el error que este módulo persigue desde el principio —ausencia de
    medición leída como cero— y `capacity.project_load` ya lo hacía bien
    (util None cuando delivery == 0); plan.py divergía.
    """
    conn = _sandbox_conn()
    try:
        conn.execute("DELETE FROM time_blocks")          # sin capacidad de entrega
        conn.execute("DELETE FROM project_week_plan")
        pid = _insert_project(conn, "sobre cero", weekly_hours=None)
        future = sprints._iso_week_str(offset_weeks=1)
        conn.execute(
            "INSERT INTO project_week_plan (project_id, iso_week, hours, set_at) "
            "VALUES (?,?,?,?)", (pid, future, 10.0, int(time.time())))
        conn.commit()

        data = plan.week_plan(conn=conn)
        w = [x for x in data["weeks"] if x["iso_week"] == future][0]
        assert data["delivery_hours"] == 0.0, "el fixture no tiene bloques de entrega"
        assert w["hours"] == 10.0 and w["planned_count"] >= 1
        assert w["utilization"] is None, \
            "sin denominador no hay porcentaje: 0.0 sería medir lo inmedible"
        assert w["band"] != "green", "verde sobre capacidad cero es la mentira"
    finally:
        conn.rollback()
        conn.close()


def test_aplicar_una_propuesta_es_idempotente_y_deja_de_proponerla():
    """Hallazgo de la crítica de OpenCode (severidad media): `apply_overdue` lee,
    escribe `weekly_hours` y borra la propuesta en TRES conexiones distintas, sin
    transacción. Si el proceso muere entre la escritura y el borrado, la fila
    vencida sobrevive y la UI sigue ofreciendo "aplicar" sobre algo ya aplicado.

    No es corrupción —reaplicar el mismo valor da el mismo estado— pero el
    contrato tiene que FIJAR esa idempotencia, o el día que alguien reordene las
    tres operaciones nadie se entera. Aplicar consume la propuesta; la segunda
    llamada se niega en vez de reescribir a ciegas.
    """
    prev = sprints._iso_week_str(offset_weeks=-1)
    conn = _sandbox_conn()
    try:
        pid = _insert_project(conn, "propuesta", weekly_hours=1.0)
        conn.execute(
            "INSERT INTO project_week_plan (project_id, iso_week, hours, set_at) "
            "VALUES (?,?,?,?)", (pid, prev, 6.0, int(time.time())))
        conn.commit()          # apply_overdue abre su PROPIA conexión: sin commit no lo ve

        primera = plan.apply_overdue(pid)
        assert primera["status"] == "updated", primera
        assert primera["hours"] == 6.0

        assert conn.execute("SELECT weekly_hours FROM projects WHERE id=?",
                            (pid,)).fetchone()[0] == 6.0
        # La propuesta se consumió: ya no queda nada vencido que ofrecer.
        assert conn.execute(
            "SELECT COUNT(*) FROM project_week_plan WHERE project_id=? AND iso_week<=?",
            (pid, sprints._iso_week_str())).fetchone()[0] == 0

        segunda = plan.apply_overdue(pid)
        assert segunda["status"] == "error", segunda
        assert conn.execute("SELECT weekly_hours FROM projects WHERE id=?",
                            (pid,)).fetchone()[0] == 6.0, "la segunda no movió nada"
    finally:
        conn.execute("DELETE FROM project_week_plan WHERE project_id LIKE 'plan_%'")
        conn.execute("DELETE FROM projects WHERE id LIKE 'plan_%'")
        conn.commit()
        conn.close()
