# Cohesive Sprint/Week/Backlog Model — Design Spec

## Problem Statement

The current model has 3 independent storage mechanisms that don't sync properly, causing user confusion:

1. `tasks.sprint_id` — which cycle a task is committed to (execution dimension)
2. `tasks.scheduled_week` — which ISO week a task is planned for (planning dimension)
3. `task_sprints` — append-only commit ledger (audit trail)

**The gap:** `scheduled_week` is a planning tag that doesn't translate to sprint membership until the sprint object exists and the task is committed. A task can have `scheduled_week=W29` but no W29 sprint exists yet, so it falls into the icebox mixed with truly unscheduled tasks. The user can't distinguish "next week" from "backlog" in the UI.

## Current State (the mess)

### Storage
- `tasks.sprint_id` → points to active sprint (W28). NULL = icebox.
- `tasks.scheduled_week` → ISO week tag ("2026-W29"). NULL = truly unscheduled.
- `task_sprints` → {task_id, sprint_id, committed_at, outcome}. Append-only.
- `sprints` table → cycles with start_date/end_date, status (active/completed).

### What the user sees
- **Cycle tab** → shows active sprint board (tasks WHERE sprint_id = active). Icebox drawer shows sprint_id IS NULL (mixes W29-scheduled with truly unscheduled).
- **Today tab** → "later" section shows tasks with no planned_for date (mixes everything not planned for today).
- **No way to see "next week" or "+2" as distinct buckets.**

### The confusion
1. Moving a task to "next week" sets scheduled_week=W29 but doesn't create a W29 sprint → task lands in icebox
2. When W28 sprint closes and W29 is auto-created, W29-scheduled tasks DON'T auto-commit → they stay in icebox
3. The icebox mixes "scheduled for a future week" with "truly unscheduled backlog"
4. The day plan "later" section doesn't distinguish sprints or weeks

## Proposed Cohesive Model

### 4 Buckets (the mental model)

| Bucket | Definition | UI Location |
|---|---|---|
| **This Week** | `sprint_id = active_sprint.id` | Cycle board (current) |
| **Next Week (+1)** | `scheduled_week = next_week_ISO` AND `sprint_id IS NULL` | Cycle tab → "Next Week" drawer |
| **+2 and beyond** | `scheduled_week >= +2_week_ISO` AND `sprint_id IS NULL` | Cycle tab → "Future" drawer (collapsible) |
| **Backlog** | `sprint_id IS NULL` AND `scheduled_week IS NULL` | Cycle tab → "Backlog" drawer (current icebox) |

### Key Principle

`scheduled_week` is the **planning dimension**. `sprint_id` is the **execution dimension**. They sync at two points:

1. **Sprint creation:** When a new sprint is created (auto on close, or manual), all tasks with `scheduled_week` matching that sprint's ISO week auto-commit to it.
2. **Sprint close:** When a sprint closes, unfinished tasks get `scheduled_week` stamped to next week (if not already set), then sprint_id clears to icebox.

### Changes Needed

#### 1. Backend: `sprints.py`

**A. `get_cycle_board()` — add next-week and future drawers**

Currently returns: `{cycle, columns, icebox, ...}`

Change to return:
```
{
  cycle: {...},
  columns: {backlog, in_progress, review, done},  // THIS week
  next_week: [...],      // tasks with scheduled_week = next ISO week, no sprint
  future: [...],         // tasks with scheduled_week >= +2 weeks, no sprint
  icebox: [...],         // tasks with no sprint AND no scheduled_week (truly unscheduled)
  ...
}
```

**B. Auto-commit on sprint creation**

In `close_sprint()` (or `create_cycle()`), after creating the next sprint:
- Find all tasks with `scheduled_week` matching the new sprint's ISO week
- Auto-commit them to the new sprint via `assign_task_sprint()`
- Log a `cycle_auto_committed` event

**C. `close_sprint()` — stamp unfinished tasks with scheduled_week**

When closing a sprint, unfinished (carried) tasks should:
- Get `scheduled_week` set to next week's ISO (if not already set)
- Get `sprint_id` cleared to NULL (existing behavior)
- This way they show in "Next Week" until the next sprint is created and auto-commits them

