"""Daily Reflection v2 (Reflection–Action Loop) — module + API + MCP contract.

Isolation mirrors tests/test_health.py: db.KANBAN_DB is repointed at a copy of
the real kanban.db; the JSON backup dir is repointed via HERMES_REFLECTIONS_DIR
and the day-review store via HERMES_DAY_REVIEWS_FILE (both resolved at call
time, so the fixture can set them after import). The FastAPI app is imported
under the repointed DB (test_funnel_trend.py precedent) so all six v2 routes
are exercised through TestClient — including 404s for the removed v1 routes.

Run:  python -m pytest tests/test_reflection.py -v
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db as _db
from dashboard import reflection

MCP_REFLECTION_TOOLS = (
    "get_reflection_today",
    "save_reflection_morning",
    "save_reflection_evening",
)

_CLIENT = None


# ─── Isolation fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _isolated_db(tmp_path_factory):
    import shutil
    global _CLIENT
    real = _db.KANBAN_DB
    fd, tmp = tempfile.mkstemp(prefix="kanban_test_reflection_", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    if real.exists():
        shutil.copy(str(real), str(tmp_path))
    _db.KANBAN_DB = tmp_path
    backup_dir = tmp_path_factory.mktemp("reflections")
    os.environ["HERMES_REFLECTIONS_DIR"] = str(backup_dir)
    # Empty-by-default day-review store: prefill tests write their own.
    reviews = tmp_path_factory.mktemp("dayreviews") / "day-reviews.jsonl"
    os.environ["HERMES_DAY_REVIEWS_FILE"] = str(reviews)
    try:
        reflection.ensure_schema()
        from dashboard.api import app
        from starlette.testclient import TestClient
        _CLIENT = TestClient(app, raise_server_exceptions=False)
        yield
    finally:
        _db.KANBAN_DB = real
        os.environ.pop("HERMES_REFLECTIONS_DIR", None)
        os.environ.pop("HERMES_DAY_REVIEWS_FILE", None)
        try:
            tmp_path.unlink()
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _clean_table():
    conn = _db.get_conn()
    try:
        conn.execute("DELETE FROM daily_reflections")
        conn.commit()
    finally:
        conn.close()


def _win(what="cerré el schema", why="lo hice temprano"):
    return {"what": what, "why": why}


# ─── Schema + v1→v2 migration ────────────────────────────────────────────────

def test_schema_v2_columns_and_unique_date():
    conn = _db.get_conn()
    try:
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(daily_reflections)")}
        assert {"date", "morning_intentions", "morning_created_at",
                "morning_completed",
                "evening_wins", "evening_misses", "evening_adjustments",
                "evening_created_at", "day_review_data",
                "created_at"} <= cols
        assert "morning" not in cols          # v1 shape is gone
        conn.execute("INSERT INTO daily_reflections(date) VALUES('2026-07-01')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_reflections(date) VALUES('2026-07-01')")
    finally:
        conn.close()


def test_ensure_schema_idempotent():
    reflection.ensure_schema()
    reflection.ensure_schema()


def test_m13_adds_progress_without_rewriting_existing_reflections():
    from dashboard.migrations.m13_reflection_goals import m13_reflection_goals
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript("""
            CREATE TABLE daily_reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                morning_intentions TEXT,
                morning_created_at TEXT
            );
            INSERT INTO daily_reflections(date, morning_intentions)
            VALUES('2026-07-01', '["meta existente"]');
        """)
        m13_reflection_goals(conn)
        m13_reflection_goals(conn)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(daily_reflections)").fetchall()}
        row = conn.execute(
            "SELECT date, morning_intentions, morning_completed "
            "FROM daily_reflections").fetchone()
        assert "morning_completed" in cols
        assert row == ("2026-07-01", '["meta existente"]', None)
    finally:
        conn.close()


def test_v1_table_archived_with_rows_preserved():
    """A v1-shaped table (has `morning`) is renamed to the archive verbatim
    and a fresh v2 table is created — archive, never transform."""
    fd, tmp = tempfile.mkstemp(prefix="kanban_test_refl_mig_", suffix=".db")
    os.close(fd)
    mig_db = Path(tmp)
    orig = _db.KANBAN_DB
    try:
        conn = sqlite3.connect(str(mig_db))
        conn.executescript("""
            CREATE TABLE daily_reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE, morning TEXT, evening TEXT,
                created_at INTEGER, updated_at INTEGER);
            CREATE INDEX idx_daily_reflections_date ON daily_reflections(date);
            INSERT INTO daily_reflections(date, morning)
            VALUES('2026-07-19', '{"intentions": ["paciente"]}');
        """)
        conn.commit()
        conn.close()
        _db.KANBAN_DB = mig_db
        reflection.ensure_schema()
        conn = sqlite3.connect(str(mig_db))
        conn.row_factory = sqlite3.Row
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'daily_reflections%'")}
        assert "daily_reflections_v1_archive" in names
        archived = conn.execute(
            "SELECT * FROM daily_reflections_v1_archive").fetchall()
        assert len(archived) == 1 and archived[0]["date"] == "2026-07-19"
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(daily_reflections)")}
        assert "evening_wins" in cols and "morning" not in cols
        conn.close()
        reflection.ensure_schema()                    # idempotent post-migration
    finally:
        _db.KANBAN_DB = orig
        mig_db.unlink(missing_ok=True)


# ─── Save / read round-trips ─────────────────────────────────────────────────

def test_morning_roundtrip_survives_fresh_connection():
    reflection.save_morning("2026-07-02", ["cerrar Acme", "PR Acme"],
                            source="telegram")
    got = reflection.get_reflection("2026-07-02")
    assert got["morning"]["intentions"] == ["cerrar Acme", "PR Acme"]
    assert got["morning"]["completed"] == [False, False]
    assert got["morning"]["created_at"]
    assert got["evening"] is None


def test_evening_roundtrip():
    reflection.save_evening(
        "2026-07-02",
        wins=[_win(), _win("PR mergeado", "2h sin interrupciones")],
        misses=[{"what": "no leí", "what_happened": "solo 10 min",
                 "why": "reuniones largas"}],
        adjustments=[{"action": "bloquear 30 min de lectura", "when": "7am"}],
        source="telegram")
    e = reflection.get_reflection("2026-07-02")["evening"]
    assert len(e["wins"]) == 2 and e["wins"][0]["why"] == "lo hice temprano"
    assert e["misses"][0]["what_happened"] == "solo 10 min"
    assert e["adjustments"][0]["when"] == "7am"
    assert e["created_at"]


def test_resave_updates_row_never_duplicates():
    reflection.save_morning("2026-07-03", ["a"])
    reflection.save_morning("2026-07-03", ["b", "c"])
    conn = _db.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM daily_reflections "
                         "WHERE date='2026-07-03'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1
    assert reflection.get_reflection("2026-07-03")["morning"]["intentions"] == ["b", "c"]


def test_morning_progress_is_date_scoped_and_changes_only_one_goal():
    reflection.save_morning("2026-07-12", ["meta A", "meta B", "meta C"])
    reflection.save_morning("2026-07-13", ["otra meta"])

    got = reflection.set_morning_progress("2026-07-12", 1, True)
    assert got["morning"] == {
        "intentions": ["meta A", "meta B", "meta C"],
        "completed": [False, True, False],
        "created_at": got["morning"]["created_at"],
    }
    assert reflection.get_reflection("2026-07-13")["morning"]["completed"] == [False]

    got = reflection.set_morning_progress("2026-07-12", 1, False)
    assert got["morning"]["completed"] == [False, False, False]


def test_editing_morning_preserves_only_unchanged_goal_slots():
    reflection.save_morning("2026-07-14", ["A", "B", "C"])
    reflection.set_morning_progress("2026-07-14", 0, True)
    reflection.set_morning_progress("2026-07-14", 1, True)

    same = reflection.save_morning("2026-07-14", ["A", "B", "C"])
    assert same["morning"]["completed"] == [True, True, False]

    edited = reflection.save_morning("2026-07-14", ["A", "B edited", "C"])
    assert edited["morning"]["completed"] == [True, False, False]

    reordered = reflection.save_morning("2026-07-14", ["B edited", "A"])
    assert reordered["morning"]["completed"] == [False, False]


@pytest.mark.parametrize("index,completed", [
    (-1, True), (1, True), (0, "yes"), (0, 1),
])
def test_morning_progress_validation(index, completed):
    reflection.save_morning("2026-07-15", ["A"])
    with pytest.raises(ValueError):
        reflection.set_morning_progress("2026-07-15", index, completed)
    with pytest.raises(ValueError):
        reflection.set_morning_progress("2026-07-16", 0, True)


def test_morning_and_evening_share_one_row():
    reflection.save_morning("2026-07-04", ["a"])
    reflection.save_evening("2026-07-04", [_win()], [], [])
    conn = _db.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM daily_reflections "
                         "WHERE date='2026-07-04'").fetchone()["c"]
    finally:
        conn.close()
    got = reflection.get_reflection("2026-07-04")
    assert n == 1 and got["morning"] and got["evening"]


# ─── Validation (methodology counts: 1-3 / 0-2 / 0-2) ────────────────────────

@pytest.mark.parametrize("bad", [[], ["", "  "], ["a", "b", "c", "d"], "no-list"])
def test_bad_intentions_rejected(bad):
    with pytest.raises(ValueError):
        reflection.save_morning("2026-07-05", bad)


@pytest.mark.parametrize("wins,misses,adjustments", [
    ([], [], []),                                    # no wins at all
    ([{"why": "sin what"}], [], []),                 # win without what
    ([_win()] * 4, [], []),                          # >3 wins
    ([_win()], [{"what": "x"}] * 3, []),             # >2 misses
    ([_win()], [], [{"action": "x"}] * 3),           # >2 adjustments
    (["plain string"], [], []),                      # non-dict entry
])
def test_bad_evening_rejected(wins, misses, adjustments):
    with pytest.raises(ValueError):
        reflection.save_evening("2026-07-05", wins, misses, adjustments)


def test_partial_evening_accepted_and_blank_rows_dropped():
    """Honest partial reply: wins only; blank-what rows from the UI vanish."""
    got = reflection.save_evening(
        "2026-07-05", [_win(), {"what": "", "why": "fila vacía del form"}],
        [{"what": ""}], [])
    e = got["evening"]
    assert len(e["wins"]) == 1 and e["misses"] == [] and e["adjustments"] == []


def test_bad_date_and_source_rejected():
    with pytest.raises(ValueError):
        reflection.save_morning("07/05/2026", ["a"])
    with pytest.raises(ValueError):
        reflection.save_morning("2026-07-05", ["a"], source="cosmic")
    with pytest.raises(ValueError):
        reflection.get_reflection("today")


# ─── History ─────────────────────────────────────────────────────────────────

def test_history_newest_first_and_limited():
    for i in range(1, 10):
        reflection.save_morning(f"2026-06-{i:02d}", ["a"])
    hist = reflection.get_history(7)
    assert len(hist) == 7
    assert hist[0]["date"] == "2026-06-09"
    assert [h["date"] for h in hist] == sorted((h["date"] for h in hist),
                                               reverse=True)
    assert len(reflection.get_history(0)) >= 1      # clamped to >= 1


def test_get_today_shape():
    out = reflection.get_today()
    assert set(out) >= {"date", "morning", "evening", "history"}


# ─── Backup ──────────────────────────────────────────────────────────────────

def test_backup_json_written():
    reflection.save_morning("2026-07-06", ["a"])
    path = Path(os.environ["HERMES_REFLECTIONS_DIR"]) / "2026-07-06.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["morning"]["intentions"] == ["a"]


# ─── Day-review prefill (conservative: only Completed: fragments) ────────────

_TIMELINE = """📊 Day Review — Monday 20 July

