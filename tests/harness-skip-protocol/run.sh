#!/usr/bin/env bash
# Contract for the SKIP protocol: harness-verify must distinguish "the contract
# ran and the target failed" from "the contract could not run on this host".
#
# Why this exists: three of the flaw queue's open harness-verify reds were
# Mac-only "tmux missing" rows (lq-ffc3c61b / lq-b73b6fd7 / lq-c3e6c297) against
# an Ubuntu baseline of 0 errors — an environment gap promoted to a harness flaw
# that the machine reading the queue could never reproduce. Same family as the
# timeout misreport the loop already guards: report inability-to-measure AS
# inability-to-measure.
#
# It drives the REAL run_behavioral_contract and the REAL contract_issue against
# fixture scripts, so it cannot pass on a grep. C3/C4/C6 are proven red against
# the pre-fix verifier, where exit 77 was classified `failed`/error.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HV="${HARNESS_VERIFY_UNDER_TEST:-$ROOT/bin/harness-verify}"
LIB="$ROOT/tests/lib/precondition.sh"

[[ -f "$HV" ]]  || { printf 'skip-protocol: harness-verify missing\n' >&2; exit 1; }
[[ -f "$LIB" ]] || { printf 'skip-protocol: tests/lib/precondition.sh missing\n' >&2; exit 1; }

python3 - "$HV" "$LIB" <<'PY'
import glob
import importlib.machinery
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

hv_path, lib_path = sys.argv[1:3]
spec = importlib.util.spec_from_loader(
    "hv_under_test", importlib.machinery.SourceFileLoader("hv_under_test", hv_path))
hv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hv)

fails = []
def check(cid, cond, msg):
    print(("  ok   " if cond else "  FAIL ") + f"{cid}  {msg}")
    if not cond:
        fails.append(f"{cid}: {msg}")

# Resolved defensively so the pre-fix verifier produces legible FAILs instead of
# an import crash — that is what makes the red proof readable.
SKIP_EXIT = getattr(hv, "HARNESS_SKIP_EXIT", None)
classify = getattr(hv, "contract_issue", None)
check("C0", SKIP_EXIT == 77, f"harness-verify exposes HARNESS_SKIP_EXIT == 77 (got {SKIP_EXIT!r})")
check("C0b", callable(classify), "harness-verify exposes contract_issue()")
if SKIP_EXIT is None:
    SKIP_EXIT = 77
if not callable(classify):
    classify = lambda _r: ("MISSING", "contract_issue() not defined")

tmp = tempfile.mkdtemp(prefix="skip-protocol-")

def fixture(name, body):
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path

passing = fixture("pass.sh", "exit 0\n")
failing = fixture("fail.sh", "echo 'target did the wrong thing' >&2\nexit 1\n")
# The authentic skip goes through the REAL helper — a hand-rolled `exit 77` is
# no longer enough on purpose (C17), so this fixture must not fake it.
skipping = fixture("skip.sh",
                   f". {lib_path}\nharness_need_cmd definitely-not-a-real-tmux-xyz 'tmux'\n")

def run(path, contract_id="skip-protocol"):
    return hv.run_behavioral_contract(contract_id, ["bash", path],
                                      timeout_seconds=20, retry_observer=True)

# ── C1/C2: the two ordinary outcomes still classify as before ────────────────
r_pass = run(passing)
check("C1", r_pass["outcome"] == "passed", f"exit 0 -> passed (got {r_pass['outcome']})")
r_fail = run(failing)
check("C2", r_fail["outcome"] == "failed", f"exit 1 -> failed (got {r_fail['outcome']})")

# ── C3: the SKIP exit is its own outcome, not a failure ──────────────────────
# Proven red pre-fix: exit 77 was indistinguishable from exit 1.
r_skip = run(skipping)
check("C3", r_skip["outcome"] == "skipped",
      f"exit {SKIP_EXIT} -> skipped (got {r_skip['outcome']})")
check("C3b", r_skip["outcome"] != "failed", "a skip is never reported as a failure")

