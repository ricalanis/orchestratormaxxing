"""m19 — `paid_amount`: el dinero que REALMENTE llegó, junto al hecho de que llegó.

`paid_at` (m11) dice CUÁNDO cayó el dinero; `deals.value` dice cuánto se
pactó. Entre los dos falta el monto real del depósito — que puede diferir
del pactado (ajustes, redondeos, retenciones cuando aplican). El operador lo
pidió explícito: "el campo de dinero recibido".

- REAL, nullable, sin default, sin backfill. NULL significa "no registrado
  aparte" y TODO lector de efectivo lee `COALESCE(paid_amount, value)`: el
  monto pactado es la verdad por omisión y el real la corrige cuando existe.
- Se captura en el gesto que ya existe: el tap ✅ acepta `{paid_amount}` en
  el body, prellenado con `value` en el drawer — un tap si el depósito fue
  exacto. Corrección posterior: PATCH validado con evento
  `paid_amount_set {from,to}`, solo mientras el deal esté pagado (un monto
  recibido sin pago registrado es un error de captura, no un estado).
- NO es una quinta columna de fecha ni un booleano nuevo: "facturado" como
  booleano ya existe estructuralmente (`invoiced_at IS NOT NULL`) y
  duplicarlo en una columna aparte sería una segunda fuente de verdad — el
  lector que quiera el booleano lo deriva, nunca lo almacena.

Recibe la conexión PROPIA del runner dentro de su transacción — no debe
hacer commit, close, ni abrir conexión propia.
"""

_ADD = "ALTER TABLE deals ADD COLUMN paid_amount REAL"


def m19_paid_amount(conn) -> dict:
    """Add `deals.paid_amount`. Idempotent, additive, no backfill."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    added = []
    if "paid_amount" not in cols:
        conn.execute(_ADD)
        added.append("paid_amount")
    return {"columns": added}
