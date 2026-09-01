"""
CRM Growth System — the growth layer on top of the Phase-6 CRM (crm.py).

The operator runs a one-person data-science/AI/agentic-dev shop. The lead-gen
playbook (docs/lead-gen-playbook.md) defines a growth engine; this module maps
that engine onto the existing CRM (accounts / contacts / deals / initiatives)
and keeps it LEAN — one person, simplicity over features.

Four concepts, all additive to `deals` (never a new deal store):

  • VALUE LADDER (playbook Cap. 2): where a deal sits on the offer ladder —
        Imán → Entrada → Core → Recurrente.
  • GROWTH LOOP (Cap. 3): which flywheel produced/serves the deal —
        Autoridad→Inbound · Cliente→Referido · Producto→Datos.
  • LEAD SCORING (Cap. 4): a 0–100 score from simple, transparent rules
        (source weight + engagement + industry match), backed by a
        `lead_scoring_features` row per lead.
  • TOUCH TRACKING (Cap. 4): touch_count / last_touch_date / next_touch_date so
        no active deal goes cold.

Plus the read models the dashboard needs:
  • pipeline_math()   — backward from a revenue goal to leads & touches needed.
  • scorecard()       — the weekly 5 (leads, touches, discovery calls,
                        content, proposals), AUTO-derived from the week's events.
  • growth_loops()    — per-loop leads / conversion / output→input ratio.
  • content_cadence() — content pieces per week + the publishing streak.

Doctrine (same as crm.py): additive ALTERs only, one validated write path per
mutation, every touch/score an event on deal_events.
"""
import datetime
import datetime as _dt
import json
import os
import time
from typing import Optional

from . import db
from . import crm

# --- closed vocabularies (validated at the write path) ---------------------
VALUE_LADDER = ("iman", "entrada", "core", "recurrente")
GROWTH_LOOP = ("autoridad", "referido", "producto")
LEAD_SOURCE = ("linkedin", "evento", "referral", "cold_email", "inbound")
CONTENT_CHANNEL = ("blog", "linkedin", "twitter", "youtube", "newsletter", "other")
CONTENT_STATUS = ("idea", "draft", "scheduled", "published")

# --- lead-scoring rule weights (Cap. 4: firmographic + behavioral + intent) --
# source weight (0–40): referral/inbound are warmest; cold_email coldest.
_SOURCE_WEIGHT = {
    "referral": 40, "inbound": 35, "linkedin": 25, "evento": 20, "cold_email": 10,
}

# New 4-category scoring weights (CRM Lead Scoring + Fireflies Integration).
# Source quality has been moved into the Firmographic category.
_FIRM_WEIGHTS = {
    "source_quality": {"referral": 12, "inbound": 10, "linkedin": 8,
                       "evento": 6, "website": 4, "other": 4, "cold_email": 2,
                       "whatsapp": 4},
    "industry_match": 12,
    "profile_fit": {"A": 2, "B": 4, "C": 6},
}
_BEHAVIOR_WEIGHTS = {
    "touch_count": {"per_touch": 2, "cap": 10},
    "recency": {"7d": 10, "14d": 7, "30d": 4, "older": 0},
    "meeting": {"discovery_14d": 10, "meeting_14d": 6, "none": 0},
}
_FIREFLIES_WEIGHTS = {
    "talk_ratio": {"45": 8, "55": 5, "64": 2, "over": 0},
    "questions": {"6": 6, "4": 4, "2": 2, "0": 0},
    "filler": {"3": 3, "5": 1, "bad": 0},
    "action_items": {"3": 4, "1": 2, "0": 0},
    "sentiment": {"positive": 4, "neutral": 2, "negative": 0},
}
_PRODUCT_WEIGHTS = {
    "track_match": 5,
    "interest": 5,
    "ladder_fit": 5,
    "expected_value": {"80k": 5, "40k": 4, "20k": 3, "10k": 2, "low": 1, "none": 0},
}

_CATEGORY_MAX = {
    "firmographic": 30,
    "behavioral": 30,
    "fireflies": 25,
    "product_fit": 20,
}

# ICP industries — an industry match is worth the full firmographic-fit bonus.
# ENV is the FALLBACK; the live value comes from icp_config() (DB → env). Keep
# this set as the default the ICP editor starts from.
_ENV_ICP_INDUSTRIES = {
    s.strip().lower() for s in os.environ.get(
        "HERMES_ICP_INDUSTRIES",
        "saas,fintech,ecommerce,logistics,healthtech,edtech,retail,manufacturing"
    ).split(",") if s.strip()
}
_ENV_POSITIONING = os.environ.get("HERMES_POSITIONING", "")

# --- pipeline-math defaults (Cap. 4 backward funnel), env-overridable --------
# Monthly revenue goal (MXN). The playbook names an active $100k MXN pipeline;
# default the monthly bet lower and let the operator override per real target.
def _envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


GROWTH_CONFIG = {
    # Part-time practice defaults: ~10 h/week of practice, ≤6 h/week of
    # meetings. Goal = ~2 concurrent recurring engagements at the floor rate;
    # a deal = a quarterly engagement (45k MXN). Referral-warm close rate.
    "revenue_goal": _envf("HERMES_REVENUE_GOAL", 30_000.0),    # MXN / month
    "avg_ticket": _envf("HERMES_AVG_TICKET", 45_000.0),        # MXN / deal (quarter)
    "close_rate": _envf("HERMES_CLOSE_RATE", 0.5),             # won / proposal
    "proposal_rate": _envf("HERMES_PROPOSAL_RATE", 0.5),       # proposal / discovery
    "discovery_rate": _envf("HERMES_DISCOVERY_RATE", 0.3),     # discovery / lead
    "touches_per_lead": _envf("HERMES_TOUCHES_PER_LEAD", 5.0),
    # CLTV inputs (Cap. 8 unit economics): CLTV = avg_ticket * repeat * lifespan.
    "repeat_purchases": _envf("HERMES_REPEAT_PURCHASES", 1.5),      # purchases / customer
    "avg_lifespan_months": _envf("HERMES_AVG_LIFESPAN_MONTHS", 12.0),
    "currency": os.environ.get("HERMES_CURRENCY", "MXN"),
}

# Revenue mix target model (operator's target mix — not the backward funnel).
# These are the intended monthly revenue streams that sum to the goal coverage.
REVENUE_MIX = [
    {"stream": "Recurring advisory", "units": 2, "unit_price_mxn": 15_000.0,
     "revenue_mxn": 30_000.0, "share": 1.00, "value_ladder_stage": "recurrente"},
    {"stream": "Pro-bono help", "units": 1, "unit_price_mxn": 0.0,
     "revenue_mxn": 0.0, "share": 0.00, "value_ladder_stage": "iman"},
]

# Client profile probability model (operator's target mix of customer types).
# Advisory era: p_fractional reads as "recurring mentorship"; p_core_build as
# "one-off paid build" (rare — execution belongs to the client's team or an
# executor from the bench, not to the operator).
CLIENT_PROFILES = [
    {
        "key": "A",
        "label": "Recurring advisory — established team",
        "share": 0.50,
        "p_core_build": 0.05,
        "p_fractional": 0.90,
    },
    {
        "key": "B",
        "label": "Pro-bono help — nonprofit",
        "share": 0.25,
        "p_core_build": 0.00,
        "p_fractional": 0.10,
    },
    {
        "key": "C",
        "label": "Pro-bono help — new business",
        "share": 0.25,
        "p_core_build": 0.05,
        "p_fractional": 0.20,
    },
]

_DISCOVERY_KINDS = ("discovery_call", "discovery", "call", "meeting")
_TOUCH_KIND = "touch"
# Motor-caliente kinds (2026-08-09): the operator's layer of the scorecard. A warm
# touch IS a touch (counted by the legacy KPI) but is attributed to the human
# layer, not the fleet's cadence layer.
_WARM_KINDS = ("warm_touch", "referral_ask")


# ==========================================================================
# ICP config — DB-backed (icp_config table), with env vars as the fallback.
# The ICP editor (Strategy tab) writes rows; every read below layers those
# rows on top of the env defaults so an unset field always has a sane value.
# ==========================================================================

def _icp_rows() -> dict:
    """Raw {key: value} from the DB layer — never raises (feature just off)."""
    try:
        return db.get_icp_config()
    except Exception:
        return {}


def icp_config() -> dict:
    """The EFFECTIVE ICP config (DB overrides on top of env defaults), shaped
    for the API / editor: industries list · positioning_statement ·
    target_revenue · avg_ticket · close_rate."""
    rows = _icp_rows()

    ind = rows.get("industries")
    if ind is not None:
        industries = [s.strip().lower() for s in ind.split(",") if s.strip()]
    else:
        industries = sorted(_ENV_ICP_INDUSTRIES)

    def _num(key: str, default: float) -> float:
        v = rows.get(key)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return {
        "industries": industries,
        "positioning_statement": rows.get("positioning_statement") or _ENV_POSITIONING,
        "target_revenue": _num("target_revenue", GROWTH_CONFIG["revenue_goal"]),
        "avg_ticket": _num("avg_ticket", GROWTH_CONFIG["avg_ticket"]),
        "close_rate": _num("close_rate", GROWTH_CONFIG["close_rate"]),
        "currency": GROWTH_CONFIG["currency"],
    }


def icp_industries() -> set:
    """The live ICP-industry set used by lead scoring (DB → env fallback)."""
    return {s.lower() for s in icp_config()["industries"]}


def growth_config() -> dict:
    """GROWTH_CONFIG with the ICP-editable fields (revenue goal / avg ticket /
    close rate) overlaid from the DB. This is what the funnel math reads."""
    cfg = dict(GROWTH_CONFIG)
    icp = icp_config()
    cfg["revenue_goal"] = icp["target_revenue"]
    cfg["avg_ticket"] = icp["avg_ticket"]
    cfg["close_rate"] = icp["close_rate"]
    return cfg


def set_icp(updates: dict) -> dict:
    """Validate + persist ICP-editor fields. Returns the new effective config.
    Accepts industries as a list OR a comma-separated string; close_rate as a
    fraction in [0,1] (a value >1 is treated as a percentage)."""
    db.ensure_icp_schema()
    to_write: dict = {}

    if "industries" in updates:
        val = updates["industries"]
        if isinstance(val, (list, tuple)):
            items = [str(s).strip().lower() for s in val if str(s).strip()]
        else:
            items = [s.strip().lower() for s in str(val or "").split(",") if s.strip()]
        to_write["industries"] = ",".join(items)

    if "positioning_statement" in updates:
        to_write["positioning_statement"] = str(updates["positioning_statement"] or "").strip()

    for key in ("target_revenue", "avg_ticket"):
        if key in updates and updates[key] is not None and updates[key] != "":
            try:
                num = float(updates[key])
            except (TypeError, ValueError):
                return {"status": "error", "error": f"{key} must be a number"}
            if num < 0:
                return {"status": "error", "error": f"{key} must be ≥ 0"}
            to_write[key] = str(num)

    if "close_rate" in updates and updates["close_rate"] is not None and updates["close_rate"] != "":
        try:
            cr = float(updates["close_rate"])
        except (TypeError, ValueError):
            return {"status": "error", "error": "close_rate must be a number"}
        if cr > 1:                 # tolerate a percentage (e.g. 25 → 0.25)
            cr = cr / 100.0
        if not 0 <= cr <= 1:
            return {"status": "error", "error": "close_rate must be between 0 and 1"}
        to_write["close_rate"] = str(cr)

    db.set_icp_config(to_write)
    return {"status": "ok", "icp": icp_config()}


# ==========================================================================
# Product catalog — example productized offers, one per value-ladder rung.
# Schema lives in db.py; validation + the 3 seed defaults live here.
# ==========================================================================

import uuid as _uuid

# Example 9-product catalog, seeded on first read of an empty catalog.
PRODUCT_DEFAULTS = [
    # Track A — Datos → IA
    {
        "name": "Data & AI Readiness Scorecard",
        "description": "Diagnóstico gratuito exprés (30 min + mini-reporte): dónde estás en datos + IA, top 3 quick wins y roadmap inicial. Tu imán de leads.",
        "value_ladder_stage": "iman",
        "fixed_price_mxn": 0.0,
        "ficha_html": "<ul><li>Assessment exprés de madurez datos/IA</li><li>Top 3 quick wins priorizadas</li><li>Mini-reporte ejecutivo</li></ul>",
        "track": "A",
    },
    {
        "name": "Data & AI Readiness Audit",
        "description": "Diagnóstico profundo (1-2 semanas): auditoría de madurez de datos + IA, quick wins priorizados por ROI y roadmap a 90 días. El primer 'sí' de bajo riesgo.",
        "value_ladder_stage": "entrada",
        "fixed_price_mxn": 18000.0,
        "ficha_html": "<ul><li>Auditoría de madurez datos/IA end-to-end</li><li>3-5 oportunidades priorizadas por ROI</li><li>Roadmap a 90 días</li><li>Golden Record assessment</li></ul>",
        "track": "A",
    },
    {
        "name": "Data Roadmap Sprint",
        "description": "Sprint de 1 semana: diseño del data product, definición de Golden Record, arquitectura objetivo y plan de ejecución por fases. El primer 'sí' pagado de bajo riesgo.",
        "value_ladder_stage": "entrada",
        "fixed_price_mxn": 18000.0,
        "ficha_html": "<ul><li>Diseño del data product</li><li>Definición de Golden Record</li><li>Arquitectura objetivo (Gold-Silver-Bronze)</li><li>Plan de ejecución por fases</li></ul>",
        "track": "A",
    },
    {
        "name": "AI-Ready Data Stack",
        "description": "Construcción del stack de datos que habilita IA: ingesta, calidad, arquitectura Gold-Silver-Bronze y un primer caso de uso en producción.",
        "value_ladder_stage": "core",
        "fixed_price_mxn": 85000.0,
        "ficha_html": "<ul><li>Arquitectura Gold-Silver-Bronze</li><li>Pipeline reproducible (ELT + tests)</li><li>Gobernanza de datos</li><li>1 caso de uso de IA desplegado</li></ul>",
        "track": "A",
    },
    {
        "name": "Fractional CDO",
        "description": "Retainer mensual: tu Chief Data Officer fraccional —Roadmap de datos en evolución, modelos/experimentos nuevos por sprint y soporte del stack.",
        "value_ladder_stage": "recurrente",
        "fixed_price_mxn": 45000.0,
        "ficha_html": "<ul><li>Roadmap de datos en evolución</li><li>Modelos/experimentos nuevos por sprint</li><li>Soporte y evolución del stack</li><li>Gobernanza continua</li></ul>",
        "track": "A",
    },
    # Track B — Producto con Agentes
    {
        "name": "AI Agent Readiness Scorecard",
        "description": "Diagnóstico gratuito exprés (30 min + mini-reporte): cómo desarrollas software hoy, brechas para adoptar AI agents y quick wins. Tu imán de leads para product engineering.",
        "value_ladder_stage": "iman",
        "fixed_price_mxn": 0.0,
        "ficha_html": "<ul><li>Assessment de madurez en desarrollo con agentes</li><li>Top 3 oportunidades de forward engineering</li><li>Mini-reporte ejecutivo</li></ul>",
        "track": "B",
    },
    {
        "name": "Agent Prototype Sprint",
        "description": "Sprint de 1 semana: diseño y prototipo de features fundamentales con filosofía AI-Native (especificación → agentes → human-in-the-loop). Entrega un prototipo funcional y el proceso reproducible.",
        "value_ladder_stage": "entrada",
        "fixed_price_mxn": 18000.0,
        "ficha_html": "<ul><li>Diseño de features con forward engineering</li><li>Prototipo funcional con agentes</li><li>Proceso reproducible (spec → agent → review)</li><li>Capacitación al equipo</li></ul>",
        "track": "B",
    },
    {
        "name": "AI-Native MVP Build",
        "description": "Construcción del MVP con agentes: arquitectura AI-Native, features fundamentales implementadas con forward engineering, human-in-the-loop y proceso reproducible.",
        "value_ladder_stage": "core",
        "fixed_price_mxn": 85000.0,
        "ficha_html": "<ul><li>Arquitectura AI-Native</li><li>Features fundamentales implementadas</li><li>Forward engineering process (spec → agent → review)</li><li>Human-in-the-loop integrado</li><li>Proceso reproducible para el equipo</li></ul>",
        "track": "B",
    },
    {
        "name": "Fractional Head of Product & Engineering",
        "description": "Retainer mensual: tu líder fraccional de producto e ingeniería —entrega continua de features con agentes, evolución del stack y gobernanza del proceso AI-Native.",
        "value_ladder_stage": "recurrente",
        "fixed_price_mxn": 45000.0,
        "ficha_html": "<ul><li>Entrega continua de features con agentes</li><li>Evolución del stack AI-Native</li><li>Gobernanza del proceso forward engineering</li><li>Soporte y mentoría al equipo</li></ul>",
        "track": "B",
    },
]


