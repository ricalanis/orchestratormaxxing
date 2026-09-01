"""m00 — the migration floor: ONE schema entrypoint for every process.

Both processes that open the kanban DB (the FastAPI dashboard, `dashboard/api.py`,
and the MCP server, `mcp_server.py`) used to carry their own hand-maintained list
of `ensure_schema()` calls. They drifted: the MCP server's list was a strict
SUBSET, so a DB bootstrapped by the MCP server was missing the fireflies /
readiness / nurture / events / task-flag / comments / consulting-time schema and
the P3 indexes. `run()` is now the single chain both call, so drift is structural
impossible rather than a review promise.

Two phases:

1. **Legacy ensure phase** — the exact chain api.py ran, in the same order. Each
   `ensure_schema()` owns its own connection and stays unchanged: they are
   idempotent, additive, and are what bootstraps a fresh DB (tests depend on it).
   These predate the versioned ledger and are NOT recorded in `orch_migrations`.

2. **Versioned phase** — the forward-looking half. A single connection takes
   `BEGIN IMMEDIATE` (write lock up front, so two processes racing at startup
   serialize instead of one dying with "database is locked" halfway through a
   DDL) and applies every entry of `MIGRATIONS` not yet recorded in
   `orch_migrations`, writing the name row on the SAME connection in the SAME
   transaction — so a migration and its ledger row commit or roll back together.
   `MIGRATIONS` is empty in this commit: the floor ships before anything stands
   on it.

The 7 migrations that shipped before the ledger existed are backfilled by name
(`INSERT OR IGNORE`) so they are never re-applied to a live DB.

Backup gate: if — and only if — there is pending versioned work, `bin/backup-kanban`
runs first (verified online snapshot, exit 0). A non-zero exit ABORTS the versioned
phase (fail closed): no snapshot, no DDL on the live DB.

Run:  python -m dashboard.migrations.runner
"""
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .. import db

try:  # POSIX only — the fleet is Linux + macOS. No flock → run unserialized.
    import fcntl
except ImportError:  # pragma: no cover - not reachable on this fleet
    fcntl = None

# --- the historical migrations -------------------------------------------
# Applied to the live DB before `orch_migrations` existed. Names match the
# module filenames in this package 1:1, so the ledger reads as a directory
# listing. Backfilled, never re-run.
HISTORICAL = [
    "p0_2_unify_cycle",
    "p1_3_initiative_attribution",
    "p2_4_backlog_cleanup",
    "p3_indexes",
    "phase1_backlog_scheduling",
    "crm_growth",
    "daily_reflections",
]

def _m01_brief_runs(conn) -> None:
    """m01 — `brief_runs`, the 3x-daily ritual's persistence anchor.

    The DDL lives in `dashboard/brief.py` beside the only code that reads and
    writes the table; this is the registration shim. The import is LAZY on
    purpose: the runner is imported at startup by BOTH entrypoints, and only a
    DB with this migration actually pending should pay for pulling the composer
    (and its canvas/crm/growth dependencies) into memory.
    """
    from ..brief import m01_brief_runs
    m01_brief_runs(conn)


def _m02_spine(conn) -> None:
    """m02 — the spine: the columns and tables phase 1's verbs stand on.

    `deals.project_id` (the money→delivery join), the `projects` delivery record,
    `tasks.executor_*`/`thread_id`, the `threads` topic registry and the
    `task_dispatches` outbox — plus the executor/autonomy backfills. Lazy import
    for the same reason as m01: the runner is imported at startup by BOTH
    entrypoints, and only a DB with this migration pending should pay for it.
    """
    from .m02_spine import m02_spine
    m02_spine(conn)


def _m03_initiatives_fold(conn) -> None:
    """m03 — the initiatives fold: the roadmap layer moves onto `projects`.

    Copies each initiative's roadmap vocabulary onto the project it folds into
    (never over a non-NULL value), maps its status into the `projects.status`
    lifecycle, re-points the one retargeted workstream's open tasks, and records
    an `initiative_folded` audit event per initiative. Strictly AFTER m02, which
    is what adds the `projects` columns it writes into. Lazy import for the same
    reason as m01/m02.
    """
    from .m03_initiatives_fold import m03_initiatives_fold
    m03_initiatives_fold(conn)


