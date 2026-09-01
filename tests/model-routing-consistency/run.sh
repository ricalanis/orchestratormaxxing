#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import pathlib
import runpy
import sys

root = pathlib.Path(sys.argv[1])
read = lambda rel: (root / rel).read_text(encoding="utf-8")

# One host-native planner per host. /oplan is compatibility, not a second
# OpenCode planner on a different model.
fable = read(".claude/agents/fable-planner.md")
sol = read(".codex/agents/sol-planner.toml")
kimi = read("opencode/agents/kimiplan.md")
oplan_agent = read("opencode/agents/oplanner.md")
oplan_command = read("opencode/commands/oplan.md")
assert "model: fable" in fable
assert 'model = "gpt-5.6-sol"' in sol and 'sandbox_mode = "read-only"' in sol
for label, text in (("kimiplan", kimi), ("oplan compatibility agent", oplan_agent)):
    assert "model: ollama-cloud/kimi-k3" in text, f"{label} is not K3"
    assert "edit: deny" in text and "bash: deny" in text, f"{label} is not read-only"
assert "agent: kimiplan" in oplan_command, "/oplan is not an alias to kimiplan"

# Model choice is orthogonal to topology: V4 Pro handles volume, GLM explicit
# reasoning, K2.7 bounded code, and K3 long-horizon/planning.
oll = runpy.run_path(root / "bin/oll")
assert oll["DEFAULT_MODEL"] == "deepseek-v4-flash:0731"
assert oll["NORMAL_WORKER_MODELS"] == (
    "deepseek-v4-flash:0731",
    "glm-5.3",
    "kimi-k3",
    "kimi-k2.7-code",
    "qwen3.5:397b",
)
legacy = oll["LEGACY_MODEL_REPLACEMENTS"]
assert "kimi-k2.7-code" not in legacy
for old in ("kimi-k2.5", "kimi-k2.6", "kimi-k2-thinking"):
    assert legacy[old] == "kimi-k2.7-code"

install = read("install.sh")
assert '"kimi-coder": {"model": "ollama-cloud/kimi-k2.7-code"' in install
assert '"kimi-k3-coder": {"model": "ollama-cloud/kimi-k3"' in install
assert '"deepseekv4-coder": {"model": "ollama-cloud/deepseek-v4-flash:0731"' in install
assert '"qwen-coder": {"model": "ollama-cloud/qwen3.5:397b"' in install

expected_routes = {
    "volume": ("deepseekv4-coder", "deepseek-v4-flash:0731"),
    "reasoning": ("glm-coder", "glm-5.3"),
    "bounded-code": ("kimi-coder", "kimi-k2.7-code"),
    "long-horizon": ("kimi-k3-coder", "kimi-k3"),
    "general": ("qwen-coder", "qwen3.5:397b"),
}
assert {
    name: (row["agent"], row["model"])
    for name, row in oll["STATEFUL_ROUTES"].items()
    if name in expected_routes
} == expected_routes

cheap = read("knowledge/delegation-playbook.md") + read(".claude/commands/cheap-delegate.md") + read("plugins/orchestratormaxxing/skills/cheap-delegate/SKILL.md")
fanout = read(".claude/commands/fanout.md") + read("plugins/orchestratormaxxing/skills/fanout/SKILL.md")
assert cheap.lower().count("one bounded execution task") >= 3
assert cheap.lower().count("http 500") >= 3
assert cheap.lower().count("infra") >= 3
assert fanout.lower().count("at least two") >= 2
assert fanout.lower().count("after planning") >= 2

# Every harness that may ask Ollama to write workspace code must name the
# stateful o boundary; ollama-worker remains response-only by construction.
stateful_surfaces = [
    ".claude/commands/cheap-delegate.md",
    ".claude/commands/fanout.md",
    ".claude/commands/gauntlet.md",
    ".claude/commands/self-improve.md",
    "plugins/orchestratormaxxing/skills/cheap-delegate/SKILL.md",
    "plugins/orchestratormaxxing/skills/fanout/SKILL.md",
    "plugins/orchestratormaxxing/skills/gauntlet/SKILL.md",
    "plugins/orchestratormaxxing/skills/self-improve/SKILL.md",
]
for rel in stateful_surfaces:
    body = read(rel)
    assert "o delegate" in body and "--profile" in body, f"{rel} bypasses stateful routing"
    assert "o close" in body, f"{rel} does not close stateful workers"
for rel in (".claude/agents/ollama-worker.md", ".codex/agents/ollama-worker.toml"):
    body = read(rel).lower()
    assert "response-only" in body, f"{rel} can impersonate a workspace writer"
    assert "never edit" in body or "never write" in body, f"{rel} lacks a write boundary"

active = "\n".join([
    read("README.md"), read("CLAUDE.md"), read("AGENTS.md"),
    read("knowledge/provider-routing.md"), read("knowledge/delegation-playbook.md"),
])
assert "K3 is included-first" in active
assert "higher-consumption" in active
assert "K3 is free" not in active and "K3 is unmetered" not in active
PY

printf 'model-routing-consistency: PASS\n'
