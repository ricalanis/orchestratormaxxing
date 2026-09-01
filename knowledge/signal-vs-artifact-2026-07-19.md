# Signal vs. artifact: when the measurement is the thing that's broken

Three times in two days, something in this harness looked like evidence of a fault and was
actually an artifact of how we were looking. Once it cost a production incident; the last time
it cost a single tool call. The difference was entirely whether the signal got corroborated
before anyone acted on it.

This note exists because the failure mode is *not* obvious in the moment. Each of these read as
a clear, specific, actionable fault. None of them were.

## The rule

**Before acting on a fault signal, establish that the signal is about the system and not about
the apparatus observing it.**

Four questions, in order of how often they pay:

1. **Could the measurement itself produce this exact reading?** An empty capture, a matched
   process, a failing parse — each has a boring explanation rooted in the tool, not the target.
2. **Is "it failed" distinguishable from "I could not measure it"?** If one code path produces
   both, that path is a bug regardless of what else is true.
3. **Is there an independent corroborating source?** One signal is a hypothesis. Two
   independent signals are evidence.
4. **Is the action I'm about to take terminal?** If yes, require *positive* evidence. Refusing
   to act on ambiguity is cheap; an unwarranted terminal state is not.

## Case 1 — an unreadable pane became a dead executor (cost: a production incident)

`orch-monitor` captured the executor pane with `-t "=name"` — a *session* target handed to a
pane-scoped command. tmux answered `can't find pane` on stderr **and exited 0**, so the return
code showed success. Every capture was wrapped `2>/dev/null || echo ""`, so the failed read
degraded to an empty string. `classify()` mapped empty → `dead`, and the monitor wrote a
terminal `escalated` state over a Codex that was visibly working.

Three separate layers had to conspire, and each was individually defensible:
- a target that looked right (`=name` is correct for `has-session`, wrong for pane commands)
- an exit code that lied (tmux reports this failure without a non-zero status)
- a fallback that manufactured a plausible value (`|| echo ""`) instead of a null

**Then it got worse.** The contract asserted `C4 empty pane → dead → escalated`, promoting the
accident to a requirement. Fixing the bug required *deleting an assertion* — the wrong reading
had been frozen into the spec.

Fix: `capture()` returns `None`, never `''`; the caller refuses to transition and exits 3.
Doctrine: **absence of evidence is never evidence of death.**

## Case 2 — a concurrent mutation run became two harness regressions (cost: one tool call)

Running `harness-verify` while `bin/mut` was working produced two REDs against
`bin/orch-dispatch`: `OSError: [Errno 8] Exec format error` and a bash syntax error at line 13.
Both looked exactly like a broken tool. Both vanished when mut finished.

Cause: `mut` mutates the source file **in place**, so any concurrent reader sees a mutant.

The subtlety worth remembering: **the same mechanism produces both false alarms and real
corruption.** An *interrupted* mut had previously left `bin/orch-monitor` genuinely broken — it
lost its shebang and took an `or` → `and` flip in `classify()` — and that corruption survived
into a later session, where it presented identically to this false alarm. So "mut was running"
does not automatically mean "ignore it"; it means *check the file*, then decide. Queued as
`lq-cd123ac1`.

## Case 3 — pgrep matched itself (cost: one tool call)

`pgrep -af "bin/mut"` reported a running process. The match was the wrapper shell executing
that very pgrep, whose command line contained the pattern string. Corroborated in one call:
`git diff --stat bin/` was empty and the file parsed, so nothing was mutated on disk.

Generalizes to any self-inspecting check — `ps | grep`, log scans that match the scanner's own
output, a health check that records its own probe.

## Case 4 — the rule cuts both ways (cost: ~6 tool calls, and worth every one)

Minutes after writing the three cases above, `harness-verify` went red:
`tests/context-stateless/run.sh timed out after 30 seconds`. Pattern-matching on the fresh
lesson, the obvious read was *artifact* — a contract that normally takes ~1.5s, unrelated to
the doc-only change being committed, green on two immediate re-runs. Filed as a flake.

Then the identical red appeared on the **Mac**, on a different OS, minutes later.

Two independent machines is corroboration. That is not a local hiccup — it is a real
intermittent fragility, whatever its cause. Chasing it further:

| Test | Result |
|---|---|
| Standalone, Linux | green, 1.5s |
| Standalone, Mac ×2 | green, 2s / 1s |
| `harness-verify` alone, Mac ×2 | green |
| `install.sh` → `harness-verify`, Mac | green — **hypothesis falsified** |

Both initial reds occurred inside `harness-verify` shortly after `install.sh`, which looked like a
sharp correlation until testing that exact sequence came back green. It then reproduced a
**third and fourth** time across the two machines, so it is real and recurring, not a one-off.

**Two hypotheses formed, both falsified by direct test:**

1. *"Correlated with `install.sh`."* Ran that exact sequence on the Mac — green.
2. *"An `oll` call reaches the network and blocks on the 300s `urlopen` timeout in `bin/oll:138`,
   10× the 30s contract gate."* This was compelling: the timeout is genuinely there, and the
   contract does exercise IDs that pass the policy gate. But the contract's own comment says
   those IDs "fail at the deliberately missing auth fixture **without reaching a network
   call**", and timing the exact invocation confirms it — **64ms**, exiting on a missing
   `auth.json`. The 300s bound is real but unreachable from this contract.

