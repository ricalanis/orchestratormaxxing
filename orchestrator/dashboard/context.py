"""P0-3 — one-call entity context for the drawer (revised-final-plan §3).

`build_context(type, id)` returns `{entity, ancestors, children, actions}` for
any of: task, project, initiative, deal, session. This is the single upward+
downward traversal the entity drawer needs, so the client never makes N chatty
round-trips to reconstruct a breadcrumb.

Shape contract:
  entity    — {type, id, title, ...type-specific fields}
  ancestors — parents, clickable refs, ordered top-of-hierarchy → nearest
  children  — descendants / attached records, clickable refs
  actions   — UI action hints for this entity type (advisory)

A ref is {type, id, title, clickable, ...}. Traversal reuses the existing
read layer (db / sprints / strategy / crm / object_graph) — no new SQL truth,
just joins across the spine. Returns None when the entity doesn't exist so the
endpoint can 404; raises ValueError for an unknown type (→ 400).
"""
from typing import Optional

from . import db
from . import sprints
from . import strategy
from . import crm
from . import object_graph as graph
from . import orchestration as orch

ENTITY_TYPES = ("task", "project", "initiative", "deal", "session", "account")

_ACTIONS = {
    "task": ["accept", "reject", "cycle", "assign", "epic"],
    "initiative": ["edit", "attribute"],
    "project": ["archive"],
    # `deliver` is conversion verb 2/3 (won deal → the project that delivers it).
    "deal": ["advance", "deliver", "event"],
    "session": ["open", "send"],
    "account": [],
}


def _ref(type_: str, id_: str, title, **extra) -> dict:
    ref = {"type": type_, "id": id_, "title": title, "clickable": True}
    ref.update(extra)
    return ref


def build_context(entity_type: str, entity_id: str) -> Optional[dict]:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity type '{entity_type}' (expected one of {ENTITY_TYPES})")
    builder = {
        "task": _task_context,
        "project": _project_context,
        "initiative": _initiative_context,
        "deal": _deal_context,
        "session": _session_context,
        "account": _account_context,
    }[entity_type]
    ctx = builder(entity_id)
    if ctx is None:
        return None
    ctx.setdefault("actions", _ACTIONS.get(entity_type, []))
    return ctx


# --------------------------------------------------------------------- task

