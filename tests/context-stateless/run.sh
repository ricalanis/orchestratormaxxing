#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# `harness-verify` supplies this path. Markers are evidence about where observation
# stopped, not a causal claim; direct standalone runs remain silent and unchanged.
trace() {
  [ -n "${HARNESS_TRACE_FILE:-}" ] || return 0
  printf '%s %s\n' "$1" "$2" >> "$HARNESS_TRACE_FILE"
}
stage_begin() { trace BEGIN "$1"; }
stage_end() { trace END "$1"; }

mkdir -p "$tmp/home/.local/share/opencode"
printf '%s\n' '{"ollama-cloud":{"key":"must-not-be-read"}}' > "$tmp/home/.local/share/opencode/auth.json"

stage_begin policy-module
python3 - "$root/bin/oll" <<'PY'
import runpy, sys
mod = runpy.run_path(sys.argv[1])
assert mod["estimate_input_tokens"]("abcdef", 3) == 2
assert mod["estimate_input_tokens"]("abcdefg", 3) == 3
assert mod["input_limit"]() == 50000
assert mod["DEFAULT_MODEL"] == "deepseek-v4-flash:0731"
assert mod["REASONING_MODEL"] == "glm-5.3"
assert mod["select_model"]() == "deepseek-v4-flash:0731"
assert mod["select_model"](reasoning=True) == "glm-5.3"
assert mod["NORMAL_WORKER_MODELS"] == (
    "deepseek-v4-flash:0731", "glm-5.3", "kimi-k3", "kimi-k2.7-code", "qwen3.5:397b")
assert mod["STATEFUL_ROUTES"] == {
    "volume": {"agent": "deepseekv4-coder", "model": "deepseek-v4-flash:0731"},
    "reasoning": {"agent": "glm-coder", "model": "glm-5.3"},
    "bounded-code": {"agent": "kimi-coder", "model": "kimi-k2.7-code"},
    "long-horizon": {"agent": "kimi-k3-coder", "model": "kimi-k3"},
    "general": {"agent": "qwen-coder", "model": "qwen3.5:397b"},
    "long-context": {"agent": "minimax-coder", "model": "minimax-m3"},
    "planning": {"agent": "kimiplan", "model": "kimi-k3"},
}
assert mod["resolve_stateful_route"]("bounded-code") == {
    "profile": "bounded-code", "agent": "kimi-coder", "model": "kimi-k2.7-code"
}
assert mod["resolve_stateful_agent"]("deepseekv4-coder") == {
    "profile": "volume", "agent": "deepseekv4-coder", "model": "deepseek-v4-flash:0731"
}
assert mod["resolve_stateful_agent"]("oplanner") == {
    "profile": "planning", "agent": "kimiplan", "model": "kimi-k3"
}
try:
    mod["resolve_stateful_agent"]("user-agent")
except KeyError:
    pass
else:
    raise AssertionError("custom agent resolved as harness-owned")
assert mod["LEGACY_MODEL_REPLACEMENTS"] == {
    "glm-4.7": "glm-5.3",
    "glm-5": "glm-5.3",
    "glm-5.1": "glm-5.3",
    "glm-5.2": "glm-5.3",
    "kimi-k2.5": "kimi-k2.7-code",
    "kimi-k2.6": "kimi-k2.7-code",
    "kimi-k2-thinking": "kimi-k2.7-code",
    "qwen3-coder-next": "qwen3.5:397b",
    "qwen3-coder:480b": "qwen3.5:397b",
    "qwen3-vl:235b": "qwen3.5:397b",
}
PY
stage_end policy-module

