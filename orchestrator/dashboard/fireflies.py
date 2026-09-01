"""
Fireflies GraphQL client + signal extractor for the CRM Lead Scoring integration.

This module is intentionally self-contained: it reads the API key from the
environment or ~/.hermes/.env, queries Fireflies' GraphQL endpoint, extracts
meeting signals (talk-listen ratio, questions, filler words, action items,
sentiment, topics), and caches results in the DB via dashboard.db helpers.

Doctrine (same as growth.py): additive only, one read path, fail-soft when
Fireflies is not configured so the dashboard never 500s.
"""
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

try:
    import requests
except Exception:  # pragma: no cover
    requests = None
from datetime import datetime

FIREFLIES_ENDPOINT = "https://api.fireflies.ai/graphql"
def _operator_aliases() -> set:
    """Speaker-name aliases that identify the operator (the coached host).

    Read at CALL time, same rule as _api_key(): the dashboard loads its env
    after this module is imported. ORCHESTRATORMAXXING_OPERATOR_ALIASES is a
    comma-separated, case-insensitive substring list; neutral default:
    "operator".
    """
    raw = os.environ.get("ORCHESTRATORMAXXING_OPERATOR_ALIASES", "operator")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}

_FILLER_WORDS = (
    "um", "uh", "o sea", "entonces", "like", "you know", "i mean",
    "pues", "este", "eh", "mmm", "hmm",
)

_POSITIVE_MARKERS = (
    "interested", "budget", "timeline", "next steps", "move forward",
    "go ahead", "proceed", "confirm", "commit", "buy", "purchase",
    "approve", "sí", "yes", "definitely", "absolutely", "prioridad",
)
_NEGATIVE_MARKERS = (
    "not interested", "no budget", "not now", "too expensive", "delay",
    "cancel", "postpone", "not a priority", "no tenemos", "todavía no",
)


def _api_key() -> Optional[str]:
    """Read FIREFLIES_API_KEY from env or ~/.hermes/.env manually.
    Per skill pitfall: dotenv can silently truncate this key."""
    key = os.environ.get("FIREFLIES_API_KEY", "").strip()
    if key:
        return key
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("FIREFLIES_API_KEY="):
                    return line.strip().split("=", 1)[1].strip()
    except Exception:
        return None
    return None


