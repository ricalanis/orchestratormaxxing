# fableplan: why the session-wide model remap was rolled back (2026-07-13)

Short version: **the launcher worked and was still the wrong design.** It has been replaced by
subagent delegation. Don't rebuild it — read this first if you're tempted.

## What was built (and removed)

`bin/fableplan`: a shell launcher that inverted `opusplan`. Claude Code has no native `fableplan`
alias (the alias list is hardcoded, and no hook, plugin setting, or command frontmatter can switch
the *main loop's* model mid-session — verified against the CLI binary). But `opusplan` is just
"the **opus alias slot** in plan mode, the **sonnet alias slot** otherwise," so the launcher
remapped the slots cross-family:

    ANTHROPIC_DEFAULT_OPUS_MODEL=claude-fable-5
    ANTHROPIC_DEFAULT_SONNET_MODEL=claude-opus-4-8
    claude --model opusplan

It was **verified by billed `modelUsage`**, not by intent: plan mode billed `claude-fable-5[1m]`
(keeping opusplan's 1M-context plan upgrade), execution billed `claude-opus-4-8`. `fableplan --verify`
re-ran both probes headlessly and passed. The `c` helper defaulted every interactive session to it.

## Why it was rolled back

Ricardo's call, and it's the right one: **the cost wasn't the tokens, it was the reasoning load.**

1. **The remap is session-wide and invisible.** It rewrites what the *words* `opus` and `sonnet`
   mean for the entire process. Inside a fableplan session, a subagent declaring `model: opus`
   silently got **Fable** — so every agent whose family mattered had to pin a full model ID, and
   forgetting to was a silent, correct-looking failure. That's a global mutable variable wearing a
   model alias, and it taxed every future decision in the repo.
2. **It bought the cheap half.** The value is "let Fable design, let Opus build." That value lives
   entirely in *who plans*. Achieving it via a process-wide alias rewrite is enormous blast radius
   for a small, local benefit.
3. **The user has to remember to launch it.** A mode you must opt into at process start is a mode
   you forget to use.

## What replaced it

Plain Opus sessions. Planning — and only planning — is delegated:

- **`.claude/agents/fable-planner.md`** (`model: fable`, read-only): designs the plan, returns
  steps + acceptance contract + risks. With no launcher, aliases mean what they say, so `model: fable`
  is honest and needs no full-ID pin.
- **`.claude/commands/fableplan.md`**: the Opus loop spawns the planner, reads the plan *critically*,
  takes it to the user, then **implements it itself**. Its description is trigger-shaped, so the skill
  fires whenever a task needs a plan (plan mode, architectural/multi-file work) rather than only when
  typed — which was Ricardo's actual requirement, and the one thing the launcher genuinely did better
  than a manual slash command.
- **`.claude/agents/opus-executor.md` was deleted**: with the main loop already Opus, handing
  implementation to a subagent only threw away context for nothing.

Net: same outcome ("Fable plans, Opus builds"), zero global state, no alias shadowing, nothing to
launch, and it triggers on its own.

## The transferable lesson

The launcher passed its contract. Passing a contract is not the same as being a good design —
`--verify` measured *whether the remap happened*, and could not measure *what the remap cost the
person reasoning about the system afterwards*. When a mechanism's blast radius (a whole session, every
subagent, every future agent file) exceeds the scope of the thing it buys (one step: planning), scope
the mechanism down to that step, even when the big version demonstrably works.
