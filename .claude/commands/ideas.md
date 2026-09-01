---
description: Boot several different Ollama Cloud models on the SAME problem to get diverse approaches, then synthesize the best one.
argument-hint: <problem / decision to get approaches for>
---

You need **diverse approaches** to: **$ARGUMENTS**

Do this:
1. **Convene the council.** Run `oll-council "$ARGUMENTS"` to ask several different models (different labs/architectures) for independent approaches in parallel. Pipe in relevant context if it helps:
   `cat <relevant-file> | oll-council "$ARGUMENTS"`
   Use `--models a,b,c` to steer the panel (default is a strong diverse set).
2. **Read every approach.** Note where they agree (likely robust) and where they diverge (the interesting design space).
3. **Synthesize — don't just pick one.** Produce a single recommended approach that grafts the best ideas across models. Explicitly call out:
   - the consensus core,
   - the one or two genuinely different alternatives worth considering,
   - your recommendation + why.
4. Keep it decisive. The council generates options; you (Opus) make the call.

This is for getting *unstuck* and avoiding single-model tunnel vision — use it on design decisions, tricky bugs, and "what's another way to do this?" moments.
