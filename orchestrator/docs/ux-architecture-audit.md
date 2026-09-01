# Hermes Orchestrator — UX & Architecture Audit

_Senior architect + UX review. Author: Claude (Opus 4.8), 2026-07-05._
_Scope: `dashboard/` (api.py, templates/index.html, usage.py, sprints.py, crm.py, db.py, graph/strategy), `mcp_server.py`, and the live `~/.hermes/kanban.db`._

> **Method note.** Every claim below is grounded in either a `file:line` reference or a direct query
> against the live DB (75 tasks, 10 projects, 6 initiatives, 1 deal, 2 sprints). Where the operator
> reported a symptom, this audit traces it to the exact code path and the exact data condition that
> produces it. Nothing here is inferred from the UI alone.

---

## 0. Executive summary

The backend is in far better shape than the UI suggests. The **data model already has the full
FK spine** (`deal → initiative → project → epic → task → run → commit`, plus `task ↔ cycle` and
`task ↔ session`), and `api.py` exposes a traversal endpoint for nearly every hop. The product does
**not** have a schema problem so much as three connected problems:

1. **Empty layers.** The FK spine has holes the UI faithfully renders as "nothing here": **0 epics
   exist**, so `0/75` tasks reach an initiative through the intended path; only **3/75** tasks are
   committed to a cycle. The chain isn't broken — it's unpopulated, and nothing in the UI makes
   populating it easy.
2. **Read-only dead-ends.** Where the backend _does_ join across domains (CRM drill-down, Roadmap
   drill-down), the UI renders the result as a flat, unclickable list. You can _see_ a deal's tasks
   but you can't _click_ into one. There is no shared entity router — every cross-link is a bespoke
   modal opener, and most views have none.
3. **Two overlapping "board" concepts with no bridge.** The **Cycle** tab is a 4-column board scoped
   to `WHERE sprint_id = <active cycle>` (3 tasks). **My Work / The Fleet** are the firehose showing
   all 75. There is no one-click way to move a task from the firehose into the cycle, so the cycle
   stays empty and the two boards look like different apps showing contradictory counts.

Fixing #2 and #3 is mostly UI wiring against endpoints that already exist. Fixing #1 is a workflow +
one small schema decision (how a task attaches to an initiative). Details and a prioritized plan follow.

---

## 1. Data Model Analysis

### 1.1 The actual schema (from the live DB)

Tables (row counts in parens): `accounts`(1), `agents`(7), `contacts`(1), `deal_events`(1),
`deals`(1), `epics`(**0**), `initiative_events`(6), `initiatives`(6), `orch_meta`(2), `projects`(10),
`session_events`(306), `session_meta`(3), `sprints`(2), `task_attachments`(0), `task_comments`(43),
`task_events`(1672), `task_ledger`(54), `task_links`(19), `task_runs`(88), `task_sprints`(3),
`tasks`(75).

The FK spine, as declared in the schema:

```
accounts ──< contacts
   │
   └──< deals.account_id
          deals.contact_id ─> contacts
          deals.initiative_id ─> initiatives        ← the "strategy join" (crm.py:84)
                                    │
initiatives.project_id ─> projects  │
                                    └──< epics.initiative_id     (epics.project_id ─> projects)
                                                │
                                                └──< tasks.epic_id
projects ──< tasks.project_id  (75/75 populated)
sprints.project_id ─> projects
sprints ──< tasks.sprint_id          (3/75 populated)   ← the LIVE cycle pointer
sprints ──< task_sprints (join)      (3 rows)           ← the APPEND-ONLY commit ledger
tasks ──< task_links (parent_id, child_id)  (12/75 tasks have a parent)
tasks ──< task_runs, task_ledger, task_comments, task_events
tasks.session_id ─> (session hard-link)
```

`db.py:24` enables `PRAGMA foreign_keys = ON` per connection — the declared FKs are actually enforced.
`projects.slug` is the single canonical namespace (`db.resolve_project`, db.py:38) that every registry
resolves through. That part of the model is clean.

