# Design/UX QA Audit — Hermes Orchestrator Dashboard

**Date:** 2026-07-11
**Scope:** `dashboard/templates/index.html` (12,407 lines; single inline script block, 1,942–12,404)
**Method:** static audit — grep/read of the template, `node --check` on the extracted inline JS, WCAG relative-luminance contrast computed for every token pair in use. No code changed.

---

## 1. Design quality checklist

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | Trust: mutations confirmed + never show wrong state | ✅ PASS | 92 mutating fetches / 152 `toast()` calls. Every optimistic path found has an authoritative revert on failure: kanban drag (`loadCycle` revert, L2935), CRM stage move (`loadCRM()` revert, L5240), inline stage/product selects (`data-prev` + revert, L4971/4997), task status (L12396). Failure toasts say so explicitly ("Move failed — reverting"). |
| 2 | Empty states via `emptyState()` | ⚠️ PARTIAL | `emptyState()` (L8772) + `widgetEmpty()`/`widgetError()` (L8788/8798, distinguishing "no data" from "fetch failed" with retry) exist and are used ~35×: pipeline ✅ (L4699), scorecard ✅ (L4475), growth loops ✅ (L6191), sessions ✅ (L10679), forecast ✅, ladder/CLTV/funnel/usage ✅. **Roadmap** (L11867) and **Memory** (L6893) use hand-rolled inline divs instead — still meaningful, but off-pattern; roadmap's fetch-error state is plain red text with no retry CTA. |
| 3 | Skeleton loaders | ✅ PASS | `.skeleton` shimmer CSS (L118) + `skeletonGrid()` (L8808); wired for Cycle tab first load (`renderCycleSkeleton`, L2499), board first load (L7406), sessions multi-host scan (L10518), growth sections (L5832). Shimmer disabled under `prefers-reduced-motion`. |
| 4 | Consistent toast system | ✅ PASS | Single `toast(msg, kind, action)` (L8718). Normalizes kind aliases (`error`→`err`, `warning`→`warn`), unknown kinds fall back to default styling, optional action button (Undo), stacked in `#toast-stack`, 3s/6s auto-dismiss, animation respects reduced-motion. All toast color pairs pass AA (red-200/red-900 6.9:1, amber-100/amber-900 8.2:1, zinc-100/zinc-800 ~13:1). |
| 5 | Keyboard navigation | ⚠️ PARTIAL | Strong where it matters: cycle board has a full keyboard handler (`cycleBoardKeydown`, L641 — keyboard alternative to drag), cycle calendar is a roving-tabindex `listbox` (L2568–2585), task/deal/session cards carry `role="button" tabindex` + Enter/Space handlers, tabs use `role="tab"/"tablist"`, `#sr-live` aria-live region exists (L1941). Gaps listed in §2. |
| 6 | WCAG AA contrast | ❌ FAIL | Primary text excellent (`--text` 15.7:1, zinc-400 6.9:1, zinc-300 10:1). **But `text-zinc-500`/`--text-muted` (#71717a) = 3.67:1 on surface / 4.12:1 on bg — below the 4.5:1 AA floor for normal-size text, and it is used 333 times, mostly at 11–13px** (all widget descriptions, sub-labels, hints). `text-zinc-600`/`--text-dim` (#52525b) = 2.29–2.57:1, fails even the large-text bar (9 + 3 usages, mostly decorative). |
| 7 | 11px text floor | ⚠️ PARTIAL | The floor is `text-[11px]` almost everywhere, but **9 instances of `text-[10px]`** remain: chip badges (L300/359/414), scheduled-week chips (L3002/3007), the pipeline-temporal table headers (L3799), deal-event stage labels (L3848), forecast deal rows (L4809/4811). |
| 8 | Color-coded stages | ✅ PASS | `CRM_STAGE_META` (L4201–4208) maps all 8 stages to distinct tokens (lead=muted, engaged=teal, qualified=accent, demo=warning, proposal=violet, stalled=cyan+🧊, won=success, lost=danger). All keys referenced from `COLOR_TOKENS` exist in `:root` — no undefined token lookups anywhere (verified against the full 27-key list). |
| 9 | Value ladder w/ emoji | ✅ PASS | `LADDER_META` (L4217): 🧲 Magnet → 🎟️ Entry → 🎯 Core → 🔁 Recurring, each with color + hover hint; rendered per Track A/B with per-rung count, value subtotal, and color-coded top border (L4832ff). Empty rungs show "—" rather than collapsing. |
| 10 | Pipeline vs catalog visually distinct | ✅ PASS | Two separately-labeled sections in the CRM tab: `#crm-ladder` ("🪜 Value ladder · N active deals" — deals only, selling stages only, stalled/won/lost excluded by design) and `#crm-products` ("Product catalog" with 📊 Track A / 🤖 Track B colored headers — offers, not deals). Card chrome is similar (zinc-900/40 rounded-2xl) but headers, icons, and content make the distinction unambiguous. |
| 11 | Pipeline monthly view | ✅ PASS | `renderPipelineTemporal` (L3786): month-by-month table, newest first, color-coded new/moves/won/lost counts, `tabular-nums` alignment, expandable per-month event rows, distinct empty vs fetch-error states. Readable; only nit is its 10px uppercase headers (see item 7). |
| 12 | 30/60/90 forecast | ✅ PASS | `renderForecast` (L4798) + `FORECAST_BUCKETS`: three bucket cards keyed off `/api/growth/forecast`, per-bucket deal drill-down (top 4 + "+N more"), header shows active-deal count, two distinct empty states (no data at all vs no active deals). |
| 13 | Deal creation complete | ✅ PASS | "+ Deal" in the CRM header (L779) and as the pipeline empty-state CTA (L5143). `openDealCreate` (L5451) resets every field; modal covers title (required, inline `#de-error` validation), stage, account (fetched fresh), value, recurrence type + interval (conditional field), expected close date (clearable — explicitly always sent), product link (auto-sets ladder rung), initiative link, and sub-deal management. Success/failure both toast; list reloads after create. |
| 14 | Responsive | ✅ PASS | Viewport meta ✓. Mobile (<768px): kanban becomes a horizontal scroll-snap carousel (84vw columns, momentum scroll, `overscroll-behavior-x: contain`), chip rows wrap, modals capped at 90vh. Sticky nav offset is measured via ResizeObserver, not hard-coded (L8817). 40+ responsive grid utilities across breakpoints. `prefers-reduced-motion` and `hover: none` (touch grip always visible) both handled. |
| 15 | No broken tokens / template literals | ✅ PASS | Full inline script passes `node --check` (syntax clean). All `COLOR_TOKENS[...]` keys resolve. One **stale comment** (L1946) tells readers to use `TOKENS.warning`, but no `TOKENS` object exists — comment-only, nothing broken at runtime. |

---

## 2. Issues found

### High

**H1 — Secondary text fails WCAG AA across the entire dashboard.**
`text-zinc-500` / `--text-muted` (#71717a) measures **3.67:1 on `--surface`** and 4.12:1 on `--bg` — below the 4.5:1 requirement for normal-size text, and this is the dashboard's *default* secondary/description color: **333 usages**, nearly all at 11–13px (widget descriptions, empty-state copy, sub-labels, section subtitles). One shade up (zinc-400, #a1a1aa, 6.9:1) already passes with headroom, so the fix is a token swap, not a redesign. Note the irony that `emptyState()`/`widgetEmpty()` descriptions themselves ship in zinc-500 — the accessibility gap is baked into the shared helpers.

### Medium

**M1 — CRM-family modals lack dialog semantics and Escape-to-close.**
Only 7 of ~19 overlay modals declare `role="dialog" aria-modal="true" aria-labelledby` (the cycle/task/project family, L1757–1921). The CRM set — `deal-edit-modal`, `quick-lead-modal`, `icp-modal`, `product-modal`, `talk-modal`, `content-modal`, `contact-edit-modal`, `initiative-modal`, `wrap-day-modal` — has none of them, and the global Escape handler (L11178) only covers `session-output-modal` and `initiative-modal`. The deal editor closes via backdrop click and the × button but **not** via Escape, and there is no focus trap or focus-on-open (contrast with `confirmAction`, L8746, which does Esc/Enter/backdrop/focus correctly).

**M2 — `text-zinc-600`/`--text-dim` (2.3–2.6:1) is illegible for anyone with low vision.**
12 usages: zero-count cells in the monthly table (L3807–3816), event dates/stage labels (L3843/3848), the "drop here" hint (L5090), the drag grip (L8528). Most are decorative, but the monthly-table zeros and event dates are *data*. Same one-shade-up fix as H1.

**M3 — Destructive-action UX is split between two patterns.**
`confirmAction()` (P2-25) was built to replace native dialogs, yet **7 destructive flows still use native `confirm()`**: delete time block (L3717), delete sub-deal (L5398), delete product (L5821), delete talk (L6178), delete content (L6358), move-out-of-cycle (L10195), revive session (L11060). Functional and keyboard-accessible, but visually jarring against the styled modal used everywhere else, and native confirm blocks the main thread.

### Low

**L1 — 9 instances of `text-[10px]` breach the 11px floor** (locations in checklist item 7). The monthly-table headers and forecast deal rows are the ones carrying real information.

**L2 — Roadmap and Memory empty/error states bypass the shared helpers.** Roadmap renders a bare centered div (no icon, no CTA styling) and its fetch-error state has no retry button (`widgetError()` exists precisely for this); Memory's per-list empty message is an inline 11px zinc-400 div. Cosmetic inconsistency, not a functional gap.

**L3 — Stale comment at L1946** references a `TOKENS` object that was never created (the const is `COLOR_TOKENS`). Harmless today; a future edit that trusts the comment would throw a ReferenceError.

**L4 — A couple of clickable spans lack keyboard affordances.** The overall pattern is disciplined (role="button" + tabindex + Enter/Space on cards), but at least one truncated-title span (`cursor-pointer` without role/tabindex) slipped through. Worth a sweep next time card templates are touched.

**L5 — White-on-`--accent` (#3b82f6) is 3.68:1.** Fine for large text/UI components; would fail AA if used for small button labels. Current buttons use blue-600 (#2563eb, 5.17:1 ✅), so this is only a risk if someone reaches for `--accent` as a small-text button background.

---

## 3. Alignment with product goals

**The design supports the product well.** This dashboard's product thesis is *operator trust in an agent-run pipeline* — the human must believe the board reflects reality even while agents mutate it underneath. The audit confirms the load-bearing patterns for that thesis are real, not aspirational:

- **State honesty is systematically enforced.** Every optimistic mutation found has an authoritative server revert on failure, and the failure toast admits it ("failed — reverting"). `widgetEmpty` vs `widgetError` distinguishing "no data yet" from "the fetch broke" is exactly the right distinction for a trust-first dashboard — a blank widget never lies about which of the two happened.
- **The growth playbook is legible in the UI.** Value ladder (Cap. 2), growth loops (Cap. 3), pipeline math (Cap. 4), scorecard (Cap. 6), forecast, and unit economics each map to a named, color-coded, empty-state-covered section in a deliberate top-to-bottom narrative (scorecard → stats → economics → math → forecast → mix → ladder → catalog → board). The ladder/catalog split correctly separates *demand* (active deals per rung) from *supply* (productized offers per rung).
- **The polish program (P0/P1/P2 comments) is visibly disciplined** — skeletons, sticky-nav measurement, reduced-motion, mobile carousel are all annotated and consistently applied.

**The one place design undercuts the goals is accessibility of secondary text (H1).** A dashboard whose value is dense, scannable secondary detail — hints, deltas, sub-labels, empty-state guidance — renders precisely that layer below the AA contrast floor at 11px. It affects readability for everyone in bright light, not just users with low vision, and the fix is a two-token swap (`zinc-500→zinc-400`, `zinc-600→zinc-500`) plus bumping the 9 remaining 10px labels to 11px. That single pass would move the checklist from 10✅/4⚠️/1❌ to effectively clean.

**Recommended fix order:** H1 (token swap) → M1 (dialog semantics + a shared modal open/close helper with Esc + focus) → M3 (migrate 7 native `confirm()` to `confirmAction`) → L1/L2 in the same pass.

---

## 4. Addendum — second independent pass (2026-07-11, verification audit)

A second auditor re-ran the audit from scratch. **Every number above reproduced independently** (zinc-500 3.67:1 on surface / 4.12:1 on bg at ~335 uses; 9× `text-[10px]`; `node --check` clean on the extracted script; all 24 referenced `COLOR_TOKENS` keys defined; Esc gap on the CRM modal family confirmed at the global handler, L12324–12347). Findings §2 stand as written. The second pass surfaced four items not covered above:

**A1 (Medium) — Toasts are invisible to screen readers.** `#toast-stack` (L1939) has no `aria-live`/`role="status"`, and `toast()` (L8718) never routes through the existing `#sr-live` announcer (L1941, currently used only for drag-drop via `announce()`, L9020). Since toasts are the *sole* confirmation channel for most mutations — the core of design goal 1 — non-sighted users receive zero mutation feedback. Cheapest fix: `role="status" aria-live="polite"` on the stack container; it composes with M1's modal work.

**A2 (Low) — An interactive control at 10px.** `.crm-inline-select { font-size: 10px }` (L85) — the inline stage/product taggers on deal cards. Worse than the 9 decorative `text-[10px]` labels in L1 because it's a form control users must read to operate. Fold into the L1 pass.

**A3 (Low) — Specific keyboard-unreachable click targets** (concretizing L4's "worth a sweep"): deal-title span → `openEntity` (L5984), project chip span → `openProjectDetail` (L8243), session-link div → `openSessionPanel` (L8466), resend-from-history div (L11157), and the expandable month rows in the pipeline-temporal table (`<tr onclick>`, L3829 — no tabindex/role/Enter, so the per-month event drill-down is mouse-only).

**A4 (Info) — Mixed-language strings.** Spanish surfaces inside otherwise-English UI: trend tooltips "pipeline ↑ vs mes anterior" (L3820–3822), Track A header "Datos → IA" (L802). If bilingual playbook terminology is intentional, ignore; tooltips flipping language mid-table reads as an oversight.

Adjusted fix order with the addendum: **H1 → M1 (+A1 in the same accessibility pass) → M3 → L1 (+A2) → L4 (+A3).**
