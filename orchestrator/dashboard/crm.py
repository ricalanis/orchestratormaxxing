"""
Phase 6 — CRM entities (the top of the spine): Contact / Account / Deal.

The point is not a sales tool — it's closing the END-TO-END CHAIN the vision
names: a customer signal (Deal) traces down through the strategy it funds
(deals.initiative_id → initiative → epics → tasks → runs → commits), and
results roll back UP the same joins (a deal card shows the derived progress of
the work it depends on — never a typed number).

Stage flow (closed set, §L5 'Customer signal → Lead → … → Invoice'):
    lead → engaged → qualified → demo → proposal → won
                                                    ↘ lost
    (won and lost are sales-closed; **won is terminal** — a won deal stays won)

`delivered` is RETIRED as a writable stage (journey fase 1, ruling 2). It used
to sit after `won`, which made one column carry two different truths: whether
the money landed, and whether the work shipped. So a delivery deleted the
commercial fact — the deal left the pipeline the moment the project shipped, and
every won-rate, CAC denominator and forecast had to remember to say
`IN ('won','delivered')` or silently under-count. Delivery is a fact about the
PROJECT: `projects.status = 'delivered'` (single writer: `sprints.set_project_status`),
and "delivered" as a *read* derives from `deals.project_id → projects.status`.
The value survives in `STAGES` as legacy read vocabulary only — every write path
refuses it (`stage_retired`), and `m05_retire_delivered_stage` puts the same
refusal in the storage engine as a trigger.

Provenance of the signal itself: contacts and deals carry `source`
(linkedin|whatsapp|referral|event|website|other) — where the customer signal
entered, the CRM's own [src:] pointer.

Same doctrine as every other layer: one validated write path per entity, every
mutation an event (deal_events mirrors task_events/initiative_events), FKs
resolve or the write is refused.
"""
import json
import re
import subprocess
import sqlite3
import time
import uuid
import datetime
from pathlib import Path
from typing import Optional

from . import db
from . import object_graph
from . import graph_memory

STAGES = ("lead", "engaged", "qualified", "demo", "proposal", "stalled",
          "won", "delivered", "lost")

# The retired half of that tuple. `delivered` stays in STAGES because readers
# still recognise it (legacy vocabulary, vacuously correct at 0 rows) — it is
# removed from every WRITE path instead, which is the only removal that can be
# enforced. Ruling 2: the enum keeps the value as unreadable-for-writing until a
# later phase confirms 0 readers.
RETIRED_STAGES = ("delivered",)
WRITABLE_STAGES = tuple(s for s in STAGES if s not in RETIRED_STAGES)

_WON_OUTCOMES = ("won", "delivered")
_CLOSED = (*_WON_OUTCOMES, "lost")

# THE single source for "which stages are still in play". friday_prep,
# crm_proposals and growth_radar import these — a hand-rolled copy shipped
# naming 'discovery' (a stage that never existed) and silently excluded
# engaged/qualified/demo (caught by the design panel, 2026-08-10).
OPEN_STAGES = tuple(s for s in STAGES if s not in _CLOSED)  # incl. stalled
ACTIVE_PIPELINE_STAGES = tuple(s for s in OPEN_STAGES if s != "stalled")


def _stage_retired_error(stage: str) -> dict:
    """The typed refusal every deal write path returns for a retired stage.

    `code` is the machine-readable branch (the UI, the MCP surface and the tests
    all switch on it) and it names the replacement rather than only the ban: a
    refusal that does not say what to do instead gets worked around."""
    return {
        "status": "error",
        "code": "stage_retired",
        "error": (f"stage '{stage}' is retired — a won deal stays 'won'. "
                  "Delivery is projects.status='delivered' (deliver the deal "
                  "into a project, then mark the project delivered)."),
    }

# Win/loss categories. Deliberately short and mutually exclusive — a long list
# gets mis-picked and stops being countable. "other" always exists so a real
# loss is never forced into a wrong bucket (the detail goes in lost_notes).
LOST_REASONS = {
    "price":        "Price / budget",
    "timing":       "Bad timing",
    "competitor":   "Lost to competitor",
    "in_house":     "Did it in-house",
    "no_decision":  "No decision / went cold",
    "bad_fit":      "Not a fit",
    "no_response":  "Never responded",
    "other":        "Other",
}
_INACTIVE = ("stalled",)  # not closed, not active — icebox
SOURCES = ("linkedin", "whatsapp", "referral", "event", "website", "other")


def _now() -> int:
    return int(time.time())


