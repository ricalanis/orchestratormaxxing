"""La superficie de revisión: el clasificador propone por carriles, el operador decide.

Tres decisiones de diseño, y las tres existen para que aprobar en bloque sea
*seguro*, no nada más rápido:

**El carril es la unidad de consentimiento, no la fila.** Una lista de 1143
renglones ordenada por confianza es una máquina de sellar: filas idénticas,
certeza que baja despacio, el mismo gesto repetido. Aquí cada carril tiene una
regla distinta, escrita en español, y solo el carril determinista se puede
aprobar completo.

**El bloque opera sobre una REGLA, nunca sobre una selección.** "Permitir los 9
chats con el patrón «A <> B»" se puede volver a derivar y queda escrito en la
decisión; "permitir los 9 que seleccioné" no. Dentro de seis meses, la pregunta
"¿por qué este chat está autorizado?" tiene respuesta.

**Se confirma contra un lote fijado en el servidor.** El navegador manda un
`batch_id`, no una lista. Una confirmación que reenvía su propio arreglo puede
traer algo que la vista previa nunca enseñó — lo que viste es lo que autorizas.

Y la regla que atraviesa todo el archivo: **de aquí no sale texto de ningún
mensaje.** Ni en una vista previa, ni en un "asomarse". Enseñar el contenido para
decidir si se puede leer el contenido es el círculo que este sistema existe para
no cerrar. Se decide con forma (cuánto, cuándo, con quién), no con fondo.
"""
import json
import secrets
import sqlite3
import time
from typing import Optional

from . import db
from . import whatsapp as wa

# Un lote de este tamaño se revisa de una sentada; más allá, la atención se
# agota y la aprobación se vuelve un barrido. Se revisa en tandas.
MAX_LOTE = 25
# Cuántos renglones se muestran al azar en la vista previa. Cambian en cada
# apertura, para que el diálogo no se pueda contestar de memoria.
MUESTRA = 3
LOTE_TTL = 900

# Los carriles deterministas: su veredicto sale de un empate verificable, no de
# una inferencia. Son los únicos que se pueden autorizar en bloque.
CARRILES_DETERMINISTAS = ("patron_b2b", "crm", "nombre_entidad")

_lotes: dict = {}


def _now() -> int:
    return int(time.time())


def _mirror():
    return sqlite3.connect(f"file:{wa.WACLI_DB}?mode=ro", uri=True)


def _forma(mc, jids: list) -> dict:
    """Forma de cada chat: cuánto, cuándo, qué tanto contesta. Puros conteos.

    Se calcula al vuelo contra el espejo y **no se guarda** — igual que el nombre
    en `list_chats`. Persistir esto sería empezar a construir un perfil de
    conversaciones que nadie autorizó."""
    if not jids:
        return {}
    ahora = _now()
    q = ",".join("?" * len(jids))
    filas = mc.execute(
        f"SELECT chat_jid, count(*), sum(from_me), "
        f"max(CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END), "
        f"min(CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END), "
        f"sum(CASE WHEN CAST(strftime('%w', CASE WHEN ts > 100000000000 THEN ts/1000 "
        f"  ELSE ts END, 'unixepoch', 'localtime') AS INTEGER) IN (0,6) THEN 1 ELSE 0 END) "
        f"FROM messages WHERE chat_jid IN ({q}) GROUP BY chat_jid", jids).fetchall()
    out = {}
    for jid, n, mios, ult, prim, finde in filas:
        mios, finde = mios or 0, finde or 0
        out[jid] = {
            "mensajes": n,
            "mios_pct": round(100 * mios / n) if n else 0,
            "finde_pct": round(100 * finde / n) if n else 0,
            "dias_sin": round((ahora - ult) / 86400) if ult else None,
            "antiguedad_dias": round((ahora - prim) / 86400) if prim else None,
        }
    return out


def _contraevidencia(f: dict, is_group: int) -> list:
    """Lo que argumenta EN CONTRA de autorizar.

    Una vista previa que solo acumula razones para decir que sí entrena el sello
    automático. Un chat de fin de semana o dormido hace un año merece que la duda
    aparezca en la misma línea, no en la letra chica."""
    if not f:
        return ["sin mensajes espejados todavía — no hay con qué juzgar la forma"]
    avisos = []
    if f["finde_pct"] >= 40:
        avisos.append(f"{f['finde_pct']}% de su actividad cae en fin de semana")
    if f["dias_sin"] is not None and f["dias_sin"] > 120:
        avisos.append(f"sin actividad hace {f['dias_sin']} días")
    if f["mensajes"] >= 20 and f["mios_pct"] < 15:
        avisos.append(f"casi no respondes ({f['mios_pct']}% tuyo)")
    if f["mensajes"] < 5:
        avisos.append(f"solo {f['mensajes']} mensajes espejados")
    return avisos


