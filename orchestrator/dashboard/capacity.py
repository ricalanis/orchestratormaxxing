"""capacity — cuántos proyectos caben en una semana, y a costa de qué.

Este módulo responde UNA pregunta: *¿cuántos proyectos puedo tener activos a la
vez sin ahogarme?* No planifica, no reprograma, no toca leads, deals, entrega
ni dinero. Es una lectura pura.

Está construido sobre tres decisiones que son el diseño entero:

**1. Dos mitades con dueños distintos, y la medida contradice a la declarada.**
`declared` es lo que el operador dice que vale cada proyecto (`projects.weekly_hours`,
un tap). `measured` es lo que ya es cierto sin que teclee nada: cuántos proyectos
tocó de verdad, cuántas tareas cerró por semana, cuáles llevan 28 días sin
actividad. La mitad medida existe porque el día que esto se despliega las 13
filas activas están en NULL — un tablero que solo supiera leer lo declarado
arrancaría vacío y se moriría ahí. Y porque un proyecto que él declaró en 6 h y
que no ha movido una tarea en un mes es exactamente el que hay que soltar: ese
contraste es el instrumento, no un adorno.

**2. La capacidad se MIDE de `time_blocks`, nunca se codifica.**
El denominador sale de los bloques activos, sumando solo los roles de entrega.
Los otros (prospección, discovery, contenido, pipeline math) se reportan aparte
como *reservado a crecimiento*: son horas reales de la semana, pero no son horas
que puedan ir a un proyecto, y meterlas en el denominador haría que una semana
de pura venta se leyera como holgada. Si el operador alarga el bloque de consultoría,
el número sube solo. Nadie tiene que acordarse de editar una constante.

**3. Incompleto NUNCA puede leerse en verde.**
Con proyectos sin dimensionar la suma declarada es un piso, no un total, así que
la banda hace piso en ámbar y la cifra se imprime con `≥`. Es la única forma de
que la mitad declarada no mienta mientras se está llenando. El contrato prueba
esto en ROJO antes de existir el clamp.

Las bandas salen de Kingman (1961): la espera relativa en cola es ρ/(1−ρ), o sea
2.3× al 70% y 5.7× al 85%. No son gusto: son el codo de la curva.

**Los agentes acreditan CERO horas, y eso es aritmética, no pesimismo.** El
crédito autónomo exige que el trabajo se pueda calificar solo; hoy `contract_cmd`
está en NULL en las 313 tareas y solo 5 de 217 llevan `autonomy='auto'`. Además
la fórmula de fan-out (Crandall & Cummings 2007, `FO = NT/(IT+WT) + 1`) da
exactamente 1.00 cuando el agente se detiene a preguntar: sin tiempo de
desatención no hay multiplicación, por bueno que sea el modelo. Los carriles se
reportan porque se derivan de columnas que ya existen y cuestan una consulta;
el crédito se reporta en 0.0 con su razón fechada. Poner `autonomy='auto'` en
todas las tareas NO puede mover ese número — y el contrato lo prueba.
"""
import collections
import datetime
import sqlite3
from typing import Optional

from .db import get_conn

# TRES carriles, no dos, y son los del ciclo que ya existe en `stage_kind`:
#
#   comercial → lead · oportunidad · discovery · propuesta   (contacto/formalización)
#   entrega   → la ejecución conjunta hasta el delivery      (ejecución/entrega)
#   admin     → facturar y cobrar                            (facturación/cobranza)
#
# El tercero no es cosmético: medido en el calendario de 8 semanas, cobrar y
# facturar es media hora a la semana, y mientras vivió dentro de `consultant`
# esa media hora se presentaba como capacidad de proyecto. Poca cantidad, pero
# de otra naturaleza — el proyecto no puede gastarla.
#
# Sólo `entrega` es el denominador de la carga. Esta tabla es la única perilla.
ROLE_LANE = {
    "consultant": "delivery",
    "sdr": "growth", "ae": "growth", "marketer": "growth",
    "analyst": "admin", "cobranza": "admin",
}
# Un rol nuevo que nadie mapeó NO se vuelve capacidad de proyecto por omisión:
# inventar entrega es el error caro; reservar de más sólo es conservador.
DEFAULT_LANE = "growth"

