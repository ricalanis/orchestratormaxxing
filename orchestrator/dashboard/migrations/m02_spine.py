"""m02 — the spine: the columns and tables phase 1's verbs stand on.

Everything here is ADDITIVE. No column is dropped, no value that carries human
intent is rewritten, and `tasks.epic_id` / `tasks.initiative_id` are FROZEN —
they are hermes-owned and phase 2 folds them deliberately, not as a side effect
of this migration.

What it lays down, and why each piece exists:

1. **`deals.project_id`** — the missing money→delivery join. A won deal with no
   delivering project is revenue that arrived and then fell out of the system;
   until this column exists the Close brief can only honestly report an empty
   list (`brief.compose_needs_you` guards on it with `PRAGMA table_info`).
2. **`projects.*`** — the delivery half of the same join (`account_id`), plus the
   strategy vocabulary `initiatives` already carries (tier / quarter / why /
   success_check / health / confidence) so a project can be read the same way as
   the initiative above it, plus `repo_path` (step 7's codex dispatch resolves a
   workspace through it) and `delivered_at`.
3. **`tasks.executor_kind` / `executor_target` / `thread_id`** — honest dispatch.
   Today `assignee` is overloaded into "who owns this" AND "what will run it";
   splitting them means `assignee` stays a display/ownership field while the
   dispatcher reads a field that actually says how to execute.
4. **`threads`** — the Telegram DM topic registry. The routing table that turns
   "notify Ricardo" into "notify Ricardo *in the right topic*", and the reason a
   dispatch can name its destination instead of guessing.
5. **`task_dispatches`** — the dispatch OUTBOX. `task_runs` is hermes-owned and
   the dashboard never writes it (spec §2); the saga in step 7 records its own
   intent here first, so a spawn that dies still leaves a row saying what was
   attempted instead of a silent flag write.
6. **Executor backfill** — derives 3 from the existing `assignee` values, once.
7. **Autonomy normalisation** — `NULL` and the dead `dispatch` value both mean
   "ask me"; `auto` is the only real opt-out and is left alone.
8. **Hygiene** — three small data-shape repairs (see `_hygiene`).

**Idempotency.** The runner records the name row once, so in the normal path this
body runs exactly once. It is still written to survive a rerun after a partial
failure (a crash between DDL and the ledger COMMIT rolls back, but a rerun must
also be safe if the ledger is ever rebuilt by hand): every ALTER is guarded by
`PRAGMA table_info`, every CREATE is `IF NOT EXISTS`, the seed is
`INSERT OR IGNORE`, and every backfill UPDATE is predicated on the value it is
about to write being absent.

**Static literal, never a live read.** The thread seed below was enumerated ONCE,
read-only, from `~/.hermes/state.db:telegram_dm_topic_bindings` at authoring time
(2026-07-28, 20 bindings) and frozen here by hand. The migration must never open
state.db: a migration whose result depends on another database's contents at
apply time is not reproducible, and state.db is the gateway's, not ours.

Receives the runner's OWN connection inside its transaction — it must not commit,
close, or open a connection of its own, or the all-or-nothing guarantee is lost.
"""
import os
import time

# --- the thread registry seed --------------------------------------------
# The operator's Telegram DM is CONFIG ($HERMES_DEFAULT_CHAT_ID); unset means
# a standalone install and the registry seed is skipped entirely.
CHAT_ID = os.environ.get("HERMES_DEFAULT_CHAT_ID", "")

# The named topics: the eight live ones the operator uses plus `Hoy`, the destination
# the 3x-daily brief posts into. Names are provisional (editable in the UI);
# `role` is not — it is what routing reads. `project_id` is the binding, and
# there is deliberately exactly ONE today: a binding is a claim that work in a
# project belongs in a topic, and eight guessed claims would be worse than one
# true one.
#     (thread_id, name, role, project_id)
NAMED_THREADS = [
    (15185, "Hoy",        "ops",    None),
    (7363,  "🧑‍💻 Code",    "code",   "proj_orchestrator"),
    (7348,  "Capacity",   "ops",    None),
    (10783, "Health",     "health", None),
    (7371,  "Memoria",    "ops",    None),
    (8037,  "Review",     "ops",    None),
    (9193,  "Growth SDR", "growth", None),
    (9278,  "Servicio",   "growth", None),
    (7350,  "Mentoring",  "growth", None),
]

# Every OTHER binding in state.db: ad-hoc topics from a single conversation that
# were never given a job. They are registered (so a stray inbound message resolves
# to a known thread instead of nothing) but `archived`, so no picker or router
# ever offers them. Enumerated read-only at authoring time; frozen literal.
ARCHIVED_TOPIC_IDS = [
    7490, 7597, 7954, 8215, 8930, 11441,
    11875, 12470, 12862, 13614, 14021, 14025,
]

