# Growth Operating Framework — Conversación → Contexto → Compromiso → Conversión → Entrega

**Role: Growth Hacker (Design Expert).** The complete lifecycle operating framework for
Ricardo's one-person data/AI consulting + agentic-product shop (Monterrey, MX). This doc is
the reusable playbook; the UX design lives in `growth-dashboard-design.md` and the code
breakdown in `growth-implementation-tasks.md`.

Spec: `~/.hermes/orchestration/specs/growth-operating-framework/spec.md`.

---

## 0. Operating principles

1. **One store, additive fields.** Everything hangs off the existing CRM
   (`accounts / contacts / deals / deal_events`). New concepts (events, cadences,
   proposals, channels) are new tables that *reference* deals/contacts — never a
   parallel pipeline.
2. **Every touch is an event.** `deal_events` stays the audit spine; the scorecard,
   cadence compliance, and channel metrics are all *derived* from events, never typed.
3. **Score gates the funnel.** Lead score (0–100, 4 categories: firmographic 30 /
   behavioral 30 / fireflies 25 / product_fit 20) is the router: `>60` → proposal,
   `30–60` → keep nurturing, `<30` → long-term drip or discard.
4. **Three dimensions, three clocks.** Operational = today (do the touch). Tactical =
   this week (move the right deals). Strategic = this month/quarter (is the system
   working). Every phase exposes all three.
5. **One person.** Any ritual that takes >15 min/day operationally or >90 min/month
   strategically is over-designed. Automation suggests; Ricardo decides.

---

## 1. The five phases

```
CONVERSACIÓN      CONTEXTO         COMPROMISO        CONVERSIÓN         ENTREGA
Phase 1 CAPTURE → Phase 2 NURTURE → Phase 3 DISCOVERY → Phase 4 CONVERSION → Phase 5 DELIVERY+REVIEW
evento→contacto   3-10-30-90       discovery call     propuesta 3 opciones  proyecto→retainer
                  teaching funnel  fireflies+score    producto↔perfil       revisión mensual
```

### Phase 1 — CAPTURE (Operational) · Conversación

**Objective:** meet someone → registered contact with context in ≤30 seconds.

**Pre-event ritual — "5-5-5 + One Offer" (Scorecard prep):**
- 5 *targets* (people/companies you want to meet — from ICP), 5 *existing relationships*
  to deepen, 5 *connectors* (people who know your targets).
- One clear CTA for the event: always the **Scorecard** (the Imán / lead-magnet offer).
- Stored on the event record (`events.prep` JSON: `targets[]`, `relationships[]`,
  `connectors[]`, `cta`), reviewed on the event card the morning of.

**The Context Card — 4-field capture (QR or manual):**
| Field | CRM destination |
|---|---|
| Nombre + rol + empresa | `contacts.name/role` + `accounts.name` (find-or-create) |
| Email o LinkedIn | `contacts.email` / `contacts.linkedin_url` |
| Su problema en una frase | `contacts.problem_statement` (NEW column) |
| Tier: 🔥 hot / 🌤 warm / 🌱 long-term | `contacts.tier` (NEW column) |

**What quick-add does (one write path):**
1. Find-or-create account → create contact (`source='evento'`, `source_notes=<event name>`).
2. Link contact ↔ event via `event_attendance` (NEW table) — this is the attribution row.
3. Create a deal at stage `lead` with `lead_source='evento'`, `growth_loop` per origin
   (usually `autoridad` if they came from a talk, `referido` if introduced).
4. Tier decides the follow-up machine: **hot** → schedule discovery directly (skip to
   Phase 3), **warm** → start the 3-10-30-90 cadence, **long-term** → start cadence but
   with the day-90 step only + quarterly clinic list.
5. Emit `deal_events(kind='captured_at_event', payload={event_id, tier})` → scorecard
   counts it as a lead; behavioral score starts accruing.

**Exit criterion:** contact exists with tier + problem statement + event attribution, and
either a cadence or a discovery call is scheduled. **Metric:** capture-to-first-touch
< 72h for 100% of hot/warm.

### Phase 2 — NURTURE (Operational + Tactical) · Contexto

