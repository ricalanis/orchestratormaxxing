"""Fireflies behavioral analytics — regression guard.

Covers the three pieces of the integration:
  1. scripts/fireflies_analytics.py  — call-time API-key read + pure summarize()
  2. dashboard/fireflies.py          — fail-soft read model (analytics())
  3. GET /api/growth/fireflies-analytics — endpoint never 500s without a key

The script + glue tests need NO database (fireflies.py has no import side
effects). The endpoint test reuses the CRM-growth pattern: point the DB layer at
a COPY of kanban.db before importing dashboard.api, else skip.

Run:  python -m pytest tests/test_fireflies.py -v
      python -m unittest tests.test_fireflies
"""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- load the standalone script + the dashboard glue (no DB needed) ----------
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fireflies_analytics.py"
_spec = importlib.util.spec_from_file_location("fireflies_analytics", _SCRIPT)
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)

from dashboard import fireflies as ff  # no side effects


# ----------------------------------------------------------- fixtures / helpers
def _analysis(talk_pct, mtype="discovery"):
    """A minimal per-meeting analysis dict (only the keys summarize() reads)."""
    return {"meeting_type": mtype, "talk_pct": talk_pct}


def _transcript(title, host, others, participants):
    """A Fireflies-shaped transcript: `host`/`others` = sentence counts."""
    sents = [{"speaker_name": "Op Host"} for _ in range(host)]
    sents += [{"speaker_name": "Prospect Perez"} for _ in range(others)]
    return {
        "title": title,
        "date": 1_700_000_000_000,  # ms epoch
        "duration": 30,
        "participants": participants,
        "sentences": sents,
    }


def _coaching_analysis(talk_pct, fillers, monologue, mtype="discovery"):
    """A per-meeting analysis dict with the 3 coaching metrics."""
    return {"meeting_type": mtype, "talk_pct": talk_pct,
            "filler_words": fillers, "longest_monologue_sec": monologue}