def _m04_project_lifecycle(conn) -> None:
    """m04 — the lifecycle floor: every non-archived project gets a status.

    m02 added the column, m03 filled 5 of 18 rows from the initiatives it
    folded; the other 13 stayed NULL, so no reader could trust it. Backfills
    each remaining project from its own tasks (open work → active, all settled →
    delivered, none → planned). Strictly AFTER m03, whose writes it must not
    pre-empt. Lazy import for the same reason as m01/m02/m03.
    """
    from .m04_project_lifecycle import m04_project_lifecycle
    m04_project_lifecycle(conn)


def _m05_retire_delivered_stage(conn) -> None:
    """m05 — `deals.stage = 'delivered'` stops being writable, in the engine.

    A won deal stays won; delivery is `projects.status` (ruling 2). The
    application refuses the value in `crm.create_deal`/`update_deal`; this
    installs the same refusal as a BEFORE INSERT / BEFORE UPDATE OF stage
    trigger pair, so a writer that never goes through `crm.py` cannot
    resurrect the second meaning. Asserts the table is already clean first
    (measured: 0 live rows) and raises rather than rewriting stages silently.
    Strictly AFTER m04, which is what gives `projects.status` its floor — the
    column the retirement moves the truth onto. Lazy import for the same reason
    as m01-m04.
    """
    from .m05_retire_delivered_stage import m05_retire_delivered_stage
    m05_retire_delivered_stage(conn)


def _m10_attachments(conn) -> None:
    """m10 — `attachments`, the edge table the five-facet project hub reads.

    One generic polymorphic table (node_kind × kind) for the four facets that
    point OUTSIDE this DB — conversations (Fireflies), resources (Drive), code
    (GitHub), plans (the `~/dev/planning` repo). The fifth facet, tasks, stays
    where it lives: the hub counts the `tasks` table, it never shadows it here.
    Purely additive, so its position after m05 is ordering hygiene rather than a
    dependency. Lazy import for the same reason as m01-m05.
    """
    from .m10_attachments import m10_attachments
    m10_attachments(conn)


def _m06_task_deal(conn) -> None:
    """m06 — `tasks.deal_id` + `tasks.stage_kind`: the spine's last hop.

    A task could say which PROJECT delivers it and never which DEAL it exists
    for; `deal_id` closes that, and `stage_kind` names the resulting position in
    the cycle (contacto → … → cobranza, directiva ADICIÓN 9). Deliberately
    backfill-free: `stage_kind` is DERIVED at read time by
    `dashboard/stagekind.py`, so a NULL means "ask the rule" and a backfill
    would freeze today's answer into the row.

    Runs AFTER m05, which is what made `projects.status` the single truth of
    delivery — the signal `stage_kind` reads to tell `ejecucion` from `entrega`.
    Its position after m10 in this list is APPEND-ONLY hygiene, not a
    dependency: the ledger records what has run by NAME, so appending is the
    only way to add a migration that cannot re-order what an already-migrated DB
    believes it has applied. Lazy import for the same reason as m01-m05/m10.
    """
    from .m06_task_deal import m06_task_deal
    m06_task_deal(conn)


def _m07_cadence_ledger(conn) -> None:
    """m07 — the cadence ledger: `nurture_sequences.sent_at` + `task_id`.

    `sent_at` is the column `crm.get_cadence_status` was ALREADY reading (its
    compliance arithmetic could not be nonzero without it); `task_id` is the
    backref that lets a task moving to `done` find the step it carried, so the
    loop can close without guessing. Runs AFTER m06 — ordering hygiene rather
    than a hard dependency, but m06's `idx_tasks_deal_cadence_open` and m07's
    `idx_nurture_task` are one anti-nag rule split across two migrations
    (ruling 7), so they read in order. Lazy import for the same reason as
    m01-m06.
    """
    from .m07_cadence_ledger import m07_cadence_ledger
    m07_cadence_ledger(conn)


