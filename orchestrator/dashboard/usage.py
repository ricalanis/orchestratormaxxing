"""
Hermes Orchestrator Dashboard — Usage tracking layer (real data).

- **Claude Max**: aggregated from the local Claude Code transcripts
  (~/.claude/projects/*/*.jsonl) — every assistant message carries a `usage`
  blob (input/output/cache tokens + model). This is real consumption, no API
  needed. We also show an "API-equivalent" cost so the Max subscription's value
  is legible.
- **Ollama Cloud**: aggregated from ~/.local/share/orchestratormaxxing/oll-usage.jsonl,
  which the `oll` bridge appends to on every call (real going forward). Plus the
  live model catalog.
Both are cached briefly (scanning transcripts is not free).
"""
import json
import os
import time
import datetime
from pathlib import Path
from typing import Optional

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
OLL_USAGE_LOG = Path.home() / ".local" / "share" / "orchestratormaxxing" / "oll-usage.jsonl"
HERMES_STATE_DB = Path.home() / ".hermes" / "state.db"
OLLAMA_BASE = "https://ollama.com/v1"
# The REAL Ollama usage %, scraped from ollama.com/settings (which has no API) by
# scrape_ollama_usage.py and cached here. When fresh, it drives the Ollama card's
# "● real" bars instead of the token-proxy estimate.
OLLAMA_SCRAPE_FILE = Path.home() / ".local" / "share" / "orchestratormaxxing" / "ollama-usage.json"
OLLAMA_SCRAPE_TTL = 6 * 3600   # scraped numbers count as fresh for 6h
# Claude Code's own usage endpoint — the REAL rolling-window numbers (the same
# ones the TUI /usage screen shows): five_hour + seven_day utilization + reset
# times. Authenticated with the local OAuth access token.
CLAUDE_USAGE_API = "https://api.anthropic.com/api/oauth/usage"