def _gen(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _log(conn, deal_id: str, kind: str, payload: dict,
         source: Optional[str] = None, channel: Optional[str] = None) -> None:
    """THE writer for `deal_events` — now with provenance (m08).

    `source` names the WRITER (`web` · `cadence` · `agent` · `mcp` · `cli`) and
    `channel` the MEDIUM the interaction used (whatsapp · email · linkedin ·
    call · …). Both optional and both nullable in the schema: a caller that does
    not know is required to say so by omission rather than to guess, and NULL
    reads as "recorded before we asked" — which is exactly what the ~40k
    pre-m08 rows are.

    The column list is resolved from `PRAGMA table_info` per call, not
    hard-coded, because both entrypoints import this module BEFORE the runner
    has necessarily applied m08 (the MCP server does it in a bare try). Against
    a pre-m08 DB the two extra values are dropped and the row still lands —
    losing provenance is acceptable, losing the audit row is not.

    Best-effort by design (the bare `except`): an audit row must never be the
    reason a commercial write fails. Same contract as `sprints._log_event`.
    """
    try:
        cols = ["deal_id", "kind", "payload", "created_at"]
        vals = [deal_id, kind, json.dumps(payload), _now()]
        if source is not None or channel is not None:
            have = {r[1] for r in conn.execute("PRAGMA table_info(deal_events)")}
            if "source" in have:
                cols.append("source"); vals.append(source)
            if "channel" in have:
                cols.append("channel"); vals.append(channel)
        conn.execute(
            f"INSERT INTO deal_events ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", vals)
    except Exception:
        pass


def _close_cadence(conn, deal_id: str, *, reason: str) -> int:
    """Stop nurturing a deal, inside the CALLER's transaction (ruling 8 shape).

    Journey fase 1, step 5 — the "closed-deal hygiene" graft. Every pending
    nurture step becomes `skipped` and `deals.next_touch_date` is cleared. It
    does NOT touch the minted TASKS: `cadence.reconcile` closes those on its
    next pass by noticing the precondition vanished, and a task the operator is
    halfway through is not this function's to cancel.

    `skipped`, not deleted: `nurture_sequences` is the compliance ledger
    `get_cadence_status` reads, and a deleted step makes a deal that was
    diligently worked look like a deal with no cadence at all. It is also the
    only one of the three statuses (pending/sent/skipped) that says what
    happened — the touch was planned and deliberately not made.

    Receives the connection and does NOT commit — the caller's commit is the one
    that counts, which is the whole point of doing this in `update_deal`'s
    transaction rather than after it. Returns the number of steps closed.
    Best-effort against a pre-nurture schema (the table is created by the
    runner's legacy phase, which every entrypoint runs, but a hand-built fixture
    DB may not have it).
    """
    try:
        cur = conn.execute(
            "UPDATE nurture_sequences SET status = 'skipped' "
            "WHERE deal_id = ? AND status = 'pending'", (deal_id,))
        closed = cur.rowcount or 0
    except sqlite3.OperationalError:
        return 0
    conn.execute("UPDATE deals SET next_touch_date = NULL WHERE id = ?", (deal_id,))
    if closed:
        _log(conn, deal_id, "cadence_closed",
             {"steps_skipped": closed, "reason": reason}, source="web")
    return closed


def ensure_schema() -> None:
    """Idempotent CRM install: accounts / contacts / deals + deal_events."""
    conn = db.get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                domain     TEXT,
                notes      TEXT,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS contacts (
                id         TEXT PRIMARY KEY,
                account_id TEXT REFERENCES accounts(id),
                name       TEXT NOT NULL,
                email      TEXT,
                role       TEXT,
                notes      TEXT,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS deals (
                id            TEXT PRIMARY KEY,
                account_id    TEXT NOT NULL REFERENCES accounts(id),
                contact_id    TEXT REFERENCES contacts(id),
                title         TEXT NOT NULL,
                stage         TEXT NOT NULL DEFAULT 'lead',
                value         REAL,
                currency      TEXT DEFAULT 'MXN',
                initiative_id TEXT,          -- the STRATEGY JOIN (validated in code;
                                             -- initiatives lives in the same DB now)
                notes         TEXT,
                created_at    INTEGER,
                updated_at    INTEGER,
                closed_at     INTEGER
            );
            CREATE TABLE IF NOT EXISTS deal_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id    TEXT NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT,
                created_at INTEGER NOT NULL
            );
        """)
        # Additive column migration (ALTER TABLE only, never drops): contact
        # reachability + signal provenance (source) on contacts and deals.
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
        for col in ("phone", "whatsapp", "linkedin_url", "source", "source_notes"):
            if col not in ccols:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
        dcols = [r[1] for r in conn.execute("PRAGMA table_info(deals)").fetchall()]
        if "source" not in dcols:
            conn.execute("ALTER TABLE deals ADD COLUMN source TEXT")
        # Nested recurring deals: parent_deal_id + recurrence metadata.
        for col in ("parent_deal_id", "recurrence_type", "recurrence_interval", "display_order"):
            if col not in dcols:
                conn.execute(f"ALTER TABLE deals ADD COLUMN {col} TEXT")
        # Win/loss analysis: WHY a deal was lost. `lost_reason` is a CATEGORY
        # from LOST_REASONS (so losses are countable — free text alone can't be
        # reported on); `lost_notes` is the optional detail. Both cleared when a
        # deal leaves 'lost' (a stale reason on a live deal is a lie).
        for col in ("lost_reason", "lost_notes"):
            if col not in dcols:
                conn.execute(f"ALTER TABLE deals ADD COLUMN {col} TEXT")
        # P3 — performance indexes: deal_events audit spine + CRM FK lookups.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deal_events_deal "
            "ON deal_events(deal_id, created_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deal_events_created "
            "ON deal_events(created_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contacts_account "
            "ON contacts(account_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_account "
            "ON deals(account_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_contact "
            "ON deals(contact_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_stage "
            "ON deals(stage)")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- writes

def create_account(name: str, domain: str = "", notes: str = "") -> dict:
    if not (name or "").strip():
        return {"status": "error", "error": "name required"}
    conn = db.get_conn()
    try:
        existing = conn.execute("SELECT id FROM accounts WHERE name = ?", (name.strip(),)).fetchone()
        if existing:
            return {"status": "exists", "account_id": existing["id"]}
        aid = _gen("acct")
        conn.execute("INSERT INTO accounts (id, name, domain, notes, created_at) VALUES (?,?,?,?,?)",
                     (aid, name.strip(), domain or None, notes or None, _now()))
        conn.commit()
        return {"status": "created", "account_id": aid}
    finally:
        conn.close()


def create_contact(account_id: str, name: str, email: str = "",
                   role: str = "", notes: str = "", phone: str = "",
                   whatsapp: str = "", linkedin_url: str = "",
                   source: str = "", source_notes: str = "") -> dict:
    if not (name or "").strip():
        return {"status": "error", "error": "name required"}
    if source and source not in SOURCES:
        return {"status": "error", "error": f"source must be one of {SOURCES}"}
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            return {"status": "error", "error": f"account '{account_id}' not found"}
        cid = _gen("cont")
        conn.execute(
            "INSERT INTO contacts (id, account_id, name, email, role, notes, phone, "
            "whatsapp, linkedin_url, source, source_notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, account_id, name.strip(), email or None, role or None, notes or None,
             phone or None, whatsapp or None, linkedin_url or None,
             source or None, source_notes or None, _now()))
        conn.commit()
        return {"status": "created", "contact_id": cid}
    finally:
        conn.close()


def normalize_domain(raw: str) -> Optional[str]:
    """Reduce whatever an operator pastes to the bare lowercase host.

    `accounts.domain` is compared against the host of a meeting participant's
    address, so "https://Acme.com/", "ana@acme.com" and
    "acme.com" must all land on the same string. Returns None when the
    input carries no usable host — a domain that looks configured and never
    matches is worse than an empty one.
    """
    s = (raw or "").strip().lower()
    if not s:
        return None
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.rpartition("@")[2]
    s = s.split("/")[0].split("?")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    s = s.strip(".")
    # A real host: at least one dot, and labels of letters/digits/hyphens.
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s):
        return None
    return s


def update_account(account_id: str, name: Optional[str] = None,
                   domain: Optional[str] = None,
                   notes: Optional[str] = None) -> dict:
    """The one validated account write path (2026-08-10).

    `accounts.domain` had no writer at all, which is why 37 of 38 rows were
    empty and the Fireflies matcher — which resolves a deal's meetings from
    its contacts' emails OR its account's domain — could only ever match the
    handful of contacts with an email. One domain covers every contact of an
    account, so this is the leveraged half of that identity fix.

    `domain=""` clears it explicitly; a value that does not reduce to a host is
    REFUSED rather than stored (see normalize_domain).
    """
    conn = db.get_conn()
    try:
        prior = conn.execute("SELECT * FROM accounts WHERE id = ?",
                             (account_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "account not found"}
        sets, params = [], []
        if name is not None:
            t = name.strip()
            if not t:
                return {"status": "error", "error": "name cannot be empty"}
            sets.append("name = ?"); params.append(t)
        if domain is not None:
            if (domain or "").strip():
                host = normalize_domain(domain)
                if host is None:
                    return {"status": "error",
                            "error": f"'{domain}' is not a usable domain"}
                sets.append("domain = ?"); params.append(host)
            else:
                sets.append("domain = ?"); params.append(None)
        if notes is not None:
            sets.append("notes = ?"); params.append(notes or None)
        if not sets:
            return {"status": "error", "error": "nothing to update"}
        conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?",
                     (*params, account_id))
        conn.commit()
        return {"status": "updated", "account_id": account_id}
    finally:
        conn.close()


def update_contact(contact_id: str, name: Optional[str] = None,
                   email: Optional[str] = None, role: Optional[str] = None,
                   notes: Optional[str] = None, phone: Optional[str] = None,
                   whatsapp: Optional[str] = None, linkedin_url: Optional[str] = None,
                   source: Optional[str] = None, source_notes: Optional[str] = None,
                   account_id: Optional[str] = None) -> dict:
    """The one validated contact write path: inline-editable fields mirroring
    the deal-edit modal. FK validation on account_id reassignment; source
    validated against the closed set."""
    conn = db.get_conn()
    try:
        prior = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "contact not found"}
        sets, params = [], []
        if name is not None:
            t = name.strip()
            if not t:
                return {"status": "error", "error": "name cannot be empty"}
            sets.append("name = ?"); params.append(t)
        if account_id is not None and account_id != prior["account_id"]:
            if not conn.execute("SELECT 1 FROM accounts WHERE id = ?",
                                (account_id,)).fetchone():
                return {"status": "error", "error": f"account '{account_id}' not found"}
            sets.append("account_id = ?"); params.append(account_id)
        if email is not None:
            sets.append("email = ?"); params.append(email or None)
        if role is not None:
            sets.append("role = ?"); params.append(role or None)
        if notes is not None:
            sets.append("notes = ?"); params.append(notes or None)
        if phone is not None:
            sets.append("phone = ?"); params.append(phone or None)
        if whatsapp is not None:
            sets.append("whatsapp = ?"); params.append(whatsapp or None)
        if linkedin_url is not None:
            sets.append("linkedin_url = ?"); params.append(linkedin_url or None)
        if source is not None:
            if source and source not in SOURCES:
                return {"status": "error", "error": f"source must be one of {SOURCES}"}
            sets.append("source = ?"); params.append(source or None)
        if source_notes is not None:
            sets.append("source_notes = ?"); params.append(source_notes or None)
        if not sets:
            return {"status": "error", "error": "nothing to update"}
        conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?",
                     (*params, contact_id))
        conn.commit()
        return {"status": "updated", "contact_id": contact_id}
    finally:
        conn.close()


def _autoscore(deal_id: str) -> None:
    """Best-effort lead-score + readiness refresh after a deal write. Never
    raises — a scoring failure must not break the deal mutation (the scorers
    open their own connections, so this is called *after* the caller's conn
    is closed)."""
    try:
        from . import growth
        growth.score_deal(deal_id)
    except Exception:
        pass
    try:
        from . import readiness
        readiness.score_readiness(deal_id)
    except Exception:
        pass


def create_deal(account_id: str, title: str, stage: str = "lead",
                value: Optional[float] = None, currency: str = "MXN",
                contact_id: Optional[str] = None,
                initiative_id: Optional[str] = None, notes: str = "",
                source: str = "",
                parent_deal_id: Optional[str] = None,
                recurrence_type: Optional[str] = None,
                recurrence_interval: Optional[int] = None,
                product_id: Optional[str] = None,
                expected_close_date: Optional[str] = None) -> dict:
    """The customer-signal entry point. Both FK ends validated; landing with an
    initiative link closes the strategy join at birth; `source` records where
    the signal entered. `parent_deal_id` creates a sub-deal (recurrence of
    recurrence). `recurrence_type` is one of: one-shot, sprint, monthly,
    quarterly, custom."""
    if stage not in STAGES:
        return {"status": "error", "error": f"stage must be one of {STAGES}"}
    if stage in RETIRED_STAGES:
        # Not "unknown stage": the value IS known, and saying so is what stops
        # the caller from retrying it as a typo. `won` stays writable — the web
        # pipeline (and a deal recorded after the fact) both need it.
        return _stage_retired_error(stage)
    if source and source not in SOURCES:
        return {"status": "error", "error": f"source must be one of {SOURCES}"}
    if not (title or "").strip():
        return {"status": "error", "error": "title required"}
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            return {"status": "error", "error": f"account '{account_id}' not found"}
        if contact_id and not conn.execute(
                "SELECT 1 FROM contacts WHERE id = ?", (contact_id,)).fetchone():
            return {"status": "error", "error": f"contact '{contact_id}' not found"}
        if initiative_id and not conn.execute(
                "SELECT 1 FROM initiatives WHERE id = ?", (initiative_id,)).fetchone():
            return {"status": "error", "error": f"initiative '{initiative_id}' not found"}
        if parent_deal_id and not conn.execute(
                "SELECT 1 FROM deals WHERE id = ?", (parent_deal_id,)).fetchone():
            return {"status": "error", "error": f"parent deal '{parent_deal_id}' not found"}
        if product_id:
            try:
                if not conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
                    return {"status": "error", "error": f"product '{product_id}' not found"}
            except sqlite3.OperationalError:
                return {"status": "error", "error": f"product '{product_id}' not found"}
        did = _gen("deal")
        now = _now()
        conn.execute(
            "INSERT INTO deals (id, account_id, contact_id, title, stage, value, currency, "
            "initiative_id, notes, source, parent_deal_id, recurrence_type, recurrence_interval, "
            "product_id, expected_close_date, created_at, updated_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, account_id, contact_id, title.strip(), stage, value, currency,
             initiative_id, notes or None, source or None,
             parent_deal_id, recurrence_type, recurrence_interval, product_id or None,
             expected_close_date or None, now, now,
             now if stage in _CLOSED else None))
        _log(conn, did, "deal_created",
             {"stage": stage, "value": value, "initiative_id": initiative_id,
              "source": source or None, "product_id": product_id or None})
        conn.commit()
        result = {"status": "created", "deal_id": did}
    finally:
        conn.close()
    _autoscore(result["deal_id"])
    return result


def update_deal(deal_id: str, stage: Optional[str] = None,
                value: Optional[float] = None,
                initiative_id: Optional[str] = None,
                notes: Optional[str] = None,
                title: Optional[str] = None,
                account_id: Optional[str] = None,
                clear_initiative: bool = False,
                recurrence_type: Optional[str] = None,
                recurrence_interval: Optional[int] = None,
                parent_deal_id: Optional[str] = None,
                product_id: Optional[str] = None,
                clear_product: bool = False,
                expected_close_date: Optional[str] = None,
                lost_reason: Optional[str] = None,
                lost_notes: Optional[str] = None,
                payment_terms_days=None,
                expected_invoice_date: Optional[str] = None,
                paid_amount=None) -> dict:
    """The one validated deal write path: stage transitions evented
    (stage_changed; first terminal entry stamps closed_at), the initiative link validated
    both ends (deal_linked event — the strategy join is load-bearing). Title and
    account reassignment (FK-validated) are editable from the deal-edit modal."""
    conn = db.get_conn()
    try:
        prior = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if prior is None:
            return {"status": "error", "error": "deal not found"}
        sets, params = [], []
        now = _now()
        if stage is not None:
            if stage not in STAGES:
                return {"status": "error", "error": f"stage must be one of {STAGES}"}
            if stage in RETIRED_STAGES:
                # The drag-to-delivered path died with the column's second
                # meaning: the UI drops the deal into `won` and delivery is a
                # separate, human, project-shaped act.
                return _stage_retired_error(stage)
            sets.append("stage = ?"); params.append(stage)
            if stage in _CLOSED and not prior["closed_at"]:
                sets.append("closed_at = ?"); params.append(now)
            elif stage not in _CLOSED and prior["closed_at"]:
                sets.append("closed_at = NULL")   # reopened
            # Leaving 'lost' drops the loss reason: keeping it would attribute a
            # live (or won) deal to a loss category in every win/loss report.
            if stage != "lost":
                sets.append("lost_reason = NULL"); sets.append("lost_notes = NULL")
        if title is not None:
            t = title.strip()
            if not t:
                return {"status": "error", "error": "title cannot be empty"}
            sets.append("title = ?"); params.append(t)
        if account_id is not None and account_id != prior["account_id"]:
            if not conn.execute("SELECT 1 FROM accounts WHERE id = ?",
                                (account_id,)).fetchone():
                return {"status": "error", "error": f"account '{account_id}' not found"}
            sets.append("account_id = ?"); params.append(account_id)
        if value is not None:
            sets.append("value = ?"); params.append(value)
        if notes is not None:
            sets.append("notes = ?"); params.append(notes)
        if recurrence_type is not None:
            sets.append("recurrence_type = ?"); params.append(recurrence_type or None)
        if recurrence_interval is not None:
            sets.append("recurrence_interval = ?"); params.append(recurrence_interval or None)
        if parent_deal_id is not None:
            if parent_deal_id and not conn.execute("SELECT 1 FROM deals WHERE id = ?",
                                                    (parent_deal_id,)).fetchone():
                return {"status": "error", "error": f"parent deal '{parent_deal_id}' not found"}
            sets.append("parent_deal_id = ?"); params.append(parent_deal_id or None)
        if clear_initiative:
            sets.append("initiative_id = NULL")
        elif initiative_id is not None:
            if not conn.execute("SELECT 1 FROM initiatives WHERE id = ?",
                                (initiative_id,)).fetchone():
                return {"status": "error", "error": f"initiative '{initiative_id}' not found"}
            sets.append("initiative_id = ?"); params.append(initiative_id)
        if clear_product:
            sets.append("product_id = NULL")
        elif product_id is not None:
            if product_id:
                try:
                    if not conn.execute("SELECT 1 FROM products WHERE id = ?",
                                        (product_id,)).fetchone():
                        return {"status": "error", "error": f"product '{product_id}' not found"}
                except sqlite3.OperationalError:
                    return {"status": "error", "error": f"product '{product_id}' not found"}
            sets.append("product_id = ?"); params.append(product_id or None)
        if expected_close_date is not None:
            # "" clears the date (NULL); a non-empty value is stored as-is.
            sets.append("expected_close_date = ?"); params.append(expected_close_date or None)
        terms_change = None
        if payment_terms_days is not None:
            # m17: the credit terms agreed for THIS deal (0 = contado). "" clears.
            # The 30-day default lives only as a UI prefill suggestion — never
            # written by this function on its own.
            if "payment_terms_days" not in prior.keys():
                return {"status": "error",
                        "error": "deals.payment_terms_days is missing — the "
                                 "m17_cash_flow migration has not run"}
            if payment_terms_days == "":
                new_terms = None
            else:
                try:
                    new_terms = int(payment_terms_days)
                except (TypeError, ValueError):
                    return {"status": "error",
                            "error": "payment_terms_days must be an integer "
                                     "0–365, or '' to clear"}
                if isinstance(payment_terms_days, bool) or not 0 <= new_terms <= 365:
                    return {"status": "error",
                            "error": "payment_terms_days must be an integer "
                                     "0–365, or '' to clear"}
            if new_terms != prior["payment_terms_days"]:
                sets.append("payment_terms_days = ?"); params.append(new_terms)
                terms_change = (prior["payment_terms_days"], new_terms)
        amount_change = None
        if paid_amount is not None:
            # m19 correction path: the amount is captured at the ✅ tap; this
            # PATCH exists to FIX a typo afterwards, evented, and only while
            # the deal IS paid — a received amount on an unpaid deal is a
            # capture error, not a state.
            if "paid_amount" not in prior.keys():
                return {"status": "error",
                        "error": "deals.paid_amount is missing — the "
                                 "m19_paid_amount migration has not run"}
            if not prior["paid_at"]:
                return {"status": "error",
                        "error": "the deal is not paid — record the payment "
                                 "with the ✅ tap (it takes the amount) "
                                 "instead of patching an amount onto nothing"}
            if paid_amount == "":
                new_amount = None
            else:
                if isinstance(paid_amount, bool):
                    return {"status": "error",
                            "error": "paid_amount must be a number"}
                try:
                    new_amount = float(paid_amount)
                except (TypeError, ValueError):
                    return {"status": "error",
                            "error": "paid_amount must be a number, or '' to clear"}
                if new_amount <= 0:
                    return {"status": "error",
                            "error": "paid_amount must be a positive number"}
            if new_amount != prior["paid_amount"]:
                sets.append("paid_amount = ?"); params.append(new_amount)
                amount_change = (prior["paid_amount"], new_amount)
        launch_change = None
        if expected_invoice_date is not None:
            # m18: el LANZAMIENTO del cobro — plan de una acción PROPIA, así
            # que vive en el PATCH genérico (gobernanza ligera, evento sin
            # razón), a diferencia de expected_payment_date (promesa del
            # cliente, verbo auditado). La asimetría es deliberada.
            if "expected_invoice_date" not in prior.keys():
                return {"status": "error",
                        "error": "deals.expected_invoice_date is missing — the "
                                 "m18_invoice_launch migration has not run"}
            if expected_invoice_date == "":
                new_launch = None
            elif _valid_plan_date(expected_invoice_date):
                new_launch = expected_invoice_date
            else:
                return {"status": "error",
                        "error": "expected_invoice_date must be TEXT ISO "
                                 "'YYYY-MM-DD', or '' to clear"}
            if new_launch is not None and prior["invoiced_at"]:
                return {"status": "error",
                        "error": "already invoiced — the launch already "
                                 "happened, so planning it again is moot"}
            if new_launch != prior["expected_invoice_date"]:
                sets.append("expected_invoice_date = ?"); params.append(new_launch)
                launch_change = (prior["expected_invoice_date"], new_launch)
        # A reason only means anything on a lost deal: accept it when the deal IS
        # lost or is being moved there in this same call, and validate the
        # category so the field stays countable.
        lands_lost = stage == "lost" or (stage is None and prior["stage"] == "lost")
        if lost_reason is not None and lands_lost:
            if lost_reason and lost_reason not in LOST_REASONS:
                return {"status": "error",
                        "error": f"lost_reason must be one of {sorted(LOST_REASONS)}"}
            sets.append("lost_reason = ?"); params.append(lost_reason or None)
        if lost_notes is not None and lands_lost:
            sets.append("lost_notes = ?"); params.append((lost_notes or "").strip() or None)
        if not sets:
            return {"status": "error", "error": "nothing to update"}
        sets.append("updated_at = ?"); params.append(now)
        conn.execute(f"UPDATE deals SET {', '.join(sets)} WHERE id = ?", (*params, deal_id))
        if stage is not None and stage != prior["stage"]:
            payload = {"from": prior["stage"], "to": stage}
            if stage == "lost" and lost_reason:
                payload["lost_reason"] = lost_reason
                if lost_notes:
                    payload["lost_notes"] = (lost_notes or "").strip()[:500]
            _log(conn, deal_id, "stage_changed", payload, source="web")
            # --- closed-deal hygiene (journey fase 1, step 5) ----------------
            # A deal that lands on `won` or `lost` stops being nurtured. Without
            # this the step-5 materializer wakes up tomorrow and mints a
            # "Romper el hielo con …" card for each of the 12 lost deals on the
            # board — the nag that kills a cadence layer on its first morning.
            # Same transaction as the stage write on purpose: a rollback that
            # left the steps cancelled would silently drop a live deal's
            # cadence.
            if stage in _CLOSED:
                _close_cadence(conn, deal_id, reason=f"deal_{stage}")
        if clear_initiative and prior["initiative_id"]:
            _log(conn, deal_id, "deal_unlinked", {"was": prior["initiative_id"]})
        elif initiative_id is not None and initiative_id != prior["initiative_id"]:
            _log(conn, deal_id, "deal_linked", {"initiative_id": initiative_id})
        if clear_product and prior["product_id"]:
            _log(conn, deal_id, "deal_product_cleared", {"was": prior["product_id"]})
        elif product_id is not None and product_id != (prior["product_id"] or ""):
            _log(conn, deal_id, "deal_product_set", {"product_id": product_id})
        if terms_change is not None:
            _log(conn, deal_id, "payment_terms_set",
                 {"from": terms_change[0], "to": terms_change[1]}, source="web")
        if launch_change is not None:
            _log(conn, deal_id, "invoice_launch_planned",
                 {"from": launch_change[0], "to": launch_change[1]}, source="web")
        if amount_change is not None:
            _log(conn, deal_id, "paid_amount_set",
                 {"from": amount_change[0], "to": amount_change[1]}, source="web")
        conn.commit()
        result = {"status": "updated", "deal": get_deal(deal_id)}
    finally:
        conn.close()
    _autoscore(deal_id)
    return result


# ------------------------------------------- conversion verb: "Deliver this"
#
# Conversion verbs (advance stage · deliver this · mark delivered) are the three
# moments a lifecycle actually changes shape, and they are HUMAN-ONLY by design
# (spec §1, red line 11): agents create/comment/progress/complete tasks
# unattended, but they only ever PROPOSE a conversion into the brief — the operator
# taps. That is why this function is reachable through the dashboard API only
# and is deliberately NOT exposed as an MCP verb.
#
# "Deliver this" creates the spine join (deals.project_id) at the one moment a
# human is guaranteed to be paying attention — the moment a deal is won — which
# is what makes another orphaned won deal structurally impossible.

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "project"


def _unique_slug(conn, base: str) -> str:
    """projects.slug is NOT NULL UNIQUE — a colliding slug would make the
    create raise instead of delivering the deal, so disambiguate up front."""
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _deliver_error(code: str, message: str) -> dict:
    """Typed error: `code` is the machine-readable branch (the UI and the tests
    both switch on it), `error` the human string the API surfaces."""
    return {"status": "error", "code": code, "error": message}


def deliver_deal(deal_id: str, project_id: Optional[str] = None,
                 new_project_name: Optional[str] = None,
                 repo_path: Optional[str] = None) -> dict:
    """Join a WON deal to the project that delivers it.

    Writes `deals.project_id`, `projects.status='active'` and
    `projects.account_id`, and logs a `delivered_link` deal_event.

    Direction is many-deals → one-project on purpose (spec §1): one client may fund a
    single delivery with three deals, so `projects.deal_id` would invent three
    projects. Delivering a second deal of the same account into the same project
    is therefore the normal case, not a conflict.

    Idempotent: a deal that already carries a project_id returns
    `already_delivered` with the existing link rather than re-linking or raising.

    Exactly one of `project_id` / `new_project_name` is required. The API stays
    explicit here *because* the modal is the thing that defaults it (picker
    pre-selected to the account's existing active project, else prefilled with
    the account name) — a silent server-side default would quietly mint a second
    project for an account that already has one, which is the exact failure the
    join direction exists to prevent.

    `repo_path` is optional, matching the general project-creation contract.
    When the deal came through a proposal workspace the modal prefills that
    path; otherwise delivery creates the project record without inventing or
    creating a directory. A supplied path is still validated before any write.
    """
    from . import sprints
    conn = db.get_conn()
    try:
        if "project_id" not in {r[1] for r in conn.execute("PRAGMA table_info(deals)")}:
            return _deliver_error(
                "spine_missing",
                "deals.project_id is missing — the m02_spine migration has not run")
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            return _deliver_error("not_found", "deal not found")
        if deal["stage"] != "won":
            return _deliver_error(
                "not_won",
                f"only a won deal can be delivered (this one is '{deal['stage']}')")
        if deal["project_id"]:
            return {"status": "already_delivered", "deal_id": deal_id,
                    "project_id": deal["project_id"]}
        # m17/F3: delivering is the natural moment to invoice — the response
        # says so and the drawer offers '💵 Facturar ahora'. A hint, never a
        # write: the invoice stamp stays a separate human tap.
        uninvoiced = ("invoiced_at" not in deal.keys()
                      or not deal["invoiced_at"])

        # Resolve the target BEFORE creating anything, so a rejected call never
        # leaves a stray project behind.
        target, target_name, slug = None, None, None
        if project_id:
            proj = conn.execute(
                "SELECT id, name, archived_at FROM projects WHERE id = ?",
                (project_id,)).fetchone()
            if proj is None:
                return _deliver_error("project_not_found", f"project '{project_id}' not found")
            if proj["archived_at"]:
                return _deliver_error(
                    "project_archived",
                    f"project '{project_id}' is archived — unarchive it or create a new one")
            target, target_name = proj["id"], proj["name"]
        elif (new_project_name or "").strip():
            target_name = new_project_name.strip()
            slug = _unique_slug(conn, _slugify(target_name))
            raw_path = str(repo_path or "").strip()
            repo_path = None
            if raw_path:
                candidate = Path(raw_path).expanduser()
                if not candidate.is_absolute() or not candidate.is_dir():
                    return _deliver_error(
                        "repo_path_invalid", "repo_path must be an existing absolute directory")
                repo_path = str(candidate.resolve())
        else:
            return _deliver_error(
                "project_required",
                "pass project_id (deliver into an existing project) or "
                "new_project_name (create one)")
        account_id = deal["account_id"]
    finally:
        conn.close()

    created = False
    if target is None:
        res = sprints.create_project(target_name, slug, repo_path=repo_path)
        target = res.get("id")
        if not target:
            return _deliver_error("project_create_failed",
                                  res.get("error", "could not create the delivering project"))
        created = True

    conn = db.get_conn()
    try:
        # `AND project_id IS NULL` makes the idempotency guard atomic rather than
        # advisory: a concurrent deliver of the same deal loses the UPDATE and
        # gets the same already_delivered answer instead of overwriting the link.
        cur = conn.execute(
            "UPDATE deals SET project_id = ?, updated_at = ? "
            "WHERE id = ? AND project_id IS NULL", (target, _now(), deal_id))
        if cur.rowcount == 0:
            row = conn.execute("SELECT project_id FROM deals WHERE id = ?",
                               (deal_id,)).fetchone()
            conn.rollback()
            return {"status": "already_delivered", "deal_id": deal_id,
                    "project_id": row["project_id"] if row else None}
        # The lifecycle half goes through the SINGLE writer (ruling 8), which
        # receives this transaction rather than opening its own — a project that
        # claimed to be active while the deal→project link rolled back would be
        # exactly the split-brain the single-writer rule exists to prevent.
        # Unchanged semantics: a won deal joining a project means there is work
        # in it, so the project is `active`.
        sprints.set_project_status(conn, target, "active", via="deliver_deal")
        # COALESCE, not an overwrite: this verb owns the deal→project join, not
        # account administration. Re-pointing an existing project at a different
        # account would silently rewrite someone else's delivery history. The
        # account column is NOT the lifecycle writer's — it owns status and
        # delivered_at, nothing else.
        conn.execute(
            "UPDATE projects SET account_id = COALESCE(account_id, ?) WHERE id = ?",
            (account_id, target))
        _log(conn, deal_id, "delivered_link",
             {"project_id": target, "project_name": target_name,
              "created_project": created, "account_id": account_id, "suggest_invoice": uninvoiced})
        conn.commit()
    finally:
        conn.close()
    return {"status": "delivered", "deal_id": deal_id, "project_id": target,
            "project_name": target_name, "created_project": created,
            "account_id": account_id, "suggest_invoice": uninvoiced}


# ------------------------------------------ conversion verb: "Mark delivered"
#
# Verb 3/3, and the reason the delivery leg had no terminal state: a project
# could enter `active` (deliver_deal writes it) and never leave. The declared
# lifecycle is planned|active|delivered|archived; before this the live DB had
# exactly two of those values and 0 delivered projects, so
# `projects.delivered_at` — shipped in m02_spine — had never been written by
# anything. Human-only, exactly like the other two verbs.

def _iso_now() -> str:
    """`projects.delivered_at` is a TEXT column (m02_spine), so it gets an
    ISO-8601 local timestamp WITH its offset rather than an epoch integer
    smuggled into a text column. Kept beside the verb because `deliver_deal`'s
    events read from the same clock; the delivery stamp itself is written by
    `sprints.set_project_status` (the single writer, same format)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def mark_project_delivered(project_id: str) -> dict:
    """Close the delivery leg: `projects.status = 'delivered'` + `delivered_at`.

    **This verb never touches `deals.stage`** (ruling 2, the CRITICAL-2 fix). It
    used to move every won deal into a `delivered` stage, and that was one
    column carrying two independent truths — did the money land, did the work
    ship. The consequences were not theoretical: a delivered deal left the `won`
    column, so every won-rate/CAC/forecast reader had to remember
    `IN ('won','delivered')` or under-count, and the deal's commercial identity
    was overwritten by an operational event that happened weeks later.

    A won deal stays `won` forever. Delivery is a fact about the PROJECT, and
    every "delivered" READ derives from it: `deals.project_id → projects.status`
    (see `pipeline()`'s history bucket). The status write goes through
    `sprints.set_project_status` — the single lifecycle writer (ruling 8),
    handed THIS transaction so the status and the audit rows commit together.

    Typed outcomes, same vocabulary as `deliver_deal`:
      * `not_found`          — no such project
      * `already_delivered`  — status is already 'delivered' (idempotent: the
                               second call changes nothing and events nothing)
      * `spine_missing`      — m02_spine has not run

    Only `stage = 'won'` deals are named in the result and evented. A deal that
    is still open, stalled or lost is not part of this delivery: the project
    shipping does not mean the money landed.

    AUDIT: there is no `project_events` table in this schema (the event tables
    are task_events / deal_events / initiative_events / session_events), so the
    audit row is ONE `project_delivered` deal_event per covered deal, naming the
    project and the date. The `stage_changed won→delivered` row it used to write
    beside it is gone with the write it described — an event for a transition
    that no longer happens is a lie the brief's 💰 Money block would read. A
    delivery with no won deals therefore leaves no event; `projects.delivered_at`
    is the record in that case.
    """
    from . import sprints
    conn = db.get_conn()
    try:
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        dcols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
        if not {"status", "delivered_at"} <= pcols or "project_id" not in dcols:
            return _deliver_error(
                "spine_missing",
                "projects.status/delivered_at or deals.project_id is missing — "
                "the m02_spine migration has not run")
        proj = conn.execute(
            "SELECT id, name, status, delivered_at FROM projects WHERE id = ?",
            (project_id,)).fetchone()
        if proj is None:
            return _deliver_error("not_found", "project not found")
        if proj["status"] == "delivered":
            return {"status": "already_delivered", "project_id": project_id,
                    "project_name": proj["name"], "delivered_at": proj["delivered_at"],
                    "delivered_deals": [],
                    "uncovered_open_deals": _uncovered_open_deals(conn, project_id)}

        # The deals this delivery COVERS — named and evented, never rewritten.
        # Still `stage = 'won'` only: the read is the same, what changed is that
        # nothing follows it into another stage.
        covered = [{"id": r["id"], "title": r["title"], "value": r["value"]}
                   for r in conn.execute(
                       "SELECT id, title, value FROM deals "
                       "WHERE project_id = ? AND stage = 'won' ORDER BY value DESC",
                       (project_id,)).fetchall()]

        # Single writer, caller's transaction (ruling 8). It owns the status and
        # the `delivered_at` stamp — including the COALESCE that keeps the FIRST
        # delivery date — so this verb no longer computes either.
        res = sprints.set_project_status(conn, project_id, "delivered",
                                         via="mark_project_delivered")
        delivered_at = res["delivered_at"]
        for d in covered:
            _log(conn, d["id"], "project_delivered",
                 {"project_id": project_id, "project_name": proj["name"],
                  "delivered_at": delivered_at})
        # Read while the connection is still open — the finally below closes it
        # before the return statement at the bottom of this function evaluates.
        uncovered = _uncovered_open_deals(conn, project_id)
        conn.commit()
    finally:
        conn.close()
    return {"status": "delivered", "project_id": project_id,
            "project_name": proj["name"], "delivered_at": delivered_at,
            "delivered_deals": covered,
            "uncovered_open_deals": uncovered}


# ------------------------------------------- delivery drift: report, never repair
#
# The three conversion verbs (won → deliver_deal → mark_project_delivered) are
# HUMAN-ONLY by design, and verb 3 deliberately never touches `deals.stage`
# (ruling 2 / CRITICAL-2). That separation is correct, but it left a hole: a
# project can be marked delivered while the deal that funds it is still sitting
# open in the pipeline, because verb 3 selects `WHERE project_id = ? AND
# stage = 'won'` and finds zero rows when the deal was never linked (verb 2 was
# skipped). Observed 2026-08-17: a $100K deal in `stalled` while its initiative's
# project was already `delivered`. The verb returned `delivered_deals: []` and
# surfaced nothing.
#
# `delivery_drift()` is the read-only coherence check that was missing. It
# REPORTS two drift shapes; it never repairs them — repair is a human tap on
# the verbs above, by design. `mark_project_delivered` additionally echoes the
# open deals reachable through the project's initiatives on its own response,
# so the next silent pass is at least loud in its own return value.

def _uncovered_open_deals(conn, project_id: str) -> list:
    """Open deals reachable via the project's initiatives.

    "Open" is `stage NOT IN ('won','lost')` — stalled COUNTS as open here,
    because a stalled deal on a delivered project is exactly the drift this
    surfaces. "Reachable" is `deals.initiative_id → initiatives.project_id`,
    the indirect path that exists when verb 2 (`deals.project_id`) was never
    set. Read-only: this is a warning, not a write.

    Used by `mark_project_delivered` (additive `uncovered_open_deals` key) and
    by `delivery_drift` (the `candidate_deals` of a `delivered_project_no_won_deal`
    row). Same read, two callers — kept here so the predicate is in one place.
    """
    icols = {r[1] for r in conn.execute("PRAGMA table_info(initiatives)")}
    if "project_id" not in icols:
        return []
    rows = conn.execute(
        "SELECT d.id AS id, d.title AS title, d.stage AS stage, "
        "       d.value AS value, d.currency AS currency "
        "FROM deals d JOIN initiatives i ON i.id = d.initiative_id "
        "WHERE i.project_id = ? AND d.stage NOT IN ('won','lost') "
        "ORDER BY d.value DESC", (project_id,)).fetchall()
    return [{"id": r["id"], "title": r["title"], "stage": r["stage"],
             "value": r["value"], "currency": r["currency"]} for r in rows]


def delivery_drift() -> dict:
    """Report delivery/coherence drift between the project and deal spines.

    A pure READ — it performs zero writes (no row-mutating SQL anywhere in its
    body; drift is reported, never repaired). Repair is a human tap on the
    conversion verbs (`deliver_deal` to link a won deal, `mark_project_delivered`
    to close the leg), by design (spec red line 11).

    Two drift shapes, both consequences of the deliberate separation between
    "the money landed" (`deals.stage = 'won'`) and "the work shipped"
    (`projects.status = 'delivered'`):

      * `delivered_project_no_won_deal` — a project is `delivered` but has NO
        deal with `deals.project_id = <project> AND stage = 'won'`. The project
        shipped without a closed sale on its own spine. Each row carries the
        deals reachable via `initiatives.project_id` as `candidate_deals`, so a
        human can see what was likely meant to fund it. A project with NO
        commercial evidence at all — no `account_id` and no deal on either
        path — is skipped: internal and personal projects ship without a
        sale by definition, and flagging them is noise that costs the
        check its credibility.

      * `open_deal_on_delivered_project` — a deal whose `stage NOT IN
        ('won','lost')` is reachable (via `initiatives.project_id`) from a
        project that is already `delivered`. The work shipped while the sale is
        still open in the pipeline. Stalled counts as open here: a stalled deal
        on a delivered project is the exact silent pass this exists to catch.

    Archived projects (`archived_at IS NOT NULL`) are excluded from both. A
    missing spine (`projects.status`, `deals.project_id`, or the
    `initiatives` table) degrades to an empty drift report with
    `spine_missing: True` rather than raising — same forward-schema guard
    pattern `mark_project_delivered` and `deliver_deal` already use.
    """
    conn = db.get_conn()
    try:
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        dcols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
        icols = {r[1] for r in conn.execute("PRAGMA table_info(initiatives)")}
        if (not {"status", "delivered_at", "archived_at"} <= pcols
                or "project_id" not in dcols
                or "project_id" not in icols):
            return {"checked_at": _iso_now(), "drift": [],
                    "counts": {"delivered_project_no_won_deal": 0,
                               "open_deal_on_delivered_project": 0, "total": 0},
                    "ok": True, "spine_missing": True}

        drift = []

        # 1) Delivered projects with no linked won deal on their own spine.
        delivered = conn.execute(
            "SELECT id, name, delivered_at, account_id FROM projects "
            "WHERE status = 'delivered' AND archived_at IS NULL "
            "ORDER BY delivered_at DESC").fetchall()
        for p in delivered:
            won_count = conn.execute(
                "SELECT COUNT(*) FROM deals "
                "WHERE project_id = ? AND stage = 'won'", (p["id"],)).fetchone()[0]
            if won_count > 0:
                continue
            candidates = _uncovered_open_deals(conn, p["id"])
            # A project that was never commercial is not drift. Legacy
            # personal projects are `delivered` with no account
            # and no deal anywhere on their spine — flagging them buried
            # the one real case under five false ones on the first live
            # run (2026-08-18), which is precisely how a check earns the
            # right to be ignored. Drift needs SOME commercial evidence:
            # an account on the project, or a deal reachable from it.
            any_deal = conn.execute(
                "SELECT 1 FROM deals d JOIN initiatives i "
                "  ON i.id = d.initiative_id WHERE i.project_id = ? "
                "UNION ALL SELECT 1 FROM deals WHERE project_id = ? "
                "LIMIT 1", (p["id"], p["id"])).fetchone() is not None
            if not any_deal and not p["account_id"]:
                continue
            drift.append({
                "kind": "delivered_project_no_won_deal",
                "project_id": p["id"], "project_name": p["name"],
                "delivered_at": p["delivered_at"],
                "linked_won_deals": 0,
                "candidate_deals": candidates,
            })

        # 2) Open deals reachable from an already-delivered project.
        open_rows = conn.execute(
            "SELECT d.id AS deal_id, d.title AS deal_title, d.stage AS stage, "
            "       d.value AS value, d.currency AS currency, "
            "       p.id AS project_id, p.name AS project_name, "
            "       p.delivered_at AS delivered_at "
            "FROM deals d "
            "JOIN initiatives i ON i.id = d.initiative_id "
            "JOIN projects p ON p.id = i.project_id "
            "WHERE d.stage NOT IN ('won','lost') "
            "  AND p.status = 'delivered' AND p.archived_at IS NULL "
            "ORDER BY d.value DESC").fetchall()
        for r in open_rows:
            drift.append({
                "kind": "open_deal_on_delivered_project",
                "deal_id": r["deal_id"], "deal_title": r["deal_title"],
                "stage": r["stage"], "value": r["value"],
                "currency": r["currency"],
                "project_id": r["project_id"], "project_name": r["project_name"],
                "delivered_at": r["delivered_at"],
            })

        c1 = sum(1 for d in drift if d["kind"] == "delivered_project_no_won_deal")
        c2 = sum(1 for d in drift if d["kind"] == "open_deal_on_delivered_project")
        counts = {"delivered_project_no_won_deal": c1,
                  "open_deal_on_delivered_project": c2,
                  "total": c1 + c2}
        return {"checked_at": _iso_now(), "drift": drift, "counts": counts,
                "ok": counts["total"] == 0}
    finally:
        conn.close()


# ------------------------------------ conversion verbs 4 & 5: the money's tail
#
# Directiva ADICIÓN 8 — "después del cierre del proyecto está la FACTURACIÓN Y
# COBRANZA como último paso". Two more HUMAN-ONLY verbs, same doctrine as the
# other three (spec red line 11): an agent may propose "hay que facturar a
# Acme" into the brief or mint the card, but only the operator asserts that an
# invoice was issued or that money arrived. That is why both live here, are
# reachable through the dashboard API only, and are deliberately absent from
# `mcp_server.py` — the absence IS the guard, so do not add MCP parity.
#
# Both are idempotent and both are one-way: `invoiced_at` / `paid_at` are
# stamped once (COALESCE, like `projects.delivered_at`) and never rewritten by
# a second tap. Un-invoicing is a correction, not a verb — it would need its own
# audited path, and inventing one now would give the two-tap gesture a way to
# silently erase a financial fact.

def _billing_error(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "error": message}


# The three m17 cash-plan columns. Selected into the money verbs' deal row only
# when they exist, so both verbs keep working against a pre-m17 DB.
_PLAN_COLS = ("payment_terms_days", "expected_payment_date",
              "expected_payment_date_original", "expected_invoice_date",
              "paid_amount")


def _valid_plan_date(s) -> bool:
    """A plan date is a civil calendar date: TEXT ISO 'YYYY-MM-DD', exactly.

    Rejects ints (an epoch in a plan-date field is the m11 convention mix this
    schema forbids), datetimes, and anything `date.fromisoformat` won't take.
    """
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        datetime.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _mark_money(deal_id: str, column: str, *, event: str, require: Optional[str],
                require_error: tuple, after=None) -> dict:
    """Shared body of `mark_deal_invoiced` / `mark_deal_paid`.

    One function because the two verbs differ in exactly three literals (the
    column, the event kind, and the precondition) and duplicating the guard
    chain is how the second one drifts from the first. `require` names a column
    that must already be set — `paid_at` requires `invoiced_at`, because money
    arriving against an invoice that was never issued is a data-entry mistake,
    not a state.

    `after(conn, deal, now)` runs INSIDE the transaction, after the stamp and
    before the event/commit — the hook point for the m17 cash-plan side
    effects (derive the expected date on invoice, compute the reconciliation
    delta on paid). It returns `{"event": {...}, "response": {...}}`; both
    merges are additive and the hook must never commit or close.
    """
    conn = db.get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
        if column not in cols:
            return _billing_error(
                "billing_missing",
                f"deals.{column} is missing — the m11_billing migration has not run")
        plan_cols = [c for c in _PLAN_COLS if c in cols]
        deal = conn.execute(
            "SELECT id, title, stage, value, currency, account_id, project_id, "
            "invoiced_at, paid_at"
            + "".join(f", {c}" for c in plan_cols)
            + " FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            return _billing_error("not_found", "deal not found")
        if deal["stage"] != "won":
            return _billing_error(
                "not_won",
                f"only a won deal can be invoiced or collected (this one is "
                f"'{deal['stage']}')")
        if require and not deal[require]:
            return _billing_error(*require_error)
        if deal[column]:
            return {"status": "already_" + event, "deal_id": deal_id,
                    column: deal[column]}
        now = _now()
        conn.execute(
            f"UPDATE deals SET {column} = COALESCE({column}, ?), updated_at = ? "
            f"WHERE id = ?", (now, now, deal_id))
        extra = after(conn, dict(deal), now) if after else {}
        _log(conn, deal_id, event,
             {"value": deal["value"], "currency": deal["currency"],
              "project_id": deal["project_id"], **extra.get("event", {})},
             source="web")
        conn.commit()
    finally:
        conn.close()
    return {"status": event, "deal_id": deal_id, column: now,
            **extra.get("response", {})}


def mark_deal_invoiced(deal_id: str,
                       expected_payment_date: Optional[str] = None) -> dict:
    """💵 Facturado — the invoice went out. Human-only, idempotent.

    Requires `stage = 'won'`: an invoice against an open deal is a quote, and
    the queue this feeds ("won + delivered + not invoiced") would be wrong the
    moment it accepted one. Stamps `deals.invoiced_at` and logs a
    `deal_invoiced` event with `source='web'`.

    m17 — the invoice tap is the moment of maximum context for the cash PLAN,
    so it also materializes `expected_payment_date` when it is still NULL:
    the explicit date if the drawer sent one, else `date(invoiced) + terms`
    when `payment_terms_days` is set. COALESCE-style — a manual date is never
    overwritten (the response says `expected_kept`), and an invalid explicit
    date refuses the WHOLE verb before anything is stamped (`bad_date`), so a
    typo can't half-land. First set also anchors
    `expected_payment_date_original` and logs `payment_promised`.

    Side effect the operator sees: `cadence.reconcile` closes the open
    "Facturar …" card on its next pass (its precondition vanished) and later
    mints the cobranza card against the promised date if `paid_at` is NULL.
    """
    if expected_payment_date is not None and not _valid_plan_date(expected_payment_date):
        return _billing_error(
            "bad_date",
            "expected_payment_date must be TEXT ISO 'YYYY-MM-DD' — refused "
            "before the invoice stamp so a typo can't half-land")

    def _after(conn, deal, now):
        if "expected_payment_date" not in deal:
            return {}                      # pre-m17 DB: the verb works as before
        if deal.get("expected_payment_date"):
            return {"response": {"expected_kept": deal["expected_payment_date"]}}
        promised = expected_payment_date
        derived = False
        terms = deal.get("payment_terms_days")
        if promised is None and terms is not None:
            promised = (datetime.date.fromtimestamp(now)
                        + datetime.timedelta(days=int(terms))).isoformat()
            derived = True
        if promised is None:
            return {}                      # no date, no terms: NULL stays honest
        conn.execute(
            "UPDATE deals SET expected_payment_date = ?, "
            "expected_payment_date_original = "
            "COALESCE(expected_payment_date_original, ?) WHERE id = ?",
            (promised, promised, deal["id"]))
        _log(conn, deal["id"], "payment_promised",
             {"to": promised, "derived": derived, "terms": terms}, source="web")
        return {"response": {"expected_payment_date": promised,
                             "expected_derived": derived}}

    return _mark_money(deal_id, "invoiced_at", event="deal_invoiced",
                       require=None, require_error=(), after=_after)


def mark_deal_paid(deal_id: str, paid_amount=None) -> dict:
    """✅ Pagado — the money landed. Human-only, idempotent.

    Requires `invoiced_at` (typed `not_invoiced`): paid-but-never-invoiced is a
    missing tap, not a state, and accepting it would leave a deal that can never
    appear in the "por facturar" queue again.

    Closing the loop is deliberate and immediate rather than left to the next
    reconcile: `cadence.close_deal_tasks` cancels the deal's open cobranza card
    in the SAME transaction as the stamp, so the card is gone the instant the
    drawer closes instead of surviving until the next materializer pass.
    """
    if paid_amount is not None:
        if isinstance(paid_amount, bool):
            return _billing_error("bad_amount",
                                  "paid_amount must be a number, not a boolean")
        try:
            paid_amount = float(paid_amount)
        except (TypeError, ValueError):
            return _billing_error("bad_amount",
                                  "paid_amount must be a number (MXN, neto)")
        if paid_amount <= 0:
            return _billing_error("bad_amount",
                                  "paid_amount must be a positive number")

    def _after(conn, deal, now):
        # m17 reconciliation: the delta is COMPUTED here and carried in the
        # event payload + response — never a stored column. `paid_at` against
        # the promise IS the reconciliation; overwriting the plan with the
        # fact would destroy the slippage signal.
        paid_date = datetime.date.fromtimestamp(now)
        extra = {}
        # m19: the money that actually landed rides the same tap. Written
        # only when the column exists (pre-m19 DBs keep working) and only
        # the value given — NULL keeps meaning "= value".
        if paid_amount is not None and "paid_amount" in deal:
            conn.execute("UPDATE deals SET paid_amount = ? WHERE id = ?",
                         (paid_amount, deal["id"]))
            extra["paid_amount"] = paid_amount
            if deal.get("value") is not None:
                diff = round(paid_amount - float(deal["value"]), 2)
                if diff:
                    extra["amount_diff"] = diff
        for src, key in (("expected_payment_date", "delta_days"),
                         ("expected_payment_date_original", "delta_original_days")):
            v = deal.get(src)
            if v and _valid_plan_date(v):
                extra[key] = (paid_date - datetime.date.fromisoformat(v)).days
        if not extra:
            return {}
        return {"event": extra, "response": extra}

    res = _mark_money(deal_id, "paid_at", event="deal_paid",
                      require="invoiced_at",
                      require_error=("not_invoiced",
                                     "mark the deal invoiced first — a payment "
                                     "against an invoice that was never issued "
                                     "is a missing tap, not a state"),
                      after=_after)
    if res.get("status") == "deal_paid":
        # Local import: `cadence` imports `crm`, so a module-level import here
        # would be a cycle. Best-effort — the card is also closed by the next
        # reconcile, so a failure here costs freshness, never correctness.
        try:
            from . import cadence
            conn = db.get_conn()
            try:
                closed = cadence.close_deal_tasks(
                    conn, deal_id, kinds=("collect", "invoice"), reason="deal_paid")
                conn.commit()
            finally:
                conn.close()
            res["tasks_closed"] = closed
        except Exception:  # pragma: no cover - defensive
            pass
    return res


def set_payment_promise(deal_id: str, expected_payment_date: str,
                        reason: Optional[str] = None) -> dict:
    """The ONE audited write path for `expected_payment_date` (m17).

    The date is a plan, not a fact — so unlike `invoiced_at`/`paid_at` it is
    editable, but ONLY through here, and every movement leaves a trail:

    - first set (prior NULL) → `payment_promised {to}`, and the write-once
      `expected_payment_date_original` anchors via COALESCE;
    - any later change → `reason` is mandatory (`reason_required`) and the
      move logs `payment_repromised {from, to, reason}` — kicking the date
      down the road leaves evidence instead of erasing it, which is what
      keeps the month's standing honest;
    - after `paid_at` is stamped the plan FREEZES (`already_paid`): the pair
      plan/fact is the reconciliation record, and editing the plan post-hoc
      would fabricate punctuality.

    The generic PATCH rejects this field loudly (api.py) so no unaudited
    side door exists. Human-only like every money verb: absent from
    `mcp_server.py` — the absence IS the guard, do not add MCP parity.
    """
    if not _valid_plan_date(expected_payment_date):
        return _billing_error(
            "bad_date",
            "expected_payment_date must be TEXT ISO 'YYYY-MM-DD' (a plan date "
            "is a civil calendar date — an epoch here is the convention mix "
            "m11 forbids)")
    conn = db.get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
        if "expected_payment_date" not in cols:
            return _billing_error(
                "billing_missing",
                "deals.expected_payment_date is missing — the m17_cash_flow "
                "migration has not run")
        deal = conn.execute(
            "SELECT id, stage, paid_at, expected_payment_date, "
            "expected_payment_date_original FROM deals WHERE id = ?",
            (deal_id,)).fetchone()
        if deal is None:
            return _billing_error("not_found", "deal not found")
        if deal["stage"] != "won":
            return _billing_error(
                "not_won",
                f"only a won deal carries a payment promise (this one is "
                f"'{deal['stage']}')")
        if deal["paid_at"]:
            return _billing_error(
                "already_paid",
                "the deal is paid — the promise is frozen: plan vs. paid_at "
                "IS the reconciliation record, and editing the plan after the "
                "money landed would fabricate punctuality")
        prior = deal["expected_payment_date"]
        if prior == expected_payment_date:
            return {"status": "unchanged", "deal_id": deal_id,
                    "expected_payment_date": prior}
        now = _now()
        if prior is None:
            conn.execute(
                "UPDATE deals SET expected_payment_date = ?, "
                "expected_payment_date_original = "
                "COALESCE(expected_payment_date_original, ?), updated_at = ? "
                "WHERE id = ?",
                (expected_payment_date, expected_payment_date, now, deal_id))
            _log(conn, deal_id, "payment_promised",
                 {"to": expected_payment_date}, source="web")
            status = "payment_promised"
        else:
            if not (reason or "").strip():
                return _billing_error(
                    "reason_required",
                    "moving a promised date needs a reason — the repromise "
                    "trail is what keeps the standing honest")
            conn.execute(
                "UPDATE deals SET expected_payment_date = ?, updated_at = ? "
                "WHERE id = ?", (expected_payment_date, now, deal_id))
            _log(conn, deal_id, "payment_repromised",
                 {"from": prior, "to": expected_payment_date,
                  "reason": (reason or "").strip()[:500]}, source="web")
            status = "payment_repromised"
        conn.commit()
    finally:
        conn.close()
    return {"status": status, "deal_id": deal_id,
            "expected_payment_date": expected_payment_date}


# Spanish month labels for the standing line — fixed, deterministic, no locale.
_MES = ("ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
        "JUL", "AGO", "SEP", "OCT", "NOV", "DIC")

# Slippage (median delta of reconciled collections) stays silent below this
# many data points: with n=1–2 the "trend" is one anecdote wearing a number,
# and a block that teaches the operator to distrust it dies (the m11 lesson).
_SLIPPAGE_MIN = 3


def _paid_date(row) -> Optional[datetime.date]:
    ts = row.get("paid_at")
    return datetime.date.fromtimestamp(ts) if ts else None


def _expected_date(row) -> Optional[datetime.date]:
    v = row.get("expected_payment_date")
    if v and _valid_plan_date(v):
        return datetime.date.fromisoformat(v)
    return None


def _launch_date(row) -> Optional[datetime.date]:
    v = row.get("expected_invoice_date")
    if v and _valid_plan_date(v):
        return datetime.date.fromisoformat(v)
    return None


def _cash_of(row) -> float:
    """Received cash reads COALESCE(paid_amount, value) — the agreed figure is
    the default truth, the real deposit corrects it when recorded (m19)."""
    v = row.get("paid_amount")
    if v is None:
        v = row.get("value")
    return float(v or 0)


def cash_flow(date: Optional[str] = None) -> dict:
    """The ONE deterministic read behind the Today 💰 Cobro block.

    Pure SQL + arithmetic — zero LLM, zero randomness; `date` is injectable
    ('YYYY-MM-DD') so contracts freeze the clock. Definitions, decided once:

    - WEEK  = won deals whose `expected_payment_date` falls in [Mon..Sun] of
      `date` and are unpaid, plus the deals PAID inside that window (the ✅
      rows). `days_late` is measured against the promised date.
    - MONTH = COBRADO: SUM(value) of won deals paid inside `date`'s month;
      ESPERADO: cobrado + SUM(value) of won deals expected inside the month
      and still unpaid. Weighted pipeline is deliberately OUT — proposal ×
      probability contaminates a cash view. `target` is the ICP's
      `target_revenue` when configured (the bar's denominator), else None —
      no invented percentage.
    - FUGAS = the HONEST query: won + `invoiced_at IS NULL` (delivered is a
      badge via the project join, never a filter — the canonical m11 queue
      requires `delivered` and returns $0 today while $200k sleeps in won
      deals with no project). Plus the blind spots: won-unpaid with no
      expected date, and won with no project. Each leak carries a first
      deal_id so the UI can deep-link straight into a drawer.
    - SLIPPAGE = median of (paid − promised) days across reconciled deals,
      reported only at n ≥ 3 (see `_SLIPPAGE_MIN`).
    - NARRATIVE = ONE deterministic line by severity ranking:
      vencido > ciego > slippage > sano. Templates, never a model.

    Amounts are `deals.value` — the operator's NET (pre-IVA) figure by his own
    convention; taxes vary per client and live outside this view.
    """
    try:
        today = (datetime.date.fromisoformat(date) if date
                 else datetime.date.today())
    except ValueError:
        return {"status": "error", "code": "bad_date",
                "error": "date must be 'YYYY-MM-DD'"}
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)

    conn = db.get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
        has_m17 = "expected_payment_date" in cols
        has_m11 = "invoiced_at" in cols
        rows = [dict(r) for r in conn.execute(
            "SELECT d.*, a.name AS account_name, p.status AS project_status "
            "FROM deals d "
            "LEFT JOIN accounts a ON a.id = d.account_id "
            "LEFT JOIN projects p ON p.id = d.project_id "
            "WHERE LOWER(COALESCE(d.stage, '')) = 'won'")]
        target = None
        try:
            t = conn.execute(
                "SELECT value FROM icp_config WHERE key = 'target_revenue'"
            ).fetchone()
            if t and t[0] is not None:
                target = float(t[0])
        except (sqlite3.OperationalError, ValueError, TypeError):
            target = None
    finally:
        conn.close()

    def slim(r, kind="payment", date=None, **extra):
        exp = _expected_date(r)
        return {"deal_id": r["id"], "title": r.get("title"),
                "account_name": r.get("account_name"),
                "value": float(r.get("value") or 0),
                "currency": r.get("currency") or "MXN",
                # `kind` is what the calendar distinguishes: 'launch' = 🧾 the
                # operator's own collection launch; 'payment' = 💰 the client's
                # money. `date` is the row's calendar date, whichever plan it
                # came from.
                "kind": kind,
                "date": date,
                "expected_payment_date": exp.isoformat() if exp else None,
                "invoiced": bool(has_m11 and r.get("invoiced_at")),
                "paid": bool(has_m11 and r.get("paid_at")),
                "delivered": (r.get("project_status") or "") == "delivered",
                **extra}

    week_rows, overdue, no_expected, launch_overdue = [], [], [], []
    month_collected = month_invoiced = month_expected_pending = 0.0
    deltas = []
    for r in rows:
        paid_on = _paid_date(r) if has_m11 else None
        exp = _expected_date(r) if has_m17 else None
        value = float(r.get("value") or 0)
        inv_ts = r.get("invoiced_at") if has_m11 else None
        if inv_ts:
            inv_on = datetime.date.fromtimestamp(inv_ts)
            if inv_on.year == today.year and inv_on.month == today.month:
                month_invoiced += value          # facturado del mes (pactado)
        if paid_on:
            cash = _cash_of(r)
            if exp:
                deltas.append((paid_on - exp).days)
            if paid_on.year == today.year and paid_on.month == today.month:
                month_collected += cash          # efectivo real (m19)
            if monday <= paid_on <= sunday:
                week_rows.append(slim(r, date=paid_on.isoformat(), days_late=0,
                                      paid_on=paid_on.isoformat(), cash=cash))
            continue
        # Unpaid won from here down. The launch plan (🧾) is independent of
        # the payment plan and only meaningful while uninvoiced.
        if not r.get("invoiced_at"):
            launch = _launch_date(r)
            if launch is not None:
                l_late = (today - launch).days
                if monday <= launch <= sunday:
                    week_rows.append(slim(r, kind="launch",
                                          date=launch.isoformat(),
                                          days_late=max(0, l_late)))
                if l_late >= 1:
                    launch_overdue.append(slim(r, kind="launch",
                                               date=launch.isoformat(),
                                               days_late=l_late))
        if exp is None:
            no_expected.append(slim(r, days_late=0))
            continue
        late = (today - exp).days
        if exp.year == today.year and exp.month == today.month:
            month_expected_pending += value
        if monday <= exp <= sunday:
            week_rows.append(slim(r, date=exp.isoformat(),
                                  days_late=max(0, late)))
        if late >= 1:
            overdue.append(slim(r, date=exp.isoformat(), days_late=late))

    overdue.sort(key=lambda x: -x["days_late"])
    launch_overdue.sort(key=lambda x: -x["days_late"])
    week_rows.sort(key=lambda x: (x["paid"], x["date"] or ""))
    uninvoiced = [r for r in rows
                  if not (has_m11 and r.get("invoiced_at"))
                  and not (has_m11 and r.get("paid_at"))]
    no_project = [r for r in rows if not r.get("project_id")]
    leaks = {
        "uninvoiced_count": len(uninvoiced),
        "uninvoiced_value": sum(float(r.get("value") or 0) for r in uninvoiced),
        "no_expected_count": len(no_expected),
        "no_project_count": len(no_project),
        "launch_overdue_count": len(launch_overdue),
        "first_uninvoiced_deal_id": uninvoiced[0]["id"] if uninvoiced else None,
        "first_no_expected_deal_id":
            no_expected[0]["deal_id"] if no_expected else None,
        "first_no_project_deal_id": no_project[0]["id"] if no_project else None,
        "first_launch_overdue_deal_id":
            launch_overdue[0]["deal_id"] if launch_overdue else None,
    }
    slippage = None
    if len(deltas) >= _SLIPPAGE_MIN:
        s = sorted(deltas)
        mid = len(s) // 2
        median = (s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2)
        slippage = {"median_days": median, "count": len(deltas)}

    label = _MES[today.month - 1]
    if overdue:
        worst = overdue[0]
        narrative = {"severity": "overdue",
                     "text": f"🔴 {worst['title']} venció hace "
                             f"{worst['days_late']}d — "
                             f"${worst['value']:,.0f} sin caer"}
    elif launch_overdue:
        worst = launch_overdue[0]
        narrative = {"severity": "launch",
                     "text": f"🧾 {worst['title']}: el lanzamiento de cobro "
                             f"se pasó hace {worst['days_late']}d — la "
                             f"factura no ha salido"}
    elif leaks["uninvoiced_count"] or leaks["no_expected_count"]:
        if leaks["uninvoiced_count"]:
            narrative = {"severity": "blind",
                         "text": f"⚠ {leaks['uninvoiced_count']} won sin "
                                 f"facturar — "
                                 f"${leaks['uninvoiced_value']:,.0f} sin reloj "
                                 f"de cobro"}
        else:
            narrative = {"severity": "blind",
                         "text": f"⚠ {leaks['no_expected_count']} won sin "
                                 f"fecha esperada — el forecast está ciego"}
    elif slippage and abs(slippage["median_days"]) > 7:
        narrative = {"severity": "slippage",
                     "text": f"cobros llegan {slippage['median_days']:+.0f}d "
                             f"vs promesa (mediana de {slippage['count']})"}
    else:
        narrative = {"severity": "healthy",
                     "text": f"Al día · {label}: "
                             f"${month_collected:,.0f} cobrado"}

    return {
        "status": "ok",
        "date": today.isoformat(),
        "week": {"start": monday.isoformat(), "end": sunday.isoformat(),
                 # Money pending only: a 🧾 launch is an action on the
                 # calendar, never incoming cash.
                 "total": sum(r["value"] for r in week_rows
                              if r["kind"] == "payment" and not r["paid"]),
                 "rows": week_rows},
        "month": {"label": label, "collected": month_collected,
                  "invoiced": month_invoiced,
                  "expected": month_collected + month_expected_pending,
                  "target": target},
        "overdue": overdue,
        "launch_overdue": launch_overdue,
        "no_expected": no_expected,
        "leaks": leaks,
        "slippage": slippage,
        "narrative": narrative,
    }


def loss_reasons(days: Optional[int] = None) -> dict:
    """Win/loss breakdown: lost deals grouped by category, with count and value.
    `days` windows on closed_at (None = all time). Uncategorised losses are
    reported as their own bucket rather than dropped — an honest denominator is
    the whole point of a loss report."""
    conn = db.get_conn()
    try:
        where, params = "stage = 'lost'", []
        if days:
            where += " AND closed_at >= ?"
            params.append(_now() - int(days) * 86400)
        rows = conn.execute(
            f"SELECT COALESCE(lost_reason, '') AS reason, COUNT(*) AS count, "
            f"COALESCE(SUM(value), 0) AS value FROM deals WHERE {where} "
            f"GROUP BY COALESCE(lost_reason, '') ORDER BY count DESC", params).fetchall()
    finally:
        conn.close()
    buckets = [{"reason": r["reason"] or None,
                "label": LOST_REASONS.get(r["reason"], "Uncategorised"),
                "count": r["count"], "value": r["value"]} for r in rows]
    total = sum(b["count"] for b in buckets)
    return {"days": days, "total": total,
            "categorised": sum(b["count"] for b in buckets if b["reason"]),
            "buckets": buckets, "vocabulary": LOST_REASONS}


# ---------------------------------------------------------------- reads

def list_accounts() -> list:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM contacts c WHERE c.account_id = a.id) AS contacts, "
            "(SELECT COUNT(*) FROM deals d WHERE d.account_id = a.id) AS deals "
            "FROM accounts a ORDER BY a.name").fetchall()]
    finally:
        conn.close()


