"""
Hermes Orchestrator Dashboard — Personal Health module.
Daily ritual timeline: exercise, meditation, meals, supplements, sleep, devocional.
Tracks check-offs per day. Seeds an example protocol.
"""
import time
from datetime import date, datetime, timedelta
from . import db

# ─── Schema ─────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    """Idempotent install: health_routines + health_log + health_config + seed."""
    conn = db.get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS health_routines (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                time_block  TEXT NOT NULL,
                sort_order  INTEGER DEFAULT 0,
                label       TEXT NOT NULL,
                description TEXT,
                link_url    TEXT,
                link_label  TEXT,
                icon        TEXT,
                category    TEXT NOT NULL,
                target_time TEXT,
                active      INTEGER DEFAULT 1,
                created_at  REAL DEFAULT (unixepoch())
            );
            CREATE TABLE IF NOT EXISTS health_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_id  INTEGER NOT NULL REFERENCES health_routines(id),
                log_date    TEXT NOT NULL,
                done_at     REAL,
                note        TEXT,
                UNIQUE(routine_id, log_date)
            );
            CREATE TABLE IF NOT EXISTS health_config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
        _seed(conn)
    finally:
        conn.close()


# ─── Seed data ──────────────────────────────────────────────────────────────

DEVOCIONAL_URL = None

SEED_ROUTINES = [
    # Morning
    ("morning", 1, "🏋️", "exercise", "06:30", "Exercise block",
     "Strength + mobility — first thing in the morning", None, None),
    ("morning", 2, "🧘", "meditation", "07:15", "Morning meditation",
     "Guided session to start the day", None, None),
    ("morning", 3, "🍳", "meal", "08:00", "Breakfast",
     "Balanced plate — 25% of the day", None, None),

    # Midday
    ("midday", 4, "🍽️", "meal", "13:00", "Lunch",
     "Balanced plate — 40% of the day", None, None),

    # Evening
    ("evening", 5, "🌙", "meal", "19:00", "Light dinner",
     "Balanced plate — 25% of the day", None, None),

    # Night
    ("night", 6, "😴", "sleep", "22:30", "Sleep target",
     "Wind down, screens off · latest 23:00", None, None),
]

SEED_CONFIG = {
    "wake_time": "06:30",
    "sleep_target": "22:30",
    "sleep_latest": "23:00",
    "exercise_window": "06:00-08:00",
    "meditation_source": "https://coaching.healthygamer.gg/guide/meditation-tracks?utm_source=nav",
    "devocional_url": DEVOCIONAL_URL or "",
    "plate_cal_exercise": "2200",
    "plate_cal_no_exercise": "1700",
    "plate_macros_carbs": "55%",
    "plate_macros_protein": "20%",
    "plate_macros_fat": "25%",
    "omega3_current_mg": "1000",
    "omega3_target_mg": "2000",
}

ALLOWED_CONFIG_KEYS = frozenset(SEED_CONFIG)
INT_CONFIG_KEYS = frozenset({
    "plate_cal_exercise",
    "plate_cal_no_exercise",
    "omega3_current_mg",
    "omega3_target_mg",
})

CATEGORY_COLORS = {
    "exercise": "emerald",
    "devocional": "purple",
    "supplement": "teal",
    "meal": "amber",
    "meditation": "violet",
    "sleep": "indigo",
}

TIME_BLOCK_LABELS = {
    "morning": "🌅 Mañana",
    "midday": "☀️ Mediodía",
    "evening": "🌆 Tarde",
    "night": "🌙 Noche",
}

TIME_BLOCK_ORDER = ["morning", "midday", "evening", "night"]