# Rough API-equivalent $/million tokens, to make Max consumption legible.
# (in, out) — cache reads billed ~= 0.1x input. Best-effort, clearly labeled est.
CLAUDE_RATES = {
    "opus":   (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (0.80, 4.0),
    "fable":  (5.0, 25.0),
    "mythos": (5.0, 25.0),
    "_default": (5.0, 25.0),
}

_CACHE = {"claude": (0.0, None), "ollama": (0.0, None)}
_TTL = 120

# --- Capacity budgets (the "what % of my plan have I used?" numbers) ---
# Claude Max/Pro enforce weekly + rolling limits SERVER-SIDE; the quota is not
# written to any local file (~/.claude.json only carries the plan *tier*). So we
# estimate a weekly token ceiling per tier to turn raw consumption into a
# percentage + progress bar. These are deliberately rough and clearly labeled
# "est." in the UI, and are overridable via env so the operator can calibrate them
# to what he actually observes hitting a wall at.
#   HERMES_CLAUDE_WEEKLY_TOKEN_BUDGET  — override the weekly ceiling (tokens)
#   HERMES_CLAUDE_HOURLY_TOKEN_BUDGET  — override the hourly (rolling 60m) ceiling
# Tokens counted include cache reads (they dominate), so ceilings are large.
_WEEKLY_BUDGET_BY_TIER = {
    "default_claude_max_20x": 7_000_000_000,   # Max 20x — the top tier
    "claude_max_20x":         7_000_000_000,
    "default_claude_max_5x":  1_750_000_000,    # 5x ≈ a quarter of 20x
    "claude_max_5x":          1_750_000_000,
    "default_claude_pro":       350_000_000,
    "claude_pro":               350_000_000,
    "_default":               3_000_000_000,
}


def _weekly_budget(tier: str) -> int:
    env = os.environ.get("HERMES_CLAUDE_WEEKLY_TOKEN_BUDGET")
    if env and env.strip().isdigit():
        return int(env.strip())
    return _WEEKLY_BUDGET_BY_TIER.get(tier or "", _WEEKLY_BUDGET_BY_TIER["_default"])


def _session_budget(weekly: int) -> int:
    # Claude's real limit is a 5-HOUR rolling session window (not an hourly cap).
    # This estimated ceiling is only a FALLBACK for when the live usage API is
    # unavailable; normally the real percent comes straight from the API.
    env = os.environ.get("HERMES_CLAUDE_SESSION_TOKEN_BUDGET")
    if env and env.strip().isdigit():
        return int(env.strip())
    return max(1, round(weekly / 10))


def _pct(used: int, limit: int) -> float:
    if not limit:
        return 0.0
    return round(min(100.0, used / limit * 100), 1)


# --- Ollama Cloud capacity ---
# Ollama Cloud uses the SAME window shape as Claude Max — a 5-hour session limit +
# a weekly limit — but metered by GPU-TIME (not tokens/requests), with NO published
# numbers, NO usage API, and the real % visible only on ollama.com/settings (behind
# web login). So we approximate capacity from tokens (the only local signal) against
# an estimated per-tier ceiling, clearly labeled a proxy. Override hooks:
#   HERMES_OLLAMA_TIER            free | pro | max  (default free)
#   HERMES_OLLAMA_{SESSION,WEEKLY}_TOKEN_BUDGET   calibrate the ceilings
#   HERMES_OLLAMA_{SESSION,WEEKLY}_PCT            plug in the REAL % read off settings
_OLLAMA_TIER_MULT = {"free": 1.0, "pro": 50.0, "max": 250.0}   # per ollama.com/pricing
_OLLAMA_FREE_WEEKLY_TOKENS = 5_000_000   # rough free-tier weekly token-equivalent


def _ollama_tier() -> str:
    return (os.environ.get("HERMES_OLLAMA_TIER") or "unknown").strip().lower()


def _ollama_budgets():
    tier = _ollama_tier()
    mult = _OLLAMA_TIER_MULT.get(tier, 1.0)
    env_w = os.environ.get("HERMES_OLLAMA_WEEKLY_TOKEN_BUDGET")
    weekly = int(env_w) if env_w and env_w.strip().isdigit() else round(_OLLAMA_FREE_WEEKLY_TOKENS * mult)
    env_s = os.environ.get("HERMES_OLLAMA_SESSION_TOKEN_BUDGET")
    session = int(env_s) if env_s and env_s.strip().isdigit() else max(1, round(weekly / 10))
    return tier, session, weekly


def get_ollama_scraped() -> Optional[dict]:
    """Return the latest successful settings scrape, including a preserved
    last-good reading when the most recent attempt failed."""
    attempt = load_json(OLLAMA_SCRAPE_FILE, {})
    if not attempt:
        return None
    d = attempt if attempt.get("ok") else attempt.get("last_good")
    if not isinstance(d, dict) or not d.get("ok"):
        return None
    age = time.time() - (d.get("scraped_at") or 0)
    d = dict(d)
    d["age_seconds"] = int(age)
    d["stale"] = age > OLLAMA_SCRAPE_TTL
    if not attempt.get("ok"):
        d["refresh_error"] = attempt.get("reason") or "latest scrape failed"
        d["last_attempt_at"] = attempt.get("scraped_at")
    return d


def get_ollama_scrape_meta() -> dict:
    """Scrape freshness/status — ALWAYS returned, even when the last scrape
    FAILED (ok: false), so the UI can show 'Last refreshed: X min ago', a
    stale (>6h) warning, and 'Unavailable: <reason>' instead of silently
    dropping the failure. get_ollama_scraped() returns None on ok:false and
    thus loses `scraped_at`/`reason`; this keeps them.

    {attempted, ok, last_scrape_at, age_seconds, stale, reason}
      attempted=False → the scraper has never run (no file at all)."""
    d = load_json(OLLAMA_SCRAPE_FILE, {})
    if not d:
        return {"attempted": False, "ok": False, "last_scrape_at": None,
                "age_seconds": None, "stale": None, "reason": "never scraped"}
    using_last_good = not d.get("ok") and isinstance(d.get("last_good"), dict)
    good = d.get("last_good") if using_last_good else d
    scraped_at = good.get("scraped_at") or 0
    age = int(time.time() - scraped_at) if scraped_at else None
    return {
        "attempted": True,
        "ok": bool(d.get("ok")),
        "last_scrape_at": scraped_at or None,
        "last_attempt_at": d.get("scraped_at") or None,
        "age_seconds": age,
        "stale": (age is not None and age > OLLAMA_SCRAPE_TTL),
        "reason": d.get("reason"),
        "using_last_good": using_last_good,
    }


_LIVE_LIMITS_CACHE = {"ts": 0.0, "data": None}
_LIVE_LIMITS_TTL = 90


def get_claude_live_limits() -> dict:
    """The REAL Claude plan limits from Claude Code's own usage endpoint —
    `five_hour` (the rolling session window) and `seven_day` (weekly), each with a
    true utilization percent and reset time. This is the exact data the TUI
    `/usage` screen shows; no estimation. Authenticated with the local OAuth
    access token (~/.claude/.credentials.json). Returns {available: False} if the
    token is missing/expired or the call fails, so callers can fall back."""
    now = time.time()
    if _LIVE_LIMITS_CACHE["data"] is not None and now - _LIVE_LIMITS_CACHE["ts"] < _LIVE_LIMITS_TTL:
        return _LIVE_LIMITS_CACHE["data"]

    out = {"available": False}
    creds = load_json(CLAUDE_CREDS, {})
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if token:
        # stdlib urllib (no `requests` dependency — the dashboard venv lacks it).
        import urllib.request
        import urllib.error
        req = urllib.request.Request(CLAUDE_USAGE_API, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))

            def win(key):
                w = data.get(key) or {}
                return {
                    "pct": round(float(w.get("utilization") or 0), 1),
                    "resets_at": w.get("resets_at"),
                }

            out = {
                "available": True,
                "five_hour": win("five_hour"),
                "seven_day": win("seven_day"),
                "limits": data.get("limits") or [],
                "source": "live",
            }
        except urllib.error.HTTPError as e:
            # 401 = token expired (Claude Code refreshes it on next run) → fall back.
            out = {"available": False, "http_status": e.code}
        except Exception as e:  # network / parse failure → caller falls back
            out = {"available": False, "error": str(e)[:120]}

    _LIVE_LIMITS_CACHE["ts"] = now
    _LIVE_LIMITS_CACHE["data"] = out
    return out


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


