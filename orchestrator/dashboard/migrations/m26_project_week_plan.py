"""m26 — propuestas de horas por proyecto para semanas futuras.

La semana en curso sigue viviendo exclusivamente en ``projects.weekly_hours``.
Esta tabla conserva propuestas explícitas para semanas estrictamente futuras;
el runner posee la conexión y la transacción.
"""


def m26_project_week_plan(conn) -> dict:
    """Create the future project-week plan table and lookup index."""
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_week_plan'"
    ).fetchone() is not None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_week_plan (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            iso_week   TEXT NOT NULL CHECK (iso_week GLOB '[0-9][0-9][0-9][0-9]-W[0-9][0-9]'),
            hours      REAL NOT NULL CHECK (hours >= 0 AND hours <= 40),
            set_at     INTEGER NOT NULL,
            PRIMARY KEY (project_id, iso_week)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pwp_week ON project_week_plan(iso_week)"
    )
    return {"tables": [] if existed else ["project_week_plan"]}
