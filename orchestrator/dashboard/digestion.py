"""Digestión diferencial: eventos de captura → objetivos gobernados → sugerencias.

El cuerpo del loop que diseñamos en
`knowledge/differential-capture-architecture-2026-08-03.md` (plan de récord §V2).
Cuatro etapas metabólicas, y solo UNA de ellas habla con un modelo:

    ingerir   `ingest_fireflies_event`  — determinista, sin LLM
    digerir   `digest_event`            — UNA llamada sin herramientas, JSON tipado
    asimilar  `apply_state_ops`         — determinista, transaccional
    excretar  `decay_and_excrete`       — determinista

**El modelo propone, el código dispone.** El turno de digestión no tiene
herramientas y no escribe una sola fila: devuelve operaciones tipadas que este
módulo valida y aplica. Esa frontera es deliberada — el texto que entra es habla
de clientes (y en Fase 2, mensajes de terceros), o sea entrada no confiable, y un
agente con herramientas masticando entrada no confiable es una superficie de
prompt injection. Por eso la digestión corre como script `--no-agent`.

Cuatro invariantes que el código sostiene y los tests prueban en rojo:

  * **La cita sale de habla real.** `quote` debe ser subcadena verbatim de
    `sentences[].text`. `summary.action_items` es texto *generado por la IA de
    Fireflies*, así que aporta el candidato y el ancla `(MM:SS)` — nunca la cita.
    Un modelo que alucina una frase no puede citarla.
  * **Validez cronológica** (GEM): una op de un evento más viejo que el
    `last_evidence_ts` del objetivo se rechaza — el pasado no reescribe el
    presente cuando los eventos llegan fuera de orden.
  * **Todo o nada por evento.** Ledger + objetivos + evidencia + sugerencias +
    `digest_status` commitean en una sola transacción. Una salida malformada
    deja el evento en `failed` (reintentable) y a los 3 intentos en
    `dead_letter` — visible. Nunca `digested`, que lo perdería en silencio.
  * **Las sugerencias las deriva el código**, desde transiciones de objetivo.
    Por eso la dedup es estructural: `UNIQUE(objective_id, kind)` significa que
    una segunda mención de un objetivo abierto solo puede subir `seen_count`.

Convenciones del módulo (iguales a `cadence.py`/`dispatch.py`): conexión por
llamada vía `db.get_conn()`, errores como dicts tipados en vez de excepciones, y
TODO cruce de proceso por el seam `_run_cli` para que los tests lo intercepten.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import db
from .migrations.m15_differential_capture import MAX_DIGEST_ATTEMPTS

# El topic donde aterrizan las tarjetas: «Tasks», thread 8037 (fijado por el
# operador, 2026-08-04). Se fija por ID y no por nombre a propósito — el registro de
# `threads` es sembrado a mano y los renombres del lado Telegram son externos a
# él, así que el nombre puede desfasarse (hoy la fila 8037 todavía dice
# "Review") mientras el id es estable. Nunca se auto-crea un topic: doctrina de
# `threads.py`. «Hoy» es la caída declarada si el id desaparece del registro.
CARD_THREAD_ID = int(os.environ.get("DIGESTION_CARD_THREAD_ID", "8037"))
FALLBACK_THREAD_NAME = "📅 Hoy"
DEFAULT_CHAT_ID = os.environ.get("HERMES_DEFAULT_CHAT_ID", "")

# Marcador greppeable en el body de la tarea — el mismo patrón que `cadence.MARKER`.
# Es lo que hace reconciliable un accept interrumpido: antes de reintentar se
# busca este marcador en `tasks.body` para saber si el efecto ya había aterrizado.
SUGGESTION_MARKER = "[suggestion:{id}]"

DIGEST_MODEL = os.environ.get("DIGESTION_MODEL", "glm-5.3")
E0_TTL_DAYS = int(os.environ.get("DIGESTION_E0_TTL_DAYS", "60"))
SUGGESTION_TTL_DAYS = int(os.environ.get("DIGESTION_SUGGESTION_TTL_DAYS", "14"))
LEASE_SECONDS = int(os.environ.get("DIGESTION_LEASE_SECONDS", "1800"))
CARDS_PER_RUN = 10
CARD_SPACING_SECONDS = 3
# Supresión estilo CDHF: un bucket con historial suficiente y aceptación pobre
# deja de gastar la atención del operador. Umbral el miner, no el humano.
SUPPRESS_AFTER_DECIDED = 8
SUPPRESS_BELOW_RATE = 0.5

# Dos presupuestos distintos a propósito. `hermes send` es un POST a Telegram:
# si tarda dos minutos algo está roto, y esperar más solo alarga la ventana
# ambigua en la que no sabemos si el mensaje aterrizó. La llamada al modelo es
# otra cosa — medido en vivo contra glm-5.2: 9 s con un contrato de forma
# explícito, 53 s cuando el modelo se pone a razonar de más, y una junta real
# lleva ~130 oraciones en vez de las 4 del probe. 120 s la cortaría a media
# generación y perdería el evento a `failed` por una razón que no es del evento.
_SUBPROCESS_TIMEOUT = 120
DIGEST_TIMEOUT = int(os.environ.get("DIGESTION_TIMEOUT", "600"))


# ----------------------------------------------------------------- seams

def _now() -> int:
    """Seam de reloj: los tests inyectan un `now` para probar decaimiento y
    expiración sin dormir."""
    return int(time.time())


def _run_cli(argv: list, timeout: int = _SUBPROCESS_TIMEOUT) -> tuple:
    """Corre un CLI y DEVUELVE SU CÓDIGO DE SALIDA — (returncode, stdout, stderr).
    Los fallos se convierten en código, nunca se lanzan: una excepción escapando
    de aquí abandonaría una fila en vuelo mientras el efecto externo pudo sí
    haber aterrizado."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as e:
        return 127, "", f"{argv[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(argv[:3])}"
    except Exception as e:                                    # pragma: no cover
        return 1, "", f"{type(e).__name__}: {e}"


def _error(code: str, msg: str) -> dict:
    return {"status": "error", "code": code, "error": msg}


# ------------------------------------------------- ingerir (sin LLM)

# `**Nombre**` abre el bloque de un responsable; cada línea siguiente es un
# item que termina en `(MM:SS)` o `(H:MM:SS)`. Verificado en vivo contra 12
# juntas reales: 101 items, 100% con timestamp.
_ASSIGNEE_RE = re.compile(r"^\*\*(.+?)\*\*:?\s*$")
_ITEM_TS_RE = re.compile(r"\((\d{1,2}:\d{2}(?::\d{2})?)\)\s*$")


def _mmss_to_seconds(mmss: str) -> Optional[int]:
    parts = mmss.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def parse_action_items(blob: Optional[str]) -> list:
    """El blob de `summary.action_items` → items estructurados.

    Fireflies lo entrega como texto plano agrupado por responsable. Devuelve
    `[{assignee, text, at_seconds}]`. Falla suave por diseño: un blob con formato
    inesperado produce items sin ancla en vez de tirar el evento — evidencia
    degradada es mejor que evidencia perdida, y la fila lo declara con
    `at_seconds=None`.
    """
    if not blob or not blob.strip():
        return []
    items, assignee = [], None
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _ASSIGNEE_RE.match(line)
        if m:
            assignee = m.group(1).strip()
            continue
        ts = _ITEM_TS_RE.search(line)
        at = _mmss_to_seconds(ts.group(1)) if ts else None
        text = _ITEM_TS_RE.sub("", line).strip() if ts else line
        if text:
            items.append({"assignee": assignee, "text": text, "at_seconds": at})
    return items


def _anchor_sentence(sentences: list, at_seconds: Optional[int]) -> Optional[dict]:
    """La oración cuyo `start_time` está más cerca del ancla del action item.

    Este es el puente entre el resumen (generado por IA) y el habla real: el
    action item dice QUÉ, la oración anclada aporta la CITA verificable.
    """
    if at_seconds is None or not sentences:
        return None
    best, best_d = None, None
    for s in sentences:
        st = s.get("start_time")
        if st is None:
            continue
        d = abs(float(st) - at_seconds)
        if best_d is None or d < best_d:
            best, best_d = s, d
    return best


def event_id_for(source_kind: str, source_ref: str, window_start, window_end) -> str:
    """Identidad desde coordenadas de captura, jamás desde prosa: reprocesar el
    mismo evento es un no-op, venga del webhook o del poll de reconciliación."""
    import hashlib
    key = f"{source_kind}|{source_ref}|{window_start or ''}|{window_end or ''}"
    return "ev_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def build_event_payload(transcript: dict) -> dict:
    """El verbatim E0 que el turno de digestión leerá: oraciones con hablante y
    tiempo, action items parseados con su oración anclada, y el overview."""
    sentences = transcript.get("sentences") or []
    summary = transcript.get("summary") or {}
    items = parse_action_items(summary.get("action_items"))
    for it in items:
        anchor = _anchor_sentence(sentences, it["at_seconds"])
        if anchor is not None:
            it["anchor_index"] = anchor.get("index")
            it["anchor_quote"] = anchor.get("text")
            it["anchor_speaker"] = anchor.get("speaker_name")
    return {
        "title": transcript.get("title"),
        "overview": summary.get("overview"),
        "action_items": items,
        "sentences": [
            {"index": s.get("index"), "speaker": s.get("speaker_name"),
             "text": s.get("text"), "start_time": s.get("start_time")}
            for s in sentences
        ],
    }


def quotable_texts(payload: dict) -> list:
    """Las cadenas de las que una cita PUEDE salir: solo habla real.

    Deliberadamente excluye `overview` y el texto de `action_items` — los genera
    la IA de Fireflies, así que citarlos sería citar un resumen como si fuera
    algo que alguien dijo.
    """
    return [s.get("text") or "" for s in (payload.get("sentences") or [])]


def _payload_richness(payload: dict) -> tuple:
    """Cuánta evidencia trae una captura. Ordena capturas del MISMO evento para
    decidir si una nueva vale más que la guardada: más action items primero
    (son el disparador), luego más oraciones."""
    if not isinstance(payload, dict):
        return (0, 0)
    return (len(payload.get("action_items") or []), len(payload.get("sentences") or []))