stage_begin stateful-route-cli
route_json="$(HOME="$tmp/noauth" "$root/bin/oll" --route-profile long-horizon --json)"
[ "$route_json" = '{"agent":"kimi-k3-coder","model":"kimi-k3","profile":"long-horizon"}' ]
python3 - "$route_json" <<'PY'
import json, sys
assert json.loads(sys.argv[1]) == {
    "profile": "long-horizon", "agent": "kimi-k3-coder", "model": "kimi-k3"
}
PY
agent_json="$(HOME="$tmp/noauth" "$root/bin/oll" --route-agent oplanner --json)"
[ "$agent_json" = '{"agent":"kimiplan","model":"kimi-k3","profile":"planning"}' ]
python3 - "$agent_json" <<'PY'
import json, sys
assert json.loads(sys.argv[1]) == {
    "profile": "planning", "agent": "kimiplan", "model": "kimi-k3"
}
PY
[ "$(HOME="$tmp/noauth" "$root/bin/oll" --route-agent deepseekv4-coder)" = $'volume\tdeepseekv4-coder\tdeepseek-v4-flash:0731' ]
set +e
HOME="$tmp/noauth" "$root/bin/oll" --route-profile unknown >"$tmp/route-out" 2>"$tmp/route-err"
route_rc=$?
HOME="$tmp/noauth" "$root/bin/oll" --route-agent user-agent >"$tmp/agent-out" 2>"$tmp/agent-err"
agent_rc=$?
HOME="$tmp/noauth" "$root/bin/oll" prompt --route-profile volume >"$tmp/combo-out" 2>"$tmp/combo-err"
combo_rc=$?
HOME="$tmp/noauth" "$root/bin/oll" prompt --json >"$tmp/json-out" 2>"$tmp/json-err"
json_rc=$?
HOME="$tmp/noauth" "$root/bin/oll" --check-model kimi-k2.6 --json >"$tmp/check-old-out" 2>"$tmp/check-old-err"
check_old_rc=$?
set -e
[ "$route_rc" -eq 2 ]
grep -q 'unknown' "$tmp/route-err"
[ "$agent_rc" -eq 2 ]
grep -q 'not harness-owned' "$tmp/agent-err"
[ "$combo_rc" -eq 2 ]
grep -q 'cannot be combined' "$tmp/combo-err"
[ "$json_rc" -eq 2 ]
grep -q -- '--json is only valid' "$tmp/json-err"
[ "$check_old_rc" -eq 2 ]
grep -q "use 'kimi-k2.7-code'" "$tmp/check-old-err"
[ ! -s "$tmp/check-old-out" ]
allowed_json="$(HOME="$tmp/noauth" "$root/bin/oll" --check-model kimi-k3 --json)"
python3 - "$allowed_json" <<'PY'
import json, sys
assert json.loads(sys.argv[1]) == {"model": "kimi-k3", "status": "allowed"}
PY
stage_end stateful-route-cli

stage_begin cli-model-selection
# Scratch HOME: main() resolves ~/.local/share/opencode/auth.json and the usage-ledger dir
# from HOME, so this stage must never see the live home.
HOME="$tmp/home" python3 - "$root/bin/oll" <<'PY'
import contextlib, io, json, runpy, sys
mod = runpy.run_path(sys.argv[1])

class Response:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()

seen = []
def fake_urlopen(req, **kwargs):
    seen.append(json.loads(req.data)["model"])
    return Response()

# runpy.run_path returns a COPY of the module globals: patching `mod` never reaches
# main()'s namespace, so patch the namespace main() actually resolves load_key from.
mod["main"].__globals__["load_key"] = lambda: "fixture-key"
mod["urllib"].request.urlopen = fake_urlopen
for argv in (["oll", "hello"], ["oll", "hello", "--reasoning"]):
    sys.argv = argv
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert mod["main"]() == 0
assert seen == ["deepseek-v4-flash:0731", "glm-5.3"], seen
PY
stage_end cli-model-selection

stage_begin catalog-k3-policy
python3 - "$root/bin/oll-sync" <<'PY'
import json, runpy, sys
mod = runpy.run_path(sys.argv[1])
assert mod["EXCLUDE"] == set()
assert mod["ctx_for"]("kimi-k3") == (1_000_000, 32768)
assert mod["ctx_for"]("library/kimi-k3:latest") == (1_000_000, 32768)

payload = {"data": [
    {"id": "glm-5.3"},
    {"id": "kimi-k3"},
    {"id": "library/kimi-k3:latest"},
]}
class Response:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(payload).encode()
def fake_urlopen(*args, **kwargs): return Response()
mod["urllib"].request.urlopen = fake_urlopen

assert mod["live_models"]("fixture-key") == ["glm-5.3", "kimi-k3", "library/kimi-k3:latest"]
# Known-bad old policy: the same plain + namespaced K3 rows disappear. This is
# the negative fixture proving the acceptance check discriminates the bug.
mod["EXCLUDE"].add("kimi-k3")
assert mod["live_models"]("fixture-key") == ["glm-5.3"]
PY
stage_end catalog-k3-policy

# Legacy normal-worker models fail before auth/network and name the replacement.
stage_begin legacy-model-gate
while read -r legacy replacement; do
  set +e
  HOME="$tmp/home" "$root/bin/oll" short --model "$legacy" >"$tmp/legacy-out" 2>"$tmp/legacy-err"
  legacy_rc=$?
  set -e
  [ "$legacy_rc" -eq 2 ]
  grep -q "$replacement" "$tmp/legacy-err"
  grep -q -- '--allow-legacy-model' "$tmp/legacy-err"
  ! grep -q 'must-not-be-read' "$tmp/legacy-err"
done <<'EOF'
glm-4.7 glm-5.3
glm-5 glm-5.3
glm-5.1 glm-5.3
glm-5.2 glm-5.3
kimi-k2.5 kimi-k2.7-code
kimi-k2.6 kimi-k2.7-code
kimi-k2-thinking kimi-k2.7-code
qwen3-coder-next qwen3.5:397b
qwen3-coder:480b qwen3.5:397b
qwen3-vl:235b qwen3.5:397b
EOF
stage_end legacy-model-gate

