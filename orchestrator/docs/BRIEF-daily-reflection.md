# Daily Reflection System — Design Brief (v2)

**Status:** design updated, ready for implementation
**Scope:** Hermes Orchestrator dashboard + Telegram rituals
**Metodología:** Reflection–Action Loop (Harvard Business School, 15-min end-of-day debrief)

---

## 1. Visión

Un espacio **Personal** en el dashboard donde el operador haga dos pausas al día:

- **Mañana (~7:15 am):** definir 1-3 intenciones para el día (qué quiero lograr, en qué enfocarme).
- **Noche (~6:45 pm):** 15-min end-of-day reflection con el formato Harvard:
  1. What went well? (1-3 wins + por qué funcionaron)
  2. What didn't go as planned? (1-2 momentos clave + qué pasó + por qué)
  3. What will I do differently? (1-2 ajustes concretos para mañana)

La reflexión nocturna se alimenta del **day review** (timeline de actividades) pero vive en su propia sección Personal.

---

## 2. Metodología: Reflection–Action Loop (Harvard B School)

Investigación de Harvard Business School: 15 minutos de reflexión al final del día mejora productividad ~23%.

### Estructura (3 partes, ~5 min cada una)

**Parte 1 — What went well? (~5 min)**
- Identificar 1-3 wins o acciones efectivas
- Anotar por qué funcionaron
- Hacer visible el progreso (dopamine hit)

**Parte 2 — What didn't go as planned? (~5 min)**
- Elegir 1-2 momentos clave que no cumplieron expectativas
- Preguntar: ¿Qué pasó? ¿Por qué pudo pasar así?
- Matter-of-fact, sin auto-castigo

**Parte 3 — What will I do differently? (~5 min)**
- Extraer 1-2 ajustes concretos para mañana
- Escribirlos como próximos pasos específicos
- Cerrar con una acción, no con una abstracción

**Formato:** facts → meaning → next step. No gratitud religiosa, no examen de conciencia. Productividad secular.

### Variante matutina (lighter, 5 min)

La mañana no necesita el formato completo. Solo:
- 1-3 intenciones para el día (qué quiero lograr/hacer)
- Opcional: 1 cosa que me preocupa → convertirla en acción

---

## 3. Diseño de la sección "Personal" en el dashboard

### 3.1 Ubicación

- Nuevo workspace/tab **"Personal"** al lado de Today.
- Sub-views:
  - `reflection` — reflexión diaria (default)
  - `health` — lo que hoy vive en Wellness (renombrar sub-nav)
  - `plate` / `supplements` — se mueven desde Wellness
- En Today se conserva una **tarjeta resumen** de la reflexión matutina.

### 3.2 Layout de la sub-view Reflection

```
┌─────────────────────────────────────────┐
│  🧘 Reflexión — 20 jul 2026              │
├─────────────────────────────────────────┤
│  ☀️  Mañana (si existe)                   │
│  · Intención 1: cerrar el deal de Acme   │
│  · Intención 2: revisar PR de Orion      │
│  · Intención 3: leer 30 min              │
├─────────────────────────────────────────┤
│  🌙 Noche — Reflection–Action Loop       │
│                                           │
│  ✅ What went well?                       │
│  1. Cerré el schema de Acme               │
│     → Funcionó porque lo hice temprano   │
│  2. PR de Orion mergeado                  │
│     → Bloque de 2h sin interrupciones     │
│                                           │
│  ⚠️ What didn't go as planned?            │
│  1. No leí (solo 10 min)                  │
│     → Reuniones se extendieron            │
│                                           │
│  🔄 What will I do differently?           │
│  1. Bloquear 30 min de lectura a las 7am │
│  2. Timeboxear reuniones a 45 min max     │
├─────────────────────────────────────────┤
│  📓 Historial (últimos 7 días)           │
│  19 jul · 18 jul · 17 jul · ...          │
└─────────────────────────────────────────┘
```

