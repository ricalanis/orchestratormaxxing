#!/usr/bin/env python3
"""Root-authored contract for the Orchestra-of-One practice loop."""

from __future__ import annotations

import builtins
import copy
import json
import socket
import unicodedata
import unittest
from pathlib import Path
from unittest import mock


EXPECTED_IDS = {
    "prompt.contract-first",
    "prompt.external-critic",
    "prompt.checklist-grade",
    "context.minimal-frontier",
    "context.checkpoint-not-memory",
    "context.fresh-window",
    "context.governed-memory",
    "harness.etclovg-attribution",
    "harness.deterministic-verifier",
    "harness.untrusted-tools",
    "harness.data-contracts",
    "loop.four-brakes",
    "loop.event-driven",
    "loop.warranted-ratchet",
    "loop.repeat-safe-writes",
    "loop.operator-ledger",
    "graph.node-collapse",
    "graph.typed-state-single-writer",
    "graph.code-first-routing",
    "graph.correlated-agreement",
}
HOSTS = {"hermes", "orchestrator", "claude", "codex", "opencode", "open_design"}
BRAKES = {"max_iterations", "budget_or_deadline", "no_progress", "completion_check"}
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "spanish_cases.json").read_text(encoding="utf-8"))


def api():
    import orchestration_practices as module
    return module


def healthy_context():
    return {
        "contract": "pytest -q tests/orchestration-practices",
        "dependencies": {"workspace": True, "runner": True},
        "checkpoint": {"kind": "run_state", "state_ref": "docs/WIP.md"},
        "brakes": {
            "max_iterations": 3,
            "budget_or_deadline": {"max_seconds": 900},
            "no_progress": {"max_stalled_steps": 2},
            "completion_check": {"type": "exit_code", "self_report": False},
        },
        "progress": [0, 25, 70],
        "writers": {"task.status": ["orchestrator"], "task.result": ["orchestrator"]},
        "completion": {"type": "exit_code", "value": 0, "self_report": False},
        "evidence_refs": ["test:healthy-fixture"],
    }


