# Open Design in the orchestratormaxxing stack (2026-08-05)

Setting up `nexu-io/open-design` so Claude, Codex and the Hermes Designer thread
can work with design artifacts, with Ollama Cloud doing the generation.

**Adopt-before-build decision: ADOPT + thin EXTEND.** Upstream's Docker image,
`deploy/docker-compose.yml`, stdio MCP server and 22 tools are used verbatim;
hosts register via their own `claude mcp add` / `codex mcp add`. Local code is
limited to routing (`bin/opendesign`), the loopback compose override, and the
refusal to invoke the name `od`.

## What runs where

| Piece | Location |
|---|---|
| Daemon | Docker `ghcr.io/nexu-io/od:latest` on **the GPU box**, `127.0.0.1:7456` |
| Data | named volume `open-design_open_design_data` → `/app/.od` |
| Compose | upstream base + `deploy/open-design.override.yml` (ours) |
| MCP | `opendesign mcp` → `docker exec -i open-design node /app/apps/daemon/dist/cli.js mcp` |
| Mac | no install; `opendesign` re-execs itself on the box over Tailscale SSH |
| Telegram | thread **15957** (`🎨 Designer`), persona via `channel_overrides`, skill `designer` |

## The four things that were not obvious

### 1. Ollama BYOK cannot reach the MCP path

This was the finding that reshaped the design. The operator's ask was "set up Open
Design with our Ollama config" *and* "have Claude and Codex interact with it" —
those are two different modes and they do not meet:

- `apps/daemon/src/mcp.ts:2391` throws on `apiKey`/`byokProvider` in tool args:
  *"raw API keys are not accepted by Open Design MCP. Configure Local BYOK in
  the Open Design UI and start that run from the local product instead."*
- The daemon **persists no provider**. BYOK config lives in browser
  localStorage (`open-design:config`, `apps/web/src/state/config.ts:26`) and is
  re-sent per request; `withoutSensitiveRunInput` strips it server-side.

So: **generation happens in the browser UI on Ollama Cloud; over MCP the agents
read and write project files.** A headless per-request injection would only ever
serve a CLI verb no MCP host calls.

### 2. `start_run` is accepted, then fails — it is not refused

Measured, after initially getting this wrong by inferring from `list_agents: []`:

```
start_run -> {"runId": "...", ...}          # ACCEPTED
get_run   -> status=failed  errorCode=AGENT_UNAVAILABLE
             error="unknown agent: undefined"  childPid=null  exitCode=1
```

No child process is ever spawned, because the image ships no agent CLIs and we
decline to mount the host's. The security property holds; the *mechanism* is
fail-at-runtime-selection, not refuse-at-call. Worth stating precisely, because
"it refuses" would be a false guarantee.

### 3. The RCE landmine sits in upstream's Linux override

`deploy/docker-compose.linux.yml` does two separable things. We take the first:

1. `network_mode: host` + `OD_BIND_HOST=127.0.0.1` → the daemon binds the **real**
   host loopback. Without it, bridge networking forces `0.0.0.0`, which upstream
   correctly refuses to start without `OD_API_TOKEN`.
2. Mounts `~/.local/bin`, `~/.opencode/bin`, `~/.local/share/claude` and
   `~/.claude` so Open Design can spawn agent CLIs. **Declined.** Those runtimes
   launch with `--permission-mode bypassPermissions` (`runtimes/defs/claude.ts:87`),
   `--dangerously-skip-permissions` (`opencode-permissions.ts:4`) and
   `sandbox_workspace_write.network_access=true` (`codex.ts:220`) — i.e. any MCP
   client that can call `start_run` gets unsandboxed execution as `ricardo` with
   real credentials.

`tests/opendesign/run.sh` C6 asserts the override declares no `volumes:` block
and references no host path. Proven red by appending `~/.claude` to a copy.

### 4. `od` collides with coreutils — inside the container too

