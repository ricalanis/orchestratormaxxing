"""journey pulse — one client's whole cycle, composed once, TASK-FIRST.

Journey fase 1, step 7 + directiva ADICIÓN 9. The MCP is *the contextual layer*
for the four hosts (Hermes · Claude · Codex · OpenCode): any agent asks "¿dónde
está <cliente>?" and gets back the complete cycle — the deals, the delivering
project, and above all **the open work typed by journey stage** (contacto →
formalización → ejecución → entrega → facturación → cobranza).

Three rules decide everything in this module:

  * **It never guesses which entity you meant** (ruling 1). `refs.resolve` is
    run for all three kinds and the result is a single unambiguous entity or a
    typed `ambiguous` carrying candidates — never a "best match". The one
    tie-break allowed is *strength of match*: an exact id or an exact name beats
    a substring hit, because an exact name is a statement, not a coincidence.
    Without it every account would be ambiguous with its own deals ("Acme" is
    a substring of "Acme — pilot"), which is a refusal that helps nobody. Two
    exact matches are still ambiguous.
  * **It composes, it does not re-query.** The deals come from
    `crm.account_chain`, the project from `sprints.get_project_detail`, the
    attachment counts from `attachments.list_project_hub`. A second copy of
    those reads here would be a second answer to the same question — the exact
    failure mode `attachments` refuses for tasks.
  * **Every derived fact is a rule, never an LLM call.** The task's stage comes
    from `stagekind.derive` (never asked — Ley 1) and each deal's *stopper* from
    the fixed ladder in `stopper()` below. The composer is a pure read: it has
    no writes, and the MCP verbs built on it are read-only proposals (ruling 3).

`compose()` returns a plain dict; `render()` turns it into the compact Spanish
block agents relay. Both are deterministic given (DB, today), which is what lets
the contract be table-driven.
"""
import datetime
import sqlite3
from typing import Optional

from . import attachments
from . import canvas
from . import crm
from . import db
from . import refs
from . import sprints
from . import stagekind

# The three kinds a journey reference may name. Ordered account → deal →
# project only for stable candidate output; resolution itself is order-free
# (all three are tried, and a tie is a refusal, not a precedence win).
KINDS = ("account", "deal", "project")

# The kinds of `deal_events` a HUMAN did — the same whitelist the deal drawer's
# Actividad renders (`index.html` DEAL_HUMAN_EVENTS). A whitelist, not a
# blacklist: a new machine event kind must not be able to appear here by
# default, or the pulse becomes the robot chatter log the drawer already refuses
# to be.
HUMAN_EVENT_KINDS = ("touch", "meeting", "discovery_call", "stage_changed",
                     "delivered_link")

# A task is OPEN unless it is finished, refused, cancelled or parked. Stricter
# than `attachments._OPEN_TASKS` by one status on purpose: `cancelled` is what
# `cadence.close_task` writes when a card's reason to exist vanished, and a
# pulse that still listed those would nag about work the system itself retired.
_OPEN_TASKS = ("t.status NOT IN ('done', 'rejected', 'cancelled') "
               "AND t.archived_at IS NULL")

# Bounds. A pulse is a briefing, not an export: an account with 300 open tasks
# must still answer in one screen, and every cap is applied AFTER a total,
# deterministic ordering so the same DB always yields the same block.
MAX_DEALS = 20
MAX_TASKS = 200
MAX_EVENTS = 5
MAX_PROJECTS = 10
MAX_CANDIDATES = 4


# ---------------------------------------------------------------- helpers

def _error(code: str, message: str, **extra) -> dict:
    """Module-layer error convention (same as crm/sprints/attachments): a dict,
    never a raise — the HTTP edge maps `code` to a status and the MCP handlers
    hand the same dict to an agent."""
    return {"status": "error", "code": code, "error": message, **extra}


def _today(today: Optional[datetime.date] = None) -> datetime.date:
    return today or datetime.date.today()


def _days_since_epoch(value, today: datetime.date) -> Optional[int]:
    """Whole days from an epoch-seconds stamp to `today`, or None."""
    try:
        stamped = datetime.date.fromtimestamp(int(value))
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return (today - stamped).days


def _days_since_iso(value, today: datetime.date) -> Optional[int]:
    """Whole days from a YYYY-MM-DD string to `today`, or None."""
    try:
        stamped = datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return (today - stamped).days


def _clean(value) -> str:
    return str(value or "").strip()


# ---------------------------------------------------------------- the stopper

