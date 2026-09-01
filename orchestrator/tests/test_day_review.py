"""Contracts for the Day Review collector, API, persistence, and Today UI."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import day_review


DAY = date(2026, 7, 20)


def _ts(hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 7, 20, hour, minute).astimezone().timestamp())


def test_collect_claude_and_codex_sessions(tmp_path):
    claude = tmp_path / ".claude" / "projects" / "-home-operator-dev-demo"
    claude.mkdir(parents=True)
    rows = [
        {"timestamp": datetime.fromtimestamp(_ts(9)).astimezone().isoformat(),
         "sessionId": "claude-1", "cwd": "/home/operator/dev/demo"},
        {"timestamp": datetime.fromtimestamp(_ts(10)).astimezone().isoformat(),
         "sessionId": "claude-1", "cwd": "/home/operator/dev/demo"},
    ]
    (claude / "claude-1.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "history.jsonl").write_text(
        json.dumps({"ts": _ts(11), "session_id": "codex-1", "text": "work on demo"}) + "\n"
        + json.dumps({"ts": _ts(11, 20), "session_id": "codex-1", "text": "run tests"}) + "\n"
    )

    c = day_review.collect_claude(DAY, home=tmp_path)
    x = day_review.collect_codex(DAY, home=tmp_path, tmux_sessions=[])

    assert [(a["kind"], a["project"], a["start_ts"], a["end_ts"]) for a in c] == [
        ("claude_session", "demo", _ts(9), _ts(10, 5))
    ]
    assert x[0]["kind"] == "codex_session"
    assert x[0]["end_ts"] == _ts(11, 25)


def test_kanban_cron_and_git_collectors(tmp_path):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    db = hermes / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT);"
        "CREATE TABLE task_events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,payload TEXT,created_at INTEGER);"
        "INSERT INTO tasks VALUES('t1','Ship timeline');"
    )
    conn.execute("INSERT INTO task_events VALUES(1,'t1','status_changed',?,?)",
                 (json.dumps({"from": "ready", "to": "in_progress"}), _ts(12)))
    conn.execute("INSERT INTO task_events VALUES(2,'t1','status_changed',?,?)",
                 (json.dumps({"from": "in_progress", "to": "done"}), _ts(13)))
    conn.commit(); conn.close()

    out = hermes / "cron" / "output" / "job1"
    out.mkdir(parents=True)
    (hermes / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "job1", "name": "Daily Standup"}]}))
    cron_file = out / "2026-07-20_09-00-00.md"
    cron_file.write_text("# Cron Job: Daily Standup\n**Run Time:** 2026-07-20 09:00:00\n")
    os.utime(cron_file, (_ts(9), _ts(9)))

    repo = tmp_path / "dev" / "demo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Operator"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "operator@example.test"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-07-20T14:00:00-06:00", "GIT_COMMITTER_DATE": "2026-07-20T14:00:00-06:00"}
    subprocess.run(["git", "add", "x"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "ship day review"], cwd=repo, check=True, env=env)

    events = day_review.collect_kanban(DAY, home=tmp_path)
    assert [e["label"] for e in events] == ["Started: Ship timeline", "Completed: Ship timeline"]
    assert day_review.collect_cron(DAY, home=tmp_path)[0]["label"] == "Cron · Daily Standup"
    commits = day_review.collect_git(DAY, dev_root=tmp_path / "dev", author="Test Operator")
    assert commits[0]["label"].endswith("ship day review")


def test_gap_fill_and_idempotent_persistence(tmp_path):
    activities = [
        day_review.activity(_ts(9), _ts(10), "claude_session", "Claude Code · demo", "demo", "claude", 3),
        day_review.activity(_ts(12), _ts(13), "git_commit", "Commit · demo", "demo", "git", 3),
    ]
    merged = day_review.merge_and_fill(activities, DAY, hours=(9, 14))
    assert any(a["kind"] == "gap" and a["start_ts"] == _ts(10) and a["end_ts"] == _ts(12) for a in merged)

    path = tmp_path / "day-reviews.jsonl"
    first = {"date": DAY.isoformat(), "generated_at": "one", "stats": {}, "timeline_text": "one"}
    second = {**first, "generated_at": "two", "timeline_text": "two"}
    day_review.persist_review(first, path)
    day_review.persist_review(second, path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [second]


def test_mac_collector_quotes_remote_code_and_fails_open(monkeypatch):
    # The laptop target is config (ORCHESTRATORMAXXING_EARTH_SSH / DAY_REVIEW_MAC_HOST),
    # never a hostname in code; unconfigured machines skip the remote hop.
    monkeypatch.setenv("ORCHESTRATORMAXXING_EARTH_SSH", "operator@laptop.example")
    expected = [day_review.activity(_ts(9), _ts(10), "claude_session",
                                    "Claude Code · mac", "demo", "claude:mac", 2)]

    def success(command, **_kwargs):
        assert command[-1].startswith("python3 -c '")
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected), stderr="")

    monkeypatch.setattr(day_review.subprocess, "run", success)
    rows, note = day_review.collect_mac(DAY)
    assert rows == expected
    assert note is None

    monkeypatch.setattr(day_review.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(returncode=255, stdout="", stderr="host asleep"))
    rows, note = day_review.collect_mac(DAY)
    assert rows == []
    assert note == "Mac unreachable: host asleep"

    # Standalone client: no configured earth host degrades to data, no ssh spawn.
    monkeypatch.delenv("ORCHESTRATORMAXXING_EARTH_SSH", raising=False)
    monkeypatch.delenv("DAY_REVIEW_MAC_HOST", raising=False)
    monkeypatch.setattr(day_review.subprocess, "run",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ssh spawned")))
    rows, note = day_review.collect_mac(DAY)
    assert rows == []
    assert "no earth host configured" in note


def test_api_and_today_template(monkeypatch):
    from dashboard import api
    from starlette.testclient import TestClient

    expected = {"date": DAY.isoformat(), "activities": [], "stats": {}, "timeline_text": "ok"}
    monkeypatch.setattr(api.day_review, "get_day_review", lambda value=None, raw=False: {**expected, "raw": raw})
    client = TestClient(api.app)
    assert client.get(f"/api/day-review?date={DAY}").json()["date"] == DAY.isoformat()
    assert client.get(f"/api/day-review/raw?date={DAY}").json()["raw"] is True

    # Re-pinned 2026-08-02: the Today tab's Day Review CARD was deleted with the
    # whole 🕐 Rhythm group (orphaned since July demoted the brief). The pruning
    # was UI-only, so the assertions that matter — the two API routes above —
    # are unchanged; what flipped is that the template must no longer mount the
    # card, and must not quietly grow one back.
    html = (Path(api.__file__).parent / "templates" / "index.html").read_text()
    assert 'id="today-day-review"' not in html
    assert "function renderDayReview" not in html
    assert "fetch('/api/day-review?date=today')" not in html
    # Wrap Day survived the deletion — it has its own button in the Today header.
    assert "onclick=\"wrapDay()\"" in html
