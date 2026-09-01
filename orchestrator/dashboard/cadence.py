"""cadence — the OTHER path: sales work becomes TASKS, deterministically.

Journey fase 1, step 5, and the answer to the question that started the whole
encomienda: *"además de las tareas asociadas con la venta, que puede ser OTRO
camino."*

Commercial work already existed in this DB — `nurture_sequences`,
`deals.next_touch_date`, `touch_count`, `deal_events` — and none of it was ever
a **task**. So it never appeared on the kanban the operator likes or in the daily
planner he loves; it lived in a drawer nobody opens at 08:00. This module is the
bridge: it reads those commercial facts and materializes the ONE next thing to
do as a real card, in a real project, with a real due date.

--------------------------------------------------------------------------
Four rules that make a materializer safe to run unattended
--------------------------------------------------------------------------

1. **DETERMINISTIC. No LLM, ever.** Every title, date and precondition here is
   SQL and arithmetic. A generator that phrases cards with a model would make
   the board's contents unreproducible and un-diffable, and the brief's
   no-LLM-in-composition rule (red line 4) exists for the same reason.

2. **ONE open card per deal, enforced by the storage engine.** m06's partial
   UNIQUE index (`idx_tasks_deal_cadence_open`) refuses a second open
   `created_by='cadence'` task on the same deal, and m07's `idx_nurture_task`
   refuses two steps claiming one task. `reconcile()` runs under
   `BEGIN IMMEDIATE` (ruling 7) so two hosts firing the same morning serialize
   instead of racing — but the indexes are the floor, because an application
   rule is a rule that survives until the next caller.

3. **It mints the NEXT step, never the sequence.** Five pending steps produce
   one card. A cadence that dumps five cards on the board is a cadence the
   operator archives on day two.

4. **A human's card is never touched.** An open MANUAL task on a deal blocks
   minting (checked inside the transaction), and is never closed by this module.
   The materializer's job is to notice that nothing is happening — if something
   already is, it gets out of the way.

--------------------------------------------------------------------------
The four kinds of card, and why each is minted-only
--------------------------------------------------------------------------

    touch    — the next pending nurture step, scheduled_date <= today+1
    deliver  — 🚚 a WON deal with no delivering project (money that fell out)
    invoice  — 💵 won + delivered project + `invoiced_at IS NULL`   (ADICIÓN 8)
    collect  — 📩 invoiced, unpaid, and older than COLLECTION_GRACE_DAYS

They are mutually exclusive by construction, not by precedence: `touch` needs an
OPEN deal; the other three need `won`; `deliver` needs no project while
`invoice` needs a delivered one; `invoice` needs `invoiced_at` NULL and
`collect` needs it set. The precedence order in `_DESIRED_ORDER` is therefore
belt-and-braces — it makes the choice deterministic if a future stage vocabulary
ever makes two of them true at once, rather than letting row order decide.

`invoice` and `collect` stamp `stage_kind` explicitly because
`stagekind.derive()` REFUSES to conclude either one (both are "won + delivered"
to a rule — see `dashboard/stagekind.py`). That refusal is what makes the stamp
meaningful: a `facturacion` chip is true only because a writer asserted it.

--------------------------------------------------------------------------
Sticky dismissal — the anti-nag rule that has teeth
--------------------------------------------------------------------------

A card the operator REJECTED or CANCELLED is never minted again for that deal
and kind. Not "not today" — never. The alternative (re-mint tomorrow) turns a
rejection into a snooze button and trains the operator to ignore the whole lane,
which is the failure mode every "smart inbox" ships with. `done` is NOT sticky:
finishing a card is the loop working, and the precondition check is what decides
whether there is a next one.

--------------------------------------------------------------------------
The loop, closed
--------------------------------------------------------------------------

`sprints.set_task_status(task, 'done')` calls `complete_step_for_task()` inside
its own transaction: the step becomes `sent` with `sent_at` (which is what makes
`crm.get_cadence_status`'s compliance arithmetic finally capable of a nonzero
answer), a `touch` deal_event is logged with `source='cadence'` and the step's
`channel`, `deals.touch_count` / `last_touch_date` advance, and `recompute()`
re-derives `next_touch_date` from the remaining pending steps. No new card is
minted mid-afternoon — deliberately. The morning reconcile is the one moment
cards appear, so the board does not grow while the operator is working it.
"""
import datetime
import sqlite3
import uuid
from typing import Optional

