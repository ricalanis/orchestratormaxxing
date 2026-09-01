# Growth Dashboard — UX Design (5 phases × 3 dimensions)

**Role: Product/Design Lead.** UX for the Growth Operating Framework
(`growth-operating-framework.md`) on the existing hermes dashboard. Companion code
breakdown: `growth-implementation-tasks.md`.

## Design constraints (from the existing app)

- **Stack:** single-page `dashboard/templates/index.html`, Tailwind (vendored), dark
  zinc palette, section `<div id="...">` containers filled by `loadX()` JS functions,
  `switchTab()` routing with `?tab=` deep links, entity drawer via `openEntity()`.
- **Nav model:** 6 workspaces; **Strategy** owns sub-tabs `roadmap / crm (Pipeline) /
  growth`. We extend Strategy with one new sub-tab and enrich Today + CRM + Growth —
  no new workspace (one person, don't multiply chrome).
- **Idiom:** cards with `bg-zinc-900 border border-zinc-800 rounded-xl p-4`, 11–12px
  meta text, emoji section markers, `<details>` for long tables, no external requests.

## Information architecture (where each phase lives)

| Phase / dimension | Location |
|---|---|
| P1 Capture (operational) | Global header **“+ Contacto”** button → quick-add modal (works from any tab, mobile-first) + Events section in Growth |
| P2 Nurture (operational) | **Today tab: “Hoy toca” card** (follow-up queue) |
| P2 Nurture (tactical) | Deal drawer: cadence timeline + nurture manager |
| P3 Discovery (tactical) | Deal drawer: discovery panel (pre-call template + fireflies + score delta) |
| P4 Conversion (tactical) | CRM tab: proposals strip + conversion-path visualizer |
| P5 Review (strategic) | **NEW Strategy sub-tab `mensual` (“Mensual”)** — the monthly strategic view |
| Attribution/channels (strategic) | Growth tab: Events & Channels sections |

`WS_SUBS.strategy` becomes: `roadmap 🎯 · crm 📊 · growth 📈 · mensual 🗓️`. Add
`mensual` to `TAB_WORKSPACE` + `ROUTE_TABS`.

---

## C1 — Contact quick-add modal (Phase 1, operational)

**Trigger:** header button `+ Contacto` (visible on every tab) · keyboard `c` ·
`?tab=crm&quickadd=1` deep link (→ phone home-screen shortcut; this is the "QR" path:
the QR at an event points here).

**Layout (one column, 4 fields + 2 chips rows — 30-second budget):**
```
┌─ Nuevo contacto ────────────────────────────┐
│ Nombre y empresa   [ Ana Torres — Kavak   ] │   ← one smart field, "Name — Company"
│ Email o LinkedIn   [ ana@… / linkedin.com/…] │
│ Su problema (1 frase)                        │
│ [ reportes manuales, nadie confía en datos ] │
│ Evento  [▾ Data Day MTY (hoy) | + nuevo ]    │   ← defaults to today's event if any
│ Tier    (🔥 Hot) (🌤 Warm) (🌱 Largo plazo)  │   ← chip select, default Warm
│ Canal   (evento) (linkedin) (referral) (…)   │   ← chip select, default evento
│              [ Guardar y otro ]  [ Guardar ] │
└──────────────────────────────────────────────┘
```
- **“Guardar y otro”** keeps the modal open with Evento/Tier sticky — batch capture
  between talks. Toast confirms: *“Ana Torres → lead creado · cadencia 3-10-30-90
  iniciada”* (or *“→ agendar discovery”* if Hot, with a one-tap link).
- Role is optional (edit later in drawer); account is find-or-create by name.

**Data contract:** `POST /api/crm/quick-capture`
```json
{"name":"Ana Torres","company":"Kavak","email":"","linkedin_url":"…",
 "problem_statement":"reportes manuales…","event_id":"ev_…|null",
 "tier":"warm","channel":"evento"}
→ {"contact":{…},"deal":{…},"cadence":{"steps":4,"first_due":"2026-07-12"},
   "next_action":"nurture|schedule_discovery"}
```

**States:** duplicate email/LinkedIn → inline “ya existe → abrir” link (opens drawer,
no dup created). Offline/error → keep form values, retry button.

## C2 — “Hoy toca” follow-up queue (Phase 2, operational)

**Location:** Today tab, card directly under the day plan (operational = today's clock);
compact count badge also shown on Strategy→Pipeline header (“3 toques vencidos”).

**Layout (list of due items, 2-click logging):**
```
┌─ ✋ Hoy toca (4) ── 2 vencidos 🔴 ───────────────────────────┐
│ 🔴 Ana Torres · Kavak      paso 2/4 (día 10) · vencido 3d    │
│    "insight + recurso" · 📧 email     [✓ Hecho] [✎] [⏭ Saltar]│
│ 🟠 Luis P. · Femsa         paso 1/4 (día 1-3) · hoy          │
│    "scorecard + CTA call" · 📧        [✓ Hecho] [✎] [⏭]      │
│ · Marta G. · startup X     🔥 hot sin contacto 48h  [Agendar] │
└──────────────────────────────────────────────────────────────┘
```
- **[✓ Hecho]** = one click logs the touch with the step's channel (POST touch +
  step status `sent`) — optimistic UI, row slides out. **[✎]** opens the drawer at the
  nurture panel with the rendered template ready to copy. **[⏭]** marks `skipped`.
- Sort: overdue first (red), then due today, then hot-uncontacted alerts.
- Empty state: *“Nadie pendiente hoy — 100% cadencia al día ✅”*.

**Data contract:** `GET /api/growth/followups-today` →
`{"items":[{"deal_id","contact_name","account","step_id","step_number","total_steps",
"label","channel","due_date","days_overdue","kind":"cadence|hot_alert"}],
"overdue_count":2}`

## C3 — Cadence tracker (Phase 2, tactical — in the deal drawer)

**Location:** deal drawer (openEntity deal), new “Cadencia” block above events log.

```
Cadencia 3-10-30-90            cumplimiento 75%
 ●──────●──────○──────○
 d1 ✓   d10 ✓  d30 ·due 12 jul  d90 ·
 email  linkedin
[Generar teaching funnel]  [+ toque manual]
```
- Dots: filled green = sent (hover: date + channel), amber ring = due/overdue, hollow =
  future, grey strike = skipped. Compliance % = sent-within-±2d / elapsed steps.
- The same component renders any sequence type (cadence / teaching / hook) as stacked
  rows when several exist. “Generar teaching funnel” appears once cadence step ≥2 sent
  and no call booked.

**Data contract:** existing `GET /api/growth/nurture/{deal_id}` extended with
`sequence_type`, `channel`, plus `compliance_pct` per sequence; `PATCH
/api/growth/nurture/{step_id}` unchanged (`status: sent|skipped`).

## C4 — Nurture manager (Phase 2, tactical)

**Location:** same drawer block, expanded view (“✎” from C2 lands here).

- Step list with **rendered template** (placeholders filled from contact/deal/ICP) in a
  copy-ready `<textarea>` + `[Copiar]` + channel chip + reschedule date input.
- Template edits are per-step (`template_text` persisted); “Restaurar plantilla” resets.
- Teaching-funnel generation button → 5 steps (Problema→Lente→Método→Casos→Decisión),
  weekly `scheduled_date`, editable before first send.

## C5 — Discovery workflow panel (Phase 3, tactical)

**Location:** deal drawer, “Discovery” block, state-machine by call status.

**Before call** (discovery scheduled, no fireflies yet):
```
☎️ Discovery — vie 12 jul 10:00
Guion: 1 Frame · 2 Negocio · 3 Problema ("reportes manuales…")
       4 Restricciones · 5 Resumen · 6 Siguiente paso
Su problema: "reportes manuales, nadie confía en datos"    perfil: B
Meta: talk ratio <45% · ≥6 preguntas · acordar siguiente paso
```
(The 6-step script with their problem sentence inlined — glanceable during the call.)

**After call** (fireflies fetched):
```
Señales fireflies (12 jul)          [↻ Refetch] [Recalcular score]
talk 41% ✅ · preguntas 8 ✅ · filler 2 ✅ · action items 3 · sentimiento +
Score 47 → 68  (+21: fireflies 0→18, behavioral +3)   → PRIORIZAR: propuesta
```
- Score delta rendered as before→after with per-category breakdown (data already in
  `score_deal` result). The routing verdict (>60 propose / 30–60 nurture / <30 drip)
  is stated explicitly with a CTA button (“Crear propuesta” → C6).
- Auto-fetch: on drawer open, if a discovery event is <48h old and no fireflies row,
  fire fetch in background (existing endpoint) and show a spinner chip.

**Data contract:** existing `/api/crm/deals/{id}/fireflies` + `/fireflies/fetch` +
`/score`; panel needs `GET /api/crm/deals/{id}/discovery` composite →
`{"scheduled":…,"signals":…,"score_before":…,"score_after":…,"routing":"propose"}`.

## C6 — Proposal builder + strip (Phase 4, tactical)

**Strip (CRM tab, under pipeline columns):** one row per open proposal:
`Kavak · enviada 3 jul · Bueno $60k / Mejor $180k / Óptimo $420k · [decidir ▾]`.

**Builder (drawer block or modal from C5 CTA):**
```
┌─ Propuesta — 3 opciones ─────────────────────────────────┐
│         BUENO            MEJOR ★           ÓPTIMO         │
│ prod [▾ Sprint IA]   [▾ Sprint+Build F1] [▾ Build+Fract.] │
│ $    [ 60,000 ]      [ 180,000 ]         [ 420,000 ]      │
│ scope[ 2 sem, quick…] [ …            ]   [ … + retainer ] │
│ Perfil B sugiere: Build + Fractional      [Marcar enviada]│
└───────────────────────────────────────────────────────────┘
```
- Product dropdowns from the existing catalog (track A/B); profile-based suggestion
  line (A→Build, B→Build+Fractional, C→Fractional) prefills the Mejor column.
- “Marcar enviada” → proposal `sent`, deal stage → `proposal` (feeds scorecard KPI).
  “Decidir” → accepted option (bueno/mejor/óptimo) or rejected + reason; accepting sets
  deal value = option price, ladder stage = option's rung.

**Data contract:** `GET/POST /api/crm/deals/{id}/proposal`, `PATCH
/api/crm/proposals/{pid}` (`status`, `accepted_option`, `reason`).

## C7 — Conversion path visualizer (Phase 4, tactical/strategic)

**Location:** Growth tab, replacing/extending the current `#crm-ladder` value-ladder
block; also embedded read-only in Mensual.

```
Scorecard ──40%──▶ Sprint/Audit ──50%──▶ Build ──60%──▶ Fractional
  12 deals            5 deals            3 deals          2 deals
  $0                  $310k              $890k            $85k/mes
  (n=10 medido)       (prior)            (prior)
```
- Horizontal 4-node path; node = count of deals currently at that rung + won value;
  edge = measured rung→rung conversion (child-deal mechanics) with `(prior)` badge
  until n≥5. Clicking a node filters the All-deals table to that rung.

**Data contract:** `GET /api/growth/conversion-path` →
`{"rungs":[{"key":"iman","label":"Scorecard","deals":12,"won_value":0}…],
"edges":[{"from":"iman","to":"entrada","p":0.4,"measured":false,"n":3}…]}`

## C8 — Events & channels (Phases 1+5, strategic attribution)

**Growth tab, two new sections:**

**Eventos** — card per event with the attribution funnel inline:
```
🎪 Data Day MTY · 15 jul · [prep 5-5-5 ▾]     capturados 8 → calls 3 → deals 2 → won 1 ($180k)
```
- `[prep 5-5-5 ▾]` expands targets/relationships/connectors/CTA checklist (pre-event
  ritual); “+ Evento” inline form (name, date, kind, location).

**Canales** — compact table: channel · touches (30d) · leads · won · CLTV:CAC ·
tendencia sparkline. Rows from derived metrics (deal_events + lead_source +
acquisition_costs); no manual entry beyond acquisition costs (existing form).

**Data contracts:** `GET/POST /api/growth/events`, `GET
/api/growth/events/{id}/attribution`, `GET /api/growth/channels`.

## C9 — Monthly strategic view (Phase 5) — NEW sub-tab `mensual`

**Layout (single scroll, mirrors the 8-block review agenda):**
```
🗓️ Revisión mensual — Julio 2026        [◂ jun]  [Cerrar revisión 🔒]
┌ 1 Pipeline math ───────────┐ ┌ 2 Revenue mix vs 4F+2B+3S ┐
│ meta $300k · won $180k     │ │ F ▓▓░░ 2/4 · B ▓░ 1/2      │
│ weighted open $240k  🟡    │ │ S ▓▓▓ 3/3 ✅               │
└────────────────────────────┘ └────────────────────────────┘
┌ 3 Loops (leads/won por loop) ┐ ┌ 4 CLTV:CAC por canal ────┐
┌ 5 Scorecard del mes (4 semanas apiladas vs target) ───────┐
┌ 6 Precisión del scoring ──────────────────────────────────┐
│ banda >60: 3/4 won ✅ · 30-60: 1/6 · <30: 0/3  (n=13 ⚠️)  │
┌ 7 Plan 90 días (progreso milestones) ─────────────────────┐
┌ 8 Decisiones del mes ─────────────────────────────────────┐
│ [textarea 1-3 apuestas]                                    │
└───────────────────────────────────────────────────────────┘
```
- **Live vs frozen:** current month computes live; “Cerrar revisión” snapshots to
  `monthly_reviews` (badge 🔒 + timestamp). `[◂ jun]` pager renders frozen snapshots —
  numbers never shift retroactively.
- Blocks 1–5,7 reuse existing read models (pipeline_math, scorecard, growth_loops,
  cltv_cac split by channel, plan_milestones); 6 is new (won/lost by score band).
- Each block links to its tactical home (e.g. block 3 → Growth loops section).

**Data contract:** `GET /api/growth/monthly-review?month=2026-07` → `{"month","live":
bool,"blocks":{"pipeline_math":…,"revenue_mix":…,"loops":…,"cltv_cac_by_channel":…,
"scorecard_rollup":…,"scoring_accuracy":…,"plan":…},"decisions":"…","closed_at":null}`
· `POST /api/growth/monthly-review/close` `{month, decisions}`.

---

## Cross-cutting

- **Dimension legibility:** every new section carries a tiny dimension chip
  (`op · táct · estrat`) in its header, teaching the three-clock model in the UI itself.
- **Mobile:** C1 (quick-add) and C2 (Hoy toca) must be fully usable at 390px — they are
  the two phone-at-an-event surfaces. Everything else is desktop-first.
- **No new libraries.** Sparklines/paths are inline SVG; drag not required anywhere.
- **Empty/seed states:** every section renders a one-line explainer + primary action
  when empty (e.g. Canales: “Los canales se calculan de tus toques — registra el
  primero”). New tables ship with no seeds except the 4 default channels.
- **Latency:** Hoy toca + quick-add are optimistic; Mensual can load lazily per block.