def _m08_deal_event_provenance(conn) -> None:
    """m08 — `deal_events.source` / `channel`: the audit spine learns provenance.

    The touch-channel commit (4d5114b) put `channel` in the event's JSON
    PAYLOAD, not in a column — verified in situ — so the monthly rollup still
    attributes every touch to the deal's original `lead_source`. These two
    columns make the medium and the writer queryable; `crm._log` is the only
    thing that writes them. Purely additive, so its position after m07 is
    ordering hygiene. Lazy import for the same reason as m01-m07.
    """
    from .m08_deal_event_provenance import m08_deal_event_provenance
    m08_deal_event_provenance(conn)


def _m09_sales_project(conn) -> None:
    """m09 — `proj_ventas`, the lane the materializer's cards land in.

    Every Today/Later/plan read carries `t.project_id != 'proj_personal'`, and
    under SQLite's three-valued logic a NULL project makes that predicate NULL
    (not true) — a project-less cadence card would be written and rendered
    nowhere. Runs AFTER m08; no data dependency, but the materializer that needs
    it also needs m07's ledger. Lazy import for the same reason as m01-m08.
    """
    from .m09_sales_project import m09_sales_project
    m09_sales_project(conn)


def _m11_billing(conn) -> None:
    """m11 — `deals.invoiced_at` / `deals.paid_at` (directiva ADICIÓN 8).

    The last two facts of the money loop, as timestamps on the deal rather than
    a fifth noun. Runs AFTER m09 because the cobranza tasks the materializer
    mints off these columns land in `proj_ventas`. Lazy import for the same
    reason as m01-m09.
    """
    from .m11_billing import m11_billing
    m11_billing(conn)


def _m12_thread_stations(conn):
    """Threads gain their funnel station (Ricardo sign-off 2026-08-02): the four
    journey stations + Hoy as ritual, additive column, role CHECK untouched.
    Lazy import as siblings."""
    from .m12_thread_stations import apply
    apply(conn)


def _m13_reflection_goals(conn) -> None:
    """m13 — boolean completion vector for morning reflection goals.

    The canonical goal text remains in ``morning_intentions``. This migration
    only adds date-scoped completion state to the same reflection row.
    """
    from .m13_reflection_goals import m13_reflection_goals
    m13_reflection_goals(conn)


def _m14_personal_okrs(conn) -> None:
    """m14 — the operator's personal objectives and check-ins."""
    from .m14_personal_okrs import m14_personal_okrs
    m14_personal_okrs(conn)


def _m15_differential_capture(conn) -> None:
    """m15 — the differential capture spine: events (E0 verbatim + E1 episodic)
    → objectives/entity_state (E2 governed gist) → derived suggestions, with the
    applier's op ledger. Additive; identity never derives from model prose."""
    from .m15_differential_capture import apply
    apply(conn)


def _m16_capture_receipts(conn) -> None:
    """m16 — `capture_receipts`: el acuse durable del webhook de Fireflies.
    El aviso no trae contenido, así que el webhook deja rastro y el tick hace el
    fetch con reintentos; sin esta fila, un proceso que muere tras el 200 pierde
    la junta sin dejar señal."""
    from .m16_capture_receipts import apply
    apply(conn)


def _m17_cash_flow(conn) -> None:
    """m17 — el plan del cobro sobre el deal: `payment_terms_days` +
    `expected_payment_date` + su ancla write-once. m11 dio los hechos del
    dinero; esto le da el futuro pactado, sin tabla nueva y sin backfill."""
    from .m17_cash_flow import m17_cash_flow
    m17_cash_flow(conn)


