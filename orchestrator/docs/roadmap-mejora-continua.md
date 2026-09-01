# Roadmap: Orquestador de Mejora Continua

## Diseño del Sistema Multi-Expert

### Arquitectura General

```
El operador
    ↓
Hermes (GLM-5.2, orquestador principal)
    ↓ delegate_task / tmux
Orquestador de Mejora Continua
    ├── Experto en Diseño (UX/UI)
    ├── Experto en Producto (funcionalidad, user journey)
    └── Experto en Desarrollo (código, tests, performance)
         ↓
    Claude Code (Fable 5 / Opus / Kimi-coder)
         ↓
    Dashboard + Ops + MCP + CRM
```

### Principios

1. **Un orquestador, tres líderes virtuales** — no son agentes separados, son roles dentro de un mismo prompt de Claude Code. Una sesión, una visión coherente.
2. **Ciclo de mejora continua** — cada ciclo: auditar → diagnosticar → planificar → ejecutar → verificar → reportar.
3. **Autonomía overnight** — Claude Code trabaja solo, Hermes monitorea cada X tiempo, el operador despierta con todo ejecutado.
4. **Modelos por tarea** — Fable 5 para diseño/arquitectura, Opus para código complejo, Kimi-coder para tareas mecánicas, GLM para review semántico.

---

## Diagnóstico del Estado Actual (2026-07-08)

### Dashboard

| Área | Estado | Issues |
|---|---|---|
| Kanban board | ✅ Funcional | 114 tasks, 9 en review sin aceptar |
| Sessions tab | ✅ Origin badges añadidos | Commit `432584d` |
| Scorecard | ✅ Rediseñado (crmKpiCard, pipeline math) | Commit `0d235ba` |
| CRM/Deals | ✅ Stalled stage + auto-decay | Commits `4d362f6`, `a142843` |
| MCP tools | ✅ 165 tools, 6 fases completas | Todo en producción |
| Roadmap tab | ✅ Funcional | Initiatives con progress derivado |
| Memory/Graph | ✅ 617 nodes, metabolism widget | Phase 2-3 completa |
| **Dashboard service** | ❌ **CAÍDO** | Inactivo desde 21:17, necesita restart |
| Products UI | ⚠️ Pendiente | Product selector en deal modal no built |
| Growth data | ⚠️ Vacío | ICP, products, 90-day plan sin popular |

### Operaciones

| Área | Estado | Issues |
|---|---|---|
| Supervisor cron | ✅ 5min, hermes-* only | Funcionando |
| Idle notifier | ✅ 15min, Fibonacci | Funcionando |
| VM scheduling | ✅ 7am-midnight | GCP VM |
| Stale deal decay | ✅ Daily 8am | Funcionando |
| **GLM Reasoning A/B** | ❌ **BUG** | `os.path.expandpath` → debería ser `os.path.expanduser` |
| **Tech Event Scout** | ❌ **Error** | `RuntimeError: Model generated invalid tool call: execute` |
| Memory consolidation | ✅ 15min | Funcionando |
| Ollama usage refresh | ✅ 30min | Funcionando |

### Necesidades del operador

| Necesidad | Estado | Prioridad |
|---|---|---|
| Despertar con revisión ejecutada | ❌ No existe | **P0** |
| MCP tunnel para Cowork | ⚠️ Opción 1 elegida, Funnel configurado en 10443 | P1 |
| WormBase MVP | ❌ No empezado | P1 |
| Growth pipeline poblado | ❌ Vacío | P1 |
| Cliente X Mid-Year Review | ⚠️ Task creada, sin empezar | P2 |
| MVP piloto | ⚠️ Await client response | P2 |
| Products UI en dashboard | ❌ No built | P2 |
| Project description endpoint | ❌ No existe | P3 |
| Contact update endpoint | ❌ No existe | P3 |

---

## Roadmap de Mejora Continua

### Ciclo 1: Estabilización + Morning Briefing (tonight → mañana)

**Objetivo:** Despertar con dashboard funcionando, bugs arreglados, y un briefing ejecutado.

#### Fase 1A: Stop the bleeding (30 min)
- [ ] Reiniciar dashboard service
- [ ] Fix GLM Reasoning A/B cron (`os.path.expandpath` → `os.path.expanduser`)
- [ ] Fix Tech Event Scout cron (tool call error)
- [ ] Aceptar las 9 tareas en review (bulk accept)
- [ ] Verificar que todos los crons están saludables

#### Fase 1B: Morning Briefing (ejecutar antes de 7am)
- [ ] Crear endpoint `/api/morning-briefing` que compile:
  - Tasks done yesterday + carried over
  - Active deals + pipeline health
  - Session activity summary
  - Cron health check
  - Token budget status
  - Roadmap progress
- [ ] Crear cron job que ejecute el briefing a las 6:50am y lo envíe a Telegram

#### Fase 1C: Growth data population
- [ ] Popular ICP config (industries, positioning, target_revenue, avg_ticket, close_rate)
- [ ] Crear 3 productos default (Sprint 1, Sprint 2, Retainer)
- [ ] Verificar que scorecard muestre datos reales

### Ciclo 2: Multi-Expert Audit (mañana → tarde)