- Si no existe morning: muestra prompt editable "¿Qué quieres lograr hoy?" (1-3 intenciones).
- Si no existe evening y ya son >6 pm: muestra el formato Reflection–Action Loop.
- Cada sección tiene botón **Editar** y **Guardar**.
- Si el day review ya corrió (18:30), pre-llenar "What went well?" con los top items del timeline.

---

## 4. Almacenamiento

### 4.1 Tabla `daily_reflections` en `kanban.db`

```sql
CREATE TABLE IF NOT EXISTS daily_reflections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,          -- YYYY-MM-DD, local time
    morning_intentions  TEXT,                       -- JSON array of 1-3 strings
    morning_created_at TEXT,                       -- ISO timestamp
    evening_wins       TEXT,                       -- JSON array of {what, why}
    evening_misses     TEXT,                       -- JSON array of {what, what_happened, why}
    evening_adjustments TEXT,                      -- JSON array of {action, when}
    evening_created_at TEXT,                       -- ISO timestamp
    day_review_data    TEXT,                        -- JSON snapshot from day-review.py (optional)
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 4.2 Index

```sql
CREATE INDEX IF NOT EXISTS idx_reflections_date ON daily_reflections(date);
```

---

## 5. Backend — dashboard/reflection.py

```python
"""Daily Reflection — Reflection–Action Loop (Harvard B School format)."""

import json, sqlite3
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "kanban.db"

def get_reflection(date_str: str | None = None) -> dict:
    """Get reflection for a date (default today). Returns empty structure if none."""

def save_morning(date_str: str, intentions: list[str]) -> dict:
    """Save morning intentions (1-3 strings)."""

def save_evening(date_str: str, wins: list[dict], misses: list[dict], adjustments: list[dict]) -> dict:
    """Save evening reflection.
    wins: [{what: str, why: str}]
    misses: [{what: str, what_happened: str, why: str}]
    adjustments: [{action: str, when: str}]
    """

def get_history(days: int = 7) -> list[dict]:
    """Get last N days of reflections for history view."""

def prefill_from_day_review(date_str: str) -> dict:
    """If day-review.py ran, pre-fill wins from top activities."""
```

---

## 6. API Endpoints (6)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/reflection?date=YYYY-MM-DD` | Get reflection for date (default today) |
| POST | `/api/reflection/morning` | Save morning intentions `{date, intentions: [str]}` |
| POST | `/api/reflection/evening` | Save evening reflection `{date, wins, misses, adjustments}` |
| GET | `/api/reflection/history?days=7` | Get last N days |
| GET | `/api/reflection/prefill?date=YYYY-MM-DD` | Get prefill data from day-review |
| PUT | `/api/reflection/morning` | Edit morning intentions (same body as POST) |

---

## 7. Cron Jobs (2)

### 7.1 Morning Intentions — 7:15 am

```
Schedule: 15 7 * * *
Deliver: origin (Telegram)
Prompt: |
  Es la mañana. Pregunta al operador qué quiere lograr hoy.
  
  Formato simple — 1 a 3 intenciones:
  "☀️ Buenos días. ¿Qué quieres lograr hoy?
   1. ...
   2. ...
   3. ...
   
   (Escribe 1-3 cosas. Las guardo en tu reflexión diaria.)"
  
  Si el operador responde, parsea las intenciones y guárdalas en:
  POST /api/reflection/morning {date: today, intentions: [...]}
  
  Si no responde en 30 min, no insistas.
```

### 7.2 Evening Reflection — 6:45 pm

