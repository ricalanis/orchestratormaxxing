"""Personal OKRs: seed-driven catalog, append-only progress, API, and MCP."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db as _db


@pytest.fixture(scope="module", autouse=True)
def _isolated_db():
    global CLIENT, okrs
    real = _db.KANBAN_DB
    fd, tmp = tempfile.mkstemp(prefix="kanban_test_okrs_", suffix=".db")
    os.close(fd)
    path = Path(tmp)
    if real.exists():
        shutil.copy(str(real), str(path))
    _db.KANBAN_DB = path
    # Force the BUILT-IN example seed: the dev machine carries a private tenant
    # seed at ~/.hermes/okrs-seed.json, and these tests pin the shipped default.
    prev_seed = os.environ.get("HERMES_OKRS_SEED")
    os.environ["HERMES_OKRS_SEED"] = ""
    try:
        from dashboard import okrs as module
        okrs = module
        okrs.ensure_schema()
        from dashboard.api import app
        from starlette.testclient import TestClient
        CLIENT = TestClient(app, raise_server_exceptions=False)
        yield
    finally:
        if prev_seed is None:
            os.environ.pop("HERMES_OKRS_SEED", None)
        else:
            os.environ["HERMES_OKRS_SEED"] = prev_seed
        _db.KANBAN_DB = real
        path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _reset_state():
    conn = _db.get_conn()
    try:
        conn.execute("DROP TABLE IF EXISTS personal_okr_checkins")
        conn.execute("DROP TABLE IF EXISTS personal_key_results")
        conn.execute("DROP TABLE IF EXISTS personal_okrs")
        conn.commit()
    finally:
        conn.close()
    okrs.ensure_schema()


def test_seeds_the_loaded_catalog_in_order_without_business_content():
    seed = okrs._seed()
    data = okrs.get_okrs(2026)
    assert [o["id"] for o in data["objectives"]] == [o["id"] for o in seed["okrs"]]
    assert data["principles"] == list(seed["principles"])
    assert [(o["title"], o["status"], o["progress"]) for o in data["objectives"]] == \
        [(o["title"], o["status"], o["progress"]) for o in seed["okrs"]]
    kr_state = {kr["id"]: (kr["status"], kr["progress"])
                for objective in data["objectives"] for kr in objective["key_results"]}
    assert kr_state == {kr[0]: (kr[5], kr[6]) for kr in seed["key_results"]}
    per_okr = {}
    for kr in seed["key_results"]:
        per_okr[kr[1]] = per_okr.get(kr[1], 0) + 1
    assert [len(o["key_results"]) for o in data["objectives"]] == \
        [per_okr[o["id"]] for o in seed["okrs"]]
    serialized = json.dumps(data, ensure_ascii=False).lower()
    for forbidden in ("wormbase", "warnbase", "baseworm", "altis", "poncho"):
        assert forbidden not in serialized
    baseline = seed["baseline_checkin"]
    assert len(data["history"]) == 1
    got = data["history"][0]
    assert (got["id"], got["date"], got["source"], got["source_reference"]) == (
        baseline["id"], baseline["date"], baseline["source"], baseline["source_reference"]
    )
    assert got["snapshot"]["year"] == 2026
    assert got["snapshot"]["history"] == []


def test_tenant_seed_file_overrides_the_built_in_example():
    """The catalog is CONFIG: a tenant seed file replaces the example text
    while ids/structure keep the same plumbing."""
    seed = json.loads(json.dumps(okrs.EXAMPLE_SEED))
    seed["okrs"][0]["title"] = "Objetivo del tenant"
    fd, tmp = tempfile.mkstemp(prefix="okrs_seed_", suffix=".json")
    os.close(fd)
    Path(tmp).write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    prev = os.environ.get("HERMES_OKRS_SEED")
    os.environ["HERMES_OKRS_SEED"] = tmp
    conn = _db.get_conn()
    try:
        conn.execute("DROP TABLE IF EXISTS personal_okr_checkins")
        conn.execute("DROP TABLE IF EXISTS personal_key_results")
        conn.execute("DROP TABLE IF EXISTS personal_okrs")
        conn.commit()
    finally:
        conn.close()
    try:
        okrs.ensure_schema()
        data = okrs.get_okrs(2026)
        assert data["objectives"][0]["title"] == "Objetivo del tenant"
    finally:
        os.environ["HERMES_OKRS_SEED"] = prev if prev is not None else ""
        Path(tmp).unlink(missing_ok=True)


def test_seed_is_idempotent_and_never_overwrites_current_state():
    okrs.save_checkin({
        "note": "Semana sólida",
        "objectives": [{"id": "round_man", "progress": 83, "status": "active",
                        "current_note": "Semana sólida"}],
    })
    okrs.ensure_schema()
    objective = next(o for o in okrs.get_okrs()["objectives"]
                     if o["id"] == "round_man")
    assert objective["progress"] == 83
    assert objective["current_note"] == "Semana sólida"
    assert objective["checkin_count"] == 2  # seed baseline + the new check-in


def test_checkin_updates_snapshot_and_appends_immutable_history():
    first = okrs.save_checkin({
        "date": "2026-08-01", "source": "dashboard",
        "note": "Ingreso sigue siendo el cuello de botella",
        "objectives": [{"id": "economic", "progress": 45, "status": "at_risk"}],
    })
    second = okrs.save_checkin({
        "date": "2026-08-02", "source": "dashboard", "note": "Primer avance medible",
        "objectives": [{"id": "economic", "progress": 50, "status": "active",
                        "key_results": [{"id": "economic.billing", "status": "active",
                                         "progress": 47, "progress_note": "$70k de $150k"}]}],
    })
    assert first["objectives"][0]["progress"] == 45
    assert second["objectives"][0]["progress"] == 50
    history = okrs.get_okrs(history_limit=10)["history"]
    assert [row["date"] for row in history[:2]] == ["2026-08-02", "2026-08-01"]
    assert history[0]["snapshot"]["objectives"][0]["progress"] == 50

    conn = _db.get_conn()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE personal_okr_checkins SET note='x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM personal_okr_checkins")
    finally:
        conn.close()


@pytest.mark.parametrize("progress,status", [(-1, "active"), (101, "active"), (20, "bogus")])
def test_checkin_validation(progress, status):
    before = len(okrs.get_okrs()["history"])
    with pytest.raises(ValueError):
        okrs.save_checkin({"objectives": [
            {"id": "economic", "progress": progress, "status": status}
        ]})
    assert len(okrs.get_okrs()["history"]) == before


@pytest.mark.parametrize("payload", [
    {}, {"objectives": []}, {"objectives": "economic"},
    {"objectives": [{}]},
    {"source": "telegram", "objectives": [{"id": "economic"}]},
    {"date": "03/08/2026", "objectives": [{"id": "economic"}]},
    {"note": "x" * 2001, "objectives": [{"id": "economic"}]},
    {"source_reference": "x" * 501, "objectives": [{"id": "economic"}]},
])
def test_checkin_shape_and_bounds(payload):
    before = okrs.get_okrs()
    with pytest.raises(ValueError):
        okrs.save_checkin(payload)
    after = okrs.get_okrs()
    assert after["objectives"] == before["objectives"]
    assert after["history"] == before["history"]


def test_failed_multi_objective_update_rolls_back_everything():
    before = okrs.get_okrs()
    with pytest.raises(LookupError):
        okrs.save_checkin({"objectives": [
            {"id": "economic", "progress": 99},
            {"id": "does-not-exist", "progress": 1},
        ]})
    after = okrs.get_okrs()
    assert after["objectives"] == before["objectives"]
    assert after["history"] == before["history"]


def test_partial_update_preserves_unsupplied_fields_and_records_defaults():
    before = okrs.get_okrs()
    original = before["objectives"][0]
    got = okrs.save_checkin({"objectives": [{"id": "economic", "progress": 51}]})
    updated = got["objectives"][0]
    assert updated["progress"] == 51
    assert updated["status"] == original["status"]
    assert updated["current_note"] == original["current_note"]
    assert got["history"][0]["source"] == "dashboard"
    assert got["history"][0]["source_reference"] is None
    assert got["history"][0]["date"]


def test_read_coerces_year_and_clamps_history_limit():
    got = okrs.get_okrs("2026", "1")
    assert got["year"] == 2026
    assert len(got["history"]) == 1
    with pytest.raises(ValueError):
        okrs.get_okrs("twenty", 5)


def test_read_defaults_and_history_bounds_are_exact():
    for i in range(7):
        okrs.save_checkin({"date": f"2026-08-{i + 1:02d}",
                              "objectives": [{"id": "economic", "progress": i}]})
    assert okrs.get_okrs()["year"] == 2026
    assert len(okrs.get_okrs()["history"]) == 5
    assert okrs.get_okrs(history_limit=-3)["history"] == []
    assert len(okrs.get_okrs(history_limit=999)["history"]) == 8


def test_progress_endpoints_and_text_caps_are_inclusive():
    low = okrs.save_checkin({"note": "x" * 2000,
        "source_reference": "r" * 500,
        "objectives": [{"id": "economic", "progress": 0}]})
    assert low["objectives"][0]["progress"] == 0
    high = okrs.save_checkin({"objectives": [{"id": "economic", "progress": 100}]})
    assert high["objectives"][0]["progress"] == 100
    assert len(low["history"][0]["note"]) == 2000
    assert len(low["history"][0]["source_reference"]) == 500


def test_key_result_partial_update_roundtrip_and_snapshot():
    got = okrs.save_checkin({"objectives": [{
        "id": "motivation",
        "key_results": [{"id": "motivation.researcher", "status": "active",
                         "progress": 12, "progress_note": "Solicitud retomada"}],
    }]})
    objective = next(o for o in got["objectives"] if o["id"] == "motivation")
    kr = next(k for k in objective["key_results"] if k["id"] == "motivation.researcher")
    assert (kr["status"], kr["progress"], kr["progress_note"]) == (
        "active", 12, "Solicitud retomada"
    )
    snap_kr = next(k for o in got["history"][0]["snapshot"]["objectives"]
                   if o["id"] == "motivation" for k in o["key_results"]
                   if k["id"] == "motivation.researcher")
    assert snap_kr["progress"] == 12
    assert got["history"][0]["snapshot"]["history"] == []


@pytest.mark.parametrize("kr", [
    "not-an-object", {}, {"id": "unknown.kr"},
    {"id": "ordered.body", "progress": 10},
])
def test_invalid_key_result_update_is_atomic(kr):
    before = okrs.get_okrs()
    expected = ValueError if not isinstance(kr, dict) or not kr.get("id") else LookupError
    with pytest.raises(expected):
        okrs.save_checkin({"objectives": [{
            "id": "motivation", "progress": 91, "key_results": [kr],
        }]})
    after = okrs.get_okrs()
    assert after["objectives"] == before["objectives"]
    assert after["history"] == before["history"]


def test_history_caps_are_exact_for_reads_and_write_response():
    for i in range(52):
        okrs.save_checkin({"objectives": [{"id": "economic", "progress": i % 101}]})
    assert len(okrs.get_okrs(history_limit=999)["history"]) == 50
    returned = okrs.save_checkin({"objectives": [{"id": "economic", "progress": 53}]})
    assert len(returned["history"]) == 10


def test_api_reads_and_writes_progress():
    response = CLIENT.get("/api/personal/okrs")
    assert response.status_code == 200
    assert len(response.json()["objectives"]) == 4

    response = CLIENT.post("/api/personal/okrs/check-ins", json={
        "note": "Subiendo ingreso", "objectives": [
            {"id": "economic", "progress": 52, "status": "active"}
        ]
    })
    assert response.status_code == 200
    assert response.json()["objectives"][0]["progress"] == 52
    assert response.json()["history"][0]["note"] == "Subiendo ingreso"


def test_api_rejects_invalid_and_unknown_objective():
    invalid = CLIENT.post("/api/personal/okrs/check-ins", json={
        "objectives": [{"id": "economic", "progress": 900, "status": "active"}]
    })
    missing = CLIENT.post("/api/personal/okrs/check-ins", json={
        "objectives": [{"id": "nope", "progress": 10, "status": "active"}]
    })
    assert invalid.status_code == 400
    assert missing.status_code == 404


def test_mcp_tool_is_wired_and_privileged_for_personal_data():
    import mcp_server
    names = {tool["name"] for tool in mcp_server.TOOLS}
    assert "get_personal_okrs" in names
    assert "get_personal_okrs" in mcp_server.PRIVILEGED_TOOLS
    assert "get_personal_okrs" in mcp_server.TOOL_HANDLERS

    got = json.loads(mcp_server.tool_get_personal_okrs({"year": 2026}))
    assert len(got["objectives"]) == 4