### 1.2 Population reality (the gaps that matter)

| Link | Column | Populated | Consequence |
|---|---|---|---|
| task → project | `tasks.project_id` | **75/75** | Solid. Every task has a home. |
| task → parent | `tasks.parent_id` | 12/75 | Sub-task graph partially used. |
| task → cycle | `tasks.sprint_id` | **3/75** | The Cycle view is nearly empty (see §1.3). |
| task → epic | `tasks.epic_id` | **0/75** | **The initiative↔task path is entirely severed.** |
| epics exist | `epics` rows | **0** | Roadmap progress can't roll up from real work. |
| deal → initiative | `deals.initiative_id` | 1/1 | Works (the sample deal → its initiative). |
| initiative → project | `initiatives.project_id` | 6/6 | Works. |

**The single most consequential gap: the epic layer is unused.** Roadmap progress is _derived_ by
rolling up an initiative's **epics' tasks** (`api.py:1711` → `graph.initiative_progress`). With **0
epics** and **0/75 tasks carrying `epic_id`**, `0` tasks are reachable from any initiative via the
intended path. The roadmap literally cannot see the 32 orchestrator tasks, the 23 GPU-ops tasks, etc.

Worse, the fallback can't save it in general: **3 initiatives share `proj_orchestrator`**
("Dashboard v2", "Multi-Agent Coordination", "MCP Server Expansion"). So `project_id` alone can't tell
you _which_ initiative a task belongs to — that disambiguation was the epic layer's whole job, and it's
empty. `crm.deal_drilldown` (crm.py:410) falls back to `WHERE project_id = ?` when an initiative has no
epics; that works for the sample deal (its project has exactly 1 task) but would return **all 32**
orchestrator tasks for any orchestrator initiative — over-broad and misleading.

### 1.3 Why the Sprint/Cycle view shows only 3 tasks — the definitive answer

**It is not a `LIMIT`, not a `[:3]`, not a UI slice.** Three independent reads of the code path
(api.py, sprints.py, index.html) all confirm: the only caps in the entire cycle read path are a
client-side `slice(0, 6)` on the _Done_ column (index.html:1521) and `slice(0, 40)` on the icebox.
There is no "3" anywhere.

The Cycle board query (`sprints.get_cycle_board`, sprints.py:942):

```python
rows = conn.execute(
    f"SELECT {_BOARD_TASK_FIELDS} FROM tasks t "
    "LEFT JOIN projects p ON t.project_id = p.id WHERE t.sprint_id = ? "
    "ORDER BY COALESCE(t.board_order, 2000000000), t.priority DESC, t.created_at ASC",
    (cid,)).fetchall()
```

`WHERE t.sprint_id = <active cycle id>`. It shows 3 tasks because **exactly 3 tasks have
`sprint_id` set** — confirmed in the DB: `SELECT COUNT(*) FROM tasks WHERE sprint_id IS NOT NULL` → 3,
all pointing at `spr_083acc33`. The other 72 have `sprint_id = NULL` (they sit in the icebox/backlog).
Meanwhile the "Kanban" the operator compares against — **My Work + The Fleet** tabs — reads
`GET /api/tasks?limit=0` (index.html:2836), the unpaginated firehose of all 75. Same DB, different
`WHERE` clause: one filters by cycle membership, the other doesn't.

**Two structural reasons the cycle stays sparse:**

- **Re-commit gate.** `close_sprint` / `roll_cycle` (sprints.py:634, 765) clear carry-overs'
  `sprint_id` to `NULL` and start the next cycle **empty** by design — carry-over is a deliberate
  re-commit decision, not automatic. So every rolled cycle begins at zero and must be re-populated.
- **Committing is privileged-only.** The only path to set `sprint_id` is `assign_task_sprint`
  (`PRIVILEGED_TOOLS`, mcp_server.py:110). Fleet agents piling work onto the Kanban board via
  `create_task` have **no way to add it to the cycle** — so agent throughput never reaches the cycle.

