"""The 3x-daily ritual — ONE deterministic composer, ONE renderer.

Consolidation spec §3. Three slots (America/Monterrey): **08:30 Plan ·
13:30 Pulse · 18:30 Close**. Each one composes the SAME five-block payload from
queries and existing server-side compositions, persists it in `brief_runs`, and
renders it to the Telegram text. The stored JSON is what the web mirror will
render too, so the two channels can never drift into two different reports.

**No LLM anywhere in this path (red line 4).** Every number here comes out of a
SQL query or an existing function's return value. A model asked to summarise a
slow day manufactures momentum, and the brief is precisely the instrument
the operator uses to decide what is real — one hallucinated "completed" item burns
the credibility of the whole loop. There is no narrator: only counts, names and
ids that a human could re-derive from the DB by hand.

The five blocks, always present, always in this order (the ADHD accommodation —
the operator learns *where to look*, not *what to read*):

    ⚠️ Needs you · 💰 Money · 🏗️ Delivery · 🤖 Agents · ➡️ Next

An empty block renders as a single em-dash line, never as an empty card with
explanatory copy. Slots differ ONLY in emphasis (which block survives the hard
12-line cap) — never in structure.

**Write boundary.** Composition is a read, with exactly ONE sanctioned write:
the morning slot commits the day's plan when no plan exists (spec §3 "Never
stall" — silence means accept, and a forward default beats no plan on the days
the operator is too scattered to reply). Midday and evening never write. Notably
this module does NOT call `canvas.wrap_day()` for the Close DONE list even
though it is the obvious source: `wrap_day` stamps `carried_over` events, and a
brief you can re-request must not have side effects. `_done_list()` mirrors its
`accepted_today` query exactly, as a pure read.

**Forward-schema guards.** `deals.project_id` and `task_dispatches` arrive in a
LATER migration (m02_spine). Every reference to them is gated on a
`PRAGMA table_info` check and degrades to an empty list, so this composer runs
correctly on both sides of that migration.

Idempotency: `brief_runs` is keyed `(date, slot)`. A re-fired cron gets the
STORED payload back — never a second composition and never a second Telegram
message.
"""
import datetime as _dt
import json
import os
import re
import sqlite3
import time
from typing import Optional
from zoneinfo import ZoneInfo

from . import canvas
from . import db
# ONE source of truth for the dashboard base URL. `dispatch._dashboard_url()`
# already builds the dispatch brief's deep links from it; a second reader would
# drift the day the dashboard moves behind a tunnel. Imported as the MODULE (not
# the function) so a caller that repoints it is honoured here too.
from . import dispatch

# The ritual's timezone. `date.today()` follows the server clock; the ritual
# follows the operator's day, and those are only accidentally the same machine.
TZ = ZoneInfo("America/Monterrey")

SLOTS = ("morning", "midday", "evening")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Personal work is out of the professional daily view — same exclusion the
# canvas applies, so Today and the brief can't disagree about what counts.
_PERSONAL_PROJECT = canvas._PERSONAL_PROJECT

# Deal stages that are not "open pipeline": closed (won/delivered/lost) and
# iceboxed (stalled). Read from crm so there is ONE stage vocabulary.
def _open_stage_filter() -> tuple:
    from . import crm
    closed = tuple(crm._CLOSED) + tuple(crm._INACTIVE)
    return closed


# A deal with no touch in this many days is stale enough to name in the brief.
# Deliberately NOT growth.STALE_TOUCH_DAYS (7): that threshold drives the daily
# touch-cadence triage, this one is the "it is quietly dying" line in a report
# the operator reads three times a day. 14 = the spec's number.
STALE_DEAL_DAYS = 14

# task_runs.status vocabulary: the DDL comment declares
# running | done | blocked | crashed | timed_out | failed | released, and the
# live table additionally carries `completed` and `stale`. `released` is neither
# a win nor a loss (the run was handed back) so it is in neither set.
_RUN_OK = ("done", "completed")
_RUN_FAIL = ("crashed", "timed_out", "failed", "stale", "blocked")

# How many named items a payload list carries. The renderer caps lines much
# harder; this only stops a pathological day from storing a 2000-row payload.
_LIST_CAP = 20

# How long a `running` task_run may go without saying anything before the brief
# stops calling it running. `status = 'running' AND ended_at IS NULL` on its own
# is a claim the row makes about itself FOREVER: on 2026-07-29 five runs
# abandoned 18–19 days earlier were reported as "🤖 Agents · 5 running" three
# times a day, in the one instrument the operator uses to decide what is real.
RUNNING_STALE_SECONDS = 3600


# ---------------------------------------------------------------- dates + guards

def today() -> str:
    """`today` in the ritual's timezone, not the server's."""
    return _dt.datetime.now(TZ).date().isoformat()


def valid_date(d: Optional[str]) -> Optional[str]:
    """None/''/'today' → today. Bad format or impossible date → None."""
    if d in (None, "", "today"):
        return today()
    d = str(d).strip()
    if not DATE_RE.match(d):
        return None
    try:
        return _dt.date.fromisoformat(d).isoformat()
    except ValueError:
        return None


