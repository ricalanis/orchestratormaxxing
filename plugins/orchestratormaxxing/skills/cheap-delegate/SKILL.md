---
name: cheap-delegate
description: Route one bounded task to the cheapest adequate lane, verify it by a prewritten contract, and update the shared playbook. Use for “cheap delegate” or explicit cheap offloading.
---

Route one bounded execution task; root Codex stays contract author, verifier, and
final sign-off. Never plans or decomposes — `$orchestratormaxxing:astraplan` designs,
fanout takes independent chunks.

1. Read `knowledge/delegation-playbook.md` (`## Codex lanes`), then run
   `win-log match --class <c>` and `delegate-ledger stats --class <c> --json`.
   Read `authority` first: `sufficient` outranks a rule, `advisory` never does.
   Supersede a contradicted rule in place.

2. Before dispatch, write a deterministic/checklist acceptance contract and a
   self-contained brief with only required context. Keep the task in root when
   correctness is not cheap to specify; never delegate final sign-off, merges,
   doctrine/`install.sh`, credentials, architecture, or risky repo-wide edits.

<!-- durable-delegation-gate:begin -->
**Physical dispatch gate (all lanes).** Before dispatch, Root creates
`.results/delegation/<run-id>/contract.md` and sibling `brief.md`, marks both read-only,
and checks both are regular and non-empty. The worker never authors, alters, grades, or certifies
the contract. OpenCode starts with `o delegate`; repairs use `o send`; `o handoff`
writes `output.tmp`, published as `output.md` only after exit 0; `o output` is diagnostic;
`o close` ends every path. After acceptance, write `receipt.json` with
`delegate-ledger receipt` (closed fields, measured SHA-256s).
Every newly authored OpenCode `brief.md` contains exactly one non-empty
`<!-- o-delegate-turn-1:begin -->` … `<!-- o-delegate-turn-1:end -->` block with
the complete bounded assignment. `o delegate` executes that assignment immediately
in turn 1; an unmarked legacy bounded brief executes as a whole. Never use turn 1
as a read-only bootstrap for a later assignment: `o send` is repair-only, never the
initial task.
Keep prompts, credentials, and sensitive context out of receipts and `/tmp`.
`oll` is allowed only for response-only work over supplied context; Root captures
stdout. Stateful workspace work and repairs require the public `o` worker runtime;
`occ` remains an internal one-shot transport behind `o`.
<!-- durable-delegation-gate:end -->

3. Pick the cheapest adequate lane; before dispatch state it, the **exact model
   slug**, and one sentence of why. Resolve the slug, never recall it:
   `oll --route-profile <profile>` or `o delegate --json`'s `model` for OpenCode,
   the `-m`/`MODEL=` selector for Codex and Claude, `oll`'s stderr banner for a
   one-shot. An agent is not a model (`glm-coder` vs `glm-5.3`); `sonnet`/`opus`
   are aliases (`## Model attribution`). Sigil **`F.`** — strip it, skip lanes 1–3,
   go direct-to-strong (Sol), record `--override` (`## Escalation sigil`).

   | Lane | Invocation | Use |
   |---|---|---|
   | Ollama one-shot | `oll "<brief + contract>"` (`--reasoning` for GLM) | Response-only; V4 Pro default |
   | OpenCode worker | `o delegate <run-id> --profile volume\|reasoning\|bounded-code\|long-horizon\|general\|long-context --run-dir .results/delegation/<run-id> --json` | Workspace work; `bin/oll` resolves the agent/model |
   | Codex | `codex exec --ephemeral --skip-git-repo-check -s read-only -m <tier> -c model_reasoning_effort=<e> '<brief + contract>'` | Cheap→strong: `-m gpt-5.6-luna` (`low`) → `gpt-5.6-terra` (`medium`) → `gpt-5.6-sol` (`high`) |
   | Claude | `provider-ask anthropic "<brief + contract>"` | Cross-family refutation |

   **Kimi K3 boundaries.** `oll --model kimi-k3` is response-only; `o delegate`
   `--profile long-horizon` is stateful and `--profile planning` read-only. K3 is
   included-first but higher-consumption: pick it for capability or 1M context;
   K2.7 stays the bounded code worker. An HTTP 500 from GLM is `infra`, not
   content failure: retry once, then change runtime/family, no repair round.

   Independent multi-chunk work belongs in `$orchestratormaxxing:fanout`. `o handoff
   <session> --json` returns the typed result; `o output` is diagnostic
   observation only — neither accepts nor repairs. Repair with `o send` in the same
   session, never a second `o delegate`; then `o close <session>` on success,
   failure, or escalation.

4. Dispatch brief + contract together; verify only pass/fail and failure diffs
   (checklist evidence for non-testable work), never re-deriving the solution. Run
   the caller-authored contract only after `o handoff` returns
   `completed_retrievable` — transport is not acceptance.

5. Two same-lane repairs max, carrying only `check_id`, `expected`, `observed`
   and a bounded excerpt; stop when the diff stops shrinking, then escalate or
   root. A repaired high-value code chunk clears `bin/mut --src <file> --test
   "<contract runner>" --scope changed` first.

6. `delegate-ledger record --run-id <id> --class <c> --lane <lane> --verdict
   pass|fail|infra --model <slug> --attempt N --run-dir .results/delegation/<id>`
   — once per lane attempted, failures and infra included. `--verdict` is root's
   contract run, never the worker's self-report.

7. Maintain `knowledge/delegation-playbook.md`; never fork it. Add the dated lane,
   class, outcome, contract, cost and quirks under `## Codex lanes`; rewrite
   contradicted rules, marking old claims `superseded`. `win-log add` only for a
   green delivered win with no mutation survivors.

8. Open the report with one attribution line **per lane attempt** — `<subtask> —
   <lane> · <model slug> · <verdict> (attempt N)`, repairs and escalation rungs
   each naming their own model — then contract result, cost class, escalation, and
   the playbook change in 3–5 sentences. Root Codex signs off.
