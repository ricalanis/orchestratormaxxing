"""Test-only dashboard launcher for the Playwright e2e spec.

Serves the real FastAPI app against a COPY of ~/.hermes/kanban.db with every
cycle wiped — a blank first-run slate — so tests/board-empty-state.spec.js can
drive the empty-state → CTA → board flow without ever touching the real DB.
Playwright's webServer runs this; for a manual run set TEST_PORT.

NEVER point this at the real DB: it deletes all sprints.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make `dashboard` importable however this is launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The e2e suite mutates via Playwright's request fixture, which carries no
# Bearer token. Use the app's own test bypass (MutatingAuthMiddleware reads
# TESTING per-request) so the suite is hermetic: without this, whether the
# tests pass depends on ~/.config/orchestratormaxxing/dashboard-token existing on
# the machine — green on a tokenless box, 401s (silently ignored by the
# helpers) everywhere else. Must be set BEFORE importing dashboard.api.
os.environ.setdefault("TESTING", "1")

_REAL = Path.home() / ".hermes" / "kanban.db"
_PORT = int(os.environ.get("TEST_PORT", "8931"))

fd, _tmp = tempfile.mkstemp(prefix="kanban_e2e_", suffix=".db")
os.close(fd)
shutil.copy(_REAL, _tmp)
_conn = sqlite3.connect(_tmp)
_conn.execute("UPDATE tasks SET sprint_id = NULL")
_conn.execute("DELETE FROM task_sprints")
_conn.execute("DELETE FROM sprints")

# WhatsApp: la cola de revisión se siembra a mano en la COPIA. Las decisiones del
# operador son reales y ya vaciaron la cola, así que un spec que dependiera de
# ellas probaría el estado de hoy en vez del contrato — y dejaría de probar nada
# el día que él decida distinto. Los nombres son sintéticos: ninguno sale de una
# conversación suya.
try:
    _conn.execute("UPDATE whatsapp_chats SET decided_at = COALESCE(decided_at, 1)")
    for _jid, _nom, _v, _src, _mot in (
            ("e2e-b2b@g.us", "Acme <> Hacsys", "negocio", "patron_b2b",
             "patrón «A <> B» de chat entre empresas"),
            ("e2e-crm@s.whatsapp.net", "Contacto Demo", "negocio", "crm",
             "contacto del CRM: Contacto Demo"),
            ("e2e-modelo@g.us", "Equipo Sintético", "negocio", "modelo",
             "un modelo leyó el nombre"),
            ("e2e-personal@g.us", "Personal Sintético", "personal", "modelo", "familiar")):
        _conn.execute(
            "INSERT OR REPLACE INTO whatsapp_chats (jid, allowed, is_group, created_at, "
            "chat_name, verdict, verdict_source, verdict_reason, decided_at) "
            "VALUES (?,0,?,1,?,?,?,?,NULL)",
            (_jid, 1 if _jid.endswith("@g.us") else 0, _nom, _v, _src, _mot))
except sqlite3.OperationalError:
    pass          # DB anterior a m20/m21: el spec de WhatsApp se salta solo.

# Sugerencias: mismo motivo que arriba. La cita es sintética a propósito — un
# fixture con habla real de un cliente convierte cada corrida de los tests, y
# cada captura de pantalla de un fallo, en una copia más de esa conversación.
try:
    _conn.execute(
        "INSERT OR REPLACE INTO capture_events (event_id, source_kind, source_ref, title, "
        "captured_at, digest_status) VALUES ('e2e_ev','fireflies','e2e','Junta Demo',1,'digested')")
    _conn.execute(
        "INSERT OR REPLACE INTO objectives (id, title, owner, status, opened_at, updated_at) "
        "VALUES ('e2e_obj','Mandar la cotización revisada','ricardo','open',1,1)")
    _conn.execute(
        "INSERT OR REPLACE INTO objective_evidence (objective_id, event_id, quote, speaker, "
        "created_at) VALUES ('e2e_obj','e2e_ev','Va, te la mando el jueves.','Op',1)")
    _conn.execute(
        "INSERT OR REPLACE INTO suggestions (id, objective_id, kind, status, title, "
        "confidence, created_at, updated_at) VALUES "
        "('e2e_sug','e2e_obj','create_task','open','Mandar la cotización revisada',0.9,1,1)")
except sqlite3.OperationalError:
    pass          # DB anterior a m15: el spec de sugerencias se salta solo.

_conn.commit()
_conn.close()

# Redirect every DB layer at the wiped copy BEFORE importing the app (its import
# runs ensure_schema()).
from dashboard import db, sprints  # noqa: E402

db.KANBAN_DB = Path(_tmp)
sprints.KANBAN_DB = Path(_tmp)

import uvicorn  # noqa: E402
from dashboard.api import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=_PORT, log_level="warning")