def _graphql(query: str, variables: Optional[dict] = None) -> dict:
    """POST a GraphQL query to Fireflies. Raises on network/auth errors."""
    if requests is None:
        raise RuntimeError("requests library not available")
    key = _api_key()
    if not key:
        raise RuntimeError("no_api_key")
    resp = requests.post(
        FIREFLIES_ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("errors"):
        errs = json.dumps(data["errors"])
        raise RuntimeError(f"graphql_errors: {errs[:200]}")
    return data.get("data", {})


_RICH_FIELDS = """
        id
        title
        date
        participants
        summary { overview action_items }
        sentences { index speaker_name text start_time end_time }
"""


def fetch_transcripts_rich(limit: int = 25, skip: int = 0, from_date=None) -> list:
    """Transcripts con oraciones ancladas — la forma que la digestión consume.

    Separada de `fetch_transcripts` a propósito: esa alimenta lead scoring y
    coaching, y meterle `sentences` con tiempos encarecería cada una de esas
    llamadas sin que nadie use el campo. `skip`/`fromDate` existen para que el
    poll pagine hasta su watermark en vez de re-leer todo.
    """
    query = """
    query TranscriptsRich($limit: Int, $skip: Int, $fromDate: DateTime) {
      transcripts(limit: $limit, skip: $skip, fromDate: $fromDate) {
""" + _RICH_FIELDS + """      }
    }
    """
    variables = {"limit": limit, "skip": skip}
    if from_date:
        variables["fromDate"] = from_date
    return _graphql(query, variables).get("transcripts") or []


def fetch_transcript_rich(transcript_id: str) -> Optional[dict]:
    """Un transcript por id — la ruta del webhook, que solo recibe `meeting_id`."""
    query = """
    query TranscriptRich($id: String!) {
      transcript(id: $id) {
""" + _RICH_FIELDS + """      }
    }
    """
    return _graphql(query, {"id": transcript_id}).get("transcript")


def webhook_secret() -> Optional[str]:
    """El secreto HMAC del webhook. Mismo patrón que `_api_key()` — env primero,
    luego `~/.hermes/.env`, que es el almacén de credenciales establecido."""
    key = os.environ.get("FIREFLIES_WEBHOOK_SECRET", "").strip()
    if key:
        return key
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("FIREFLIES_WEBHOOK_SECRET="):
                    return line.strip().split("=", 1)[1].strip() or None
    except Exception:
        return None
    return None


def _is_operator(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    return any(r in n for r in _operator_aliases())


def _parse_date(raw) -> Optional[str]:
    """Best-effort ISO date extraction from Fireflies date field.

    Fireflies returns `date` as epoch milliseconds. Convert to YYYY-MM-DD.
    """
    if not raw:
        return None
    try:
        ms = int(float(raw))
        return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    s = str(raw)
    if "T" in s:
        return s.split("T")[0]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return None


def _extract_sentences(transcript: dict) -> list[dict]:
    """Normalize sentences from various Fireflies response shapes."""
    sentences = transcript.get("sentences") or []
    if not isinstance(sentences, list):
        return []
    out = []
    for s in sentences:
        if not isinstance(s, dict):
            continue
        speaker = s.get("speaker_name") or s.get("speakerName") or ""
        text = s.get("text") or ""
        out.append({"speaker_name": speaker, "text": text})
    return out


def extract_signals(transcript: dict) -> dict:
    """Convert a Fireflies transcript dict into scoring signals."""
    sentences = _extract_sentences(transcript)
    total_sentences = max(len(sentences), 1)
    prospect_texts = [s["text"] for s in sentences if not _is_operator(s["speaker_name"])]
    prospect_sentences = len(prospect_texts)
    prospect_words = " ".join(prospect_texts).lower().split()
    total_words = max(len(prospect_words), 1)

    talk_ratio = round(prospect_sentences / total_sentences, 4)
    questions = sum(1 for t in prospect_texts if "?" in t)

    filler_count = sum(prospect_words.count(w) for w in _FILLER_WORDS)
    filler_density = round(filler_count / total_words, 4)

    summary = transcript.get("summary") or {}
    action_items = summary.get("action_items") or summary.get("actionItems") or []
    if isinstance(action_items, str):
        action_items = [action_items]
    action_count = len(action_items)

    overview = (summary.get("overview") or "").lower()
    keywords = summary.get("keywords") or summary.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    key_text = " ".join(str(k).lower() for k in keywords)
    combined = overview + " " + key_text
    positive = sum(1 for m in _POSITIVE_MARKERS if m in combined)
    negative = sum(1 for m in _NEGATIVE_MARKERS if m in combined)
    sentiment = "positive" if positive > negative else ("negative" if negative > positive else "neutral")

    topics = summary.get("topics") or []
    if not isinstance(topics, list):
        topics = []
    topics = [str(t) for t in topics[:5]]

    return {
        "talk_ratio": talk_ratio,
        "questions": questions,
        "filler_density": filler_density,
        "action_items": action_count,
        "sentiment": sentiment,
        "topics": topics,
        "prospect_duration": prospect_sentences,
        "total_duration": total_sentences,
        "prospect_words": total_words,
    }


MAX_TRANSCRIPT_WINDOW = 50


def fetch_transcripts(limit: int = 25, after_date: Optional[str] = None) -> list[dict]:
    """Fetch recent Fireflies transcripts using the reliable list query."""
    query = """
    query Transcripts($limit: Int) {
      transcripts(limit: $limit) {
        id
        title
        date
        participants
        summary { overview action_items keywords }
        sentences { speaker_name text }
      }
    }
    """
    # 50 is the API's real ceiling, measured 2026-08-10 against the live
    # endpoint: limit=50 returns 50 transcripts, limit=60 and limit=100 are
    # rejected with `invalid_arguments`. The old clamp of 100 let a caller ask
    # for a window the API refuses, turning a widened backfill into an error
    # instead of the widest legal look.
    data = _graphql(query, {"limit": max(1, min(int(limit), MAX_TRANSCRIPT_WINDOW))})
    transcripts = data.get("transcripts") or []
    if after_date:
        transcripts = [t for t in transcripts if (_parse_date(t.get("date")) or "9999-99-99") >= after_date]
    return transcripts


def latest_signals() -> dict:
    """Fail-soft read model for dashboard: latest meeting signals or no_api_key."""
    if not _api_key():
        return {"available": False, "reason": "no_api_key"}
    try:
        transcripts = fetch_transcripts(limit=10)
        meetings = []
        for t in transcripts:
            signals = extract_signals(t)
            meetings.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "date": _parse_date(t.get("date")),
                "signals": signals,
            })
        return {"available": True, "meetings": meetings}
    except Exception as e:
        return {"available": False, "reason": f"fetch_error: {e}"}


