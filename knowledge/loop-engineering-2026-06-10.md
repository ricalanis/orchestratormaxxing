# Loop engineering — the June 2026 turmoil + adaptation plan for this harness

*Researched 2026-06-10 via `xsearch` (xAI Agent Tools, grok-4.3; X + web, 21–90 day windows). X hits are leads, not facts — primary essays verified by cross-citation.*

## 1. What happened (the turmoil)

The term **"loop engineering"** ignited in the week of **June 7–9, 2026**:

- **Jun 7** — Addy Osmani publishes the essay **"Loop Engineering"** (addyosmani.com/blog/loop-engineering), naming the layer *above* harness engineering: you stop prompting agents and instead design the recurring system (trigger → agent run → verification → state update → repeat) that prompts them for you.
- **Jun 7** — Peter Steinberger (@steipete): *"you shouldn't be prompting coding agents anymore. You should be designing loops that prompt your agents."* (~19k likes; the spark). https://x.com/steipete/status/2063697162748260627
- **Jun 8–9** — the backlash/debate peaks. Camps:
  - **Proponents**: paradigm shift — human designs the runtime; the LLM becomes a subroutine inside external verification + memory + parallelism (echoes Boris Cherny's earlier "write loops, not prompts").
  - **Skeptics**: rebranded cron + agent iteration ("1975 tech"); doesn't solve memory/context retention or **comprehension debt**; "sounds like labs want you to burn more tokens" (@sunnygg, https://x.com/sunnygg/status/2064481821069553978); cost blowup anecdotes.
  - **Middle**: "terrible name, important idea" (@kaibuildsai, https://x.com/kaibuildsai/status/2064496542791422286).

Pre-history that the discourse rediscovered: Geoffrey Huntley's **Ralph Wiggum loop** (late 2025) — `while :; do cat PROMPT.md | claude-code; done` with state in the repo, fresh context per iteration.

## 2. The good reads (ranked)

**Core:**
1. **Addy Osmani — "Loop Engineering"** — https://addyosmani.com/blog/loop-engineering/ (Jun 7, 2026). The naming essay: loop = trigger + skill/instructions + persistent state file + verification gate + sub-agent spawning; sits "one floor above the harness" in his factory model. Caveats: token cost, human review is still the scarce resource.
2. **Geoffrey Huntley — "everything is a ralph loop"** — https://ghuntley.com/loop/ (Jan 2026). The monolithic-orchestrator counterpoint: one repo, one goal, one task per iteration, context engineering over multi-agent complexity. Also https://github.com/ghuntley/how-to-ralph-wiggum.
3. **Anthropic Engineering — "Effective harnesses for long-running agents"** — https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents (Nov 2025) and **"Harness design for long-running application development"** — https://www.anthropic.com/engineering/harness-design-long-running-apps (Mar 2026). Initializer+coder two-agent harness, progress logs, git commits as state, context resets — the engineering substance under the buzzword.
4. **Amplitude — "The Ralph loop"** — https://amplitude.com/blog/ralph-loop. Best practitioner failure-mode writeup: the loop is trivial; the dispatcher, **honest outcome signals**, and self-instrumentation are the hard part; without honest signals loops just spin.

**Critiques / failure modes:**
5. **Addy Osmani — "Comprehension debt: the hidden cost of AI-generated code"** — https://www.oreilly.com/radar/comprehension-debt-the-hidden-cost-of-ai-generated-code/. Loops amplify the gap between codebase growth and human mental models.
6. **Augment Code — token cost & context constraints in agent loops** — https://www.augmentcode.com/guides/ai-agent-loop-token-cost-context-constraints. Naive loops grow O(N²) in cost; fresh sessions + state-in-filesystem are mandatory.
7. **Martin Fowler — "Humans and Agents in SE loops"** — https://martinfowler.com/articles/exploring-gen-ai/humans-and-agents.html (Mar 2026). Where the human sits as loops eat implementation+verification.

**Foundational:** ReAct (arXiv:2210.03629) — the inner agentic loop everything else wraps.

**Distilled consensus anatomy of a good loop** (Osmani + steipete + Huntley + Amplitude):
1. **Trigger** (cron/schedule/event) — not a human.
2. **Skill/instruction file** — the loop body's standing orders.
3. **Persistent state file** (STATE.md / task list / git) — fresh context per iteration; the repo remembers, not the conversation.
4. **Verification gate** — deterministic (tests/build/lint), *honest* signals or the loop reward-hacks/spins.
5. **Stop conditions / circuit breakers** — max iterations, budget cap, stuck-detection, red-gate halt.
6. **Bounded human gate** — loop proposes; human reviews at a batch point, not per-iteration.

## 3. Adaptation to this harness (gap analysis)

**Where we're already ahead** (the turmoil's main critiques are things our doctrine solved first):
- **Honest signals / anti-reward-hacking**: selection by deterministic verifier (`harness-verify`, `mem-audit`, `bin/mut`), never self-preference — exactly Amplitude's "honest outcome signals" lesson.
- **Token cost**: the #1 skeptic complaint. Our orchestrator pattern (loop drives cheap Ollama workers; Opus reads only pass/fail) is the structural answer — a loop here burns worker tokens, not frontier tokens.
- **Micro-loops exist**: bounded 2-round repair with shrinking-diff early-exit is a loop-with-circuit-breaker at chunk level.
- **State substrate**: bitemporal governed memory + `knowledge/self-improve-log.md`.

**Gaps, by ETCLOVG layer** (what loop engineering names that we lack):
- **Lifecycle (the big one): no trigger layer.** `/self-improve` is a complete loop *body* (mine → propose → verify → select → archive) but is human-invoked, one round at a time. We built the loop body and never closed the loop. Fix: schedule it (Claude Code `/schedule` cloud routine or cron) — e.g. nightly `harness-scan` intake + one `/self-improve` round.
- **Context: archive ≠ state file.** `self-improve-log.md` is backward-looking audit; Ralph needs a forward-looking queue ("next candidate flaws / blocked / in-flight") that a fresh-context iteration reads first. Fix: add a `## Queue` section to the log (or `LOOP-STATE.md`) that MINE reads before re-mining.
- **Governance: loop-level stop conditions.** We have chunk-level bounds but no round-level ones. Fix: unattended loop = **1 round per trigger**, halt-and-alert on any `harness-verify` red, loop-until-dry (2 consecutive no-flaw rounds → back off cadence), hard worker-call budget per round.
- **Governance: the human gate vs. unattended runs.** Doctrine requires user confirm on doctrine/`install.sh` changes — correct, and it means the unattended loop must **stop at SELECT** for those: queue the verifier-green, critic-approved diff as a *proposal* for morning sign-off, never auto-commit doctrine. "Loop proposes overnight; human disposes at breakfast."
- **Verification: regression guard ≠ progress signal.** `harness-verify` green proves *not worse*, not *better* — a loop with only a regression gate spins. Fix: ratchet rule — an accepted round must either close a queued flaw or add a new `harness-verify` assertion (monotone, countable progress).
- **Observability**: log every loop *run* (including no-op rounds) with trigger time, rounds, budget spent — the loop equivalent of the self-improve log line.

**Minimum viable loops to build (in order, per steipete's "start manual, then automate"):**
1. **Research-intake loop** (low stakes, first): scheduled `bin/harness-scan --days 2 --x` → digest to `knowledge/` → flag pillar gaps into the self-improve queue. Read-only, no commit risk.
2. **Governed self-improve loop**: scheduled one-round `/self-improve` with the stop conditions + proposal-queue above. The existing command already contains the gate, critic, and archive — it only needs the trigger, the queue, and the ratchet.
3. **Not** a code-writing overnight Ralph loop on this repo — the harness *is* the doctrine; comprehension debt on your own governance layer is the worst place to take it.

**Doctrinal fit check**: a runtime-triggered, task-decomposed, verifier-selected loop is exactly the class arXiv:2606.05670 says *wins* (vs. static multi-agent); selection stays deterministic per our iron rule; the critic stays cross-model per 2606.05976. Loop engineering, as practiced well, is our existing doctrine plus a Lifecycle layer.

## 4. 2026-08-14 refinement — topology before autonomy, judgment after gates

Addy Osmani's “Practical Loop Engineering” sharpens three operator rules without
changing the event-driven architecture built since this June snapshot:

1. **Pick the smallest sufficient topology.** Manual agentic work owns unclear,
   subjective, sensitive, and load-bearing tasks; a bounded goal owns one
   objectively checkable item; an observer only detects/enqueues external work;
   a proactive routine owns only predefined, repeat-safe AUTO streams.
2. **Separate observer from executor.** Observation can run on a cadence, but
   each signal starts one bounded execution with its own four brakes. Empty
   observation must never manufacture a round merely because a timer fired.
3. **Make spin visible without model judgment.** An optional bounded
   `action_results` trace fails `progress.not-spinning` when the final three
   exact action/result pairs are identical. Numeric progress cannot override
   that physical signal; semantic similarity remains intentionally out of scope.

Passing hard completion rules still means eligible, not good. Deterministic
contracts accept checkable behavior; root/human retains taste, sensitive and
SELECT judgment, and a builder never accepts its own artifact.
