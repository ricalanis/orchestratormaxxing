#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT/bin/oll-sync" <<'PY'
import json
import runpy
import sys

mod = runpy.run_path(sys.argv[1])

# Ricardo reversed the K3 exclusion on 2026-08-19; the other machine kept an
# EMPTY, hardened mechanism on 2026-08-20 instead of deleting it. The
# invariant that matters is that nothing is excluded, not that the hook is gone.
assert mod.get("EXCLUDE", set()) == set(), "a catalog exclusion policy is active"
assert mod["PRETTY"]["kimi-k3"] == "Kimi K3"

payload = {
    "data": [
        {"id": "kimi-k3"},
        {"id": "library/kimi-k3:latest"},
        {"id": "glm-5.2"},
    ]
}

class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(payload).encode()

original = mod["urllib"].request.urlopen
mod["urllib"].request.urlopen = lambda *_args, **_kwargs: Response()
try:
    assert mod["live_models"]("fixture-key") == [
        "glm-5.2",
        "kimi-k3",
        "library/kimi-k3:latest",
    ]
finally:
    mod["urllib"].request.urlopen = original
PY

# --- reconcile: a curated entry with a WRONG window must be corrected --------
# "Preserve curated entries as-is" is why 13 of 20 models sat at a guessed
# context for weeks: the sync only ever filled gaps, so a wrong number, once
# written, was permanent. Reconciliation must fix the window and touch nothing
# else (lq-17b20b52).
python3 - "$ROOT/bin/oll-sync" <<'PYREC'
import runpy, sys

mod = runpy.run_path(sys.argv[1])
for name in ("reconcile_entry", "catalog_limits", "load_catalog"):
    assert name in mod, f"RECONCILE FAIL: bin/oll-sync exposes no {name}"

reconcile = mod["reconcile_entry"]
catalog_limits = mod["catalog_limits"]

CAT = {"models": {
    "deepseek-v4-pro:0813": {"context": 1_000_000, "output": None},
    "glm-5.2": {"context": 976_000, "output": None},
}}

# A wrong window is corrected, and the correction is REPORTED (old value back).
entry = {"name": "deepseek-v4-pro:0813", "limit": {"context": 200_000, "output": 32768}}
fixed, was = reconcile(("deepseek-v4-pro:0813"), entry, CAT)
assert was == 200_000, f"RECONCILE FAIL: correction not reported: {was!r}"
assert fixed["limit"]["context"] == 1_000_000, f"RECONCILE FAIL: {fixed!r}"

# Everything else on the curated entry survives byte-for-byte, and the input
# object is not mutated in place (the caller still needs the original to report).
entry2 = {"name": "Pretty Name", "extra": {"keep": True},
          "limit": {"context": 200_000, "output": 65536}}
fixed2, _ = reconcile("glm-5.2", entry2, CAT)
assert fixed2["name"] == "Pretty Name" and fixed2["extra"] == {"keep": True}, \
    f"RECONCILE FAIL: curated fields lost: {fixed2!r}"
assert fixed2["limit"]["output"] == 65536, "RECONCILE FAIL: clobbered a curated output cap"
assert entry2["limit"]["context"] == 200_000, "RECONCILE FAIL: mutated the caller's entry"

# A correct window is a no-op and reports nothing -- so `already in sync` stays
# truthful and the sync does not rewrite the config every run.
ok = {"name": "glm-5.2", "limit": {"context": 976_000, "output": 32768}}
same, was = reconcile("glm-5.2", ok, CAT)
assert was is None and same is ok, f"RECONCILE FAIL: churned a correct entry: {was!r}"

# UNKNOWN stays unknown: a model absent from the catalog is LEFT ALONE, never
# rewritten toward a guess. This is the assertion that keeps ctx_for's bug dead.
untouched = {"name": "mystery", "limit": {"context": 12345, "output": 32768}}
same, was = reconcile("not-in-catalog", untouched, CAT)
assert was is None and same is untouched, "RECONCILE FAIL: guessed for an unknown model"
assert catalog_limits("not-in-catalog", CAT) is None
assert catalog_limits("glm-5.2", {}) is None, "RECONCILE FAIL: empty catalog produced limits"
assert catalog_limits("glm-5.2", None) is None, "RECONCILE FAIL: absent catalog produced limits"
print("reconcile checks pass")
PYREC

printf 'oll-sync contract: PASS\n'
