"""
Hermes Orchestrator Dashboard — Daily Reflection module (v2).

Reflection–Action Loop (Harvard Business School 15-min end-of-day debrief,
docs/BRIEF-daily-reflection.md v2): morning = 1-3 intentions for the day;
evening = 3 parts — what went well (wins {what, why}), what didn't go as
planned (misses {what, what_happened, why}), what I'll do differently
(adjustments {action, when}). Facts → meaning → next step; secular, no examen.

One row per local date in `daily_reflections` (BRIEF §4.1 columns). Prompt
generation is deterministic — no LLM call, no invented data: the evening
prompt only echoes what the DB and the persisted day review actually contain.
The v1 (examen) table is renamed to `daily_reflections_v1_archive` on first
boot — archived, not transformed, because the examen fields (gratitude, "ser"
intentions, learning, commitment) have no faithful mapping onto wins/misses.
"""
import json
import os
import re
from datetime import date as _date, datetime as _datetime
from pathlib import Path
from typing import Optional

from . import db

VALID_SOURCES = {"telegram", "dashboard", "cron"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Methodology counts (BRIEF §2): 1-3 wins, 1-2 misses, 1-2 adjustments.
# Caps are hard; the minimums are soft except wins ≥ 1, so an honest partial
# Telegram reply is never dropped (v1 philosophy, Sol finding #3 middle ground).
MAX_WINS, MAX_MISSES, MAX_ADJUSTMENTS = 3, 2, 2
TIMELINE_PROMPT_LINES = 12   # bounded slice of timeline_text in the prompt

# Hardcoded Spanish names — the box has no es_MX locale guarantee.
_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun",
              "jul", "ago", "sep", "oct", "nov", "dic"]


# ─── Schema ─────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    """Idempotent install of the v2 table (BRIEF §4.1) + v1 archive migration.

    A table whose columns include `morning` is the v1 (examen) shape: it is
    RENAMED to daily_reflections_v1_archive verbatim — any rows a machine
    holds are preserved, never transformed.
    """
    conn = db.get_conn()
    try:
        cols = [r["name"] for r in
                conn.execute("PRAGMA table_info(daily_reflections)")]
        if "morning" in cols:
            conn.execute("ALTER TABLE daily_reflections "
                         "RENAME TO daily_reflections_v1_archive")
            # The v1 index travels with the renamed table; drop the name so
            # the v2 index below can be created fresh.
            conn.execute("DROP INDEX IF EXISTS idx_daily_reflections_date")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_reflections (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date                TEXT NOT NULL UNIQUE,
                morning_intentions  TEXT,
                morning_created_at  TEXT,
                morning_completed   TEXT,
                evening_wins        TEXT,
                evening_misses      TEXT,
                evening_adjustments TEXT,
                evening_created_at  TEXT,
                day_review_data     TEXT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_reflections_date
            ON daily_reflections(date);
        """)
        conn.commit()
    finally:
        conn.close()


# ─── Helpers ────────────────────────────────────────────────────────────────

def today_str() -> str:
    """Local calendar date, aligned with the rest of the dashboard."""
    return _date.today().isoformat()


def _now_iso() -> str:
    return _datetime.now().astimezone().isoformat(timespec="seconds")


def _valid_date(value: Optional[str]) -> str:
    value = (value or "").strip()
    if not _DATE_RE.match(value):
        raise ValueError("date debe ser YYYY-MM-DD")
    return value


def _fmt_es(date_str: str) -> str:
    """'2026-07-20' → 'lunes 20 jul'."""
    d = _date.fromisoformat(date_str)
    return f"{_DAYS_ES[d.weekday()]} {d.day} {_MONTHS_ES[d.month - 1]}"


def _reflections_dir() -> Path:
    """Backup dir, resolved at call time so tests can repoint it via env."""
    return Path(os.environ.get("HERMES_REFLECTIONS_DIR",
                               str(Path.home() / ".hermes" / "reflections")))


