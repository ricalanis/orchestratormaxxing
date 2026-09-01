"""m03 — the initiatives fold: the layer stops being a concept, keeps being an audit row.

Spec §1: *"Initiative | **Folded into Project.** Roadmap fields move onto
`projects`."* `projects` grew the whole strategy vocabulary in m02 (quarter /
tier / why / success_check / health / confidence / status), so an initiative now
carries nothing a project cannot. What is left is a second container above the
project that every roll-up has to branch on — including the
`>1 initiative → suppress roll-up` branch in `object_graph.py`, which is why
`proj_orchestrator`'s tasks have never rolled up at all.

**This migration moves the DATA. It does not remove the layer** — the UI/API that
stops rendering initiatives ships separately, and the `initiatives` /
`initiative_events` tables are kept FOREVER as the frozen audit trail (spec §1:
"`initiative_events` is a real audit trail — read-only, never dropped").

--------------------------------------------------------------------------
The mapping, and how it was derived
--------------------------------------------------------------------------

Enumerated ONCE, read-only, from `~/.hermes/kanban.db` at authoring time
(2026-07-29): 9 initiatives, 5 target projects, 32 initiative-attributed tasks.
Frozen below as a static literal — a migration whose result depends on a live
read at apply time is not reproducible, and six rows do not justify a matcher
(the m02 doctrine, applied again).

The decisive measurement: **every one of the 32 attributed tasks is `done`.**
Not one initiative owns a single open task. That is what settles the only real
judgement call in this migration — "is this a workstream that deserves its own
project, or a heading?" — because a workstream with no open work is a heading:
converting it would mint a project whose entire contents are history, which adds
a surface without deleting one (the spec's governing rule) and hands Ricardo a
new empty row on the Projects page for every idea he once wrote down.

So there are **no new projects**. Two shapes only:

* **merge** — the initiative's fields land on the project it already belongs to,
  its tasks stay exactly where they are.
* **retarget** — the same, but onto a DIFFERENT project than its parent, because
  one already exists for that workstream (`Seguimiento a Fleet`). This is the
  conversion case as it actually occurred: Ricardo made the project by hand 4
  minutes after the initiative, and the open work is already in it. Minting a
  second project of the same name would be the bug, not the migration. The
  task re-point is what makes it a real move rather than a field copy, and it is
  the same mechanic a convert would use.

**One donor per project.** Five initiatives donate their roadmap fields; four do
not. `proj_orchestrator` has SIX initiatives pointed at it and one set of roadmap
columns — "copy them all where NULL" would resolve the conflict by list order,
i.e. by accident. The donor is named explicitly instead. Nothing is lost: the
non-donors' fields stay readable in `initiatives` forever, which is the entire
reason that table is kept.

--------------------------------------------------------------------------
What is NOT done here, deliberately
--------------------------------------------------------------------------

* **`initiatives.status` is not rewritten.** The schema has no `archived_at` on
  `initiatives`, and `strategy.STATUSES` offers only `shipped` / `dropped` as
  terminal values — both of which would be a lie ("Orchestratormaxxing Self-Improve
  Loop" was neither shipped nor dropped; it became a project). m02's rule is
  that no value carrying human intent is rewritten, so the fold is recorded as
  what it is: an **`initiative_folded` event** per initiative, the marker that
  says this row is history. Presence of that event is the authoritative "folded"
  predicate for anything that needs one.
* **`tasks.initiative_id` / `tasks.epic_id` are FROZEN** — never written, by
  anything, ever. They are the attribution trail this mapping was derived from;
  rewriting them would destroy the evidence. The only task column this migration
  touches is `project_id`, on the retarget row, and only for OPEN tasks.
* **`deals.initiative_id` is frozen too** (spec: deals are not auto-linked here).
* Nothing is dropped, renamed, or rewritten. Additive only.

**Open tasks only, on a re-point.** A done task rolled up to its project when it
was completed; moving it later rewrites delivery history that has already been
reported. Open work is the only thing a re-point is for.

**Idempotency.** Every field copy is predicated on the destination being NULL
(which also means: never overwrite something a human typed), the status write on
`projects.status IS NULL`, the re-point on the task not already being at the
target, and the event on no `initiative_folded` row existing yet. A rerun after a
partial failure — or against a hand-rebuilt ledger — is a no-op.

Receives the runner's OWN connection inside its transaction — it must not commit,
close, or open a connection of its own, or the all-or-nothing guarantee is lost.
"""
import json
import os
import time
from collections import namedtuple

