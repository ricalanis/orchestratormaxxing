# Skill style guide

Authoring contract for `plugins/claudemaxxing/skills/*/SKILL.md`.

Harvested 2026-07-25 from gentle-ai's `skill-creator/references/skill-style-guide.md`
during the example-client-install audit. Adopted selectively — what we rejected, and why, is at
the bottom. This is a **lint target read at authoring time**, not a runtime skill: it
costs zero frontier menu slots, which is the point (arXiv:2606.06284 — larger skill
menus reduce reliability).

## The core claim we adopted

> A SKILL.md is a **runtime instruction contract for an LLM**, not human documentation.

Everything below follows from that. If a line does not change what the model *does*, it
belongs in `knowledge/` or `docs/`, not in a skill body.

## Body budget

| Bound | Value | Enforcement |
|---|---|---|
| Target | 180–450 tokens | convention |
| Recommended max | 700 tokens | convention |
| Hard max | **1000 tokens** | `harness-verify` — `warn` |

Token estimate is `words × 1.33`. Over budget, move examples, schemas and background
into `references/` or `assets/` and link them. The skill body states *what to do*; the
reference holds *the detail you need once you're doing it*.

An always-on skill is the most expensive kind — it is charged on every turn of every
session. It should be the **smallest**, not the largest.

## Writing rules

**Do**
- Write imperative runtime instructions: "Load X", "Check Y", "Return Z".
- Lead with the activation trigger and the hard constraints.
- Use compact tables for decision gates.
- Keep examples minimal and executable.
- Link to local supporting files for detail.

**Don't**
- Explain history, motivation, or tutorial background.
- Duplicate long docs inside the skill.
- Add generic advice the model cannot execute.
- Add a `Keywords` section — discovery uses frontmatter.

## Frontmatter

- `name` and `description` are required (already gated by `harness-verify`).
- `description` MUST be one physical line and YAML-safe.
- Put trigger words first: `"Trigger: … . {what the skill does}."`

## Deliberately NOT adopted

| Upstream rule | Why we rejected it |
|---|---|
| `description` ≤250 chars | Measured against our catalogue: **5 of 8** skills exceed it, and `bin/harness-verify:1316-1324` greps `fanout`'s 343-char description for the plan-before-fanout contract. Adopting the cap would break our own gate. Our descriptions carry routing logic, not just a summary. |
| Fixed 7-section body structure (Activation Contract / Hard Rules / Decision Gates / …) | Our skills already have their own shapes fitted to their jobs. Imposing a foreign taxonomy is form over function — the body *budget* is the constraint that actually matters, not the section names. |
| `AGENTS.md` skill-registry coupling | Requires `gentle-ai skill-registry refresh`, which writes `.atl/` into every repo it touches. Its product is a bigger always-available menu — the opposite of what we want. |
| `license` / `metadata.author` / `metadata.version` required | Ceremony for a single-maintainer lab. No consumer reads them. |

## Enforcement

`bin/harness-verify` warns when any `plugins/claudemaxxing/skills/*/SKILL.md` body
exceeds the 1000-token hard maximum. Everything else here is convention, and is stated
as convention rather than dressed up as a gate — prose that claims to be binding without
an exit code behind it is the failure mode this whole audit was about.