# ── C4: severity mapping — a skip warns, a failure errors ────────────────────
# This is the bit loop-tick reads: it enqueues severity == "error" only, so a
# skip landing as an error is exactly how a machine-local gap becomes a
# permanent phantom flaw.
sev_fail = classify(r_fail)
sev_skip = classify(r_skip)
sev_pass = classify(r_pass)
check("C4", sev_fail is not None and sev_fail[0] == "error",
      f"failed contract -> error severity (got {sev_fail})")
check("C4b", sev_skip is not None and sev_skip[0] == "warn",
      f"skipped contract -> warn severity (got {sev_skip})")
check("C4c", sev_pass is None, f"passing contract reports nothing (got {sev_pass})")

# ── C5: the skip names its missing dependency ────────────────────────────────
# A skip that does not say WHAT is missing is indistinguishable from silence,
# and silence is the failure mode this whole protocol is guarding against.
check("C5", sev_skip is not None and "tmux" in sev_skip[1],
      f"skip message carries the dependency name (got {sev_skip and sev_skip[1]!r})")

# ── C6: NEGATIVE FIXTURE — the classifier must still discriminate ────────────
# An always-"skipped" or always-None classifier would pass C3/C4b/C5 while
# hiding every real red. Feed it a genuine failure result and require an error.
forged = dict(r_fail)
forged["outcome"] = "failed"
check("C6", classify(forged)[0] == "error",
      "classifier still returns error for a failed contract (not always-skip)")
check("C6b", classify({"outcome": "inconclusive", "attempts": [{}]}) is None,
      "an inconclusive contract is left to the existing observer path, not re-labelled")

# ── C7: the shared helper RESOLVES before it skips ───────────────────────────
# The regression this replaces: an installed-but-off-minimal-PATH tool read as
# absent (lq-450528ba). A present command must never skip.
probe = fixture("resolve.sh",
                f". {lib_path}\nharness_need_cmd bash 'probe: bash'\necho RESOLVED\n")
out = subprocess.run(["bash", probe], capture_output=True, text=True, timeout=20,
                     env=dict(os.environ, PATH="/usr/bin:/bin"))
check("C7", out.returncode == 0 and "RESOLVED" in out.stdout,
      f"present command does not skip (rc={out.returncode})")

# ── C8: a genuinely absent command exits with the SKIP code, naming itself ───
absent = fixture("absent.sh",
                 f". {lib_path}\nharness_need_cmd definitely-not-a-real-binary-xyz 'probe: xyz'\necho REACHED\n")
out = subprocess.run(["bash", absent], capture_output=True, text=True, timeout=20)
check("C8", out.returncode == SKIP_EXIT,
      f"absent command exits {SKIP_EXIT} (got {out.returncode})")
check("C8b", "definitely-not-a-real-binary-xyz" in out.stderr or "probe: xyz" in out.stderr,
      f"skip names the missing dependency (stderr={out.stderr.strip()!r})")
check("C8c", "REACHED" not in out.stdout, "skip aborts the contract instead of continuing")

# ── C9: every tmux-dependent contract routes through the shared helper ───────
# The 2026-07 fix was copied into one contract and regressed into three others.
# The FIRST version of this check pinned a hardcoded list of five contracts —
# and that is precisely why it stayed green while tests/warp-recovery-shell and
# tests/ubuntu-clipboard regressed the same class in 2026-08 (lq-6cc03e4c hard-
# failed "real tmux is required"; lq-2544ef07 died under `set -e` with EMPTY
# stderr, which harness-verify can only render as "unknown failure"). Both were
# Mac-only reds against an Ubuntu baseline of 0 errors, and loop-tick enqueued
# them as harness flaws — the exact artifact-as-signal failure this contract
# exists to prevent.
#
# A ratchet that ENUMERATES what it protects leaves every contract written
# after it unprotected by default. So the set is now DISCOVERED from the tree:
# a contract is tmux-dependent if it resolves tmux for itself OR already
# declares the precondition, and either way it must go through the helper.
# Discovery is a superset of the original five (C9f), so this can only widen
# coverage, never narrow it.
#
# LIMITATION, stated so it is not mistaken for a completeness guarantee (raised
# by BOTH cross-family critics of this change): this is a best-effort floor,
# not a proof. A contract can depend on tmux without matching the pattern —
# indirect invocation (`cmd=tmux; $cmd`), a wrapper script, or a dependency
# reached through tests/lib. Such a contract regresses this class and stays
# green, exactly the way the hardcoded list did. What IS guaranteed: coverage
# never shrinks below the pinned five, the pattern is proven live (C9d), and
# every contract that resolves tmux in the three common forms is caught. The
# residual indirect-invocation gap is tracked in the flaw queue, not closed here.
repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(lib_path))))
RAW_TMUX = re.compile(r"(?:command -v|which|type -P)\s+tmux\b")
PINNED = ("o", "warp-agent-transport", "tmux-send", "agent-tab-status", "task-plan")

