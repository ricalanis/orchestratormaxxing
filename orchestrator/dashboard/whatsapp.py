"""WhatsApp: pulso de actividad, allowlist, y cosecha de ventanas de conversación.

La mitad de WhatsApp del loop diferencial. Todo lo que hace es alimentar el MISMO
`capture_events` que Fireflies, con las mismas reglas — la digestión, el gate de
citas, los objetivos y las tarjetas no saben de dónde vino el evento y no
necesitan saberlo.

**Nunca escribe hacia WhatsApp.** Ni siquiera puede: el espejo se abre con
`mode=ro` y el único binario que toca la red corre en su propio servicio con
`sync`, que no envía. `tests/test_whatsapp_readonly.py` lo sostiene por lint.

La asimetría que gobierna la privacidad aquí:

  * el **pulso** (`record_activity`) se guarda para TODOS los chats, y no lleva
    una sola palabra: solo "este chat se movió a esta hora";
  * el **contenido** se lee del espejo solo cuando la ventana cerró y solo si el
    chat está permitido.

Así, un chat que el operador nunca autorice jamás deja texto en nuestra base — y eso
no depende de acordarse de filtrarlo después, sino de nunca haberlo escrito.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import db
from .migrations.m20_whatsapp_allowlist import WINDOW_GAP_SECONDS

WACLI_STORE = Path(os.environ.get("WACLI_STORE_DIR", str(Path.home() / ".wacli")))
WACLI_DB = WACLI_STORE / "wacli.db"
# Un mensaje suelto rara vez es un compromiso; una ventana con contexto sí. Y una
# ventana enorme satura la entrada del turno.
MIN_MESSAGES_PER_WINDOW = int(os.environ.get("WHATSAPP_MIN_MESSAGES", "3"))
MAX_MESSAGES_PER_WINDOW = int(os.environ.get("WHATSAPP_MAX_MESSAGES", "300"))


def _now() -> int:
    return int(time.time())


def _mirror():
    """El espejo de wacli, SIEMPRE en solo-lectura.

    `mode=ro` no es decoración: es lo que hace imposible que un bug nuestro
    corrompa el store que sostiene la sesión emparejada. Sin `immutable`, porque
    el demonio escribe en WAL mientras leemos.
    """
    if not WACLI_DB.exists():
        return None
    return sqlite3.connect(f"file:{WACLI_DB}?mode=ro", uri=True)


# ------------------------------------------------------- pulso (sin contenido)

def record_activity(jid: str, conn=None) -> dict:
    """Anota que un chat se movió. Cero contenido, a propósito."""
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        conn.execute(
            "INSERT INTO whatsapp_activity (jid, last_seen_at, message_count, window_open_at) "
            "VALUES (?,?,1,?) ON CONFLICT(jid) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, "
            "message_count = whatsapp_activity.message_count + 1, "
            "window_open_at = COALESCE(whatsapp_activity.window_open_at, excluded.window_open_at)",
            (jid, now, now))
        # Alta pasiva en la allowlist: aparece en la lista para que el operador pueda
        # decidir, y nace DENEGADO. Ver el chat no es leerlo.
        conn.execute(
            "INSERT OR IGNORE INTO whatsapp_chats (jid, allowed, is_group, created_at) "
            "VALUES (?, 0, ?, ?)", (jid, 1 if jid.endswith("@g.us") else 0, now))
        conn.commit()
        return {"status": "ok", "jid": jid}
    finally:
        if own:
            conn.close()


def reconcile_activity(conn=None, now: Optional[int] = None, limit: int = 5000) -> dict:
    """El pulso reconstruido desde el espejo, para cuando el webhook no llega.

    El webhook es el camino rápido, no el único — igual que en Fireflies, donde
    el poll existe porque un aviso perdido no puede perder el evento. Aquí hizo
    falta de verdad: se midieron 29 mensajes espejados y cero pulsos, con wacli
    en silencio después de que un 429 nuestro tumbara sus primeros intentos. El
    síntoma de un webhook caído es indistinguible de «nadie te escribió», así que
    un sistema que solo lo tenga a él se queda callado y parece tranquilo.

    Reconcilia contra el ESPEJO, que es la fuente de verdad y ya está al día: se
    marca hasta qué instante se leyó y solo se mira lo posterior. Sigue sin tocar
    contenido — cuenta mensajes por chat, ni una palabra — así que un chat no
    autorizado deja aquí exactamente lo mismo que dejaría por el webhook: un
    pulso, nada más.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now() if now is None else now
    try:
        fila = conn.execute(
            "SELECT last_seen_ts FROM capture_watermarks WHERE source_kind = 'whatsapp'"
        ).fetchone()
        # Sin marca previa se arranca en AHORA, no en el principio de los tiempos:
        # la primera corrida no puede convertir años de espejo en un pulso masivo.
        desde = fila[0] if fila and fila[0] else now

        mirror = _mirror()
        if mirror is None:
            return {"status": "error", "error": "espejo_ilegible", "detalle":
                    "no se pudo leer; esto NO significa que no haya mensajes"}
        try:
            filas = mirror.execute(
                "SELECT chat_jid, count(*), max(CASE WHEN ts > 100000000000 THEN ts/1000 "
                "ELSE ts END), min(CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END) "
                "FROM messages WHERE "
                "(CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END) > ? "
                "AND (CASE WHEN ts > 100000000000 THEN ts/1000 ELSE ts END) <= ? "
                "AND COALESCE(revoked,0) = 0 AND COALESCE(deleted_for_me,0) = 0 "
                "GROUP BY chat_jid LIMIT ?", (desde, now, limit)).fetchall()
        except Exception:
            return {"status": "error", "error": "espejo_ilegible"}
        finally:
            mirror.close()

        tope = desde
        for jid, cuantos, ultimo, primero in filas:
            # La ventana la abre el PRIMER mensaje del lote, no el último. Por el
            # webhook eso sale gratis (llega uno por uno y COALESCE conserva el
            # primero); aquí se agrupa, así que hay que decirlo — con el último,
            # la ventana nace vacía y la cosecha la descarta por corta.
            conn.execute(
                "INSERT INTO whatsapp_activity (jid, last_seen_at, message_count, window_open_at) "
                "VALUES (?,?,?,?) ON CONFLICT(jid) DO UPDATE SET "
                "last_seen_at = max(whatsapp_activity.last_seen_at, excluded.last_seen_at), "
                "message_count = whatsapp_activity.message_count + excluded.message_count, "
                "window_open_at = COALESCE(whatsapp_activity.window_open_at, excluded.window_open_at)",
                (jid, ultimo, cuantos, primero))
            conn.execute(
                "INSERT OR IGNORE INTO whatsapp_chats (jid, allowed, is_group, created_at) "
                "VALUES (?, 0, ?, ?)", (jid, 1 if jid.endswith("@g.us") else 0, now))
            tope = max(tope, ultimo)
        conn.execute(
            "INSERT INTO capture_watermarks (source_kind, last_seen_ts, last_run_at) "
            "VALUES ('whatsapp', ?, ?) ON CONFLICT(source_kind) DO UPDATE SET "
            "last_seen_ts = excluded.last_seen_ts, last_run_at = excluded.last_run_at",
            (tope, now))
        conn.commit()
        return {"status": "ok", "chats": len(filas),
                "mensajes": sum(f[1] for f in filas), "hasta": tope}
    finally:
        if own:
            conn.close()


