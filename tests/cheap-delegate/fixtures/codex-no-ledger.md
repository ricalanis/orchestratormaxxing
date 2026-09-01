---
name: cheap-delegate
description: Route one bounded task to the cheapest adequate lane, verify it by a prewritten contract, and update the shared playbook. Use for “cheap delegate” or explicit cheap offloading.
---

Route exactly one task. Keep root Codex as contract author, verifier, and final
decision-maker.

1. Read `knowledge/delegation-playbook.md` (`## Codex lanes`), then run
   Read `authority` first: `sufficient` outranks a rule, `advisory` never does.
   Supersede a contradicted rule in place.

2. Apply the Tier-0 spec gate before dispatch. Write:

   - an acceptance contract: deterministic tests/assertions or a bounded boolean
     checklist that defines correct;
   - a self-contained context brief: only the necessary excerpts, interfaces,
     constraints, and prior decisions.

   Keep the task in root when correct cannot be specified cheaply. Never
   delegate final sign-off, merges, doctrine or `install.sh`, credentials-adjacent
   work, cross-file architecture, or another risky edit requiring full-repo
   judgment.

<!-- durable-delegation-gate:begin -->
**Physical dispatch gate (all lanes).** Before dispatch, Root creates
`.results/delegation/<run-id>/contract.md` and sibling `brief.md`, marks both read-only,
then checks that both are regular, non-empty files. The worker never authors, alters, grades, or certifies
the contract; it may only consume the immutable Root-authored copy.
For an OpenCode session, Root starts it with `o delegate`, keeps repairs in that
session with `o send`, captures its bounded handoff with `o output`
into `output.tmp`, and ends it with `o close`; pane output is observation, never acceptance. Publish it atomically as `output.md`
only after a zero exit. After Root runs the original acceptance check, write
`receipt.json` with lane, exit status, contract verdict, and output SHA-256.
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
   | Ollama one-shot | `oll "<brief + contract>" --model glm-5.2` | Included-quota response-only text, transforms, triage, review over supplied context |
   | OpenCode worker | `o delegate <run-id> --agent kimi-coder\|glm-coder\|minimax-coder --run-dir .results/delegation/<run-id> --json` | Workspace reads/edits, commands/tests, artifacts, repairs via `o send` in the same session |
   | Codex | `codex exec --ephemeral --skip-git-repo-check -s read-only -m <tier> -c model_reasoning_effort=<e> '<brief + contract>'` | Three tiers, cheap→strong: `-m gpt-5.6-luna` (`low`, the default rung) → `gpt-5.6-terra` (`medium`) → `gpt-5.6-sol` (`high`, only after the cheaper tiers are inadequate) |
   | Claude | `provider-ask anthropic "<brief + contract>"` | Cross-family opinion or refutation |

   Independent multi-chunk work belongs in `$claudemaxxing:fanout`.
   `o output <session> --json` is bounded transport observation only; it never
   accepts or repairs content. Keep repairs in the returned session with `o send`;
   a second `o delegate` is a new run, not a repair.

4. Dispatch the context brief and acceptance contract together. Verify by the
   contract only: read pass/fail and failure diffs from a deterministic runner,
   or scan a non-testable result only for evidence against the bounded boolean
   checklist. Never re-read the full solution to re-derive correctness.
   When `o output` succeeds, run the caller-authored contract against the
   captured handoff exactly; observation success is not artifact success.

5. On failure, allow at most two bounded repairs in the same lane. Feed back a
   structured failure diff containing only `check_id`, `expected`, `observed`,
   and a bounded output excerpt. Stop early when the diff is not shrinking.
   After two failed repairs, escalate one rung; a non-converging failure is a
   spec-ceiling signal, so finish in root. A repaired high-value code chunk must
   clear `bin/mut --src <file> --test "<contract runner>" --scope changed` before
   acceptance.

   pass|fail|infra --model <slug> --attempt N --run-dir .results/delegation/<id>`
   — **once per lane attempted, failures and infra included**. `--verdict` is
   root's contract run, never the worker's self-report.

7. Maintain `knowledge/delegation-playbook.md`; never fork it. Add dated evidence
   inside `## Codex lanes`: lane, task class, outcome, contract result, cost class,
   any quirk. When evidence contradicts an active Codex rule, rewrite that rule and
   move the old claim into the same section as `superseded`; never append a second
   active claim. Run `win-log add` only for a delivered win with a green contract
   and no mutation survivors; never force a refused record.

8. Report the lane, contract result, cost class, repairs/escalation, and the
   playbook change in 3–5 sentences. Root Codex performs final sign-off.
