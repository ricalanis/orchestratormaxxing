---
name: solplan
description: Explicit Sol planning alternative to default astraplan. Plan nontrivial unplanned Codex work with an ephemeral read-only GPT-5.6 Sol Ultra planner using bounded read-only delegation, then have root Codex review and execute. Prioritize this before $orchestratormaxxing:fanout for architectural, multi-file, or otherwise design-bearing work; fanout may follow only for genuinely independent implementation chunks. Skip small mechanical or already-approved work.
---

Keep the root Codex thread as executor and final decision-maker. Delegate only design.

1. Author a self-contained brief with the goal, constraints, relevant paths, explicit unknowns, and a starting acceptance contract. Do not pre-solve the design.
2. Prefer the `sol-planner` custom agent when the current surface can select it explicitly. It pins Sol Ultra and a read-only sandbox. Ultra delegation is intentional: the planner may use at most three direct read-only subagents for independent exploration, while the root Sol planner owns synthesis. Keep agent depth at one.
3. Otherwise use the bundled runner. Resolve this skill's directory, then pass the brief on standard input. The runner closes the child stdin, ignores unrelated user plugins/configuration, explicitly enables multi-agent operation with four total threads and depth one, streams content-free lifecycle progress to stderr, emits only the validated final plan to stdout, and has no wall-clock deadline. Silence is reported with a liveness heartbeat instead of being treated as failure. Stream reads are nonblocking and byte-bounded: individual JSONL events above 1 MiB are discarded, raw child stderr is counted but never echoed, and the final plan is limited to 256 KiB and must be a regular non-symlink file.

   ```bash
   python3 <skill-dir>/scripts/run_solplan.py --workdir "$PWD" <<'SOLPLAN_BRIEF'
   <brief plus the child guard and required output shape>
   SOLPLAN_BRIEF
   ```

   Let the command yield periodically instead of wrapping it in an external timeout. Relay meaningful runner updates to the user while it works and poll an active command session at least once per minute. User cancellation remains authoritative. The runner terminates the complete planner process group on cancellation, an external terminating signal, or leader exit with inherited pipe writers; SIGTERM-resistant descendants are escalated to SIGKILL after the cleanup grace period.

   The bundled runner is observable but remains a one-shot `codex exec` flow; do not claim that its in-flight turn is steerable. Prefer the native `sol-planner` agent path in step 2 when the current surface needs live steering or direct inspection of subagent threads. A future App Server migration must preserve the runner's ignored-user-config, read-only, Ultra, bounded-delegation, and final-plan validation guarantees before replacing this fallback.

   Include this child guard and budget in the prompt: `You are the Solplan child. Do not invoke $orchestratormaxxing:solplan or launch another planning subprocess. You may delegate independent read-only exploration to at most three direct subagents; do not delegate final synthesis or recursively delegate. Use at most 8 root-thread read-only tool calls, do not browse externally unless the task requires current sources, and keep the plan under 1,200 words.`
4. Require exactly these sections: `SUMMARY`, numbered `STEPS` with exact paths, `CONTRACT`, `EXECUTION SHAPE`, `RISKS / ASSUMPTIONS`, and `OUT OF SCOPE`. `EXECUTION SHAPE` must choose `ROOT-DIRECT` or `FANOUT`; a `FANOUT` recommendation must name at least two independent chunks and their separate contracts.
5. If the runner fails, report its bounded error. Retry once only when the error is explicitly transient. A long healthy run is not a failure condition. Never silently substitute a different model or Claude Fable.
6. Review the returned plan as external evidence. Check named files and assumptions against the repo. Surface material user choices before implementation.
7. Implement and verify in the root Codex thread. Invoke `$orchestratormaxxing:fanout` only when the reviewed plan's execution shape is `FANOUT`; otherwise execute directly. Do not send execution back to the planner.

Claude's `/fableplan` and `fable-planner` remain a separate host-native workflow; do not edit or remap them from this skill.
