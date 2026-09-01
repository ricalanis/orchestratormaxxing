"""m16 — `capture_receipts`: el acuse durable del webhook.

Un webhook de Fireflies avisa que una junta ya tiene resumen, pero **no trae el
contenido** — hay que ir a buscarlo por GraphQL. Hacer ese fetch dentro del
request (o en un `BackgroundTasks`) significa que si el proceso muere entre el
200 y el fetch, el aviso se perdió y nadie lo sabe: el evento nunca se captura y
no queda rastro de que debió capturarse.

Así que el webhook hace lo mínimo durable — escribe un acuse — y responde 200.
El tick drena los acuses pendientes y hace el fetch, con reintentos visibles. El
poll de reconciliación sigue existiendo como segunda red: si un acuse se pierde
antes de escribirse (proceso muerto ANTES del insert), el poll lo levanta igual.

`fetched_at` no vacía la fila a propósito: el acuse es también el registro de que
Fireflies avisó, y compararlo contra `capture_events` es lo que delata un webhook
configurado que dejó de llegar.
"""

RECEIPT_STATUSES = ("pending", "fetched", "failed")
MAX_RECEIPT_ATTEMPTS = 5


def apply(conn):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS capture_receipts (
            source_kind  TEXT NOT NULL,
            source_ref   TEXT NOT NULL,
            event_name   TEXT,
            received_at  INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ({", ".join(repr(s) for s in RECEIPT_STATUSES)})),
            attempts     INTEGER NOT NULL DEFAULT 0,
            fetched_at   INTEGER,
            event_id     TEXT,
            last_error   TEXT,
            PRIMARY KEY (source_kind, source_ref)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_status ON capture_receipts(status)")