def review(conn=None, limit_por_carril: int = 200) -> dict:
    """Los carriles, con su regla escrita y su contra-evidencia.

    Lo denegado no se pone en cola: se cuenta. Volver "1143 decisiones
    pendientes" en "1100 siguen denegados, no se leen, no tienes que hacer nada"
    es lo que evita que esto se vuelva una tarea eterna."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        filas = conn.execute(
            "SELECT jid, chat_name, is_group, allowed, verdict, verdict_source, "
            "verdict_reason, decided_at FROM whatsapp_chats "
            "WHERE verdict = 'negocio' AND allowed = 0 AND decided_at IS NULL "
            "ORDER BY chat_name LIMIT ?", (limit_por_carril * 4,)).fetchall()

        mc = _mirror()
        try:
            formas = _forma(mc, [r[0] for r in filas])
        finally:
            mc.close()

        carriles = {}
        for jid, name, grupo, _, _, src, reason, _ in filas:
            f = formas.get(jid, {})
            carriles.setdefault(src, []).append({
                "jid": jid, "nombre": name, "grupo": bool(grupo),
                "motivo": reason, "forma": f,
                "contra": _contraevidencia(f, grupo),
            })
        for v in carriles.values():
            v.sort(key=lambda r: (r["forma"].get("dias_sin") if r["forma"] else 9999))

        decididos = conn.execute(
            "SELECT count(*) FROM whatsapp_chats WHERE decided_at IS NOT NULL").fetchone()[0]
        permitidos = conn.execute(
            "SELECT count(*) FROM whatsapp_chats WHERE allowed = 1").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM whatsapp_chats").fetchone()[0]
        pend_clasif = conn.execute(
            "SELECT count(*) FROM whatsapp_chats WHERE verdict IS NULL").fetchone()[0]

        orden = ["patron_b2b", "crm", "nombre_entidad", "modelo"]
        reglas = {
            "patron_b2b": "el nombre usa tu convención «A <> B» para grupos con cliente",
            "crm": "el nombre del chat coincide con un contacto de tu CRM",
            "nombre_entidad": "el nombre menciona una cuenta, proyecto o deal tuyo",
            "modelo": "un modelo leyó el nombre y le pareció de negocio (inferencia)",
        }
        return {
            "status": "ok",
            "carriles": [{
                "id": k, "regla": reglas.get(k, k),
                "determinista": k in CARRILES_DETERMINISTAS,
                "chats": carriles.get(k, [])[:limit_por_carril],
                "total": len(carriles.get(k, [])),
            } for k in orden if carriles.get(k)],
            "resumen": {
                "total": total, "permitidos": permitidos, "decididos": decididos,
                "denegados_en_silencio": total - decididos,
                "pendientes_de_clasificar": pend_clasif,
            },
            "max_lote": MAX_LOTE,
        }
    finally:
        if own:
            conn.close()


def stage(carril: str, conn=None) -> dict:
    """Fija un lote en el servidor y devuelve qué contiene. **No autoriza nada.**

    Lo que se confirma después es este `batch_id`, no un arreglo que el navegador
    reconstruya: así lo que se autorizó es exactamente lo que se enseñó."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        if carril not in CARRILES_DETERMINISTAS:
            # Una inferencia sobre un nombre no alcanza para autorizar en bloque.
            # Ese carril se revisa de uno en uno o no se revisa.
            return {"status": "error", "error": "carril_no_masivo",
                    "detalle": f"«{carril}» es inferencia; se autoriza chat por chat"}
        r = review(conn=conn)
        via = next((c for c in r["carriles"] if c["id"] == carril), None)
        if not via or not via["chats"]:
            return {"status": "error", "error": "carril_vacio"}

        elegidos = via["chats"][:MAX_LOTE]
        bid = "lote_" + secrets.token_hex(6)
        _lotes[bid] = {"jids": [c["jid"] for c in elegidos], "carril": carril,
                       "regla": via["regla"], "creado": _now()}
        # La muestra se sortea aquí y se rehace en cada apertura: un diálogo que
        # siempre enseña lo mismo se contesta sin leerlo.
        muestra = ([elegidos[i] for i in
                    sorted(secrets.SystemRandom().sample(range(len(elegidos)),
                                                         min(MUESTRA, len(elegidos))))])
        return {"status": "ok", "batch_id": bid, "carril": carril, "regla": via["regla"],
                "cuantos": len(elegidos), "restantes": max(0, via["total"] - len(elegidos)),
                "muestra": muestra, "chats": elegidos}
    finally:
        if own:
            conn.close()


