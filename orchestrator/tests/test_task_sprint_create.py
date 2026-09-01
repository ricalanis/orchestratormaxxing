"""Task creation with ``sprint_id`` must be persisted or reported honestly.

The Hermes CLI owns the base task INSERT and has no sprint argument.  Both the
MCP and REST create paths therefore perform a sidecar ``assign_task_sprint``
after the INSERT.  These tests cross that real DB boundary while stubbing only
the CLI process.  Every write lands in a private copy of pytest's session DB;
the operator's live ``~/.hermes/kanban.db`` is never opened for writing.
"""
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server as mcp
from dashboard import api, canvas, db, sprints


@pytest.fixture()
def sprint_create_db(tmp_path, monkeypatch):
    target = tmp_path / "task-sprint-create.db"
    shutil.copy(db.KANBAN_DB, target)
    monkeypatch.setattr(db, "KANBAN_DB", target)
    monkeypatch.setattr(sprints, "KANBAN_DB", target)
    monkeypatch.setattr(mcp, "KANBAN_DB", target)

    now = int(time.time())
    active_id = "cyc_test_create_active"
    completed_id = "cyc_test_create_completed"
    conn = sqlite3.connect(target)
    conn.execute("UPDATE sprints SET status = 'planning' WHERE status = 'active'")
    conn.executemany(
        "INSERT OR REPLACE INTO sprints "
        "(id, project_id, name, start_date, end_date, status, closed_at, created_at) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            (active_id, "Create active", now - 60, now + 7 * 86400,
             "active", None, now - 60),
            (completed_id, "Create completed", now - 14 * 86400,
             now - 7 * 86400, "completed", now - 7 * 86400, now - 14 * 86400),
        ],
    )
    project_id = conn.execute(
        "SELECT id FROM projects WHERE archived_at IS NULL "
        "AND COALESCE(kind, 'product') NOT IN ('personal', 'system') LIMIT 1"
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return target, project_id, active_id, completed_id


def _insert_cli_task(path, task_id, title):
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.execute(
        "INSERT INTO tasks "
        "(id,title,assignee,status,priority,created_by,created_at,workspace_kind,"
        "consecutive_failures,goal_mode) VALUES (?,?,?,'ready',99,'test',?,'scratch',0,0)",
        (task_id, title, "ricardo", int(time.time())),
    )
    conn.commit()
    conn.close()


def _task_and_ledger(path, task_id):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    task = conn.execute(
        "SELECT sprint_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    ledger = conn.execute(
        "SELECT sprint_id, outcome FROM task_sprints WHERE task_id = ?", (task_id,)
    ).fetchall()
    conn.close()
    return (dict(task) if task else None), [dict(row) for row in ledger]


def _stub_mcp_cli(monkeypatch, path, task_id):
    calls = []

    def fake_cli(argv):
        calls.append(list(argv))
        _insert_cli_task(path, task_id, argv[1])
        return 0, json.dumps({"id": task_id}), ""

    monkeypatch.setattr(mcp, "run_hermes_cli", fake_cli)
    return calls


def _stub_api_cli(monkeypatch, path, task_id):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        cmd = args[0]
        _insert_cli_task(path, task_id, cmd[3])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": task_id}), stderr="")

    # Replace api's module reference, not stdlib subprocess.run process-wide:
    # pytest and asyncio also use the stdlib module during teardown.
    monkeypatch.setattr(api, "subprocess", SimpleNamespace(run=fake_run))
    return calls


def _run_api_inline(monkeypatch, coroutine):
    """Run api_create_task without a thread-pool lifecycle in this unit test."""
    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(api, "asyncio", SimpleNamespace(to_thread=inline))
    return asyncio.run(coroutine)


def test_mcp_active_sprint_persists_pointer_ledger_and_this_week_bucket(
        sprint_create_db, monkeypatch):
    path, project_id, active_id, _ = sprint_create_db
    task_id = "t_a11ce001"
    _stub_mcp_cli(monkeypatch, path, task_id)

    result = json.loads(mcp.tool_create_task({
        "title": "Active sprint create", "assignee": "ricardo",
        "project_id": project_id, "sprint_id": active_id,
    }))

    task, ledger = _task_and_ledger(path, task_id)
    assert result["sprint_id"] == active_id
    assert task == {"sprint_id": active_id}
    assert ledger == [{"sprint_id": active_id, "outcome": None}]
    groups = {
        name: {row["id"] for row in rows}
        for name, rows in canvas.get_day_plan()["later_groups"].items()
    }
    assert task_id in groups["this_week"]
    assert all(task_id not in groups[name] for name in ("next_week", "future", "backlog"))


def test_mcp_completed_sprint_is_rejected_before_task_creation(
        sprint_create_db, monkeypatch):
    path, project_id, _, completed_id = sprint_create_db
    task_id = "t_c105ed01"
    calls = _stub_mcp_cli(monkeypatch, path, task_id)

    result = json.loads(mcp.tool_create_task({
        "title": "Stale sprint create", "assignee": "ricardo",
        "project_id": project_id, "sprint_id": completed_id,
    }))

    assert calls == []
    assert result["error"].endswith("is completed — commit to the active cycle instead")
    assert _task_and_ledger(path, task_id) == (None, [])


def test_mcp_post_create_assignment_failure_is_reported_not_claimed(
        sprint_create_db, monkeypatch):
    path, project_id, active_id, _ = sprint_create_db
    task_id = "t_ace00002"
    _stub_mcp_cli(monkeypatch, path, task_id)
    monkeypatch.setattr(
        mcp._sprints, "assign_task_sprint",
        lambda *_: {"status": "error", "error": "cycle closed during create"},
    )

    result = json.loads(mcp.tool_create_task({
        "title": "Racing sprint create", "assignee": "ricardo",
        "project_id": project_id, "sprint_id": active_id,
    }))

    assert result["status"] == "created"
    assert result["sprint_id"] is None
    assert result["sprint_error"] == "cycle closed during create"
    assert _task_and_ledger(path, task_id) == ({"sprint_id": None}, [])


def test_rest_completed_sprint_returns_409_before_cli(
        sprint_create_db, monkeypatch):
    path, project_id, _, completed_id = sprint_create_db
    task_id = "t_c105ed02"
    calls = _stub_api_cli(monkeypatch, path, task_id)

    with pytest.raises(HTTPException) as exc:
        _run_api_inline(monkeypatch, api.api_create_task(api.TaskCreate(
            title="REST stale sprint", project_id=project_id, sprint_id=completed_id,
        )))

    assert exc.value.status_code == 409
    assert calls == []
    assert _task_and_ledger(path, task_id) == (None, [])


def test_rest_active_sprint_persists_pointer_and_reports_assignment(
        sprint_create_db, monkeypatch):
    path, project_id, active_id, _ = sprint_create_db
    task_id = "t_ace00004"
    _stub_api_cli(monkeypatch, path, task_id)

    result = _run_api_inline(monkeypatch, api.api_create_task(api.TaskCreate(
        title="REST active sprint", project_id=project_id, sprint_id=active_id,
    )))

    assert result["sprint_assigned"] is True
    assert result["warnings"] == []
    assert _task_and_ledger(path, task_id) == (
        {"sprint_id": active_id},
        [{"sprint_id": active_id, "outcome": None}],
    )


def test_rest_post_create_assignment_failure_is_an_explicit_partial_warning(
        sprint_create_db, monkeypatch):
    path, project_id, active_id, _ = sprint_create_db
    task_id = "t_ace00003"
    _stub_api_cli(monkeypatch, path, task_id)
    monkeypatch.setattr(
        api.sprints, "assign_task_sprint",
        lambda *_: {"status": "error", "error": "cycle closed during create"},
    )

    result = _run_api_inline(monkeypatch, api.api_create_task(api.TaskCreate(
        title="REST racing sprint", project_id=project_id, sprint_id=active_id,
    )))

    assert result["status"] == "created"
    assert result["sprint_assigned"] is False
    assert result["warnings"] == ["sprint link failed: cycle closed during create"]
    assert _task_and_ledger(path, task_id) == ({"sprint_id": None}, [])
