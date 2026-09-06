# Five passes with capability projection

Root authors scope and acceptance before dispatch. Use native `cheap-delegate`'s
playbook, routing evidence, live model resolution, immutable brief/contract gate,
repair budget, canonical `delegate-ledger receipt`/`record`, and worker cleanup.
Do not create a second security-specific model ladder. A routing receipt proves an
attempt and root's contract verdict; it is separate from publication authorization.

1. **Confidentiality and export.** Root reviews all outgoing commit history, resulting
   files, paths, assets, generated material, commit messages and planned PR text. Check
   private code/data, credentials, account/host identities, logs, redistribution rights
   and licenses. Run local secret scanning before sending context to any reviewer.
   A deleted secret still exists in outgoing history; rebuild unpublished commits if
   needed. Check binaries/archives explicitly. After clearance, delegate a bounded audit
   of the public-only snapshot and metadata. Never send raw private material to a worker
   to discover whether it is safe to send.
2. **Automated analysis.** Root scans complete outgoing history, final payload and PR
   metadata locally with a secret scanner. Delegate non-secret static/security checks on
   an isolated cleared public worktree. Include changed executable and embedded code;
   use current dependency advisories for dependency changes and configuration analysis
   for CI/containers/infrastructure. Record versions, commands, scope, exits and each
   finding's disposition. Missing tools or required data mean incomplete coverage.
   Tool-level not-applicable requires a specific scope reason. No zero-file scans,
   new broad ignores, disabled rules or silent baseline waivers.
3. **Projection and trust boundaries.** Delegate abuse cases against each declared
   capability and the complete public interface context. Verify source intent is
   preserved or intentionally changed; necessary dependencies, discovery surfaces,
   schemas/migrations, cleanup/retry semantics and host contracts are complete. Check
   paths/symlinks, command injection, untrusted agent/tool outputs, authentication,
   privilege, secrets/logs, resource bounds and network targets where affected.
   Optional server access must remain authenticated and optional; private network
   placement alone is not authorization. Tests use isolated synthetic fixtures, never
   customer data or production targets. Root validates counterexamples and corrections.
4. **Independent adversarial review.** After passes1–3, give a separate reviewer fresh
   public-only context, exact base/head, the public projection scope and a checklist.
   Prefer a different family from the author and boundary reviewer. Ask for missing
   behavior/dependencies and security counterexamples, not endorsement. Do not prime it
   with previous reviewers' conclusions. Root adjudicates every finding against evidence.
   Truncated, unavailable or insufficient review is incomplete. Record coverage limits.
5. **Exact publication.** Keep this pass in root under cheap-delegate's hard-keep rule.
   Reconcile projection rows and required evidence with the final tree, resolved findings,
   clean intended worktree, refreshed base, outgoing commits and final PR metadata.
   Required omissions cannot be hidden as passing projections. Record repository,
   base/head/tree, commit IDs, title/body hashes, projection contract/report hashes,
   each pass's evidence/reviewer/native receipt, justified tool-level exclusions and
   root verdict in a private `security-receipt.json`. Recheck these exact artifacts
   immediately before each authorized push or PR write.

Any code/base/head change requires renewed passes1–4. A metadata-only change requires
renewed confidentiality/secret checks and final sign-off; also renew projection review
if commands, capability claims or scope change. Fixes use the contribution's shared
two-round repair budget; an escalation does not reset it. Unrelated baseline findings
need recorded evidence that they are neither introduced nor worsened, not silent dismissal.

Store detailed reports locally outside the public payload. Public summaries name actual
checks, host coverage and limitations without raw findings, private source identities or
security guarantees. If a disclosure is discovered after push, stop further publication
and report it privately; do not claim history rewriting erases exposure or automatically
delete refs/rotate credentials. Obtain operator-directed recovery for those actions.