# ---- Ollama Cloud ----

def get_ollama_cloud_key() -> Optional[str]:
    data = load_json(AUTH_FILE, {})
    key = data.get("ollama-cloud", {}).get("key")
    return str(key) if key else None


def get_ollama_models() -> list:
    if requests is None:
        return []
    try:
        headers = {"Accept": "application/json"}
        k = get_ollama_cloud_key()
        if k:
            headers["Authorization"] = f"Bearer {k}"
        resp = requests.get(f"{OLLAMA_BASE}/models", headers=headers, timeout=8)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        return []


def invalidate_ollama_cache() -> None:
    """Drop the memoized Ollama usage so the next read re-aggregates and re-reads
    the freshly-scraped store. Used by the manual-refresh endpoint after it runs
    the scraper, so the response reflects the new numbers immediately."""
    _CACHE["ollama"] = (0.0, None)


def invalidate_claude_cache() -> None:
    """Drop BOTH the memoized Claude usage and the live-limits cache (90s TTL), so
    the next read re-hits Claude Code's usage API with the current OAuth token.
    Used by the manual-refresh endpoint to reflect a freshly-refreshed token
    (or a still-expired one) immediately."""
    _CACHE["claude"] = (0.0, None)
    _LIVE_LIMITS_CACHE["ts"] = 0.0
    _LIVE_LIMITS_CACHE["data"] = None


