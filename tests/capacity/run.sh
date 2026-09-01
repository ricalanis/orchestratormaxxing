#!/usr/bin/env bash
# Hermetic rate-limit contract: loopback Hermes fixture + fake Codex app-server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$ROOT/tests/capacity/run.py"
