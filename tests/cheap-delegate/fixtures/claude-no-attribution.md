---
description: Smart cheapest-adequate delegation — route a task to Ollama workers, OpenCode agents, Codex, Sonnet or Opus via the living delegation playbook, verify by contract, and update the playbook with the outcome. Fires when the user says "cheap delegate".
argument-hint: <task to delegate>
---

You are the **delegation router**. The task: **$ARGUMENTS**

Route it to the cheapest lane that will clear a real acceptance contract — and
leave the playbook smarter than you found it.

This command routes one bounded execution task. It does not plan or decompose:
use `/fableplan` for design and `/fanout` for at least two independent chunks.

1. **Load the brain.** Read `knowledge/delegation-playbook.md` (lanes, active
   rules, escalation ladder) and run `win-log match --class <task-class>` for
   evidence of a winning shape. Then run
   `delegate-ledger stats --class <task-class> --json` for the pass/fail record of
   every lane on this class. If win-log and the playbook disagree, win-log's
   evidence outranks prose. Read the ledger's `authority` field first: a
   `sufficient` stat outranks a playbook rule, an `advisory` one never does — and an
   advisory stat omits `preferred_lane` entirely, so there is no ranking to quote.
   Read only the aggregate (pass/fail counts, rates); never re-derive it.

2. **Spec-gate (Tier 0) — before any dispatch.** Author the **acceptance
   contract** (tests / assertions / boolean checklist defining "correct") and the
   **context brief** (everything the worker needs: file excerpts, interfaces,
   constraints, decisions already made). If you cannot state the contract
   cheaply, the task is not delegable — do it here and say so. Hard keeps (final
   sign-off, merges, doctrine/install.sh, credentials-adjacent, cross-file
   architecture) are never delegated regardless of lane.

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

3. **Pick the lane** from the playbook's table, cheapest adequate first — unless
   the task text begins with the escalation sigil **`F.`**, in which case strip the
   token, skip lanes 1–3 entirely and go direct-to-strong (Codex **Sol** by default,
   the Claude lane when the question is Claude-shaped), and pass `--override` when
   you record the row. The sigil is the operator declaring the class known-hard; see
   `## Escalation sigil` in the playbook. The
   Codex rung is **three tiers, not one** — name the tier explicitly, never a
   bare `provider-ask openai` (that silently rides the default model):

   | Lane | Invocation | Use |
   |---|---|---|
   | Ollama one-shot | `oll "<brief + contract>"` (`--reasoning` for GLM) | Included-quota response-only text; V4 Pro is the volume default |
   | OpenCode worker | `o delegate <run-id> --profile volume\|reasoning\|bounded-code\|long-horizon\|general\|long-context --run-dir .results/delegation/<run-id> --json` | Workspace work; `bin/oll` resolves the exact agent/model and K3 is not the default |
   | Codex **Luna** | `MODEL=gpt-5.6-luna provider-ask openai "<brief + contract>"` | Fast, cheap bounded Codex work — the default Codex tier |
   | Codex **Terra** | `MODEL=gpt-5.6-terra provider-ask openai "<brief + contract>"` | Balanced everyday coding or reasoning |
   | Codex **Sol** | `MODEL=gpt-5.6-sol provider-ask openai "<brief + contract>"` | Hard bounded reasoning, after the cheaper tiers are inadequate |
   | Claude Sonnet | `provider-ask anthropic "<brief + contract>"` | Cross-family frontier check on a bounded question |
   | Claude Opus | `MODEL=opus provider-ask anthropic "<brief + contract>"` | Highest-stakes bounded second opinion — rare by design |
   | Keep in session | no dispatch | Hard keeps (see step 2) |

   **Kimi K3 has three explicit boundaries.** Use `oll --model kimi-k3` only
   for response-only work over supplied context. Use `o delegate <run-id>
   --profile long-horizon --run-dir .results/delegation/<run-id> --json` for
   stateful workspace work, and `o delegate <run-id> --profile planning
   --run-dir .results/delegation/<run-id> --json` for read-only planning.
   K3 is included-first but higher-consumption, so select it for capability or
   1M context, not as the universal cheapest lane.

   An HTTP 500 from GLM is `infra`, not a failed artifact. Retry the same lane
   once, then change runtime/family; do not spend a content-repair round or
   demote GLM's capability from a transport failure.

   `provider-ask openai` maps to `codex exec -m <model> -s read-only`, so the
   `MODEL=` selector *is* the Codex model choice; all three slugs were live-probed
   2026-08-12. Start at Luna and escalate one tier on a failed contract — a
   known-hard task class may go direct to Sol. State the lane **and tier** plus one
   sentence of why before dispatching. Genuinely independent multi-chunk work goes
   to `/fanout` instead — this command routes ONE task.

   For workspace work, run `o delegate <run-id> --profile <profile> --run-dir
   .results/delegation/<run-id> --json`; reuse the returned session with `o send`
   for repairs, retrieve the typed result with `o handoff --json`, and inspect
   pane state only when needed with `o output --json`.
   The `occ` wrapper is an internal one-shot transport behind `o`, not a host-facing
   delegation lane. Run `o close <session>` on success, failure, or escalation.
   Transport success never accepts the worker artifact.

4. **Dispatch from the persisted brief + contract**, then **verify by contract
   only** (two-tier policy: deterministic runner or boolean-checklist scan; never
   re-read the full output to re-derive it). On failure: up to **2 repair rounds
   in the same lane** feeding back the failure diff, then escalate **one rung**.
   A repaired code chunk must clear `bin/mut` before acceptance when the chunk is
   high-value. Non-converging repair = spec ceiling → finish it here.
   A `completed_retrievable` `o handoff` still must pass the original caller-authored
   artifact contract; transport observation is not acceptance.

5. **Maintain the playbook (this is what makes it smart).** After the outcome:
   - Record the row, **once per lane attempted — including the failures and the
     infra aborts**, which is the half `win-log` refuses to hold:
     `delegate-ledger record --run-id <id> --class <c> --lane <lane> --verdict
     pass|fail|infra --model <slug> --attempt N --run-dir
     .results/delegation/<id>` plus `--duration-s`, `--repair-rounds`,
     `--survivors` and `--override` when they apply. `--verdict` is **your**
     contract run, never the worker's claim about itself; passing `--run-dir` makes
     the tool refuse a row that contradicts the receipt.
   - Append a dated evidence entry to the log in
     `knowledge/delegation-playbook.md` (lane, task class, result, anything
     surprising: latency, quality, a model quirk).
   - If the outcome **contradicts an active rule**, rewrite the rule in place and
     move the old text to the log as a dated "superseded" entry — never leave two
     contradictory rules active.
   - If it was a true win (delivered, contract green, no mutation survivors),
     record it: `win-log add` (it refuses non-wins by design — don't force it).
   - New externally-sourced best practices (from `/research` or `harness-scan`)
     enter as log entries only; they become rules after surviving a local
     outcome.

6. **Report**: lane used, verification result (pass/fail + what the contract
   checked), cost class (included-quota vs subscription lane), and what the
   playbook learned — in 3–5 sentences, no transcript dumps.
