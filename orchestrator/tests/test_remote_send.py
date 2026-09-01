"""Contract for remote session read/write (Chunk A of the Hermes↔c/g bridge).

Authored by the orchestrator BEFORE the production code (Tier-0: root writes
the acceptance contract, the worker implements against it). Each assertion
discriminates a real failure mode:

  * SSH as the tmux-owning user, not the transport user — the Mac's `root`
    tmux server is empty, so user-mixups make live sessions invisible.
  * A remote send must be shell-quoted end-to-end — text travels inside an
    `ssh <host> <cmd>` string, so an unquoted prompt is command injection.
  * "failed" must be distinguishable from "couldn't measure" — never report
    `sent` on rc≠0/timeout, never fail open on an unknown host.
  * The local send budget must cover tmux-send's worst case (~3s wait × 3
    retries + margin); a 5s cap reported false errors for landed sends.
"""

import shlex
import subprocess

import pytest

from dashboard import sessions

MAC = "remote-mac"
MAC_TMUX_USER = "mac-operator"

# Neutral fixture fleet row: these tests pin the CONFIG PLUMBING (a
# REMOTE_HOSTS row drives transport identity), never a real tenant's
# inventory. `user` differs from `tmux_user` on purpose - the contract
# discriminates "SSH as the tmux-owning user, not the transport user".
MAC_ROW = {
    "name": "Remote Mac",
    "host": MAC,
    "user": "root",
    "tmux_user": MAC_TMUX_USER,
    "tailscale_ip": "100.64.0.2",
    "icon": "mac",
    "home": "/Users/mac-operator",
}


@pytest.fixture(autouse=True)
def fleet(monkeypatch):
    """Register the fixture host for every test: REMOTE_HOSTS is config-loaded
    (HERMES_REMOTE_HOSTS / ~/.hermes/remote-hosts.json) and empty in the test
    environment by design."""
    monkeypatch.setattr(sessions, "REMOTE_HOSTS", [MAC_ROW])


class _Calls(list):
    """A list of captured subprocess.run calls that also carries the fake."""
    fake = None


@pytest.fixture
def calls(monkeypatch):
    """Capture every subprocess.run the module makes; default rc=0."""
    seen = _Calls()

    def fake_run(argv, **kw):
        seen.append({"argv": list(argv), "kw": kw})
        rc = fake_run.rc
        if fake_run.raise_timeout:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 1))
        # Honor capture_output/text like the real subprocess: without them the
        # streams come back as None / bytes, so a sender that stops capturing
        # (or stops decoding) loses its error evidence — the contract must
        # notice that.
        if not kw.get("capture_output"):
            return subprocess.CompletedProcess(argv, rc, None, None)
        if kw.get("text"):
            return subprocess.CompletedProcess(argv, rc, fake_run.stdout,
                                               fake_run.stderr)
        return subprocess.CompletedProcess(argv, rc, fake_run.stdout.encode(),
                                           fake_run.stderr.encode())

    fake_run.rc = 0
    fake_run.stdout = ""
    fake_run.stderr = ""
    fake_run.raise_timeout = False
    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    seen.fake = fake_run
    return seen


@pytest.fixture
def resolved(monkeypatch):
    """Pin the resolver so send tests exercise only the transport."""
    monkeypatch.setattr(sessions, "resolve_tmux_target",
                        lambda host, name: "claude-scratch")


# --- remote send transport ---

def test_remote_send_sshes_as_tmux_user(calls, resolved):
    result = sessions.send_to_session(MAC, "claude-scratch", "hola")
    assert result["status"] == "sent"
    assert result["confirmed_executing"] is True
    ssh = [c for c in calls if c["argv"][:2] == ["tailscale", "ssh"]]
    assert ssh, "remote send never went over tailscale ssh"
    assert ssh[-1]["argv"][2] == f"{MAC_TMUX_USER}@{MAC}", (
        "must SSH as the tmux-owning user, not the transport user")


