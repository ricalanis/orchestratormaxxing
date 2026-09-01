#!/usr/bin/env bash
# Contract: bin/oll can bound a thinking model's reasoning budget, and doing so
# is BOTH opt-in and validated before anything sensitive is touched.
#
# Why this exists (measured 2026-08-26, knowledge/glm-5.3-flash-evaluation-2026-08-26.md
# section 3e): Ollama Cloud honours OpenAI's `reasoning_effort`, and on an identical
# code-review task at an identical 4000-token cap glm-5.2 went 0/3 (whole budget
# spent thinking, empty answer) at default effort but PASSED at effort=low using
# 936 tokens and at effort=none using 187 -- a 21x reduction. `"think": false` is
# silently IGNORED by the provider, so `reasoning_effort` is the only real control.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OLL="$ROOT/bin/oll"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "oll-reasoning-effort contract: FAIL — $*" >&2; exit 1; }

# --------------------------------------------------------------- unit surface
python3 - "$OLL" <<'PY' || exit 1
import runpy, sys
mod = runpy.run_path(sys.argv[1])

for name in ("build_payload", "normalize_effort", "REASONING_EFFORTS"):
    assert name in mod, f"C0 FAIL: bin/oll exposes no {name}() -- effort is not plumbed"

build = mod["build_payload"]
norm = mod["normalize_effort"]
EFFORTS = mod["REASONING_EFFORTS"]

base = dict(model="glm-5.2", system="sys", user="usr", max_tokens=100, temperature=0.2)

# C1 -- default must be BYTE-IDENTICAL to the pre-change payload: the key is
# ABSENT, never present-as-null. A null would change provider behaviour for
# every existing caller, which is exactly what an opt-in must not do.
p = build(effort=None, **base)
assert "reasoning_effort" not in p, f"C1 FAIL: unset effort leaked a key: {p!r}"
assert p == {
    "model": "glm-5.2",
    "messages": [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "usr"}],
    "max_tokens": 100,
    "temperature": 0.2,
    "stream": False,
}, f"C1 FAIL: default payload drifted: {p!r}"

# C2 -- an explicit effort reaches the wire under the provider's own key name.
p = build(effort="none", **base)
assert p.get("reasoning_effort") == "none", f"C2 FAIL: {p!r}"

# C3 -- exactly the provider's accepted set, and nothing wider. Quoted from its
# own 400: 'must be "high", "medium", "low", "max", or "none"'. OpenAI's
# "minimal" is NOT accepted by this provider and must be refused here, not
# discovered mid-flight (measured 2026-08-27).
assert EFFORTS == ("none", "low", "medium", "high", "max"), \
    f"C3 FAIL: effort set drifted from the provider's: {EFFORTS!r}"
try:
    norm("minimal")
except ValueError:
    pass
else:
    raise AssertionError("C3 FAIL: 'minimal' accepted, but the provider returns HTTP 400 for it")
for e in ("none", "low", "medium", "high", "max"):
    assert e in EFFORTS, f"C3 FAIL: {e!r} missing from REASONING_EFFORTS"
    assert build(effort=e, **base).get("reasoning_effort") == e, f"C3 FAIL: {e}"

# C4 -- normalize_effort is the single validator and it is case/space tolerant
# but closed: an unknown value RAISES rather than reaching the provider.
assert norm("  LOW ") == "low", "C4 FAIL: not tolerant of case/whitespace"
assert norm(None) is None and norm("") is None, "C4 FAIL: empty must mean unset"
try:
    norm("bogus")
except ValueError as ex:
    assert "bogus" in str(ex) and "none" in str(ex), f"C4 FAIL: unhelpful error {ex}"
else:
    raise AssertionError("C4 FAIL: an unknown effort was accepted")

# C10 -- the measured per-MODEL default policy (lq-956ab278). The 2026-08-27
# sweep proved the effort dial is a MODEL property: deepseek-v4-flash:0731 is
# INVERTED (low is its most expensive level, none its cheapest at 215t, 2/2
# correct), glm-5.3-flash is cheapest at low and BREAKS at none (0/2 -- it
# stops segregating reasoning from the answer), glm-5.2 is cheapest at none.
# The table must contain EXACTLY the swept models: an unmeasured model with an
# invented level is the same silent-default failure ROLE_USAGE_CEILING exists
# to prevent, and a missing swept model quietly re-globalises the dial.
assert "MODEL_EFFORT_POLICY" in mod, \
    "C10 FAIL: bin/oll has no MODEL_EFFORT_POLICY -- effort is still global-only"
