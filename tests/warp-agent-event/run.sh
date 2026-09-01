#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/warp-agent-event"

fail() { printf 'warp-agent-event contract: %s\n' "$*" >&2; exit 1; }
[[ -f "$TOOL" ]] || fail 'helper missing'

python3 - "$TOOL" <<'PY'
import json
import runpy
import sys

ns = runpy.run_path(sys.argv[1])
payload = ns["build_payload"]("claude", "stop")
assert payload == b'{"v":1,"agent":"claude","event":"stop"}'

request = ns["build_payload"]("codex", "permission_request")
assert json.loads(request) == {
    "v": 1,
    "agent": "codex",
    "event": "permission_request",
    "summary": "Permission required.",
}
question = json.loads(ns["build_payload"]("claude", "question_asked"))
assert question["summary"] == "Waiting for your answer."

direct = ns["osc777_frame"](payload)
expected = b'\x1b]777;notify;warp://cli-agent;{"v":1,"agent":"claude","event":"stop"}\x07'
assert direct == expected
wrapped = ns["tmux_frame"](direct)
assert wrapped == b"\x1bPtmux;" + expected.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"

for agent in ("claude", "codex"):
    for event in (
        "session_start", "prompt_submit", "stop", "permission_request",
        "permission_replied", "question_asked",
    ):
        decoded = json.loads(ns["build_payload"](agent, event))
        assert set(decoded) <= {"v", "agent", "event", "summary"}
        serialized = json.dumps(decoded)
        for forbidden_key in ("transcript_path", "response", "prompt", "tool_input"):
            assert forbidden_key not in decoded, (agent, event, forbidden_key)
        for forbidden_value in ("password", "secret", "token", "api_key"):
            assert forbidden_value not in serialized.lower(), (agent, event, forbidden_value)
PY

# Production invocation is a silent, successful no-op when it is not running
# inside a Warp-owned c/g tmux pane.
out="$(env -u TMUX -u TMUX_PANE -u WARP_IS_LOCAL_SHELL_SESSION \
  -u WARP_CLI_AGENT_PROTOCOL_VERSION "$TOOL" claude stop 2>&1)" || \
  fail 'non-Warp invocation failed'
[[ -z "$out" ]] || fail 'non-Warp invocation emitted output'

printf 'warp-agent-event contract: PASS\n'
