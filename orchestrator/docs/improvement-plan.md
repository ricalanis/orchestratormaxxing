# Improvement Plan — Hermes Dashboard Multi-Expert Audit

_Synthesized 2026-07-09 from three independent expert audits: [Design](design-audit.md) · [Product](product-audit.md) · [Dev](dev-audit.md). All fix tasks live in project `proj_orchestrator`._

## Executive Summary
The dashboard has **solid bones** — real toast system, `confirmAction` modal, `emptyState()` helper, skeleton loaders, 385 passing tests, sound core loops (tasks, sessions, roadmap drilldown all work E2E). The defects cluster in four places: **(1) trust** — the primary board can silently show the wrong server state; **(2) security governance** — 93 mutating endpoints have no app-level auth and the advertised MCP token is never enforced; **(3) CRM completeness** — contacts can't be viewed/edited, projects can't be edited, and deals can't attach a product or initiative from the UI; **(4) perf hygiene** — a 641 KB uncompressed page and event-loop-blocking I/O.

### 🔑 Cross-audit corrections (important — don't waste effort on these)
- **The `/api/icp`, `/api/products`, `/api/cltv-cac`, `/api/funnel/trend` 404s in the brief are NOT bugs.** They were wrong probe paths. Real routes are under `/api/growth/*` (all return 200 live) and the UI calls them correctly. **All three agents independently confirmed this.**
- **Products are NOT empty** — `/api/growth/products` has real products (e.g. "Data & AI Readiness Audit"). ICP returns data too; only its `positioning_statement` is blank. So "Growth data VACÍOS" is only partly true → it's a *data-seeding* task, not a build task.
- `/api/crm/leads` GET→405 is **correct by design** (POST-only).
- The `os.path.expandpath` cron bug is **confirmed fixed** (zero grep hits).

## Priority Matrix

| # | Issue | Sev | Source | Owner model | Task |
|---|---|---|---|---|---|
| 1 | Enforce auth on 93 mutating routes + real `HERMES_MCP_TOKEN` gate | P0 | Dev | **Opus** (security, cross-cutting) | `t_b3448002` |
| 2 | Silent task/deal mutation failures — toast+revert, kill empty catches | P0 | Design | Kimi-coder → Opus verify | `t_58d0bc71` |
| 3 | Missing `PATCH /api/projects/{id}` + `PATCH /api/crm/contacts/{id}` + `crm.update_contact` | P0 | Dev + Product | GLM-coder → Opus verify | `t_6b274b33` |
| 4 | Build Contacts view under CRM (list + inline edit) | P0 | Product | Kimi-coder → Opus verify | `t_32d469b1` |
| 5 | Fix Tech Event Scout cron `invalid tool call: execute` | P0 | Dev | **Opus** (ETCLOVG tool-layer dx) | `t_70d8b6b3` |
| 6 | Deal modal: product + initiative selectors + standalone "+Deal" | P1 | Product ×2 | Kimi-coder → Opus verify | `t_e4fe3b88` |
| 7 | Route empty/error widgets through `emptyState()` + growth skeletons | P1 | Design | GLM-coder | `t_0e5d4c0d` |
| 8 | WCAG AA contrast (`zinc-600`→`zinc-400`) + 11px text floor | P1 | Design | GLM-coder | `t_1c4c046a` |
| 9 | `GZipMiddleware` (641KB→143KB) + `Cache-Control` | P1 | Dev | **Opus** (trivial, do inline) | `t_38ff5eaf` |
| 10 | Wrap blocking `subprocess.run` in `api_create_task` with `to_thread` | P1 | Dev | GLM-coder | `t_d3159902` |
| 11 | Missing DB indexes migration (FKs + hot filter columns) | P1 | Dev | GLM-coder → Opus verify EXPLAIN | `t_effbfdb9` |
| 12 | Seed growth data (ICP positioning, tag deals w/ loops) | P2 | Product | **the operator** (their positioning/data) | `t_de62325f` |
| 13 | CRM polish: drag-to-stage + scorecard targets/deltas/tooltips | P2 | Product | Kimi-coder | `t_4003d61f` |
| 14 | Feedback consistency: toast `'error'` alias + ES/EN unify + a11y onclick | P2 | Design | GLM-coder | `t_bf118ea9` |
| 15 | Tech-debt: nav cleanup + color tokens + lifespan + to_thread sqlite sweep | P3 | Design + Dev | GLM-coder | `t_5d6c23f6` |

