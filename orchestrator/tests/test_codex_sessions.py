from dashboard import sessions


def test_local_codex_tmux_session_is_classified(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "_run_local",
        lambda *args, **kwargs: "codex-orchestratormaxxing|1700000000|2|1|/tmp/project",
    )

    result = sessions.get_tmux_sessions()

    assert result == [
        {
            "name": "codex-orchestratormaxxing",
            "display_name": "codex-orchestratormaxxing",
            "agent_type": "codex",
            "origin": "user",
            "created": 1700000000,
            "windows": 2,
            "attached": True,
            "path": "/tmp/project",
            "status": "active",
        }
    ]


def test_remote_codex_tmux_session_is_classified(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "_run_remote",
        lambda *args, **kwargs: "codex-remote|1700000001|1|0|/srv/project",
    )
    host = {
        "host": "remote-box",
        "user": "agent",
        "tmux_user": "tmux-owner",
        "name": "Remote Box",
    }

    result = sessions.get_remote_tmux(host)

    assert result == [
        {
            "name": "codex-remote",
            "display_name": "codex-remote",
            "agent_type": "codex",
            "origin": "user",
            "created": 1700000001,
            "windows": 1,
            "attached": False,
            "path": "/srv/project",
            "host": "remote-box",
            "host_name": "Remote Box",
            "status": "active",
        }
    ]
