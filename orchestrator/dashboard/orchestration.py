"""
Parallel-orchestration layer — the role→spec→work→result→guardrail loop.

This is the sidecar that turns a pile of independent Claude Code sessions into a
governed *fleet*, implementing the six patterns the research (Anthropic +
2025-26 community) converges on for a solo operator running many agents:

  1. SESSIONS BY ROLE   — every session carries a role (implementation /
                          verification / docs / planning / review) so the board
                          shows *what each agent is for*, not just "a claude".
  2. TASK LEDGER        — a structured, append-only result record per finished
                          task ({summary, files_modified, risks, status}) the
                          dashboard reads and the operator reviews.
  3. HOOKS NOTIFICATION — a Claude Code `Notification` hook posts "I need input"
                          up to the dashboard, so a blocked agent surfaces
                          instead of silently waiting (session_events).
  4. AUTO-COMPACT       — detect a session whose transcript is getting large and
                          suggest / auto-send `/compact` (keeps context sharp).
  5. SHARED SPEC        — a per-feature `spec.md` the operator (Hermes) controls;
                          each session pulls only its role's slice (context
                          hygiene + no cross-agent contradiction).
  6. AUTO-ABORT         — a task that fails its contract 3× is killed, logged,
                          and a *clean* restart plan is queued for a fresh
                          instance (circuit breaker, not an infinite retry).

Storage follows the repo convention (graph.py / sprints.py): sidecar tables on
the Hermes kanban DB, plus flat files under ~/.hermes/orchestration for the
things that want to be human- and git-friendly (specs, the ledger mirror).

Nothing here auto-commits load-bearing plan structure — agents report and
propose; the operator's gates (trust, autonomy, review Inbox) stay authoritative
(same doctrine as loop.py's route_result).
"""
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import db

# --- Layout -----------------------------------------------------------------
ORCH_DIR = Path(os.environ.get("ORCH_DIR", str(Path.home() / ".hermes" / "orchestration")))
SPECS_DIR = ORCH_DIR / "specs"
LEDGER_FILE = ORCH_DIR / "ledger.jsonl"

# --- Roles ------------------------------------------------------------------
# The 3 the brief names, plus the two the research adds for a full pipeline.
ROLES = ("implementation", "verification", "docs", "planning", "review")
_ROLE_ALIASES = {
    "impl": "implementation", "implement": "implementation", "code": "implementation",
    "build": "implementation", "dev": "implementation",
    "verify": "verification", "test": "verification", "tests": "verification",
    "qa": "verification", "vfy": "verification",
    "doc": "docs", "documentation": "docs",
    "plan": "planning", "architect": "planning", "design": "planning",
    "rev": "review", "reviewer": "review", "audit": "review",
}

# --- Auto-compact policy ----------------------------------------------------
# A Claude Code jsonl transcript is ~4 bytes/token of raw text but includes tool
# output the live context trims — so bytes/4 is a generous *upper* proxy for how
# full the window is. We flag well before any hard limit so /compact lands early
# (community rule: compact around half-full), and only ever *suggest* unless the
# session opted into auto_compact.
CONTEXT_BUDGET_TOKENS = int(os.environ.get("ORCH_CONTEXT_BUDGET", "160000"))
COMPACT_THRESHOLD = float(os.environ.get("ORCH_COMPACT_THRESHOLD", "0.72"))

# --- Auto-abort policy ------------------------------------------------------
# tasks.consecutive_failures is maintained by Hermes/us; 3 strikes = circuit
# break. Matches the research's "if an agent repeatedly fails, kill the branch,
# log the failure, and hand a clean plan to a new instance".
FAILURE_LIMIT = int(os.environ.get("ORCH_FAILURE_LIMIT", "3"))


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- schema

