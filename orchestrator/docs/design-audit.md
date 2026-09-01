# Design Audit

## Summary
The Hermes Orchestrator dashboard is a single-file ~10.6k-line Tailwind SPA with solid bones — a real toast system, a `confirmAction` modal, a reusable `emptyState()` helper, skeleton loaders, `prefers-reduced-motion` handling, and a mobile scroll-snap carousel for the boards. The problems are consistency and feedback: mutating actions fail silently or with mis-styled toasts, zero-data/error widgets blank themselves instead of using the empty-state helper, secondary text is systematically below WCAG contrast, and the UI mixes Spanish and English. Note: the probed `/api/icp` and `/api/products` 404s are red herrings — the app actually calls `/api/growth/icp` and `/api/growth/products`, which exist.

## Issues

### P0: Core task mutations fail silently and leave the board showing the wrong state
**Severity:** P0
**Location:** `dashboard/templates/index.html:10564` (`updateTaskStatus`), `:4021` (`crmTouch`)
**Problem:** `updateTaskStatus` does `if (res.ok) loadTasks()` and wraps everything in `catch(e){}` (10571-10572). On a non-OK response or thrown error it does nothing — no toast, no revert, no reload. An optimistic drag that the server rejects therefore stays visually "moved" until the next 45s auto-refresh silently snaps it back. `crmTouch` (4021) similarly only `console.error`s on failure with no user-facing toast. There are 22 empty `catch(){}` blocks total.
**Impact:** The user believes a status change / touch succeeded when it didn't — the board lies about server state. This is a data-trust failure on the primary interaction.
**Fix:** On failure, `toast('Move failed', 'err')` and re-run the loader to snap the card back to truth (or revert the optimistic DOM change explicitly). Never leave a mutation path with an empty catch.

### P1: Widgets silently vanish on fetch error or empty data instead of showing an empty state
**Severity:** P1
**Location:** `dashboard/templates/index.html:4419, 4473, 4530, 4713, 3672` (and 36 total `innerHTML = ''`); driven by `loadGrowth` at `:4395`
**Problem:** `loadGrowth` fires 6 parallel fetches each ending `.catch(() => null)` (4398-4403), and every render function blanks its container on null/empty: `renderPlanTracker` (`if (!data...) el.innerHTML=''`, 4419), `renderScorecardStrip` (3672), `renderGrowthLoops` (4713), `renderCltvTiles` (4530), `renderCltvCac` (4473). A 404/500 or empty payload produces a blank void with no message and no console trace. A reusable `emptyState(icon,title,desc,cta)` helper exists (6995) but is used for rendering exactly once (5860).
**Impact:** When Growth/Strategy data is empty or an endpoint errors, whole sections disappear. The user can't tell "no data yet" from "the page broke" — it just looks unfinished.
**Fix:** Route the null/empty branches through `emptyState(...)` with a helpful line + CTA (e.g. "No growth loops yet — add a lead"). Distinguish error (fetch rejected) from empty (200 with empty array) and show a retry affordance for the former.

