#!/usr/bin/env bash
# Offline acceptance contract for .github/scripts/verify-result.py.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$ROOT/tests/ci-verify/run.py"