def _m18_invoice_launch(conn) -> None:
    """m18 — `expected_invoice_date`: el lanzamiento del cobro (plan de acción
    PROPIA, gobernanza ligera) junto al pago esperado de m17 (promesa del
    cliente, verbo auditado). Dos dueños, dos columnas, dos writers."""
    from .m18_invoice_launch import m18_invoice_launch
    m18_invoice_launch(conn)


def _m19_paid_amount(conn) -> None:
    """m19 — `paid_amount`: el monto que realmente llegó, capturado en el tap
    ✅ y leído siempre como COALESCE(paid_amount, value). El booleano
    'facturado' sigue siendo derivado (invoiced_at IS NOT NULL), nunca columna."""
    from .m19_paid_amount import m19_paid_amount
    m19_paid_amount(conn)


def _m20_whatsapp_allowlist(conn) -> None:
    """m17 — allowlist de chats de WhatsApp (default-deny) + pulso de actividad
    sin contenido. El webhook solo escribe el pulso; el texto se lee del espejo
    de wacli únicamente para chats permitidos y con la ventana cerrada."""
    from .m20_whatsapp_allowlist import apply
    apply(conn)


def _m21_whatsapp_verdicts(conn) -> None:
    """m21 — el veredicto del clasificador, en columnas SEPARADAS de `allowed`.
    Nunca se copia una en la otra: un clasificador que se auto-otorga permiso de
    leer conversaciones es el fallo que este sistema no puede tener."""
    from .m21_whatsapp_verdicts import apply
    apply(conn)


def _m22_whatsapp_backfill(conn) -> None:
    """m22 — hasta dónde se bajó el historial de cada chat. Bajar lo anterior al
    permiso es una decisión distinta de darlo, y deja su propio rastro."""
    from .m22_whatsapp_backfill import apply
    apply(conn)


def _m23_project_weekly_hours(conn) -> None:
    """m23 — cuántas horas por semana vale cada proyecto activo. Presupuesto
    declarado, no pronóstico: sin backfill, NULL significa sin dimensionar."""
    from .m23_project_weekly_hours import m23_project_weekly_hours
    m23_project_weekly_hours(conn)


def _m24_weekly_hours_set_at(conn) -> None:
    """m24 — cuándo se declaró el reparto semanal. Sin fecha, una declaración
    de hace seis semanas se ve igual que la de esta mañana."""
    from .m24_weekly_hours_set_at import m24_weekly_hours_set_at
    m24_weekly_hours_set_at(conn)


def _m25_thread_role_design(conn) -> None:
    """m25 — `design` entra al vocabulario de roles y el hilo Designer (15957)
    se registra. Un diseñador archivado como `code` hace mentir a toda
    superficie que rebana por rol."""
    from .m25_thread_role_design import m25_thread_role_design
    m25_thread_role_design(conn)


def _m26_project_week_plan(conn) -> None:
    """m26 — horas propuestas por proyecto para semanas futuras."""
    from .m26_project_week_plan import m26_project_week_plan
    m26_project_week_plan(conn)


def _m27_crm_proposals(conn) -> None:
    """m27 — bandeja propose-only de correcciones al CRM (motor caliente)."""
    from .m27_crm_proposals import m27_crm_proposals
    m27_crm_proposals(conn)


def _m28_weekly_reflections(conn) -> None:
    """m28 — las 5 preguntas del viernes (motor caliente)."""
    from .m28_weekly_reflections import m28_weekly_reflections
    m28_weekly_reflections(conn)


def _m30_fireflies_dedupe(conn) -> None:
    """m30 — una sola fila de cache por (deal, transcripción)."""
    from .m30_fireflies_dedupe import m30_fireflies_dedupe
    m30_fireflies_dedupe(conn)


def _m31_commercial_proposals(conn) -> None:
    """m31 — paquetes comerciales versionados + recibos de envío."""
    from .m31_commercial_proposals import m31_commercial_proposals
    m31_commercial_proposals(conn)


def _m32_commercial_proposal_quality(conn) -> None:
    """m32 — identidad inmutable de evidencia/calidad para workspaces v2."""
    from .m32_commercial_proposal_quality import m32_commercial_proposal_quality
    m32_commercial_proposal_quality(conn)