POLICY = mod["MODEL_EFFORT_POLICY"]
assert POLICY == {
    "deepseek-v4-flash:0731": "none",
    "glm-5.3": "low",
    "glm-5.3-flash": "low",
    "glm-5.2": "none",
}, f"C10 FAIL: policy drifted from the 2026-08-27/30 measurements: {POLICY!r}"
for m, level in POLICY.items():
    assert level in EFFORTS, f"C10 FAIL: {m} carries a non-provider level {level!r}"
assert POLICY.get("glm-5.3-flash") != "none", \
    "C10 FAIL: glm-5.3-flash at none emits reasoning as content (0/2 measured)"

# C11 -- ONE definition of precedence: --effort > OLL_REASONING_EFFORT >
# model policy > unset. The token 'default' at either explicit layer forces
# the provider default (key absent), so the unset baseline stays measurable
# on a policy-covered model. Empty means NOT PROVIDED and falls through to
# policy; a provided-but-invalid value raises even when policy could cover
# the model -- an env typo must never silently fall back while the command
# line claims the budget was bounded.
assert "resolve_effort" in mod, "C11 FAIL: no resolve_effort() -- precedence has no single owner"
resolve = mod["resolve_effort"]
DS = "deepseek-v4-flash:0731"
assert resolve("low", "max", DS) == "low", "C11 FAIL: explicit must beat env"
assert resolve(None, "max", DS) == "max", "C11 FAIL: env must beat policy"
assert resolve(None, None, DS) == "none", "C11 FAIL: policy must apply when nothing is provided"
assert resolve(None, None, "kimi-k3") is None, "C11 FAIL: an unmeasured model got an invented level"
assert resolve(None, None, None) is None, "C11 FAIL: no model, no policy"
assert resolve("default", None, DS) is None, "C11 FAIL: --effort default must force the provider default"
assert resolve(None, "default", DS) is None, "C11 FAIL: env 'default' must force the provider default"
assert resolve(" DEFAULT ", None, DS) is None, "C11 FAIL: the opt-out token is not case/space tolerant"
assert resolve("", "", DS) == "none", "C11 FAIL: empty means not-provided and falls through to policy"
for bad in (("bogus", None), (None, "bogus")):
    try:
        resolve(bad[0], bad[1], DS)
    except ValueError:
        pass
    else:
        raise AssertionError(f"C11 FAIL: invalid value {bad!r} fell back to policy instead of raising")
print("unit checks pass")
PY

# ------------------------------------------------------------- CLI behaviour
# A HOME with no OpenCode auth store. If validation ran AFTER auth, these calls
# would die on the missing key instead of on the bad effort -- which is the
# whole point: reject before touching credentials or the network.
export HOME="$TMP/home"
mkdir -p "$HOME"

# C5 -- an invalid --effort exits 2 and says so, with no auth read.
set +e
out="$(printf 'x' | "$OLL" "task" --effort bogus 2>&1)"; rc=$?
set -e
[ "$rc" -eq 2 ] || fail "C5: invalid --effort exited $rc, want 2 (got: $out)"
grep -qi "bogus" <<<"$out" || fail "C5: message does not name the bad value: $out"
grep -qi "auth" <<<"$out" && fail "C5: it reached the auth store before validating: $out"

# C6 -- an invalid OLL_REASONING_EFFORT is rejected the same way. An env typo
# must not silently pass through to the provider on every call in a fanout.
set +e
out="$(OLL_REASONING_EFFORT=nope "$OLL" "task" </dev/null 2>&1)"; rc=$?
set -e
[ "$rc" -eq 2 ] || fail "C6: invalid env effort exited $rc, want 2 (got: $out)"
grep -qi "OLL_REASONING_EFFORT" <<<"$out" || fail "C6: message does not name the env var: $out"

# C7 -- route inspection stays offline and unaffected by effort.
out="$(OLL_REASONING_EFFORT=low "$OLL" --route-profile reasoning --json 2>&1)" \
  || fail "C7: route inspection broke under an effort setting: $out"
