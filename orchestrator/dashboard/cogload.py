"""Read-only aggregation module for the cogload cognitive-load collector.

This module is the read-only back-end behind a personal dashboard tab. It reads
the cogload store (default ``~/.local/share/cogload``) and produces aggregate
views for the dashboard. It NEVER copies behavioral data anywhere and NEVER
writes to the repo or to the store. Every function here is a pure read.

Store layout (read-only):
  keys/YYYY-MM/keys-YYYY-MM-DD.jsonl   one aggregate row per 60s window
  digest/YYYY-MM.jsonl                 one durable row per day (from `cogload digest`)
  labels.jsonl                          {"ts","stress","eff","note","src"} ground-truth labels

All functions are defensive: missing fields are treated as ``None`` rather than
raising, and missing files / directories / malformed JSON lines are skipped.
Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator

__all__ = [
    "capture_health",
    "weekly",
    "monthly",
    "readiness",
    "live_status",
    "weekly_markdown",
    "load_digest_days",
    "fleet_devices",
    "load_fleet_days",
    "person_merge",
    "load_labels",
    "load_day_windows",
    "store_dir",
]

# ---------------------------------------------------------------------------
# Store resolution
# ---------------------------------------------------------------------------

def store_dir() -> str:
    """Resolve the cogload store directory (read-only)."""
    return os.path.expanduser(
        os.environ.get("COGLOAD_DIR", "~/.local/share/cogload")
    )


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):  # bool is a subclass of int; treat as not-a-number
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
            return None
        return int(v)
    return None


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    return None


def _iter_jsonl(path: str) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, skipping malformed lines."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_digest_days(months: Iterable[str] | None = None,
                     start: date | None = None,
                     end: date | None = None) -> list[dict]:
    """Load digest day rows.

    ``months`` is an iterable of ``YYYY-MM`` strings. If omitted, the months
    spanning ``start``..``end`` are used; if those are also omitted, the current
    and previous month are read.
    """
    root = os.path.join(store_dir(), "digest")
    if months is None:
        if start is not None and end is not None:
            months = _months_between(start, end)
        else:
            today = date.today()
            months = [
                today.strftime("%Y-%m"),
                (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
            ]
    out: list[dict] = []
    for ym in months:
        path = os.path.join(root, f"{ym}.jsonl")
        for row in _iter_jsonl(path):
            out.append(row)
    return out


def _read_json_object(path: str) -> dict | None:
    """Read one JSON object, returning None for any missing/bad input."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _device_summary(path: str, digest_path: str | None = None) -> dict | None:
    """Return the non-behavioural fields needed by the fleet renderer."""
    raw = _read_json_object(path)
    if not raw or not raw.get("id"):
        return None
    out = {
        key: raw[key]
        for key in ("id", "slug", "platform", "role", "hub", "channels")
        if key in raw
    }
    try:
        out["last_push"] = os.path.getmtime(digest_path) if digest_path else None
    except OSError:
        out["last_push"] = None
    return out


def fleet_devices(root: str | None = None) -> list[dict]:
    """List local and pushed device metadata without mutating the store.

    ``root`` overrides only the fleet directory. It exists for callers that
    need to probe a particular fleet root; unreadable and malformed entries
    are ignored.
    """
    store = store_dir()
    out: list[dict] = []
    local = _device_summary(os.path.join(store, "device.json"))
    if local is not None:
        out.append(local)

    fleet_root = os.fspath(root) if root is not None else os.path.join(store, "fleet")
    try:
        entries = sorted(os.scandir(fleet_root), key=lambda entry: entry.name)
    except OSError:
        return out
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        summary = _device_summary(
            os.path.join(entry.path, "device.json"),
            os.path.join(entry.path, "digest.jsonl"),
        )
        if summary is not None:
            out.append(summary)
    return out