def ensure_schema() -> None:
    """Idempotently create the orchestration sidecar tables + on-disk dirs.
    Safe to call at every startup (mirrors graph.ensure_schema)."""
    ORCH_DIR.mkdir(parents=True, exist_ok=True)
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    conn = db.get_conn()
    try:
        # (1) Role registry — a session's identity beyond its tmux name.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_meta (
                session_key  TEXT PRIMARY KEY,   -- tmux name OR jsonl session_id
                host         TEXT,
                role         TEXT,               -- one of ROLES
                feature      TEXT,               -- spec/feature this session serves
                project      TEXT,
                auto_compact INTEGER DEFAULT 0,
                auto_abort   INTEGER DEFAULT 1,
                notes        TEXT,
                created_at   INTEGER,
                updated_at   INTEGER
            )""")
        # Additive migration: session tag (prioritization label — critical /
        # experiment / …). Idempotent: errors when the column exists.
        try:
            conn.execute("ALTER TABLE session_meta ADD COLUMN tag TEXT")
        except sqlite3.OperationalError:
            pass
        # (3/4/6) Session lifecycle events — input-needed, compaction, aborts.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT,
                host        TEXT,
                kind        TEXT,   -- input_needed|compact_suggested|compacted|aborted|stop|note|result
                payload     TEXT,   -- json
                resolved_at INTEGER,
                created_at  INTEGER
            )""")
        # (2) Task ledger — the structured RESULT contract, canonical here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_ledger (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        TEXT,
                session_key    TEXT,
                agent          TEXT,
                role           TEXT,
                summary        TEXT,
                files_modified TEXT,   -- json array
                risks          TEXT,   -- json array
                status         TEXT,   -- passed|failed|blocked|partial
                passed         INTEGER,
                created_at     INTEGER
            )""")
        # P3 — performance indexes on session_events + task_ledger.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_events_session "
            "ON session_events(session_key, created_at DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_events_unresolved "
            "ON session_events(resolved_at) WHERE resolved_at IS NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_task "
            "ON task_ledger(task_id, created_at DESC, id DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_role "
            "ON task_ledger(role)")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- (1) roles

def normalize_role(role: Optional[str]) -> Optional[str]:
    if not role:
        return None
    r = role.strip().lower()
    r = _ROLE_ALIASES.get(r, r)
    return r if r in ROLES else None


def role_from_name(session_name: Optional[str]) -> Optional[str]:
    """Best-effort role parse from a `claude-<project>-<role>[-N]` tmux name.
    The zero-config path: name a session `claude-api-verify` and the board knows
    its role even with no registration."""
    if not session_name:
        return None
    stem = re.sub(r"-\d+$", "", session_name)          # drop the auto-number suffix
    parts = stem.split("-")
    for tok in reversed(parts):                        # last meaningful token wins
        r = normalize_role(tok)
        if r:
            return r
    return None


def set_session_role(session_key: str, role: Optional[str] = None,
                     feature: Optional[str] = None, project: Optional[str] = None,
                     host: str = "local", auto_compact: Optional[bool] = None,
                     auto_abort: Optional[bool] = None,
                     notes: Optional[str] = None,
                     tag: Optional[str] = None) -> dict:
    """Register/update a session's role + feature + policy. Upsert keyed on
    session_key (only overwrites fields that were provided)."""
    role_n = normalize_role(role) if role else None
    if role and role_n is None:
        return {"status": "error", "error": f"unknown role '{role}' (want one of {', '.join(ROLES)})"}
    # tag: lowercase slug; empty string = CLEAR (distinct from None = untouched)
    clear_tag = tag == ""
    if tag:
        tag = tag.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", tag):
            return {"status": "error",
                    "error": f"invalid tag '{tag}' (want 1-24 chars of a-z 0-9 - _)"}
    conn = db.get_conn()
    try:
        now = _now()
        existing = conn.execute("SELECT session_key FROM session_meta WHERE session_key = ?", (session_key,)).fetchone()
        if existing:
            sets, params = [], []
            for col, val in (("role", role_n), ("feature", feature), ("project", project),
                             ("host", host), ("notes", notes),
                             ("tag", None if clear_tag else tag),
                             ("auto_compact", None if auto_compact is None else int(auto_compact)),
                             ("auto_abort", None if auto_abort is None else int(auto_abort))):
                if val is not None:
                    sets.append(f"{col} = ?")
                    params.append(val)
            if clear_tag:
                sets.append("tag = NULL")
            sets.append("updated_at = ?")
            params.append(now)
            params.append(session_key)
            conn.execute(f"UPDATE session_meta SET {', '.join(sets)} WHERE session_key = ?", params)
        else:
            conn.execute(
                "INSERT INTO session_meta (session_key, host, role, feature, project, "
                "auto_compact, auto_abort, notes, tag, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_key, host, role_n, feature, project,
                 int(auto_compact) if auto_compact is not None else 0,
                 int(auto_abort) if auto_abort is not None else 1,
                 notes, None if clear_tag else tag, now, now),
            )
        conn.commit()
        return {"status": "ok", "session_key": session_key, "role": role_n,
                "feature": feature, "tag": None if clear_tag else tag}
    finally:
        conn.close()


def get_session_meta(session_key: str) -> Optional[dict]:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM session_meta WHERE session_key = ?", (session_key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def all_session_meta() -> dict:
    """Every registered session, keyed by session_key — for cheap enrichment."""
    conn = db.get_conn()
    try:
        return {r["session_key"]: dict(r) for r in conn.execute("SELECT * FROM session_meta").fetchall()}
    finally:
        conn.close()


def role_summary() -> dict:
    """Count registered sessions by role (dashboard header)."""
    conn = db.get_conn()
    try:
        out = {r: 0 for r in ROLES}
        for row in conn.execute("SELECT role, COUNT(*) n FROM session_meta WHERE role IS NOT NULL GROUP BY role"):
            out[row["role"]] = row["n"]
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- (2) ledger

def append_ledger(task_id: Optional[str], summary: str, files_modified=None, risks=None,
                  status: str = "passed", agent: Optional[str] = None,
                  session_key: Optional[str] = None, role: Optional[str] = None,
                  passed: Optional[bool] = None) -> dict:
    """Write one structured VERIFICATION record to the ledger (DB canonical +
    jsonl mirror).

    Phase 4 (item 1): the ledger is the VERIFICATION record — the VALIDATE
    session is its ONLY writer (§6.4; implementation results go through
    report_result). Enforced here, at the single write choke point:
      • role must be 'verification' (aliases like verify/test/qa normalize);
      • the row needs a task and an agent identity (independence is provable);
      • the verifier may not be the task's implementing agent or session —
        self-review is empirically broken (arXiv 2606.05976)."""
    role_n = normalize_role(role)
    if role_n != "verification":
        return {"status": "error",
                "error": "the task_ledger is the VERIFICATION record — only a "
                         "verification-role session writes it (role='verification'). "
                         "Implementation results go through report_result."}
    if not task_id:
        return {"status": "error", "error": "task_id required for a verification record"}
    if not agent:
        return {"status": "error", "error": "agent identity required (independence must be provable)"}
    conn = db.get_conn()
    try:
        trow = conn.execute("SELECT assignee, session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if trow is None:
        return {"status": "error", "error": "task not found"}
    implementer = trow["assignee"]
    if implementer and agent == implementer and implementer not in ("ricardo", "user"):
        return {"status": "error",
                "error": f"self-review refused: '{agent}' is the implementing agent — "
                         "verification must come from a separate session"}
    if session_key and trow["session_id"] and session_key == trow["session_id"]:
        return {"status": "error",
                "error": "self-review refused: verification must come from a separate "
                         "session, not the implementing one"}
    files_modified = files_modified or []
    risks = risks or []
    if isinstance(files_modified, str):
        files_modified = [f.strip() for f in files_modified.splitlines() if f.strip()]
    if isinstance(risks, str):
        risks = [r.strip() for r in risks.splitlines() if r.strip()]
    if passed is None:
        passed = status in ("passed", "partial")
    now = _now()
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO task_ledger (task_id, session_key, agent, role, summary, "
            "files_modified, risks, status, passed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, session_key, agent, normalize_role(role), summary,
             json.dumps(files_modified), json.dumps(risks), status, int(bool(passed)), now),
        )
        entry_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    entry = {
        "id": entry_id, "task_id": task_id, "session_key": session_key, "agent": agent,
        "role": normalize_role(role), "summary": summary, "files_modified": files_modified,
        "risks": risks, "status": status, "passed": bool(passed), "created_at": now,
    }
    # Best-effort human/git-friendly mirror (locked; MCP + API are separate procs).
    try:
        ORCH_DIR.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_FILE, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(entry) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass
    return entry


def _ledger_row(row) -> dict:
    d = dict(row)
    for k in ("files_modified", "risks"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    d["passed"] = bool(d.get("passed"))
    return d


def get_ledger(limit: int = 50, task_id: Optional[str] = None,
               session_key: Optional[str] = None) -> list:
    conn = db.get_conn()
    try:
        sql = "SELECT * FROM task_ledger"
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if session_key:
            clauses.append("session_key = ?")
            params.append(session_key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [_ledger_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def report_ledger(task_id: Optional[str], summary: str, files_modified=None, risks=None,
                  status: str = "passed", agent: Optional[str] = None,
                  session_key: Optional[str] = None, role: Optional[str] = None,
                  route: bool = True) -> dict:
    """The VALIDATE session's sign-off (Phase 4): record the verification to
    the ledger, log a session event, and (with route=True) drive the task's
    state machine —
      • passed  → hand to loop.report_result on the IMPLEMENTER's behalf
                  (agent=None → the claim holder), so route_result judges the
                  implementing agent's trust, not the verifier's (§7 gate).
      • failed  → count the strike; auto-abort on the 3rd (feature 6).
    Only role='verification' writes land (append_ledger enforces it — the
    ledger is the verification record, never the implementer's self-report)."""
    passed = status in ("passed", "partial")
    entry = append_ledger(task_id, summary, files_modified, risks, status,
                          agent=agent, session_key=session_key, role=role, passed=passed)
    if isinstance(entry, dict) and entry.get("status") == "error":
        return entry
    if session_key:
        record_event(session_key, "result", {"task_id": task_id, "status": status,
                     "summary": summary[:200]}, host=None)
    routing = None
    if route and task_id:
        if passed:
            try:
                from . import loop
                routing = loop.report_result(task_id, summary, passed=True,
                                             artifacts=files_modified, agent=None)
            except Exception as e:
                routing = {"status": "error", "error": str(e)}
        elif status in ("failed", "blocked"):
            routing = record_failed_attempt(task_id, error=summary, agent=agent,
                                            session_key=session_key)
    return {"status": "recorded", "ledger": entry, "routing": routing}


# ---------------------------------------------------------------- (3) session events

def record_event(session_key: str, kind: str, payload: Optional[dict] = None,
                 host: Optional[str] = None) -> dict:
    """Append a session lifecycle event (input_needed / compacted / aborted / …).
    The write-side of the hooks-notification and the auto-compact/abort audit."""
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO session_events (session_key, host, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (session_key, host, kind, json.dumps(payload or {}), _now()),
        )
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid, "kind": kind, "session_key": session_key}
    finally:
        conn.close()


