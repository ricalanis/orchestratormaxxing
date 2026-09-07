#!/usr/bin/env python3
"""Root-authored Warp serialization regression contract; offline and disposable."""
import importlib.machinery
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
TOOL = Path(os.environ.get("WARP_PARSER_TOOL", ROOT / "bin/agent-guard"))
guard = importlib.machinery.SourceFileLoader("guard_contract", str(TOOL)).load_module()

samples = [
    "'plain', # comment",
    "'''has ' quote and \\s''',",
    "'''trailing quote'''',",
    "'''two quotes''''',",
    '"escaped\\tvalue",',
    '"""has \\"quote\\" and \\\\s""",',
    '"""trailing quote"""",',
    '"""two quotes""""",',
    "'''contains # and , punctuation''', # tail",
]
for item in samples:
    expected = tomllib.loads("a = [\n" + item + "\n]")["a"][0]
    assert guard.toml_array_element(item) == expected, ("decode", item)
for item in ["'''unfinished", "\"unfinished", "'ok' garbage", "'''ok''' garbage"]:
    try:
        guard.toml_array_element(item)
    except ValueError:
        pass
    else:
        raise AssertionError(("malformed accepted", item))
print("PASS decoding matches independent TOML parser; malformed entries refused")

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    data = home / "guard"
    data.mkdir()
    # A synthetic rule keeps the test independent of the production denylist.
    owned = "owned'pattern"
    (data / "dangerous-patterns.txt").write_text(owned + "\nsecond-pattern\n")
    (data / "warp-default-patterns.json").write_bytes((ROOT / "deploy/agent-guard/warp-default-patterns.json").read_bytes())
    settings = home / "settings.toml"
    before = ("# retain header\n[agents.execution_profiles.default]\n"
              "command_denylist = [\n"
              "  '''owned'pattern''', # first\n"
              "  \"owned'pattern\",\n"
              "  '''owned'pattern''',\n"
              "  'user-entry', # keep duplicate user rules\n"
              "  'user-entry',\n]\n"
              "unrelated = {\n  value = true,\n}\n")
    settings.write_text(before)
    env = dict(os.environ, HOME=str(home), ORCHESTRATORMAXXING_GUARD_DIR=str(data))
    def run(mode):
        return subprocess.run([str(TOOL), "warp", "--settings", str(settings), mode],
                              env=env, capture_output=True, text=True, timeout=10)
    assert run("--check").returncode == 1, "missing pattern must report drift"
    result = run("--apply")
    assert result.returncode == 0, result.stderr
    after = settings.read_text()
    assert after.count("owned'pattern") == 1, "only owned duplicates removed"
    assert after.count("'user-entry'") == 2, "user duplicates preserved"
    assert "'''owned'pattern''', # first" in after, "first representation preserved"
    stripped = after.replace('  "second-pattern",\n', '')
    import json
    for value in json.loads((data / 'warp-default-patterns.json').read_text()):
        stripped = stripped.replace('  ' + guard.toml_basic_encode(value) + ',\n', '')
    stripped = stripped.replace('\n[agents.warp_agent.other]\nauto_approve_bypasses_command_denylist = false\n', '')
    expected = before.replace('  "owned\'pattern",\n', '').replace("  '''owned'pattern''',\n", '')
    assert stripped == expected, "all unrelated bytes preserved"
    assert run("--check").returncode == 0, "converged settings must pass"
    assert run("--apply").returncode == 0
    assert settings.read_text() == after, "second apply must be byte-idempotent"
    settings.write_text(before.replace("  'user-entry',\n", "  '''unterminated,\n"))
    malformed = settings.read_bytes()
    assert run("--apply").returncode == 2, "malformed file must refuse"
    assert settings.read_bytes() == malformed, "refusal must not write"
    settings.write_text(before.replace("'user-entry', # keep", "'user-entry' # keep"))
    malformed = settings.read_bytes()
    assert run("--apply").returncode == 2, "missing comma between entries must refuse"
    assert settings.read_bytes() == malformed
print("PASS real CLI deduplication, byte preservation, idempotence, refusal")