def list_contacts(account_id: Optional[str] = None) -> list:
    conn = db.get_conn()
    try:
        sql = ("SELECT c.*, a.name AS account_name FROM contacts c "
               "JOIN accounts a ON a.id = c.account_id ")
        params = []
        if account_id:
            sql += "WHERE c.account_id = ? "
            params.append(account_id)
        sql += "ORDER BY c.name"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def account_chain(account_id: str) -> dict:
    """The LATERAL view (audit gap): Account → its contacts → its deals (each
    with initiative link + stage). get_deal_chain goes DOWN the spine; this
    answers 'what is our whole relationship with this account?'."""
    conn = db.get_conn()
    try:
        acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acct:
            return {"status": "error", "error": "account not found"}
        contacts = [dict(r) for r in conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? ORDER BY name", (account_id,))]
        deals = [dict(r) for r in conn.execute(
            "SELECT d.*, i.title AS initiative_title, p.name AS product_name, p.track AS product_track FROM deals d "
            "LEFT JOIN initiatives i ON i.id = d.initiative_id "
            "LEFT JOIN products p ON p.id = d.product_id "
            "WHERE d.account_id = ? ORDER BY d.updated_at DESC", (account_id,))]
        open_value = sum(d.get("value") or 0 for d in deals if d["stage"] not in _CLOSED and d["stage"] not in _INACTIVE)
        return {"account": dict(acct), "contacts": contacts, "deals": deals,
                "open_value": open_value,
                "won_value": sum(d.get("value") or 0 for d in deals
                                 if d["stage"] in _WON_OUTCOMES)}
    finally:
        conn.close()


def add_deal_event(deal_id: str, kind: str, note: str = "",
                   agent: Optional[str] = None) -> dict:
    """Free-form commercial interaction log (audit gap): 'called client',
    'sent proposal', 'demo held' — the deal's history between stage changes."""
    kind = (kind or "").strip()
    if not kind:
        return {"status": "error", "error": "kind required (e.g. call, meeting, email, note)"}
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
            return {"status": "error", "error": "deal not found"}
        _log(conn, deal_id, kind, {"note": note, "agent": agent, "via": "add_deal_event"})
        conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (_now(), deal_id))
        conn.commit()
        return {"status": "ok", "deal_id": deal_id, "kind": kind}
    finally:
        conn.close()


