---
description: Second opinion from Claude Opus (subscription CLI via provider-ask; never an in-OpenCode Anthropic provider)
---
Consult Claude Opus on the question below, then continue your work using its
answer as a second opinion from another agent — integrate it critically, do not
treat it as ground truth.

1. Run this with your bash tool, passing the question as ONE safely
   single-quoted argument (escape embedded single quotes as '\''):
   `MODEL=opus provider-ask anthropic '<question>'`
2. Report the key points of Opus's answer, state where you agree or disagree
   and why, then act on your own judgment.

Question: $ARGUMENTS