def get_events(limit: int = 50, session_key: Optional[str] = None,
               unresolved_only: bool = False, kind: Optional[str] = None) -> list:
    conn = db.get_conn()
    try:
        sql, params, clauses = "SELECT * FROM session_events", [], []
        if session_key:
            clauses.append("session_key = ?")
            params.append(session_key)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if unresolved_only:
            clauses.append("resolved_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out
    finally:
        conn.close()


def pending_input() -> list:
    """Unresolved `input_needed` events — the attention queue (who's blocked on
    you). This is what the dashboard badges and what a fresh operator checks
    first — only genuine `input_needed` asks, not every unresolved event."""
    return get_events(limit=100, unresolved_only=True, kind="input_needed")


def resolve_event(event_id: int) -> dict:
    conn = db.get_conn()
    try:
        conn.execute("UPDATE session_events SET resolved_at = ? WHERE id = ?", (_now(), event_id))
        conn.commit()
        return {"status": "ok", "id": event_id}
    finally:
        conn.close()


def resolve_inputs_for(session_key: str) -> dict:
    """Clear all open input_needed events for a session — called when the session
    stops or produces new output (the ask has been answered)."""
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "UPDATE session_events SET resolved_at = ? WHERE session_key = ? "
            "AND kind = 'input_needed' AND resolved_at IS NULL",
            (_now(), session_key),
        )
        conn.commit()
        return {"status": "ok", "resolved": cur.rowcount}
    finally:
        conn.close()