def quick_add_contact(name: str, company: str = "", email: str = "",
                      phone: str = "", whatsapp: str = "",
                      linkedin_url: str = "", source: str = "",
                      notes: str = "") -> dict:
    """The fastest capture path: name + company → account + contact + deal.
    Idempotent on account name; creates a lead-stage deal so the contact is
    immediately part of the pipeline. Growth attributes (loop, ladder) are
    applied by the caller or the API wrapper."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "error": "name required"}
    if source and source not in SOURCES:
        return {"status": "error", "error": f"source must be one of {SOURCES}"}
    company = (company or "").strip() or name

    acct = create_account(company)
    account_id = acct.get("account_id")
    if not account_id:
        return {"status": "error", "error": acct.get("error", "account create failed")}

    contact = create_contact(
        account_id, name, email=email, phone=phone, whatsapp=whatsapp,
        linkedin_url=linkedin_url, source=source, source_notes=notes)
    contact_id = contact.get("contact_id")
    if not contact_id:
        return {"status": "error", "error": contact.get("error", "contact create failed")}

    title = f"{company} — {name}" if company != name else name
    deal = create_deal(account_id, title, stage="lead", contact_id=contact_id,
                       notes=notes, source=source)
    deal_id = deal.get("deal_id")
    if not deal_id:
        return {"status": "error", "error": deal.get("error", "deal create failed")}

    # Best-effort growth ladder/loop default + score + nurture (local import avoids cycle).
    try:
        from . import growth as _growth
        _growth.update_deal_growth(deal_id, value_ladder_stage="iman")
        _growth.score_deal(deal_id)
        _growth.generate_nurture(deal_id)
    except Exception:
        pass

    return {
        "status": "created",
        "account_id": account_id,
        "contact_id": contact_id,
        "deal_id": deal_id,
    }


def get_cadence_status(deal_id: str) -> dict:
    """Per-deal nurture cadence: steps, next due date, and compliance %
    (sent within ±2 days of scheduled_date among elapsed steps)."""
    deal = get_deal(deal_id)
    if deal is None:
        return {"status": "error", "error": "deal not found"}
    db.ensure_nurture_schema()
    steps = db.nurture_for_deal(deal_id)
    today = datetime.date.today()

    total = len(steps)
    elapsed = [s for s in steps if s.get("scheduled_date") and
               datetime.date.fromisoformat(s["scheduled_date"]) <= today]
    compliant = 0
    for s in elapsed:
        if s.get("status") == "sent" and s.get("sent_at"):
            try:
                sd = datetime.date.fromisoformat(s["scheduled_date"])
                sent = datetime.date.fromisoformat(str(s["sent_at"])[:10])
                if abs((sent - sd).days) <= 2:
                    compliant += 1
            except Exception:
                pass
    compliance = round(compliant / len(elapsed), 2) if elapsed else None
    pending = [s for s in steps if s.get("status") == "pending"]
    next_due = None
    for s in sorted(pending, key=lambda x: x.get("scheduled_date") or ""):
        if s.get("scheduled_date"):
            next_due = s["scheduled_date"]
            break
    overdue = [s for s in pending if s.get("scheduled_date") and
               datetime.date.fromisoformat(s["scheduled_date"]) < today]

    return {
        "status": "ok",
        "deal_id": deal_id,
        "deal_title": deal.get("title"),
        "total_steps": total,
        "elapsed_steps": len(elapsed),
        "compliance": compliance,
        "next_due_date": next_due,
        "overdue_count": len(overdue),
        "overdue_steps": overdue,
        "steps": steps,
    }


def list_deal_events(deal_id: str, limit: int = 50) -> dict:
    """Read a deal's commercial-interaction history (the deal_events spine: stage
    changes, touches, growth updates, free-form notes), newest first. The history
    existed in the DB but had no reader over MCP — this closes that gap."""
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
            return {"status": "error", "error": "deal not found"}
        rows = conn.execute(
            "SELECT kind, payload, created_at FROM deal_events WHERE deal_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (deal_id, max(1, min(int(limit or 50), 500)))).fetchall()
        events = []
        for r in rows:
            e = dict(r)
            e["payload"] = _json_or_none(e.get("payload"))
            events.append(e)
        return {"status": "ok", "deal_id": deal_id, "count": len(events), "events": events}
    finally:
        conn.close()


def delete_deal(deal_id: str) -> dict:
    """Guarded hard-delete of a deal (mistake/duplicate/test). REFUSES won outcomes —
    closed revenue is history (reopen or archive, don't erase). Sub-deals must be
    handled first. Cleans sidecar rows (events, nurture, scoring features)."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT stage, title FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "deal not found"}
        if row["stage"] in _WON_OUTCOMES:
            return {"status": "error",
                    "error": "refusing to delete a won/delivered deal — closed revenue is history "
                             "(reopen or archive instead)"}
        if conn.execute("SELECT 1 FROM deals WHERE parent_deal_id = ?",
                        (deal_id,)).fetchone():
            return {"status": "error",
                    "error": "deal has sub-deals — delete or reparent them first"}
        conn.execute("DELETE FROM deal_events WHERE deal_id = ?", (deal_id,))
        for tbl, col in (("nurture_sequences", "deal_id"),
                         ("lead_scoring_features", "lead_id")):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (deal_id,))
            except sqlite3.OperationalError:
                pass  # table absent in older schemas — nothing to clean
        conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        conn.commit()
        return {"status": "deleted", "deal_id": deal_id, "title": row["title"]}
    finally:
        conn.close()


