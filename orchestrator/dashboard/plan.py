"""Weekly project-hour proposals for the Projects > Plan view."""

import datetime
import re
import time
from typing import Optional

from . import sprints
from .capacity import (
    COUNTED_KINDS,
    SELF_KIND,
    _band,
    _capacity,
    _worst_band,
    project_load,
)
from .db import get_conn


HOURS_FLOOR = 6.0
DEFAULT_WEEKS_AHEAD = 2
MIN_WEEKS_AHEAD = 1
MAX_WEEKS_AHEAD = 6
_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun",
           "jul", "ago", "sep", "oct", "nov", "dic")


def _week_start(now: int, offset: int) -> datetime.date:
    start, _ = sprints._week_window(now + offset * 7 * 86400)
    return datetime.date.fromtimestamp(start)


def _future_label(day: datetime.date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def _valid_iso_week(value) -> bool:
    match = _ISO_WEEK_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return False
    try:
        datetime.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return False
    return True


def get_weeks_ahead(conn) -> int:
    """Read the cross-machine visible horizon from ``orch_meta``."""
    row = conn.execute(
        "SELECT value FROM orch_meta WHERE key='plan_weeks_ahead'"
    ).fetchone()
    try:
        value = int(row[0]) if row else DEFAULT_WEEKS_AHEAD
    except (TypeError, ValueError):
        value = DEFAULT_WEEKS_AHEAD
    return min(MAX_WEEKS_AHEAD, max(MIN_WEEKS_AHEAD, value))


def set_weeks_ahead(n) -> dict:
    """Persist the visible future-week count, clamped to 1..6."""
    if isinstance(n, bool):
        return {"status": "error", "error": "weeks_ahead must be a number"}
    try:
        value = int(n)
    except (TypeError, ValueError):
        return {"status": "error", "error": "weeks_ahead must be a number"}
    value = min(MAX_WEEKS_AHEAD, max(MIN_WEEKS_AHEAD, value))
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO orch_meta(key, value) VALUES (?, ?)",
            ("plan_weeks_ahead", str(value)),
        )
        conn.commit()
        return {"status": "updated", "weeks_ahead": value}
    finally:
        conn.close()