**Proximate cause of the specific "3" you see right now:** there are **two rows** in the `sprints`
table — `spr_083acc33` "Sprint 1 — Dashboard polish" (`status=active`, project-scoped) and
`cyc_27dd8d89` "Cycle 2026-W28" (`status=planning`, `project_id=NULL`, **0 tasks**). The active-cycle
picker (`sprints.py:926`) filters `WHERE status='active'`, so it surfaces **Sprint 1** (3 tasks) and
ignores the planning week-cycle. You are looking at an old project sprint, not this week's cycle — and
this week's cycle is empty and never started. This is an **identity problem**: the `sprints` table
holds two different concepts (project-scoped 14-day "sprints" _and_ cross-project weekly "cycles"),
and the picker mixes them. See §5.4.

### 1.4 Dual source of truth for task↔cycle (a real but subtle risk)

Task membership in a cycle is stored **twice**:
- `tasks.sprint_id` — the **live pointer** (current membership; what the board reads). One cycle at a time.
- `task_sprints` — an **append-only commit ledger** `{task_id, sprint_id, committed_at, outcome ∈
  delivered|carried|dropped}` used for velocity/history (sprints.py:396).

This is defensible (it's HEAD vs. reflog), and `assign_task_sprint`/`close_sprint` update both together.
But there is **no invariant check**: if any writer sets `sprint_id` without a ledger row (or vice
versa), velocity and the board silently disagree. Today they agree (3 = 3), but this is a latent
consistency bug worth a guard (see §5.1).

---

## 2. View-by-View Breakdown

The dashboard is a single-page app (`index.html`, 5361 lines) with **13 tabs** toggled by
`switchTab()` (index.html:1073). Default is **Today**.

### 2.1 Today (☀️) — the home canvas
- **Data:** server-composed day plan — Overdue / Do (by project) / Review / Needs-you / Later.
- **Source:** `GET /api/day-plan` (api.py:640 → `canvas.get_day_plan`). Deliberately does _not_ re-filter `/api/tasks`.
- **Controls:** `→ Today` plan, `✓` done, `↓` unplan, `✓ Accept` (review cards), bulk-accept, `✕` reject, resolve input-needed.
- **Cross-links:** cards → `showTaskDetail`; input-needed rows → `openSessionPanel`. **This is one of only two views that links outward well.**
- **Status:** Healthy. Good model of a composed, opinionated view.

### 2.2 Cycle (🎯) — the weekly board  ← **P0 lives here**
- **Data:** 4 columns (backlog / in_progress / review / done) scoped to the active cycle, + burndown SVG, week calendar strip, per-project status cards, icebox.
- **Source:** `GET /api/cycle/active/board` (api.py:1497 → `sprints.get_cycle_board`) + `GET /api/cycles/calendar`, in parallel (index.html:1215).
- **Controls:** SortableJS drag (reorder + cross-column status change), calendar drag-to-week, icebox `→ commit` / bulk-commit, `wrapCycle`/`deleteCycle`/`startThisWeekCycle`.
- **Broken:** shows 3 tasks because only 3 are committed (§1.3); surfaces an old project-sprint instead of this week's planning cycle (§1.3); cycle cards `cycleCard` (index.html:1620) are **read-only** — no Accept/Reject.
- **Missing:** any commit affordance reachable from the primary Kanban; a clear "this cycle is empty — pull in your active work" empty state; reconciliation of Sprint vs Cycle identity.

### 2.3 & 2.4 My Work + The Fleet — the primary Kanban  ← **P0 lives here**
- **Data:** the firehose. `routeTask` (index.html:2861) sorts every task into `mine` (Inbox/Doing/Done) or `fleet` (Pool/Working/Done) by owner-of-next-action.
- **Source:** `GET /api/tasks?limit=0` (index.html:2836) — all 75, client-routed. No pagination.
- **Card controls** (`createTaskCard`, index.html:3026): drag grip, `⋯` menu (move / dispatch / reclaim / pool toggle / autonomy), and an **action row that only appears conditionally**:
  - **`✓ Accept` renders _only_ when `needsReview(task)` i.e. `status === 'review'`** (index.html:3050, 3070). The live DB has **0 tasks in `review`** → the Accept button is **never visible** to the operator. This is the reported "no Accept button" bug (§4, P0-b).
  - **`✕ Reject`** shows on any non-rejected card.
  - **No Claim button exists at all.** "Claim" is an agent/MCP action needing an `agent` param (`POST /api/tasks/{id}/claim`, api.py:701). The operator's only way to "take" a pool task is `⋯ → reclaim` (fleet→me) buried in the menu. There is no primary "I'll take this" button on Pool/Inbox cards.
- **Cross-links:** card body → `showTaskDetail`; project chip → `openProjectDetail`; session chip → `openSessionPanel`. Good _within_ tasks; nothing to Cycle/Roadmap.

### 2.5 Sessions
- **Data:** claude-code + opencode sessions, active/recent vs idle, tag filter bar.
- **Source:** `GET /api/sessions` (api.py:1210). Modal: output / send / revive / history.
- **Broken/missing:** **one-way.** You reach Sessions _from_ tasks (session chip), but the session panel does **not** list the tasks that session is working on — even though `GET /api/sessions/{host}/{name}/tasks` (api.py:1653) exists and returns exactly that. Pure wiring gap.

### 2.6 Roadmap (🗺️)
- **Data:** initiatives grouped by quarter; each card shows tier/status/health/confidence + **derived** progress.
- **Source:** `GET /api/roadmap` (api.py:1702). Drill-down: `GET /api/roadmap/{id}/drilldown` (api.py:1752 → `graph.initiative_drilldown`) renders Initiative → Project → epics/cycles/unscheduled → tasks.
- **Broken:** progress is derived from epics' tasks, but with **0 epics** most initiatives roll up to ~0% regardless of real work. Drill-down **task rows have no `onclick`** (index.html:5161) — a dead-end list. `+ Add to Sprint` is a one-way write (`PATCH /api/roadmap/{id}` `{in_sprint:true}`), not navigation.
- **Missing:** click-through from a task row to its detail / cycle; a way to attach work to an initiative that doesn't require the (unused) epic layer.

### 2.7 CRM (💼)
- **Data:** pipeline board by stage; deal cards carry an **initiative chip** (`🎯 {progress}% {title}`) when linked.
- **Source:** `GET /api/crm/pipeline` (api.py:1792). Drill-down: `GET /api/crm/deals/{id}/drilldown` (api.py:1874 → `crm.deal_drilldown`) — **the full spine**: deal → initiative → tasks → runs → commits, with a "⛓️ chain complete" flag.
- **Broken:** the richest cross-domain data in the whole app, rendered **inline and read-only**. Chain tasks have no `onclick`; you cannot jump to the Roadmap card for that initiative, to the Cycle board, or to task detail. The strongest join in the product is a visual dead-end.

### 2.8 Memory (🧠) + Graph sub-tab
- **Source:** `GET /api/memory` (stores) / `GET /api/graph` (vis-network). Node click → in-graph detail only.
- **Status:** self-contained; graph is the _only_ place the object model is visualized, but it doesn't link to the tabs that own those entities.

### 2.9 Usage (📊), 2.10 Health (🩺), 2.11 Lakehouse (🏞️)
- **Sources:** `GET /api/usage`, `GET /api/ops-status`, `GET /api/lakehouse/overview`.
- **Status:** all healthy and self-contained. No outbound links (appropriate for dashboards, except Lakehouse answers like "how many tasks did we accept?" could deep-link to the filtered board).

### 2.12 Archive (📦) & 2.13 Coordinators (🧭)
- **Archive:** `GET /api/archive` (shipped, bucketed by day/week/month) + `GET /api/tasks?limit=0` (all). Cards link to `showTaskDetail`. Healthy.
- **Coordinators:** derived Code/Research/Commercial view from `GET /api/coordinators`. Task rows **read-only, no onclick** — another dead-end.

### 2.13 Cross-view navigation — the systemic finding
There are exactly **three** shared cross-domain primitives — `showTaskDetail`, `openProjectDetail`,
`openSessionPanel` — plus two `switchTab` shortcuts (Done "+N more" → Archive; header widget →
Sessions). Everything else is a per-view bespoke modal or a dead-end list. **There is no global entity
router, no breadcrumb, no back-stack, no URL routing** (modals are explicitly "not a route",
index.html:3742). That is why the views feel like separate apps: they share data through the DB but
almost nothing through the UI.

---

## 3. Integration Gaps (where the thread breaks)

| Thread | Backend | UI | Verdict |
|---|---|---|---|
| **CRM deal → initiative → project → task** | ✅ `crm.deal_drilldown` returns the whole spine | ❌ rendered inline, read-only, no click-through | **Wiring gap.** Backend done; UI is a dead-end. |
| **Kanban firehose → Cycle** | ⚠️ commit is privileged-only; re-commit gate empties each cycle | ❌ no commit affordance on the primary board | **Workflow gap.** Two boards, no bridge. |
| **Roadmap → Cycle** | ⚠️ `+ Add to Sprint` sets a flag, not membership | ❌ no navigation from initiative to its cycle | **Model + wiring gap.** |
| **Roadmap initiative → task** | ❌ requires epics; 0 epics exist | ❌ drill-down rows non-clickable | **Population + model gap.** The big one (§1.2). |
| **Sessions → Tasks** | ✅ hard link `tasks.session_id`; endpoint exists | ⚠️ task→session works; session→tasks not rendered | **Half-wired.** One-way only. |

**The through-line:** the operator's four complaints are one root problem viewed from four angles —
_the model connects the entities; the UI does not let you walk the connections, and two key layers
(epic, cycle-membership) are unpopulated so even the walkable parts look empty._

---

## 4. UX Recommendations (prioritized)

### P0 — Broken things the operator hits daily

**P0-a. Make the Cycle actually reflect the board.** Pick both:
1. **Add a primary commit affordance to the firehose Kanban.** On My Work / Fleet cards, add a one-click
   `＋ Cycle` action (card action row or `⋯` menu) → `PATCH /api/tasks/{id}/sprint` with the active
   cycle id. Endpoint already exists (api.py:1546). This is the missing bridge.
2. **Fix the Cycle identity + empty state.** Default the Cycle tab to _this week's_ cycle; if it's in
   `planning` and empty, show a first-class empty state: "Cycle 2026-W28 hasn't started — [Start &
   pull in your 6 active tasks]" that bulk-commits `in_progress`/`running` board tasks via
   `POST /api/cycles/{id}/commit` (api.py:1552). Stop silently surfacing an old project-sprint.