def _m33_task_run_envelopes(conn) -> None:
    """m33 — four-brake execution envelopes and durable practice receipts."""
    from .m33_task_run_envelopes import m33_task_run_envelopes
    m33_task_run_envelopes(conn)


def _m29_task_plan_requests(conn) -> None:
    """m29 — outbox honesto para lanzar sesiones de planeación."""
    from .m29_task_plan_requests import m29_task_plan_requests
    m29_task_plan_requests(conn)


# --- the versioned migrations --------------------------------------------
# Ordered [(name, apply_fn)]. `apply_fn(conn)` receives the runner's OWN open
# connection inside the transaction — it must NOT commit, close, or open its
# own connection, or the all-or-nothing guarantee is lost.
MIGRATIONS: list = [
    ("m01_brief_runs", _m01_brief_runs),
    ("m02_spine", _m02_spine),
    ("m03_initiatives_fold", _m03_initiatives_fold),
    ("m04_project_lifecycle", _m04_project_lifecycle),
    ("m05_retire_delivered_stage", _m05_retire_delivered_stage),
    ("m10_attachments", _m10_attachments),
    ("m06_task_deal", _m06_task_deal),
    ("m07_cadence_ledger", _m07_cadence_ledger),
    ("m08_deal_event_provenance", _m08_deal_event_provenance),
    ("m09_sales_project", _m09_sales_project),
    ("m11_billing", _m11_billing),
    ("m12_thread_stations", _m12_thread_stations),
    ("m13_reflection_goals", _m13_reflection_goals),
    ("m14_personal_okrs", _m14_personal_okrs),
    ("m15_differential_capture", _m15_differential_capture),
    ("m16_capture_receipts", _m16_capture_receipts),
    ("m17_cash_flow", _m17_cash_flow),
    ("m18_invoice_launch", _m18_invoice_launch),
    ("m19_paid_amount", _m19_paid_amount),
    ("m20_whatsapp_allowlist", _m20_whatsapp_allowlist),
    ("m21_whatsapp_verdicts", _m21_whatsapp_verdicts),
    ("m22_whatsapp_backfill", _m22_whatsapp_backfill),
    ("m23_project_weekly_hours", _m23_project_weekly_hours),
    ("m24_weekly_hours_set_at", _m24_weekly_hours_set_at),
    ("m25_thread_role_design", _m25_thread_role_design),
    ("m26_project_week_plan", _m26_project_week_plan),
    ("m27_crm_proposals", _m27_crm_proposals),
    ("m28_weekly_reflections", _m28_weekly_reflections),
    ("m29_task_plan_requests", _m29_task_plan_requests),
    ("m30_fireflies_dedupe", _m30_fireflies_dedupe),
    ("m31_commercial_proposals", _m31_commercial_proposals),
    ("m32_commercial_proposal_quality", _m32_commercial_proposal_quality),
    ("m33_task_run_envelopes", _m33_task_run_envelopes),
]

# orchestrator/bin/backup-kanban — resolved absolutely: a systemd --user unit's
# PATH does not include the repo, and the CWD is not ours to assume.
BACKUP_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "backup-kanban"

# The versioned phase is serialized ACROSS PROCESSES by BEGIN IMMEDIATE; this
# lock serializes it WITHIN one process (the dashboard is threaded), so two
# threads calling run() can't both drive the non-transactional legacy ensures.
_RUN_LOCK = threading.Lock()

BUSY_TIMEOUT_MS = 30000

# The cross-process lock for the LEGACY phase. Sidecar file, always resolved
# beside the DB this runner is about to alter (callers repoint `db.KANBAN_DB`
# without touching the environment, and two DBs must not share one lock).
LEGACY_LOCK_SUFFIX = ".migrate.lock"


def legacy_lock_path() -> Path:
    return Path(str(db.KANBAN_DB) + LEGACY_LOCK_SUFFIX)