def _participant_email(p) -> str:
    """Participants are plain strings in the current Fireflies schema."""
    return str(p or "").lower().strip()


def _resolve_deal_match_keys(deal_id: str) -> dict:
    """Resolve participant-match keys from the CRM lineage for one deal."""
    from . import db

    conn = db.get_conn()
    try:
        deal = conn.execute(
            "SELECT d.account_id, c.email AS contact_email, "
            "a.domain AS account_domain "
            "FROM deals d "
            "LEFT JOIN contacts c ON c.id = d.contact_id "
            "LEFT JOIN accounts a ON a.id = d.account_id "
            "WHERE d.id = ?",
            (deal_id,),
        ).fetchone()
        if not deal:
            return {"emails": set(), "account_domain": ""}

        emails = set()
        contact_email = _participant_email(deal["contact_email"])
        if contact_email:
            emails.add(contact_email)
        if deal["account_id"]:
            account_contacts = conn.execute(
                "SELECT email FROM contacts WHERE account_id = ?",
                (deal["account_id"],),
            ).fetchall()
            emails.update(
                email
                for row in account_contacts
                if (email := _participant_email(row["email"]))
            )

        return {
            "emails": emails,
            "account_domain": _participant_email(deal["account_domain"]),
        }
    finally:
        conn.close()


def _match_deal(transcript: dict, match_keys: dict) -> bool:
    """Match participant email/domain against keys resolved from CRM lineage."""
    participants = transcript.get("participants") or []
    emails = [_participant_email(p) for p in participants if isinstance(p, str)]
    if not emails:
        return False
    resolved_emails = match_keys.get("emails") or set()
    account_domain = _participant_email(match_keys.get("account_domain"))
    if any(email in resolved_emails for email in emails):
        return True
    if account_domain and any(
        email.rpartition("@")[0] and email.rpartition("@")[2] == account_domain
        for email in emails
    ):
        return True
    return False


def _meeting_id(deal_id: str = "", transcript_id: str = "") -> str:
    """Derive the row id from the natural key so a re-fetch REFRESHES.

    This used to mint a random uuid, which made `fireflies_meeting_insert`'s
    INSERT OR REPLACE a no-op: the conflict target is the primary key, so every
    fetch wrote a new row for the same meeting (measured 2026-08-10 — the first
    full backfill doubled the cache). The pair is the natural key; two deals
    may legitimately share one transcript, so transcript_id alone would be
    wrong. m30 enforces the same key with a UNIQUE index.
    """
    if deal_id and transcript_id:
        digest = hashlib.sha1(f"{deal_id}:{transcript_id}".encode()).hexdigest()
        return "ffm_" + digest[:12]
    return "ffm_" + uuid.uuid4().hex[:12]