**Cause remains unestablished.** Both hypotheses are recorded *as falsified* rather than
deleted, because each was plausible enough to become folklore and send a later round down a
dead end. Queued as `lq-d0f750ab`, corrected by `lq-4b49e63c`.

Worth noticing: hypothesis 2 was assembled from three true facts (a 300s timeout exists, the
gate is 30s, the contract invokes `oll`) into a false conclusion. Reading the contract's own
comment — which stated the answer plainly — took one call and cost less than writing the
hypothesis did.

The lesson is the symmetry, and it is the easier half to get wrong: **"is this an artifact?"
is a question, not a conclusion.** A fresh rule about false alarms makes the next real signal
look like one. Corroboration is the actual discipline — it is what demoted cases 2 and 3, and
it is equally what *promoted* this one. Recording a falsified hypothesis is part of that;
"correlated with install.sh" would have become folklore if nobody had tested it.

This one also has teeth beyond the flake, because `bin/loop-tick` **enqueues harness-verify
reds automatically**: an intermittent red manufactures a phantom flaw that a scheduled
`/self-improve` round then acts on. That is the signal-vs-artifact failure mode with a robot
holding the other end — which is why the queued fix includes having a contract *timeout* count
as "inconclusive, re-run once" rather than as a red.

### Containment shipped; cause still unknown

The verifier now makes the distinction mechanically. Every behavioral contract has a stable ID
and runs in its own process group. A measured nonzero exit is `failed` and is never retried. A
timeout or observer error gets one targeted retry: timeout→pass is `recovered`; two observation
failures are `inconclusive`, not a target regression. The runner kills the whole process group
TERM→KILL, retains bounded PID/PPID/PGID/state plus stdout/stderr tails, and removes its scratch
directory. `tests/context-stateless/run.sh` emits `BEGIN`/`END` stage markers to a runner-provided
trace, so the next occurrence identifies the last active section without adding a network probe.

`bin/loop-tick` remains the only queue-policy owner. It writes recovered/inconclusive evidence to
the capped, gitignored `.results/harness-observations.jsonl`; a recovery enqueues nothing, while
two targeted failures enqueue one stable **Observability** flaw whose text excludes volatile
timing/stage data. Measured errors still need to survive the existing whole-verifier persistence
recheck. If `harness-verify` itself emits unreadable output twice, the queue item names the
*observer*, never a guessed target fault.

This fixes the automated response and supplies the missing evidence path. It does **not** fix or
name the intermittent cause. A later trace should create/reopen a causal repair item from the
captured stage; until then, "fixed the flake" would overstate the evidence.

## What actually made the difference

Not intelligence about the specific bug — the corroborating checks were trivial in all three
cases (`git diff`, wait for the process, read the file). What differed was **whether anything
was checked before acting**, and that tracked how terminal the pending action was.

Case 1's action was terminal (write `escalated` into the ledger) and got no check. Case 3's
action was a report and got one. That's exactly backwards from how it should be, and it is the
single most portable lesson here: **the more irreversible the response, the more the signal has
to earn it.**

## Related doctrine

- "Absence of evidence is never evidence of death" — the orch-exec invariant in `CLAUDE.md`.
- **Tier 1c** (`CLAUDE.md`) — a mocked contract needs a real-path counterpart; prove a new
  contract red; never let a test freeze an implementation accident. Case 1 is where all three
  of those rules came from.
- `tests/orch-transport/run.sh` — the contract that exists because mocked green proved nothing.

## Addendum 2026-07-26 — artifact cases from the gentle-ai removal

Six more artifact cases from failed observability during the foreign harness removal:

- **Missing tools swallowed by `2>/dev/null`:** eza/fd were not installed; their empty output read as "directory empty / nothing found". A missing instrument reads as a negative result, not an absence of the measurement.

- **Alias shadowing:** `command -v gga` matched a zsh git alias (git gui citool --amend) after the binary was uninstalled — read as "still installed". Check the resolved path, not the name lookup.

- **Pipeline exit codes:** checking `$?` after `cmd | tail` reads tail's status, not cmd's. Test's real rc needs `PIPESTATUS[0]`.

- **pgrep matching its own command line:** `pgrep -af "bin/mut"` reported a running process that was the wrapper shell executing that pgrep. Corroborate with `git diff --stat` or file inspection.

- **cwd case-string vs canonical path:** bash logical `$PWD` carries the case the session typed (dev/) while `os.getcwd()` returns kernel-canonical (Dev/) on case-insensitive FS. A string-equality contract failed only in sessions launched from the other spelling (lq-edd04bfe, recurred 3×, misdiagnosed once as cascade). Compare path identity (`os.path.samefile`), not strings.

- **Exported one-shot latches:** shell/warp-recovery.sh exports `_WARP_AGENT_RECOVERY_CLAIMED`, so a contract run from inside a real Warp session inherited it and code under test silently short-circuited (lq-8d3db194). A fixture is hermetic only if it strips host state it asserts on.