def unstage(batch_id: str, jid: str) -> dict:
    """Quitar un chat del lote antes de confirmar. Solo se puede QUITAR: el lote
    se compone por regla, y agregarle algo a mano rompería la trazabilidad."""
    lote = _lotes.get(batch_id)
    if not lote:
        return {"status": "error", "error": "lote_desconocido"}
    if jid in lote["jids"]:
        lote["jids"].remove(jid)
    return {"status": "ok", "batch_id": batch_id, "cuantos": len(lote["jids"])}


def commit(batch_id: str, conn=None) -> dict:
    """Autoriza el lote fijado. Aquí sí se escribe `allowed`, y solo aquí.

    Cada chat queda con la regla como `decided_by`, así que la autorización trae
    consigo por qué se dio."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        lote = _lotes.get(batch_id)
        if not lote:
            return {"status": "error", "error": "lote_desconocido",
                    "detalle": "expiró o el servicio reinició; vuelve a armarlo"}
        if _now() - lote["creado"] > LOTE_TTL:
            _lotes.pop(batch_id, None)
            return {"status": "error", "error": "lote_expirado"}
        if len(lote["jids"]) > MAX_LOTE:
            return {"status": "error", "error": "lote_excedido"}

        # Re-derivar el carril antes de aplicar. Entre fijar el lote y confirmarlo
        # el mundo se pudo mover — el operador pudo negar uno desde otra pestaña, o un
        # chat pudo dejar de calificar. Aplicar la foto vieja autorizaría algo que
        # ya se había decidido que no. Si cambió, no se aplica nada: se vuelve a
        # enseñar.
        vigentes = {c["jid"] for via in review(conn=conn)["carriles"]
                    if via["id"] == lote["carril"] for c in via["chats"]}
        fuera = [j for j in lote["jids"] if j not in vigentes]
        if fuera:
            _lotes.pop(batch_id, None)
            return {"status": "error", "error": "lote_desfasado",
                    "cambiaron": fuera,
                    "detalle": "algo cambió desde que armaste el lote; revísalo de nuevo"}

        hechos = []
        for jid in lote["jids"]:
            wa.set_chat_allowed(jid, True, by=f"regla:{lote['carril']}", conn=conn)
            hechos.append(jid)
        _lotes.pop(batch_id, None)
        return {"status": "ok", "permitidos": hechos, "cuantos": len(hechos),
                "regla": lote["regla"]}
    finally:
        if own:
            conn.close()


def decide(jid: str, allowed: bool, purgar: bool = True, conn=None) -> dict:
    """Decisión individual. `decided_at` la marca como resuelta, así que un chat
    denegado a mano no vuelve a salir en la cola — decir que no una vez es
    suficiente.

    Quitar el permiso **borra por omisión lo crudo que quedó sin digerir**. El
    verbatim solo existe para ser digerido; retirado el permiso deja de tener
    razón de estar guardado, y una revocación que conserva el texto es una que
    solo apaga la llave hacia adelante. Lo ya asimilado (objetivos, sugerencias)
    no se toca: eso es trabajo del operador, no material en bruto."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        res = wa.set_chat_allowed(jid, allowed, by="humano", conn=conn)
        if not allowed and purgar:
            # Se vacía el contenido y se deja la fila: queda constancia de que algo
            # se capturó y de cuándo se purgó, sin conservar el texto. Borrar la
            # fila entera destruiría también el rastro de la revocación.
            cur = conn.execute(
                "UPDATE capture_events SET payload = NULL, payload_purged_at = ? "
                "WHERE source_kind = 'whatsapp' AND source_ref = ? "
                "AND payload IS NOT NULL", (_now(), jid))
            conn.commit()
            res["crudo_purgado"] = cur.rowcount
        return res
    finally:
        if own:
            conn.close()