# A dead regex would make every assertion below pass vacuously, so the pattern
# is proven live against a literal fixture before it is trusted on the tree.
check("C9d", bool(RAW_TMUX.search('X="$(command -v tmux || true)"')),
      "the discovery pattern matches a raw tmux lookup (not a dead regex)")

discovered = []
for path in sorted(glob.glob(os.path.join(repo, "tests", "*", "run.sh"))):
    body = open(path).read()
    if RAW_TMUX.search(body) or "harness_need_cmd tmux" in body:
        discovered.append((os.path.basename(os.path.dirname(path)), body))

check("C9e", len(discovered) > 0,
      f"the scan discovered tmux-dependent contracts (got {len(discovered)})")
# Fleet-only contracts (their tool, test and installer half live in the private
# install-fleet.sh) are absent from the graduated public tree by design.
FLEET_ONLY = {"task-plan"}
fleet_present = os.path.isfile(os.path.join(repo, "install-fleet.sh"))
expected = set(PINNED) if fleet_present else set(PINNED) - FLEET_ONLY
missing = sorted(expected - {n for n, _ in discovered})
check("C9f", not missing,
      f"discovery still covers every originally pinned contract (missing: {missing})")

for name, body in discovered:
    check("C9", "harness_need_cmd tmux" in body,
          f"{name} declares its tmux precondition through the shared helper")
    check("C9b", "tmux not installed\\n' >&2; exit 1" not in body,
          f"{name} no longer hard-fails on a missing tmux")

# ── C11: NEGATIVE — exit 77 WITHOUT the marker is still a failure ────────────
# The forgery route a code-only protocol leaves open: a contract under `set -e`
# propagating some tool's own 77 would silently stop being measured. Requiring
# the stderr marker means only a deliberate harness_need_cmd skip is honoured.
bare77 = fixture("bare77.sh", "echo 'the tool under test exploded' >&2\nexit 77\n")
r_bare = run(bare77)
check("C11", r_bare["outcome"] == "failed",
      f"exit 77 without the SKIP marker stays a failure (got {r_bare['outcome']})")
sev_bare = classify(r_bare)
check("C11b", sev_bare is not None and sev_bare[0] == "error",
      f"unmarked 77 still reaches the flaw queue as an error (got {sev_bare})")

# ── C15: NEGATIVE — the marker must be the contract's LAST word ──────────────
# An unanchored `"SKIP:" in stderr` would excuse any exit-77 target whose stderr
# merely MENTIONS a skip somewhere. Raised by a cross-family critic against the
# first version of this guard; both fixtures below passed it and must not now.
mid = fixture("midskip.sh",
              "echo 'ERROR: SKIP: retry later' >&2\necho 'the tool under test exploded' >&2\nexit 77\n")
r_mid = run(mid)
check("C15", r_mid["outcome"] == "failed",
      f"a mentioned-but-not-final SKIP: does not excuse measurement (got {r_mid['outcome']})")
inline = fixture("inlineskip.sh", "echo 'boom because SKIP: unsupported' >&2\nexit 77\n")
check("C15b", run(inline)["outcome"] == "failed",
      "a mid-line SKIP: does not excuse measurement")
check("C15c", run(skipping)["outcome"] == "skipped",
      "the real harness_need_cmd shape still skips (anchoring did not break it)")

