#!/usr/bin/env bash
set -euo pipefail

# Build → tag → push → redeploy the orchestrator on <your-vm>'s Coolify.
# Preflighted and CREDENTIAL-GATED: it refuses (rather than half-deploys) when
# a required secret is missing, so a dry run is safe. Nothing here is secret;
# the token/URL come from the environment.
#
# Required env:
#   COOLIFY_REGISTRY     e.g. registry.example.com  (where images are pushed)
#   COOLIFY_URL          e.g. https://coolify.example.com
#   COOLIFY_TOKEN        Coolify API token (Settings → API)
#   COOLIFY_APP_UUID     the app/resource UUID to redeploy
# Optional:
#   IMAGE_TAG            default: git short SHA
#   --dry-run            build only, no push/redeploy
#
# The VM <your-vm> is on the tailnet (<tailnet-ip>); run this from a
# machine on the same tailnet.

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

cd "$(dirname "$0")/../.."   # orchestrator/
TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
LOCAL_IMAGE="hermes-orchestrator:${TAG}"

echo "→ Building ${LOCAL_IMAGE}…"
docker build -t "${LOCAL_IMAGE}" .

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "✓ dry-run: built ${LOCAL_IMAGE}; skipping push + redeploy."
  exit 0
fi

: "${COOLIFY_REGISTRY:?set COOLIFY_REGISTRY}"
: "${COOLIFY_URL:?set COOLIFY_URL}"
: "${COOLIFY_TOKEN:?set COOLIFY_TOKEN}"
: "${COOLIFY_APP_UUID:?set COOLIFY_APP_UUID}"

REMOTE_IMAGE="${COOLIFY_REGISTRY}/hermes-orchestrator:${TAG}"
echo "→ Tagging + pushing ${REMOTE_IMAGE}…"
docker tag "${LOCAL_IMAGE}" "${REMOTE_IMAGE}"
docker push "${REMOTE_IMAGE}"

echo "→ Triggering Coolify redeploy of ${COOLIFY_APP_UUID}…"
# Coolify's API can 403 a default UA; send an explicit one.
curl -fsSL -X POST \
  -H "Authorization: Bearer ${COOLIFY_TOKEN}" \
  -H "User-Agent: hermes-orchestrator-deploy/1.0" \
  -H "Content-Type: application/json" \
  "${COOLIFY_URL}/api/v1/deploy?uuid=${COOLIFY_APP_UUID}&force=true"

# Record the deployed tag so rollback.sh has a reliable "previous tag" source
# even when Coolify's deployment-history API shape varies by version.
HISTORY_FILE="${COOLIFY_DEPLOY_HISTORY:-$HOME/.config/orchestratormaxxing/coolify-deploy-history}"
mkdir -p "$(dirname "$HISTORY_FILE")"
printf '%s\t%s\n' "$(date -u +%FT%TZ)" "${TAG}" >> "$HISTORY_FILE"

echo ""
echo "✓ Pushed ${REMOTE_IMAGE} and triggered redeploy (tag ${TAG} recorded)."
echo "  Verify:   curl https://orch.example.com/healthz"
echo "            curl https://mcp.example.com/health"
echo "  Rollback: deploy/coolify/rollback.sh"