# Qué proyectos consumen HORAS DE ENTREGA. El esfuerzo comercial y lo personal
# son trabajo real, pero no se pagan del mismo bolsillo: comercial tiene su
# propio carril de horas y lo personal no es capacidad de negocio. Meterlos en
# la misma lista hacía que 14 proyectos se repartieran un presupuesto que en
# realidad es de 9 — y que el ritual semanal pidiera declarar el Inbox.
#
# `kind` ya existía con exactamente estos cuatro valores. NULL cuenta como
# 'product', que es el DEFAULT de la columna: un proyecto sin clasificar es
# trabajo hasta que alguien diga lo contrario.
#
# CORRECCIÓN (antes decía aquí que "casi nadie lo lee", citando sprints.py:1474
# — era falso y lo probó un crítico): `sprints.py:1521` filtra el backlog de
# grooming del ciclo con `COALESCE(p.kind,'product') NOT IN ('personal',
# 'system','sales')`. O sea que cambiar el `kind` de un proyecto NO sólo lo saca
# de este presupuesto: le saca las tareas del backlog que el operador prioriza cada
# ciclo. Por eso `kind` dejó de ser un botón de esta tarjeta — un efecto de ese
# tamaño no puede vivir en un glifo que cicla y que además pasa por dos estados
# intermedios en el camino. Se edita donde vive lo que cambia una vez en la vida.
COUNTED_KINDS = ("product",)

# AUTOMEJORA — el quinto carril. Son horas reales y declaradas, pero de otra
# naturaleza: no las paga un cliente. Hermes Orchestrator cerró 21 tareas en 28
# días siendo el proyecto más activo del operador, y estaba declarado en 0h porque
# el único modo de sacarlo del presupuesto de entrega era mentir. Ahora tiene
# bolsillo propio.
#
# `self` NO entra en COUNTED_KINDS (no compite por el presupuesto de entrega),
# pero SÍ reclama del mismo pozo de 32h — porque las horas salen del mismo día.
# Y no entra en la lista negra de `sprints.py:1521`, así que sus tareas siguen
# apareciendo en el backlog de grooming del ciclo: separar las horas no es
# exiliar el trabajo.
SELF_KIND = "self"
KIND_LABEL = {"product": "producto", "sales": "comercial",
              "personal": "personal", "system": "sistema",
              "self": "automejora"}

# Peor gana, y `unknown` nunca gana: una mitad sin medir no puede teñir de gris
# una mitad que sí se midió en rojo.
_BAND_RANK = {"unknown": -1, "green": 0, "amber": 1, "red": 2}


def _worst_band(a: str, b: str) -> str:
    return a if _BAND_RANK.get(a, -1) >= _BAND_RANK.get(b, -1) else b

# Kingman 1961: espera relativa ρ/(1−ρ) → 2.33× en 0.70, 5.67× en 0.85.
BAND_GREEN = 0.70
BAND_AMBER = 0.85

# Un proyecto sin una sola tarea movida en esta ventana está inactivo. 28 días
# = cuatro semanas completas, para que un proyecto de cadencia mensual no se
# marque por su propio ritmo.
IDLE_DAYS = 28

# Los carriles agente/humano se derivan de columnas existentes. Un ejecutor
# desconocido cae SIEMPRE al carril del operador: lo no clasificado cuesta horas,
# nunca las regala.
AGENT_EXECUTORS = ("hermes", "claude", "codex")


def _minutes(hhmm: str) -> int:
    """'09:00' → 540. Un bloque ilegible vale 0 en vez de tumbar la lectura:
    esto alimenta una tarjeta, no una transacción."""
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def _epoch(ts) -> Optional[int]:
    """Los timestamps de kanban.db conviven en segundos y en milisegundos según
    quién los escribió. Normalizar a segundos aquí evita que una comparación de
    ventana lea 1970 y declare inactivo un proyecto vivo."""
    if ts is None:
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return ts // 1000 if ts > 1e11 else ts


def _band(util: Optional[float], complete: bool) -> str:
    """Verde solo si la medición está completa. Con proyectos sin dimensionar la
    suma es un piso, y un piso bajo no es evidencia de holgura."""
    if util is None:
        return "unknown"
    if util > BAND_AMBER:
        return "red"
    if util > BAND_GREEN:
        return "amber"
    return "green" if complete else "amber"