# --- vocabulary -----------------------------------------------------------

# The roadmap fields m02 added to `projects`, in the order they are copied.
# `progress` is NOT here: `projects` has no such column and progress is derived
# from tasks, not stored (strategy.py D2).
ROADMAP_FIELDS = ("quarter", "tier", "why", "success_check", "health", "confidence")

# `strategy.STATUSES` → the `projects.status` vocabulary m02 introduced
# (planned | active | delivering | delivered | archived). `shipped` is a project
# that was delivered; `dropped` is one that was shelved. `delivering` has no
# initiative-side counterpart and is only ever set by the "Deliver this" verb.
INITIATIVE_STATUS_TO_PROJECT_STATUS = {
    "planned": "planned",
    "active":  "active",
    "shipped": "delivered",
    "dropped": "archived",
}

# A task in one of these is finished; anything else is open work. Same set
# `dashboard/brief.py` uses to count what still needs the operator.
TERMINAL_TASK_STATUSES = ("done", "rejected", "cancelled")

# The audit row. `initiative_events` already carries `initiative_created` and
# `initiative_updated` written by `strategy._log`; this is the third kind and
# the last one that will ever be added.
FOLD_EVENT_KIND = "initiative_folded"

# --- the mapping ----------------------------------------------------------

#   initiative      the row being folded
#   title           authoring-time snapshot, DOCUMENTATION ONLY (never written,
#                   never matched on) — a table of bare ids is unreviewable
#   parent          its `initiatives.project_id` at authoring time
#   target          the project it folds INTO
#   donates_fields  whether ITS roadmap fields land on `target` (one donor per
#                   target project; see the docstring)
#   decision        "merge" (target is the parent) | "retarget" (target is not)
#   note            why, in one line
Fold = namedtuple(
    "Fold", "initiative title parent target donates_fields decision note")


def _load_folds() -> list:
    """The fold table is TENANT DATA, not code: each row froze a decision about
    one tenant's real initiative/project rows. It loads from
    $HERMES_M03_FOLDS (path) else ~/.hermes/m03-folds.json — a JSON list of
    7-item rows matching the Fold fields. A machine without the file (any
    fresh install) has nothing to fold and the migration no-ops.
    """
    import pathlib
    override = os.environ.get("HERMES_M03_FOLDS", "")
    path = pathlib.Path(override) if override else \
        pathlib.Path.home() / ".hermes" / "m03-folds.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Fold(*row) for row in rows if isinstance(row, (list, tuple)) and len(row) == 7]


FOLDS = _load_folds()


# --- helpers --------------------------------------------------------------

def _blank(value) -> bool:
    """NULL and `''` are the same thing here: absent.

    Two live initiatives carry `why = ''` / `success_check = ''` (the create form
    submits empty strings). Copying those onto a project would turn "this field
    was never filled in" into "this field was answered, with nothing" — the
    project page would then render an empty section instead of prompting."""
    return value is None or (isinstance(value, str) and not value.strip())


