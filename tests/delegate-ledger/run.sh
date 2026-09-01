#!/usr/bin/env bash
# Hermetic delegation-ledger contract: fixture store dir, fixture clock,
# fixture receipts carrying the REAL observed schema drift. No network, no LLM.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$ROOT/tests/delegate-ledger/run.py"