def resolve_entity_from_signals(conn, signals: dict) -> tuple:
    """(entity_kind, entity_id) desde señales genéricas — sin saber de dónde vienen.

    Deliberadamente NO recibe un transcript de Fireflies. Cuando entre WhatsApp
    no habrá título de junta ni lista de participantes, solo un chat y texto; una
    resolución atada a la forma de Fireflies habría que reescribirla entera. Aquí
    entra un diccionario de señales y cada fuente arma el suyo.

        {"text": "...",              # título, asunto, lo que nombre a alguien
         "identities": ["a@b.com"]}  # correos, teléfonos — lo que ligue a un contacto

    Dos rutas, la barata primero. Ambas se rinden ante la ambigüedad: una entidad
    equivocada contamina el estado de otro cliente, mientras que ninguna solo
    degrada al pozo global y el humano lo corrige en la tarjeta.
    """
    text = _norm(signals.get("text") or "")
    identities = [i.strip().lower() for i in (signals.get("identities") or []) if i]

    hits = []
    if text:
        # 1. El texto nombra un proyecto o un deal.
        for kind, table in (("project", "projects"), ("deal", "deals")):
            try:
                rows = conn.execute(f"SELECT id, name FROM {table}").fetchall()
            except Exception:
                continue
            for rid, name in rows:
                n = _norm(name or "")
                if len(n) >= 4 and n in text:
                    hits.append((kind, rid, len(n)))
        # 2. El texto nombra una CUENTA que tiene proyecto activo. Una junta con
        #    un cliente pertenece al proyecto de ese cliente aunque nadie lo
        #    nombre — que es el caso normal ("We track visita - Alina").
        if not hits:
            # Fail-soft por tabla: una base sin `accounts` (o sin la columna
            # `account_id` en projects) degrada a las otras rutas en vez de
            # tumbar la resolución entera.
            try:
                accounts = conn.execute("SELECT id, name FROM accounts").fetchall()
            except Exception:
                accounts = []
            for aid, aname in accounts:
                n = _norm(aname or "")
                if len(n) >= 4 and n in text:
                    try:
                        rows = conn.execute(
                            "SELECT id FROM projects WHERE account_id = ? AND "
                            "COALESCE(status,'') NOT IN ('archived','delivered')",
                            (aid,)).fetchall()
                    except Exception:
                        rows = []
                    for (pid,) in rows:
                        hits.append(("project", pid, len(n)))

    # 3. Una identidad conocida (correo hoy, teléfono cuando entre WhatsApp)
    #    lleva a su cuenta y de ahí a su proyecto.
    if not hits and identities:
        marks = ",".join("?" * len(identities))
        try:
            accs = [a for (a,) in conn.execute(
                f"SELECT DISTINCT account_id FROM contacts WHERE account_id IS NOT NULL "
                f"AND lower(COALESCE(email,'')) IN ({marks})", identities)]
        except Exception:
            accs = []
        for aid in accs:
            try:
                rows = conn.execute(
                    "SELECT id FROM projects WHERE account_id = ? AND "
                    "COALESCE(status,'') NOT IN ('archived','delivered')", (aid,)).fetchall()
            except Exception:
                rows = []
            for (pid,) in rows:
                hits.append(("project", pid, 0))

    if not hits:
        return (None, None)
    uniq = {(k, i) for k, i, _ in hits}
    if len(uniq) > 1:
        best = max(h[2] for h in hits)
        top = {(k, i) for k, i, w in hits if w == best}
        if len(top) > 1:
            return (None, None)                  # empate → ambiguo → sin entidad
        return next(iter(top))
    return next(iter(uniq))


def fireflies_signals(transcript: dict) -> dict:
    """Adaptador delgado: la forma de Fireflies → señales genéricas.

    Todo lo que sabe de Fireflies vive aquí. WhatsApp traerá el suyo (JID del
    chat, nombre del contacto, texto de la ventana) sin tocar el resolvedor.
    """
    parts = transcript.get("participants") or []
    if isinstance(parts, str):
        parts = [parts]
    return {"text": transcript.get("title") or "", "identities": list(parts)}


def resolve_event_entity(conn, transcript: dict) -> tuple:
    """Compatibilidad: la ruta de Fireflies, ahora sobre el resolvedor genérico."""
    return resolve_entity_from_signals(conn, fireflies_signals(transcript))


def ingest_fireflies_event(transcript: dict, conn=None) -> dict:
    """Un transcript → una fila `capture_events`. Determinista, idempotente.

    Lo llaman los DOS caminos de captura (el webhook `meeting.summarized` y el
    poll de reconciliación) sin coordinarse entre ellos: como el id sale de las
    coordenadas, el segundo en llegar es un no-op en vez de un duplicado.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        ref = transcript.get("id")
        if not ref:
            return _error("bad_transcript", "transcript sin id")
        occurred = transcript.get("date")
        try:
            occurred = int(float(occurred) / 1000) if occurred else None
        except (TypeError, ValueError):
            occurred = None
        eid = event_id_for("fireflies", ref, occurred, None)
        payload = build_event_payload(transcript)
        blob = json.dumps(payload, ensure_ascii=False)
        ek, ei = resolve_event_entity(conn, transcript)
        cur = conn.execute(
            "INSERT OR IGNORE INTO capture_events (event_id, source_kind, source_ref, title, "
            "window_start, window_end, occurred_at, captured_at, payload, entity_kind, entity_id) "
            "VALUES (?, 'fireflies', ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (eid, ref, transcript.get("title"), occurred, occurred, _now(), blob, ek, ei))
        created = cur.rowcount == 1
        enriched = False
        if not created:
            # El webhook puede ganarle al resumen de Fireflies y capturar un
            # evento pobre. Un `INSERT OR IGNORE` a secas congelaría esa versión
            # para siempre y el poll posterior —con el resumen ya listo— sería un
            # no-op. Así que una captura MÁS RICA reemplaza a una más pobre,
            # pero solo mientras el evento siga sin digerirse: después, el
            # payload es la evidencia sobre la que ya se decidió.
            row = conn.execute(
                "SELECT payload, digest_status FROM capture_events WHERE event_id = ?",
                (eid,)).fetchone()
            if row and row[1] == "pending":
                try:
                    prev = json.loads(row[0]) if row[0] else {}
                except (TypeError, ValueError):
                    prev = {}
                if _payload_richness(payload) > _payload_richness(prev):
                    conn.execute(
                        "UPDATE capture_events SET payload = ?, title = COALESCE(?, title), "
                        "entity_kind = COALESCE(?, entity_kind), entity_id = COALESCE(?, entity_id) "
                        "WHERE event_id = ?",
                        (blob, transcript.get("title"), ek, ei, eid))
                    enriched = True
        if own:
            conn.commit()
        return {"status": "ok", "event_id": eid, "created": created, "enriched": enriched,
                "entity": [ek, ei], "action_items": len(payload["action_items"])}
    finally:
        if own:
            conn.close()


# --------------------------------- digerir (el único turno con modelo)

# Presupuesto de tiempo, derivado de una sola ecuación en vez de tres números
# sueltos: el worker de consolidación mata al tick a los 540 s, así que
# TICK_BUDGET < 540, y cada evento consume a lo más DIGEST_TIMEOUT. Se reclama
# UN evento por turno y se sigue mientras quede presupuesto — así el tick nunca
# se corta a media digestión dejando eventos abandonados, que es lo que pasaba
# con un lote de 10 × 180 s = 1800 s bajo un techo de 540 s.
TICK_BUDGET_SECONDS = int(os.environ.get("DIGESTION_TICK_BUDGET", "480"))
# Por debajo de esto no vale la pena empezar un evento: se quedaría a medias y
# el reintento pagaría la llamada completa otra vez.
MIN_DIGEST_SECONDS = int(os.environ.get("DIGESTION_MIN_SECONDS", "30"))
# glm-5.2 razona antes de contestar y ese razonamiento SALE del mismo
# presupuesto de tokens. Con 4096 una junta real (130 oraciones) se quedaba sin
# aire y devolvía vacío — que el gate leía como "salida malformada" y le gastaba
# un intento al evento, castigándolo por un problema de configuración ajeno a su
# contenido. Medido en vivo el 2026-08-04.
DIGEST_MAX_TOKENS = os.environ.get("DIGESTION_MAX_TOKENS", "16384")
# Reasoning effort, opt-in and UNSET by default: with no value the argv below is
# byte-identical to before, so this adds a control without changing behaviour.
# `oll` validates the value against the provider's accepted set before it touches
# credentials, so a typo fails fast here rather than mid-event. Levels are
# none|low|medium|high|max; cost across them is NOT monotonic and is
# model-specific (measured 2026-08-27), so measure before pinning one.
DIGEST_EFFORT = os.environ.get("DIGESTION_EFFORT") or None
DIGESTION_INPUT_CHAR_BUDGET = int(os.environ.get("DIGESTION_INPUT_CHARS", "120000"))
MAX_OBJECTIVES_IN_CONTEXT = 20
PROMPT_VERSION = 2


def _oll_bin() -> str:
    """`oll` por ruta absoluta: el PATH de un cron/systemd no trae ~/.local/bin.
    Mismo razonamiento que `db.hermes_bin()`."""
    import shutil
    return shutil.which("oll") or str(Path.home() / ".local" / "bin" / "oll")


def _run_worker(argv: list, stdin_text: str, timeout: int) -> tuple:
    """Seam propio para la llamada al modelo — separado de `_run_cli` porque
    pasa stdin y usa otro presupuesto de tiempo. (Por argv el prompt largo
    cuelga; por stdin funciona: medido 2026-08-04.)"""
    try:
        proc = subprocess.run(argv, input=stdin_text, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as e:
        return 127, "", f"{argv[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as e:                                    # pragma: no cover
        return 1, "", f"{type(e).__name__}: {e}"


def _prompt_doc() -> str:
    return (Path(__file__).resolve().parents[1] / "docs" /
            "digestion-turn-prompt.md").read_text(encoding="utf-8")


def claim_next_event(conn=None) -> dict:
    """Reclama UN evento para digerir. Libera leases vencidos ANTES de reclamar.

    Uno por turno, no un lote: el lote obligaba a adivinar cuántos caben en el
    presupuesto del tick, y adivinar de más abandona eventos a media corrida.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Un proceso que murió a media digestión no puede reservar el evento
        # para siempre — y liberar ANTES de reclamar es lo que hace que el mismo
        # tick pueda recogerlo, en vez de esperar al barrido de decaimiento.
        conn.execute(
            "UPDATE capture_events SET digest_status = 'pending', lease_token = NULL, "
            "lease_expires_at = NULL WHERE digest_status = 'leased' AND lease_expires_at < ?",
            (now,))
        row = conn.execute(
            "SELECT event_id FROM capture_events WHERE digest_status IN ('pending','failed') "
            "AND attempts < ? ORDER BY COALESCE(occurred_at, captured_at) LIMIT 1",
            (MAX_DIGEST_ATTEMPTS,)).fetchone()
        if row is None:
            conn.commit()
            return {"status": "ok", "event_id": None}
        import uuid
        token = uuid.uuid4().hex
        conn.execute(
            "UPDATE capture_events SET digest_status = 'leased', lease_token = ?, "
            "lease_expires_at = ? WHERE event_id = ?",
            (token, now + LEASE_SECONDS, row[0]))
        conn.commit()
        return {"status": "ok", "event_id": row[0], "lease_token": token}
    finally:
        if own:
            conn.close()