**Objective:** turn a captured contact into a booked discovery call through valuable,
scheduled touches — never "just checking in".

**The 3-10-30-90 cadence** (post-event follow-up; generated automatically at capture):

| Step | Day | Touch | Channel default | CTA |
|---|---|---|---|---|
| 1 | 1–3 | Personalized email referencing *their problem sentence* + the promised asset (**Scorecard**) | email | "Agenda 30 min" (book call) |
| 2 | 10 | One insight relevant to their problem + one resource (post/case/tool) | email or LinkedIn | soft — reply/react |
| 3 | 30 | Case study matched to their industry/problem | email | "¿Te suena? 20 min" |
| 4 | 90 | Industry observation + invite to the quarterly clinic | email or WhatsApp | clinic RSVP |

- Stored as rows in `nurture_sequences` with `sequence_type='cadence_3_10_30_90'` (NEW
  column; existing rows default to `'hook'`), `scheduled_date` computed from capture date.
- Marking a step **sent** = logging the touch (`record_touch`) with the step's channel
  → `touch_count`/`last_touch_date` update → behavioral score updates. `next_touch_date`
  is always the next pending step — this powers the **"Hoy toca"** follow-up queue.
- **Cadence compliance** (tactical KPI): % of steps sent within ±2 days of schedule,
  per deal and aggregate. Overdue >7 days → deal flags 🔴 in pipeline health.

**The Teaching Funnel** (deeper nurture for warm leads who didn't book after step 2 —
5 emails, Spanish, one per week; `sequence_type='teaching'`):

1. **Problema** — name the problem better than they can.
   > *Asunto: El dato que nadie está viendo en {industria}*
   > La mayoría de las empresas de {industria} ya tienen los datos para {resultado},
   > pero están atrapados en {síntoma — ej. "reportes manuales que nadie lee"}. Cuando
   > platicamos en {evento} me dijiste: "{problema en una frase}". Eso no es un problema
   > de herramientas — es un problema de {reframe}. Esta semana te voy a mostrar cómo lo
   > veo. — Ricardo