def stopper(deal: dict, today: Optional[datetime.date] = None) -> Optional[str]:
    """What is STUCK about this deal, in four words, or None.

    A fixed ladder of deterministic rules — no model, no scoring, no threshold
    that drifts. First match wins, and `None` is the common, honest answer.

        lost · stalled                  → None   (an exit, not a position)
        won + no delivering project     → "sin entregar Nd"
        proposal + no touch in >7d      → "propuesta sin respuesta Nd"
        invoiced + unpaid >10d          → "factura sin pago Nd"
        open pipeline + no next touch   → "sin siguiente toque"
        else                            → None

    **Two deviations from the literal spec order, both forced and both load-
    bearing** (reported, not silent):

      1. `invoiced+unpaid` is evaluated BEFORE `no next_touch`. In the written
         order it is unreachable: only a `won` deal can be invoiced
         (`crm._mark_money` requires it) and `crm.update_deal`'s closed-deal
         hygiene clears `next_touch_date` on the transition to won — so every
         invoiced deal would have matched "sin siguiente toque" first and no
         unpaid invoice could ever surface.
      2. `no next_touch` only fires on a deal still IN the pipeline (not won /
         lost / stalled). "Sin siguiente toque" on a delivered-and-paid deal is
         not a stopper, it is the correct end state — and nagging it is how a
         signal teaches the operator to ignore the whole column.

    `lost`/`stalled` yielding None mirrors `stagekind.derive` deliberately: the
    two modules must not disagree about whether a dead deal is a position in the
    cycle.
    """
    today = _today(today)
    stage = _clean(deal.get("stage")).lower()

    # Exits from the cycle — never a stopper (same rule as stagekind.derive).
    if stage in crm._INACTIVE or stage == "lost":
        return None

    if stage == "won" and not deal.get("project_id"):
        days = _days_since_epoch(
            deal.get("closed_at") or deal.get("updated_at") or deal.get("created_at"),
            today)
        return f"sin entregar {days}d" if days is not None else "sin entregar"

    if stage == "proposal":
        # The touch clock, with the same fallback `brief.py` uses when a deal
        # was never touched: its creation date. A proposal nobody has ever
        # touched is the *worst* case, not an exempt one.
        days = _days_since_iso(deal.get("last_touch_date"), today)
        if days is None:
            days = _days_since_epoch(deal.get("created_at"), today)
        if days is not None and days > 7:
            return f"propuesta sin respuesta {days}d"

    if deal.get("invoiced_at") and not deal.get("paid_at"):
        days = _days_since_epoch(deal.get("invoiced_at"), today)
        if days is not None and days > 10:
            return f"factura sin pago {days}d"

    # `crm._CLOSED` + `_INACTIVE` referenced live, not mirrored: this is a
    # runtime rule over the current vocabulary, so it must follow the CRM's
    # definition of "still in the pipeline" rather than freeze a copy of it.
    if stage not in crm._CLOSED and stage not in crm._INACTIVE \
            and not deal.get("next_touch_date"):
        return "sin siguiente toque"

    return None


# ---------------------------------------------------------------- the horizon
# La línea de horizonte (Visión V1). Six numbers, one line, zero cards: the
# glanceable answer to the operator's six questions, deep-linked so a number is one
# tap from the surface that can act on it.
#
# Three properties make it the line and not a sixth dashboard:
#
#   * **Fixed structure.** The six segments ALWAYS render, in this order, even
#     at zero — a segment that disappears at zero makes the line a different
#     shape every morning, and re-reading the shape is the cost the line exists
#     to remove. Zero is dimmed by the client, never hidden.
#   * **The six PARTITION.** No fact is counted twice: `oportunidades_trabadas`
#     is scoped to deals still IN the pipeline, so the two post-win stopper
#     rungs (`sin entregar`, `factura sin pago`) belong to `entregables` and to
#     nothing else. A number that appears in two segments teaches the operator
#     that the line does not add up, and then it stops being read.
#   * **Every count is a rule already in the codebase, referenced live.**
#     `stopper()` decides trabadas (the same ladder the pulse and the deal drawer
#     speak); `crm._CLOSED`/`_INACTIVE` decide what "open" means; the three
#     entregables mirror `cadence._precondition_holds` exactly; `hoy_pendientes`
#     mirrors `canvas.get_day_plan`'s own `do` predicate, so the number over the
#     plan pane and the cards inside it cannot disagree.

# The fixed render order. This tuple IS the line, left to right.
#
# AMENDMENT (m17, cobro first-class): the line gained a 7th segment, `cobro`.
# The first six count ACTIONS and partition — no fact twice. `cobro` is a
# different animal on purpose: a calendar-money lens (what lands this week,
# what is overdue) that deliberately OVERLAPS `entregables.cobrar` (that one
# counts deals whose collect-precondition holds; this one reads the promised
# dates). A documented exception, not drift: money earned its own glanceable
# number, and folding it into the partition would have meant either counting
# it twice silently or hiding the calendar behind an action count.
HORIZON_ORDER = ("clientes_activos", "oportunidades_trabadas", "proyectos_vivos",
                 "delegando", "entregables", "hoy_pendientes", "cobro")

