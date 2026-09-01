#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/memory-bridge-hermes.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/memory-bridge-hermes.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

SOURCE="$TMP/shared"
HERMES="$TMP/.hermes/memories"
mkdir -p "$SOURCE" "$HERMES"
printf 'Hermes-native fact.\n' > "$HERMES/MEMORY.md"
printf 'Hermes user profile sentinel.\n' > "$HERMES/USER.md"

write_fact() {
  local name="$1" description="$2" type="$3" verified="$4" sensitivity="$5" status="$6"
  mkdir -p "$SOURCE"
  printf '%s\n' \
    '---' \
    "name: $name" \
    "description: \"$description\"" \
    'metadata:' \
    "  type: $type" \
    "  last_verified: $verified" \
    "  status: $status" \
    "  sensitivity: $sensitivity" \
    '---' \
    'Fixture body.' > "$SOURCE/$name.md"
  printf -- '- [%s](%s.md) — %s\n' "$name" "$name" "$description" >> "$SOURCE/MEMORY.md"
}

printf '# Shared project memory\n\n' > "$SOURCE/MEMORY.md"
write_fact provider-routing 'Provider routing uses Ollama and OpenAI.' project 2026-08-09 normal active
write_fact provider-old 'Provider routing fact that is stale.' project 2026-07-01 normal active
write_fact dashboard-endpoint 'Dashboard endpoint implementation detail.' project 2026-08-09 normal active
write_fact private-tool-config 'Tool config contains private context.' project 2026-08-09 sensitive active
write_fact user-routing 'User routing preference.' user 2026-08-09 normal active
write_fact inactive-service 'Old service state.' project 2026-08-09 normal superseded
write_fact unrelated-theory 'Abstract decision theory result.' reference 2026-08-09 normal active

ARGS=(--source-dir "$SOURCE" --memory-file "$HERMES/MEMORY.md" --today 2026-08-09 --log-file "$TMP/bridge.log")

# C1/C2: select only fresh operational project facts; preserve native MEMORY and all USER bytes.
user_before="$(sha256sum "$HERMES/USER.md")"
"$TOOL" "${ARGS[@]}" > "$TMP/first.out"
grep -q '^Hermes-native fact.$' "$HERMES/MEMORY.md"
grep -q '^\[claudemaxxing\] \[provider-routing\] Provider routing uses Ollama and OpenAI\.$' "$HERMES/MEMORY.md"
grep -q '^§$' "$HERMES/MEMORY.md"
! grep -qE 'provider-old|dashboard-endpoint|private-tool-config|user-routing|inactive-service|unrelated-theory' "$HERMES/MEMORY.md"
[ "$user_before" = "$(sha256sum "$HERMES/USER.md")" ]
grep -q 'added=1 updated=0 removed=0' "$TMP/bridge.log"

# C3: a second run is byte-idempotent; changing a source description updates, not duplicates.
memory_before="$(sha256sum "$HERMES/MEMORY.md")"
"$TOOL" "${ARGS[@]}" > "$TMP/second.out"
[ "$memory_before" = "$(sha256sum "$HERMES/MEMORY.md")" ]
[ "$(grep -c '\[claudemaxxing\] \[provider-routing\]' "$HERMES/MEMORY.md")" -eq 1 ]
python3 - "$SOURCE/MEMORY.md" "$SOURCE/provider-routing.md" <<'PY'
from pathlib import Path
import sys
for raw in sys.argv[1:]:
    path = Path(raw)
    path.write_text(path.read_text().replace(
        "Provider routing uses Ollama and OpenAI.",
        "Provider routing now uses OpenAI and Ollama."))
PY
"$TOOL" "${ARGS[@]}" > "$TMP/update.out"
grep -q 'updated=1' "$TMP/update.out"
[ "$(grep -c '\[claudemaxxing\] \[provider-routing\]' "$HERMES/MEMORY.md")" -eq 1 ]
grep -q 'Provider routing now uses OpenAI and Ollama.' "$HERMES/MEMORY.md"

# C4/C5: dry-run never mutates; the hard character budget omits managed entries.
memory_before="$(sha256sum "$HERMES/MEMORY.md")"
"$TOOL" "${ARGS[@]}" --dry-run --max-chars 25 > "$TMP/dry.out"
[ "$memory_before" = "$(sha256sum "$HERMES/MEMORY.md")" ]
grep -q 'dry-run synced' "$TMP/dry.out"
grep -q 'omitted_at_capacity=1' "$TMP/dry.out"

printf 'memory-bridge-hermes contract: ok\n'
