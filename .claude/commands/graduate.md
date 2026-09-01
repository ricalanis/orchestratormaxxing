---
description: Maintain the public project (orchestratormaxxing) as a gated projection of this repo — run the graduation gate, publish as a rolling PR (or direct push), and absorb public PRs back into the private source of truth.
argument-hint: [check | pr | push | absorb <n>]
---

You are maintaining the **public projection** of this private repo. The private repo is the source
of truth; `deploy/graduation.manifest` decides what graduates; `bin/core-export` is the only writer of
the public checkout (`../orchestratormaxxing`). Never hand-edit the public tree.

Mode: **$ARGUMENTS** (default: `check` then `pr`).

1. **Gate** — `core-export --check --worktree --json`. Read only the `BLOCK file:line: /pattern/` lines.
   Exit 3 → a tenant literal or secret would ship: fix the *source* (wrap prose in
   `<!-- tenant:begin -->…<!-- tenant:end -->`, route code through `~/.config/orchestratormaxxing/fleet.env`,
   or `exclude` a fleet-only path). Never weaken a `block` line. Exit 2 → manifest/marker error.
2. **Commit privately** first (contracts green; `harness-verify` if a `bin/` tool changed) — publication
   reads committed objects only, `--worktree` is check-only.
3. **Publish** — `pr` (default): `core-export --pr --json` → one rolling PR on branch `graduate/next`,
   created or refreshed, never duplicated; report the URL. `push`: `core-export --push --json` (first
   publication, or when the operator asks for direct main). "no changes" is a clean outcome.
4. **Absorb** — `absorb <n>`: `core-export --absorb-pr <n> --json` (check-only). If it applies cleanly
   and the change is wanted: re-run with `--apply` (lands staged, uncommitted), run the touched contracts, commit privately, then
   `pr`/`push` again and close the public PR with a comment naming the private commit. A PR touching
   non-graduated paths exits 3 — say so on the PR; do not force it.
5. Report in ≤5 lines: gate result, published (URL / no changes), PRs absorbed or refused, next step.