def list_deals(stage: Optional[str] = None) -> list:
    conn = db.get_conn()
    try:
        sql = ("SELECT d.*, a.name AS account_name, c.name AS contact_name, "
               "c.source AS contact_source, "
               "i.title AS initiative_title, i.quarter AS initiative_quarter, "
               "p.name AS product_name, p.track AS product_track, "
               "(SELECT COUNT(*) FROM deals sub WHERE sub.parent_deal_id = d.id) AS child_count "
               "FROM deals d JOIN accounts a ON a.id = d.account_id "
               "LEFT JOIN contacts c ON c.id = d.contact_id "
               "LEFT JOIN initiatives i ON i.id = d.initiative_id "
               "LEFT JOIN products p ON p.id = d.product_id ")
        params = []
        if stage:
            sql += "WHERE d.stage = ? "
            params.append(stage)
        sql += "ORDER BY d.updated_at DESC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for d in rows:
            d["lead_score_details"] = _json_or_none(d.get("lead_score_details"))
            d["readiness_dimensions"] = _json_or_none(d.get("readiness_dimensions"))
        return rows
    finally:
        conn.close()


def list_deal_children(parent_deal_id: str) -> list:
    """Return all sub-deals of a parent deal, ordered by display_order then created_at."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT d.*, a.name AS account_name "
            "FROM deals d JOIN accounts a ON a.id = d.account_id "
            "WHERE d.parent_deal_id = ? ORDER BY COALESCE(d.display_order, '0'), d.created_at",
            (parent_deal_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_deal(deal_id: str) -> Optional[dict]:
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT d.*, a.name AS account_name, c.name AS contact_name, "
            "p.name AS product_name, p.track AS product_track "
            "FROM deals d JOIN accounts a ON a.id = d.account_id "
            "LEFT JOIN contacts c ON c.id = d.contact_id "
            "LEFT JOIN products p ON p.id = d.product_id WHERE d.id = ?",
            (deal_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["events"] = [dict(r) for r in conn.execute(
            "SELECT kind, payload, created_at FROM deal_events WHERE deal_id = ? "
            "ORDER BY created_at DESC LIMIT 30", (deal_id,)).fetchall()]
        # Lead-scoring + readiness details stored as JSON on the deal row.
        d["lead_score_details"] = _json_or_none(d.get("lead_score_details"))
        d["readiness_dimensions"] = _json_or_none(d.get("readiness_dimensions"))
        d["client_profile"] = d.get("client_profile")
        d["fireflies_signals"] = get_deal_fireflies_latest(deal_id)
        return d
    finally:
        conn.close()


def get_deal_fireflies_latest(deal_id: str) -> Optional[dict]:
    """Return latest Fireflies signals dict for a deal, or None if none stored."""
    from . import fireflies as _ff
    return _ff.latest_signals_for_deal(deal_id)


def _json_or_none(val):
    if not val:
        return None
    try:
        return json.loads(val)
    except Exception:
        return None


def get_deal_fireflies(deal_id: str) -> list:
    """Return cached Fireflies meeting records (with signals) for a deal."""
    return db.fireflies_meetings_for_deal(deal_id)


# ---------------------------------------------------------------- the chain

# Convention (documented, ratcheted): a commit that ships a task references the
# task id in its message — `git log --grep=<task_id>` is the deterministic
# Task→Commit hop (t_c7ab4210 set the precedent).
def _commits_for_task(task_id: str, limit: int = 5) -> list:
    try:
        out = subprocess.run(
            ["git", "-C", str(graph_memory.REPO_DIR), "log", f"--grep={task_id}",
             f"-{limit}", "--pretty=format:%h%x1f%s"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 2:
            commits.append({"sha": parts[0], "subject": parts[1]})
    return commits


def link_task_deal(task_id: str, deal_id: str) -> dict:
    """Writer 1 of the three that may set `tasks.deal_id` (ruling 5).

    The named verb behind `POST /api/tasks` (optional `deal_id`) and
    `PATCH /api/tasks/{id}/deal`. Deliberately NOT reachable from the generic
    `PATCH /api/tasks/{id}`: a lineage edge is not an inline field edit, and a
    generic patch body is exactly where an agent would set it by accident. The
    third writer is step 5's cadence materializer, which writes inside its own
    transaction and calls this only for the audit shape, never for the update.

    Validates BOTH ids before writing, because `tasks.deal_id` carries no
    foreign key (the column is added by ALTER TABLE, and SQLite cannot add a
    REFERENCES clause with an enforced FK afterwards). An unvalidated write
    would land a pointer at a deal that does not exist and read back as an empty
    Trabajo group forever — the quiet lie the attachments layer refuses for the
    same reason.

    Logs a `deal_linked` task_event (the audit spine the drawer timeline reads).
    Returns `{"status": "ok", ...}` or a `{"status": "error", "error": …}` dict
    the HTTP edge turns into 404/400.
    """
    tid = str(task_id or "").strip()
    did = str(deal_id or "").strip()
    if not tid:
        return {"status": "error", "error": "task_id is required"}
    if not did:
        return {"status": "error", "error": "deal_id is required"}

    conn = db.get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "deal_id" not in cols:
            return {"status": "error",
                    "error": "tasks.deal_id is missing — m06_task_deal has not run"}
        task = conn.execute("SELECT id, deal_id FROM tasks WHERE id = ?", (tid,)).fetchone()
        if task is None:
            return {"status": "error", "error": f"task '{tid}' not found"}
        deal = conn.execute("SELECT id, title FROM deals WHERE id = ?", (did,)).fetchone()
        if deal is None:
            return {"status": "error", "error": f"deal '{did}' not found"}

        previous = task["deal_id"]
        conn.execute("UPDATE tasks SET deal_id = ? WHERE id = ?", (did, tid))
        from . import sprints as _sprints
        _sprints._log_event(conn, tid, "deal_linked",
                            {"deal_id": did, "deal_title": deal["title"],
                             "from": previous})
        # Deliberately no `deal_events` row: the deal's Actividad timeline is
        # filtered to HUMAN kinds (touch/meeting/discovery_call/stage_changed/
        # delivered_link), so a machine-written link would be noise with no
        # reader. The task_events row above is the audit record.
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "task_id": tid, "deal_id": did, "previous_deal_id": previous}


# The board's columns, as the drilldown groups them. Two statuses share the
# first column on the real board (Pool/Inbox = backlog + ready), and the two
# terminal-but-not-shipped statuses share the last — grouping by raw status
# instead would invent columns the operator has never seen.
_COLUMN_OF = {
    "backlog": "pool", "ready": "pool", "pending": "pool",
    "in_progress": "in_progress",
    "blocked": "blocked",
    "review": "review",
    "done": "done",
    "rejected": "closed", "cancelled": "closed",
}
COLUMN_ORDER = ("in_progress", "review", "blocked", "pool", "done", "closed")

# Enrichment (runs + verification + commits) is capped: `_commits_for_task`
# shells out to git per task, and the union below can legitimately return a
# whole project's backlog. The legacy initiative walk never exceeded a few dozen
# tasks; this keeps the endpoint's cost bounded now that it can. Tasks past the
# cap carry the same keys with empty values, so no renderer has to branch.
_ENRICH_LIMIT = 25


def _drilldown_tasks(conn, deal: dict) -> list:
    """The tasks of a deal: its OWN tasks ∪ the tasks of the project delivering it.

    The spine's last hop (journey fase 1, step 4). Before m06 the only way from a
    deal to work was `deals.initiative_id → epics → tasks`, which answered
    `{initiative: None, tasks: []}` for every deal that had never been linked to
    a quarterly bet — i.e. for all of them. Two sources, unioned:

      * `tasks.deal_id = <deal>` — the commercial lineage, written by the three
        ruling-5 writers. A sales task (a touch, an invoice chase) has no
        delivering project at all, so this is the only way it can be found.
      * `tasks.project_id = deals.project_id` — the delivery lineage. The work
        that ships this deal was never tagged with the deal and never will be:
        it is tagged with the project, which is precisely what `deliver_deal`
        linked.

    UNION, not UNION ALL: a task that is both (a deal task inside the delivering
    project) must appear once.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    has_deal_col = "deal_id" in cols
    select = ("SELECT t.id, t.title, t.status, t.assignee, t.reviewed_at, t.epic_id, "
              "t.project_id, t.created_at, "
              + ("t.deal_id, t.stage_kind " if has_deal_col else "NULL AS deal_id, NULL AS stage_kind ")
              + "FROM tasks t")
    where, params = [], []
    if has_deal_col:
        where.append("t.deal_id = ?")
        params.append(deal["id"])
    project_id = deal.get("project_id")
    if project_id:
        where.append("t.project_id = ?")
        params.append(project_id)
    if not where:
        return []
    sql = f"{select} WHERE {' OR '.join(where)} ORDER BY t.created_at DESC LIMIT 300"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _group_by_stage(tasks: list, deal_stage, project_status) -> list:
    """Group the drilldown's tasks by stage-kind, then by board column.

    Directiva ADICIÓN 9: the drawer's Trabajo section is not a flat list — it
    reads as the CYCLE (contacto → formalización → ejecución → entrega →
    facturación → cobranza), so the operator sees *where* a client is, not just
    how many cards exist. The stage is derived per task by `stagekind.derive`
    (never asked); tasks whose stage nothing implies land in a trailing
    `stage_kind: null` group rather than being dropped — a task the rule cannot
    place is still work, and hiding it would make the count lie.
    """
    from . import stagekind
    buckets: dict = {}
    for t in tasks:
        kind = stagekind.derive(t, deal_stage=deal_stage, project_status=project_status)
        # Overwrite the raw column with the RESOLVED value — the same collapse
        # `db._row_to_task` does for the board feed. Two payloads where
        # `stage_kind` means "the stored NULL" in one and "the answer" in the
        # other is exactly how a surface ends up disagreeing with the card.
        t["stage_kind"] = kind
        column = _COLUMN_OF.get(str(t.get("status") or "").lower(), "pool")
        t["column"] = column
        buckets.setdefault(kind, {}).setdefault(column, []).append(t)

    order = [*stagekind.STAGE_KINDS, None]
    out = []
    for kind in order:
        cols = buckets.get(kind)
        if not cols:
            continue
        columns = [{"column": c, "count": len(cols[c]), "tasks": cols[c]}
                   for c in COLUMN_ORDER if cols.get(c)]
        out.append({
            "stage_kind": kind,
            "label": stagekind.label(kind) if kind else None,
            "count": sum(c["count"] for c in columns),
            "columns": columns,
        })
    return out


