---
name: orchestration-practices
description: Match agent work to the governed Orchestra-of-One vocabulary and prepare its deterministic preflights, four brakes, failure rescue, and evidence receipt. Use for nontrivial prompting, context handoffs, tool/MCP or verification work, repeated/autonomous loops, delegation, fan-out, multi-agent graphs, recovery after a failed or stalled run, or when the user mentions prompt engineering, context engineering, harness engineering, loop engineering, graph engineering, healthchecks, brakes, rescue, orchestration, or meta-loops.
---

# Orchestration Practices

Use the shared resolver to retrieve a bounded practice pack. The vocabulary is
advisory; deterministic checks and the task's authority boundary remain the
gate.

## Workflow

1. Match the request before choosing a topology:

   ```bash
   orchestration-practice match --host <hermes|orchestrator|claude|codex|opencode|open_design> --text "<request>" --json
   ```

2. Treat `abstain` as a real result. Do not approximate an unknown expression,
   unsupported host, missing capability, or conflicting practice.

3. Choose the simplest sufficient topology before dispatch:

   | Topology | Use only when |
   |---|---|
   | Manual agentic | The goal is unclear, subjective, sensitive, or load-bearing; root/human watches and decides each next step. |
   | Bounded goal | One finite item has an objective completion check and all four brakes. |
   | Observer/polling | External state must be detected; the observer only records/enqueues a signal, then one bounded execution handles one item. |
   | Proactive routine | A predefined stream is repeat-safe, AUTO-authorized, durably checkpointed, and independently verifiable. |

   Observation cadence never manufactures work. A timer may observe; it does
   not convert an empty queue or vague aspiration into an autonomous task.

4. Build the supplied runtime context and evaluate it:

   ```bash
   orchestration-practice evaluate --host <host> --text "<request>" --context <context.json> --json
   ```

   The context carries the creator-authored contract, dependency health,
   checkpoint, progress, state writers, objective completion evidence, and all
   four brakes: maximum iterations, time/token/cost budget, no-progress limit,
   and completion check. When available, include `action_results` as bounded
   JSON values shaped like `{"action": ..., "result": ...}`. The evaluator
   blocks the third consecutive identical pair even if a self-reported progress
   number increased; malformed supplied traces also fail closed.

5. On `blocked`, stop before dispatch. Report the failed check IDs and the
   allowlisted rescue options. A rescue is a proposal until its policy and the
   existing AUTO/SELECT boundary authorize it; matching never grants write,
   retry, external-action, or acceptance authority.

6. On `ready`, execute through the host's native workflow. Preserve the
   returned receipt with the task/run evidence. The receipt does not replace
   `run_contract`, mutation testing, a required human gate, or any host's
   deterministic verifier.

   `ready` means eligible to execute, never accepted as good. Hard evaluators
   can prove stated rules were met; they cannot supply product taste or judge
   artifact quality. A builder cannot accept its own output. Root/human retains
   subjective judgment and every sensitive, load-bearing, or SELECT decision.

## Invariants

- Keep run state in checkpoints and durable task events; keep cross-run facts in
  governed memory. Never let one impersonate the other.
- Route checkable conditions with code. Spend model judgment only where the
  condition requires interpretation.
- Default to one agent. Add a node only for a real specialty; collapse adjacent
  nodes when merging them loses nothing.
- Give every shared state field one writer. Make retries and external writes
  idempotent before enabling them.
- Pair throughput with confidence, rework, incident, and cost countermetrics.

## Host boundary

Hermes, Claude, Codex, and OpenCode consume the same installed skill and CLI.
Hermes Orchestrator enforces the catalog directly on task runs. Open Design only
receives the visual-work practice pack through its existing designer/resolver
path; it abstains from unsupported code, deployment, memory, and acceptance
actions.