# ---------------------------------------------------------------- (4) auto-compact

def context_estimate(size_kb: Optional[int]) -> dict:
    """A transcript-size → context-fullness proxy. Deliberately coarse: bytes/4
    over-estimates real tokens, so a flag here is a safe early warning, never a
    hard claim about the live window."""
    if not size_kb:
        return {"context_tokens": 0, "context_pct": 0, "needs_compact": False}
    tokens = int(size_kb * 1024 / 4)
    pct = round(min(tokens / CONTEXT_BUDGET_TOKENS, 1.5) * 100)
    return {"context_tokens": tokens, "context_pct": pct,
            "needs_compact": tokens >= CONTEXT_BUDGET_TOKENS * COMPACT_THRESHOLD}


def compact_session(host: str, session_name: str, auto: bool = False) -> dict:
    """Send `/compact` to a live session and log it. Used by the operator button
    and by the sweeper for auto_compact sessions."""
    from . import sessions
    res = sessions.send_to_session(host, session_name, "/compact")
    ok = res.get("status") == "sent"
    record_event(session_name, "compacted",
                 {"auto": auto, "sent": ok, "detail": res}, host=host)
    return {"status": "ok" if ok else "error", "sent": ok, "detail": res}


def compact_candidates(sessions_data: dict) -> list:
    """Live sessions past the compact threshold (dashboard highlights these)."""
    out = []
    for s in sessions_data.get("claude_code", []):
        est = context_estimate(s.get("size_kb"))
        if est["needs_compact"] and s.get("status") in ("active", "recent"):
            out.append({"session": s.get("session_id"), "name": s.get("display_name"),
                        "host": s.get("host", "local"), **est})
    return out


# ---------------------------------------------------------------- (5) specs

_SLICE_RE = re.compile(r"^##\s*@([a-z]+)\s*$", re.IGNORECASE | re.MULTILINE)


def _spec_file(feature: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]+", "-", feature.lower()).strip("-") or "feature"
    return SPECS_DIR / safe / "spec.md"


def list_specs() -> list:
    if not SPECS_DIR.exists():
        return []
    out = []
    for d in sorted(SPECS_DIR.iterdir()):
        f = d / "spec.md"
        if f.is_file():
            st = f.stat()
            out.append({"feature": d.name, "size": st.st_size,
                        "updated_at": int(st.st_mtime),
                        "roles": sorted(set(m.lower() for m in _SLICE_RE.findall(f.read_text())))})
    return out


def read_spec(feature: str) -> Optional[str]:
    f = _spec_file(feature)
    return f.read_text() if f.is_file() else None


