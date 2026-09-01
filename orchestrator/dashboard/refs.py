"""Deterministic human-reference resolution for journey entities."""
import sqlite3
from typing import Optional

from . import db


_KINDS = {
    "project": ("projects", "name"),
    "deal": ("deals", "title"),
    "account": ("accounts", "name"),
}


def _resolved(kind: str, rows) -> dict:
    """Turn an ordered row set into a unique or typed ambiguous result."""
    if not rows:
        return {"ok": False, "code": "not_found"}
    if len(rows) > 1:
        return {
            "ok": False,
            "code": "ambiguous",
            "candidates": [
                {"id": row["id"], "name": row["name"]} for row in rows[:4]
            ],
        }
    row = rows[0]
    return {"ok": True, "id": row["id"], "kind": kind, "name": row["name"]}


def resolve(kind: str, ref, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Resolve an entity reference without guessing between multiple matches.

    Resolution is deliberately ordered: exact id, exact project slug, exact
    case-insensitive display name, then case-insensitive prefix/substring.
    A caller-supplied connection remains owned by the caller.
    """
    if kind not in _KINDS:
        allowed = ", ".join(_KINDS)
        raise ValueError(f"kind must be one of: {allowed}")

    wanted = str(ref).strip() if ref is not None else ""
    if not wanted:
        return {"ok": False, "code": "not_found"}

    table, name_column = _KINDS[kind]
    own_conn = conn is None
    conn = conn or db.get_conn()
    try:
        select = f"SELECT id, {name_column} AS name FROM {table}"

        rows = conn.execute(
            f"{select} WHERE id = ? LIMIT 1", (wanted,)
        ).fetchall()
        if rows:
            return _resolved(kind, rows)

        if kind == "project":
            rows = conn.execute(
                f"{select} WHERE slug = ? ORDER BY id LIMIT 5", (wanted,)
            ).fetchall()
            if rows:
                return _resolved(kind, rows)

        rows = conn.execute(
            f"{select} WHERE {name_column} = ? COLLATE NOCASE "
            f"ORDER BY lower({name_column}), id LIMIT 5",
            (wanted,),
        ).fetchall()
        if rows:
            return _resolved(kind, rows)

        rows = conn.execute(
            f"{select} WHERE instr(lower({name_column}), lower(?)) > 0 "
            f"ORDER BY CASE WHEN substr(lower({name_column}), 1, length(?)) = lower(?) "
            f"THEN 0 ELSE 1 END, lower({name_column}), id LIMIT 5",
            (wanted, wanted, wanted),
        ).fetchall()
        return _resolved(kind, rows)
    finally:
        if own_conn:
            conn.close()