def _product_id() -> str:
    return "prod_" + _uuid.uuid4().hex[:12]


def seed_products() -> None:
    """Insert the 9 default products IFF the catalog is empty. Idempotent."""
    db.ensure_products_schema()
    existing = db.products_all()
    if existing:
        # Backfill track classification for legacy products based on name keywords.
        for p in existing:
            name = (p.get("name") or "").lower()
            track = "B" if any(k in name for k in ("agent", "prototype", "mvp", "product & engineering", "head of product")) else "A"
            if not p.get("track"):
                db.product_update(p["id"], {"track": track})
        return
    for p in PRODUCT_DEFAULTS:
        db.product_insert({
            "id": _product_id(),
            "name": p["name"],
            "description": p["description"],
            "value_ladder_stage": p["value_ladder_stage"],
            "fixed_price_mxn": p["fixed_price_mxn"],
            "ficha_html": p["ficha_html"],
            "track": p.get("track", "A"),
            "created_at": _now(),
        })


def list_products() -> dict:
    """All products (seeding the 3 defaults on first call of an empty table)."""
    seed_products()
    return {"products": db.products_all()}


def _validate_price(v) -> tuple:
    """(ok, value_or_error). Empty/None → None; else a float ≥ 0."""
    if v is None or v == "":
        return True, None
    try:
        price = float(v)
    except (TypeError, ValueError):
        return False, "fixed_price_mxn must be a number"
    if price < 0:
        return False, "fixed_price_mxn must be ≥ 0"
    return True, price