# key → (short label in the line, the long hint on hover, the deep-link target).
#
# The target grammar is the one the client already routes on: `?tab=<key>` is
# read by `routeFromUrl`/`switchTab`, and a `#<id>` target scrolls to an anchor
# on the tab you are already on. Nothing new to interpret.
_HORIZON_META = {
    "clientes_activos": (
        "clientes", "cuentas con un trato abierto o un proyecto vivo", "?tab=crm"),
    "oportunidades_trabadas": (
        "trabadas", "tratos abiertos con un stopper", "?tab=crm"),
    "proyectos_vivos": (
        "proyectos", "proyectos activos o entregando", "?tab=projects"),
    "delegando": (
        "delegando", "tareas abiertas en manos de un agente", "?tab=agent-tasks"),
    # The label spec'd by the Visión: the segment reads "entregables", the hint
    # names the three questions it sums.
    "entregables": (
        "entregables", "por entregar/facturar/cobrar", "?tab=crm"),
    "hoy_pendientes": (
        "hoy", "tareas abiertas en el plan de hoy", "#today-plan-wrap"),
    # The 7th (m17): calendar-money. `display` carries the compact amount the
    # client shows instead of the bare count; `alert` the red overdue suffix.
    "cobro": (
        "cobro", "cobro esperado esta semana y vencido", "#today-cobro"),
}

# A project whose work is alive. `delivering` does not exist yet
# (`sprints.PROJECT_STATUSES` is planned/active/delivered/archived) and is
# handled ahead of its existence for exactly the reason `stagekind` handles it:
# one tuple entry now means the rule does not have to be re-derived the day the
# status lands.
_LIVE_PROJECT_STATUSES = ("active", "delivering")


def _open_stages() -> tuple:
    """The deal stages that are still IN the pipeline.

    Derived from `crm.STAGES` minus `crm._CLOSED` minus `crm._INACTIVE` at call
    time, never mirrored: `stopper()` already refuses to freeze a copy of the
    CRM's vocabulary, and a horizon that disagreed with it about what "open"
    means would count the same deal in two segments.
    """
    return tuple(s for s in crm.STAGES
                 if s not in crm._CLOSED and s not in crm._INACTIVE)


def _entregable_kinds(deal: dict) -> tuple:
    """Which of the three delivery questions this deal is currently asking.

    A mirror of `cadence._precondition_holds` for the three deal-scoped cadence
    kinds — the reason a card exists, read as a state rather than as a card, so
    the number is right even before the materializer has run (and stays right
    after the operator dismissed the card, which is a decision about the CARD,
    not about the money).

      deliver — won with no delivering project      (the money fell out)
      invoice — won, project delivered, no invoice  (the work shipped, unbilled)
      collect — invoiced and never paid             (billed, unpaid)

    A deal may ask more than one at a time only in principle; they are counted
    independently because each is a different action.
    """
    stage = _clean(deal.get("stage")).lower()
    kinds = []
    if stage == "won" and not deal.get("project_id"):
        kinds.append("entregar")
    if (stage == "won" and not deal.get("invoiced_at")
            and _clean(deal.get("project_status")).lower() == "delivered"):
        kinds.append("facturar")
    if deal.get("invoiced_at") and not deal.get("paid_at"):
        kinds.append("cobrar")
    return tuple(kinds)


def _segment(key: str, count: int, **extra) -> dict:
    label, hint, target = _HORIZON_META[key]
    return {"key": key, "count": int(count), "label": label, "hint": hint,
            "target": target, **extra}


