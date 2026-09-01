"""Tests for the personal Health module (daily ritual timeline + check-offs).

Covers:
  1. dashboard/health.py — schema/seed, today canvas, check/uncheck, config,
     plate data, streaks — against the NEW contract:
       * check_routine / uncheck_routine raise LookupError for unknown ids
       * check_routine upserts with COALESCE (re-check preserves saved note)
       * update_config validates key (ALLOWED_CONFIG_KEYS) + int values
         (INT_CONFIG_KEYS) with ValueError
       * get_plate_data survives corrupt calorie config (integer defaults)
  2. /api/health/* endpoints (LookupError -> 404, ValueError -> 400)
  3. mcp_server wiring for the 6 health tools

Isolation (mirrors tests/test_time_blocks.py): dashboard.db.get_conn() reads
the module global db.KANBAN_DB at call time, so a module-scoped autouse
fixture points it at a fresh temp sqlite file for the whole module and
restores it afterwards. The REAL ~/.hermes/kanban.db is never touched.

Run:  python -m pytest tests/test_health.py -v
"""
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db as _db
from dashboard import health


ALL_KEYS_TODAY = ("blocks", "total", "done", "remaining", "streak")
MCP_HEALTH_TOOLS = (
    "get_health_today",
    "get_health_routines",
    "check_health_routine",
    "uncheck_health_routine",
    "get_health_config",
    "get_health_plate",
)


