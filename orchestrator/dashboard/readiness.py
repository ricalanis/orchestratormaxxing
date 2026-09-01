"""
Readiness Scoring Model — multi-dimensional readiness on top of lead_score.

Where lead_score (growth.py) answers "how good is this lead?", readiness answers
"is this deal ready to BUY, and ready for WHAT?" — three dimensions, each 0–100
(adapted to a solo consultant in Mexico
selling sprint-based AI/data services):

  • BUYER READINESS   — intent / authority / budget / urgency. NOT full
        BANT/MEDDIC (enterprise frameworks, research inaccuracy #3): a lean
        4-signal read from stage, contact role, deal value, close-date proximity
        and Fireflies meeting language.
  • PRODUCT READINESS — is the offer packaged for THIS prospect? From
        product_id (a productized fixed-price offer attached), value-ladder
        rung ↔ pipeline-stage alignment, and technical-question depth in
        meetings (a prospect asking deep technical questions is ready for a
        scoped sprint).
  • MARKET READINESS  — timing: ICP/industry fit, source warmth, engagement
        velocity (accelerating vs cooling event stream), meeting sentiment.

Overall = weighted mix (buyer 50% · market 30% · product 20% — buyer dominates
for a solo consultant: one person can't nurture a big book, so buyer intent is
the scarce signal), then DECAYED by days since last touch — a hot score from 90
days ago is not hot today (research best-practice #3).

Buckets (research table, thresholds conventional until calibrated on real
closed deals — inaccuracy #4):
    0–30 nurture · 31–60 qualified · 61–85 sales_ready · 86–100 hot

Next-best-action maps buckets onto the SPRINT ladder (imán → Sprint 1
definición/validación → Sprint 2 ejecución → recurrente), not a SaaS funnel.

Fireflies: `extract_fireflies_readiness()` parses STORED meeting rows
(fireflies_meetings — signals + raw_summary) for Spanish-first (+ English)
readiness markers: budget mentions, urgency/timeline language, technical
question depth, decision-maker presence, sentiment.

Doctrine (same as crm.py / growth.py): additive ALTERs only, pure scoring
functions so tests pin exact numbers, one validated write path, every persisted
score an event on deal_events, fail-soft reads.
"""
import datetime
import json
import time
from typing import Optional

from . import db
from . import crm

# --- buckets (research table; thresholds conventional, recalibrate on data) --
BUCKETS = (
    ("nurture", 0, 30),
    ("qualified", 31, 60),
    ("sales_ready", 61, 85),
    ("hot", 86, 100),
)

# --- dimension weights (buyer-heavy for a solo consultant) -------------------
DIMENSION_WEIGHTS = {"buyer": 0.5, "market": 0.3, "product": 0.2}

# --- buyer readiness sub-weights (lean intent/authority/budget/urgency) ------
_STAGE_INTENT = {"lead": 5, "engaged": 12, "qualified": 20, "demo": 25,
                 "proposal": 30, "won": 30, "delivered": 30,
                 "lost": 0, "stalled": 0}
_BUYER_MAX = {"intent": 30, "authority": 20, "budget": 25, "urgency": 25}

# Decision-maker role markers — Spanish-first (Mexico), plus English. A solo
# consultant sells to owners/founders/directors, not procurement committees.
_DECISION_ROLES = (
    "ceo", "cto", "coo", "cfo", "founder", "co-founder", "cofounder",
    "fundador", "fundadora", "director", "directora", "dueño", "dueña",
    "socio", "socia", "gerente general", "presidente", "propietario",
    "propietaria", "owner", "vp", "head of",
)

# --- product readiness sub-weights -------------------------------------------
_PRODUCT_MAX = {"packaged": 40, "ladder_fit": 30, "technical_depth": 30}

# Which pipeline stages each value-ladder rung is *expected* to close from.
# Rung ↔ stage alignment = the offer is packaged for where the prospect IS.
_LADDER_STAGE_FIT = {
    "iman": ("lead", "engaged"),
    "entrada": ("engaged", "qualified", "demo"),           # Sprint 1
    "core": ("qualified", "demo", "proposal", "won", "delivered"),
    "recurrente": ("demo", "proposal", "won", "delivered"),
}

