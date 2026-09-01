---
name: ideas
description: Generate genuinely diverse approaches to a hard design, debugging, or decision problem with several model families, then synthesize one recommendation. Use to escape tunnel vision; do not use when a deterministic answer or a straightforward edit is already clear.
---

1. State the decision and the constraints that every approach must respect.
2. Run `oll-council` for within-Ollama diversity, or `multi-council` when cross-provider de-correlation materially matters. Pass only the relevant context.
3. Treat all model claims as proposals. Identify the consensus core, meaningful disagreements, and assumptions that need deterministic verification.
4. Synthesize one recommendation instead of voting or selecting a response verbatim.
5. State one or two alternatives worth preserving and the condition under which each would win.

The root Codex thread owns the decision. Cross-model agreement lowers correlated blind spots but does not replace a contract, test, or human sign-off.
