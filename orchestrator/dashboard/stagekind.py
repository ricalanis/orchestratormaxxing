"""stagekind — where in the client cycle a task sits, DERIVED and never asked.

Directiva ADICIÓN 9: "En ese ciclo hay tareas: de CONTACTO, de FORMALIZACIÓN, de
EJECUCIÓN, de ENTREGA/delivery, de FACTURACIÓN, de COBRANZA y CERRADOS."

The whole design is one sentence: **a task's stage is a fact about its deal and
its project, not a field on a form** (Ley 1 — nothing the operator has to
remember to fill in). So this module is a pure function over three inputs the
callers already have in hand, and `tasks.stage_kind` stays NULL for almost every
row. It is a *pure* function on purpose — no DB handle, no clock, no imports
from `dashboard.*` — which is what makes it table-testable and safe to call from
the row-mapping hot path (`db._row_to_task`, `canvas._rows`, `context`).

--------------------------------------------------------------------------
The rule, in precedence order
--------------------------------------------------------------------------

1. **An explicit `tasks.stage_kind` always wins.** Two writers set it, and both
   are assertions the rule could not have made: the cadence materializer
   stamping a minted task, and the operator correcting the chip. A derivation
   that overrode a human correction would make the chip un-correctable.

2. **A deal answers next**, because commercial lineage outranks delivery
   lineage (the same precedence the contextChip renders):
       lead · engaged                → contacto        (still earning the right)
       qualified · demo · proposal   → formalizacion   (shaping the agreement)
       won + project delivering/delivered → entrega
       won + anything else           → ejecucion       (the work is the point)
       lost · stalled                → None            (see below)

3. **No deal, but a project** → the delivery cycle:
       delivering · delivered        → entrega
       planned · active              → ejecucion
       archived                      → None

4. **Neither** → None.

--------------------------------------------------------------------------
What it REFUSES to answer, and why that is the important half
--------------------------------------------------------------------------

* **`facturacion` and `cobranza` are minted-only.** Nothing in a deal's stage or
  a project's status distinguishes "invoice this client" from "chase that
  invoice" — both are `won` + `delivered`. A rule that guessed would put a
  billing chip on every delivered project's tasks and be wrong most of the time.
  They are true only when a writer said so (rule 1), which is exactly the shape
  ADICIÓN 8 gives them: tasks the materializer mints off `invoiced_at`/`paid_at`.
  `derive()` never returns either one from rules 2-4 — asserted by contract.

* **A lost or stalled deal yields None**, not a chip. Those are exits from the
  cycle, not positions in it; labelling a lost deal's leftovers "contacto" would
  invite work on a dead deal.

* **None is a legitimate answer.** The caller renders no chip. An honest absence
  beats a plausible guess (the same reason `refs.resolve` never guesses) — and
  because the chip is display-only this wave, a wrong chip is a lie with no
  compensating action attached to it.

`delivering` is handled but does not exist yet: `sprints.PROJECT_STATUSES` is
(planned, active, delivered, archived) — the status was deliberately unshipped
(step-1 decision log). Handling it here costs one tuple entry and means the rule
does not have to be re-derived on the day it lands.
"""
from typing import Any, Optional

# The vocabulary. MIRRORS `migrations/m06_task_deal.STAGE_KINDS` (and the CHECK
# constraint underneath it) — deliberately a second literal, not an import: the
# migration is a historical record of what it wrote and must keep meaning the
# same thing after this runtime vocabulary is edited. tests/test_stagekind.py
# asserts the two never drift.
STAGE_KINDS = ("contacto", "formalizacion", "ejecucion", "entrega",
               "facturacion", "cobranza")

# Display labels for the chip (Spanish, lowercase — the chip is small and muted).
STAGE_LABELS = {
    "contacto": "contacto",
    "formalizacion": "formalización",
    "ejecucion": "ejecución",
    "entrega": "entrega",
    "facturacion": "facturación",
    "cobranza": "cobranza",
}

