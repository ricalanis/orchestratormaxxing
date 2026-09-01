"""m12 — threads gain their funnel STATION (ADICIÓN 6/ADICIÓN 10 era, Ricardo sign-off 2026-08-02).

The four stations of the journey become the conversational structure:
    🔍 Clientes (buscar) · 📄 Oportunidades (proponer) · 🔨 Proyectos (ejecutar)
    · ✅ Delivery (cerrar) · 📅 Hoy (ritual)
`station` is an ADDITIVE column — the `role` CHECK vocabulary (code/growth/ops/
health/personal) stays untouched (journey ruling 9: use the real vocabulary,
never rebuild a CHECK under a live gateway). Functional topics (Capacity,
Memoria, Review, Health) carry station NULL: conversations without funnel
obligations, which is a feature.

Seed literal verified against the live registry 2026-08-02. Renames land in
the registry here; the Telegram-side topic renames are gateway-external
(Bot API editForumTopic) and performed by the operator flow, not a migration.
"""

STATIONS = ("clientes", "oportunidades", "proyectos", "delivery", "ritual")

# thread_id → (new name, station)
_SEED = {
    9193:  ("🔍 Clientes", "clientes"),
    9278:  ("📄 Oportunidades", "oportunidades"),
    7363:  ("🔨 Proyectos", "proyectos"),
    7350:  ("✅ Delivery", "delivery"),
    15185: ("📅 Hoy", "ritual"),
}


def apply(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "station" not in cols:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN station TEXT "
            "CHECK(station IS NULL OR station IN "
            "('clientes','oportunidades','proyectos','delivery','ritual'))"
        )
    for thread_id, (name, station) in _SEED.items():
        # Idempotent: only touch rows that exist; never resurrect an archived
        # topic and never invent one the registry doesn't know.
        conn.execute(
            "UPDATE threads SET name = ?, station = ? WHERE thread_id = ?",
            (name, station, thread_id),
        )
