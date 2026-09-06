---
name: public-improve-security
description: Validate feature and capability projection into a public project, including dependency and host coverage, confidentiality, security analysis, independent review and exact-artifact sign-off before publication.
---

# Public capability projection and security

Use this for a proposed public contribution or a requested projection audit. An audit
does not authorize publishing. Load [the five-pass procedure](references/review.md).
The calling host owns scope, private information, evidence adjudication and sign-off.

First compare source intent with public behavior. Check that required entry points,
dependencies, configuration, schemas, migrations and tests travel together. Verify
standalone/client/server/container contracts only where claimed. Record omitted or
changed behavior with reasons; a sanitized feature that no longer works is not complete.
Keep private source descriptions and raw findings outside the public checkout.

Author the projection contract before implementation or review. Use
[the coverage schema](references/projection.md) and run:

```bash
python3 <installed-skill>/scripts/verify_projection.py --contract <private-contract.json> --report <private-report.json>
```

This checks declared scope accounting and evidence fields. It does **not** execute tests,
prove evidence true, discover undeclared capabilities, approve a security finding or
authorize publication. Root checks the actual evidence and final Git artifacts.

Complete five distinct passes before any public branch push or PR, including a draft:
confidentiality/export; automated security analysis; capability/trust boundaries;
independent adversarial projection review; root exact-publication verification.

Use the installed [`cheap-delegate`](../cheap-delegate/SKILL.md) skill natively for bounded
reviews and scanner execution. Resolve current models; persist root-authored read-only
brief/contract gates; use `o` for workspace work and `oll` only for cleared supplied text.
Keep raw secrets, architectural decisions and final sign-off in root. Use a fresh,
distinct reviewer for the independent pass, preferably a different model family.
Record actual attempts with canonical receipts and close every workspace worker.

Unresolved introduced or worsened security flaws, private disclosures, missing required
projection evidence and unavailable required analysis block publication. Do not weaken
rules or claim worker agreement is proof. Changed base/head invalidates review; metadata
changes require renewed confidentiality/secret checks and exact-artifact sign-off.
