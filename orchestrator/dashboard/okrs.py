"""The operator's personal OKRs and immutable check-in history.

This module deliberately contains only the personal-OKR surface. It never loads
meeting transcripts at runtime and never mixes commercial/project data into the
personal surface.

The CONTENT is tenant data, not code: the seed catalog (principles, objectives,
key results, baseline check-in) resolves at call time from
  1. $HERMES_OKRS_SEED — path to a seed JSON (set-but-empty forces the built-in
     example, which is how the tests pin the shipped default),
  2. ~/.hermes/okrs-seed.json — the tenant's private seed, outside the repo,
  3. the built-in neutral example below.
A tenant seed only changes TEXT and baseline metadata; the schema, validation,
and append-only history semantics are code and identical for every tenant.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import date as _date, datetime as _datetime
from pathlib import Path

from . import db


VALID_STATUSES = {"not_started", "active", "at_risk", "paused", "completed"}
VALID_SOURCES = {"dashboard", "mcp", "fireflies", "manual"}

# --- Built-in neutral example seed (structure is load-bearing; text is not) --

EXAMPLE_SEED = {
    "principles": [
        "La estabilidad de ingresos desbloquea los demás objetivos.",
        "La diversidad de frentes es una señal de equilibrio.",
        "El balance trabajo–vida requiere seguimiento deliberado.",
        "Priorizar en secuencia: estabilizar antes de expandir.",
    ],
    "okrs": [
        {
            "id": "economic", "year": 2026, "sort_order": 1,
            "title": "Económico", "area": "Ingresos y gastos",
            "target": "Estabilizar y elevar ingresos conservando control de gastos.",
            "horizon": "2026", "status": "at_risk", "progress": None,
            "current_note": "Ejemplo: facturación por debajo de la meta anual.",
        },
        {
            "id": "motivation", "year": 2026, "sort_order": 2,
            "title": "Motivación",
            "area": "Investigación, dirección, ejecución y comunicación",
            "target": "Activar proyectos alineados en varios frentes.",
            "horizon": "2026", "status": "active", "progress": None,
            "current_note": "Ejemplo: un frente activo de tres.",
        },
        {
            "id": "round_man", "year": 2026, "sort_order": 3,
            "title": "Desarrollo integral",
            "area": "Crecimiento intelectual, emocional y artístico",
            "target": "Crecer de forma deliberada más allá del trabajo.",
            "horizon": "2026", "status": "active", "progress": 80,
            "current_note": "Ejemplo: lectura constante; arte activo pendiente.",
        },
        {
            "id": "ordered_life", "year": 2026, "sort_order": 4,
            "title": "Vida ordenada", "area": "Salud y relaciones",
            "target": "Sostener una vida estructurada que proteja salud y vínculos.",
            "horizon": "2026", "status": "active", "progress": 80,
            "current_note": "Ejemplo: rutina y descanso retomándose.",
        },
    ],
    "key_results": [
        ["economic.billing", "economic", 1, "Elevar facturación", "Meta mensual definida", "at_risk", 47,
         "Ejemplo: por debajo de la meta."],
        ["economic.transition", "economic", 2, "Decidir la transición laboral", "Decisión tomada en fecha", "completed", 100,
         "Ejemplo: transición completada."],
        ["economic.savings", "economic", 3, "Cerrar el año con ahorro", "Meta anual de ahorro", "at_risk", None,
         "Ejemplo: saldo no registrado."],
        ["motivation.researcher", "motivation", 1, "Investigación: admisión a posgrado", "Programa de posgrado o equivalente", "paused", None,
         "Ejemplo: pausado hasta estabilizar ingresos."],
        ["motivation.director", "motivation", 2, "Dirección: mentoría o programa ejecutivo", "Programa estructurado o equivalente", "active", None,
         "Ejemplo: búsqueda activa."],
        ["motivation.executor", "motivation", 3, "Ejecución: proyecto sostenible", "De idea a ingresos", "active", None,
         "Ejemplo: iniciativa en construcción."],
        ["motivation.communicator", "motivation", 4, "Comunicación: contenido y enseñanza", "Estrategia activa y medible", "active", None,
         "Ejemplo: conferencia en el horizonte."],
        ["motivation.diversity", "motivation", 5, "Diversidad de impacto", "Tres frentes activos (3/3)", "at_risk", 33,
         "Ejemplo: un frente activo."],
        ["round.reading", "round_man", 1, "Lectura y escucha activa", "Hábito consistente", "active", 70,
         "Ejemplo: autoevaluación aproximada."],
        ["round.passive_art", "round_man", 2, "Arte pasivo", "Una salida cultural al mes", "active", None,
         "Ejemplo: salida reciente realizada."],
        ["round.active_art", "round_man", 3, "Arte activo", "Una producción trimestral", "active", None,
         "Ejemplo: pieza en progreso."],
        ["round.wellbeing", "round_man", 4, "Bienestar y descanso", "Apoyo profesional y descanso intencional", "active", None,
         "Ejemplo: balance en fricción."],
        ["ordered.body", "ordered_life", 1, "Cuerpo", "Meta de peso definida", "active", None,
         "Ejemplo: dieta retomándose."],
        ["ordered.schedule", "ordered_life", 2, "Horario", "Rutina estructurada", "active", None,
         "Ejemplo: mayormente estructurado."],
        ["ordered.driving", "ordered_life", 3, "Movilidad", "Habilidad y acceso para emergencias", "not_started", None,
         "Ejemplo: no retomado."],
        ["ordered.relationships", "ordered_life", 4, "Relaciones y seguridad familiar", "Vínculos intencionales y plan familiar", "active", None,
         "Ejemplo: plan avanzando."],
    ],
    "baseline_checkin": {
        "id": "seed_baseline", "date": "2026-01-05", "source": "manual",
        "source_reference": None, "note": "Línea base del ejemplo.",
        "created_at": "2026-01-05T12:00:00+00:00",
    },
    "seeded_updated_at": "2026-01-05T00:00:00+00:00",
}


def _seed() -> dict:
    """Resolve the seed catalog at call time (env > tenant file > example)."""
    override = os.environ.get("HERMES_OKRS_SEED")
    if override is not None:
        if not override.strip():
            return EXAMPLE_SEED
        candidates = [Path(override)]
    else:
        candidates = [Path.home() / ".hermes" / "okrs-seed.json"]
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and raw.get("okrs") and raw.get("key_results"):
            merged = dict(EXAMPLE_SEED)
            merged.update({k: raw[k] for k in
                           ("principles", "okrs", "key_results",
                            "baseline_checkin", "seeded_updated_at") if k in raw})
            return merged
    return EXAMPLE_SEED


def _now() -> str:
    return _datetime.now().astimezone().isoformat(timespec="seconds")


def install_schema(conn) -> None:
    """Install tables, immutable-history triggers, and the baseline seed.

    Uses only the supplied connection and never commits so m14 can remain one
    transaction with the migration ledger and backup gate.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS personal_okrs (
        id TEXT PRIMARY KEY, year INTEGER NOT NULL, sort_order INTEGER NOT NULL,
        title TEXT NOT NULL, area TEXT NOT NULL, target TEXT NOT NULL,
        horizon TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER,
        current_note TEXT NOT NULL DEFAULT '', updated_at TEXT,
        CHECK(progress IS NULL OR (progress BETWEEN 0 AND 100))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS personal_key_results (
        id TEXT PRIMARY KEY, okr_id TEXT NOT NULL REFERENCES personal_okrs(id) ON DELETE CASCADE,
        sort_order INTEGER NOT NULL, title TEXT NOT NULL, target TEXT NOT NULL,
        status TEXT NOT NULL, progress INTEGER, progress_note TEXT NOT NULL DEFAULT '',
        updated_at TEXT, CHECK(progress IS NULL OR (progress BETWEEN 0 AND 100))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS personal_okr_checkins (
        id TEXT PRIMARY KEY, checkin_date TEXT NOT NULL, source TEXT NOT NULL,
        source_reference TEXT, note TEXT NOT NULL DEFAULT '', snapshot_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_personal_okrs_year ON personal_okrs(year, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_personal_krs_okr ON personal_key_results(okr_id, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_personal_checkins_date ON personal_okr_checkins(checkin_date DESC, created_at DESC)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS personal_checkins_no_update
        BEFORE UPDATE ON personal_okr_checkins BEGIN
        SELECT RAISE(ABORT, 'personal check-ins are append-only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS personal_checkins_no_delete
        BEFORE DELETE ON personal_okr_checkins BEGIN
        SELECT RAISE(ABORT, 'personal check-ins are append-only'); END""")

    seed = _seed()
    stamp = seed.get("seeded_updated_at") or "2026-01-01T00:00:00+00:00"
    for item in seed["okrs"]:
        conn.execute("""INSERT OR IGNORE INTO personal_okrs
            (id,year,sort_order,title,area,target,horizon,status,progress,current_note,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
            item["id"], item["year"], item["sort_order"], item["title"], item["area"],
            item["target"], item["horizon"], item["status"], item["progress"],
            item["current_note"], stamp,
        ))
    for kr in seed["key_results"]:
        conn.execute("""INSERT OR IGNORE INTO personal_key_results
            (id,okr_id,sort_order,title,target,status,progress,progress_note,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (*kr, stamp))

    baseline = seed.get("baseline_checkin")
    if baseline and baseline.get("id"):
        exists = conn.execute(
            "SELECT 1 FROM personal_okr_checkins WHERE id=?", (baseline["id"],)
        ).fetchone()
        if not exists:
            snapshot = _compose(conn, 2026, history_limit=0)
            conn.execute("""INSERT INTO personal_okr_checkins
                (id,checkin_date,source,source_reference,note,snapshot_json,created_at)
                VALUES (?,?,?,?,?,?,?)""", (
                baseline["id"], baseline["date"], baseline["source"],
                baseline.get("source_reference"), baseline.get("note", ""),
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                baseline["created_at"],
            ))


def ensure_schema() -> None:
    conn = db.get_conn()
    try:
        install_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _objective_rows(conn, year: int) -> list[dict]:
    objectives = []
    rows = conn.execute(
        "SELECT * FROM personal_okrs WHERE year=? ORDER BY sort_order", (year,)
    ).fetchall()
    counts = {r["okr_id"]: r["n"] for r in conn.execute(
        """SELECT kr.okr_id, COUNT(DISTINCT c.id) AS n
           FROM personal_key_results kr CROSS JOIN personal_okr_checkins c
           GROUP BY kr.okr_id"""
    ).fetchall()}
    for row in rows:
        objective = dict(row)
        objective["key_results"] = [dict(kr) for kr in conn.execute(
            "SELECT * FROM personal_key_results WHERE okr_id=? ORDER BY sort_order",
            (row["id"],),
        ).fetchall()]
        objective["checkin_count"] = counts.get(row["id"], 0)
        objectives.append(objective)
    return objectives


def _compose(conn, year: int, history_limit: int) -> dict:
    result = {"year": year, "principles": list(_seed()["principles"]),
              "objectives": _objective_rows(conn, year), "history": []}
    if history_limit:
        rows = conn.execute(
            """SELECT * FROM personal_okr_checkins
               ORDER BY checkin_date DESC, created_at DESC LIMIT ?""",
            (history_limit,),
        ).fetchall()
        result["history"] = [{
            "id": row["id"], "date": row["checkin_date"], "source": row["source"],
            "source_reference": row["source_reference"], "note": row["note"],
            "snapshot": json.loads(row["snapshot_json"]), "created_at": row["created_at"],
        } for row in rows]
    return result


def get_okrs(year: int = 2026, history_limit: int = 5) -> dict:
    try:
        year = int(year)
        history_limit = max(0, min(int(history_limit), 50))
    except (TypeError, ValueError):
        raise ValueError("year e history_limit deben ser enteros")
    conn = db.get_conn()
    try:
        return _compose(conn, year, history_limit)
    finally:
        conn.close()


def _progress(value, label: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError(f"{label} debe ser un entero entre 0 y 100 o null")
    return value


def _status(value, label: str) -> str:
    if value not in VALID_STATUSES:
        raise ValueError(f"{label} inválido")
    return value


def _clean_text(value, label: str, cap: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) > cap:
        raise ValueError(f"{label}: máximo {cap} caracteres")
    return text


def save_checkin(payload: dict) -> dict:
    """Apply a partial state update and append the resulting full snapshot."""
    if not isinstance(payload, dict):
        raise ValueError("check-in debe ser un objeto")
    changes = payload.get("objectives")
    if not isinstance(changes, list) or not changes:
        raise ValueError("objectives requiere al menos un objetivo")
    source = payload.get("source") or "dashboard"
    if source not in VALID_SOURCES:
        raise ValueError("source inválido")
    checkin_date = str(payload.get("date") or _date.today().isoformat())
    try:
        _date.fromisoformat(checkin_date)
    except ValueError:
        raise ValueError("date debe ser YYYY-MM-DD")
    note = _clean_text(payload.get("note"), "note")
    source_reference = _clean_text(payload.get("source_reference"), "source_reference", 500) or None

    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for change in changes:
            if not isinstance(change, dict) or not change.get("id"):
                raise ValueError("cada objetivo requiere id")
            oid = str(change["id"])
            if not conn.execute("SELECT 1 FROM personal_okrs WHERE id=?", (oid,)).fetchone():
                raise LookupError(f"objetivo desconocido: {oid}")
            fields, values = [], []
            if "progress" in change:
                fields.append("progress=?"); values.append(_progress(change["progress"], "progress"))
            if "status" in change:
                fields.append("status=?"); values.append(_status(change["status"], "status"))
            if "current_note" in change:
                fields.append("current_note=?"); values.append(_clean_text(change["current_note"], "current_note"))
            if fields:
                fields.append("updated_at=?"); values.append(_now()); values.append(oid)
                conn.execute(f"UPDATE personal_okrs SET {', '.join(fields)} WHERE id=?", values)
            for kr in change.get("key_results") or []:
                if not isinstance(kr, dict) or not kr.get("id"):
                    raise ValueError("cada key result requiere id")
                kid = str(kr["id"])
                owner = conn.execute("SELECT okr_id FROM personal_key_results WHERE id=?", (kid,)).fetchone()
                if not owner or owner["okr_id"] != oid:
                    raise LookupError(f"key result desconocido para {oid}: {kid}")
                kfields, kvals = [], []
                if "progress" in kr:
                    kfields.append("progress=?"); kvals.append(_progress(kr["progress"], "KR progress"))
                if "status" in kr:
                    kfields.append("status=?"); kvals.append(_status(kr["status"], "KR status"))
                if "progress_note" in kr:
                    kfields.append("progress_note=?"); kvals.append(_clean_text(kr["progress_note"], "progress_note"))
                if kfields:
                    kfields.append("updated_at=?"); kvals.append(_now()); kvals.append(kid)
                    conn.execute(f"UPDATE personal_key_results SET {', '.join(kfields)} WHERE id=?", kvals)

        snapshot = _compose(conn, 2026, history_limit=0)
        conn.execute("""INSERT INTO personal_okr_checkins
            (id,checkin_date,source,source_reference,note,snapshot_json,created_at)
            VALUES (?,?,?,?,?,?,?)""", (
            "okr_" + secrets.token_hex(8), checkin_date, source,
            source_reference, note,
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), _now(),
        ))
        conn.commit()
        return get_okrs(2026, 10)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
