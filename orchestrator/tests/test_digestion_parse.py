"""El parser de captura y el puente resumen→habla real.

`summary.action_items` es texto GENERADO por la IA de Fireflies; las oraciones
son habla real. Esa distinción es la que sostiene la garantía anti-alucinación
del aplicador, así que se prueba aquí explícitamente: el action item aporta el
candidato y el ancla, la oración aporta la cita citable.

El fixture tiene forma real y contenido sintético a propósito — capturar el
fixture en vivo trajo datos personales de terceros, que no van en un repo.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import digestion  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "digestion" / "transcript_shape.json"


def fixture(i=0):
    return json.loads(FIXTURE.read_text())[i]


class ParseActionItems(unittest.TestCase):
    def test_assignee_blocks_and_timestamps(self):
        items = digestion.parse_action_items(fixture(0)["summary"]["action_items"])
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["assignee"], "Dora Cliente")
        self.assertEqual(items[2]["assignee"], "Op Host")
        self.assertEqual(items[0]["at_seconds"], 14 * 60 + 2)
        self.assertEqual(items[2]["at_seconds"], 28 * 60 + 10)
        # El `(MM:SS)` se retira del texto — es metadato, no parte de la tarea.
        self.assertNotIn("(14:02)", items[0]["text"])
        self.assertTrue(items[0]["text"].startswith("Enviar resumen"))

    def test_hour_length_timestamps(self):
        items = digestion.parse_action_items("**A**\nHacer algo (1:05:30)")
        self.assertEqual(items[0]["at_seconds"], 3930)

    def test_degrades_instead_of_dropping(self):
        """Un blob sin formato produce items sin ancla, nunca cero items:
        evidencia degradada es mejor que evidencia perdida."""
        items = digestion.parse_action_items(fixture(1)["summary"]["action_items"])
        self.assertEqual(len(items), 2)
        self.assertIsNone(items[0]["assignee"])
        self.assertIsNone(items[0]["at_seconds"])
        self.assertEqual(items[0]["text"], "Revisar el contrato")

    def test_empty_and_none(self):
        self.assertEqual(digestion.parse_action_items(""), [])
        self.assertEqual(digestion.parse_action_items(None), [])
        self.assertEqual(digestion.parse_action_items("   \n\n "), [])


class AnchoringToRealSpeech(unittest.TestCase):
    def test_item_anchors_to_the_nearest_sentence(self):
        payload = digestion.build_event_payload(fixture(0))
        first = payload["action_items"][0]
        # 14:02 = 842s → la oración de Dora en 842.1
        self.assertEqual(first["anchor_index"], 1)
        self.assertEqual(first["anchor_speaker"], "Dora Cliente")
        self.assertIn("resumen de funcionalidades", first["anchor_quote"])

    def test_unanchored_item_carries_no_quote(self):
        payload = digestion.build_event_payload(fixture(1))
        self.assertNotIn("anchor_quote", payload["action_items"][0])

    def test_meeting_without_action_items_still_builds(self):
        payload = digestion.build_event_payload(fixture(2))
        self.assertEqual(payload["action_items"], [])
        self.assertEqual(len(payload["sentences"]), 1)


class QuotableSurface(unittest.TestCase):
    """La superficie citable excluye texto generado por IA. Si `overview` o el
    texto de un action item fueran citables, el aplicador aceptaría como 'cita
    verbatim' algo que ningún humano dijo."""

    def test_only_sentences_are_quotable(self):
        payload = digestion.build_event_payload(fixture(0))
        quotable = digestion.quotable_texts(payload)
        self.assertIn("Te mando hoy mismo el resumen de funcionalidades y los anexos "
                      "para que puedan cotizar.", quotable)
        joined = "\n".join(quotable)
        self.assertNotIn("Sesión de alcance del MVP", joined, "el overview no es citable")
        self.assertNotIn("Enviar resumen de funcionalidades y anexos", joined,
                         "el texto del action item no es citable")


class EventIdentity(unittest.TestCase):
    def test_id_is_stable_and_coordinate_derived(self):
        a = digestion.event_id_for("fireflies", "tr_1", 100, 200)
        b = digestion.event_id_for("fireflies", "tr_1", 100, 200)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("ev_"))

    def test_different_windows_are_different_events(self):
        a = digestion.event_id_for("fireflies", "tr_1", 100, 200)
        c = digestion.event_id_for("fireflies", "tr_1", 100, 300)
        d = digestion.event_id_for("whatsapp", "tr_1", 100, 200)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)


if __name__ == "__main__":
    unittest.main()
