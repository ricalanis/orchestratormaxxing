---
name: graduate
description: Maintain the public project (orchestratormaxxing) as a gated projection of this private repo — check the graduation gate, publish as a rolling PR or direct push, and absorb public PRs back into the private source of truth. Use after a committed change to graduated paths, or when the public repo has open PRs.
---

The private repo is the source of truth; the public repo is a deterministic projection
(`deploy/graduation.manifest` decides what graduates). Never edit the public checkout by hand.

1. **Gate first.** `core-export --check --worktree --json`. Exit 3 = a tenant literal or secret would
   ship: fix the source (wrap prose in `<!-- tenant:begin -->…<!-- tenant:end -->`, parameterize code
   through `~/.config/claudemaxxing/fleet.env`, or add an `exclude`) — never weaken the blocklist. Exit 2 =
   manifest/marker error. Read only the `BLOCK file:line` lines.
2. **Commit privately** (contracts green, `harness-verify` if a tool changed). Publication reads
   committed content only.
3. **Publish.** Default `core-export --pr --json` → one rolling PR (`graduate/next`) on the public
   repo, created or refreshed; report its URL. `--push` is the operator's direct path (first
   publication, or explicitly requested). Both are idempotent; "no changes" is a clean outcome.
4. **Inbound.** For each open public PR not authored by the tool (`gh pr list --repo <owner/repo>`),
   run `core-export --absorb-pr <n>`; if it applies cleanly and the change is wanted, re-run with
   `--apply` (the patch lands staged, uncommitted), run the touched contracts, commit privately, graduate again (step 3), then close the PR
   with a comment naming the private commit. A PR touching non-graduated paths is refused — explain
   why on the PR instead of forcing it.
5. Report: gate status, what was published (URL or "no changes"), PRs absorbed/refused.
