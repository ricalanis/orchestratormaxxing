"""m09 — `proj_ventas`: the project sales tasks land in, so they can be SEEN.

Journey fase 1, step 5. The materializer mints commercial cards ("Romper el
hielo con Cliente A", "Facturar Cliente B — $120,000"), and they need a `project_id`
for a reason that is mechanical, not aesthetic.

--------------------------------------------------------------------------
Why a NULL project_id would make the cards invisible
--------------------------------------------------------------------------

Every Today/Later/plan read in `dashboard/canvas.py` carries the predicate
`t.project_id != 'proj_personal'` (canvas.py:333, 361, 382, 492, 512 …). Under
SQLite's three-valued logic `NULL != 'proj_personal'` evaluates to **NULL**,
not to true — so a task with no project is silently dropped by every one of
them. A minted card with a NULL project would be written, counted by nothing,
and rendered nowhere: the exact failure mode of the 44 orphaned tasks the
`trg_task_default_project` trigger was installed to end.

That trigger would in fact catch it and floor the card into `proj_inbox` — a
*worse* outcome than being invisible, because the untriaged Inbox is the one
place whose whole meaning is "nobody has decided what this is yet", and a
cadence card is a card the system decided about with certainty.

So: one system row, `proj_ventas`, and the materializer names it explicitly.

--------------------------------------------------------------------------
`kind = 'sales'`, and the hiding mechanism — VERIFIED, then EXTENDED
--------------------------------------------------------------------------

The fase-1 plan flagged the `proj_inbox` / `proj_personal` hiding mechanism as
*not line-verified*. It is now. There is exactly ONE mechanism in the codebase:

    dashboard/sprints.py:1423 — `_week_bucket_tasks`
    "AND COALESCE(p.kind, 'product') NOT IN ('personal', 'system')"

That predicate is what keeps the Inbox and personal-admin work out of the three
sprint-less planning buckets (icebox / next week / future). Nothing else filters
on `kind`: `sprints.list_projects()` returns `proj_inbox` like any other row,
and the Projects tab renders it. So "hidden like proj_inbox" means precisely
*"excluded from cycle planning"*, and nothing more — a smaller claim than the
plan assumed, and worth writing down rather than inheriting.

`kind` is therefore the hiding axis, and the migration ships `'sales'` rather
than reusing `'system'`: ADICIÓN 9 makes the cycle's stages first-class, and a
sales project labelled `system` would be indistinguishable from the triage
inbox to every reader that groups by kind. The one predicate above is extended
to `('personal', 'system', 'sales')` in the same change — a cadence card belongs
in Hoy (via `plan_candidates`' `why='cliente'` source), never in the backlog the
operator grooms for a delivery cycle.

--------------------------------------------------------------------------
Idempotency, and why it is keyed on the SLUG
--------------------------------------------------------------------------

`identity.ensure_taxonomy` keys on `slug`, and this row must survive that
function running against it on every startup. So the guard is
`SELECT 1 FROM projects WHERE slug = 'ventas'`, exactly as ensure_taxonomy's
is — and the id is pinned to `proj_ventas` (with a collision fallback) because
`dashboard/cadence.py` names it as a constant. If the id were generated the
materializer would have to resolve it by slug on every call, which is a join
per mint for a row that never changes.

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own.
"""
import time

SALES_SLUG = "ventas"
SALES_PROJECT_ID = "proj_ventas"
SALES_KIND = "sales"
SALES_NAME = "Ventas · cadencia"
SALES_ICON = "💼"
SALES_COLOR = "#0ea5e9"


def m09_sales_project(conn) -> dict:
    """Create the `proj_ventas` system row. Idempotent, keyed on the slug.

    Registered as `m09_sales_project` in `runner.py`, after m08. Status is
    `active` — the project is a permanent lane, not a delivery with a lifecycle,
    and `planned` would make `stagekind.derive` answer `ejecucion` for a card
    whose real position is whatever its DEAL says (the materializer stamps that
    explicitly anyway).

    Returns `{"project_id": …, "created": bool}`; the runner ignores it and the
    contract reads it.
    """
    now = int(time.time())
    existing = conn.execute(
        "SELECT id FROM projects WHERE slug = ?", (SALES_SLUG,)).fetchone()
    if existing is not None:
        return {"project_id": existing[0], "created": False}

    pid = SALES_PROJECT_ID
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        # Same collision guard as ensure_taxonomy's: a row already owns the id
        # under a different slug. Cadence resolves by id first and falls back to
        # the slug, so a suffixed id still works.
        pid = f"{SALES_PROJECT_ID}_{now}"

    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    names = ["id", "slug", "name", "description", "color", "icon", "kind", "created_at"]
    values = [pid, SALES_SLUG, SALES_NAME,
              "Tareas de venta materializadas por la cadencia (nurture, entrega, "
              "facturación, cobranza). No es una entrega: es el carril comercial.",
              SALES_COLOR, SALES_ICON, SALES_KIND, now]
    if "status" in cols:
        names.append("status")
        values.append("active")
    conn.execute(
        f"INSERT INTO projects ({', '.join(names)}) "
        f"VALUES ({', '.join('?' * len(names))})", values)
    return {"project_id": pid, "created": True}
