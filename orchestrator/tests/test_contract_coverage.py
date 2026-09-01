"""contract_coverage (K10 v1) contract: each ratio must equal an independent
raw-SQL count computed IN THE TEST (spec-sized verification, not a re-derivation
of the endpoint), and the function must be read-only (sha256 byte-identical).

Fixture: 3 agent tasks (one contract_cmd, one fenced `## Contract` body, one
bare), 1 human task WITH a contract (must not enter the denominator), 1
unassigned task (excluded); 4 verification ledger rows (1 contract-runner, 2
operator-manual-accept, 1 hand-written); 2 contract_run events.
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import db as _db  # noqa: E402

_SANDBOX_DB = Path(os.environ["HERMES_KANBAN_DB"])

FENCED_BODY = "work item\n\n## Contract\n```bash\npytest -q\n```\n"


def _sha(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ContractCoverage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="contract-cov-")
        self.tmp = Path(self.tmpdir) / "kanban.db"
        shutil.copy(_SANDBOX_DB, self.tmp)
        self._orig = _db.KANBAN_DB
        _db.KANBAN_DB = self.tmp
        conn = sqlite3.connect(str(self.tmp))
        for table in ("task_events", "task_ledger", "task_runs", "tasks"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        conn.executemany(
            "INSERT INTO tasks (id, title, body, status, assignee, contract_cmd, created_at) "
            "VALUES (?,?,?,?,?,?,0)",
            [
                ("t1", "agent w/ contract_cmd", None, "done", "glm-worker", "pytest -q"),
                ("t2", "agent w/ fenced contract", FENCED_BODY, "done", "kimi-worker", None),
                ("t3", "agent bare", None, "review", "glm-worker", None),
                ("t4", "human task w/ contract", None, "done", "ricardo", "make test"),
                ("t5", "unassigned", None, "backlog", None, None),
            ])
        conn.executemany(
            "INSERT INTO task_ledger (task_id, agent, role, summary, status, passed, created_at) "
            "VALUES (?,?,?,?,?,?,0)",
            [
                ("t1", "contract-runner", "verification", "contract `pytest -q` → rc=0", "passed", 1),
                ("t2", "ricardo", "verification",
                 "operator manual accept (the human gate reviewed this work)", "passed", 1),
                ("t3", "ricardo", "verification",
                 "operator manual accept (the human gate reviewed this work)", "passed", 1),
                ("t1", "reviewer-agent", "verification", "hand-written spot check", "passed", 1),
            ])
        conn.executemany(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES (?,?,0)",
            [("t1", "contract_run"), ("t1", "contract_run")])
        conn.commit()
        conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ratios_match_independent_sql_and_read_only(self):
        from dashboard.governance import contract_coverage
        before = _sha(self.tmp)
        out = contract_coverage()
        after = _sha(self.tmp)
        self.assertEqual(before, after, "contract_coverage WROTE to the DB")

        # independent raw-SQL ground truth, computed here — never via the module
        conn = sqlite3.connect(str(self.tmp))
        try:
            q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
            agents = q("SELECT COUNT(*) FROM tasks WHERE assignee IS NOT NULL "
                       "AND assignee NOT IN ('ricardo','user')")
            cmd_contracts = q("SELECT COUNT(*) FROM tasks WHERE assignee IS NOT NULL "
                              "AND assignee NOT IN ('ricardo','user') "
                              "AND contract_cmd IS NOT NULL AND contract_cmd != ''")
            ver_total = q("SELECT COUNT(*) FROM task_ledger WHERE role='verification'")
            ver_run = q("SELECT COUNT(*) FROM task_ledger WHERE role='verification' "
                        "AND summary LIKE 'contract %'")
            ver_manual = q("SELECT COUNT(*) FROM task_ledger WHERE role='verification' "
                           "AND summary LIKE 'operator manual accept%'")
            run_events = q("SELECT COUNT(*) FROM task_events WHERE kind='contract_run'")
        finally:
            conn.close()

        self.assertEqual(out["agent_tasks"]["total"], agents)          # 3
        # fenced-body contract (t2) counts beyond contract_cmd (t1):
        self.assertEqual(out["agent_tasks"]["with_contract"], cmd_contracts + 1)  # 2
        self.assertEqual(out["agent_tasks"]["coverage"], round(2 / 3, 3))
        self.assertEqual(out["verification_rows"]["total"], ver_total)  # 4
        self.assertEqual(out["verification_rows"]["from_contract_run"], ver_run)  # 1
        self.assertEqual(out["verification_rows"]["manual_accept"], ver_manual)   # 2
        self.assertEqual(out["verification_rows"]["other"], ver_total - ver_run - ver_manual)  # 1
        self.assertEqual(out["verification_rows"]["contract_provenance"], 0.25)
        self.assertEqual(out["contract_run_events"], run_events)  # 2

    def test_empty_db_degrades_to_none(self):
        conn = sqlite3.connect(str(self.tmp))
        for table in ("task_events", "task_ledger", "tasks"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        from dashboard.governance import contract_coverage
        out = contract_coverage()
        self.assertIsNone(out["agent_tasks"]["coverage"])
        self.assertIsNone(out["verification_rows"]["contract_provenance"])
        self.assertEqual(out["contract_run_events"], 0)


if __name__ == "__main__":
    unittest.main()
