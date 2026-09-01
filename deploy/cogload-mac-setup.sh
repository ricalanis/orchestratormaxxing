#!/usr/bin/env bash
# cogload — one-command macOS rollout. Idempotent; safe to re-run.
#
# MUST BE RUN IN Terminal.app ON THE MAC, not over SSH. macOS attaches TCC
# grants (Accessibility / Input Monitoring) to the process that asks, and an
# SSH session cannot raise that prompt. This is macOS by design, not a gap here
# — it is the one step in the whole harness that cannot be automated.
set -uo pipefail

# The fleet hub is tenant identity: COGLOAD_HUB env, else CLAUDEMAXXING_SERVER_SSH from
# ~/.config/claudemaxxing/fleet.env (env > fleet.env > empty). No built-in default.
_fleet_env="${CLAUDEMAXXING_FLEET_ENV:-$HOME/.config/claudemaxxing/fleet.env}"
_fleet_val(){ [ -f "$_fleet_env" ] && awk -F= -v k="$1" '$0 !~ /^[[:space:]]*#/ && $1 ~ ("^(export[[:space:]]+)?" k "$") {v=$2; gsub(/^["'"'"']|["'"'"']$/, "", v); print v; exit}' "$_fleet_env"; }
HUB="${COGLOAD_HUB:-${CLAUDEMAXXING_SERVER_SSH:-$(_fleet_val CLAUDEMAXXING_SERVER_SSH)}}"
if [ -z "$HUB" ]; then
  echo "cogload-mac-setup: no fleet hub configured (set COGLOAD_HUB or CLAUDEMAXXING_SERVER_SSH in $_fleet_env)" >&2
  exit 2
fi
STORE="$HOME/.local/share/cogload"
VENV="$STORE/venv"
BIN="$HOME/.local/bin/cogload"
PLIST="$HOME/Library/LaunchAgents/com.claudemaxxing.cogload-keys.plist"
NIGHTLY_PLIST="$HOME/Library/LaunchAgents/com.claudemaxxing.cogload-nightly.plist"
CATCHUP_PLIST="$HOME/Library/LaunchAgents/com.claudemaxxing.cogload-catchup.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[1;31m✗\033[0m %s\n' "$*"; }

if [ "$(uname -s)" != "Darwin" ]; then
  bad "this is the macOS rollout; run it on the Mac."; exit 1
fi
if [ -n "${SSH_CONNECTION:-}" ]; then
  bad "you are over SSH. TCC cannot prompt here — run this in Terminal.app on the Mac."
  exit 1
fi

say "1/7  tool"
mkdir -p "$HOME/.local/bin" "$STORE"
chmod 700 "$STORE"
install -m 0755 "$REPO/bin/cogload" "$BIN"
ok "$BIN"

say "2/7  venv + pynput"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check pynput >/dev/null 2>&1
if "$VENV/bin/python3" -c "import pynput" 2>/dev/null; then ok "pynput ready"
else bad "pynput failed to install"; exit 1; fi

say "3/7  device identity"
"$BIN" fleet id | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  id:",d["id"]); print("  channels:",d["channels"])'
echo "  screen/light are false on macOS BY DESIGN: the window-list API can pull"
echo "  window TITLES into the process, and not-reading-them is a promise, not a"
echo "  guarantee. Keys and mouse are collected honestly instead."

# Capability is a fact about the running system, not about the install date:
# it used to be frozen at device creation keyed only on is_mac, so a machine
# that changed display stack kept claiming channels it could no longer deliver
# and every one of its days was damned for the wrong reason. Idempotent.
"$BIN" channels --redeclare || true

say "4/7  collector launch agent"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__HOME__|$HOME|g" "$REPO/deploy/com.claudemaxxing.cogload-keys.plist" > "$PLIST"
ok "$PLIST"

say "5/7  join the fleet (digest rows only, ~1 KB/day over Tailscale)"
"$BIN" fleet join --hub "$HUB"
if "$BIN" fleet push --dry-run >/dev/null 2>&1; then ok "hub reachable: $HUB"
else bad "hub not reachable yet — fix SSH to $HUB, then: cogload fleet push"; fi

say "6/7  close-of-day + login catch-up"
mkdir -p "$HOME/.config/claudemaxxing"
install -m 0755 "$REPO/deploy/cogload-nightly.sh" \
  "$HOME/.config/claudemaxxing/cogload-nightly.sh"
install -m 0755 "$REPO/deploy/cogload-catchup.sh" \
  "$HOME/.config/claudemaxxing/cogload-catchup.sh"
sed "s|__HOME__|$HOME|g" "$REPO/deploy/com.claudemaxxing.cogload-nightly.plist" \
  > "$NIGHTLY_PLIST"
sed "s|__HOME__|$HOME|g" "$REPO/deploy/com.claudemaxxing.cogload-catchup.plist" \
  > "$CATCHUP_PLIST"
for job in nightly catchup; do
  label="com.claudemaxxing.cogload-$job"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/$UID" "$plist"; then ok "$label armed"
  else bad "$label failed to arm"; exit 1; fi
done

say "7/7  PERMISSIONS — this part is yours"
cat <<'EOT'
  macOS will now ask for two grants. Both are required for keystroke COUNTS
  (never which keys — that invariant is in the code, not in the prompt):

    System Settings → Privacy & Security → Input Monitoring
    System Settings → Privacy & Security → Accessibility

  Add and enable this exact application in BOTH:
EOT
echo "      /Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app"
cat <<'EOT'

  Then come back and run:

      cogload on && sleep 70 && cogload selftest && cogload status

  selftest injects three keys and asserts they were observed. If the grants did
  not take, it FAILS LOUDLY rather than letting the collector write zeros —
  a silent zero would read as a calm day, which is the one failure this whole
  system exists to prevent.
EOT

open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent" 2>/dev/null || true
sleep 1
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true

say "setup complete — grant the two permissions above, then run the line in step 7"