def horizon(today: Optional[datetime.date] = None) -> dict:
    """The six numbers of the horizon line, each with its deep-link target.

    Returns `{"status": "ok", "date", "order", <six keys>}` where every one of
    the six keys carries `{key, count, label, hint, target}` (entregables also
    carries `parts`). `order` is `HORIZON_ORDER` — the client renders the line
    by walking it, so the structure of the line is decided here and cannot drift
    into a template.

    Read-only and deterministic given (DB, today). No LLM, no scoring, no
    threshold that moves.
    """
    today = _today(today)
    iso = today.isoformat()
    conn = db.get_conn()
    try:
        open_stages = _open_stages()
        stage_ph = ",".join("?" * len(open_stages))
        live_ph = ",".join("?" * len(_LIVE_PROJECT_STATUSES))

        # --- Q1 clientes activos ------------------------------------------
        clientes = conn.execute(
            "SELECT COUNT(*) FROM accounts a WHERE "
            "  EXISTS (SELECT 1 FROM deals d WHERE d.account_id = a.id "
            f"          AND LOWER(COALESCE(d.stage, '')) IN ({stage_ph})) "
            "  OR EXISTS (SELECT 1 FROM projects p WHERE p.account_id = a.id "
            f"          AND LOWER(COALESCE(p.status, '')) IN ({live_ph}) "
            "            AND p.archived_at IS NULL)",
            (*open_stages, *_LIVE_PROJECT_STATUSES)).fetchone()[0]

        # --- Q2 + Q5: ONE read of the deals, two questions ----------------
        # `SELECT d.*` rather than a column list so a pre-m11 schema (no
        # invoiced_at / paid_at) degrades to "no billing facts" instead of
        # raising — the same tolerance `cadence._deals` buys with a PRAGMA.
        deals = [dict(r) for r in conn.execute(
            "SELECT d.*, p.status AS project_status FROM deals d "
            "LEFT JOIN projects p ON p.id = d.project_id")]

        trabadas = sum(
            1 for d in deals
            if _clean(d.get("stage")).lower() in open_stages
            and stopper(d, today) is not None)

        parts = {"entregar": 0, "facturar": 0, "cobrar": 0}
        for d in deals:
            for kind in _entregable_kinds(d):
                parts[kind] += 1
        entregables = sum(parts.values())

        # --- Q3 proyectos vivos -------------------------------------------
        proyectos = conn.execute(
            f"SELECT COUNT(*) FROM projects WHERE archived_at IS NULL "
            f"AND LOWER(COALESCE(status, '')) IN ({live_ph})",
            _LIVE_PROJECT_STATUSES).fetchone()[0]

        # --- Q4 delegando --------------------------------------------------
        # NULL is NOT a delegation: 31 live rows carry it and they are simply
        # unstamped, not in an agent's hands. Counting them would make the
        # segment a measure of schema debt.
        delegando = conn.execute(
            f"SELECT COUNT(*) FROM tasks t WHERE {_OPEN_TASKS} "
            "AND t.executor_kind IS NOT NULL "
            "AND LOWER(t.executor_kind) != 'human'").fetchone()[0]

        # --- Q6 hoy --------------------------------------------------------
        # The `do` predicate of `canvas.get_day_plan`, constants referenced live
        # (never copied), plus this module's own OPEN filter — so the number
        # over the plan pane counts exactly the cards still to do inside it.
        human_ph = ",".join("?" * len(canvas._HUMAN))
        hoy = conn.execute(
            f"SELECT COUNT(*) FROM tasks t WHERE t.planned_for = ? "
            f"AND (t.assignee IS NULL OR t.assignee IN ({human_ph})) "
            f"AND t.project_id != ? AND {_OPEN_TASKS}",
            (iso, *canvas._HUMAN, canvas._PERSONAL_PROJECT)).fetchone()[0]
    finally:
        conn.close()

    # --- Q7 cobro (m17) — calendar-money over the SAME deals read ----------
    # count = collections landing this week (promised & unpaid, plus already
    # paid inside the window) so the client's existing dim-at-zero works;
    # display = the compact pending amount ('$25k') the bold span shows
    # instead of the count; alert = the red '· N vencido' suffix (>= 3 days
    # past the promise, the same threshold as the block's red strip).
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)
    week_n = overdue_n = 0
    week_pending = 0.0
    for d in deals:
        if _clean(d.get("stage")).lower() != "won":
            continue
        paid_ts = d.get("paid_at")
        if paid_ts:
            if monday <= datetime.date.fromtimestamp(paid_ts) <= sunday:
                week_n += 1
            continue
        exp_raw = d.get("expected_payment_date")
        try:
            exp = datetime.date.fromisoformat(exp_raw) if exp_raw else None
        except (ValueError, TypeError):
            exp = None
        if exp is None:
            continue
        if monday <= exp <= sunday:
            week_n += 1
            week_pending += float(d.get("value") or 0)
        if (today - exp).days >= 3:
            overdue_n += 1
    if week_pending >= 1000:
        display = f"${week_pending / 1000:.0f}k"
    elif week_pending > 0:
        display = f"${week_pending:,.0f}"
    else:
        display = None
    cobro = _segment(
        "cobro", week_n, display=display,
        alert=(f"{overdue_n} vencido" if overdue_n else None))

    return {
        "status": "ok",
        "date": iso,
        "order": list(HORIZON_ORDER),
        "clientes_activos": _segment("clientes_activos", clientes),
        "oportunidades_trabadas": _segment("oportunidades_trabadas", trabadas),
        "proyectos_vivos": _segment("proyectos_vivos", proyectos),
        "delegando": _segment("delegando", delegando),
        "entregables": _segment("entregables", entregables, parts=parts),
        "hoy_pendientes": _segment("hoy_pendientes", hoy),
        "cobro": cobro,
    }


# ---------------------------------------------------------------- resolution

def _candidate(kind: str, item: dict) -> dict:
    return {"kind": kind, "id": item.get("id"), "name": item.get("name")}


