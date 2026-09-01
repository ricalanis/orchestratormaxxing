"""m18 — `expected_invoice_date`: el LANZAMIENTO del cobro, como fecha propia.

m17 dio al deal el plan del PAGO (`expected_payment_date` — cuándo promete
caer el dinero del cliente). El operador pidió distinguir la otra fecha del
ciclo: cuándo LANZA él el cobro (emitir/enviar la factura, arrancar la
cobranza). Son dos planes de dueño distinto y por eso dos columnas:

- `expected_invoice_date` — plan de una acción PROPIA. Gobernanza ligera a
  propósito: se escribe por el PATCH genérico validado (ISO estricto, evento
  `invoice_launch_planned {from,to}`), sin razón obligatoria — mover tu
  propia agenda no es patear una promesa. Se vuelve discutible (`moot`) al
  estamparse `invoiced_at`: el writer rechaza sets posteriores.
- `expected_payment_date` (m17) — promesa del CLIENTE. Sigue detrás del
  verbo auditado con razón y congelamiento; la asimetría es deliberada y es
  la razón de que estas dos fechas no compartan writer.

Misma familia de tipos que toda fecha-plan de la fila: TEXT ISO
'YYYY-MM-DD' (los hechos-instante son epoch INTEGER; ver m17). Nullable,
sin default, sin backfill: NULL = "todavía no planeado" es el único valor
honesto para historia.

En el calendario semanal del bloque 💰 Cobro las dos se distinguen por
diseño: 🧾 lanzamiento (acción tuya, azul; ámbar si se te pasó) vs
💰/✅ pago (dinero del cliente: ámbar esperando, rojo vencido, esmeralda
cobrado). Un lanzamiento vencido jamás pinta rojo — el rojo queda reservado
para dinero de cliente que no llegó.

Recibe la conexión PROPIA del runner dentro de su transacción — no debe
hacer commit, close, ni abrir conexión propia.
"""

_ADD = "ALTER TABLE deals ADD COLUMN expected_invoice_date TEXT"


def m18_invoice_launch(conn) -> dict:
    """Add `deals.expected_invoice_date`. Idempotent, additive, no backfill."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    added = []
    if "expected_invoice_date" not in cols:
        conn.execute(_ADD)
        added.append("expected_invoice_date")
    return {"columns": added}
