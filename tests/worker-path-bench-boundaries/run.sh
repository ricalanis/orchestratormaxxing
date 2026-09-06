#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
python3 "$ROOT/tests/worker-path-bench-boundaries/protocol.py" "$ROOT"
python3 "$ROOT/tests/worker-path-bench-boundaries/process.py" "$ROOT"