**D. `get_icebox_tasks()` — split into 3 functions**

- `get_next_week_tasks()` — scheduled_week = next week, no sprint
- `get_future_tasks()` — scheduled_week >= +2 weeks, no sprint
- `get_icebox_tasks()` — no sprint, no scheduled_week (truly unscheduled, renamed semantics)

#### 2. Backend: `canvas.py`

**Day plan "later" section** — group by:
- `this_week` — sprint_id = active, no planned_for date
- `next_week` — scheduled_week = next week, no sprint
- `future` — scheduled_week >= +2, no sprint
- `backlog` — no sprint, no scheduled_week, no planned_for

#### 3. Frontend: `index.html` — Cycle tab

**Current:** Cycle board (columns) + icebox drawer at bottom.

**New:** Cycle board (columns) + 3 drawers at bottom:
1. **Next Week** (amber accent) — tasks scheduled for next week. Each card has a "commit to cycle" button (appears when next sprint is created).
2. **Future** (purple accent, collapsible) — tasks scheduled for +2 and beyond. Grouped by week.
3. **Backlog** (gray, current icebox style) — truly unscheduled tasks.

Each drawer shows task count and can be collapsed.

#### 4. Frontend: Today tab "later" section

Group the "later" tasks into subsections:
- This Week (not planned for today)
- Next Week
- Future
- Backlog

#### 5. API: New endpoints (or extend existing)

- `GET /api/sprints/cycle-board` already exists — extend response with `next_week` and `future` arrays
- No new endpoints needed if we extend the existing board response

#### 6. Sync rules (the invariant)

These rules must ALWAYS hold:
1. A task with `sprint_id` set should NOT have `scheduled_week` pointing to a different week (already fixed in previous commit)
2. A task with `scheduled_week = current_week` and no sprint should auto-commit when the active sprint matches (new)
3. A task with `sprint_id` set should have `scheduled_week` cleared or matching (already fixed)
4. The icebox (truly unscheduled) = `sprint_id IS NULL AND scheduled_week IS NULL` (new semantics)

### Migration

- No schema changes needed (all columns exist)
- Data migration: run a one-time script to ensure existing tasks comply with the new invariants
- The `sprint_ledger_drift()` check should be extended to also verify `scheduled_week` consistency

### Tests

Write tests for:
1. `get_cycle_board()` returns next_week, future, icebox correctly
2. Auto-commit on sprint creation works
3. `close_sprint()` stamps unfinished tasks with scheduled_week
4. Sprint sync invariant holds after all operations
5. Day plan "later" section groups by week
6. Icebox only contains truly unscheduled tasks

### What NOT to change

- `planned_for` / `plan_order` — these are the TODAY canvas commitment, orthogonal to sprint/week
- `due_date` — deadline-driven, orthogonal to sprint/week
- The append-only `task_sprints` ledger — still the audit trail
- Board statuses (backlog/ready/in_progress/blocked/review/done) — unchanged

### File paths

- Backend: `orchestrator/dashboard/sprints.py` (main changes)
- Backend: `orchestrator/dashboard/canvas.py` (day plan grouping)
- Backend: `orchestrator/dashboard/api.py` (if endpoint changes needed)
- Frontend: `orchestrator/dashboard/templates/index.html` (Cycle tab drawers, Today tab grouping)
- Tests: `orchestrator/tests/test_sprint_week_model.py` (new test file)

### Commit message format

```
feat: cohesive sprint/week/backlog model — 4 buckets (this week, next week, future, backlog)

- get_cycle_board() now returns next_week + future + icebox (3 drawers)
- close_sprint() stamps unfinished tasks with scheduled_week (carries to next week)
- Auto-commit on sprint creation: scheduled_week matching → sprint_id
- get_icebox_tasks() = truly unscheduled only (no sprint, no scheduled_week)
- Day plan "later" section groups by week bucket
- Cycle tab: 3 collapsible drawers (Next Week, Future, Backlog)

QA: [N] tests pass
```