def build_digest_input(conn, event_id: str) -> Optional[dict]:
    """La entrada acotada del turno: gist + objetivos abiertos + el evento.

    Acotada por construcción — nunca se re-lee la historia, solo el estado
    compacto y el incremento. Si el verbatim se pasa del presupuesto se recortan
    oraciones del medio conservando SIEMPRE las ancladas (son las citables) y se
    declara `truncated` para que el modelo sepa que no lo vio todo.
    """
    ev = conn.execute(
        "SELECT payload, title, entity_kind, entity_id FROM capture_events WHERE event_id = ?",
        (event_id,)).fetchone()
    if ev is None:
        return None
    payload = json.loads(ev[0]) if ev[0] else {}
    ek, ei = ev[2], ev[3]
    if ek and ei:
        gist_row = conn.execute(
            "SELECT gist FROM entity_state WHERE entity_kind = ? AND entity_id = ?",
            (ek, ei)).fetchone()
        objs = conn.execute(
            "SELECT id, title, status, owner, waiting_on, due_hint FROM objectives "
            "WHERE status IN ('open','blocked') AND entity_kind = ? AND entity_id = ? "
            "ORDER BY prominence DESC LIMIT ?", (ek, ei, MAX_OBJECTIVES_IN_CONTEXT)).fetchall()
    else:
        gist_row = None
        objs = conn.execute(
            "SELECT id, title, status, owner, waiting_on, due_hint FROM objectives "
            "WHERE status IN ('open','blocked') AND entity_id IS NULL "
            "ORDER BY prominence DESC LIMIT ?", (MAX_OBJECTIVES_IN_CONTEXT,)).fetchall()

    sentences = payload.get("sentences") or []
    anchored = {i.get("anchor_index") for i in (payload.get("action_items") or [])}
    catalogo = []
    if not (ek and ei):
        # Sin ancla determinista, el modelo puede reconocer el proyecto POR EL
        # CONTENIDO — que es la señal fuerte cuando el título no nombra nada
        # ("We track visita - Alina"). Se le pasan opciones REALES con su id: así
        # no inventa, elige, y la validación es una comparación exacta.
        try:
            catalogo = [{"entity_kind": "project", "entity_id": r[0], "nombre": r[1]}
                        for r in conn.execute(
                            "SELECT id, name FROM projects WHERE COALESCE(status,'') "
                            "NOT IN ('archived','delivered') ORDER BY name LIMIT 40")]
        except Exception:
            catalogo = []

    data = {
        "gist": gist_row[0] if gist_row else None,
        "proyectos_candidatos": catalogo,
        "objetivos_abiertos": [
            {"id": o[0], "title": o[1], "status": o[2], "owner": o[3],
             "waiting_on": o[4], "due_hint": o[5]} for o in objs],
        "evento": {"title": ev[1], "action_items": payload.get("action_items") or [],
                   "sentences": sentences},
        "truncated": False,
    }
    if len(json.dumps(data, ensure_ascii=False)) > DIGESTION_INPUT_CHAR_BUDGET:
        keep, budget = [], DIGESTION_INPUT_CHAR_BUDGET // 2
        used = 0
        for s_ in sentences:
            is_anchor = s_.get("index") in anchored
            cost = len(s_.get("text") or "") + 40
            if is_anchor or used + cost <= budget:
                keep.append(s_)
                used += cost
        data["evento"]["sentences"] = keep
        data["truncated"] = True
    return data


def _extract_ops_json(text: str) -> Optional[list]:
    """Saca la lista de ops de la salida del modelo. Determinista y tolerante a
    envoltorios, pero nunca adivina: lo que no parsea devuelve None, y None
    significa malformado, no 'sin cambios'."""
    if not text or not text.strip():
        return None

    def _coerce(obj):
        if isinstance(obj, dict) and isinstance(obj.get("ops"), list):
            return obj["ops"]
        if isinstance(obj, list):
            return obj
        return None

    for candidate in _json_candidates(text):
        try:
            out = _coerce(json.loads(candidate))
        except (TypeError, ValueError):
            continue
        if out is not None:
            return out
    return None


def _json_candidates(text: str):
    yield text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        yield fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


# `oll` señala así el caso "el modelo razonó y no le quedaron tokens para
# contestar". Se reconoce por su texto porque es el contrato de esa herramienta;
# si cambia, el peor caso es volver a tratarlo como malformado, que es el
# comportamiento anterior.
_EMPTY_WORKER_MARKERS = ("empty response", "(empty response")


# Ollama Cloud limita la CONCURRENCIA de la cuenta, y el gateway de Hermes corre
# sobre esa misma cuenta (`ollama-cloud/glm-5.2`): una ráfaga suya y la digestión
# choca. A diferencia de "connection refused", un 429 cede solo, así que se
# espera en vez de rendirse — rendirse dejaría el loop parado cada vez que
# el operador usa su propio agente.
_RATE_LIMIT_MARKERS = ("429", "too many concurrent", "rate limit")
RATE_LIMIT_RETRIES = int(os.environ.get("DIGESTION_RATE_RETRIES", "3"))
RATE_LIMIT_BACKOFF = int(os.environ.get("DIGESTION_RATE_BACKOFF", "20"))


def _is_rate_limited(out: Optional[str], err: Optional[str]) -> bool:
    blob = f"{out or ''} {err or ''}".lower()
    return any(m in blob for m in _RATE_LIMIT_MARKERS)


def _looks_truncated(out: Optional[str]) -> bool:
    """¿El modelo EMPEZÓ a responder bien y se quedó sin tokens a media frase?

    Un JSON que abre llaves y nunca las cierra no es una respuesta equivocada: es
    una respuesta cortada. Castigar al evento por eso lo manda a `dead_letter`
    por capacidad, no por contenido — la misma injusticia que la respuesta vacía.
    """
    t = (out or "").strip()
    if not t:
        return False
    start = t.find("{")
    if start < 0:
        return False
    depth = 0
    for ch in t[start:]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth > 0          # quedaron llaves sin cerrar


def _is_empty_worker_output(out: Optional[str]) -> bool:
    text = (out or "").strip()
    if not text:
        return True
    low = text.lower()
    return len(text) < 200 and any(m in low for m in _EMPTY_WORKER_MARKERS)


def _release_event(conn, event_id: str) -> None:
    """Devuelve el evento a la cola SIN gastar un intento."""
    conn.execute(
        "UPDATE capture_events SET digest_status = 'pending', lease_token = NULL, "
        "lease_expires_at = NULL WHERE event_id = ?", (event_id,))
    conn.commit()


def digest_event(event_id: str, lease_token: str, conn=None, timeout=None) -> dict:
    """El turno: evento → modelo sin herramientas → ops validadas y aplicadas.

    La distinción que sostiene todo: **una caída de infraestructura no es un
    intento fallido del evento**. Si Ollama está caído o falta la llave, el
    evento vuelve a la cola intacto; contarlo como intento lo mandaría a
    `dead_letter` por algo que no tiene nada que ver con su contenido. Solo una
    salida que el modelo sí produjo y que no se puede parsear gasta intento.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        data = build_digest_input(conn, event_id)
        if data is None:
            return _error("unknown_event", f"evento {event_id!r} no existe")
        argv = [_oll_bin(), "Sigue las instrucciones del sistema sobre la ENTRADA de stdin.",
                "--model", DIGEST_MODEL, "--system", _prompt_doc(),
                "--max-tokens", DIGEST_MAX_TOKENS, "--temperature", "0"]
        if DIGEST_EFFORT:
            argv += ["--effort", DIGEST_EFFORT]
        budget = DIGEST_TIMEOUT if timeout is None else int(timeout)
        stdin_text = json.dumps(data, ensure_ascii=False)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            code, out, err = _run_worker(argv, stdin_text, budget)
            if not _is_rate_limited(out, err) or attempt == RATE_LIMIT_RETRIES:
                break
            wait = RATE_LIMIT_BACKOFF * (attempt + 1)
            if wait >= budget:
                break
            time.sleep(wait)
            budget -= wait
        if _is_rate_limited(out, err):
            _release_event(conn, event_id)
            return _error("worker_rate_limited",
                          "Ollama Cloud rechazó por concurrencia tras "
                          f"{RATE_LIMIT_RETRIES} reintentos — evento devuelto a la cola")
        if code != 0:
            _release_event(conn, event_id)
            return _error("worker_unavailable",
                          f"oll salió {code}: {(err or '')[:200]} — evento devuelto a la cola")
        ops = _extract_ops_json(out)
        if ops is None:
            if _is_empty_worker_output(out) or _looks_truncated(out):
                # El modelo no produjo respuesta (razonó hasta agotar el
                # presupuesto de tokens). Eso es configuración, no contenido:
                # gastarle un intento al evento lo condenaría a `dead_letter`
                # por algo que no tiene que ver con la junta. Vuelve a la cola.
                _release_event(conn, event_id)
                return _error("worker_unavailable",
                              "respuesta vacía o cortada (sube DIGESTION_MAX_TOKENS): "
                              f"{(out or '')[:120]}")
            return _fail_event(conn, event_id, f"salida no parseable: {(out or '')[:200]}", False)
        return apply_state_ops(event_id, ops, lease_token=lease_token, conn=conn)
    finally:
        if own:
            conn.close()


# --------------------------------------------------- el álgebra de ops

# Operadores CERRADOS. Un patch libre recrearía el CRUD que GEM rechaza, así que
# cada operador declara exactamente qué campos puede tocar y sobre qué estados
# es legal. Lo que no está aquí, se rechaza.
OPS = {
    "objective.add":       {"args": {"title", "owner", "waiting_on", "due_hint",
                                     "entity_kind", "entity_id", "quote", "anchor",
                                     "speaker", "confidence"},
                            "required": {"title", "quote"}, "from": None},
    "objective.advance":   {"args": {"objective_id", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "quote"}, "from": {"open", "blocked"}},
    "objective.complete":  {"args": {"objective_id", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "quote"}, "from": {"open", "blocked"}},
    "objective.block":     {"args": {"objective_id", "waiting_on", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "waiting_on", "quote"}, "from": {"open"}},
    "objective.unblock":   {"args": {"objective_id", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "quote"}, "from": {"blocked"}},
    "objective.reopen":    {"args": {"objective_id", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "quote"}, "from": {"done", "archived"}},
    "objective.reassign":  {"args": {"objective_id", "owner", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "owner", "quote"},
                            "from": {"open", "blocked"}},
    "objective.reschedule": {"args": {"objective_id", "due_hint", "quote", "anchor", "speaker"},
                             "required": {"objective_id", "due_hint", "quote"},
                             "from": {"open", "blocked"}},
    "objective.rename":    {"args": {"objective_id", "title", "quote", "anchor", "speaker"},
                            "required": {"objective_id", "title", "quote"},
                            "from": {"open", "blocked"}},
    "objective.supersede": {"args": {"objective_id", "title", "owner", "due_hint",
                                     "quote", "anchor", "speaker"},
                            "required": {"objective_id", "title", "quote"},
                            "from": {"open", "blocked"}},
    "entity.set_gist":     {"args": {"gist"}, "required": {"gist"}, "from": None},
    # Vincular el evento a un proyecto POR CONTENIDO. Es la única op cuyo valor
    # sale del catálogo que se le pasó, así que validarla es comparación exacta
    # contra filas vivas — el modelo no puede inventar un proyecto.
    "entity.link":         {"args": {"entity_kind", "entity_id", "quote"},
                            "required": {"entity_kind", "entity_id"}, "from": None},
}

MAX_TITLE = 120
MAX_GIST = 700
MAX_OPS_PER_EVENT = 20

# Qué transición merece molestar a un humano, y con qué tarjeta.
_TRANSITION_SUGGESTION = {
    "objective.add": "create_task",
    "objective.complete": "close_task",
    "objective.block": "unblock",
}

# Cómo aparece el operador en las transcripciones. Fireflies a veces no
# identifica al hablante y escribe "Speaker 2"; eso NO cuenta como el operador —
# pedir una tarea por un compromiso que quizá era de otro es peor que perderse
# uno propio, porque entrena a ignorar la bandeja. Los alias son CONFIG
# (ORCHESTRATORMAXXING_OPERATOR_ALIASES, lista separada por comas, comparación exacta
# en minúsculas), leídos al momento de usarse — nunca un nombre en el código.
def _operator_alias_set() -> tuple:
    raw = os.environ.get("ORCHESTRATORMAXXING_OPERATOR_ALIASES", "operator")
    return tuple(a.strip().lower() for a in raw.split(",") if a.strip())

# Transiciones que solo valen tarjeta si el compromiso es del operador. `close_task`
# no está aquí a propósito: cierra una tarea que YA es suya (se la ligó al
# aceptar), así que el dueño ya quedó decidido antes.
_OWNER_GATED_KINDS = ("create_task", "unblock")


def is_operator(owner: Optional[str]) -> bool:
    """¿El compromiso es del operador? Comparación exacta contra sus alias.

    Deliberadamente estricta: un "Speaker 2" o un nombre desconocido devuelve
    False. El objetivo igual se guarda —el estado del mundo se queda completo—
    pero no se convierte en tarjeta. Separar las dos capas es lo que deja que la
    bandeja sea SOLO lo que al operador le toca hacer.
    """
    n = (owner or "").strip().lower()
    if not n:
        return False
    return n in _operator_alias_set()


def _norm(s: str) -> str:
    """Normalización de espacios para comparar citas: un modelo re-envuelve
    líneas sin cambiar las palabras, y eso no debería contar como alucinación."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def objective_id_for(event_id: str, op_index: int) -> str:
    import hashlib
    return "obj_" + hashlib.sha256(f"{event_id}|{op_index}".encode()).hexdigest()[:12]


