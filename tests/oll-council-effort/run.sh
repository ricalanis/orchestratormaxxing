#!/usr/bin/env bash
# Contract: bin/oll-council inherits bin/oll's reasoning-budget control, and it
# inherits it by REUSE rather than by copy.
#
# Why this exists (lq-e3ffec0b, 2026-08-26): `reasoning_effort` landed in bin/oll
# but oll-council builds its own chat/completions body (it never shells out to
# oll), so --effort/OLL_REASONING_EFFORT had no effect there. A council is the
# one call shape where that hurts most: N heavy thinking models answer the SAME
# prompt in parallel, so an unbounded thinking tax is paid N times, and a
# council comparison then measures verbosity against a token cap rather than
# capability. Measured on bin/oll the same day: glm-5.2 went 160 -> 6 completion
# tokens for the same answer at --effort low.
#
# The vocabulary must NOT be re-declared here. Two closed lists that both claim
# to be "what the provider accepts" drift, and the drifted one fails open: an
# effort this tool accepts but bin/oll rejects would be silently ignored by the
# provider while the command line claims the budget was bounded.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COUNCIL="$ROOT/bin/oll-council"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "oll-council-effort contract: FAIL — $*" >&2; exit 1; }

# ------------------------------------------------------- C0: reuse, not copy
python3 - "$COUNCIL" "$ROOT/bin/oll" <<'PY' || exit 1
import re, sys
src = open(sys.argv[1]).read()
assert not re.search(r"^\s*REASONING_EFFORTS\s*=", src, re.M), \
    "C0 FAIL: oll-council re-declares REASONING_EFFORTS; it must read bin/oll's"
assert not re.search(r"^\s*def\s+normalize_effort\b", src, re.M), \
    "C0 FAIL: oll-council defines its own normalize_effort; it must read bin/oll's"
assert not re.search(r"^\s*def\s+build_payload\b", src, re.M), \
    "C0 FAIL: oll-council defines its own build_payload; it must read bin/oll's"
# The six efforts must not appear as a hand-copied literal list either.
assert not re.search(r"minimal[\"'],\s*[\"']low", src), \
    "C0 FAIL: the effort vocabulary is copied into oll-council as a literal"
print("C0 pass: no duplicated effort vocabulary")
PY

# ------------------------------------------------- CLI rejection, pre-auth
# A HOME with no OpenCode auth store. If validation ran AFTER auth, these calls
# would die on the missing key instead of on the bad effort.
export HOME="$TMP/home"
mkdir -p "$HOME"

# C1 -- an invalid --effort exits 2 with the SHARED validator's message.
# "choose from ..." is normalize_effort's wording; argparse's own
# "unrecognized arguments: --effort bogus" also exits 2 and also names the
# value, so naming the value alone would pass against the UNFIXED tool.
set +e
out="$("$COUNCIL" "task" --effort bogus </dev/null 2>&1)"; rc=$?
set -e
[ "$rc" -eq 2 ] || fail "C1: invalid --effort exited $rc, want 2 (got: $out)"
grep -qi "bogus" <<<"$out" || fail "C1: message does not name the bad value: $out"
grep -qi "choose from" <<<"$out" \
  || fail "C1: not the shared validator's message (flag likely unrecognized): $out"
grep -qi "auth" <<<"$out" && fail "C1: it reached the auth store before validating: $out"

# C2 -- an invalid OLL_REASONING_EFFORT is rejected the same way. An env typo
# must not pass through to the provider once per council member.
set +e
out="$(OLL_REASONING_EFFORT=nope "$COUNCIL" "task" </dev/null 2>&1)"; rc=$?
set -e
[ "$rc" -eq 2 ] || fail "C2: invalid env effort exited $rc, want 2 (got: $out)"
grep -qi "OLL_REASONING_EFFORT" <<<"$out" || fail "C2: message does not name the env var: $out"

# C3 -- a VALID --effort is accepted by the parser and gets as far as auth.
# Without this, C1 would still pass on a tool that simply has no --effort flag.
set +e
out="$("$COUNCIL" "task" --effort low </dev/null 2>&1)"; rc=$?
set -e
[ "$rc" -ne 2 ] || fail "C3: a valid --effort was rejected (flag missing?): $out"
grep -qi "auth" <<<"$out" || fail "C3: expected to reach the auth store, got: $out"

# ------------------------------------------------------------- wire behaviour
# Tier-1c: the control must reach the REQUEST, for EVERY council member, not
# merely exist as a function. bin/mut proved on bin/oll that a builder can be
# present and unwired while every unit check stays green.
python3 - "$COUNCIL" <<'PYWIRE' || exit 1
import io, json, runpy, sys, threading

mod = runpy.run_path(sys.argv[1])
main = mod["main"]
g = main.__globals__            # run_path returns a COPY; patch the live namespace

sent = []
lock = threading.Lock()

class FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()

def fake_urlopen(req, *a, **k):
    with lock:
        sent.append(json.loads(req.data.decode()))
    return FakeResponse()

g["load_key"] = lambda: "fixture-key"
g["urllib"].request.urlopen = fake_urlopen

MODELS = "glm-5.3,kimi-k3,qwen3.5:397b"

diag = {}

def run(argv, env=None):
    sent.clear()
    diag.clear()
    import contextlib, os
    old_argv, old_stdin = sys.argv, sys.stdin
    old_env = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    sys.argv = argv
    sys.stdin = io.StringIO("")          # not a tty -> read() returns ""
    err, out = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = main()
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin
        for k, v in old_env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
    diag["stderr"] = err.getvalue()
    assert rc == 0, f"FAIL: main() returned {rc} for {argv}: {err.getvalue()}"
    return list(sent)