`which od` resolves to `/usr/bin/od` (GNU octal-dump) on the host **and** in
upstream's image; `node_modules/.bin` carries no `od` link. Worse, upstream's own
`od mcp install` falls back to emitting a bare `od` whenever the daemon is
unreachable (`cli.ts:1991-1998`) — so registering while the daemon is down
silently writes a spec that execs octal-dump. Always name the script path.

## Telegram Designer thread

- The operator created topic **15957** on 2026-08-04 ("This is going to be mi
  designer"). It is a **DM in topic mode**, not a supergroup forum topic:
  `chat_id` is the operator's own user id, shared by every one of his topics.
- `ChannelOverride` carries exactly `model`, `provider`, `system_prompt`
  (`gateway/config.py:548`). Lookup order is **chat_id → thread_id → parent_id**,
  chat_id **first** (`gateway/run.py:3177`). So the override must key on
  `"15957"` alone — a bare user-id entry would hijack every topic he has.
- Capability comes from the **skill** (`skills/designer/SKILL.md`, deployed to
  `~/.hermes/skills/designer/`), not from registering Open Design's MCP server
  into Hermes. That was deliberate: `mcp_servers` is a single global block, and
  the gateway's webhook listener is bound to `0.0.0.0:8644` (verified via
  `ss -ltnp`; the dashboard and MCP-SSE are loopback by contrast). A skill adds
  competence without adding a LAN-reachable tool surface.

### `design` became a real role (m25)

`threads.role` had a CHECK of `code·growth·ops·health·personal`. m12 had recorded
*"never rebuild a CHECK under a live gateway"* — that ruling is about inventing a
parallel vocabulary to dodge the enum; here `design` **is** the real vocabulary,
and a Designer filed as `code` makes every role-sliced surface lie.
`m25_thread_role_design.py` rebuilds the table (explicit column list, row-count
equality checked before the DROP, all inside the runner's single transaction and
its verified `backup-kanban` snapshot) and registers 15957.

Two test notes:
- `test_role_must_be_one_of_the_five` → renamed to `..._of_the_enum`. A name that
  pins cardinality turns "we grew the vocabulary" into "a test broke".
- `test_m02_spine::test_threads_seed` asserted an **exact** table total, which
  pinned "no later migration ever adds a thread" — merely true, never required.
  Relaxed to `assertGreaterEqual`; the per-thread assertions are the real
  guarantee. That file has two **pre-existing** failures unrelated to this work
  (`THREADS_COLUMNS` lacks `station` since m12; the seed test looks for a thread
  named `"Hoy"` which m12 renamed to `"📅 Hoy"`).

## Operating it

```bash
opendesign status          # where/up?/URL          (read-only)
opendesign up              # idempotent
opendesign tunnel          # from the Mac: UI on local :7456
open http://127.0.0.1:7456 # enter the Ollama BYOK key ONCE in Settings
```

Bundled on the daemon: **151 design systems** in 22 categories and **162 skills**
(`/api/design-systems`, `/api/skills`) — that catalogue is the Designer's real
vocabulary, and the skill teaches it to propose from it by name.

## Known ceilings

- **Vision:** native image blocks are built for the Anthropic protocol only, and
  the Ollama BYOK runtime does not declare image support — an attached rendering
  degrades to a filename-only text line. If the model must actually *see* a
  design, that needs an Anthropic-protocol BYOK provider.
- **Tool frontier:** the MCP server is registered at **user scope**, so all 22
  tools load in every Claude session. Deliberate (the operator wants design reachable
  everywhere) but it is a real cost against the minimal-frontier doctrine.
  Narrow with `claude mcp remove open-design -s user` + a project-scoped add.
- **`delete_file`** takes no `confirm` argument (unlike `delete_project`), and
  `codex mcp add` has no tool allow/deny option at all — so Codex sessions see
  both destructive tools unfiltered. Not mitigated in config; mitigated only by
  the data being a scratch design store, not a source of truth.