def create_product(name: str = "", description: str = "",
                   value_ladder_stage: str = "", fixed_price_mxn=None,
                   ficha_html: str = "",
                   revenue_model: str = "", recurrence_pattern: str = "") -> dict:
    """Validate + insert a product. Returns the created row."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}
    stage = (value_ladder_stage or "").strip().lower()
    if stage and stage not in VALUE_LADDER:
        return {"status": "error",
                "error": f"value_ladder_stage must be one of {list(VALUE_LADDER)}"}
    if revenue_model and revenue_model not in ("recurring", "one-off"):
        return {"status": "error", "error": "revenue_model must be 'recurring' or 'one-off'"}
    ok, price = _validate_price(fixed_price_mxn)
    if not ok:
        return {"status": "error", "error": price}

    db.ensure_products_schema()
    pid = _product_id()
    db.product_insert({
        "id": pid, "name": name, "description": (description or "").strip(),
        "value_ladder_stage": stage or None, "fixed_price_mxn": price,
        "ficha_html": ficha_html or "", "created_at": _now(),
        "revenue_model": revenue_model or None,
        "recurrence_pattern": recurrence_pattern or None,
    })
    return {"status": "ok", "product": db.product_get(pid)}


def update_product(pid: str, updates: dict) -> dict:
    """Patch whitelisted fields on a product. Unknown keys are ignored."""
    if not db.product_get(pid):
        return {"status": "error", "error": "product not found"}
    fields: dict = {}
    if "name" in updates:
        nm = (updates["name"] or "").strip()
        if not nm:
            return {"status": "error", "error": "name cannot be empty"}
        fields["name"] = nm
    if "description" in updates:
        fields["description"] = (updates["description"] or "").strip()
    if "value_ladder_stage" in updates:
        stage = (updates["value_ladder_stage"] or "").strip().lower()
        if stage and stage not in VALUE_LADDER:
            return {"status": "error",
                    "error": f"value_ladder_stage must be one of {list(VALUE_LADDER)}"}
        fields["value_ladder_stage"] = stage or None
    if "fixed_price_mxn" in updates:
        ok, price = _validate_price(updates["fixed_price_mxn"])
        if not ok:
            return {"status": "error", "error": price}
        fields["fixed_price_mxn"] = price
    if "ficha_html" in updates:
        fields["ficha_html"] = updates["ficha_html"] or ""
    if "revenue_model" in updates:
        rm = (updates["revenue_model"] or "").strip()
        if rm and rm not in ("recurring", "one-off"):
            return {"status": "error", "error": "revenue_model must be 'recurring' or 'one-off'"}
        fields["revenue_model"] = rm or None
    if "recurrence_pattern" in updates:
        fields["recurrence_pattern"] = updates["recurrence_pattern"] or None
    if "track" in updates:
        tr = (updates["track"] or "").strip().upper()
        if tr and tr not in ("A", "B"):
            return {"status": "error", "error": "track must be 'A' (Datos→IA) or 'B' (Producto con Agentes)"}
        fields["track"] = tr or None

    db.product_update(pid, fields)
    return {"status": "ok", "product": db.product_get(pid)}


def delete_product(pid: str) -> dict:
    if not db.product_delete(pid):
        return {"status": "error", "error": "product not found"}
    return {"status": "ok", "deleted": pid}


# ==========================================================================
# 90-Day Plan tracker — the playbook's 3-phase launch plan as milestones.
# Schema in db.py; phases + seed defaults here.
# ==========================================================================

PLAN_PHASES = [
    {"key": "fundaciones",  "label": "Fundaciones",      "days": "Días 1–30",  "order": 1},
    {"key": "motor",        "label": "Motor de Demanda", "days": "Días 31–60", "order": 2},
    {"key": "optimizacion", "label": "Optimización",     "days": "Días 61–90", "order": 3},
]
_PLAN_PHASE_KEYS = {p["key"] for p in PLAN_PHASES}

# (phase, title, description) in plan order — sort_order is the list index.
PLAN_MILESTONE_DEFAULTS = [
    # Phase 1 — Fundaciones (días 1–30)
    ("fundaciones", "Write positioning statement",
     "Una frase clara: a quién sirves, qué problema resuelves, por qué tú."),
    ("fundaciones", "Productize 3 offers with fixed pricing",
     "Imán/Entrada/Core/Recurrente con precio fijo — cero cotización a medida."),
    ("fundaciones", "Set up CRM with lead scoring fields",
     "Deals + campos de scoring (fuente, engagement, industria ICP)."),
    ("fundaciones", "Write pipeline math backward from goal",
     "De la meta de ingresos → clientes → propuestas → discovery → leads → toques."),
    # Phase 2 — Motor de Demanda (días 31–60)
    ("motor", "Publish weekly insight-led content 4 weeks",
     "Una pieza de valor por semana durante 4 semanas — construye autoridad."),
    ("motor", "Design and rehearse signature talk",
     "Una charla firma diseñada y ensayada para eventos."),
    ("motor", "Launch nurture sequence of 5 touches",
     "Secuencia de nurture de 5 toques para leads capturados."),
    ("motor", "Install time blocks",
     "Bloques de tiempo protegidos para contenido, ventas y entrega."),
    # Phase 3 — Optimización (días 61–90)
    ("optimizacion", "Fill dashboard with real data",
     "El dashboard operando con datos reales, no supuestos."),
    ("optimizacion", "Calculate CLTV:CAC and ROMI",
     "Unit economics: CLTV:CAC y retorno sobre inversión de marketing."),
    ("optimizacion", "Train lead scoring baseline at 100 leads",
     "Con ~100 leads, calibra el baseline de scoring."),
    ("optimizacion", "Execute attraction loop at event",
     "Ejecuta el loop de atracción completo en un evento real."),
    ("optimizacion", "Review talk-listen ratio",
     "Revisa el talk-listen ratio hacia la meta ≤45% en discovery."),
]


def _plan_id() -> str:
    return "mile_" + _uuid.uuid4().hex[:12]


def seed_plan_milestones() -> None:
    """Insert the playbook milestones IFF the table is empty. Idempotent."""
    db.ensure_plan_schema()
    if db.plan_milestones_all():
        return
    for i, (phase, title, desc) in enumerate(PLAN_MILESTONE_DEFAULTS):
        db.plan_milestone_insert({
            "id": _plan_id(), "phase": phase, "title": title, "description": desc,
            "sort_order": i, "completed": 0, "completed_at": None,
        })


def _plan_progress(items: list) -> dict:
    total = len(items)
    done = sum(1 for m in items if m["completed"])
    return {"total": total, "done": done,
            "pct": round(done / total * 100) if total else 0}


def list_plan_milestones() -> dict:
    """All milestones grouped by phase (seeding defaults on first call), plus
    per-phase and overall progress."""
    seed_plan_milestones()
    ms = db.plan_milestones_all()
    phases = []
    for meta in PLAN_PHASES:
        items = [m for m in ms if m["phase"] == meta["key"]]
        phases.append({**meta, "milestones": items, **_plan_progress(items)})
    return {"milestones": ms, "phases": phases, "overall": _plan_progress(ms)}


def set_milestone_completed(mid: str, completed: Optional[bool] = None) -> dict:
    """Set (or toggle when completed is None) a milestone's completed flag.
    Stamps completed_at on completion, clears it on un-completion."""
    m = db.plan_milestone_get(mid)
    if not m:
        return {"status": "error", "error": "milestone not found"}
    new = (0 if m["completed"] else 1) if completed is None else (1 if completed else 0)
    db.plan_milestone_set_completed(mid, new, _now() if new else None)
    return {"status": "ok", "milestone": db.plan_milestone_get(mid)}


# ==========================================================================
# Pipeline health — touch-cadence alert triage over active deals.
# ==========================================================================

STALE_TOUCH_DAYS = 7   # no touch in this many days on an active deal → cold

def _parse_iso_date(d):
    """ISO 'YYYY-MM-DD' → date, or None if unset/malformed."""
    if not d:
        return None
    try:
        return datetime.date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return None


def pipeline_health() -> dict:
    """Touch-cadence triage over ACTIVE deals (stage not won/lost/stalled). Each
    active deal is assigned to at most ONE alert level, by priority red > yellow >
    blue, so the counts never double-count a deal. Stalled (icebox) deals are
    intentionally parked, so they are excluded — no touch-cadence nag:

      red    — next_touch_date is in the past      → touch today
      yellow — no touch in STALE_TOUCH_DAYS+ days   → gone cold on cadence
      blue   — no next_touch_date set               → schedule the next touch
    """
    today = datetime.date.today()
    deals = [d for d in crm.list_deals()
             if d.get("stage") not in crm._CLOSED and d.get("stage") not in crm._INACTIVE]
    red, yellow, blue = [], [], []

    for d in deals:
        nxt = _parse_iso_date(d.get("next_touch_date"))
        last = _parse_iso_date(d.get("last_touch_date"))
        base = {
            "id": d["id"], "title": d.get("title"),
            "account_name": d.get("account_name"), "stage": d.get("stage"),
            "next_touch_date": d.get("next_touch_date"),
            "last_touch_date": d.get("last_touch_date"),
            "touch_count": d.get("touch_count") or 0,
        }
        if nxt is not None and nxt < today:
            red.append({**base, "days_overdue": (today - nxt).days})
        elif last is not None and (today - last).days >= STALE_TOUCH_DAYS:
            yellow.append({**base, "days_since_touch": (today - last).days})
        elif not d.get("next_touch_date"):
            blue.append(base)

    def _plural(n: int) -> str:
        return "s" if n != 1 else ""

    def _level(items, label):
        return {"count": len(items), "label": label, "deals": items}

    return {
        "today": today.isoformat(),
        "active_total": len(deals),
        "levels": {
            "red": _level(red, f"{len(red)} deal{_plural(len(red))} need a touch today"),
            "yellow": _level(
                yellow,
                f"{len(yellow)} deal{_plural(len(yellow))} gone cold "
                f"({STALE_TOUCH_DAYS}+ days no touch)"),
            "blue": _level(
                blue, f"{len(blue)} deal{_plural(len(blue))} need a next touch scheduled"),
        },
        "ok": not (red or yellow or blue),
        # Delivery drift is a SEPARATE verdict from touch cadence: a project can
        # ship while its deal is still open, and that has nothing to do with
        # whether the deal was touched this week. Surfaced here (read-only) so the
        # dashboard's pipeline-health card and `get_pipeline_health` MCP carry it
        # where a human already looks — but the top-level `ok` above stays the
        # touch-cadence verdict and is NOT influenced by drift (C5).
        "delivery_drift": crm.delivery_drift(),
    }


# ---------------------------------------------------------------------------
# LEGACY STAGE VOCABULARY — read `delivered`, never write it (ruling 2).
#
# `deals.stage = 'delivered'` was retired in journey fase 1 (see `crm.py`'s
# module docstring and `migrations/m05_retire_delivered_stage.py`): a won deal
# now stays `won` forever and delivery is `projects.status = 'delivered'`. Every
# `IN ('won','delivered')` in this module — here, and at the value-ladder,
# channel-mix, and score-accuracy reads below — is therefore a READER of a
# vocabulary that can no longer be produced: correct, and vacuous at 0 rows.
#
# They are deliberately NOT mass-edited. Dropping the value from a read would
# make these queries lie about any legacy row (a restored backup, an archive
# export) for no gain, and every one of them treats `delivered` exactly as it
# treats `won` — which is now the only truth there is. A later phase removes
# them once 0 readers is confirmed; until then the rule is one-directional:
# these lines may KEEP recognising the value, and nothing here may write it.
# ---------------------------------------------------------------------------

# Win-probability per stage (mirrors the frontend STAGE_PROB) — used to weight
# pipeline value. Closed stages included for completeness (won=1, lost=0).
# `delivered` is legacy-read vocabulary (see above), scored identically to won.
STAGE_PROB = {"lead": 0.1, "engaged": 0.25, "qualified": 0.4, "demo": 0.6,
              "proposal": 0.75, "won": 1.0, "delivered": 1.0, "lost": 0.0}

# Auto-estimate of days-until-close from stage when a deal has no explicit
# expected_close_date. A transparent heuristic (later stage → sooner close),
# not a model. Only active stages are estimated.
_STAGE_FORECAST_DAYS = {"proposal": 30, "demo": 45, "qualified": 60,
                        "engaged": 75, "lead": 90}


def forecast() -> dict:
    """30/60/90-day revenue forecast over ACTIVE deals (stage not won/lost/stalled).

    Each active deal's close date is its explicit `expected_close_date` if set,
    else auto-estimated from stage via `_STAGE_FORECAST_DAYS` (a simple
    heuristic, not a model). Deals are bucketed by days-until-close vs today:

      overdue — close date in the past (should have closed already)
      30d     — closes within 0–30 days
      60d     — closes within 31–60 days
      90d     — closes within 61–90 days
      beyond  — closes beyond 90 days (or undetermined)

    Per bucket: deal count, total value, weighted value (value × STAGE_PROB[stage]).
    """
    today = datetime.date.today()
    deals = [d for d in crm.list_deals()
             if d.get("stage") not in crm._CLOSED and d.get("stage") not in crm._INACTIVE]

    keys = ("overdue", "30d", "60d", "90d", "beyond")
    buckets = {k: {"count": 0, "value": 0.0, "weighted": 0.0, "deals": []} for k in keys}
    total_value = 0.0
    total_weighted = 0.0
    with_expected = 0
    auto_estimated = 0

    for d in deals:
        stage = d.get("stage")
        value = d.get("value") or 0
        weighted = value * STAGE_PROB.get(stage, 0.0)

        explicit = _parse_iso_date(d.get("expected_close_date"))
        if explicit is not None:
            eff = explicit
            with_expected += 1
        else:
            est_days = _STAGE_FORECAST_DAYS.get(stage)
            eff = today + datetime.timedelta(days=est_days) if est_days is not None else None
            auto_estimated += 1

        if eff is None:
            key = "beyond"
            days_until = None
        else:
            days_until = (eff - today).days
            if days_until < 0:
                key = "overdue"
            elif days_until <= 30:
                key = "30d"
            elif days_until <= 60:
                key = "60d"
            elif days_until <= 90:
                key = "90d"
            else:
                key = "beyond"

        b = buckets[key]
        b["count"] += 1
        b["value"] += value
        b["weighted"] += weighted
        b["deals"].append({
            "id": d["id"], "title": d.get("title"),
            "account_name": d.get("account_name"), "stage": stage,
            "value": value,
            "expected_close_date": eff.isoformat() if eff is not None else None,
            "days_until_close": days_until,
        })
        total_value += value
        total_weighted += weighted

    for b in buckets.values():
        b["weighted"] = round(b["weighted"])

    return {
        "today": today.isoformat(),
        "buckets": buckets,
        "totals": {
            "all_active": len(deals),
            "total_value": total_value,
            "total_weighted": round(total_weighted),
            "with_expected_date": with_expected,
            "auto_estimated": auto_estimated,
        },
    }


# ==========================================================================
# CLTV:CAC — unit economics per lead source (Cap. 8).
#   CLTV = avg_ticket * repeat_purchases * avg_lifespan_months  (one value)
#   CAC  = total_acquisition_cost(source) / customers_acquired(source)
#   ratio = CLTV / CAC  → rating green >3 · yellow 1–3 · red <1
# ==========================================================================

def _acq_id() -> str:
    return "acq_" + _uuid.uuid4().hex[:12]


def _valid_month(m: str) -> bool:
    try:
        datetime.datetime.strptime(m, "%Y-%m")
        return True
    except (ValueError, TypeError):
        return False


def list_acquisition_costs() -> dict:
    db.ensure_acquisition_schema()
    return {"costs": db.acquisition_costs_all()}


def add_acquisition_cost(source: str = "", cost_mxn=None, month: str = "",
                         notes: str = "") -> dict:
    """Validate + record a monthly acquisition cost for a lead source."""
    source = (source or "").strip().lower()
    if source not in LEAD_SOURCE:
        return {"status": "error", "error": f"source must be one of {list(LEAD_SOURCE)}"}
    try:
        cost = float(cost_mxn)
    except (TypeError, ValueError):
        return {"status": "error", "error": "cost_mxn must be a number"}
    if cost < 0:
        return {"status": "error", "error": "cost_mxn must be ≥ 0"}
    month = (month or "").strip()
    if month and not _valid_month(month):
        return {"status": "error", "error": "month must be YYYY-MM"}

    db.ensure_acquisition_schema()
    cid = _acq_id()
    db.acquisition_cost_insert({
        "id": cid, "source": source, "cost_mxn": cost,
        "month": month or None, "notes": (notes or "").strip() or None,
        "created_at": _now(),
    })
    return {"status": "ok", "cost": db.acquisition_cost_get(cid)}


def delete_acquisition_cost(cid: str) -> dict:
    if not db.acquisition_cost_delete(cid):
        return {"status": "error", "error": "cost not found"}
    return {"status": "ok", "deleted": cid}


def _cltv_cac_rating(ratio: Optional[float]) -> str:
    if ratio is None:
        return "na"
    if ratio > 3:
        return "green"
    if ratio >= 1:
        return "yellow"
    return "red"


def _customers_by_source() -> dict:
    """Won outcomes grouped by their lead_source → {source: count}."""
    out: dict = {}
    for d in crm.list_deals():
        if d.get("stage") not in crm._WON_OUTCOMES:
            continue
        s = (d.get("lead_source") or "").strip().lower()
        if s:
            out[s] = out.get(s, 0) + 1
    return out


def cltv_cac() -> dict:
    """CLTV (single value) + CAC and CLTV:CAC ratio per lead source."""
    cfg = growth_config()
    avg_ticket = cfg["avg_ticket"]
    repeat = cfg["repeat_purchases"]
    lifespan = cfg["avg_lifespan_months"]
    cltv = round(avg_ticket * repeat * lifespan, 2)

    costs = db.acquisition_cost_totals_by_source()
    customers = _customers_by_source()

    def _row(source: str) -> dict:
        cost = round(costs.get(source, 0.0), 2)
        n = customers.get(source, 0)
        cac = round(cost / n, 2) if n else None
        ratio = round(cltv / cac, 2) if cac else None
        return {"source": source, "cost_mxn": cost, "customers": n,
                "cac": cac, "ratio": ratio, "rating": _cltv_cac_rating(ratio)}

    # every source that has either spend or a won customer
    rows = [_row(s) for s in sorted(set(costs) | set(customers))]

    total_cost = round(sum(costs.values()), 2)
    total_cust = sum(customers.values())
    total_cac = round(total_cost / total_cust, 2) if total_cust else None
    total_ratio = round(cltv / total_cac, 2) if total_cac else None

    return {
        "cltv": cltv,
        "params": {"avg_ticket": avg_ticket, "repeat_purchases": repeat,
                   "avg_lifespan_months": lifespan, "currency": cfg["currency"]},
        "sources": rows,
        "totals": {"cost_mxn": total_cost, "customers": total_cust,
                   "cac": total_cac, "ratio": total_ratio,
                   "rating": _cltv_cac_rating(total_ratio)},
    }


# ==========================================================================
# Nurture sequences — a 5-touch Hook-model cadence per deal.
# Hook loop (Nir Eyal): Trigger → Action → Variable Reward → Investment,
# then a re-trigger to close the loop.
# ==========================================================================

NURTURE_STATUS = ("pending", "sent", "skipped")

# Per-source opener for the first (trigger) touch — grounds the outreach in how
# the lead actually entered.
_SOURCE_OPENER = {
    "referral":   "Me pasaron tu contacto y",
    "linkedin":   "Vi tu perfil en LinkedIn y",
    "evento":     "Nos cruzamos en el evento y",
    "cold_email": "Te escribo en frío porque",
    "inbound":    "Gracias por acercarte —",
}

# (step_number, touch_type, offset_days, template). Templates interpolate
# {opener}/{name}/{stage}. Cadence: 0 → 2 → 5 → 9 → 14 days.
_HOOK_STEPS = [
    (1, "trigger", 0,
     "{opener} creo que puedo ayudar a {name} con datos/IA. "
     "¿Te late que te comparta un ángulo concreto?"),
    (2, "action", 2,
     "Aquí un recurso rápido y accionable para {name} (2 min). "
     "¿Lo checas y me dices qué resuena?"),
    (3, "variable_reward", 5,
     "Encontré un caso parecido al de {name} donde movimos la aguja — "
     "te comparto el insight sin costo."),
    (4, "investment", 9,
     "¿15 min esta semana para aterrizarlo a {name}? Con una respuesta tuya "
     "lo personalizo (etapa actual: {stage})."),
    (5, "re_trigger", 14,
     "Cierro el loop: si el timing no es ahora para {name}, ¿cuándo tendría "
     "sentido retomar? Te reactivo entonces."),
]


def _nurture_id() -> str:
    return "nur_" + _uuid.uuid4().hex[:12]


def _render_hook_template(template: str, name: str, source: str, stage: str) -> str:
    opener = _SOURCE_OPENER.get(source, "Hola,")
    return template.format(opener=opener, name=name, source=source or "—", stage=stage)


def get_nurture(deal_id: str) -> dict:
    """A deal's nurture sequence + the next suggested touch date (earliest
    still-pending step). Empty steps when none has been generated yet."""
    db.ensure_nurture_schema()
    steps = db.nurture_for_deal(deal_id)
    pending_dates = [s["scheduled_date"] for s in steps
                     if s["status"] == "pending" and s["scheduled_date"]]
    counts = {st: sum(1 for s in steps if s["status"] == st) for st in NURTURE_STATUS}
    return {
        "deal_id": deal_id,
        "steps": steps,
        "total": len(steps),
        "completed": counts["sent"] + counts["skipped"],
        "counts": counts,
        "next_suggested_date": min(pending_dates) if pending_dates else None,
    }


def generate_nurture(deal_id: str) -> dict:
    """(Re)generate a 5-touch Hook sequence from the deal's name/source/stage.
    Regenerating replaces any existing sequence for the deal."""
    deal = crm.get_deal(deal_id)
    if not deal:
        return {"status": "error", "error": "deal not found"}

    db.ensure_nurture_schema()
    db.nurture_delete_for_deal(deal_id)   # clean regenerate

    name = (deal.get("account_name") or deal.get("contact_name")
            or deal.get("title") or "el prospecto")
    source = (deal.get("lead_source") or deal.get("source") or "").strip().lower()
    stage = deal.get("stage") or "lead"
    today = datetime.date.today()

    for step_number, touch_type, offset_days, template in _HOOK_STEPS:
        db.nurture_insert({
            "id": _nurture_id(), "deal_id": deal_id, "step_number": step_number,
            "touch_type": touch_type,
            "template_text": _render_hook_template(template, name, source, stage),
            "scheduled_date": (today + datetime.timedelta(days=offset_days)).isoformat(),
            "status": "pending", "created_at": _now(),
        })

    # Seed next_touch_date from the nurture ledger so the deal doesn't
    # start with a null next-touch (which would flag it as blue/cold in
    # pipeline health until the first manual touch).  Uses cadence.recompute
    # so the date is derived from the same ledger as set_nurture_status.
    from . import cadence
    conn = db.get_conn()
    try:
        cadence.recompute(conn, deal_id)
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok", "sequence": get_nurture(deal_id)}


def set_nurture_status(nid: str, status: str) -> dict:
    """Update one step's status (pending/sent/skipped).

    When a step is marked 'sent', also record a touch on the deal so that
    touch_count, last_touch_date, and next_touch_date stay in sync with
    the nurture cadence. This closes the two-clock problem where nurture
    steps and deal touches diverged.
    """
    status = (status or "").strip().lower()
    if status not in NURTURE_STATUS:
        return {"status": "error", "error": f"status must be one of {list(NURTURE_STATUS)}"}
    step = db.nurture_get(nid)
    if not step:
        return {"status": "error", "error": "step not found"}
    db.nurture_set_status(nid, status)

    # When a nurture step is sent, record a touch on the deal.
    # This keeps deals.touch_count / last_touch_date / next_touch_date
    # in sync with the nurture cadence (fixes the two-clock problem).
    touch_result = None
    if status == "sent":
        deal_id = step.get("deal_id", "")
        step_label = step.get("touch_type") or f"step {step.get('step_number', '?')}"
        if deal_id:
            try:
                touch_result = record_touch(
                    deal_id, note=f"Nurture: {step_label}", kind="touch",
                    next_in_days=7)
            except Exception:
                touch_result = {"status": "error", "error": "touch sync failed"}

    result = {"status": "ok", "step": db.nurture_get(nid)}
    if touch_result:
        result["touch_synced"] = touch_result
    return result


def _now() -> int:
    return int(time.time())


def _today() -> str:
    return datetime.date.today().isoformat()


def iso_week(d: Optional[datetime.date] = None) -> str:
    d = d or datetime.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _week_bounds(week: Optional[str]) -> tuple[int, int, str]:
    """(start_epoch, end_epoch, canonical_week) for an ISO-week string. The
    window is [Monday 00:00, next Monday 00:00). Defaults to the current week."""
    if week:
        try:
            y, w = week.split("-W")
            monday = datetime.date.fromisocalendar(int(y), int(w), 1)
        except (ValueError, TypeError):
            monday = datetime.date.today() - datetime.timedelta(
                days=datetime.date.today().weekday())
    else:
        today = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday())
    start = int(datetime.datetime.combine(monday, datetime.time()).timestamp())
    end = int(datetime.datetime.combine(
        monday + datetime.timedelta(days=7), datetime.time()).timestamp())
    return start, end, iso_week(monday)


# ==========================================================================
# Schema — additive to deals + two small growth tables (idempotent)
# ==========================================================================

def ensure_schema() -> None:
    """Install the growth layer. Additive ALTERs on deals + the
    lead_scoring_features and content_log tables. Safe to run every boot."""
    crm.ensure_schema()   # deals/accounts/contacts must exist first
    db.ensure_icp_schema()  # ICP-editor key→value store (Strategy tab)
    db.ensure_products_schema()  # product catalog (Strategy tab)
    db.ensure_plan_schema()      # 90-day plan tracker (Strategy tab)
    db.ensure_acquisition_schema()  # acquisition costs (CLTV:CAC)
    db.ensure_nurture_schema()      # per-deal nurture sequences (Hook cadence)
    db.ensure_content_pieces_schema()  # content pipeline calendar (supersedes content_log)
    db.ensure_speaking_schema()        # speaking pipeline (talks → attraction loops)
    db.ensure_conversion_schema()      # weekly conversion-funnel snapshots
    db.ensure_time_blocks_schema()     # weekly role-block calendar (Today tab)
    conn = db.get_conn()
    try:
        dcols = [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        add = {
            "value_ladder_stage": "TEXT",
            "growth_loop": "TEXT",
            "lead_source": "TEXT",
            "lead_score": "INTEGER",
            "touch_count": "INTEGER NOT NULL DEFAULT 0",
            "last_touch_date": "TEXT",
            "next_touch_date": "TEXT",
            "expected_close_date": "TEXT",
        }
        for col, decl in add.items():
            if col not in dcols:
                conn.execute(f"ALTER TABLE deals ADD COLUMN {col} {decl}")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lead_scoring_features (
                lead_id          TEXT PRIMARY KEY,   -- deal id (one row per lead)
                account_type     TEXT,
                source           TEXT,
                product_interest TEXT,
                engagement_score INTEGER DEFAULT 0,
                industry         TEXT,
                created_at       INTEGER
            );
            CREATE TABLE IF NOT EXISTS content_log (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                url          TEXT,
                channel      TEXT,
                published_at TEXT NOT NULL,          -- ISO date (YYYY-MM-DD)
                loop         TEXT,                   -- which growth loop it feeds
                notes        TEXT,
                created_at   INTEGER
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ==========================================================================
# Lead scoring (Cap. 4) — simple, transparent, deterministic rules
# ==========================================================================

def _profile_fit(profile: Optional[str]) -> int:
    p = (profile or "").strip().upper()
    return _FIRM_WEIGHTS["profile_fit"].get(p, 0)


def _industry_fit(industry: str) -> int:
    ind = (industry or "").strip().lower()
    if not ind:
        return 3
    if ind in icp_industries():
        return _FIRM_WEIGHTS["industry_match"]
    # partial: any ICP industry word inside the provided industry
    for icp in icp_industries():
        if icp in ind or ind in icp:
            return 6
    return 3


def _source_quality(source: str) -> int:
    s = (source or "").strip().lower()
    return _FIRM_WEIGHTS["source_quality"].get(s, 4)


def _firmographic_score(source: str, industry: str, profile: Optional[str]) -> dict:
    src = _source_quality(source)
    ind = _industry_fit(industry)
    prof = _profile_fit(profile)
    return {
        "value": min(_CATEGORY_MAX["firmographic"], src + ind + prof),
        "max": _CATEGORY_MAX["firmographic"],
        "sub": {"source": src, "industry": ind, "profile": prof},
    }


def _days_since(d: Optional[str]) -> Optional[int]:
    if not d:
        return None
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(str(d)[:10])).days
    except (ValueError, TypeError):
        return None


def _behavioral_score(deal: dict, events: Optional[list]) -> dict:
    touches = (deal.get("touch_count") or 0)
    # TODO: once CRM stores explicit prospect replies, switch fully to response_rate.
    # For now, if response events exist use them; otherwise keep legacy per-touch model.
    _RESPONSE_KINDS = {"response", "reply", "email_reply", "email_open", "clicked",
                       "meeting_accepted", "whatsapp_reply"}
    response_count = sum(
        1 for e in (events or [])
        if (e.get("kind") or "").lower() in _RESPONSE_KINDS
    )
    if touches > 0 and response_count > 0:
        response_rate = response_count / touches
        if response_rate >= 0.50:
            touch_pts = 10
        elif response_rate >= 0.25:
            touch_pts = 7
        elif response_rate >= 0.10:
            touch_pts = 4
        else:
            touch_pts = 1
    else:
        per = _BEHAVIOR_WEIGHTS["touch_count"]["per_touch"]
        cap = _BEHAVIOR_WEIGHTS["touch_count"]["cap"]
        touch_pts = min(touches * per, cap)

    days = _days_since(deal.get("last_touch_date"))
    recency = _BEHavior_RECENCY(days, touches)

    # meeting discovery in last 14 days
    meeting_pts = _BEHAVIOR_WEIGHTS["meeting"]["none"]
    cutoff = time.time() - 14 * 86400
    for e in (events or []):
        ts = e.get("created_at") or 0
        kind = (e.get("kind") or "").lower()
        if ts >= cutoff:
            if kind in ("discovery_call", "discovery", "demo"):
                meeting_pts = _BEHAVIOR_WEIGHTS["meeting"]["discovery_14d"]
                break
            if kind in ("call", "meeting"):
                meeting_pts = max(meeting_pts, _BEHAVIOR_WEIGHTS["meeting"]["meeting_14d"])

    return {
        "value": min(_CATEGORY_MAX["behavioral"], touch_pts + recency + meeting_pts),
        "max": _CATEGORY_MAX["behavioral"],
        "sub": {"touches": touch_pts, "recency": recency, "meeting": meeting_pts},
    }


def _BEHavior_RECENCY(days: Optional[int], touch_count: int = 0) -> int:
    # Recency only matters if the prospect has been touched at least once
    if days is None or touch_count == 0:
        return 0
    if days <= 7:
        return _BEHAVIOR_WEIGHTS["recency"]["7d"]
    if days <= 14:
        return _BEHAVIOR_WEIGHTS["recency"]["14d"]
    if days <= 30:
        return _BEHAVIOR_WEIGHTS["recency"]["30d"]
    return _BEHAVIOR_WEIGHTS["recency"]["older"]


def _fireflies_score(signals: Optional[dict]) -> dict:
    if not signals:
        return {"value": 0, "max": _CATEGORY_MAX["fireflies"],
                "sub": {"talk_ratio": 0, "questions": 0, "filler": 0,
                        "actions": 0, "sentiment": 0}}
    ratio = signals.get("talk_ratio") or 0
    # signals.talk_ratio is the prospect's share; the operator's share is 1 - that.
    # Lower operator ratio = better discovery. Playbook target: ≤45%.
    ricardo_ratio = 1 - ratio
    if ricardo_ratio <= 0.45:
        talk = _FIREFLIES_WEIGHTS["talk_ratio"]["45"]
    elif ricardo_ratio <= 0.55:
        talk = _FIREFLIES_WEIGHTS["talk_ratio"]["55"]
    elif ricardo_ratio <= 0.64:
        talk = _FIREFLIES_WEIGHTS["talk_ratio"]["64"]
    else:
        talk = _FIREFLIES_WEIGHTS["talk_ratio"]["over"]

    q = signals.get("questions") or 0
    if q >= 6:
        questions = _FIREFLIES_WEIGHTS["questions"]["6"]
    elif q >= 4:
        questions = _FIREFLIES_WEIGHTS["questions"]["4"]
    elif q >= 2:
        questions = _FIREFLIES_WEIGHTS["questions"]["2"]
    else:
        questions = _FIREFLIES_WEIGHTS["questions"]["0"]

    density = signals.get("filler_density") or 0
    if density < 0.03:
        filler = _FIREFLIES_WEIGHTS["filler"]["3"]
    elif density < 0.05:
        filler = _FIREFLIES_WEIGHTS["filler"]["5"]
    else:
        filler = _FIREFLIES_WEIGHTS["filler"]["bad"]

    actions = signals.get("action_items") or 0
    if actions >= 3:
        action_pts = _FIREFLIES_WEIGHTS["action_items"]["3"]
    elif actions >= 1:
        action_pts = _FIREFLIES_WEIGHTS["action_items"]["1"]
    else:
        action_pts = _FIREFLIES_WEIGHTS["action_items"]["0"]

    sentiment = signals.get("sentiment") or "neutral"
    sent_pts = _FIREFLIES_WEIGHTS["sentiment"].get(sentiment, 2)

    return {
        "value": min(_CATEGORY_MAX["fireflies"], talk + questions + filler + action_pts + sent_pts),
        "max": _CATEGORY_MAX["fireflies"],
        "sub": {"talk_ratio": talk, "questions": questions, "filler": filler,
                "actions": action_pts, "sentiment": sent_pts},
    }


def _product_fit_score(deal: dict, product: Optional[dict]) -> dict:
    # track match
    track = deal.get("product_track") or (product.get("track") if product else None) or ""
    track_pts = _PRODUCT_WEIGHTS["track_match"] if track else 0

    # explicit product interest
    product_id = deal.get("product_id")
    product_name = deal.get("product_name") or (product.get("name") if product else "")
    interest_pts = _PRODUCT_WEIGHTS["interest"] if (product_id or product_name) else 0

    # ladder stage: all stages have equal potential (5 pts) — earlier funnel is not worse.
    stage = (deal.get("value_ladder_stage") or "").lower()
    has_product_fit = bool(stage or product_id or track)
    ladder_pts = _PRODUCT_WEIGHTS["ladder_fit"] if has_product_fit else 0

    # expected deal value: use deal.value if set, else profile-based estimate
    value = deal.get("value")
    profile = (deal.get("client_profile") or "").strip().upper()
    try:
        numeric_value = float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        numeric_value = 0.0
    if not numeric_value and profile in ("A", "B", "C"):
        numeric_value = {"A": 18000.0, "B": 45000.0, "C": 45000.0}[profile]
    if numeric_value >= 80000:
        value_pts = _PRODUCT_WEIGHTS["expected_value"]["80k"]
    elif numeric_value >= 40000:
        value_pts = _PRODUCT_WEIGHTS["expected_value"]["40k"]
    elif numeric_value >= 20000:
        value_pts = _PRODUCT_WEIGHTS["expected_value"]["20k"]
    elif numeric_value >= 10000:
        value_pts = _PRODUCT_WEIGHTS["expected_value"]["10k"]
    elif numeric_value > 0:
        value_pts = _PRODUCT_WEIGHTS["expected_value"]["low"]
    else:
        value_pts = _PRODUCT_WEIGHTS["expected_value"]["none"]

    return {
        "value": min(_CATEGORY_MAX["product_fit"], track_pts + interest_pts + ladder_pts + value_pts),
        "max": _CATEGORY_MAX["product_fit"],
        "sub": {"track": track_pts, "interest": interest_pts, "ladder": ladder_pts, "expected_value": value_pts},
    }


def _profile_note(profile: Optional[str]) -> str:
    p = (profile or "").strip().upper()
    rec = {
        "A": "Track B (build con agentes) — alta probabilidad de core build",
        "B": "Tracks A+B — evaluar fractional + build según urgencia",
        "C": "Track A (Datos→IA) + fractional — alta probabilidad de retainer",
    }.get(p, "Sin perfil — asignar A/B/C para mejorar la recomendación")
    label = next((x["label"] for x in CLIENT_PROFILES if x["key"] == p), "Perfil no definido")
    return f"{label}. {rec}"


def score_features(account_type: str = "", source: str = "",
                   engagement_score: int = 0, industry: str = "",
                   client_profile: Optional[str] = None,
                   deal: Optional[dict] = None,
                   signals: Optional[dict] = None,
                   product: Optional[dict] = None) -> dict:
    """0–100 weighted-sum score across 4 categories with full JSON breakdown.
    Pure function so the contract can pin exact numbers."""
    d = deal or {}
    firm = _firmographic_score(source or d.get("lead_source") or d.get("source") or "",
                                industry or d.get("industry") or "",
                                client_profile or d.get("client_profile"))
    beh = _behavioral_score(d, d.get("events") if deal else [])
    ff = _fireflies_score(signals)
    prod = _product_fit_score(d, product)
    total = min(100, firm["value"] + beh["value"] + ff["value"] + prod["value"])
    profile = client_profile or d.get("client_profile")
    return {
        "score": total,
        "categories": {
            "firmographic": firm,
            "behavioral": beh,
            "fireflies": ff,
            "product_fit": prod,
        },
        "profile": profile,
        "profile_note": _profile_note(profile),
    }


def set_lead_features(deal_id: str, account_type: str = "", source: str = "",
                      product_interest: str = "", engagement_score: int = 0,
                      industry: str = "", client_profile: Optional[str] = None,
                      product_id: Optional[str] = None,
                      fireflies_signals: Optional[dict] = None) -> dict:
    """Upsert a lead's scoring features and (re)compute deals.lead_score + details."""
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
            return {"status": "error", "error": "deal not found"}
        # enrich deal dict
        from . import crm
        deal = crm.get_deal(deal_id) or {}
        if product_id:
            deal["product_id"] = product_id
        product = None
        if deal.get("product_id"):
            product = db.product_get(deal_id[:0] + deal["product_id"])  # placeholder; fixed below
        if product is None and deal.get("product_id"):
            product = db.product_get(deal["product_id"])
        scored = score_features(account_type, source, engagement_score, industry,
                                client_profile=client_profile, deal=deal,
                                signals=fireflies_signals, product=product)
        conn.execute(
            "INSERT INTO lead_scoring_features "
            "(lead_id, account_type, source, product_interest, engagement_score, "
            " industry, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(lead_id) DO UPDATE SET "
            "account_type=excluded.account_type, source=excluded.source, "
            "product_interest=excluded.product_interest, "
            "engagement_score=excluded.engagement_score, industry=excluded.industry",
            (deal_id, account_type or None, source or None, product_interest or None,
             int(engagement_score or 0), industry or None, _now()))
        conn.execute(
            "UPDATE deals SET lead_score = ?, lead_score_details = ?, "
            "client_profile = ?, updated_at = ? WHERE id = ?",
            (scored["score"], json.dumps(scored, default=str), client_profile or deal.get("client_profile"),
             _now(), deal_id))
        crm._log(conn, deal_id, "lead_scored", scored)
        conn.commit()
        return {"status": "ok", "deal_id": deal_id, **scored}
    finally:
        conn.close()


