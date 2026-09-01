#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import pathlib
import runpy
import sys

root = pathlib.Path(sys.argv[1])
routing = (root / "knowledge/provider-routing.md").read_text()
claude = (root / ".claude/agents/ollama-worker.md").read_text()
codex = (root / ".codex/agents/ollama-worker.toml").read_text()
oll = runpy.run_path(root / "bin/oll")
playbook = (root / "knowledge/delegation-playbook.md").read_text()
claude_lane = (root / ".claude/commands/cheap-delegate.md").read_text()
codex_lane = (root / "plugins/orchestratormaxxing/skills/cheap-delegate/SKILL.md").read_text()
oplanner = (root / "opencode/agents/oplanner.md").read_text()

for text, label in ((routing, "provider routing"), (claude, "Claude worker"), (codex, "Codex worker")):
    assert "kimi-k3" in text, f"{label} does not admit kimi-k3"
    assert "kimi-k2.7-code" in text, f"{label} retired kimi-k2.7-code"

# Assert the ROUTING RELATIONSHIP, not one branch's sentence. Both machines
# documented "long-horizon work goes to K3" in different words, and the verbatim
# form silently went red on a rewrite that changed nothing about the routing.
import re
_pair = lambda t, m: re.search(rf"long-horizon[^\n]*{m}|{m}[^\n]*long-horizon", t, re.I)
assert _pair(claude, "kimi-k3"), "Claude worker does not route long-horizon work to kimi-k3"
assert _pair(codex, "kimi-k3"), "Codex worker does not route long-horizon work to kimi-k3"
assert _pair(routing, "kimi-k3"), "provider routing does not route long-horizon work to kimi-k3"
# The host lanes deliberately stopped naming agents: they select a PROFILE and
# bin/oll resolves the agent/model, so "K3 is not the default" stays true in one
# place. Assert the indirection end-to-end instead of the agent name on surfaces
# that no longer carry one — naming the agent on a lane is now the regression.
for text, label in ((claude_lane, "Claude lane"), (codex_lane, "Codex lane")):
    assert "--profile long-horizon" in text, f"{label} does not expose the long-horizon profile"
assert "kimi-k3-coder" in playbook, "playbook does not expose kimi-k3-coder"
# oplanner is now a compatibility ALIAS for the kimiplan planner, not a coder —
# planning and long-horizon coding are separate profiles on the same model, so
# assert the alias resolves rather than that a planner names a coder agent.
assert "kimi-k3" in oplanner, "OpenCode planner no longer runs on kimi-k3"
assert oll["STATEFUL_AGENT_ALIASES"].get("oplanner") == "kimiplan", \
    "oplanner no longer aliases to the kimiplan planner"
assert oll["STATEFUL_ROUTES"]["planning"] == {"agent": "kimiplan", "model": "kimi-k3"}, \
    "the planning profile no longer resolves to kimiplan/kimi-k3"
assert oll["STATEFUL_ROUTES"]["long-horizon"] == {"agent": "kimi-k3-coder", "model": "kimi-k3"}, \
    "the long-horizon profile no longer resolves to kimi-k3-coder/kimi-k3"
assert _pair(playbook, "kimi-k3-coder"), "playbook does not bind long-horizon chains to kimi-k3-coder"
assert oll["NORMAL_WORKER_MODELS"] == (
    "deepseek-v4-flash:0731",
    "glm-5.3",
    "kimi-k3",
    "kimi-k2.7-code",
    "qwen3.5:397b",
)
PY

printf 'kimi-k3 routing contract: PASS\n'