def load_fleet_days() -> list[dict]:
    """Load every local and pushed digest row, normalized with a host key."""
    store = store_dir()
    local_device = _read_json_object(os.path.join(store, "device.json")) or {}
    local_id = local_device.get("id")
    out: list[dict] = []

    digest_root = os.path.join(store, "digest")
    try:
        local_files = sorted(
            entry.path for entry in os.scandir(digest_root)
            if entry.is_file() and entry.name.endswith(".jsonl")
        )
    except OSError:
        local_files = []
    for path in local_files:
        for raw in _iter_jsonl(path):
            row = dict(raw)
            if not row.get("host"):
                row["host"] = local_id
            out.append(row)

    fleet_root = os.path.join(store, "fleet")
    try:
        fleet_entries = sorted(os.scandir(fleet_root), key=lambda entry: entry.name)
    except OSError:
        fleet_entries = []
    for entry in fleet_entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        remote_device = _read_json_object(
            os.path.join(entry.path, "device.json")
        ) or {}
        remote_id = remote_device.get("id") or local_id
        for raw in _iter_jsonl(os.path.join(entry.path, "digest.jsonl")):
            row = dict(raw)
            if not row.get("host"):
                # Legacy pushed rows predate ``host``. Their frozen sender id
                # is the closest local identity; only fall back to the hub id
                # when the pushed device metadata is itself unreadable.
                row["host"] = remote_id
            out.append(row)
    return out


def person_merge(rows: list) -> dict:
    """Reduce fleet instrument rows to one person row per calendar day."""
    by_day = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        day = r.get("day")
        if not day:
            continue
        host = r.get("host") or "legacy"
        slot = by_day.setdefault(day, {"day": day, "devices": {}})
        # Last row wins per (day, host): a re-digest supersedes, never doubles.
        slot["devices"][host] = r

    def day_valid(row: dict) -> tuple[bool, str]:
        health = capture_health(row)
        return bool(health["valid"]), health.get("reason") or ""

    out = {}
    for day, slot in by_day.items():
        devs = list(slot["devices"].values())
        valid = [d for d in devs if day_valid(d)[0]]
        bs = sum(d.get("keys_bs_active") or 0 for d in valid)
        act = sum(d.get("keys_active_total") or 0 for d in valid)
        out[day] = {
            "day": day,
            "hosts": sorted(slot["devices"]),
            "n_devices": len(devs),
            "n_valid_devices": len(valid),
            "valid": bool(valid),
            "keys_total": sum(d.get("keys_total") or 0 for d in valid),
            "keys_enter": sum(d.get("keys_enter") or 0 for d in valid),
            "keys_bs_active": bs,
            "keys_active_total": act,
            "backspace_ratio": (bs / act) if act else None,
            "active_minutes": sum(d.get("active_minutes") or 0 for d in valid),
            "per_device": {d.get("host") or "legacy": {
                "coverage": d.get("day_coverage_pct"),
                "valid": day_valid(d)[0],
                "reason": day_valid(d)[1],
                "app_switches": d.get("app_switches"),
                "light_mean": d.get("light_mean"),
            } for d in devs},
        }
    return out


def load_labels() -> list[dict]:
    """Load all ground-truth labels from ``labels.jsonl``."""
    path = os.path.join(store_dir(), "labels.jsonl")
    return list(_iter_jsonl(path))


def load_day_windows(day: date) -> list[dict]:
    """Load the raw 60s window rows for a single day."""
    ym = day.strftime("%Y-%m")
    path = os.path.join(store_dir(), "keys", ym, f"keys-{day.isoformat()}.jsonl")
    return list(_iter_jsonl(path))


def _wmean(acc: dict) -> float | None:
    """Weighted mean, or None when nothing contributed.

    None — not 0.0 — is the honest answer when no day carried the field. A zero
    here would be indistinguishable from a genuinely quiet period, which is the
    failure mode this whole surface exists to prevent.
    """
    w = acc.get("w") or 0
    if not w:
        return None
    return round(acc["sum"] / w, 3)


def _months_between(start: date, end: date) -> list[str]:
    out: list[str] = []
    cur = start.replace(day=1)
    end_m = end.replace(day=1)
    while cur <= end_m:
        out.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


# ---------------------------------------------------------------------------
# Day health
# ---------------------------------------------------------------------------

