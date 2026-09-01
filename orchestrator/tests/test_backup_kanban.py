"""Regression guard for P2-5 — bin/backup-kanban (consistent snapshot + rotation).

Runs the real script (subprocess) against an isolated source DB + backup dir and
pins: it writes a VALID, integrity-clean snapshot; rotation keeps only the newest
HERMES_BACKUP_KEEP; and a missing source exits non-zero (loud) rather than
silently succeeding. No network, no real DB, no systemd.

Run: python -m unittest tests.test_backup_kanban   # from orchestrator/
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "backup-kanban"


def _seed_db(path: Path):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    c.executemany("INSERT INTO tasks VALUES (?,?)", [("t_1", "a"), ("t_2", "b")])
    c.commit(); c.close()


@unittest.skipUnless(SCRIPT.exists(), "bin/backup-kanban missing")
class BackupKanban(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bk_test_"))
        self.src = self.tmp / "kanban.db"
        self.out = self.tmp / "backups"
        _seed_db(self.src)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, keep="14"):
        env = {**os.environ, "HERMES_KANBAN_DB": str(self.src),
               "HERMES_BACKUP_DIR": str(self.out), "HERMES_BACKUP_KEEP": keep}
        return subprocess.run([sys.executable, str(SCRIPT)], env=env,
                              capture_output=True, text=True)

    def test_writes_a_valid_snapshot(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        snaps = list(self.out.glob("kanban-*.db"))
        self.assertEqual(len(snaps), 1)
        # The snapshot is a sound DB carrying the source's data.
        conn = sqlite3.connect(str(snaps[0]))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_rotation_keeps_only_newest(self):
        self.out.mkdir(parents=True, exist_ok=True)
        # Pre-seed three OLD snapshots (names sort chronologically).
        for name in ("kanban-20200101-000001.db", "kanban-20200101-000002.db",
                     "kanban-20200101-000003.db"):
            (self.out / name).write_bytes(b"old")
        r = self._run(keep="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        snaps = sorted(p.name for p in self.out.glob("kanban-*.db"))
        self.assertEqual(len(snaps), 2)                            # KEEP=2 enforced
        self.assertFalse((self.out / "kanban-20200101-000001.db").exists())  # oldest rotated
        self.assertTrue(any(not n.startswith("kanban-2020") for n in snaps))  # the fresh snapshot survived

    def test_missing_source_fails_loud(self):
        self.src.unlink()
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("source DB not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