# --- market readiness sub-weights --------------------------------------------
_MARKET_MAX = {"icp_fit": 35, "source": 25, "velocity": 25, "sentiment": 15}
_VELOCITY_WINDOW_DAYS = 14

# --- decay: days since last touch → multiplier (research best-practice #3) ---
_DECAY_STEPS = ((7, 1.0), (14, 0.9), (30, 0.75), (60, 0.55))
_DECAY_FLOOR = 0.4

# ==========================================================================
# Fireflies readiness markers — Spanish-first + English (research gap #6:
# the baseline was US-centric; these are the phrases a Mexican prospect
# actually says in a discovery call).
# ==========================================================================

_BUDGET_MARKERS = (
    "presupuesto", "inversión", "inversion", "costo", "precio",
    "cotización", "cotizacion", "cuánto cuesta", "cuanto cuesta",
    "cuánto sale", "cuanto sale", "facturación", "facturacion", "pesos",
    "mxn", "budget", "pricing", "cost", "quote", "how much",
)
_URGENCY_MARKERS = (
    "urgente", "lo antes posible", "cuanto antes", "para ayer", "ya mismo",
    "este mes", "este trimestre", "próxima semana", "proxima semana",
    "empezamos", "arrancamos", "fecha límite", "fecha limite",
    "cuándo empezamos", "cuando empezamos", "urgent", "asap", "timeline",
    "deadline", "this month", "this quarter", "next week", "right away",
)
_TECHNICAL_MARKERS = (
    "api", "integración", "integracion", "arquitectura", "pipeline",
    "base de datos", "infraestructura", "seguridad", "automatización",
    "automatizacion", "modelo", "dashboard", "etl", "stack", "agente",
    "datos", "data warehouse", "machine learning", "llm", "agent",
    "database", "infrastructure", "security", "architecture",
)
_DECISION_MAKER_MARKERS = _DECISION_ROLES + (
    "quien decide", "quién decide", "yo decido", "yo apruebo",
    "decision maker", "sign off", "lo apruebo",
)


def _now() -> int:
    return int(time.time())


def _days_since(iso_or_epoch) -> Optional[int]:
    """Days since an ISO date string or epoch int; None if unset/garbage."""
    if not iso_or_epoch:
        return None
    try:
        if isinstance(iso_or_epoch, (int, float)):
            then = datetime.date.fromtimestamp(int(iso_or_epoch))
        else:
            then = datetime.date.fromisoformat(str(iso_or_epoch)[:10])
        return (datetime.date.today() - then).days
    except (ValueError, TypeError, OSError):
        return None


def bucket_for(score) -> str:
    """0–100 → bucket key. Out-of-range clamps to the edges."""
    try:
        s = int(score)
    except (TypeError, ValueError):
        return BUCKETS[0][0]
    s = max(0, min(100, s))
    for key, lo, hi in BUCKETS:
        if lo <= s <= hi:
            return key
    return BUCKETS[0][0]  # pragma: no cover — ranges are contiguous


def decay_factor(days_since_touch: Optional[int]) -> float:
    """Time decay on the overall score. None (never touched, no activity
    timestamp at all) is treated as fully cold."""
    if days_since_touch is None:
        return _DECAY_FLOOR
    for limit, factor in _DECAY_STEPS:
        if days_since_touch <= limit:
            return factor
    return _DECAY_FLOOR


# ==========================================================================
# Fireflies readiness extraction — pure over stored meeting rows
# ==========================================================================

def _meeting_text(meeting: dict) -> str:
    """Searchable lowercase corpus for one stored fireflies_meetings row:
    title + summary overview + action items + keywords + signal topics."""
    parts = [str(meeting.get("title") or "")]
    signals = meeting.get("signals") or {}
    parts.extend(str(t) for t in (signals.get("topics") or []))
    raw = meeting.get("raw_summary")
    if raw:
        try:
            summary = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            summary = {}
        if isinstance(summary, dict):
            parts.append(str(summary.get("overview") or ""))
            items = summary.get("action_items") or summary.get("actionItems") or []
            if isinstance(items, str):
                items = [items]
            parts.extend(str(i) for i in items)
            kws = summary.get("keywords") or []
            if isinstance(kws, list):
                parts.extend(str(k) for k in kws)
    return " ".join(parts).lower()