def sweep_pending(conn=None) -> dict:
    """Saca del tracker todo lo que sigue sin decidir. Deniega; nunca autoriza.

    Es el primer paso natural de la revisión y por eso existe como verbo: una
    cola de mil renglones se despacha tirando lo que no va, no evaluando uno por
    uno hasta que la atención se acaba — y una atención acabada es la que aprueba
    de corrido.

    Va en la dirección barata: denegar no lee nada y se deshace con un clic,
    mientras que autorizar de más entrega conversaciones que ya no se
    des-entregan. Por eso este verbo no tiene gemelo que autorice en bloque todo
    lo pendiente, y no lo va a tener.

    Los ya decididos no se tocan: quien fue autorizado sigue autorizado.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        now = _now()
        cur = conn.execute(
            "UPDATE whatsapp_chats SET allowed = 0, decided_at = ?, decided_by = 'barrido' "
            "WHERE decided_at IS NULL", (now,))
        conn.commit()
        quedan = conn.execute(
            "SELECT count(*) FROM whatsapp_chats WHERE allowed = 1").fetchone()[0]
        return {"status": "ok", "fuera": cur.rowcount, "siguen_dentro": quedan}
    finally:
        if own:
            conn.close()


# --------------------------------------------------- bajar historial (SELECT)

# Ninguna decisión de este archivo es tan poco reversible como ésta, así que el
# tope existe para que "bajar historial" nunca signifique "todo lo que exista".
BACKFILL_MAX_DIAS = 180


def _ventanas_historicas(jid: str, desde: int, hasta: int) -> list:
    """Parte el historial en las MISMAS ventanas que produciría la captura viva.

    El corte lo define el silencio, no un reloj: si se troceara por día o por
    tamaño, una conversación quedaría partida a mitad y el turno perdería el
    contexto que hace reconocible un compromiso. Reusar el mismo criterio es
    además lo que hace que un evento traído del pasado sea indistinguible de uno
    capturado en vivo — mismo id determinista, misma idempotencia.
    """
    mc = _mirror()
    try:
        filas = mc.execute(
            "SELECT CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END AS t "
            "FROM messages WHERE chat_jid = ? AND COALESCE(text,'') != '' "
            "AND COALESCE(revoked,0) = 0 AND COALESCE(deleted_for_me,0) = 0 "
            "AND (CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END) BETWEEN ? AND ? "
            "ORDER BY t", (jid, desde, hasta)).fetchall()
    except sqlite3.Error:
        # `None`, no `[]`. Un espejo que no se pudo leer no es un chat sin
        # historial: devolver lista vacía haría que "no pude medir" se lea como
        # "no hay nada que bajar", y el operador decidiría sobre un cero inventado.
        return None
    finally:
        mc.close()
    if not filas:
        return []
    hueco = wa.WINDOW_GAP_SECONDS
    ventanas, ini, prev, n = [], filas[0][0], filas[0][0], 0
    for (t,) in filas:
        if t - prev > hueco:
            ventanas.append({"start": ini, "end": prev, "mensajes": n})
            ini, n = t, 0
        prev, n = t, n + 1
    ventanas.append({"start": ini, "end": prev, "mensajes": n})
    return [v for v in ventanas if v["mensajes"] >= wa.MIN_MESSAGES_PER_WINDOW]


def backfill(jid: str, dias: int = 30, confirmar: bool = False, conn=None) -> dict:
    """Baja el historial ANTERIOR al permiso. Propone; solo escribe si se confirma.

    Autorizar un chat es "de aquí en adelante" — este verbo es la única forma de
    mover ese piso hacia atrás, y por eso es la operación más cara de deshacer en
    todo el sistema: leer no se deshace. Tres candados, en orden:

    1. **Solo sobre lo ya autorizado.** No se puede bajar el historial de un chat
       que nadie permitió; eso sería autorizar por la puerta de atrás.
    2. **Un chat a la vez.** No hay versión masiva, a propósito: el bloque existe
       para decisiones que se parecen entre sí, y "cuánto de tu pasado entrego"
       no se parece de un chat a otro.
    3. **Previsualiza por omisión.** Sin `confirmar` devuelve cuántos mensajes y
       de qué fechas, sin escribir nada. La decisión se toma con el número
       enfrente, no con una estimación.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        row = conn.execute(
            "SELECT allowed, chat_name, decided_at, backfill_from FROM whatsapp_chats "
            "WHERE jid = ?", (jid,)).fetchone()
        if not row:
            return {"status": "error", "error": "chat_desconocido"}
        allowed, nombre, decided_at, ya_bajado = row[0], row[1], row[2], row[3]
        if not allowed:
            return {"status": "error", "error": "chat_no_autorizado",
                    "detalle": "primero autorízalo; bajar su pasado no puede ser la puerta de entrada"}
        dias = max(1, min(int(dias), BACKFILL_MAX_DIAS))

        ahora = _now()
        # El techo es el permiso: de ahí en adelante ya lo cubre la captura viva.
        hasta = decided_at or ahora
        desde = ahora - dias * 86400
        # Lo ya bajado no se vuelve a ofrecer: sin esto, cada corrida "encontraría"
        # otra vez el mismo historial y parecería que hay más por decidir.
        if ya_bajado:
            hasta = min(hasta, ya_bajado)
        if desde >= hasta:
            return {"status": "ok", "nada_que_bajar": True, "jid": jid, "nombre": nombre,
                    "detalle": "ese rango ya está cubierto"}

        ventanas = _ventanas_historicas(jid, desde, hasta)
        if ventanas is None:
            return {"status": "error", "error": "espejo_ilegible", "jid": jid,
                    "detalle": "no se pudo leer el espejo; esto NO significa que "
                               "no haya historial"}
        total = sum(v["mensajes"] for v in ventanas)
        propuesta = {
            "status": "ok", "jid": jid, "nombre": nombre, "dias": dias,
            "desde": desde, "hasta": hasta,
            "ventanas": len(ventanas), "mensajes": total,
        }
        if not confirmar:
            # Nada escrito. Esto es lo que se le enseña al operador para decidir.
            return {**propuesta, "confirmado": False,
                    "detalle": f"{total} mensajes en {len(ventanas)} conversaciones — "
                               f"nada se ha leído todavía"}

        from . import digestion as dg
        creados = []
        for v in ventanas:
            payload = wa.read_window(jid, v["start"], v["end"])
            if payload is None:
                continue
            eid = dg.event_id_for("whatsapp", jid, v["start"], v["end"])
            ek, ei = dg.resolve_entity_from_signals(
                conn, {"text": payload.get("title") or "", "identities": [jid.split("@")[0]]})
            conn.execute(
                "INSERT OR IGNORE INTO capture_events (event_id, source_kind, source_ref, title, "
                "window_start, window_end, occurred_at, captured_at, payload, entity_kind, "
                "entity_id) VALUES (?, 'whatsapp', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, jid, payload.get("title"), v["start"], v["end"], v["end"], ahora,
                 json.dumps(payload, ensure_ascii=False), ek, ei))
            creados.append(eid)
        conn.execute(
            "UPDATE whatsapp_chats SET backfill_from = ?, backfill_at = ? WHERE jid = ?",
            (min(desde, ya_bajado or desde), ahora, jid))
        conn.commit()
        return {**propuesta, "confirmado": True, "eventos": len(creados)}
    finally:
        if own:
            conn.close()


