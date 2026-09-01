---
description: Fable plans, Opus executes. Delegates the design of a nontrivial change to the fable-planner subagent (Fable), then implements the approved plan here in Opus. Sol (Codex/GPT-5.6) critiques the plan, can be consulted mid-plan, and can be handed genuinely independent execution chunks. Use this whenever a task needs a plan before code gets written — the user asking to plan, plan mode, or any multi-file / architectural / "how should we do this" change — not only when invoked by name.
argument-hint: <task to plan, then execute>
---

Plan the task on Fable, execute it here on Opus. The task: **$ARGUMENTS**

You are the Opus main loop. You keep the session, the user relationship, and sign-off; you delegate only the *design* step to Fable, because planning is where a second, different model pays for itself. There is no model switching and no env remapping involved — just a subagent.

## Flow

1. **Delegate the planning.** Spawn the `fable-planner` agent (runs on Fable, read-only) with a self-contained brief: the task, the files or entry points you already know are relevant, any constraints the user stated, and what "done" should mean. Do *not* pre-solve it — if you hand over a finished design, you spent the tokens you were trying to save. Hand over the problem, not the answer.

2. **Read the plan critically — it is a proposal, not an order.** Fable planned with fresh context and may have missed something you know from this session. Check its `RISKS / ASSUMPTIONS` against what you actually know, and check that the files it names exist and say what it thinks they say. If it's wrong, correct it or send it back once — never silently execute a plan you don't believe.

3. **Get it critiqued by Sol.** Default on; skip only when the plan is small and its contract fully mechanically verifiable, or the user asked for speed. Sol gets **clean context** — the original task brief and Fable's plan text, nothing else. Never Fable's reasoning, never this session's transcript: the reviewer's independence is the whole mechanism, and anchoring it on the author's thinking destroys the benefit. Paste the plan **verbatim** — do not paraphrase or compress it to save tokens. A summarized plan makes Sol review your summary, not the plan, and any detail you drop or garble comes back as a false-positive finding you'll waste a verification pass rejecting.

   ```bash
   ART=~/.claude/fableplan-artifacts; mkdir -p "$ART"
   BASE="$ART/sol-critique-$(date +%Y%m%d-%H%M%S)-$$"
   cat > "$BASE.prompt.txt" <<'EOF'
   You are a one-shot adversarial reviewer of an implementation plan you did not
   write. Work single-threaded; do not launch subagents or run codex or claude.
   You may read the repository to check the plan's factual claims. You cannot run
   tests, so mark any finding you could not verify by reading. Your output is
   evidence for the implementer; it is not approval and must not be worded as a
   verdict of acceptance.
   TASK BRIEF: <...>
   PLAN: <paste Fable's plan verbatim>
   Return numbered findings: severity (BLOCKER/MAJOR/MINOR), file evidence, what
   to change. If the plan declares DELEGABLE chunks, judge each chunk's
   independence. End with OVERALL: one sentence.
   EOF
   codex exec -s read-only -C "$PWD" --skip-git-repo-check --ephemeral \
     --ignore-user-config -c model_reasoning_effort=high \
     -o "$BASE.md" - < "$BASE.prompt.txt"
   ```

   The prompt and the critique both persist under `~/.claude/fableplan-artifacts/` for review. One round only — no critique-of-critique, no debate loop. Verify each material finding against the repo yourself before adopting it; Sol can't run anything, so an unverified claim is a lead, not a fact. An unresolvable BLOCKER triggers the one-time send-back to Fable in step 2, carrying that specific finding. For a high-stakes plan you may raise the block's `-c model_reasoning_effort=high` to `max` for one deeper critique (change that flag's value — don't add a second one); if it risks exceeding the Bash timeout, run it in the background and poll.

4. **Get the user's approval.** Present the plan (via ExitPlanMode when you're in plan mode). Show provenance: what Fable proposed, which of Sol's findings you adopted or rejected and why, and what you changed yourself. Surface the assumptions instead of burying them. **Sol's review is not validation and never functions as user consent** — only the user approves. Iterate until they do. If the task is underspecified — the planner said so, or you can't state a contract — resolve that with the user *before* writing code.

5. **Execute it — yourself by default.** For `OPUS-DIRECT`, and for any chunk the user didn't explicitly approve for delegation, you build it here. For each approved `DELEGABLE` chunk, route one bounded execution task through `/cheap-delegate`; if the approved plan contains at least two independent chunks, `/fanout` may execute them after planning. Never hardcode Sol, Kimi, or any lane in the plan: the live playbook and ledger choose the cheapest adequate executor. K3 is the long-horizon worker; `kimiplan` is a planner and never an execution lane. An HTTP 500 from GLM is `infra`, not a content failure.

6. **Verify against the contract** the plan defined (this repo's two-tier policy): run it, report pass/fail honestly, and gate with `bin/mut` when the change is high-value. Report what changed and any deviation from what was approved.

## Guardrails for every Codex call

Always pass an explicit `-s`, plus `--ephemeral` and `--skip-git-repo-check` (`codex exec` refuses to run outside a trusted dir without it). Always pass `--ignore-user-config` so the call runs hermetically — but that also drops the config's default reasoning effort to `none`, so you must pin `-c model_reasoning_effort=<effort>` explicitly in the same call, as every block above does. The **critique** (step 3) and the planner's **consult** are Sol at `high` — they ride the surviving `gpt-5.6-sol` default and need no `-m`. A step-5 **execution chunk** is the opposite case: it enters at the cheapest tier, so it pins `-m` and the matching effort together (`gpt-5.6-luna`/`low` → `gpt-5.6-terra`/`medium` → `gpt-5.6-sol`/`high`). Pinning one without the other is the bug this sentence exists to prevent. Never pass `--strict-config` — it aborts on this user's config. Never pass `--full-auto`, `--yolo`, `--ask-for-approval`, or `--include-plan-tool`; they don't exist in codex-cli 0.144.6. Never use `ultra` as a reasoning effort — it isn't one, it's an orchestration mode that spawns ~4 parallel threads and blurs accountability; `max` is the deepest single agent. Direction is outbound only — Claude→Codex and Claude→OpenCode: nothing here calls back into Claude (the `o` lane runs OpenCode's own coder agents, which are not Claude and must never be pointed at it), and recursion is bounded by the read-only sandbox, `--ephemeral`, and the Bash timeout rather than by a depth counter — so keep every prompt's child-guard line intact. Every planning-side call writes its prompt and Sol's output to `~/.claude/fableplan-artifacts/` — that directory is the planning audit trail; it accumulates, so clear it when you want. Execution-side chunks audit elsewhere on purpose: their contract, brief, output, and receipt live under `.results/delegation/<run-id>/`, and their row lives in the delegation ledger, so plan-chunk delegation is ranked by the same evidence as every other lane rather than sitting in a directory nothing reads.

## When *not* to use this

Skip the planner for trivial or mechanical work — a one-line fix, a rename, an obvious edit. Spawning an agent to plan a two-minute change is pure overhead. The trigger is *"this needs thinking before it needs typing,"* not *"this is a task."*
