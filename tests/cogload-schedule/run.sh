#!/usr/bin/env bash
# Deterministic contract for the macOS close-of-day schedule and startup catch-up.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NIGHTLY="$ROOT/deploy/com.claudemaxxing.cogload-nightly.plist"
CATCHUP_PLIST="$ROOT/deploy/com.claudemaxxing.cogload-catchup.plist"
CATCHUP_SH="$ROOT/deploy/cogload-catchup.sh"
INSTALL="$ROOT/install.sh"
SETUP="$ROOT/deploy/cogload-mac-setup.sh"

pass=0
fail=0
check() {
  local label="$1"; shift
  if "$@"; then
    printf 'PASS %s\n' "$label"
    pass=$((pass + 1))
  else
    printf 'FAIL %s\n' "$label" >&2
    fail=$((fail + 1))
  fi
}

plist_contract() {
  python3 - "$NIGHTLY" "$CATCHUP_PLIST" <<'PY'
import plistlib
import sys
from pathlib import Path

nightly_path, catchup_path = map(Path, sys.argv[1:])
nightly = plistlib.loads(nightly_path.read_bytes())
assert nightly["StartCalendarInterval"] == {"Hour": 21, "Minute": 20}
assert nightly["RunAtLoad"] is False
assert nightly["ProgramArguments"] == [
    "/bin/bash", "__HOME__/.config/claudemaxxing/cogload-nightly.sh"]

catchup = plistlib.loads(catchup_path.read_bytes())
assert catchup["RunAtLoad"] is True
assert catchup["KeepAlive"] == {"SuccessfulExit": False}
assert catchup["ThrottleInterval"] == 300
assert "StartCalendarInterval" not in catchup
assert catchup["ProgramArguments"] == [
    "/bin/bash", "__HOME__/.config/claudemaxxing/cogload-catchup.sh"]
assert catchup["StandardOutPath"].endswith("/cogload/catchup.log")
assert catchup["StandardErrorPath"] == catchup["StandardOutPath"]
PY
}

catchup_boundary() {
  python3 - "$CATCHUP_SH" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
gate = text.index('COGLOAD_DIR/DISABLED')
first_call = text.index('cogload digest')
assert gate < first_call
for forbidden in ("rsync", "transcripts", "rotate", ".claude/projects"):
    assert forbidden not in text
assert text.count("cogload digest") == 1
assert text.count("cogload fleet push") == 1
PY
}

catchup_behavior() {
  local tmp fakebin log rc
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/cogload-schedule.XXXXXX")" || return 1
  fakebin="$tmp/bin"
  log="$tmp/calls"
  mkdir -p "$fakebin" "$tmp/home/.local/share/cogload"
  cat > "$fakebin/cogload" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALL_LOG"
if [[ "$*" == "fleet push" && "${FAIL_PUSH:-0}" == 1 ]]; then exit 23; fi
exit 0
SH
  chmod +x "$fakebin/cogload"

  HOME="$tmp/home" PATH="$fakebin:/usr/bin:/bin" CALL_LOG="$log" \
    bash "$CATCHUP_SH" >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
  [[ "$(cat "$log")" == $'digest\nfleet push' ]] || { rm -rf "$tmp"; return 1; }

  : > "$log"
  touch "$tmp/home/.local/share/cogload/DISABLED"
  HOME="$tmp/home" PATH="$fakebin:/usr/bin:/bin" CALL_LOG="$log" \
    bash "$CATCHUP_SH" >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
  [[ ! -s "$log" ]] || { rm -rf "$tmp"; return 1; }

  rm -f "$tmp/home/.local/share/cogload/DISABLED"
  HOME="$tmp/home" PATH="$fakebin:/usr/bin:/bin" CALL_LOG="$log" FAIL_PUSH=1 \
    bash "$CATCHUP_SH" >/dev/null 2>&1
  rc=$?
  rm -rf "$tmp"
  [[ $rc -eq 1 ]]
}

wiring_contract() {
  for token in cogload-catchup.sh com.claudemaxxing.cogload-catchup.plist; do
    grep -q "$token" "$INSTALL" || return 1
    grep -q "$token" "$SETUP" || return 1
  done
  grep -q 'launchctl.*bootstrap' "$SETUP" || return 1
  python3 - "$SETUP" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
assert text.index('fleet join --hub') < text.index('launchctl bootstrap')
PY
}

check C1_plist_schedule plist_contract
check C2_lightweight_boundary catchup_boundary
check C3_runtime_and_retry_signal catchup_behavior
check C4_install_and_setup_wiring wiring_contract

printf 'cogload-schedule: %d passed, %d failed\n' "$pass" "$fail"
(( fail == 0 ))
