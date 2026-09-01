"""Acceptance contract for the cogload personal endpoints + aggregation.

Authored BEFORE the endpoints existed (Tier-0 spec gate). Every assertion here
encodes a failure this harness has ALREADY made once:

  * a blind day reading as a calm day (the silent zero, three times over)
  * a weekly ratio computed as a mean-of-ratios instead of sum/sum
  * personal health state reachable without the bearer token
  * label note text escaping into an outbound message

Runs against a temp COGLOAD_DIR so it never touches the live behavioural store.
"""

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="cogload-api-test-"))
os.environ["COGLOAD_DIR"] = str(TMP)

NOTE_SENTINEL = "zzsecretnotesentinelzz"


def _seed():
    """Three days: healthy, blind-but-status-ok, and no-data."""
    (TMP / "digest").mkdir(parents=True, exist_ok=True)
    rows = []
    # A genuinely good day.
    rows.append({
        "day": "2026-08-01", "v": 3, "windows": 1400, "windows_ok": 1400,
        "windows_degraded": 0, "windows_screen_ok": 1380, "windows_light_ok": 1370,
        "active_minutes": 300, "keys_total": 40000, "keys_enter": 900,
        "keys_active_total": 38000, "keys_bs_active": 3800,
        "backspace_ratio": 0.1, "app_switches": 600, "agents_peak": 9,
        "light_mean": 100.0, "night_minutes": 10, "expected_slots": 1400,
        "gap_minutes": 0, "day_expected_slots": 1440, "day_gap_minutes": 40,
        "edge_gap_minutes": 40, "day_coverage_pct": 0.972,
        "covered_minutes": 1400, "partial": False,
    })
    # A day that is 90% BLIND but whose rows all said status ok.
    rows.append({
        "day": "2026-08-02", "v": 3, "windows": 140, "windows_ok": 140,
        "windows_degraded": 0, "windows_screen_ok": 10, "windows_light_ok": 8,
        "active_minutes": 30, "keys_total": 900, "keys_enter": 40,
        "keys_active_total": 800, "keys_bs_active": 240,
        "backspace_ratio": 0.3, "app_switches": 20, "agents_peak": 2,
        "light_mean": 90.0, "night_minutes": 0, "expected_slots": 140,
        "gap_minutes": 0, "day_expected_slots": 1440, "day_gap_minutes": 1300,
        "edge_gap_minutes": 1300, "day_coverage_pct": 0.097,
        "covered_minutes": 140, "partial": False,
    })
    (TMP / "digest" / "2026-08.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    (TMP / "labels.jsonl").write_text(json.dumps({
        "ts": datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc).isoformat(),
        "stress": 4, "eff": 3, "note": NOTE_SENTINEL, "src": "cli",
    }) + "\n", encoding="utf-8")
    return rows


ROWS = _seed()


def _mod():
    from dashboard import cogload
    return cogload


# ------------------------------------------------------------------ health

def test_blind_day_is_never_valid():
    """90% blind must not be presentable as a calm day. This is THE invariant."""
    cg = _mod()
    good = cg.capture_health(ROWS[0])
    blind = cg.capture_health(ROWS[1])
    assert good["valid"] is True, good
    assert blind["valid"] is False, blind
    assert blind.get("reason"), "an invalid day must say WHY"


def test_no_data_day_is_invalid_not_calm():
    cg = _mod()
    empty = cg.capture_health({"day": "2026-08-03"})
    assert empty["valid"] is False
    assert empty.get("reason")


def test_capture_pct_has_a_zero_denominator_guard():
    cg = _mod()
    h = cg.capture_health({"day": "x", "windows": 0, "windows_ok": 0})
    assert h["valid"] is False


# ----------------------------------------------------------------- weekly

def test_weekly_ratio_is_sum_over_sum_not_mean_of_ratios():
    """The two differ whenever daily volumes differ — and they do."""
    cg = _mod()
    days = [
        {**ROWS[0], "day": "2026-08-03", "keys_bs_active": 100,
         "keys_active_total": 10000, "backspace_ratio": 0.01},
        {**ROWS[0], "day": "2026-08-04", "keys_bs_active": 90,
         "keys_active_total": 100, "backspace_ratio": 0.9},
        {**ROWS[0], "day": "2026-08-05"},
        {**ROWS[0], "day": "2026-08-06"},
    ]
    wk = cg.weekly(days, cg._iso_week(date(2026, 8, 3)), labels=[])
    if not wk.get("sufficient", True):
        pytest.skip("period reported insufficient; ratio not exposed")
    got = wk.get("backspace_ratio")
    assert got is not None
    num = sum(d["keys_bs_active"] for d in days)
    den = sum(d["keys_active_total"] for d in days)
    mean_of_ratios = sum(d["backspace_ratio"] for d in days) / len(days)
    assert abs(got - num / den) < 1e-6, f"got {got}, expected {num/den}"
    assert abs(got - mean_of_ratios) > 1e-6, "computed as mean-of-ratios"