def _capacity(conn) -> dict:
    """Las horas de la semana, medidas de `time_blocks`, partidas por carril."""
    lanes = {"delivery": 0.0, "growth": 0.0, "admin": 0.0}
    roles = {"delivery": [], "growth": [], "admin": []}
    blocks = 0
    for row in conn.execute(
            "SELECT role, start_time, end_time FROM time_blocks WHERE active = 1"):
        hours = max(0, _minutes(row["end_time"]) - _minutes(row["start_time"])) / 60.0
        blocks += 1
        lane = ROLE_LANE.get(row["role"], DEFAULT_LANE)
        lanes[lane] += hours
        if row["role"] not in roles[lane]:
            roles[lane].append(row["role"])
    return {
        "week_hours": round(sum(lanes.values()), 2),
        "delivery_hours": round(lanes["delivery"], 2),
        "growth_reserved_hours": round(lanes["growth"], 2),
        "admin_reserved_hours": round(lanes["admin"], 2),
        "source": {"table": "time_blocks", "active_blocks": blocks,
                   "delivery_roles": sorted(roles["delivery"]),
                   "growth_roles": sorted(roles["growth"]),
                   "admin_roles": sorted(roles["admin"])},
    }


def _task_activity(conn, now: int) -> dict:
    """Por proyecto: tareas abiertas, cerradas en 28d, y el último latido.

    `last_touch` usa COALESCE(completed_at, started_at, created_at) porque
    `tasks.updated_at` NO EXISTE — leerlo era el bug de un diseño competidor.
    """
    d28 = now - IDLE_DAYS * 86400
    out = {}
    for row in conn.execute(
            "SELECT project_id, status, completed_at, started_at, created_at "
            "FROM tasks WHERE project_id IS NOT NULL AND archived_at IS NULL"):
        pid = row["project_id"]
        acc = out.setdefault(pid, {"open_tasks": 0, "done_28d": 0, "last_touch": None})
        if row["status"] not in ("done", "cancelled", "rejected"):
            acc["open_tasks"] += 1
        done = _epoch(row["completed_at"])
        if done is not None and done >= d28:
            acc["done_28d"] += 1
        touch = done or _epoch(row["started_at"]) or _epoch(row["created_at"])
        if touch is not None and (acc["last_touch"] is None or touch > acc["last_touch"]):
            acc["last_touch"] = touch
    return out


def _measured(conn, now: int) -> dict:
    """La mitad que ya es cierta sin que el operador teclee nada.

    Las ventanas son MÓVILES (últimos 7/28 días), no semanas ISO: la pregunta
    es "¿cuánto estoy cargando ahora?", y un lunes por la mañana una semana ISO
    contestaría casi cero. Se declara en `window` para que nadie lo confunda con
    el calendario.
    """
    d7, d28 = now - 7 * 86400, now - 28 * 86400
    touched_7 = touched_28 = 0
    for row in conn.execute(
            "SELECT project_id, MAX(COALESCE(completed_at, started_at, created_at)) AS t "
            "FROM tasks WHERE project_id IS NOT NULL AND archived_at IS NULL "
            "GROUP BY project_id"):
        t = _epoch(row["t"])
        if t is None:
            continue
        if t >= d7:
            touched_7 += 1
        if t >= d28:
            touched_28 += 1
    done_by_week = []
    for i in range(3, -1, -1):
        lo, hi = now - (i + 1) * 7 * 86400, now - i * 7 * 86400
        done_by_week.append(conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE completed_at IS NOT NULL "
            "AND CASE WHEN completed_at > 100000000000 THEN completed_at/1000 "
            "ELSE completed_at END >= ? AND CASE WHEN completed_at > 100000000000 "
            "THEN completed_at/1000 ELSE completed_at END < ?", (lo, hi)).fetchone()[0])
    return {"touched_projects_7d": touched_7, "touched_projects_28d": touched_28,
            "done_by_week": done_by_week,
            "window": "rolling", "note": "counts, not hours; newest last"}


def _lanes(conn) -> dict:
    """Carriles de ejecución sobre tareas abiertas, derivados de columnas que ya
    existen. Fail-closed en ambos sentidos: un `executor_kind` desconocido cae a
    `ricardo`, y un `autonomy` NULL nunca llega a `autonomous`."""
    counts = {"ricardo": 0, "supervised": 0, "autonomous": 0}
    for row in conn.execute(
            "SELECT executor_kind, autonomy FROM tasks WHERE archived_at IS NULL "
            "AND status NOT IN ('done','cancelled','rejected')"):
        if row["executor_kind"] in AGENT_EXECUTORS:
            counts["autonomous" if row["autonomy"] == "auto" else "supervised"] += 1
        else:
            counts["ricardo"] += 1
    return counts


