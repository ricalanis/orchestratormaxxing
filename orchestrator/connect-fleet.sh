#!/usr/bin/env bash
# connect-fleet.sh — connect an external Claude Code fleet to the Hermes
# orchestrator MCP server with LEAST-AUTHORITY (default) scope.
#
# The default scope can orient (read the plan), pull work from the pool, report
# progress/results, and declare new tasks — but it CANNOT restructure the plan
# (dispatch other agents, edit the roadmap/sprints, or change trust grades).
# That orchestration surface is a separate privileged scope the operator grants
# explicitly (see --privileged below). This is the PRD §8 safety model.
#
# Usage:
#   ./connect-fleet.sh                 # add with default (least-authority) scope
#   ./connect-fleet.sh --privileged    # operator only — requires a matching token
#   ./connect-fleet.sh --name myfleet  # override the MCP server name
#   ./connect-fleet.sh --show          # print the manifest and exit (no changes)
#   ./connect-fleet.sh --print         # print the `claude mcp add` command only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PY="${SCRIPT_DIR}/mcp_server.py"
NAME="hermes-orchestrator"
SCOPE="default"
ACTION="add"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --privileged) SCOPE="privileged"; shift ;;
    --name) NAME="$2"; shift 2 ;;
    --show) ACTION="show"; shift ;;
    --print) ACTION="print"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$SERVER_PY" ]]; then
  echo "error: mcp_server.py not found at $SERVER_PY" >&2
  exit 1
fi

PY="python3"
# Prefer the orchestrator venv if present (has the sqlite-only deps the loop needs).
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
  PY="${SCRIPT_DIR}/.venv/bin/python"
fi

# Environment the MCP server reads to decide its scope. Default scope needs
# nothing; privileged requires BOTH the opt-in AND a matching token, or the
# server falls back to default (fail-safe).
declare -a ENV_ARGS=()
if [[ "$SCOPE" == "privileged" ]]; then
  ENV_ARGS+=(-e HERMES_MCP_SCOPE=privileged)
  if [[ -n "${HERMES_MCP_TOKEN:-}" ]]; then
    ENV_ARGS+=(-e "HERMES_MCP_TOKEN=${HERMES_MCP_TOKEN}")
  else
    echo "note: --privileged set but HERMES_MCP_TOKEN is not exported." >&2
    echo "      The server grants privileged scope only if the presented token" >&2
    echo "      matches ~/.config/orchestratormaxxing/mcp-privileged-token (or the" >&2
    echo "      HERMES_MCP_PRIVILEGED_TOKEN env). Otherwise it falls back to default." >&2
  fi
fi

MCP_CMD=(claude mcp add "$NAME" "${ENV_ARGS[@]}" -- "$PY" "$SERVER_PY")

case "$ACTION" in
  print)
    printf '%q ' "${MCP_CMD[@]}"; echo
    exit 0 ;;
  show)
    echo "Fetching the connection manifest from the dashboard…"
    curl -sf "http://127.0.0.1:5555/api/mcp/manifest" \
      || { [ -n "${ORCH_URL:-}" ] && curl -skf "${ORCH_URL}/api/mcp/manifest"; } \
      || { echo "dashboard not reachable — is hermes-dashboard running?" >&2; exit 1; }
    echo
    exit 0 ;;
  add)
    echo "Connecting fleet → '${NAME}'  (scope: ${SCOPE})"
    if command -v claude >/dev/null 2>&1; then
      "${MCP_CMD[@]}"
      echo "✅ Added. Verify with:  claude mcp list"
      echo "   Least-authority scope: orient + pull + report + declare."
      [[ "$SCOPE" == "default" ]] && echo "   (Plan-restructuring tools are hidden — grant with --privileged as operator.)"
    else
      echo "claude CLI not found. Run this on the machine to connect, or add manually:" >&2
      printf '  %q ' "${MCP_CMD[@]}"; echo
      exit 1
    fi
    ;;
esac