def midnight_ts(date: str) -> int:
    """Epoch seconds at local midnight of `date` in the ritual's timezone."""
    d = _dt.date.fromisoformat(date)
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def end_ts(date: str) -> int:
    """The window's UPPER bound: local midnight at the END of `date`.

    Every "since" query used to be open-ended, so a brief composed for a PAST
    date reported events that happened after it — `compose("midday",
    "2026-07-15")` returning a dispatch created on 2026-07-29, and (once more
    than `_LIST_CAP` deals moved today) crowding that day's real stage changes
    out of the list entirely. A brief is a report about ONE day; it must not be
    able to see past its own midnight. For today's brief this changes nothing —
    the bound is tomorrow.
    """
    return midnight_ts(date) + 86400


def _columns(conn, table: str) -> set:
    """`PRAGMA table_info` as a column-name set — EMPTY when the table does not
    exist. The one guard behind every forward-schema reference in this module:
    `"project_id" in _columns(conn, "deals")` answers the column question and
    `bool(_columns(conn, "task_dispatches"))` answers the table question, both
    without a second dialect of existence check."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:  # pragma: no cover - defensive
        return set()


def since_ts(conn, date: str, exclude_slot: Optional[str] = None) -> int:
    """"Since your last brief": the newest brief already composed for `date`,
    else local midnight. This is what makes Pulse and Close *diffs* rather than
    three narrations of the same day (the failure mode the ritual replaces).

    `exclude_slot` is for a FORCED recompose (`?force=1`): the row being
    replaced must not be its own "since", or a repaired brief would report the
    window since the broken one it exists to replace — i.e. nothing.
    """
    row = conn.execute(
        "SELECT MAX(created_at) FROM brief_runs WHERE date = ? AND slot != COALESCE(?, '')",
        (date, exclude_slot)).fetchone()
    prev = row[0] if row else None
    return int(prev) if prev else midnight_ts(date)


# ---------------------------------------------------------------- the migration

def m01_brief_runs(conn) -> None:
    """m01 — the brief's persistence anchor.

    Registered in `dashboard/migrations/runner.py`; receives the runner's OWN
    connection inside its transaction (never commits, never opens its own).

    `(date, slot)` is the primary key, and that IS the idempotency: a re-fired
    cron reads the stored row instead of composing and posting twice. It also
    buys the web mirror, the "since your last brief" diff, and an answer to
    "what have I already been told" — none of which exist today, where cron
    output lands in ~/.hermes/cron/output/<job_id>/ and nothing ever reads it.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS brief_runs ("
        "  date            TEXT NOT NULL,"
        "  slot            TEXT NOT NULL,"
        "  payload_json    TEXT NOT NULL,"
        "  rendered_md     TEXT NOT NULL,"
        "  created_at      INTEGER NOT NULL,"
        "  sent_at         INTEGER,"
        "  acknowledged_at INTEGER,"
        "  PRIMARY KEY (date, slot)"
        ")")


# ---------------------------------------------------------------- composers
# One function per block. Each is a pure read of the DB (or of an existing
# server-side composition) and returns exactly the payload sub-object.

def orphan_won_deals(conn, limit: int = _LIST_CAP) -> list:
    """Won deals with no delivering project — money that arrived and then fell
    out of the system.

    THE query, shared by the composer and by `GET /api/crm/deals/orphan-won`.
    The web read must be live rather than a mirror of the stored brief: on
    2026-07-29 the 06:01 morning brief was composed 11 minutes BEFORE
    m02_spine added `deals.project_id`, so the guard below fired, the payload
    froze at `[]`, and four won deals worth $194,500 were invisible on Today for
    the whole day with no way to un-stick them. Same SQL, two callers.

    `deals.project_id` lands in m02_spine — until then this is honestly empty
    rather than fabricated.
    """
    if "project_id" not in _columns(conn, "deals"):
        return []
    return [
        {"id": r["id"], "title": r["title"], "value": r["value"]}
        for r in conn.execute(
            "SELECT id, title, value FROM deals "
            "WHERE stage = 'won' AND project_id IS NULL "
            "ORDER BY value DESC LIMIT ?", (limit,)).fetchall()
    ]


def orphan_won_deals_now(limit: int = _LIST_CAP) -> list:
    """`orphan_won_deals` on its own connection — the endpoint's entry point."""
    conn = db.get_conn()
    try:
        return orphan_won_deals(conn, limit)
    finally:
        conn.close()


# A SELECT intent open this long reads as chronic, not new.
_INTENT_CHRONIC_DAYS = 14
# How many open SELECT intents may be named by name when more than one is
# pending. Three, not more: the block's 12-line budget still has to carry
# blocked work and unlinked won deals, and a needs-you block that is all
# intents trains the reader to skim the one block that must never be skimmed.
_INTENT_NAMED_MAX = 3