def score_deal(deal_id: str) -> dict:
    """Load deal + latest Fireflies signals + product + events and recompute score."""
    from . import crm, fireflies
    db.ensure_fireflies_schema()
    deal = crm.get_deal(deal_id)
    if not deal:
        return {"status": "error", "error": "deal not found"}
    product = None
    if deal.get("product_id"):
        product = db.product_get(deal["product_id"])
    signals = fireflies.latest_signals_for_deal(deal_id)
    # read persisted features
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM lead_scoring_features WHERE lead_id = ?", (deal_id,)).fetchone()
        features = dict(row) if row else {}
    finally:
        conn.close()
    return set_lead_features(
        deal_id,
        account_type=features.get("account_type") or "",
        source=features.get("source") or deal.get("lead_source") or deal.get("source") or "",
        product_interest=features.get("product_interest") or "",
        engagement_score=int(features.get("engagement_score") or 0),
        industry=features.get("industry") or "",
        client_profile=deal.get("client_profile"),
        product_id=deal.get("product_id"),
        fireflies_signals=signals,
    )


def score_all_leads() -> dict:
    """Recompute scores for all non-closed deals AND won deals (won deals need
    scores for scoring-accuracy bands). Returns counts."""
    from . import crm
    deals = [d for d in crm.list_deals()
             if d.get("stage") not in crm._CLOSED and d.get("stage") not in crm._INACTIVE]
    # Also include won deals so scoring accuracy bands reflect reality.
    won_deals = [d for d in crm.list_deals()
                 if d.get("stage") in crm._WON_OUTCOMES and (d.get("lead_score") or 0) == 0]
    deals.extend(won_deals)
    ok = errors = 0
    for d in deals:
        res = score_deal(d["id"])
        if res.get("status") == "ok":
            ok += 1
        else:
            errors += 1
    return {"status": "ok", "scored": ok, "errors": errors, "total": len(deals)}


