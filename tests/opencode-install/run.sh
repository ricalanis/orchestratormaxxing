#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import json
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1])
install = (root / "install.sh").read_text(encoding="utf-8")
marker = 'python3 - "$OPENCODE_CFG_DIR/opencode.json" "$REPO_DIR" <<\'PY\'\n'
start = install.index(marker) + len(marker)
program = install[start:install.index("\nPY\n", start)]

def run_config(cfg, home):
    path = pathlib.Path(home) / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ, HOME=str(home))
    subprocess.run([sys.executable, "-c", program, str(path), str(root)], env=env, check=True)
    return json.loads(path.read_text(encoding="utf-8"))

with tempfile.TemporaryDirectory() as tmp:
    home = pathlib.Path(tmp)
    custom = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"ollama-cloud": {"models": {"custom-model": {"name": "Keep"}}}},
        "agent": {
            "v4-coder": {
                "temperature": 0.4,
                "custom": {"migrate": True},
            },
            "kimi-coder": {
                "model": "old/model",
                "permission": {"bash": "deny"},
                "description": "user description",
                "temperature": 0.7,
                "prompt": "user prompt",
                "custom": {"keep": True},
            },
            "user-agent": {"model": "user/model", "permission": {"bash": "deny"}},
        },
        "permission": {"external_directory": "deny"},
        "mcp": {"user-mcp": {"type": "remote", "url": "https://example.invalid"}},
    }
    first = run_config(custom, home)
    kimi = first["agent"]["kimi-coder"]
    assert kimi["model"] == "ollama-cloud/kimi-k2.7-code"
    assert kimi["permission"] == "allow"
    assert kimi["description"] == "user description"
    assert kimi["temperature"] == 0.7
    assert kimi["prompt"] == "user prompt"
    assert kimi["custom"] == {"keep": True}
    assert first["agent"]["user-agent"] == {
        "model": "user/model", "permission": {"bash": "deny"}
    }
    assert first["permission"] == {"external_directory": "deny"}
    for name in (
        "deepseekv4-coder", "kimi-coder", "kimi-k3-coder", "glm-coder",
        "qwen-coder", "minimax-coder",
    ):
        assert first["agent"][name]["permission"] == "allow", name
    assert "v4-coder" not in first["agent"]
    assert first["agent"]["deepseekv4-coder"]["temperature"] == 0.4
    assert first["agent"]["deepseekv4-coder"]["custom"] == {"migrate": True}
    assert first["mcp"]["user-mcp"]["url"] == "https://example.invalid"
    # Fleet MCP servers are registered by install-fleet.sh, never by the graduated core:
    # a standalone client must not get an open-design entry that re-execs over SSH.
    assert "open-design" not in first["mcp"], first["mcp"].keys()
    assert "hermes-orchestrator" not in first["mcp"], first["mcp"].keys()
    assert first["mcp"]["browser"]["type"] == "local"
    assert first["provider"]["ollama-cloud"]["models"]["custom-model"] == {"name": "Keep"}
    k3 = first["provider"]["ollama-cloud"]["models"]["kimi-k3"]
    assert k3["name"] == "Kimi K3"
    assert k3["limit"] == {"context": 1_000_000, "output": 32768}

    second = run_config(first, home)
    assert second == first, "OpenCode config is not semantically idempotent"

with tempfile.TemporaryDirectory() as tmp:
    old = {
        "agent": {"kimi-coder": {
            "description": "Heavy coding agent on Kimi K2.7 Code (256k ctx) — implementation, refactors, tests."
        }}
    }
    migrated = run_config(old, pathlib.Path(tmp))
    assert migrated["agent"]["kimi-coder"]["description"].startswith(
        "Heavy coding agent on Kimi K2.7 Code (256k ctx)"
    )
    assert migrated["agent"]["kimi-k3-coder"]["model"] == "ollama-cloud/kimi-k3"
    assert migrated["agent"]["deepseekv4-coder"]["model"] == "ollama-cloud/deepseek-v4-flash:0731"
    assert "v4-coder" not in migrated["agent"]
    assert migrated["agent"]["qwen-coder"]["model"] == "ollama-cloud/qwen3.5:397b"

with tempfile.TemporaryDirectory() as tmp:
    stale_catalog = {
        "provider": {"ollama-cloud": {"models": {"kimi-k3": {
            "name": "Kimi K3",
            "limit": {"context": 256_000, "output": 32768},
            "custom": "preserve",
        }}}}
    }
    migrated = run_config(stale_catalog, pathlib.Path(tmp))
    k3 = migrated["provider"]["ollama-cloud"]["models"]["kimi-k3"]
    assert k3["limit"] == {"context": 1_000_000, "output": 32768}
    assert k3["custom"] == "preserve"

agent = (root / "opencode" / "agents" / "kimiplan.md").read_text(encoding="utf-8")
command = (root / "opencode" / "commands" / "kimiplan.md").read_text(encoding="utf-8")
compat_agent = (root / "opencode" / "agents" / "oplanner.md").read_text(encoding="utf-8")
compat_command = (root / "opencode" / "commands" / "oplan.md").read_text(encoding="utf-8")
research_agent = (root / "opencode" / "agents" / "deep-researcher.md").read_text(encoding="utf-8")
assert "model: ollama-cloud/kimi-k3" in agent
assert "edit: deny" in agent and "bash: deny" in agent and "steps: 20" in agent
assert "agent: kimiplan" in command and "subtask: true" in command
assert "model: ollama-cloud/kimi-k3" in compat_agent
assert "agent: kimiplan" in compat_command
assert "edit: deny" in research_agent and "bash: deny" in research_agent
assert 'for sub in agents commands plugins' in install
PY

printf 'opencode-install: K3 config preservation + kimiplan deployment contract passed\n'
