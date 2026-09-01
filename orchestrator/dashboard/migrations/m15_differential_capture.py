"""m15 — the differential capture spine: events → objectives → suggestions.

The store behind "lectura continua, digestión diferencial por eventos" (design:
`knowledge/differential-capture-architecture-2026-08-03.md`; plan of record §V2).
Three strata from MM-Mem's fuzzy-trace split, one table each:

  * **E0 sensorial (verbatim, purgable)** — `capture_events.payload`, the raw JSON of a
    meeting/conversation. It carries customer speech, so it has a TTL and is
    nulled after digestion. Nothing downstream may depend on it surviving.
  * **E1 episódico (append-only)** — the rest of the `capture_events` row: identity,
    window bounds, digest state. This is the unit of differential processing.
  * **E2 gist gobernado (estable)** — `objectives` + `entity_state`, written
    ONLY by the deterministic applier through typed operators (GEM/MemState:
    state-level operators, chronological validity, provenance preservation).

Two rules are enforced by the schema rather than left to the caller, because
both were failure modes the design review caught:

  * **Identity never comes from model prose.** `capture_events.event_id` hashes the
    capture coordinates and `objectives.id` hashes the applying op's
    coordinates, so a paraphrase cannot fork a duplicate row or slip past a
    sticky dismiss. `suggestions` goes further: `UNIQUE(objective_id, kind)`
    makes dedup structural — a second mention of an open objective can only
    bump `seen_count`, never mint a second card.
  * **A malformed digestion must not consume the event.** `digest_status`
    carries `failed` and `dead_letter` alongside `digested`, with `attempts`,
    so a bad model response is retried and then parked visibly instead of
    being marked done and lost.

Every nullable enum is written `CHECK (x IS NULL OR x IN (...))`: a bare
`IN (NULL, ...)` evaluates to NULL for an unmatched value, and SQLite accepts a
NULL CHECK — the constraint would silently permit anything.

Purely additive. `objective_evidence` copies its quote at digestion time
precisely so the E0 purge can never break the "por qué te lo sugiero" citation.
"""

# Kept in Python next to the DDL so callers validate against one vocabulary.
SOURCE_KINDS = ("fireflies", "whatsapp", "whatsapp_voice")
DIGEST_STATUSES = ("pending", "leased", "digested", "failed", "dead_letter")
ENTITY_KINDS = ("deal", "project", "account")
OBJECTIVE_STATUSES = ("open", "blocked", "done", "superseded", "archived")
OP_VERDICTS = ("applied", "rejected_validation", "rejected_stale",
               "rejected_unknown_ref", "skipped_duplicate")
SUGGESTION_KINDS = ("create_task", "close_task", "unblock", "review")
SUGGESTION_STATUSES = ("open", "accepting", "accepted", "dismissed",
                       "expired", "suppressed")
CARD_STATUSES = ("unsent", "sending", "sent", "ambiguous")

# Retries before an event is parked. Three is enough to ride out a transient
# malformed completion without grinding on a genuinely undigestible event.
MAX_DIGEST_ATTEMPTS = 3