_INSERT_ROUTINE = """INSERT INTO health_routines
       (time_block, sort_order, icon, category, target_time, label, description, link_url, link_label)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _seed(conn) -> None:
    """Seed the example routines + config on a fresh (empty) DB only.

    The neutral seed is example content: it must never back-fill into an
    existing timeline, where a tenant's own routine list (different labels)
    would receive duplicate example rows. Tenant protocols live in private
    data, not in this seed."""
    count = conn.execute("SELECT COUNT(*) FROM health_routines").fetchone()[0]
    if count == 0:
        conn.executemany(_INSERT_ROUTINE, SEED_ROUTINES)
        conn.commit()

    cfg_count = conn.execute("SELECT COUNT(*) FROM health_config").fetchone()[0]
    if cfg_count == 0:
        for k, v in SEED_CONFIG.items():
            conn.execute("INSERT INTO health_config (key, value) VALUES (?, ?)", (k, v))
        conn.commit()



# ─── Queries ────────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def get_routines(active_only: bool = True) -> list[dict]:
    """All routines, ordered by time_block → sort_order."""
    conn = db.get_conn()
    try:
        sql = "SELECT * FROM health_routines"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY CASE time_block WHEN 'morning' THEN 0 WHEN 'midday' THEN 1 WHEN 'evening' THEN 2 WHEN 'night' THEN 3 ELSE 4 END, sort_order"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_today() -> dict:
    """Today's canvas: routines grouped by time_block with done status."""
    today = _today()
    conn = db.get_conn()
    try:
        sql = """
            SELECT r.*, CASE WHEN l.done_at IS NOT NULL THEN 1 ELSE 0 END AS done, l.done_at, l.note
            FROM health_routines r
            LEFT JOIN health_log l ON l.routine_id = r.id AND l.log_date = ?
            WHERE r.active = 1
            ORDER BY CASE r.time_block
                WHEN 'morning' THEN 0 WHEN 'midday' THEN 1
                WHEN 'evening' THEN 2 WHEN 'night' THEN 3 ELSE 4
            END, r.sort_order
        """
        rows = conn.execute(sql, (today,)).fetchall()

        grouped = {}
        for r in rows:
            d = dict(r)
            d["done"] = bool(d["done"])
            tb = d["time_block"]
            if tb not in grouped:
                grouped[tb] = []
            grouped[tb].append(d)

        # Ordered blocks
        blocks = []
        for tb in TIME_BLOCK_ORDER:
            if tb in grouped:
                blocks.append({
                    "key": tb,
                    "label": TIME_BLOCK_LABELS.get(tb, tb),
                    "items": grouped[tb],
                })

        total = len(rows)
        done = sum(1 for r in rows if r["done"])
        return {
            "date": today,
            "blocks": blocks,
            "total": total,
            "done": done,
            "remaining": total - done,
            "streak": _streak(conn),
        }
    finally:
        conn.close()


def check_routine(routine_id: int, note: str | None = None) -> dict:
    """Mark a routine done for today (idempotent atomic upsert)."""
    today = _today()
    conn = db.get_conn()
    try:
        exists = conn.execute(
            "SELECT id FROM health_routines WHERE id = ?", (routine_id,)
        ).fetchone()
        if exists is None:
            raise LookupError(f"routine {routine_id} not found")
        conn.execute(
            """INSERT INTO health_log (routine_id, log_date, done_at, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(routine_id, log_date) DO UPDATE SET
                   done_at = excluded.done_at,
                   note = COALESCE(excluded.note, health_log.note)""",
            (routine_id, today, time.time(), note),
        )
        conn.commit()
        return {"routine_id": routine_id, "date": today, "done": True}
    finally:
        conn.close()


def uncheck_routine(routine_id: int) -> dict:
    """Remove done status for today."""
    today = _today()
    conn = db.get_conn()
    try:
        exists = conn.execute(
            "SELECT id FROM health_routines WHERE id = ?", (routine_id,)
        ).fetchone()
        if exists is None:
            raise LookupError(f"routine {routine_id} not found")
        conn.execute(
            "DELETE FROM health_log WHERE routine_id = ? AND log_date = ?",
            (routine_id, today),
        )
        conn.commit()
        return {"routine_id": routine_id, "date": today, "done": False}
    finally:
        conn.close()


def _streak(conn) -> int:
    """Count consecutive days where ALL active routines were done, ending today (or yesterday)."""
    today = _today()
    # Get all dates with at least one log entry, and count active routines
    active_count = conn.execute("SELECT COUNT(*) FROM health_routines WHERE active = 1").fetchone()[0]
    if active_count == 0:
        return 0

    # Count completed routines per date
    rows = conn.execute(
        """SELECT log_date, COUNT(DISTINCT routine_id) as cnt
           FROM health_log
           WHERE log_date <= ?
           GROUP BY log_date
           ORDER BY log_date DESC""",
        (today,),
    ).fetchall()

    completed_dates = {r["log_date"] for r in rows if r["cnt"] >= active_count}

    # Walk backwards from today (or yesterday if today isn't complete yet)
    streak = 0
    d = datetime.strptime(today, "%Y-%m-%d")
    # If today is not fully complete, start from yesterday
    if today not in completed_dates:
        d = d - timedelta(days=1)

    while d.strftime("%Y-%m-%d") in completed_dates:
        streak += 1
        d = d - timedelta(days=1)

    return streak


