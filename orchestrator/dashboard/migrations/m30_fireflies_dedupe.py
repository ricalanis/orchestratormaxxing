"""m30 — one cached row per (deal, transcript), enforced by the schema.

Found 2026-08-10 during the first full Fireflies backfill: fetching a deal
twice left two rows for the same meeting. `fireflies_meeting_insert` uses
INSERT OR REPLACE, but its conflict target is the PRIMARY KEY `id` and the
caller minted a fresh uuid per write — so the REPLACE could never fire while
the natural key, (deal_id, transcript_id), went unenforced. A duplicated cache
is not cosmetic: meetings_analyzed, the readiness signals and the coaching
metrics all count these rows.

The caller now derives `id` from the pair (see fireflies._meeting_id), which
makes the existing OR REPLACE collapse a re-fetch. This migration handles the
other half: it removes what already leaked (keeping the NEWEST row per pair —
the freshest fetch wins) and then locks the key with a UNIQUE index, so the
guarantee survives a future caller that forgets the convention.

Two deals may legitimately share one transcript (a joint call), so the key is
the PAIR, never transcript_id alone.

Additive + idempotent. Runs inside the runner's transaction: no commit here.
"""


def m30_fireflies_dedupe(conn) -> None:
    # Keep the newest row per (deal_id, transcript_id). `rowid` breaks ties so
    # two rows written in the same second still collapse deterministically.
    conn.execute(
        "DELETE FROM fireflies_meetings WHERE rowid NOT IN ("
        "  SELECT rowid FROM ("
        "    SELECT rowid,"
        "           ROW_NUMBER() OVER ("
        "             PARTITION BY deal_id, transcript_id"
        "             ORDER BY COALESCE(fetched_at, created_at, 0) DESC, rowid DESC"
        "           ) AS rn"
        "    FROM fireflies_meetings"
        "  ) WHERE rn = 1)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fireflies_deal_transcript "
        "ON fireflies_meetings(deal_id, transcript_id)"
    )