class ApiKeyRead(unittest.TestCase):
    """The bug fix: the key is read from FIREFLIES_API_KEY at CALL time."""

    def setUp(self):
        self._saved = os.environ.pop("FIREFLIES_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["FIREFLIES_API_KEY"] = self._saved
        else:
            os.environ.pop("FIREFLIES_API_KEY", None)

    def test_reads_from_environ_at_call_time(self):
        self.assertEqual(fa.api_key(), "")            # unset → empty
        os.environ["FIREFLIES_API_KEY"] = "ff-123"
        self.assertEqual(fa.api_key(), "ff-123")      # picked up without re-import

    def test_query_raises_when_key_missing(self):
        with self.assertRaises(RuntimeError):
            fa.query("query { transcripts { title } }")


class Summarize(unittest.TestCase):
    """Pure summary logic — avg over last 5 non-solo, gap, trend."""

    def test_excludes_solo_and_averages_window(self):
        analyses = [_analysis(60), _analysis(40), _analysis(90, "solo"), _analysis(50)]
        s = fa.summarize(analyses)
        self.assertEqual(s["sample_size"], 3)          # solo dropped
        self.assertEqual(s["avg_talk_pct"], 50.0)      # (60+40+50)/3
        self.assertEqual(s["target_pct"], 45)
        self.assertEqual(s["gap_to_target"], 5.0)      # 50 - 45
        self.assertFalse(s["on_target"])
        self.assertEqual(s["over_target_count"], 2)    # 60 and 50 > 45

    def test_window_caps_at_recent_five(self):
        analyses = [_analysis(p) for p in (10, 20, 30, 40, 50, 99)]
        s = fa.summarize(analyses)                      # newest-first → drops the 99
        self.assertEqual(s["sample_size"], 5)
        self.assertEqual(s["avg_talk_pct"], 30.0)       # (10+20+30+40+50)/5

    def test_on_target(self):
        s = fa.summarize([_analysis(30), _analysis(40)])
        self.assertTrue(s["on_target"])
        self.assertLessEqual(s["gap_to_target"], 0)

    def test_trend_improving_when_recent_lower(self):
        # newest-first: recent talk% below older → moving toward target
        s = fa.summarize([_analysis(30), _analysis(32), _analysis(55), _analysis(58)])
        self.assertEqual(s["trend"], "improving")
        self.assertLess(s["trend_delta"], 0)

    def test_trend_worsening_when_recent_higher(self):
        s = fa.summarize([_analysis(58), _analysis(55), _analysis(32), _analysis(30)])
        self.assertEqual(s["trend"], "worsening")
        self.assertGreater(s["trend_delta"], 0)

    def test_empty_is_safe(self):
        s = fa.summarize([])
        self.assertEqual(s["sample_size"], 0)
        self.assertIsNone(s["avg_talk_pct"])
        self.assertEqual(s["trend"], "n/a")


class AnalyticsReadModel(unittest.TestCase):
    """dashboard/fireflies.py analytics() — fail-soft, never raises.

    fireflies.py is self-contained now (own GraphQL client, no loaded script):
    patch its `_api_key` + `fetch_transcripts` module attributes. Patching the
    env alone is NOT hermetic — `_api_key()` falls back to ~/.hermes/.env,
    which may hold a real key on a dev machine."""

    def setUp(self):
        self._orig_key = ff._api_key
        self._orig_fetch = ff.fetch_transcripts
        ff._api_key = lambda: None
        self._prev_aliases = os.environ.get("ORCHESTRATORMAXXING_OPERATOR_ALIASES")
        os.environ["ORCHESTRATORMAXXING_OPERATOR_ALIASES"] = "op host, operator"

    def tearDown(self):
        ff._api_key = self._orig_key
        ff.fetch_transcripts = self._orig_fetch
        if self._prev_aliases is None:
            os.environ.pop("ORCHESTRATORMAXXING_OPERATOR_ALIASES", None)
        else:
            os.environ["ORCHESTRATORMAXXING_OPERATOR_ALIASES"] = self._prev_aliases

    def test_no_key_is_unavailable_not_error(self):
        out = ff.analytics(limit=10)
        self.assertFalse(out["available"])
        self.assertEqual(out["reason"], "no_api_key")
        self.assertEqual(out["meetings"], [])
        self.assertEqual(out["summary"]["sample_size"], 0)
        self.assertEqual(out["target_pct"], 45)

    def test_fetch_error_is_unavailable_not_error(self):
        ff._api_key = lambda: "ff-123"
        def _boom(limit):
            raise RuntimeError("network down")
        ff.fetch_transcripts = _boom
        out = ff.analytics(limit=5)
        self.assertFalse(out["available"])
        self.assertTrue(out["reason"].startswith("fetch_error"))

    def test_happy_path_summarizes(self):
        ff._api_key = lambda: "ff-123"
        transcripts = [
            _transcript("Discovery call — Acme", host=8, others=2,
                        participants=["operator@example.com", "cto@acme.com"]),
            _transcript("Discovery call — Globex", host=3, others=7,
                        participants=["operator@example.com", "ceo@globex.com"]),
            _transcript("Focus block", host=5, others=0,
                        participants=["operator@example.com"]),
        ]
        ff.fetch_transcripts = lambda limit: transcripts
        out = ff.analytics(limit=10)
        self.assertTrue(out["available"])
        self.assertEqual(len(out["meetings"]), 3)
        # 80% and 30% are non-solo; the focus block is solo → excluded.
        self.assertEqual(out["summary"]["sample_size"], 2)
        self.assertEqual(out["summary"]["avg_talk_pct"], 55.0)  # (80+30)/2
        types = {m["meeting_type"] for m in out["meetings"]}
        self.assertIn("solo", types)
        self.assertIn("discovery", types)

    def test_limit_is_clamped(self):
        out = ff.analytics(limit=9999)          # no key → still returns, clamped
        self.assertEqual(out["limit"], 50)
        out = ff.analytics(limit=0)
        self.assertEqual(out["limit"], 10)      # falsy → default


class Fillers(unittest.TestCase):
    """count_fillers — phrases + word-boundary tokens, no false positives."""

    def test_counts_phrases_and_tokens(self):
        # um, o sea, like, you know, este, pues = 6
        self.assertEqual(fa.count_fillers("Um, o sea, like this is, you know, este pues"), 6)

    def test_word_boundaries_no_false_positive(self):
        # "likely"/"liked" must not match "like"; "eshtes" must not match "este".
        self.assertEqual(fa.count_fillers("likely liked eshtes"), 0)

    def test_empty_and_none(self):
        self.assertEqual(fa.count_fillers(""), 0)
        self.assertEqual(fa.count_fillers(None), 0)


class Monologue(unittest.TestCase):
    """longest_monologue_seconds — longest consecutive run by one speaker."""

    def _sents(self):
        return [
            {"speaker_name": "Op Host", "start_time": 0, "end_time": 10},
            {"speaker_name": "Op Host", "start_time": 10, "end_time": 30},
            {"speaker_name": "Prospect Perez", "start_time": 30, "end_time": 35},
            {"speaker_name": "Op Host", "start_time": 40, "end_time": 160},
        ]

    def test_longest_run(self):
        # runs: 0–30 (30s) and 40–160 (120s) → 120
        self.assertEqual(fa.longest_monologue_seconds(self._sents(), "Op Host"), 120)

    def test_other_speaker(self):
        self.assertEqual(fa.longest_monologue_seconds(self._sents(), "Prospect Perez"), 5)

    def test_unknown_speaker_zero(self):
        self.assertEqual(fa.longest_monologue_seconds(self._sents(), None), 0)

    def test_missing_timing_is_zero(self):
        sents = [{"speaker_name": "Op Host"}, {"speaker_name": "Op Host"}]
        self.assertEqual(fa.longest_monologue_seconds(sents, "Op Host"), 0)


class AnalyzeMeetingMetrics(unittest.TestCase):
    """analyze_meeting adds filler_words + longest_monologue_sec (host-scoped)."""

    def setUp(self):
        self._prev_aliases = os.environ.get("ORCHESTRATORMAXXING_OPERATOR_ALIASES")
        os.environ["ORCHESTRATORMAXXING_OPERATOR_ALIASES"] = "op host, operator"

    def tearDown(self):
        if self._prev_aliases is None:
            os.environ.pop("ORCHESTRATORMAXXING_OPERATOR_ALIASES", None)
        else:
            os.environ["ORCHESTRATORMAXXING_OPERATOR_ALIASES"] = self._prev_aliases

    def test_metrics_present(self):
        t = {
            "title": "Discovery call — Acme", "date": 1_700_000_000_000, "duration": 30,
            "participants": ["operator@example.com", "cto@acme.com"],
            "sentences": [
                {"speaker_name": "Op Host", "start_time": 0, "end_time": 90,
                 "text": "um o sea this is este the plan"},          # 3 fillers, 90s
                {"speaker_name": "Prospect Perez", "start_time": 90, "end_time": 95,
                 "text": "um sounds good"},                          # prospect fillers ignored
            ],
        }
        a = fa.analyze_meeting(t)
        self.assertEqual(a["filler_words"], 3)                 # only the host's counted
        self.assertEqual(a["longest_monologue_sec"], 90)
        self.assertEqual(a["talk_pct"], 50.0)

    def test_back_compat_without_new_fields(self):
        # Old-shaped sentences (speaker_name only) → metrics degrade to 0.
        t = _transcript("Discovery", host=6, others=4,
                        participants=["operator@example.com", "x@y.com"])
        a = fa.analyze_meeting(t)
        self.assertEqual(a["filler_words"], 0)
        self.assertEqual(a["longest_monologue_sec"], 0)


class CoachingSummary(unittest.TestCase):
    """coaching_summary — 3 metrics with trend/target + most-off-target tip."""

    def test_shape_and_averages(self):
        ms = [
            _coaching_analysis(70, 8, 120),
            _coaching_analysis(60, 10, 90),
            _coaching_analysis(100, 0, 300, "solo"),   # solo excluded
        ]
        s = fa.coaching_summary(ms, week_index=0)
        self.assertEqual(s["sample_size"], 2)
        self.assertEqual(s["metrics"]["talk_pct"]["avg"], 65.0)
        self.assertEqual(s["metrics"]["fillers"]["avg"], 9.0)
        self.assertEqual(s["metrics"]["longest_monologue_sec"]["avg"], 105.0)
        # targets exposed for the UI
        self.assertEqual(s["targets"]["talk_pct"], 45)
        self.assertEqual(s["targets"]["monologue_sec"], 60)
        self.assertEqual(s["targets"]["fillers"], "down")

    def test_on_target_flags(self):
        s = fa.coaching_summary([_coaching_analysis(40, 1, 30),
                                 _coaching_analysis(42, 2, 45)])
        self.assertTrue(s["metrics"]["talk_pct"]["on_target"])
        self.assertTrue(s["metrics"]["longest_monologue_sec"]["on_target"])
        # all on target → generic "keep it up" tip (no specific metric)
        self.assertIsNone(s["tip"]["metric"])

    def test_series_is_oldest_to_newest(self):
        # newest-first input [70, 50] → series should read [50, 70]
        s = fa.coaching_summary([_coaching_analysis(70, 5, 50),
                                 _coaching_analysis(50, 3, 40)])
        self.assertEqual(s["metrics"]["talk_pct"]["series"], [50, 70])

    def test_tip_targets_most_off_metric(self):
        # talk wildly over (90 vs 45 → overage 1.0) beats fillers (5 vs 3 → 0.67)
        s = fa.coaching_summary([_coaching_analysis(90, 5, 40),
                                 _coaching_analysis(88, 5, 40)], week_index=0)
        self.assertEqual(s["tip"]["metric"], "talk_pct")

    def test_tip_rotates_by_week(self):
        ms = [_coaching_analysis(90, 5, 40), _coaching_analysis(88, 5, 40)]
        t0 = fa.coaching_summary(ms, week_index=0)["tip"]["text"]
        t1 = fa.coaching_summary(ms, week_index=1)["tip"]["text"]
        self.assertNotEqual(t0, t1)   # different weeks → different tip

    def test_empty_is_safe(self):
        s = fa.coaching_summary([])
        self.assertEqual(s["sample_size"], 0)
        self.assertIsNone(s["metrics"]["talk_pct"]["avg"])
        self.assertIn("targets", s)


class CoachingReadModel(unittest.TestCase):
    """dashboard/fireflies.py coaching() — fail-soft, never raises.

    Same hermetic patching as AnalyticsReadModel (see its docstring)."""

    def setUp(self):
        self._orig_key = ff._api_key
        self._orig_fetch = ff.fetch_transcripts
        ff._api_key = lambda: None
        self._prev_aliases = os.environ.get("ORCHESTRATORMAXXING_OPERATOR_ALIASES")
        os.environ["ORCHESTRATORMAXXING_OPERATOR_ALIASES"] = "op host, operator"

    def tearDown(self):
        ff._api_key = self._orig_key
        ff.fetch_transcripts = self._orig_fetch
        if self._prev_aliases is None:
            os.environ.pop("ORCHESTRATORMAXXING_OPERATOR_ALIASES", None)
        else:
            os.environ["ORCHESTRATORMAXXING_OPERATOR_ALIASES"] = self._prev_aliases

    def test_no_key_is_unavailable(self):
        out = ff.coaching(limit=10)
        self.assertFalse(out["available"])
        self.assertEqual(out["reason"], "no_api_key")
        self.assertEqual(out["meetings"], [])
        self.assertEqual(out["sample_size"], 0)
        self.assertIn("targets", out)

    def test_fetch_error_is_unavailable(self):
        ff._api_key = lambda: "ff-123"
        def _boom(limit):
            raise RuntimeError("network down")
        ff.fetch_transcripts = _boom
        out = ff.coaching(limit=5)
        self.assertFalse(out["available"])
        self.assertTrue(out["reason"].startswith("fetch_error"))

    def test_happy_path(self):
        ff._api_key = lambda: "ff-123"
        t = {
            "title": "Discovery call — Acme", "date": 1_700_000_000_000, "duration": 30,
            "participants": ["operator@example.com", "cto@acme.com"],
            "sentences": [
                {"speaker_name": "Op Host", "text": "um este o sea the plan"},
                {"speaker_name": "Op Host", "text": "so here is the demo"},
                {"speaker_name": "Prospect Perez", "text": "ok"},
            ],
        }
        ff.fetch_transcripts = lambda limit: [t]
        out = ff.coaching(limit=10)
        self.assertTrue(out["available"])
        self.assertEqual(len(out["meetings"]), 1)
        self.assertEqual(out["sample_size"], 1)
        self.assertEqual(out["meetings"][0]["filler_words"], 3)
        # Fireflies sentences no longer carry timestamps — the monologue metric
        # is now a consecutive-sentence-count proxy (2 host sentences in a row).
        self.assertEqual(out["meetings"][0]["longest_monologue_sec"], 2)
        self.assertIn("tip", out)


# --------------------------------------------------------------- endpoint (DB)
# The fireflies endpoint never touches the DB, but importing dashboard.api runs
# ensure_schema() as a side effect — so point the DB layer at a throwaway COPY
# just long enough to import safely (never the real kanban.db), then RESTORE the
# global. Leaving KANBAN_DB pointed at our private copy would hijack the shared
# `_db.KANBAN_DB` for every other test module in the run (ensure_schema/migrations
# only ran once, on whichever copy imported first) — so we put it back as found.
_READY = False
_CLIENT = None
_TMP_DB = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_ff_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)

        _orig_kdb = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard.api import app
            from starlette.testclient import TestClient
            _CLIENT = TestClient(app, raise_server_exceptions=False)
            _READY = True
        finally:
            _db.KANBAN_DB = _orig_kdb  # good citizen: don't leak our copy
