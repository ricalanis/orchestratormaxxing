"""m33 — durable, capability-neutral task run envelopes.

The row is a sidecar around Hermes-owned ``task_runs``. It stores the declared
four brakes and the immutable advisory-practice receipt; it does not create a
second run state machine. Tasks without a row are legacy-compatible during the
rollout. Once a row exists, claim/progress fail closed through the envelope.
"""


def m33_task_run_envelopes(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_run_envelopes (
            task_id TEXT PRIMARY KEY,
            practice_text TEXT,
            host TEXT,
            context_json TEXT,
            receipt_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','ready','blocked','completed')),
            reason TEXT,
            max_iterations INTEGER,
            deadline_at INTEGER,
            max_stalled_steps INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_progress INTEGER,
            stalled_steps INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_run_envelopes_status "
        "ON task_run_envelopes(status, deadline_at)"
    )
