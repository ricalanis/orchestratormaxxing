# research-prompt — the 14 rules

Apply every rule while drafting the paragraph (SKILL.md keeps only the process and the template).
Adapted from upstream: the gap round is bounded to one pass, unresolved uncertainty is reported
rather than chased, and context collection is limited to cleared/supplied material.

- **One paragraph.** No headers, no bullet list in the deliverable.
- **Prompt the job, not the topic.** Give search handles (timeframe, ranking, source type, decision logic) — not just a subject.
- **Assume zero prior knowledge.** Write for a researcher who has never heard of the project. Open by explaining, in plain English, what the project/product is, why it exists, and the current situation — so they understand what's going on, what we need, and why we need it.
- **Lead with the goal + decision.** Right after that explainer, state the single question the research must answer and the decision/use it informs.
- **Embed all context.** Names, dates, product, prior known facts, constraints, drawn only from cleared/supplied material. The researcher must not need to ask anything or guess.
- **Number the sub-questions inline** (1, 2, 3…) so coverage is explicit. Keep to 3–6. One mission per prompt — don't cram unrelated questions.
- **State constraints.** What to include, what to avoid (e.g. "only non-Chinese competitors", "no marketing fluff").
- **Source hierarchy.** Prefer primary sources (official docs, GitHub, papers, filings, changelogs); forums/X/Reddit are weak signal only, never factual proof.
- **Contradiction handling.** If sources conflict, separate confirmed facts / inference / unresolved uncertainty — don't force fake consensus. Flag low-confidence claims for verification.
- **Completion bar (define "done").** Don't stop at the first plausible answer. Corroborate each key claim with multiple independent primary sources where they exist; where sources are scarce, say so explicitly instead of padding. Keep going until every numbered sub-question is covered to this bar.
- **Gap round before finishing.** Require exactly one bounded self-critique pass: list gaps, contradictions, and any single-source claims, run one more round of searches to close them, then stop. Report any remaining unresolved uncertainty explicitly instead of searching further.
- **Constrain output hard, method loosely.** Be strict on the deliverable; leave the search path flexible so the researcher can explore.
- **Demand a fixed output per finding:** source link + specific claim + one-line "why it matters / why a viewer should care".
- Verifiable, citable facts only. No opinions.
- **Last sentence:** instruct them to output everything into a single detailed markdown file.