# ── C17: the skip must be AUTHENTICATED, not inferred from stderr ────────────
# Raised by a cross-lab critic: "a nested tool exiting 77 whose final stderr
# line starts with SKIP: is indistinguishable from the contract's own gate —
# the protocol authenticates neither source nor call site." It does now: only
# harness_need_cmd writes the per-attempt receipt path harness-verify invents.
forger = fixture("forger.sh",
                 "echo 'SKIP: pretending a dependency is missing' >&2\nexit 77\n")
r_forge = run(forger)
check("C17", r_forge["outcome"] == "failed",
      f"exit 77 + a perfect SKIP: last line but NO receipt stays failed (got {r_forge['outcome']})")
check("C17b", classify(r_forge)[0] == "error",
      "an unauthenticated skip still reaches the flaw queue as an error")
# ...and the real helper, which DOES write the receipt, still skips.
authentic = fixture("authentic.sh",
                    f". {lib_path}\nharness_need_cmd definitely-not-a-real-binary-xyz 'authentic: xyz'\n")
r_auth = run(authentic)
check("C17c", r_auth["outcome"] == "skipped",
      f"the real helper's authenticated skip is honoured (got {r_auth['outcome']})")
check("C17d", "xyz" in (classify(r_auth) or ("", ""))[1],
      f"the authenticated skip still names its dependency (got {classify(r_auth)})")
# A nested command's skip must NOT excuse the parent contract: the parent has
# work of its own left to do, and set -e propagating a child's 77 is exactly
# the laundering path the receipt closes. (The receipt IS written here — by the
# child — so this pins that the guard is about the whole protocol, not one bit.)
nested = fixture("nested.sh",
                 f"set -euo pipefail\nbash {tmp}/authentic.sh\necho 'never reached'\n")
check("C17e", run(nested)["outcome"] in {"skipped", "failed"},
      "a nested skip resolves deterministically (documented behaviour, not undefined)")

# ── C18: BOTH halves of the authentication are load-bearing ──────────────────
# The receipt proves the skip came from this contract's gate; the marker proves
# the gate was its last act. Each fixture below supplies exactly one half.
receipt_only = fixture("receipt-only.sh",
                       'printf "gizmo missing\\n" > "$HARNESS_SKIP_RECEIPT"\n'
                       "echo 'the tool under test exploded' >&2\nexit 77\n")
check("C18", run(receipt_only)["outcome"] == "failed",
      f"a receipt without the marker does not excuse measurement (got {run(receipt_only)['outcome']})")
silent_receipt = fixture("silent-receipt.sh",
                         'printf "gizmo missing\\n" > "$HARNESS_SKIP_RECEIPT"\nexit 77\n')
check("C18b", run(silent_receipt)["outcome"] == "failed",
      "a receipt with NO stderr at all does not excuse measurement")

# ── C12: the DISPATCH routing, not just the classifier ───────────────────────
# What actually reaches the flaw queue is this routing: loop-tick enqueues
# severity=="error" only. Asserting contract_issue() alone left the wiring
# unmeasured (bin/mut: `(err if severity == 'error' else warn)` inverted and
# deleted both survived), so drive it with recording sinks.
route = getattr(hv, "record_contract_result", None)
check("C12", callable(route), "harness-verify exposes record_contract_result()")
if callable(route):
    errs, warns = [], []
    route(r_fail, "bin/thing", lambda w, m: errs.append((w, m)), lambda w, m: warns.append((w, m)))
    check("C12a", len(errs) == 1 and not warns, f"failed contract routed to err (err={errs}, warn={warns})")
    errs.clear(); warns.clear()
    route(r_skip, "bin/thing", lambda w, m: errs.append((w, m)), lambda w, m: warns.append((w, m)))
    check("C12b", len(warns) == 1 and not errs,
          f"skipped contract routed to warn, never err (err={errs}, warn={warns})")
    errs.clear(); warns.clear()
    route(r_pass, "bin/thing", lambda w, m: errs.append((w, m)), lambda w, m: warns.append((w, m)))
    check("C12c", not errs and not warns, f"passing contract routes nowhere (err={errs}, warn={warns})")
    check("C12e", route(r_skip, "bin/thing", lambda *_: None, lambda *_: None)
          == ("warn", "behavioral contract skipped: SKIP: tmux not installed — "
                      "contract cannot be measured on this host"),
          "routing reports back exactly what it recorded")
    check("C12f", route(r_pass, "bin/thing", lambda *_: None, lambda *_: None) is None,
          "routing a passing contract reports nothing back")
    errs.clear(); warns.clear()
    route(r_bare, "bin/thing", lambda w, m: errs.append((w, m)), lambda w, m: warns.append((w, m)))
    check("C12d", len(errs) == 1 and not warns,
          f"unmarked 77 still routed to err (err={errs}, warn={warns})")

