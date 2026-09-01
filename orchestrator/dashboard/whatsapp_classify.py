"""Clasificador de chats: propone cuáles son de negocio. NUNCA otorga permiso.

El operador tiene 1143 chats y 428 grupos. Elegir a mano cuáles puede leer el sistema
no es realista, y aprobar en bloque sin entender es como alguien termina dándole
acceso a las conversaciones de su familia. Este módulo produce una PROPUESTA
ordenada y con motivo; la decisión sigue siendo suya.

La asimetría que gobierna cada umbral: un falso positivo hace que el sistema lea
una conversación privada, y leer no se deshace. Un falso negativo solo retrasa
algo que él puede habilitar en dos clics. Por eso `incierto` es una respuesta de
primera clase y no un empujón hacia "sí".

Tres señales, de la más barata y verificable a la más inferencial:

  1. **CRM** — el nombre del chat empata con un contacto suyo que cuelga de una
     cuenta. Es un hecho, no una opinión, y por eso gana sobre todo lo demás.
     (Medido: rinde poco hoy — 2 empates exactos, 4 parciales — porque su CRM
     casi no tiene teléfonos. Se queda porque cuando acierta, acierta duro.)
  2. **Patrón B2B** — el nombre trae `<>`, la convención que el operador usa para
     chats entre empresas ("Acme <> Empresa B"). Determinista y muy preciso.
  3. **Modelo** — lee SOLO el nombre del chat, nunca los mensajes, y clasifica.
     Es la única señal con alcance real (531 nombres legibles) y la única
     inferencial. Se marca como tal para que el humano sepa qué está revisando.

Los chats cuyo "nombre" es un número de teléfono no se clasifican: no hay señal
que leer, y quedan denegados hasta que el operador diga otra cosa.
"""
import json
import os
import re
import subprocess
import time
import unicodedata
from typing import Optional

from . import db
from . import whatsapp as wa

CLASSIFY_MODEL = os.environ.get("WHATSAPP_CLASSIFY_MODEL", "glm-5.3")
# Opt-in effort knob; unset by default so the argv below is unchanged. See
# digestion.py for why a level must be measured per model before being pinned.
CLASSIFY_EFFORT = os.environ.get("WHATSAPP_CLASSIFY_EFFORT") or None
# Medido: 20 nombres → 13 s. Lotes más grandes amortizan la latencia; demasiado
# grandes y el modelo empieza a perder nombres del final.
BATCH = int(os.environ.get("WHATSAPP_CLASSIFY_BATCH", "40"))
# El presupuesto de tokens tiene que dejar espacio al razonamiento: con 4096 la
# respuesta salió VACÍA (medido 2026-08-04), que el gate leería como malformada.
CLASSIFY_MAX_TOKENS = os.environ.get("WHATSAPP_CLASSIFY_MAX_TOKENS", "16384")
# El vocabulario cerrado. Lo que caiga fuera se descarta; no se aproxima.
CLASES = ("negocio", "personal", "incierto")
CLASSIFY_TIMEOUT = int(os.environ.get("WHATSAPP_CLASSIFY_TIMEOUT", "300"))

VERDICTS = ("negocio", "personal", "incierto")


def _now() -> int:
    return int(time.time())


def _oll_bin() -> str:
    import shutil
    from pathlib import Path
    return shutil.which("oll") or str(Path.home() / ".local" / "bin" / "oll")


def _run_worker(argv: list, stdin_text: str, timeout: int) -> tuple:
    try:
        p = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError as e:
        return 127, "", f"{argv[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout {timeout}s"
    except Exception as e:                                    # pragma: no cover
        return 1, "", f"{type(e).__name__}: {e}"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def is_phone_name(name: str, jid: str) -> bool:
    """¿El 'nombre' es en realidad un número? Entonces no hay nada que clasificar."""
    n = (name or "").strip()
    return (not n) or n == jid or bool(re.fullmatch(r"[\d+@.\-\s]+", n))


# ------------------------------------------------------- señales deterministas

def _contiene_frase(hay: str, aguja: str) -> bool:
    """¿`aguja` aparece en `hay` como palabras completas?

    Sin la frontera, "Antonio" empata dentro de "San Antonio Team" y el chat de
    un equipo se vuelve el contacto de un cliente. Medido: ese falso positivo
    salió en la primera corrida real."""
    if not aguja:
        return False
    return re.search(r"(?:^| )" + re.escape(aguja) + r"(?:$| )", hay) is not None