3. **(Optional, opt-in) Auto-commit on start.** When a task moves to `in_progress`, auto-assign it to
   the active cycle unless the operator opts out. Closes the "agent work never reaches the cycle" hole
   without a manual step.

**P0-b. Make Accept/Claim always reachable from the board.**
- The Accept button is invisible because it's gated on `status==='review'` and nothing is in review
  (agents auto-accept or the operator never sees the review lane). **Surface Accept in two places
  unconditionally:** (a) keep the card action-row Accept for `review`, but also (b) expose Accept/Reject
  in the `⋯` menu for any agent-owned `done`-but-unreviewed task, and add a persistent **Review count
  badge** on the Fleet board so a review queue is discoverable.
- **Add a `Claim / Take` primary button on Pool and Inbox cards** — the operator's "I'll do this"
  action. Today claiming is agent-only; give the human a first-class button that reassigns to the operator's assignee
  and moves the card to Doing (via `PATCH /api/tasks/{id}`). This is likely what "can't accept/claim
  from the board" actually means.

### P1 — Integration gaps (make the thread navigable)

- **CRM drill-down click-through.** Make every chain row clickable: task → `showTaskDetail`; initiative
  header → jump to Roadmap + open that initiative's drill-down; deal value/stage stays. Zero new
  endpoints — pure wiring against data already returned by `crm.deal_drilldown`.