# ==========================================================================
# Deal growth fields — one validated write path (mirrors crm.update_deal)
# ==========================================================================

def update_deal_growth(deal_id: str, value_ladder_stage: Optional[str] = None,
                       growth_loop: Optional[str] = None,
                       lead_source: Optional[str] = None,
                       next_touch_date: Optional[str] = None,
                       expected_close_date: Optional[str] = None,
                       product_id: Optional[str] = None) -> dict:
    """Set the growth attributes on a deal (validated enums). When `product_id`
    is given (""=clear), it's validated against the catalog and — unless
    `value_ladder_stage` is passed explicitly — the deal's value_ladder_stage is
    auto-derived from the product's rung (each product is pinned to one rung).
    This is the single write path behind the inline card selectors."""
    conn = db.get_conn()
    try:
        prior = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "deal not found"}
        sets, params, changed = [], [], {}
        # Product first: validate + auto-sync the ladder rung to the product's,
        # unless the caller set value_ladder_stage explicitly this call.
        if product_id is not None:
            if product_id == "":
                sets.append("product_id = NULL")
                changed["product_id"] = None
            else:
                prod = db.product_get(product_id)
                if prod is None:
                    return {"status": "error", "error": f"product '{product_id}' not found"}
                sets.append("product_id = ?"); params.append(product_id)
                changed["product_id"] = product_id
                if value_ladder_stage is None and prod.get("value_ladder_stage"):
                    value_ladder_stage = prod["value_ladder_stage"]
        checks = [("value_ladder_stage", value_ladder_stage, VALUE_LADDER),
                  ("growth_loop", growth_loop, GROWTH_LOOP),
                  ("lead_source", lead_source, LEAD_SOURCE)]
        for col, val, allowed in checks:
            if val is not None:
                if val != "" and val not in allowed:
                    return {"status": "error", "error": f"{col} must be one of {allowed}"}
                sets.append(f"{col} = ?"); params.append(val or None)
                changed[col] = val or None
        if next_touch_date is not None:
            sets.append("next_touch_date = ?"); params.append(next_touch_date or None)
            changed["next_touch_date"] = next_touch_date or None
        if expected_close_date is not None:
            # "" clears the date; a non-empty value must be a valid ISO date.
            if expected_close_date != "" and _parse_iso_date(expected_close_date) is None:
                return {"status": "error",
                        "error": "expected_close_date must be ISO 'YYYY-MM-DD' or empty"}
            sets.append("expected_close_date = ?"); params.append(expected_close_date or None)
            changed["expected_close_date"] = expected_close_date or None
        if not sets:
            return {"status": "error", "error": "nothing to update"}
        sets.append("updated_at = ?"); params.append(_now())
        conn.execute(f"UPDATE deals SET {', '.join(sets)} WHERE id = ?", (*params, deal_id))
        crm._log(conn, deal_id, "growth_updated", changed)
        conn.commit()
        return {"status": "updated", "deal": crm.get_deal(deal_id)}
    finally:
        conn.close()