def _task_context(task_id: str) -> Optional[dict]:
    task = db.get_task(task_id)
    if not task:
        return None
    conn = db.get_conn()
    try:
        # Resolve the task's initiative the same way the roll-up does:
        # COALESCE(epic.initiative_id, task.initiative_id) (P0-1).
        init_id = task.initiative_id
        epic = None
        if task.epic_id:
            erow = conn.execute(
                "SELECT id, title, initiative_id FROM epics WHERE id = ?",
                (task.epic_id,)).fetchone()
            if erow:
                epic = dict(erow)
                init_id = erow["initiative_id"] or init_id

        ancestors = []
        # The commercial lineage comes FIRST when it exists — the same
        # precedence the contextChip renders on the card (a task that exists
        # because of a deal is answered by the client, not by the project).
        # Read off the task's own `deal_id` (m06), not through the initiative:
        # a sales task has no project and no initiative at all, so the
        # initiative walk below can never find its deal.
        deal_row = None
        if getattr(task, "deal_id", None):
            deal_row = conn.execute(
                "SELECT d.id, d.title, d.stage, d.account_id, a.name AS account_name "
                "FROM deals d LEFT JOIN accounts a ON a.id = d.account_id "
                "WHERE d.id = ?", (task.deal_id,)).fetchone()
            if deal_row:
                if deal_row["account_id"]:
                    ancestors.append(_ref("account", deal_row["account_id"],
                                          deal_row["account_name"]))
                ancestors.append(_ref("deal", deal_row["id"], deal_row["title"],
                                      stage=deal_row["stage"]))
        proj = _project_row(conn, task.project_id)
        if proj:
            ancestors.append(_ref("project", proj["id"], proj["name"], icon=proj["icon"]))
        if init_id:
            irow = conn.execute(
                "SELECT id, title, status FROM initiatives WHERE id = ?", (init_id,)).fetchone()
            if irow:
                ancestors.append(_ref("initiative", irow["id"], irow["title"], status=irow["status"]))
                for d in conn.execute(
                    "SELECT id, title, stage FROM deals WHERE initiative_id = ?", (init_id,)):
                    # Not twice: the task's own deal is already the first hop of
                    # the breadcrumb, and a duplicated ancestor reads as two
                    # different deals with the same name.
                    if deal_row is not None and d["id"] == deal_row["id"]:
                        continue
                    ancestors.append(_ref("deal", d["id"], d["title"], stage=d["stage"]))
        if epic:
            ancestors.append(_ref("epic", epic["id"], epic["title"], clickable=False))
        if task.sprint_id:
            srow = conn.execute(
                "SELECT id, name, status FROM sprints WHERE id = ?", (task.sprint_id,)).fetchone()
            if srow:
                ancestors.append(_ref("cycle", srow["id"], srow["name"], status=srow["status"]))

        # Children: linked child tasks, runs, comments.
        children = []
        for cid in db.get_task_links(task_id)["children"]:
            crow = conn.execute(
                "SELECT id, title, status FROM tasks WHERE id = ?", (cid,)).fetchone()
            if crow:
                children.append(_ref("task", crow["id"], crow["title"], status=crow["status"]))
    finally:
        conn.close()

    for r in db.get_task_runs(task_id, limit=10):
        children.append(_ref("run", r["id"], r.get("summary") or r.get("step_key") or f"run {r['id']}",
                             clickable=False, status=r.get("status"), outcome=r.get("outcome"),
                             step_key=r.get("step_key")))
    # Comments are rendered as a dedicated, editable section in the drawer
    # (fed by /api/tasks/{id}/comments), not as truncated child chips here.

    entity = {
        "type": "task", "id": task.id, "title": task.title, "status": task.status,
        "assignee": task.assignee, "project_id": task.project_id,
        # The commercial half of the lineage + WHERE IN THE CYCLE this task sits
        # (directiva ADICIÓN 9). `stage_kind` arrives already resolved from
        # `db._row_to_task` — explicit value if the row carries one, else the
        # rule — so the drawer never re-derives it and can never disagree with
        # the card's chip.
        "deal_id": getattr(task, "deal_id", None),
        "deal_title": getattr(task, "deal_title", None),
        "deal_stage": getattr(task, "deal_stage", None),
        "account_id": getattr(task, "account_id", None),
        "account_name": getattr(task, "account_name", None),
        "stage_kind": getattr(task, "stage_kind", None),
        "initiative_id": init_id, "sprint_id": task.sprint_id,
        "epic_id": task.epic_id, "session_id": task.session_id,
        # Full task detail so the drawer can show WHO did WHAT (previously the
        # entity was a thin id/title stub and this data never reached the UI).
        "body": task.body, "result": task.result, "contract_cmd": task.contract_cmd,
        "priority": task.priority, "due_date": task.due_date,
        "origin": task.origin, "owner": task.owner, "delegate": task.delegate,
        "progress_note": task.progress_note, "progress_pct": task.progress_pct,
        "current_step_key": task.current_step_key,
        "rejection_reason": task.rejection_reason,
        "consecutive_failures": task.consecutive_failures,
        "last_failure_error": task.last_failure_error,
        "created_at": task.created_at, "started_at": task.started_at,
        "completed_at": task.completed_at, "reviewed_at": task.reviewed_at,
    }
    # Verification ledger + status/audit events ride along so the drawer renders
    # them without extra round-trips (runs already arrive as `children`).
    ledger = db.get_task_ledger(task_id)
    events = db.get_task_events(task_id)
    return {"entity": entity, "ancestors": ancestors, "children": children,
            "ledger": ledger, "events": events}


# ------------------------------------------------------------------ project

