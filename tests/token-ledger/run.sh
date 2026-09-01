#!/usr/bin/env bash
# Hermetic token-audit + loop-portfolio contract: fixture $HOME, fixture repo,
# fixture systemd user units. No network, no real systemd, no real transcripts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$ROOT/tests/token-ledger/run.py"