def fetch_and_store_for_deal(deal_id: str, deal: Optional[dict] = None,
                             limit: int = 25, since: Optional[str] = None) -> dict:
    """Fetch Fireflies transcripts, match to deal, store in DB, return summary.

    The window is the caller's (2026-08-10): this used to hardcode 25 and drop
    whatever the caller asked for, so a deal whose meetings sat outside the 25
    most recent transcripts reported `stored: 0` — the same reading as "this
    deal has no meetings". With ~62 meetings a month that window is ~12 days:
    right for the weekly cadence, useless for backfill. `limit` widens it and
    `since` (ISO date) floors it.

    The result also reports `scanned` — how many transcripts were actually
    examined — because a zero you cannot falsify is a claim, not a
    measurement: 0-of-40-scanned is a real absence, 0-of-0 is a blind look.
    """
    from . import db
    db.ensure_fireflies_schema()
    if not _api_key():
        return {"status": "no_api_key"}
    if deal is None:
        from . import crm
        deal = crm.get_deal(deal_id)
    if not deal:
        return {"status": "error", "error": "deal not found"}
    try:
        transcripts = fetch_transcripts(limit=limit, after_date=since)
    except Exception as e:
        return {"status": "error", "error": str(e)}
    match_keys = _resolve_deal_match_keys(deal_id)
    stored = 0
    now = int(time.time())
    for t in transcripts:
        if not _match_deal(t, match_keys):
            continue
        signals = extract_signals(t)
        db.fireflies_meeting_insert({
            "id": _meeting_id(deal_id, t.get("id") or ""),
            "deal_id": deal_id,
            "transcript_id": t.get("id"),
            "title": t.get("title"),
            "meeting_date": _parse_date(t.get("date")),
            "duration_seconds": int(signals.get("total_duration") or 0),
            "signals": signals,
            "raw_summary": json.dumps(t.get("summary") or {}),
            "fetched_at": now,
            "created_at": now,
        })
        stored += 1
    return {"status": "ok", "deal_id": deal_id, "stored": stored,
            "scanned": len(transcripts),
            "window": {"limit": limit, "since": since}}


def latest_signals_for_deal(deal_id: str) -> Optional[dict]:
    """Return the signals dict of the latest stored Fireflies meeting for a deal."""
    from . import db
    db.ensure_fireflies_schema()
    m = db.fireflies_latest_for_deal(deal_id)
    if not m:
        return None
    return m.get("signals") or {}


def meetings_for_deal(deal_id: str) -> list[dict]:
    """Return stored Fireflies meetings for a deal, newest first."""
    from . import db
    db.ensure_fireflies_schema()
    return db.fireflies_meetings_for_deal(deal_id)


# ==========================================================================
# Behavioral coaching read models (dashboard endpoints)
# ==========================================================================

_TARGET_TALK_RATIO = 45       # talk ≤45% in discovery calls
_TARGET_MONOLOGUE_SEC = 60  # no single monologue longer than 60s
_TARGET_FILLERS = 3           # soft target: ≤3 fillers/meeting (real goal: trend ↓)

_FILLER_WORDS_COACHING = (
    "you know", "i mean", "o sea", "osea", "digamos", "no sé", "no se",
    "um", "uh", "uhm", "erm", "hmm", "mmm", "like", "este", "eh", "pues",
)
_FILLER_PHRASES = tuple(w for w in _FILLER_WORDS_COACHING if " " in w)
_FILLER_TOKENS = tuple(w for w in _FILLER_WORDS_COACHING if " " not in w)
_FILLER_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _FILLER_TOKENS) + r")\b", re.IGNORECASE
) if _FILLER_TOKENS else None

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


def _count_fillers(text: str) -> int:
    if not text:
        return 0
    low = text.lower()
    n = sum(low.count(p) for p in _FILLER_PHRASES)
    if _FILLER_TOKEN_RE:
        n += len(_FILLER_TOKEN_RE.findall(low))
    return n


def _longest_monologue_sentences(sentences: list, speaker_name: str) -> int:
    """Longest run of consecutive sentences for a speaker.

    Sentences from Fireflies no longer expose duration/timestamps, so we use
    consecutive sentence count as a proxy for monologue length.
    """
    if not speaker_name:
        return 0
    best = 0
    run = 0
    for s in sentences:
        if s.get("speaker_name") == speaker_name:
            run += 1
        else:
            best = max(best, run)
            run = 0
    best = max(best, run)
    return best