def crm_match(conn, name: str) -> Optional[tuple]:
    """(motivo, cuenta) si el nombre del chat es un contacto del CRM.

    Dos formas de empatar, y la diferencia entre ellas es deliberada:

    - **Exacta**: el nombre del chat ES el del contacto. Es un hecho.
    - **Contenida**: el nombre del contacto aparece completo dentro del nombre
      del chat ("Ana P. Acme" ⊃ "Ana Pérez"), pero **solo si el contacto
      tiene nombre y apellido**. Un nombre de pila suelto empatando dentro de
      cualquier cadena más larga es una conjetura, y este carril es el único que
      se puede aprobar en bloque — una conjetura ahí cuesta privacidad de golpe.
    """
    n = _norm(name)
    if len(n) < 5:
        return None
    try:
        rows = conn.execute(
            "SELECT c.name, a.name FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id "
            "WHERE COALESCE(c.name,'') != ''").fetchall()
    except Exception:
        return None
    for cname, acc in rows:
        kn = _norm(cname)
        if len(kn) < 5:
            continue
        if kn == n:
            return (f"contacto del CRM: {cname}" + (f" ({acc})" if acc else ""), acc)
        if len(kn.split()) >= 2 and _contiene_frase(n, kn):
            return (f"contiene al contacto {cname}" + (f" ({acc})" if acc else ""), acc)
    return None


def entity_match(conn, name: str) -> Optional[str]:
    """El nombre del chat menciona una cuenta, proyecto o deal vivo."""
    n = _norm(name)
    if len(n) < 4:
        return None
    for table, label in (("accounts", "cuenta"), ("projects", "proyecto"), ("deals", "trato")):
        try:
            rows = conn.execute(f"SELECT name FROM {table}").fetchall()
        except Exception:
            continue
        for (ename,) in rows:
            en = _norm(ename)
            # Frontera de palabra también aquí: sin ella "Coppel" empata dentro de
            # "BanCoppel", que es otra empresa. Si de verdad es de negocio, el
            # modelo lo dirá y se revisa de uno en uno — que es donde va una
            # coincidencia que en realidad es un parecido.
            if len(en) >= 4 and _contiene_frase(n, en):
                return f"menciona {label}: {ename}"
    return None


def b2b_pattern(name: str) -> Optional[str]:
    """`A <> B` es la convención del operador para chats entre empresas.

    Determinista y muy preciso: nadie nombra así un chat familiar. Aparece
    también en sus títulos de Fireflies, así que es su vocabulario, no una
    heurística inventada.
    """
    if re.search(r"\S\s*<>\s*\S", name or ""):
        return "patrón «A <> B» de chat entre empresas"
    return None


# ----------------------------------------------------------- señal del modelo

_SYSTEM = """Clasificas chats de WhatsApp del operador, un consultor independiente.
Solo ves el NOMBRE del chat, nunca los mensajes.

Clases:
  negocio  — cliente, proveedor, proyecto, equipo de trabajo, facturación, evento
             o comunidad profesional
  personal — familia, amigos, salud, escuela, social, celebraciones
  incierto — un nombre de persona sin más contexto, o cualquier cosa que no
             permita decidir

REGLA DURA: ante la duda, "incierto". Marcar personal como negocio haría que un
sistema lea una conversación privada, y eso no se deshace; marcar de menos solo
retrasa algo recuperable.

Responde SOLO JSON, sin markdown:
{"r":[{"n":"<nombre tal cual>","c":"negocio|personal|incierto","p":"<motivo, 6 palabras>"}]}"""


def _extract(text: str) -> Optional[list]:
    if not text or not text.strip():
        return None
    for cand in (text.strip(), *(m.group(0) for m in re.finditer(r"\{.*\}", text, re.S))):
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("r"), list):
            return obj["r"]
    return None


def classify_names(names: list) -> Optional[dict]:
    """{nombre: (clase, motivo)} para un lote. None si el worker falló."""
    if not names:
        return {}
    payload = _SYSTEM + "\n\nCHATS:\n" + "\n".join(names)
    argv = [_oll_bin(), "Sigue la instrucción de arriba.", "--model", CLASSIFY_MODEL,
            "--max-tokens", CLASSIFY_MAX_TOKENS, "--temperature", "0"]
    if CLASSIFY_EFFORT:
        argv += ["--effort", CLASSIFY_EFFORT]
    code, out, err = _run_worker(argv, payload, CLASSIFY_TIMEOUT)
    if code != 0:
        return None
    rows = _extract(out)
    if rows is None:
        return None
    got = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        n, c = r.get("n"), r.get("c")
        # Una clase fuera del vocabulario NO se aproxima a la más cercana: se
        # descarta. Adivinar aquí es adivinar sobre privacidad.
        if isinstance(n, str) and c in VERDICTS:
            got[n.strip()] = (c, str(r.get("p") or "")[:120])
    return got


# ------------------------------------------------------------- orquestación

def sync_chats_from_mirror(conn=None) -> dict:
    """Da de alta en la allowlist los chats del espejo, TODOS denegados.

    Verlos no es leerlos: sin esta fila el operador no puede decidir sobre un chat
    que ni sabe que existe. El nombre se copia aquí porque es lo único que el
    clasificador y la revisión necesitan — el contenido se queda en el espejo.
    """
    own = conn is None
    conn = conn or db.get_conn()
    now = _now()
    try:
        mirror = wa._mirror()
        if mirror is None:
            return {"status": "error", "code": "sin_espejo"}
        try:
            rows = mirror.execute("SELECT jid, name, kind FROM chats").fetchall()
        finally:
            mirror.close()
        nuevos = 0
        for jid, name, kind in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO whatsapp_chats (jid, allowed, is_group, chat_name, "
                "created_at) VALUES (?, 0, ?, ?, ?)",
                (jid, 1 if (kind == "group" or jid.endswith("@g.us")) else 0, name, now))
            nuevos += cur.rowcount
            conn.execute("UPDATE whatsapp_chats SET chat_name = ? WHERE jid = ? AND "
                         "COALESCE(chat_name,'') = ''", (name, jid))
        conn.commit()
        return {"status": "ok", "total": len(rows), "nuevos": nuevos}
    finally:
        if own:
            conn.close()