def test_remote_send_routes_through_tmux_send_quoted(calls, resolved):
    hostile = "review this: '; rm -rf /tmp/x; echo '"
    sessions.send_to_session(MAC, "claude-scratch", hostile)
    ssh = [c for c in calls if c["argv"][:2] == ["tailscale", "ssh"]]
    assert ssh, "remote send never went over tailscale ssh"
    remote_cmd = ssh[-1]["argv"][3]
    assert "tmux-send" in remote_cmd, "remote send must use the tmux-send validator"
    assert shlex.quote(hostile) in remote_cmd, (
        "prompt text must be shlex-quoted inside the remote command")
    assert shlex.quote("claude-scratch") in remote_cmd


def test_remote_send_unknown_host_fails_closed(calls, resolved):
    result = sessions.send_to_session("no-such-host", "claude-scratch", "hola")
    assert result["status"] == "error"
    assert not result.get("confirmed_executing")
    assert not calls, "unknown host must not reach the network"


def test_remote_send_unresolved_session_is_error(calls, monkeypatch):
    monkeypatch.setattr(sessions, "resolve_tmux_target", lambda h, n: None)
    result = sessions.send_to_session(MAC, "ghost", "hola")
    assert result["status"] == "error"
    assert not calls, "unresolved session must not reach the network"


def test_remote_send_rc1_is_error_not_sent(calls, resolved):
    calls.fake.rc = 1
    calls.fake.stderr = "tmux-send: session not executing"
    result = sessions.send_to_session(MAC, "claude-scratch", "hola")
    assert result["status"] == "error"
    assert result.get("confirmed_executing") is False
    assert "tmux-send" in result.get("error", "")


def test_remote_send_timeout_is_error_not_sent(calls, resolved):
    calls.fake.raise_timeout = True
    result = sessions.send_to_session(MAC, "claude-scratch", "hola")
    assert result["status"] == "error"
    assert result.get("confirmed_executing") is False


# --- local send budget ---

def test_local_send_budget_covers_tmux_send_retries(calls, resolved):
    result = sessions.send_to_session("local", "claude-scratch", "hola")
    assert result["status"] == "sent"
    assert calls, "local send must invoke tmux-send"
    last = calls[-1]
    assert last["argv"][0].endswith("tmux-send")
    assert last["kw"].get("timeout", 0) >= 30, (
        "tmux-send retries can take ~15-20s; a 5s budget reports false errors")


def test_remote_send_budget_covers_tmux_send_retries(calls, resolved):
    sessions.send_to_session(MAC, "claude-scratch", "hola")
    ssh = [c for c in calls if c["argv"][:2] == ["tailscale", "ssh"]]
    assert ssh and ssh[-1]["kw"].get("timeout", 0) >= 30


# --- remote enumeration / capture identity ---

def test_tmux_registry_sshes_as_tmux_user(monkeypatch):
    seen = []

    def fake_remote(host, user, cmd, timeout=8):
        seen.append({"host": host, "user": user, "cmd": cmd})
        return "claude-scratch|/tmp/proj|uuid-1"

    monkeypatch.setattr(sessions, "_run_remote", fake_remote)
    reg = sessions._tmux_registry(MAC)
    assert reg and reg[0]["name"] == "claude-scratch"
    assert seen[0]["user"] == MAC_TMUX_USER, (
        "registry must enumerate the login user's tmux server, not root's")


def test_capture_pane_sshes_as_tmux_user(monkeypatch):
    seen = []

    def fake_remote(host, user, cmd, timeout=8):
        seen.append({"host": host, "user": user, "cmd": cmd})
        return "pane output line"

    monkeypatch.setattr(sessions, "_run_remote", fake_remote)
    monkeypatch.setattr(sessions, "resolve_tmux_target",
                        lambda h, n: "claude-scratch")
    kind, payload = sessions._resolve_session_source(MAC, "claude-scratch", 30)
    assert kind == "terminal"
    capture = [c for c in seen if "capture-pane" in c["cmd"]]
    assert capture and capture[0]["user"] == MAC_TMUX_USER, (
        "pane capture must run as the tmux-owning user")