def write_spec(feature: str, content: str) -> dict:
    """Operator (Hermes) writes the single source-of-truth spec for a feature."""
    f = _spec_file(feature)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return {"status": "ok", "feature": feature, "path": str(f), "bytes": len(content)}


def spec_slice(feature: str, role: Optional[str] = None) -> dict:
    """Return only what a session with this role should see: the `## @all`
    shared preamble + the `## @<role>` section. Untagged content (before any
    @section) is treated as shared. No role → the whole spec.

    This is the context-hygiene win: each agent gets its slice, not the whole
    document, so sessions don't bloat or contradict each other."""
    text = read_spec(feature)
    if text is None:
        return {"status": "error", "error": f"no spec for '{feature}'"}
    role_n = normalize_role(role) if role else None
    if not role_n:
        return {"status": "ok", "feature": feature, "role": None, "slice": text}

    # Split into (tag, body) blocks; content before the first @tag is "shared".
    blocks, order = {"__preamble__": ""}, ["__preamble__"]
    cur = "__preamble__"
    for line in text.splitlines(keepends=True):
        m = _SLICE_RE.match(line.rstrip("\n"))
        if m:
            cur = m.group(1).lower()
            if cur not in blocks:
                blocks[cur] = ""
                order.append(cur)
        else:
            blocks[cur] += line
    keep = [blocks["__preamble__"].strip()]
    if blocks.get("all"):
        keep.append("## Shared\n" + blocks["all"].strip())
    if blocks.get(role_n):
        keep.append(f"## Your slice — {role_n}\n" + blocks[role_n].strip())
    sliced = "\n\n".join(p for p in keep if p).strip()
    return {"status": "ok", "feature": feature, "role": role_n,
            "slice": sliced or text, "sliced": bool(blocks.get(role_n) or blocks.get("all"))}


# ---------------------------------------------------------------- (6) auto-abort

def _create_restart_task(orig, reason: str, plan: str) -> Optional[str]:
    """Queue a CLEAN restart task in the operator Inbox (a fresh plan for a new
    instance — never a blind re-run of the failed one)."""
    title = f"🔄 Restart: {orig['title']}"[:120]
    cmd = ["hermes", "kanban", "create", title, "--json",
           "--assignee", "ricardo", "--created-by", "orchestrator",
           "--body", plan]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        payload = json.loads(r.stdout.strip())
        new_id = payload.get("id") or payload.get("task_id") or (payload.get("task") or {}).get("id")
    except Exception:
        m = re.search(r"\bt_[0-9a-f]+\b", r.stdout if 'r' in dir() else "")
        new_id = m.group(0) if m else None
    if new_id:
        conn = db.get_conn()
        try:
            conn.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                         (orig["id"], new_id))
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                         (new_id, "restart_of", json.dumps({"orig": orig["id"], "reason": reason}), _now()))
            conn.commit()
        finally:
            conn.close()
    return new_id


def _clean_plan(orig, reason: str) -> str:
    """Build the restart brief: the spec slice (if the session had a feature) +
    what to avoid, from the failure record. A fresh instance starts clean."""
    lines = [f"Auto-generated after **{FAILURE_LIMIT} failed attempts** on `{orig['id']}`.",
             "", f"**Original goal:** {orig['title']}", ""]
    body = orig.get("body")
    if body:
        lines += ["**Original brief:**", body.strip(), ""]
    lines += [f"**Why the last instance was aborted:** {reason}", "",
              "**Restart guidance:**",
              "- Start in a fresh session (clean context — do not resume the aborted transcript).",
              "- Re-read the acceptance contract below and plan before editing.",
              "- Address the failure cause above explicitly.", ""]
    acc = _acceptance(body)
    if acc:
        lines += ["## Acceptance", acc]
    return "\n".join(lines)


def _acceptance(body: Optional[str]) -> Optional[str]:
    if not body or "## Acceptance" not in body:
        return None
    return body.split("## Acceptance", 1)[1].strip() or None


def _session_host(session_key: str) -> str:
    """Where does this session live? session_meta.host is authoritative (a
    registered session declared it); else scan the live session inventory for
    a matching session_id / display name; default local."""
    meta = get_session_meta(session_key)
    if meta and meta.get("host"):
        return meta["host"]
    try:
        from . import sessions as _sessions
        for s in _sessions.get_all_sessions().get("claude_code", []):
            if str(s.get("session_id")) == str(session_key) or \
                    s.get("display_name") == session_key:
                return s.get("host") or "local"
    except Exception:
        pass
    return "local"