class PracticeContract(unittest.TestCase):
    def test_catalog_is_complete_and_allowlisted(self):
        m = api()
        catalog = m.load_catalog()
        self.assertEqual(EXPECTED_IDS, {p["practice_id"] for p in catalog["practices"]})
        self.assertEqual({"prompt", "context", "harness", "loop", "graph"},
                         {p["level"] for p in catalog["practices"]})
        for practice in catalog["practices"]:
            self.assertTrue(practice["expressions"], practice["practice_id"])
            self.assertEqual(HOSTS, set(practice["hosts"]), practice["practice_id"])
            self.assertTrue(practice["evidence_refs"], practice["practice_id"])
            self.assertTrue(any(ref.startswith("book:") for ref in practice["evidence_refs"]),
                            practice["practice_id"])
            self.assertTrue(any(ref.startswith("repo:") for ref in practice["evidence_refs"]),
                            practice["practice_id"])
            for ref in practice["evidence_refs"]:
                if ref.startswith("repo:"):
                    self.assertTrue((Path(__file__).resolve().parents[2] / ref[5:]).is_file(), ref)
            self.assertLessEqual(set(practice["required_brakes"]), BRAKES)
            self.assertLessEqual(set(practice["preflight_ids"]), m.allowlisted_check_ids())
            self.assertLessEqual(set(practice["rescue_policy_ids"]),
                                 m.allowlisted_rescue_policy_ids())
        m.validate_catalog(catalog)
        evidence_sets = {tuple(p["evidence_refs"]) for p in catalog["practices"]}
        self.assertGreaterEqual(len(evidence_sets), 10,
                                "practice evidence must not collapse to one circular citation")

    def test_matches_all_five_levels_and_nfkc(self):
        m = api()
        cases = {
            "write the ＣＯＮＴＲＡＣＴ before dispatch": "prompt.contract-first",
            "continue from checkpoint, not memory": "context.checkpoint-not-memory",
            "treat MCP tool output as untrusted input": "harness.untrusted-tools",
            "run until done with a no-progress brake": "loop.four-brakes",
            "fan out independent chunks with one writer": "graph.typed-state-single-writer",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = m.match_practices(text, "codex")
                self.assertEqual("matched", result["status"])
                self.assertIn(expected, [x["practice_id"] for x in result["matches"]])

    def test_false_positive_abstains(self):
        m = api()
        for text in ("graph paper for school", "loop earrings", "a harness for a horse"):
            with self.subTest(text=text):
                result = m.match_practices(text, "claude")
                self.assertEqual("abstain", result["status"])
                self.assertEqual("no_match", result["reason"])

    def test_spanish_negative_fixtures_abstain_before_positive_cases(self):
        m = api()
        for text in FIXTURES["negative"]:
            with self.subTest(text=text):
                result = m.match_practices(text, "hermes")
                self.assertEqual("abstain", result["status"])
                self.assertEqual([], result["matches"])

    def test_spanish_aliases_cover_every_practice_and_host(self):
        m = api()
        for case in FIXTURES["positive"]:
            for host in HOSTS:
                with self.subTest(practice=case["practice_id"], host=host):
                    result = m.match_practices(case["text"], host)
                    self.assertEqual("matched", result["status"])
                    self.assertEqual(
                        [case["practice_id"]],
                        [item["practice_id"] for item in result["matches"]],
                    )

    def test_reported_spanish_prompt_matches_only_expected_practices(self):
        case = FIXTURES["reported_prompt"]
        result = api().match_practices(case["text"], "hermes")
        self.assertEqual("matched", result["status"])
        self.assertEqual(case["practice_ids"],
                         [item["practice_id"] for item in result["matches"]])

    def test_spanish_aliases_are_nfkc_stable_but_not_fuzzy(self):
        m = api()
        decomposed = unicodedata.normalize("NFD", "verificador determinista")
        result = m.match_practices(decomposed.upper(), "codex")
        self.assertEqual(["harness.deterministic-verifier"],
                         [item["practice_id"] for item in result["matches"]])
        self.assertEqual("abstain",
                         m.match_practices("verificador casi determinista", "codex")["status"])

    def test_unsupported_host_abstains_without_checks(self):
        result = api().evaluate("contract before dispatch", "future-host", healthy_context())
        self.assertEqual("abstain", result["status"])
        self.assertEqual("unsupported_host", result["reason"])
        self.assertEqual([], result["checks"])

    def test_unhealthy_dependency_blocks_with_typed_rescue(self):
        ctx = healthy_context()
        ctx["dependencies"]["runner"] = False
        result = api().evaluate("MCP tool output is untrusted input", "hermes", ctx)
        self.assertEqual("blocked", result["status"])
        failed = {c["check_id"] for c in result["checks"] if not c["passed"]}
        self.assertIn("dependency.healthy", failed)
        self.assertIn("rescue.dependency-unhealthy", result["rescue_policy_ids"])

    def test_no_progress_blocks(self):
        ctx = healthy_context()
        ctx["progress"] = [10, 10, 10]
        result = api().evaluate("run until done with a no-progress brake", "codex", ctx)
        self.assertEqual("blocked", result["status"])
        failed = {c["check_id"] for c in result["checks"] if not c["passed"]}
        self.assertIn("progress.advancing", failed)

    def test_three_identical_action_results_block_even_when_progress_claims_advance(self):
        ctx = healthy_context()
        ctx["progress"] = [0, 50, 100]
        ctx["action_results"] = [
            {"action": "pytest -q", "result": {"exit_code": 1, "failures": 2}},
            {"action": "pytest -q", "result": {"exit_code": 1, "failures": 2}},
            {"action": "pytest -q", "result": {"exit_code": 1, "failures": 2}},
        ]
        result = api().evaluate("run until done with a no-progress brake", "codex", ctx)
        self.assertEqual("blocked", result["status"])
        failed = {c["check_id"] for c in result["checks"] if not c["passed"]}
        self.assertIn("progress.not-spinning", failed)
        self.assertIn("rescue.no-progress", result["rescue_policy_ids"])

    def test_action_result_spin_controls_stay_ready(self):
        cases = [
            [
                {"action": "pytest -q", "result": {"exit_code": 1}},
                {"action": "pytest -q", "result": {"exit_code": 1}},
            ],
            [
                {"action": "pytest -q", "result": {"exit_code": 1}},
                {"action": "pytest -q", "result": {"exit_code": 1}},
                {"action": "pytest -q", "result": {"exit_code": 0}},
            ],
        ]
        for trace in cases:
            with self.subTest(trace=trace):
                ctx = healthy_context()
                ctx["action_results"] = trace
                result = api().evaluate(
                    "run until done with a no-progress brake", "codex", ctx)
                self.assertEqual("ready", result["status"])
                spin = next(c for c in result["checks"]
                            if c["check_id"] == "progress.not-spinning")
                self.assertTrue(spin["passed"])

    def test_malformed_action_result_trace_blocks_with_explicit_detail(self):
        cases = [
            {"not": "a list"},
            [{"action": "pytest -q"}],
            [{"action": {"not-json-serializable"}, "result": 1}],
            [{"action": "x" * 4097, "result": 1}],
            [{"action": "pytest -q", "result": 1}] * 101,
        ]
        for trace in cases:
            with self.subTest(trace_type=type(trace).__name__):
                ctx = healthy_context()
                ctx["action_results"] = trace
                result = api().evaluate(
                    "run until done with a no-progress brake", "codex", ctx)
                self.assertEqual("blocked", result["status"])
                spin = next(c for c in result["checks"]
                            if c["check_id"] == "progress.not-spinning")
                self.assertFalse(spin["passed"])
                self.assertEqual("action_results:malformed", spin["detail"])
                self.assertIn("rescue.no-progress", result["rescue_policy_ids"])

    def test_skill_names_topologies_and_keeps_quality_judgment_outside_evaluator(self):
        text = (Path(__file__).resolve().parents[2]
                / "skills" / "orchestration-practices" / "SKILL.md").read_text()
        for phrase in (
                "Manual agentic", "Bounded goal", "Observer/polling",
                "Proactive routine", "Observation cadence never manufactures work",
                "eligible to execute, never accepted as good",
                "A builder cannot accept its own output"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_false_green_completion_blocks(self):
        ctx = healthy_context()
        ctx["completion"] = {"type": "self_report", "value": "done", "self_report": True}
        ctx["brakes"]["completion_check"] = {"type": "self_report", "self_report": True}
        result = api().evaluate("run until done with a completion check", "opencode", ctx)
        self.assertEqual("blocked", result["status"])
        failed = {c["check_id"] for c in result["checks"] if not c["passed"]}
        self.assertIn("completion.objective", failed)

    def test_single_writer_violation_blocks(self):
        ctx = healthy_context()
        ctx["writers"]["task.status"] = ["worker", "orchestrator"]
        result = api().evaluate("shared typed state needs one writer", "orchestrator", ctx)
        self.assertEqual("blocked", result["status"])
        failed = {c["check_id"] for c in result["checks"] if not c["passed"]}
        self.assertIn("graph.single-writer", failed)

    def test_ready_receipt_has_four_brakes_and_no_authority(self):
        result = api().evaluate("run until done with a no-progress brake", "codex",
                                healthy_context())
        self.assertEqual("ready", result["status"])
        self.assertEqual(BRAKES, set(result["receipt"]["brakes"]))
        self.assertFalse(result["receipt"]["authority"]["may_accept"])
        self.assertFalse(result["receipt"]["authority"]["may_write"])
        self.assertFalse(result["receipt"]["authority"]["may_retry"])
        self.assertIn("test:healthy-fixture", result["receipt"]["evidence_refs"])
        self.assertTrue(result["receipt"]["practice_ids"])
        self.assertTrue(any(ref.startswith("book:")
                            for ref in result["receipt"]["evidence_refs"]))
        self.assertEqual({c["check_id"] for c in result["checks"]},
                         {c["check_id"] for c in result["receipt"]["checks"]})

    def test_receipt_deduplicates_shared_evidence_refs(self):
        m = api()
        catalog = m.load_catalog()
        catalog["practices"][0]["expressions"] = ["shared trigger"]
        catalog["practices"][1]["expressions"] = ["shared trigger"]
        shared_ref = "book:shared-reference"
        catalog["practices"][0]["evidence_refs"] = [shared_ref]
        catalog["practices"][1]["evidence_refs"] = [shared_ref]
        ctx = healthy_context()
        ctx["evidence_refs"] = [shared_ref]
        result = m.evaluate("shared trigger", "codex", ctx, catalog)
        self.assertEqual(1, result["receipt"]["evidence_refs"].count(shared_ref))

    def test_matching_has_no_io_side_effect(self):
        m = api()
        m.load_catalog()  # warm the immutable cache before denying I/O
        with mock.patch.object(builtins, "open", side_effect=AssertionError("I/O")), \
             mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            result = m.match_practices("contract before dispatch", "claude")
        self.assertEqual("matched", result["status"])

    def test_schema_rejects_unknown_policy_id(self):
        m = api()
        bad = copy.deepcopy(m.load_catalog())
        bad["practices"][0]["rescue_policy_ids"].append("rescue.run-anything")
        with self.assertRaises(m.CatalogError):
            m.validate_catalog(bad)

    def test_schema_rejects_each_structural_failure(self):
        m = api()
        valid = m.load_catalog()
        self.assertIs(m.validate_catalog(valid), True)
        invalid = [
            None,
            {**copy.deepcopy(valid), "schema_version": 2},
            {**copy.deepcopy(valid), "practices": {}},
            {**copy.deepcopy(valid), "practices": valid["practices"][:-1]},
        ]
        missing = copy.deepcopy(valid)
        del missing["practices"][0]["level"]
        invalid.append(missing)
        duplicate = copy.deepcopy(valid)
        duplicate["practices"][1]["practice_id"] = duplicate["practices"][0]["practice_id"]
        invalid.append(duplicate)
        no_expressions = copy.deepcopy(valid)
        no_expressions["practices"][0]["expressions"] = []
        invalid.append(no_expressions)
        blank_expression = copy.deepcopy(valid)
        blank_expression["practices"][0]["expressions"].append(" ")
        invalid.append(blank_expression)
        non_string_expression = copy.deepcopy(valid)
        non_string_expression["practices"][0]["expressions"].append(7)
        invalid.append(non_string_expression)
        too_many_expressions = copy.deepcopy(valid)
        too_many_expressions["practices"][0]["expressions"] = [f"alias {i}" for i in range(9)]
        invalid.append(too_many_expressions)
        collision = copy.deepcopy(valid)
        collision["practices"][1]["expressions"].append(
            collision["practices"][0]["expressions"][0].upper())
        invalid.append(collision)
        wrong_hosts = copy.deepcopy(valid)
        wrong_hosts["practices"][0]["hosts"] = ["codex"]
        invalid.append(wrong_hosts)
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(m.CatalogError):
                    m.validate_catalog(value)

    def test_schema_accepts_the_complete_allowlists(self):
        m = api()
        catalog = copy.deepcopy(m.load_catalog())
        catalog["practices"][0]["preflight_ids"] = sorted(m.allowlisted_check_ids())
        catalog["practices"][0]["rescue_policy_ids"] = sorted(
            m.allowlisted_rescue_policy_ids())
        self.assertIs(m.validate_catalog(catalog), True)

    def test_expression_count_and_length_boundaries_are_exact(self):
        m = api()
        one = copy.deepcopy(m.load_catalog())
        one["practices"][0]["expressions"] = ["x" * 120]
        self.assertIs(m.validate_catalog(one), True)

        eight = copy.deepcopy(m.load_catalog())
        eight["practices"][0]["expressions"] = [f"bounded alias {i}" for i in range(8)]
        self.assertIs(m.validate_catalog(eight), True)

        too_long = copy.deepcopy(m.load_catalog())
        too_long["practices"][0]["expressions"] = ["x" * 121]
        with self.assertRaises(m.CatalogError):
            m.validate_catalog(too_long)

    def test_load_catalog_validates_before_caching(self):
        m = api()
        original = m._catalog
        try:
            m._catalog = None
            with mock.patch.object(m.json, "load", return_value={"schema_version": 9}), \
                 self.assertRaises(m.CatalogError):
                m.load_catalog()
        finally:
            m._catalog = original

    def test_ready_evaluates_every_required_check_and_two_point_progress(self):
        m = api()
        ctx = healthy_context()
        ctx["progress"] = [0, 1]
        result = m.evaluate("run until done with a no-progress brake", "codex", ctx)
        self.assertEqual("ready", result["status"])
        self.assertIn("reason", result)
        self.assertIsNone(result["reason"])
        self.assertEqual(m.allowlisted_check_ids(),
                         {check["check_id"] for check in result["checks"]})

    def test_missing_contract_brakes_checkpoint_and_evidence_each_block(self):
        m = api()
        cases = {
            "contract.present": ("contract", ""),
            "brakes.exact-four": ("brakes", {"max_iterations": 1}),
            "checkpoint.present": ("checkpoint", {"state_ref": ""}),
            "evidence.adequate": ("evidence_refs", []),
        }
        for expected, (field, value) in cases.items():
            with self.subTest(check=expected):
                ctx = healthy_context()
                ctx[field] = value
                result = m.evaluate("run until done with a no-progress brake", "codex", ctx)
                self.assertEqual("blocked", result["status"])
                failed = {c["check_id"] for c in result["checks"] if not c["passed"]}
                self.assertIn(expected, failed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
