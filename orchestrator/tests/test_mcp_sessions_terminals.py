"""Contract for MCP session enumeration + output bounds (Chunk B).

Hermes drives these tools from Telegram, so the responses must be small and
self-describing:

  * `get_sessions` must NAME the c/g terminals per host (counts alone are not
    actionable from a chat), capped so a runaway host can't flood the reply.
  * `get_sessions` must go through the `_dash` loopback proxy — the
    human-facing DASHBOARD_URL may resolve to the tailscale-serve front.
  * `lines` must be clamped at the MCP boundary; a chat client passing a huge
    or negative value must not translate into an unbounded capture.
"""

import json

import pytest

import mcp_server


def _payload(local_names=("claude-alpha", "codex-beta", "opencode-gamma",
                          "misc-shell"),
             remote=(("claude-mac", "remote-mac"),)):
    return {
        "claude_code": [],
        "opencode": [],
        "hosts": {},
        "total_active": 1,
        "tmux_local": [
            {"name": n, "attached": True, "path": "/tmp/p", "agent_type": "x"}
            for n in local_names
        ],
        "tmux_remote": [
            {"name": n, "host": h, "attached": False, "path": "/tmp/r",
             "host_name": "Mac"}
            for n, h in remote
        ],
    }


@pytest.fixture
def no_direct_curl(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tool must not shell out around _dash")
    monkeypatch.setattr(mcp_server.subprocess, "run", boom)


def test_get_sessions_names_terminals_per_host(monkeypatch, no_direct_curl):
    dash_calls = []

    def fake_dash(method, path, body=None):
        dash_calls.append((method, path))
        return json.dumps(_payload())

    monkeypatch.setattr(mcp_server, "_dash", fake_dash)
    out = json.loads(mcp_server.tool_get_sessions({}))

    assert dash_calls == [("GET", "/api/sessions")], (
        "get_sessions must use the _dash loopback proxy")
    terms = out["terminals"]
    names = {(t["name"], t["host"]) for t in terms}
    assert ("claude-alpha", "local") in names
    assert ("codex-beta", "local") in names
    assert ("opencode-gamma", "local") in names
    assert ("claude-mac", "remote-mac") in names
    assert all(t["name"].startswith(("claude-", "codex-", "opencode-"))
               for t in terms), (
        "only c/g/o sessions belong in the chat-facing list")
    for t in terms:
        assert {"name", "host", "attached", "path", "agent"} <= set(t)
    agent_of = {t["name"]: t["agent"] for t in terms}
    assert agent_of["claude-alpha"] == "claude"
    assert agent_of["codex-beta"] == "codex"
    assert agent_of["opencode-gamma"] == "opencode"


def test_get_sessions_terminals_capped_at_40(monkeypatch, no_direct_curl):
    many = tuple(f"claude-{i}" for i in range(60))
    monkeypatch.setattr(mcp_server, "_dash",
                        lambda m, p, b=None: json.dumps(_payload(local_names=many)))
    out = json.loads(mcp_server.tool_get_sessions({}))
    assert len(out["terminals"]) == 40


def test_get_sessions_error_passthrough(monkeypatch, no_direct_curl):
    monkeypatch.setattr(mcp_server, "_dash",
                        lambda m, p, b=None: json.dumps({"error": "dashboard down"}))
    out = json.loads(mcp_server.tool_get_sessions({}))
    assert "error" in out


@pytest.mark.parametrize("asked,expected", [(999999, 500), (500, 500), (7, 7), (-3, 1)])
def test_get_session_output_clamps_lines(monkeypatch, asked, expected):
    paths = []
    monkeypatch.setattr(mcp_server, "_dash",
                        lambda m, p, b=None: (paths.append(p), "{}")[1])
    mcp_server.tool_get_session_output(
        {"host": "local", "session_name": "claude-x", "lines": asked})
    assert paths and paths[0].endswith(f"lines={expected}"), paths


def test_send_to_session_outlives_tmux_send_retries(monkeypatch):
    """The send proxy must wait longer than the sender's worst case (~35s),
    or a slow-but-landed send reads as 'couldn't measure'."""
    seen = {}

    def fake_dash(method, path, body=None, timeout=30):
        seen.update(method=method, path=path, timeout=timeout)
        return "{}"

    monkeypatch.setattr(mcp_server, "_dash", fake_dash)
    mcp_server.tool_send_to_session(
        {"host": "local", "session_name": "claude-x", "text": "hola"})
    assert seen["method"] == "POST"
    assert seen["timeout"] >= 40


def test_api_session_output_clamps_lines(monkeypatch):
    from dashboard import api

    seen = {}

    def fake_view(host, session_name, lines):
        seen["lines"] = lines
        return {"kind": "terminal", "output": "", "messages": []}

    monkeypatch.setattr(api.sessions, "get_session_view", fake_view)
    api.api_session_output("local", "claude-x", lines=10**6)
    assert seen["lines"] == 500
    api.api_session_output("local", "claude-x", lines=-5)
    assert seen["lines"] == 1