### P1: Secondary text fails WCAG AA contrast and micro-typography is below legible minimums
**Severity:** P1
**Location:** `dashboard/templates/index.html` — `text-zinc-600` ×173, `text-[8px]` ×6, `text-[9px]` ×75, `text-[10px]` ×262 (e.g. 4730-4732, 4791, 4808)
**Problem:** `text-zinc-600` (#52525b) on the `bg-zinc-950` (#09090b) body is ≈3:1 contrast — below the 4.5:1 AA threshold for normal text — and it's the default for hints, labels, dates, and empty-state copy across the app. Compounding it, hundreds of labels render at 8-10px, well under a comfortable/accessible minimum.
**Impact:** Metadata, hints, and empty-state guidance are hard to read for anyone, and inaccessible to low-vision users. The 8-9px KPI sub-labels ("leads", "conversión") are effectively decorative.
**Fix:** Bump secondary text to `text-zinc-400` (≈7:1) minimum; reserve `zinc-600` for non-informational dividers. Set a floor of ~11px for any text conveying meaning.

### P1: Inconsistent success/failure feedback across mutating actions
**Severity:** P1
**Location:** `dashboard/templates/index.html` — compare `:4258/4313/4378` (toast on success) vs `:10571/4030` (no failure toast) vs the 22 empty catches
**Problem:** There's no single convention. `create_lead`, `update_icp`, `create_product`, `create_content` toast on both success and failure; `updateTaskStatus`, `crmTouch`, and several others give no failure feedback at all. Some paths toast, some `console.error`, some swallow.
**Impact:** The reliability of the app feels arbitrary — the same class of action (mutate → refresh) confirms itself in some tabs and stays mute in others.
**Fix:** Adopt one helper (e.g. `mutate(url, opts, {ok, fail})`) that always toasts on both outcomes and reloads, and route every POST/PATCH/DELETE through it.

### P2: Error toasts render as neutral grey — failures look like normal notices
**Severity:** P2
**Location:** `dashboard/templates/index.html:6947` (`toast`), called wrong at `:4388, 4461, 4923, 7807, 7817`
**Problem:** `toast(msg, kind)` only styles `kind === 'err'` red and `'warn'` amber, defaulting everything else to neutral zinc (6947-6949). Five call sites pass `'error'` (not `'err'`), so those failure toasts render in the same grey as a success/info toast. 58 sites correctly use `'err'`.
**Impact:** "Delete failed", "Generate failed", "Update failed" appear identical to success confirmations — the user misses that something went wrong.
**Fix:** Normalize the kind (accept `'error'` as an alias of `'err'`) or fix the 5 call sites. A tiny map (`{error:'err'}`) at the top of `toast()` is the safe change.

### P2: Flash of the wrong navigation on load (late bootstrap)
**Severity:** P2
**Location:** `dashboard/templates/index.html:200` (legacy nav visible), `:10527` (swap), `:10575` (`window.onload = init`)
**Problem:** The 13-tab `legacy-nav` ships visible in the initial HTML while the 6-tab `workspace-nav` ships `hidden`; the swap only happens inside `init()`, which runs on `window.onload` — the latest possible lifecycle event (after all images/CSS/scripts). Until then the user sees the legacy 13-tab bar, then it jumps to the 6-workspace layout, and all data loading is deferred to that same late point.
**Impact:** A visible nav flash + jank on every cold load, and a slower first meaningful paint than necessary.
**Fix:** Gate the initial HTML the other way (ship `workspace-nav` visible, `legacy-nav` hidden, since `WORKSPACES_V2 = true`), and bootstrap on `DOMContentLoaded` rather than `window.onload`.

### P2: Mixed Spanish/English UI copy
**Severity:** P2
**Location:** `dashboard/templates/index.html` — Spanish: `:4423` "Plan 90 días", `:3683` "Scorecard semanal", `:4771` "Sin piezas", `:7684/7720/7757` "Cargando…", `:4502` "Sin datos — agrega costos…"; English: `:286` "Wrap Day", `:4767` "Content calendar", `:1159` "Loading…"
**Problem:** The interface freely mixes two languages, often within the same card (a "Content calendar" header over "Sin piezas" empty text; "Cargando…" vs "Loading…").
**Impact:** Reads as unfinished/machine-assembled and adds cognitive friction. Inconsistent even within a single view.
**Fix:** Pick one UI language (or add a light i18n string table) and make loading/empty strings consistent — at minimum unify "Cargando…"/"Loading…".

### P2: No loading state on Strategy/Growth widgets — blank then pop-in (layout shift)
**Severity:** P2
**Location:** `dashboard/templates/index.html:4395` (`loadGrowth`); skeleton infra unused here (`skeletonGrid` at `:7007`, used only board/sessions/cycle at 5738, 8698)
**Problem:** The board, sessions, and cycle tabs show shimmer skeletons while loading, but the Growth/Strategy widgets fire their 6 fetches with empty containers and only fill in when each promise resolves — so the section is blank, then each card pops in and shoves layout.
**Impact:** Perceived slowness and content jump (CLS) on the Strategy tab; inconsistent with the polished loading in other tabs.
**Fix:** Render `skeletonGrid(...)` into the growth containers before `Promise.all`, replacing on resolve — reuse the pattern already in the codebase.

### P2: Interactive `<span>`/`<div>` elements aren't keyboard-accessible
**Severity:** P2
**Location:** `dashboard/templates/index.html:4797` (content status chip `<span ... onclick>`), plus 18 `<div onclick>` and 4 `<span onclick>` total
**Problem:** Several primary actions are attached to non-semantic elements — e.g. the content-piece status advances via `<span ... onclick="cycleContentStatus(...)">` (4797-4798). These get no default tab-focus, no Enter/Space activation, and no button role.
**Impact:** Keyboard and screen-reader users can't reach or trigger these actions; they also lack visible focus states.
**Fix:** Convert clickable spans/divs that perform actions into `<button>` (or add `role="button"` + `tabindex="0"` + keydown handling). The app already uses 224 real `<button>`s — these are the stragglers.

### P3: No color-token layer — palette scattered across Tailwind, JS constants, and raw hex
**Severity:** P3
**Location:** `dashboard/templates/index.html` — 72 raw `#rrggbb` outside the `<style>` block; JS palettes at `:4416` (`PLAN_PHASE_COLORS`), `:4467` (`CLTV_RATING`), `LOOP_META`, `CRM_STAGE_META`; plus inline `style="border-top:2px solid ${color}"`
**Problem:** Brand/semantic colors live in three parallel systems: Tailwind utility classes (`bg-zinc-900`, `text-blue-500`), hardcoded hex in JS objects, and inline style strings. `#3b82f6` (the focus/accent blue) is repeated literally in the style block (18, 19, 39, 46…) rather than referenced from one variable.
**Impact:** No single source of truth for the palette — theming or a brand tweak means hunting across CSS, JS objects, and template literals, and drift is easy.
**Fix:** Define CSS custom properties (`:root { --accent: #3b82f6; --surface: #18181b; ... }`) and reference them from both the style block and the JS color maps (or expose a small JS token object the render functions read).

### P3: Dual navigation permanently duplicated in the DOM
**Severity:** P3
**Location:** `dashboard/templates/index.html:167` (`workspace-nav`) and `:200` (`legacy-nav`)
**Problem:** Both the 6-workspace nav and the full 13-tab legacy nav are always present in the DOM; one is just `hidden`. `WORKSPACES_V2` has been `true` for a while (1722), so the legacy nav is dead weight carried on every render, and every workspace switch mirrors badges across both (`mirrorBadge` calls at 3461, 5785…).
**Impact:** Extra markup, double book-keeping, and a live-but-invisible second IA that can silently drift from the real one.
**Fix:** If the workspace nav is the committed direction, delete the legacy nav and its mirror-badge plumbing; if the flag is still an escape hatch, render only one nav server-side based on the flag.

## Top 3
1. **P0 — stop silent mutation failures** (`updateTaskStatus`/`crmTouch`): toast + revert on failure so the board never lies about server state.
2. **P1 — route empty/error widget states through the existing `emptyState()` helper** instead of blanking containers, so failures read as "no data" not "broken".
3. **P1 — fix contrast/typography**: raise `text-zinc-600` secondary text to `zinc-400` and floor meaningful text at ~11px for WCAG AA legibility.
