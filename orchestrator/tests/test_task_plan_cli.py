import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LOADER = importlib.machinery.SourceFileLoader("task_plan_cli", str(ROOT / "bin" / "task-plan"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
task_plan = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(task_plan)


def _fixture_db(path: Path, repo_path: str | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, slug TEXT, repo_path TEXT);
    CREATE TABLE deals (id TEXT PRIMARY KEY, title TEXT, project_id TEXT);
    CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, project_id TEXT, deal_id TEXT);
    """)
    conn.execute("INSERT INTO projects VALUES (?,?,?,?)", ("p1", "Demo", "demo", repo_path))
    conn.execute("INSERT INTO deals VALUES (?,?,?)", ("d1", "Signed", "p1"))
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?)",
        ("t1", "Plan me", "Context\n\n## Acceptance\n- ship exact brief", "p1", "d1"),
    )
    conn.commit()
    conn.close()


def test_read_only_resolution_precedence_and_actionable_refusal(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _fixture_db(db_path, str(explicit))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    row = task_plan._read_task("t1")
    assert task_plan._resolve_folder(row) == explicit.resolve()
    assert "Task ID: t1" in task_plan._brief(row)
    assert "- ship exact brief" in task_plan._brief(row)

    fallback = tmp_path / "home" / "dev" / "demo"
    fallback.mkdir(parents=True)
    row["repo_path"] = str(tmp_path / "gone")
    assert task_plan._resolve_folder(row) == fallback.resolve()
    fallback.rmdir()
    with pytest.raises(task_plan.TaskPlanError, match="Populate projects.repo_path"):
        task_plan._resolve_folder(row)


@pytest.mark.parametrize(
    "planner,expected_host,launcher_env,launcher_name,required",
    [
        ("fable", "claude", "TASK_PLAN_C_SH", "claude-c.sh", ["/fableplan", "Task ID: t1"]),
        ("opus1m", "claude", "TASK_PLAN_C_SH", "claude-c.sh", ["--model", "opus[1m]", "--permission-mode", "plan"]),
        ("sol", "codex", "TASK_PLAN_G_SH", "codex-g.sh", ["$orchestratormaxxing:solplan", "Task ID: t1"]),
        ("oplan", "opencode", "TASK_PLAN_O_SH", "opencode-o.sh", ["--agent", "kimiplan", "--", "--prompt", "Task ID: t1"]),
        ("kimiplan", "opencode", "TASK_PLAN_O_SH", "opencode-o.sh", ["--agent", "kimiplan", "--", "--prompt", "Task ID: t1"]),
    ],
)
def test_launch_argv_keeps_planner_contract_literal(
    tmp_path, monkeypatch, planner, expected_host, launcher_env, launcher_name, required
):
    launcher = tmp_path / launcher_name
    launcher.write_text("# fixture\n")
    monkeypatch.setenv(launcher_env, str(launcher))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return type("P", (), {"returncode": 0, "stdout": f"{expected_host}-demo-planning\n", "stderr": ""})()

    monkeypatch.setattr(task_plan.subprocess, "run", fake_run)
    task = {"id": "t1", "project_slug": "demo", "title": "Plan me"}
    session, host = task_plan._launch(task, tmp_path, planner, "Task ID: t1")
    assert session == f"{expected_host}-demo-planning"
    assert host == expected_host
    flattened = "\n".join(calls[0])
    for token in required:
        assert token in flattened
    assert "--detach" in calls[0] and "planning" in calls[0] and "t1" in calls[0]

    if planner in {"oplan", "kimiplan"}:
        agent = "kimiplan"  # oplan is a compatibility alias to the canonical kimiplan agent
        argv = calls[0]
        start = argv.index("--agent")
        assert argv[start : start + 5] == ["--agent", agent, "--", "--prompt", "Task ID: t1"]


def test_kimiplan_assets_are_bounded_and_read_only():
    agent = (ROOT / "opencode" / "agents" / "kimiplan.md").read_text()
    command = (ROOT / "opencode" / "commands" / "kimiplan.md").read_text()

    for token in (
        "model: ollama-cloud/kimi-k3",
        "temperature: 0.1",
        "steps: 20",
        "edit: deny",
        "bash: deny",
        "SUMMARY",
        "STEPS",
        "CONTRACT",
        "EXECUTION SHAPE",
        "RISKS / ASSUMPTIONS",
        "OUT OF SCOPE",
        "1,200 words",
    ):
        assert token in agent
    assert "agent: kimiplan" in command
    assert "subtask: true" in command
    assert "$ARGUMENTS" in command
