---
description: Persist this session's work to the current project's docs/ — append a changelog entry and refresh the WIP handoff note via the deterministic session-log tool.
argument-hint: [optional one-line summary to seed the changelog entry]
---

You are wrapping up the current session. Persist what happened into **this project's** `docs/`
so the next session (or the next person) can resume without re-deriving context. Scope is the
current project only (git repo root, else the current folder) — `session-log` resolves the path.

Optional seed summary: **$ARGUMENTS**

The split (two artifacts, opposite lifecycles):
- **`docs/changelog.md`** — durable, append-only audit: *what was done & why*.
- **`docs/WIP.md`** — sticky, overwritten each time: *where things stand now & next steps*.

The deterministic file writes go through `session-log`; you only supply the prose. Do **not**
hand-edit `docs/changelog.md` or `docs/WIP.md` directly — always go through the tool so format,
timestamps, and archive-rolling stay consistent.

## Steps

1. **Review what changed this session.** Skim `git status` / `git diff --stat` and recall the
   decisions made. Keep it factual — describe what was actually done, not what was planned.

2. **Append a changelog entry** — one concise entry covering the session's net change:
   ```
   session-log changelog "<what changed and why, 1–3 sentences>" --tag <feat|fix|docs|refactor|chore>
   ```
   If the work spans clearly distinct units, you may run it more than once (one entry per unit).
   Lead with the user-visible/behavioral change; include the *why* when it isn't obvious.

   **Evidence triple — state all three, or say explicitly why one doesn't apply:**
   - **Exact command and exact result.** `harness-verify: 0 errors, 1 warning`, not "tests pass".
   - **Runtime boundary crossed, or explicit N/A.** Which contract actually crossed the real
     boundary (tmux, network, filesystem, another process), and its result — or `N/A: no runtime
     boundary` and why. A fully-mocked suite is a statement about the mocks, not the system.
   - **Rollback boundary.** The exact files to remove or revert to undo this.

   This is a **checklist, not a gate** — you can write "passed" without having run anything, so it
   proves nothing on its own. `harness-verify` and `bin/mut` remain the only real verifiers. Its
   value is forcing the runtime-boundary question to be answered out loud. Never record a check
   you did not run.

3. **Refresh the WIP handoff** — overwrite `docs/WIP.md` with the current forward state. Pipe the
   body on stdin using this template (omit sections that are empty):
   ```
   session-log wip set <<'EOF'
   ## Current State
   <task / phase / overall progress in 1–2 lines>

   ## What We Did
   <2–3 sentence overview of this session>

   ## Decisions
   - <decision> — <rationale>

   ## Open Questions
   - <unresolved issue needing attention>

   ## Next Steps
   - <clear, actionable item to resume with>

   ## Files to Review on Resume
   - <path> — <why it matters>
   EOF
   ```
   WIP is the *current* state, not a log — overwrite it fully each time; don't append.

4. **Confirm clean.** Run `session-log check` — it should print nothing (no stale work). If it
   still reports changed files, you missed something above; capture it, then re-check.

5. **Report** a one-line summary of the changelog entry written and the WIP next-steps, so the
   user sees what was persisted.

Notes:
- If the project has no `docs/` yet, the tool creates it — that's expected on first wrap-up.
- The changelog auto-rolls older entries into `docs/changelog-archive/` once it grows large; you
  never need to prune it by hand.
