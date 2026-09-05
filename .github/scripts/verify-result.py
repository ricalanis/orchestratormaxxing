#!/usr/bin/env python3
"""verify-result — enforce the CI policy on harness-verify's JSON output.

Reads the hv.json produced by `bin/harness-verify --json` plus the exit code
the verifier returned, and decides whether the CI job should pass.

Policy (mirrors the acceptance contract for the public coverage CI):
  * rc must be 0, 1, or 2 — anything else (3, 137, ...) is an unexpected
    failure and fails the job.
  * rc == 1 (errors) always fails the job.
  * The JSON must parse, be a non-empty object, and carry the four required
    counters: errors, warnings, inconclusive, skipped — each a nonnegative
    int (booleans are rejected).
  * rc/result consistency:
      rc == 0  requires errors == 0 and inconclusive == 0
      rc == 2  requires errors == 0 and inconclusive > 0
      rc == 1  always fails, including inconsistent result counters

Exit 0 = pass (job continues), 1 = fail (job fails). No network, no deps.
"""

from __future__ import annotations

import json
import sys

REQUIRED_COUNTERS = ("errors", "warnings", "inconclusive", "skipped")


def fail(message: str) -> int:
    print(f"verify-result: FAIL: {message}", file=sys.stderr)
    return 1


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify-result.py <hv.json> <rc>", file=sys.stderr)
        return 1
    json_path, rc_arg = sys.argv[1], sys.argv[2]

    try:
        rc = int(rc_arg)
    except ValueError:
        return fail(f"exit code is not an integer: {rc_arg!r}")

    if rc not in (0, 1, 2):
        return fail(f"unexpected harness-verify exit code {rc} (expected 0, 1, or 2)")

    try:
        with open(json_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return fail(f"cannot read {json_path}: {e}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return fail(f"malformed JSON in {json_path}: {e}")

    if not isinstance(data, dict):
        return fail(f"JSON root in {json_path} is not an object")

    for key in REQUIRED_COUNTERS:
        if key not in data:
            return fail(f"missing required counter '{key}'")
        val = data[key]
        if isinstance(val, bool):
            return fail(f"counter '{key}' is a boolean, expected an int")
        if not isinstance(val, int):
            return fail(f"counter '{key}' is not an integer: {val!r}")
        if val < 0:
            return fail(f"counter '{key}' is negative: {val}")

    errors = data["errors"]
    inconclusive = data["inconclusive"]

    if rc == 0:
        if errors != 0:
            return fail(f"rc=0 but errors={errors} (expected 0)")
        if inconclusive != 0:
            return fail(f"rc=0 but inconclusive={inconclusive} (expected 0)")
    elif rc == 1:
        # rc=1 means harness-verify reported errors; the job must fail.
        return fail(f"harness-verify reported {errors} error(s)")
    elif rc == 2:
        if errors != 0:
            return fail(f"rc=2 but errors={errors} (expected 0)")
        if inconclusive <= 0:
            return fail(f"rc=2 but inconclusive={inconclusive} (expected >0)")

    print(f"verify-result: OK (rc={rc}, errors={errors}, "
          f"inconclusive={inconclusive})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
