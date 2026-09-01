import shutil

import pytest

from dashboard import sessions


pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux is required for real transport tests",
)


def test_local_send_reports_real_tmux_send_failure(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "resolve_tmux_target",
        lambda host, session_name: "nonexistent-session-deadbeef",
    )

    result = sessions.send_to_session("local", "ignored", "test prompt")

    assert result["status"] == "error"
    assert result["confirmed_executing"] is False


def test_fully_nonexistent_session_returns_error():
    result = sessions.send_to_session(
        "local",
        "nonexistent-session-deadbeef",
        "test prompt",
    )

    assert result["status"] == "error"


def test_unknown_remote_host_is_refused_before_transport(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "resolve_tmux_target",
        lambda host, session_name: "remote-session",
    )

    result = sessions.send_to_session("remote-host", "remote-session", "test prompt")

    assert result == {
        "status": "error",
        "confirmed_executing": False,
        "error": "unknown_host",
    }
