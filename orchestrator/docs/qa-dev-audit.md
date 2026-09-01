# Development QA Audit — Hermes Orchestrator

**Date:** 2026-07-11 (refresh of same-day earlier run) · **Auditor:** Claude (dev QA audit, read-only) · **Scope:** `dashboard/`, `mcp_server.py`, `mcp_sse_server.py`, `tests/`, live DB (`~/.hermes/kanban.db`, read-only)

**Verdict: HEALTHY.** Test suite is stable (463 passed × 3 consecutive runs, zero flakes, ~17s), auth is constant-time and correctly layered, SQL and XSS hygiene are disciplined, MCP tool⇄handler parity is exact and ratchet-guarded, and the live database has zero orphaned records with `integrity_check: ok`. One MEDIUM git-hygiene finding (`.db` files tracked, no `*.db` gitignore rule); everything else LOW/INFO.

---

## 1. Test suite health — ✅ EXCELLENT

`python -m pytest tests/ -q --tb=no`, three consecutive runs:

| Run | Result | Time |
|---|---|---|
| 1 | 463 passed, 13 skipped | 16.7s |
| 2 | 463 passed, 13 skipped | 16.5s |
| 3 | 463 passed, 13 skipped | 16.7s |

- **Zero flaky tests** — identical pass/skip sets and near-identical runtimes across all three runs (a same-day earlier run also got 3× 463, at 22–28s under heavier machine load — stable across conditions).
- The 13 skips are **deliberate environmental gates**, not rot:
  - 10× `test_container_security.py` — gated on `RUN_CONTAINER_TESTS=1` + docker.
  - 3× `test_sse_drain.py` / `test_sse_security.py` — "token file present — dev mode not reachable": they assert the *no-token* dev path, which can't exist on a machine where security is configured. Skipped *because* auth is set up.
