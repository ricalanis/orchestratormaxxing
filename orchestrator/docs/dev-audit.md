# Development Audit

## Summary
Audited the Hermes dashboard (api.py ~2,880 lines / 185 async routes, crm.py, growth.py, 10.5k-line index.html) against the live server (127.0.0.1:3000) and kanban.db. Suite collects cleanly (385 tests). Core structure is sound and the previously-reported `os.path.expandpath` bug is **confirmed gone**. Real risks cluster in governance (93 unauthenticated mutating routes, an advertised-but-unenforced MCP token), two genuinely missing edit endpoints, one broken cron, and page-weight/blocking-I/O perf. Several prompt-flagged "404 endpoints" turned out to be wrong probe paths, not bugs (see note under P0-3).

## Issues

### P0: 93 mutating endpoints have no application-level auth; advertised MCP token is never enforced
**Severity:** P0
**Location:** `dashboard/api.py:1182` (manifest advertises the guard), `dashboard/api.py:190` (bind), plus 93 routes e.g. `dashboard/api.py:795` `DELETE /api/tasks/{id}`, `dashboard/api.py:1042` `DELETE /api/memory`, `dashboard/api.py:2556` `DELETE /api/growth/products/{id}`
**Problem:** `grep -cE "@app\.(post|patch|delete|put)"` = 93 mutating routes. None use a `Depends(...)` auth guard — there is no auth dependency anywhere in api.py. The MCP manifest string advertises `"operator-only — HERMES_MCP_SCOPE=privileged + matching HERMES_MCP_TOKEN"`, but `HERMES_MCP_TOKEN`/`HERMES_MCP_SCOPE` appear **only inside that description literal** — no code reads or checks them in api.py. Sole protection is the network bind to the Tailscale IP (`DASHBOARD_BIND` default the machine's Tailscale IP, not `0.0.0.0`).
**Impact:** Anyone on the tailnet (or anything that can reach the bind IP) can delete tasks, wipe memory, delete products/deals with an unauthenticated `curl`. The manifest gives a false sense that a privileged token gates destructive tools; it does not. Defense-in-depth is a single network ACL.
**Fix:** Add a FastAPI dependency that checks a shared secret (e.g. `X-Hermes-Token` vs `HERMES_MCP_TOKEN`) on all `POST/PATCH/PUT/DELETE` routes, or at minimum on the destructive set (`/api/memory`, `/api/tasks`, `/api/sprints`, `/api/growth/*` deletes). Actually enforce the `privileged` scope the manifest already promises. Keep the Tailscale bind as layer 2.

### P0: Tech Event Scout cron is broken every run — "invalid tool call: execute"
**Severity:** P0
**Location:** `~/.hermes/cron/jobs.json:177` (job `d56b9d9c7b1a`, `"model": "kimi-k2.7-code"`, `"last_status": "error"`, `"last_error": "RuntimeError: Model generated invalid tool call: execute"`)
**Problem:** The weekly cron (`0 6 * * 1`, next run 2026-07-13) fails on every fire: the `kimi-k2.7-code` model emits a tool call named `execute` that the agent runtime can't resolve (no such tool registered / tool-call parser mismatch). `completed: 2`, both errored. The prompt asks it to save a markdown file, so the model is reaching for a shell/file `execute` tool that isn't exposed (`"skills": [], "enabled_toolsets": null`).
**Impact:** The event-scout feature silently never produces output; the user gets nothing every Monday.
**Fix:** Either (a) give the job the filesystem/shell toolset it needs (`enabled_toolsets`) so an `execute`-style tool exists, or (b) switch to a model/harness whose tool-call grammar matches the registered tools, or (c) reframe the prompt to return markdown inline (no file-write tool) and have the cron wrapper persist it. Root cause is a Tool-layer (ETCLOVG) mismatch, not the prompt content.

### P0: Missing edit endpoints — project description and contact update cannot be persisted
**Severity:** P0
**Location:** `dashboard/api.py:1722` (`POST /api/projects` create only), `dashboard/api.py:804` (archive only) — no `PATCH /api/projects/{id}`; `dashboard/api.py:2172` (`POST /api/crm/contacts` create only) — no `PATCH /api/crm/contacts/{id}`
**Problem:** Projects expose create + archive + `GET /detail`, but no update route, so editing a project's description/title has no backend. Contacts expose create + list but no update route (compare deals, which *do* have `PATCH /api/crm/deals/{id}` at line 2218). The CRM drawer can render a contact but any edit has nowhere to POST.
**Impact:** UI edit actions for project descriptions and contact fields are dead ends (silent failure or 404/405).
**Fix:** Add `PATCH /api/projects/{project_id}` (title/description, mirroring the deals patch pattern) and `PATCH /api/crm/contacts/{contact_id}` in crm.py + api.py.
> **False-alarm note (verified, no action):** the shared-context 404s for `/api/icp`, `/api/products`, `/api/cltv-cac`, `/api/funnel/trend` are **wrong probe paths, not bugs** — the real routes are `/api/growth/icp` (2512), `/api/growth/products` (2529), `/api/growth/cltv-cac` (2593), `/api/growth/funnel-trend` (2476), all returning 200 live, and index.html fetches the correct `/api/growth/*` paths. `/api/crm/leads` being POST-only (405 on GET) is also correct by design (2328).

### P1: Blocking `subprocess.run` in the async task-create handler stalls the event loop
**Severity:** P1
**Location:** `dashboard/api.py:692` inside `async def api_create_task` (route `POST /api/tasks`, def at 691)
**Problem:** The handler is `async def` but calls `subprocess.run(["hermes","kanban","create",...], timeout=15)` synchronously. Unlike the healthz/urlopen paths (which correctly wrap work in `asyncio.to_thread`, lines 1445-1543), this shells out on the event-loop thread and can block **up to 15s**.
**Impact:** A slow `hermes kanban create` freezes *every* concurrent request on the single Uvicorn worker for the duration. Task creation is a hot path.
**Fix:** `await asyncio.to_thread(subprocess.run, cmd, ...)` (the codebase already uses `to_thread` 14× — apply the same pattern here).

### P1: index.html (641 KB) is served uncompressed — no GZip, no cache headers
**Severity:** P1
**Location:** `dashboard/api.py` (no `GZipMiddleware` registered; only `BodySizeLimitMiddleware` at 267) — verified live: `curl -H "Accept-Encoding: gzip" http://127.0.0.1:3000/` returns `size=641336` (uncompressed even when gzip is offered)
**Problem:** The single-file template is 641,337 bytes / 10,578 lines. `gzip -c` compresses it to 143,040 bytes (**78% smaller**), but FastAPI has no `GZipMiddleware`, so every full page load ships ~626 KB over the wire. The HTML response also has no `Cache-Control` (the two `no-store` headers at 1602/2869 are on other routes), and static assets are mounted via `StaticFiles` (196) with default (no explicit long-lived) caching.
**Impact:** Every dashboard open transfers ~626 KB it could send as ~140 KB; slow on constrained/mobile tailnet links.
**Fix:** `app.add_middleware(GZipMiddleware, minimum_size=1024)`. Consider splitting the monolithic template's inline JS into cacheable `/static/*.js` (only 4 inline `<script>` blocks today) and adding `Cache-Control: max-age=...` to static assets.

### P1: 185 async handlers, only 14 offload — sync sqlite runs on the event loop
**Severity:** P1
**Location:** `dashboard/api.py` (`grep -c "async def"` = 185; `grep -c "to_thread"` = 14), e.g. `dashboard/api.py:1735` `api_project_detail` → `sprints.get_project_detail(...)` calls blocking sqlite directly
**Problem:** The vast majority of `async def` routes call synchronous sqlite (db.py opens blocking `sqlite3` connections) inline rather than via `asyncio.to_thread`. sqlite is local and fast, so single-user latency is fine, but every query serializes the one event-loop thread under concurrency.
**Impact:** Concurrent dashboard tabs / fleet agents hitting the API contend on the loop; a slow query blocks unrelated requests. Latent scaling ceiling.
**Fix:** Route DB work through `asyncio.to_thread` consistently (as done for lakehouse/healthz/sessions), or run Uvicorn with multiple workers. Prioritize the hot read paths (project detail, sessions, board).

### P1: Missing indexes on foreign-key / hot filter columns (confirmed table SCANs)
**Severity:** P1
**Location:** kanban.db schema — `EXPLAIN QUERY PLAN` confirms full scans:
- `session_events.session_key` — **516 rows, no index**, `SCAN` (grows fastest of the hot tables; queried by session_key at api.py:2728 via `orch.get_events`)
- `deal_events.deal_id` — `SCAN deal_events`
- `task_ledger.task_id` — `SCAN task_ledger`
- `contacts.account_id` — `SCAN contacts` (declared FK → accounts, no covering index)
- also uncovered: `deals.contact_id`, `deals.account_id`, `tasks.initiative_id` (declared FKs, no index)
**Problem:** Several declared FKs and hot filter columns have no index; SQLite falls back to full scans. tasks itself is well-indexed (assignee/status/tenant/project/sprint/session/idempotency), but the CRM/session/ledger tables are not.
**Impact:** Low today (deals=5, contacts=9, tasks=115) but session_events (516) and task_runs/ledger grow unbounded; scans get linearly slower and, combined with the event-loop blocking above, amplify latency.
**Fix:** `CREATE INDEX` on `session_events(session_key)`, `deal_events(deal_id)`, `task_ledger(task_id)`, `contacts(account_id)`, `deals(contact_id)`, `deals(account_id)`, `tasks(initiative_id)`. Add to a migration under `dashboard/migrations/`.

### P2: Dynamic `ALTER TABLE`/`UPDATE` built with f-strings (safe now, fragile pattern)
**Severity:** P2
**Location:** `dashboard/growth.py:793`, `dashboard/growth.py:898`, `dashboard/crm.py:105`, `dashboard/crm.py:112`, `dashboard/crm.py:275`, `dashboard/crm.py:462`
**Problem:** Column names are interpolated into SQL via f-strings (`ALTER TABLE deals ADD COLUMN {col} {decl}`, `UPDATE deals SET {', '.join(sets)}`). **Not currently injectable** — every interpolated `col`/`decl` comes from static dict literals or a hardcoded allow-list (values are still parameterized `?`), so this is not a live vulnerability. But the pattern invites a future bug if a user-supplied field name ever reaches one of these builders.
**Impact:** Latent injection surface; no current exploit path.
**Fix:** Keep column names strictly from a module-level constant allow-list (they are today — add an `assert col in ALLOWED` guard to make the invariant explicit and grep-proof).

### P2: Deprecated FastAPI `@app.on_event("startup")`
**Severity:** P2
**Location:** `dashboard/api.py:2843` (pytest emits `DeprecationWarning: on_event is deprecated, use lifespan event handlers instead`)
**Problem:** Uses the deprecated startup-event API; will break on a future FastAPI major.
**Impact:** None today; future upgrade breakage + warning noise in every test run.
**Fix:** Migrate to a `lifespan` async context manager.

## Test Coverage Map
- `dashboard/api.py` core (tasks/comments/sprints/cycles) → **y** (test_comments, test_cycle_*, test_sprint_ledger_drift, board/drawer .spec.js)
- `dashboard/crm.py` → **y** (test_crm_growth, drawer-crm.spec.js; 8 test files reference crm)
- `dashboard/growth.py` → **y** (12 test files: test_crm_growth, test_cltv_cac, test_funnel_trend, test_nurture, test_pipeline_health, test_speaking, test_content_pipeline, growth.spec.js)
- icp config (`/api/growth/icp`) → **y** (test_icp_config + 2 others)
- products (`/api/growth/products`) → **y** (test_products)
- MCP/SSE security → **y** (test_mcp_sse_security, test_sse_security, test_sse_drain, test_container_security)
- body-size limit → **y** (test_body_limit)
- sessions → **y** (test_sessions_parallel, drawer-sessions.spec.js)
- **Auth on mutating REST endpoints → n** (no test asserts a token is required — because none is enforced; see P0-1)
- **project-description / contact update endpoints → n** (endpoints don't exist; see P0-3)
- **Tech Event Scout cron path → n** (cron config lives outside the repo in ~/.hermes; no regression test for its tool-call contract)
- graph_memory / object_graph → partial (test_graph_memory_upgrade covers upgrade fns; broad surface untested)

## Top 3
1. **P0 — 93 unauthenticated mutating endpoints + phantom MCP token** (`api.py:1182`, all `@app.delete/post/patch`): destructive routes (`DELETE /api/memory`, `/api/tasks`) are guarded only by the Tailscale bind; the advertised `HERMES_MCP_TOKEN` is manifest text, never enforced. Add a real auth dependency.
2. **P0 — Tech Event Scout cron dead** (`~/.hermes/cron/jobs.json:177`): errors every run with `invalid tool call: execute` (Tool-layer mismatch for `kimi-k2.7-code`); feature produces nothing. Fix the toolset or reframe to inline output.
3. **P1 — 641 KB index.html served uncompressed** (`api.py`, no GZipMiddleware; live curl confirms): gzip would cut it to 143 KB (78%). One-line middleware fix.
