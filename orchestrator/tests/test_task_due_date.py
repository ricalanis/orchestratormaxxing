"""Authoritative due-date contract for task create/update/context.

Runs only against a private copy of the pytest session sandbox.  The CLI create
boundary is stubbed with a function that inserts into that private copy; no
Hermes subprocess or live dashboard is touched.
"""
import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dashboard import api, context, db, sprints


@pytest.fixture()
def due_db(tmp_path, monkeypatch):
    target = tmp_path / "due-date.db"
    shutil.copy(Path(db.KANBAN_DB), target)
    monkeypatch.setattr(db, "KANBAN_DB", target)
    monkeypatch.setattr(sprints, "KANBAN_DB", target)

    task_id = "t_due_contract"
    conn = sqlite3.connect(target)
    conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return target, task_id


def _insert_task(path: Path, task_id: str, due_date=None, status="backlog"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tasks "
        "(id,title,status,priority,created_by,created_at,workspace_kind,"
        "consecutive_failures,goal_mode,due_date) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, "Due contract", status, 0, "test", int(time.time()),
         "none", 0, 0, due_date),
    )
    conn.commit()
    conn.close()


def _task_row(path: Path, task_id: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, due_date FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def test_update_validates_a_real_date_and_clear_stores_sql_null(due_db):
    path, task_id = due_db
    _insert_task(path, task_id)

    set_result = sprints.update_task_fields(task_id, due_date="2026-08-21")
    assert set_result["status"] == "updated"
    assert _task_row(path, task_id)["due_date"] == "2026-08-21"

    clear_result = sprints.update_task_fields(task_id, due_date="")
    assert clear_result["status"] == "updated"
    assert _task_row(path, task_id)["due_date"] is None

    conn = sqlite3.connect(path)
    payloads = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'task_updated' "
        "ORDER BY id", (task_id,)
    )]
    conn.close()
    assert payloads[-1]["changed"]["due_date"]["to"] is None


def test_writer_refuses_bad_types_and_preserves_omitted_or_unchanged_date(due_db):
    path, task_id = due_db
    _insert_task(path, task_id, due_date="2026-08-21")

    bad = sprints.update_task_fields(task_id, due_date=20260821)
    assert bad["status"] == "error"
    assert _task_row(path, task_id)["due_date"] == "2026-08-21"

    omitted = sprints.update_task_fields(task_id)
    same = sprints.update_task_fields(task_id, due_date="2026-08-21")
    assert omitted["status"] == "unchanged"
    assert same["status"] == "unchanged"
    assert _task_row(path, task_id)["due_date"] == "2026-08-21"

    missing = sprints.update_task_fields("t_due_missing", due_date="2026-08-21")
    assert missing == {"status": "error", "error": "task not found"}


@pytest.mark.parametrize("bad", [
    "2026-8-05", "2026-02-30", "today", "2026-08-05T09:00:00",
    " 2026-08-05 ",
])
def test_invalid_update_is_refused_before_any_other_patch_write(due_db, bad):
    path, task_id = due_db
    _insert_task(path, task_id, due_date="2026-08-20", status="backlog")

    with pytest.raises(HTTPException) as exc:
        api.api_update_task(
            task_id,
            api.TaskUpdate(status="done", due_date=bad),
        )

    assert exc.value.status_code == 400
    assert _task_row(path, task_id) == {
        "status": "backlog", "due_date": "2026-08-20"
    }


def test_context_exposes_due_date_and_clear_as_null(due_db):
    path, task_id = due_db
    _insert_task(path, task_id, due_date="2026-08-21")

    assert context.build_context("task", task_id)["entity"]["due_date"] == "2026-08-21"
    sprints.update_task_fields(task_id, due_date="")
    assert context.build_context("task", task_id)["entity"]["due_date"] is None


def test_create_validates_before_cli_and_persists_due_date(due_db, monkeypatch):
    path, task_id = due_db
    cli_calls = []

    def fake_run(*args, **kwargs):
        cli_calls.append(args)
        _insert_task(path, task_id)
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"id": task_id}), stderr=""
        )

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(
        api.identity, "resolve_create_project",
        lambda explicit, session_key=None: None,
    )
    monkeypatch.setattr(
        api.sprints, "assign_task_project",
        lambda *args, **kwargs: {"status": "unchanged"},
    )

    result = asyncio.run(api.api_create_task(api.TaskCreate(
        title="Due contract", due_date="2026-08-21"
    )))

    assert len(cli_calls) == 1
    assert result["task_id"] == task_id
    assert result["due_date_set"] is True
    assert _task_row(path, task_id)["due_date"] == "2026-08-21"


def test_invalid_create_never_invokes_cli(due_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        api.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.api_create_task(api.TaskCreate(
            title="Bad due", due_date="2026-02-30"
        )))

    assert exc.value.status_code == 400
    assert calls == []
