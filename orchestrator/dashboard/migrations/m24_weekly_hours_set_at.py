"""m24 — `projects.weekly_hours_set_at`: cuándo se declaró el reparto.

Sin esta marca, `weekly_hours` es una declaración SIN FECHA, y una declaración
sin fecha envejece en silencio: el número de hace seis semanas se ve idéntico
al de esta mañana, y el panel presenta como plan de la semana algo que ya nadie
sostiene. Es el mismo error que `tier`/`health`/`delivered_at` —columnas que se
llenaron una vez y quedaron mintiendo— pero peor, porque esta sí suma.

La marca convierte el reparto en un RITUAL SEMANAL, que es lo que Ricardo pidió:
«debería ser un ritual semanal aproximar por proyecto dónde estará puesta mi
atención de la semana». Con la fecha, el lector puede distinguir tres cosas que
sin ella son una sola: declarado esta semana, declarado hace semanas, y nunca
declarado. Solo la primera es un plan.

- INTEGER epoch en segundos, nullable, SIN backfill. NULL = nunca se declaró,
  y no se inventa una fecha para las filas que ya tienen horas: fecharlas hoy
  afirmaría un ritual que no ocurrió. Arrancan como «declarado, sin fecha» y se
  fechan solas en el primer toque.
- La escribe el ÚNICO escritor (`sprints.update_project`) cada vez que
  `weekly_hours` cambia de mano. No hay verbo aparte: una marca que se pueda
  poner sin mover el número sería una forma de fingir el ritual.
- No cambia el pipeline de leads/oportunidades/deals/entrega/dinero.

Recibe la conexión PROPIA del runner dentro de su transacción — no debe hacer
commit, close, ni abrir conexión propia.
"""

_ADD = "ALTER TABLE projects ADD COLUMN weekly_hours_set_at INTEGER"


def m24_weekly_hours_set_at(conn) -> dict:
    """Add `projects.weekly_hours_set_at`. Idempotent, additive, no backfill."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    added = []
    if "weekly_hours_set_at" not in cols:
        conn.execute(_ADD)
        added.append("weekly_hours_set_at")
    return {"columns": added}