def _analyze_meeting_for_coaching(transcript: dict) -> dict:
    """Per-meeting analysis used by the dashboard behavioral-coaching endpoint."""
    sentences = transcript.get("sentences") or []
    participants = transcript.get("participants") or []
    duration = transcript.get("duration") or 0
    # Without sentence-level durations, use sentence count as the time proxy.
    if not duration and sentences:
        duration = len(sentences)

    speaker_counts = {}
    for s in sentences:
        sp = s.get("speaker_name") or s.get("speakerName") or "Unknown"
        speaker_counts[sp] = speaker_counts.get(sp, 0) + 1
    total = sum(speaker_counts.values()) or 1

    ricardo_name = None
    for name in speaker_counts:
        if _is_operator(name):
            ricardo_name = name
            break
    if not ricardo_name and len(participants) == 1 and speaker_counts:
        ricardo_name = list(speaker_counts.keys())[0]

    ricardo_sentences = speaker_counts.get(ricardo_name, 0)
    talk_pct = round(ricardo_sentences / total * 100, 1) if total > 0 else 0.0

    filler_words = sum(
        _count_fillers(s.get("text", ""))
        for s in sentences
        if (s.get("speaker_name") or s.get("speakerName")) == ricardo_name
    ) if ricardo_name else 0
    longest_monologue = _longest_monologue_sentences(sentences, ricardo_name) if ricardo_name else 0

    title = (transcript.get("title") or "").lower()
    others = [
        str(p)
        for p in participants if isinstance(p, str)
    ]
    others = [o for o in others if o and not _is_operator(o)]
    if any(w in title for w in ("discovery", "demo", "call", "intro")):
        meeting_type = "discovery"
    elif any(w in title for w in ("follow", "follow up", "sync", "check-in")):
        meeting_type = "followup"
    elif not others:
        meeting_type = "solo"
    else:
        meeting_type = "meeting"

    return {
        "id": transcript.get("id"),
        "title": transcript.get("title", ""),
        "date": _parse_date(transcript.get("date")),
        "duration_min": round(duration / 60) if duration else 0,
        "participants": len(participants),
        "others": others,
        "ricardo_sentences": ricardo_sentences,
        "talk_pct": talk_pct,
        "filler_words": filler_words,
        "longest_monologue_sec": longest_monologue,
        "target_pct": _TARGET_TALK_RATIO,
        "meeting_type": meeting_type,
        "over_target": talk_pct > _TARGET_TALK_RATIO,
    }


def _trend(values_newest_first: list) -> tuple:
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


def _metric(values_newest_first: list, target: float) -> dict:
    if not values_newest_first:
        return {"avg": None, "latest": None, "trend": "n/a", "delta": None,
                "on_target": None, "series": []}
    avg = round(sum(values_newest_first) / len(values_newest_first), 1)
    trend, delta = _trend(values_newest_first)
    on_target = avg <= target
    return {
        "avg": avg,
        "latest": values_newest_first[0],
        "trend": trend,
        "delta": delta,
        "on_target": on_target,
        "series": list(reversed(values_newest_first)),
    }