@contextmanager
def _legacy_lock():
    """Serialize `ensure_legacy_schema()` ACROSS PROCESSES.

    The versioned phase has taken BEGIN IMMEDIATE since it shipped; the legacy
    phase never had anything, and it is built almost entirely out of
    `PRAGMA table_info` → `if column not in cols` → `ALTER TABLE` — a TOCTOU per
    column. Both entrypoints run this chain at startup (api.py at import time,
    mcp_server.py in its own try), so with ANY legacy DDL genuinely pending, two
    processes starting together meant one of them dying on `duplicate column
    name: …`: the dashboard failing to boot, or the gateway coming up crippled
    and silent. Proven with 4 concurrent `run()`s against a DB missing `epics`
    (tests/test_migration_runner.py::ConcurrentStartup) — 2 of 4 crashed.

    An flock, not BEGIN IMMEDIATE: the legacy ensures each own their private
    connection (they predate the ledger and are deliberately left unchanged), so
    there is no single transaction to hold. The lock is advisory and OS-owned —
    it is released when the fd closes, INCLUDING on a crash, so a dead process
    can never wedge startup. Blocking, not timed: the chain is bounded (~1s) and
    a bounded wait that then proceeds anyway would just reinstate the race.

    Fails OPEN (yields False) if the lock file cannot be created — a read-only
    directory must not stop a process from booting; it only loses serialization,
    which is exactly where we were before.
    """
    if fcntl is None:
        yield False
        return
    path = legacy_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
    except OSError:
        yield False
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def ensure_legacy_schema() -> None:
    """Phase 1 — the pre-ledger ensure chain, in api.py's original order.

    Imported lazily so `import runner` stays a cheap, side-effect-free import
    for the MCP server (which pulls this module in at startup).
    """
    from .. import object_graph as graph
    from .. import orchestration as orch
    from .. import identity
    from .. import canvas
    from .. import sprints

    # Phase 2 — object-graph tables/columns.
    graph.ensure_schema()
    # Parallel-orchestration sidecar tables/dirs.
    orch.ensure_schema()
    # Phase 1 — projects.kind + canonical taxonomy (proj_inbox / proj_gpu_ops /
    # personal) + the ownership/origin model (owner/delegate/origin + agent seed
    # + create-time identity trigger).
    identity.ensure_schema()
    identity.ensure_taxonomy()
    identity.ensure_identity()
    # Phase 3 — canvas columns (planned_for / plan_order / due_date).
    canvas.ensure_schema()
    # Phase 3 — cycle model: nullable sprints.project_id, commit-ledger columns,
    # cycle_velocity VIEW.
    sprints.ensure_cycle_schema()
    # Phase 4 — governance: contract_cmd column, auto-pool triggers, epoch marker.
    from .. import governance
    governance.ensure_schema()
    # Phase 6 — strategy in the DB: initiatives + initiative_events (+ the
    # one-time roadmap.json migration; the file is a generated export).
    from .. import strategy
    strategy.ensure_schema()
    # Phase 6 — CRM entities: accounts / contacts / deals (+ deal_events).
    from .. import crm
    crm.ensure_schema()
    # CRM Growth System — value ladder / growth loops / lead scoring / touches.
    from .. import growth
    growth.ensure_schema()
    # Fireflies meeting signals cache (additive to deals + fireflies_meetings).
    from .. import fireflies  # noqa: F401 - imported for parity with api.py
    db.ensure_fireflies_schema()
    # Readiness scoring — additive readiness_score + readiness_dimensions on deals.
    from .. import readiness
    readiness.ensure_schema()
    db.ensure_nurture_schema()
    db.ensure_events_schema()
    # Kanban card flags: tasks.pinned_bottom.
    db.ensure_task_flags_schema()
    # Task comments: match hermes' existing task_comments shape on a fresh DB.
    from .. import comments as task_comments
    task_comments.ensure_schema()
    # Personal Health — daily ritual timeline + check-offs + plate reference.
    from .. import health as _health_mod
    _health_mod.ensure_schema()
    # Daily Reflection — examen adaptado: morning intentions + evening review.
    from .. import reflection as _reflection_mod
    _reflection_mod.ensure_schema()
    # Consultant Time Ledger — actual consulting time per project (PRD §6–7).
    from .. import consulting_time as _consulting_time_mod
    _consulting_time_mod.ensure_schema()
    # Personal OKRs — seed-driven catalog + immutable check-in history.
    from .. import okrs as _okrs_mod
    _okrs_mod.ensure_schema()
    # P3 — performance indexes, including the hermes-owned tasks.initiative_id FK
    # that no dashboard ensure_schema() owns.
    from . import p3_indexes as _p3_indexes
    _p3_indexes.run()