def _resolve(conn, ref) -> dict:
    """The entity a reference names, or a typed refusal. Never a guess.

    All three kinds are resolved (one shared connection). Then:

      * exactly one EXACT match (id, or name equal case-insensitively) → that
        entity, even if softer substring matches exist in other kinds;
      * otherwise exactly one match in total → that entity;
      * otherwise → `ambiguous`, carrying every candidate found (≤4, each
        labelled with its kind so the agent can echo a disambiguating ref);
      * nothing anywhere → `not_found`.
    """
    wanted = _clean(ref)
    if not wanted:
        return _error("not_found", "ref is required (an id, a slug or a name)")

    hits, candidates = [], []
    for kind in KINDS:
        res = refs.resolve(kind, wanted, conn)
        if res.get("ok"):
            hit = {"kind": kind, "id": res["id"], "name": res["name"]}
            hits.append(hit)
            candidates.append(dict(hit))
        elif res.get("code") == "ambiguous":
            candidates.extend(_candidate(kind, c) for c in res.get("candidates", []))

    low = wanted.lower()
    exact = [h for h in hits
             if h["id"] == wanted or _clean(h["name"]).lower() == low]
    if len(exact) == 1:
        return {"status": "ok", **exact[0]}
    if len(exact) > 1:
        return _error("ambiguous",
                      f"'{wanted}' names {len(exact)} entities exactly — say which",
                      candidates=exact[:MAX_CANDIDATES])
    if len(hits) == 1 and len(candidates) == 1:
        return {"status": "ok", **hits[0]}
    if candidates:
        return _error("ambiguous",
                      f"'{wanted}' matches {len(candidates)} entities — say which",
                      candidates=candidates[:MAX_CANDIDATES])
    return _error("not_found", f"'{wanted}' matches no account, deal or project")


# ---------------------------------------------------------------- the reads

def _deals_for_project(conn, project_id: str) -> list:
    """The deals a project delivers. The one read with no existing reader:
    `crm.list_deals` filters by stage only and `account_chain` needs an account,
    which a project is not required to have."""
    return [dict(r) for r in conn.execute(
        "SELECT d.*, a.name AS account_name FROM deals d "
        "LEFT JOIN accounts a ON a.id = d.account_id "
        "WHERE d.project_id = ?", (project_id,)).fetchall()]


def _order_deals(deals: list) -> list:
    """Newest activity first, id as the tiebreak — a TOTAL order, so the same
    DB always renders the same block (an unordered tie is how two agents quote
    'the pulse' and disagree)."""
    return sorted(deals, key=lambda d: (-(d.get("updated_at") or 0),
                                        _clean(d.get("id"))))[:MAX_DEALS]


def _deal_view(deal: dict, today: datetime.date) -> dict:
    return {
        "id": deal.get("id"),
        "title": deal.get("title"),
        "stage": deal.get("stage"),
        "value": deal.get("value"),
        "currency": deal.get("currency"),
        "project_id": deal.get("project_id"),
        "next_touch": deal.get("next_touch_date"),
        "invoiced_at": deal.get("invoiced_at"),
        "paid_at": deal.get("paid_at"),
        "stopper": stopper(deal, today),
    }


def _tasks(conn, deal_ids: list, project_ids: list, today: datetime.date) -> list:
    """Every OPEN task on the spine, each carrying its derived journey stage.

    Union of the two lineages, exactly as `crm._drilldown_tasks` defines them:
    the commercial one (`tasks.deal_id`) and the delivery one
    (`tasks.project_id`). The stage is derived per row with the joined facts —
    the task's own deal stage and its own project status, the same inputs
    `canvas._rows` feeds the board chip, so a card and the pulse cannot disagree
    about what stage it is in.

    Degrades to the delivery lineage alone on a pre-m06 schema (no
    `tasks.deal_id`) instead of raising: a pulse missing its sales cards is a
    worse answer than a full one, and no answer at all is worse than both.
    """
    if not deal_ids and not project_ids:
        return []
    where, params = [], []
    if deal_ids:
        where.append(f"t.deal_id IN ({','.join('?' * len(deal_ids))})")
        params.extend(deal_ids)
    if project_ids:
        where.append(f"t.project_id IN ({','.join('?' * len(project_ids))})")
        params.extend(project_ids)
    sql = (
        "SELECT t.id, t.title, t.status, t.planned_for, t.due_date, t.created_at, "
        "       t.project_id, t.deal_id, t.stage_kind, "
        "       d.stage AS deal_stage, p.status AS project_status "
        "FROM tasks t "
        "LEFT JOIN deals d ON d.id = t.deal_id "
        "LEFT JOIN projects p ON p.id = t.project_id "
        f"WHERE ({' OR '.join(where)}) AND {_OPEN_TASKS} "
        f"ORDER BY t.created_at DESC, t.id LIMIT {MAX_TASKS}")
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        if not project_ids:
            return []
        rows = conn.execute(
            "SELECT t.id, t.title, t.status, t.planned_for, t.due_date, "
            "       t.created_at, t.project_id, NULL AS deal_id, "
            "       NULL AS stage_kind, NULL AS deal_stage, p.status AS project_status "
            "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
            f"WHERE t.project_id IN ({','.join('?' * len(project_ids))}) "
            f"AND {_OPEN_TASKS} ORDER BY t.created_at DESC, t.id LIMIT {MAX_TASKS}",
            project_ids).fetchall()

    iso = today.isoformat()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "id": d["id"],
            "title": d["title"],
            "status": d["status"],
            "planned_today": d.get("planned_for") == iso,
            "deal_id": d.get("deal_id"),
            "project_id": d.get("project_id"),
            "stage_kind": stagekind.derive(
                d, deal_stage=d.get("deal_stage"),
                project_status=d.get("project_status")),
        })
    return out