def capture_health(day_row: dict) -> dict:
    """Compute the capture-health summary for a single digest day row.

    Returns a dict with capture_pct, screen_pct, light_pct, valid (bool) and,
    when not valid, a short human ``reason``. A day with no data must return
    ``valid=False`` with reason ``"sin datos"`` -- never anything that reads
    as calm.
    """
    # A missing digest row (``days.get(date)`` -> None) is exactly how a day with
    # no data reaches this function. It must read as "sin datos", never raise and
    # never read as calm.
    if not isinstance(day_row, dict):
        return {
            "windows": 0,
            "windows_ok": 0,
            "windows_screen_ok": 0,
            "windows_light_ok": 0,
            "capture_pct": 0,
            "screen_pct": None,
            "light_pct": None,
            "partial": None,
            "gap_minutes": None,
            "valid": False,
            "reason": "sin datos",
        }

    windows = _coerce_int(day_row.get("windows"))
    windows_ok = _coerce_int(day_row.get("windows_ok"))
    windows_screen_ok = _coerce_int(day_row.get("windows_screen_ok"))
    windows_light_ok = _coerce_int(day_row.get("windows_light_ok"))
    partial = day_row.get("partial")
    gap_minutes = _coerce_float(day_row.get("gap_minutes"))

    # Treat absent windows as zero (no data) rather than None for the math.
    w = windows if windows is not None else 0
    wok = windows_ok if windows_ok is not None else 0
    wscreen = windows_screen_ok if windows_screen_ok is not None else 0
    wlight = windows_light_ok if windows_light_ok is not None else 0

    # No data at all.
    if w == 0:
        return {
            "windows": 0,
            "windows_ok": 0,
            "windows_screen_ok": 0,
            "windows_light_ok": 0,
            "capture_pct": 0,
            "screen_pct": None,
            "light_pct": None,
            "partial": partial,
            "gap_minutes": gap_minutes,
            "valid": False,
            "reason": "sin datos",
        }

    capture_pct = wok / w if w else 0
    screen_pct = (wscreen / wok) if wok else None
    light_pct = (wlight / wok) if wok else None

    valid = True
    reason = None
    if w < 60:
        valid = False
        reason = "pocas ventanas (<60)"
    elif capture_pct < 0.9:
        valid = False
        reason = "captura baja (<90%)"
    elif (day_row.get("screen_expected") is not False
          and (screen_pct is None or screen_pct < 0.8)):
        valid = False
        reason = "captura de pantalla baja (<80%)"
    elif (day_row.get("light_expected") is not False
          and (light_pct is None or light_pct < 0.8)):
        valid = False
        reason = "captura de luz baja (<80%)"
    elif partial:
        valid = False
        reason = "día parcial"
    elif gap_minutes is None:
        # FAIL CLOSED. `gap_minutes is not None and > 120` let a row that simply
        # lacks the field sail through as valid — an unmeasured day presenting
        # itself as a good one. "I could not measure the gaps" is not evidence
        # of no gaps, and this whole surface exists to keep those two apart.
        valid = False
        reason = "continuidad no medida"
    elif gap_minutes > 120:
        valid = False
        reason = "huecos grandes (>120 min)"

    return {
        "windows": w,
        "windows_ok": wok,
        "windows_screen_ok": wscreen,
        "windows_light_ok": wlight,
        "capture_pct": capture_pct,
        "screen_pct": screen_pct,
        "light_pct": light_pct,
        "partial": partial,
        "gap_minutes": gap_minutes,
        "valid": valid,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Period aggregation
# ---------------------------------------------------------------------------

def _day_date(row: dict) -> date | None:
    if not isinstance(row, dict):
        return None
    for key in ("date", "day", "ts", "timestamp"):
        v = row.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                continue
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(float(v)).date()
            except (ValueError, OSError, OverflowError):
                continue
    return None


def _iso_week(d: date) -> str:
    """Return ISO week as ``YYYY-Www``."""
    iso_y, iso_w, _ = d.isocalendar()
    return f"{iso_y:04d}-W{iso_w:02d}"


def _ym(d: date) -> str:
    return d.strftime("%Y-%m")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _aggregate_period(days: list[dict],
                      in_period: callable,
                      period_key: str,
                      period_value: str,
                      labels: list[dict] | None) -> dict:
    """Aggregate valid days that fall in a period.

    The backspace ratio is computed as ``sum(keys_bs_active) /
    sum(keys_active_total)`` (a ratio of sums, NOT the mean of per-day
    ratios). ``light_mean`` is weighted by ``windows_light_ok``.
    """
    # One row per (day, host) can arrive. Two rules, and they are different:
    #
    #   COUNTS are additive across devices — a person who typed on the laptop
    #   and the desktop on the same day typed both totals. That is the
    #   person-day semantic person_merge() already implements.
    #
    #   The DAY COUNT is not. Counting rows there is pseudo-replication: the
    #   same calendar day observed by two devices would count twice, and a
    #   14-day gate would unlock on 7 real days. `_seen_dates` was declared
    #   here to prevent exactly that and was then never used, so the guard the
    #   comment promised did not exist — harmless while only one device fed
    #   this function, and a live bug the moment fleet rows do.
    #
    # A repeated (date, host) is a RE-DIGEST, not a second observation:
    # last row wins, never sums. Same rule as person_merge().
    by_date_host: dict = {}
    for row in days:
        d = _day_date(row)
        if d is None or not in_period(d):
            continue
        h = capture_health(row)
        if not h["valid"]:
            continue
        by_date_host[(d, row.get("host") or "legacy")] = (d, row, h)

    valid_days = list(by_date_host.values())
    valid_dates = {d for d, _, _ in valid_days}

    base = {
        "period": period_key,
        "value": period_value,
        "valid_days": len(valid_dates),
        "valid_device_days": len(valid_days),
        "hosts": sorted({(r.get("host") or "legacy") for _, r, _ in valid_days}),
    }

    if labels is not None:
        period_labels = [
            lab for lab in labels
            if _label_in_period(lab, in_period)
        ]
        base["labeled_days"] = len({ _label_date(lab) for lab in period_labels if _label_date(lab) is not None })
        stress_vals = [ _coerce_float(lab.get("stress")) for lab in period_labels
                        if _coerce_float(lab.get("stress")) is not None ]
        eff_vals = [ _coerce_float(lab.get("eff")) for lab in period_labels
                     if _coerce_float(lab.get("eff")) is not None ]
        base["stress_median"] = _median(stress_vals)
        base["eff_median"] = _median(eff_vals)
    else:
        base["labeled_days"] = 0
        base["stress_median"] = None
        base["eff_median"] = None

    # Sufficiency counts PERSON-days, not device-days.
    if len(valid_dates) < 4:
        base["sufficient"] = False
        return base

    keys_bs = 0
    keys_active = 0
    app_switches = 0
    windows_ok_total = 0
    light_weighted_sum = 0.0
    light_weight_total = 0
    keys_total = 0
    keys_enter = 0
    clicks = 0
    weighted = {k: {"sum": 0.0, "w": 0} for k in
                ("open_windows", "focus_fragment", "agents")}
    agents_peak = 0
    night_minutes = 0
    pauses_2_10s = 0
    iki_p50_values: list[float] = []
    light_churn_values: list[float] = []
    light_contrast_values: list[float] = []

    for _d, row, h in valid_days:
        def gint(k: str) -> int:
            v = _coerce_int(row.get(k))
            return v if v is not None else 0
        def gfloat(k: str) -> float:
            v = _coerce_float(row.get(k))
            return v if v is not None else 0.0

        keys_bs += gint("keys_bs_active")
        keys_active += gint("keys_active_total")
        app_switches += gint("app_switches")
        windows_ok_total += h["windows_ok"]
        keys_total += gint("keys_total")
        keys_enter += gint("keys_enter")
        clicks += gint("clicks")
        night_minutes += gint("night_minutes")
        pauses_2_10s += gint("pauses_2_10s")
        agents_peak = max(agents_peak, gint("agents_peak"))

        # Weighted by each day's captured windows, and days MISSING the field are
        # excluded from their own denominator. The previous version summed
        # per-day means and divided by len(valid_days), which is two bugs at
        # once: a 20-minute day counted as much as a 14-hour day, and gfloat()'s
        # 0.0 default let an absent field drag the average toward zero while
        # still occupying a slot in the denominator — i.e. a day we could not
        # measure quietly reported as a low-load day.
        _w = h["windows_ok"]
        for _key, _acc in (("open_windows_mean", "open_windows"),
                           ("focus_fragment_s_mean", "focus_fragment"),
                           ("agents_mean", "agents")):
            _v = _coerce_float(row.get(_key))
            if _v is not None and _w:
                weighted[_acc]["sum"] += _v * _w
                weighted[_acc]["w"] += _w

        wlight = h["windows_light_ok"]
        lm = _coerce_float(row.get("light_mean"))
        if wlight and lm is not None:
            light_weighted_sum += lm * wlight
            light_weight_total += wlight

        iki = _coerce_float(row.get("iki_p50_median_ms"))
        if iki is not None:
            iki_p50_values.append(iki)
        lc = _coerce_float(row.get("light_contrast"))
        if lc is not None:
            light_contrast_values.append(lc)
        lch = _coerce_float(row.get("light_churn"))
        if lch is not None:
            light_churn_values.append(lch)

    backspace_ratio = (keys_bs / keys_active) if keys_active else None
    light_mean = (light_weighted_sum / light_weight_total) if light_weight_total else None
    captured_hours = windows_ok_total / 60.0 if windows_ok_total else 0.0
    switches_per_captured_hour = (
        app_switches / (windows_ok_total / 60.0)
    ) if windows_ok_total else None

    base.update({
        "sufficient": True,
        "backspace_ratio": backspace_ratio,
        "light_mean": light_mean,
        "switches_per_captured_hour": switches_per_captured_hour,
        "keys_total": keys_total,
        "keys_enter": keys_enter,
        "clicks": clicks,
        "app_switches": app_switches,
        "windows_ok": windows_ok_total,
        "captured_hours": captured_hours,
        "night_minutes": night_minutes,
        "pauses_2_10s": pauses_2_10s,
        "agents_peak": agents_peak,
        "open_windows_mean": _wmean(weighted["open_windows"]),
        "focus_fragment_s_mean": _wmean(weighted["focus_fragment"]),
        "agents_mean": _wmean(weighted["agents"]),
        "iki_p50_median_ms": _median(iki_p50_values),
        "light_contrast": _median(light_contrast_values),
        "light_churn": _median(light_churn_values),
    })
    return base


def _label_date(label: dict) -> date | None:
    if not isinstance(label, dict):
        return None
    # `day` FIRST — it is the local calendar day stamped at capture time and is
    # the only field that joins correctly to digest rows (which are keyed by
    # local date). `ts` is UTC: an evening label at 21:15 Monterrey is already
    # tomorrow in UTC, so truncating it would file the label against the WRONG
    # day and invert every association computed from it.
    day = label.get("day")
    if isinstance(day, str):
        try:
            return date.fromisoformat(day[:10])
        except ValueError:
            pass

    v = label.get("ts") or label.get("date")
    if v is None:
        return None
    if isinstance(v, str):
        # Legacy rows carry only a UTC ts. Convert to LOCAL before taking the
        # date, rather than truncating the UTC string.
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone().date()
            return dt.date()
        except ValueError:
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v)).date()
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _label_in_period(label: dict, in_period: callable) -> bool:
    d = _label_date(label)
    return d is not None and in_period(d)


