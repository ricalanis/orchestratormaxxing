# Parallel Orchestration Layer

Turns a pile of independent Claude Code sessions into a governed **fleet**. Six
patterns (from the research in `knowledge/research-multi-agent-orchestration.md`
+ `research-claude-code-parallel.md`), all additive sidecars on the Hermes
kanban DB — nothing here touches the PRD dashboard's `index.html` / `loop.py`.

One loop: **role → spec → work → ledger → guardrails.**

Console: **`/orchestration`** (the Fleet Console page). Core module:
`dashboard/orchestration.py`. MCP tools in `mcp_server.py`. Hook in `hooks/`.

**The one live DB is `~/.hermes/kanban.db`** (resolved in `dashboard/db.py`,
override `HERMES_KANBAN_DB`), shared with the running hermes-gateway and
versioned by `dashboard/migrations/runner.py` (`orch_migrations` ledger).
Repo-local `*.db` files are test artifacts, never data. `~/.hermes/projects.db`
is a **live, Hermes-owned** store (`hermes_cli/projects_db.py`) — the dashboard
must never open, write, or delete it, even when its tables look empty.

---

## 1. Sessions by role
Every session carries a role: `implementation · verification · docs · planning · review`.

- **Zero-config:** name a tmux session `claude-<project>-<role>` and the board
  parses the role off the name (`sessions.py`). `c <name> --role verify` does this
  (and, best-effort, registers the feature link too).
- **Registry:** `session_meta` table (role, feature, project, auto_compact,
  auto_abort). Set via `POST /api/session-meta`, MCP `set_session_role`, or the
  role dropdown on the console.

## 2. Task ledger
Structured results, not prose: `{summary, files_modified, risks, status}`.

- Canonical in the `task_ledger` table; mirrored to `~/.hermes/orchestration/ledger.jsonl`.
- Report via MCP **`report_ledger`** or `POST /api/tasks/<id>/ledger`. On
  `status=passed` it routes the task through loop.py's §7 accept/escalate rule;
  on `status=failed` it counts a strike (→ auto-abort, feature 6).
- Read: `GET /api/ledger`, `GET /api/tasks/<id>/ledger`; shown on the console.

## 3. Hooks notification
When a session needs your input, it surfaces instead of waiting silently.

- `hooks/orch-notify.py` on the `Notification` / `PreCompact` / `Stop` Claude
  Code hooks → `POST /api/session-events`. `Notification` → `input_needed`
  (attention queue); `Stop` clears that session's open asks.
- Install once (global, all projects): `python3 hooks/install-hooks.py`
  (idempotent; preserves existing hooks). Override target with `ORCH_DASHBOARD_URL`.
- The console's **🔔 Needs you** panel = `pending_input()` (unresolved `input_needed`).

## 4. Auto-compact
Detect a session whose transcript is getting large and `/compact` it.

- `context_estimate(size_kb)` proxies window fullness (bytes/4 vs
  `ORCH_CONTEXT_BUDGET`, flag at `ORCH_COMPACT_THRESHOLD`, default 72%). Surfaced
  as a context bar per session + `GET /api/compact-candidates`.
- Manual: `/compact` button → `POST /api/sessions/<host>/<name>/compact`.
- **Auto:** a session with `auto_compact=1` is compacted by the background
  **sweeper** when it crosses the threshold (rate-limited to 1/10 min).

## 5. Shared spec
A per-feature `spec.md` the operator (Hermes) controls; each session pulls only
its slice.

- Files under `~/.hermes/orchestration/specs/<feature>/spec.md`. Tag sections
  with `## @all` (shared) and `## @<role>`. Untagged preamble is shared.
- `spec_slice(feature, role)` returns preamble + `@all` + `@<role>` only —
  context hygiene + no cross-agent contradiction.
- MCP **`get_spec_slice`** / **`list_specs`**; `GET /api/specs/<feature>?role=`;
  `PUT /api/specs/<feature>`; edit on the console.

## 6. Auto-abort
A task that fails its contract **3×** (`ORCH_FAILURE_LIMIT`) is circuit-broken:
mark blocked → kill the live session (`sessions.kill_session`) → log
`auto_aborted` → queue a **clean restart plan** (fresh brief + acceptance,
linked to the original) in the operator Inbox. Never a blind infinite retry.

- Drives off `tasks.consecutive_failures`. Reported via ledger `status=failed`,
  `POST /api/tasks/<id>/fail`, or picked up by the sweeper.
- Manual: `POST /api/tasks/<id>/abort`.

---

## The sweeper (features 4 + 6, autonomous)
A background task in the FastAPI app (`ORCH_SWEEPER=1`, every
`ORCH_SWEEPER_INTERVAL=120`s) runs `orchestration.sweep()`: auto-compact opted-in
large sessions + auto-abort tasks over the failure limit. Manual: `POST
/api/orchestration/sweep`. Set `ORCH_SWEEPER=0` to disable.

## Env knobs
`ORCH_DIR` · `ORCH_CONTEXT_BUDGET` (160000) · `ORCH_COMPACT_THRESHOLD` (0.72) ·
`ORCH_FAILURE_LIMIT` (3) · `ORCH_SWEEPER` (1) · `ORCH_SWEEPER_INTERVAL` (120) ·
`ORCH_DASHBOARD_URL` (hook target).

## Doctrine
Agents report and propose; the operator's gates (trust, autonomy, review Inbox)
stay authoritative — same governance as `loop.py`'s `route_result`. Nothing here
auto-commits load-bearing plan structure.

---

## The two-orchestrator contract (Hermes macro / harness micro)

Two orchestrators share this platform. The contract that keeps them from
fighting over the same state (Phase 4, item 6):

**Hermes = MACRO.** Strategy and commitment: the roadmap, projects/epics,
cycles, the day plan, dispatch, and the rituals (standup/wrap-up). It connects
over the **privileged** MCP scope (`HERMES_MCP_SCOPE=privileged` +
`HERMES_MCP_ORIGIN=hermes` in `~/.hermes/config.yaml`) and is the only agent
besides the operator that may restructure the plan (`plan_day`, `edit_roadmap`,
`create_epic`, `set_contract`, …). Hermes **never executes work items** and
never writes the verification ledger.

**The harness (Claude Code / OpenCode fleet) = MICRO.** Execution and
evidence: `claim → plan → code → validate → report`. Fleet sessions connect
over the **default** scope (orient / pull / report / declare) and cannot
restructure the plan. Within the micro layer the roles split further —
implementation sessions do the work and `report_result`; a **separate
verification-role session** runs the operator-authored contract
(`run_contract`) and is the **only** `task_ledger` writer (never self-review).

**The joint invariants** (enforced in code, ratcheted by `bin/orch-verify`):
1. One status writer path; every transition is an event on the spine.
2. `done` for an agent task requires an independent `role='verification'`
   ledger row — auto-accept additionally requires HIGH trust + `autonomy=auto`.
3. Trust and autonomy dials are operator-only; neither orchestrator can raise
   its own.
4. Macro writes plan structure, micro writes execution evidence — neither
   writes the other's layer; the dashboard writes nothing except through the
   sanctioned sidecar.