def _check_source(source: str) -> str:
    if source not in VALID_SOURCES:
        raise ValueError(f"source inválido: {source!r}")
    return source


def _clean_list(items, label: str, cap: int = 3) -> list:
    """1–cap non-empty strings; fewer than asked is accepted (honest partial
    replies are never dropped), never more than cap."""
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"{label} debe ser una lista de textos")
    cleaned = [str(s).strip() for s in items if str(s).strip()]
    if not cleaned:
        raise ValueError(f"{label}: se necesita al menos una respuesta")
    if len(cleaned) > cap:
        raise ValueError(f"{label}: máximo {cap} respuestas")
    return cleaned


def _clean_dicts(items, label: str, required: str, optional: tuple,
                 cap: int, min_items: int = 0) -> list:
    """Validate a Harvard list: dicts with one required non-empty key,
    optional keys coerced to stripped strings. Entries whose required field
    is empty are dropped, not rejected — the UI sends blank rows."""
    if items is None:
        items = []
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"{label} debe ser una lista")
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{label}: cada elemento debe ser un objeto")
        main = str(item.get(required) or "").strip()
        if not main:
            continue
        entry = {required: main}
        for key in optional:
            entry[key] = str(item.get(key) or "").strip()
        cleaned.append(entry)
    if len(cleaned) < min_items:
        raise ValueError(f"{label}: se necesita al menos {min_items} "
                         f"con '{required}'")
    if len(cleaned) > cap:
        raise ValueError(f"{label}: máximo {cap}")
    return cleaned


def _parse_col(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _normalize_completed(raw, intentions: list) -> list[bool]:
    """One strict boolean per canonical intention; old/corrupt rows are false.

    A nullable additive column makes the migration backward compatible. Never
    coerce ``1``/``"true"`` into completion: ambiguous stored state is not proof
    that the operator completed a goal.
    """
    parsed = _parse_col(raw)
    if not isinstance(parsed, list):
        parsed = []
    return [parsed[i] if i < len(parsed) and type(parsed[i]) is bool else False
            for i in range(len(intentions))]


def _row_dict(date: str, row) -> dict:
    """API/UI shape: morning/evening grouped, None when that half is unsaved."""
    if not row:
        return {"date": date, "morning": None, "evening": None,
                "day_review": None, "created_at": None}
    morning = None
    if row["morning_intentions"]:
        intentions = _parse_col(row["morning_intentions"]) or []
        morning = {"intentions": intentions,
                   "completed": _normalize_completed(
                       row["morning_completed"], intentions),
                   "created_at": row["morning_created_at"]}
    evening = None
    if row["evening_wins"]:
        evening = {"wins": _parse_col(row["evening_wins"]) or [],
                   "misses": _parse_col(row["evening_misses"]) or [],
                   "adjustments": _parse_col(row["evening_adjustments"]) or [],
                   "created_at": row["evening_created_at"]}
    return {"date": row["date"], "morning": morning, "evening": evening,
            "day_review": _parse_col(row["day_review_data"]),
            "created_at": row["created_at"]}


# ─── Cognitive-load check-in (READ ONLY — `cogload mark` owns the store) ────
#
# The check-in is not a second ritual: its numbers arrive inside the two
# prompts the operator already answers, and they are written through `cogload mark`,
# which is the store's only writer. What lives here is the READ, so the
# reflection and the Personal tab show one shared truth instead of two
# half-truths. A missing check-in is never an error, and an unreachable
# collector must never break a reflection — hence the total degradation to
# {"morning": None, "evening": None}.

CARGA_MEASURES = ("anx", "anx_day", "stress", "eff", "tol")


def _load_labels_safe() -> list:
    try:
        from . import cogload as _cg
        return _cg.load_labels() or []
    except Exception:
        return []


def load_carga(date_str: str, labels: Optional[list] = None) -> dict:
    """This date's check-in, by moment: {"morning": {...}|None, "evening": …}.

    Last writer wins WITHIN a moment (a correction replaces the mistake it
    corrects); the two moments are never folded together — comparing them is
    the reason both are asked.
    """
    out: dict = {"morning": None, "evening": None}
    if labels is None:
        labels = _load_labels_safe()
    try:
        from . import cogload as _cg
        for lab in labels:
            ld = _cg._label_date(lab)
            if ld is None or ld.isoformat() != date_str:
                continue
            slot = lab.get("slot") or "evening"
            if slot not in out:
                continue
            prev = out[slot]
            if prev and (prev.get("ts") or "") > (lab.get("ts") or ""):
                continue
            entry = {"ts": lab.get("ts"), "slot": slot}
            for k in CARGA_MEASURES:
                v = lab.get(k)
                if v is not None:
                    entry[k] = v
            out[slot] = entry
    except Exception:
        return {"morning": None, "evening": None}
    return out


# ─── Reads ──────────────────────────────────────────────────────────────────

def get_reflection(date_str: Optional[str] = None) -> dict:
    """One day's reflection (default today); empty structure when unsaved."""
    date_str = _valid_date(date_str or today_str())
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM daily_reflections WHERE date = ?",
            (date_str,)).fetchone()
        out = _row_dict(date_str, row)
    finally:
        conn.close()
    out["carga"] = load_carga(date_str)
    return out


