"""m05 — `deals.stage = 'delivered'` is retired, in the storage engine.

Journey fase 1, step 3 (ruling 2). `delivered` used to sit after `won` in the
stage flow, which made one column carry two independent truths: whether the
money landed, and whether the work shipped. Delivering a project therefore
DELETED the commercial fact — the deal left the `won` column the moment the
work shipped — and every won-rate, CAC denominator and forecast had to remember
to write `IN ('won','delivered')` or silently under-count.

The application half of the fix lands in the same commit: `crm.create_deal` /
`crm.update_deal` refuse the value (`stage_retired`), `mark_project_delivered`
no longer touches `deals.stage` at all, and `pipeline()`'s delivered column is
DERIVED (`stage = 'won'` AND the deal's project is delivered). This migration is
the floor under all of it — spec regla 7: a rule the storage engine does not
enforce is a rule that survives exactly until the next writer.

--------------------------------------------------------------------------
Two things, and deliberately only two
--------------------------------------------------------------------------

1. **An assertion, not a data rewrite.** Measured read-only on
   `~/.hermes/kanban.db` (2026-08-01): `SELECT COUNT(*) FROM deals WHERE
   stage = 'delivered'` → **0**, against 4 won. There is nothing to migrate, so
   this migration writes no deal row — and if that count is ever non-zero it
   RAISES rather than rewriting silently. Two reasons the abort is the correct
   branch: a `delivered` row would mean an unknown writer exists (the audit
   question has to be answered before the guard is installed on top of it), and
   flipping such a row to `won` would be a stage change with no event and no
   human — exactly the kind of quiet history edit the retirement exists to stop.
   The runner applies migrations inside one transaction, so raising leaves
   neither the trigger nor the ledger row.

2. **The trigger pair.** `BEFORE INSERT` and `BEFORE UPDATE OF stage`, both
   `WHEN NEW.stage = 'delivered'` → `RAISE(ABORT)`. `UPDATE OF stage` (rather
   than a bare `BEFORE UPDATE`) keeps every other column write on a legacy
   `delivered` row legal, which matters if one ever arrives from a restored
   backup: the guard bans the VALUE being written, not the row existing.

   SQLite has no multi-event trigger, so "the `trg_deal_stage_guard`" of the
   plan is physically two objects sharing that prefix. Both are `IF NOT EXISTS`
   so a re-run is a no-op, and `RAISE(ABORT)` (not FAIL/ROLLBACK) rolls back the
   whole statement while leaving the caller's transaction to decide — which is
   what turns a stray write into a `sqlite3.IntegrityError` the API layer can
   report instead of a corrupted board.

What this does NOT do: touch `crm.STAGES`. The value stays in the read
vocabulary (ruling 2 — legacy readers keep recognising it, vacuously, at 0
rows); what is retired is the ability to WRITE it, which is the only half that
can be enforced.

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own, or the all-or-nothing guarantee
is lost. (Which is also why the DDL below goes through `conn.execute` one
statement at a time: `conn.executescript` COMMITs any pending transaction first,
so it would break the runner's atomicity.)
"""

# The retired value, as a literal. Deliberately not imported from `crm.STAGES`:
# a migration is a historical record of what it wrote, and it must keep meaning
# the same thing after the runtime vocabulary is edited.
RETIRED_STAGE = "delivered"

# Deliberately quote-free: this string is interpolated INTO a single-quoted SQL
# literal, so an apostrophe here would terminate it early — which is exactly how
# the first version of this trigger failed to compile (`near "delivered": syntax
# error`). Doubling the quotes would work too and reads worse in the message the
# operator actually sees.
_ABORT_MESSAGE = (
    "deals.stage delivered is retired — a won deal stays won; "
    "delivery is projects.status delivered"
)

_GUARD_INSERT = f"""
CREATE TRIGGER IF NOT EXISTS trg_deal_stage_guard_insert
BEFORE INSERT ON deals
WHEN NEW.stage = '{RETIRED_STAGE}'
BEGIN
    SELECT RAISE(ABORT, '{_ABORT_MESSAGE}');
END
"""

_GUARD_UPDATE = f"""
CREATE TRIGGER IF NOT EXISTS trg_deal_stage_guard_update
BEFORE UPDATE OF stage ON deals
WHEN NEW.stage = '{RETIRED_STAGE}'
BEGIN
    SELECT RAISE(ABORT, '{_ABORT_MESSAGE}');
END
"""

TRIGGERS = ("trg_deal_stage_guard_insert", "trg_deal_stage_guard_update")


def m05_retire_delivered_stage(conn) -> dict:
    """Assert the table is already clean, then make it stay that way.

    Registered as `m05_retire_delivered_stage` in `runner.py`, after m04.
    Returns `{"delivered_rows": 0, "triggers": [...]}` — a summary the runner
    ignores and the contract reads.

    Raises `RuntimeError` if any deal still carries the retired stage: fail
    closed, inside the runner's transaction, where a returned error dict would
    be ignored and committed.
    """
    rows = conn.execute(
        "SELECT id FROM deals WHERE stage = ?", (RETIRED_STAGE,)).fetchall()
    if rows:
        ids = ", ".join(str(r[0]) for r in rows[:10])
        raise RuntimeError(
            f"m05 refuses to install the stage guard: {len(rows)} deal(s) still "
            f"carry stage='{RETIRED_STAGE}' ({ids}). The live DB had 0 when this "
            "migration was written, so a non-zero count means a writer exists "
            "that nobody knows about — audit it and decide what those deals are "
            "(won? delivered into which project?) before the guard goes on top.")

    conn.execute(_GUARD_INSERT)
    conn.execute(_GUARD_UPDATE)
    return {"delivered_rows": 0, "triggers": list(TRIGGERS)}