- **Roadmap drill-down click-through.** Wire `onclick` on the `taskRow` (index.html:5161) →
  `showTaskDetail`; add a "→ Cycle" chip when the task has a `sprint_id`.
- **Session → Tasks panel.** In `openSessionPanel`, render the session's tasks from the existing
  `GET /api/sessions/{host}/{name}/tasks` (api.py:1653). Makes Sessions↔Tasks bidirectional.
- **Coordinators click-through.** Wire the read-only task rows to `showTaskDetail`.

### P2 — Polish / unified design language

- **A shared entity drawer + breadcrumb.** Replace the bespoke `showTaskDetail`/`openProjectDetail`/
  `openSessionPanel` modals with one drawer that always renders a breadcrumb of parents (deal ▸
  initiative ▸ project ▸ epic ▸ task) and a list of clickable children. Every entity opens the same
  drawer; every relationship is a link. This single component collapses most of the "separate apps"
  feeling.
- **URL routing / back-stack.** Reflect the open tab + entity in the URL (`?tab=cycle&task=t_...`) so
  deep links and browser Back work. Today all state is in-memory.
- **Unify Pool/Working/Done vs backlog/in_progress/review/done vocabulary** across Cycle and
  My-Work/Fleet so a task's column is consistent whichever board you're on.