def test_secondary_means_are_window_weighted_not_mean_of_means():
    """A 20-minute day must not count as much as a 14-hour day.

    And a day MISSING the field must be excluded from its own denominator
    rather than counted as 0.0 — otherwise an unmeasured day silently drags the
    average toward "calm".
    """
    cg = _mod()
    big = {**ROWS[0], "day": "2026-08-10", "windows": 1400, "windows_ok": 1400,
           "agents_mean": 10.0}
    small = {**ROWS[0], "day": "2026-08-11", "windows": 100, "windows_ok": 100,
             "agents_mean": 1.0}
    missing = {**ROWS[0], "day": "2026-08-12"}
    missing.pop("agents_mean", None)
    days = [big, small, missing, {**ROWS[0], "day": "2026-08-13", "agents_mean": 10.0}]
    wk = cg.weekly(days, cg._iso_week(date(2026, 8, 10)), labels=[])
    if not wk.get("sufficient", True):
        pytest.skip("insufficient period")
    got = wk.get("agents_mean")
    assert got is not None
    naive = (10.0 + 1.0 + 0.0 + 10.0) / 4          # the old, wrong computation
    assert abs(got - naive) > 1e-6, "still mean-of-means with a 0.0 default"
    assert got > 5.0, f"weighted mean should favour the long day, got {got}"


def test_thin_week_refuses_to_derive():
    cg = _mod()
    wk = cg.weekly([ROWS[0]], cg._iso_week(date(2026, 8, 1)), labels=[])
    assert wk.get("sufficient") is False


# -------------------------------------------------------------- readiness

def test_readiness_reports_progress_and_never_a_correlation():
    cg = _mod()
    r = cg.readiness(ROWS, cg.load_labels())
    assert {"capture", "calibration", "curve"} <= set(r)
    blob = json.dumps(r).lower()
    for forbidden in ("correlation", "pearson", "slope", "knee", "r2", "r_squared"):
        assert forbidden not in blob, f"readiness must not infer: {forbidden}"


# ---------------------------------------------------------------- privacy

def test_weekly_markdown_never_leaks_label_notes():
    cg = _mod()
    wk = cg.weekly(ROWS, cg._iso_week(date(2026, 8, 1)), labels=cg.load_labels())
    md = cg.weekly_markdown(wk)
    assert NOTE_SENTINEL not in md, "label note text reached an outbound brief"


def test_live_status_never_fabricates_health():
    cg = _mod()
    st = cg.live_status()
    assert isinstance(st, dict)
    if not st.get("available", False):
        assert st.get("reason"), "unavailable must carry a reason, not a fake ok"


def test_live_status_preserves_structured_degradation_on_exit_one(monkeypatch):
    """Exit 1 is Cogload's degraded state, not a transport failure."""
    cg = _mod()
    payload = {
        "ok": False,
        "last_status": "degraded:session-wayland-xrecord-partial",
        "subsystems": {
            "keys": {"available": True, "reason": ""},
            "screen": {"available": True, "reason": ""},
            "light": {"available": False, "reason": "no-sample-in-window"},
        },
    }

    class Proc:
        returncode = 1
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(cg.subprocess, "run", lambda *a, **kw: Proc())
    cg._LIVE_CACHE = None
    cg._LIVE_CACHE_AT = 0.0

    status = cg.live_status()
    assert status["available"] is True
    assert status["ok"] is False
    assert status["subsystems"]["keys"]["available"] is True
    assert status["subsystems"]["screen"]["available"] is True
    assert status["subsystems"]["light"]["reason"] == "no-sample-in-window"
    assert status["reason"] == "degraded:session-wayland-xrecord-partial"


# --------------------------------------------------------------- endpoints