def _events(conn, deal_ids: list) -> list:
    """The last few things a HUMAN did on these deals, newest first."""
    if not deal_ids:
        return []
    kinds = ",".join("?" * len(HUMAN_EVENT_KINDS))
    ids = ",".join("?" * len(deal_ids))
    rows = conn.execute(
        f"SELECT e.deal_id, e.kind, e.created_at, d.title AS deal_title "
        f"FROM deal_events e JOIN deals d ON d.id = e.deal_id "
        f"WHERE e.deal_id IN ({ids}) AND e.kind IN ({kinds}) "
        f"ORDER BY e.created_at DESC, e.id DESC LIMIT {MAX_EVENTS}",
        [*deal_ids, *HUMAN_EVENT_KINDS]).fetchall()
    return [{"deal_id": r["deal_id"], "deal_title": r["deal_title"],
             "kind": r["kind"], "created_at": r["created_at"],
             "date": (datetime.date.fromtimestamp(r["created_at"]).isoformat()
                      if r["created_at"] else None)}
            for r in rows]


def _attachments_summary(project_ids: list) -> dict:
    """Plans · resources · conversations across the projects in scope, counted
    by the hub reader itself (so the derived Fireflies conversations are
    included exactly as the drawer counts them)."""
    total = {"plans": 0, "resources": 0, "conversations": 0}
    for pid in project_ids[:MAX_PROJECTS]:
        hub = attachments.list_project_hub(pid)
        facets = hub.get("facets") if isinstance(hub, dict) else None
        if not facets:
            continue
        for key in total:
            total[key] += (facets.get(key) or {}).get("count", 0)
    return total


def _project_view(project_id: str) -> Optional[dict]:
    """One project as {id, name, status, progress} — read through the existing
    detail reader, so `progress` is the same number the project drawer shows."""
    detail = sprints.get_project_detail(project_id)
    if not isinstance(detail, dict) or detail.get("status") == "error":
        return None
    project = detail.get("project") or {}
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "status": project.get("status"),
        "progress": (detail.get("stats") or {}).get("done_pct"),
    }


# ---------------------------------------------------------------- compose

def compose(ref, today: Optional[datetime.date] = None) -> dict:
    """The whole cycle around one reference, task-first and stage-grouped.

    Returns `{"status": "ok", entity, account, deals, project, projects,
    tasks_by_stage, unstaged_tasks, today, recent_events, attachments_summary,
    url}` — or the typed refusal `_resolve` produced (`ambiguous` with
    candidates, `not_found`).

    Shape notes that are decisions, not accidents:

      * `tasks_by_stage` always carries all six `stagekind.STAGE_KINDS` keys, so
        an empty stage is an empty list rather than a missing key every caller
        has to guard.
      * a task the rule cannot place lands in `unstaged_tasks`, never nowhere.
        Dropping it would make the counts lie, and adding a seventh key to
        `tasks_by_stage` would break the promise that its keys ARE the stage
        vocabulary.
      * `project` is the single delivering project when there is exactly one,
        else None — with `projects` always carrying the full list. An account
        delivering three projects has no "the" project, and picking one would be
        the guess this module exists to refuse.
    """
    today = _today(today)
    conn = db.get_conn()
    try:
        entity = _resolve(conn, ref)
        if entity.get("status") == "error":
            return entity
        kind, eid = entity["kind"], entity["id"]

        # --- account + deals (composed from the existing lateral reader) -----
        account_id = None
        if kind == "account":
            account_id = eid
        elif kind == "deal":
            row = conn.execute("SELECT account_id FROM deals WHERE id = ?",
                               (eid,)).fetchone()
            account_id = row["account_id"] if row else None
        else:
            row = conn.execute("SELECT account_id FROM projects WHERE id = ?",
                               (eid,)).fetchone()
            account_id = row["account_id"] if row else None

        account, deals = None, []
        if account_id:
            chain = crm.account_chain(account_id)
            if isinstance(chain, dict) and chain.get("account"):
                account = {"id": chain["account"].get("id"),
                           "name": chain["account"].get("name")}
                deals = list(chain.get("deals") or [])

        if kind == "deal":
            deals = [d for d in deals if d.get("id") == eid]
            if not deals:
                row = conn.execute("SELECT * FROM deals WHERE id = ?", (eid,)).fetchone()
                deals = [dict(row)] if row else []
        elif kind == "project":
            scoped = [d for d in deals if d.get("project_id") == eid]
            deals = scoped or _deals_for_project(conn, eid)

        deals = _order_deals(deals)
        deal_ids = [d["id"] for d in deals if d.get("id")]

        # --- projects in scope ----------------------------------------------
        project_ids = []
        if kind == "project":
            project_ids.append(eid)
        for d in deals:
            pid = d.get("project_id")
            if pid and pid not in project_ids:
                project_ids.append(pid)
        if kind == "account" and account_id:
            for r in conn.execute(
                    "SELECT id FROM projects WHERE account_id = ? "
                    "AND archived_at IS NULL ORDER BY id", (account_id,)):
                if r["id"] not in project_ids:
                    project_ids.append(r["id"])

        tasks = _tasks(conn, deal_ids, project_ids, today)
        recent_events = _events(conn, deal_ids)
    finally:
        conn.close()

    projects = [p for p in (_project_view(pid) for pid in project_ids[:MAX_PROJECTS])
                if p]

    by_stage = {k: [] for k in stagekind.STAGE_KINDS}
    unstaged = []
    for t in tasks:
        item = {"id": t["id"], "title": t["title"], "status": t["status"],
                "planned_today": t["planned_today"], "deal_id": t["deal_id"],
                "project_id": t["project_id"]}
        (by_stage[t["stage_kind"]] if t["stage_kind"] in by_stage
         else unstaged).append(item)

    return {
        "status": "ok",
        "entity": {"kind": kind, "id": eid, "name": entity.get("name")},
        "account": account,
        "deals": [_deal_view(d, today) for d in deals],
        "project": projects[0] if len(projects) == 1 else None,
        "projects": projects,
        "tasks_by_stage": by_stage,
        "unstaged_tasks": unstaged,
        "today": [t["title"] for t in tasks if t["planned_today"]],
        "recent_events": recent_events,
        "attachments_summary": _attachments_summary(project_ids),
        "url": entity_url(kind, eid),
    }


