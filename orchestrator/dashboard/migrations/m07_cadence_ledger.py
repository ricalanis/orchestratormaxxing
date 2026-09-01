"""m07 — the cadence LEDGER: a nurture step learns when it was sent, and which
task carried it.

Journey fase 1, step 5. Two columns on `nurture_sequences`, and each one closes
a hole that made the whole cadence layer unmeasurable:

--------------------------------------------------------------------------
`sent_at` — the column `get_cadence_status` was already reading
--------------------------------------------------------------------------

`crm.get_cadence_status` (crm.py:906) computes compliance as *"a step marked
sent within ±2 days of its scheduled_date"* — and it reads `s.get("sent_at")`.
The column did not exist. `dict(row)` on a table without it simply has no key,
`.get()` answers None, and every elapsed step failed the `and s.get("sent_at")`
guard: **compliance was arithmetically incapable of being anything but 0.0**,
forever, on every deal. Not a bug that showed up as an error — a number that
was always the same wrong number.

So this is not a new feature: it is the storage the existing reader assumed.
TEXT, holding an ISO `YYYY-MM-DD` — the same shape as `scheduled_date` beside
it, because the compliance arithmetic compares the two directly
(`date.fromisoformat(str(s["sent_at"])[:10])`) and a mixed epoch/ISO pair would
compare as garbage rather than fail loudly.

--------------------------------------------------------------------------
`task_id` — the backref that makes the loop CLOSABLE
--------------------------------------------------------------------------

Step 5's materializer mints a task from a pending step. Closing the loop means
the reverse hop: a task moving to `done` must find *its* step and mark it sent.
Without a backref that hop is a guess (title matching, or "the earliest pending
step of this deal" — which silently marks the wrong step whenever the operator
finishes them out of order).

It is **UNIQUE, partially** (`WHERE task_id IS NOT NULL`): one task carries at
most one step. NULLs stay distinct — which is the normal case, since a sequence
of five steps has at most one minted at a time — so the partial predicate is
not decoration, it is what makes the index mean *"a minted task is claimed by
exactly one step"* instead of *"a deal may have only one unminted step"*.

Ruling 7 names this index beside `idx_tasks_deal_cadence_open` (m06): the pair
is the anti-nag floor. m06's says a deal has at most one open cadence task;
this one says a task belongs to at most one step. Together the materializer
cannot double-mint even if it is run twice concurrently — the storage engine
refuses, rather than the application remembering to check.

--------------------------------------------------------------------------
Why the migration VERIFIES its own index (the m10/m06 lesson)
--------------------------------------------------------------------------

Index names are **global** in SQLite, not scoped to their table, and this DB
shares its namespace with hermes' own tables. `CREATE UNIQUE INDEX IF NOT
EXISTS` against a name already taken by an index on ANOTHER table is silently
skipped — the migration would commit, ledger itself as applied, and leave the
uniqueness floor simply absent. So after the DDL the name must resolve to an
index whose `tbl_name` is `nurture_sequences`, or this raises inside the
runner's transaction (fail closed).

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own, or the all-or-nothing guarantee
is lost.
"""

_ADD_SENT_AT = "ALTER TABLE nurture_sequences ADD COLUMN sent_at TEXT"
_ADD_TASK_ID = "ALTER TABLE nurture_sequences ADD COLUMN task_id TEXT"

# Ruling 7: a minted task is claimed by exactly ONE step.
_IDX_TASK = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_nurture_task
ON nurture_sequences (task_id)
WHERE task_id IS NOT NULL
"""

INDEXES = ("idx_nurture_task",)


def m07_cadence_ledger(conn) -> dict:
    """Add `nurture_sequences.sent_at` + `task_id` and the UNIQUE backref index.

    Idempotent. Registered as `m07_cadence_ledger` in `runner.py`, after m06 —
    ordering hygiene rather than a hard dependency (the two touch different
    tables), but the pair of UNIQUE indexes is one rule split across two
    migrations, so they read in order.

    Creates `nurture_sequences` first if it is absent: the table is installed by
    `db.ensure_nurture_schema()` in the runner's LEGACY phase, which always runs
    before the versioned one — but a caller that drives `run_versioned()`
    directly (the migration contracts do) would otherwise hit
    `no such table`. Calling the same ensure would open a SECOND connection
    outside this transaction, so the DDL is inlined here instead.

    Returns `{"columns": [...], "indexes": [...]}`; the runner ignores it and
    the contract reads it.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nurture_sequences ("
        " id TEXT PRIMARY KEY,"
        " deal_id TEXT NOT NULL,"
        " step_number INTEGER NOT NULL,"
        " touch_type TEXT,"
        " template_text TEXT,"
        " scheduled_date TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at INTEGER)")

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nurture_sequences)")}
    added = []
    if "sent_at" not in cols:
        conn.execute(_ADD_SENT_AT)
        added.append("sent_at")
    if "task_id" not in cols:
        conn.execute(_ADD_TASK_ID)
        added.append("task_id")

    conn.execute(_IDX_TASK)

    ours = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'nurture_sequences'")}
    missing = [name for name in INDEXES if name not in ours]
    if missing:
        raise RuntimeError(
            f"m07 refuses to install a half-indexed nurture_sequences table: "
            f"{', '.join(missing)} did not land on `nurture_sequences`. Index "
            "names are GLOBAL in SQLite, so `CREATE UNIQUE INDEX IF NOT EXISTS` "
            "silently does nothing when the name is already taken by an index "
            "on another table. Without idx_nurture_task two steps could claim "
            "the same minted task and the loop closure would mark whichever one "
            "it read first. Rename the colliding index or this migration's, "
            "then re-run.")

    return {"columns": added, "indexes": list(INDEXES)}