### P3 — Future

- Burndown is already computed for the cycle; extend to per-initiative and per-project burnup.
- Notifications on `needs-you` (blocked + input-needed) beyond the Today lane.
- Lakehouse answers that deep-link into a filtered board.
- A real "review inbox" surface (with the P0-b badge as its entry point).

---

## 5. Architecture Recommendations (how the model should evolve)

### 5.1 Task ↔ cycle: keep both stores, add an invariant
Do **not** collapse `tasks.sprint_id` and `task_sprints` — they serve different roles (current
membership vs. append-only history). Instead:
- Document the invariant: _a live `sprint_id` implies an open (`outcome IS NULL`) ledger row for the
  same pair._
- Add a cheap consistency check (a `harness-verify`-style query, or a `/healthz` check) that flags
  drift. This turns the latent dual-write bug (§1.4) into a caught one.
- The real fix for "cycle is empty" is **workflow, not schema** (P0-a) — make committing trivial and
  optionally automatic.

### 5.2 Initiative ↔ task: stop depending on an unused epic layer
This is the highest-leverage schema decision. Three options, recommend **B**:
- **A. Enforce epics.** Require every initiative to have ≥1 epic and auto-create a default "General"
  epic on initiative creation; assign tasks to epics in the UI. Keeps the roll-up path but adds
  ceremony the team clearly isn't doing (0 epics after 6 initiatives).
- **B (recommended). Add an optional direct `tasks.initiative_id`.** Let a task attach straight to an
  initiative, with the epic as an _optional_ finer grouping. Compute initiative progress by
  `COALESCE(epic→initiative, task.initiative_id)` and only fall back to `project_id` when the project
  has exactly one initiative. This makes the Roadmap see real work immediately, disambiguates the
  3-initiatives-on-one-project case, and doesn't force the epic ceremony.
- **C. Do nothing, rely on `project_id`.** Rejected: ambiguous whenever a project has >1 initiative
  (already true for `proj_orchestrator`).

### 5.3 CRM deal ↔ initiative: already correct
`deals.initiative_id` exists, is validated on write (`crm.create_deal`, crm.py:179), and is populated.
No schema change — this hop just needs UI navigation (P1). Keep it.

