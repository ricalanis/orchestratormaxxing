#!/usr/bin/env python3
"""Offline acceptance contract for .github/scripts/verify-result.py.

Drives the real helper via subprocess against fixture JSON files (no network,
no package deps). Covers the full policy matrix: unexpected rc, empty/malformed
JSON, missing/invalid/boolean/negative counters, rc/result inconsistency, and
the two green paths (rc0 with errors0/inconclusive0, rc2 with errors0/
inconclusive>0).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(os.environ.get(
    "CI_VERIFY_HELPER", ROOT / ".github" / "scripts" / "verify-result.py"))


def run(json_text: str, rc: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "hv.json"
        path.write_text(json_text, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(HELPER), str(path), rc],
            capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr)


def expect_pass(json_text: str, rc: str, label: str) -> None:
    code, out = run(json_text, rc)
    if code != 0:
        raise AssertionError(f"{label}: expected pass, got rc={code}\n{out}")


def expect_fail(json_text: str, rc: str, label: str) -> None:
    code, out = run(json_text, rc)
    if code != 1 or "Traceback" in out:
        raise AssertionError(f"{label}: expected a controlled exit 1, got {code}\n{out}")


def main() -> int:
    for args in ([], ["unused.json", "0", "extra"]):
        usage = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True)
        assert usage.returncode == 1 and "Traceback" not in usage.stderr, usage.stderr
    green = {"errors": 0, "warnings": 0, "inconclusive": 0, "skipped": 0}
    green_json = json.dumps(green)

    # Green paths.
    expect_pass(green_json, "0", "rc0 green")
    inconclusive = dict(green, inconclusive=2)
    expect_pass(json.dumps(inconclusive), "2", "rc2 inconclusive green")

    # Unexpected exit codes.
    for rc in ("3", "137", "99"):
        expect_fail(green_json, rc, f"unexpected rc {rc}")

    # rc=1 (errors) always fails.
    with_errors = dict(green, errors=1)
    expect_fail(json.dumps(with_errors), "1", "rc1 errors fails")

    # Empty / malformed JSON.
    expect_fail("", "0", "empty json")
    expect_fail("   \n  ", "0", "whitespace json")
    expect_fail("{not json", "0", "malformed json")
    expect_fail("[]", "0", "json root not object")
    for value in (None, True, 42, ["errors"]):
        expect_fail(json.dumps(value), "0", "invalid JSON root type")

    # Missing / invalid / boolean / negative counters.
    for key in ("errors", "warnings", "inconclusive", "skipped"):
        missing = {k: v for k, v in green.items() if k != key}
        expect_fail(json.dumps(missing), "0", f"missing counter {key}")
    bad_types = {
        "errors": "x",
        "warnings": 1.5,
        "inconclusive": None,
        "skipped": [0],
    }
    for key, val in bad_types.items():
        bad = dict(green, **{key: val})
        expect_fail(json.dumps(bad), "0", f"invalid counter {key}")
    for key in ("errors", "warnings", "inconclusive", "skipped"):
        neg = dict(green, **{key: -1})
        expect_fail(json.dumps(neg), "0", f"negative counter {key}")
    for key in ("errors", "warnings", "inconclusive", "skipped"):
        boolv = dict(green, **{key: True})
        expect_fail(json.dumps(boolv), "0", f"boolean counter {key}")

    # rc/result inconsistency.
    expect_fail(green_json, "2", "rc2 but inconclusive=0")
    expect_fail(json.dumps(with_errors), "0", "rc0 but errors>0")
    expect_fail(json.dumps(with_errors), "2", "rc2 but errors>0")
    expect_fail(green_json, "1", "rc1 but errors=0")
    expect_fail(json.dumps(inconclusive), "0", "rc0 but inconclusive>0")

    print("ci-verify: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
