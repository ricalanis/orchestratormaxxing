#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

python3 - "$ROOT" "$TMP/opencode.json" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
config_path = pathlib.Path(sys.argv[2])
config_path.write_text(json.dumps({
    "agent": {
        "user-coder": {
            "model": "user/model",
            "prompt": "preserve me",
            "permission": {"bash": "deny"},
        }
    }
}))

install = (root / "install.sh").read_text()
anchor = 'python3 - "$OPENCODE_CFG_DIR/opencode.json" "$REPO_DIR" <<\'PY\'\n'
embedded = install.split(anchor, 1)[1].split("\nPY\n", 1)[0]

old_argv = sys.argv
sys.argv = ["install-opencode-agents", str(config_path), str(root)]
try:
    exec(compile(embedded, "install.sh:opencode-agents", "exec"), {})
finally:
    sys.argv = old_argv

cfg = json.loads(config_path.read_text())
models = {name: spec["model"] for name, spec in cfg["agent"].items()}
required = {
    "kimi-coder": "ollama-cloud/kimi-k2.7-code",
    "kimi-k3-coder": "ollama-cloud/kimi-k3",
    "glm-coder": "ollama-cloud/glm-5.3",
    "minimax-coder": "ollama-cloud/minimax-m3",
}
for name, model in required.items():
    assert models.get(name) == model, f"{name} missing or misrouted: {models.get(name)!r}"

for name in (
    "deepseekv4-coder", "kimi-coder", "kimi-k3-coder", "glm-coder",
    "qwen-coder", "minimax-coder",
):
    assert cfg["agent"][name]["permission"] == "allow", name

assert models["user-coder"] == "user/model"
assert cfg["agent"]["user-coder"]["prompt"] == "preserve me"
assert cfg["agent"]["user-coder"]["permission"] == {"bash": "deny"}
PY

printf 'install OpenCode agents contract: PASS\n'