# ── C13: the counts that make a skip visible ─────────────────────────────────
# A skip that is not counted is a contract that silently stopped being measured.
counts = getattr(hv, "contract_outcome_counts", None)
check("C13", callable(counts), "harness-verify exposes contract_outcome_counts()")
if callable(counts):
    tally = counts([r_pass, r_fail, r_skip, r_skip, {"outcome": "inconclusive"}])
    check("C13a", tally.get("skipped") == 2, f"two skips counted as 2 (got {tally.get('skipped')})")
    check("C13b", tally.get("inconclusive") == 1,
          f"inconclusive still counted separately (got {tally.get('inconclusive')})")
    check("C13c", counts([r_pass, r_fail]).get("skipped") == 0,
          "no skips counts 0 (not an always-nonzero counter)")

# ── C14: the reported detail is the LAST stderr line ─────────────────────────
# Preamble noise must not swallow the reason; the reason is the whole point of
# reporting a skip instead of swallowing it.
noisy = {"outcome": "skipped",
         "attempts": [{"stderr_tail": "warming up\n\nSKIP: gizmo not installed — cannot measure\n"}]}
sev_noisy = classify(noisy)
check("C14", sev_noisy == ("warn", "behavioral contract skipped: "
                           "SKIP: gizmo not installed — cannot measure"),
      f"multi-line stderr reports EXACTLY its last non-empty line (got {sev_noisy})")

# ── C16: the real main() summary — the surface a human and the JSON reader see ─
# main() is where the counts become the exit code, the JSON `skipped` field and
# the printed header. Nothing else in the suite reaches it (bin/mut: deleting
# `counts = contract_outcome_counts(...)` and the whole header print both
# survived), so drive the REAL main() with audit() stubbed to a known result.
import contextlib
import io

def summarize(results, issues=()):
    hv.audit = lambda _root: (list(issues), results)
    buf = io.StringIO()
    code = None
    argv = sys.argv
    sys.argv = ["harness-verify", "--json"]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                hv.main()
            except SystemExit as ex:
                code = ex.code
    finally:
        sys.argv = argv
    return code, buf.getvalue()

real_audit = hv.audit
try:
    code, out = summarize([r_pass, r_skip, r_skip])
    payload = __import__("json").loads(out)
    check("C16", payload.get("skipped") == 2,
          f"JSON reports skipped=2 (got {payload.get('skipped')})")
    check("C16b", payload.get("errors") == 0,
          f"skips alone do not make errors (got {payload.get('errors')})")
    check("C16c", code == 0,
          f"a run whose only anomaly is a skip still exits 0 (got {code})")
    code_clean, out_clean = summarize([r_pass])
    payload_clean = __import__("json").loads(out_clean)
    check("C16d", payload_clean.get("skipped") == 0,
          f"no skips reports 0, not a constant (got {payload_clean.get('skipped')})")
    # the human surface must SAY it skipped — a silent skip is the failure mode
    hv.audit = lambda _root: ([], [run(passing, "id-was-passing"),
                                   run(skipping, "id-was-skipped")])
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["harness-verify"]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                hv.main()
            except SystemExit:
                pass
    finally:
        sys.argv = argv
    human = buf.getvalue()
    check("C16e", "1 skipped" in human, f"header states the skip count (got {human.splitlines()[:2]})")
    check("C16f", "OK — harness green." not in human,
          "a run with a skipped contract does not print an unqualified green")
    # Distinct ids on purpose: naming "the skipped one" is only meaningful if the
    # passing one is NOT named (an unfiltered print would satisfy a same-id check).
    check("C16g", "id-was-skipped" in human,
          f"the skipped contract is named on the human surface (got {human!r})")
    check("C16h", "id-was-passing" not in human,
          f"a passing contract is NOT listed as an observation (got {human!r})")
    # A genuinely clean run must still SAY it is clean — otherwise the operator
    # cannot tell "green" from "printed nothing".
    hv.audit = lambda _root: ([], [r_pass])
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["harness-verify"]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                hv.main()
            except SystemExit:
                pass
    finally:
        sys.argv = argv
    check("C16i", "OK — harness green." in buf.getvalue(),
          f"a clean run prints the explicit green line (got {buf.getvalue()!r})")
