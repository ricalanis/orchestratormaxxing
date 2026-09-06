# research-prompt — worked example

Synthetic scenario: a small team ships a self-hosted CLI that turns meeting transcripts
into action items. They must decide whether to add a local-first vector store for
semantic search over past notes. The research prompt below is the deliverable — one
self-contained paragraph a researcher with zero prior knowledge can act on.

> Acme Notes is a self-hosted CLI that converts meeting transcripts into action items
> and stores them as plain Markdown files on the user's own machine; it has no cloud
> account and no server. We are deciding whether to add a local-first vector store so
> users can semantically search their past notes, and we need to pick an approach that
> stays fully offline and dependency-light. Research local-first vector search options
> for a Python CLI that must run on macOS and Linux without a network, to answer one
> question: which embedded vector store gives the best recall-to-footprint trade-off for
> a few thousand short Markdown notes — for a build-vs-adopt decision. Find: (1) the
> current maintenance status and license of the top 3 candidate embedded vector stores
> (e.g. sqlite-vec, LanceDB, Chroma embedded); (2) their on-disk size and index build
> time for ~5,000 short documents; (3) whether each supports incremental updates without
> a server process; (4) any known recall regressions on short-document retrieval. Include
> official docs, GitHub repos and changelogs; avoid marketing pages and forum anecdotes.
> Prefer primary sources; treat forums/social as weak signal only; if sources conflict,
> separate fact from inference and flag what needs verification. Don't stop at the first
> plausible answer: corroborate each key claim with multiple independent primary sources
> where they exist (and say so explicitly where they don't), continuing until every
> numbered question is covered to that bar. Before finishing, do exactly one bounded
> self-critique pass — list gaps, contradictions, and any single-source claims, run one
> more round of searches to close them, then stop; report any remaining unresolved
> uncertainty explicitly instead of searching further. For each point, give the source
> link, the specific claim, and a one-line "why it matters". No marketing fluff —
> verifiable, citable facts only. Output everything into a single detailed markdown file.

Decision scenario: the team reads the returned report against a boolean checklist
(one item per numbered sub-question, plus "exactly one gap pass present" and "each finding
has a source link"). If sqlite-vec is maintained, tiny, and supports incremental updates,
they adopt it; otherwise they defer the feature. Any unresolved uncertainty the report
flags is treated as a decision input, not a reason to keep searching. The checklist is the
acceptance gate — the report is never re-derived to check it.
