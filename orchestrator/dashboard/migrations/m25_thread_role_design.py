"""m25 — `design` becomes a first-class thread role, and the Designer thread registers.

Ricardo created Telegram thread 15957 on 2026-08-04 ("This is going to be mi
designer") and chose, on 2026-08-05, to give it a real role rather than file it
under `code`. That requires rebuilding the `role` CHECK, which is why this
migration is longer than the one-line INSERT it looks like.

WHY THIS REBUILDS A CHECK WHEN m12 REFUSED TO
    m12_thread_stations recorded "journey ruling 9: use the real vocabulary,
    never rebuild a CHECK under a live gateway" and added `station` as an
    ADDITIVE column instead. That ruling still stands for the case it was made
    about — inventing a *parallel* vocabulary to dodge the enum. Here the
    opposite is true: `design` IS the real vocabulary. A Designer thread filed
    as `code` would make every role-sliced surface lie about what that thread
    is, which is the exact failure ruling 9 exists to prevent.
    The gateway is "live" in the sense that it may write `last_activity_at`
    concurrently. The runner's guarantees cover that: the whole migration runs
    inside ONE transaction on the runner's own connection, and it takes a
    verified `bin/backup-kanban` snapshot first, aborting fail-closed if the
    backup fails. A rebuild that loses a `last_activity_at` write racing the
    transaction is a lost timestamp, not a lost row.

SQLITE MECHANICS (the part that is easy to get subtly wrong)
    SQLite cannot ALTER a CHECK. The supported sequence is create-new → copy →
    drop-old → rename, which we do with the column list stated EXPLICITLY rather
    than `SELECT *`: a positional copy silently transposes values the day
    someone adds a column, and this table already gained `station` once.
    `legacy_alter_table` is left alone — the runner owns pragma state, and the
    rename here is the last statement, so no view/trigger rewrite is in flight.
    The threads table has no foreign keys pointing AT it (verified 2026-08-05:
    `PRAGMA foreign_key_list` on every table returns no reference to threads),
    so the drop cannot orphan a child row.

THE ROW ITSELF
    thread 15957 is a DM-in-topic-mode: chat_id is the operator's own user id
    (config: $HERMES_DESIGNER_CHAT_ID; unset skips the seed), so `station`
    stays NULL — the Designer has no funnel obligation. Seeded idempotently,
    and it never overwrites a name the operator may have edited in the panel.
"""

import os

ROLES = ("code", "growth", "ops", "health", "personal", "design")

# The full column list of `threads`, in schema order. Stated explicitly so the
# copy below is positional-safe. m02_spine created the first seven; m12 added
# `station`.
_COLUMNS = (
    "thread_id", "chat_id", "name", "project_id", "role", "status",
    "last_activity_at", "station",
)

_NEW_TABLE = """
CREATE TABLE threads_m25 (
  thread_id        INTEGER PRIMARY KEY,
  chat_id          TEXT NOT NULL,
  name             TEXT NOT NULL,
  project_id       TEXT,
  role             TEXT NOT NULL
    CHECK (role IN ('code','growth','ops','health','personal','design')),
  status           TEXT NOT NULL DEFAULT 'active',
  last_activity_at INTEGER,
  station          TEXT
    CHECK(station IS NULL OR station IN
          ('clientes','oportunidades','proyectos','delivery','ritual'))
)
"""

# The Designer. chat_id is the operator's DM (config, never a literal in code).
_DESIGNER = (15957, os.environ.get("HERMES_DESIGNER_CHAT_ID", ""), "🎨 Designer", "design")


def m25_thread_role_design(conn) -> None:
    # --- 1. rebuild the CHECK, only if it does not already allow 'design' ----
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone()
    if sql_row is None:
        return  # no registry yet; m02_spine owns creation
    current_sql = sql_row[0] or ""

    if "'design'" not in current_sql:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        missing = [c for c in _COLUMNS if c not in cols]
        if missing:
            # A shape we do not recognise — refuse rather than copy blind.
            raise RuntimeError(
                f"m25: threads is missing expected column(s) {missing}; "
                "refusing to rebuild a table whose shape I cannot state."
            )
        collist = ", ".join(_COLUMNS)
        before = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

        conn.execute("DROP TABLE IF EXISTS threads_m25")
        conn.execute(_NEW_TABLE)
        conn.execute(
            f"INSERT INTO threads_m25 ({collist}) SELECT {collist} FROM threads")

        after = conn.execute("SELECT COUNT(*) FROM threads_m25").fetchone()[0]
        if after != before:
            # Inside the runner's transaction, so raising rolls the whole thing
            # back — the old table is still there and intact.
            raise RuntimeError(
                f"m25: copied {after} rows but threads had {before}; aborting "
                "before the DROP so nothing is lost.")

        conn.execute("DROP TABLE threads")
        conn.execute("ALTER TABLE threads_m25 RENAME TO threads")

    # --- 2. register the Designer thread ------------------------------------
    thread_id, chat_id, name, role = _DESIGNER
    if not chat_id:
        return  # no configured operator DM — a standalone install seeds nothing
    exists = conn.execute(
        "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if exists:
        # Idempotent re-run: only correct the role. Never clobber a name or a
        # status Ricardo set from the panel.
        conn.execute(
            "UPDATE threads SET role = ? WHERE thread_id = ? AND role <> ?",
            (role, thread_id, role))
    else:
        conn.execute(
            "INSERT INTO threads (thread_id, chat_id, name, role, status, station) "
            "VALUES (?, ?, ?, ?, 'active', NULL)",
            (thread_id, chat_id, name, role))
