"""m23 — `projects.weekly_hours`: el presupuesto semanal declarado por proyecto.

El problema que resuelve NO es "no sé cuánto trabajo hay". Es "no sé cuántos
proyectos puedo tener activos a la vez". Hoy la capa de proyecto no tiene
ningún número de carga: `tier`, `health` y `quarter` están puestos en 5 de 23
filas y `delivered_at` está en NULL en las 23 — columnas muertas. Sumar otra
columna muerta sería el peor resultado posible, así que esta nace con un
lector que la muestra siempre y un gesto de un tap que la escribe.

- REAL, nullable, SIN backfill. Tres estados, y los tres significan algo:
  NULL = *sin dimensionar* (nunca se declaró), 0 = *aparcado* (declarado en
  cero: la forma reversible de soltar un proyecto sin archivarlo), >0 =
  *comprometido*. Escribir un número en las 13 filas activas al migrar
  fabricaría una medición que Ricardo no hizo, y el tablero arrancaría en
  verde mintiendo. Por eso el backfill es explícitamente ninguno.

- Es un PRESUPUESTO, no un pronóstico. La distinción es la que sostiene todo
  el diseño: Tawosi & Sarro (ESEM 2022, 37,440 historias) miden que la
  correlación entre tamaño estimado y tiempo real es fuerte en apenas el 7%
  de los proyectos — estimar horas para *predecir* no funciona. Pero
  Jørgensen & Sjøberg (2001) miden que una estimación *ancla y restringe* la
  conducta incluso cuando se sabe arbitraria — que es exactamente el efecto
  que se busca aquí. El número no predice la semana: la limita.

- No toca leads, oportunidades, deals, entrega ni dinero. No cambia la
  semántica de `status` ni ningún flujo existente. Es aditiva y legible sola.

Recibe la conexión PROPIA del runner dentro de su transacción — no debe hacer
commit, close, ni abrir conexión propia.
"""

_ADD = "ALTER TABLE projects ADD COLUMN weekly_hours REAL"


def m23_project_weekly_hours(conn) -> dict:
    """Add `projects.weekly_hours`. Idempotent, additive, no backfill."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    added = []
    if "weekly_hours" not in cols:
        conn.execute(_ADD)
        added.append("weekly_hours")
    return {"columns": added}