def _count_markers(text: str, markers: tuple) -> int:
    """Distinct markers present in the corpus (not raw occurrences — one
    prospect repeating 'presupuesto' five times is one budget signal)."""
    return sum(1 for m in markers if m in text)


def extract_fireflies_readiness(meetings: list) -> dict:
    """Parse stored Fireflies meetings into readiness signals. Pure.

    Returns counts of distinct markers across all meetings plus the latest
    meeting's sentiment/questions — the shape compute_readiness() consumes.
    """
    out = {
        "meetings_analyzed": len(meetings or []),
        "budget_mentions": 0,
        "urgency_mentions": 0,
        "technical_depth": 0,
        "decision_maker_present": False,
        "sentiment": None,
        "questions": 0,
    }
    if not meetings:
        return out
    for m in meetings:
        text = _meeting_text(m)
        out["budget_mentions"] += _count_markers(text, _BUDGET_MARKERS)
        out["urgency_mentions"] += _count_markers(text, _URGENCY_MARKERS)
        out["technical_depth"] += _count_markers(text, _TECHNICAL_MARKERS)
        if _count_markers(text, _DECISION_MAKER_MARKERS):
            out["decision_maker_present"] = True
    # Latest meeting's conversational signals (rows arrive newest-first from
    # db.fireflies_meetings_for_deal).
    latest = (meetings[0].get("signals") or {})
    out["sentiment"] = latest.get("sentiment")
    out["questions"] = int(latest.get("questions") or 0)
    return out


# ==========================================================================
# Dimension scorers — pure (deal dict + events + ff signals in, dict out)
# ==========================================================================