2. **Lente** — the mental model you use (your positioning).
   > *Asunto: Cómo decido qué automatizar primero*
   > Mi lente: {posicionamiento del ICP — ej. "los agentes no reemplazan procesos, los
   > exponen"}. Antes de construir nada, mapeo {método corto}. Aplícalo a {su problema}
   > y dime qué sale.
3. **Método** — your method, step by step (the Sprint/Audit shape).
   > *Asunto: Los 3 pasos que uso con cada cliente*
   > Paso 1: {diagnóstico}. Paso 2: {quick win medible}. Paso 3: {sistema que se queda}.
   > Así es exactamente como corre un {producto Entrada — Sprint/Audit} de 2 semanas.
4. **Casos** — proof: one case per profile (A startup / B mediana / C grande).
   > *Asunto: De {estado inicial} a {resultado} en {tiempo}*
   > {Caso más parecido a su perfil, con número}. El patrón se repite: {patrón}.
5. **Decisión** — the ask, with the ladder visible.
   > *Asunto: ¿Empezamos con algo chico?*
   > Tres formas de trabajar juntos: {Scorecard gratis} → {Sprint/Audit $X} →
   > {Build $Y}. La mayoría empieza con el Sprint. ¿Agendamos 30 min esta semana?

Templates are code (rendered with `{name}`, `{industria}`, `{problema}`, `{evento}`,
`{producto}` placeholders from the deal/contact/ICP), editable per-send in the UI.

**Exit criterion:** discovery call booked (`deal_events kind='discovery_scheduled'`,
stage → `qualified`... booking is the *only* exit; cadence exhaustion loops back to the
90-day/quarterly list, never silently dies). **Metrics:** touch→call conversion,
cadence compliance %, median days capture→call.

### Phase 3 — DISCOVERY (Tactical) · Compromiso

**Objective:** one structured call → scored, qualified lead with a clear next step.

**Call structure (the pre-call template, visible during the call):**
1. **Frame** (2 min) — agenda + permission: diagnóstico, no pitch.
2. **Understand business** (8 min) — model, customers, how money is made.
3. **Probe problem** (10 min) — the problem sentence from capture: "cuéntame más";
   quantify cost of the problem (¿cuánto cuesta al mes no resolverlo?).
4. **Explore constraints** (5 min) — budget reality, timeline, who decides, data access.
5. **Summarize** (3 min) — mirror back; get a "sí, eso es".
6. **Propose next step** (2 min) — match to ladder: Sprint/Audit if problem is clear,
   Scorecard if not ready, Build/Fractional if urgent + funded.

**Post-call machine (automated):**
1. Fireflies records → `fetch_fireflies_for_deal` pulls transcript + signals (talk
   ratio, questions, filler, action items, sentiment) into `fireflies_meetings`.
2. `score_deal` recomputes: fireflies category (0–25) now has data; behavioral gets the
   meeting-within-14d bonus.
3. Stage: `lead → qualified` (score ≥ threshold) with the event logged.
4. Coaching panel surfaces the behavioral read (existing `get_coaching`): talk ratio
   target <45%, ≥6 questions, action items captured.

**Routing rule (the framework's core gate):**
- **Score > 60** → prioritize → Phase 4 (proposal within 5 business days).
- **30–60** → back to Phase 2 (teaching funnel) with a specific objection noted.
- **< 30** → long-term drip (day-90 + clinic) or discard; mark `stage='nurture'`.

**Metrics:** calls/week (scorecard KPI), call→proposal rate, avg score delta from
fireflies, talk-ratio trend.

### Phase 4 — CONVERSION (Tactical + Strategic) · Conversión

**Objective:** qualified lead → signed project, positioned on the value ladder.

**Product matching (profile → track → entry product):**

| Profile | Who | Recommended path | Entry |
|---|---|---|---|
| A — startup | <30 people, product-led | **Track B (Producto con Agentes)** → Build | Sprint |
| B — mediana | 30–300, data chaos | Track A (Datos→IA) Build + Track B optional → **Build + Fractional** | Audit/Sprint |
| C — grande | 300+, committees | **Fractional** (advisory-first) | Audit → Fractional |

**Proposal = always 3 options (Blair Enns — anchor high, sell the middle):**
- **Bueno** — smallest scoped win (usually the Entrada product: Sprint/Audit).
- **Mejor** — the recommended path (Entrada + first Build phase). *This is the anchor
  you expect to sell.*
- **Óptimo** — full path incl. retainer (Build + Fractional). Prices the ceiling.

Stored in a `proposals` table (deal_id, three option slots each linking a `product_id`
+ price + scope note, status draft/sent/accepted/rejected, sent_at, decided_at,
accepted_option). Sending a proposal emits `stage_changed → proposal` (scorecard KPI).

**Conversion path (the ladder as a machine):**
```
Scorecard (Imán, gratis)  →  Sprint / Audit (Entrada, ~$40–80k)  →  Build (Core, ~$150–400k)  →  Fractional (Recurrente, ~$40–80k/mes)
        p₁≈40%                        p₂≈50%                              p₃≈60%
```
- Each deal carries `value_ladder_stage`; a won deal at one rung **spawns a child deal**
  at the next rung (existing `parent_deal_id` mechanics) at stage `lead`, tier hot.
- The probabilities p₁–p₃ are *measured* from historical rung-to-rung conversions
  (child-deal won / parent-deal won), displayed on the visualizer — start with the
  priors above until n≥5 per rung.
- **Project-to-retainer is a play, not a hope:** every Build proposal's Óptimo option
  includes the Fractional; month 2 of every Build includes a "sistema que se queda"
  conversation logged as a touch.

**Metrics:** proposal→won rate, avg option sold (bueno/mejor/óptimo mix), rung
conversion probabilities, median days qualified→won.

### Phase 5 — DELIVERY + REVIEW (Strategic) · Entrega

**Objective:** deliver → convert to recurring → learn monthly.

**Delivery linkage (already exists):** won deal → `initiative_id` → epics → tasks in the
orchestrator. Delivery health is visible from the deal drilldown (tasks/commits).

**Weekly scorecard (existing, Cap. 6 — auto-derived, never typed):**
5 KPIs vs targets with WoW: leads · toques · discovery calls · contenido · propuestas.

**Monthly strategic review (NEW — the 90-minute ritual, first Friday of month):**

| # | Block | Question | Data |
|---|---|---|---|
| 1 | Pipeline math vs actual | On track to $300K/mes? | `pipeline_math` (needed) vs won+weighted open (actual) |
| 2 | Revenue mix vs target | 4 Fractional + 2 Builds + 3 Sprints? | won deals by ladder rung vs target mix |
| 3 | Growth loop attribution | Which loop produced the leads/wins? | `growth_loops` + event/channel attribution |
| 4 | CLTV:CAC by channel | Which channel is worth feeding? | `cltv_cac` split by channel (NEW) |
| 5 | Scorecard month roll-up | Did the weekly inputs happen? | 4–5 weekly scorecards aggregated |
| 6 | Scoring model accuracy | Do >60 scores actually convert? | won/lost rate by score band (NEW; retrain weights when n≥20) |
| 7 | 90-day plan | Milestones on schedule? | `plan_milestones` progress |
| 8 | Decisions | 1–3 bets for next month | typed into the review record |

Each review is **snapshotted** (`monthly_reviews` table: month, metrics JSON frozen at
review time, decisions text, completed_at) so month-over-month comparisons don't shift
as data changes. The dashboard view computes live; "Cerrar revisión" freezes it.

**Quarterly:** clinic event (the day-90 CTA destination) + ICP/positioning re-read +
scoring weight review.

---

## 2. Three dimensions — what each clock sees

**Operational (today, ≤15 min):** "Hoy toca" queue (cadence steps due + overdue, hot
contacts uncontacted 72h) · quick-add contact (30 s) · log touch in 2 clicks · trigger
nurture · schedule discovery. Lives on the **Today tab** + a header quick-add.

**Tactical (this week):** lead scoring board (sorted, banded >60 / 30–60 / <30) ·
pipeline health (🔴 stale / 🟡 slowing / 🔵 moving) · cadence compliance % · discovery
call quality (fireflies signals trend) · proposal pipeline (sent/pending/decided).
Lives on **Strategy → Pipeline (CRM)**.

**Strategic (month/quarter):** monthly review (above) · loop attribution · CLTV:CAC by
channel · revenue mix vs 4F+2B+3S target · scoring accuracy · 90-day plan. Lives on
**Strategy → Growth → Mensual**.

---

## 3. Data model (delta only — full DDL in growth-implementation-tasks.md)

**New tables:** `events` (the networking/conference events + 5-5-5 prep JSON) ·
`event_attendance` (contact↔event attribution) · `channels` (linkedin/whatsapp/email/
evento/inbound + per-channel notes; metrics derived) · `proposals` (3-option Enns
proposals) · `monthly_reviews` (frozen snapshots + decisions).

**New columns:** `contacts.tier` ('hot'/'warm'/'long_term') · `contacts.problem_statement`
· `nurture_sequences.sequence_type` ('hook'/'cadence_3_10_30_90'/'teaching') ·
`nurture_sequences.channel`.

**Reused as-is:** deals growth columns (`value_ladder_stage`, `growth_loop`,
`lead_source`, `lead_score`, `touch_count`, `last/next_touch_date`), `deal_events`
spine, `fireflies_meetings`, `acquisition_costs`, `plan_milestones`,
`conversion_snapshots`, products, ICP.

**Scoring impacts by phase:** capture → firmographic (source=evento, industry, profile);
nurture → behavioral (touches, recency); discovery → fireflies (25 pts) + behavioral
meeting bonus; conversion → product_fit (track match, ladder fit, expected value).
No new score categories — the framework *feeds* the existing 4.

---

## 4. Success criteria (from spec, restated as checks)

1. Event → contact in ≤30 s with tier + problem + attribution (Phase 1 quick-add).
2. Cadence visible + "who needs follow-up today" answerable in one glance.
3. Every discovery call ends with fireflies signals in the score within 24 h.
4. Every qualified lead gets a 3-option proposal linked to products.
5. Monthly strategic view exists, snapshottable, alongside the weekly scorecard.
6. Every won deal shows its event/channel/loop attribution chain.