# The two stages a rule may never conclude (see the docstring). Minted-only.
MINTED_ONLY = ("facturacion", "cobranza")

# Deal stage → stage kind, for every stage that is a POSITION in the cycle.
# `won` is absent on purpose: it is the one stage whose answer depends on the
# delivering project, so it is resolved below rather than in this table.
_BY_DEAL_STAGE = {
    "lead": "contacto",
    "engaged": "contacto",
    "qualified": "formalizacion",
    "demo": "formalizacion",
    "proposal": "formalizacion",
}

# Project status → stage kind, once the money question is settled (a won deal, or
# no deal at all). Missing keys (archived, and anything a future migration adds)
# deliberately yield None rather than a default.
_BY_PROJECT_STATUS = {
    "planned": "ejecucion",
    "active": "ejecucion",
    "delivering": "entrega",
    "delivered": "entrega",
}

# Deal stages that are EXITS from the cycle, not positions in it.
_DEAD_DEAL_STAGES = ("lost", "stalled")


def _field(task: Any, name: str):
    """Read `name` off a row that may be a dict, a sqlite3.Row or a dataclass.

    All three shapes reach this module (the API hands dicts, `canvas._rows`
    hands dicts built from Rows, `db._row_to_task` hands raw Rows, the drawer
    hands a Task). Normalising here keeps every caller from writing the same
    three-way access, and an absent field is simply None — a task from a
    pre-m06 DB must derive, not raise.
    """
    if task is None:
        return None
    try:
        if hasattr(task, "keys"):            # dict / sqlite3.Row
            return task[name] if name in task.keys() else None
    except Exception:                        # pragma: no cover - defensive
        pass
    if isinstance(task, dict):               # pragma: no cover - covered above
        return task.get(name)
    return getattr(task, name, None)


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s or None


def derive(task: Any = None, deal_stage: Optional[str] = None,
           project_status: Optional[str] = None) -> Optional[str]:
    """Where in the client cycle this task sits, or None when nothing implies it.

    `task` may be a dict, a sqlite3.Row, or any object with the attributes — it
    is read for two fields only: `stage_kind` (an explicit value, which wins)
    and `deal_id` (whether the commercial branch applies at all). `deal_stage`
    and `project_status` are the joined facts; pass what you have, omit what you
    do not.

    Pure: no I/O, no clock, no globals. Never returns a value outside
    `STAGE_KINDS`, and never returns one of `MINTED_ONLY` unless the task itself
    already carried it.
    """
    explicit = _clean(_field(task, "stage_kind"))
    if explicit in STAGE_KINDS:
        return explicit

    stage = _clean(deal_stage)
    status = _clean(project_status)
    has_deal = bool(_field(task, "deal_id")) or bool(stage)

    if has_deal and stage:
        if stage in _DEAD_DEAL_STAGES:
            return None
        mapped = _BY_DEAL_STAGE.get(stage)
        if mapped:
            return mapped
        if stage == "won":
            # The money is settled; the delivery leg now says where we are. A won
            # deal with NO delivering project yet is still execution waiting to
            # start — never `entrega`, which would claim work that has not begun.
            # But a project whose status we DO know and cannot place (archived)
            # yields None, exactly as it does on the deal-less branch: a known
            # status is a signal, an absent one is not.
            return _BY_PROJECT_STATUS.get(status) if status else "ejecucion"
        # An unknown stage (a vocabulary that grew without this table) is an
        # honest None, not a default — see the docstring.
        return None

    if has_deal and not stage:
        # A deal_id we could not resolve a stage for: the join was not made, or
        # the deal is gone. Refuse rather than assume a position in its cycle.
        return None

    if status:
        return _BY_PROJECT_STATUS.get(status)
    return None


def label(kind: Optional[str]) -> Optional[str]:
    """The display string for a chip, or None when there is no chip to draw."""
    return STAGE_LABELS.get(_clean(kind) or "")