from . import db
from . import crm
from . import stagekind

# The lane. m09 creates it; `sales_project_id` resolves id-first, slug-second so
# a collision-suffixed id still works.
SALES_PROJECT_ID = "proj_ventas"
SALES_SLUG = "ventas"

# Every task this module writes carries it. It is the discriminator the m06
# partial UNIQUE index keys on, so it is a load-bearing literal, not a label.
CREATED_BY = "cadence"

# How far ahead a step is minted. ONE day: a card for tomorrow's touch is useful
# at the 08:00 standup; a card for next week is clutter that ages on the board.
LOOKAHEAD_DAYS = 1

# How long an unpaid invoice waits before it becomes a chase (ADICIÓN 8:
# "facturado sin pago >N días"). Ten days is one working fortnight minus the
# weekends — long enough that a normal payment run is not nagged, short enough
# that a forgotten invoice surfaces inside the month it was issued.
COLLECTION_GRACE_DAYS = 10

# Body markers. The kind of a minted card has to survive a round trip through
# the DB, and `stage_kind` cannot carry it (a `touch` card's stage_kind is
# whatever its DEAL implies — contacto, formalizacion — which is the point of
# that column). A marker in the body is unambiguous, greppable, and visible to
# the operator, who should be able to see why a card exists.
MARKER = {
    "touch":   "[cadence:touch]",
    "deliver": "[cadence:deliver]",
    "invoice": "[cadence:invoice]",
    "collect": "[cadence:collect]",
}
KINDS = tuple(MARKER)

# Deterministic precedence (see the module docstring — the four are mutually
# exclusive by construction; this only removes any dependence on row order).
_DESIRED_ORDER = ("deliver", "collect", "invoice", "touch")

# Touch types a MACHINE can send unattended. A step in this set logs a
# `deal_event` and is marked sent — it never becomes a card, because a card
# asking the operator to send something the system already sent is worse than no card
# at all.
#
# Measured: none of the five shipped Hook steps (`growth._HOOK_STEPS` —
# trigger / action / variable_reward / investment / re_trigger) is in it. All
# five are messages the operator writes. The branch exists because `touch_type` is a
# free TEXT column and the sequence generator's vocabulary is open: the day a
# drip channel is added, it must not mint cards.
AUTOMATED_TOUCH_TYPES = ("drip", "newsletter", "auto_email", "sequence")

# One Spanish sentence per Hook step (regla: título = touch_type + cliente).
# Imperative and specific — "Dar seguimiento" is what a card says when nobody
# decided what the touch is FOR.
_TOUCH_LABEL = {
    "trigger":         "Romper el hielo con",
    "action":          "Mandar el recurso a",
    "variable_reward": "Compartir el caso con",
    "investment":      "Pedir 15 min a",
    "re_trigger":      "Cerrar el loop con",
}
_TOUCH_LABEL_DEFAULT = "Dar seguimiento a"

# Statuses that FREE the one-open-per-deal slot. Mirrors the m06 partial index's
# `status NOT IN (…)` exactly — they are one rule, and a drift here would let
# the application believe a slot is free that the storage engine still holds.
SETTLED = ("done", "rejected", "cancelled")

# Settled *by the operator's refusal*. Sticky: never minted again (see docstring).
DISMISSED = ("rejected", "cancelled")

