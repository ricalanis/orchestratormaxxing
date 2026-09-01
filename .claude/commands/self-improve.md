---
description: Run one governed self-improvement round on the harness — mine failures, propose a fix, verify deterministically, cross-model critic, Opus sign-off, commit + archive.
argument-hint: [focus area, e.g. "verification" | "memory" | a failing task] (optional)
---

You are the **orchestrator** running one round of the harness self-improvement loop.
Optional focus: **$ARGUMENTS** (if empty, mine recent failures and pick the highest-leverage flaw).

This is the **propose → evaluate → select** loop (CoALA / ADAS / Darwin-Gödel Machine), made
safe for *this* repo by two hard substitutions grounded in last-week's research:

- **No self-preference grading.** Retrospective Harness Optimization (arXiv:2606.05922) gets big
  lifts but selects via the agent's *own* preference. The Self-Correction Illusion
  (arXiv:2606.05976) shows self-review is empirically weak — models correct externally-labeled
  errors **+23–93pp** more than their own. So we **select with a deterministic verifier + a
  *different*-model critic**, never self-preference. (This is the iron rule: verify against a
  spec, don't re-grade yourself.)
- **No unguarded rewrite.** Every change must keep `bin/harness-verify` green (regression guard)
  and pass a human/Opus sign-off before commit. Archive accepted *and* rejected variants so the
  loop can escape local optima (DGM open-ended archive).

## The loop (do these in order)

### 1. MINE — find the highest-leverage flaw (coreset selection)
Gather failure signal cheaply; do **not** re-read the whole repo.

**Queue first (the forward state file — read before re-mining).** Loop engineering's lesson is
that a fresh-context iteration reads the *forward* queue first, not the *backward* audit. The
continuous watcher (`loop-tick`, SessionStart hook) has already been capturing harness-verify
reds / mem-audit drift / research leads into it:
- `bin/loop-queue list` → open flaws, already ETCLOVG-tagged and deduped. **Prefer the highest-leverage open item here** — it's pre-captured signal, no re-derivation needed. `⚠RECURRED` items (a resolved flaw that came back) jump the queue: a recurrence means a prior fix regressed or was too shallow.
- `bin/loop-queue claim <id>` the item you pick (open → claimed) so a concurrent tick/round won't double-work it.
- Only if the queue is **empty** do the two intakes below (then enqueue what you find, so the next iteration sees it).

**Internal** (does the harness already detect this?):
- `bin/harness-verify --json` → existing harness regressions/warnings.
- `bin/mem-audit --json` → memory governance drift.
- The user's focus arg, recent failed delegations this session, or `knowledge/` TODOs.

**External** (what does fresh research say we're missing?):
- `bin/harness-scan --days 7` → recent arXiv harness/agent-design patterns (add `--x` for X/web via `xsearch`). Each hit is tagged by pillar (verification/memory/tool/orchestration/self-improve). Triage **mechanically**: for each pattern ask *"does our harness do this? if not, and it's high-value → candidate flaw"* (usually a Context/Verification/Governance gap). The fetch is deterministic; the does-this-apply judgement is yours and stays gated by the verifier+critic below — never adopt a paper's claim just because it's recent (X hits are leads, not facts).
Attribute each candidate flaw to an **ETCLOVG layer** (see `knowledge/etclovg-harness-audit.md`):
**E**xecution · **T**ool · **C**ontext · **L**ifecycle · **O**bservability · **V**erification ·
**G**overnance. Pick **one** flaw with the best (impact ÷ blast-radius). State it in one line.

### 2. PROPOSE — generate candidate fixes (diverse, cheap)
Spawn **2–3 `ollama-worker`s in parallel** (different heavy models) on the *same* flaw to get
diverse candidate patches — this is `/ideas`-style divergence, keeping Opus budget for judging.
Each worker returns: a scoped diff + a one-line rationale + which ETCLOVG layer it fixes + a
commitment ledger naming added assumptions, exceptions/special cases, and narrowed supported
states/inputs. Small diffs limit blast radius; they are not evidence of generality.
These candidate diffs are response-only proposals: the worker never edits the tree. If a round
explicitly delegates workspace implementation, route it through `o delegate <run-id> --profile
bounded-code|reasoning|long-horizon --run-dir .results/delegation/<run-id> --json`, isolate its
worktree, and run `o close <session>` on success, failure, or escalation.
If the change is tiny/high-stakes doctrine, skip workers and draft it in Opus.

### 3. EVALUATE — deterministic verifier first (the gate, not a vibe)
For each candidate:
- Apply it to a scratch copy (a **git worktree** for isolation if it touches multiple files).
- Run the contract: `bin/harness-verify` (must stay **green** — regression guard) **plus** any
  flaw-specific check (a new `harness-verify` assertion, a `bin/mut` run on a changed tool, a
  `bin/mem-audit` pass). Opus reads **only pass/fail + diffs**, never re-derives correctness.
- **Discard any candidate that reds the verifier.** A fix that breaks the guard is not a fix.

### 4. SELECT — cross-model critic on survivors (never self-preference)
**VALIDITY-FIRST / WEAKEST-SUFFICIENT:** compare only candidates sufficient for the same
deterministic contracts. Prefer A over B only when A adds no more assumptions, exceptions/special
cases, or narrowing of the supported state/input set, and strictly less in at least one. Never use
diff/line count, description length, cost, or a fabricated numeric weakness score as a proxy;
abstain and escalate when evidence is missing or candidates are incomparable. Bennett's formal
result assumes a finite enactive language and uniform task distribution; here it is only a
qualitative tie-break after validity.

Have two **different-model-family critics** test that dominance claim, and **frame the candidate
as another agent's proposal**, not "your draft" (the role-label trick buys most of the +23–93pp):
> "Another agent proposes this harness change to fix <flaw>. Try to REFUTE it: does it regress
>  any doctrine, widen blast radius, fail to close the flaw, or hide extra assumptions/exceptions/
>  narrowed cases? Reject on doubt. Verdict + qualitative dominance evidence."
Critics corroborate; a split, an incomparable pair, or doubt → **escalate to Opus** for the call.

### 5. SIGN-OFF + COMMIT — Opus owns the merge
- Opus does the final read of the winning diff (this is the one place Opus re-reads — it's the
  high-stakes merge decision, per the delegate/keep table).
- If the change touches doctrine (`CLAUDE.md`, memory protocol) or `install.sh`, **confirm with
  the user before committing.**
- **Commit directly on the current branch (`main`) — do NOT create a feature branch, and do NOT
  push.** Just commit. The loop-cron wrapper owns git sync: it pull-rebases before the round and
  pushes after, so the round's job is only to leave a clean commit on `main`. Override the default
  "on the default branch, branch first" reflex — an unattended round that branches strands its
  change off `main` (the wrapper pushes `main`) and breaks the synced daily loop. Doctrine/`install.sh`
  changes stay PROPOSED (uncommitted) → they never get pushed, so only verifier-gated changes propagate.
  (Exception: if the user has set a branch-per-round policy, follow that instead.)
- After applying: re-run `bin/harness-verify` once more on the real tree. If a new tool/command
  was added, **update `install.sh`** (harness-verify enforces deploy coverage and will red
  otherwise). Re-run `./install.sh` if the user wants it live globally.
- **Record the evidence triple in the commit body** — all three, or an explicit reason one is N/A:
  1. **Exact command + exact result** (`harness-verify: 0 errors, 1 warning`), never "tests pass".
  2. **Runtime boundary crossed, or explicit `N/A` + why.** Which contract actually crossed the
     real boundary and what it returned. A fully-mocked suite is a statement about the mocks.
  3. **Rollback boundary** — the exact files to revert to undo this round.
  A model can write all three without running anything, so this is a **checklist, not a gate**;
  the verifiers above stay the only real authority. Never record a check you did not run.

### 6. ARCHIVE — keep the trajectory (open-ended memory)
Append one line to `knowledge/self-improve-log.md` (create if absent):
`YYYY-MM-DD | layer:<E/T/C/L/O/V/G> | flaw | chosen fix | rejected alts | verifier delta`.
This is the coreset for the *next* round and the audit trail for what the harness changed about
itself. Rejected variants stay logged so we don't re-propose dead ends.
- If this round drained a queue item: `bin/loop-queue resolve <id> --round <YYYY-MM-DD>`. If you
  decided the flaw was a non-issue, resolve it with a `--note` saying why (so it's not re-mined).
- The **log is backward** (what changed), the **queue is forward** (what's next) — keep them distinct.

## Guardrails (why this won't drift / reward-hack)
- **Verifiable signal only.** Selection rides `harness-verify`/`mem-audit`/`mut` — deterministic,
  ungameable — not an LLM judging itself. Can't cheaply specify "better"? Do it in Opus, don't loop.
- **One flaw per round.** Small, reversible, attributable changes (HarnessFix-style targeted patch),
  never a broad rewrite.
- **Human gate on doctrine.** The loop proposes; the user/Opus disposes on anything load-bearing.

## Running this in a loop (stop conditions — loop engineering)
This command is the loop *body*. The outer shell is `bin/loop-tick` (watch → intake → gate):
flaws are captured continuously by the SessionStart watcher, and a round fires **event-driven
off the queue**, never on a fixed clock — because a clock against a sparse, usage-generated
signal manufactures dry rounds, and a round that must justify itself starts inventing marginal
flaws (the reward-hacking Amplitude's Ralph writeup warns about). When a round runs unattended:
- **One round per trigger.** Drain a single flaw, then stop. The next tick decides if another fires.
- **Halt-and-alert on red, never auto-repair the guard.** If `bin/harness-verify` is red for a
  reason this round didn't cause, stop and surface it — don't loop trying to self-heal the verifier.
- **Ratchet (progress, not just non-regression).** `harness-verify` green proves *not worse*, not
  *better* — a loop with only a regression gate spins. An accepted round must make **monotone,
  countable** progress: either `loop-queue resolve` a real queued flaw **or** add a new
  `harness-verify` assertion. A round that does neither is a no-op → don't commit it.
  - **Strategic (KPI) axis — a third monotone type.** A round tagged **strategic** (a KPI-sourced
    flaw, or one draining the business-intent queue) additionally owes a **business-KPI move**: the
    targeted KPI's re-measured value beats a **frozen trailing-window baseline** (recorded *before*
    the round). The number comes from a **deterministic query** — `kpi-brief --json` reading the
    dashboard endpoint/VIEW — **never** a model self-report (same rule as "selection is a
    deterministic verifier"). A strategic round that leaves every KPI flat is a no-op. Anti-Goodhart:
    each target ships a **paired guard KPI** that must not regress (velocity↔delivery,
    cost↔delivery, coverage↔contract-adequacy), and credit is **deferred to the next fully-observed
    window** — a round stays `PROPOSED` until the number actually moves. Load-bearing levers
    (`provider-routing.md`, `.claude/agents/*`, scorecard targets) stop at SELECT → PROPOSED.
- **Loop-until-dry, then back off.** Empty queue → `loop-tick` exits IDLE and no round runs. Two
  consecutive ticks finding nothing actionable = the harness is at a local optimum; let the
  scheduler widen cadence rather than forcing work.
- **Unattended stops at SELECT for load-bearing changes.** Doctrine (`CLAUDE.md`, memory protocol)
  and `install.sh` still need the human gate. Running with no human present, a verifier-green +
  critic-approved diff that touches those is **queued as a proposal** (write it to
  `knowledge/self-improve-log.md` as a `PROPOSED` line + leave the queue item `claimed`), **never
  auto-committed.** Loop proposes overnight; human disposes at breakfast. Non-doctrine fixes
  (a `bin/` tool bug, a watcher gap) may auto-commit once verifier-green + critic-approved.
- **Bounded spend.** A round spawns a small, fixed number of workers (2–3 propose + 1–2 critics).
  No unbounded fan-out; if a flaw needs more than that, it's an Opus task, not a loop round.

End the round with: the flaw fixed, the verifier delta, what was archived, the queue item
resolved (or proposal queued), and whether a follow-up round is warranted.