- Import smoke check passes: `from dashboard import api, crm, growth, strategy, db; import mcp_server` → ok. (Note: `mcp_server.py` lives at repo root, not in an `orchestrator/` package — the audit prompt's `from orchestrator import mcp_server` path is stale.)

## 2. Architecture assessment — ✅ GOOD, one monolith flagged

**Layout.** Clean two-layer design: ~26 domain modules in `dashboard/` (crm, growth, sprints, canvas, sessions, object_graph, graph_memory, …); `api.py` is route-layer only; `mcp_server.py` wraps the same domain modules **in-process** (`from dashboard import loop, object_graph, db …`) — one validated write path, no duplicated business logic between API and MCP.

| File | Lines | Note |
|---|---|---|
| `mcp_server.py` | 4,750 | 205 tools + handlers; parity-ratcheted (§5) |
| `dashboard/api.py` | 3,230 | ~191 `/api` routes; logic delegated to domain modules |
| `dashboard/growth.py` | 2,674 | Largest domain module, cohesive (growth/funnel/cadence) |
| `dashboard/db.py` | 1,462 | Schema + migrations + CRUD |
| `dashboard/crm.py` | 871 · `strategy.py` 279 · `mcp_sse_server.py` 781 | Fine |
| `dashboard/templates/index.html` | ~12.4k (≈628 KB) | ⚠ single-file template with inline JS |

- **Dead code / debt: near zero.** Exactly **1** TODO in the audited Python surface (`growth.py:1099`, a documented future switch to `response_rate`). No FIXME/HACK markers. Middleware ordering is documented in comments and matches actual registration order (body-limit → gzip → auth → logger).
- ⚠ `index.html` is the one maintainability hotspot — escape-disciplined today (§3), but every feature enlarges an unsplittable file. `dashboard/static/cycle-board.js` proves extraction works as a pattern.

## 3. Security audit — ✅ PASS

- **Auth on mutating endpoints: active and correct.** `MutatingAuthMiddleware` (`api.py:372`, registered `:430`) gates every POST/PATCH/DELETE/PUT to `/api/*`:
  - **Constant-time comparison confirmed:** `secrets.compare_digest` (`api.py:405`).
  - Proper 401 + `WWW-Authenticate: Bearer` (RFC 6750); registered inner to the HTTP logger so 401s hit metrics.
  - `TESTING=1` bypass is read per-request and has its own regression guard (`tests/test_auth_middleware.py` clears TESTING to assert real enforcement). Never set in production.
  - Dev mode (no token configured) leaves mutations open **with a loud one-time stderr warning** — acceptable for localhost; INFO below.
- **MCP privileged tools:** `PRIVILEGED_TOOLS` set (`mcp_server.py:125`) gated by `HERMES_MCP_PRIVILEGED_TOKEN`, also compared via `secrets.compare_digest` (`mcp_server.py:224`) ✅.
- **SQL injection: none found.** All 42 f-string SQL sites reviewed/spot-checked: SET-clause builders interpolate **hardcoded literal** `"col = ?"` fragments with values through `?` params (`crm.py:229`, `growth.py:1447`, `db.py:582`, …); whitelist-validated enum columns (`growth.py` VALUE_LADDER/GROWTH_LOOP/LEAD_SOURCE checks); internal constants (`_TASK_FIELDS`, `_review_where()`, `int(limit)` in `canvas.py:215`); count-generated `IN (?,…)` placeholders (`graph_memory.py:230`). No string-concat or `%`-format execute calls anywhere.
- **XSS: disciplined.** 252 `innerHTML` assignments in `index.html`/`planning.html`; line-level grep flagged 39 as "unescaped" but sampling shows these interpolate **pre-escaped builder strings** (`blocks`/`chain`/`rows` apply `escapeHtml()` to every user-sourced field at build time) or server-controlled enums/IDs (`${d.stage}`, `${d.account_id}`). One real (LOW) sink verified: `index.html:10874` interpolates the tmux session name into the session-modal title unescaped — operator-controlled input, not remote-attacker-reachable.
- **Defense in depth:** `BodySizeLimitMiddleware` (413 on oversized bodies), dedicated SSE/container security suites, token read from env or `~/.config/orchestratormaxxing/dashboard-token` — never hardcoded.

## 4. Performance audit — ✅ PASS

- **GZip: active.** `GZipMiddleware(minimum_size=1024)` (`api.py:360`) — index.html ~628 KB → ~143 KB on the wire; registered outside the logger so timing/status stay accurate.
- **No event-loop blocking.** AST scan of every `async def` in `sessions.py`, `usage.py`, `fireflies.py`, `agent_status.py`, `orchestration.py`, `governance.py`: **zero** blocking calls (`subprocess.run`, `time.sleep`, `requests.*`) inside async bodies. The blocking sites all live in **sync** functions (FastAPI threadpools sync-def routes — the correct pattern for SQLite) or are explicitly wrapped: `asyncio.to_thread` (`api.py:855`), `run_in_executor` (`api.py:1860`). `time.sleep` appears only in the standalone `scrape_ollama_usage.py` scraper, itself launched via executor with a 90s timeout.
- **Indexes (live `~/.hermes/kanban.db`, 36 tables):** every declared-FK column is indexed except `sprints.project_id` (3 rows) and `initiatives.project_id` (8 rows) — no-ops at this size. No `*_id` column on any table > 200 rows lacks an index. Watchlist as data grows: the small-table FK-like columns (`tasks.parent_id`, `task_ledger.session_key`, `deals.product_id`).

## 5. MCP / API parity — ✅ EXACT (internal), no genuine cross-layer gap found

| Surface | Count |
|---|---|
| MCP `TOOLS` definitions | 205 |
| MCP `TOOL_HANDLERS` entries | 205 |
| TOOLS without handler / handlers without TOOL | **0 / 0** |
| `/api` routes | 191 |

- TOOLS⇄TOOL_HANDLERS verified two ways this run: direct introspection (`len`, set-diff → empty both directions) **and** the ratchet suite `tests/test_mcp_api_parity.py` (9 passed) — the McpGlobalParity guard shipped in commit `9a5b272` (2026-07-10 parity audit, which closed the 15 genuine wrapper gaps), making handler drift permanently red.
- Tool↔route parity can't be 1:1 by design: MCP carries agent-protocol tools with no REST twin (`report_progress`, `report_blocked`, `claim_next`, `heartbeat`, `register_agent`, `dispatch_to_agent`, `send_to_session`), and several tools share one route family. A name-token diff auto-matched 164/205; manual sampling of the remainder showed **naming artifacts, not gaps** (`quick_add_contact` ↔ `POST /api/crm/quick-add`, `get_stale_deals` ↔ `GET /api/crm/stale`, `capture_funnel` ↔ `POST /api/growth/funnel-snapshot`, `get_cadence_status` ↔ `GET /api/growth/cadence/{deal_id}`, `get_monthly_strategic_view` ↔ `GET /api/growth/monthly-view`, `delete_comment` ↔ `DELETE /api/comments/{comment_id}`). Since MCP wraps the dashboard modules in-process, functional parity holds wherever the underlying function is shared.
- INFO: the name-diff is noisy — if tool↔endpoint parity should stay an audited invariant, extend the ratchet with an explicit tool→route (or tool→dashboard-function) mapping table instead of re-deriving it by grep each audit.

## 6. Issues found (by severity)

| # | Sev | Issue | Location |
|---|---|---|---|
| 1 | **MEDIUM** | `.db` files tracked in git and no `*.db` gitignore rule: `kanban.db` + `orchestrator.db` are tracked (both currently 0-table shells — the live DB is `~/.hermes/kanban.db`), and `dashboard/growth.db` sits untracked in the worktree. Risk: accidentally committing live business data. Fix: `git rm --cached` the shells if unused + add `*.db` to `.gitignore`. | repo root, `.gitignore` |
| 2 | LOW | `index.html` ~12.4k-line monolith (252+ innerHTML sites) — maintainability + XSS-regression surface; extract feature JS modules like `cycle-board.js` | `dashboard/templates/index.html` |
| 3 | LOW | Session-modal title interpolates tmux session name into innerHTML unescaped (operator-controlled input) | `index.html:10874` |
| 4 | LOW | Declared-FK columns without index — harmless at 3/8 rows; index if these tables grow | `sprints.project_id`, `initiatives.project_id` |
| 5 | INFO | Dev mode leaves mutating endpoints open when no token configured — documented + stderr-warned; localhost only | `api.py:424` |
| 6 | INFO | 10 container-security tests never run locally (`RUN_CONTAINER_TESTS=1` gate) — ensure they run somewhere (CI or a scheduled tick) | `tests/test_container_security.py` |
| 7 | INFO | Uncommitted working-tree change (`dashboard/scrape_ollama_usage.py`, from a parallel session) — shared-tree hygiene: stage own hunks only | git status |
| 8 | INFO | 1 TODO (documented, intentional) | `growth.py:1099` |
| 9 | INFO | Recurring-audit prompt drift: import path (`from orchestrator import mcp_server` → `import mcp_server`) | audit prompt |

**Data integrity:** 0 orphaned records across all key FK relationships checked in the live `kanban.db` (tasks→projects/epics/sprints, deals→accounts, contacts→accounts, deal_events→deals, epics→initiatives); `PRAGMA integrity_check` → ok.

## 7. Alignment — is the codebase healthy enough?

**Yes.** All five dev goals are met:

1. **Security** ✅ — constant-time Bearer auth on all mutating `/api/*` endpoints; privileged MCP tools separately token-gated (also constant-time); parameterized SQL throughout; consistent XSS escaping (one LOW operator-controlled sink).
2. **Performance** ✅ — GZip + body-size limit active; zero event-loop blocking; hot-path indexes present on the live DB.
3. **Tests** ✅ — 463/463 × 3 consecutive runs, zero flakes, ~17s suite; skips are deliberate environment gates.
4. **Code quality** ✅ — clean domain-module separation, single shared write path for API+MCP, 1 TODO total, documented middleware ordering. The `index.html` monolith is the one structural debt item.
5. **MCP parity** ✅ — 205⇄205 exact with a permanent ratchet test (`test_mcp_api_parity.py`, 9 passing); no genuine tool↔endpoint gap found in sampling.

Recent commit history reinforces the trend: the last ten commits are focused, well-scoped fixes with descriptive messages (parity ratchet, test-leak fix, 1349-errors/day elimination), each closing a verified defect. The one action worth taking this week is issue #1 (`.db` git hygiene).