def deal_drilldown(deal_id: str) -> dict:
    """THE full spine (Phase 6, the point of it all):
    Deal → its work (deal tasks ∪ the delivering project's tasks) → Runs
    (+ verification) → Commits, grouped by where in the client cycle each task
    sits and then by board column.

    The primary path is the m06 spine (`_drilldown_tasks`). The
    `deals.initiative_id → epics → tasks` walk survives as a LEGACY FALLBACK,
    used only when the spine returns nothing: the initiative/epic columns are
    frozen audit (ruling 6) and the handful of deals that carry one must keep
    resolving, but a chain that only worked for deals wired to a quarterly bet
    is not the chain — it is why this endpoint answered `{initiative: None,
    tasks: []}` for every real deal on the board.

    `initiative` / `epics` / `progress` / `chain_complete` keep their meanings so
    the existing renderer and its contracts are untouched; `groups` is the new
    read the drawer's Trabajo section uses.
    """
    deal = get_deal(deal_id)
    if deal is None:
        return {"status": "error", "error": "deal not found"}
    out = {"deal": deal, "initiative": None, "epics": [], "tasks": [],
           "groups": [], "source": "spine",
           "progress": None, "chain_complete": False}

    conn = db.get_conn()
    try:
        project_status = None
        if deal.get("project_id"):
            prow = conn.execute("SELECT status FROM projects WHERE id = ?",
                                (deal["project_id"],)).fetchone()
            if prow:
                project_status = prow["status"]

        trows = _drilldown_tasks(conn, deal)

        # --- legacy fallback: the frozen initiative → epics walk.
        iid = deal.get("initiative_id")
        if iid:
            from . import strategy
            init = strategy.get_initiative(iid)
            if init:
                out["initiative"] = init
                out["progress"] = object_graph.initiative_progress(init)
                out["epics"] = [dict(r) for r in conn.execute(
                    "SELECT id, title, status FROM epics WHERE initiative_id = ? "
                    "AND archived_at IS NULL", (iid,)).fetchall()]
                if not trows:
                    out["source"] = "initiative"
                    epic_ids = [e["id"] for e in out["epics"]]
                    if epic_ids:
                        ph = ",".join("?" * len(epic_ids))
                        trows = [dict(r) for r in conn.execute(
                            f"SELECT id, title, status, assignee, reviewed_at, epic_id, "
                            f"project_id, created_at FROM tasks "
                            f"WHERE epic_id IN ({ph}) ORDER BY created_at DESC",
                            epic_ids).fetchall()]
                    elif init.get("project_id"):
                        trows = [dict(r) for r in conn.execute(
                            "SELECT id, title, status, assignee, reviewed_at, epic_id, "
                            "project_id, created_at FROM tasks "
                            "WHERE project_id = ? ORDER BY created_at DESC",
                            (init["project_id"],)).fetchall()]

        tasks = []
        for i, td in enumerate(trows):
            td = dict(td)
            td["accepted"] = bool(td.get("status") == "done" and td.get("reviewed_at"))
            if i < _ENRICH_LIMIT:
                td["runs"] = [dict(r) for r in conn.execute(
                    "SELECT id, step_key, status, outcome, started_at, ended_at "
                    "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 3",
                    (td["id"],)).fetchall()]
                v = conn.execute(
                    "SELECT agent, passed, created_at FROM task_ledger WHERE task_id = ? "
                    "AND role = 'verification' ORDER BY created_at DESC LIMIT 1",
                    (td["id"],)).fetchone()
                td["verification"] = dict(v) if v else None
                td["commits"] = _commits_for_task(td["id"])
            else:
                td["runs"], td["verification"], td["commits"] = [], None, []
            tasks.append(td)
        out["tasks"] = tasks
        out["groups"] = _group_by_stage(tasks, deal.get("stage"), project_status)
    finally:
        conn.close()

    out["chain_complete"] = bool(
        out["initiative"] and tasks and any(t["runs"] for t in tasks))
    return out