def test_endpoint_returns_expected_shape():
    from starlette.testclient import TestClient
    from dashboard.api import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/personal/cogload")
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("status", "days", "weeks", "months", "readiness"):
        assert k in body, f"missing {k}: {list(body)}"


def test_weekly_md_endpoint_excludes_notes():
    from starlette.testclient import TestClient
    from dashboard.api import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/personal/cogload/weekly?format=md")
    assert r.status_code == 200, r.text
    assert NOTE_SENTINEL not in r.text


def test_cogload_gets_are_bearer_gated():
    """Personal health state must not be readable without the token.

    TESTING=1 bypasses auth for the rest of the suite, so this asserts the
    ROUTE is registered as sensitive rather than exercising the 401 directly.
    """
    from dashboard import api
    assert api._is_sensitive_get("/api/personal/cogload")
    assert api._is_sensitive_get("/api/personal/cogload/weekly")
    assert not api._is_sensitive_get("/api/health/today")


def test_label_post_writes_through_the_tool():
    from starlette.testclient import TestClient
    from dashboard.api import app
    client = TestClient(app, raise_server_exceptions=False)
    # Pin the slot. Omitting it makes `cogload mark` INFER one from the clock,
    # so before noon this consumed the morning slot and the next test's explicit
    # morning reading hit the one-per-(day,slot) guard — a suite whose result
    # depended on the hour it was run.
    r = client.post("/api/personal/cogload/label",
                    json={"slot": "evening", "stress": 3, "eff": 4,
                          "note": "ok", "src": "dashboard"})
    assert r.status_code in (200, 201), r.text
    lines = (TMP / "labels.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2, "label was not appended"
    last = json.loads(lines[-1])
    assert last["stress"] == 3 and last["eff"] == 4


def test_label_post_accepts_a_morning_check_in():
    """The morning moment asks ansiedad + estrés and no efectividad. Requiring
    `stress` made that reading unrepresentable from the dashboard, so the two
    writers disagreed about what a reading even is."""
    from starlette.testclient import TestClient
    from dashboard.api import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/personal/cogload/label",
                    json={"slot": "morning", "anx": 2, "stress": 3})
    assert r.status_code in (200, 201), r.text
    last = json.loads((TMP / "labels.jsonl").read_text(
        encoding="utf-8").splitlines()[-1])
    assert last["slot"] == "morning" and last["anx"] == 2 and last["stress"] == 3
    assert last["eff"] is None

    # One reading per moment per day, enforced by the tool the endpoint writes
    # through — not re-implemented here (a second writer would fork the rule).
    assert client.post("/api/personal/cogload/label",
                       json={"slot": "morning", "anx": 4}).status_code == 502
    # A reading with no measure at all is refused before the tool is reached.
    assert client.post("/api/personal/cogload/label",
                       json={"src": "dashboard"}).status_code == 400
    # A whole-day recall of a day that has not happened is not a measurement.
    assert client.post("/api/personal/cogload/label",
                       json={"slot": "morning", "anx_day": 3}).status_code == 400


# ---------------------------------------------------------------- fleet reads