# The override and arbitrary specialty/future IDs pass the policy gate, then fail
# at the deliberately missing auth fixture without reaching a network call.
stage_begin missing-auth-offline
mkdir -p "$tmp/noauth"
for args in '--model glm-5.1 --allow-legacy-model' '--model future-frontier-model'; do
  set +e
  HOME="$tmp/noauth" "$root/bin/oll" short $args >"$tmp/pass-out" 2>"$tmp/pass-err"
  pass_rc=$?
  set -e
  [ "$pass_rc" -ne 2 ]
  grep -q 'ERROR reading' "$tmp/pass-err"
done
stage_end missing-auth-offline

stage_begin companion-defaults
python3 - "$root/bin/oll-council" "$root/bin/warp-ollama" <<'PY'
import runpy, sys
council = runpy.run_path(sys.argv[1])
warp = runpy.run_path(sys.argv[2])
assert council["DEFAULT_MODELS"][:2] == ["deepseek-v4-flash:0731", "glm-5.3"]
assert "glm-5.1" not in council["DEFAULT_MODELS"]
assert "kimi-k3" in council["DEFAULT_MODELS"]
assert warp["RECOMMENDED"] == ["deepseek-v4-flash:0731", "glm-5.3", "kimi-k3", "kimi-k2.7-code", "qwen3.5:397b"]
PY
stage_end companion-defaults

stage_begin council-legacy-gate
set +e
HOME="$tmp/home" "$root/bin/oll-council" short --models kimi-k2.5 >"$tmp/council-legacy" 2>"$tmp/council-legacy-err"
council_legacy_rc=$?
set -e
[ "$council_legacy_rc" -eq 2 ]
grep -q 'kimi-k2.7-code' "$tmp/council-legacy-err"
stage_end council-legacy-gate

stage_begin input-limit-gate
set +e
HOME="$tmp/home" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 \
  "$root/bin/oll" abcdefghi --model fake >"$tmp/out" 2>"$tmp/err"
rc=$?
set -e
[ "$rc" -eq 2 ]
grep -q 'estimated input' "$tmp/err"
! grep -q 'must-not-be-read' "$tmp/err"
stage_end input-limit-gate

stage_begin oversize-override
set +e
HOME="$tmp/noauth" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 \
  "$root/bin/oll" abcdefghi --model fake --allow-oversize >"$tmp/out2" 2>"$tmp/err2"
override_rc=$?
set -e
[ "$override_rc" -ne 2 ]
grep -q 'oversize override' "$tmp/err2"
stage_end oversize-override

stage_begin council-provider-gates
set +e
HOME="$tmp/home" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 \
  "$root/bin/oll-council" abcdefghi --models fake >"$tmp/council" 2>"$tmp/council-err"
council_rc=$?
HOME="$tmp/noauth" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 \
  "$root/bin/oll-council" abcdefghi --models fake --allow-oversize >"$tmp/council2" 2>"$tmp/council-err2"
council_override_rc=$?
HOME="$tmp/home" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 \
  "$root/bin/provider-ask" ollama abcdefghi >"$tmp/provider" 2>"$tmp/provider-err"
provider_rc=$?
HOME="$tmp/noauth" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 OLL_ALLOW_OVERSIZE=1 \
  "$root/bin/provider-ask" ollama abcdefghi >"$tmp/provider2" 2>"$tmp/provider-err2"
provider_override_rc=$?
set -e

[ "$council_rc" -eq 2 ]
[ "$council_override_rc" -ne 2 ]
[ "$provider_rc" -eq 2 ]
[ "$provider_override_rc" -ne 2 ]
grep -q 'estimated input' "$tmp/council-err"
grep -q 'estimated input' "$tmp/provider-err"
grep -q 'oversize override' "$tmp/council-err2"
grep -q 'oversize override' "$tmp/provider-err2"
stage_end council-provider-gates

# provider-ask must degrade gracefully (reach oll's gate, never 127) even when
# no timeout binary is reachable on PATH — macOS has no /usr/bin/timeout, and
# launchd/sandboxed environments don't see /opt/homebrew/bin.
stage_begin minimal-path-provider
set +e
HOME="$tmp/home" PATH="/usr/bin:/bin" OLL_MAX_ESTIMATED_INPUT_TOKENS=2 \
  "$root/bin/provider-ask" ollama abcdefghi >"$tmp/notimeout" 2>"$tmp/notimeout-err"
notimeout_rc=$?
set -e
[ "$notimeout_rc" -eq 2 ]
grep -q 'estimated input' "$tmp/notimeout-err"
stage_end minimal-path-provider

# Suspended providers must not remain callable or in fan-out defaults.
stage_begin suspended-provider
set +e
HOME="$tmp/home" "$root/bin/provider-ask" gemini test >"$tmp/gemini" 2>"$tmp/gemini-err"
gemini_rc=$?
set -e
[ "$gemini_rc" -eq 1 ]
grep -q "unknown provider 'gemini'" "$tmp/gemini-err"
! grep -Eq '^PROVIDERS=.*gemini' "$root/bin/multi-council"
! grep -Eq '^PROVIDERS=.*gemini' "$root/bin/cross-review"
stage_end suspended-provider