def _write_verdict(conn, jid, verdict, reason, source, now):
    """El veredicto NUNCA toca `allowed`. Esa columna es del operador."""
    conn.execute(
        "UPDATE whatsapp_chats SET verdict = ?, verdict_reason = ?, verdict_source = ?, "
        "verdict_at = ? WHERE jid = ?", (verdict, reason[:200], source, now, jid))


def classify_pending(limit: int = 600, conn=None, classifier=None) -> dict:
    """Clasifica los chats sin veredicto. Deterministas primero, modelo después.

    El orden importa: un empate con el CRM es un hecho verificable y no tiene
    caso gastarle una llamada al modelo, ni dejar que una inferencia lo pise.
    """
    own = conn is None
    conn = conn or db.get_conn()
    classifier = classifier or classify_names
    now = _now()
    out = {"crm": 0, "entidad": 0, "b2b": 0, "modelo": 0, "sin_nombre": 0, "fallos": 0}
    try:
        pend = conn.execute(
            "SELECT jid, chat_name, is_group FROM whatsapp_chats WHERE verdict IS NULL "
            "LIMIT ?", (limit,)).fetchall()

        para_modelo = []
        for jid, name, is_group in pend:
            if is_phone_name(name, jid):
                # Sin nombre no hay señal. Se marca `incierto` para que no vuelva
                # a intentarse en cada corrida, y queda denegado como todo.
                _write_verdict(conn, jid, "incierto", "sin nombre, solo número", "modelo", now)
                out["sin_nombre"] += 1
                continue
            hit = crm_match(conn, name)
            if hit:
                _write_verdict(conn, jid, "negocio", hit[0], "crm", now)
                out["crm"] += 1
                continue
            pat = b2b_pattern(name)
            if pat:
                _write_verdict(conn, jid, "negocio", pat, "patron_b2b", now)
                out["b2b"] += 1
                continue
            ent = entity_match(conn, name)
            if ent:
                _write_verdict(conn, jid, "negocio", ent, "nombre_entidad", now)
                out["entidad"] += 1
                continue
            para_modelo.append((jid, name, is_group))
        conn.commit()

        for i in range(0, len(para_modelo), BATCH):
            lote = para_modelo[i:i + BATCH]
            etiquetas = [f"{n} [{'grupo' if g else '1a1'}]" for _, n, g in lote]
            got = classifier(etiquetas)
            if got is None:
                out["fallos"] += len(lote)
                continue
            for (jid, name, g), etiqueta in zip(lote, etiquetas):
                # El modelo puede devolver el nombre con o sin la etiqueta de tipo.
                r = got.get(etiqueta) or got.get(name)
                if r is None or r[0] not in CLASES:
                    # Una clase fuera de vocabulario se descarta entera. Redondearla
                    # a la más parecida sería adivinar, y aquí adivinar de más
                    # significa proponer que se lea algo privado.
                    out["fallos"] += 1
                    continue
                _write_verdict(conn, jid, r[0], r[1], "modelo", now)
                out["modelo"] += 1
            conn.commit()
        return {"status": "ok", **out}
    finally:
        if own:
            conn.close()


def proposals(conn=None, limit: int = 300) -> list:
    """La propuesta para revisión: qué cree el clasificador y por qué.

    Ordenada por lo que más le conviene ver primero al operador — negocio con
    señal dura arriba, y dentro de eso lo que más se mueve, porque un chat
    activo cambia más su día que uno muerto.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        rows = conn.execute("""
            SELECT c.jid, c.chat_name, c.is_group, c.allowed, c.verdict, c.verdict_reason,
                   c.verdict_source, COALESCE(a.message_count,0), a.last_seen_at
            FROM whatsapp_chats c LEFT JOIN whatsapp_activity a ON a.jid = c.jid
            ORDER BY
              CASE c.verdict WHEN 'negocio' THEN 0 WHEN 'incierto' THEN 1 ELSE 2 END,
              CASE c.verdict_source WHEN 'crm' THEN 0 WHEN 'patron_b2b' THEN 1
                                    WHEN 'nombre_entidad' THEN 2 ELSE 3 END,
              COALESCE(a.last_seen_at, 0) DESC
            LIMIT ?""", (limit,)).fetchall()
        cols = ("jid", "name", "is_group", "allowed", "verdict", "reason", "source",
                "message_count", "last_seen_at")
        return [dict(zip(cols, r)) for r in rows]
    finally:
        if own:
            conn.close()