def weekly(days: list[dict], iso_week: str, labels: list[dict] | None = None) -> dict:
    """Aggregate valid days within an ISO week (``YYYY-Www``)."""
    if labels is None:
        labels = load_labels()

    def in_period(d: date) -> bool:
        return _iso_week(d) == iso_week

    return _aggregate_period(days, in_period, "week", iso_week, labels)


def monthly(days: list[dict], ym: str, labels: list[dict] | None = None) -> dict:
    """Aggregate valid days within a month (``YYYY-MM``)."""
    if labels is None:
        labels = load_labels()

    def in_period(d: date) -> bool:
        return _ym(d) == ym

    return _aggregate_period(days, in_period, "month", ym, labels)


# ---------------------------------------------------------------------------
# Readiness (counts only)
# ---------------------------------------------------------------------------

def readiness(days: list[dict], labels: list[dict] | None = None) -> dict:
    """Three counters toward "enough continuous data".

    This function computes COUNTS ONLY. It must NEVER compute or return a
    correlation, a fitted curve, or a knee -- only ``bin/cogload curve`` may,
    so its refusal guard cannot be bypassed by a renderer.

    capture:     valid days within the last rolling 7 days, target 6
    calibration: distinct labeled days, target 14
    curve:       distinct days having BOTH a digest row and a label, target 14,
                 plus distinct load levels, target 3
    """
    if labels is None:
        labels = load_labels()

    today = date.today()
    week_ago = today - timedelta(days=6)  # rolling 7-day window inclusive

    # capture: valid days in the last rolling 7 days
    # DISTINCT person-days, not rows. With N devices the same calendar day is
    # observed once per machine, so counting rows is pseudo-replication: 7 real
    # days across 2 devices reported as 14 and unlocked a 14-day gate on half
    # the evidence. A day counts once, and counts if ANY device was valid on it.
    capture_dates: set = set()
    for row in days:
        d = _day_date(row)
        if d is None:
            continue
        if week_ago <= d <= today and capture_health(row)["valid"]:
            capture_dates.add(d)
    capture_count = len(capture_dates)

    # calibration: distinct labeled days
    label_dates: set[date] = set()
    for lab in labels:
        ld = _label_date(lab)
        if ld is not None:
            label_dates.add(ld)
    calibration_count = len(label_dates)

    # curve: distinct days having BOTH a digest row and a label, plus distinct
    # load levels (stress buckets). COUNTS ONLY -- no curve fitting here.
    # A digest row alone is NOT evidence: a blind day has a row too. Counting
    # it toward the curve would let the 14-day gate unlock on days the
    # instrument could not measure — the same silent-zero failure, one layer up.
    day_dates: set[date] = set()
    for row in days:
        d = _day_date(row)
        if d is not None and capture_health(row).get("valid"):
            day_dates.add(d)
    curve_days = day_dates & label_dates
    curve_count = len(curve_days)

    # Distinct load levels: coarse stress buckets among labels that also have
    # a digest day. This is a count of distinct levels, NOT a correlation.
    levels: set[int] = set()
    for lab in labels:
        ld = _label_date(lab)
        if ld is None or ld not in curve_days:
            continue
        s = _coerce_float(lab.get("stress"))
        if s is None:
            continue
        # Coarse 3-bucket mapping (low / mid / high) on a 0-10 scale.
        # Buckets for the ACTUAL 1-5 scale. They were 3.5/7.0 "on a 0-10 scale"
        # while `cogload mark --stress` is choices=range(1,6): stress 1-5 mapped
        # only to buckets {0,1}, so bucket 2 was unreachable and
        # load_levels_met could NEVER become true — the curve gate was
        # permanently unsatisfiable no matter how much data accrued.
        if s <= 2:
            levels.add(0)
        elif s == 3:
            levels.add(1)
        else:
            levels.add(2)
    load_levels = len(levels)

    return {
        "capture": {"count": capture_count, "target": 6, "met": capture_count >= 6},
        "calibration": {"count": calibration_count, "target": 14, "met": calibration_count >= 14},
        "curve": {
            "count": curve_count,
            "target": 14,
            "met": curve_count >= 14 and load_levels >= 3,
            "load_levels": load_levels,
            "load_levels_target": 3,
            "load_levels_met": load_levels >= 3,
        },
    }