finally:
    hv.audit = real_audit

# ── C10: REAL runtime boundary — a real contract on a host without tmux ──────
# C3-C8 exercise fixtures; this runs the actual tests/o/run.sh with tmux absent
# from every directory the helper probes, and asserts the whole chain end to end:
# real contract -> real helper -> exit 77 -> run_behavioral_contract -> "skipped"
# -> warn severity. Without it this suite would be a statement about its fixtures.
# A PATH that has everything the contract needs EXCEPT tmux — an empty PATH
# would break its own bootstrapping (dirname/mktemp) and prove nothing about
# tmux. Mirror the real system directories as symlinks, minus the one binary
# under test.
nobin = os.path.join(tmp, "nobin")
os.makedirs(nobin, exist_ok=True)
for srcdir in ("/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"):
    if not os.path.isdir(srcdir):
        continue
    for entry in os.listdir(srcdir):
        if entry == "tmux":
            continue
        link = os.path.join(nobin, entry)
        if not os.path.lexists(link):
            try:
                os.symlink(os.path.join(srcdir, entry), link)
            except OSError:
                pass
if shutil.which("tmux", path=nobin):
    check("C10-setup", False, "fixture PATH still resolves tmux — C10 would not be measuring")
no_tmux_home = os.path.join(tmp, "no-tmux-home")
os.makedirs(no_tmux_home, exist_ok=True)
o_contract = os.path.join(repo, "tests", "o", "run.sh")
if not os.path.isfile(o_contract):
    check("C10", False, "tests/o/run.sh missing — cannot cross the real boundary")
else:
    stripped = dict(os.environ, PATH=nobin, HOME=no_tmux_home)
    # The helper deliberately rediscovers Homebrew tools omitted from PATH.
    # Disable only that fallback in this genuine-absence fixture; production
    # callers still exercise the default lookup, while C7 pins its resolution.
    stripped["HARNESS_PRECONDITION_TEST_FALLBACK_DIRS"] = ""
    for var in ("HOMEBREW_PREFIX",):
        stripped.pop(var, None)
    bash_bin = shutil.which("bash") or "/bin/bash"
    live = subprocess.run([bash_bin, o_contract], capture_output=True, text=True,
                          timeout=60, env=stripped)
    check("C10", live.returncode == SKIP_EXIT,
          f"real o contract with tmux absent exits {SKIP_EXIT} (got {live.returncode}; "
          f"stderr={live.stderr.strip()[-160:]!r})")
    check("C10b", "tmux" in live.stderr,
          f"real skip names tmux (stderr={live.stderr.strip()[-160:]!r})")
    synthetic = {"outcome": "skipped",
                 "attempts": [{"stderr_tail": live.stderr, "stdout_tail": live.stdout}]}
    sev = classify(synthetic)
    check("C10c", sev is not None and sev[0] == "warn" and "tmux" in sev[1],
          f"real skip lands as a warning naming tmux (got {sev})")

print(f"skip-protocol: {'FAILED' if fails else 'PASSED'} ({len(fails)} failure(s))")
sys.exit(1 if fails else 0)
PY