# The kinds a refusal silences FOREVER. All three are DEAL-scoped — there is one
# "deliver this deal" question, one "invoice this deal", one "chase this
# invoice", so refusing the card refuses the question. `touch` is absent: a
# nurture card is STEP-scoped, and one rejected opener must not silence a client
# for the rest of its sequence (see `_dismissed`).
STICKY_KINDS = ("deliver", "invoice", "collect")


# --------------------------------------------------------------------- helpers

def _today() -> datetime.date:
    return datetime.date.today()


def _iso(d: datetime.date) -> str:
    return d.isoformat()


def _gen_task_id() -> str:
    """`t_` + 8 hex, the id shape every other writer in this DB uses (hermes'
    CLI, `sprints._gen_id`). A cadence card must be indistinguishable from any
    other card once it exists — the operator moves it, comments on it and
    completes it with the same verbs."""
    return f"t_{uuid.uuid4().hex[:8]}"


def _money(value, currency: str = "MXN") -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    if not n:
        return "sin valor"
    return f"${n:,.0f}" + (f" {currency}" if currency and currency != "MXN" else "")


def sales_project_id(conn) -> Optional[str]:
    """The `proj_ventas` id, or None when m09 has not run.

    Id first, slug second: m09 pins the id but keeps ensure_taxonomy's collision
    fallback, so a DB where something else already owned `proj_ventas` has the
    row under a suffixed id and only the slug is stable.
    """
    row = conn.execute("SELECT id FROM projects WHERE id = ?",
                       (SALES_PROJECT_ID,)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT id FROM projects WHERE slug = ?",
                       (SALES_SLUG,)).fetchone()
    return row[0] if row else None


def _kind_of(body: Optional[str]) -> Optional[str]:
    """Which of the four cards this is, read back off its body marker."""
    b = body or ""
    for kind, mark in MARKER.items():
        if mark in b:
            return kind
    return None


def _deal_link(deal_id: str, action: str = "") -> str:
    """The deep link the card carries. Same `?entity=` router the drawer, the
    brief and (fase 1 step 7) the chat proposals use — one URL grammar, so a
    link that works in Telegram works in the card body."""
    url = f"{db.dashboard_url()}/?entity=deal:{deal_id}"
    return url + (f"&action={action}" if action else "")


def _client_name(deal: dict) -> str:
    return (deal.get("account_name") or deal.get("title") or "el cliente")


# ------------------------------------------------------------------ the writes

def close_task(conn, task_id: str, *, reason: str) -> None:
    """Cancel a cadence card whose reason to exist is gone. Caller's txn.

    `cancelled`, not `done`: the work did not happen, and a `done` here would
    inflate the velocity feed and the Close brief's delivery line with cards
    nobody worked. `cancelled` is also one of the three statuses the m06 partial
    index treats as settled, so the deal's slot is freed in the same statement.
    """
    from . import sprints
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', completed_at = NULL WHERE id = ?",
        (task_id,))
    sprints._log_event(conn, task_id, "cadence_closed",
                       {"reason": reason, "via": "cadence.reconcile"})


def close_deal_tasks(conn, deal_id: str, *, kinds=KINDS, reason: str) -> list:
    """Close every OPEN cadence card of `kinds` on one deal. Caller's txn.

    Used by `crm.mark_deal_paid` so the cobranza card disappears the instant the
    money is recorded rather than at the next reconcile. Returns the ids closed.
    """
    ph = ",".join("?" * len(SETTLED))
    closed = []
    for row in conn.execute(
            f"SELECT id, body FROM tasks WHERE deal_id = ? AND created_by = ? "
            f"AND status NOT IN ({ph})",
            (deal_id, CREATED_BY, *SETTLED)).fetchall():
        if _kind_of(row["body"]) in kinds:
            close_task(conn, row["id"], reason=reason)
            closed.append(row["id"])
    return closed