def week_plan(conn=None, now: Optional[int] = None) -> dict:
    """Build the current-plus-future weekly plan read model."""
    own = conn is None
    conn = conn or get_conn()
    try:
        now = int(now or time.time())
        weeks_ahead = get_weeks_ahead(conn)
        cap = _capacity(conn)
        delivery_hours = float(cap["delivery_hours"])
        current_complete = bool(project_load(conn=conn, now=now)["total"]["complete"])
        current_week = sprints._iso_week_str(now)
        week_ids = [sprints._iso_week_str(now, offset)
                    for offset in range(weeks_ahead + 1)]
        future_ids = week_ids[1:]
        allowed_kinds = (*COUNTED_KINDS, SELF_KIND)
        kind_marks = ",".join("?" for _ in allowed_kinds)
        project_rows = conn.execute(
            "SELECT id, name, color, COALESCE(kind,'product') AS kind, weekly_hours "
            "FROM projects WHERE status='active' AND archived_at IS NULL "
            f"AND COALESCE(kind,'product') IN ({kind_marks})",
            allowed_kinds,
        ).fetchall()

        plan_rows = []
        if future_ids:
            marks = ",".join("?" for _ in future_ids)
            plan_rows = conn.execute(
                "SELECT project_id, iso_week, hours FROM project_week_plan "
                f"WHERE iso_week IN ({marks})", future_ids,
            ).fetchall()
        plan_values = {(r["project_id"], r["iso_week"]): float(r["hours"])
                       for r in plan_rows}
        rows_by_week = {week: set() for week in future_ids}
        for row in plan_rows:
            rows_by_week[row["iso_week"]].add(row["project_id"])

        overdue = {}
        for row in conn.execute(
            "SELECT project_id, iso_week, hours FROM project_week_plan "
            "WHERE iso_week <= ? ORDER BY iso_week", (current_week,)
        ):
            overdue[row["project_id"]] = float(row["hours"])

        carriers = {
            row["id"] for row in project_rows
            if row["weekly_hours"] is not None and float(row["weekly_hours"]) > 0
        }
        nmax = int(delivery_hours // HOURS_FLOOR)
        weeks = []
        for offset in range(weeks_ahead + 1):
            current = offset == 0
            iso_week = week_ids[offset]
            starts = _week_start(now, offset)
            if current:
                positive = [float(r["weekly_hours"]) for r in project_rows
                            if r["weekly_hours"] is not None
                            and float(r["weekly_hours"]) > 0]
                planned_count = len(positive)
                hours = round(sum(positive), 2)
                n_projects = len(positive)
                complete = current_complete
            else:
                values = [float(r["hours"]) for r in plan_rows
                          if r["iso_week"] == iso_week and float(r["hours"]) > 0]
                planned_count = len(rows_by_week[iso_week])
                hours = round(sum(values), 2)
                n_projects = len(values)
                complete = (planned_count > 0
                            and carriers.issubset(rows_by_week[iso_week]))
            # None, NO 0.0, cuando no hay denominador: un porcentaje contra una
            # capacidad inexistente no es "cero carga", es una medición que no se
            # puede hacer — y `_band(0.0, complete)` la pintaría VERDE. Es la
            # misma regla de `capacity.project_load` (capacity.py:414); tenerlas
            # distintas era una segunda definición de "no se puede medir".
            utilization = (round(hours / delivery_hours, 4)
                           if delivery_hours > 0 else None)
            band_hours = _band(utilization, complete)
            band_count = ("red" if n_projects > nmax
                          else "amber" if n_projects == nmax else "green")
            if not current and planned_count == 0:
                band_hours = "unknown"
            band = ("unknown" if not current and planned_count == 0
                    else _worst_band(band_hours, band_count))
            weeks.append({
                "iso_week": iso_week,
                "label": "en curso" if current else _future_label(starts),
                "starts": starts.isoformat(),
                "current": current,
                "hours": hours,
                "n_projects": n_projects,
                "utilization": utilization,
                "complete": complete,
                "band": band,
                "band_hours": band_hours,
                "band_count": band_count,
                "planned_count": planned_count,
            })
        bucket_rank = {"budget": 0, "self": 1, "outside": 2}
        projects = []
        for row in project_rows:
            weekly = (None if row["weekly_hours"] is None
                      else float(row["weekly_hours"]))
            kind = row["kind"]
            bucket = ("self" if kind == SELF_KIND
                      else "outside" if weekly == 0 else "budget")
            cells = [{
                "iso_week": current_week,
                "hours": weekly,
                "source": "weekly_hours",
                "editable": False,
                "proposal": overdue.get(row["id"]),
            }]
            for iso_week in future_ids:
                cells.append({
                    "iso_week": iso_week,
                    "hours": plan_values.get((row["id"], iso_week)),
                    "source": "plan",
                    "editable": True,
                    "proposal": None,
                })
            positive_weeks = []
            if weekly is not None and weekly > 0:
                positive_weeks.append(current_week)
            positive_weeks.extend(
                week for week in future_ids
                if (plan_values.get((row["id"], week)) or 0) > 0
            )
            horizon = max(positive_weeks) if positive_weeks else None
            if horizon is None:
                horizon_label = "—"
            elif horizon in (current_week, week_ids[-1]):
                horizon_label = "sin planear"
            else:
                horizon_label = f"hasta {horizon.split('-')[1]}"
            projects.append({
                "id": row["id"], "name": row["name"],
                "color": row["color"], "kind": kind, "bucket": bucket,
                "horizon": horizon, "horizon_label": horizon_label,
                "cells": cells,
            })
        projects.sort(key=lambda p: (
            bucket_rank[p["bucket"]],
            -(next((c["hours"] or 0 for c in p["cells"] if not c["editable"]), 0)),
            p["name"],
        ))

        return {
            "current_week": current_week,
            "weeks_ahead": weeks_ahead,
            "delivery_hours": delivery_hours,
            "hours_floor": HOURS_FLOOR,
            "nmax": nmax,
            "weeks": weeks,
            "projects": projects,
        }
    finally:
        if own:
            conn.close()


def set_cell(project_id, iso_week, hours) -> dict:
    """Upsert one strictly-future project-week proposal."""
    current_week = sprints._iso_week_str()
    if not _valid_iso_week(iso_week):
        return {"status": "error", "error": "iso_week must be YYYY-Www"}
    if iso_week <= current_week:
        return {
            "status": "error",
            "error": "la semana en curso se declara en projects.weekly_hours "
                     "(PATCH /api/projects/{id})",
        }
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return {"status": "error", "error": "hours must be a number"}
    hours = float(hours)
    if hours < 0 or hours > 40:
        return {"status": "error", "error": "hours must be between 0 and 40"}
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not exists:
            return {"status": "error", "error": "project not found"}
        conn.execute(
            "INSERT INTO project_week_plan(project_id, iso_week, hours, set_at) "
            "VALUES (?,?,?,?) ON CONFLICT(project_id, iso_week) DO UPDATE SET "
            "hours=excluded.hours, set_at=excluded.set_at",
            (project_id, iso_week, hours, int(time.time())),
        )
        conn.commit()
        return {"status": "updated", "project_id": project_id,
                "iso_week": iso_week, "hours": hours}
    finally:
        conn.close()


def apply_overdue(project_id) -> dict:
    """Human promotion of the latest overdue proposal into weekly_hours."""
    current_week = sprints._iso_week_str()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT iso_week, hours FROM project_week_plan "
            "WHERE project_id=? AND iso_week<=? ORDER BY iso_week DESC LIMIT 1",
            (project_id, current_week),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "error", "error": "no overdue proposal"}
    hours = float(row["hours"])
    updated = sprints.update_project(project_id, weekly_hours=hours)
    if updated.get("status") == "error":
        return updated
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM project_week_plan WHERE project_id=? AND iso_week<=?",
            (project_id, current_week),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "updated", "project_id": project_id, "hours": hours}
