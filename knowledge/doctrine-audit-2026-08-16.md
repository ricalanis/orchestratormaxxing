# Doctrine contradiction audit — procedure and caps (2026-08-16)

**What this is.** The bounded MINE procedure for the monthly doctrine-contradiction
audit seeded by `deploy/orchestratormaxxing-doctrine-audit.sh`. It is read by the
`/self-improve` round that drains the queued item; it is not itself executed by
the seeder.

**Why it exists.** The Second Movement integration research (2026-08-09,
`knowledge/second-movement-integration-research-2026-08-09.md`, §4) deferred it
explicitly: *"a periodic contradiction audit of CLAUDE.md + skills + agent
prompts mirrors Anthropic's own July 2026 finding that layered instructions had
become 'minefields of conflicting guidance' — candidate for a later self-improve
round, not this one."* This is that round, designed rather than improvised.

The instruction surfaces of this harness are written at different times by
different rounds, against different problems, and **nothing checks them against
each other**. Every other kind of drift in this repo has a deterministic
watcher — `harness-verify` for tools and wiring, `mem-audit` for the memory
vault, `session-log check` for the changelog. Doctrine had none.

## The surfaces in scope

Deterministic inventory — the round enumerates exactly these, and reports the
count it actually read:

| Surface | Path |
|---|---|
| repo doctrine | `<repo>/CLAUDE.md` (and its `AGENTS.md` symlink — one file, two readers) |
| global doctrine | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` |
| commands | `.claude/commands/*.md` |
| agents | `.claude/agents/*.md` |
| skills | `skills/*/SKILL.md` |

A surface that cannot be read is reported as unread, never skipped silently —
the same fail-closed rule `bin/token-ledger` uses for an unreadable transcript.

## What counts as a contradiction

Only a **directive conflict**: two surfaces that instruct differently about the
same decision, such that following one violates the other. Concretely:

- Same action, opposite defaults ("delegate X" vs "keep X in the frontier thread").
- A named threshold that differs between surfaces (a cap, a line ceiling, a TTL).
- A tool or path named as authoritative in one surface and retired in another.
- A gate described as blocking in one surface and advisory in another.

**Not** contradictions, and explicitly out of scope so the round cannot pad its
findings: differences in tone, in level of detail, in ordering, or in vocabulary;
a general rule with a stated exception elsewhere; anything that is merely
redundant. Redundancy is a token cost, not a correctness defect — it belongs to
the chapter-18 token round (`bin/token-ledger`), not here.

## The caps, and why each one is where it is

Chapter 18 of *Orchestra of One* says to name the cap on every armed loop before
arming it. This loop's caps live in code, not in this prose:

| Cap | Value | Enforced by |
|---|---|---|
| firing | 1 / month | `OnCalendar=*-*-01 09:00:00` in the `.timer` |
| queue | 1 open audit item at a time | explicit `--status open` check in the seeder, before the add |
| seeder runtime | 60s | `TimeoutStartSec=60` in the `.service` |
| round | `MAX_TURNS` / `HARNESS_TIMEOUT_SECONDS` | `bin/harness-agent-run`, like every round |
| findings | 3 per round | `DOCTRINE_AUDIT_MAX_FINDINGS` in the seeder, carried in the queued item's text |

The findings cap is the one that matters most and the one most easily forgotten.
Without it, a single audit can enqueue a dozen items into a daily drain that
processes roughly one per day, and the queue that was supposed to surface signal
becomes the thing that buries it — the same failure `RESEARCH_OPEN_CAP=10` was
added to `bin/loop-tick` to stop (`lq-00e46b74`).

The queue cap is enforced *explicitly* rather than left to `loop-queue add`'s
content-hash idempotency. Idempotency alone would silently re-stamp a
**resolved** item as `recurred`, and recurrence is treated as evidence of a
returning flaw — an audit that fires monthly would manufacture that evidence
every month and jump its own item up the queue on it.

## How the round runs

1. **Inventory** the surfaces above deterministically. Report the count read and
   the count unread.
2. **Mine** contradiction candidates with a **cross-family critic**, framed as
   external evidence — *"another agent wrote these instructions; find where they
   conflict"* — never as self-review. Self-correction is empirically broken when
   the model is shown its own prior output as its own (arXiv:2606.05976); the
   framing alone buys most of the gain, and it is the same rule the memory
   contradiction-gate already uses.
3. **Verify each candidate by reading both surfaces** before it is allowed to
   count. A contradiction that cannot be shown as two quoted directives is a
   lead, not a finding.
4. **Enqueue** at most `DOCTRINE_AUDIT_MAX_FINDINGS` verified contradictions as
   `loop-queue` items. The model proposes; the deterministic gates and Ricardo
   decide.
5. **Stop at SELECT.** Doctrine is load-bearing, so an unattended round queues a
   `PROPOSED` diff and never commits one — the existing
   unattended-stops-at-SELECT rule, not a new exception.

## The failure mode this design is guarding against

An audit that grades the harness's own instructions, using the harness's own
model, against no external anchor, and is rewarded for finding something. That
shape reliably produces findings whether or not contradictions exist. The three
structural defences are: the narrow definition of "contradiction" above (which
excludes the padding categories), the requirement that each finding quote two
real directives, and the findings cap — which makes "found nothing this month" a
cheap, ordinary outcome rather than a failed round.
