"""m06 — the spine's last hop: a task can finally say WHICH DEAL it exists for.

Journey fase 1, step 4. Until now the only lineage a task carried was
`project_id` (delivery) — so "why am I doing this" could be answered with a
product name and never with a client. `deal_id` is the commercial half of that
lineage, and `stage_kind` (directiva ADICIÓN 9) is the *position in the cycle*
the pair implies: contacto → formalización → ejecución → entrega → facturación
→ cobranza.

--------------------------------------------------------------------------
Two columns, and why only ONE of them is ever written by a human
--------------------------------------------------------------------------

`deal_id` is a **fact**: someone (the operator, or the step-5 materializer)
asserts that this task exists because of that deal. Three writers, all named in
ruling 5 — `api_create_task`'s optional `deal_id`, the named
`PATCH /api/tasks/{id}/deal`, and the cadence materializer. Nothing else.

`stage_kind` is a **derivation** (Ley 1: never a form to fill in). Almost every
task's stage is implied by the deal's stage and the project's status, so
`dashboard/stagekind.py` computes it at READ time and this column stays NULL —
which is exactly why the migration does **no backfill**. It exists for the two
cases a rule cannot reach:

  * the materializer STAMPS `facturacion` / `cobranza`, because those two are
    *minted-only*: nothing in a deal's stage or a project's status distinguishes
    "invoice this" from "chase this payment". `derive()` refuses to guess them,
    so the only way they are ever true is that a writer said so.
  * the operator CORRECTS a derived value from the chip.

A NULL therefore means "ask the rule", not "unknown" — the reason a backfill
would be actively harmful: it would freeze today's derivation into the row and
the same task would stop moving through the cycle when its deal did.

--------------------------------------------------------------------------
The CHECK, and where it is (and is not) enforced
--------------------------------------------------------------------------

`ALTER TABLE … ADD COLUMN … CHECK (…)` is legal in SQLite and IS enforced for
every subsequent INSERT/UPDATE (verified against this schema, 2026-08-01) —
existing rows are not re-validated, which costs nothing here because every
existing row gets NULL. The vocabulary is a literal, deliberately not imported
from `dashboard/stagekind.py`: a migration is a historical record of what it
wrote and must keep meaning the same thing after the runtime vocabulary is
edited. `stagekind.STAGE_KINDS` mirrors it and a contract asserts the two agree.

--------------------------------------------------------------------------
The partial UNIQUE index — ruling 7's floor, in the storage engine
--------------------------------------------------------------------------

`idx_tasks_deal_cadence_open` is UNIQUE over `(deal_id)` but only
`WHERE deal_id IS NOT NULL AND created_by = 'cadence' AND status NOT IN
('done','rejected','cancelled')`. It says one sentence: **a deal may have at
most one OPEN task that the cadence materializer minted.** That is the anti-nag
floor — the materializer runs on a schedule, from more than one entry point, and
without this a retry, a double reconcile or two hosts firing the same morning
would leave three "Contactar a WePort" cards on the board.

Each conjunct in the partial predicate earns its place, and all four behaviours
were measured before shipping:
  * `created_by = 'cadence'` — a HUMAN may absolutely open five tasks on one
    deal. The constraint governs the robot, never the operator.
  * `status NOT IN (…)` — a settled task must stop occupying the slot, or the
    deal could never be nagged again after its first task was closed.
  * `deal_id IS NOT NULL` — SQLite treats NULLs as distinct in a UNIQUE index,
    so an unpartialled index would *look* correct while letting every
    deal-less task through anyway; being explicit is free and says what is meant.

`idx_tasks_deal` (non-unique, partial) is the read path: `deal_drilldown`, the
board feed and the canvas all ask "the tasks of this deal", and without it that
is a full scan of `tasks` per drawer open.

--------------------------------------------------------------------------
Why the migration VERIFIES its own indexes
--------------------------------------------------------------------------

The m10 lesson, applied before it can bite: index names are **global** in
SQLite, not scoped to their table, and this DB shares its namespace with
hermes' own tables. `CREATE INDEX IF NOT EXISTS` against a name already taken by
an index on ANOTHER table is silently skipped — the migration would commit,
ledger itself as applied, and leave the uniqueness floor simply absent. So after
the DDL the two names must resolve to indexes whose `tbl_name` is `tasks`, or
this raises inside the runner's transaction (fail closed).

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own, or the all-or-nothing guarantee
is lost.
"""

# The stage vocabulary as a literal (see the docstring). `dashboard/stagekind.py`
# mirrors it; tests/test_stagekind.py asserts the two never drift.
STAGE_KINDS = ("contacto", "formalizacion", "ejecucion", "entrega",
               "facturacion", "cobranza")

_ADD_DEAL_ID = "ALTER TABLE tasks ADD COLUMN deal_id TEXT"

_ADD_STAGE_KIND = (
    "ALTER TABLE tasks ADD COLUMN stage_kind TEXT "
    "CHECK (stage_kind IN ('contacto','formalizacion','ejecucion','entrega',"
    "'facturacion','cobranza'))"
)

_IDX_DEAL = """
CREATE INDEX IF NOT EXISTS idx_tasks_deal
ON tasks (deal_id)
WHERE deal_id IS NOT NULL
"""

# Ruling 7: at most ONE open cadence-minted task per deal.
_IDX_CADENCE_OPEN = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_deal_cadence_open
ON tasks (deal_id)
WHERE deal_id IS NOT NULL
  AND created_by = 'cadence'
  AND status NOT IN ('done', 'rejected', 'cancelled')
"""

INDEXES = ("idx_tasks_deal", "idx_tasks_deal_cadence_open")


def m06_task_deal(conn) -> dict:
    """Add `tasks.deal_id` + `tasks.stage_kind` and the two indexes. Idempotent.

    Registered as `m06_task_deal` in `runner.py`, after m05 (it needs the
    delivered-stage retirement to have landed: `stage_kind` derives `ejecucion`
    vs `entrega` from `projects.status`, which only became the single truth of
    delivery once `deals.stage = 'delivered'` stopped being writable).

    Purely additive and deliberately backfill-free — a NULL `stage_kind` means
    "derive me", so writing a value here would freeze today's answer into the
    row. Returns `{"columns": [...], "indexes": [...]}`; the runner ignores it
    and the contract reads it.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    added = []
    if "deal_id" not in cols:
        conn.execute(_ADD_DEAL_ID)
        added.append("deal_id")
    if "stage_kind" not in cols:
        conn.execute(_ADD_STAGE_KIND)
        added.append("stage_kind")

    conn.execute(_IDX_DEAL)
    conn.execute(_IDX_CADENCE_OPEN)

    ours = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'tasks'")}
    missing = [name for name in INDEXES if name not in ours]
    if missing:
        raise RuntimeError(
            f"m06 refuses to install a half-indexed tasks table: "
            f"{', '.join(missing)} did not land on `tasks`. Index names are "
            "GLOBAL in SQLite, so `CREATE INDEX IF NOT EXISTS` silently does "
            "nothing when the name is already taken by an index on another "
            "table. Without idx_tasks_deal_cadence_open the materializer can "
            "mint a second open task on the same deal — the nag this whole "
            "step exists to prevent. Rename the colliding index or this "
            "migration's, then re-run.")

    return {"columns": added, "indexes": list(INDEXES)}
