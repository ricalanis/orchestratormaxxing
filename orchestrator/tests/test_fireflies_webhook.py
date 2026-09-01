"""El webhook de Fireflies: la única ruta pública que escribe.

Vive en el servicio expuesto por Funnel, o sea que su superficie es internet
entero. Tres propiedades tienen que sostenerse o la ruta es un pasivo:

  * **La firma se verifica ANTES de cualquier trabajo.** No basta con devolver
    401 al final: si un desconocido puede hacernos buscar transcripts o escribir
    filas antes del rechazo, la firma no está protegiendo nada. Por eso el test
    afirma que el seam de escritura NO fue llamado.
  * **Falla cerrada.** Sin secreto configurado responde 503, nunca "modo dev".
    El bearer del MCP puede permitirse modo dev porque su default es lectura
    segura; esta ruta escribe.
  * **`meeting.transcribed` se ignora.** Llega antes de que exista el resumen, y
    el resumen es toda nuestra evidencia — actuar sobre él capturaría vacío.
"""
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

import mcp_sse_server as sse  # noqa: E402
from dashboard import fireflies as ff  # noqa: E402
from dashboard import digestion as dg  # noqa: E402

SECRET = "secreto-de-prueba"
BODY = {"eventType": "meeting.summarized", "meetingId": "tr_abc", "timestamp": 1785900000000}


def sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(sse.app)
        self.receipts = []
        self._saved_secret, self._saved_receipt = ff.webhook_secret, dg.record_receipt
        ff.webhook_secret = lambda: SECRET
        dg.record_receipt = self.fake_receipt

    def tearDown(self):
        ff.webhook_secret, dg.record_receipt = self._saved_secret, self._saved_receipt

    def fake_receipt(self, source_ref, event_name="meeting.summarized", **kw):
        self.receipts.append((source_ref, event_name))
        return {"status": "ok"}

    def post(self, body=None, signature=None, secret=SECRET, headers=None):
        raw = json.dumps(body if body is not None else BODY).encode()
        h = {"content-type": "application/json",
             "X-Hub-Signature": signature if signature is not None else sign(raw, secret)}
        h.update(headers or {})
        return self.client.post(sse.FIREFLIES_WEBHOOK_PATH, content=raw, headers=h)


class SignatureGate(Base):
    def test_valid_signature_records_a_receipt(self):
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["received"])
        self.assertEqual(self.receipts, [("tr_abc", "meeting.summarized")])

    def test_sha256_prefix_is_tolerated(self):
        raw = json.dumps(BODY).encode()
        self.assertEqual(self.post(signature="sha256=" + sign(raw)).status_code, 200)

    def test_invalid_signature_is_rejected_before_any_work(self):
        r = self.post(signature="0" * 64)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.receipts, [], "nada debe escribirse antes de verificar la firma")

    def test_absent_signature_is_rejected(self):
        r = self.post(signature="")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.receipts, [])

    def test_a_signature_from_the_wrong_secret_is_rejected(self):
        self.assertEqual(self.post(secret="otro-secreto").status_code, 401)

    def test_tampered_body_invalidates_the_signature(self):
        raw = json.dumps(BODY).encode()
        good = sign(raw)
        tampered = dict(BODY, meetingId="tr_del_atacante")
        self.assertEqual(self.post(body=tampered, signature=good).status_code, 401)
        self.assertEqual(self.receipts, [])


class FailsClosed(Base):
    def test_no_secret_configured_is_503_not_open(self):
        ff.webhook_secret = lambda: None
        r = self.post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.receipts, [])

    def test_empty_secret_is_also_closed(self):
        ff.webhook_secret = lambda: ""
        self.assertEqual(self.post().status_code, 503)


class BodyGuards(Base):
    def test_oversized_declared_body_is_rejected_before_reading(self):
        raw = json.dumps(BODY).encode()
        r = self.client.post(sse.FIREFLIES_WEBHOOK_PATH, content=raw,
                             headers={"content-type": "application/json",
                                      "content-length": str(sse._WEBHOOK_MAX_BODY + 1),
                                      "X-Hub-Signature": sign(raw)})
        self.assertIn(r.status_code, (413, 400))
        self.assertEqual(self.receipts, [])

    def test_bad_json_after_a_valid_signature_is_400(self):
        raw = b"{no soy json"
        r = self.client.post(sse.FIREFLIES_WEBHOOK_PATH, content=raw,
                             headers={"content-type": "application/json",
                                      "X-Hub-Signature": sign(raw)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.receipts, [])


class EventRouting(Base):
    def test_transcribed_is_ignored(self):
        """Llega antes del resumen; actuar sobre él capturaría una junta vacía."""
        r = self.post(dict(BODY, eventType="meeting.transcribed"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ignored"])
        self.assertEqual(self.receipts, [])

    def test_bot_joined_is_ignored(self):
        self.assertTrue(self.post(dict(BODY, eventType="meeting.bot_joined")).json()["ignored"])

    def test_missing_meeting_id_is_400(self):
        body = {"eventType": "meeting.summarized"}
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.receipts, [])

    def test_alternate_field_names_are_accepted(self):
        """Fireflies documenta `meeting_id`; algunos ejemplos usan `meetingId`."""
        self.assertEqual(
            self.post({"event": "meeting.summarized", "meeting_id": "tr_snake"}).status_code, 200)
        self.assertEqual(self.receipts, [("tr_snake", "meeting.summarized")])


class RoutesAreReachableInProduction(unittest.TestCase):
    """Un TestClient importa el módulo; el servicio lo corre como script.

    Esa diferencia escondió un 404 real: la ruta quedó DEBAJO de
    `if __name__ == "__main__": uvicorn.run(...)`, que bloquea para siempre, así
    que al ejecutar como script nunca se registraba. Los 14 tests de arriba
    pasaban igual — eran una afirmación sobre el import, no sobre el servicio.
    Este guard es estructural porque el síntoma solo aparece fuera del proceso
    de pruebas.
    """

    def test_no_route_is_declared_after_the_blocking_main_block(self):
        src = (Path(__file__).resolve().parents[1] / "mcp_sse_server.py").read_text()
        lines = src.split("\n")
        main_at = next(i for i, l in enumerate(lines)
                       if l.startswith('if __name__ == "__main__":'))
        stranded = [(i + 1, l.strip()) for i, l in enumerate(lines[main_at:], start=main_at)
                    if l.startswith("@app.")]
        self.assertEqual(stranded, [],
                         f"rutas inalcanzables al correr como script: {stranded}")

    def test_the_webhook_path_is_registered_on_the_app(self):
        paths = {r.path for r in sse.app.routes if hasattr(r, "path")}
        self.assertIn(sse.FIREFLIES_WEBHOOK_PATH, paths)


if __name__ == "__main__":
    unittest.main()