def record_touch(deal_id: str, note: str = "", kind: str = _TOUCH_KIND,
                 next_in_days: int = 7, on_date: Optional[str] = None) -> dict:
    """Cap. 4 touch tracking: bump touch_count, stamp the day the interaction
    HAPPENED, and set the next touch date FROM THE CADENCE LEDGER when one
    exists.

    `on_date` (ISO) exists because the correction inbox files meetings after
    the fact: approving a client meeting of 2026-07-22 used to stamp
    "today", flipping a 24-day-cold deal to warm — the touch clock lying in
    the opposite direction from the bug the inbox exists to fix. A backdated
    touch never rewinds a fresher clock and never rewrites the forward plan;
    a future date is refused.

    Journey fase 1, step 5 — this used to stamp a flat `today + next_in_days`
    unconditionally, and that made it a **second clock**. `set_nurture_status`
    calls it every time a step is marked sent, so recording touch #2 of a
    5-step sequence overwrote the sequence's own step-3 date with +7d: the deal
    drawer showed one date and the nurture panel another, and the materializer
    (which reads the ledger) would have minted a card for a day the deal card
    denied. There is one cadence; `cadence.recompute` derives the date from it —
    `MIN(scheduled_date)` over the still-pending steps.

    The `+next_in_days` fallback survives for deals with **no** ledger at all
    (measured: `nurture_sequences` is empty until the retroactive
    `generate_nurture` runs, and an ad-hoc touch on a deal that was never
    sequenced still deserves a nudge). A deal whose ledger exists but is spent
    gets NULL — honest: there is no next scheduled touch, and inventing one a
    week out is how the stale-deal report fills with fiction.
    """
    from . import cadence          # local: cadence imports crm, crm imports db
    today = datetime.date.today()
    occurred = today
    if on_date is not None:
        try:
            occurred = datetime.date.fromisoformat(str(on_date)[:10])
        except (TypeError, ValueError):
            return {"status": "error", "error": f"on_date '{on_date}' is not YYYY-MM-DD"}
        if occurred > today:
            # A touch that has not happened is not a touch.
            return {"status": "error", "error": "on_date cannot be in the future"}
    backdated = occurred < today
    conn = db.get_conn()
    try:
        prior = conn.execute(
            "SELECT touch_count, lead_source, last_touch_date, next_touch_date "
            "FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "deal not found"}
        new_count = (prior["touch_count"] or 0) + 1
        # The clock only ever moves FORWARD: recording a July meeting today
        # must not make an August-touched deal look colder than it is.
        stamp = occurred.isoformat()
        known = prior["last_touch_date"]
        if known and str(known)[:10] > stamp:
            stamp = str(known)[:10]
        conn.execute(
            "UPDATE deals SET touch_count = ?, last_touch_date = ?, "
            "updated_at = ? WHERE id = ?",
            (new_count, stamp, _now(), deal_id))
        if backdated:
            # Recording history must not rewrite the forward plan the operator
            # (or the cadence ledger) already set.
            nxt = prior["next_touch_date"]
        elif cadence.has_sequence(conn, deal_id):
            nxt = cadence.recompute(conn, deal_id)
        else:
            nxt = (today + datetime.timedelta(days=max(1, next_in_days))).isoformat()
            conn.execute("UPDATE deals SET next_touch_date = ? WHERE id = ?",
                         (nxt, deal_id))
        # kind='touch' (default) or a discovery kind — both counted by scorecard.
        k = kind if kind in (_TOUCH_KIND, *_DISCOVERY_KINDS, *_WARM_KINDS) else _TOUCH_KIND
        channel = prior["lead_source"] or "unknown"
        crm._log(conn, deal_id, k,
                 {"note": note, "touch_count": new_count, "next_touch_date": nxt,
                  "channel": channel, "occurred_on": occurred.isoformat()},
                 source="web", channel=channel)
        conn.commit()
        return {"status": "ok", "deal_id": deal_id, "touch_count": new_count,
                "last_touch_date": stamp, "occurred_on": occurred.isoformat(),
                "next_touch_date": nxt}
    finally:
        conn.close()


# ==========================================================================
# Lead capture (Phase 2) — one form → account + contact + deal
# ==========================================================================

def quick_add_lead(name: str, company: str = "", source: str = "",
                   loop: str = "", notes: str = "",
                   engagement_score: int = 0, industry: str = "",
                   value: Optional[float] = None) -> dict:
    """The minimal capture path: name + company + source + loop → an account, a
    contact, and a deal landed at value-ladder 'iman' / stage 'lead', scored.
    Idempotent on the account (create_account returns the existing id)."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "error": "name required"}
    if source and source not in LEAD_SOURCE:
        return {"status": "error", "error": f"source must be one of {LEAD_SOURCE}"}
    if loop and loop not in GROWTH_LOOP:
        return {"status": "error", "error": f"loop must be one of {GROWTH_LOOP}"}
    company = (company or "").strip() or name

    acct = crm.create_account(company)
    account_id = acct.get("account_id")
    if not account_id:
        return {"status": "error", "error": acct.get("error", "account create failed")}

    # Map the growth lead_source enum to the crm SOURCES vocabulary for the
    # contact/deal `source` (signal provenance) — best-effort, unknown → 'other'.
    _CRM_SRC = {"linkedin": "linkedin", "evento": "event", "referral": "referral",
                "cold_email": "other", "inbound": "website"}
    crm_src = _CRM_SRC.get(source, "")
    contact = crm.create_contact(account_id, name, notes=notes, source=crm_src)
    contact_id = contact.get("contact_id")

    deal = crm.create_deal(account_id, f"{company} — {name}" if company != name else name,
                           stage="lead", value=value, contact_id=contact_id,
                           notes=notes, source=crm_src)
    deal_id = deal.get("deal_id")
    if not deal_id:
        return {"status": "error", "error": deal.get("error", "deal create failed")}

    update_deal_growth(deal_id, value_ladder_stage="iman",
                       growth_loop=loop or None, lead_source=source or None)
    scored = set_lead_features(deal_id, source=source,
                               engagement_score=engagement_score, industry=industry)
    # Auto-generate nurture sequence for new leads.
    try:
        generate_nurture(deal_id)
    except Exception:
        pass
    return {"status": "created", "account_id": account_id, "contact_id": contact_id,
            "deal_id": deal_id, "lead_score": scored.get("score")}


# ==========================================================================
# Read models
# ==========================================================================

def pipeline_math(revenue_goal: Optional[float] = None,
                  avg_ticket: Optional[float] = None) -> dict:
    """Cap. 4 backward funnel: revenue goal → clients → proposals → discovery
    calls → leads → touches. Also reports current pipeline coverage (open deal
    value / goal; aim 3–5×)."""
    cfg = growth_config()
    goal = revenue_goal if revenue_goal is not None else cfg["revenue_goal"]
    ticket = avg_ticket if avg_ticket is not None else cfg["avg_ticket"]
    import math

    def _up(x: float) -> int:
        return int(math.ceil(x)) if x > 0 else 0

    clients = _up(goal / ticket) if ticket else 0
    close = cfg["close_rate"] or 1
    prop_r = cfg["proposal_rate"] or 1
    disc_r = cfg["discovery_rate"] or 1
    proposals = _up(clients / close)
    discovery = _up(proposals / prop_r)
    leads = _up(discovery / disc_r)
    touches = _up(leads * cfg["touches_per_lead"])

    p = crm.pipeline()
    open_value = p.get("open_value", 0) or 0
    coverage = round(open_value / goal, 2) if goal else None

    return {
        "goal": goal, "avg_ticket": ticket, "currency": cfg["currency"],
        "funnel": [
            {"key": "clients", "label": "Clientes", "need": clients, "note": "meta / ticket"},
            {"key": "proposals", "label": "Propuestas", "need": proposals,
             "note": f"cierre {int(close*100)}%"},
            {"key": "discovery", "label": "Discovery calls", "need": discovery,
             "note": f"propuesta {int(prop_r*100)}%"},
            {"key": "leads", "label": "Leads", "need": leads,
             "note": f"discovery {int(disc_r*100)}%"},
            {"key": "touches", "label": "Toques", "need": touches,
             "note": f"{int(cfg['touches_per_lead'])}× / lead"},
        ],
        "revenue_mix": [
            {
                **item,
                "share_pct": round(item["share"] * 100),
                "coverage_pct": round((item["revenue_mxn"] / goal) * 100) if goal else None,
            }
            for item in REVENUE_MIX
        ],
        "revenue_mix_total": sum(item["revenue_mxn"] for item in REVENUE_MIX),
        "revenue_mix_coverage": round(sum(item["revenue_mxn"] for item in REVENUE_MIX) / goal, 2) if goal else None,
        "client_profiles": CLIENT_PROFILES,
        "rates": {"close_rate": close, "proposal_rate": prop_r,
                  "discovery_rate": disc_r, "touches_per_lead": cfg["touches_per_lead"]},
        "pipeline": {"open_value": open_value, "coverage": coverage,
                     "coverage_target": "3–5×",
                     "healthy": bool(coverage is not None and coverage >= 3)},
    }


# Weekly targets for the 5 scorecard KPIs (Cap. 6 — the minimum viable cadence).
# These are baseline activity goals, not stretch goals — zero on any is amber.
# Advisory-era recalibration (2026-08-21): the whole practice runs on 10 h/week
# (≤6 h meetings) around full-time employment, so the floor is deliberately lower
# than the sprint-selling era. Override per-KPI via HERMES_TARGET_<KPI>.
def _envi(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


SCORECARD_TARGETS = {
    "leads": _envi("HERMES_TARGET_LEADS", 1),
    "touches": _envi("HERMES_TARGET_TOUCHES", 5),
    "discovery": _envi("HERMES_TARGET_DISCOVERY", 1),
    "content": _envi("HERMES_TARGET_CONTENT", 1),
    "proposals": _envi("HERMES_TARGET_PROPOSALS", 1),
}


def _scorecard_counts(conn, start: int, end: int) -> dict:
    """Raw KPI counts for a given [start, end) epoch window."""
    def _count(clause: str, params: tuple = ()) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM deal_events WHERE created_at >= ? AND created_at < ? "
            f"AND {clause}", (start, end, *params)).fetchone()[0]

    leads = _count("kind = 'deal_created'")
    # A warm touch / referral ask counts as a touch for the legacy KPI — the
    # layer split below attributes it to the operator, never loses it from K7.
    _tk = (_TOUCH_KIND, *_WARM_KINDS)
    touches = _count(f"kind IN ({','.join('?' * len(_tk))})", _tk)
    ph = ",".join("?" * len(_DISCOVERY_KINDS))
    discovery = _count(f"kind IN ({ph})", _DISCOVERY_KINDS)
    proposals = 0
    for r in conn.execute(
            "SELECT payload FROM deal_events WHERE kind = 'stage_changed' "
            "AND created_at >= ? AND created_at < ?", (start, end)):
        try:
            if (json.loads(r[0] or "{}")).get("to") == "proposal":
                proposals += 1
        except (json.JSONDecodeError, TypeError):
            pass
    content = db.content_pieces_created_between(start, end)
    return {"leads": leads, "touches": touches, "discovery": discovery,
            "content": content, "proposals": proposals}


def scorecard(week: Optional[str] = None) -> dict:
    """The weekly 5 (Cap. 6), AUTO-derived from the week's events — never typed:
      leads · touches · discovery calls · content · proposals.
    Everything from deal_events + content_log inside [Mon, next Mon).
    Returns per-KPI: value, target, wow_delta (vs prior week), wow_pct."""
    start, end, wk = _week_bounds(week)
    # Prior week window for WoW delta.
    prev_start = start - 7 * 86400
    prev_end = start
    conn = db.get_conn()
    try:
        cur = _scorecard_counts(conn, start, end)
        prev = _scorecard_counts(conn, prev_start, prev_end)
    finally:
        conn.close()

    kpi_defs = [
        ("leads", "Leads", "🎯"),
        ("touches", "Toques", "✋"),
        ("discovery", "Discovery calls", "☎️"),
        ("content", "Contenido", "✍️"),
        ("proposals", "Propuestas", "📄"),
    ]
    kpis = []
    for key, label, icon in kpi_defs:
        val = cur[key]
        prev_val = prev[key]
        target = SCORECARD_TARGETS.get(key)
        delta = val - prev_val
        wow_pct = None
        if prev_val > 0:
            wow_pct = round((delta / prev_val) * 100)
        elif val > 0:
            wow_pct = 100  # went from zero to something
        kpis.append({
            "key": key, "label": label, "value": val, "icon": icon,
            "target": target,
            "wow_delta": delta,
            "wow_pct": wow_pct,
            "prev_value": prev_val,
        })
    total = sum(cur.values())
    return {"week": wk, "start": start, "end": end, "kpis": kpis,
            "total_activity": total,
            "layers": _scorecard_layers(start, end, cur)}


def _scorecard_layers(start: int, end: int, cur: dict) -> dict:
    """The motor-caliente split (2026-08-09, approved): the operator's layer is
    declaración + warm touches + referral asks; the fleet's layer is cadence
    and registro. Additive — the legacy `kpis` shape is untouched, so
    bin/kpi-brief (K7) and bin/hermes-watch keep reading what they always did.
    Identity: fleet.touches_raw + ricardo.toques_calientes +
    ricardo.referral_asks == the legacy Toques KPI."""
    conn = db.get_conn()
    try:
        def _count(clause: str, params: tuple = ()) -> int:
            return conn.execute(
                "SELECT COUNT(*) FROM deal_events WHERE created_at >= ? "
                f"AND created_at < ? AND {clause}",
                (start, end, *params)).fetchone()[0]

        warm = _count("kind = 'warm_touch'")
        referral_asks = _count("kind = 'referral_ask'")
        raw_touches = _count("kind = ?", (_TOUCH_KIND,))
        speaking = conn.execute(
            "SELECT COUNT(*) FROM speaking_events WHERE created_at >= ? "
            "AND created_at < ?", (start, end)).fetchone()[0]
        # Sync fidelity: terminal success rate, with the backlog visible —
        # pending/leased events are NOT counted as success (a 100% built on
        # one digested event atop 100 pending would be a lie).
        digs = {r[0]: r[1] for r in conn.execute(
            "SELECT digest_status, COUNT(*) FROM capture_events "
            "WHERE captured_at >= ? AND captured_at < ? GROUP BY digest_status",
            (start, end))}
        digested = digs.get("digested", 0)
        failed = digs.get("failed", 0) + digs.get("dead_letter", 0)
        pending = digs.get("pending", 0) + digs.get("leased", 0)
        terminal = digested + failed
        fidelity = round(digested / terminal, 3) if terminal else None
        # The retainer bug fix (a monthly retainer was invisible): MRR = won deals that
        # recur monthly. Read from the deals table, never self-reported.
        mrr = conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM deals WHERE stage = 'won' "
            "AND recurrence_type = 'monthly'").fetchone()[0]
    finally:
        conn.close()
    return {
        "ricardo": {
            "declaracion": cur["content"] + speaking,
            "toques_calientes": warm,
            "referral_asks": referral_asks,
        },
        "fleet": {
            "leads": cur["leads"],
            "touches_raw": raw_touches,
            "discovery": cur["discovery"],
            "sync": {"digested": digested, "failed": failed,
                     "pending": pending, "fidelity": fidelity},
        },
        "mrr": mrr,
    }


def growth_loops() -> dict:
    """Cap. 3: the three flywheels. Per loop — leads generated, conversion
    (won / non-lost), and the output→input ratio (won deals that then spawned a
    downstream deal via the loop; approximated as won / leads for a services
    shop). Deals with no growth_loop fold into an 'unassigned' bucket."""
    deals = crm.list_deals()
    meta = {
        "autoridad": {"label": "Autoridad → Inbound", "color": "#60a5fa",
                      "input": "Contenido publicado", "output": "Leads inbound"},
        "referido": {"label": "Cliente → Referido", "color": "#34d399",
                     "input": "Clientes felices", "output": "Intros / referidos"},
        "producto": {"label": "Producto → Datos", "color": "#a78bfa",
                     "input": "Producto usado", "output": "Datos / casos"},
    }
    loops = {}
    for key in GROWTH_LOOP:
        loops[key] = {"key": key, **meta[key], "leads": 0, "won": 0, "lost": 0,
                      "open": 0, "value_won": 0.0}
    for d in deals:
        gl = d.get("growth_loop")
        if gl not in loops:
            continue
        b = loops[gl]
        b["leads"] += 1
        st = d.get("stage")
        if st in crm._WON_OUTCOMES:
            b["won"] += 1
            b["value_won"] += d.get("value") or 0
        elif st == "lost":
            b["lost"] += 1
        else:
            b["open"] += 1
    out = []
    for key in GROWTH_LOOP:
        b = loops[key]
        closed = b["won"] + b["lost"]
        b["conversion"] = round(b["won"] / closed, 2) if closed else None
        b["ratio"] = round(b["won"] / b["leads"], 2) if b["leads"] else None
        out.append(b)
    return {"loops": out,
            "total_leads": sum(b["leads"] for b in out),
            "total_won": sum(b["won"] for b in out)}


# ---------------------------------------------------------------- content log

# ==========================================================================
# Content pipeline — pieces linked to growth loops (content_pieces table).
# The source of truth for the calendar + cadence (supersedes content_log).
# ==========================================================================

def _content_id() -> str:
    return "cnt_" + _uuid.uuid4().hex[:12]


def _valid_publish_date(d: str) -> bool:
    try:
        datetime.date.fromisoformat(d)
        return True
    except (ValueError, TypeError):
        return False


def create_content_piece(title: str = "", topic: str = "", channel: str = "",
                         growth_loop: str = "", hook: str = "",
                         publish_date: str = "", status: str = "") -> dict:
    """Validate + insert a content piece. publish_date defaults to today (so a
    bare piece lands on the calendar); status defaults to 'idea'."""
    title = (title or "").strip()
    if not title:
        return {"status": "error", "error": "title required"}
    channel = (channel or "").strip().lower()
    if channel and channel not in CONTENT_CHANNEL:
        return {"status": "error", "error": f"channel must be one of {list(CONTENT_CHANNEL)}"}
    loop = (growth_loop or "").strip().lower()
    if loop and loop not in GROWTH_LOOP:
        return {"status": "error", "error": f"growth_loop must be one of {list(GROWTH_LOOP)}"}
    st = (status or "").strip().lower() or "idea"
    if st not in CONTENT_STATUS:
        return {"status": "error", "error": f"status must be one of {list(CONTENT_STATUS)}"}
    pub = (publish_date or "").strip() or _today()
    if not _valid_publish_date(pub):
        return {"status": "error", "error": "publish_date must be YYYY-MM-DD"}

    db.ensure_content_pieces_schema()
    cid = _content_id()
    db.content_piece_insert({
        "id": cid, "title": title, "topic": (topic or "").strip() or None,
        "channel": channel or None, "growth_loop": loop or None,
        "hook": (hook or "").strip() or None, "publish_date": pub,
        "status": st, "created_at": _now(),
    })
    return {"status": "created", "content_id": cid, "piece": db.content_piece_get(cid)}


def update_content_piece(cid: str, updates: dict) -> dict:
    """Patch whitelisted fields on a content piece. Unknown keys ignored."""
    if not db.content_piece_get(cid):
        return {"status": "error", "error": "content piece not found"}
    fields: dict = {}
    if "title" in updates:
        nm = (updates["title"] or "").strip()
        if not nm:
            return {"status": "error", "error": "title cannot be empty"}
        fields["title"] = nm
    if "topic" in updates:
        fields["topic"] = (updates["topic"] or "").strip() or None
    if "hook" in updates:
        fields["hook"] = (updates["hook"] or "").strip() or None
    if "channel" in updates:
        ch = (updates["channel"] or "").strip().lower()
        if ch and ch not in CONTENT_CHANNEL:
            return {"status": "error", "error": f"channel must be one of {list(CONTENT_CHANNEL)}"}
        fields["channel"] = ch or None
    if "growth_loop" in updates:
        lp = (updates["growth_loop"] or "").strip().lower()
        if lp and lp not in GROWTH_LOOP:
            return {"status": "error", "error": f"growth_loop must be one of {list(GROWTH_LOOP)}"}
        fields["growth_loop"] = lp or None
    if "status" in updates:
        st = (updates["status"] or "").strip().lower()
        if st not in CONTENT_STATUS:
            return {"status": "error", "error": f"status must be one of {list(CONTENT_STATUS)}"}
        fields["status"] = st
    if "publish_date" in updates:
        pub = (updates["publish_date"] or "").strip()
        if pub and not _valid_publish_date(pub):
            return {"status": "error", "error": "publish_date must be YYYY-MM-DD"}
        fields["publish_date"] = pub or None

    db.content_piece_update(cid, fields)
    return {"status": "ok", "piece": db.content_piece_get(cid)}


def delete_content_piece(cid: str) -> dict:
    if not db.content_piece_delete(cid):
        return {"status": "error", "error": "content piece not found"}
    return {"status": "ok", "deleted": cid}


def content_cadence(weeks: int = 8) -> dict:
    """Content pieces per ISO week (last N weeks) + the current publishing streak
    (consecutive weeks, ending this week, with ≥1 piece). Reads content_pieces,
    bucketed by publish_date. Also returns the raw `pieces` for the calendar."""
    weeks = max(1, min(52, weeks))
    pieces = db.content_pieces_all()

    by_week: dict[str, list] = {}
    for p in pieces:
        try:
            d = datetime.date.fromisoformat((p.get("publish_date") or "")[:10])
        except (ValueError, TypeError):
            continue
        by_week.setdefault(iso_week(d), []).append(p)

    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    series = []
    for i in range(weeks - 1, -1, -1):
        wk_monday = monday - datetime.timedelta(days=7 * i)
        wk = iso_week(wk_monday)
        series.append({"week": wk, "count": len(by_week.get(wk, [])),
                       "label": wk_monday.strftime("%b %-d")})
    streak = 0
    for entry in reversed(series):
        if entry["count"] > 0:
            streak += 1
        else:
            break
    return {"weeks": series, "streak": streak,
            "total": sum(len(v) for v in by_week.values()),
            "this_week": len(by_week.get(iso_week(), [])),
            "pieces": pieces}


# ==========================================================================
# Speaking pipeline — talks tracked as attraction-loop pipeline generators.
# ==========================================================================

# PRESERVED, and not a deal stage (ruling 4). A talk's `delivered` means the
# conference happened — a different entity, a different lifecycle, and none of
# the CRITICAL-2 ambiguity: a Speaking row has no commercial-vs-operational
# double meaning to split. The deal-stage retirement stops at the CRM; the two
# vocabularies only ever shared a word.
SPEAKING_STATUS = ("proposed", "accepted", "scheduled", "delivered")
ATTRACTION_LOOP_STATUS = ("none", "pre", "during", "post")


def _speaking_id() -> str:
    return "talk_" + _uuid.uuid4().hex[:12]


def list_speaking() -> dict:
    db.ensure_speaking_schema()
    return {"events": db.speaking_events_all()}


def create_speaking(title: str = "", event_name: str = "", event_date: str = "",
                    status: str = "", attraction_loop_status: str = "",
                    deal_id: str = "") -> dict:
    """Validate + insert a speaking event."""
    title = (title or "").strip()
    if not title:
        return {"status": "error", "error": "title required"}
    st = (status or "").strip().lower() or "proposed"
    if st not in SPEAKING_STATUS:
        return {"status": "error", "error": f"status must be one of {list(SPEAKING_STATUS)}"}
    loop = (attraction_loop_status or "").strip().lower() or "none"
    if loop not in ATTRACTION_LOOP_STATUS:
        return {"status": "error",
                "error": f"attraction_loop_status must be one of {list(ATTRACTION_LOOP_STATUS)}"}
    event_date = (event_date or "").strip()
    if event_date and not _valid_publish_date(event_date):
        return {"status": "error", "error": "event_date must be YYYY-MM-DD"}
    deal_id = (deal_id or "").strip()
    if deal_id and not crm.get_deal(deal_id):
        return {"status": "error", "error": "deal not found"}

    db.ensure_speaking_schema()
    sid = _speaking_id()
    db.speaking_event_insert({
        "id": sid, "title": title, "event_name": (event_name or "").strip() or None,
        "event_date": event_date or None, "status": st,
        "attraction_loop_status": loop, "deal_id": deal_id or None,
        "created_at": _now(),
    })
    return {"status": "created", "speaking_id": sid, "event": db.speaking_event_get(sid)}


def update_speaking(sid: str, updates: dict) -> dict:
    """Patch whitelisted fields on a speaking event. Unknown keys ignored."""
    if not db.speaking_event_get(sid):
        return {"status": "error", "error": "speaking event not found"}
    fields: dict = {}
    if "title" in updates:
        nm = (updates["title"] or "").strip()
        if not nm:
            return {"status": "error", "error": "title cannot be empty"}
        fields["title"] = nm
    if "event_name" in updates:
        fields["event_name"] = (updates["event_name"] or "").strip() or None
    if "status" in updates:
        st = (updates["status"] or "").strip().lower()
        if st not in SPEAKING_STATUS:
            return {"status": "error", "error": f"status must be one of {list(SPEAKING_STATUS)}"}
        fields["status"] = st
    if "attraction_loop_status" in updates:
        lp = (updates["attraction_loop_status"] or "").strip().lower()
        if lp not in ATTRACTION_LOOP_STATUS:
            return {"status": "error",
                    "error": f"attraction_loop_status must be one of {list(ATTRACTION_LOOP_STATUS)}"}
        fields["attraction_loop_status"] = lp
    if "event_date" in updates:
        ed = (updates["event_date"] or "").strip()
        if ed and not _valid_publish_date(ed):
            return {"status": "error", "error": "event_date must be YYYY-MM-DD"}
        fields["event_date"] = ed or None
    if "deal_id" in updates:
        did = (updates["deal_id"] or "").strip()
        if did and not crm.get_deal(did):
            return {"status": "error", "error": "deal not found"}
        fields["deal_id"] = did or None

    db.speaking_event_update(sid, fields)
    return {"status": "ok", "event": db.speaking_event_get(sid)}


def delete_speaking(sid: str) -> dict:
    if not db.speaking_event_delete(sid):
        return {"status": "error", "error": "speaking event not found"}
    return {"status": "ok", "deleted": sid}


# ==========================================================================
# Conversion funnel over time — weekly snapshots of lead→discovery→proposal→won.
# ==========================================================================

# CRM stage → furthest funnel step reached (lost = dropped out, excluded).
#   1 lead · 2 discovery · 3 proposal · 4 won
# `delivered` maps to the same step as `won` — legacy-read vocabulary (see the
# block above STAGE_PROB): a delivered deal was always a won conversion, and now
# it never stops being one, because it never leaves `won` in the first place.
_FUNNEL_STEP = {
    "lead": 1, "engaged": 1,
    "qualified": 2, "demo": 2,
    "proposal": 3,
    "won": 4, "delivered": 4,
}


def compute_funnel(deals: Optional[list] = None) -> dict:
    """Cumulative lead→discovery→proposal→won counts + step conversion rates from
    the CURRENT stage of each deal. A won deal counts in every earlier step
    (monotonic funnel); lost deals are excluded (drop-outs). Rates are fractions."""
    if deals is None:
        deals = crm.list_deals()
    reached = {1: 0, 2: 0, 3: 0, 4: 0}
    for d in deals:
        step = _FUNNEL_STEP.get(d.get("stage"), 0)
        for lvl in range(1, step + 1):
            reached[lvl] += 1
    lead, disc, prop, won = reached[1], reached[2], reached[3], reached[4]

    def _rate(n: int, denom: int) -> float:
        return round(n / denom, 4) if denom else 0.0

    return {
        "lead_count": lead, "discovery_count": disc,
        "proposal_count": prop, "won_count": won,
        "lead_to_discovery_rate": _rate(disc, lead),
        "discovery_to_proposal_rate": _rate(prop, disc),
        "proposal_to_won_rate": _rate(won, prop),
        "overall_rate": _rate(won, lead),
    }


def _week_monday(d: Optional[datetime.date] = None) -> str:
    d = d or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def snapshot_funnel(week_start: Optional[str] = None) -> dict:
    """Compute the current funnel and upsert it as this week's snapshot (one per
    ISO week — re-running the same week overwrites). Called by the Monday timer."""
    db.ensure_conversion_schema()
    wk = (week_start or "").strip() or _week_monday()
    funnel = compute_funnel()
    existing = db.conversion_snapshot_get_week(wk)
    sid = existing["id"] if existing else "conv_" + _uuid.uuid4().hex[:12]
    db.conversion_snapshot_upsert({"id": sid, "week_start": wk, **funnel,
                                   "created_at": _now()})
    return {"status": "ok", "snapshot": db.conversion_snapshot_get_week(wk)}


def funnel_trend(weeks: int = 12) -> dict:
    """Last N weeks of conversion snapshots (oldest→newest). Seeds the first
    snapshot from current deals if the table is empty."""
    weeks = max(1, min(52, weeks))
    db.ensure_conversion_schema()
    if not db.conversion_snapshots_all():
        snapshot_funnel()   # seed the first snapshot from current deals
    rows = db.conversion_snapshots_all()[-weeks:]
    return {"weeks": weeks, "snapshots": rows,
            "latest": rows[-1] if rows else None}


# ==========================================================================
# Time-block calendar (Today tab) — the operator's week as role-specialized blocks.
# Lead-gen playbook: each block is a "hat" he wears at a fixed time so the whole
# funnel gets worked every week. Seeded once, then user-editable.
# ==========================================================================

TIME_BLOCK_ROLES = ("sdr", "ae", "marketer", "consultant", "analyst",
                    "employment", "study")

# The 5 default blocks (seeded on first read). day_of_week: 0=Mon … 6=Sun.
# "Rest = Consultant" and "Month-end = Analyst" from the playbook are pinned to
# representative weekday slots so they render in the weekly calendar; labels keep
# the cadence note. Users can retime/add/remove any of them afterwards.
_DEFAULT_TIME_BLOCKS = (
    (0, "09:00", "13:00", "sdr",        "Prospección (SDR)"),
    (2, "09:00", "13:00", "ae",         "Discovery & Propuestas (AE)"),
    (4, "09:00", "13:00", "marketer",   "Contenido & Scorecard (Marketer)"),
    (1, "09:00", "18:00", "consultant", "Entrega / Consultoría (Consultant)"),
    (4, "14:00", "16:00", "analyst",    "Pipeline Math (Analyst · fin de mes)"),
)


def _time_block_id() -> str:
    return "tblk_" + _uuid.uuid4().hex[:12]


def _valid_hhmm(t: str) -> bool:
    """True iff t is a zero-padded 24h HH:MM string (00:00–23:59)."""
    try:
        parts = (t or "").split(":")
        if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (TypeError, ValueError):
        return False


def _seed_time_blocks() -> None:
    """Insert the 5 default blocks. Caller guarantees the table is empty."""
    for dow, start, end, role, label in _DEFAULT_TIME_BLOCKS:
        db.time_block_insert({
            "id": _time_block_id(), "day_of_week": dow, "start_time": start,
            "end_time": end, "role": role, "label": label, "active": 1,
            "done_week": None, "created_at": _now(),
        })


def _read_block(bid: str) -> Optional[dict]:
    """Fetch one block with derived `active`/`done` fields (like list_time_blocks)."""
    b = db.time_block_get(bid)
    if not b:
        return None
    b["active"] = bool(b.get("active"))
    b["done"] = (b.get("done_week") == iso_week())
    return b


def list_time_blocks() -> dict:
    """The weekly schedule. Seeds the 5 default blocks on first call (empty
    table). `done` is derived: true iff the block was marked done THIS ISO week
    (so it auto-resets every Monday)."""
    db.ensure_time_blocks_schema()
    if db.time_blocks_count() == 0:
        _seed_time_blocks()
    wk = iso_week()
    blocks = []
    for b in db.time_blocks_all():
        b["active"] = bool(b.get("active"))
        b["done"] = (b.get("done_week") == wk)
        blocks.append(b)
    return {"week": wk, "roles": list(TIME_BLOCK_ROLES), "blocks": blocks}


def time_block_activities(day_of_week: int) -> dict:
    """Tasks planned for the next occurrence of `day_of_week` (0=Mon)."""
    db.ensure_time_blocks_schema()
    today = _dt.date.today()
    delta = (day_of_week - today.weekday()) % 7
    target = today + _dt.timedelta(days=delta)
    date = target.isoformat()
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.title, t.status, t.priority, t.assignee, "
            "       p.name AS project_name, p.color AS project_color, "
            "       t.planned_for, t.due_date "
            "FROM tasks t "
            "LEFT JOIN projects p ON t.project_id = p.id "
            "WHERE t.planned_for = ? AND t.status NOT IN ('done','rejected') "
            "  AND (t.assignee IS NULL OR t.assignee IN ('ricardo','user')) "
            "ORDER BY COALESCE(t.plan_order, 999), t.priority DESC, t.created_at ASC",
            (date,),
        ).fetchall()
        activities = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"date": date, "day_of_week": day_of_week, "activities": activities}


def create_time_block(day_of_week=None, start_time: str = "", end_time: str = "",
                      role: str = "", label: str = "", active: bool = True) -> dict:
    """Validate + insert a custom block."""
    try:
        dow = int(day_of_week)
    except (TypeError, ValueError):
        return {"status": "error", "error": "day_of_week must be an integer 0–6"}
    if not 0 <= dow <= 6:
        return {"status": "error", "error": "day_of_week must be 0–6 (0=Mon)"}
    role = (role or "").strip().lower()
    if role not in TIME_BLOCK_ROLES:
        return {"status": "error", "error": f"role must be one of {list(TIME_BLOCK_ROLES)}"}
    start_time = (start_time or "").strip()
    end_time = (end_time or "").strip()
    if not _valid_hhmm(start_time) or not _valid_hhmm(end_time):
        return {"status": "error", "error": "start_time/end_time must be HH:MM (24h)"}
    if end_time <= start_time:
        return {"status": "error", "error": "end_time must be after start_time"}
    label = (label or "").strip()
    if not label:
        return {"status": "error", "error": "label required"}

    db.ensure_time_blocks_schema()
    bid = _time_block_id()
    db.time_block_insert({
        "id": bid, "day_of_week": dow, "start_time": start_time,
        "end_time": end_time, "role": role, "label": label,
        "active": 1 if active else 0, "done_week": None, "created_at": _now(),
    })
    return {"status": "created", "time_block_id": bid, "block": _read_block(bid)}


def update_time_block(bid: str, updates: dict) -> dict:
    """Patch whitelisted fields. Special key `done` (bool) marks/unmarks the
    block done for the CURRENT week. Unknown keys ignored."""
    db.ensure_time_blocks_schema()
    cur = db.time_block_get(bid)
    if not cur:
        return {"status": "error", "error": "time block not found"}
    fields: dict = {}
    if "day_of_week" in updates:
        try:
            dow = int(updates["day_of_week"])
        except (TypeError, ValueError):
            return {"status": "error", "error": "day_of_week must be an integer 0–6"}
        if not 0 <= dow <= 6:
            return {"status": "error", "error": "day_of_week must be 0–6 (0=Mon)"}
        fields["day_of_week"] = dow
    if "role" in updates:
        role = (updates["role"] or "").strip().lower()
        if role not in TIME_BLOCK_ROLES:
            return {"status": "error", "error": f"role must be one of {list(TIME_BLOCK_ROLES)}"}
        fields["role"] = role
    if "start_time" in updates:
        st = (updates["start_time"] or "").strip()
        if not _valid_hhmm(st):
            return {"status": "error", "error": "start_time must be HH:MM (24h)"}
        fields["start_time"] = st
    if "end_time" in updates:
        et = (updates["end_time"] or "").strip()
        if not _valid_hhmm(et):
            return {"status": "error", "error": "end_time must be HH:MM (24h)"}
        fields["end_time"] = et
    # Cross-field time check against the merged (new ∪ existing) values.
    new_start = fields.get("start_time", cur["start_time"])
    new_end = fields.get("end_time", cur["end_time"])
    if new_end <= new_start:
        return {"status": "error", "error": "end_time must be after start_time"}
    if "label" in updates:
        lbl = (updates["label"] or "").strip()
        if not lbl:
            return {"status": "error", "error": "label cannot be empty"}
        fields["label"] = lbl
    if "active" in updates:
        fields["active"] = 1 if updates["active"] else 0
    if "done" in updates:
        # Mark done for THIS week (or clear). Auto-resets next ISO week.
        fields["done_week"] = iso_week() if updates["done"] else None

    db.time_block_update(bid, fields)
    return {"status": "ok", "block": _read_block(bid)}


def delete_time_block(bid: str) -> dict:
    if not db.time_block_delete(bid):
        return {"status": "error", "error": "time block not found"}
    return {"status": "ok", "deleted": bid}


# ==========================================================================
# Pipeline temporal — month-by-month view of deal flow + revenue closed.
# Answers "what happened in my pipeline each month?" without manual tracking.
# ==========================================================================

def _month_bounds(year: int, month: int) -> tuple:
    """(start_epoch, end_epoch) for a calendar month (UTC)."""
    import calendar as _cal
    start = _cal.timegm((year, month, 1, 0, 0, 0, 0, 0, 0))
    if month == 12:
        end = _cal.timegm((year + 1, 1, 1, 0, 0, 0, 0, 0, 0))
    else:
        end = _cal.timegm((year, month + 1, 1, 0, 0, 0, 0, 0, 0))
    return start, end


def _month_label(year: int, month: int) -> str:
    """Short month abbreviation + year (e.g. 'Jul 2026')."""
    return time.strftime("%b %Y", time.gmtime(_month_bounds(year, month)[0]))


# Human "what happened" labels for the expandable per-month event rows. Kinds
# outside this map fall through to the raw kind (+ any note); stage_changed is
# rendered specially as "from → to".
_TEMPORAL_EVENT_LABELS = {
    "deal_created": "creado",
    "touch": "touch",
    "meeting": "reunión",
    "growth_updated": "growth actualizado",
    "deal_linked": "vinculado a iniciativa",
    "deal_unlinked": "desvinculado",
    "deal_product_set": "producto asignado",
    "deal_product_cleared": "producto removido",
    "auto_stalled": "auto-stalled",
    "auto_lost": "auto-lost",
}


def _temporal_event_detail(kind: str, payload: dict) -> str:
    """One-line 'what happened this month' string for a deal_event."""
    p = payload if isinstance(payload, dict) else {}
    if kind == "stage_changed":
        return f"{p.get('from') or '?'} → {p.get('to') or '?'}"
    base = _TEMPORAL_EVENT_LABELS.get(kind, kind)
    note = p.get("note")
    return f"{base}: {note}" if note else base


def pipeline_temporal(months: int = 12) -> dict:
    """Month-by-month pipeline flow: new deals, stage movements, revenue closed.

    Each month bucket carries:
      • new_deals     — deals created (deal_created event) in that month
      • stage_moves   — stage_changed events in that month (forward + backward)
      • won_count     — deals moved to 'won' in that month
      • won_value     — sum of value for won deals that month
      • lost_count    — deals moved to 'lost' in that month
      • active_end    — deals in active stages (not won/lost/stalled) at month end
      • active_value  — sum of value for active deals at month end
      • trend         — up|down|flat of active_value vs the previous month
      • events        — every deal_event that month (created / stage change /
                        touch / won / lost / …), joined to deal + account, for the
                        expandable row: {deal_id, deal_title, account_name, stage,
                        kind, detail, value, date}

    Months with zero activity are included (the table should show the dry spells,
    not skip them — that's the whole point of a temporal view).

    Returns the last `months` calendar months, oldest→newest, including the
    current partial month.
    """
    months = max(1, min(60, months))

    now = datetime.datetime.utcnow()
    # Build the list of (year, month) tuples, oldest→newest.
    ym_list = []
    for i in range(months - 1, -1, -1):
        total = now.year * 12 + (now.month - 1) - i
        y, m = divmod(total, 12)
        ym_list.append((y, m + 1))

    conn = db.get_conn()
    try:
        # Pre-fetch all deals once (cheaper than N queries for active-at-month-end).
        all_deals = [dict(r) for r in conn.execute(
            "SELECT id, stage, value, created_at, closed_at, updated_at FROM deals"
        ).fetchall()]

        # Pre-fetch stage_changed events with their payloads.
        stage_events = []
        for r in conn.execute(
            "SELECT deal_id, payload, created_at FROM deal_events "
            "WHERE kind = 'stage_changed' ORDER BY created_at"
        ).fetchall():
            row = dict(r)
            try:
                row["payload"] = json.loads(row["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                row["payload"] = {}
            stage_events.append(row)

        # Pre-fetch deal_created events for touch/new-deal counts.
        created_events = [dict(r) for r in conn.execute(
            "SELECT deal_id, created_at FROM deal_events WHERE kind = 'deal_created'"
        ).fetchall()]

        # Pre-fetch ALL deal_events joined to deal + account for the expandable
        # per-month rows (spec Option B: "deals that had activity that month").
        activity_events = []
        for r in conn.execute(
            "SELECT e.deal_id, e.kind, e.payload, e.created_at, "
            "d.title AS deal_title, d.stage AS deal_stage, d.value AS deal_value, "
            "a.name AS account_name "
            "FROM deal_events e "
            "JOIN deals d ON d.id = e.deal_id "
            "JOIN accounts a ON a.id = d.account_id "
            "ORDER BY e.created_at"
        ).fetchall():
            row = dict(r)
            try:
                row["payload"] = json.loads(row["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                row["payload"] = {}
            activity_events.append(row)
    finally:
        conn.close()

    buckets = []
    for year, month in ym_list:
        start, end = _month_bounds(year, month)
        mk = f"{year:04d}-{month:02d}"
        label = _month_label(year, month)

        # New deals: deal_created events in [start, end).
        new_deal_ids = set()
        for ev in created_events:
            if start <= ev["created_at"] < end:
                new_deal_ids.add(ev["deal_id"])
        new_deals = len(new_deal_ids)

        # Stage movements: stage_changed events in [start, end).
        stage_moves = 0
        won_count = 0
        won_value = 0.0
        lost_count = 0
        for ev in stage_events:
            if start <= ev["created_at"] < end:
                stage_moves += 1
                to_stage = ev["payload"].get("to", "")
                if to_stage == "won":
                    won_count += 1
                    # Find the deal's value at close time.
                    deal = next((d for d in all_deals if d["id"] == ev["deal_id"]), None)
                    if deal:
                        won_value += deal.get("value") or 0
                elif to_stage == "lost":
                    lost_count += 1

        # Active deals at month end: not in won/lost/stalled, and created before
        # month end, and (closed_at is None OR closed_at >= end — i.e. was still
        # active during this month). We approximate "active at month end" as:
        # created_at < end AND closed_at is NULL or closed_at >= end AND stage
        # not in won/lost/stalled.
        # For a temporal view, we want: how many deals were ALIVE in the pipeline
        # at the end of this month. A deal is alive if it was created before end
        # and not closed before end.
        active_end = 0
        active_value = 0.0
        for d in all_deals:
            created = d.get("created_at") or 0
            closed = d.get("closed_at")
            if created >= end:
                continue  # created after this month
            if closed is not None and closed < end:
                continue  # closed before this month ended
            stage = d.get("stage") or ""
            # Skip stalled/sales-closed — those aren't "active pipeline".
            # `delivered` is legacy-read vocabulary (see the block above
            # STAGE_PROB); it is closed for the same reason `won` is.
            if stage in ("stalled", "won", "delivered", "lost"):
                # But if the deal was only moved to a terminal stage AFTER this
                # month ended, it was still active during this month. Check the
                # stage_events to see if it was moved to a terminal stage before end.
                # Simplification: if closed_at >= end or closed_at is None, the
                # deal was active during this month (even if it's won/lost NOW).
                # The current `closed` check above already handles this — if
                # closed >= end, we don't skip. But the stage check would skip it.
                # Since we can't know the stage at an arbitrary past month without
                # replaying events, we use closed_at as the proxy: if not closed
                # before end, it was active. This is correct for won/lost (which
                # stamp closed_at). For stalled, there's no closed_at, but stalled
                # deals do have updated_at changes — we accept this approximation.
                if stage in ("won", "delivered", "lost") and closed is not None and closed >= end:
                    pass  # was still active during this month
                else:
                    continue
            active_end += 1
            active_value += d.get("value") or 0

        # Expandable row: every deal_event in [start, end), newest first.
        month_events = []
        for ev in activity_events:
            if start <= ev["created_at"] < end:
                month_events.append({
                    "deal_id": ev["deal_id"],
                    "deal_title": ev["deal_title"],
                    "account_name": ev["account_name"],
                    "stage": ev["deal_stage"],
                    "kind": ev["kind"],
                    "detail": _temporal_event_detail(ev["kind"], ev["payload"]),
                    "value": ev["deal_value"],
                    "date": time.strftime("%Y-%m-%d", time.gmtime(ev["created_at"])),
                })
        month_events.reverse()  # newest activity first within the month

        buckets.append({
            "month": mk,
            "label": label,
            "new_deals": new_deals,
            "stage_moves": stage_moves,
            "won_count": won_count,
            "won_value": round(won_value, 2),
            "lost_count": lost_count,
            "active_end": active_end,
            "active_value": round(active_value, 2),
            "events": month_events,
        })

    # Trend of pipeline (active) value vs the previous month.
    prev_active = None
    for b in buckets:
        if prev_active is None:
            b["trend"] = None
        elif b["active_value"] > prev_active:
            b["trend"] = "up"
        elif b["active_value"] < prev_active:
            b["trend"] = "down"
        else:
            b["trend"] = "flat"
        prev_active = b["active_value"]

    # Totals row.
    totals = {
        "month": "total",
        "label": "Total",
        "new_deals": sum(b["new_deals"] for b in buckets),
        "stage_moves": sum(b["stage_moves"] for b in buckets),
        "won_count": sum(b["won_count"] for b in buckets),
        "won_value": round(sum(b["won_value"] for b in buckets), 2),
        "lost_count": sum(b["lost_count"] for b in buckets),
    }

    return {
        "months": months,
        "buckets": buckets,
        "totals": totals,
        "currency": GROWTH_CONFIG.get("currency", "MXN"),
    }


# ==========================================================================
# Growth Operating Framework — operational + strategic views
# ==========================================================================

def conversion_path_for_deal(deal_id: str) -> dict:
    """Where a single deal sits on the value ladder and the measured probability
    of moving to the next rung. Uses parent→child won pairs to measure edge
    probabilities; falls back to priors 0.4/0.5/0.6 until n≥5 samples."""
    deal = crm.get_deal(deal_id)
    if not deal:
        return {"status": "error", "error": "deal not found"}
    conn = db.get_conn()
    try:
        # Won outcomes with a parent_deal_id are "next-rung" conversions.
        # `delivered` is legacy-read vocabulary (see the block above STAGE_PROB):
        # kept so a legacy row still counts, never written.
        rows = [dict(r) for r in conn.execute(
            "SELECT parent_deal_id, value_ladder_stage FROM deals "
            "WHERE stage IN ('won','delivered') AND parent_deal_id IS NOT NULL"
        ).fetchall()]
    finally:
        conn.close()

    # Count transitions from each rung to the child's rung.
    transitions: dict = {}
    for r in rows:
        parent = crm.get_deal(r["parent_deal_id"])
        if not parent:
            continue
        from_stage = (parent.get("value_ladder_stage") or "unassigned").lower()
        to_stage = (r["value_ladder_stage"] or "unassigned").lower()
        transitions.setdefault(from_stage, {}).setdefault(to_stage, 0)
        transitions[from_stage][to_stage] += 1

    # For ladder path iman→entrada→core→recurrente, compute measured rate or prior.
    ladder = list(VALUE_LADDER)
    path = []
    current = (deal.get("value_ladder_stage") or "unassigned").lower()
    for i, stage in enumerate(ladder):
        total_from = sum(transitions.get(stage, {}).values())
        nxt = ladder[i + 1] if i + 1 < len(ladder) else None
        if nxt:
            measured = total_from >= 5
            conv = transitions.get(stage, {}).get(nxt, 0)
            rate = round(conv / total_from, 2) if total_from else None
            if rate is None:
                rate = {0: 0.4, 1: 0.5, 2: 0.6}.get(i, 0.5)
                measured = False
        else:
            rate = None
            measured = None
        path.append({
            "stage": stage,
            "current": stage == current,
            "next_stage": nxt,
            "conversion_rate": rate,
            "measured": measured,
            "samples": total_from if nxt else None,
        })

    # rung counts + won value (from full pipeline)
    all_deals = crm.list_deals()
    rung_counts = {s: {"leads": 0, "won": 0, "won_value": 0.0} for s in ladder}
    unassigned = {"leads": 0, "won": 0, "won_value": 0.0}
    for d in all_deals:
        st = (d.get("value_ladder_stage") or "unassigned").lower()
        bucket = rung_counts.get(st, unassigned)
        bucket["leads"] += 1
        if d.get("stage") in crm._WON_OUTCOMES:
            bucket["won"] += 1
            bucket["won_value"] += d.get("value") or 0

    return {
        "status": "ok",
        "deal_id": deal_id,
        "deal_title": deal.get("title"),
        "current_stage": current,
        "path": path,
        "rung_counts": rung_counts,
        "unassigned": unassigned,
    }


def monthly_strategic_view(month: Optional[str] = None) -> dict:
    """C9 — 8-block strategic monthly view: pipeline_math, revenue mix, growth
    loops, channel metrics, scorecard rollup, scoring accuracy, plan milestones,
    and a decisions-ready summary. Reads live data; no snapshot table required
    for the view itself."""
    today = datetime.date.today()
    month = (month or "").strip() or today.strftime("%Y-%m")
    try:
        year, mon = map(int, month.split("-"))
        month_start = datetime.date(year, mon, 1)
    except (ValueError, TypeError):
        return {"status": "error", "error": "month must be YYYY-MM"}
    if mon == 12:
        month_end = datetime.date(year + 1, 1, 1)
    else:
        month_end = datetime.date(year, mon + 1, 1)
    start_epoch = int(datetime.datetime.combine(
        month_start, datetime.time()).timestamp())
    end_epoch = int(datetime.datetime.combine(
        month_end, datetime.time()).timestamp())

    pm = pipeline_math()
    # Use pipeline_temporal (local, same module) — not crm.pipeline_monthly (doesn't exist).
    temporal = pipeline_temporal(months=12)
    temporal_buckets = temporal.get("buckets", []) if isinstance(temporal, dict) else []
    t = next((b for b in temporal_buckets if b.get("month") == month), None) or {}
    this_month = {
        "nuevos": t.get("new_deals", 0),
        "won": t.get("won_count", 0),
        "lost": t.get("lost_count", 0),
        "revenue_cerrado": t.get("won_value", 0),
        "activos": t.get("active_end", 0),
        "pipeline_value": t.get("active_value", 0),
    }

    # revenue mix vs 4F+2B+3S target (from REVENUE_MIX / pipeline_math)
    revenue_mix = pm.get("revenue_mix", [])
    mix_total = pm.get("revenue_mix_total", 0)
    mix_coverage = pm.get("revenue_mix_coverage", None)

    loops = growth_loops()

    # channel metrics (simple attribution by lead_source over the month)
    conn = db.get_conn()
    try:
        # `IN ('won','delivered')`: legacy-read vocabulary (see the block above
        # STAGE_PROB). Won revenue is unaffected by whether the work shipped.
        channel_rows = [dict(r) for r in conn.execute(
            "SELECT d.lead_source, COUNT(*) AS leads, "
            "SUM(CASE WHEN d.stage IN ('won','delivered') "
            "THEN d.value ELSE 0 END) AS won_value "
            "FROM deals d WHERE d.created_at >= ? AND d.created_at < ? "
            "AND d.lead_source IS NOT NULL "
            "GROUP BY d.lead_source", (start_epoch, end_epoch)).fetchall()]
        # touches per channel: join deal_events to deals by lead_source,
        # since touch payloads don't carry a channel field.
        touch_rows = [dict(r) for r in conn.execute(
            "SELECT d.lead_source FROM deal_events e "
            "JOIN deals d ON d.id = e.deal_id "
            "WHERE e.kind = 'touch' "
            "AND e.created_at >= ? AND e.created_at < ? "
            "AND d.lead_source IS NOT NULL",
            (start_epoch, end_epoch)).fetchall()]
    finally:
        conn.close()

    touches_by_channel: dict = {}
    for r in touch_rows:
        ch = (r.get("lead_source") or "unknown").lower()
        touches_by_channel[ch] = touches_by_channel.get(ch, 0) + 1

    channel_metrics = []
    for cr in channel_rows:
        ch = cr["lead_source"] or "unknown"
        channel_metrics.append({
            "channel": ch,
            "leads": cr.get("leads", 0),
            "won_value": round(cr.get("won_value") or 0, 2),
            "touches": touches_by_channel.get(ch, 0),
        })

    # scorecard rollup over the ISO weeks that fall inside the month.
    weeks: list = []
    d = month_start
    while d < month_end:
        weeks.append(iso_week(d))
        d += datetime.timedelta(days=7)
    if not weeks:
        weeks.append(iso_week(month_start))
    rollup = {"leads": 0, "touches": 0, "discovery": 0, "content": 0, "proposals": 0}
    for wk in weeks:
        sc = scorecard(week=wk)
        for k in rollup:
            rollup[k] += next((x["value"] for x in sc.get("kpis", [])
                               if x["key"] == k), 0)

    # scoring accuracy: won/lost by score band. `delivered` is legacy-read
    # vocabulary (see the block above STAGE_PROB) and is banded with won.
    conn = db.get_conn()
    try:
        scored_deals = [dict(r) for r in conn.execute(
            "SELECT lead_score, stage FROM deals "
            "WHERE stage IN ('won','delivered','lost') "
            "AND lead_score IS NOT NULL").fetchall()]
    finally:
        conn.close()
    bands = {
        "hot (70-100)": {"won": 0, "lost": 0},
        "warm (40-69)": {"won": 0, "lost": 0},
        "cold (0-39)": {"won": 0, "lost": 0},
    }
    for d in scored_deals:
        s = d.get("lead_score") or 0
        st = d.get("stage")
        band = "hot (70-100)" if s >= 70 else "warm (40-69)" if s >= 40 else "cold (0-39)"
        bands[band]["won" if st in crm._WON_OUTCOMES else "lost"] += 1

    # plan milestones progress.
    db.ensure_plan_schema()
    milestones = db.plan_milestones_all()
    total_m = len(milestones)
    done_m = sum(1 for m in milestones if m.get("completed"))

    return {
        "status": "ok",
        "month": month,
        "label": time.strftime("%b %Y", time.gmtime(start_epoch)),
        "pipeline_math": pm,
        "this_month": {
            "nuevos": this_month.get("nuevos", 0),
            "won": this_month.get("won", 0),
            "lost": this_month.get("lost", 0),
            "revenue_cerrado": this_month.get("revenue_cerrado", 0),
            "activos": this_month.get("activos", 0),
            "pipeline_value": this_month.get("pipeline_value", 0),
        },
        "revenue_mix": {
            "items": revenue_mix,
            "total": mix_total,
            "coverage": mix_coverage,
        },
        "growth_loops": loops,
        "channel_metrics": channel_metrics,
        "scorecard_rollup": rollup,
        "score_bands": bands,
        "milestones": {
            "done": done_m,
            "total": total_m,
            "pct": round(done_m / total_m * 100, 1) if total_m else 0,
        },
    }
