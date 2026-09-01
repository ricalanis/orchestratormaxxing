"""m04 — the project lifecycle stops being optional.

`projects.status` shipped in m02_spine and m03 wrote it for the five projects an
initiative folded into. Everything else was left NULL: measured read-only on
`~/.hermes/kanban.db` (2026-08-01) **13 of the 18 rows — every non-archived
project that never had an initiative** — against 2 `active` and 3 `planned`.

That is not a half-filled column, it is a column with no meaning. A lifecycle
value only says something if the absence of one is impossible; while most rows
are NULL every reader has to invent what NULL means, and the four that exist
invent different things (the drawer renders "—", the roadmap treats it as
not-started, `pipeline()` cannot derive delivery from it at all, and fase 1's
whole delivery leg is `projects.status = 'delivered'`). Filling the floor is
what lets the next steps *read* the column instead of guessing around it.

--------------------------------------------------------------------------
The rule, and why it is evidence rather than a default
--------------------------------------------------------------------------

A blanket `planned` would have been one line, and it would have claimed that a
project delivered eight months ago is about to start — a fabricated fact, on the
column the delivery leg is about to trust. So each project is given the status
its OWN TASKS justify, which is the only evidence in the DB about where it is:

  * any task not settled           → `active`     (there is open work in it)
  * tasks, and every one settled    → `delivered`  (the work in it is finished)
  * no tasks at all                 → `planned`    (nothing has started)

"Settled" is `done | rejected | cancelled` — the same terminal set m03 and the
brief use, so "still needs Ricardo" means one thing across the codebase.

The split this produced at authoring time (documentation, never matched on —
the rule is re-evaluated at apply time, which is what makes the fabricated
per-branch fixtures in `tests/test_project_lifecycle.py` meaningful):

    active     7   proj_client_a · proj_drones · proj_client_b · proj_client_c
                   proj_inbox · proj_personal · proj_side
    delivered  5   proj_client_d · proj_teaching · proj_client_e
                   proj_gpu_ops · proj_grad_school
    planned    1   proj_records

--------------------------------------------------------------------------
What is deliberately NOT done
--------------------------------------------------------------------------

* **No `delivered_at`.** The status is derivable from evidence; the *date* is
  not. Stamping `now` on a project delivered months ago would turn "we do not
  know when" into a specific false claim, and `delivered_at` is a field the
  drawer renders. It stays NULL until a human presses a verb that knows.
* **Archived projects are out of scope.** `archived_at` already says what an
  archived row is, and the backfill is about the rows the UI still renders.
* **A value a human set is never overwritten** — every write is guarded by
  `status IS NULL`, which is also what makes a rerun a no-op.
* **This migration does not route through `sprints.set_project_status`.**
  Ruling 8 makes the lifecycle writer the single writer for RUNTIME changes and
  explicitly exempts historical migrations. Two reasons it must be exempt here:
  the writer stamps `delivered_at` on →delivered (see above), and it validates
  against a vocabulary this migration is what establishes. A backfill is not a
  lifecycle event; it is the floor the lifecycle starts from.

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own, or the all-or-nothing guarantee
is lost.
"""

# The shipped vocabulary (`sprints.PROJECT_STATUSES`), named here as the three
# values this migration can produce. Deliberately duplicated as literals rather
# than imported: a migration is a historical record of what it wrote, and it must
# keep meaning the same thing after the runtime vocabulary is edited.
PLANNED = "planned"
ACTIVE = "active"
DELIVERED = "delivered"

# A task in one of these is finished; anything else is open work. Same set
# m03_initiatives_fold and dashboard/brief.py use.
TERMINAL_TASK_STATUSES = ("done", "rejected", "cancelled")


def m04_project_lifecycle(conn) -> dict:
    """Give every non-archived project a status. Registered as
    `m04_project_lifecycle` in `runner.py`, after m03 (which writes 5 of them).

    Returns `{status: count}` for the rows it actually wrote — a summary the
    runner ignores and the contract reads."""
    settled = ",".join("?" * len(TERMINAL_TASK_STATUSES))
    rows = conn.execute(
        "SELECT id FROM projects WHERE archived_at IS NULL AND status IS NULL"
    ).fetchall()

    written: dict = {}
    for row in rows:
        project_id = row[0]
        # Two counts, one pass: `total` separates "finished" from "never
        # started", which a single open-task count cannot.
        total, open_count = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status NOT IN "
            f"({settled}) THEN 1 ELSE 0 END) "
            "FROM tasks WHERE project_id = ?",
            (*TERMINAL_TASK_STATUSES, project_id)).fetchone()
        status = ACTIVE if (open_count or 0) else (DELIVERED if total else PLANNED)
        cur = conn.execute(
            "UPDATE projects SET status = ? "
            "WHERE id = ? AND status IS NULL AND archived_at IS NULL",
            (status, project_id))
        if cur.rowcount:
            written[status] = written.get(status, 0) + 1
    return written