# --- the executor backfill ------------------------------------------------
# assignee → (executor_kind, executor_target). `assignee` itself is UNTOUCHED:
# after this migration it is a display/ownership field and this pair is what the
# dispatcher reads. Anything not listed (including a NULL assignee) falls through
# to ('human', NULL) — "a person owns this, we don't know how it runs" is the
# only safe default, because the alternative is inventing an executor that would
# then be dispatched to.
EXECUTOR_BY_ASSIGNEE = [
    ("ricardo",     "human",  "ricardo"),
    ("default",     "hermes", "default"),
    ("claude-code", "claude", None),
    ("hermes",      "hermes", "default"),
]
EXECUTOR_FALLBACK = ("human", None)

# --- hygiene targets ------------------------------------------------------
# `strategy.STATUSES` is ("planned", "active", "shipped", "dropped"): `in_progress`
# is not in the vocabulary, so a row carrying it is invisible to every status
# filter. Fixed as a SET-based update rather than by id — see `_hygiene`.
INITIATIVE_BAD_STATUS = "in_progress"
INITIATIVE_GOOD_STATUS = "active"
# A cycle that ended long ago but never went through finish_sprint.
STALE_SPRINT_ID = "spr_083acc33"
# Throwaway fixtures from a verb audit that were never cleaned up.
PATCH_TEST_PREFIX = "patch-test-%"


# --- helpers --------------------------------------------------------------