# ─── Isolation fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _isolated_db():
    """Point db.KANBAN_DB at a COPY of the real kanban.db for this whole module.

    Mirrors tests/test_time_blocks.py: copy the real DB → temp file so all
    existing tables (tasks, projects, initiatives, agents, deals, etc.) already
    exist and the import-time migrations in dashboard.api don't crash on FK
    constraints or missing columns.  health_log is wiped per-test by _clean_log.
    """
    import shutil
    real = _db.KANBAN_DB
    fd, tmp = tempfile.mkstemp(prefix="kanban_test_health_", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    if real.exists():
        shutil.copy(str(real), str(tmp_path))
    _db.KANBAN_DB = tmp_path
    try:
        health.ensure_schema()
        yield
    finally:
        _db.KANBAN_DB = real
        try:
            tmp_path.unlink()
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _clean_log():
    """Order-independence: start every test with an empty health_log and
    deterministic routine IDs (re-seed so IDs are 1-12)."""
    conn = _db.get_conn()
    try:
        conn.execute("DELETE FROM health_log")
        conn.execute("DELETE FROM health_routines")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='health_routines'")
        from dashboard.health import SEED_ROUTINES
        for r in SEED_ROUTINES:
            conn.execute(
                """INSERT INTO health_routines
                   (time_block, sort_order, icon, category, target_time, label, description, link_url, link_label)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r,
            )
        conn.commit()
    finally:
        conn.close()
    yield


def _active_ids() -> list[int]:
    return [r["id"] for r in health.get_routines()]


def _today_item(routine_id: int) -> dict:
    t = health.get_today()
    return [i for b in t["blocks"] for i in b["items"] if i["id"] == routine_id][0]


# ─── Schema / seed ───────────────────────────────────────────────────────────

def test_schema_creates_tables():
    """ensure_schema is idempotent — calling twice is safe."""
    health.ensure_schema()
    health.ensure_schema()  # no crash
    routines = health.get_routines()
    assert len(routines) >= len(health.SEED_ROUTINES), \
        "should seed every SEED_ROUTINES entry"


def test_today_returns_blocks():
    t = health.get_today()
    for key in ALL_KEYS_TODAY:
        assert key in t
    assert t["total"] >= len(health.SEED_ROUTINES)
    assert t["done"] >= 0
    assert len(t["blocks"]) == 4  # morning/midday/evening/night


def test_blocks_have_correct_order():
    t = health.get_today()
    labels = [b["key"] for b in t["blocks"]]
    assert labels == ["morning", "midday", "evening", "night"]


# ─── Check / uncheck ─────────────────────────────────────────────────────────

def test_check_and_uncheck():
    r = health.check_routine(1)
    assert r["done"] is True

    t = health.get_today()
    assert t["done"] == 1
    assert _today_item(1)["done"] is True

    r2 = health.uncheck_routine(1)
    assert r2["done"] is False

    t2 = health.get_today()
    assert t2["done"] == 0


def test_check_is_idempotent():
    """Checking the same routine twice doesn't duplicate."""
    health.check_routine(2)
    health.check_routine(2)  # idempotent
    t = health.get_today()
    done_count = sum(1 for b in t["blocks"] for item in b["items"] if item["done"])
    assert done_count == 1
    health.uncheck_routine(2)


def test_check_unknown_routine_404s_at_module_level():
    """Unknown routine ids raise LookupError (the API maps it to 404)."""
    with pytest.raises(LookupError):
        health.check_routine(99999)
    with pytest.raises(LookupError):
        health.uncheck_routine(99999)


def test_note_preserved_on_recheck():
    """Re-checking WITHOUT a note must not clobber a previously saved note
    (single upsert with COALESCE)."""
    health.check_routine(1, note="con cafe")
    health.check_routine(1)  # no note — must preserve, not null out
    assert _today_item(1)["note"] == "con cafe"
    health.uncheck_routine(1)


# ─── Streaks ─────────────────────────────────────────────────────────────────

def test_streak_calculation():
    """Streak counts consecutive fully-complete days."""
    # No routines done today → streak should be 0
    t = health.get_today()
    assert t["streak"] == 0

    # Complete all routines today
    ids = _active_ids()
    for i in ids:
        health.check_routine(i)

    t2 = health.get_today()
    assert t2["done"] == len(ids)
    # Today is complete → streak should be at least 1
    assert t2["streak"] >= 1

    # Clean up
    for i in ids:
        health.uncheck_routine(i)


def test_multiday_streak():
    """Yesterday + day-before fully logged, then complete today → streak 3."""
    ids = _active_ids()
    conn = _db.get_conn()
    try:
        for delta in (1, 2):
            log_date = (date.today() - timedelta(days=delta)).isoformat()
            for rid in ids:
                conn.execute(
                    "INSERT INTO health_log (routine_id, log_date, done_at) "
                    "VALUES (?, ?, ?)",
                    (rid, log_date, time.time()),
                )
        conn.commit()
    finally:
        conn.close()

    for rid in ids:
        health.check_routine(rid)

    assert health.get_today()["streak"] == 3

    # Clean up (the autouse fixture would too, but be explicit)
    conn = _db.get_conn()
    try:
        conn.execute("DELETE FROM health_log")
        conn.commit()
    finally:
        conn.close()


# ─── Config ──────────────────────────────────────────────────────────────────

def test_config_get_and_update():
    cfg = health.get_config()
    assert "wake_time" in cfg
    assert cfg["wake_time"] == "06:30"

    health.update_config("wake_time", "07:00")
    cfg2 = health.get_config()
    assert cfg2["wake_time"] == "07:00"

    # Restore
    health.update_config("wake_time", "06:30")


def test_config_rejects_unknown_key():
    assert "wake_time" in health.ALLOWED_CONFIG_KEYS
    with pytest.raises(ValueError):
        health.update_config("nonexistent_key", "x")
    assert "nonexistent_key" not in health.get_config()


def test_config_rejects_non_int_value():
    assert "plate_cal_exercise" in health.INT_CONFIG_KEYS
    before = health.get_config()["plate_cal_exercise"]
    with pytest.raises(ValueError):
        health.update_config("plate_cal_exercise", "not-a-number")
    # Value must be unchanged after the rejected write
    assert health.get_config()["plate_cal_exercise"] == before


# ─── Plate data ──────────────────────────────────────────────────────────────

def test_plate_data():
    p = health.get_plate_data()
    for key in ("macros", "calories", "segments", "supplements",
                "psoriasis", "shopping_lists", "alternatives"):
        assert key in p

    assert len(p["segments"]) == 3  # verduras, proteina, cereales
    assert len(p["supplements"]) >= 1
    for supp in p["supplements"]:
        for key in ("name", "dose", "when", "note"):
            assert key in supp
    assert len(p["shopping_lists"]) == 3  # clasica, medit, economica


def test_plate_survives_corrupt_config():
    """A corrupt calorie value in health_config (written past validation)
    must not crash get_plate_data — it falls back to the integer defaults."""
    conn = _db.get_conn()
    try:
        conn.execute(
            "UPDATE health_config SET value = 'garbage' "
            "WHERE key IN ('plate_cal_exercise', 'plate_cal_no_exercise')"
        )
        conn.commit()
    finally:
        conn.close()

    try:
        p = health.get_plate_data()  # must not raise
        assert p["calories"]["exercise"] == int(health.SEED_CONFIG["plate_cal_exercise"])
        assert p["calories"]["no_exercise"] == int(health.SEED_CONFIG["plate_cal_no_exercise"])
        assert isinstance(p["calories"]["exercise"], int)
        assert isinstance(p["calories"]["no_exercise"], int)
    finally:
        # Restore the seeded values
        conn = _db.get_conn()
        try:
            for key in ("plate_cal_exercise", "plate_cal_no_exercise"):
                conn.execute(
                    "UPDATE health_config SET value = ? WHERE key = ?",
                    (health.SEED_CONFIG[key], key),
                )
            conn.commit()
        finally:
            conn.close()


# ─── Seeded content ──────────────────────────────────────────────────────────

def test_routines_can_carry_links():
    """A routine's link_url/link_label round-trip through get_routines
    (behavior pin — the neutral seed itself ships no links)."""
    conn = _db.get_conn()
    try:
        conn.execute(
            """INSERT INTO health_routines
               (time_block, sort_order, icon, category, target_time,
                label, description, link_url, link_label)
               VALUES ('morning', 99, '🔗', 'meditation', '07:45',
                       'Linked routine', 'link plumbing probe',
                       'https://example.com/guide', 'Open guide')""",
        )
        conn.commit()
    finally:
        conn.close()

    routines = health.get_routines()
    probe = [r for r in routines if r["label"] == "Linked routine"][0]
    assert probe["link_url"] == "https://example.com/guide"
    assert probe["link_label"] == "Open guide"



def test_sleep_routines_exist():
    """The night block seeds at least one sleep-category routine."""
    routines = health.get_routines()
    night = [r for r in routines if r["time_block"] == "night"]
    assert night, "night block should be seeded"
    assert any(r["category"] == "sleep" for r in night)
    assert "Sleep target" in [r["label"] for r in night]


def test_seeded_categories_are_renderable():
    """Every seeded routine's category has a front-end color mapping."""
    routines = health.get_routines()
    assert routines
    for r in routines:
        assert r["category"] in health.CATEGORY_COLORS, r["label"]


# ─── API endpoints ───────────────────────────────────────────────────────────

def test_api_health_endpoints():
    """GET/POST/PATCH /api/health/* — 200s, plus LookupError→404 and
    ValueError→400 mappings. Imported lazily so TestClient requests run while
    db.KANBAN_DB points at the temp file (conftest sets TESTING=1, so the
    mutating calls need no Bearer token)."""
    from dashboard.api import app
    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/api/health/today")
    assert r.status_code == 200
    body = r.json()
    for key in ALL_KEYS_TODAY:
        assert key in body

    r = client.get("/api/health/plate")
    assert r.status_code == 200
    plate = r.json()
    for key in ("macros", "segments", "supplements"):
        assert key in plate

    r = client.post("/api/health/routines/1/check")
    assert r.status_code == 200
    r = client.post("/api/health/routines/1/uncheck")
    assert r.status_code == 200

    # Unknown routine → 404 (LookupError mapped by the API layer)
    r = client.post("/api/health/routines/99999/check")
    assert r.status_code == 404

    # Unknown config key → 400 (ValueError mapped by the API layer)
    r = client.patch(
        "/api/health/config",
        params={"key": "nonexistent_key", "value": "x"},
    )
    assert r.status_code == 400


# ─── MCP wiring ──────────────────────────────────────────────────────────────

def test_mcp_health_tools_wired():
    """The 6 health tools are declared in TOOLS and routed in TOOL_HANDLERS."""
    import mcp_server

    tool_names = {t["name"] for t in mcp_server.TOOLS}
    for name in MCP_HEALTH_TOOLS:
        assert name in tool_names, f"{name} missing from mcp_server.TOOLS"
        assert name in mcp_server.TOOL_HANDLERS, \
            f"{name} missing from mcp_server.TOOL_HANDLERS"
