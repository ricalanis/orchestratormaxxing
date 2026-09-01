"""m08 — deal_events learn WHERE they came from: `source` and `channel`.

Journey fase 1, step 5. `deal_events` is the commercial audit spine (touches,
stage changes, deliveries, growth edits) and every row of it answered *what*
happened without ever answering *how it reached us*.

--------------------------------------------------------------------------
What already existed, and why it was not enough (measured, not assumed)
--------------------------------------------------------------------------

The touch-channel commit (`4d5114b`, "auto-generate nurture on lead creation +
add channel to touch events") did NOT add columns. It added a `"channel"` key
to the **JSON payload** of `growth.record_touch`'s event
(`growth.py:1528` — `{"note": …, "channel": prior["lead_source"] or "unknown"}`),
and only there. Verified in situ: `PRAGMA table_info(deal_events)` is
`(id, deal_id, kind, payload, created_at)` and `crm._log` (crm.py:102) takes
`(conn, deal_id, kind, payload)` with no provenance parameters at all.

A key inside a JSON blob is not a queryable fact. The monthly channel rollup
(`growth.py:2664`) says so out loud — it joins `deal_events` back to
`deals.lead_source` *"since touch payloads don't carry a channel field"*, i.e.
it attributes every touch to the deal's ORIGINAL lead source rather than to the
channel the touch actually used. So a WhatsApp follow-up on a LinkedIn lead
counts as LinkedIn, forever.

Two columns, therefore, not one:

  * **`channel`** — the medium this interaction used (whatsapp · email ·
    linkedin · call · meeting · …). The step-5 loop closure writes it from the
    nurture step's `touch_type`, so a cadence card completed on the board
    records the channel it was designed for instead of inheriting one.
  * **`source`** — the WRITER: `web` (Ricardo tapped), `cadence` (the
    materializer), `agent`, `mcp`, `cli`. This is the provenance half, and it
    is what makes "how much of this pipeline did the human actually touch"
    answerable. It is deliberately NOT derivable from `kind`: a `touch` event
    can be written by the operator finishing a card or by an automated step
    logging itself, and those two are not the same claim.

Both nullable, no backfill: an existing row genuinely does not know its channel
or its writer, and inventing `'web'` for 40k rows of history would make the
first honest query lie. NULL means "recorded before we asked", and every reader
already treats a missing key that way.

--------------------------------------------------------------------------
Written by ONE function
--------------------------------------------------------------------------

`crm._log` gains two optional keyword parameters and stays the only writer —
the same single-writer shape as `sprints.set_project_status` (ruling 8). The
column list is built from `PRAGMA table_info` at write time so the function
still works against a pre-m08 DB (the dashboard and the MCP server both import
`crm` before the runner has necessarily applied anything).

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own.
"""

_ADD_SOURCE = "ALTER TABLE deal_events ADD COLUMN source TEXT"
_ADD_CHANNEL = "ALTER TABLE deal_events ADD COLUMN channel TEXT"


def m08_deal_event_provenance(conn) -> dict:
    """Add `deal_events.source` + `deal_events.channel`. Idempotent, additive.

    Registered as `m08_deal_event_provenance` in `runner.py`, after m07.
    Deliberately backfill-free (see the docstring). Returns
    `{"columns": [...]}`; the runner ignores it and the contract reads it.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "deal_events" not in tables:
        # Same shape as `crm.ensure_schema`'s CREATE, inlined for the same
        # reason as m07's: calling the ensure would open a second connection
        # outside the runner's transaction.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS deal_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " deal_id TEXT NOT NULL,"
            " kind TEXT NOT NULL,"
            " payload TEXT,"
            " created_at INTEGER NOT NULL)")

    cols = {r[1] for r in conn.execute("PRAGMA table_info(deal_events)")}
    added = []
    if "source" not in cols:
        conn.execute(_ADD_SOURCE)
        added.append("source")
    if "channel" not in cols:
        conn.execute(_ADD_CHANNEL)
        added.append("channel")
    return {"columns": added}
