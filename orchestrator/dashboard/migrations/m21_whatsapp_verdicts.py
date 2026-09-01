"""m21 — el veredicto del clasificador, SEPARADO del permiso.

Dos columnas nuevas y la distancia entre ellas es el diseño entero:

    verdict  — lo que el clasificador CREE (negocio | personal | incierto)
    allowed  — lo que Ricardo DECIDIÓ (sigue naciendo en 0)

Nunca se copia una en la otra. Un clasificador que se auto-otorga permiso de
leer conversaciones es exactamente el fallo que este sistema no puede tener:
marcar de más significa leer algo privado, y eso no se deshace. Marcar de menos
solo retrasa algo recuperable, y por eso `incierto` es una respuesta de primera
clase en vez de un empujón hacia "sí".

`verdict_source` distingue de dónde vino la opinión, porque no todas valen igual:
un empate exacto con un contacto del CRM es un hecho verificable; el
reconocimiento de un nombre por un modelo es una inferencia. Cuando el humano
revisa, saber cuál está viendo cambia cuánto debe leer antes de decidir.
"""


def apply(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(whatsapp_chats)")}
    add = [
        ("verdict", "TEXT CHECK (verdict IS NULL OR verdict IN "
                    "('negocio','personal','incierto'))"),
        ("verdict_reason", "TEXT"),
        ("verdict_source", "TEXT CHECK (verdict_source IS NULL OR verdict_source IN "
                           "('crm','nombre_entidad','modelo','patron_b2b'))"),
        ("verdict_at", "INTEGER"),
        ("chat_name", "TEXT"),
    ]
    for name, decl in add:
        if name not in cols:
            conn.execute(f"ALTER TABLE whatsapp_chats ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_chats_verdict ON whatsapp_chats(verdict)")