def _clamp_limit(limit, default: int = 10, cap: int = 50) -> int:
    """Public read-model limit contract: falsy/garbage → default, hard cap."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    if not n:
        n = default
    return max(1, min(n, cap))


def _empty_coaching_response() -> dict:
    targets = {
        "talk_pct": _TARGET_TALK_RATIO,
        "fillers": "down",
        "monologue_sec": _TARGET_MONOLOGUE_SEC,
    }
    return {
        "sample_size": 0,
        "meetings": [],
        "targets": targets,
        "metrics": {
            "talk_pct": _metric([], _TARGET_TALK_RATIO),
            "fillers": _metric([], _TARGET_FILLERS),
            "longest_monologue_sec": _metric([], _TARGET_MONOLOGUE_SEC),
        },
        "tip": {"metric": None, "text": "Aún no hay reuniones (no-solo) para analizar."},
    }


def _coaching_summary(analyses: list, recent: int = 10) -> dict:
    non_solo = [a for a in analyses if a.get("meeting_type") != "solo"]
    window = non_solo[:recent]
    base = _empty_coaching_response()
    if not window:
        return base

    talk = [a.get("talk_pct", 0) for a in window]
    fillers = [a.get("filler_words", 0) for a in window]
    monolog = [a.get("longest_monologue_sec", 0) for a in window]

    metrics = {
        "talk_pct": _metric(talk, _TARGET_TALK_RATIO),
        "fillers": _metric(fillers, _TARGET_FILLERS),
        "longest_monologue_sec": _metric(monolog, _TARGET_MONOLOGUE_SEC),
    }

    def _overage(avg, target):
        if avg is None or target <= 0:
            return 0.0
        return max(0.0, (avg - target) / target)

    scores = {
        "talk_pct": _overage(metrics["talk_pct"]["avg"], _TARGET_TALK_RATIO),
        "fillers": _overage(metrics["fillers"]["avg"], _TARGET_FILLERS),
        "longest_monologue_sec": _overage(
            metrics["longest_monologue_sec"]["avg"], _TARGET_MONOLOGUE_SEC),
    }
    worst = max(scores, key=lambda k: scores[k])
    tip_key = worst if scores[worst] > 0 else "on_target"
    week_index = datetime.now().isocalendar()[1]
    tips = _COACHING_TIPS[tip_key]
    tip_text = tips[week_index % len(tips)]

    base["sample_size"] = len(window)
    base["metrics"] = metrics
    base["tip"] = {"metric": tip_key if tip_key != "on_target" else None, "text": tip_text}
    return base


def _empty_analytics_response(limit: int) -> dict:
    return {
        "available": False,
        "reason": "no_api_key",
        "limit": limit,
        "target_pct": _TARGET_TALK_RATIO,
        "summary": {
            "target_pct": _TARGET_TALK_RATIO,
            "sample_size": 0,
            "avg_talk_pct": None,
            "gap_to_target": None,
            "on_target": None,
            "over_target_count": 0,
            "trend": "n/a",
            "trend_delta": None,
        },
        "meetings": [],
    }


def _summarize_analytics(analyses: list, recent: int = 5) -> dict:
    non_solo = [a for a in analyses if a.get("meeting_type") != "solo"]
    window = non_solo[:recent]
    empty = {
        "target_pct": _TARGET_TALK_RATIO,
        "sample_size": 0,
        "avg_talk_pct": None,
        "gap_to_target": None,
        "on_target": None,
        "over_target_count": 0,
        "trend": "n/a",
        "trend_delta": None,
    }
    if not window:
        return empty
    pcts = [a["talk_pct"] for a in window]
    avg = round(sum(pcts) / len(pcts), 1)
    trend, delta = _trend(pcts)
    return {
        "target_pct": _TARGET_TALK_RATIO,
        "sample_size": len(window),
        "avg_talk_pct": avg,
        "gap_to_target": round(avg - _TARGET_TALK_RATIO, 1),
        "on_target": avg <= _TARGET_TALK_RATIO,
        "over_target_count": sum(1 for p in pcts if p > _TARGET_TALK_RATIO),
        "trend": trend,
        "trend_delta": delta,
    }


def coaching(limit: int = 10) -> dict:
    """Fail-soft behavioral-coaching read model for the dashboard."""
    limit = _clamp_limit(limit)
    if not _api_key():
        return {"available": False, "reason": "no_api_key", "limit": limit,
                **_empty_coaching_response()}
    try:
        transcripts = fetch_transcripts(limit=limit)
        analyses = [_analyze_meeting_for_coaching(t) for t in transcripts]
        result = _coaching_summary(analyses, recent=limit)
        result["available"] = True
        result["limit"] = limit
        result["meetings"] = analyses
        return result
    except Exception as e:
        return {"available": False, "reason": f"fetch_error: {e}", "limit": limit,
                **_empty_coaching_response()}


def analytics(limit: int = 10) -> dict:
    """Fail-soft Fireflies analytics read model for the dashboard."""
    limit = _clamp_limit(limit)
    if not _api_key():
        return _empty_analytics_response(limit)
    try:
        transcripts = fetch_transcripts(limit=limit)
        analyses = [_analyze_meeting_for_coaching(t) for t in transcripts]
        return {
            "available": True,
            "limit": limit,
            "target_pct": _TARGET_TALK_RATIO,
            "summary": _summarize_analytics(analyses, recent=min(5, limit)),
            "meetings": analyses,
        }
    except Exception as e:
        resp = _empty_analytics_response(limit)
        resp["reason"] = f"fetch_error: {e}"
        return resp
