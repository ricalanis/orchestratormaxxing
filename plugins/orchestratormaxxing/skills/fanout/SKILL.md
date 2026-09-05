---
name: fanout
description: Execute genuinely independent, acceptance-gated chunks with heavy Ollama Cloud models in parallel, then verify and merge. Use only after planning is complete or clearly unnecessary. For nontrivial unplanned Codex work, prioritize $orchestratormaxxing:solplan first; do not use fanout for coupled work or when correctness cannot be cheaply specified.
---

You are the primary Codex orchestrator. Keep requirements, contracts, verification, and final merge in the root thread; push only bounded bulk work to Ollama Cloud.

1. Confirm the work is already planned or clearly needs no design. If it is nontrivial and unplanned, stop decomposition and invoke `$orchestratormaxxing:solplan` in the root thread; resume fanout only if the reviewed plan identifies genuinely independent implementation chunks.
2. Decompose the accepted execution plan only when it has at least two chunks that share no mutable state and need no mid-flight handoff. Fanout runs after planning; default to one agent when the split is artificial.
3. Before dispatch, author an acceptance contract **and a self-contained context brief** for every chunk. Contract: deterministic tests/assertions for code, a boolean evidence checklist for non-testable output. Brief (BMAD story-file rule): the exact file excerpts, interface shapes, constraints, and decisions already made, baked into the dispatch so the worker never re-derives context. If you cannot state the contract or write the brief cheaply, refine the task once or keep it in the root Codex thread — a chunk whose brief is expensive depends on shared context and must not be dispatched.
4. Normal fanout uses the live baseline from `bin/oll`: `deepseek-v4-flash:0731` (volume/default), `glm-5.3` (explicit reasoning), `kimi-k3` (higher-consumption long-horizon/1M context), `kimi-k2.7-code` (bounded code-focused work), and `qwen3.5:397b` (general). Response-only chunks use direct parallel `oll` processes. Workspace reads/edits, commands/tests, or persistent artifacts use one independent `o delegate <run-id> --profile volume|reasoning|bounded-code|long-horizon|general --run-dir .results/delegation/<run-id> --json` session per chunk (`o-ubuntu delegate` remotely); run `o close <session>` on success, failure, or escalation. K3 does not justify fanout by itself. Never select legacy generations.
5. For code prompts, ask the worker for a minimal self-test, but never treat worker-authored tests as authoritative.
6. Verify without redoing the work:
   - Tier 1a: run the root-authored contract and read pass/fail plus failure diffs.
   - Tier 1b: for risky/high-value/new-worker chunks run `bin/mut --src <file> --test "<contract>" --scope changed`; accept only when residual survivors are equivalent or the contract is tightened to kill real holes.
   - Tier 2: scan against the prewritten checklist, or use a different-family critic framed as external work to refute.
7. On a deterministic failure, return only the failure diff to the same worker for at most two repairs. Any repaired code chunk must pass Tier 1b. Escalate early if the diff is not shrinking.
8. Reconcile only verified outputs and make the final decision in the root thread.

Report the models used, which contracts ran, pass/fail, and any mutation score. Agreement between LLMs is corroboration, never proof.

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