def _merge_model_counts(by_model, model, calls, prompt_tokens, completion_tokens, total_tokens):
    m = model or "unknown"
    bm = by_model.setdefault(m, {"model": m, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    bm["calls"] += calls
    bm["prompt_tokens"] += prompt_tokens
    bm["completion_tokens"] += completion_tokens
    bm["total_tokens"] += total_tokens


def get_ollama_usage() -> dict:
    """Aggregate Ollama Cloud usage from oll-usage.jsonl AND Hermes state.db."""
    now = time.time()
    ts, cached = _CACHE["ollama"]
    if cached is not None and now - ts < _TTL:
        return cached

    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    by_model = {}
    by_day = {}
    recent = []
    session_cut = now - 5 * 3600       # Ollama's window shape mirrors Claude's
    week_cut = now - 7 * 86400
    session_tokens = 0
    week_tokens = 0

    # Source 1: per-call log from the `oll` bridge
    if OLL_USAGE_LOG.exists():
        try:
            lines = OLL_USAGE_LOG.read_text().strip().split("\n")
        except Exception:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            pt = r.get("prompt_tokens", 0) or 0
            ct = r.get("completion_tokens", 0) or 0
            tt = r.get("total_tokens", 0) or (pt + ct)
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["total_tokens"] += tt
            totals["calls"] += 1
            m = r.get("model", "unknown")
            _merge_model_counts(by_model, m, 1, pt, ct, tt)
            if r.get("ts"):
                day = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
                by_day[day] = by_day.get(day, 0) + tt
                if r["ts"] > session_cut:
                    session_tokens += tt
                if r["ts"] > week_cut:
                    week_tokens += tt
        recent = [json.loads(l) for l in lines[-8:] if l.strip()][::-1]

    # Source 2: per-session cumulative totals from every Hermes conversation using Ollama Cloud
    try:
        import sqlite3
        if HERMES_STATE_DB.exists():
            conn = sqlite3.connect(HERMES_STATE_DB)
            cur = conn.execute(
                """
                SELECT model, input_tokens, output_tokens, api_call_count, started_at
                FROM sessions
                WHERE billing_provider LIKE '%ollama%'
                """
            )
            for row in cur.fetchall():
                model, input_tokens, output_tokens, api_call_count, started_at = row
                pt = input_tokens or 0
                ct = output_tokens or 0
                tt = pt + ct
                calls = api_call_count or 0
                totals["prompt_tokens"] += pt
                totals["completion_tokens"] += ct
                totals["total_tokens"] += tt
                totals["calls"] += calls
                _merge_model_counts(by_model, model, calls, pt, ct, tt)
                if started_at:
                    day = datetime.datetime.fromtimestamp(started_at).strftime("%Y-%m-%d")
                    by_day[day] = by_day.get(day, 0) + tt
                    if started_at > session_cut:
                        session_tokens += tt
                    if started_at > week_cut:
                        week_tokens += tt
            conn.close()
    except Exception:
        pass

    # Ollama Cloud has no published hard token cap, so "capacity used" is
    # relative: today's tokens vs the average active day. Gives the operator the same
    # "am I running hot?" read without inventing a ceiling.
    days14 = _last_days(by_day, 14)
    today_key = datetime.date.today().strftime("%Y-%m-%d")
    today_tokens = by_day.get(today_key, 0)
    active = [d["tokens"] for d in days14 if d["tokens"] > 0]
    avg_per_day = round(sum(active) / len(active)) if active else 0
    peak_day = max((d["tokens"] for d in days14), default=0)
    relative = {
        "today_tokens": today_tokens,
        "avg_per_day": avg_per_day,
        "peak_day": peak_day,
        # % of an average day (100 = a typical day; >100 = running hotter).
        "pct_of_avg": _pct(today_tokens, avg_per_day) if avg_per_day else 0.0,
        # % of the busiest day in the window — a soft "headroom vs your own peak".
        "pct_of_peak": _pct(today_tokens, peak_day) if peak_day else 0.0,
        "has_baseline": avg_per_day > 0,
    }

    # 5h-session + weekly capacity, mirroring the Claude Max card. Ollama exposes
    # the real percentages only in the signed-in settings page; manual values are
    # supported for recovery, but token activity must not impersonate quota.
    tier = _ollama_tier()
    man_s = os.environ.get("HERMES_OLLAMA_SESSION_PCT")
    man_w = os.environ.get("HERMES_OLLAMA_WEEKLY_PCT")
    scraped = get_ollama_scraped()

    def _f(x):
        try:
            return round(float(x), 1)
        except Exception:
            return None

    if scraped and not scraped.get("stale"):
        # REAL numbers scraped from ollama.com/settings (the only source of truth).
        capacity = {
            "source": "real",
            "tier": scraped.get("tier") or tier,
            "scraped_at": scraped.get("scraped_at"),
            "age_seconds": scraped.get("age_seconds"),
            "session": {"pct": _f(scraped.get("session_pct")) or 0.0,
                        "resets_at": scraped.get("session_resets_at"), "window": "5h rolling"},
            "weekly": {"pct": _f(scraped.get("weekly_pct")) or 0.0,
                       "resets_at": scraped.get("weekly_resets_at"), "window": "7 days"},
        }
        if scraped.get("refresh_error"):
            capacity["refresh_error"] = scraped["refresh_error"]
    elif man_s is not None or man_w is not None:
        capacity = {
            "source": "manual",   # real % supplied by the operator (e.g. off settings)
            "tier": tier,
            "session": {"pct": _f(man_s) or 0.0, "window": "5h rolling"},
            "weekly": {"pct": _f(man_w) or 0.0, "window": "7 days"},
        }
    else:
        capacity = {
            # Ollama exposes quota only on its signed-in settings page. Raw token
            # counts are useful activity telemetry, but they are not a quota and
            # must never manufacture a red capacity alarm.
            "source": "unavailable",
            "tier": ((scraped or {}).get("tier")
                     or (tier if tier in _OLLAMA_TIER_MULT else None)),
            "session": {"pct": None, "window": "5h rolling"},
            "weekly": {"pct": None, "window": "7 days"},
        }
    capacity["settings_url"] = "https://ollama.com/settings"
    capacity["tier_mult"] = _OLLAMA_TIER_MULT.get(capacity.get("tier"), 1.0)
    # Always surface scrape freshness — even when a STALE scrape (>6h) has fallen
    # back to the manual/unavailable source, the UI needs it to show a 'stale'
    # indicator and prompt a manual refresh (TTL = OLLAMA_SCRAPE_TTL).
    if scraped:
        capacity["scraped_at"] = scraped.get("scraped_at")
        capacity["scrape_age_seconds"] = scraped.get("age_seconds")
        capacity["scrape_stale"] = bool(scraped.get("stale"))

    # Health: the oll-usage.jsonl log is the PRIMARY, always-available source
    # (Ollama Cloud has no usage API — verified: /api/usage etc. all 404). The
    # CDP scraper supplies the authoritative % from ollama.com/settings. When it
    # is broken/absent, activity remains available but capacity degrades honestly.
    log_present = OLL_USAGE_LOG.exists()
    scrape_meta = get_ollama_scrape_meta()
    src = capacity["source"]
    if src == "real":
        # A stale-but-present scrape is still "healthy scraped" — flag staleness
        # in the reason so the UI shows a warning without hiding the number.
        stale_note = " (stale >6h — refresh)" if scrape_meta.get("stale") else ""
        refresh_note = (f" (latest refresh failed: {scrape_meta.get('reason')})"
                        if scrape_meta.get("using_last_good") else "")
        health = {"healthy": True, "source": "real",
                  "reason": f"live % from ollama.com/settings{stale_note}{refresh_note}"}
    elif log_present:
        # Log estimate is the working fallback. When the scraper FAILED, name
        # why (e.g. the 405) so the UI can show it, not just "approximate".
        why = ""
        if scrape_meta.get("attempted") and not scrape_meta.get("ok"):
            why = f" (scraper failed: {scrape_meta.get('reason') or 'unknown'})"
        if src == "manual":
            reason = "real % supplied manually"
        else:
            reason = ("activity log available, but real capacity is unavailable"
                      f" — scraper offline/stale{why}")
        health = {"healthy": True, "source": src, "reason": reason}
    else:
        health = {"healthy": False, "source": src,
                  "reason": "no oll-usage.jsonl log yet and no scrape — unavailable"}

    result = {
        "provider": "ollama",
        "label": "Ollama Cloud",
        "available": True,
        "key_present": get_ollama_cloud_key() is not None,
        "totals": totals,
        # Normalized fields for the unified cross-provider roll-up:
        "grand_total_tokens": totals["total_tokens"],
        "today_tokens": relative.get("today_tokens", 0),
        "week_tokens": week_tokens,
        "session_tokens": session_tokens,
        "cost_est_usd": 0.0,  # Ollama Cloud has no published $/token — free tier
        "by_model": sorted(by_model.values(), key=lambda x: -x["total_tokens"]),
        "by_day": days14,
        "relative": relative,
        "capacity": capacity,
        "recent": recent,
        "models": get_ollama_models(),
        "log_present": log_present,
        "health": health,
        # Phase 3: scrape freshness/status — present even when the scrape
        # FAILED, so the UI can render "Last refreshed: X min ago", a >6h stale
        # warning, and "Unavailable: <reason>" from the CDP scraper.
        "scrape": scrape_meta,
        "last_scrape_at": scrape_meta.get("last_scrape_at"),
    }
    _CACHE["ollama"] = (now, result)
    return result


def log_ollama_completion(model: str, usage: dict, metadata: dict = None) -> dict:
    """Back-compat: append a usage record from the API endpoint."""
    try:
        OLL_USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(time.time()),
            "model": model,
            "prompt_tokens": (usage or {}).get("prompt_tokens", 0),
            "completion_tokens": (usage or {}).get("completion_tokens", 0),
            "total_tokens": (usage or {}).get("total_tokens", 0),
        }
        with open(OLL_USAGE_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        _CACHE["ollama"] = (0.0, None)  # invalidate
        return rec
    except Exception as e:
        return {"error": str(e)}


# ---- Claude Max (from local transcripts) ----

def _model_family(model: str) -> str:
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if fam in m:
            return fam
    return "other"


def _rate(model: str):
    return CLAUDE_RATES.get(_model_family(model), CLAUDE_RATES["_default"])


def _last_days(by_day: dict, n: int) -> list:
    """Return [{day,label,tokens}] for the last n calendar days, oldest→newest."""
    today = datetime.date.today()
    out = []
    for i in range(n - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        out.append({"day": key, "label": d.strftime("%b %-d"), "tokens": by_day.get(key, 0)})
    return out


def get_claude_usage() -> dict:
    """Aggregate real Claude Code token usage from local transcripts."""
    now = time.time()
    ts, cached = _CACHE["claude"]
    if cached is not None and now - ts < _TTL:
        return cached

    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "messages": 0}
    by_model = {}
    by_day = {}
    by_project = {}
    cost_est = 0.0
    today_key = datetime.date.today().strftime("%Y-%m-%d")
    week_cut = time.time() - 7 * 86400
    session_cut = time.time() - 5 * 3600   # Claude's real window is 5h rolling
    today_tokens = 0
    week_tokens = 0
    session_tokens = 0

    if CLAUDE_PROJECTS.exists():
        cutoff = time.time() - 30 * 86400  # only scan the last 30 days of files
        files = [f for f in CLAUDE_PROJECTS.glob("*/*.jsonl")
                 if f.stat().st_mtime > cutoff]
        for f in files:
            home_slug = str(Path.home()).replace("/", "-") + "-"
            proj = f.parent.name.replace(home_slug, "").replace("-", "/")
            try:
                fh = open(f, "r", encoding="utf-8", errors="ignore")
            except Exception:
                continue
            with fh:
                for line in fh:
                    if '"output_tokens"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    model = msg.get("model", "")
                    i_tok = u.get("input_tokens", 0) or 0
                    o_tok = u.get("output_tokens", 0) or 0
                    cr = u.get("cache_read_input_tokens", 0) or 0
                    cc = u.get("cache_creation_input_tokens", 0) or 0
                    totals["input"] += i_tok
                    totals["output"] += o_tok
                    totals["cache_read"] += cr
                    totals["cache_creation"] += cc
                    totals["messages"] += 1
                    tok = i_tok + o_tok + cr + cc

                    fam = _model_family(model) if model else "other"
                    bm = by_model.setdefault(fam, {"model": fam, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "messages": 0, "cost_est": 0.0})
                    bm["input"] += i_tok; bm["output"] += o_tok
                    bm["cache_read"] += cr; bm["cache_creation"] += cc
                    bm["messages"] += 1

                    r_in, r_out = _rate(model)
                    c = (i_tok + cc) / 1e6 * r_in + cr / 1e6 * (r_in * 0.1) + o_tok / 1e6 * r_out
                    cost_est += c
                    bm["cost_est"] += c

                    by_project[proj] = by_project.get(proj, 0) + tok

                    ts_iso = d.get("timestamp")
                    if ts_iso:
                        try:
                            dt = datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone()
                            dk = dt.strftime("%Y-%m-%d")
                            by_day[dk] = by_day.get(dk, 0) + tok
                            if dk == today_key:
                                today_tokens += tok
                            if dt.timestamp() > week_cut:
                                week_tokens += tok
                            if dt.timestamp() > session_cut:
                                session_tokens += tok
                        except Exception:
                            pass

    top_projects = sorted(
        ({"project": k, "tokens": v} for k, v in by_project.items()),
        key=lambda x: -x["tokens"])[:8]

    sub = get_claude_subscription_info()
    tier = sub.get("rate_limit_tier") or ""
    plan = sub.get("plan") or _plan_label(sub.get("organization_type"), tier)

    # Prefer the REAL rolling-window numbers from Claude Code's usage API
    # (five_hour session + seven_day weekly, true % + reset time). Only if that's
    # unavailable do we fall back to an estimated ceiling over local tokens.
    live = get_claude_live_limits()
    if live.get("available"):
        limits = {
            "session": {                       # the 5-hour rolling window
                "pct": live["five_hour"]["pct"],
                "resets_at": live["five_hour"]["resets_at"],
                "window": "5h rolling",
            },
            "weekly": {
                "pct": live["seven_day"]["pct"],
                "resets_at": live["seven_day"]["resets_at"],
                "window": "7 days",
            },
            "raw": live.get("limits", []),     # per-scope limits (opus/weekly/etc.)
            "tier": tier,
            "plan": plan,
            "source": "live",                  # real numbers from Claude's usage API
        }
    else:
        weekly_budget = _weekly_budget(tier)
        session_budget = _session_budget(weekly_budget)
        est_source = "manual" if os.environ.get("HERMES_CLAUDE_WEEKLY_TOKEN_BUDGET") else "estimate"
        limits = {
            "session": {
                "used": session_tokens, "limit": session_budget,
                "pct": _pct(session_tokens, session_budget),
                "remaining": max(0, session_budget - session_tokens),
                "window": "5h rolling",
            },
            "weekly": {
                "used": week_tokens, "limit": weekly_budget,
                "pct": _pct(week_tokens, weekly_budget),
                "remaining": max(0, weekly_budget - week_tokens),
                "window": "7 days",
            },
            "tier": tier,
            "plan": plan,
            "source": est_source,              # 'estimate' | 'manual' — live API unavailable
            # Surface WHY the live API is unavailable so the UI can warn instead
            # of silently showing estimates. 401 → the OAuth token expired (Claude
            # Code refreshes it on its next run).
            "live_unavailable": True,
            "live_http_status": live.get("http_status"),
            "live_error": live.get("error"),
            "token_expired": live.get("http_status") == 401,
        }

    result = {
        "provider": "claude",
        "label": "Claude Max",
        "available": True,
        "subscription": sub,
        "limits": limits,
        "totals": totals,
        "grand_total_tokens": totals["input"] + totals["output"] + totals["cache_read"] + totals["cache_creation"],
        "today_tokens": today_tokens,
        "week_tokens": week_tokens,
        "session_tokens": session_tokens,
        "cost_est_usd": round(cost_est, 2),
        "by_model": sorted(by_model.values(), key=lambda x: -(x["input"] + x["output"] + x["cache_read"] + x["cache_creation"])),
        "by_day": _last_days(by_day, 14),
        "top_projects": top_projects,
    }
    _CACHE["claude"] = (now, result)
    return result


def _plan_label(org_type: str, tier: str) -> str:
    """Human-readable plan name from the config's org type + rate-limit tier."""
    t = (tier or "").lower()
    if "20x" in t:
        return "Claude Max 20×"
    if "5x" in t:
        return "Claude Max 5×"
    o = (org_type or "").lower()
    if "max" in o:
        return "Claude Max"
    if "pro" in o:
        return "Claude Pro"
    return org_type or "Claude"


def get_claude_subscription_info() -> dict:
    data = load_json(CLAUDE_JSON, {})
    oauth = data.get("oauthAccount", {})
    if not oauth:
        return {"available": False}
    tier = oauth.get("organizationRateLimitTier") or oauth.get("userRateLimitTier") or "unknown"
    org_type = oauth.get("organizationType")
    return {
        "available": True,
        "email": oauth.get("emailAddress"),
        "display_name": oauth.get("displayName"),
        "organization": oauth.get("organizationName"),
        "organization_type": org_type,
        "rate_limit_tier": tier,
        "plan": _plan_label(org_type, tier),
        "has_extra_usage": oauth.get("hasExtraUsageEnabled", False),
    }


def get_usage_summary(limit: int = 100) -> dict:
    """Backward-compatible summary: {claude: ..., ollama: ..., fetched_at: ...}.

    Still returns the per-provider dicts at the top level (the dashboard
    template and MCP tool read data.claude / data.ollama). The unified
    cross-provider roll-up is available via providers.get_unified_summary().
    """
    return {
        "claude": get_claude_usage(),
        "ollama": get_ollama_usage(),
        "fetched_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Provider adapter wrappers — wire existing functions into the registry.
# This lets providers.get_unified_summary() produce cross-provider roll-ups
# without duplicating any aggregation logic. New providers just need a new
# adapter class + register_adapter() call.
# ---------------------------------------------------------------------------

from . import providers as _providers  # noqa: E402


class ClaudeAdapter(_providers.ProviderAdapter):
    """Claude Max usage — aggregated from local Claude Code transcripts."""

    name = "claude"
    label = "Claude Max"

    def fetch(self) -> dict:
        return get_claude_usage()

    def invalidate_cache(self) -> None:
        invalidate_claude_cache()


class OllamaAdapter(_providers.ProviderAdapter):
    """Ollama Cloud usage — aggregated from the oll-usage.jsonl bridge log and Hermes state.db."""

    name = "ollama"
    label = "Ollama Cloud"

    def fetch(self) -> dict:
        return get_ollama_usage()

    def invalidate_cache(self) -> None:
        invalidate_ollama_cache()


# Register adapters on import (idempotent — registry is a dict keyed by name).
_providers.register_adapter(ClaudeAdapter())
_providers.register_adapter(OllamaAdapter())
