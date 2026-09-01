"""Graph projection contract: practices are derived; retired Initiative is absent."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import graph_memory as graph  # noqa: E402

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "orchestration-practices" /
     "fixtures" / "spanish_cases.json").read_text(encoding="utf-8"))
PRACTICE_IDS = {
    item["practice_id"] for item in graph._practices.load_catalog()["practices"]
}


class GraphPracticeVocabulary(unittest.TestCase):
    def test_canonical_practice_expressions_project_as_concepts(self):
        concepts = graph.extract_concepts(
            "Write the contract before dispatch and keep one writer for shared state")
        self.assertIn("prompt.contract-first", concepts)
        self.assertIn("graph.typed-state-single-writer", concepts)

    def test_graph_uses_the_same_spanish_positive_and_negative_contract(self):
        for case in FIXTURES["positive"]:
            with self.subTest(practice=case["practice_id"]):
                self.assertIn(case["practice_id"], graph.extract_concepts(case["text"]))
        for text in FIXTURES["negative"]:
            with self.subTest(text=text):
                self.assertFalse(PRACTICE_IDS & set(graph.extract_concepts(text)))

    def test_retired_initiative_layer_is_not_projected(self):
        self.assertNotIn("Initiative", graph.NODE_TYPES)
        with tempfile.TemporaryDirectory() as tmp:
            store = graph.GraphStore(Path(tmp) / "graph.db")
            with mock.patch.object(graph, "ingest_notes", return_value={}), \
                 mock.patch.object(graph, "ingest_tasks", return_value={}), \
                 mock.patch.object(graph, "ingest_git", return_value={}), \
                 mock.patch.object(graph, "ingest_memory", return_value={}), \
                 mock.patch.object(graph, "ingest_sprints", return_value={}), \
                 mock.patch.object(graph, "ingest_ledger", return_value={}), \
                 mock.patch.object(graph, "ingest_cc_memory", return_value={}), \
                 mock.patch.object(graph, "ingest_changelog", return_value={}), \
                 mock.patch.object(graph, "ingest_obsidian", return_value={}):
                summary = graph.ingest_all(store, rebuild=True)
            self.assertNotIn("initiatives", summary)
            self.assertNotIn("Initiative", summary["stats"]["nodes_by_type"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
