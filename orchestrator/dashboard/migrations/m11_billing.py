"""m11 — `deals.invoiced_at` / `deals.paid_at`: the money's last two facts.

Directiva ADICIÓN 8 — *"Después del cierre del proyecto está la FACTURACIÓN Y
COBRANZA como último paso, que a su vez conecta con tareas."*

--------------------------------------------------------------------------
Two columns, no fifth noun
--------------------------------------------------------------------------

The July invariant is four visible nouns — Cliente · Oportunidad · Proyecto ·
Tarea — and nothing new gets added without killing one. An `invoices` table
would be a fifth. So billing lands as **two additive timestamps on the deal**
(the noun that already owns the money) plus tasks the materializer mints off
them. Nothing new to learn, and the two questions the operator actually asks —
*what have I not invoiced* and *what have I not been paid for* — become one
`WHERE` each instead of a report:

    won + delivered project + invoiced_at IS NULL   → por facturar
    invoiced_at IS NOT NULL + paid_at IS NULL       → por cobrar

INTEGER epoch seconds, matching `deals.created_at` / `updated_at` / `closed_at`
beside them (NOT the ISO TEXT of `projects.delivered_at`, which is a different
table's established convention — mixing the two inside one row is how a
`fromisoformat(1754...)` lands in production).

--------------------------------------------------------------------------
Nullable, no default, no backfill
--------------------------------------------------------------------------

NULL means *not yet* and is the only honest value for history: the DB has never
recorded an invoice date, so inventing one from `closed_at` or `delivered_at`
would put four already-collected deals into the cobranza queue on day one and
teach the operator that the queue lies. The first real values arrive when
Ricardo taps 💵 Facturado / ✅ Pagado — human-only verbs, exactly like the three
conversion verbs (spec red line 11), reachable from the dashboard API and
deliberately absent from `mcp_server.py`.

Ordering is a fact, not a convention: `paid_at` without `invoiced_at` is
nonsense, and `crm.mark_deal_paid` refuses it (`not_invoiced`). That guard lives
in the writer rather than in a CHECK constraint because SQLite cannot add a
table-level CHECK by `ALTER TABLE`, and a constraint that can only be added by
rebuilding a 62-row live table is a constraint that will be dropped by the next
rebuild.

Receives the runner's OWN connection inside its transaction — it must not
commit, close, or open a connection of its own.
"""

_ADD_INVOICED = "ALTER TABLE deals ADD COLUMN invoiced_at INTEGER"
_ADD_PAID = "ALTER TABLE deals ADD COLUMN paid_at INTEGER"


def m11_billing(conn) -> dict:
    """Add `deals.invoiced_at` + `deals.paid_at`. Idempotent, additive.

    Registered as `m11_billing` in `runner.py`, after m09. Deliberately
    backfill-free (see the docstring). Returns `{"columns": [...]}`; the runner
    ignores it and the contract reads it.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    added = []
    if "invoiced_at" not in cols:
        conn.execute(_ADD_INVOICED)
        added.append("invoiced_at")
    if "paid_at" not in cols:
        conn.execute(_ADD_PAID)
        added.append("paid_at")
    return {"columns": added}