7:00am  Claude Code · mac · +6 more
10:00am  Completed: Terminar drones — Acme · Completed: Demo Slice 1 · +2 more
11:00am  Gap / no recorded activity
12:00pm  Completed: Revisar montos Belardi · Cron · CRM Auto-Decay"""


def _write_review(date):
    Path(os.environ["HERMES_DAY_REVIEWS_FILE"]).write_text(
        json.dumps({"date": date, "stats": {"activity_count": 9},
                    "timeline_text": _TIMELINE}) + "\n")


def test_prefill_only_completions_become_wins():
    _write_review("2026-07-07")
    out = reflection.prefill_from_day_review("2026-07-07")
    assert out["available"] is True
    whats = [w["what"] for w in out["wins"]]
    assert whats == ["Terminar drones — Acme", "Demo Slice 1",
                     "Revisar montos Belardi"]           # ≤ 3, order kept
    joined = " ".join(whats)
    assert "Gap" not in joined and "Cron" not in joined and "📊" not in joined
    # Timeline card: bounded, no title/blank lines; gaps stay (honest).
    assert all(l.strip() and not l.startswith("📊") for l in out["timeline"])
    assert len(out["timeline"]) <= reflection.TIMELINE_PROMPT_LINES


def test_prefill_missing_review_is_honest():
    out = reflection.prefill_from_day_review("1999-01-01")
    assert out == {"available": False, "date": "1999-01-01", "wins": [],
                   "timeline": []}


# ─── Prompt generation (deterministic, honest, es-MX) ────────────────────────

def test_morning_prompt_spanish_and_bounded():
    p = reflection.generate_morning_prompt("2026-07-20")
    assert "lunes 20 jul" in p and "lograr hoy" in p
    assert "SER hoy" not in p                        # v1 examen wording gone


def test_morning_prompt_carries_the_whole_morning_check_in():
    """The check-in is not a second ritual: everything the morning slot
    measures is asked inside the prompt the operator already answers."""
    p = reflection.generate_morning_prompt("2026-07-20")
    assert "ansiedad AHORA" in p and "estrés AHORA" in p
    # A whole-day recall in the morning would be a recall of a day that has
    # not happened; `cogload mark` refuses it, so the prompt must not ask it.
    assert "TODO EL DÍA" not in p


def test_evening_prompt_states_which_moment_each_number_is_about():
    """`stress` is one field measured at two moments. If the evening asked for
    a whole-day stress while the morning asked for a momentary one, the same
    field would carry two constructs and the two ends could not be compared."""
    p = reflection.generate_evening_prompt(None, None)
    assert "ansiedad AHORA · ansiedad de TODO EL DÍA · estrés AHORA" in p
    assert "efectividad del día" in p


def test_carga_is_read_shared_not_stored_twice(tmp_path, monkeypatch):
    """The reflection READS the check-in from the collector's store; it never
    keeps a second copy that could drift from it."""
    import dashboard.cogload as dcg
    labels = [
        {"ts": "2026-07-20T07:15:00-06:00", "day": "2026-07-20",
         "slot": "morning", "anx": 2, "stress": 3},
        {"ts": "2026-07-20T18:45:00-06:00", "day": "2026-07-20",
         "slot": "evening", "anx": 4, "anx_day": 3, "stress": 4, "eff": 3},
        {"ts": "2026-07-20T18:50:00-06:00", "day": "2026-07-20",
         "slot": "evening", "anx": 5, "anx_day": 3, "stress": 4, "eff": 3},
    ]
    monkeypatch.setattr(dcg, "load_labels", lambda: labels)
    carga = reflection.load_carga("2026-07-20")
    assert carga["morning"]["anx"] == 2 and carga["morning"]["stress"] == 3
    # Last writer wins WITHIN a moment...
    assert carga["evening"]["anx"] == 5
    # ...and never across them: folding the two ends is the one thing that
    # makes the instrument useless.
    assert carga["morning"]["anx"] != carga["evening"]["anx"]
    assert "eff" not in carga["morning"]
    assert reflection.load_carga("2026-07-19")["morning"] is None


def test_carga_read_never_breaks_a_reflection(monkeypatch):
    """An unreachable collector must cost the operator nothing."""
    import dashboard.cogload as dcg

    def boom():
        raise OSError("store unreadable")

    monkeypatch.setattr(dcg, "load_labels", boom)
    assert reflection.load_carga("2026-07-20") == {"morning": None, "evening": None}
    out = reflection.get_reflection("2026-07-20")
    assert out["carga"] == {"morning": None, "evening": None}


def test_evening_prompt_echoes_real_data_only():
    morning = {"intentions": ["cerrar Acme", "leer 30 min"]}
    review = {"timeline_text": _TIMELINE}
    p = reflection.generate_evening_prompt(review, morning)
    assert "cerrar Acme, leer 30 min" in p
    assert "Terminar drones — Acme" in p and "📊 Tu día hoy" in p
    for part in ("¿Qué salió bien hoy?", "¿Qué no salió como esperabas?",
                 "¿Qué harás diferente mañana?"):
        assert part in p
    # Missing data → honest lines, nothing fabricated.
    p2 = reflection.generate_evening_prompt(None, None)
    assert "No tengo el day review" in p2
    assert "no registraste intenciones" in p2


def test_evening_prompt_timeline_bounded():
    many = "📊 title\n\n" + "\n".join(f"{h}:00am  linea {h}" for h in range(20))
    p = reflection.generate_evening_prompt({"timeline_text": many}, None)
    shown = [l for l in p.splitlines() if l.strip().endswith(tuple(
        f"linea {h}" for h in range(20)))]
    assert len(shown) == reflection.TIMELINE_PROMPT_LINES


# ─── API (the six v2 routes through TestClient; v1 routes are 404) ───────────

def test_api_get_reflection_default_and_by_date():
    reflection.save_morning("2026-07-08", ["x"])
    r = _CLIENT.get("/api/reflection")
    assert r.status_code == 200 and "date" in r.json()
    r = _CLIENT.get("/api/reflection?date=2026-07-08")
    assert r.status_code == 200
    assert r.json()["morning"]["intentions"] == ["x"]
    assert _CLIENT.get("/api/reflection?date=nope").status_code == 400


def test_api_morning_post_then_put():
    r = _CLIENT.post("/api/reflection/morning",
                     json={"date": "2026-07-09", "intentions": ["v1"]})
    assert r.status_code == 200
    r = _CLIENT.put("/api/reflection/morning",
                    json={"date": "2026-07-09", "intentions": ["v2 editada"]})
    assert r.status_code == 200
    assert r.json()["morning"]["intentions"] == ["v2 editada"]
    assert _CLIENT.post("/api/reflection/morning",
                        json={"date": "2026-07-09",
                              "intentions": []}).status_code == 400


def test_api_morning_progress_roundtrip_and_validation():
    reflection.save_morning("2026-07-16", ["ship", "walk"])
    r = _CLIENT.put("/api/reflection/morning/progress", json={
        "date": "2026-07-16", "index": 0, "completed": True})
    assert r.status_code == 200
    assert r.json()["morning"]["intentions"] == ["ship", "walk"]
    assert r.json()["morning"]["completed"] == [True, False]
    assert _CLIENT.get("/api/reflection?date=2026-07-16").json()["morning"]["completed"] == [True, False]

    assert _CLIENT.put("/api/reflection/morning/progress", json={
        "date": "2026-07-16", "index": 2, "completed": True}).status_code == 400
    assert _CLIENT.put("/api/reflection/morning/progress", json={
        "date": "2026-07-16", "index": 0, "completed": "true"}).status_code == 400


def test_api_evening_post_and_validation():
    r = _CLIENT.post("/api/reflection/evening", json={
        "date": "2026-07-09", "wins": [_win()], "misses": [],
        "adjustments": [{"action": "a", "when": "mañana"}]})
    assert r.status_code == 200
    body = r.json()["evening"]
    assert body["wins"][0]["what"] and body["adjustments"][0]["when"] == "mañana"
    assert _CLIENT.post("/api/reflection/evening",
                        json={"date": "2026-07-09",
                              "wins": []}).status_code == 400


def test_api_history_and_prefill():
    reflection.save_morning("2026-07-10", ["x"])
    r = _CLIENT.get("/api/reflection/history?days=7")
    assert r.status_code == 200 and "history" in r.json()
    _write_review("2026-07-11")
    r = _CLIENT.get("/api/reflection/prefill?date=2026-07-11")
    assert r.status_code == 200
    out = r.json()
    assert out["available"] is True and "wins" in out
    r = _CLIENT.get("/api/reflection/prefill?date=1999-01-01")
    assert r.status_code == 200 and r.json()["available"] is False


def test_api_v1_routes_removed():
    assert _CLIENT.get("/api/reflection/today").status_code == 404
    assert _CLIENT.get("/api/reflection/2026-07-08").status_code == 404


def test_api_generate_evening_uses_persisted_store():
    _write_review(reflection.today_str())
    r = _CLIENT.post("/api/reflection/generate-evening")
    assert r.status_code == 200
    out = r.json()
    assert out["exists"] is False
    assert "Terminar drones — Acme" in out["prompt"]
    # Saved evening → exists=true → the cron script stays silent.
    reflection.save_evening(reflection.today_str(), [_win()], [], [])
    assert _CLIENT.post("/api/reflection/generate-evening").json()["exists"] is True


# ─── MCP wiring ──────────────────────────────────────────────────────────────

def test_mcp_tools_registered_wired_unprivileged():
    import mcp_server
    registered = {t["name"] for t in mcp_server.TOOLS}
    for name in MCP_REFLECTION_TOOLS:
        assert name in registered, f"{name} missing from TOOLS"
        assert name in mcp_server.TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"
        assert callable(mcp_server.TOOL_HANDLERS[name])
        # Deliberately unprivileged (today-only + create-guarded instead) so the
        # Telegram thread agent can save the operator's replies — health precedent.
        assert name not in mcp_server.PRIVILEGED_TOOLS


def test_mcp_evening_schema_is_harvard_shape():
    import mcp_server
    tool = next(t for t in mcp_server.TOOLS
                if t["name"] == "save_reflection_evening")
    props = tool["inputSchema"]["properties"]
    assert {"wins", "misses", "adjustments", "overwrite"} <= set(props)
    assert tool["inputSchema"]["required"] == ["wins"]


def test_mcp_save_is_create_guarded():
    """Default-scope safety: an existing entry is NOT overwritten unless the
    tool is called with overwrite=true."""
    import mcp_server
    today = reflection.today_str()
    reflection.save_morning(today, ["original"], source="dashboard")
    res = json.loads(mcp_server.tool_save_reflection_morning(
        {"intentions": ["clobber"]}))
    assert res.get("status") == "exists"
    assert reflection.get_reflection(today)["morning"]["intentions"] == ["original"]
    res2 = json.loads(mcp_server.tool_save_reflection_morning(
        {"intentions": ["replaced"], "overwrite": True}))
    assert res2["morning"]["intentions"] == ["replaced"]


def test_mcp_evening_save_harvard_roundtrip():
    import mcp_server
    res = json.loads(mcp_server.tool_save_reflection_evening({
        "wins": [_win()], "misses": [{"what": "no leí"}],
        "adjustments": [{"action": "leer 7am"}]}))
    assert res["evening"]["wins"][0]["what"] == "cerré el schema"
    res2 = json.loads(mcp_server.tool_save_reflection_evening(
        {"wins": [{"what": "clobber"}]}))
    assert res2.get("status") == "exists"


# ─── The check-in rides the reflection: ONE input, ONE call, TWO stores ─────

def _fake_cogload(tmp_path, monkeypatch, returncode=0):
    """Stand in for the `cogload` binary and record the argv it was handed.

    The real binary owns a store outside this repo; what this contract has to
    pin is that the reflection tools INVOKE it (rather than keeping a private
    copy of the numbers) and with which slot.
    """
    calls = []

    class _R:
        def __init__(self):
            self.returncode = returncode
            self.stdout = "marked"
            self.stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)
    return calls


def test_mcp_morning_saves_intentions_and_check_in_in_one_call(tmp_path, monkeypatch):
    import mcp_server
    calls = _fake_cogload(tmp_path, monkeypatch)
    res = json.loads(mcp_server.tool_save_reflection_morning(
        {"intentions": ["cerrar Acme"], "carga": {"anx": 2, "stress": 3}}))
    assert res["morning"]["intentions"] == ["cerrar Acme"]
    assert res["carga"]["ok"] is True and res["carga"]["slot"] == "morning"
    assert len(calls) == 1, "the check-in must go through `cogload mark`, once"
    cmd = calls[0]
    assert cmd[:2] == ["cogload", "mark"]
    assert "--slot" in cmd and cmd[cmd.index("--slot") + 1] == "morning"
    assert cmd[cmd.index("--anx") + 1] == "2"
    assert cmd[cmd.index("--stress") + 1] == "3"


def test_mcp_evening_saves_the_harvard_parts_and_parte_4_in_one_call(tmp_path, monkeypatch):
    import mcp_server
    calls = _fake_cogload(tmp_path, monkeypatch)
    res = json.loads(mcp_server.tool_save_reflection_evening(
        {"wins": [_win()],
         "carga": {"anx": 2, "anx_day": 3, "stress": 4, "eff": 3}}))
    assert res["evening"]["wins"][0]["what"] == "cerré el schema"
    assert res["carga"]["ok"] is True and res["carga"]["slot"] == "evening"
    cmd = calls[0]
    assert cmd[cmd.index("--anx") + 1] == "2"
    assert cmd[cmd.index("--anx-day") + 1] == "3"


def test_the_two_halves_have_independent_guards(tmp_path, monkeypatch):
    """An already-saved reflection must not swallow numbers that arrive later,
    and a refused check-in must never cost the operator his reflection."""
    import mcp_server
    today = reflection.today_str()
    reflection.save_morning(today, ["original"], source="dashboard")
    calls = _fake_cogload(tmp_path, monkeypatch)
    res = json.loads(mcp_server.tool_save_reflection_morning(
        {"intentions": ["clobber"], "carga": {"anx": 4}}))
    assert res["status"] == "exists"                    # reflection untouched
    assert reflection.get_reflection(today)["morning"]["intentions"] == ["original"]
    assert res["carga"]["ok"] is True                   # ...check-in still saved
    assert len(calls) == 1

    # And the other direction: the collector refusing does not lose the wins.
    calls2 = _fake_cogload(tmp_path, monkeypatch, returncode=3)
    res2 = json.loads(mcp_server.tool_save_reflection_evening(
        {"wins": [_win()], "carga": {"anx": 4}}))
    assert res2["carga"]["status"] == "exists"
    assert res2["evening"]["wins"][0]["what"] == "cerré el schema"


def test_free_text_cannot_reach_the_store_through_a_reflection(tmp_path, monkeypatch):
    """The strict allowlist is the control, not the advertised schema: MCP
    arguments arrive unvalidated, so an injected note must be REJECTED here."""
    import mcp_server
    calls = _fake_cogload(tmp_path, monkeypatch)
    res = json.loads(mcp_server.tool_save_reflection_evening(
        {"wins": [_win()], "carga": {"anx": 3, "note": "SECRET", "force": True}}))
    assert "unexpected fields" in res["carga"]["error"]
    assert calls == [], "nothing may be written when the payload is rejected"


def test_a_whole_day_recall_is_refused_in_the_morning(tmp_path, monkeypatch):
    import mcp_server
    calls = _fake_cogload(tmp_path, monkeypatch)
    res = json.loads(mcp_server.tool_save_reflection_morning(
        {"intentions": ["x"], "carga": {"anx_day": 3}}))
    assert "evening slot only" in res["carga"]["error"]
    assert calls == []


def test_morning_schema_asks_for_exactly_what_the_prompt_asks(tmp_path):
    """The tool and the prompt must not drift apart: a number the prompt asks
    for and the schema cannot carry is a measurement collected and dropped."""
    import mcp_server
    tool = next(t for t in mcp_server.TOOLS
                if t["name"] == "save_reflection_morning")
    carga = tool["inputSchema"]["properties"]["carga"]["properties"]
    assert set(carga) == {"anx", "stress"}
    evening = next(t for t in mcp_server.TOOLS
                   if t["name"] == "save_reflection_evening")
    assert set(evening["inputSchema"]["properties"]["carga"]["properties"]) == {
        "anx", "anx_day", "stress", "eff"}