def has_sequence(conn, deal_id: str) -> bool:
    """Does this deal have a nurture ledger at all? Read-only.

    The guard that keeps `recompute` from being destructive. A deal with zero
    steps has no ledger to derive a date from, and its `next_touch_date` may
    well have been typed by the operator through `update_deal_growth` — deriving
    NULL over it would delete a human's decision. (Live, re-measured: the whole
    table is empty until the retroactive `generate_nurture` runs, so without
    this guard the first reconcile would clear every next_touch_date on the
    board.)
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM nurture_sequences WHERE deal_id = ? LIMIT 1",
            (deal_id,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def recompute(conn, deal_id: str) -> Optional[str]:
    """Re-derive `deals.next_touch_date` from the ledger. Caller's txn.

    `MIN(scheduled_date)` over the deal's still-PENDING steps, or NULL when the
    sequence is finished. This replaces the flat `+7d` that
    `growth.record_touch` stamped: a next-touch date invented by arithmetic on
    *today* is a second clock, and the moment it disagreed with the sequence the
    operator had two answers to "when do I follow up" and no way to tell which
    was real.

    **No ledger → no write.** A deal with no steps keeps whatever
    `next_touch_date` it has (see `has_sequence`); returning None there means
    "nothing to derive", not "there is no next touch".

    NULL *is* a legitimate derived answer when the ledger exists and is spent —
    the pipeline's stale-deal reader already treats a missing next_touch_date as
    "no scheduled follow-up", which is then the truth.
    """
    if not has_sequence(conn, deal_id):
        return None
    row = conn.execute(
        "SELECT MIN(scheduled_date) FROM nurture_sequences "
        "WHERE deal_id = ? AND status = 'pending' AND scheduled_date IS NOT NULL",
        (deal_id,)).fetchone()
    nxt = row[0] if row else None
    conn.execute("UPDATE deals SET next_touch_date = ? WHERE id = ?", (nxt, deal_id))
    return nxt


def complete_step_for_task(conn, task_id: str) -> Optional[dict]:
    """THE loop closure. Called from `sprints.set_task_status`'s done-branch,
    inside ITS transaction (ruling 8 — this function never opens one).

    A cadence touch card moving to `done` means the touch HAPPENED. Four writes,
    all of which were previously impossible because nothing connected the card
    back to the step it came from:

      1. the step → `sent` + `sent_at` (today, ISO — the shape
         `crm.get_cadence_status` compares against `scheduled_date`);
      2. a `touch` deal_event with `source='cadence'` and `channel` = the step's
         `touch_type` — the provenance m08 added, so the monthly channel rollup
         stops attributing every touch to the deal's original lead_source;
      3. `deals.touch_count += 1` and `last_touch_date = today`, the counters
         the weekly scorecard reads;
      4. `recompute()` — the next pending step becomes the next touch date.

    Returns a summary dict, or None when this task carries no step (which is the
    common case: most tasks are not cadence cards). Fail-soft on a pre-m07
    schema — a missing column must not stop a board move from landing.
    """
    try:
        step = conn.execute(
            "SELECT id, deal_id, touch_type, step_number, status "
            "FROM nurture_sequences WHERE task_id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if step is None or step["status"] != "pending":
        return None

    today = _iso(_today())
    conn.execute(
        "UPDATE nurture_sequences SET status = 'sent', sent_at = ? WHERE id = ?",
        (today, step["id"]))

    deal_id = step["deal_id"]
    prior = conn.execute(
        "SELECT touch_count FROM deals WHERE id = ?", (deal_id,)).fetchone()
    count = ((prior["touch_count"] if prior else 0) or 0) + 1
    conn.execute(
        "UPDATE deals SET touch_count = ?, last_touch_date = ? WHERE id = ?",
        (count, today, deal_id))
    crm._log(conn, deal_id, "touch",
             {"note": f"Cadencia paso {step['step_number']}",
              "touch_count": count, "task_id": task_id,
              "nurture_id": step["id"]},
             source=CREATED_BY, channel=step["touch_type"])
    nxt = recompute(conn, deal_id)
    return {"nurture_id": step["id"], "deal_id": deal_id, "touch_count": count,
            "sent_at": today, "next_touch_date": nxt}


# ------------------------------------------------------------------- the reads

_DEAL_SELECT = """
SELECT d.id, d.title, d.stage, d.value, d.currency, d.account_id,
       d.project_id, d.invoiced_at, d.paid_at,
       a.name AS account_name, p.status AS project_status