def _delivered_project_ids(conn) -> set:
    """Projects whose delivery leg is closed — the join the `delivered` history
    bucket derives from. Degrades to empty (never raises) on a DB that predates
    `projects.status`: an un-migrated schema means "nothing is delivered yet",
    which is true, rather than a 500 on the board."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT id FROM projects WHERE status = 'delivered'")}
    except sqlite3.OperationalError:
        return set()


def pipeline() -> dict:
    """The CRM board: deals bucketed by stage, each carrying its initiative's
    DERIVED progress (results roll UP the same joins) and product track.

    The `delivered` column is the one bucket that is NOT a stage read. Since
    ruling 2 a deal stays `won` forever, so delivered history is derived —
    `stage = 'won' AND deals.project_id → projects.status = 'delivered'` — and
    those rows leave the `won` column, which keeps "won" meaning *still being
    delivered* on the board while `won_value` (which sums `_WON_OUTCOMES`) keeps
    counting them: the money is unaffected by the work shipping, which is the
    entire point of separating the two.
    """
    deals = list_deals()
    from . import strategy
    conn = db.get_conn()
    try:
        delivered_projects = _delivered_project_ids(conn)
    finally:
        conn.close()
    by_stage = {s: [] for s in STAGES}
    for d in deals:
        if d.get("initiative_id"):
            init = strategy.get_initiative(d["initiative_id"])
            if init:
                d["initiative_progress"] = object_graph.initiative_progress(init)["progress"]
        stage = d["stage"]
        # A legacy row still carrying the retired stage (there are none live, and
        # m05's trigger makes new ones impossible) falls through to setdefault
        # and lands in the same bucket — history reads the same either way.
        if stage == "won" and d.get("project_id") in delivered_projects:
            by_stage["delivered"].append(d)
        else:
            by_stage.setdefault(stage, []).append(d)
    open_value = sum(d.get("value") or 0 for d in deals if d["stage"] not in _CLOSED and d["stage"] not in _INACTIVE)
    won_value = sum(d.get("value") or 0 for d in deals
                    if d["stage"] in _WON_OUTCOMES)
    stalled_value = sum(d.get("value") or 0 for d in deals if d["stage"] in _INACTIVE)
    return {"stages": STAGES, "by_stage": by_stage,
            "counts": {s: len(by_stage[s]) for s in STAGES},
            "open_value": open_value, "won_value": won_value,
            "stalled_value": stalled_value}


def detect_stale_deals(days_idle: int = 7, include_stalled: bool = True) -> list:
    """Find deals idle >= days_idle days on the TOUCH clock, still open.

    Idle basis is last_touch_date (a real contact) when present, else
    updated_at: a record edit must never refresh a cold deal — a real deal once
    sat 23 days untouched while edits kept updated_at current, so the old
    30d edit-clock sweep reported an empty list (2026-08-09). 'stalled' is
    included by default because that bucket hid $208K from every alert;
    sales-closed stages stay out. Rows carry days_idle + basis."""
    conn = db.get_conn()
    try:
        now = _now()
        today = datetime.date.today()
        cutoff_epoch = now - (days_idle * 86400)
        cutoff_date = (today - datetime.timedelta(days=days_idle)).isoformat()
        excluded = ("won", "delivered", "lost")
        if not include_stalled:
            excluded = ("stalled",) + excluded
        marks = ",".join("?" * len(excluded))
        rows = conn.execute(
            "SELECT d.id, d.title, d.stage, d.updated_at, d.last_touch_date, "
            "d.value, d.currency, a.name AS account_name "
            "FROM deals d JOIN accounts a ON a.id = d.account_id "
            f"WHERE d.stage NOT IN ({marks}) AND "
            "((d.last_touch_date IS NOT NULL AND d.last_touch_date < ?) OR "
            " (d.last_touch_date IS NULL AND d.updated_at < ?)) "
            "ORDER BY COALESCE(d.last_touch_date, date(d.updated_at, 'unixepoch')) ASC",
            (*excluded, cutoff_date, cutoff_epoch)).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["days_idle"], rec["basis"] = _touch_idle(rec, now, today)
            out.append(rec)
        return out
    finally:
        conn.close()


def _touch_idle(rec: dict, now: int, today: datetime.date) -> tuple:
    """Days idle + which clock measured it (touch clock when it exists)."""
    touch = rec.get("last_touch_date")
    if touch:
        try:
            idle = (today - datetime.date.fromisoformat(str(touch)[:10])).days
            return int(idle), "last_touch_date"
        except ValueError:
            pass
    return int((now - (rec.get("updated_at") or now)) // 86400), "updated_at"


def auto_stale_decay(days_to_stalled: int = 30, days_to_lost: int = 90) -> dict:
    """Automatic stage decay for idle deals, on the TOUCH clock:
    - idle >= days_to_stalled (30d) in active stage → move to 'stalled'
    - idle >= days_to_lost (90d) in 'stalled' → move to 'lost' (reason: no decision / went cold)
    Idle basis matches detect_stale_deals: last_touch_date when present, else
    updated_at — an edited-but-untouched deal still decays, and a
    recently-touched deal never does (clock unified 2026-08-09, approved).
    Returns counts of what was moved."""
    conn = db.get_conn()
    try:
        now = _now()
        today = datetime.date.today()
        stalled_cutoff = now - (days_to_stalled * 86400)
        lost_cutoff = now - (days_to_lost * 86400)
        stalled_cutoff_date = (today - datetime.timedelta(days=days_to_stalled)).isoformat()
        lost_cutoff_date = (today - datetime.timedelta(days=days_to_lost)).isoformat()
        _idle_pred = ("((last_touch_date IS NOT NULL AND last_touch_date < ?) OR "
                      " (last_touch_date IS NULL AND updated_at < ?))")

        # Move active deals idle >30d (touch clock) to stalled
        to_stall = conn.execute(
            "SELECT id, stage, updated_at, last_touch_date FROM deals "
            "WHERE stage NOT IN ('stalled', 'won', 'delivered', 'lost') AND " + _idle_pred,
            (stalled_cutoff_date, stalled_cutoff)).fetchall()
        for d in to_stall:
            idle_days, _basis = _touch_idle(dict(d), now, today)
            conn.execute("UPDATE deals SET stage = 'stalled', updated_at = ? WHERE id = ?",
                         (now, d["id"]))
            _log(conn, d["id"], "stage_changed", {"from": d["stage"], "to": "stalled", "reason": "auto-stale-30d"})
            _log(conn, d["id"], "auto_stalled", {"idle_days": idle_days})

        # Move stalled deals idle >90d (touch clock) to lost
        to_lose = conn.execute(
            "SELECT id, updated_at, last_touch_date FROM deals "
            "WHERE stage = 'stalled' AND " + _idle_pred,
            (lost_cutoff_date, lost_cutoff)).fetchall()
        for d in to_lose:
            # The reason its own comment always claimed, now stored (it was only
            # ever in the event payload, so reports counted these as unknown).
            conn.execute("UPDATE deals SET stage = 'lost', closed_at = ?, updated_at = ?, "
                         "lost_reason = COALESCE(lost_reason, 'no_decision'), "
                         "lost_notes = COALESCE(lost_notes, 'Auto-closed after 90d idle') "
                         "WHERE id = ?",
                         (now, now, d["id"]))
            _log(conn, d["id"], "stage_changed", {"from": "stalled", "to": "lost",
                                                  "reason": "auto-decay-90d",
                                                  "lost_reason": "no_decision"})
            _log(conn, d["id"], "auto_lost", {"idle_days": (now - d["updated_at"]) // 86400})

        conn.commit()
        return {"stalled_count": len(to_stall), "lost_count": len(to_lose),
                "stalled_deals": [dict(d) for d in to_stall],
                "lost_deals": [dict(d) for d in to_lose]}
    finally:
        conn.close()
