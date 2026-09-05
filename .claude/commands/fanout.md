---
description: Decompose a task into independent chunks and run them in parallel on cheap Ollama Cloud workers, then verify + merge.
argument-hint: <task to parallelize>
---

You are the **orchestrator**. The task: **$ARGUMENTS**

Do this:
1. **Decompose + spec-gate after planning.** Use fanout only after planning is complete (or design is clearly unnecessary) and only with at least two independent subtasks that can run in parallel (no shared state). For each, author its **acceptance contract** and self-contained **context brief** before dispatch. If either is expensive to state, or the chunks overlap or need a mid-flight handoff, keep the work in Opus instead of forcing a split.
2. **Dispatch in parallel.** Response-only chunks use one `ollama-worker`/`oll` process each. A chunk that must read/edit the workspace, run commands/tests, or persist files uses its own `o delegate <run-id> --profile <profile> --run-dir .results/delegation/<run-id> --json` session instead. `o-ubuntu delegate` is the identical remote entrypoint. Send independent starts together; never ask `oll` to impersonate a workspace writer.
   - Normal fanout uses `deepseek-v4-flash:0731` (volume/default), `glm-5.3` (explicit reasoning), `kimi-k3` (higher-consumption long-horizon/1M context), `kimi-k2.7-code` (bounded code-focused work), and `qwen3.5:397b` (general). K3 is selected inside a chunk that needs a long sequential chain; it does not justify fanout by itself. Never select legacy GLM or Kimi generations.
   - Profiles resolve centrally: `volume`→V4 Pro, `reasoning`→GLM, `bounded-code`→K2.7, `long-horizon`→K3, `general`→Qwen. Always run `o close <session>` after success, failure, or escalation.
3. **Verify (two-tier) + merge — never re-do the work to check it.**
   - *Testable subtasks (1a — correctness):* **author the contract yourself** (tests/assertions) — ideally up front in the subtask prompt — then run it with a deterministic runner and read only pass/fail + failure diffs. Don't let the worker be the sole author of its own tests.
   - *Testable subtasks (1b — mutation gate, gated):* for **high-value / risky / new-worker** chunks only, after 1a is green run `bin/mut --src <file> --test "<your contract>" --scope changed`. Read **only** the score + survivors. Per survivor: real hole → add one targeted assertion → re-run; equivalent mutant → accept. **Accept when** score ≥ threshold **and** residual survivors are all equivalent. Skip 1b on trivial chunks (running it everywhere violates the iron rule). A low score after tightening = spec ceiling → do that chunk in Opus.
   - *Non-testable subtasks:* turn the requirement into a boolean checklist, scan the output only for evidence per item (token-capped), or spawn a second different worker as critic and accept on agreement.
   - *On failure — repair before escalating:* feed the contract's failure diff back to the **same worker** for up to **2** bounded rounds before falling back to Opus (Asuka-Bench 2606.05920). A **repaired chunk must then clear 1b (mutation)** before accept — that's the anti-gaming gate (stops the worker overfitting the visible contract); escalate early if the diff isn't shrinking. Don't repair-loop non-testable chunks.
   - Then reconcile conflicts and produce one coherent result. (Full policy: CLAUDE.md → "Verifying worker output".)
4. **Report** which workers/models ran, a one-line cost note, and the mutation score for any chunk that ran 1b. Spend Opus only on decomposition, verification, and merge — push the bulk work to the workers.

Workspace isolation is required before parallel writers start. Give each writer a
separate `git worktree` and branch (or a separate clone), then verify its actual
working directory and Git root before `o delegate`. A distinct run directory or
session does not isolate repository state. Specify owned paths in every brief;
workers must preserve others' edits and must not run reset, checkout, stash or
clean against a shared checkout. Keep planning and read-only reviews read-only.

After integrating every accepted chunk, root reruns all affected contracts on the
final combined tree. Individual worker passes do not establish that the combined
result works. Resolve integration failures before publishing or merging; retain
both per-chunk and combined verification evidence.