def get_history(days: int = 7) -> list:
    days = max(1, min(int(days or 7), 90))
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM daily_reflections ORDER BY date DESC LIMIT ?",
            (days,)).fetchall()
        out = [_row_dict(r["date"], r) for r in rows]
    finally:
        conn.close()
    # One label read for the whole history, not one per row.
    labels = _load_labels_safe()
    for entry in out:
        entry["carga"] = load_carga(entry["date"], labels)
    return out


def get_today() -> dict:
    """Today + 7-day history — the MCP get_reflection_today shape."""
    out = get_reflection(today_str())
    out["history"] = get_history(7)
    return out


# ─── Day review integration (read the persisted store, never collect) ───────

def _day_reviews_file() -> Path:
    override = os.environ.get("HERMES_DAY_REVIEWS_FILE")
    if override:
        return Path(override)
    from . import day_review as _dr
    return _dr.DEFAULT_REVIEW_STORE


def load_day_review(date_str: str) -> Optional[dict]:
    """The persisted day-review row for a date ({date, stats, timeline_text})
    or None. Pure read of the jsonl store — never triggers a live collect."""
    path = _day_reviews_file()
    if not path.exists():
        return None
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("date") == date_str:
            return row
    return None


def _timeline_lines(review: Optional[dict], cap: int) -> list:
    """Hour lines from timeline_text: no title, no blanks; gaps stay (honest)."""
    text = (review or {}).get("timeline_text") or ""
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.startswith("📊")]
    return lines[:cap]


def prefill_from_day_review(date_str: Optional[str] = None) -> dict:
    """Candidate wins from the persisted day review (BRIEF §3.2/§8.2).

    Conservative extraction (Sol finding #2): only `Completed: …` fragments —
    real task completions — ever become win candidates; headings, gaps,
    session noise and cron lines never do. Also returns the bounded timeline
    for the read-only Personal-tab card.
    """
    date_str = _valid_date(date_str or today_str())
    review = load_day_review(date_str)
    if not review:
        return {"available": False, "date": date_str, "wins": [],
                "timeline": []}
    wins, seen = [], set()
    for line in _timeline_lines(review, cap=50):
        for frag in line.split(" · "):
            # The first fragment of a line carries the clock prefix
            # ("10:00am  Completed: …"), so match anywhere, not startswith.
            if "Completed: " not in frag:
                continue
            what = frag.split("Completed: ", 1)[1].strip()
            if what and what not in seen:
                seen.add(what)
                wins.append({"what": what, "why": ""})
            if len(wins) >= MAX_WINS:
                break
        if len(wins) >= MAX_WINS:
            break
    return {"available": True, "date": date_str, "wins": wins,
            "timeline": _timeline_lines(review, cap=TIMELINE_PROMPT_LINES),
            "stats": review.get("stats") or {}}