def _project_row(conn, project_id) -> Optional[dict]:
    pid = db.resolve_project(project_id, conn)
    if not pid:
        return None
    # `status`/`delivered_at` land in m02_spine — read them only when they are
    # there, same forward-schema guard the brief composer uses, so this builder
    # keeps working on both sides of the migration.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    extra = ", status, delivered_at" if {"status", "delivered_at"} <= cols else ""
    if "account_id" in cols:
        extra += ", account_id"
    if "health" in cols:
        extra += ", health"
    row = conn.execute(
        f"SELECT id, slug, name, icon, kind{extra} FROM projects WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def _project_context(project_id: str) -> Optional[dict]:
    conn = db.get_conn()
    try:
        proj = _project_row(conn, project_id)
        if not proj:
            return None
        pid = proj["id"]
        children = []
        for i in conn.execute(
            "SELECT id, title, status FROM initiatives WHERE project_id = ? ORDER BY created_at", (pid,)):
            children.append(_ref("initiative", i["id"], i["title"], status=i["status"]))
        for s in conn.execute(
            "SELECT id, name, status FROM sprints WHERE project_id = ? ORDER BY created_at DESC", (pid,)):
            children.append(_ref("cycle", s["id"], s["name"], status=s["status"]))
        for t in conn.execute(
            "SELECT id, title, status FROM tasks WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT 100", (pid,)):
            children.append(_ref("task", t["id"], t["title"], status=t["status"]))
        # --- ancestors: the commercial lineage this project delivers.
        #
        # This used to be a hard-coded `[]` — the project was the ONLY entity in
        # the drawer with no breadcrumb, which is the same disguise the
        # contextChip removed from the card: a project reads as a container of
        # tasks when it is really *the delivery half of a client relationship*.
        # Two hops, both real FK joins that already existed (m02_spine):
        #   1. `projects.account_id` → the client. Top of the chain.
        #   2. `deals WHERE project_id = ?` → every deal this project delivers.
        #      Plural on purpose and verified against the live data — a real
        #      observed case: three deals delivered by one project, so a singular
        #      "the deal" would silently pick one and hide the rest.
        # Ordered client-first so the breadcrumb reads outside-in, like every
        # other entity's.
        ancestors = []
        account_id = proj.get("account_id")
        account_name = None
        deals = []
        delivered_value = 0.0
        currency = None
        if _has_column(conn, "projects", "account_id") and account_id:
            arow = conn.execute("SELECT id, name FROM accounts WHERE id = ?",
                                (account_id,)).fetchone()
            if arow:
                account_name = arow["name"]
                ancestors.append(_ref("account", arow["id"], arow["name"]))
        if _has_column(conn, "deals", "project_id"):
            for d in conn.execute(
                    "SELECT d.id, d.title, d.stage, d.value, d.currency, "
                    "       d.account_id, a.name AS account_name "
                    "FROM deals d LEFT JOIN accounts a ON a.id = d.account_id "
                    "WHERE d.project_id = ? ORDER BY d.created_at", (pid,)):
                ancestors.append(_ref("deal", d["id"], d["title"], stage=d["stage"],
                                      value=d["value"]))
                deals.append(dict(d))
                try:
                    delivered_value += float(d["value"] or 0)
                except (TypeError, ValueError):
                    pass
                currency = currency or d["currency"]
                # A project with no `account_id` of its own still has a client if
                # it delivers someone's deal — the join m02 made possible and the
                # backfill never ran. Read it rather than render "—".
                if not account_name and d["account_name"]:
                    account_name = d["account_name"]
                    account_id = account_id or d["account_id"]

        # `status` is what tells the drawer whether "✅ Entregado" still applies:
        # a delivered project must not be offered a verb it has already run.
        entity = {"type": "project", "id": pid, "title": proj["name"],
                  "slug": proj["slug"], "icon": proj["icon"], "kind": proj["kind"],
                  "status": proj.get("status"), "delivered_at": proj.get("delivered_at"),
                  "health": proj.get("health"),
                  # The commercial facts the drawer's Cliente / Valor entregado
                  # rows read. `delivered_value` is Σ of the linked deals — a
                  # DERIVED number (never typed), which is why it ships with the
                  # deals it was summed from rather than alone.
                  "account_id": account_id, "account_name": account_name,
                  "deals": deals, "deal_count": len(deals),
                  "delivered_value": delivered_value if deals else None,
                  "currency": currency}
        return {"entity": entity, "ancestors": ancestors, "children": children}
    finally:
        conn.close()


# --------------------------------------------------------------- initiative

def _initiative_context(initiative_id: str) -> Optional[dict]:
    init = strategy.get_initiative(initiative_id)
    if not init:
        return None
    prog = graph.initiative_progress(init)
    conn = db.get_conn()
    try:
        ancestors = []
        proj = _project_row(conn, init.get("project_id"))
        if proj:
            ancestors.append(_ref("project", proj["id"], proj["name"], icon=proj["icon"]))
        for d in conn.execute(
            "SELECT id, title, stage FROM deals WHERE initiative_id = ?", (initiative_id,)):
            ancestors.append(_ref("deal", d["id"], d["title"], stage=d["stage"]))

        children = []
        for e in conn.execute(
            "SELECT id, title, status FROM epics WHERE initiative_id = ? "
            "AND archived_at IS NULL ORDER BY created_at", (initiative_id,)):
            children.append(_ref("epic", e["id"], e["title"], clickable=False, status=e["status"]))
        # Task children must match the roll-up's scope exactly (P0-1), so the
        # drawer shows precisely what counts toward the %: attributed tasks for a
        # shared project, but the whole project for a sole initiative (project
        # fallback). `scope == 'project'` iff the fallback was used.
        if prog.get("scope") == "project" and proj:
            where, param = "project_id = ?", proj["id"]
        else:
            where, param = f"{graph._TASK_INITIATIVE} = ?", initiative_id
        for t in conn.execute(
            f"SELECT id, title, status FROM tasks WHERE {where} "
            "ORDER BY created_at DESC LIMIT 100", (param,)):
            children.append(_ref("task", t["id"], t["title"], status=t["status"]))
    finally:
        conn.close()
    entity = {"type": "initiative", "id": init["id"], "title": init["title"],
              "status": init.get("status"), "health": init.get("health"),
              "tier": init.get("tier"), "quarter": init.get("quarter"),
              "project_id": init.get("project_id"), "progress": prog}
    return {"entity": entity, "ancestors": ancestors, "children": children}


# --------------------------------------------------------------------- deal

def _deal_context(deal_id: str) -> Optional[dict]:
    deal = crm.get_deal(deal_id)
    if not deal:
        return None
    conn = db.get_conn()
    try:
        ancestors = []
        # The account is the top of the commercial chain and now has its own
        # context view, so the breadcrumb no longer dead-ends there. (A
        # breadcrumb that dead-ends teaches people not to click breadcrumbs.)
        # `contact` still has no context builder — left non-clickable rather than
        # rendered as a link that 400s.
        if deal.get("account_id"):
            a = conn.execute("SELECT id, name FROM accounts WHERE id = ?",
                             (deal["account_id"],)).fetchone()
            if a:
                ancestors.append(_ref("account", a["id"], a["name"]))
        if deal.get("contact_id"):
            c = conn.execute("SELECT id, name FROM contacts WHERE id = ?",
                             (deal["contact_id"],)).fetchone()
            if c:
                ancestors.append(_ref("contact", c["id"], c["name"], clickable=False))

        # Children: the initiative this deal funds, then its tasks (the spine).
        children = []
        iid = deal.get("initiative_id")
        if iid:
            irow = conn.execute(
                "SELECT id, title, status FROM initiatives WHERE id = ?", (iid,)).fetchone()
            if irow:
                children.append(_ref("initiative", irow["id"], irow["title"], status=irow["status"]))
                for t in conn.execute(
                    f"SELECT id, title, status FROM tasks WHERE {graph._TASK_INITIATIVE} = ? "
                    "ORDER BY created_at DESC LIMIT 50", (iid,)):
                    children.append(_ref("task", t["id"], t["title"], status=t["status"]))
        # The spine join: the project that DELIVERS this deal (deals.project_id).
        project_id = deal.get("project_id") if _has_column(conn, "deals", "project_id") else None
        if project_id:
            prow = _project_row(conn, project_id)
            if prow:
                children.append(_ref("project", prow["id"], prow["name"], icon=prow["icon"]))
        for ev in deal.get("events", [])[:10]:
            children.append(_ref("deal_event", None, f"{ev.get('kind')}", clickable=False,
                                 created_at=ev.get("created_at")))
        deliver = _deliver_options(conn, deal["id"], deal.get("account_id"), deal.get("account_name"))
    finally:
        conn.close()
    entity = {"type": "deal", "id": deal["id"], "title": deal["title"],
              "stage": deal.get("stage"), "value": deal.get("value"),
              "currency": deal.get("currency"), "account_id": deal.get("account_id"),
              "account_name": deal.get("account_name"),
              "project_id": project_id,
              "initiative_id": deal.get("initiative_id"),
              "lost_reason": deal.get("lost_reason"), "lost_notes": deal.get("lost_notes"),
              # ADICIÓN 8 — the money's tail. Both NULL until the operator taps
              # 💵/✅; the drawer offers each button exactly when it applies, the
              # same rule as "Deliver this" (never a disabled control the eye
              # has to learn to skip). `.get` because a pre-m11 DB has no such
              # columns and the drawer must still render.
              "invoiced_at": deal.get("invoiced_at"),
              "paid_at": deal.get("paid_at"),
              # m17 — the money's PLAN beside its two facts: the drawer needs
              # these to prefill "cobro esperado" on the 💵 tap, offer the
              # terms chips, and show the reconciliation delta. Same `.get`
              # convention: a pre-m17 DB renders them as null.
              "payment_terms_days": deal.get("payment_terms_days"),
              "expected_payment_date": deal.get("expected_payment_date"),
              "expected_payment_date_original":
                  deal.get("expected_payment_date_original"),
              # m18/m19 — the launch plan (🧾, own-action, light governance)
              # and the cash that actually landed. Same pre-migration .get
              # tolerance as every money field above.
              "expected_invoice_date": deal.get("expected_invoice_date"),
              "paid_amount": deal.get("paid_amount"),
              # Everything the "Deliver this" modal needs to open with a default
              # already chosen — so the drawer never makes a second round-trip
              # just to find out what to pre-select.
              "deliver": deliver}
    return {"entity": entity, "ancestors": ancestors, "children": children}


def _has_column(conn, table: str, column: str) -> bool:
    """Forward-schema guard (the dashboard/brief.py convention): a context view
    must still render on a DB where m02_spine has not run."""
    try:
        return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:  # pragma: no cover - defensive
        return False


def _deliver_options(conn, deal_id, account_id, account_name) -> dict:
    """Pre-resolve the "Which project delivers this?" picker.

    `default_project_id` is the client's existing delivering project when there
    is one — the picker is the escape hatch, not the path (red line 6), so the
    modal opens with a valid answer already selected and `new_project_name`
    prefilled for the create case. `projects` lists the account's projects
    first (`account: true`), then every other live project as the escape hatch.
    """
    out = {"account_id": account_id, "account_name": account_name,
           "new_project_name": account_name or "New project",
           "default_project_id": None, "proposal_workspace_path": None,
           "projects": []}
    if _has_column(conn, "commercial_proposal_packets", "workspace_path"):
        row = conn.execute(
            "SELECT p.workspace_path FROM commercial_proposal_packets p "
            "WHERE p.deal_id=? AND EXISTS (SELECT 1 FROM commercial_proposal_sends s WHERE s.packet_id=p.id) "
            "ORDER BY p.revision DESC LIMIT 1", (deal_id,)).fetchone()
        if row:
            out["proposal_workspace_path"] = row["workspace_path"]
    if not _has_column(conn, "projects", "account_id"):
        return out          # pre-m02 DB: the modal falls back to create-only
    mine, others = [], []
    account_ids = set()
    if account_id:
        # A project is "the client's" if it carries the account_id OR if it
        # already delivers another deal of the same account (the observed case:
        # three deals, one delivery — the second deal must find the first's
        # project, not mint a new one).
        sql = "SELECT id FROM projects WHERE account_id = ?"
        params = [account_id]
        if _has_column(conn, "deals", "project_id"):
            sql += (" UNION SELECT project_id FROM deals "
                    "WHERE account_id = ? AND project_id IS NOT NULL")
            params.append(account_id)
        account_ids = {r[0] for r in conn.execute(sql, params) if r[0]}
    for p in conn.execute(
            "SELECT id, name, status FROM projects WHERE archived_at IS NULL ORDER BY name"):
        ref = {"id": p["id"], "name": p["name"], "status": p["status"],
               "account": p["id"] in account_ids}
        (mine if ref["account"] else others).append(ref)
    out["projects"] = mine + others
    if mine:
        active = [p for p in mine if (p["status"] or "active") == "active"]
        out["default_project_id"] = (active or mine)[0]["id"]
    return out


# ------------------------------------------------------------------ account

def _account_context(account_id: str) -> Optional[dict]:
    """The top of the commercial chain — the client. Children are everything the
    relationship consists of: its deals, its contacts, and the projects that
    deliver for it. Reuses crm.account_chain (the existing lateral read) rather
    than inventing a second definition of "the account's deals"."""
    chain = crm.account_chain(account_id)
    if chain.get("status") == "error":
        return None
    acct = chain["account"]
    children = []
    for d in chain.get("deals", []):
        children.append(_ref("deal", d["id"], d["title"], stage=d.get("stage"),
                             value=d.get("value")))
    for c in chain.get("contacts", []):
        children.append(_ref("contact", c["id"], c["name"], clickable=False,
                             role=c.get("role")))
    conn = db.get_conn()
    try:
        # The delivering projects: bound by projects.account_id, plus any project
        # reached through this account's delivered deals (the join direction is
        # many-deals → one-project, so both paths can name the same project).
        pids = []
        if _has_column(conn, "projects", "account_id"):
            pids += [r[0] for r in conn.execute(
                "SELECT id FROM projects WHERE account_id = ?", (account_id,))]
        if _has_column(conn, "deals", "project_id"):
            pids += [r[0] for r in conn.execute(
                "SELECT DISTINCT project_id FROM deals "
                "WHERE account_id = ? AND project_id IS NOT NULL", (account_id,))]
        seen = set()
        for pid in pids:
            if not pid or pid in seen:
                continue
            seen.add(pid)
            prow = _project_row(conn, pid)
            if prow:
                children.append(_ref("project", prow["id"], prow["name"], icon=prow["icon"]))
    finally:
        conn.close()
    # m17-m19 — the client's money detail, derived per read (never stored):
    # facturado is the derived boolean invoiced_at IS NOT NULL summed as
    # value; cobrado reads COALESCE(paid_amount, value) — real cash.
    money = {"invoiced_value": 0.0, "collected_cash": 0.0,
             "pending_value": 0.0, "next_expected": None}
    conn = db.get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
        if {"invoiced_at", "paid_at"} <= cols:
            has_amount = "paid_amount" in cols
            has_exp = "expected_payment_date" in cols
            for d in conn.execute(
                    "SELECT * FROM deals WHERE account_id = ? "
                    "AND LOWER(COALESCE(stage,'')) = 'won'", (account_id,)):
                d = dict(d)
                value = float(d.get("value") or 0)
                if d.get("invoiced_at"):
                    money["invoiced_value"] += value
                if d.get("paid_at"):
                    cash = d.get("paid_amount") if has_amount else None
                    money["collected_cash"] += float(
                        cash if cash is not None else value)
                else:
                    money["pending_value"] += value
                    exp = d.get("expected_payment_date") if has_exp else None
                    if exp and (money["next_expected"] is None
                                or exp < money["next_expected"]):
                        money["next_expected"] = exp
    finally:
        conn.close()
    entity = {"type": "account", "id": acct["id"], "title": acct["name"],
              "domain": acct.get("domain"), "notes": acct.get("notes"),
              "open_value": chain.get("open_value"),
              "won_value": chain.get("won_value"),
              "deal_count": len(chain.get("deals", [])),
              "contact_count": len(chain.get("contacts", [])),
              "money": money}
    return {"entity": entity, "ancestors": [], "children": children}


# ------------------------------------------------------------------ session

def _session_context(session_id: str) -> Optional[dict]:
    meta = orch.get_session_meta(session_id)
    tasks = graph.tasks_for_session(session_id)
    if not meta and not tasks:
        return None  # neither a registered session nor any hard-linked task
    meta = meta or {}
    ancestors = []
    if meta.get("project"):
        conn = db.get_conn()
        try:
            proj = _project_row(conn, meta["project"])
        finally:
            conn.close()
        if proj:
            ancestors.append(_ref("project", proj["id"], proj["name"], icon=proj["icon"]))
    children = [_ref("task", t["id"], t["title"], status=t["status"]) for t in tasks]
    entity = {"type": "session", "id": session_id,
              "title": meta.get("feature") or session_id,
              "role": meta.get("role"), "feature": meta.get("feature"),
              "project": meta.get("project"), "host": meta.get("host"),
              "tag": meta.get("tag")}
    return {"entity": entity, "ancestors": ancestors, "children": children}