### 5.4 Sprint vs Cycle: pick one concept
The `sprints` table currently stores two things: project-scoped 14-day **sprints** (`create_sprint`,
`project_id` NOT NULL) and cross-project weekly **cycles** (`create_cycle`, `project_id` NULL, ISO
week). The active-cycle picker mixes them, which is why the UI shows "Sprint 1" under a tab labeled
"Cycle". Recommend **unify on the weekly cross-project cycle** as the one timebox:
- Migrate the lone `spr_083acc33` project-sprint into a cycle (or close it), so `status='active'` is
  unambiguous.
- Treat "project" as a _filter_ on a cycle board, not a separate row-type.
- Rename consistently in the UI (one word: "Cycle").

### 5.5 Agent session ↔ task: already solid
`tasks.session_id` is a hard link, enriched onto cards (`enrich_tasks_with_sessions`, api.py) and
queryable both directions (`graph.tasks_for_session`, `graph.set_task_session`). Keep. Only the
session→tasks _render_ is missing (P1).

---

## 6. Proposed Unified Navigation

The model already supports the walk; the UI needs to expose it. Target: **one shared entity drawer,
a breadcrumb of parents, clickable children, and URL routing** — so every entity is a hyperlink and
the three user journeys below just work.

```
                    ┌─────────────────────────────────────────────────────────┐
                    │  Breadcrumb (always): Account ▸ Deal ▸ Initiative ▸       │
                    │            Project ▸ Cycle ▸ Epic ▸ Task ▸ Run            │
                    └─────────────────────────────────────────────────────────┘

  CRM                 Roadmap              Cycle board          Sessions
  ───                 ───────              ──────────           ────────
  Deal ──initiative_id──▶ Initiative ──project_id──▶ Project ──┬──▶ Tasks ──sprint_id──▶ Cycle
   │                        │  (progress rolls UP the same joins)│      │
   └─ drill-down chain      └─ drill-down: proj▸cycle▸epic▸task  │      └─ session_id ──▶ Session ──▶ (its tasks)
      (now CLICKABLE)          (rows now CLICKABLE)              └─ epic_id / initiative_id (§5.2)
```

**Journey 1 — Commercial (start at a deal):** CRM ▸ click deal → drawer shows `deal → initiative`
(click → Roadmap initiative) `→ project → tasks` (click any → task detail → its cycle, its session,
its runs). Every hop a link. All data already returned by `crm.deal_drilldown`.

**Journey 2 — Execution (start at the cycle):** Cycle tab defaults to this week; empty state offers
"Start & pull your active tasks"; committed tasks match a filter of the firehose Kanban (same
vocabulary, same cards). One board, two lenses (all work vs. this-cycle), bridged by a `＋ Cycle`
button.

**Journey 3 — Strategy (start at the roadmap):** Roadmap ▸ initiative ▸ drill-down (now clickable) →
project → cycle → task progress, with real % because tasks attach to initiatives via the direct link
(§5.2) instead of the empty epic layer.

---

## Appendix A — Fastest path to "it feels connected" (2–3 days of UI wiring, 0 new endpoints)
1. Wire `onclick → showTaskDetail` on every read-only task row (Roadmap 5161, CRM chain, Coordinators). _(P1)_
2. Render session→tasks in `openSessionPanel` from the existing endpoint. _(P1)_
3. Add `＋ Cycle` and `Claim/Take` buttons to firehose cards; add `⋯ → Accept` for unreviewed done. _(P0)_
4. Fix the Cycle tab default + empty state to target this week's cycle. _(P0)_

## Appendix B — Follow-on schema work (1 small migration)
1. Add nullable `tasks.initiative_id` + progress `COALESCE` roll-up (§5.2, option B).
2. Unify Sprint/Cycle: migrate/close `spr_083acc33`; one active cycle (§5.4).
3. Add the `sprint_id ⇔ task_sprints` consistency check to `/healthz` (§5.1).

---
_End of audit._
