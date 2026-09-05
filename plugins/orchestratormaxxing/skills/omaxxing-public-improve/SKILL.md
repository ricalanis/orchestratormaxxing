---
name: omaxxing-public-improve
description: Review a week of harness learnings, assess project-wide public value, project eligible capabilities into a public repository, validate coverage and security, and open a contribution PR when requested.
---

# Public harness improvement

Use [the workflow](references/workflow.md) for a contribution. Resolve the source
repository, public repository, branch and optional host locations from the operator's
request and verified Git metadata. Never infer a private source from a sibling name.
Without an additional source, the current public project can supply its own learnings.

1. Inventory the requested period (default seven days) and the public capability
   landscape. Account for every discovered item, including already-shipped work,
   exclusions, deferrals and inaccessible evidence. Separate review coverage from
   functional test coverage; a file count is neither.
2. Select a coherent package with concrete public benefits across supported domains.
   Trace each selected capability to its dependency closure and regression oracle.
   Preserve source/private evidence locally outside the public candidate.
3. Use the installed `astraplan` skill for nontrivial design. The calling host reviews
   the plan, authors contracts, then uses native `cheap-delegate` for bounded execution.
   Use `fanout` only for independent chunks in separate worktrees. Preserve explicit
   planner choices and the host's interactive model.
4. Build fresh commits on current public main. Reconcile the projection and host
   matrix, run the required contracts, and load the installed
   [`public-improve-security`](../public-improve-security/SKILL.md) skill. Its five
   passes and exact-artifact sign-off precede every public push or PR, including drafts.
5. When contribution publication is requested, push the verified contribution branch
   and open the PR with the reviewed body file. Confirm repository, base, head and CI.
   Never merge or push the default branch. An audit-only request authorizes no publish.

Sun means an optional persistent server, earth a client/laptop, and moons containers.
These roles do not determine ownership or export eligibility. Preserve standalone use;
never require the operator's cloud account, remote hosts or private services for core
features. Compare both hosts when available and relevant; report gaps honestly.

Keep a private checkpoint across interruptions. Limit repairs to two rounds per selected
capability, carrying the same budget through delegation and security fixes. If a required
gate remains unavailable or failing, report the concrete blocker and retained work.
Do not manufacture changes or publish first to obtain missing security review.
