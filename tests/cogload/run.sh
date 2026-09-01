#!/usr/bin/env bash
# Acceptance contract for bin/cogload.
#
# Offline and hermetic: every check runs against an injected event source in a
# temp store, so this never touches the live collector or its data.
#
# The one check that must cross a real boundary is deliberately NOT here:
#   cogload selftest      (injects keys through the real X server)
# Run that manually after arming, and after any display-stack change. A fully
# mocked suite is a statement about the mocks, not about the system.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# Prefer the collector's own venv (it has the deps); fall back to system python
# since the contract itself needs no third-party packages.
PY="$HOME/.local/share/cogload/venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

exec "$PY" "$HERE/test_contract.py"