# ─── Writes ─────────────────────────────────────────────────────────────────

def _backup(date_str: str) -> None:
    """Mirror the full row to ~/.hermes/reflections/<date>.json.
    Best-effort: a backup failure must never fail the save."""
    try:
        target = _reflections_dir()
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{date_str}.json").write_text(
            json.dumps(get_reflection(date_str), ensure_ascii=False, indent=2))
    except OSError as e:
        print(f"[reflection] backup {date_str} falló: {e}")


def save_morning(date_str: str, intentions: list,
                 source: str = "dashboard") -> dict:
    """Save 1-3 morning intentions (BRIEF §2 variante matutina)."""
    date_str = _valid_date(date_str)
    _check_source(source)
    intentions = _clean_list(intentions, "intentions")
    conn = db.get_conn()
    try:
        current = conn.execute(
            "SELECT morning_intentions, morning_completed "
            "FROM daily_reflections WHERE date = ?", (date_str,)).fetchone()
        old_intentions = (_parse_col(current["morning_intentions"]) or []) \
            if current else []
        old_completed = _normalize_completed(
            current["morning_completed"] if current else None,
            old_intentions)
        # Progress belongs to a slot only while BOTH its index and exact text
        # stay unchanged. Editing/reordering cannot silently attach completion
        # to a different goal.
        completed = [
            old_completed[i]
            if i < len(old_intentions) and old_intentions[i] == goal else False
            for i, goal in enumerate(intentions)
        ]
        conn.execute(
            "INSERT INTO daily_reflections(date, morning_intentions, "
            "morning_created_at, morning_completed) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "morning_intentions = excluded.morning_intentions, "
            "morning_created_at = excluded.morning_created_at, "
            "morning_completed = excluded.morning_completed",
            (date_str, json.dumps(intentions, ensure_ascii=False), _now_iso(),
             json.dumps(completed)))
        conn.commit()
    finally:
        conn.close()
    _backup(date_str)
    return get_reflection(date_str)


def set_morning_progress(date_str: str, index: int, completed: bool) -> dict:
    """Set exactly one morning goal's binary completion state."""
    date_str = _valid_date(date_str)
    if type(index) is not int:
        raise ValueError("index debe ser un entero")
    if type(completed) is not bool:
        raise ValueError("completed debe ser booleano")
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT morning_intentions, morning_completed "
            "FROM daily_reflections WHERE date = ?", (date_str,)).fetchone()
        if not row or not row["morning_intentions"]:
            raise ValueError("no hay reflexión matutina para esa fecha")
        intentions = _parse_col(row["morning_intentions"]) or []
        if index < 0 or index >= len(intentions):
            raise ValueError("index fuera de rango")
        progress = _normalize_completed(row["morning_completed"], intentions)
        progress[index] = completed
        conn.execute(
            "UPDATE daily_reflections SET morning_completed = ? WHERE date = ?",
            (json.dumps(progress), date_str))
        conn.commit()
    finally:
        conn.close()
    _backup(date_str)
    return get_reflection(date_str)


