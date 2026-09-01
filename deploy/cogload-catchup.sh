#!/usr/bin/env bash
# Lightweight macOS login catch-up. launchd retries this script after a
# non-zero exit, so transient network failures heal without waiting a day.
set -uo pipefail

COGLOAD_DIR="${COGLOAD_DIR:-$HOME/.local/share/cogload}"

# Kill switch first: disabled means no reads, writes, or fleet communication.
if [[ -f "$COGLOAD_DIR/DISABLED" ]]; then
  echo "cogload-catchup: kill switch on — skipping."
  exit 0
fi

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

rc=0
if cogload digest; then
  echo "cogload-catchup: digest OK"
else
  echo "cogload-catchup: digest FAILED" >&2
  rc=1
fi

if cogload fleet push; then
  echo "cogload-catchup: fleet-push OK"
else
  echo "cogload-catchup: fleet-push FAILED" >&2
  rc=1
fi

exit "$rc"