**Objetivo:** Los 3 expertos virtuales auditan el dashboard completo y generan plan de mejora.

#### Fase 2A: Design Expert audit
- [ ] Audit visual consistency: empty states, loading states, toasts, feedback
- [ ] Audit responsive design (mobile, tablet)
- [ ] Audit color palette consistency
- [ ] Audit information hierarchy (what's important vs noise)
- [ ] Audit accessibility (contrast, keyboard nav, ARIA)
- [ ] Output: `docs/design-audit.md` con issues priorizados

#### Fase 2B: Product Expert audit
- [ ] Audit user journey: can the operator do X end-to-end from the UI?
  - Create deal → move stages → win/lose
  - Create task → assign → track → complete
  - View roadmap → drill into initiative → see tasks
  - Check sessions → identify idle → take action
  - View scorecard → understand pipeline health
- [ ] Audit feature completeness vs roadmap initiatives
- [ ] Audit data quality (empty fields, missing relationships)
- [ ] Output: `docs/product-audit.md` con gaps priorizados

#### Fase 2C: Development Expert audit
- [ ] Audit code quality: no regressions, tests pass, no broken endpoints
- [ ] Audit performance: page load time, API response times
- [ ] Audit security: no secrets in code, auth on sensitive endpoints
- [ ] Audit DB schema: missing indexes, orphaned FKs, stale data
- [ ] Audit test coverage: what's tested, what's not
- [ ] Output: `docs/dev-audit.md` con issues priorizados

#### Fase 2D: Joint plan (los 3 expertos juntos)
- [ ] Sintetizar los 3 audits en un plan unificado priorizado
- [ ] Crear kanban tasks para cada issue encontrado
- [ ] Asignar a Claude Code para ejecución
- [ ] Output: `docs/improvement-plan.md`

### Ciclo 3: Execution + Verification (tarde → noche)

**Objetivo:** Ejecutar el plan de mejora y verificar que todo funciona.

#### Fase 3A: Execute fixes (Claude Code autónomo)
- [ ] Fix P0 issues del audit conjunto
- [ ] Fix P1 issues
- [ ] Commit por cada fix (atomic commits)
- [ ] Run tests after each fix

#### Fase 3B: Verification
- [ ] Restart dashboard, verify all endpoints
- [ ] Run smoke tests: kanban, CRM, sessions, roadmap, scorecard
- [ ] Check all crons healthy
- [ ] Verify token budget

#### Fase 3C: Report to the operator
- [ ] Generate summary: what was fixed, what was improved, what's pending
- [ ] Update kanban tasks with results
- [ ] Notify via Telegram

---

## Implementación Técnica

### Cómo se ejecuta

1. **Hermes crea el prompt multi-expert** con todo el contexto del diagnóstico
2. **Una sola sesión Claude Code** (Opus o Fable si disponible) recibe el prompt
3. **Claude Code ejecuta las 3 fases** del Ciclo 2 secuencialmente (audit design → audit product → audit dev → joint plan)
4. **Hermes monitorea cada 15-20 min** con `tmux capture-pane`
5. **Cuando termina, Hermes verifica** los outputs (docs generados, commits)
6. **Hermes notifica al operador** con el resumen

### Prompt template para Claude Code

```
Act as THREE experts working in tandem on the Hermes Orchestrator dashboard:

DESIGN EXPERT: Audit visual consistency, responsive design, color palette, 
information hierarchy, accessibility. Output docs/design-audit.md.

PRODUCT EXPERT: Audit user journey completeness, feature gaps, data quality.
Output docs/product-audit.md.

DEVELOPMENT EXPERT: Audit code quality, performance, security, test coverage.
Output docs/dev-audit.md.

Then synthesize all three into a joint prioritized improvement plan.
Output docs/improvement-plan.md.

For each audit:
1. Read the relevant code (templates/index.html, api.py, crm.py, growth.py)
2. Check the live dashboard at http://127.0.0.1:3000
3. Document issues with severity (P0/P1/P2/P3) and specific file/line references
4. Suggest concrete fixes

Context: [pegar diagnóstico completo del estado actual]
```

### Cron de monitoreo

Hermes crea un cron job que cada 20 min:
1. Captura el pane de la sesión Claude Code
2. Si está activa → `[SILENT]`
3. Si está idle → verifica si commiteó, notifica progreso
4. Si murió → notifica error

### Modelos por fase

| Fase | Modelo | Razón |
|---|---|---|
| Design audit | Opus (o Fable si disponible) | Necesita razonamiento visual profundo |
| Product audit | Opus | Necesita entender user journeys |
| Dev audit | Opus | Necesita leer código y encontrar bugs |
| Joint plan | Opus | Síntesis de 3 perspectives |
| Fix execution | Opus / Kimi-coder | Opus para complejos, Kimi para mecánicos |
| Verification | GLM (Hermes) | Review semántico, no código |

---

## Próximos Pasos

1. **Ahora:** Generar este documento ✅
2. **Ahora:** Crear kanban task para la ejecución
3. **Ahora:** Escribir el prompt multi-expert para Claude Code
4. **Cuando el operador apruebe:** Lanzar Claude Code con autonomía overnight
5. **Mañana 6:50am:** Cron de morning briefing despierta al operador con todo listo