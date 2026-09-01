"""Hermes Dashboard — Friday five-question weekly reflection log."""

import re
import time
from datetime import date
from typing import Optional

from . import db


ANSWER_KEYS = (
    "q_regale",
    "q_declare",
    "q_referido",
    "q_propuesta",
    "q_aprendi",
)
_ANSWER_KEY_SET = frozenset(ANSWER_KEYS)
_WEEK_RE = re.compile(r"^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$")


def _error(message: str) -> dict:
    return {"status": "error", "error": message}


def _current_week() -> str:
    iso = date.today().isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _valid_week(week: Optional[str]) -> str:
    value = _current_week() if week is None else week
    if not isinstance(value, str) or not _WEEK_RE.fullmatch(value):
        raise ValueError("week must be YYYY-Www (01-53)")
    return value


def _row_dict(row) -> Optional[dict]:
    if row is None:
        return None
    return {key: row[key] for key in
            ("week", *ANSWER_KEYS, "created_at", "updated_at")}


def save_week(answers: dict, week: Optional[str] = None) -> dict:
    """Insert or partially update one ISO week's supplied answers."""
    if not isinstance(answers, dict):
        return _error("answers must be a dict")
    if not answers:
        return _error("answers must contain at least one answer")

    unknown = set(answers) - _ANSWER_KEY_SET
    if unknown:
        return _error("unknown answer key(s): "
                      + ", ".join(sorted(map(str, unknown))))
    invalid = [key for key, value in answers.items()
               if not isinstance(value, str)]
    if invalid:
        return _error("answers must be strings: " + ", ".join(sorted(invalid)))
    try:
        week = _valid_week(week)
    except ValueError as exc:
        return _error(str(exc))

    keys = [key for key in ANSWER_KEYS if key in answers]
    now = int(time.time())
    columns = ["week", *keys, "created_at", "updated_at"]
    values = [week, *(answers[key] for key in keys), now, now]
    assignments = [f"{key} = excluded.{key}" for key in keys]
    assignments.append(
        "updated_at = CASE "
        "WHEN weekly_reflections.updated_at >= excluded.updated_at "
        "THEN weekly_reflections.updated_at + 1 "
        "ELSE excluded.updated_at END"
    )
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO weekly_reflections({', '.join(columns)}) "
        f"VALUES({placeholders}) ON CONFLICT(week) DO UPDATE SET "
        + ", ".join(assignments)
    )

    conn = db.get_conn()
    try:
        conn.execute(sql, values)
        conn.commit()
    finally:
        conn.close()

    row = get_week(week)
    return {"status": "ok", **row}


def get_week(week: Optional[str] = None) -> Optional[dict]:
    """Return one ISO week's reflection, or ``None`` when it is unsaved."""
    try:
        week = _valid_week(week)
    except ValueError as exc:
        return _error(str(exc))

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT week, q_regale, q_declare, q_referido, q_propuesta, "
            "q_aprendi, created_at, updated_at "
            "FROM weekly_reflections WHERE week = ?",
            (week,),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def history(n: int = 8) -> list:
    """Return at most ``n`` weekly reflections, newest ISO week first."""
    try:
        limit = max(0, int(n))
    except (TypeError, ValueError):
        limit = 8

    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT week, q_regale, q_declare, q_referido, q_propuesta, "
            "q_aprendi, created_at, updated_at "
            "FROM weekly_reflections ORDER BY week DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row)
            item["answered"] = sum(bool(item[key]) for key in ANSWER_KEYS)
            result.append(item)
        return result
    finally:
        conn.close()
