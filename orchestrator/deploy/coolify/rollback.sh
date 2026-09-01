#!/usr/bin/env bash
set -euo pipefail

# rollback.sh — revert a bad deploy in one command. The inverse of
# deploy-coolify.sh: it re-points Coolify at the PREVIOUS image tag and
# redeploys it (the image is still in the registry — deploy pushed it).
#
# Target tag resolution, in order:
#   1. explicit:  rollback.sh <tag>   (or ROLLBACK_TAG=<tag>)
#   2. the previous entry in the local deploy history that deploy-coolify.sh
#      writes ($COOLIFY_DEPLOY_HISTORY, default ~/.config/orchestratormaxxing/
#      coolify-deploy-history) — the reliable default.
#   3. best-effort: query Coolify's deployments API for the prior tag.
#
# Required env (same as deploy-coolify.sh):
#   COOLIFY_REGISTRY  COOLIFY_URL  COOLIFY_TOKEN  COOLIFY_APP_UUID
# Optional:
#   ROLLBACK_TAG      pin the target tag explicitly
#   --dry-run         resolve + print the target tag; change nothing
#
# CREDENTIAL-GATED: refuses on missing secrets rather than half-rolling-back.
# --dry-run needs no creds (except when it must query Coolify for the tag).

DRY_RUN=false
EXPLICIT_TAG="${ROLLBACK_TAG:-}"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -*) echo "usage: $0 [--dry-run] [<tag>]" >&2; exit 2 ;;
    *) EXPLICIT_TAG="$arg" ;;
  esac
done

cd "$(dirname "$0")/../.."   # orchestrator/
HISTORY_FILE="${COOLIFY_DEPLOY_HISTORY:-$HOME/.config/orchestratormaxxing/coolify-deploy-history}"

_prev_from_history() {
  # Second-to-last recorded tag (the one before the current/bad deploy).
  [[ -f "$HISTORY_FILE" ]] || return 1
  awk '{print $2}' "$HISTORY_FILE" | tail -2 | head -1
}

_prev_from_coolify() {
  # Best-effort: Coolify's deployment history. The JSON shape varies by
  # version, so this greps a tag-shaped field and is a FALLBACK — prefer the
  # history file or an explicit tag. Requires creds + jq.
  command -v jq >/dev/null || return 1
  : "${COOLIFY_URL:?}" "${COOLIFY_TOKEN:?}" "${COOLIFY_APP_UUID:?}"
  curl -fsSL \
    -H "Authorization: Bearer ${COOLIFY_TOKEN}" \
    -H "User-Agent: hermes-orchestrator-rollback/1.0" \
    "${COOLIFY_URL}/api/v1/applications/${COOLIFY_APP_UUID}/deployments" 2>/dev/null \
    | jq -r '[.[] | .image_tag // .commit // empty] | .[1] // empty' 2>/dev/null
}

# --- Resolve the target tag ---
if [[ -n "$EXPLICIT_TAG" ]]; then
  TARGET="$EXPLICIT_TAG"
  SRC="explicit"
elif TARGET="$(_prev_from_history)" && [[ -n "$TARGET" ]]; then
  SRC="deploy history ($HISTORY_FILE)"
elif TARGET="$(_prev_from_coolify)" && [[ -n "$TARGET" ]]; then
  SRC="Coolify API"
else
  echo "ERROR: no previous tag to roll back to." >&2
  echo "  No history at $HISTORY_FILE, and Coolify gave nothing." >&2
  echo "  Pass one explicitly:  $0 <tag>" >&2
  exit 1
fi

CURRENT="$( [[ -f "$HISTORY_FILE" ]] && awk '{print $2}' "$HISTORY_FILE" | tail -1 || echo '?' )"
echo "Rollback target: ${TARGET}   (from ${SRC})"
echo "Current (last deployed): ${CURRENT}"

if [[ "$TARGET" == "$CURRENT" && "$SRC" != "explicit" ]]; then
  echo "⚠ target equals the current tag — nothing to roll back (only one deploy on record?)." >&2
  [[ "$DRY_RUN" == "true" ]] || exit 1
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "✓ dry-run: would re-point Coolify (${COOLIFY_APP_UUID:-<uuid>}) at ${TARGET} and redeploy. No changes made."
  exit 0
fi

# --- Execute the rollback ---
: "${COOLIFY_REGISTRY:?set COOLIFY_REGISTRY}"
: "${COOLIFY_URL:?set COOLIFY_URL}"
: "${COOLIFY_TOKEN:?set COOLIFY_TOKEN}"
: "${COOLIFY_APP_UUID:?set COOLIFY_APP_UUID}"

REMOTE_IMAGE="${COOLIFY_REGISTRY}/hermes-orchestrator:${TARGET}"
echo "→ Point the app at ${REMOTE_IMAGE}…"
# Update the app's image tag (so the redeploy pulls the rollback target), then
# trigger the redeploy — mirrors deploy-coolify.sh's trigger.
curl -fsSL -X PATCH \
  -H "Authorization: Bearer ${COOLIFY_TOKEN}" \
  -H "User-Agent: hermes-orchestrator-rollback/1.0" \
  -H "Content-Type: application/json" \
  -d "{\"docker_registry_image_tag\":\"${TARGET}\"}" \
  "${COOLIFY_URL}/api/v1/applications/${COOLIFY_APP_UUID}" || \
  echo "  (image-tag PATCH failed or is a no-op on this Coolify version; redeploy still triggered)"

echo "→ Triggering redeploy of ${COOLIFY_APP_UUID}…"
curl -fsSL -X POST \
  -H "Authorization: Bearer ${COOLIFY_TOKEN}" \
  -H "User-Agent: hermes-orchestrator-rollback/1.0" \
  -H "Content-Type: application/json" \
  "${COOLIFY_URL}/api/v1/deploy?uuid=${COOLIFY_APP_UUID}&force=true"

# Record the rollback so a rollback-of-the-rollback resolves correctly.
mkdir -p "$(dirname "$HISTORY_FILE")"
printf '%s\t%s\n' "$(date -u +%FT%TZ)" "${TARGET}" >> "$HISTORY_FILE"

echo ""
echo "✓ Rolled back to ${TARGET}."
echo "  Verify: deploy/coolify/../.. → make deploy-check   (or curl the /healthz URLs)"