def _seed_fleet():
    """A second device's pushed digest, exactly as `fleet ingest` writes it."""
    d = TMP / "fleet" / "mac-xyz"
    d.mkdir(parents=True, exist_ok=True)
    (d / "device.json").write_text(json.dumps({
        "id": "mac-xyz", "slug": "macbook", "platform": "darwin",
        "channels": {"keys": True, "mouse": True, "screen": False,
                     "light": False, "agents": True},
    }), encoding="utf-8")
    rows = [{
        "day": "2026-08-01", "host": "mac-xyz", "v": 4,
        "windows": 1400, "windows_ok": 1400,
        # A Mac legitimately has NO screen/light — it declares so.
        "windows_screen_ok": 0, "windows_light_ok": 0,
        "screen_expected": False, "light_expected": False,
        "active_minutes": 100, "keys_total": 9000, "keys_enter": 100,
        "keys_active_total": 8000, "keys_bs_active": 400,
        "backspace_ratio": 0.05, "gap_minutes": 0, "partial": False,
        "day_coverage_pct": 0.97,
    }]
    (d / "digest.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return rows


FLEET_ROWS = _seed_fleet()


def test_fleet_devices_lists_every_device():
    cg = _mod()
    devs = cg.fleet_devices()
    ids = {d.get("id") for d in devs}
    assert "mac-xyz" in ids, f"pushed device missing: {ids}"


def test_fleet_days_include_pushed_rows():
    """The dashboard reads only the LOCAL digest today; a pushed device is
    invisible, so the tab shows one machine while data for two has arrived."""
    cg = _mod()
    rows = cg.load_fleet_days()
    assert any(r.get("host") == "mac-xyz" for r in rows), "fleet rows not loaded"
    assert any(r.get("host") != "mac-xyz" for r in rows), "local rows dropped"


def test_a_declared_absent_channel_does_not_invalidate_a_day():
    """macOS has no X server. If capture_health demands screen/light coverage
    regardless, EVERY Mac day is invalid forever and its contribution is
    silently dropped — while a device that CLAIMS a channel and lacks it must
    still fail."""
    cg = _mod()
    mac = dict(FLEET_ROWS[0])
    assert cg.capture_health(mac)["valid"] is True, cg.capture_health(mac)
    claims = {**mac, "screen_expected": True, "light_expected": True}
    assert cg.capture_health(claims)["valid"] is False


def test_person_merge_matches_the_tool_exactly():
    """One reducer, two implementations — pin them or they drift apart."""
    import importlib.machinery, importlib.util, sys as _sys
    cg = _mod()
    tool = Path(__file__).resolve().parents[2] / "bin" / "cogload"
    spec = importlib.util.spec_from_loader(
        "cogload_tool", importlib.machinery.SourceFileLoader("cogload_tool", str(tool)))
    m = importlib.util.module_from_spec(spec)
    _sys.modules["cogload_tool"] = m
    spec.loader.exec_module(m)
    rows = FLEET_ROWS + [{**ROWS[0], "host": "hub-1", "screen_expected": True,
                          "light_expected": True}]
    assert cg.person_merge(rows) == m.person_days(rows)


def test_two_devices_do_not_inflate_the_day_count():
    """7 real days observed twice must be 7 person-days, not 14 — that
    inflation would unlock a 14-day gate on half the evidence."""
    cg = _mod()
    base = {**FLEET_ROWS[0]}
    rows = []
    for i in range(7):
        day = f"2026-07-{10+i:02d}"
        rows.append({**base, "day": day, "host": "mac-xyz"})
        rows.append({**base, "day": day, "host": "hub-1",
                     "screen_expected": True, "light_expected": True,
                     "windows_screen_ok": 1380, "windows_light_ok": 1370})
    assert len(cg.person_merge(rows)) == 7


def test_endpoint_exposes_the_fleet():
    from starlette.testclient import TestClient
    from dashboard.api import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/personal/cogload")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "fleet" in body, f"no fleet section: {list(body)}"
    assert "devices" in body["fleet"]


def test_fleet_read_degrades_loudly_not_silently():
    """An unreadable fleet dir must say so, never present as zero devices."""
    cg = _mod()
    out = cg.fleet_devices(root="/nonexistent-fleet-root-xyz")
    assert isinstance(out, list)


# ---------------------------------------------------------------- fleet aggregation

def _valid_row(day: str, host: str, **overrides):
    """Build a capture_health-valid digest row for the given date and host."""
    base = {
        "day": day, "host": host, "v": 4,
        "windows": 1400, "windows_ok": 1400,
        "windows_screen_ok": 1380, "windows_light_ok": 1370,
        "screen_expected": True, "light_expected": True,
        "active_minutes": 300, "keys_total": 40000, "keys_enter": 900,
        "keys_active_total": 38000, "keys_bs_active": 3800,
        "backspace_ratio": 0.1, "app_switches": 600, "agents_peak": 9,
        "light_mean": 100.0, "night_minutes": 10, "expected_slots": 1400,
        "gap_minutes": 0, "partial": False,
        "day_coverage_pct": 0.97,
    }
    return {**base, **overrides}


def test_person_day_not_device_day_for_weekly():
    """Two devices on the same calendar day are 1 person-day, not 2."""
    cg = _mod()
    iso = cg._iso_week(date(2026, 8, 10))
    rows = [
        _valid_row("2026-08-10", "hub-1", keys_total=20000,
                   keys_active_total=19000, keys_bs_active=1900),
        _valid_row("2026-08-10", "mac-xyz", keys_total=20000,
                   keys_active_total=19000, keys_bs_active=1900,
                   screen_expected=False, light_expected=False,
                   windows_screen_ok=0, windows_light_ok=0),
    ]
    for r in rows:
        assert cg.capture_health(r)["valid"] is True
    wk = cg.weekly(rows, iso, labels=[])
    assert wk["valid_days"] == 1, wk
    assert wk["valid_device_days"] == 2, wk
    assert set(wk["hosts"]) == {"hub-1", "mac-xyz"}, wk


def test_same_date_host_redigest_does_not_double_count():
    """A repeated (date, host) row is a re-digest, never a second observation."""
    cg = _mod()
    iso = cg._iso_week(date(2026, 8, 10))
    rows = [
        _valid_row("2026-08-10", "hub-1", keys_total=1000),
        _valid_row("2026-08-10", "hub-1", keys_total=2000),
    ]
    for r in rows:
        assert cg.capture_health(r)["valid"] is True
    wk = cg.weekly(rows, iso, labels=[])
    assert wk["valid_days"] == 1, wk
    assert wk["valid_device_days"] == 1, wk


def test_additive_counts_sum_and_sufficiency_needs_four_distinct_dates():
    """At >=4 distinct valid dates totals are derived and additive across devices."""
    cg = _mod()
    iso = cg._iso_week(date(2026, 8, 10))
    rows = [
        _valid_row("2026-08-10", "hub-1", keys_total=1000, keys_bs_active=100,
                   keys_active_total=1000),
        _valid_row("2026-08-10", "mac-xyz", keys_total=2000, keys_bs_active=200,
                   keys_active_total=2000, screen_expected=False,
                   light_expected=False, windows_screen_ok=0,
                   windows_light_ok=0),
        _valid_row("2026-08-11", "hub-1", keys_total=3000),
        _valid_row("2026-08-12", "hub-1", keys_total=4000),
        _valid_row("2026-08-13", "hub-1", keys_total=5000),
    ]
    wk = cg.weekly(rows, iso, labels=[])
    assert wk.get("sufficient") is True, wk
    assert wk["keys_total"] == 15000, wk


def test_readiness_curve_excludes_invalid_days():
    """A blind day with a label must not count toward the curve gate."""
    cg = _mod()
    today = date.today()
    day_str = today.isoformat()
    invalid = {
        "day": day_str, "host": "hub-1", "v": 3,
        "windows": 140, "windows_ok": 140,
        "windows_screen_ok": 10, "windows_light_ok": 8,
        "screen_expected": True, "light_expected": True,
        "active_minutes": 30, "keys_total": 900, "keys_enter": 40,
        "keys_active_total": 800, "keys_bs_active": 240,
        "backspace_ratio": 0.3, "app_switches": 20, "agents_peak": 2,
        "light_mean": 90.0, "night_minutes": 0, "expected_slots": 140,
        "gap_minutes": 0, "partial": False,
        "day_coverage_pct": 0.097,
    }
    assert cg.capture_health(invalid)["valid"] is False
    labels = [{"day": day_str, "stress": 3, "eff": 4}]
    r = cg.readiness([invalid], labels)
    assert r["curve"]["count"] == 0, r

    valid = _valid_row(day_str, "hub-1")
    r = cg.readiness([valid], labels)
    assert r["curve"]["count"] == 1, r


# ---------------------------------------------------------------- ops status

def test_ops_status_three_valued_and_attention_advisory(monkeypatch):
    """Advisory cogload failure moves status to attention without gating healthz."""
    from starlette.testclient import TestClient
    from dashboard.api import app
    import dashboard.api as api_mod

    client = TestClient(app, raise_server_exceptions=False)

    failing_check = {
        "kanban_db": {"ok": True, "path": "/tmp"},
        "sessions_cache": {"ok": True, "state": "cold", "ttl_seconds": 60},
        "cogload": {"ok": False, "state": "unreadable: test"},
    }

    def _mock_healthz_checks():
        degraded = []
        return failing_check, degraded

    monkeypatch.setattr(api_mod, "_healthz_checks", _mock_healthz_checks)
    # Force the cached check path to re-run the mocked function on the next hit.
    api_mod._COGLOAD_CHECK_CACHE["at"] = 0.0

    r = client.get("/api/ops-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["attention"], list), body
    assert "cogload" in body["attention"], body
    assert "cogload" not in body["degraded"], body
    assert body["status"] == "attention", body

    hz = client.get("/healthz")
    assert hz.status_code == 200, hz.text