def run_backup() -> None:
    """The fail-closed gate before any versioned DDL touches the live DB.

    `bin/backup-kanban` writes a transaction-consistent online snapshot and
    verifies it with PRAGMA integrity_check; exit 0 means a snapshot exists.
    Anything else raises — we would rather not migrate than migrate unbacked.
    The DB path is passed explicitly so the snapshot is provably of the SAME
    file this runner is about to alter (callers may have repointed
    ``db.KANBAN_DB`` without touching the environment).
    """
    if not BACKUP_SCRIPT.exists():
        raise RuntimeError(f"migration aborted: backup tool missing at {BACKUP_SCRIPT}")
    env = dict(os.environ)
    env["HERMES_KANBAN_DB"] = str(db.KANBAN_DB)
    proc = subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT)],
        capture_output=True, text=True, env=env, timeout=300,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(
            f"migration aborted: backup-kanban exited {proc.returncode}: {tail}")


def _applied_names(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM orch_migrations")}


def run() -> dict:
    """The one schema entrypoint. Idempotent; safe to call from every process.

    Both phases are now serialized across processes: the legacy chain by the
    sidecar flock, the versioned one by its own BEGIN IMMEDIATE (kept there,
    where the transaction is, rather than widened to cover both — the flock
    holds only for the ~1s the ensure chain needs).
    """
    with _RUN_LOCK:
        with _legacy_lock():
            ensure_legacy_schema()
        return run_versioned()


def run_versioned() -> dict:
    """Phase 2 only — the ledger + pending `MIGRATIONS`, one transaction."""
    conn = db.get_conn()
    # Autocommit mode: WE issue BEGIN IMMEDIATE, so sqlite3 must not open its
    # own implicit deferred transaction underneath us.
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    backfilled: list = []
    applied: list = []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS orch_migrations ("
            "  name       TEXT PRIMARY KEY,"
            "  applied_at INTEGER NOT NULL"
            ")")

        # Cheap pre-check: only pay for a backup when there is real work. The
        # authoritative read happens again under the write lock below.
        if [n for n, _ in MIGRATIONS if n not in _applied_names(conn)]:
            run_backup()

        conn.execute("BEGIN IMMEDIATE")
        try:
            now = int(time.time())
            done = _applied_names(conn)
            for name in HISTORICAL:
                if name in done:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO orch_migrations (name, applied_at) VALUES (?,?)",
                    (name, now))
                backfilled.append(name)
            for name, apply_fn in MIGRATIONS:
                if name in done:
                    continue
                apply_fn(conn)
                conn.execute(
                    "INSERT INTO orch_migrations (name, applied_at) VALUES (?,?)",
                    (name, int(time.time())))
                applied.append(name)
            conn.execute("COMMIT")
        except BaseException:
            # All-or-nothing: SQLite rolls DDL back too, so a half-applied
            # migration can never leave a name row claiming it succeeded.
            try:
                conn.execute("ROLLBACK")
            except Exception:  # pragma: no cover - defensive
                pass
            raise
    finally:
        conn.close()

    return {
        "status": "ok",
        "db": str(db.KANBAN_DB),
        "backfilled": backfilled,
        "applied": applied,
        "registered": [n for n, _ in MIGRATIONS],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, ensure_ascii=False))