def _agent_capacity(conn, lanes: dict) -> dict:
    """Horas acreditadas al carril autónomo. Hoy: 0.0, con la razón medida.

    Acreditar exige que el trabajo se pueda calificar SOLO (un `contract_cmd`
    que decida pasa/falla sin el operador). Mientras esa cuenta sea 0, cualquier
    número positivo sería inventado — y `autonomy='auto'` es un permiso, no una
    prueba de que algo funcione. Por eso el crédito NO se calcula desde el
    conteo de carriles: poner todas las tareas en `auto` deja esto en 0.0.
    """
    gradable = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE contract_cmd IS NOT NULL "
        "AND archived_at IS NULL").fetchone()[0]
    verified = conn.execute(
        "SELECT COUNT(DISTINCT task_id) FROM task_runs WHERE outcome = 'completed'"
    ).fetchone()[0] if _has_table(conn, "task_runs") else 0
    return {
        "credited_h": 0.0,
        "status": "unproven",
        "lane_counts": lanes,
        "gradable_tasks": gradable,
        "distinct_completed_runs": verified,
        "reason": (f"{gradable} tareas con contract_cmd — sin criterio automático de "
                   "pasa/falla no hay trabajo desatendido acreditable"),
    }


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def project_load(conn: Optional[sqlite3.Connection] = None,
                 now: Optional[int] = None) -> dict:
    """La carga de la cartera: declarada contra medida. Lectura pura."""
    own = conn is None
    conn = conn or get_conn()
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    try:
        now = now or int(datetime.datetime.now().timestamp())
        cap = _capacity(conn)
        activity = _task_activity(conn, now)

        week = datetime.datetime.fromtimestamp(now).strftime("%G-W%V")
        rows, inactive, committed = [], [], 0.0
        unsized = parked = committed_count = declared_week = outside = 0
        self_hours = 0.0
        self_count = self_committed = self_unsized = 0
        self_on = False
        # UNA sola forma de fila para todas las secciones. Antes había tres, con
        # campos y controles distintos según la lista en la que cayeras, y eso
        # obligaba a que "salir del presupuesto" fuera un cambio de LISTA — que
        # es exactamente por qué no se podía salir sin re-etiquetar el proyecto.
        # Ahora la sección se DERIVA de la fila, no al revés.
        for row in conn.execute(
                "SELECT id, name, slug, color, kind, tier, status, weekly_hours, "
                "weekly_hours_set_at FROM projects WHERE archived_at IS NULL "
                "ORDER BY name"):
            wh = row["weekly_hours"]
            act = activity.get(row["id"], {"open_tasks": 0, "done_28d": 0, "last_touch": None})
            kind = row["kind"] or "product"
            counts = kind in COUNTED_KINDS
            active = row["status"] == "active"
            last_touch = act["last_touch"]
            days = None if last_touch is None else int((now - last_touch) // 86400)
            set_at = _epoch(row["weekly_hours_set_at"])
            set_week = (None if set_at is None
                        else datetime.datetime.fromtimestamp(set_at).strftime("%G-W%V"))
            set_this_week = active and counts and set_week == week

            if wh is None:
                state = "unsized"
            elif wh <= 0:
                state = "parked"
            else:
                state = "committed"
            # `0h` ES "fuera del presupuesto esta semana". No hace falta columna
            # nueva: aparcar ya significaba eso y sólo faltaba que se LEYERA así.
            # Un `in_delivery` aparte sería un segundo escritor del mismo hecho,
            # con el estado imposible `in_delivery=1 ∧ weekly_hours=0` — el bug
            # de `kind`, una columna después.
            in_budget = active and counts and state != "parked"
            is_self = kind == SELF_KIND
            # Una fila de automejora SIEMPRE vive en su sección, encendida o no.
            # Si al apagarla se fuera a APAGADOS, el encabezado y su botón se
            # irían con ella y prender sería buscar cinco filas en un cementerio:
            # el interruptor volvería a ser de un solo sentido, que es el bug que
            # ya se arregló una vez.
            if is_self:
                bucket = "self"
            else:
                bucket = "off" if not active else ("budget" if in_budget else "outside")

            if active and counts:
                if state == "unsized":
                    unsized += 1
                elif state == "parked":
                    parked += 1
                else:
                    committed_count += 1
                    committed += float(wh)
                if set_this_week:
                    declared_week += 1
            if bucket == "outside":
                outside += 1
            if is_self:
                self_count += 1
                if active:
                    self_on = True
                    if state == "committed":
                        self_hours += float(wh)
                        self_committed += 1
                    elif state == "unsized":
                        self_unsized += 1

            item = {
                "id": row["id"], "name": row["name"], "slug": row["slug"],
                "color": row["color"], "kind": kind,
                "kind_label": KIND_LABEL.get(kind, kind), "counts": counts,
                "active": active, "status": row["status"], "tier": row["tier"],
                "weekly_hours": None if wh is None else float(wh), "state": state,
                "in_budget": in_budget, "bucket": bucket,
                "open_tasks": act["open_tasks"], "done_28d": act["done_28d"],
                "days_since_task": days,
                "set_this_week": set_this_week, "set_week": set_week,
                "set_days_ago": None if set_at is None else int((now - set_at) // 86400),
            }
            (inactive if bucket == "off" else rows).append(item)

        # El orden lo da el NÚMERO, no `tier`: priorizar es darle horas y
        # despriorizar es quitárselas, así que un proxy ordinal aparte sólo podía
        # contradecir al cardinal que además entra al denominador. Sin estimar
        # primero (son el pendiente del ritual), luego lo pesado, luego nombre.
        _B = {"budget": 0, "self": 1, "outside": 2}
        rows.sort(key=lambda p: (_B[p["bucket"]],
                                 0 if p["state"] == "unsized" else 1,
                                 -(p["weekly_hours"] or 0), p["name"]))
        inactive.sort(key=lambda p: p["name"])
        for i, p in enumerate(rows + inactive):
            p["sort"] = i
        projects = rows

        # `active_count` NO cambia de significado: sigue siendo "activos que se
        # pagan del presupuesto" y sigue siendo el denominador del ritual. Lo que
        # cambia es que ya no es `len(projects)`, porque `projects` ahora trae
        # también los activos de otro carril.
        active_count = unsized + parked + committed_count
        delivery = cap["delivery_hours"]
        util = (round(committed / delivery, 4)
                if committed_count > 0 and delivery > 0 else None)
        complete = active_count > 0 and unsized == 0
        # EL POZO. Entrega y automejora son dos reclamos sobre las MISMAS 32h,
        # porque las horas salen del mismo día. La utilización de entrega
        # conserva su numerador puro —es suya—, pero el COLOR de la tarjeta sale
        # del total: si no, un pozo lleno de automejora se leería verde mientras
        # el día ya no cabe. Es el mismo principio que el clamp de incompletitud.
        pool_hours = round(committed + self_hours, 2)
        util_total = (round(pool_hours / delivery, 4)
                      if pool_hours > 0 and delivery > 0 else None)
        complete_total = complete and self_unsized == 0
        total_band = _worst_band(_band(util, complete),
                                 _band(util_total, complete_total))
        lanes = _lanes(conn)
        return {
            "week": week,
            "capacity": cap,
            "declared": {
                "committed_hours": round(committed, 2), "utilization": util,
                "band": _band(util, complete), "complete": complete,
                "active_count": active_count, "committed_count": committed_count,
                "parked_count": parked, "unsized_count": unsized,
                # Fuera del presupuesto ESTA SEMANA: los aparcados en 0 h más los
                # activos de otro carril. Es el número que el operador pidió ver.
                "outside_count": outside,
                # El ritual: cuántos proyectos llevan reparto DE ESTA SEMANA, y
                # cuántos siguen con el de una semana pasada o sin ninguno. Con
                # cero activos no hay ritual pendiente — no hay nada que repartir.
                "declared_this_week": declared_week,
                "pending_this_week": active_count - declared_week,
                "ritual_due": active_count > 0 and declared_week < active_count,
            },
            "measured": _measured(conn, now),
            "agent_capacity": _agent_capacity(conn, lanes),
            "projects": projects,
            # Activos que NO se pagan del presupuesto de entrega. Visibles a
            # propósito: un proyecto excluido y en silencio es exactamente
            # como se acumulan catorce.
            # AUTOMEJORA — su propio KPI. `on` es derivado: el carril está
            # prendido si alguna de sus filas lo está. Cero columnas, cero
            # `orch_meta`, cero semana ISO guardada.
            "self_lane": {
                "on": self_on, "hours": round(self_hours, 2),
                "count": self_count, "committed_count": self_committed,
                "unsized_count": self_unsized,
                "share": (round(self_hours / delivery, 4)
                          if self_hours > 0 and delivery > 0 else None),
            },
            # El pozo combinado. La tarjeta y la barra se pintan de AQUÍ.
            "total": {
                "claimed_hours": pool_hours, "utilization": util_total,
                "complete": complete_total, "band": total_band,
            },
            # Los apagados viajan en la MISMA respuesta que los encendidos para
            # que el interruptor funcione en los dos sentidos desde una sola
            # pantalla. Los archivados no: archivar es la salida definitiva y
            # tiene su propio verbo.
            "inactive": {
                "count": len(inactive),
                "by_status": dict(collections.Counter(
                    p["status"] or "sin estado" for p in inactive)),
                "projects": inactive,
            },
        }
    finally:
        if own:
            conn.close()