# Los denegados son mil y pico: se recortan y se buscan en el servidor. Los
# autorizados son decenas y caben enteros — el tope no los toca, y por eso la
# misma función sirve para los dos lados sin volverse dos.
LISTA_TOPE = 60


def listed_chats(allowed: bool, q: str = "", limit: int = LISTA_TOPE, conn=None) -> dict:
    """Un lado u otro de la decisión, buscable por nombre.

    Existe en los DOS sentidos a propósito. Una lista de permisos que no se puede
    ver completa es una que nadie revoca; y una de denegados que no se puede
    buscar convierte un "no" apurado en definitivo, porque encontrar ese chat
    entre mil para reconsiderarlo deja de ser posible. Ambas direcciones tienen
    que ser transitables o la decisión no es realmente reversible.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        args = [1 if allowed else 0]
        # Solo lo YA decidido: lo que sigue en la cola vive en los carriles, y
        # mezclarlo aquí lo haría parecer resuelto.
        sql = ("SELECT jid, chat_name, is_group, decided_at, decided_by, verdict_reason "
               "FROM whatsapp_chats WHERE allowed = ? AND decided_at IS NOT NULL")
        if q.strip():
            sql += " AND lower(COALESCE(chat_name,'')) LIKE ?"
            args.append(f"%{q.strip().lower()}%")
        total = conn.execute(sql.replace(
            "SELECT jid, chat_name, is_group, decided_at, decided_by, verdict_reason",
            "SELECT count(*)", 1), args).fetchone()[0]
        filas = conn.execute(sql + " ORDER BY decided_at DESC LIMIT ?",
                             args + [max(1, limit)]).fetchall()
        mc = _mirror()
        try:
            formas = _forma(mc, [r[0] for r in filas])
        finally:
            mc.close()
        return {
            "status": "ok", "total": total, "mostrados": len(filas), "q": q,
            "chats": [{"jid": j, "nombre": n, "grupo": bool(g), "desde": d, "por": por,
                       "motivo": mot, "forma": formas.get(j, {})}
                      for j, n, g, d, por, mot in filas],
        }
    finally:
        if own:
            conn.close()


def allowed_chats(conn=None) -> list:
    """Lo que hoy está autorizado, para poder quitarlo."""
    return listed_chats(True, limit=10000, conn=conn)["chats"]
