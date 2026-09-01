"""m17 — la allowlist de chats de WhatsApp: default-deny, y la actividad sin contenido.

El espejo de wacli tiene 493 chats y 417 grupos de Ricardo. La digestión no debe
ver ni uno hasta que él lo diga: son conversaciones con clientes, con su familia
y con quien sea. Por eso `allowed` nace en 0 y sin fila no hay lectura — un chat
desconocido se ignora, no se pregunta.

Dos tablas y la separación entre ellas es el punto:

  * `whatsapp_chats` — la decisión de Ricardo, por chat. Vive aquí y no en el
    espejo de wacli porque es política nuestra, no estado de WhatsApp.
  * `whatsapp_activity` — un pulso: "este chat tuvo movimiento a esta hora".
    **Sin una palabra del mensaje.** El webhook solo escribe esto, así que un
    chat que nunca se permita jamás deja texto en nuestra base. El contenido se
    lee del espejo de wacli SOLO cuando la ventana cierra y SOLO si el chat está
    permitido.

Esa asimetría es deliberada: el pulso es barato y anónimo, el contenido es caro
y sensible. Guardar el primero para todos y el segundo para casi nadie es lo que
deja que el sistema sepa "aquí pasó algo" sin acumular habla que nadie autorizó.
"""

WINDOW_GAP_SECONDS = 1800          # 30 min de silencio cierran una ventana


def apply(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_chats (
            jid         TEXT PRIMARY KEY,
            label       TEXT,
            allowed     INTEGER NOT NULL DEFAULT 0,
            contact_id  TEXT,
            is_group    INTEGER NOT NULL DEFAULT 0,
            decided_at  INTEGER,
            decided_by  TEXT,
            created_at  INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_chats_allowed ON whatsapp_chats(allowed)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_activity (
            jid          TEXT NOT NULL,
            last_seen_at INTEGER NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            window_open_at INTEGER,
            harvested_at INTEGER,
            PRIMARY KEY (jid)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_activity_seen ON whatsapp_activity(last_seen_at)")