# ----------------------------------------------------------------- allowlist

def list_chats(only_active: bool = True, limit: int = 200, conn=None) -> list:
    """Los chats que el operador puede autorizar, con su nombre tomado del espejo.

    El nombre se lee del espejo al vuelo y no se guarda: es dato de WhatsApp, no
    nuestro, y copiarlo sería empezar a acumular lo que este diseño evita.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        where = "WHERE a.jid IS NOT NULL" if only_active else ""
        rows = conn.execute(f"""
            SELECT c.jid, c.allowed, c.is_group, a.last_seen_at, a.message_count
            FROM whatsapp_chats c LEFT JOIN whatsapp_activity a ON a.jid = c.jid
            {where} ORDER BY COALESCE(a.last_seen_at, 0) DESC LIMIT ?""", (limit,)).fetchall()
        names = {}
        mirror = _mirror()
        if mirror is not None:
            try:
                for jid, name in mirror.execute("SELECT jid, name FROM chats"):
                    names[jid] = name
            except Exception:
                pass
            finally:
                mirror.close()
        return [{"jid": r[0], "allowed": bool(r[1]), "is_group": bool(r[2]),
                 "last_seen_at": r[3], "message_count": r[4] or 0,
                 "name": names.get(r[0])} for r in rows]
    finally:
        if own:
            conn.close()


def set_chat_allowed(jid: str, allowed: bool, by: str = "dashboard", conn=None) -> dict:
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        cur = conn.execute(
            "UPDATE whatsapp_chats SET allowed = ?, decided_at = ?, decided_by = ? WHERE jid = ?",
            (1 if allowed else 0, now, by, jid))
        if not cur.rowcount:
            conn.execute(
                "INSERT INTO whatsapp_chats (jid, allowed, is_group, decided_at, decided_by, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (jid, 1 if allowed else 0, 1 if jid.endswith("@g.us") else 0, now, by, now))
        conn.commit()
        return {"status": "ok", "jid": jid, "allowed": bool(allowed)}
    finally:
        if own:
            conn.close()


def is_allowed(conn, jid: str) -> bool:
    """Default-deny: sin fila, o con `allowed=0`, no se lee. Nunca se pregunta."""
    row = conn.execute("SELECT allowed FROM whatsapp_chats WHERE jid = ?", (jid,)).fetchone()
    return bool(row and row[0])


# --------------------------------------------------- cosecha de ventanas

def closed_windows(conn, now: Optional[int] = None) -> list:
    """Chats PERMITIDOS cuya ventana ya cerró (silencio > el hueco configurado).

    El silencio es lo que define el corte, no un reloj fijo: una conversación
    sigue siendo una conversación aunque dure tres horas, y partirla a mitad le
    quita al turno el contexto que hace reconocible un compromiso.
    """
    now = _now() if now is None else now
    return [dict(jid=r[0], window_open_at=r[1], last_seen_at=r[2], message_count=r[3],
                 decided_at=r[4])
            for r in conn.execute(
                "SELECT a.jid, a.window_open_at, a.last_seen_at, a.message_count, c.decided_at "
                "FROM whatsapp_activity a JOIN whatsapp_chats c ON c.jid = a.jid "
                "WHERE c.allowed = 1 AND a.last_seen_at < ? "
                "AND (a.harvested_at IS NULL OR a.harvested_at < a.last_seen_at) "
                "ORDER BY a.last_seen_at", (now - WINDOW_GAP_SECONDS,))]


def read_window(jid: str, start: int, end: int) -> Optional[dict]:
    """El texto de una ventana, leído del espejo en solo-lectura.

    Devuelve la MISMA forma que `build_event_payload` produce para Fireflies —
    `sentences` con hablante y tiempo — para que la digestión no tenga que
    distinguir el origen. Un mensaje de WhatsApp es una oración con hablante; esa
    equivalencia es lo que deja reusar el turno, el gate de citas y las tarjetas
    sin una sola rama por fuente.
    """
    mirror = _mirror()
    if mirror is None:
        return None
    try:
        # Columnas verificadas contra el espejo vivo (v0.15.2): `ts`, no
        # `timestamp`; y `sender_name` da un hablante legible sin exponer el
        # número. `revoked`/`deleted_for_me` se excluyen: un mensaje que el operador
        # o su interlocutor borraron no debe resucitar como evidencia de una
        # tarjeta.
        rows = mirror.execute(
            "SELECT ts, COALESCE(NULLIF(sender_name,''), sender_jid), from_me, "
            "COALESCE(NULLIF(display_text,''), text) FROM messages "
            "WHERE chat_jid = ? AND ts BETWEEN ? AND ? "
            "AND COALESCE(text,'') != '' "
            "AND COALESCE(revoked,0) = 0 AND COALESCE(deleted_for_me,0) = 0 "
            "ORDER BY ts LIMIT ?",
            (jid, start, end, MAX_MESSAGES_PER_WINDOW)).fetchall()
        name = mirror.execute("SELECT name FROM chats WHERE jid = ?", (jid,)).fetchone()
    except Exception:
        return None
    finally:
        mirror.close()
    if len(rows) < MIN_MESSAGES_PER_WINDOW:
        return None
    sentences = []
    for i, (ts, sender, from_me, text) in enumerate(rows):
        sentences.append({"index": i, "speaker": "operador" if from_me else (sender or "contacto"),
                          "text": text, "start_time": ts})
    return {"title": (name[0] if name and name[0] else jid.split("@")[0]),
            "overview": None, "action_items": [], "sentences": sentences}


def harvest_windows(conn=None, now: Optional[int] = None, limit: int = 10) -> dict:
    """Ventanas cerradas de chats permitidos → eventos de captura.

    Aquí es donde el contenido cruza por primera vez del espejo a nuestra base, y
    solo aquí. Todo lo anterior fue pulso sin palabras.

    El evento entra al MISMO `capture_events` que Fireflies con
    `source_kind='whatsapp'`, así que el turno de digestión, el gate de citas
    verbatim, los objetivos, la supresión y las tarjetas funcionan sin una sola
    rama nueva. La ventana ES el evento: mismo id determinista por coordenadas,
    misma idempotencia.
    """
    from . import digestion as dg
    own = conn is None
    conn = conn or db.get_conn()
    now = _now() if now is None else now
    created, skipped = [], []
    try:
        for w in closed_windows(conn, now)[:limit]:
            jid = w["jid"]
            start = w["window_open_at"] or (w["last_seen_at"] - WINDOW_GAP_SECONDS)
            # Autorizar es "de aquí en adelante", NO "y todo lo que ya bajaste".
            # Sin este piso, marcar un chat ingiere retroactivamente cuanto el
            # espejo haya acumulado de él — meses de conversación que el operador
            # nunca decidió entregar, y leer no se deshace. Bajar historial viejo
            # tiene que ser una decisión aparte y explícita.
            desde = w.get("decided_at")
            if desde:
                start = max(start, desde)
                if start >= w["last_seen_at"]:
                    conn.execute("UPDATE whatsapp_activity SET harvested_at = ?, "
                                 "window_open_at = NULL WHERE jid = ?", (now, jid))
                    skipped.append(jid)
                    continue
            payload = read_window(jid, start, w["last_seen_at"])
            if payload is None:
                # Ventana demasiado corta o ilegible: se marca cosechada igual,
                # o quedaría reintentándose para siempre sobre datos que no
                # alcanzan para un compromiso.
                conn.execute("UPDATE whatsapp_activity SET harvested_at = ?, window_open_at = NULL "
                             "WHERE jid = ?", (now, jid))
                skipped.append(jid)
                continue
            eid = dg.event_id_for("whatsapp", jid, start, w["last_seen_at"])
            ek, ei = dg.resolve_entity_from_signals(
                conn, {"text": payload.get("title") or "", "identities": [jid.split("@")[0]]})
            conn.execute(
                "INSERT OR IGNORE INTO capture_events (event_id, source_kind, source_ref, title, "
                "window_start, window_end, occurred_at, captured_at, payload, entity_kind, "
                "entity_id) VALUES (?, 'whatsapp', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, jid, payload.get("title"), start, w["last_seen_at"], w["last_seen_at"],
                 now, json.dumps(payload, ensure_ascii=False), ek, ei))
            conn.execute("UPDATE whatsapp_activity SET harvested_at = ?, window_open_at = NULL, "
                         "message_count = 0 WHERE jid = ?", (now, jid))
            created.append(eid)
        conn.commit()
        return {"status": "ok", "created": created, "skipped": skipped}
    finally:
        if own:
            conn.close()