def get_config() -> dict:
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM health_config").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def update_config(key: str, value: str) -> dict:
    if key not in ALLOWED_CONFIG_KEYS:
        raise ValueError(f"unknown config key: {key}")
    if key in INT_CONFIG_KEYS:
        try:
            int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO health_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
        return {"key": key, "value": value}
    finally:
        conn.close()


def _int_or(value, default: int) -> int:
    """Crash-proof int parse — defense in depth if a bad value is already stored."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_plate_data() -> dict:
    """The Balanced plate reference data (from mi_plato_balanced artifact)."""
    cfg = get_config()
    return {
        "macros": {
            "carbs": cfg.get("plate_macros_carbs", "55%"),
            "protein": cfg.get("plate_macros_protein", "20%"),
            "fat": cfg.get("plate_macros_fat", "25%"),
        },
        "calories": {
            "exercise": _int_or(cfg.get("plate_cal_exercise"), 2200),
            "no_exercise": _int_or(cfg.get("plate_cal_no_exercise"), 1700),
        },
        "segments": {
            "verduras": {
                "label": "Verduras",
                "portion": "½ plato",
                "measure": "Medio plato",
                "note": "Base de cada comida. Variedad y color.",
                "items": ["Espinaca", "Lechuga", "Tomate", "Brócoli", "Calabacita",
                          "Champiñones", "Pimientos", "Ejotes"],
            },
            "proteina": {
                "label": "Proteína",
                "portion": "¼ · palma",
                "measure": "Palma de la mano ≈ 120g",
                "note": "Prioriza pescado graso 2–3×/semana.",
                "items": ["Salmón", "Atún", "Pollo", "Pavo", "Huevo/claras",
                          "Panela sin grasa", "Molida de res magra"],
            },
            "cereales": {
                "label": "Cereal / legumbre",
                "portion": "¼ · puño",
                "measure": "Puño cerrado ≈ 1 taza cocida",
                "note": "Integrales y legumbres aportan fibra.",
                "items": ["Quinoa", "Avena", "Arroz integral", "Tortilla de maíz",
                          "Frijoles", "Garbanzos", "Edamames", "Lentejas"],
            },
        },
        "supplements": [
            {"name": "Vitamina D3", "dose": "2000 UI", "when": "Después del desayuno",
             "note": "Ejemplo genérico — ajusta la dosis con tu médico."},
            {"name": "Omega-3 (EPA/DHA)", "dose": "1000 mg", "when": "Tras la cena",
             "note": "Ejemplo genérico — revisa el objetivo con tu nutrióloga."},
            {"name": "Multivitamínico", "dose": "1 al día", "when": "Mañana",
             "note": "Ejemplo genérico de referencia."},
        ],
        "psoriasis": {
            # Generic sample nutrition reference data (placeholder content).
            "add": [
                "Pescado graso 2–3×/semana",
                "Verduras y fruta variadas todos los días",
                "Fermentados: yogur, chucrut o kimchi",
                "Aceite de oliva extra virgen como grasa principal",
            ],
            "reduce": [
                "Alcohol",
                "Ultraprocesados, azúcar añadida y harinas refinadas",
            ],
            "avoid": [
                "Dietas de eliminación amplias sin respaldo profesional",
                "Cambios drásticos sin supervisión",
            ],
            "method": [
                "Ventana de 8–12 semanas antes de juzgar un cambio",
                "Una variable a la vez",
                "Registra cómo te sientes junto con el cambio",
            ],
        },
        "shopping_lists": {
            "clasica": {
                "meta": "Ejemplo de lista semanal base.",
                "aisles": [
                    {"ic": "🥬", "name": "Frutas y verduras",
                     "items": ["Espinaca", "Lechuga", "Tomate", "Brócoli", "Calabacita",
                               "Champiñones", "Pimientos", "Aguacate", "Manzana", "Fresas"]},
                    {"ic": "🐟", "name": "Proteína",
                     "items": ["Pechuga de pollo", "Salmón", "Atún en lata", "Huevo",
                               "Pavo", "Panela sin grasa", "Molida de res magra"]},
                    {"ic": "🌾", "name": "Cereal y legumbre",
                     "items": ["Avena", "Quinoa", "Arroz integral", "Tortilla de maíz",
                               "Frijoles", "Garbanzos", "Lentejas"]},
                    {"ic": "🫒", "name": "Grasa",
                     "items": ["Aceite de oliva extra virgen", "Nueces", "Almendras"]},
                    {"ic": "🥛", "name": "Lácteos (bajos en lactosa)",
                     "items": ["Yogur griego deslactosado", "Leche deslactosada"]},
                ],
            },
            "medit": {
                "meta": "Semana con foco mediterráneo y pescado graso.",
                "aisles": [
                    {"ic": "🐟", "name": "Pescado graso (2–3×/sem)",
                     "items": ["Salmón", "Sardinas", "Anchoas", "Macarela", "Atún"]},
                    {"ic": "🥬", "name": "Verduras y hojas",
                     "items": ["Espinaca", "Arúgula", "Tomate", "Pimientos", "Calabacita",
                               "Brócoli", "Pepino", "Cebolla morada"]},
                    {"ic": "🫒", "name": "Grasas buenas",
                     "items": ["Aceite de oliva extra virgen", "Aceitunas", "Nueces",
                               "Linaza / chía", "Aguacate"]},
                    {"ic": "🌾", "name": "Integrales y legumbre",
                     "items": ["Quinoa", "Avena", "Lentejas", "Garbanzos", "Frijoles",
                               "Pan integral / masa madre"]},
                    {"ic": "🫙", "name": "Fermentados",
                     "items": ["Chucrut", "Kimchi", "Yogur griego deslactosado"]},
                ],
            },
            "economica": {
                "meta": "Misma estructura, cuidando el bolsillo.",
                "aisles": [
                    {"ic": "🐟", "name": "Proteína rendidora",
                     "items": ["Atún en lata", "Sardina en lata", "Huevo",
                               "Pechuga de pollo", "Frijoles / lentejas"]},
                    {"ic": "🥬", "name": "Verduras de temporada",
                     "items": ["Calabacita", "Zanahoria", "Repollo", "Tomate",
                               "Cebolla", "Espinaca"]},
                    {"ic": "🌾", "name": "Granos base",
                     "items": ["Avena", "Arroz integral", "Tortilla de maíz",
                               "Frijol a granel", "Lenteja a granel"]},
                    {"ic": "🫒", "name": "Grasa",
                     "items": ["Aceite de oliva", "Cacahuate natural"]},
                    {"ic": "🍎", "name": "Fruta",
                     "items": ["Plátano", "Manzana", "Naranja", "Fruta de temporada"]},
                ],
            },
        },
        "alternatives": [
            {"ic": "🥬", "name": "Verduras",
             "t1": ["Espinaca", "Brócoli", "Pimientos", "Tomate", "Arúgula"],
             "t2": ["Calabacita", "Champiñones", "Ejotes", "Zanahoria"],
             "t3": ["Elote", "Papa (con medida)"]},
            {"ic": "🐟", "name": "Proteína",
             "t1": ["Salmón", "Sardina", "Anchoa", "Pollo / pavo", "Claras"],
             "t2": ["Atún en lata", "Huevo entero", "Panela sin grasa", "Molida magra"],
             "t3": ["Quesos grasos", "Carnes procesadas"]},
            {"ic": "🌾", "name": "Cereal y legumbre",
             "t1": ["Quinoa", "Lentejas", "Frijoles", "Garbanzos", "Avena"],
             "t2": ["Arroz integral", "Tortilla de maíz", "Pan integral / masa madre"],
             "t3": ["Arroz blanco", "Pan blanco", "Tostadas fritas"]},
            {"ic": "🍎", "name": "Fruta (al final de la comida)",
             "t1": ["Fresas", "Frutos rojos", "Manzana", "Pera"],
             "t2": ["Plátano", "Naranja", "Mango"],
             "t3": ["Jugos", "Fruta en almíbar"]},
            {"ic": "🫒", "name": "Grasa",
             "t1": ["Aceite de oliva extra virgen", "Nueces", "Linaza / chía", "Aguacate"],
             "t2": ["Almendras", "Aceitunas", "Cacahuate natural"],
             "t3": ["Mantequilla", "Mayonesa", "Frituras"]},
        ],
    }