```
Schedule: 45 18 * * *
Deliver: origin (Telegram)
Prompt: |
  Son las 6:45pm. Hora de tu reflexión de 15 minutos (Reflection–Action Loop).
  
  Primero, si el day review ya corrió, muéstrale el timeline breve:
  "📊 Tu día hoy:
   9am: Morning briefing
   10am-12pm: Claude Code en orchestratormaxxing
   2pm: Task completada — deploy dashboard
   ..."
  
  Luego pregunta las 3 partes:
  
  PARTE 1 — What went well? (~5 min)
  "✅ ¿Qué salió bien hoy? 1-3 cosas.
   Para cada una, ¿por qué funcionó?"
  
  PARTE 2 — What didn't go as planned? (~5 min)  
  "⚠️ ¿Qué no salió como esperabas? 1-2 momentos.
   ¿Qué pasó? ¿Por qué crees que pasó así?"
  
  PARTE 3 — What will I do differently? (~5 min)
  "🔄 ¿Qué harás diferente mañana? 1-2 ajustes concretos.
   Escríbelos como próximos pasos específicos."
  
  Cuando el operador responda las 3 partes, parsea y guarda:
  POST /api/reflection/evening {date, wins, misses, adjustments}
  
  Tono: directo, cálido, no religioso. Español mexicano. Sin preámbulos ni cierres (i-have-adhd).
```

---

## 8. UI — Dashboard Personal tab

### 8.1 Estructura HTML

Nuevo tab "Personal" en el nav, después de "Today". Sub-nav con: Reflection | Health | Plate | Supplements.

### 8.2 Reflection sub-view

- **Morning card** (arriba): si existe, muestra intenciones. Si no, muestra textarea + "Guardar" button.
- **Evening card** (abajo): si existe, muestra las 3 secciones. Si no y >6pm, muestra el formulario interactivo:
  - Wins: lista editable de {what, why} con botón "+ Add"
  - Misses: lista editable de {what, what_happened, why}
  - Adjustments: lista editable de {action, when}
  - Botón "Guardar reflexión"
- **History bar** (footer): últimos 7 días como pills clicables. Click carga esa fecha.
- **Prefill button**: "Usar Day Review" — llama `/api/reflection/prefill` y llena wins automáticamente.

### 8.3 Estilo

- Mismo design system del dashboard (dark theme, cards, border-radius)
- Iconos: ☀️ mañana, 🌙 noche, ✅ wins, ⚠️ misses, 🔄 adjustments
- Responsive: en móvil, las 3 secciones de evening se apilan verticalmente
- TDAH-friendly: secciones claras, máximo 5 items por lista, acciones visibles

---

## 9. Archivos a tocar

### Nuevos:
- `orchestrator/dashboard/reflection.py` — módulo backend
- `orchestrator/dashboard/templates/reflection.html` — partial (o inline en index.html)

### Modificados:
- `orchestrator/dashboard/templates/index.html` — nuevo tab "Personal" + sub-nav + reflection view
- `orchestrator/dashboard/app.py` (o server principal) — 6 nuevos endpoints
- `~/.hermes/kanban.db` — CREATE TABLE daily_reflections (ejecutar en startup)
- Cron jobs: 2 nuevos (7:15am morning, 6:45pm evening)

### Referencia:
- Day Review BRIEF: `~/.hermes/plans/2026-07-20_day-review-mechanism-brief.md`
- Day Review script: `~/.hermes/scripts/day-review.py`

---

## 10. Acceptance Criteria

### Backend
- [ ] Tabla `daily_reflections` creada en kanban.db
- [ ] `GET /api/reflection` devuelve reflection del día (o vacío si no existe)
- [ ] `POST /api/reflection/morning` guarda intenciones
- [ ] `POST /api/reflection/evening` guarda wins + misses + adjustments
- [ ] `GET /api/reflection/history?days=7` devuelve últimos 7 días
- [ ] `GET /api/reflection/prefill` devuelve datos del day review si existe

### Frontend
- [ ] Tab "Personal" visible en el nav
- [ ] Sub-view Reflection renderiza morning + evening + history
- [ ] Formulario de morning: textarea + save
- [ ] Formulario de evening: 3 secciones (wins, misses, adjustments) con listas editables
- [ ] History pills clicables cargan reflexiones pasadas
- [ ] Botón "Usar Day Review" pre-filla wins

### Cron
- [ ] Morning: 7:15am pregunta intenciones por Telegram
- [ ] Evening: 6:45pm hace Reflection–Action Loop por Telegram
- [ ] Ambos guardan en daily_reflections table

### Integración
- [ ] Evening cron usa day-review timeline si está disponible
- [ ] Dashboard muestra day review timeline + reflection en Personal tab