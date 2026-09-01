---
name: cheap-delegate
description: Route one bounded task to the cheapest adequate lane, verify it by a prewritten contract, and update the shared playbook. Use for “cheap delegate” or explicit cheap offloading.
---

Route exactly one task. Keep root Codex as contract author, verifier, and final
decision-maker.

This routes one bounded execution task. It does not plan or decompose: invoke
`$claudemaxxing:solplan` for design and fanout only for independent chunks.

1. Read `knowledge/delegation-playbook.md` (`## Codex lanes`), then run
   `win-log match --class <c>` and `delegate-ledger stats --class <c> --json`.
   Read `authority` first: `sufficient` outranks a rule, `advisory` never does.
   Supersede a contradicted rule in place.

2. Before dispatch, write a deterministic/checklist acceptance contract and a
   self-contained brief with only required context. Keep the task in root when
   correctness is not cheap to specify. Never delegate final sign-off, merges,
   doctrine/`install.sh`, credentials, architecture, or risky full-repo edits.

<!-- durable-delegation-gate:begin -->
**Physical dispatch gate (all lanes).** Before dispatch, Root creates
`.results/delegation/<run-id>/contract.md` and sibling `brief.md`, marks both read-only,
then checks that both are regular, non-empty files. The worker never authors, alters, grades, or certifies
the contract; it may only consume the immutable Root-authored copy.
For an OpenCode session, Root starts it with `o delegate`, keeps repairs in that
session with `o send`, captures its durable final handoff with `o handoff`
into `output.tmp`, and ends it with `o close`; `o output` is diagnostic observation, never the handoff or acceptance. Publish it atomically as `output.md`
only after a zero exit. After Root runs the original acceptance check, write `receipt.json` with
`delegate-ledger receipt` — closed fields, measured SHA-256s.
Keep prompts, credentials, and sensitive raw context out of the receipt; never
place run artifacts in `/tmp`. `oll` is allowed only for response-only work
over supplied context, and Root must capture its stdout. Stateful workspace
reads or edits, commands, tests, persistent artifacts, and repairs
require the public `o` worker runtime; `occ` remains an internal one-shot
transport behind `o`. OpenCode supplies tools and persistence, not better model judgment.
<!-- durable-delegation-gate:end -->

3. Choose the cheapest adequate lane the playbook supports and state it plus a
   one-sentence reason before dispatch. Exception: leading sigil **`F.`** means
   strip it, skip lanes 1–3, go direct-to-strong (Sol by default), record with
   `--override`. See `## Escalation sigil` in the playbook.

   | Lane | Invocation | Use |
   |---|---|---|
   | Ollama one-shot | `oll "<brief + contract>"` (`--reasoning` for GLM) | Response-only; V4 Pro is the volume default |
   | OpenCode worker | `o delegate <run-id> --profile volume\|reasoning\|bounded-code\|long-horizon\|general\|long-context --run-dir .results/delegation/<run-id> --json` | Workspace work; `bin/oll` resolves the exact agent/model |
   | Codex | `codex exec --ephemeral --skip-git-repo-check -s read-only -m <tier> -c model_reasoning_effort=<e> '<brief + contract>'` | Three tiers, cheap→strong: `-m gpt-5.6-luna` (`low`, the default rung) → `gpt-5.6-terra` (`medium`) → `gpt-5.6-sol` (`high`, only after the cheaper tiers are inadequate) |
   | Claude | `provider-ask anthropic "<brief + contract>"` | Cross-family opinion or refutation |

   **Kimi K3 boundaries.** `oll --model kimi-k3` is response-only. Use `o
   delegate <run-id> --profile long-horizon --run-dir
   .results/delegation/<run-id> --json` for stateful work and `o delegate
   <run-id> --profile planning --run-dir .results/delegation/<run-id> --json` for
   read-only planning. K3 is included-first but higher-consumption. Choose it
   for capability or 1M context; K2.7 remains the bounded code-focused worker.

   An HTTP 500 from GLM is `infra`, not content failure. Retry the same lane
   once, then change runtime/family without consuming a content-repair round.

   Independent multi-chunk work belongs in `$claudemaxxing:fanout`.
   `o handoff <session> --json` waits for the current turn's typed terminal
   result; `o output <session> --json` is diagnostic observation only. Neither
   accepts or repairs content. Keep repairs in the returned session with `o send`;
   a second `o delegate` is a new run, not a repair. Run `o close <session>` on
   success, failure, or escalation.

4. Dispatch brief + contract together. Verify only pass/fail and failure diffs,
   or checklist evidence for non-testable work; never re-derive the solution.
   Run the caller-authored contract only after `o handoff` returns
   `completed_retrievable`; transport is not acceptance.

5. Allow two same-lane repairs using only `check_id`, `expected`, `observed`, and
   a bounded excerpt. Stop when the diff does not shrink; then escalate or root.
   A repaired high-value code chunk must
   clear `bin/mut --src <file> --test "<contract runner>" --scope changed` before
   acceptance.

6. `delegate-ledger record --run-id <id> --class <c> --lane <lane> --verdict
   pass|fail|infra --model <slug> --attempt N --run-dir .results/delegation/<id>`
   — **once per lane attempted, failures and infra included**. `--verdict` is
   root's contract run, never the worker's self-report.

7. Maintain `knowledge/delegation-playbook.md`; never fork it. Add dated lane,
   class, outcome, contract, cost, and quirks under `## Codex lanes`. Rewrite
   contradicted rules and mark old claims `superseded`. Run `win-log add` only
   for a green delivered win with no mutation survivors.

8. Report the lane, contract result, cost class, repairs/escalation, and the
   playbook change in 3–5 sentences. Root Codex performs final sign-off.