## Execution Order

**Wave 1 — P0, unblock trust + security + CRM editing (do first):**
1. `t_b3448002` **Auth gate** (Opus) — highest-risk; destructive routes (`DELETE /api/memory`, `/api/tasks`) currently guarded only by the Tailscale bind. Keep the bind as layer 2.
2. `t_58d0bc71` **Silent mutation fix** (Kimi) — the board must never lie about server state. Ship a single `mutate()` helper that always toasts + reloads.
3. `t_6b274b33` **PATCH endpoints** (GLM) → then `t_32d469b1` **Contacts UI** (Kimi). #3 is the backend prerequisite for #4; mirror the existing deals-patch pattern.
4. `t_70d8b6b3` **Cron fix** (Opus) — root cause is a tool-layer mismatch (`kimi-k2.7-code` emits `execute`, no such tool registered); fix the toolset or reframe to inline output. Ties to the operator's "wake up with the review executed" need.

**Wave 2 — P1, completeness + perf:**
5. `t_38ff5eaf` **GZip** (one-liner, do immediately) → `t_d3159902` subprocess → `t_effbfdb9` indexes.
6. `t_e4fe3b88` **Deal selectors** — needed for revenue→product / revenue→strategy attribution (the operator's Growth-pipeline P1).
7. `t_0e5d4c0d` **emptyState routing** + `t_1c4c046a` **contrast** — parallel, both GLM, mechanical class/branch swaps.

**Wave 3 — P2/P3, polish + data:**
8. `t_de62325f` **The operator seeds growth data** (only they have the ICP positioning) — this alone lights up the currently-"empty" scorecard/loops.
9. `t_4003d61f`, `t_bf118ea9`, `t_5d6c23f6` — batched cleanup.

## Verification per doctrine
Each delegated fix gets an **Opus-authored acceptance contract before dispatch** (Tier 0), a deterministic test run (Tier 1a), and mutation-gate (`bin/mut`, Tier 1b) on the high-value security/data-trust fixes (#1, #2, #3). The auth fix (#1) and cron fix (#5) stay **in Opus** — cross-cutting/high-stakes, not delegated. The existing 385-test suite + Playwright specs are the regression floor; add a test asserting a token is required on the destructive set (currently untested — see dev-audit Coverage Map).

## Alignment with the operator's stated needs
- **P0 "despertar con revisión ejecutada"** → this audit + Wave-1 tasks are the review; cron fix (#5) restores the automated Monday scout.
- **P1 "Growth pipeline poblado"** → `t_e4fe3b88` (attach product/initiative) + `t_de62325f` (seed) close the loop.
- **P1 "MCP tunnel for Cowork"** → gated by the auth fix (#1): don't expose the MCP beyond the tailnet until a real token gate exists.

## Kanban Tasks Created (project `proj_orchestrator`)
- [ ] `t_b3448002` — [P0][security] Enforce auth on mutating endpoints + MCP token
- [ ] `t_58d0bc71` — [P0][data-trust] Fix silent task/deal mutation failures
- [ ] `t_6b274b33` — [P0][backend] PATCH projects + contacts + `update_contact`
- [ ] `t_32d469b1` — [P0][ui] Build Contacts view
- [ ] `t_70d8b6b3` — [P0][cron] Fix Tech Event Scout `execute` tool-call
- [ ] `t_e4fe3b88` — [P1][ui] Deal modal product+initiative selectors + "+Deal"
- [ ] `t_0e5d4c0d` — [P1][ui] emptyState() routing + growth skeletons
- [ ] `t_1c4c046a` — [P1][a11y] WCAG contrast + text-size floor
- [ ] `t_38ff5eaf` — [P1][perf] GZipMiddleware + cache headers
- [ ] `t_d3159902` — [P1][perf] to_thread the blocking subprocess
- [ ] `t_effbfdb9` — [P1][perf] Missing DB indexes migration
- [ ] `t_de62325f` — [P2][growth] Seed growth data _(assignee: operator)_
- [ ] `t_4003d61f` — [P2][ui] CRM drag-to-stage + scorecard interpretability
- [ ] `t_bf118ea9` — [P2][ui] Feedback consistency + a11y + i18n
- [ ] `t_5d6c23f6` — [P3][techdebt] Nav cleanup + tokens + lifespan + sqlite sweep
