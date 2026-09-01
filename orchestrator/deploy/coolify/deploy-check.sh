#!/usr/bin/env bash
set -uo pipefail   # deliberately NOT -e: run every gate, then aggregate.

# deploy-check.sh — post-deploy verification against a DEPLOYED instance over
# its public URL. Three gate categories, all credential-free (just curls the
# public surface), so anyone can run it after a deploy or rollback:
#   HEALTH    — /healthz deps up, mcp-sse alive
#   CONTRACT  — the API + metrics endpoints answer with the expected shape
#   SECURITY  — the containerized mcp-sse enforces auth (NOT dev-mode-open)
#
# Config (env):
#   ORCH_URL   dashboard base   (default: $COOLIFY_URL, else https://orch.example.com)
#   MCP_URL    mcp-sse base     (default: https://mcp.example.com)
#
# Exit 0 = all gates pass; 1 = one or more failed (so `make deploy-check` and
# CI fail loudly). Invoked by `make deploy-check`.

ORCH_URL="${ORCH_URL:-${COOLIFY_URL:-https://orch.example.com}}"
MCP_URL="${MCP_URL:-https://mcp.example.com}"

FAILS=0
pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; FAILS=$((FAILS + 1)); }
code() { curl -s -o /dev/null -w '%{http_code}' -m 10 "$@" 2>/dev/null; }  # prints 000 on connect failure
body() { curl -s -m 10 "$@" 2>/dev/null; }
field() { grep -o "\"$1\":\"[^\"]*\"" | head -1 | cut -d'"' -f4; }

echo "deploy-check → dashboard ${ORCH_URL} · mcp-sse ${MCP_URL}"
echo ""

echo "== HEALTH =="
c=$(code "${ORCH_URL}/healthz")
if [ "$c" = 200 ]; then
  st=$(body "${ORCH_URL}/healthz" | field status)
  [ "$st" = ok ] && pass "dashboard /healthz ok" || fail "dashboard degraded (status=${st:-?})"
else
  fail "dashboard /healthz → ${c} (expected 200)"
fi
c=$(code "${MCP_URL}/health")
[ "$c" = 200 ] && pass "mcp-sse /health 200" || fail "mcp-sse /health → ${c} (expected 200)"

echo "== CONTRACT =="
body "${ORCH_URL}/metrics" | grep -q hermes_api_requests_total \
  && pass "dashboard /metrics scrapeable" || fail "dashboard /metrics missing hermes_api_requests_total"
body "${ORCH_URL}/api/tasks" | grep -q '"tasks"' \
  && pass "dashboard /api/tasks returns JSON contract" || fail "dashboard /api/tasks bad shape"
body "${MCP_URL}/metrics" | grep -q hermes_mcp_sse_ \
  && pass "mcp-sse /metrics scrapeable" || fail "mcp-sse /metrics missing hermes_mcp_sse_ series"

echo "== CONTAINER SECURITY =="
auth=$(body "${MCP_URL}/health" | field auth)
[ "$auth" = bearer ] \
  && pass "mcp-sse auth configured (bearer)" \
  || fail "mcp-sse auth=${auth:-?} — running OPEN in production (set HERMES_MCP_SSE_TOKEN)"
c=$(code "${MCP_URL}/sse")
[ "$c" = 401 ] && pass "mcp-sse /sse rejects tokenless request (401)" \
  || fail "mcp-sse /sse → ${c} (expected 401 — auth not enforced?)"

echo ""
if [ "$FAILS" = 0 ]; then
  echo "✓ deploy-check PASSED — all gates green."
else
  echo "✗ deploy-check FAILED — ${FAILS} gate(s) failed. Consider deploy/coolify/rollback.sh"
  exit 1
fi