def suggestion_id_for(objective_id: str, kind: str) -> str:
    import hashlib
    return "sug_" + hashlib.sha256(f"{objective_id}|{kind}".encode()).hexdigest()[:12]


# ------------------------------------------------- asimilar (aplicador)

def _validate_op(conn, op: dict, payload: dict, event_occurred: Optional[int]) -> tuple:
    """(verdict, reason, resolved) — el gate. Determinista y sin excepciones.

    Es el único lugar donde la salida de un modelo se convierte en algo que
    puede tocar el estado, así que rechaza por defecto: tipo desconocido, campo
    de más, cita no verbatim, transición ilegal o evidencia rancia no se
    aplican. `resolved` trae lo ya buscado (la fila del objetivo) para que el
    aplicador no repita el trabajo.
    """
    name = op.get("op")
    spec = OPS.get(name)
    if spec is None:
        return "rejected_validation", f"operador desconocido: {name!r}", None

    extra = set(op) - {"op"} - spec["args"]
    if extra:
        return "rejected_validation", f"campos no permitidos: {sorted(extra)}", None
    missing = spec["required"] - set(k for k in op if op.get(k) not in (None, ""))
    if missing:
        return "rejected_validation", f"faltan campos: {sorted(missing)}", None

    title = op.get("title")
    if title is not None and (not title.strip() or len(title) > MAX_TITLE):
        return "rejected_validation", f"title vacío o >{MAX_TITLE}", None
    gist = op.get("gist")
    if gist is not None and len(gist) > MAX_GIST:
        return "rejected_validation", f"gist >{MAX_GIST}", None
    conf = op.get("confidence")
    if conf is not None:
        try:
            if not 0.0 <= float(conf) <= 1.0:
                raise ValueError
        except (TypeError, ValueError):
            return "rejected_validation", "confidence fuera de [0,1]", None

    # Anti-alucinación: la cita tiene que ser habla real de ESTE evento.
    quote = op.get("quote")
    if quote is not None:
        hay = " || ".join(_norm(t) for t in quotable_texts(payload))
        if not _norm(quote) or _norm(quote) not in hay:
            return "rejected_validation", "quote no es verbatim de sentences[]", None

    if name == "entity.set_gist":
        return "applied", None, None

    if name == "entity.link":
        ek, ei = op.get("entity_kind"), op.get("entity_id")
        if ek not in ("deal", "project", "account"):
            return "rejected_validation", f"entity_kind inválido: {ek!r}", None
        table = {"deal": "deals", "project": "projects", "account": "accounts"}[ek]
        try:
            hit = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ei,)).fetchone()
        except Exception:
            hit = None
        if not hit:
            return "rejected_unknown_ref", f"{ek} {ei!r} no existe", None
        return "applied", None, None

    if name == "objective.add":
        ek, ei = op.get("entity_kind"), op.get("entity_id")
        if (ek is None) != (ei is None):
            return "rejected_validation", "entity_kind y entity_id van juntos", None
        if ek is not None:
            if ek not in ("deal", "project", "account"):
                return "rejected_validation", f"entity_kind inválido: {ek!r}", None
            # Referencia equivocada se RECHAZA, no se globaliza en silencio.
            table = {"deal": "deals", "project": "projects", "account": "accounts"}[ek]
            try:
                hit = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ei,)).fetchone()
            except Exception:
                hit = None
            if not hit:
                return "rejected_unknown_ref", f"{ek} {ei!r} no existe", None
        return "applied", None, None

    row = conn.execute(
        "SELECT id, status, last_evidence_ts, version FROM objectives WHERE id = ?",
        (op.get("objective_id"),)).fetchone()
    if row is None:
        return "rejected_unknown_ref", f"objetivo {op.get('objective_id')!r} no existe", None
    _, status, last_ts, version = row
    if spec["from"] is not None and status not in spec["from"]:
        return ("rejected_validation",
                f"transición ilegal: {name} sobre estado {status!r}", None)
    # Validez cronológica (GEM): un evento viejo no reescribe el presente.
    if event_occurred is not None and last_ts is not None and event_occurred < last_ts:
        return "rejected_stale", f"evento ({event_occurred}) anterior a la evidencia ({last_ts})", None
    return "applied", None, {"status": status, "version": version}


def _record_evidence(conn, objective_id, event_id, op, now):
    conn.execute(
        "INSERT OR IGNORE INTO objective_evidence (objective_id, event_id, anchor, quote, "
        "speaker, ts, op, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (objective_id, event_id, str(op.get("anchor")) if op.get("anchor") is not None else None,
         op.get("quote") or "", op.get("speaker"), now, op.get("op"), now))


def _bucket_is_suppressed(conn, bucket: str) -> bool:
    """Supresión aprendida: un bucket con historial suficiente y aceptación
    pobre deja de gastar atención. Se umbraliza el miner, no al humano.

    Solo cuentan las decisiones sobre compromisos DEL OPERADOR. Descartar el
    compromiso de otra persona responde a "¿esto es mío?", no a "¿esto vale la
    pena?" — y mezclarlas envenena el aprendizaje. Pasó de verdad: 11 descartes
    de tareas ajenas (por un gate de dueño que faltaba) enseñaron "descarta
    todo", y dos compromisos suyos legítimos quedaron suprimidos sin llegarle.
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(s.status = 'accepted') FROM suggestions s "
        "JOIN objectives o ON o.id = s.objective_id "
        "WHERE s.bucket = ? AND s.status IN ('accepted','dismissed') "
        "AND lower(COALESCE(o.owner,'')) IN ({})".format(
            ",".join("?" * len(_operator_alias_set()))),
        (bucket,) + _operator_alias_set()).fetchone()
    decided, accepted = (row[0] or 0), (row[1] or 0)
    return decided >= SUPPRESS_AFTER_DECIDED and (accepted / decided) < SUPPRESS_BELOW_RATE


def _derive_suggestion(conn, objective_id: str, op_name: str, source_kind: str,
                       title: str, confidence, now: int):
    """Deriva la tarjeta desde la TRANSICIÓN — nunca la escribe el modelo.

    Aquí vive la dedup estructural: `UNIQUE(objective_id, kind)` significa que
    volver a detectar un objetivo abierto solo puede subir `seen_count`. Y el
    dismiss es pegajoso para siempre: una sugerencia rechazada no revive.
    """
    kind = _TRANSITION_SUGGESTION.get(op_name)
    if kind is None:
        return None
    if kind in _OWNER_GATED_KINDS:
        owner = conn.execute("SELECT owner FROM objectives WHERE id = ?",
                             (objective_id,)).fetchone()
        if not is_operator(owner[0] if owner else None):
            return None
    sid = suggestion_id_for(objective_id, kind)
    row = conn.execute("SELECT status, seen_count FROM suggestions WHERE id = ?",
                       (sid,)).fetchone()
    if row is not None:
        status, seen = row
        if status == "dismissed":
            return None                      # pegajoso: nunca re-sugerir
        if status in ("accepted", "accepting", "suppressed"):
            conn.execute("UPDATE suggestions SET seen_count = ?, updated_at = ? WHERE id = ?",
                         (seen + 1, now, sid))
            return None
        # open / expired → reabrir contando la reincidencia
        conn.execute("UPDATE suggestions SET status = 'open', seen_count = ?, updated_at = ? "
                     "WHERE id = ?", (seen + 1, now, sid))
        return sid
    bucket = f"{source_kind}:{kind}:{_conf_band(confidence)}"
    status = "suppressed" if _bucket_is_suppressed(conn, bucket) else "open"
    conn.execute(
        "INSERT INTO suggestions (id, objective_id, kind, status, bucket, confidence, title, "
        "proposed_project_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, objective_id, kind, status, bucket, confidence, title,
         resolve_project_for_objective(conn, objective_id), now, now))
    return sid if status == "open" else None


def resolve_project_for_objective(conn, objective_id: str) -> Optional[str]:
    """El proyecto al que pertenece un objetivo, si ya está dado de alta.

    Dos saltos, ambos existentes en la espina: si el evento se ancló a un
    PROYECTO, ese es; si se ancló a un DEAL, se sigue `deals.project_id` (la
    unión dinero→entrega). Sin ancla, devuelve None y la tarea nace en Inbox —
    proponer un proyecto equivocado es peor que no proponer ninguno, porque una
    tarea mal archivada se pierde igual que una sin archivar pero además miente.
    """
    row = conn.execute("SELECT entity_kind, entity_id FROM objectives WHERE id = ?",
                       (objective_id,)).fetchone()
    if not row or not row[1]:
        return None
    kind, eid = row
    if kind == "project":
        hit = conn.execute("SELECT id FROM projects WHERE id = ?", (eid,)).fetchone()
        return hit[0] if hit else None
    if kind == "deal":
        try:
            hit = conn.execute("SELECT project_id FROM deals WHERE id = ?", (eid,)).fetchone()
        except Exception:
            return None
        if hit and hit[0]:
            live = conn.execute("SELECT id FROM projects WHERE id = ?", (hit[0],)).fetchone()
            return live[0] if live else None
    return None


def _conf_band(c) -> str:
    try:
        c = float(c)
    except (TypeError, ValueError):
        return "med"
    return "high" if c >= 0.8 else ("low" if c < 0.5 else "med")


def apply_state_ops(event_id: str, ops: list, lease_token: Optional[str] = None,
                    conn=None) -> dict:
    """Aplica las ops de UN evento en UNA transacción. Todo o nada.

    Si algo revienta a media aplicación, el evento no queda ni medio digerido ni
    marcado como hecho: la transacción se deshace entera y el evento vuelve a la
    cola. Una salida estructuralmente malformada NO marca `digested` — sube
    `attempts` y a los `MAX_DIGEST_ATTEMPTS` cae a `dead_letter`, visible.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        if not isinstance(ops, list):
            return _fail_event(conn, event_id, "ops no es una lista", own)
        if len(ops) > MAX_OPS_PER_EVENT:
            return _fail_event(conn, event_id, f"más de {MAX_OPS_PER_EVENT} ops", own)

        ev = conn.execute(
            "SELECT payload, occurred_at, digest_status, lease_token, source_kind, entity_kind, "
            "entity_id FROM capture_events WHERE event_id = ?", (event_id,)).fetchone()
        if ev is None:
            return _error("unknown_event", f"evento {event_id!r} no existe")
        payload_raw, occurred, status, held, source_kind, ev_ek, ev_ei = ev
        if status == "digested":
            # Replay tras un crash posterior al commit: no re-aplicar.
            return {"status": "ok", "event_id": event_id, "replay": True, "applied": 0}
        if lease_token and held and lease_token != held:
            return _error("lease_lost", "otro proceso tiene el lease de este evento")
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except (TypeError, ValueError):
            return _fail_event(conn, event_id, "payload E0 ilegible", own)

        conn.execute("BEGIN IMMEDIATE")
        results, applied = [], 0
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                results.append({"op_index": i, "verdict": "rejected_validation"})
                _ledger(conn, event_id, i, {"op": "?"}, "rejected_validation",
                        "la op no es un objeto", None, now)
                continue
            if conn.execute("SELECT 1 FROM state_ops WHERE event_id = ? AND op_index = ?",
                            (event_id, i)).fetchone():
                results.append({"op_index": i, "verdict": "skipped_duplicate"})
                continue
            verdict, reason, resolved = _validate_op(conn, op, payload, occurred)
            oid = None
            if verdict == "applied":
                oid = _apply_one(conn, event_id, i, op, resolved, source_kind,
                                 ev_ek, ev_ei, occurred, now)
                applied += 1
            _ledger(conn, event_id, i, op, verdict, reason, oid, now)
            results.append({"op_index": i, "verdict": verdict, "reason": reason,
                            "objective_id": oid})

        conn.execute(
            "UPDATE capture_events SET digest_status = 'digested', digested_at = ?, "
            "ops_applied = ?, lease_token = NULL, lease_expires_at = NULL WHERE event_id = ?",
            (now, applied, event_id))
        conn.commit()
        return {"status": "ok", "event_id": event_id, "applied": applied, "ops": results}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:                                     # pragma: no cover
            pass
        return _fail_event(conn, event_id, f"{type(e).__name__}: {e}", own)
    finally:
        if own:
            conn.close()


