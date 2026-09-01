---
description: Quick second opinion from Claude Sonnet (subscription CLI via provider-ask)
---
Consult Claude Sonnet on the question below, then continue your work using its
answer as a second opinion from another agent — integrate it critically, do not
treat it as ground truth.

1. Run this with your bash tool, passing the question as ONE safely
   single-quoted argument (escape embedded single quotes as '\''):
   `MODEL=sonnet provider-ask anthropic '<question>'`
2. Report the key points of Sonnet's answer, state where you agree or disagree
   and why, then act on your own judgment.

Question: $ARGUMENTS
