#!/usr/bin/env bash
# Deterministic contract for deploy/cogload-nightly.sh.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NIGHTLY="$ROOT/deploy/cogload-nightly.sh"

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

n1_kill_switch_ordering() {
  python3 - "$NIGHTLY" <<'PY'
import sys
import re
from pathlib import Path

text = Path(sys.argv[1]).read_text()
# Locate the executable kill-switch gate, not its mention in comments.
m = re.search(r'if \[\[ -f "\$COGLOAD_DIR/DISABLED" \]\]; then', text)
assert m, "executable kill-switch gate not found"
gate = m.start()
first_digest = text.index('run_step "digest" cogload digest')
first_rsync = text.index('run_step "rsync-mirror"')
assert gate < first_digest
assert gate < first_rsync
PY
}

n2_symlink_vs_directory() {
  local tmp fakebin log
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/cogload-nightly.XXXXXX")" || return 1
  fakebin="$tmp/bin"
  log="$tmp/calls"
  mkdir -p "$fakebin"
  mkdir -p "$tmp/home/.claude/projects/slug"
  mkdir -p "$tmp/home/.local/share/cogload/transcripts-mirror/slug/memory"
  printf 'some transcript bytes\n' > "$tmp/home/.claude/projects/slug/x.jsonl"
  ln -s "$tmp/home/.local/share/cogload/transcripts-mirror/slug/memory" \
    "$tmp/home/.claude/projects/slug/memory"
  printf 'old mirrored memory content\n' > "$tmp/home/.local/share/cogload/transcripts-mirror/slug/memory/old.md"

  cat > "$fakebin/cogload" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALL_LOG"
exit 0
SH
  chmod +x "$fakebin/cogload"

  HOME="$tmp/home" PATH="$fakebin:/usr/bin:/bin" CALL_LOG="$log" \
    bash "$NIGHTLY" >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }

  [[ -f "$tmp/home/.local/share/cogload/transcripts-mirror/slug/x.jsonl" ]] || { rm -rf "$tmp"; return 1; }
  [[ -d "$tmp/home/.local/share/cogload/transcripts-mirror/slug/memory" ]] || { rm -rf "$tmp"; return 1; }
  [[ -f "$tmp/home/.local/share/cogload/transcripts-mirror/slug/memory/old.md" ]] || { rm -rf "$tmp"; return 1; }

  rm -rf "$tmp"
}

n3_step_order() {
  local tmp fakebin log
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/cogload-nightly.XXXXXX")" || return 1
  fakebin="$tmp/bin"
  log="$tmp/calls"
  mkdir -p "$fakebin" "$tmp/home/.claude/projects/slug" "$tmp/home/.local/share/cogload/transcripts-mirror"
  printf 'data\n' > "$tmp/home/.claude/projects/slug/x.jsonl"

  cat > "$fakebin/cogload" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALL_LOG"
exit 0
SH
  chmod +x "$fakebin/cogload"

  HOME="$tmp/home" PATH="$fakebin:/usr/bin:/bin" CALL_LOG="$log" \
    bash "$NIGHTLY" >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }

  python3 - "$log" <<'PY'
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text().splitlines()
idx = {line: lines.index(line) for line in lines}
required = ['digest', 'transcripts --since 45', 'rotate', 'fleet push']
for line in required:
    assert line in idx, f"missing call: {line}"
assert idx['digest'] < idx['transcripts --since 45']
assert idx['transcripts --since 45'] < idx['rotate']
assert idx['rotate'] < idx['fleet push']
PY
  rm -rf "$tmp"
}

n4_kill_switch_live() {
  local tmp fakebin log
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/cogload-nightly.XXXXXX")" || return 1
  fakebin="$tmp/bin"
  log="$tmp/calls"
  mkdir -p "$fakebin" "$tmp/home/.local/share/cogload"

  cat > "$fakebin/cogload" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALL_LOG"
exit 0
SH
  chmod +x "$fakebin/cogload"

  touch "$tmp/home/.local/share/cogload/DISABLED"
  HOME="$tmp/home" PATH="$fakebin:/usr/bin:/bin" CALL_LOG="$log" \
    bash "$NIGHTLY" >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
  [[ ! -s "$log" ]] || { rm -rf "$tmp"; return 1; }

  rm -rf "$tmp"
}

check N1 n1_kill_switch_ordering
check N2 n2_symlink_vs_directory
check N3 n3_step_order
check N4 n4_kill_switch_live

printf 'cogload-nightly: %d passed, %d failed\n' "$pass" "$fail"
(( fail == 0 ))
