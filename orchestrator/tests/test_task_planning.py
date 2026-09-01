import os
import sqlite3
import tempfile

os.environ.setdefault(
    "HERMES_BACKUP_DIR", os.path.join(tempfile.gettempdir(), "task-planning-test-backups")
)

from dashboard import api
from dashboard import db
from dashboard import task_planning
from dashboard.migrations.m29_task_plan_requests import m29_task_plan_requests


def _db(tmp_path):
    path = tmp_path / "planning.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, session_id TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t_plan', NULL)")
    m29_task_plan_requests(conn)
    conn.commit()
    conn.close()
    return path


def _row(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM task_plan_requests").fetchone())
    finally:
        conn.close()


def test_endpoint_records_requested_before_spawn_and_reports_real_session(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setattr(db, "KANBAN_DB", path)
    calls = []

    def fake_run(argv, timeout=45):
        calls.append(argv)
        assert _row(path)["state"] == "requested"
        return 0, ('{"session":"claude-demo-planning","attach_hint":"cs 2",'
                   '"folder":"/tmp/demo","planner":"fable"}\n'), ""

    monkeypatch.setattr(task_planning, "_run_cli", fake_run)
    monkeypatch.setattr(task_planning, "_task_plan_bin", lambda: "/bin/task-plan")
    assert "/api/tasks/{task_id}/plan" in {getattr(r, "path", None) for r in api.app.routes}
    body = api.api_plan_task(
        "t_plan", {"planner": "fable", "request_id": "plan_fixed"}
    )
    assert body["state"] == "created"
    assert body["session"] == "claude-demo-planning"
    assert body["attach_hint"] == "cs 2"
    assert calls == [["/bin/task-plan", "t_plan", "--planner", "fable", "--json"]]
    assert _row(path)["state"] == "created"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT session_id FROM tasks WHERE id='t_plan'").fetchone()[0] == "claude-demo-planning"
    conn.close()


def test_endpoint_spawn_failure_is_honest_and_idempotent(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setattr(db, "KANBAN_DB", path)
    calls = []

    def fail_run(argv, timeout=45):
        calls.append(argv)
        assert _row(path)["state"] == "requested"
        return 7, "", "tmux refused spawn"

    monkeypatch.setattr(task_planning, "_run_cli", fail_run)
    payload = {"planner": "sol", "request_id": "plan_fail"}
    first = api.api_plan_task("t_plan", payload)
    second = api.api_plan_task("t_plan", payload)
    assert first["state"] == "spawn_failed"
    assert "tmux refused spawn" in first["note"]
    assert first["session"] is None
    assert second["idempotent"] is True
    assert len(calls) == 1
    assert _row(path)["state"] == "spawn_failed"


def test_ui_contract_has_visible_picker_and_never_toasts_failed_spawn_as_created():
    html = (api.BASE_DIR / "templates" / "index.html").read_text()
    assert '>Planear</button>' in html
    for planner in ("fable", "opus1m", "sol"):
        assert f"planner: '{planner}'" in html
    assert "if (body.state !== 'created')" in html
    assert "No se creó sesión" in html
    assert "sesión ${body.session} creada — ábrela: ${body.attach_hint}" in html
