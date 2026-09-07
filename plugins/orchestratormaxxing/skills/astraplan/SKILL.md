---
name: astraplan
description: Plan demanding work with a read-only GPT-6 Astra planner, route substantial bounded implementation to Sol, and use cheap-delegate for routine chunks. Use for Astra planning requests or work needing this planning and execution split.
---

Astra designs; Sol executes demanding bounded chunks; cheap-delegate routes routine
work. The primary session owns the contract, architecture decisions, verification,
and final sign-off. This is an alternative to solplan, not a global model remap.

1. **Prepare the plan.** Persist a self-contained `brief.md` and Root-authored
   `contract.md` read-only in `.results/delegation/<run-id>/`. Include objective,
   evidence, exact paths, unresolved choices, constraints, and acceptance criteria.
   Keep existing user authorization; ask only for a missing material decision.

2. **Dispatch read-only Astra.** Prefer the native `astra-planner` agent. If the
   named agent is unavailable but native model selection exists, use a fresh
   agent pinned to `gpt-6-astra`, effort `ultra`, with the same read-only guard.
   Otherwise use the shared lifecycle runner through this skill's wrapper:

   ```bash
   python3 <skill-dir>/scripts/run_astraplan.py --workdir "$PWD" < <run-dir>/brief.md
   ```

   Capture its output in the run directory. The wrapper reuses solplan's bounded
   streams, cancellation cleanup, read-only sandbox, ignored user configuration,
   and final-plan validation. It requires the sibling `solplan` skill shipped in
   this plugin. Do not substitute another model silently or add a wall-clock
   deadline to a healthy planner. Relay liveness while waiting.

   Child guard: design only; do not invoke astraplan/solplan or another planning
   subprocess. At most three direct read-only exploration agents, no recursive
   delegation; at most eight read-only calls in the planner’s own thread (not the primary
   session’s later verification). Return under 1,200 words
   in `SUMMARY`, numbered `STEPS` with exact paths, `CONTRACT`, `EXECUTION SHAPE`,
   `RISKS / ASSUMPTIONS`, `OUT OF SCOPE`.

3. **Root reviews and assigns ownership.** Check assumptions against evidence.
   Each step names its dependencies, owned files, acceptance gate, and one lane:

   | Lane | Select for | Execution |
   |---|---|---|
   | ROOT | Architecture, security policy, credentials, doctrine/installer changes, final merge, or work without a cheap correctness contract | Primary session |
   | SOL | Demanding bounded implementation: coupled logic within a clear ownership boundary, difficult debugging or refactoring with deterministic acceptance | Fresh native worker pinned to `gpt-5.6-sol`, effort `high`; raise effort only when warranted |
   | CHEAP | Routine implementation, transforms, research digests, drafts, or straightforward tests | Load sibling `$orchestratormaxxing:cheap-delegate`; resolve the actual model and use its governed lane |

   `EXECUTION SHAPE` chooses `ROOT-DIRECT` for sequential orchestration or `FANOUT`
   only for at least two independent chunks with disjoint files and separate
   contracts. ROOT-DIRECT can include sequential Sol/cheap assignments; it does
   not mean Root rewrites every implementation. Do not force a Sol call for a
   task that fits the cheap lane, or demote a demanding chunk solely to save quota.

4. **Execute the reviewed plan.** Before each worker, persist a separate immutable
   brief and contract. Tell it that it is not alone and must preserve other edits.
   Sol implementers are execution workers, never the read-only `sol-planner`.
   If native Sol execution is unavailable, use the existing supported Codex
   execution surface with explicit Sol selection and bounded write scope; report
   unavailability rather than bypassing a read-only sandbox. Routine workspace
   work uses `o delegate`, not response-only `oll`. Follow cheap-delegate for
   event-bound retrieval, at most two same-session repairs, receipts and `o close`.
   Keep dependent assignments sequential; use fanout only after the split is ratified.

5. **Verify and finish.** Root runs the prewritten contracts and reads failures,
   not worker self-certification. Apply the project's mutation gate for costly
   false greens and repaired code. Capture a hash-bound receipt per attempt and
   report the actual model, verdict, and remaining limitations. Persist the plan
   with `plan-to-repo` when it becomes a durable plan of record, and use `wrap-up`
   for completed implementation and forward state. A plan-only request ends after
   the reviewed plan; this skill never authorizes execution or publication by itself.