FROM deals d
LEFT JOIN accounts a ON a.id = d.account_id
LEFT JOIN projects p ON p.id = d.project_id
"""


def _deals(conn) -> list:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    if {"invoiced_at", "paid_at"} <= cols:
        return [dict(r) for r in conn.execute(_DEAL_SELECT)]
    # Pre-m11: the billing half simply does not apply yet. Selecting the columns
    # would raise and take the nurture half down with it.
    sql = _DEAL_SELECT.replace("d.invoiced_at, d.paid_at,", "")
    return [{**dict(r), "invoiced_at": None, "paid_at": None}
            for r in conn.execute(sql)]


def _open_cadence_task(conn, deal_id: str):
    ph = ",".join("?" * len(SETTLED))
    return conn.execute(
        f"SELECT id, body, stage_kind FROM tasks WHERE deal_id = ? "
        f"AND created_by = ? AND status NOT IN ({ph}) LIMIT 1",
        (deal_id, CREATED_BY, *SETTLED)).fetchone()


def _has_open_manual_task(conn, deal_id: str) -> bool:
    """An OPEN task on this deal that a human (or any other writer) created.

    Ruling 7's second half, and the reason it is checked INSIDE the transaction:
    the whole point of the materializer is to notice that nothing is happening
    on a deal. If the operator already opened "Llamar a Acme", a second card saying
    the same thing in different words is the nag that makes the lane worthless.
    Blocking is one-directional — this module never closes a manual task.
    """
    ph = ",".join("?" * len(SETTLED))
    row = conn.execute(
        f"SELECT 1 FROM tasks WHERE deal_id = ? AND COALESCE(created_by,'') != ? "
        f"AND status NOT IN ({ph}) LIMIT 1",
        (deal_id, CREATED_BY, *SETTLED)).fetchone()
    return row is not None


def _dismissed(conn, deal_id: str, kind: str) -> bool:
    """Has the operator already refused this kind of card on this deal?

    Sticky forever, and only for the three DEAL-SCOPED kinds. Archived counts
    too: the archive is how a card leaves the board without a verdict, and
    re-minting it would make the archive a snooze button.

    `touch` is deliberately NOT sticky, and does not need to be: rejecting
    "Romper el hielo con X" is a verdict on that STEP, not on the deal's whole
    cadence. The structure already handles it — the step keeps its `task_id`
    backref, `_next_step` only considers steps with `task_id IS NULL`, so the
    refused step can never be re-minted while step 3 still can. Making touch
    sticky would let one rejected opener silence a client forever.
    """
    if kind not in STICKY_KINDS:
        return False
    ph = ",".join("?" * len(DISMISSED))
    rows = conn.execute(
        f"SELECT body FROM tasks WHERE deal_id = ? AND created_by = ? "
        f"AND (status IN ({ph}) OR archived_at IS NOT NULL)",
        (deal_id, CREATED_BY, *DISMISSED)).fetchall()
    return any(_kind_of(r["body"]) == kind for r in rows)


def _settle_refused_steps(conn, deal_id: str) -> list:
    """Mark `skipped` any step whose minted card the operator refused.

    Without this the step stays `pending` forever holding a `task_id` that names
    a rejected card: `_next_step` skips it (task_id is not NULL) so it is never
    re-minted — correct — but `recompute` still reads it as the deal's next
    pending touch, so `deals.next_touch_date` would point at a date no card will
    ever come from. The row is kept, with its backref, as the audit of what was
    refused and when.
    """
    ph = ",".join("?" * len(DISMISSED))
    try:
        rows = conn.execute(
            f"SELECT n.id FROM nurture_sequences n JOIN tasks t ON t.id = n.task_id "
            f"WHERE n.deal_id = ? AND n.status = 'pending' "
            f"AND (t.status IN ({ph}) OR t.archived_at IS NOT NULL)",
            (deal_id, *DISMISSED)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        conn.execute("UPDATE nurture_sequences SET status = 'skipped' WHERE id = ?",
                     (r[0],))
        out.append(r[0])
    return out


def _next_step(conn, deal_id: str, horizon: str):
    """The next PENDING, unminted step due on or before `horizon`."""
    try:
        return conn.execute(
            "SELECT id, step_number, touch_type, template_text, scheduled_date "
            "FROM nurture_sequences "
            "WHERE deal_id = ? AND status = 'pending' AND task_id IS NULL "
            "  AND scheduled_date IS NOT NULL AND scheduled_date <= ? "
            "ORDER BY scheduled_date ASC, step_number ASC LIMIT 1",
            (deal_id, horizon)).fetchone()
    except sqlite3.OperationalError:
        return None


def _desired(conn, deal: dict, today: datetime.date, horizon: str) -> Optional[dict]:
    """The ONE card this deal should have open right now, or None.

    Pure decision — it reads, it never writes. Returns
    `{"kind", "title", "body", "due", "stage_kind", "step"}`.
    """
    stage = (deal.get("stage") or "").lower()
    cliente = _client_name(deal)
    value = _money(deal.get("value"), deal.get("currency") or "MXN")

    for kind in _DESIRED_ORDER:
        if kind == "deliver":
            if stage == "won" and not deal.get("project_id"):
                return {
                    "kind": kind,
                    "title": f"🚚 Entregar {deal['title']} — {value}",
                    "body": (
                        f"{MARKER['deliver']} Este trato se ganó y todavía no "
                        f"tiene proyecto que lo entregue: el dinero entró y el "
                        f"trabajo no existe en el sistema.\n\n"
                        f"Abrir y entregar: {_deal_link(deal['id'], 'deliver')}"),
                    "due": _iso(today),
                    "stage_kind": "entrega",
                    "step": None,
                }
        elif kind == "collect":
            inv = deal.get("invoiced_at")
            if stage == "won" and inv and not deal.get("paid_at"):
                days = max(0, (int(_now_ts()) - int(inv)) // 86400)
                if days >= COLLECTION_GRACE_DAYS:
                    return {
                        "kind": kind,
                        "title": (f"Seguimiento de cobro {cliente} — "
                                  f"factura hace {days}d"),
                        "body": (
                            f"{MARKER['collect']} Facturado hace {days} días y "
                            f"sin pago registrado ({value}).\n\n"
                            f"Trato: {_deal_link(deal['id'])}"),
                        "due": _iso(today),
                        "stage_kind": "cobranza",
                        "step": None,
                    }
        elif kind == "invoice":
            if (stage == "won" and deal.get("project_id")
                    and (deal.get("project_status") or "") == "delivered"
                    and not deal.get("invoiced_at")):
                return {
                    "kind": kind,
                    "title": f"Facturar {cliente} — {value}",
                    "body": (
                        f"{MARKER['invoice']} El proyecto está entregado y el "
                        f"trato sigue sin factura.\n\n"
                        f"Trato: {_deal_link(deal['id'])}"),
                    "due": _iso(today),
                    "stage_kind": "facturacion",
                    "step": None,
                }
        elif kind == "touch":
            if stage in crm._CLOSED or stage in ("stalled",):
                continue
            step = _next_step(conn, deal["id"], horizon)
            if step is None:
                continue
            if (step["touch_type"] or "").lower() in AUTOMATED_TOUCH_TYPES:
                continue          # handled by the automated branch in reconcile
            label = _TOUCH_LABEL.get((step["touch_type"] or "").lower(),
                                     _TOUCH_LABEL_DEFAULT)
            body = (f"{MARKER['touch']} Paso {step['step_number']} de la "
                    f"cadencia ({step['touch_type'] or 'touch'}).\n\n"
                    f"{(step['template_text'] or '').strip()}\n\n"
                    f"Trato: {_deal_link(deal['id'])}")
            return {
                "kind": kind,
                "title": f"{label} {cliente}",
                "body": body,
                "due": step["scheduled_date"],
                # Derived, not guessed: the card's position in the cycle is
                # whatever its DEAL's stage implies (contacto / formalizacion).
                "stage_kind": stagekind.derive(
                    {"deal_id": deal["id"]}, deal_stage=stage,
                    project_status=deal.get("project_status")),
                "step": dict(step),
            }
    return None


def _precondition_holds(conn, deal: dict, task, today: datetime.date) -> bool:
    """Is the reason this OPEN card exists still true?

    Deliberately per-kind and independent of `_desired`: "the desired card
    changed" is not the same claim as "this card's reason is gone", and closing
    on the former would churn a deal's card every time a date rolled over.
    """
    kind = _kind_of(task["body"])
    stage = (deal.get("stage") or "").lower()
    if kind == "deliver":
        return stage == "won" and not deal.get("project_id")
    if kind == "invoice":
        return (stage == "won" and not deal.get("invoiced_at")
                and (deal.get("project_status") or "") == "delivered")
    if kind == "collect":
        return bool(deal.get("invoiced_at")) and not deal.get("paid_at")
    if kind == "touch":
        if stage in crm._CLOSED:
            return False
        try:
            row = conn.execute(
                "SELECT status FROM nurture_sequences WHERE task_id = ?",
                (task["id"],)).fetchone()
        except sqlite3.OperationalError:
            return True
        # No step claims it (the sequence was regenerated under the card), or the
        # step was marked sent/skipped elsewhere → the card has nothing to do.
        return row is not None and row["status"] == "pending"
    # An unmarked cadence task predates the markers, or was hand-edited. Leave
    # it alone: closing something we cannot classify is the destructive answer
    # to an ambiguous signal.
    return True


def _now_ts() -> int:
    import time as _time
    return int(_time.time())


def _mint(conn, deal: dict, want: dict, project_id: str) -> str:
    """Write the card. Caller's transaction, inside BEGIN IMMEDIATE.

    `assignee='ricardo'` and `autonomy='ask'`: every one of these four cards is
    a conversion-adjacent human act (spec red line 11 — agents propose, the operator
    taps), so none of them is dispatchable to an executor. `due_date` is the
    step's own `scheduled_date` — deliberately NOT a third clock computed here.
    """
    task_id = _gen_task_id()
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, priority, assignee, "
        " created_by, created_at, project_id, deal_id, stage_kind, due_date, "
        " autonomy) "
        "VALUES (?,?,?,'backlog',?,?,?,?,?,?,?,?,?)",
        (task_id, want["title"], want["body"], 1, "ricardo", CREATED_BY,
         _now_ts(), project_id, deal["id"], want.get("stage_kind"),
         want.get("due"), "ask"))

    from . import sprints
    sprints._log_event(conn, task_id, "cadence_minted",
                       {"deal_id": deal["id"], "kind": want["kind"],
                        "due_date": want.get("due")})
    crm._log(conn, deal["id"], "cadence_task",
             {"task_id": task_id, "kind": want["kind"], "title": want["title"]},
             source=CREATED_BY,
             channel=(want.get("step") or {}).get("touch_type"))

    step = want.get("step")
    if step:
        # The backref m07's UNIQUE index protects. Written in the same statement
        # batch as the INSERT so a mint that fails leaves no half-claimed step.
        conn.execute("UPDATE nurture_sequences SET task_id = ? WHERE id = ?",
                     (task_id, step["id"]))
    return task_id


def _log_automated(conn, deal: dict, horizon: str, today: datetime.date) -> list:
    """Automated steps: mark sent, event them, mint NOTHING.

    A card asking the operator to send what the system already sent is worse than no
    card — it teaches him the lane is noise. So an automated step closes itself
    and leaves the audit row that proves the touch happened.
    """
    out = []
    while True:
        step = _next_step(conn, deal["id"], horizon)
        if step is None:
            break
        if (step["touch_type"] or "").lower() not in AUTOMATED_TOUCH_TYPES:
            break
        conn.execute(
            "UPDATE nurture_sequences SET status = 'sent', sent_at = ? WHERE id = ?",
            (_iso(today), step["id"]))
        crm._log(conn, deal["id"], "touch",
                 {"note": f"Paso automático {step['step_number']}",
                  "nurture_id": step["id"], "automated": True},
                 source=CREATED_BY, channel=step["touch_type"])
        out.append(step["id"])
    return out


# ------------------------------------------------------------------ the driver

def reconcile(date: Optional[str] = None) -> dict:
    """One deterministic pass: close what is stale, mint the one next card.

    `BEGIN IMMEDIATE` (ruling 7): the write lock is taken UP FRONT, so two hosts
    reconciling the same morning serialize instead of one of them reading a
    pre-mint snapshot and duplicating every card. The manual-task check and the
    one-open-per-deal check both happen inside that lock, which is what makes
    them true rather than advisory.

    Idempotent by construction: a second run in the same minute closes nothing
    new (preconditions unchanged) and mints nothing new (every deal's slot is
    occupied). Returns a summary the endpoint and the contract both read.
    """
    today = datetime.date.fromisoformat(date) if date else _today()
    horizon = _iso(today + datetime.timedelta(days=LOOKAHEAD_DAYS))

    conn = db.get_conn()
    conn.isolation_level = None          # WE issue BEGIN IMMEDIATE
    conn.execute("PRAGMA busy_timeout = 15000")
    minted, closed, blocked, sticky, automated = [], [], [], [], []
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            project_id = sales_project_id(conn)
            if not project_id:
                conn.execute("ROLLBACK")
                return {"status": "error", "code": "sales_project_missing",
                        "error": "proj_ventas is missing — m09_sales_project "
                                 "has not run"}
            tcols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "deal_id" not in tcols:
                conn.execute("ROLLBACK")
                return {"status": "error", "code": "spine_missing",
                        "error": "tasks.deal_id is missing — m06_task_deal has "
                                 "not run"}

            for deal in _deals(conn):
                open_task = _open_cadence_task(conn, deal["id"])
                if open_task is not None:
                    if _precondition_holds(conn, deal, open_task, today):
                        continue                    # the slot is legitimately busy
                    close_task(conn, open_task["id"],
                               reason=f"precondition_gone:{_kind_of(open_task['body'])}")
                    closed.append(open_task["id"])

                if (deal.get("stage") or "").lower() not in crm._CLOSED:
                    _settle_refused_steps(conn, deal["id"])
                    automated += _log_automated(conn, deal, horizon, today)

                want = _desired(conn, deal, today, horizon)
                if want is None:
                    recompute(conn, deal["id"])
                    continue
                if _has_open_manual_task(conn, deal["id"]):
                    blocked.append(deal["id"])
                    continue
                if _dismissed(conn, deal["id"], want["kind"]):
                    sticky.append(deal["id"])
                    continue
                task_id = _mint(conn, deal, want, project_id)
                minted.append({"task_id": task_id, "deal_id": deal["id"],
                               "kind": want["kind"], "title": want["title"]})
                recompute(conn, deal["id"])
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # pragma: no cover - defensive
                pass
            raise
    finally:
        conn.close()

    return {"status": "ok", "date": _iso(today), "horizon": horizon,
            "minted": minted, "closed": closed, "blocked_by_manual": blocked,
            "sticky_skipped": sticky, "automated_steps": automated,
            "counts": {"minted": len(minted), "closed": len(closed),
                       "blocked": len(blocked), "sticky": len(sticky),
                       "automated": len(automated)}}
