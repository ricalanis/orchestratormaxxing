---
name: astraplan
description: Plan nontrivial unplanned work with GPT-6 Astra Ultra and bounded read-only exploration across Codex, Claude, OpenCode, Hermes, and Zed. Astra coordinates planning; the calling host reviews, executes, and verifies. Prioritize this before $orchestratormaxxing:fanout. Skip trivial or already-planned work; explicit solplan, fableplan, and kimiplan remain available.
---

Use Astra as the larger planning model and coordinator. Keep the calling host as executor and final decision-maker. This is the fleet default for nontrivial planning, including architecture and multi-file work.

1. Persist the host-authored brief and acceptance contract read-only under `.results/delegation/<run-id>/brief.md` and `contract.md` before dispatch. Supply goal, constraints, exact relevant paths, unknowns, and the child guard below. Do not pre-solve the design.
2. In Codex, prefer the native `astra-planner` agent when selectable: `gpt-6-astra`, `ultra`, read-only. Otherwise, in any host, resolve THIS installed skill directory and run its script with the brief on stdin:

   ```bash
   python3 <skill-dir>/scripts/run_astraplan.py --workdir "$PWD" < .results/delegation/<run-id>/brief.md > .results/delegation/<run-id>/output.md
   ```

   The paired `solplan` skill supplies the shared runner engine and must be installed beside `astraplan`. Astra is selected explicitly; a missing dependency, unavailable model, or failed run is an error, never permission to silently use another model. Codex authentication stays in the local Codex CLI; no API key or provider change is needed in the calling host.
3. Include this child guard: `You are the Astraplan child. Design only; never implement, invoke astraplan/solplan/fableplan/kimiplan, or start another planning subprocess. You may coordinate at most three direct read-only exploration subagents for independent questions. Do not delegate final synthesis or allow recursive delegation. Use at most 8 root-thread read-only tool calls and keep the plan under 1,200 words. Do not browse externally unless current sources are needed. Never run destructive git operations.`
4. Require exactly `SUMMARY`, numbered `STEPS` naming exact paths, `CONTRACT`, `EXECUTION SHAPE`, `RISKS / ASSUMPTIONS`, and `OUT OF SCOPE`. Put each heading alone on its own line, with its value on the following line. `EXECUTION SHAPE` chooses `ROOT-DIRECT` or `FANOUT`; FANOUT requires at least two independent chunks with disjoint ownership and separate contracts. Astra synthesizes the plan; it does not accept its own work.
5. Review the plan as external evidence, resolve material assumptions using the user's existing authorization, then implement and verify in the calling host. Route bounded execution through the installed `cheap-delegate` skill, with root-authored acceptance contracts and live model resolution. Use fanout only for independent approved chunks; writing workers require isolated worktrees. Bind the host's acceptance verdict to the output hash in `receipt.json`. Hermes may persist the reviewed plan with its native `plan-to-repo` workflow.

The runner is ephemeral, ignores unrelated user configuration, closes child stdin, bounds delegation to four total threads and depth one, validates output, and cleans up the process group. Codex Ultra enables automatic delegation; it is a Codex orchestration mode, not an API reasoning-effort value. There is no wall-clock deadline: yield/poll at least once per minute and relay meaningful progress. User cancellation ends the process group. Retry once only for a confirmed transient failure. The one-shot fallback cannot be steered mid-turn.

Host entry points: Codex `$orchestratormaxxing:astraplan`; Claude `/astraplan`; OpenCode `/astraplan` or its installed skill; Hermes uses its installed `astraplan` skill. Zed and Warp can read the repository copy of this skill and invoke its documented CLI runner; the installer points their instructions to that path, without installing a native skill or execution adapter for those surfaces. These delegate planning through Codex without replacing the host's interactive model. Explicit `solplan` continues to select Sol; explicit Fable and Kimi workflows keep their own planners. Single-threaded Astra consultation/review uses `high` with multi-agent disabled, not Ultra.
