#!/usr/bin/env python3
"""Real CLI contract for complete declared capability projection accounting."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "plugins/orchestratormaxxing/skills/public-improve-security/scripts/verify_projection.py"


class ProjectionContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.contract = dict(schema_version=1, base="a" * 40, head="b" * 40,
                             capabilities=[dict(id="feature", required=True, hosts=["linux", "macos"]),
                                           dict(id="optional", required=False, hosts=[])])
        raw = json.dumps(self.contract).encode()
        (self.path / "contract.json").write_bytes(raw)
        proof = dict(status="pass", evidence="Synthetic contract evidence reference")
        self.report = dict(schema_version=1, base="a" * 40, head="b" * 40,
                           contract_sha256=hashlib.sha256(raw).hexdigest(), capabilities=[
            dict(id="feature", disposition="included", source_evidence="Source contract",
                 reason="Portable behavior", public_behavior="Entry point works",
                 public_paths=["bin/tool"], dependencies=copy.deepcopy(proof),
                 functional=copy.deepcopy(proof), security=copy.deepcopy(proof),
                 hosts={"linux": copy.deepcopy(proof), "macos": copy.deepcopy(proof)}),
            dict(id="optional", disposition="deferred", source_evidence="Source interface",
                 reason="No support claimed")])

    def run_report(self, report):
        (self.path / "report.json").write_text(json.dumps(report))
        return subprocess.run([sys.executable, str(SCRIPT), "--contract", str(self.path / "contract.json"),
                               "--report", str(self.path / "report.json")], capture_output=True,
                              text=True, timeout=3)

    def test_complete_report_is_accounting_not_approval(self):
        result = self.run_report(self.report)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["coverage"], dict(declared=2, included=1, deferred=1, excluded=0))
        self.assertFalse(row["evidence_verified"])
        self.assertFalse(row["publication_authorized"])

    def test_missing_extra_duplicate_waived_and_stale_scope_fail(self):
        variants = []
        for key, value in [("head", "c" * 40), ("contract_sha256", "0" * 64), ("schema_version", True)]:
            row = copy.deepcopy(self.report); row[key] = value; variants.append(row)
        for caps in [[], self.report["capabilities"][:1], self.report["capabilities"] * 2,
                     [dict(id="feature", disposition="deferred", source_evidence="x", reason="skip"), self.report["capabilities"][1]]]:
            row = copy.deepcopy(self.report); row["capabilities"] = caps; variants.append(row)
        for row in variants:
            with self.subTest(row=row):
                result = self.run_report(row)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(json.loads(result.stdout)["structurally_complete"])

    def test_missing_dependency_function_security_and_host_evidence_fail(self):
        for kind in ["dependencies", "functional", "security", "linux", "macos"]:
            for status in ["fail", "unverified", "not-applicable"]:
                row = copy.deepcopy(self.report)
                target = row["capabilities"][0]
                if kind in ["linux", "macos"]: target = target["hosts"]
                target[kind]["status"] = status
                self.assertEqual(self.run_report(row).returncode, 1, (kind, status))
        row = copy.deepcopy(self.report); del row["capabilities"][0]["hosts"]["macos"]
        self.assertEqual(self.run_report(row).returncode, 1)
        row = copy.deepcopy(self.report); row["capabilities"][0]["source_evidence"] = " "
        self.assertEqual(self.run_report(row).returncode, 1)

    def test_private_or_escaping_paths_and_malformed_shapes_fail(self):
        for paths in [["/private/file"], ["../outside"], ["a/../b"], ["./bin/tool"], [], [{}]]:
            row = copy.deepcopy(self.report); row["capabilities"][0]["public_paths"] = paths
            self.assertEqual(self.run_report(row).returncode, 1)
        for row in [[], None, {"capabilities": []}]:
            self.assertEqual(self.run_report(row).returncode, 1)

    def test_duplicate_json_keys_and_nonregular_inputs_fail_promptly(self):
        self.run_report(self.report)
        contract = self.path / "contract.json"
        contract.write_text('{"schema_version":1,"schema_version":1}')
        args = [sys.executable, str(SCRIPT), "--contract", str(contract), "--report", str(self.path / "report.json")]
        self.assertEqual(subprocess.run(args, capture_output=True, timeout=3).returncode, 1)
        contract.unlink(); contract.symlink_to(self.path / "report.json")
        self.assertEqual(subprocess.run(args, capture_output=True, timeout=3).returncode, 1)
        contract.unlink(); os.mkfifo(contract)
        self.assertEqual(subprocess.run(args, capture_output=True, timeout=3).returncode, 1)

    def test_contract_schema_and_revisions_cannot_be_weakened_in_report(self):
        contracts = []
        extra = copy.deepcopy(self.contract); extra["waive_checks"] = True; contracts.append(extra)
        extra = copy.deepcopy(self.contract); extra["capabilities"][0]["waive_hosts"] = True; contracts.append(extra)
        for revision in ["not-a-revision", "", "A" * 40, 123]:
            changed = copy.deepcopy(self.contract); changed["head"] = revision; contracts.append(changed)
        for contract in contracts:
            raw = json.dumps(contract).encode(); (self.path / "contract.json").write_bytes(raw)
            report = copy.deepcopy(self.report)
            report["head"] = contract["head"]
            report["contract_sha256"] = hashlib.sha256(raw).hexdigest()
            self.assertEqual(self.run_report(report).returncode, 1, contract)

    def test_required_cli_arguments_have_usage_failure(self):
        for only in ["--contract", "--report"]:
            result = subprocess.run([sys.executable, str(SCRIPT), only, "unused.json"],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