# Where the proactive layer's queue lives. A MODULE attribute — like
# `db.KANBAN_DB` and `orchestration.ORCH_DIR` — precisely so a test can repoint
# it. A composer that reads a path no test can isolate makes "an empty day" a
# lie, which is the same trap `task_dispatches` sprang on the EmptyDay cases.
INTENT_QUEUE_DIR = os.environ.get("INTENT_QUEUE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "knowledge")


def open_select_intents(now: Optional[_dt.datetime] = None) -> list:
    """The proactive layer's open **SELECT** intents — the ones only the operator can act on.

    A pure file read of `knowledge/intent-queue.jsonl`, resolved from THIS
    module's own path (never the working directory) and degrading to `[]` when
    the queue does not exist. That resolution rule is not fussiness: the
    2026-08-10 incident was precisely a cwd fallback — the weekly watcher wrote
    three load-bearing intents outside the repo for three weeks and nothing ever
    read them (`knowledge/incidents/stranded-intent-queue-2026-08-10.md`).

    Only `load_bearing` intents surface here. AUTO ones are, by definition, work
    an unattended round may take; putting them in a human's "needs you" block
    would train the operator to skim the one block that must never be skimmed.

    `intent-queue add` is idempotent while an item is open, so `ts` is genuinely
    FIRST-seen: a watcher that trips every week does not reset the clock, and a
    signal that has been screaming since July shows its real age instead of
    arriving new each Monday. That age is the whole point of surfacing these.
    """
    try:
        with open(os.path.join(INTENT_QUEUE_DIR, "intent-queue.jsonl")) as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return []
    now = now or _dt.datetime.now(_dt.timezone.utc)
    out = []
    for it in rows:
        if it.get("status") != "open" or not it.get("load_bearing"):
            continue
        age = None
        try:
            seen = _dt.datetime.fromisoformat(str(it.get("ts") or "").replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=_dt.timezone.utc)
            age = max(0, (now - seen).days)
        except ValueError:
            pass
        out.append({
            "id": it.get("id"), "kind": it.get("kind") or "", "intent": it.get("intent") or "",
            "age_days": age,
            "chronic": bool(it.get("recurred")) or (age is not None and age >= _INTENT_CHRONIC_DAYS),
        })
    out.sort(key=lambda i: (-(i["age_days"] if i["age_days"] is not None else -1), i["id"] or ""))
    return out[:_LIST_CAP]


def compose_needs_you(conn, date: str) -> dict:
    """⚠️ Blocked work · the review queue · won deals with nowhere to land ·
    the proactive layer's open SELECT intents.

    Counts come from `canvas.plan_candidates()` (the same numbers the standup
    reports, so the brief and Today agree by construction); the named list is
    the same predicate, re-queried because plan_candidates returns only counts.
    """
    cand = canvas.plan_candidates(date)
    blocked_count = int(cand.get("blocked_count") or 0)
    review_count = int(cand.get("review_count") or 0)
    blocked = [
        {"id": r["id"], "title": r["title"]}
        for r in conn.execute(
            "SELECT id, title FROM tasks WHERE status = 'blocked' AND project_id != ? "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (_PERSONAL_PROJECT, _LIST_CAP)).fetchall()
    ]
    orphans = orphan_won_deals(conn)
    intents = open_select_intents()
    return {
        "blocked": blocked,
        "blocked_count": blocked_count,
        "review_count": review_count,
        "orphan_won_deals": orphans,
        "intents": intents,
        "count": blocked_count + review_count + len(orphans) + len(intents),
    }


def compose_money(conn, date: str, since: int) -> dict:
    """💰 Deals that moved since the last brief · deals going cold · one number.

    The touch-cadence triage is `growth.pipeline_health()` (the existing
    composition — not a re-implementation); the open-pipeline number is a plain
    SUM over the same active-stage definition `crm` uses.
    """
    from . import growth
    closed = _open_stage_filter()
    ph = ",".join("?" * len(closed))
    until = end_ts(date)

    moved = []
    for r in conn.execute(
            "SELECT e.deal_id AS id, d.title AS title, e.payload AS payload, e.created_at "
            "FROM deal_events e LEFT JOIN deals d ON d.id = e.deal_id "
            "WHERE e.kind = 'stage_changed' AND e.created_at >= ? AND e.created_at < ? "
            "ORDER BY e.created_at DESC LIMIT ?", (since, until, _LIST_CAP)).fetchall():
        try:
            p = json.loads(r["payload"] or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            p = {}
        moved.append({"id": r["id"], "title": r["title"],
                      "from": p.get("from"), "to": p.get("to")})

    row = conn.execute(
        f"SELECT COALESCE(SUM(value), 0) FROM deals WHERE stage NOT IN ({ph})",
        closed).fetchone()
    pipeline_open_value = float(row[0] or 0)

    # Stale = no touch in STALE_DEAL_DAYS. A never-touched deal falls back to
    # its creation date — "we have never spoken" is staler than "we spoke
    # three weeks ago", not exempt from the count.
    cutoff = (_dt.date.fromisoformat(date) - _dt.timedelta(days=STALE_DEAL_DAYS)).isoformat()
    stale = []
    for r in conn.execute(
            f"SELECT id, title, "
            f"  COALESCE(last_touch_date, date(created_at, 'unixepoch', 'localtime')) AS touched "
            f"FROM deals WHERE stage NOT IN ({ph}) "
            f"  AND COALESCE(last_touch_date, date(created_at, 'unixepoch', 'localtime')) <= ? "
            f"ORDER BY touched ASC LIMIT ?", (*closed, cutoff, _LIST_CAP)).fetchall():
        try:
            days = (_dt.date.fromisoformat(date) - _dt.date.fromisoformat(r["touched"])).days
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        stale.append({"id": r["id"], "title": r["title"], "days": days})

    try:
        health = growth.pipeline_health()
        alerts = {k: int(v.get("count") or 0) for k, v in (health.get("levels") or {}).items()}
        drift_count = int(((health.get("delivery_drift") or {}).get("counts") or {}).get("total") or 0)
    except Exception:  # pragma: no cover - a CRM hiccup must not kill the brief
        alerts = {}
        drift_count = 0

    return {
        "moved": moved,
        "stale_over_14d": stale,
        "pipeline_open_value": pipeline_open_value,
        "touch_alerts": alerts,
        "delivery_drift_count": drift_count,
    }


def _done_list(conn, date: str) -> list:
    """The named DONE list for `date` — tasks accepted on that local day.

    Mirrors `canvas.wrap_day()`'s `accepted_today` query exactly. We do NOT
    call `wrap_day`: it stamps `carried_over` events, and composing a brief
    must stay side-effect-free outside the one sanctioned morning write.
    """
    return [
        {"id": r["id"], "title": r["title"],
         "deal_id": r["deal_id"], "project_id": r["project_id"]}
        for r in conn.execute(
            "SELECT id, title, deal_id, project_id FROM tasks "
            "WHERE status = 'done' AND completed_at IS NOT NULL "
            "AND date(completed_at, 'unixepoch', 'localtime') = ? "
            "ORDER BY completed_at DESC LIMIT ?", (date, _LIST_CAP)).fetchall()
    ]


def _commercial_accounts(conn, done: list) -> list:
    """Count each accepted task against one explicitly-linked account.

    A deal's account wins when both lineages exist. Project attribution is a
    fallback only; task titles are deliberately never inspected.
    """
    counts = {}
    for task in done:
        account = None
        if task.get("deal_id"):
            account = conn.execute(
                "SELECT a.id, a.name FROM deals d "
                "JOIN accounts a ON a.id = d.account_id WHERE d.id = ?",
                (task["deal_id"],)).fetchone()
        if account is None and task.get("project_id"):
            account = conn.execute(
                "SELECT a.id, a.name FROM projects p "
                "JOIN accounts a ON a.id = p.account_id WHERE p.id = ?",
                (task["project_id"],)).fetchone()
        if account is None or not account["id"] or not account["name"]:
            continue
        key = account["id"]
        if key not in counts:
            counts[key] = {"id": key, "name": account["name"], "done_today": 0}
        counts[key]["done_today"] += 1
    return sorted(counts.values(),
                  key=lambda x: (-x["done_today"], x["name"].casefold(), x["id"]))


# Completion, as the task_events log records it: the explicit `completed` event
# OR a status_changed landing on done. This is the deterministic core of
# day_review.collect_kanban's completion labelling, run against db.get_conn()
# (which honours HERMES_KANBAN_DB) instead of collect_kanban's hard-coded
# ~/.hermes/kanban.db path.
_COMPLETED_EVENT = (
    "(e.kind = 'completed' OR "
    " (e.kind = 'status_changed' AND json_extract(e.payload, '$.to') = 'done'))")


def compose_delivery(conn, date: str, since: int) -> dict:
    """🏗️ Which projects moved since the last brief, and which stayed quiet."""
    moved_rows = conn.execute(
        f"SELECT p.id AS id, p.name AS name, COUNT(DISTINCT e.task_id) AS n "
        f"FROM task_events e "
        f"JOIN tasks t ON t.id = e.task_id "
        f"JOIN projects p ON p.id = t.project_id "
        f"WHERE {_COMPLETED_EVENT} AND e.created_at >= ? AND e.created_at < ? "
        f"  AND p.id != ? "
        f"GROUP BY p.id, p.name ORDER BY n DESC, p.name ASC LIMIT ?",
        (since, end_ts(date), _PERSONAL_PROJECT, _LIST_CAP)).fetchall()
    moved = [{"id": r["id"], "name": r["name"], "done_today": int(r["n"])} for r in moved_rows]
    loud = {m["id"] for m in moved}

    # Quiet = a live project carrying open work that produced no completion in
    # this window. A project with nothing to do isn't "quiet", it's finished.
    quiet = [
        r["name"] for r in conn.execute(
            "SELECT DISTINCT p.id, p.name FROM projects p JOIN tasks t ON t.project_id = p.id "
            "WHERE p.archived_at IS NULL AND p.id != ? "
            "  AND t.status NOT IN ('done', 'rejected', 'cancelled') "
            "ORDER BY p.name ASC", (_PERSONAL_PROJECT,)).fetchall()
        if r["id"] not in loud
    ][:_LIST_CAP]

    done = _done_list(conn, date)
    week_start = (_dt.date.fromisoformat(date)
                  - _dt.timedelta(days=_dt.date.fromisoformat(date).weekday())).isoformat()
    done_week = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at IS NOT NULL "
        "AND date(completed_at, 'unixepoch', 'localtime') BETWEEN ? AND ?",
        (week_start, date)).fetchone()[0]
    return {
        "projects_moved": moved,
        "projects_quiet": quiet,
        "done": done,
        "done_count": len(done),
        "done_week": int(done_week),
        "accounts_moved": _commercial_accounts(conn, done),
    }


def compose_agents(conn, since: int, now: Optional[int] = None,
                   until: Optional[int] = None) -> dict:
    """🤖 Runs in flight, and what finished or failed since the last brief.

    Read-only against `task_runs` — the run table is Hermes-owned and the
    dashboard never writes it (spec §2). `task_dispatches` is the dashboard's
    own outbox and arrives in m02_spine; until then it is honestly absent.

    "In flight" is BOUNDED by liveness, not by the row's own status word. The
    two signals the table actually carries: an unexpired claim lease
    (`claim_expires`, set by the hermes claim path and often NULL), else the
    heartbeat — falling back to `started_at` so a run that has not beaten *yet*
    is not misread as dead. A run holding neither is abandoned, and calling it
    running is the kind of false headline this whole module exists to prevent.
    """
    ok_ph = ",".join("?" * len(_RUN_OK))
    fail_ph = ",".join("?" * len(_RUN_FAIL))
    now = int(now if now is not None else time.time())
    running = conn.execute(
        "SELECT COUNT(*) FROM task_runs "
        "WHERE status = 'running' AND ended_at IS NULL "
        # NULL-safe by construction: `NULL >= n` is NULL, never true, so a run
        # with no lease is judged purely on heartbeat/start recency.
        "  AND (claim_expires >= ? OR COALESCE(last_heartbeat_at, started_at) >= ?)",
        (now, now - RUNNING_STALE_SECONDS)).fetchone()[0]

    # The window's upper edge. `until = None` means "no upper bound" only for a
    # direct caller; `compose()` always passes the brief's own end-of-day.
    hi = int(until) if until is not None else (1 << 62)

    def _runs(statuses, ph):
        return [
            {"id": r["id"], "task_id": r["task_id"], "title": r["title"],
             "status": r["status"], "outcome": r["outcome"]}
            for r in conn.execute(
                f"SELECT r.id, r.task_id, r.status, r.outcome, t.title AS title "
                f"FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id "
                f"WHERE r.status IN ({ph}) AND r.ended_at IS NOT NULL "
                f"  AND r.ended_at >= ? AND r.ended_at < ? "
                f"ORDER BY r.ended_at DESC LIMIT ?",
                (*statuses, since, hi, _LIST_CAP)).fetchall()
        ]

    dispatches: list = []
    dcols = _columns(conn, "task_dispatches")
    if {"id", "task_id", "executor_kind", "state", "created_at"} <= dcols:
        dispatches = [
            {"id": r["id"], "task_id": r["task_id"],
             "executor_kind": r["executor_kind"], "state": r["state"]}
            for r in conn.execute(
                "SELECT id, task_id, executor_kind, state FROM task_dispatches "
                "WHERE created_at >= ? AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (since, hi, _LIST_CAP)).fetchall()
        ]

    return {
        "running": int(running),
        "finished": _runs(_RUN_OK, ok_ph),
        "failed": _runs(_RUN_FAIL, fail_ph),
        "dispatches": dispatches,
    }


def compose_next(conn, date: str, slot: str) -> dict:
    """➡️ The 1–3 things next, and whether THIS brief committed the plan.

    Reads `canvas.get_day_plan(date)` — the same server-side composition the
    Today tab and the MCP verb read, so the brief can never propose a different
    day than the screen shows.

    Morning is the one sanctioned write in this module (spec §3 "Never stall"):
    when the day has NO plan at all, the top 3 `plan_candidates` are committed
    via `canvas.plan_day(..., replace=True)` and `plan_committed` says so. An
    existing plan is never touched — `replace=True` would wipe it, so the
    zero-plan check is a hard precondition, not an optimisation.
    """
    plan_committed = False
    if slot == "morning":
        planned = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE planned_for = ?", (date,)).fetchone()[0]
        if not planned:
            cand = canvas.plan_candidates(date)
            ids = [c["id"] for c in (cand.get("candidates") or [])[:3]]
            if ids:
                res = canvas.plan_day(ids, date=date, replace=True)
                plan_committed = res.get("status") != "error" and bool(res.get("planned"))

    why_by_id = {}
    try:
        for c in (canvas.plan_candidates(date).get("candidates") or []):
            why_by_id[c["id"]] = c.get("why")
    except Exception:  # pragma: no cover - defensive
        pass

    plan = canvas.get_day_plan(date)
    # Settled work leaves the list, and so does work you cannot DO: a task
    # planned while workable and blocked an hour later is still in the plan, and
    # rendering it as one of your three things is the plan disagreeing with the
    # ⚠️ Needs you block that is simultaneously reporting it as blocked (observed
    # live 2026-08-11). Excluding blocked from `plan_candidates` governs only
    # what gets PROPOSED; this governs what gets SHOWN. It stays counted where it
    # belongs — as the thing needing you, not as the thing to do.
    _hidden = set(("done", "rejected")) | set(canvas._UNWORKABLE)
    tasks = [
        {"id": t["id"], "title": t["title"], "why": why_by_id.get(t["id"], "planned")}
        for t in (plan.get("do") or []) if t.get("status") not in _hidden
    ][:3]
    return {"tasks": tasks, "plan_committed": plan_committed}


def compose(slot: str, date: Optional[str] = None, replacing: bool = False) -> dict:
    """The whole five-block payload. Same shape for every slot — see module doc.

    `replacing` is set by a forced recompose so the row being overwritten is not
    used as its own "since your last brief" horizon (see `since_ts`).
    """
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {', '.join(SLOTS)}")
    d = valid_date(date)
    if not d:
        raise ValueError("date must be YYYY-MM-DD")
    conn = db.get_conn()
    try:
        since = since_ts(conn, d, exclude_slot=slot if replacing else None)
        payload = {
            "date": d,
            "slot": slot,
            "since_ts": since,
            "needs_you": compose_needs_you(conn, d),
            "money": compose_money(conn, d, since),
            "delivery": compose_delivery(conn, d, since),
            "agents": compose_agents(conn, since, until=end_ts(d)),
            # LAST: morning's plan commit must be visible to its own `next`.
            "next": compose_next(conn, d, slot),
        }
    finally:
        conn.close()
    return payload


# ---------------------------------------------------------------- the renderer

MAX_LINES = 12
EMPTY = "—"

TITLES = {"morning": "📅 Plan", "midday": "🔄 Pulse", "evening": "🌙 Close"}

# Block order is FIXED for every slot — that is the whole accommodation.
BLOCK_ORDER = ("needs_you", "money", "delivery", "agents", "next")
LABELS = {"needs_you": "⚠️ Needs you", "money": "💰 Money",
          "delivery": "🏗️ Delivery", "agents": "🤖 Agents", "next": "➡️ Next"}

# Slots differ ONLY here: emphasis, highest first. When the 12-line cap bites,
# item lines are dropped from the LOWEST-emphasis block first, one at a time,
# so a block collapses to its single header line before a weightier one loses
# anything. Plan weights ➡️ Next, Pulse weights ⚠️ Needs you, Close weights
# 🏗️ Delivery + 🤖 Agents (spec §3).
EMPHASIS = {
    "morning": ("next", "needs_you", "money", "delivery", "agents"),
    "midday": ("needs_you", "agents", "next", "money", "delivery"),
    "evening": ("delivery", "agents", "needs_you", "money", "next"),
}


def _clip(text, n: int = 58) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _money(v) -> str:
    return f"${float(v or 0):,.0f}"


# --- deep links (spec §3 Actionability) ------------------------------------
# "Every line naming an entity carries https://<dash>/?entity=task:t_xxx …
# Never an id to copy, never 'check the dashboard.'" Tap → the drawer opens on
# that exact object → tap the action: two taps from a Telegram line to a state
# change. Before this the brief named entities in plain text with the id in the
# payload only, so the tap count from a message was unbounded (app switch +
# manual search) and `_clip` had usually eaten the end of the title too.
#
# The link rides on the SAME line as the item it belongs to, so the hard
# 12-line cap — which counts LINES — is untouched.

def entity_link(kind: str, entity_id) -> str:
    """The one deep-link form. `dispatch.task_link` builds the same string for
    the dispatch brief; both read `dispatch._dashboard_url()`."""
    return f"{dispatch._dashboard_url()}/?entity={kind}:{entity_id}"


def _linked(text: str, kind: str, entity_id) -> str:
    """An item line plus its deep link — or the bare line when the payload
    carries no id (a name with no id is not silently linked to nothing)."""
    return f"{text} — {entity_link(kind, entity_id)}" if entity_id else text


def _needs_you_block(p: dict) -> tuple:
    n = p.get("needs_you") or {}
    parts = []
    if n.get("blocked_count"):
        parts.append(f"{n['blocked_count']} blocked")
    if n.get("review_count"):
        parts.append(f"{n['review_count']} to review")
    if n.get("orphan_won_deals"):
        parts.append(f"{len(n['orphan_won_deals'])} won deal(s) unlinked")
    if n.get("intents"):
        parts.append(f"{len(n['intents'])} intent(s) to decide")
    # Named intents go FIRST — oldest first. The 12-line cap is this channel's
    # scarcest resource and it trims from the END, so a decision that has waited
    # weeks outlives a blocked-task line instead of being the first thing
    # dropped. Age is carried on the line itself, because an intent that renders
    # like a fresh one is the exact failure this surfacing exists to prevent.
    #
    # ONE name suffices while a single decision is pending; past that, one name
    # plus a count reads as "one thing to decide" and the rest dissolve behind a
    # number nobody expands. So a BACKLOG of SELECTs earns up to
    # _INTENT_NAMED_MAX names. The trigger is COUNT, not age, deliberately: five
    # decisions deferred a week ago are already invisible, and an age gate would
    # keep them invisible for another week before admitting they exist.
    intents = n.get("intents") or []
    named = _INTENT_NAMED_MAX if len(intents) > 1 else 1
    items = []
    for i in intents[:named]:
        age = f" · {i['age_days']}d" if i.get("age_days") is not None else ""
        items.append(f"{'⚠ ' if i.get('chronic') else ''}{_clip(i.get('intent'), 44)}{age}")
    items += [_linked(_clip(b.get("title")), "task", b.get("id"))
              for b in (n.get("blocked") or [])]
    items += [_linked(f"{_clip(d.get('title'), 40)} · won, no project", "deal", d.get("id"))
              for d in (n.get("orphan_won_deals") or [])]
    label = LABELS["needs_you"]
    if n.get("count"):
        label = f"{label} ({n['count']})"
    return label, " · ".join(parts), items


def _money_block(p: dict) -> tuple:
    m = p.get("money") or {}
    parts = []
    if m.get("pipeline_open_value"):
        parts.append(f"{_money(m['pipeline_open_value'])} open")
    red = (m.get("touch_alerts") or {}).get("red") or 0
    if red:
        parts.append(f"{red} need a touch today")
    # Delivery drift is a separate verdict from touch cadence (C5): a project
    # shipped while its deal is still open. One line naming the count, so the
    # silent pass that lost a $100K deal in 2026-08-17 is at least visible in the
    # one instrument the operator reads three times a day.
    drift = int(m.get("delivery_drift_count") or 0)
    if drift:
        parts.append(f"{drift} delivery drift")
    items = [_linked(f"{_clip(d.get('title'), 40)}: {d.get('from') or '?'} → {d.get('to') or '?'}",
                     "deal", d.get("id"))
             for d in (m.get("moved") or [])]
    items += [_linked(f"{_clip(d.get('title'), 40)} · {d.get('days')}d cold", "deal", d.get("id"))
              for d in (m.get("stale_over_14d") or [])]
    return LABELS["money"], " · ".join(parts), items


def _delivery_block(p: dict) -> tuple:
    d = p.get("delivery") or {}
    parts = []
    if d.get("projects_moved"):
        parts.append(f"{len(d['projects_moved'])} moved")
    if d.get("projects_quiet"):
        parts.append(f"{len(d['projects_quiet'])} quiet")
    items = []
    accounts = d.get("accounts_moved") or []
    if p.get("slot") == "evening" and accounts:
        shown = [f"{x.get('name')} ({x.get('done_today')})" for x in accounts[:3]]
        if len(shown) == 1:
            account_text = shown[0]
        else:
            account_text = ", ".join(shown[:-1]) + " y " + shown[-1]
        extra = f" +{len(accounts) - 3} más" if len(accounts) > 3 else ""
        # First is load-bearing: trimming pops from the end, and evening gives
        # Delivery the highest EMPHASIS, so this line survives the 12-line cap.
        items.append(f"Hoy moviste {account_text}{extra}.")
    items += [_linked(f"{_clip(x.get('name'), 40)}: {x.get('done_today')} done",
                      "project", x.get("id"))
              for x in (d.get("projects_moved") or [])]
    if d.get("projects_quiet"):
        items.append("quiet: " + _clip(", ".join(d["projects_quiet"])))
    return LABELS["delivery"], " · ".join(parts), items


def _agents_block(p: dict) -> tuple:
    a = p.get("agents") or {}
    parts = []
    if a.get("running"):
        parts.append(f"{a['running']} running")
    if a.get("finished"):
        parts.append(f"{len(a['finished'])} done")
    if a.get("failed"):
        parts.append(f"{len(a['failed'])} failed")
    if a.get("dispatches"):
        parts.append(f"{len(a['dispatches'])} dispatched")
    # Only failures earn a named line: a finished run needs no attention. The
    # link targets the TASK, not the run — the run has no drawer, and the task
    # is where the action (retry, unblock, re-dispatch) lives.
    items = [_linked(f"failed: {_clip(r.get('title') or r.get('task_id'), 44)}",
                     "task", r.get("task_id"))
             for r in (a.get("failed") or [])]
    return LABELS["agents"], " · ".join(parts), items


def _next_block(p: dict) -> tuple:
    n = p.get("next") or {}
    parts = []
    if n.get("plan_committed"):
        parts.append("plan committed")
    items = [_linked(f"{_clip(t.get('title'), 44)} ({t.get('why')})", "task", t.get("id"))
             for t in (n.get("tasks") or [])]
    return LABELS["next"], " · ".join(parts), items


_BUILDERS = {"needs_you": _needs_you_block, "money": _money_block,
             "delivery": _delivery_block, "agents": _agents_block, "next": _next_block}


def render_telegram(payload: dict) -> str:
    """The ONE renderer. A pure function of the payload — no DB, no clock, no
    model. Fixed block order, an em-dash for an empty block, and a HARD 12-line
    cap enforced here (a channel that pings long gets muted, and a muted channel
    takes the good messages with it).

    Close leads with the named DONE list plus the week's running count:
    completed work is invisible in this system today, and that salience is what
    makes tomorrow's 08:30 land.
    """
    slot = payload.get("slot") if payload.get("slot") in SLOTS else "morning"
    date = payload.get("date") or ""
    head = [f"{TITLES[slot]} · {date}"]

    if slot == "evening":
        # Close leads with the DONE list: completed work is invisible in this
        # system today (Archive has no nav entry), and that salience is the
        # reinforcement that makes tomorrow's 08:30 land. Counts ride on the
        # title so the NAMES get a line of their own — a name is the part that
        # reads as evidence; a count reads as a claim.
        #
        # This ONE line is deliberately unlinked: it joins up to three names
        # into a single summary (three links on one line is unreadable, and one
        # line each would eat a quarter of the 12-line budget on the slot whose
        # emphasis is Delivery). It is also the one block naming work that is
        # already finished — there is no action to deep-link to. Every ITEM line
        # below, including the ones that need a tap, carries its link.
        d = payload.get("delivery") or {}
        done = d.get("done") or []
        week = d.get("done_week", 0)
        if done:
            head[0] += f" · {len(done)} done · {week} this week"
            names = " · ".join(_clip(t.get("title"), 24) for t in done[:3])
            extra = f" +{len(done) - 3}" if len(done) > 3 else ""
            head.append(f"✅ {names}{extra}")
        else:
            head[0] += f" · {week} this week"
            head.append(f"✅ {EMPTY} nothing completed today")

    blocks = {}
    for key in BLOCK_ORDER:
        label, summary, items = _BUILDERS[key](payload)
        blocks[key] = [label, summary, list(items)]

    # Trim from the lowest-emphasis block up, one item at a time.
    order = EMPHASIS.get(slot, EMPHASIS["morning"])

    def _total():
        return len(head) + sum(1 + len(b[2]) for b in blocks.values())

    while _total() > MAX_LINES:
        for key in reversed(order):
            if blocks[key][2]:
                blocks[key][2].pop()
                break
        else:  # pragma: no cover - 6 skeleton lines can never exceed 12
            break

    lines = list(head)
    for key in BLOCK_ORDER:
        label, summary, items = blocks[key]
        if not items and not summary:
            lines.append(f"{label} {EMPTY}")
            continue
        lines.append(f"{label} · {summary}" if summary else label)
        lines.extend(f"• {t}" for t in items)
    return "\n".join(lines)


# ---------------------------------------------------------------- persistence

def _row_response(row, already: bool) -> dict:
    return {
        "status": "ok",
        "date": row["date"],
        "slot": row["slot"],
        "payload": json.loads(row["payload_json"]),
        "rendered_md": row["rendered_md"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
        "sent": bool(row["sent_at"]),
        "acknowledged_at": row["acknowledged_at"],
        "already_composed": already,
    }


def _get_row(conn, date: str, slot: str):
    return conn.execute(
        "SELECT * FROM brief_runs WHERE date = ? AND slot = ?", (date, slot)).fetchone()


def get_or_compose(slot: str, date: Optional[str] = None, force: bool = False) -> dict:
    """The endpoint's core: return the STORED brief for (date, slot) if one
    exists, else compose + render + persist it once.

    Idempotency is the point. Cron delivery is at-least-once; without this a
    retried job posts the same brief to Telegram twice, and the second copy
    teaches the operator that the channel repeats itself.

    `force` is the RECOVERY route, and it is the only thing that overwrites a
    stored row. The (date, slot) key buys idempotency, not immutability: on
    2026-07-29 the morning brief composed 11 minutes before m02_spine landed,
    froze `orphan_won_deals: []` into the payload the web mirror reads, and the
    only way back was hand-written SQL. A forced recompose writes a fresh
    payload with a fresh `created_at` and CLEARS `sent_at`/`acknowledged_at` —
    this is a new message that has not gone out, and a cron that already sent
    the broken one must be free to send the repaired one.
    """
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {', '.join(SLOTS)}")
    d = valid_date(date)
    if not d:
        raise ValueError("date must be YYYY-MM-DD")

    if not force:
        conn = db.get_conn()
        try:
            row = _get_row(conn, d, slot)
            if row is not None:
                return _row_response(row, already=True)
        finally:
            conn.close()

    payload = compose(slot, d, replacing=force)
    rendered = render_telegram(payload)

    if force:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO brief_runs "
                "(date, slot, payload_json, rendered_md, created_at, sent_at, acknowledged_at) "
                "VALUES (?,?,?,?,?,NULL,NULL)",
                (d, slot, json.dumps(payload, ensure_ascii=False), rendered, int(time.time())))
            conn.commit()
            return _row_response(_get_row(conn, d, slot), already=False)
        finally:
            conn.close()

    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO brief_runs "
            "(date, slot, payload_json, rendered_md, created_at) VALUES (?,?,?,?,?)",
            (d, slot, json.dumps(payload, ensure_ascii=False), rendered, int(time.time())))
        conn.commit()
        # A concurrent composer may have won the (date, slot) key — then the
        # INSERT was ignored and the STORED row is authoritative, not ours.
        raced = cur.rowcount == 0
        return _row_response(_get_row(conn, d, slot), already=raced)
    finally:
        conn.close()


def mark_sent(slot: str, date: Optional[str] = None) -> dict:
    """Stamp `sent_at` after the transport confirmed delivery. Idempotent: a
    second call keeps the FIRST timestamp (the message went out once)."""
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {', '.join(SLOTS)}")
    d = valid_date(date)
    if not d:
        raise ValueError("date must be YYYY-MM-DD")
    conn = db.get_conn()
    try:
        row = _get_row(conn, d, slot)
        if row is None:
            return {"status": "error", "error": f"no brief for {d} {slot}"}
        if row["sent_at"]:
            return {"status": "ok", "date": d, "slot": slot,
                    "sent_at": row["sent_at"], "already_sent": True}
        now = int(time.time())
        conn.execute(
            "UPDATE brief_runs SET sent_at = ? WHERE date = ? AND slot = ? AND sent_at IS NULL",
            (now, d, slot))
        conn.commit()
        return {"status": "ok", "date": d, "slot": slot,
                "sent_at": now, "already_sent": False}
    finally:
        conn.close()


# Slot order within a day. `created_at` is a whole-second stamp, so two briefs
# composed in the same second (backfill, a test, a cron catch-up) tie — and a
# tie that resolves arbitrarily would make "the latest brief" flicker between
# two answers. The slot's own position in the day breaks it deterministically.
_SLOT_RANK_SQL = "CASE slot WHEN 'morning' THEN 0 WHEN 'midday' THEN 1 ELSE 2 END"


def latest() -> Optional[dict]:
    """The most recently composed brief — the web mirror's read."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            f"SELECT * FROM brief_runs "
            f"ORDER BY created_at DESC, date DESC, {_SLOT_RANK_SQL} DESC LIMIT 1").fetchone()
        return _row_response(row, already=True) if row is not None else None
    finally:
        conn.close()