def record_failed_attempt(task_id: str, error: str = "", agent: Optional[str] = None,
                          session_key: Optional[str] = None) -> dict:
    """A task failed its contract. Increment the strike count and, on the 3rd,
    auto-abort. Below the limit we leave the task claimable for a bounded retry
    (loop.py doctrine: repair a couple of times before escalating)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        fails = (row["consecutive_failures"] or 0) + 1
        # Free the claim so the task can be retried by a fresh instance; keep it
        # in the pool/ready unless we're about to abort.
        conn.execute(
            "UPDATE tasks SET consecutive_failures = ?, last_failure_error = ?, "
            "status = CASE WHEN status = 'in_progress' THEN 'ready' ELSE status END, "
            "claim_lock = NULL, claim_expires = NULL WHERE id = ?",
            (fails, (error or "")[:500], task_id),
        )
        from . import loop as _loop
        _loop.close_run(conn, task_id, "failed", "gave_up", error=(error or "")[:500])
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                     (task_id, "attempt_failed",
                      json.dumps({"n": fails, "error": error[:300], "agent": agent}), _now()))
        conn.commit()
        orig = dict(row)
    finally:
        conn.close()
    if fails >= FAILURE_LIMIT:
        return abort_task(task_id, reason=error or f"{fails} consecutive contract failures",
                          agent=agent, session_key=session_key, _orig=orig)
    return {"status": "failed", "task_id": task_id, "consecutive_failures": fails,
            "limit": FAILURE_LIMIT, "aborted": False}


def abort_task(task_id: str, reason: str = "", agent: Optional[str] = None,
               session_key: Optional[str] = None, kill: bool = True,
               _orig: Optional[dict] = None) -> dict:
    """Circuit-break a task: mark it blocked, kill its live session, log the
    abort, and queue a clean restart plan for a fresh instance."""
    conn = db.get_conn()
    try:
        row = _orig or conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {"status": "error", "error": "task not found"}
        orig = dict(row)
        conn.execute(
            "UPDATE tasks SET status = 'blocked', last_failure_error = ?, "
            "claim_lock = NULL, claim_expires = NULL WHERE id = ?",
            (f"AUTO-ABORT: {reason}"[:500], task_id),
        )
        from . import loop as _loop
        _loop.close_run(conn, task_id, "crashed", "gave_up", error=f"AUTO-ABORT: {reason}"[:500])
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                     (task_id, "auto_aborted",
                      json.dumps({"reason": reason[:300], "agent": agent,
                                  "session": session_key, "after": FAILURE_LIMIT}), _now()))
        conn.commit()
    finally:
        conn.close()

    # Kill the live session (best-effort) — the branch/instance is dead weight.
    # Phase 4 (item 6): resolve the session's ORIGIN HOST instead of assuming
    # local — a fleet session on the Mac/VPS was previously never killed (the
    # kill silently no-opped on the wrong machine).
    killed = None
    sk = session_key or orig.get("session_id")
    if kill and sk:
        host = _session_host(sk)
        try:
            from . import sessions
            killed = sessions.kill_session(host, sk)
        except Exception as e:
            killed = {"status": "error", "error": str(e)}
        record_event(sk, "aborted", {"task_id": task_id, "reason": reason[:200],
                                     "host": host, "killed": killed})

    restart_id = _create_restart_task(orig, reason, _clean_plan(orig, reason))
    return {"status": "aborted", "task_id": task_id, "reason": reason,
            "killed_session": killed, "restart_task": restart_id, "aborted": True}


# How long a running run may sit without a heartbeat before it's a wedge.
RUN_STALE_SECONDS = int(os.environ.get("ORCH_RUN_STALE", str(30 * 60)))


# How long an unanswered input_needed ask stays in the attention queue before
# the sweeper auto-resolves it (UX audit #7: stale asks stack up forever).
INPUT_STALE_SECONDS = int(os.environ.get("ORCH_INPUT_STALE", str(24 * 3600)))


def _session_index(sessions_data: dict) -> dict:
    """session_id/display_name → {project, status, modified} from a live
    sessions blob."""
    idx = {}
    for s in (sessions_data or {}).get("claude_code", []):
        entry = {"project": s.get("project") or s.get("display_name") or "",
                 "status": s.get("status"), "modified": s.get("modified")}
        for key in (s.get("session_id"), s.get("display_name")):
            if key:
                idx[str(key)] = entry
    return idx


# Grace before an ask counts as superseded by later output (the Notification
# hook often fires milliseconds before the same turn keeps streaming).
INPUT_SUPERSEDED_GRACE = int(os.environ.get("ORCH_INPUT_GRACE", "60"))


def resolve_superseded_inputs(sessions_data: dict) -> dict:
    """Auto-resolve input_needed asks the session has MOVED PAST: its
    transcript was written again well after the ask, so the input was either
    given or never really needed (the false-positive class — e.g. the
    orchestrator's own session asking while Fable keeps working).

    REPLACES the earlier gone-idle rule, which had the model backwards: an
    IDLE session with an unanswered ask is the genuinely-waiting case and must
    stay in the queue. Sessions missing from the inventory are left alone
    (SSH blips must not flush the queue; the 24h stale rule reaps the dead)."""
    idx = _session_index(sessions_data)
    resolved = []
    for ev in pending_input():
        s = idx.get(str(ev.get("session_key") or ""))
        if s and s.get("modified") and \
                s["modified"] > (ev.get("created_at") or 0) + INPUT_SUPERSEDED_GRACE:
            resolve_event(ev["id"])
            resolved.append(ev["id"])
    return {"resolved": resolved, "count": len(resolved)}


def resolve_stale_inputs(max_age: int = None) -> dict:
    """Auto-resolve input_needed session events older than max_age (default
    24h): an ask nobody answered in a day is stale context, not an active
    blocker — it leaves the Needs-you queue instead of stacking forever."""
    max_age = max_age or INPUT_STALE_SECONDS
    now = _now()
    conn = db.get_conn()
    try:
        n = conn.execute(
            "UPDATE session_events SET resolved_at = ? WHERE kind = 'input_needed' "
            "AND resolved_at IS NULL AND created_at < ?",
            (now, now - max_age)).rowcount
        conn.commit()
        return {"resolved": n}
    finally:
        conn.close()


def reclaim_orphan_runs() -> dict:
    """Phase 4 (item 4) — task_runs owns liveness. A 'running' run is a WEDGE
    when its claim has expired AND its heartbeat has gone stale, or its
    recorded worker PID is dead on this host. Close it (crashed/reclaimed),
    free the task's claim + run pointers, and put an in-progress task back to
    'ready' so a fresh instance can claim it. Idempotent; sweeper-driven."""
    now = _now()
    conn = db.get_conn()
    reclaimed = []
    try:
        rows = conn.execute(
            "SELECT id AS run_id, task_id, worker_pid, claim_expires, "
            "last_heartbeat_at, started_at FROM task_runs WHERE status = 'running'"
        ).fetchall()
        for r in rows:
            beat = r["last_heartbeat_at"] or r["started_at"] or 0
            stale = (r["claim_expires"] or 0) < now and beat < now - RUN_STALE_SECONDS
            pid_dead = False
            if r["worker_pid"]:
                try:
                    os.kill(r["worker_pid"], 0)
                except ProcessLookupError:
                    pid_dead = True
                except Exception:
                    pass
            if not (stale or pid_dead):
                continue
            why = "dead worker pid" if pid_dead else "stale heartbeat + expired claim"
            conn.execute(
                "UPDATE task_runs SET status = 'crashed', outcome = 'reclaimed', "
                "ended_at = ?, error = ? WHERE id = ? AND ended_at IS NULL",
                (now, f"reclaimed by sweeper: {why}", r["run_id"]))
            # Free the task only where this run is still its live one.
            conn.execute(
                "UPDATE tasks SET current_run_id = NULL, current_step_key = NULL, "
                "claim_lock = NULL, claim_expires = NULL, "
                "status = CASE WHEN status = 'in_progress' THEN 'ready' ELSE status END "
                "WHERE id = ? AND current_run_id = ?",
                (r["task_id"], r["run_id"]))
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (r["task_id"], "run_reclaimed",
                 json.dumps({"run_id": r["run_id"], "why": why, "via": "sweeper"}), now))
            reclaimed.append({"run_id": r["run_id"], "task_id": r["task_id"], "why": why})
        conn.commit()
    finally:
        conn.close()
    return {"reclaimed": reclaimed, "count": len(reclaimed)}


# ---------------------------------------------------------------- sweeper (4 + 6, autonomous)

# --- Auto-tag policy (runs inside sweep) ---------------------------------------
# Two POLICY-OWNED tags: sessions idle past the threshold get 'stale';
# sessions with a recent trouble event (aborted) get 'needs-attention'
# (trumps stale). The policy only ever writes/clears WITHIN this vocabulary —
# a manual tag (critical, experiment, …) is never touched, and when the
# condition clears the auto tag clears with it.
AUTO_TAGS = {"stale", "needs-attention"}
AUTO_TAG_IDLE_SECONDS = int(os.environ.get("ORCH_AUTOTAG_IDLE", str(24 * 3600)))
AUTO_TAG_ERROR_WINDOW = int(os.environ.get("ORCH_AUTOTAG_ERROR_WINDOW", str(24 * 3600)))


def auto_tag_sessions(sessions_data: dict) -> dict:
    now = _now()
    conn = db.get_conn()
    try:
        trouble = {str(r["session_key"]) for r in conn.execute(
            "SELECT DISTINCT session_key FROM session_events "
            "WHERE kind = 'aborted' AND created_at > ?",
            (now - AUTO_TAG_ERROR_WINDOW,))}
    finally:
        conn.close()
    metas = all_session_meta()
    out = {"stale": 0, "needs_attention": 0, "cleared": 0}
    for sess in (sessions_data or {}).get("claude_code", []):
        sid = str(sess.get("session_id") or "")
        if not sid:
            continue
        cur = (metas.get(sid) or {}).get("tag")
        if cur and cur not in AUTO_TAGS:
            continue  # manual tag — sacred
        idle = now - (sess.get("modified") or now)
        if sid in trouble or str(sess.get("display_name") or "") in trouble:
            want = "needs-attention"
        elif idle > AUTO_TAG_IDLE_SECONDS:
            want = "stale"
        else:
            want = None
        if want == cur:
            continue
        if want is None:
            set_session_role(sid, tag="")
            out["cleared"] += 1
        else:
            set_session_role(sid, tag=want, host=sess.get("host", "local"))
            out["stale" if want == "stale" else "needs_attention"] += 1
    return out


def sweep() -> dict:
    """One autonomous pass:
      • auto-compact live sessions that opted in and are over threshold.
      • auto-abort in-progress tasks whose strike count already hit the limit
        (a failure recorded out-of-band, e.g. by a CI hook).
    Idempotent and best-effort; the background task calls this on an interval."""
    from . import sessions
    out = {"compacted": [], "aborted": [], "ts": _now()}
    # Phase 4 (item 4): reclaim wedged runs first — a freed task can then be
    # re-claimed in the same sweep cycle.
    try:
        out["runs_reclaimed"] = reclaim_orphan_runs()["reclaimed"]
    except Exception:
        out["runs_reclaimed"] = []
    # Phase 4 (item 5): graduate autonomy on the docs/research class.
    try:
        from . import governance as _gov
        out["autonomy_graduated"] = _gov.graduate_autonomy()["graduated"]
    except Exception:
        out["autonomy_graduated"] = []
    # UX audit #7: stale input_needed asks (>24h) auto-resolve out of Needs-you.
    try:
        out["stale_inputs_resolved"] = resolve_stale_inputs()["resolved"]
    except Exception:
        out["stale_inputs_resolved"] = 0
    # Superseded asks resolve (the session produced output after asking — the
    # input was given or never needed). Idle sessions' asks stay: those are
    # the genuinely-waiting ones.
    try:
        _sd = sessions.get_all_sessions()
        out["superseded_inputs_resolved"] = resolve_superseded_inputs(_sd)["count"]
        out["auto_tags"] = auto_tag_sessions(_sd)
    except Exception:
        out["superseded_inputs_resolved"] = 0
    meta = all_session_meta()

    try:
        data = sessions.get_all_sessions()
    except Exception:
        data = {"claude_code": []}
    for s in data.get("claude_code", []):
        sid = str(s.get("session_id", ""))
        name = s.get("display_name", "")
        m = meta.get(sid) or meta.get(name)
        if not m or not m.get("auto_compact"):
            continue
        if s.get("status") not in ("active", "recent"):
            continue
        if not context_estimate(s.get("size_kb"))["needs_compact"]:
            continue
        # Don't spam: skip if we compacted this session in the last 10 min.
        recent = get_events(limit=5, session_key=m["session_key"])
        if any(e["kind"] == "compacted" and _now() - e["created_at"] < 600 for e in recent):
            continue
        res = compact_session(s.get("host", "local"), name, auto=True)
        out["compacted"].append({"session": name, "result": res})

    # Auto-abort: tasks over the strike limit still sitting in-progress.
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, consecutive_failures FROM tasks WHERE consecutive_failures >= ? "
            "AND status IN ('in_progress','ready','blocked') AND reviewed_at IS NULL",
            (FAILURE_LIMIT,),
        ).fetchall()
        stuck = [r["id"] for r in rows]
    finally:
        conn.close()
    for tid in stuck:
        # Only abort those not already aborted (no prior auto_aborted event).
        prior = db.get_conn()
        try:
            done = prior.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'auto_aborted' LIMIT 1", (tid,)
            ).fetchone()
        finally:
            prior.close()
        if done:
            continue
        res = abort_task(tid, reason=f"{FAILURE_LIMIT}+ consecutive failures (sweeper)")
        out["aborted"].append({"task_id": tid, "result": res})

    # Business rituals (daily-grade, guarded by idempotency):
    from . import sprints as _sprints
    # Cycle roll — close expired active cycle, open next week's empty.
    try:
        rolled = _sprints.roll_cycle()
        if rolled.get("rolled"):
            out["cycle_rolled"] = rolled
    except Exception:
        out["cycle_rolled"] = {"error": "roll_cycle failed (best-effort)"}
    # Sprint ledger reconciliation — repair forward/reverse orphans.
    try:
        rec = _sprints.reconcile_sprint_ledger()
        if rec.get("forward_repaired") or rec.get("reverse_repaired"):
            out["ledger_reconciled"] = rec
    except Exception:
        out["ledger_reconciled"] = {"error": "reconcile_sprint_ledger failed (best-effort)"}

    return out