# ---------------------------------------------------------------------------
# Live status (subprocess, cached)
# ---------------------------------------------------------------------------

_LIVE_CACHE: dict[str, Any] | None = None
_LIVE_CACHE_AT: float = 0.0
_LIVE_TTL = 30.0


def live_status() -> dict:
    """Run ``cogload status --json`` via subprocess with a 10s timeout.

    The result is cached for 30s in a module global. On any failure, returns
    ``{"available": False, "reason": "..."}`` -- never fabricates healthy.
    """
    global _LIVE_CACHE, _LIVE_CACHE_AT
    now = time.time()
    if _LIVE_CACHE is not None and (now - _LIVE_CACHE_AT) < _LIVE_TTL:
        return _LIVE_CACHE

    try:
        proc = subprocess.run(
            ["cogload", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        result = {"available": False, "reason": "cogload not found"}
    except subprocess.TimeoutExpired:
        result = {"available": False, "reason": "timeout"}
    except OSError as e:
        result = {"available": False, "reason": f"os error: {e}"}
    else:
        out = proc.stdout.strip()
        parsed = None
        parse_error = None
        if out:
            try:
                parsed = json.loads(out)
            except (json.JSONDecodeError, ValueError) as e:
                parse_error = e

        # `cogload status --json` uses exit 1 for a valid, structured degraded
        # state. Preserve that payload: a non-zero health gate is not a broken
        # subprocess. Only accept non-zero output when it explicitly says
        # `ok: false`; arbitrary JSON from a failed command stays unavailable.
        structured = isinstance(parsed, dict)
        degraded = structured and parsed.get("ok") is False
        if structured and (proc.returncode == 0 or degraded):
            result = dict(parsed)
            result.setdefault("available", True)
            if result.get("ok") is False and not result.get("reason"):
                reason = result.get("last_status")
                if not reason:
                    failed = []
                    for name, state in (result.get("subsystems") or {}).items():
                        if isinstance(state, dict) and state.get("available") is False:
                            failed.append(f"{name}: {state.get('reason') or 'unavailable'}")
                    reason = "; ".join(failed)
                if reason:
                    result["reason"] = reason
        elif proc.returncode != 0:
            reason = ((proc.stderr or "").strip() or out or
                      f"exit {proc.returncode}")
            result = {"available": False, "reason": reason[:300]}
        elif not out:
            result = {"available": False, "reason": "empty output"}
        elif parse_error is not None:
            result = {"available": False, "reason": f"bad json: {parse_error}"}
        else:
            result = {"available": False, "reason": "non-object json"}

    _LIVE_CACHE = result
    _LIVE_CACHE_AT = now
    return result


# ---------------------------------------------------------------------------
# Outbound text (privacy-safe)
# ---------------------------------------------------------------------------

def weekly_markdown(week: dict) -> str:
    """Render a week aggregate as markdown for OUTBOUND messages.

    Emits ONLY numbers and counts. NEVER includes label note text (the
    ``note`` field of labels is never read here, and this function takes only
    the already-aggregated ``week`` dict, which contains no note strings).
    """
    if not week.get("sufficient", False):
        return (
            f"**{week.get('period','week')} {week.get('value','')}**: "
            f"datos insuficientes ({week.get('valid_days',0)} días válidos, "
            f"mínimo 4)."
        )

    def fmt(v: Any, spec: str = "") -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    lines = [
        f"**{week.get('period','week')} {week.get('value','')}**",
        f"- Días válidos: {week.get('valid_days',0)}",
        f"- Días etiquetados: {week.get('labeled_days',0)}",
        f"- Horas capturadas: {fmt(week.get('captured_hours'))}",
        f"- Backspace ratio: {fmt(week.get('backspace_ratio'))}",
        f"- Switches/hora capturada: {fmt(week.get('switches_per_captured_hour'))}",
        f"- Luz media: {fmt(week.get('light_mean'))}",
        f"- Estrés mediano: {fmt(week.get('stress_median'))}",
        f"- Eficacia mediana: {fmt(week.get('eff_median'))}",
        f"- Minutos nocturnos: {week.get('night_minutes',0)}",
        f"- Pausas 2-10s: {week.get('pauses_2_10s',0)}",
        f"- Agentes (media / pico): {fmt(week.get('agents_mean'))} / {week.get('agents_peak',0)}",
    ]
    return "\n".join(lines)