except Exception:  # pragma: no cover
    _READY = False


@atexit.register
def _cleanup_tmp_db():  # pragma: no cover
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class Endpoint(unittest.TestCase):

    def setUp(self):
        # dashboard.api calls the same module object as `ff`; patching _api_key
        # (not just the env — ~/.hermes/.env may hold a real key) keeps these
        # endpoint tests offline and deterministic.
        self._orig_key = ff._api_key
        ff._api_key = lambda: None

    def tearDown(self):
        ff._api_key = self._orig_key

    def test_endpoint_never_500s_without_key(self):
        r = _CLIENT.get("/api/growth/fireflies-analytics")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["reason"], "no_api_key")
        self.assertIn("summary", body)
        self.assertEqual(body["target_pct"], 45)

    def test_endpoint_accepts_limit(self):
        r = _CLIENT.get("/api/growth/fireflies-analytics?limit=3")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 3)

    def test_coaching_endpoint_never_500s_without_key(self):
        r = _CLIENT.get("/api/growth/behavioral-coaching")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["reason"], "no_api_key")
        self.assertIn("targets", body)
        self.assertEqual(body["sample_size"], 0)

    def test_coaching_endpoint_accepts_limit(self):
        r = _CLIENT.get("/api/growth/behavioral-coaching?limit=5")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 5)


if __name__ == "__main__":
    unittest.main()
