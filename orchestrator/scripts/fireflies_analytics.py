#!/usr/bin/env python3
"""
fireflies_analytics.py — Pull talk-listen ratio and behavioral metrics from Fireflies API.

Used by the Growth Orchestrator to track Ricardo's talk-listen ratio trend
toward the 45% target recommended in the lead-gen playbook (Cap. 6).

Metrics pulled:
  - Talk-listen ratio (% of sentences by Ricardo vs others)
  - Number of participants per meeting
  - Meeting duration
  - Sentence count (proxy for engagement)

Output: JSON to stdout, or --report for human-readable text.

Usage:
  python3 fireflies_analytics.py           # JSON to stdout
  python3 fireflies_analytics.py --report  # Human-readable
  python3 fireflies_analytics.py --limit 20 # Last 20 meetings (default: 10)
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

GRAPHQL_URL = "https://api.fireflies.ai/graphql"


def operator_aliases() -> set:
    """Speaker/participant aliases identifying the operator (the coached host).

    ORCHESTRATORMAXXING_OPERATOR_ALIASES: comma-separated, case-insensitive
    substring list, read at call time. Neutral default: "operator".
    """
    raw = os.environ.get("ORCHESTRATORMAXXING_OPERATOR_ALIASES", "operator")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}

# --- Behavioral-coaching targets (lead-gen playbook, Cap. 6) -----------------
TARGET_TALK_RATIO = 45       # talk ≤45% in discovery calls
TARGET_MONOLOGUE_SEC = 60    # no single monologue longer than 60s
TARGET_FILLERS = 3           # soft target: ≤3 fillers/meeting (real goal: trend ↓)

# Filler / hedge words to flag in Ricardo's speech. Multi-word phrases matched as
# substrings; single tokens matched on word boundaries so "like" doesn't fire
# inside "likely". English (from the brief) + the Spanish muletillas Ricardo uses.
FILLER_WORDS = (
    "you know", "i mean", "o sea", "osea", "digamos", "no sé", "no se",
    "um", "uh", "uhm", "erm", "hmm", "mmm", "like", "este", "eh", "pues",
)
_FILLER_PHRASES = tuple(w for w in FILLER_WORDS if " " in w)
_FILLER_TOKENS = tuple(w for w in FILLER_WORDS if " " not in w)
_FILLER_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _FILLER_TOKENS) + r")\b", re.IGNORECASE
) if _FILLER_TOKENS else None


def api_key() -> str:
    """Read the Fireflies API key from the environment at CALL time.

    Read fresh on every request (not captured once at import): the dashboard
    imports this module before its own env/.env is loaded, so a module-level
    constant would freeze an empty key and every query would 401.
    """
    return os.environ.get("FIREFLIES_API_KEY", "")


def query(q: str) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("FIREFLIES_API_KEY is not set")
    data = json.dumps({"query": q}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_transcripts(limit: int = 10) -> list:
    q = f"""
    query {{
      transcripts(limit: {limit}) {{
        title
        date
        duration
        participants
        sentences {{
          speaker_name
          text
          start_time
          end_time
        }}
      }}
    }}
    """
    result = query(q)
    return result.get("data", {}).get("transcripts", [])


def count_fillers(text: str) -> int:
    """Number of filler-word occurrences in a sentence (phrases + boundary tokens)."""
    if not text:
        return 0
    low = text.lower()
    n = sum(low.count(p) for p in _FILLER_PHRASES)
    if _FILLER_TOKEN_RE:
        n += len(_FILLER_TOKEN_RE.findall(low))
    return n


def longest_monologue_seconds(sentences: list, speaker_name) -> int:
    """Longest consecutive run (in seconds) spoken by `speaker_name`.

    A monologue = a maximal run of back-to-back sentences by the same speaker;
    its length is last.end_time − first.start_time. Sentences without timing
    contribute 0. Returns whole seconds. 0 if the speaker is unknown/absent.
    """
    if not speaker_name:
        return 0
    best = 0.0
    run_start = None
    run_end = None
    for s in sentences:
        if s.get("speaker_name") == speaker_name:
            st = s.get("start_time")
            et = s.get("end_time")
            if run_start is None and st is not None:
                run_start = st
            if et is not None:
                run_end = et
        else:  # speaker changed → close the run
            if run_start is not None and run_end is not None:
                best = max(best, run_end - run_start)
            run_start = run_end = None
    if run_start is not None and run_end is not None:  # trailing run
        best = max(best, run_end - run_start)
    return int(round(max(0.0, best)))


def analyze_meeting(transcript: dict) -> dict:
    """Analyze a single transcript for talk-listen ratio + behavioral metrics."""
    sentences = transcript.get("sentences", []) or []
    participants = transcript.get("participants", []) or []
    duration = transcript.get("duration", 0) or 0

    # Count sentences per speaker
    speaker_counts = {}
    for s in sentences:
        sp = s.get("speaker_name", "Unknown")
        speaker_counts[sp] = speaker_counts.get(sp, 0) + 1

    total = sum(speaker_counts.values()) or 1

    # Find the operator's speaker name (display names vary per platform).
    aliases = operator_aliases()
    ricardo_name = None
    for name in speaker_counts:
        if any(a in name.lower() for a in aliases):
            ricardo_name = name
            break

    # If we can't find Ricardo by name, use the email match
    if not ricardo_name and len(participants) == 1:
        # Solo meeting — probably Ricardo alone
        ricardo_name = list(speaker_counts.keys())[0] if speaker_counts else None

    ricardo_sentences = speaker_counts.get(ricardo_name, 0)
    talk_pct = round(ricardo_sentences / total * 100, 1) if total > 0 else 0

    # Behavioral metrics — scoped to Ricardo's own speech (the coaching target).
    filler_words = sum(
        count_fillers(s.get("text", "")) for s in sentences
        if s.get("speaker_name") == ricardo_name)
    longest_monologue = longest_monologue_seconds(sentences, ricardo_name)

    # Determine meeting type (heuristic)
    title = transcript.get("title", "").lower()
    others = [p for p in participants if not any(a in str(p).lower() for a in aliases)]
    if any(w in title for w in ["discovery", "demo", "call", "intro"]):
        meeting_type = "discovery"
    elif any(w in title for w in ["follow", "follow up", "sync", "check-in"]):
        meeting_type = "followup"
    elif len(others) == 0:
        meeting_type = "solo"
    else:
        meeting_type = "meeting"

    return {
        "title": transcript.get("title", ""),
        "date": datetime.fromtimestamp(transcript.get("date", 0) / 1000).strftime("%Y-%m-%d %H:%M"),
        "duration_min": round(duration),
        "participants": len(participants),
        "others": others,
        "total_sentences": len(sentences),
        "speaker_distribution": speaker_counts,
        "ricardo_sentences": ricardo_sentences,
        "talk_pct": talk_pct,
        "filler_words": filler_words,
        "longest_monologue_sec": longest_monologue,
        "target_pct": TARGET_TALK_RATIO,
        "meeting_type": meeting_type,
        "over_target": talk_pct > TARGET_TALK_RATIO,
    }


def summarize(analyses: list, recent: int = 5) -> dict:
    """Reduce per-meeting analyses to the behavioral-coaching summary the
    dashboard renders: avg talk ratio over the last `recent` non-solo meetings,
    the gap to the 45% target, and the trend direction.

    `analyses` is newest-first (Fireflies returns transcripts most-recent-first).
    Pure — no network — so it's directly unit-testable with fixtures.
    """
    non_solo = [a for a in analyses if a.get("meeting_type") != "solo"]
    window = non_solo[:recent]  # newest `recent` non-solo meetings

    if not window:
        return {
            "target_pct": TARGET_TALK_RATIO,
            "sample_size": 0,
            "avg_talk_pct": None,
            "gap_to_target": None,
            "on_target": None,
            "over_target_count": 0,
            "trend": "n/a",
            "trend_delta": None,
        }

    pcts = [a["talk_pct"] for a in window]
    avg = round(sum(pcts) / len(pcts), 1)

    # Trend toward target: compare the newest half of the window to the older
    # half. A DROP in talk% (delta < 0) means moving toward the ≤45% target →
    # "improving"; a rise means "worsening". Need ≥2 meetings to have a trend.
    trend, delta = "flat", 0.0
    if len(pcts) >= 2:
        half = len(pcts) // 2 or 1
        newer = pcts[:half]
        older = pcts[half:] or pcts[half - 1:half]
        delta = round(sum(newer) / len(newer) - sum(older) / len(older), 1)
        if delta < -1.0:
            trend = "improving"
        elif delta > 1.0:
            trend = "worsening"
    else:
        trend = "n/a"

    return {
        "target_pct": TARGET_TALK_RATIO,
        "sample_size": len(window),
        "avg_talk_pct": avg,
        "gap_to_target": round(avg - TARGET_TALK_RATIO, 1),
        "on_target": avg <= TARGET_TALK_RATIO,
        "over_target_count": sum(1 for p in pcts if p > TARGET_TALK_RATIO),
        "trend": trend,
        "trend_delta": delta,
    }


# --- Behavioral coaching (3-metric expansion) --------------------------------
# All three metrics are "lower is better" (talk%, fillers, monologue seconds), so
# one trend helper serves all: a DROP in the recent half vs the older half means
# "improving". Tips rotate by ISO-week so the same advice doesn't repeat forever.

_COACHING_TIPS = {
    "talk_pct": (
        "Haz una pregunta abierta y cuenta hasta 3 antes de retomar la palabra.",
        "Meta: escucha ~55%. Deja que el prospecto termine su idea sin interrumpir.",
        "Resume lo que dijo el prospecto antes de opinar — señal de escucha activa.",
    ),
    "fillers": (
        "Sustituye 'este / o sea' por una pausa breve. El silencio proyecta seguridad.",
        "Graba 2 min hablando de tu oferta y cuenta tus muletillas — consciencia = mejora.",
        "Baja el ritmo: las muletillas suben al improvisar. Prepara tus 3 mensajes clave.",
    ),
    "longest_monologue_sec": (
        "Regla de 60s: si llevas un minuto hablando, lanza un control ('¿te hace sentido?').",
        "Parte tu pitch en bloques de 30s con checkpoints — evita el monólogo.",
        "Cierra tus ideas con una pregunta, no con otra afirmación: cede el turno.",
    ),
    "on_target": (
        "Vas en meta en las 3 métricas. Mantén el hábito: pregunta, pausa, escucha.",
    ),
}


def _trend(values_newest_first: list) -> tuple:
    """(trend, delta) for a lower-is-better series. delta = newer_avg − older_avg;
    negative → improving (going down), positive → worsening. Needs ≥2 points."""
    v = values_newest_first
    if len(v) < 2:
        return "n/a", None
    half = len(v) // 2 or 1
    newer = v[:half]
    older = v[half:] or v[half - 1:half]
    delta = round(sum(newer) / len(newer) - sum(older) / len(older), 1)
    if delta < -1.0:
        return "improving", delta
    if delta > 1.0:
        return "worsening", delta
    return "flat", delta


def _metric(values_newest_first: list, target: float, lower_is_better=True) -> dict:
    """Per-metric block: avg / latest / trend / on_target + oldest→newest series."""
    if not values_newest_first:
        return {"avg": None, "latest": None, "trend": "n/a", "delta": None,
                "on_target": None, "series": []}
    avg = round(sum(values_newest_first) / len(values_newest_first), 1)
    trend, delta = _trend(values_newest_first)
    on_target = avg <= target if lower_is_better else avg >= target
    return {
        "avg": avg,
        "latest": values_newest_first[0],
        "trend": trend,
        "delta": delta,
        "on_target": on_target,
        "series": list(reversed(values_newest_first)),  # oldest→newest for sparkline
    }


def coaching_summary(analyses: list, recent: int = 10, week_index: int = 0) -> dict:
    """Behavioral-coaching read model over the last `recent` non-solo meetings:
    talk%, fillers, and longest monologue — each with avg / trend / target / a
    sparkline series — plus one rotating tip aimed at the most off-target metric.

    Pure (no network); newest-first input; directly unit-testable with fixtures.
    """
    non_solo = [a for a in analyses if a.get("meeting_type") != "solo"]
    window = non_solo[:recent]
    targets = {
        "talk_pct": TARGET_TALK_RATIO,
        "fillers": "down",              # goal is a downward trend, not an absolute
        "monologue_sec": TARGET_MONOLOGUE_SEC,
    }

    if not window:
        return {
            "sample_size": 0,
            "targets": targets,
            "metrics": {
                "talk_pct": _metric([], TARGET_TALK_RATIO),
                "fillers": _metric([], TARGET_FILLERS),
                "longest_monologue_sec": _metric([], TARGET_MONOLOGUE_SEC),
            },
            "tip": {"metric": None, "text": "Aún no hay reuniones (no-solo) para analizar."},
        }

    talk = [a.get("talk_pct", 0) for a in window]
    fillers = [a.get("filler_words", 0) for a in window]
    monolog = [a.get("longest_monologue_sec", 0) for a in window]

    metrics = {
        "talk_pct": _metric(talk, TARGET_TALK_RATIO),
        "fillers": _metric(fillers, TARGET_FILLERS),
        "longest_monologue_sec": _metric(monolog, TARGET_MONOLOGUE_SEC),
    }

    # Which metric is most off-target? Normalized overage (0 = on/under target).
    # Fillers use the soft target only for ranking (displayed goal stays "down").
    def _overage(avg, target):
        if avg is None or target <= 0:
            return 0.0
        return max(0.0, (avg - target) / target)
    scores = {
        "talk_pct": _overage(metrics["talk_pct"]["avg"], TARGET_TALK_RATIO),
        "fillers": _overage(metrics["fillers"]["avg"], TARGET_FILLERS),
        "longest_monologue_sec": _overage(
            metrics["longest_monologue_sec"]["avg"], TARGET_MONOLOGUE_SEC),
    }
    worst = max(scores, key=scores.get)
    tip_key = worst if scores[worst] > 0 else "on_target"
    tips = _COACHING_TIPS[tip_key]
    tip_text = tips[week_index % len(tips)]

    return {
        "sample_size": len(window),
        "targets": targets,
        "metrics": metrics,
        "tip": {"metric": tip_key if tip_key != "on_target" else None, "text": tip_text},
    }


def main():
    limit = 10
    report_mode = False
    if "--report" in sys.argv:
        report_mode = True
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])

    transcripts = get_transcripts(limit)
    analyses = [analyze_meeting(t) for t in transcripts]

    if report_mode:
        print(f"=== Fireflies Behavioral Analytics ===")
        print(f"Last {len(analyses)} meetings\n")
        for a in analyses:
            indicator = "🔴" if a["over_target"] else "🟢"
            others_str = ", ".join(a["others"][:2]) or "(solo)"
            print(f'{a["date"]} | {a["duration_min"]}min | {a["meeting_type"]}')
            print(f'  {indicator} Talk: {a["talk_pct"]}% (target ≤{a["target_pct"]}%) | {a["total_sentences"]} sentences | with: {others_str}')
            print()

        # Summary stats
        talk_pcts = [a["talk_pct"] for a in analyses if a["meeting_type"] != "solo"]
        if talk_pcts:
            avg = round(sum(talk_pcts) / len(talk_pcts), 1)
            over = sum(1 for p in talk_pcts if p > TARGET_TALK_RATIO)
            print(f"--- Summary (excl. solo) ---")
            print(f"  Avg talk ratio: {avg}%")
            print(f"  Over target: {over}/{len(talk_pcts)} meetings")
            print(f"  Target: ≤{TARGET_TALK_RATIO}%")
            print(f"  Trend: {'⚠️ Need to listen more' if avg > TARGET_TALK_RATIO else '✅ On target'}")
    else:
        print(json.dumps(analyses, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()