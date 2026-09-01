"""m22 — constancia de hasta dónde se bajó el historial de un chat.

Autorizar un chat es "de aquí en adelante". Bajar lo anterior es una decisión
DISTINTA, y por eso deja su propio rastro en vez de esconderse dentro del
permiso: `backfill_from` dice hasta qué fecha se fue hacia atrás y
`backfill_at` cuándo se decidió.

Sirve para dos cosas concretas. La pantalla puede decir "ya bajaste 30 días de
este chat" en vez de ofrecer otra vez lo mismo. Y si algún día hay que responder
"¿por qué el sistema tiene este mensaje de hace dos meses?", la respuesta está
escrita al lado del permiso, con fecha.
"""


def apply(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(whatsapp_chats)")}
    for name, decl in (("backfill_from", "INTEGER"), ("backfill_at", "INTEGER")):
        if name not in cols:
            conn.execute(f"ALTER TABLE whatsapp_chats ADD COLUMN {name} {decl}")