def _initiative(conn, initiative_id: str):
    """The folded row as a dict, or None when it does not exist.

    Read positionally off `cursor.description` rather than by key: the runner's
    connection has `row_factory = sqlite3.Row`, a test driving the apply function
    directly may not, and a migration must not care."""
    cur = conn.execute(
        "SELECT status, quarter, tier, why, success_check, health, confidence "
        "FROM initiatives WHERE id = ?", (initiative_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return {col[0]: row[i] for i, col in enumerate(cur.description)}


def _project_exists(conn, project_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None


# --- the migration --------------------------------------------------------

def m03_initiatives_fold(conn) -> None:
    """Apply the fold. Registered as `m03_initiatives_fold` in `runner.py`."""
    now = int(time.time())
    for fold in FOLDS:
        initiative = _initiative(conn, fold.initiative)
        if initiative is None:
            # Nothing to fold. A DB that never had this row (a fresh bootstrap,
            # a copy taken before it was created) is not a failure — and writing
            # an audit row for a row that does not exist would be a fabrication.
            continue
        if not _project_exists(conn, fold.target):
            # A fold with no destination is not a fold. Skipping leaves the
            # initiative exactly as it was, which is recoverable; inventing the
            # project would not be.
            continue

        donated = {}
        project_status = None
        if fold.donates_fields:
            donated = _donate_fields(conn, fold.target, initiative)
            project_status = _donate_status(conn, fold.target, initiative)
        repointed = _repoint_open_tasks(conn, fold) if fold.decision == "retarget" else []

        _record_fold(conn, fold, now, donated, project_status, repointed)


def _donate_fields(conn, target: str, initiative: dict) -> dict:
    """Copy the roadmap vocabulary onto the project — **never over a value**.

    `WHERE <col> IS NULL` is doing two jobs at once: it makes a rerun a no-op,
    and it means a project field Ricardo has since edited always wins over the
    initiative it came from. Returns only the fields that actually landed, so
    the audit row says what changed rather than what was attempted."""
    landed = {}
    for field in ROADMAP_FIELDS:
        value = initiative.get(field)
        if _blank(value):
            continue
        cur = conn.execute(
            f"UPDATE projects SET {field} = ? WHERE id = ? AND {field} IS NULL",
            (value, target))
        if cur.rowcount:
            landed[field] = value
    return landed


def _donate_status(conn, target: str, initiative: dict):
    """`initiatives.status` → `projects.status`, once, and only into a blank.

    `projects.status` did not exist before m02, so every project is NULL here and
    this is the only place the lifecycle gets its initial value from history
    rather than from a human pressing "Deliver this". An unmapped status (there
    is none live — m02's hygiene already collapsed `in_progress` → `active`)
    writes nothing rather than guessing."""
    mapped = INITIATIVE_STATUS_TO_PROJECT_STATUS.get(initiative.get("status"))
    if not mapped:
        return None
    cur = conn.execute(
        "UPDATE projects SET status = ? WHERE id = ? AND status IS NULL",
        (mapped, target))
    return mapped if cur.rowcount else None


def _repoint_open_tasks(conn, fold: Fold) -> list:
    """Move the initiative's OPEN attributed tasks to the project it folded into.

    Only on a retarget: on a plain merge the target IS the parent, so there is
    nothing to move and a "stray task comes home" rule would be a second,
    unrequested migration hiding inside this one.

    `tasks.initiative_id` is read here and NEVER written — it is the attribution
    trail, and after this migration it is the only record of where a task came
    from. Idempotent: after the first run the tasks are already at the target, so
    the predicate matches nothing."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM tasks "
        "WHERE initiative_id = ? AND (project_id IS NULL OR project_id <> ?) "
        f"  AND status NOT IN ({','.join('?' * len(TERMINAL_TASK_STATUSES))})",
        (fold.initiative, fold.target, *TERMINAL_TASK_STATUSES))]
    for task_id in ids:
        conn.execute(
            "UPDATE tasks SET project_id = ? WHERE id = ?", (fold.target, task_id))
    return ids


def _record_fold(conn, fold: Fold, now: int, donated: dict,
                 project_status, repointed: list) -> None:
    """The ledger row: one `initiative_folded` event per folded initiative.

    This is the fold marker — `initiatives.status` is left alone (see the module
    docstring), so the existence of this event is what says "this row is history
    and its work now lives on a project". Written through the same shape
    `strategy._log` uses (kind + JSON payload + created_at) so the audit spine
    stays one format, and guarded by NOT EXISTS because `initiative_events` has
    an autoincrement id and no natural key — an unguarded rerun would append a
    second, contradictory account of the same event."""
    already = conn.execute(
        "SELECT 1 FROM initiative_events WHERE initiative_id = ? AND kind = ?",
        (fold.initiative, FOLD_EVENT_KIND)).fetchone()
    if already:
        return
    payload = {
        "via": "m03_initiatives_fold",
        "decision": fold.decision,
        "target_project_id": fold.target,
        "parent_project_id": fold.parent,
        "donated_fields": donated,
        "project_status": project_status,
        "repointed_task_ids": repointed,
        "note": fold.note,
    }
    conn.execute(
        "INSERT INTO initiative_events (initiative_id, kind, payload, created_at) "
        "VALUES (?,?,?,?)",
        (fold.initiative, FOLD_EVENT_KIND,
         json.dumps(payload, ensure_ascii=False), now))
