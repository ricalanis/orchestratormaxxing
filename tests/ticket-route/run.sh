#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
python3 - "$repo" <<'PY'
import json, pathlib, subprocess, sys
repo = pathlib.Path(sys.argv[1])
cases = repo / "tests/ticket-route/cases.jsonl"
for line in cases.read_text().splitlines():
    case = json.loads(line)
    first = subprocess.run(
        [str(repo / "bin/ticket-route")],
        input=json.dumps(case["ticket"]), text=True, capture_output=True,
    )
    assert first.returncode == 0, (case["name"], first.stderr)
    actual = json.loads(first.stdout)
    for key, expected in case["expected"].items():
        assert actual.get(key) == expected, (case["name"], key, actual.get(key), expected)
    second = subprocess.run(
        [str(repo / "bin/ticket-route")],
        input=json.dumps(case["ticket"]), text=True, capture_output=True,
    )
    assert second.returncode == 0
    assert second.stdout == first.stdout, (case["name"], "non-idempotent output")
    base = {"valid", "execution", "reviews", "max_review_rounds", "reasons"}
    # gate_cleared appears IFF the ticket supplied a validated diff (never guessed)
    if case["ticket"].get("diff") is not None and actual["valid"]:
        assert set(actual) == base | {"gate_cleared"}, (case["name"], set(actual))
    else:
        assert set(actual) == base, (case["name"], set(actual))

bad = subprocess.run([str(repo / "bin/ticket-route")], input="not json", text=True, capture_output=True)
assert bad.returncode == 2
assert bad.stdout == ""

# the gate's exclusion list must never rot below the load-bearing floor set —
# a tiering rule that silently drops install.sh/doctrine paths removes a review
# round exactly where the unattended loop already stops at SELECT
import re
src = (repo / "bin/ticket-route").read_text()
m = re.search(r"GATE_EXCLUDED_PATHS = \((.*?)\n\)", src, re.S)
assert m, "GATE_EXCLUDED_PATHS missing from bin/ticket-route"
listed = set(re.findall(r'"([^"]+)"', m.group(1)))
floor = {"install.sh", "deploy/", "CLAUDE.md", ".claude/commands/"}
assert floor <= listed, f"gate exclusion floor set rotted: missing {floor - listed}"
PY