grep -q '"model":"glm-5.3"' <<<"$out" || fail "C7: route output drifted: $out"

# C8 -- the provider_empty remedy must name --effort. Raising --max-tokens was
# the ONLY advice before, and it is the expensive half of the fix.
grep -q -- "--effort" "$OLL" || fail "C8: bin/oll never mentions --effort"
python3 - "$OLL" <<'PY' || exit 1
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r"provider_empty(.{0,400})", src, re.S)
assert m, "C8 FAIL: provider_empty branch not found"
assert "--effort" in m.group(1), "C8 FAIL: provider_empty remedy does not mention --effort"
print("cli checks pass")
PY

# C9 -- the builder must actually be WIRED to the request, not merely present.
# bin/mut proved this hole: deleting the `payload = build_payload(...)` call
# survived every check above, because they all stop at the function boundary.
# This one drives main() to the transport with a stub and inspects the bytes
# that would have gone on the wire (Tier-1c: a contract for a tool whose job is
# to cross a boundary must cross it at least once).
python3 - "$OLL" <<'PYWIRE' || exit 1
import io, json, runpy, sys

mod = runpy.run_path(sys.argv[1])
main = mod["main"]
g = main.__globals__            # run_path returns a COPY; patch the live namespace

sent = {}

class FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()

def fake_urlopen(req, *a, **k):
    sent["body"] = json.loads(req.data.decode())
    return FakeResponse()

g["load_key"] = lambda: "fixture-key"
g["urllib"].request.urlopen = fake_urlopen
g["_log_usage"] = lambda *a, **k: None

def run(argv):
    sent.clear()
    old_argv, old_stdin = sys.argv, sys.stdin
    sys.argv = argv
    sys.stdin = io.StringIO("")          # not a tty -> read() returns ""
    try:
        rc = main()
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin
    assert rc == 0, f"C9 FAIL: main() returned {rc} for {argv}"
    assert "body" in sent, f"C9 FAIL: no request was built for {argv}"
    return sent["body"]

body = run(["oll", "hello", "--effort", "low"])
assert body.get("reasoning_effort") == "low", \
    f"C9 FAIL: --effort never reached the request body: {body!r}"
assert body["messages"][1]["content"] == "hello", f"C9 FAIL: prompt lost: {body!r}"

# C12 -- the measured policy reaches the wire (this REPLACES the opt-in-era
# assertion that a bare call carries no key: since lq-956ab278 the bare call
# on a SWEPT model must carry that model's measured level -- leaving the
# highest-frequency lane on an unbounded provider default was the flaw).
# The no-key guarantee now lives with unmeasured models and the opt-out.
import os

def run_env(argv, env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return run(argv)
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

body = run(["oll", "hello"])                       # volume default = deepseek
assert body.get("reasoning_effort") == "none", \
    f"C12 FAIL: the volume default did not carry its measured level: {body!r}"
body = run(["oll", "hello", "--reasoning"])        # glm-5.3-flash
assert body.get("reasoning_effort") == "low", \
    f"C12 FAIL: the reasoning route did not carry its measured level: {body!r}"
body = run(["oll", "hello", "--model", "kimi-k3"])  # never swept
assert "reasoning_effort" not in body, \
    f"C12 FAIL: an unmeasured model was given an invented level: {body!r}"
body = run(["oll", "hello", "--effort", "default"])
assert "reasoning_effort" not in body, \
    f"C12 FAIL: --effort default must force the provider default: {body!r}"
body = run(["oll", "hello", "--effort", "max"])
assert body.get("reasoning_effort") == "max", \
    f"C12 FAIL: an explicit effort lost to the policy: {body!r}"
body = run_env(["oll", "hello"], {"OLL_REASONING_EFFORT": "high"})
assert body.get("reasoning_effort") == "high", \
    f"C12 FAIL: the env override lost to the policy: {body!r}"
body = run_env(["oll", "hello", "--effort", "low"], {"OLL_REASONING_EFFORT": "high"})
assert body.get("reasoning_effort") == "low", \
    f"C12 FAIL: an explicit effort lost to the env: {body!r}"
print("wire checks pass")
PYWIRE

echo "oll-reasoning-effort contract: PASS"