def entity_url(kind: str, entity_id: str, action: str = "") -> str:
    """The canonical deep link for an entity — the same `?entity=<kind>:<id>`
    grammar the drawer, the Close brief and the cadence cards already speak, so
    a link that works in Telegram works in a card body and in a chat reply."""
    url = f"{db.dashboard_url()}/?entity={kind}:{entity_id}"
    return url + (f"&action={action}" if action else "")


# ---------------------------------------------------------------- render

def _plural(count: int, singular: str, plural: str) -> str:
    """'1 recurso' / '2 recursos'. Spanish agreement matters here because this
    block is read by a human on a phone, not parsed."""
    return f"{count} {singular if count == 1 else plural}"


def _money(deal: dict) -> str:
    value = deal.get("value")
    if value in (None, ""):
        return ""
    try:
        return f"${float(value):,.0f} {_clean(deal.get('currency')) or 'MXN'}"
    except (TypeError, ValueError):
        return ""


def render(payload: dict) -> str:
    """The pulse as the compact Spanish block an agent relays verbatim.

    TASK-FIRST by construction (ADICIÓN 9): the stage sections come first,
    because the question the operator is really asking is "¿qué sigue con este
    cliente?" — the deals and the project are the context for that answer, not
    the answer. Empty stages are omitted; every entity carries its deep link, so
    two taps from a phone reach the panel where the work actually happens.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        code = (payload or {}).get("code", "error")
        if code == "ambiguous":
            names = ", ".join(
                f"{c.get('kind')} «{c.get('name')}»"
                for c in (payload or {}).get("candidates", []))
            return f"Ambiguo — ¿cuál? {names}"
        return f"Sin resultado ({code})."

    entity = payload.get("entity") or {}
    account = payload.get("account") or {}
    lines = []
    name = account.get("name") or entity.get("name") or entity.get("id")
    lines.append(f"{name} — {entity.get('kind')}")
    lines.append(payload.get("url", ""))

    by_stage = payload.get("tasks_by_stage") or {}
    sections = [(k, by_stage.get(k) or []) for k in stagekind.STAGE_KINDS]
    sections.append((None, payload.get("unstaged_tasks") or []))
    total = sum(len(items) for _, items in sections)
    lines.append("")
    lines.append(f"TAREAS ({total} abiertas)")
    if not total:
        lines.append("  (ninguna abierta)")
    for kind, items in sections:
        if not items:
            continue
        label = stagekind.label(kind) if kind else "sin etapa"
        lines.append(f"  {label} ({len(items)})")
        for t in items:
            mark = " ← hoy" if t.get("planned_today") else ""
            lines.append(f"    · [{t.get('status')}] {t.get('title')}{mark}")

    today_titles = payload.get("today") or []
    if today_titles:
        lines.append("")
        lines.append("HOY")
        for title in today_titles:
            lines.append(f"  · {title}")

    deals = payload.get("deals") or []
    if deals:
        lines.append("")
        lines.append("DEALS")
        for d in deals:
            bits = [_clean(d.get("stage"))]
            money = _money(d)
            if money:
                bits.append(money)
            if d.get("next_touch"):
                bits.append(f"próximo toque {d['next_touch']}")
            lines.append(f"  · {d.get('title')} — {' · '.join(b for b in bits if b)}")
            if d.get("stopper"):
                lines.append(f"    ⛔ {d['stopper']}")
            lines.append(f"    {entity_url('deal', d.get('id'))}")

    projects = payload.get("projects") or []
    if projects:
        lines.append("")
        lines.append("PROYECTO" if len(projects) == 1 else "PROYECTOS")
        for p in projects:
            progress = p.get("progress")
            tail = f" · {progress}%" if progress is not None else ""
            lines.append(f"  · {p.get('name')} — {p.get('status')}{tail}")
            lines.append(f"    {entity_url('project', p.get('id'))}")

    events = payload.get("recent_events") or []
    if events:
        lines.append("")
        lines.append("ACTIVIDAD RECIENTE")
        for e in events:
            when = e.get("date") or ""
            lines.append(f"  · {when} {e.get('kind')} — {e.get('deal_title')}")

    att = payload.get("attachments_summary") or {}
    if any(att.values()):
        lines.append("")
        lines.append("ADJUNTOS: " + " · ".join([
            _plural(att.get("plans", 0), "plan", "planes"),
            _plural(att.get("resources", 0), "recurso", "recursos"),
            _plural(att.get("conversations", 0), "conversación", "conversaciones"),
        ]))

    return "\n".join(lines)


# ---------------------------------------------------------------- propose

def propose_deliver(deal_ref, today: Optional[datetime.date] = None) -> dict:
    """A READ-ONLY proposal to deliver a won deal — never the delivery itself.

    Ruling 3: a conversion from chat is a PROPOSAL with a deep link to the web
    modal (two taps, structurally human-only). This function therefore resolves,
    validates, and returns text plus a URL — it opens no write transaction and
    touches no row. `tests/test_journey_pulse.py` asserts the DB is byte
    identical across a call (a digest before and after), which is the only way
    "read-only" is a fact rather than a comment.

    Typed refusals, never prose: `ambiguous` (with candidates) · `not_found` ·
    `not_won` (the deal is still in the pipeline — the money question is not
    settled, and delivering it would be the write this refuses to make) ·
    `already_delivered` (a delivering project is already linked).
    """
    today = _today(today)
    conn = db.get_conn()
    try:
        res = refs.resolve("deal", deal_ref, conn)
        if not res.get("ok"):
            if res.get("code") == "ambiguous":
                return _error(
                    "ambiguous",
                    f"'{_clean(deal_ref)}' matches several deals — say which",
                    candidates=[_candidate("deal", c) for c in res.get("candidates", [])])
            return _error("not_found", f"deal '{_clean(deal_ref)}' not found")
        row = conn.execute(
            "SELECT d.*, a.name AS account_name FROM deals d "
            "LEFT JOIN accounts a ON a.id = d.account_id WHERE d.id = ?",
            (res["id"],)).fetchone()
    finally:
        conn.close()

    if row is None:                                   # pragma: no cover - defensive
        return _error("not_found", f"deal '{_clean(deal_ref)}' not found")
    deal = dict(row)
    stage = _clean(deal.get("stage")).lower()
    if stage != "won":
        return _error("not_won",
                      f"'{deal.get('title')}' is '{stage or 'unknown'}', not won — "
                      f"only a won deal can be delivered",
                      deal_id=deal.get("id"), stage=stage)
    if deal.get("project_id"):
        return _error("already_delivered",
                      f"'{deal.get('title')}' already has a delivering project "
                      f"({deal['project_id']})",
                      deal_id=deal.get("id"), project_id=deal.get("project_id"))

    url = entity_url("deal", deal["id"], action="deliver")
    money = _money(deal)
    client = _clean(deal.get("account_name")) or _clean(deal.get("title"))
    days = _days_since_epoch(
        deal.get("closed_at") or deal.get("updated_at") or deal.get("created_at"), today)
    aged = f" ({days}d sin entregar)" if days else ""
    text = (f"Propuesta: entregar «{deal.get('title')}» de {client}"
            f"{' — ' + money if money else ''}{aged}.\n"
            f"Abre el modal y confirma (2 taps, sólo tú puedes hacerlo):\n{url}")
    return {
        "status": "ok",
        "proposal": text,
        "url": url,
        "deal": {"id": deal["id"], "title": deal.get("title"),
                 "stage": deal.get("stage"), "value": deal.get("value"),
                 "currency": deal.get("currency"),
                 "account_name": deal.get("account_name")},
    }
