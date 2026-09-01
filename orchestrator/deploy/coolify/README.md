# Coolify deploy — orchestrator on <your-vm>

Two services (dashboard + MCP SSE) off one image, on the GCP VM
`<your-vm>` (Tailscale `<tailnet-ip>`, Docker 24, Coolify + Traefik).

## One-time Coolify setup (console, not scriptable)

1. **New resource → Docker Compose**, point at this repo,
   compose file `orchestrator/deploy/coolify/docker-compose.yml`.
2. **Env vars** (Coolify UI — never commit these):
   - `HERMES_MCP_SSE_TOKEN` — a strong random token; MCP clients send it as
     `Authorization: Bearer …`.
   - `HERMES_MCP_SCOPE` — `default` (or `privileged` + its token for the
     operator surface).
   - `HERMES_MCP_CORS_ORIGINS` — `https://orch.example.com`.
3. **Domains / Traefik**:
   - `dashboard` service → `orch.example.com`
   - `mcp-sse` service → `mcp.example.com`
   - Enable Coolify's automatic HTTPS (Let's Encrypt) on both.
4. **Tailnet-only bind**: set the Traefik router / proxy bind address to the
   Tailscale IP (`<tailnet-ip>`), NOT `0.0.0.0` — these hosts must not answer
   on the public interface. (Coolify: resource → Network → bind address.)
5. **DB seed**: the compose uses a named volume `orchestrator-db` at
   `/data/orchestrator.db`. Seed it once from an existing kanban.db, or let a
   fresh DB be created by the Hermes CLI on first write.

## Deploy

From a tailnet machine, with the registry + Coolify API creds in the
environment (see `deploy-coolify.sh` header):

```bash
export COOLIFY_REGISTRY=registry.example.com
export COOLIFY_URL=https://coolify.example.com
export COOLIFY_TOKEN=…            # Coolify → Settings → API
export COOLIFY_APP_UUID=…         # the resource UUID
deploy/coolify/deploy-coolify.sh          # build → push → redeploy
deploy/coolify/deploy-coolify.sh --dry-run   # build only (safe)
```

Each deploy records its tag to `$COOLIFY_DEPLOY_HISTORY`
(default `~/.config/orchestratormaxxing/coolify-deploy-history`).

## Rollback

Revert a bad deploy to the previous image tag (still in the registry):

```bash
deploy/coolify/rollback.sh              # → the previous recorded tag
deploy/coolify/rollback.sh --dry-run    # resolve + print the target, no changes
deploy/coolify/rollback.sh <tag>        # roll back to a specific tag
```

Target resolution: explicit `<tag>` / `ROLLBACK_TAG` → previous entry in the
deploy history → best-effort Coolify deployments API.

## Verify

```bash
curl https://orch.example.com/healthz     # dashboard deps
curl https://mcp.example.com/health       # SSE liveness + auth state
# authed MCP call:
curl -H "Authorization: Bearer $HERMES_MCP_SSE_TOKEN" https://mcp.example.com/sse
```

Scope/rate-limit/auth all carry over from the systemd service — same env
vars, same defaults.