def _ledger(conn, event_id, i, op, verdict, reason, objective_id, now):
    conn.execute(
        "INSERT OR IGNORE INTO state_ops (event_id, op_index, op, objective_id, args_json, "
        "verdict, reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (event_id, i, op.get("op", "?"), objective_id,
         json.dumps(op, ensure_ascii=False), verdict, reason, now))


def _fail_event(conn, event_id: str, reason: str, own: bool) -> dict:
    """Malformado ≠ digerido. Reintenta y, agotados los intentos, PARKEA visible."""
    try:
        row = conn.execute("SELECT attempts FROM capture_events WHERE event_id = ?",
                           (event_id,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        status = "dead_letter" if attempts >= MAX_DIGEST_ATTEMPTS else "failed"
        conn.execute(
            "UPDATE capture_events SET digest_status = ?, attempts = ?, last_error = ?, "
            "lease_token = NULL, lease_expires_at = NULL WHERE event_id = ?",
            (status, attempts, reason[:500], event_id))
        conn.commit()
        return {"status": "error", "code": "digest_failed", "error": reason,
                "event_id": event_id, "attempts": attempts, "digest_status": status}
    except Exception as e:                                    # pragma: no cover
        return _error("digest_failed", f"{reason} (+ {e})")


def _apply_one(conn, event_id, i, op, resolved, source_kind, ev_ek, ev_ei, occurred, now):
    name = op["op"]

    if name == "entity.link":
        # Solo rellena un ancla VACÍA: la resolución determinista (nombre exacto,
        # identidad conocida) es más confiable que el reconocimiento del modelo,
        # así que cuando existe, gana.
        if not (ev_ek and ev_ei):
            conn.execute("UPDATE capture_events SET entity_kind = ?, entity_id = ? "
                         "WHERE event_id = ?", (op["entity_kind"], op["entity_id"], event_id))
        return None

    if name == "entity.set_gist":
        if ev_ek and ev_ei:
            conn.execute(
                "INSERT INTO entity_state (entity_kind, entity_id, gist, updated_at, "
                "updated_by_event) VALUES (?,?,?,?,?) ON CONFLICT(entity_kind, entity_id) "
                "DO UPDATE SET gist = excluded.gist, updated_at = excluded.updated_at, "
                "updated_by_event = excluded.updated_by_event",
                (ev_ek, ev_ei, op["gist"], now, event_id))
        return None

    if name == "objective.add":
        oid = objective_id_for(event_id, i)
        # El id sale de (event_id, op_index), justo para que re-digerir sea
        # idempotente. Si ya existe, esta op exacta ya aterrizó: reusarlo en vez
        # de reventar con UNIQUE, que es lo que hacía morir un evento reencolado.
        if conn.execute("SELECT 1 FROM objectives WHERE id = ?", (oid,)).fetchone():
            return oid
        conn.execute(
            "INSERT INTO objectives (id, entity_kind, entity_id, title, owner, waiting_on, "
            "due_hint, opened_at, updated_at, last_evidence_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, op.get("entity_kind") or ev_ek, op.get("entity_id") or ev_ei,
             op["title"].strip(), op.get("owner"), op.get("waiting_on"), op.get("due_hint"),
             now, now, occurred))
        _record_evidence(conn, oid, event_id, op, now)
        _derive_suggestion(conn, oid, name, source_kind, op["title"].strip(),
                           op.get("confidence"), now)
        return oid

    oid = op["objective_id"]
    version = resolved["version"]
    sets, args = ["updated_at = ?", "last_evidence_ts = ?", "version = version + 1"], [now, occurred]

    if name == "objective.advance":
        sets.append("prominence = MIN(prominence + 0.25, 2.0)")
    elif name == "objective.complete":
        sets += ["status = 'done'", "closed_at = ?"]; args.append(now)
    elif name == "objective.block":
        sets += ["status = 'blocked'", "waiting_on = ?"]; args.append(op["waiting_on"])
    elif name == "objective.unblock":
        sets += ["status = 'open'", "waiting_on = NULL"]
    elif name == "objective.reopen":
        sets += ["status = 'open'", "closed_at = NULL"]
    elif name == "objective.reassign":
        sets.append("owner = ?"); args.append(op["owner"])
    elif name == "objective.reschedule":
        sets.append("due_hint = ?"); args.append(op["due_hint"])
    elif name == "objective.rename":
        sets.append("title = ?"); args.append(op["title"].strip())
    elif name == "objective.supersede":
        new_id = objective_id_for(event_id, i)
        conn.execute(
            "INSERT INTO objectives (id, entity_kind, entity_id, title, owner, due_hint, "
            "opened_at, updated_at, last_evidence_ts) "
            "SELECT ?, entity_kind, entity_id, ?, ?, ?, ?, ?, ? FROM objectives WHERE id = ?",
            (new_id, op["title"].strip(), op.get("owner"), op.get("due_hint"),
             now, now, occurred, oid))
        # Compare-and-swap: si otro escritor movió la fila, esta no aterriza.
        conn.execute("UPDATE objectives SET status = 'superseded', superseded_by = ?, "
                     "updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
                     (new_id, now, oid, version))
        _record_evidence(conn, new_id, event_id, op, now)
        _derive_suggestion(conn, new_id, "objective.add", source_kind,
                           op["title"].strip(), op.get("confidence"), now)
        return new_id

    args += [oid, version]
    conn.execute(f"UPDATE objectives SET {', '.join(sets)} WHERE id = ? AND version = ?", args)
    _record_evidence(conn, oid, event_id, op, now)
    title = conn.execute("SELECT title FROM objectives WHERE id = ?", (oid,)).fetchone()[0]
    _derive_suggestion(conn, oid, name, source_kind, title, op.get("confidence"), now)
    return oid


# ------------------------------------------------ excretar (decaimiento)

def decay_and_excrete(conn=None) -> dict:
    """Prominencia que decae, objetivos que se archivan, verbatim que se purga.

    La cuarta etapa metabólica, entera determinista. Purgar E0 es tanto higiene
    de almacenamiento como de privacidad: el habla cruda del cliente no se queda
    para siempre. La cita ya vive copiada en `objective_evidence`, así que la
    tarjeta conserva su "por qué te lo sugiero" después de la purga.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        # Decaimiento por silencio (GEM: olvido por prominencia, no por capacidad).
        conn.execute(
            "UPDATE objectives SET prominence = MAX(prominence * POWER(0.97, "
            "  MAX((? - COALESCE(last_evidence_ts, opened_at)) / 86400.0, 0)), 0.0) "
            "WHERE status IN ('open','blocked')", (now,))
        archived = conn.execute(
            "UPDATE objectives SET status = 'archived', updated_at = ? "
            "WHERE status IN ('open','blocked') AND prominence < 0.2", (now,)).rowcount
        expired = conn.execute(
            "UPDATE suggestions SET status = 'expired', updated_at = ? "
            "WHERE status = 'open' AND created_at < ?",
            (now, now - SUGGESTION_TTL_DAYS * 86400)).rowcount
        purged = conn.execute(
            "UPDATE capture_events SET payload = NULL, payload_purged_at = ? "
            "WHERE digest_status = 'digested' AND payload IS NOT NULL AND digested_at < ?",
            (now, now - E0_TTL_DAYS * 86400)).rowcount
        # Un lease vencido vuelve a la cola: un proceso que murió a media
        # digestión no puede dejar el evento reservado para siempre.
        released = conn.execute(
            "UPDATE capture_events SET digest_status = 'pending', lease_token = NULL, "
            "lease_expires_at = NULL WHERE digest_status = 'leased' AND lease_expires_at < ?",
            (now,)).rowcount
        conn.commit()
        return {"status": "ok", "archived": archived, "expired": expired,
                "purged": purged, "leases_released": released}
    finally:
        if own:
            conn.close()


# --------------------------------------------------- tarjetas a Telegram

def _resolve_card_thread(conn) -> dict:
    """El topic destino, por id. `fallback` declara que NO es el topic pedido —
    mentir sobre dónde aterrizó un mensaje es exactamente la clase de silencio
    que el registro existe para evitar."""
    row = conn.execute(
        "SELECT thread_id, chat_id, name FROM threads WHERE thread_id = ? AND status = 'active'",
        (CARD_THREAD_ID,)).fetchone()
    if row is not None:
        return {"thread_id": row[0], "chat_id": row[1], "name": row[2], "fallback": False}
    row = conn.execute(
        "SELECT thread_id, chat_id, name FROM threads WHERE name = ? AND status = 'active'",
        (FALLBACK_THREAD_NAME,)).fetchone()
    if row is not None:
        return {"thread_id": row[0], "chat_id": row[1], "name": row[2], "fallback": True}
    return {"thread_id": None, "chat_id": DEFAULT_CHAT_ID, "name": None, "fallback": True}


_CARD_ICON = {"create_task": "🆕", "close_task": "✅", "unblock": "🔓", "review": "👀"}
_CARD_LEAD = {
    "create_task": "Compromiso nuevo",
    "close_task": "Parece cumplido — ¿cierro la tarea?",
    "unblock": "Bloqueado, esperando",
    "review": "Vale la pena revisar",
}


def render_card(conn, suggestion_id: str) -> Optional[str]:
    """El texto de la tarjeta. Determinista: sin LLM en el camino de salida.

    Lleva la cita textual con su hablante porque el "por qué te lo sugiero" es
    lo que hace revisable una sugerencia — sin evidencia el humano no puede
    juzgar, solo obedecer o ignorar.
    """
    row = conn.execute(
        "SELECT s.kind, s.title, s.confidence, o.owner, o.waiting_on, o.due_hint, o.id "
        "FROM suggestions s JOIN objectives o ON o.id = s.objective_id WHERE s.id = ?",
        (suggestion_id,)).fetchone()
    if row is None:
        return None
    kind, title, conf, owner, waiting_on, due, oid = row
    ev = conn.execute(
        "SELECT quote, speaker, event_id FROM objective_evidence WHERE objective_id = ? "
        "ORDER BY id DESC LIMIT 1", (oid,)).fetchone()

    lines = [f"{_CARD_ICON.get(kind, '•')} {_CARD_LEAD.get(kind, kind)}", f"*{title}*"]
    if ev:
        quote, speaker, event_id = ev
        meeting = conn.execute("SELECT title FROM capture_events WHERE event_id = ?",
                               (event_id,)).fetchone()
        origin = " · ".join(x for x in (speaker, (meeting or [None])[0]) if x)
        lines.append(f'↳ "{quote}"' + (f" — {origin}" if origin else ""))
    meta = []
    if owner:
        meta.append(f"dueño: {owner}")
    if waiting_on:
        meta.append(f"espera a: {waiting_on}")
    if due:
        meta.append(f"para: {due}")
    if conf is not None:
        meta.append(f"conf {float(conf):.2f}")
    if meta:
        lines.append(" · ".join(meta))
    lines.append(f"{db.dashboard_url()}/?tab=suggestions&sug={suggestion_id}")
    return "\n".join(lines)


def dispatch_cards(limit: int = CARDS_PER_RUN, conn=None, sleep=None) -> dict:
    """Manda las tarjetas pendientes al topic. At-most-once por diseño.

    `hermes send` no devuelve id de mensaje, así que un envío cuyo resultado no
    podemos leer es genuinamente AMBIGUO: pudo aterrizar. Reintentarlo arriesga
    duplicar en el chat del operador, así que se marca `ambiguous` y se deja para
    inspección en vez de reintentar a ciegas. La fila se reclama ANTES de enviar
    para que un crash a media llamada no reenvíe al reiniciar.
    """
    own = conn is None
    conn = conn or db.get_conn()
    # Ligado TARDÍO: con `sleep=time.sleep` como default, el valor queda fijado
    # al definir la función y ningún test puede sustituirlo — la suite pagaría
    # el espaciado real de producción.
    sleep = time.sleep if sleep is None else sleep
    now = _now()
    sent, failed, ambiguous = [], [], []
    try:
        target = _resolve_card_thread(conn)
        rows = conn.execute(
            "SELECT id FROM suggestions WHERE status = 'open' AND card_status = 'unsent' "
            "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        for i, (sid,) in enumerate(rows):
            claimed = conn.execute(
                "UPDATE suggestions SET card_status = 'sending', updated_at = ? "
                "WHERE id = ? AND card_status = 'unsent'", (now, sid)).rowcount
            conn.commit()
            if not claimed:
                continue                       # otro proceso la tomó
            text = render_card(conn, sid)
            if text is None:
                continue
            if i:
                sleep(CARD_SPACING_SECONDS)    # tope de 20 msg/min por grupo
            dest = (f"telegram:{target['chat_id']}:{target['thread_id']}"
                    if target["thread_id"] else f"telegram:{target['chat_id']}")
            code, _out, err = _run_cli([db.hermes_bin(), "send", "--to", dest, "-q", text])
            if code == 0:
                conn.execute("UPDATE suggestions SET card_status = 'sent', card_sent_at = ?, "
                             "updated_at = ? WHERE id = ?", (now, now, sid))
                sent.append(sid)
            elif code in (124,):
                # Timeout: el envío pudo haber aterrizado. No reintentar.
                conn.execute("UPDATE suggestions SET card_status = 'ambiguous', updated_at = ? "
                             "WHERE id = ?", (now, sid))
                ambiguous.append(sid)
            else:
                conn.execute("UPDATE suggestions SET card_status = 'unsent', updated_at = ? "
                             "WHERE id = ?", (now, sid))
                failed.append({"id": sid, "error": (err or "")[:200]})
            conn.commit()
        return {"status": "ok", "sent": sent, "failed": failed, "ambiguous": ambiguous,
                "thread": target}
    finally:
        if own:
            conn.close()


# ------------------------------------------- captura: acuses y poll

def record_receipt(source_ref: str, event_name: str = "meeting.summarized",
                   source_kind: str = "fireflies", conn=None) -> dict:
    """El acuse durable del webhook. Lo único que el request hace antes del 200.

    Escribir esta fila ANTES de responder es lo que convierte un aviso en algo
    recuperable: si el proceso muere enseguida, el tick encuentra el acuse
    pendiente y hace el fetch. Sin ella, un webhook perdido no deja ninguna señal
    de que una junta debió capturarse.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        conn.execute(
            "INSERT INTO capture_receipts (source_kind, source_ref, event_name, received_at) "
            "VALUES (?,?,?,?) ON CONFLICT(source_kind, source_ref) DO UPDATE SET "
            "received_at = excluded.received_at, event_name = excluded.event_name, "
            "status = CASE WHEN capture_receipts.status = 'failed' THEN 'pending' "
            "              ELSE capture_receipts.status END",
            (source_kind, source_ref, event_name, _now()))
        conn.commit()
        return {"status": "ok", "source_ref": source_ref}
    finally:
        if own:
            conn.close()


def drain_receipts(limit: int = 10, conn=None, fetch=None) -> dict:
    """Convierte acuses pendientes en eventos de captura.

    Aquí vive el fetch que el webhook deliberadamente NO hace: el request se
    queda en lo durable y barato, y el trabajo con red pasa a un lugar donde un
    fallo es reintentable y visible.
    """
    from .migrations.m16_capture_receipts import MAX_RECEIPT_ATTEMPTS
    own = conn is None
    conn = conn or db.get_conn()
    if fetch is None:
        from . import fireflies
        fetch = fireflies.fetch_transcript_rich
    fetched, failed = [], []
    try:
        rows = conn.execute(
            "SELECT source_ref, attempts FROM capture_receipts WHERE source_kind = 'fireflies' "
            "AND status = 'pending' AND attempts < ? ORDER BY received_at LIMIT ?",
            (MAX_RECEIPT_ATTEMPTS, limit)).fetchall()
        for ref, attempts in rows:
            try:
                transcript = fetch(ref)
            except Exception as e:
                transcript = None
                err = f"{type(e).__name__}: {e}"
            else:
                err = None if transcript else "transcript vacío"
            if transcript:
                res = ingest_fireflies_event(transcript, conn=conn)
                conn.execute(
                    "UPDATE capture_receipts SET status='fetched', fetched_at=?, event_id=?, "
                    "attempts=? WHERE source_kind='fireflies' AND source_ref=?",
                    (_now(), res.get("event_id"), attempts + 1, ref))
                fetched.append(ref)
            else:
                nxt = attempts + 1
                conn.execute(
                    "UPDATE capture_receipts SET status=?, attempts=?, last_error=? "
                    "WHERE source_kind='fireflies' AND source_ref=?",
                    ("failed" if nxt >= MAX_RECEIPT_ATTEMPTS else "pending",
                     nxt, (err or "")[:300], ref))
                failed.append(ref)
            conn.commit()
        return {"status": "ok", "fetched": fetched, "failed": failed}
    finally:
        if own:
            conn.close()


def poll_fireflies(limit: int = 25, conn=None, fetch=None) -> dict:
    """La red de reconciliación: barre lo que el webhook pudo no entregar.

    Camina de viejo a nuevo y **se detiene en la primera junta sin resumen**. Esa
    parada es la frontera de completitud: `summary.action_items` es toda nuestra
    evidencia, así que avanzar el watermark por encima de una junta a la que
    Fireflies todavía no le termina el resumen la perdería para siempre. El
    webhook cubre ese hueco mientras tanto, y la próxima corrida la recoge.
    """
    own = conn is None
    conn = conn or db.get_conn()
    if fetch is None:
        from . import fireflies
        fetch = fireflies.fetch_transcripts_rich
    try:
        wm = conn.execute(
            "SELECT last_seen_ts, last_seen_id FROM capture_watermarks WHERE source_kind='fireflies'"
        ).fetchone()
        last_ts, last_id = (wm[0], wm[1]) if wm else (None, None)
        try:
            transcripts = fetch(limit=limit)
        except Exception as e:
            return _error("poll_failed", f"{type(e).__name__}: {e}")

        def _ts(t):
            try:
                return int(float(t.get("date") or 0) / 1000)
            except (TypeError, ValueError):
                return 0

        # Orden compuesto (fecha, id): dos juntas en el mismo segundo no pueden
        # quedar en orden indefinido, que haría al watermark saltarse una.
        ordered = sorted(transcripts, key=lambda t: (_ts(t), t.get("id") or ""))
        ingested, stopped_at = [], None
        cand_ts, cand_id = last_ts, last_id
        for t in ordered:
            ts, tid = _ts(t), (t.get("id") or "")
            if last_ts is not None and (ts, tid) <= (last_ts, last_id or ""):
                continue
            summary = t.get("summary") or {}
            if not (summary.get("action_items") or "").strip():
                stopped_at = tid
                break
            res = ingest_fireflies_event(t, conn=conn)
            if res.get("created") or res.get("enriched"):
                ingested.append(res["event_id"])
            cand_ts, cand_id = ts, tid
        if (cand_ts, cand_id) != (last_ts, last_id):
            conn.execute(
                "INSERT INTO capture_watermarks (source_kind, last_seen_ts, last_seen_id, "
                "last_run_at) VALUES ('fireflies',?,?,?) ON CONFLICT(source_kind) DO UPDATE SET "
                "last_seen_ts=excluded.last_seen_ts, last_seen_id=excluded.last_seen_id, "
                "last_run_at=excluded.last_run_at", (cand_ts, cand_id, _now()))
        else:
            conn.execute(
                "INSERT INTO capture_watermarks (source_kind, last_run_at) VALUES ('fireflies',?) "
                "ON CONFLICT(source_kind) DO UPDATE SET last_run_at=excluded.last_run_at", (_now(),))
        conn.commit()
        return {"status": "ok", "ingested": ingested, "stopped_at_unsummarized": stopped_at,
                "scanned": len(ordered)}
    finally:
        if own:
            conn.close()


def tick(conn=None, budget=None) -> dict:
    """Una vuelta completa del metabolismo, acotada por presupuesto de tiempo.

    El presupuesto no es adorno: el worker que la invoca mata el proceso a los
    540 s, así que el tick tiene que terminar por su cuenta ANTES y dejar la cola
    en un estado consistente, en vez de que lo maten a media digestión.

    Cada etapa falla suave y se reporta: una etapa caída no debe impedir que las
    demás corran (un Ollama caído no debe bloquear el envío de tarjetas ya
    derivadas).
    """
    own = conn is None
    conn = conn or db.get_conn()
    budget = TICK_BUDGET_SECONDS if budget is None else budget
    started = _now()
    out = {"status": "ok", "digested": [], "digest_errors": [], "budget_exhausted": False}
    try:
        def _whatsapp():
            from . import whatsapp
            # Reconciliar ANTES de cosechar: si el webhook no llegó, el pulso lo
            # reconstruye el espejo, y la cosecha de este mismo tick ya lo ve. Al
            # revés se perdería un ciclo entero por cada aviso caído.
            rec = whatsapp.reconcile_activity(conn=conn)
            res = whatsapp.harvest_windows(conn=conn)
            res["reconciliado"] = rec
            return res

        for stage, fn in (("receipts", lambda: drain_receipts(conn=conn)),
                          ("poll", lambda: poll_fireflies(conn=conn)),
                          ("whatsapp", _whatsapp)):
            try:
                out[stage] = fn()
            except Exception as e:                            # pragma: no cover
                out[stage] = _error(f"{stage}_failed", f"{type(e).__name__}: {e}")

        while _now() - started < budget:
            # El presupuesto se comprueba ANTES de cada evento, pero un evento
            # puede tardar hasta DIGEST_TIMEOUT — así que sin acotar la llamada
            # al remanente, el tick se pasa del techo del worker y lo matan a
            # media digestión. Medido: 758 s con presupuesto de 480 s y techo de
            # 540 s. El timeout de cada llamada es lo que queda, no el máximo.
            remaining = budget - (_now() - started)
            if remaining < MIN_DIGEST_SECONDS:
                out["budget_exhausted"] = True
                break
            claim = claim_next_event(conn=conn)
            if not claim.get("event_id"):
                break
            res = digest_event(claim["event_id"], claim["lease_token"], conn=conn,
                               timeout=min(DIGEST_TIMEOUT, remaining))
            if res.get("status") == "ok":
                out["digested"].append(claim["event_id"])
                continue
            out["digest_errors"].append({"event_id": claim["event_id"],
                                         "code": res.get("code")})
            if res.get("code") in ("worker_unavailable", "worker_rate_limited"):
                # El evento volvió a la cola SIN gastar intento — que es lo
                # correcto — pero eso lo hace inmediatamente re-reclamable, así
                # que seguir aquí sería girar en caliente sobre el mismo evento
                # hasta agotar el presupuesto. Si el worker está caído, el
                # siguiente evento fallará igual: se corta la etapa y las demás
                # (decaimiento, tarjetas ya derivadas) siguen corriendo.
                out["worker_down"] = True
                break
        else:
            out["budget_exhausted"] = True

        for stage, fn in (("decay", lambda: decay_and_excrete(conn=conn)),
                          ("cards", lambda: dispatch_cards(conn=conn))):
            try:
                out[stage] = fn()
            except Exception as e:                            # pragma: no cover
                out[stage] = _error(f"{stage}_failed", f"{type(e).__name__}: {e}")
        out["elapsed"] = _now() - started
        return out
    finally:
        if own:
            conn.close()


def capture_status(conn=None) -> dict:
    """Salud del loop en NÚMEROS — sin títulos ni citas.

    Es la superficie que puede vivir en scope default (o sea, alcanzable desde
    internet con token): dice si el loop se atoró sin revelar una sola palabra de
    una conversación de cliente.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        by_status = dict(conn.execute(
            "SELECT digest_status, COUNT(*) FROM capture_events GROUP BY digest_status").fetchall())
        wm = conn.execute(
            "SELECT last_run_at, last_seen_ts FROM capture_watermarks WHERE source_kind='fireflies'"
        ).fetchone()
        return {
            "status": "ok",
            "events_by_status": by_status,
            "dead_letter": by_status.get("dead_letter", 0),
            "receipts_pending": conn.execute(
                "SELECT COUNT(*) FROM capture_receipts WHERE status='pending'").fetchone()[0],
            "objectives_open": conn.execute(
                "SELECT COUNT(*) FROM objectives WHERE status IN ('open','blocked')").fetchone()[0],
            "suggestions_open": conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0],
            "watermark_age_seconds": (_now() - wm[0]) if (wm and wm[0]) else None,
        }
    finally:
        if own:
            conn.close()


# ------------------------------ decidir (solo humano — nunca por MCP)

# El accept cruza a `hermes kanban create`, un efecto externo que no podemos
# deshacer. Por eso el claim es un DUEÑO de verdad, no una etiqueta: solo quien
# ganó el UPDATE condicional puede crear, y otro que llegue mientras el primero
# está en vuelo recibe un conflicto en vez de crear una segunda tarea.
ACCEPT_LEASE_SECONDS = int(os.environ.get("DIGESTION_ACCEPT_LEASE", "120"))


def _marker(sid: str) -> str:
    return SUGGESTION_MARKER.format(id=sid)


def _find_task_by_marker(conn, sid: str) -> Optional[str]:
    """La tarea que un accept anterior ya creó, si existe.

    `instr()` y no `LIKE`: en SQL el guion bajo es comodín, así que
    `LIKE '%[suggestion_ab12]%'` haría match con `[suggestionXab12]` — o sea,
    reconciliaría contra la tarea equivocada, que es peor que no reconciliar.
    """
    row = conn.execute(
        "SELECT id FROM tasks WHERE instr(COALESCE(body,''), ?) > 0 ORDER BY id LIMIT 1",
        (_marker(sid),)).fetchone()
    return row[0] if row else None


def list_suggestions(status: str = "open", limit: int = 100, conn=None) -> list:
    own = conn is None
    conn = conn or db.get_conn()
    try:
        where, args = "", []
        if status and status != "all":
            where, args = "WHERE s.status = ?", [status]
        rows = conn.execute(f"""
            SELECT s.id, s.kind, s.status, s.card_status, s.title, s.final_title, s.confidence,
                   s.seen_count, s.bucket, s.task_id, s.proposed_project_id, s.proposed_due,
                   s.edited, s.created_at, s.decided_at, s.decided_via,
                   o.id, o.title, o.owner, o.waiting_on, o.due_hint, o.status, o.task_id
            FROM suggestions s JOIN objectives o ON o.id = s.objective_id
            {where} ORDER BY s.created_at DESC LIMIT ?""", args + [limit]).fetchall()
        out = []
        for r in rows:
            ev = conn.execute(
                "SELECT e.quote, e.speaker, c.title FROM objective_evidence e "
                "LEFT JOIN capture_events c ON c.event_id = e.event_id "
                "WHERE e.objective_id = ? ORDER BY e.id DESC LIMIT 1", (r[16],)).fetchone()
            out.append({
                "id": r[0], "kind": r[1], "status": r[2], "card_status": r[3],
                "title": r[5] or r[4], "confidence": r[6], "seen_count": r[7], "bucket": r[8],
                "task_id": r[9] or r[22], "proposed_project_id": r[10], "proposed_due": r[11],
                "edited": bool(r[12]), "created_at": r[13], "decided_at": r[14],
                "decided_via": r[15],
                "objective": {"id": r[16], "title": r[17], "owner": r[18],
                              "waiting_on": r[19], "due_hint": r[20], "status": r[21]},
                "evidence": ({"quote": ev[0], "speaker": ev[1], "meeting": ev[2]} if ev else None),
            })
        return out
    finally:
        if own:
            conn.close()


def list_objectives(status: str = None, entity_kind: str = None, entity_id: str = None,
                    limit: int = 100, conn=None) -> list:
    own = conn is None
    conn = conn or db.get_conn()
    try:
        clauses, args = [], []
        if status and status != "all":
            clauses.append("status = ?"); args.append(status)
        elif not status:
            clauses.append("status IN ('open','blocked')")
        if entity_kind:
            clauses.append("entity_kind = ?"); args.append(entity_kind)
        if entity_id:
            clauses.append("entity_id = ?"); args.append(entity_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT id, title, status, owner, waiting_on, due_hint, prominence, task_id, "
            f"entity_kind, entity_id, opened_at, last_evidence_ts FROM objectives {where} "
            f"ORDER BY prominence DESC, opened_at DESC LIMIT ?", args + [limit]).fetchall()
        cols = ("id", "title", "status", "owner", "waiting_on", "due_hint", "prominence",
                "task_id", "entity_kind", "entity_id", "opened_at", "last_evidence_ts")
        return [dict(zip(cols, r)) for r in rows]
    finally:
        if own:
            conn.close()


def dismiss_suggestion(sid: str, via: str = "dashboard", conn=None) -> dict:
    """Descartar es pegajoso: `_derive_suggestion` nunca revive un `dismissed`.

    Se niega durante un accept EN VUELO — descartar mientras el otro camino está
    creando una tarea dejaría una tarea huérfana sin sugerencia que la explique.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        row = conn.execute("SELECT status, decided_at FROM suggestions WHERE id = ?",
                           (sid,)).fetchone()
        if row is None:
            return _error("unknown_suggestion", f"sugerencia {sid!r} no existe")
        if row[0] == "dismissed":
            return {"status": "ok", "id": sid, "already": True}
        if row[0] == "accepting" and _accept_lease_is_live(conn, sid, now):
            return _error("accept_in_flight", "hay un accept en vuelo; espera a que resuelva")
        if row[0] == "accepted":
            return _error("already_accepted", "ya fue aceptada; cierra la tarea en su lugar")
        conn.execute(
            "UPDATE suggestions SET status='dismissed', decided_at=?, decided_via=?, updated_at=? "
            "WHERE id = ?", (now, via, now, sid))
        conn.commit()
        return {"status": "ok", "id": sid}
    finally:
        if own:
            conn.close()


def _accept_lease_is_live(conn, sid: str, now: int) -> bool:
    """Un `accepting` viejo es un intento que murió, no uno en curso."""
    row = conn.execute("SELECT updated_at FROM suggestions WHERE id = ?", (sid,)).fetchone()
    return bool(row and row[0] and (now - row[0]) < ACCEPT_LEASE_SECONDS)


def edit_suggestion(sid: str, title: str = None, project_id: str = None, due: str = None,
                    conn=None) -> dict:
    """Editar antes de aceptar. La edición es la señal de entrenamiento más rica
    que da el humano, así que se guarda aparte (`final_title`) en vez de pisar el
    título propuesto: comparar ambos es lo que dice en qué se equivoca el miner."""
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        row = conn.execute("SELECT status FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return _error("unknown_suggestion", f"sugerencia {sid!r} no existe")
        if row[0] != "open":
            return _error("not_open", f"solo se edita una sugerencia abierta (está {row[0]})")
        sets, args = ["edited = 1", "updated_at = ?"], [now]
        if title is not None:
            t = title.strip()
            if not t or len(t) > MAX_TITLE:
                return _error("bad_title", f"título vacío o >{MAX_TITLE}")
            sets.append("final_title = ?"); args.append(t)
        if project_id is not None:
            if project_id and not conn.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                return _error("unknown_project", f"proyecto {project_id!r} no existe")
            sets.append("proposed_project_id = ?"); args.append(project_id or None)
        if due is not None:
            sets.append("proposed_due = ?"); args.append(due or None)
        args.append(sid)
        conn.execute(f"UPDATE suggestions SET {', '.join(sets)} WHERE id = ?", args)
        conn.commit()
        return {"status": "ok", "id": sid}
    finally:
        if own:
            conn.close()


def accept_suggestion(sid: str, overrides: dict = None, via: str = "dashboard", conn=None) -> dict:
    """El gate humano: la sugerencia se vuelve tarea. Nunca por MCP.

    La saga tiene tres propiedades que la hacen segura de reintentar:

      * **Un solo dueño.** El claim es un UPDATE condicional; quien lo pierde no
        crea nada. Un segundo click mientras el primero está en vuelo recibe
        conflicto en lugar de crear una tarea gemela.
      * **Reconciliación por marcador.** Antes de crear se busca el marcador en
        `tasks.body`: si un intento anterior murió DESPUÉS de crear, se adopta
        esa tarea en vez de duplicarla.
      * **Las preferencias se guardan antes del efecto**, así un crash a mitad no
        pierde lo que el humano editó.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        if overrides:
            res = edit_suggestion(sid, title=overrides.get("title"),
                                  project_id=overrides.get("project_id"),
                                  due=overrides.get("due"), conn=conn)
            if res.get("status") == "error" and res.get("code") != "not_open":
                return res

        row = conn.execute(
            "SELECT status, kind, objective_id, COALESCE(final_title, title), "
            "proposed_project_id, proposed_due, task_id FROM suggestions WHERE id = ?",
            (sid,)).fetchone()
        if row is None:
            return _error("unknown_suggestion", f"sugerencia {sid!r} no existe")
        status, kind, oid, title, project_id, due, existing_task = row
        if status == "accepted":
            return {"status": "ok", "id": sid, "task_id": existing_task, "replay": True}
        if status == "dismissed":
            return _error("already_dismissed", "fue descartada; no se puede aceptar")
        if status == "accepting" and _accept_lease_is_live(conn, sid, now):
            return _error("accept_in_flight", "otro accept está en vuelo para esta sugerencia")

        claimed = conn.execute(
            "UPDATE suggestions SET status='accepting', accept_op_id=?, updated_at=? "
            "WHERE id = ? AND status IN ('open','expired','accepting')",
            (f"op_{now}", now, sid)).rowcount
        conn.commit()
        if not claimed:
            return _error("claim_lost", "otro proceso tomó esta sugerencia")

        if kind == "close_task":
            return _finalize_close(conn, sid, oid, via, now, own=False)
        return _finalize_create(conn, sid, oid, kind, title, project_id, due, via, now)
    finally:
        if own:
            conn.close()


def _finalize_create(conn, sid, oid, kind, title, project_id, due, via, now) -> dict:
    task_id = _find_task_by_marker(conn, sid)     # ¿un intento anterior ya creó?
    adopted = task_id is not None
    if not adopted:
        ev = conn.execute(
            "SELECT e.quote, e.speaker, c.title FROM objective_evidence e "
            "LEFT JOIN capture_events c ON c.event_id = e.event_id "
            "WHERE e.objective_id = ? ORDER BY e.id DESC LIMIT 1", (oid,)).fetchone()
        lines = []
        if ev and ev[0]:
            origin = " · ".join(x for x in (ev[1], ev[2]) if x)
            lines.append(f'> "{ev[0]}"' + (f" — {origin}" if origin else ""))
        if kind == "unblock":
            wo = conn.execute("SELECT waiting_on FROM objectives WHERE id = ?", (oid,)).fetchone()
            if wo and wo[0]:
                lines.append(f"Esperando a: {wo[0]}")
        lines.append(_marker(sid))
        argv = [db.hermes_bin(), "kanban", "create",
                (f"Destrabar: {title}" if kind == "unblock" else title),
                "--json", "--assignee", "ricardo", "--created-by", "digestion",
                "--body", "\n\n".join(lines)]
        code, out, err = _run_cli(argv)
        if code == 124:
            # Ambiguo: la tarea pudo crearse. Se queda en `accepting` para que la
            # reconciliación del próximo intento lo resuelva leyendo el marcador,
            # en vez de crear una gemela a ciegas.
            return _error("accept_ambiguous", "timeout creando la tarea; se reintentará")
        if code != 0:
            conn.execute("UPDATE suggestions SET status='open', updated_at=? WHERE id = ?",
                         (now, sid))
            conn.commit()
            return _error("create_failed", (err or out or "")[:200])
        task_id = _parse_task_id(out) or _find_task_by_marker(conn, sid)
        if not task_id:
            return _error("task_id_unknown", "la tarea se creó pero no se pudo leer su id")

    # El archivado se REPORTA, no se traga. La tarea ya existe y eso no se
    # deshace, así que fallar aquí no puede abortar el accept — pero decir
    # "Aceptada" cuando el proyecto no aterrizó manda la tarea a Inbox sin que
    # el operador lo sepa, y una tarea que cree archivada y no lo está es peor que
    # una que sabe que quedó suelta.
    project_applied, project_error = (None, None)
    if project_id:
        try:
            from . import sprints
            res = sprints.assign_task_project(task_id, project_id)
            if isinstance(res, dict) and res.get("status") == "error":
                project_applied, project_error = False, (res.get("error") or res.get("code"))
            else:
                project_applied = True
        except Exception as e:
            project_applied, project_error = False, f"{type(e).__name__}: {e}"
    if due:
        try:
            from . import sprints
            sprints.update_task_fields(task_id, due_date=due)
        except Exception:
            pass

    conn.execute("UPDATE suggestions SET status='accepted', task_id=?, decided_at=?, "
                 "decided_via=?, updated_at=? WHERE id = ?", (task_id, now, via, now, sid))
    # El objetivo guarda su tarea: es lo que después permite que un
    # `objective.complete` derive un `close_task` que sepa qué cerrar.
    conn.execute("UPDATE objectives SET task_id = COALESCE(task_id, ?), updated_at = ? "
                 "WHERE id = ?", (task_id, now, oid))
    conn.commit()
    out = {"status": "ok", "id": sid, "task_id": task_id, "adopted": adopted}
    if project_id:
        out["project_applied"] = project_applied
        if project_error:
            out["project_error"] = str(project_error)[:200]
    return out


def _finalize_close(conn, sid, oid, via, now, own=False) -> dict:
    task_id = conn.execute("SELECT task_id FROM objectives WHERE id = ?", (oid,)).fetchone()
    task_id = task_id[0] if task_id else None
    closed = False
    if task_id:
        try:
            from . import sprints
            # Sidecar sancionado, NO `hermes kanban complete`: ese sale 0 aunque
            # falle (lección registrada en la memoria del proyecto).
            sprints.set_task_status(task_id, "done")
            closed = True
        except Exception as e:
            conn.execute("UPDATE suggestions SET status='open', updated_at=? WHERE id = ?",
                         (now, sid))
            conn.commit()
            return _error("close_failed", f"{type(e).__name__}: {e}")
    conn.execute("UPDATE suggestions SET status='accepted', task_id=?, decided_at=?, "
                 "decided_via=?, updated_at=? WHERE id = ?",
                 (task_id, now, via if task_id else f"{via}:sin-tarea", now, sid))
    conn.commit()
    return {"status": "ok", "id": sid, "task_id": task_id, "closed": closed}


def _parse_task_id(out: str) -> Optional[str]:
    try:
        payload = json.loads((out or "").strip())
        tid = (payload.get("id") or payload.get("task_id")
               or (payload.get("task") or {}).get("id"))
        if tid:
            return tid
    except (TypeError, ValueError):
        pass
    m = re.search(r"\bt_[0-9a-f]{6,}\b", out or "")
    return m.group(0) if m else None


def relink_event(event_id: str, conn=None) -> dict:
    """Pide al modelo SOLO el vínculo a proyecto de un evento ya digerido.

    Re-digerir completo sería más caro y más riesgoso: el modelo vería los
    objetivos que él mismo extrajo de esa junta y podría duplicarlos. Aquí se
    reusa el mismo turno y el mismo catálogo, pero se aplica ÚNICAMENTE
    `entity.link` — las demás operaciones se descartan sin tocar nada, porque el
    estado del evento ya se decidió en su digestión original.

    Existe también para el caso normal a futuro: cuando el operador da de alta un
    proyecto nuevo, las juntas viejas que hablaban de él pueden reconocerse.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        row = conn.execute(
            "SELECT entity_kind, entity_id, payload FROM capture_events WHERE event_id = ?",
            (event_id,)).fetchone()
        if row is None:
            return _error("unknown_event", f"evento {event_id!r} no existe")
        if row[0] and row[1]:
            return {"status": "ok", "event_id": event_id, "skipped": "ya anclado"}
        if not row[2]:
            return {"status": "ok", "event_id": event_id, "skipped": "verbatim purgado"}

        data = build_digest_input(conn, event_id)
        if data is None or not data.get("proyectos_candidatos"):
            return {"status": "ok", "event_id": event_id, "skipped": "sin candidatos"}
        argv = [_oll_bin(), "Sigue las instrucciones del sistema sobre la ENTRADA de stdin.",
                "--model", DIGEST_MODEL, "--system", _prompt_doc(),
                "--max-tokens", DIGEST_MAX_TOKENS, "--temperature", "0"]
        code, out, err = _run_worker(argv, json.dumps(data, ensure_ascii=False), DIGEST_TIMEOUT)
        if code != 0 or _is_rate_limited(out, err):
            return _error("worker_unavailable", f"oll: {(err or out or '')[:120]}")
        ops = _extract_ops_json(out)
        if ops is None:
            return _error("unparseable", (out or "")[:120])

        links = [o for o in ops if isinstance(o, dict) and o.get("op") == "entity.link"]
        if not links:
            return {"status": "ok", "event_id": event_id, "linked": None}
        verdict, reason, _ = _validate_op(conn, links[0], json.loads(row[2]), None)
        if verdict != "applied":
            return {"status": "ok", "event_id": event_id, "linked": None, "rechazo": reason}
        conn.execute("UPDATE capture_events SET entity_kind = ?, entity_id = ? WHERE event_id = ?",
                     (links[0]["entity_kind"], links[0]["entity_id"], event_id))
        # Los objetivos de ese evento heredan el ancla que les faltaba, y sus
        # sugerencias abiertas ganan el proyecto: si no, el vínculo se quedaría
        # en el evento sin llegar a la tarjeta, que es donde el operador lo ve.
        oids = [r[0] for r in conn.execute(
            "SELECT DISTINCT objective_id FROM objective_evidence WHERE event_id = ?",
            (event_id,))]
        for oid in oids:
            conn.execute("UPDATE objectives SET entity_kind = ?, entity_id = ? "
                         "WHERE id = ? AND entity_id IS NULL",
                         (links[0]["entity_kind"], links[0]["entity_id"], oid))
            pid = resolve_project_for_objective(conn, oid)
            if pid:
                conn.execute("UPDATE suggestions SET proposed_project_id = ? "
                             "WHERE objective_id = ? AND status = 'open' "
                             "AND proposed_project_id IS NULL", (pid, oid))
        conn.commit()
        return {"status": "ok", "event_id": event_id,
                "linked": [links[0]["entity_kind"], links[0]["entity_id"]],
                "objectives": len(oids)}
    finally:
        if own:
            conn.close()
