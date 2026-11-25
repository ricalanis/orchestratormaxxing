---
name: no-slop
description: Keep AI slop out of code, docs, PRs and messages before a person reads them. Use when handing over agent-written work, reviewing someone else's, or when the user says slop, no-slop or the manifesto.
---

# No slop

Agent actions drawn from the ideas of the No AI Slop manifesto (gist
dexhorthy/4cc96f01be5b1208519e75dad250a948); the wording is this repo's own. Slop is output that took
less effort to make than it takes the reader to understand.

Before you hand anything over:

1. Code: `slop-check` lists slop on the lines you changed (exit 2 means it did not scan: never call
   that clean). Use it to compare versions or to review, and fix a finding only when the fix makes
   the code simpler. Do not loop a worker on its output: in one measured run a fix pass removed 14%
   of the findings it was shown for 76% more tokens, and the final code had no less slop than the
   control (`knowledge/no-slop-2026-11-24.md`).
2. Cut before you add. Delete dead branches, single-use helpers and checks the contract does not need.
   Fix the cause instead of working around it.
3. Size it for the reader: result or decision first, and small enough to review in one sitting.
   Split it otherwise.
4. Own it. Check the parts that matter yourself, say which parts an agent wrote, and name what you did
   not verify.
5. A mistake seen twice gets a check, test or lint rule, not another reminder.

When someone sends you slop (a huge unreviewed PR, an unread AI doc): say so plainly and kindly, name
what to cut or verify, and ask for a reviewed version before you spend review time on it.

Where people look depends on the stakes. A plan, design or result always gets a human read. What a
check can catch goes to the check.
