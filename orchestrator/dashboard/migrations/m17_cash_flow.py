"""m17 — el PLAN del cobro: `payment_terms_days` + `expected_payment_date`
(+ su ancla write-once `expected_payment_date_original`).

m11 dio al deal los dos HECHOS del dinero (`invoiced_at` / `paid_at`, epoch,
one-way, human-only). Lo que el deal no tenía es el futuro: cuándo se pactó
que ese dinero llegue. Estas tres columnas son ese plan, y la reconciliación
que pide el operador — esperado vs. pagado — es el par plan/hecho leído
junto, jamás una columna almacenada.

--------------------------------------------------------------------------
Tres columnas, ninguna tabla — y por qué TEXT
--------------------------------------------------------------------------

El invariante de julio sigue: cuatro sustantivos visibles, y una tabla
`invoices`/`payments`/`payment_schedule` sigue rechazada. El plan vive en el
deal, el sustantivo que ya posee el dinero.

`expected_payment_date` es TEXT ISO 'YYYY-MM-DD', NO epoch. La fila ya
separa dos familias: hechos-instante = INTEGER epoch (`created_at`,
`closed_at`, `invoiced_at`, `paid_at`) y fechas-plan = TEXT ISO
(`expected_close_date`, `next_touch_date`, `last_touch_date`). Una fecha
esperada de cobro es fecha civil de calendario — un epoch la obligaría a
inventar una hora ficticia y mete bugs de timezone en cada comparación por
día o semana. Lo que m11 prohíbe es mezclar convenciones EN UN CAMPO
(`fromisoformat(1754...)`), no la coexistencia tipada que la fila ya tiene.

`expected_payment_date_original` se escribe UNA vez (COALESCE al primer
set, cero input humano) y jamás se toca después: es el ancla del slippage
acumulado consultable en SQL plano. El historial completo de repromesas
vive en `deal_events` (`payment_promised` / `payment_repromised`), no aquí.

`payment_terms_days` (0 = contado, 15/30/45…) se pacta POR TRATO — un mismo
cliente puede pactar contado en un sprint y 30d en otro — por eso vive en el
deal y no en accounts. JAMÁS default 30 en SQL: el default es sugerencia de
UI, nunca un hecho escrito solo.

--------------------------------------------------------------------------
Parcialidades: child deals, no columnas nuevas
--------------------------------------------------------------------------

La política real del operador es 50/50 (anticipo + liquidación). LA forma
canónica es `parent_deal_id`, que ya existe: un child deal por parcialidad,
cada uno con su `value` y su ciclo completo esperado→facturado→cobrado
(mapea 1:1 con un CFDI PUE por parcialidad o el REP de un PPD). Un padre con
hijos de pago se excluye de toda suma de dinero y rechaza invoiced/paid —
ese guard llega con el verbo de split, no con esta migración.

--------------------------------------------------------------------------
Nullable, sin default, sin backfill
--------------------------------------------------------------------------

NULL significa "todavía no pactado" y es el único valor honesto para
historia: inventar fechas desde `closed_at` pondría cobros ficticios en la
vista el día uno y enseñaría al operador que la vista miente (la lección
m11). Los primeros valores reales llegan por los gestos humanos del drawer.

Recibe la conexión PROPIA del runner dentro de su transacción — no debe
hacer commit, close, ni abrir conexión propia.
"""

_ADDS = (
    ("payment_terms_days", "ALTER TABLE deals ADD COLUMN payment_terms_days INTEGER"),
    ("expected_payment_date", "ALTER TABLE deals ADD COLUMN expected_payment_date TEXT"),
    ("expected_payment_date_original",
     "ALTER TABLE deals ADD COLUMN expected_payment_date_original TEXT"),
)


def m17_cash_flow(conn) -> dict:
    """Add the three cash-plan columns to `deals`. Idempotent, additive.

    Registered as `m17_cash_flow` in `runner.py`, after m16. Deliberately
    backfill-free (see the docstring). Returns `{"columns": [...]}`; the
    runner ignores it and the contract reads it.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    added = []
    for name, ddl in _ADDS:
        if name not in cols:
            conn.execute(ddl)
            added.append(name)
    return {"columns": added}