def _buyer_readiness(deal: dict, events: Optional[list],
                     ff: Optional[dict], contact_role: str = "") -> dict:
    """Intent · authority · budget · urgency — the lean BANT read."""
    ff = ff or {}
    # intent: how far the prospect has *walked* into the pipeline
    intent = _STAGE_INTENT.get((deal.get("stage") or "lead"), 0)

    # authority: decision-maker role on the contact, or one showed up in a call
    role = (contact_role or "").strip().lower()
    if role and any(r in role for r in _DECISION_ROLES):
        authority = _BUYER_MAX["authority"]
    elif ff.get("decision_maker_present"):
        authority = 15
    elif role:
        authority = 8   # a named human, but not the one who signs
    else:
        authority = 0

    # budget: an explicit deal value is the strongest signal; meeting budget
    # talk corroborates. Tiers track the product catalog ($18k sprint / $45k
    # retainer / $85k build).
    try:
        value = float(deal.get("value") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 80_000:
        budget = 25
    elif value >= 40_000:
        budget = 20
    elif value >= 18_000:
        budget = 15
    elif value > 0:
        budget = 8
    else:
        budget = 0
    mentions = int(ff.get("budget_mentions") or 0)
    if mentions >= 2:
        budget += 8
    elif mentions == 1:
        budget += 4
    budget = min(budget, _BUYER_MAX["budget"])

    # urgency: close-date proximity + urgency language in meetings
    days_to_close = None
    d = deal.get("expected_close_date")
    if d:
        try:
            days_to_close = (datetime.date.fromisoformat(str(d)[:10])
                             - datetime.date.today()).days
        except (ValueError, TypeError):
            days_to_close = None
    if days_to_close is not None and days_to_close <= 30:
        urgency = 18
    elif days_to_close is not None and days_to_close <= 60:
        urgency = 12
    elif days_to_close is not None and days_to_close <= 90:
        urgency = 6
    else:
        urgency = 0
    umentions = int(ff.get("urgency_mentions") or 0)
    if umentions >= 2:
        urgency += 7
    elif umentions == 1:
        urgency += 4
    urgency = min(urgency, _BUYER_MAX["urgency"])

    return {
        "value": min(100, intent + authority + budget + urgency),
        "max": 100,
        "sub": {"intent": intent, "authority": authority,
                "budget": budget, "urgency": urgency},
    }


def _product_readiness(deal: dict, ff: Optional[dict]) -> dict:
    """Is the offer packaged for this prospect?"""
    ff = ff or {}
    # packaged: a catalog product (fixed price, defined scope) is attached
    ladder = (deal.get("value_ladder_stage") or "").strip().lower()
    if deal.get("product_id"):
        packaged = _PRODUCT_MAX["packaged"]
    elif ladder:
        packaged = 15   # a rung chosen, but no productized offer yet
    else:
        packaged = 0

    # ladder_fit: the rung matches where the prospect actually is
    stage = (deal.get("stage") or "lead")
    if ladder and stage in _LADDER_STAGE_FIT.get(ladder, ()):
        ladder_fit = _PRODUCT_MAX["ladder_fit"]
    elif ladder:
        ladder_fit = 15   # rung set but misaligned — repackage before pitching
    else:
        ladder_fit = 0

    # technical_depth: deep technical questions = prospect scoping real work
    depth = int(ff.get("technical_depth") or 0)
    if depth >= 5:
        tech = _PRODUCT_MAX["technical_depth"]
    elif depth >= 3:
        tech = 20
    elif depth >= 1:
        tech = 10
    else:
        q = int(ff.get("questions") or 0)
        tech = 15 if q >= 6 else (8 if q >= 3 else 0)

    return {
        "value": min(100, packaged + ladder_fit + tech),
        "max": 100,
        "sub": {"packaged": packaged, "ladder_fit": ladder_fit,
                "technical_depth": tech},
    }


def _event_velocity(events: Optional[list]) -> int:
    """Engagement velocity from the deal_events stream: recent 14d window vs
    the prior 14d. Accelerating > steady > cooling > silent."""
    now = time.time()
    recent = prior = 0
    for e in (events or []):
        ts = e.get("created_at") or 0
        age_days = (now - ts) / 86400
        if age_days <= _VELOCITY_WINDOW_DAYS:
            recent += 1
        elif age_days <= 2 * _VELOCITY_WINDOW_DAYS:
            prior += 1
    if recent >= 2 and recent > prior:
        return _MARKET_MAX["velocity"]          # accelerating
    if recent >= 1:
        return 15                               # active
    if prior >= 1:
        return 5                                # cooling
    return 0                                    # silent


def _market_readiness(deal: dict, events: Optional[list],
                      ff: Optional[dict]) -> dict:
    """Timing: ICP fit, source warmth, engagement velocity, sentiment."""
    from . import growth   # late import — growth imports crm, avoid cycles at load
    ff = ff or {}
    industry = (deal.get("industry") or "").strip()
    # growth._industry_fit returns 12 (ICP) / 6 (partial) / 3 (unknown); scale
    # onto this dimension's 35. Unknown industry stays low — timing unproven.
    ind_fit = growth._industry_fit(industry)
    icp_fit = round(ind_fit / 12 * _MARKET_MAX["icp_fit"])

    src = growth._source_quality(deal.get("lead_source") or deal.get("source") or "")
    source = round(src / 12 * _MARKET_MAX["source"])

    velocity = _event_velocity(events)

    sent = (ff.get("sentiment") or "").strip().lower()
    sentiment = {"positive": _MARKET_MAX["sentiment"], "neutral": 8,
                 "negative": 0}.get(sent, 0)

    return {
        "value": min(100, icp_fit + source + velocity + sentiment),
        "max": 100,
        "sub": {"icp_fit": icp_fit, "source": source,
                "velocity": velocity, "sentiment": sentiment},
    }


# ==========================================================================
# Next best action — the sprint ladder, not a SaaS funnel
# ==========================================================================

def next_best_action(bucket: str, deal: Optional[dict] = None) -> str:
    """Bucket → the concrete next move for a solo consultant selling sprints.
    Ladder-aware: a hot deal that already bought Sprint 1 gets the Sprint 2
    upgrade pitch, not another diagnostic."""
    d = deal or {}
    ladder = (d.get("value_ladder_stage") or "").strip().lower()
    if bucket == "hot":
        if ladder in ("core", "recurrente"):
            return ("Cerrar esta semana: propuesta directa de Sprint 2 "
                    "(ejecución) o retainer — mensaje personal en <24h.")
        return ("Contacto personal en <24h: proponer Sprint 2 (ejecución) "
                "directo, o Sprint 1 exprés si falta validar alcance.")
    if bucket == "sales_ready":
        if ladder == "entrada":
            return ("Agendar llamada y pitchear Sprint 1 (definición/"
                    "validación, precio fijo) — el 'sí' de bajo riesgo.")
        return ("Agendar llamada de 15 min esta semana; llevar la ficha del "
                "Sprint 1 (definición/validación) como siguiente paso.")
    if bucket == "qualified":
        return ("Ofrecer el Scorecard gratuito (imán) o un caso de estudio "
                "relevante; mantener cadencia de toques cada 7 días.")
    return ("Nurture: secuencia de 5 toques con contenido educativo; "
            "re-evaluar al próximo signal de intención.")


# ==========================================================================
# The composite — pure, then the persisting write path
# ==========================================================================

def compute_readiness(deal: dict, events: Optional[list] = None,
                      ff_signals: Optional[dict] = None,
                      contact_role: str = "") -> dict:
    """Pure composite: 3 dimensions → weighted overall → decay → bucket.
    Everything score_readiness() persists comes from here, so tests can pin
    exact numbers without a DB."""
    buyer = _buyer_readiness(deal, events, ff_signals, contact_role=contact_role)
    product = _product_readiness(deal, ff_signals)
    market = _market_readiness(deal, events, ff_signals)

    raw = (DIMENSION_WEIGHTS["buyer"] * buyer["value"]
           + DIMENSION_WEIGHTS["product"] * product["value"]
           + DIMENSION_WEIGHTS["market"] * market["value"])

    # Decay off the most recent sign of life: touch date, else the deal's own
    # updated_at (a stage move counts as activity), else created_at.
    days = _days_since(deal.get("last_touch_date"))
    if days is None:
        days = _days_since(deal.get("updated_at"))
    if days is None:
        days = _days_since(deal.get("created_at"))
    factor = decay_factor(days)

    score = max(0, min(100, round(raw * factor)))
    bucket = bucket_for(score)
    return {
        "score": score,
        "bucket": bucket,
        "raw_score": round(raw, 1),
        "decay": {"days_since_activity": days, "factor": factor},
        "dimensions": {"buyer": buyer, "product": product, "market": market},
        "weights": dict(DIMENSION_WEIGHTS),
        "fireflies_signals": ff_signals or None,
        "next_best_action": next_best_action(bucket, deal),
        "computed_at": _now(),
    }


def _contact_role_for_deal(deal: dict) -> str:
    """The linked contact's role, '' when none — the authority signal."""
    cid = deal.get("contact_id")
    if not cid:
        return ""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT role FROM contacts WHERE id = ?", (cid,)).fetchone()
        return (row["role"] or "") if row else ""
    finally:
        conn.close()


def _industry_for_deal(deal_id: str) -> str:
    """Industry lives in lead_scoring_features (growth layer), not on deals —
    read it there for the market ICP-fit signal. '' when unset/absent."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT industry FROM lead_scoring_features WHERE lead_id = ?",
            (deal_id,)).fetchone()
        return (row["industry"] or "") if row else ""
    except Exception:
        return ""
    finally:
        conn.close()


def score_readiness(deal_id: str) -> dict:
    """Load deal + events + stored Fireflies meetings, compute all three
    dimensions, persist readiness_score + readiness_dimensions (JSON) on the
    deal, and event it. The one validated write path."""
    ensure_schema()
    deal = crm.get_deal(deal_id)
    if not deal:
        return {"status": "error", "error": "deal not found"}
    meetings = db.fireflies_meetings_for_deal(deal_id)
    ff = extract_fireflies_readiness(meetings)
    deal = {**deal, "industry": _industry_for_deal(deal_id)}
    result = compute_readiness(deal, events=deal.get("events") or [],
                               ff_signals=ff,
                               contact_role=_contact_role_for_deal(deal))
    conn = db.get_conn()
    try:
        # Deliberately NOT bumping updated_at: a derived-score write is not
        # commercial activity — bumping it would reset the decay clock.
        conn.execute(
            "UPDATE deals SET readiness_score = ?, readiness_dimensions = ? "
            "WHERE id = ?",
            (result["score"], json.dumps(result, default=str), deal_id))
        crm._log(conn, deal_id, "readiness_scored",
                 {"score": result["score"], "bucket": result["bucket"],
                  "buyer": result["dimensions"]["buyer"]["value"],
                  "product": result["dimensions"]["product"]["value"],
                  "market": result["dimensions"]["market"]["value"]})
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "deal_id": deal_id, **result}


def score_readiness_from_fireflies(deal_id: str) -> dict:
    """Parse the deal's STORED Fireflies meetings into readiness signals and
    fold them into the readiness dimensions. Explicit entry point for 'a call
    just happened — what did it change?'. Stored rows only; run
    fetch_fireflies_for_deal first to pull fresh transcripts."""
    ensure_schema()
    deal = crm.get_deal(deal_id)
    if not deal:
        return {"status": "error", "error": "deal not found"}
    meetings = db.fireflies_meetings_for_deal(deal_id)
    if not meetings:
        return {"status": "no_meetings", "deal_id": deal_id,
                "hint": "no stored Fireflies meetings — run "
                        "fetch_fireflies_for_deal first"}
    signals = extract_fireflies_readiness(meetings)
    scored = score_readiness(deal_id)
    if scored.get("status") != "ok":
        return scored
    return {**scored, "fireflies_signals": signals}


# ==========================================================================
# Read model — the dashboard/API view
# ==========================================================================

def readiness_overview() -> dict:
    """All ACTIVE selling deals (sales-closed/stalled excluded) with fresh in-memory
    readiness scores, bucketed, each with its next best action. Read-only —
    persisting is score_readiness()'s job (POST), never a GET side effect."""
    ensure_schema()
    deals = [d for d in crm.list_deals()
             if d.get("stage") not in crm._CLOSED
             and d.get("stage") not in crm._INACTIVE]
    by_bucket = {key: [] for key, _, _ in BUCKETS}
    for d in deals:
        meetings = db.fireflies_meetings_for_deal(d["id"])
        ff = extract_fireflies_readiness(meetings)
        events = crm.list_deal_events(d["id"], limit=50).get("events") or []
        d = {**d, "industry": _industry_for_deal(d["id"])}
        r = compute_readiness(d, events=events, ff_signals=ff,
                              contact_role=_contact_role_for_deal(d))
        by_bucket[r["bucket"]].append({
            "id": d["id"], "title": d.get("title"),
            "account_name": d.get("account_name"), "stage": d.get("stage"),
            "value": d.get("value"), "currency": d.get("currency"),
            "value_ladder_stage": d.get("value_ladder_stage"),
            "lead_score": d.get("lead_score"),
            "readiness_score": r["score"],
            "readiness_bucket": r["bucket"],
            "dimensions": {k: v["value"] for k, v in r["dimensions"].items()},
            "decay": r["decay"],
            "next_best_action": r["next_best_action"],
            "stored_readiness_score": d.get("readiness_score"),
        })
    for items in by_bucket.values():
        items.sort(key=lambda x: -(x["readiness_score"] or 0))
    return {
        "buckets": [
            {"key": key, "range": [lo, hi], "count": len(by_bucket[key]),
             "deals": by_bucket[key]}
            for key, lo, hi in BUCKETS
        ],
        "total_active": len(deals),
        "weights": dict(DIMENSION_WEIGHTS),
    }


# ==========================================================================
# Schema — additive ALTERs on deals (doctrine: never drop, never rebuild)
# ==========================================================================

def ensure_schema() -> None:
    """Add readiness_score / readiness_dimensions to deals. Idempotent."""
    conn = db.get_conn()
    try:
        dcols = [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        if "readiness_score" not in dcols:
            conn.execute("ALTER TABLE deals ADD COLUMN readiness_score INTEGER")
        if "readiness_dimensions" not in dcols:
            conn.execute("ALTER TABLE deals ADD COLUMN readiness_dimensions TEXT")
        conn.commit()
    finally:
        conn.close()