def _columns(conn, table: str) -> set:
    """`PRAGMA table_info` as a column-name set — EMPTY when the table does not
    exist. Same single dialect of existence check `dashboard/brief.py` uses, so
    the guard that hides a column and the guard that adds it read alike."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table: str, column: str, decl: str) -> bool:
    """`ALTER TABLE ... ADD COLUMN` with an existence guard.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and a duplicate ADD raises — which
    inside the runner's transaction would roll the whole migration back. Returns
    True if the column was added."""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# --- the migration --------------------------------------------------------

def m02_spine(conn) -> None:
    """Apply the spine. Registered as `m02_spine` in `migrations/runner.py`."""
    now = int(time.time())

    # 1. deals → the delivering project.
    _add_column(conn, "deals", "project_id", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deals_project ON deals(project_id)")

    # 2. projects → the delivery record (account link, lifecycle, strategy
    #    vocabulary, and the repo a dispatch can run in).
    for column, decl in [
        ("status",        "TEXT"),
        ("account_id",    "TEXT"),
        ("delivered_at",  "TEXT"),
        ("quarter",       "TEXT"),
        ("tier",          "TEXT"),
        ("why",           "TEXT"),
        ("success_check", "TEXT"),
        ("health",        "TEXT"),
        ("confidence",    "TEXT"),
        ("repo_path",     "TEXT"),
    ]:
        _add_column(conn, "projects", column, decl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_account ON projects(account_id)")

    # 3. tasks → how this runs, and where it reports. (epic_id / initiative_id
    #    are hermes-owned and deliberately NOT touched here.)
    for column, decl in [
        ("executor_kind",   "TEXT"),
        ("executor_target", "TEXT"),
        ("thread_id",       "INTEGER"),
    ]:
        _add_column(conn, "tasks", column, decl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_project_status "
        "ON tasks(project_id, status)")

    # 4. threads → the Telegram topic registry.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS threads ("
        "  thread_id        INTEGER PRIMARY KEY,"
        "  chat_id          TEXT NOT NULL,"
        "  name             TEXT NOT NULL,"
        "  project_id       TEXT,"
        "  role             TEXT NOT NULL"
        "     CHECK (role IN ('code','growth','ops','health','personal')),"
        "  status           TEXT NOT NULL DEFAULT 'active',"
        "  last_activity_at INTEGER"
        ")")
    _seed_threads(conn)

    # 5. task_dispatches → the dispatch outbox (never task_runs).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_dispatches ("
        "  id            TEXT PRIMARY KEY,"
        "  task_id       TEXT NOT NULL,"
        "  executor_kind TEXT NOT NULL,"
        "  executor_target TEXT,"
        "  state         TEXT NOT NULL"
        "     CHECK (state IN ('requested','delivered','spawn_failed','send_failed')),"
        "  thread_id     INTEGER,"
        "  exit_code     INTEGER,"
        "  stdout_tail   TEXT,"
        "  note          TEXT,"
        "  created_at    INTEGER NOT NULL,"
        "  updated_at    INTEGER NOT NULL"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_dispatches_task "
        "ON task_dispatches(task_id)")

    # 6. + 7. derive the new fields from what the DB already knows.
    _backfill_executors(conn)
    _normalise_autonomy(conn)

    # 8. data-shape repairs.
    _hygiene(conn, now)


def _seed_threads(conn) -> None:
    """The frozen registry seed. `INSERT OR IGNORE`, so a rerun never clobbers a
    name the operator has since edited or a binding they have since moved."""
    if not CHAT_ID:
        return  # no configured chat — a standalone install seeds no topics
    for thread_id, name, role, project_id in NAMED_THREADS:
        conn.execute(
            "INSERT OR IGNORE INTO threads "
            "(thread_id, chat_id, name, project_id, role, status) "
            "VALUES (?,?,?,?,?,'active')",
            (thread_id, CHAT_ID, name, project_id, role))
    named = {t[0] for t in NAMED_THREADS}
    for thread_id in ARCHIVED_TOPIC_IDS:
        if thread_id in named:  # pragma: no cover - defensive, lists are disjoint
            continue
        conn.execute(
            "INSERT OR IGNORE INTO threads "
            "(thread_id, chat_id, name, project_id, role, status) "
            "VALUES (?,?,?,NULL,'personal','archived')",
            (thread_id, CHAT_ID, f"topic {thread_id}"))


def _backfill_executors(conn) -> None:
    """assignee → (executor_kind, executor_target), once.

    Every statement is predicated on `executor_kind IS NULL`, so this only ever
    fills the blank it just created: a rerun cannot overwrite an executor that a
    dispatch has since set."""
    for assignee, kind, target in EXECUTOR_BY_ASSIGNEE:
        conn.execute(
            "UPDATE tasks SET executor_kind = ?, executor_target = ? "
            "WHERE executor_kind IS NULL AND assignee = ?",
            (kind, target, assignee))
    kind, target = EXECUTOR_FALLBACK
    conn.execute(
        "UPDATE tasks SET executor_kind = ?, executor_target = ? "
        "WHERE executor_kind IS NULL",
        (kind, target))


def _normalise_autonomy(conn) -> None:
    """NULL and `dispatch` both mean "ask me first"; `auto` is the real opt-out.

    `dispatch` was a third value that no code branched on — it read as an
    autonomy level while behaving exactly like the unset one. Collapsing both
    into `ask` makes the column a two-value decision (`ask` | `auto`) that the
    dispatcher can actually honour. Idempotent by construction."""
    conn.execute(
        "UPDATE tasks SET autonomy = 'ask' "
        "WHERE autonomy IS NULL OR autonomy = 'dispatch'")


def _hygiene(conn, now: int) -> None:
    """Three data-shape repairs, all narrow and all idempotent.

    a) **Initiative status.** `strategy.STATUSES` has no `in_progress`, so such a
       row is invisible to every status filter. Fixed SET-based (`WHERE status =
       'in_progress'`) rather than by id: the plan named one row, live has two
       (a second initiative acquired the same invalid value after the plan was
       written), and repairing only the named one would leave a known-broken row
       behind for the same reason.
    b) **The stale cycle.** `spr_083acc33` ended in the past without going through
       `finish_sprint`. Closed with the SAME convention that verb uses
       (`status='completed'` + `closed_at`) so there is one meaning of "closed",
       and predicated on the status so an already-closed cycle is untouched (it
       is already `completed` live — this is a no-op there, and load-bearing on
       any copy where it is not).
    c) **Verb-audit fixtures.** `patch-test-*` projects were throwaways. DELETED
       only when nothing references them; a referenced one is archived instead,
       because deleting a row out from under a live reference trades a cosmetic
       problem for a dangling one."""
    conn.execute(
        "UPDATE initiatives SET status = ? WHERE status = ?",
        (INITIATIVE_GOOD_STATUS, INITIATIVE_BAD_STATUS))

    conn.execute(
        "UPDATE sprints SET status = 'completed', closed_at = COALESCE(closed_at, ?) "
        "WHERE id = ? AND status <> 'completed'",
        (now, STALE_SPRINT_ID))

    # Referenced anywhere → archive; referenced nowhere → delete.
    referenced = (
        "SELECT project_id FROM tasks WHERE project_id IS NOT NULL "
        "UNION SELECT project_id FROM sprints WHERE project_id IS NOT NULL "
        "UNION SELECT project_id FROM initiatives WHERE project_id IS NOT NULL"
    )
    conn.execute(
        f"UPDATE projects SET archived_at = ? "
        f"WHERE id LIKE ? AND archived_at IS NULL AND id IN ({referenced})",
        (now, PATCH_TEST_PREFIX))
    conn.execute(
        f"DELETE FROM projects WHERE id LIKE ? AND id NOT IN ({referenced})",
        (PATCH_TEST_PREFIX,))