def save_evening(date_str: str, wins: list, misses: list, adjustments: list,
                 source: str = "dashboard",
                 day_review_data: Optional[dict] = None) -> dict:
    """Save the Reflection–Action Loop (BRIEF §2):
    wins        [{what, why}]                 — 1-3, what required
    misses      [{what, what_happened, why}]  — 0-2, what required
    adjustments [{action, when}]              — 0-2, action required
    The persisted day review is snapshotted into day_review_data when present.
    """
    date_str = _valid_date(date_str)
    _check_source(source)
    wins = _clean_dicts(wins, "wins", "what", ("why",),
                        cap=MAX_WINS, min_items=1)
    misses = _clean_dicts(misses, "misses", "what", ("what_happened", "why"),
                          cap=MAX_MISSES)
    adjustments = _clean_dicts(adjustments, "adjustments", "action", ("when",),
                               cap=MAX_ADJUSTMENTS)
    if day_review_data is None:
        review = load_day_review(date_str)
        if review:
            day_review_data = {"stats": review.get("stats"),
                               "timeline_text": review.get("timeline_text")}
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO daily_reflections(date, evening_wins, evening_misses,"
            " evening_adjustments, evening_created_at, day_review_data) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "evening_wins = excluded.evening_wins, "
            "evening_misses = excluded.evening_misses, "
            "evening_adjustments = excluded.evening_adjustments, "
            "evening_created_at = excluded.evening_created_at, "
            "day_review_data = excluded.day_review_data",
            (date_str,
             json.dumps(wins, ensure_ascii=False),
             json.dumps(misses, ensure_ascii=False),
             json.dumps(adjustments, ensure_ascii=False),
             _now_iso(),
             json.dumps(day_review_data, ensure_ascii=False)
             if day_review_data else None))
        conn.commit()
    finally:
        conn.close()
    _backup(date_str)
    return get_reflection(date_str)


# ─── Prompt generation (deterministic — BRIEF §7) ────────────────────────────

def generate_morning_prompt(date_str: Optional[str] = None) -> str:
    date_str = _valid_date(date_str or today_str())
    return f"""☀️ Buenos días — {_fmt_es(date_str)}

¿Qué quieres lograr hoy?
1. ____________________
2. ____________________
3. ____________________

(Escribe 1-3 cosas. Las guardo en tu reflexión diaria.)

Opcional: si algo te preocupa, dilo y lo convertimos en una acción.

🧠 Carga (10 segundos)
Dos números 1-5, en este orden:
  ansiedad AHORA · estrés AHORA
Ej: "2 3". Si no te late hoy, sáltalo."""


def generate_evening_prompt(day_review: Optional[dict],
                            morning: Optional[dict]) -> str:
    """Reflection–Action Loop prompt from REAL data only: if the day review
    or the morning intentions are missing, say so — never fabricate."""
    date_str = today_str()
    if morning and morning.get("intentions"):
        intentions_line = "Esta mañana quisiste lograr: " + \
            ", ".join(morning["intentions"]) + "."
    else:
        intentions_line = "Hoy no registraste intenciones en la mañana."
    timeline = _timeline_lines(day_review, cap=TIMELINE_PROMPT_LINES)
    if timeline:
        review_block = "📊 Tu día hoy:\n" + "\n".join(timeline)
    else:
        review_block = ("No tengo el day review de hoy — "
                        "cuéntame tú cómo estuvo.")
    return f"""🌙 Reflexión de 15 minutos — {_fmt_es(date_str)} (Reflection–Action Loop)

{intentions_line}
{review_block}

PARTE 1 — ✅ ¿Qué salió bien hoy? (~5 min)
1-3 cosas. Para cada una, ¿por qué funcionó?

PARTE 2 — ⚠️ ¿Qué no salió como esperabas? (~5 min)
1-2 momentos. ¿Qué pasó? ¿Por qué crees que pasó así?

PARTE 3 — 🔄 ¿Qué harás diferente mañana? (~5 min)
1-2 ajustes concretos, escritos como próximos pasos específicos.

PARTE 4 — 🧠 Carga (15 segundos)
Cuatro números 1-5, en este orden:
  ansiedad AHORA · ansiedad de TODO EL DÍA · estrés AHORA · efectividad del día
Ej: "2 3 4 3". Si no te late hoy, sáltalo.

Responde las partes que quieras y las guardo en tu sección Personal."""