def _q(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def apply(conn):
    # --- E0 + E1: the capture log ------------------------------------------
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS capture_events (
            event_id        TEXT PRIMARY KEY,
            source_kind     TEXT NOT NULL CHECK (source_kind IN ({_q(SOURCE_KINDS)})),
            source_ref      TEXT NOT NULL,
            title           TEXT,
            window_start    INTEGER,
            window_end      INTEGER,
            occurred_at     INTEGER,
            captured_at     INTEGER NOT NULL,
            payload         TEXT,
            payload_purged_at INTEGER,
            entity_kind     TEXT CHECK (entity_kind IS NULL OR entity_kind IN ({_q(ENTITY_KINDS)})),
            entity_id       TEXT,
            digest_status   TEXT NOT NULL DEFAULT 'pending'
                            CHECK (digest_status IN ({_q(DIGEST_STATUSES)})),
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_error      TEXT,
            lease_token     TEXT,
            lease_expires_at INTEGER,
            digested_at     INTEGER,
            ops_applied     INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_digest ON capture_events(digest_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_source ON capture_events(source_kind, occurred_at)")

    # Composite watermark: a bare timestamp cannot disambiguate two transcripts
    # that land in the same second, so the poll would either re-read or skip one.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS capture_watermarks (
            source_kind   TEXT PRIMARY KEY,
            last_seen_ts  INTEGER,
            last_seen_id  TEXT,
            last_run_at   INTEGER
        )
    """)

    # --- E2: the governed gist ---------------------------------------------
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS objectives (
            id              TEXT PRIMARY KEY,
            entity_kind     TEXT CHECK (entity_kind IS NULL OR entity_kind IN ({_q(ENTITY_KINDS)})),
            entity_id       TEXT,
            title           TEXT NOT NULL,
            owner           TEXT,
            waiting_on      TEXT,
            status          TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ({_q(OBJECTIVE_STATUSES)})),
            prominence      REAL NOT NULL DEFAULT 1.0,
            version         INTEGER NOT NULL DEFAULT 1,
            due_hint        TEXT,
            task_id         TEXT,
            opened_at       INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            closed_at       INTEGER,
            last_evidence_ts INTEGER,
            superseded_by   TEXT REFERENCES objectives(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objectives_status ON objectives(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objectives_entity ON objectives(entity_kind, entity_id)")

    # Provenance, append-only. The quote is COPIED here (not referenced into
    # events.payload) so the E0 purge cannot orphan a card's citation.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS objective_evidence (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id TEXT NOT NULL REFERENCES objectives(id),
            event_id     TEXT NOT NULL REFERENCES capture_events(event_id),
            anchor       TEXT,
            quote        TEXT NOT NULL,
            speaker      TEXT,
            ts           INTEGER,
            op           TEXT,
            created_at   INTEGER NOT NULL,
            UNIQUE(objective_id, event_id, anchor)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obj_evidence ON objective_evidence(objective_id)")

    # Bounded by construction: gist is current state, not a log.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS entity_state (
            entity_kind     TEXT NOT NULL CHECK (entity_kind IN ({_q(ENTITY_KINDS)})),
            entity_id       TEXT NOT NULL,
            gist            TEXT CHECK (gist IS NULL OR length(gist) <= 700),
            updated_at      INTEGER NOT NULL,
            updated_by_event TEXT REFERENCES capture_events(event_id),
            PRIMARY KEY (entity_kind, entity_id)
        )
    """)

    # The applier's ledger: GEM auditability + crash-idempotent replay. A retry
    # of an already-applied op collides on UNIQUE and resolves to
    # `skipped_duplicate` instead of double-applying.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS state_ops (
            event_id     TEXT NOT NULL REFERENCES capture_events(event_id),
            op_index     INTEGER NOT NULL,
            op           TEXT NOT NULL,
            objective_id TEXT,
            args_json    TEXT,
            verdict      TEXT NOT NULL CHECK (verdict IN ({_q(OP_VERDICTS)})),
            reason       TEXT,
            created_at   INTEGER NOT NULL,
            PRIMARY KEY (event_id, op_index)
        )
    """)

    # --- Derived: suggestions ----------------------------------------------
    # Never written by the model; derived by code from objective transitions.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS suggestions (
            id            TEXT PRIMARY KEY,
            objective_id  TEXT NOT NULL REFERENCES objectives(id),
            kind          TEXT NOT NULL CHECK (kind IN ({_q(SUGGESTION_KINDS)})),
            status        TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ({_q(SUGGESTION_STATUSES)})),
            card_status   TEXT NOT NULL DEFAULT 'unsent'
                          CHECK (card_status IN ({_q(CARD_STATUSES)})),
            bucket        TEXT,
            confidence    REAL,
            title         TEXT NOT NULL,
            body          TEXT,
            proposed_project_id TEXT,
            proposed_due  TEXT,
            seen_count    INTEGER NOT NULL DEFAULT 1,
            edited        INTEGER NOT NULL DEFAULT 0,
            final_title   TEXT,
            task_id       TEXT,
            accept_op_id  TEXT,
            card_sent_at  INTEGER,
            decided_at    INTEGER,
            decided_via   TEXT,
            created_at    INTEGER NOT NULL,
            updated_at    INTEGER NOT NULL,
            UNIQUE(objective_id, kind)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_card ON suggestions(card_status)")
