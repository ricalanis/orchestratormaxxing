"""Behavioral contract for the Orchestra-of-One task run envelope.

Negative fixtures come first: a declared-but-unready envelope, exhausted
iteration budget, expired deadline, stalled progress, and the historical
three-completion false-green must all fail closed. Legacy tasks without an
envelope remain claimable during rollout.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db, governance, loop  # noqa: E402
from dashboard.migrations.m33_task_run_envelopes import m33_task_run_envelopes  # noqa: E402

SANDBOX_DB = Path(os.environ["HERMES_KANBAN_DB"])


def context(deadline=None, max_iterations=3, stalled=2):
    return {
        "contract": "true",
        "dependencies": {"workspace": True, "runner": True},
        "checkpoint": {"kind": "task_run", "state_ref": "task_runs.current"},
        "brakes": {
            "max_iterations": max_iterations,
            "budget_or_deadline": {"deadline_at": deadline or int(time.time()) + 3600},
            "no_progress": {"max_stalled_steps": stalled},
            "completion_check": {"type": "exit_code", "self_report": False},
        },
        "progress": [0, 1],
        "writers": {"task.status": ["orchestrator"], "task.result": ["orchestrator"]},
        "completion": {"type": "exit_code", "value": 0, "self_report": False},
        "evidence_refs": ["task:test-envelope"],
    }


class TaskRunEnvelope(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="task-envelope-")
        self.path = Path(self.tmpdir) / "kanban.db"
        shutil.copy(SANDBOX_DB, self.path)
        self.orig = db.KANBAN_DB
        db.KANBAN_DB = self.path
        conn = sqlite3.connect(self.path)
        m33_task_run_envelopes(conn)
        for table in ("task_run_envelopes", "task_events", "task_ledger", "task_runs", "tasks"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    def tearDown(self):
        db.KANBAN_DB = self.orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add_task(self, task_id, *, contract="true"):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO tasks (id,title,status,assignee,pool,contract_cmd,autonomy,created_at) "
            "VALUES (?,?, 'ready','worker',1,?,'auto',0)",
            (task_id, task_id, contract),
        )
        conn.commit()
        conn.close()

    def event_payloads(self, task_id, kind):
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (task_id, kind),
        ).fetchall()
        conn.close()
        return [json.loads(row[0]) for row in rows]

    def test_declared_pending_envelope_blocks_but_legacy_task_is_compatible(self):
        self.add_task("t_pending")
        pending = governance.require_run_envelope("t_pending")
        self.assertEqual({"status": "pending", "task_id": "t_pending"}, pending)
        blocked = loop.claim_task("t_pending", "worker")
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("rescue.missing-contract", blocked["rescue_policy_id"])
        self.assertEqual("envelope is pending", blocked["reason"])

        self.add_task("t_legacy")
        legacy = loop.claim_task("t_legacy", "worker")
        self.assertEqual("claimed", legacy["status"])

    def test_ready_receipt_is_durable_and_allows_claim(self):
        self.add_task("t_ready")
        result = governance.set_run_envelope(
            "t_ready", "run until done with a no-progress brake", "orchestrator", context())
        self.assertEqual("ready", result["status"])
        claimed = loop.claim_task("t_ready", "worker")
        self.assertEqual("claimed", claimed["status"])
        stored = governance.get_run_envelope("t_ready")
        self.assertEqual(1, stored["attempts"])
        self.assertEqual(context(), stored["context"])
        self.assertEqual(4, len(stored["receipt"]["brakes"]))
        self.assertIn("task:test-envelope", stored["receipt"]["evidence_refs"])
        configured = self.event_payloads("t_ready", "envelope_configured")
        self.assertEqual("ready", configured[-1]["status"])
        self.assertIn("loop.four-brakes", configured[-1]["practice_ids"])
        conn = sqlite3.connect(self.path)
        raw_context, raw_receipt = conn.execute(
            "SELECT context_json,receipt_json FROM task_run_envelopes WHERE task_id='t_ready'"
        ).fetchone()
        conn.close()
        self.assertEqual(json.dumps(context(), sort_keys=True), raw_context)
        self.assertEqual(json.dumps(stored["receipt"], sort_keys=True), raw_receipt)

    def test_iteration_and_deadline_brakes_fail_closed_with_typed_events(self):
        for task_id, ctx, expected in (
            ("t_iter", context(max_iterations=0), "rescue.iteration-budget"),
            ("t_deadline", context(deadline=int(time.time()) - 1), "rescue.deadline-exceeded"),
        ):
            with self.subTest(task_id=task_id):
                self.add_task(task_id)
                governance.set_run_envelope(
                    task_id, "run until done with a no-progress brake", "orchestrator", ctx)
                result = loop.claim_task(task_id, "worker")
                self.assertEqual("blocked", result["status"])
                self.assertEqual(expected, result["rescue_policy_id"])
                self.assertEqual(expected, self.event_payloads(task_id, "envelope_blocked")[-1]["rescue_policy_id"])
                stored = governance.get_run_envelope(task_id)
                self.assertEqual("blocked", stored["status"])
                self.assertTrue(stored["reason"])

    def test_deadline_equal_to_now_is_expired(self):
        self.add_task("t_deadline_equal")
        with mock.patch.object(governance, "_now", return_value=100):
            governance.set_run_envelope(
                "t_deadline_equal", "run until done with a no-progress brake",
                "orchestrator", context(deadline=100))
            result = loop.claim_task("t_deadline_equal", "worker")
        self.assertEqual("blocked", result["status"])
        self.assertEqual("rescue.deadline-exceeded", result["rescue_policy_id"])

    def test_stalled_progress_stops_run_and_records_rescue(self):
        self.add_task("t_stall")
        governance.set_run_envelope(
            "t_stall", "run until done with a no-progress brake", "orchestrator",
            context(stalled=1))
        self.assertEqual("claimed", loop.claim_task("t_stall", "worker")["status"])
        self.assertEqual("ok", loop.report_progress("t_stall", "same", 10, "worker")["status"])
        stopped = loop.report_progress("t_stall", "same again", 10, "worker")
        self.assertEqual("blocked", stopped["status"])
        self.assertEqual("rescue.no-progress", stopped["rescue_policy_id"])
        self.assertEqual("rescue.no-progress", self.event_payloads("t_stall", "envelope_blocked")[-1]["rescue_policy_id"])

    def test_three_completions_do_not_bypass_governed_acceptance(self):
        self.add_task("t_false_green")
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO task_events(task_id,kind,created_at) VALUES ('t_false_green','completed',0)",
            [(), (), ()],
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id='t_false_green'").fetchone()
        conn.close()
        with mock.patch.object(loop.graph, "trust_grade_for", return_value="low"):
            verdict = loop.route_result(row, "worker", True)
        self.assertEqual("escalate", verdict["decision"])
        self.assertIn("trust", verdict["reason"])

    def test_missing_tasks_and_contract_mismatch_fail_closed(self):
        self.assertEqual("error", governance.require_run_envelope("missing")["status"])
        self.assertEqual(
            "error",
            governance.set_run_envelope(
                "missing", "run until done with a no-progress brake",
                "orchestrator", context())["status"],
        )
        self.assertIsNone(governance.get_run_envelope("missing"))

        self.add_task("t_mismatch", contract="printf right")
        result = governance.set_run_envelope(
            "t_mismatch", "run until done with a no-progress brake",
            "orchestrator", context())
        self.assertEqual("blocked", result["status"])
        self.assertEqual(["rescue.missing-contract"], result["rescue_policy_ids"])
        self.assertIn("differs", result["reason"])

    def test_abstention_persists_as_blocked_and_runtime_rescues_are_allowlisted(self):
        import orchestration_practices
        self.add_task("t_abstain")
        result = governance.set_run_envelope(
            "t_abstain", "loop earrings", "orchestrator", context())
        self.assertEqual("abstain", result["status"])
        self.assertEqual("blocked", governance.get_run_envelope("t_abstain")["status"])
        runtime_rescues = {
            "rescue.iteration-budget", "rescue.deadline-exceeded",
            "rescue.envelope-blocked", "rescue.no-progress",
        }
        self.assertLessEqual(runtime_rescues,
                             orchestration_practices.allowlisted_rescue_policy_ids())

    def test_relative_time_budget_becomes_a_deadline(self):
        self.add_task("t_relative_deadline")
        ctx = context()
        ctx["brakes"]["budget_or_deadline"] = {"max_seconds": 30}
        with mock.patch.object(governance, "_now", return_value=100):
            result = governance.set_run_envelope(
                "t_relative_deadline", "run until done with a no-progress brake",
                "orchestrator", ctx)
        self.assertEqual("ready", result["status"])
        self.assertEqual(130, governance.get_run_envelope("t_relative_deadline")["deadline_at"])

    def test_progress_none_legacy_and_recovery_do_not_false_stall(self):
        self.add_task("t_progress")
        governance.set_run_envelope(
            "t_progress", "run until done with a no-progress brake",
            "orchestrator", context(stalled=2))
        conn = db.get_conn()
        try:
            self.assertIsNone(governance.record_envelope_progress("t_progress", None, conn))
            self.assertIsNone(governance.record_envelope_progress("t_progress", 10, conn))
            self.assertIsNone(governance.record_envelope_progress("t_progress", 10, conn))
            self.assertIsNone(governance.record_envelope_progress("t_progress", 20, conn))
            self.assertIsNone(governance.record_envelope_progress("legacy", 10, conn))
            conn.commit()
        finally:
            conn.close()
        stored = governance.get_run_envelope("t_progress")
        self.assertEqual(20, stored["last_progress"])
        self.assertEqual(0, stored["stalled_steps"])

    def test_coverage_is_raw_sql_grounded_and_rescues_are_typed(self):
        self.add_task("t_metric_ready")
        self.add_task("t_metric_legacy")
        governance.set_run_envelope(
            "t_metric_ready", "run until done with a no-progress brake",
            "orchestrator", context(max_iterations=0))
        loop.claim_task("t_metric_ready", "worker")
        result = governance.envelope_coverage()
        self.assertEqual(2, result["agent_tasks"])
        self.assertEqual(1, result["governed"])
        self.assertEqual(0.5, result["coverage"])
        self.assertEqual(1, result["blocked_events"])
        self.assertEqual({"rescue.iteration-budget": 1}, result["rescue_counts"])

    def test_completion_brake_requires_independent_verification(self):
        self.add_task("t_needs_verify")
        governance.set_run_envelope(
            "t_needs_verify", "run until done with a completion check",
            "orchestrator", context())
        loop.claim_task("t_needs_verify", "worker")
        with mock.patch.object(loop.graph, "trust_grade_for", return_value="high"):
            result = loop.report_result("t_needs_verify", "claimed done", True, agent="worker")
        self.assertEqual("review", result["status"])
        self.assertEqual("ready", governance.get_run_envelope("t_needs_verify")["status"])

        self.add_task("t_verified")
        governance.set_run_envelope(
            "t_verified", "run until done with a completion check",
            "orchestrator", context())
        loop.claim_task("t_verified", "worker")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO task_ledger(task_id,agent,role,summary,status,passed,created_at) "
            "VALUES ('t_verified','verifier','verification','contract `true` rc=0','passed',1,1)")
        conn.commit()
        conn.close()
        with mock.patch.object(loop.graph, "trust_grade_for", return_value="high"):
            result = loop.report_result("t_verified", "objectively done", True, agent="worker")
        self.assertEqual("done", result["status"])
        self.assertEqual("completed", governance.get_run_envelope("t_verified")["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