BASE = ["oll-council", "hello", "--models", MODELS, "--max-tokens", "100"]

# C4 -- with nothing provided, each member resolves its OWN model's measured
# policy default (lq-956ab278: there is no single level that is cheap for
# every model, so a council forcing one level per run has the same disease as
# a global env var). In this panel glm-5.3 was swept (low, 2026-08-30); kimi-k3 and
# qwen3.5 were not, so their bodies must stay byte-identical to the opt-in
# era: key ABSENT, never null, never an invented level. The payload-shape
# asserts are the pin on build_payload's signature; key order is deliberately
# not asserted (a wire non-semantic).
bodies = run(BASE)
assert len(bodies) == 3, f"C4 FAIL: {len(bodies)} requests for 3 models"
for b in bodies:
    if b["model"] == "glm-5.3":
        assert b.get("reasoning_effort") == "low", \
            f"C4 FAIL: a swept member did not carry its measured level: {b!r}"
        assert set(b) == {"model", "messages", "max_tokens", "temperature",
                          "stream", "reasoning_effort"}, \
            f"C4 FAIL: unexpected payload keys: {sorted(b)}"
    else:
        assert "reasoning_effort" not in b, \
            f"C4 FAIL: an unmeasured member was given an invented level: {b!r}"
        assert set(b) == {"model", "messages", "max_tokens", "temperature", "stream"}, \
            f"C4 FAIL: unexpected payload keys: {sorted(b)}"
    assert b["max_tokens"] == 100 and b["temperature"] == 0.7 and b["stream"] is False, \
        f"C4 FAIL: council payload drifted: {b!r}"
    assert b["messages"][1]["content"] == "hello", f"C4 FAIL: prompt lost: {b!r}"

# C4b -- the opt-out reaches every member: 'default' forces the provider
# default (key absent) even on a swept model, so the unset baseline stays
# measurable through the council too.
bodies = run(BASE + ["--effort", "default"])
for b in bodies:
    assert "reasoning_effort" not in b, \
        f"C4b FAIL: --effort default did not force the provider default: {b!r}"

# C5 -- --effort reaches EVERY member's request. A council fans out per model,
# so plumbing it into one call site and not the loop is the live failure mode.
bodies = run(BASE + ["--effort", "low"])
assert len(bodies) == 3, f"C5 FAIL: {len(bodies)} requests for 3 models"
assert {b["model"] for b in bodies} == set(MODELS.split(",")), \
    f"C5 FAIL: wrong models: {[b['model'] for b in bodies]}"
for b in bodies:
    assert b.get("reasoning_effort") == "low", \
        f"C5 FAIL: --effort never reached {b.get('model')}: {b!r}"

# C6 -- OLL_REASONING_EFFORT is honoured, and an explicit flag BEATS it.
bodies = run(BASE, env={"OLL_REASONING_EFFORT": "none"})
for b in bodies:
    assert b.get("reasoning_effort") == "none", f"C6 FAIL: env ignored: {b!r}"
bodies = run(BASE + ["--effort", "high"], env={"OLL_REASONING_EFFORT": "none"})
for b in bodies:
    assert b.get("reasoning_effort") == "high", f"C6 FAIL: flag lost to env: {b!r}"

# C7 -- the body is built by bin/oll's build_payload, not a look-alike here.
# Patching the shared builder must change what goes on the wire; if it does
# not, oll-council hand-rolls the body and the two will drift apart silently.
real_policy = g["worker_policy"]()
patched = dict(real_policy)
def marked_build(**kw):
    body = real_policy["build_payload"](**kw)
    body["_shared_builder"] = True
    return body
patched["build_payload"] = marked_build
g["worker_policy"] = lambda: patched

bodies = run(BASE + ["--effort", "low"])
for b in bodies:
    assert b.get("_shared_builder") is True, \
        f"C7 FAIL: oll-council does not route through bin/oll's build_payload: {b!r}"

# C8 -- the run must SAY what budget it ran at. bin/mut caught this hole: the
# whole payload can be correct while the operator has no way to tell a bounded
# council from an unbounded one, and the two differ only in tokens spent. An
# unreadable control is the failure mode knowledge/signal-vs-artifact warns
# about, so the effort is stamped on the done-line -- and only when it is set.
g["worker_policy"] = lambda: real_policy          # undo C7's marker
run(BASE + ["--effort", "low"])
assert "effort=low" in diag["stderr"], \
    f"C8 FAIL: the done-line hides which budget the council ran at: {diag['stderr']!r}"
# A panel with a swept member ran partly bounded by policy -- hiding that is
# the same unreadable-control failure; a panel with NO swept member and no
# request ran unbounded and must not claim otherwise.
run(BASE)
assert "effort=model-policy" in diag["stderr"], \
    f"C8 FAIL: a policy-bounded run is reported as unbounded: {diag['stderr']!r}"
run(["oll-council", "hello", "--models", "kimi-k3,qwen3.5:397b", "--max-tokens", "100"])
assert "effort=" not in diag["stderr"], \
    f"C8 FAIL: an unbounded run is reported as if a budget were applied: {diag['stderr']!r}"
run(BASE + ["--effort", "default"])
assert "effort=" not in diag["stderr"], \
    f"C8 FAIL: an opted-out run is reported as bounded: {diag['stderr']!r}"
assert "council done" in diag["stderr"], \
    f"C8 FAIL: the done-line vanished entirely: {diag['stderr']!r}"
print("wire checks pass")
PYWIRE

echo "oll-council-effort contract: PASS"
