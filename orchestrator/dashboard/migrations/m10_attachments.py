"""m10 — `attachments`, the edge table the five-facet project hub stands on.

Journey F3.5 (directiva ADICIÓN 7). A project is not a task list: it is a hub
with five facets — **conversations** (Fireflies), **resources** (Drive),
**code** (GitHub), **plans** (the `~/dev/planning` repo) and **tasks**. Four of
those five are *pointers at things that live outside this DB*, and before this
migration there was nowhere to put a pointer: a finished deep plan died in the
agent's transcript, a shared Drive doc lived in a chat message, a repo was
knowable only if someone had filled `projects.repo_path`.

--------------------------------------------------------------------------
Why ONE generic table and not four
--------------------------------------------------------------------------

`attachments(node_kind, node_id, kind, …)` is deliberately polymorphic on both
axes. Four typed tables (`project_plans`, `deal_documents`, …) would mean four
schemas, four APIs and four skill contracts for what is one sentence — *this
node points at that artifact* — and the fifth facet (tasks) proves the table
must NOT own everything: tasks already exist, with their own lifecycle, and the
hub reads them where they live rather than shadowing them here. An attachment
row is a pointer, never a copy.

`node_id` therefore carries **no foreign key**: SQLite cannot declare an FK
whose target table depends on another column's value. Existence is validated in
`dashboard/attachments.py` before every write, against the table `node_kind`
names — the same shape as `threads.update_thread` validating `project_id`
against a live project, and for the same reason: a pointer at a row that does
not exist is a silent lie that reads as success.

--------------------------------------------------------------------------
The three indexes, and what each one refuses
--------------------------------------------------------------------------

1. `idx_attachments_node (node_kind, node_id)` — the read path. Every facet
   query is "everything hanging off this node"; without it that is a table scan
   per drawer open.

2. + 3. `idx_attachments_url` / `idx_attachments_path` — **UNIQUE, PARTIAL**
   (`WHERE url IS NOT NULL` / `WHERE path IS NOT NULL`), keyed on
   `(node_kind, node_id, kind, url|path)`. This is the anti-duplication floor,
   and it exists because of who writes here: four host skills, on four hosts,
   each firing "plan profundo terminado → attachment registrado". Re-running a
   skill, re-syncing the planning repo, or two hosts finishing the same plan
   must converge on ONE row, not append a third copy of the same file. The
   application upserts against exactly these keys; the index is what makes that
   true for a writer that never goes through `attachments.py`.

   They are partial because SQLite treats NULLs as distinct in a UNIQUE index:
   a plain `UNIQUE(node_kind, node_id, kind, url)` would happily accept ten
   path-only rows (url NULL ten times) and still claim uniqueness. Two partial
   indexes say what is actually meant — *a URL is unique per node+kind, and so
   is a path* — while a row that carries only one of them constrains only that
   one.

The table-level `CHECK (url IS NOT NULL OR path IS NOT NULL)` closes the last
hole: an attachment that points at nothing is not an attachment.

--------------------------------------------------------------------------
Why the migration VERIFIES its own indexes instead of trusting the DDL
--------------------------------------------------------------------------

Index names are **global** in SQLite, not scoped to their table — and this DB
already carries `idx_attachments_task` **on hermes' unrelated `task_attachments`
table** (measured on `~/.hermes/kanban.db`, 2026-08-01). So the
`idx_attachments_*` namespace is shared with a writer we do not control, and
`CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_url` against a name already
taken by an index on ANOTHER table does not fail — it is **silently skipped**
(verified). The migration would then commit, ledger itself as applied, and leave
the two anti-duplication constraints simply absent: every duplicate registration
would land, and nothing would say so.

Hence the post-check: after the DDL, the three names must resolve to indexes
whose `tbl_name` is `attachments`, or the migration RAISES inside the runner's
transaction (fail closed — no table, no ledger row, an operator who has to look).

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own, or the all-or-nothing guarantee
is lost. (Which is also why the DDL below goes through `conn.execute` one
statement at a time: `conn.executescript` COMMITs any pending transaction
first, so it would break the runner's atomicity.)
"""

# The two vocabularies, as literals. Deliberately NOT imported from
# `dashboard.attachments`: a migration is a historical record of what it wrote,
# and it must keep meaning the same thing after the runtime vocabulary is
# edited. `attachments.py` mirrors these and a contract asserts the two agree.
NODE_KINDS = ("account", "deal", "project", "task")
KINDS = ("conversation", "resource", "code", "plan")

_TABLE = """
CREATE TABLE IF NOT EXISTS attachments (
    id           TEXT PRIMARY KEY,
    node_kind    TEXT NOT NULL CHECK (node_kind IN ('account','deal','project','task')),
    node_id      TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('conversation','resource','code','plan')),
    url          TEXT,
    path         TEXT,
    title        TEXT NOT NULL,
    source_agent TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    CHECK (url IS NOT NULL OR path IS NOT NULL)
)
"""

_IDX_NODE = """
CREATE INDEX IF NOT EXISTS idx_attachments_node
ON attachments (node_kind, node_id)
"""

_IDX_URL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_url
ON attachments (node_kind, node_id, kind, url)
WHERE url IS NOT NULL
"""

_IDX_PATH = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_path
ON attachments (node_kind, node_id, kind, path)
WHERE path IS NOT NULL
"""

INDEXES = ("idx_attachments_node", "idx_attachments_url", "idx_attachments_path")


def m10_attachments(conn) -> dict:
    """Create the table and its three indexes. Idempotent.

    Registered as `m10_attachments` in `runner.py`, after m05. Returns
    `{"table": "attachments", "indexes": [...]}` — a summary the runner ignores
    and the contract reads.

    Purely additive: it creates one new table and touches no existing row, so
    unlike m05 there is nothing to assert about the state of the DB BEFORE the
    DDL. What it does assert is the state AFTER: index names are global in
    SQLite and this DB already shares the `idx_attachments_*` namespace with
    hermes' `task_attachments`, so `IF NOT EXISTS` can silently skip a UNIQUE
    index that was never created on our table. Raises `RuntimeError` in that
    case rather than shipping a table whose anti-duplication floor is missing.
    """
    conn.execute(_TABLE)
    conn.execute(_IDX_NODE)
    conn.execute(_IDX_URL)
    conn.execute(_IDX_PATH)

    ours = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'attachments'")}
    missing = [name for name in INDEXES if name not in ours]
    if missing:
        raise RuntimeError(
            f"m10 refuses to install a half-indexed attachments table: "
            f"{', '.join(missing)} did not land on `attachments`. Index names "
            "are GLOBAL in SQLite, so `CREATE INDEX IF NOT EXISTS` silently "
            "does nothing when the name is already taken by an index on another "
            "table (this DB already has idx_attachments_task on hermes' "
            "task_attachments). Without the UNIQUE indexes every repeated "
            "registration from the host skills would duplicate. Rename the "
            "colliding index or this migration's, then re-run.")
    return {"table": "attachments", "indexes": list(INDEXES)}
