---
name: research-prompt
description: "Trigger: research brief, deep research prompt, arXiv/paper research, what should the researcher look for, a one-paragraph task for a researcher. Writes ONE self-contained Deep Research paragraph (full context, numbered sub-questions, per-finding output format), then routes its execution to this harness's research surfaces and verifies the report against a boolean checklist."
---
<!-- Adapted from davidondrej/skills@76724e4 skills/research-and-web/research-prompt/SKILL.md (MIT).
     Rules (references/rules.md), Process and Template are upstream verbatim; "Executing the prompt" is rewritten for cmaxxing. -->

# Research Prompt

Goal: turn a vague research need into ONE self-contained paragraph that a researcher with zero prior knowledge of the project can act on with zero back-and-forth.

## Rules

Read `references/rules.md` before drafting and apply all 14 rules (one paragraph; prompt the job, not the topic; zero prior knowledge; lead with goal + decision; embed all context; 3–6 numbered sub-questions; constraints; primary-source hierarchy; contradiction handling; completion bar; gap round; strict output, loose method; fixed per-finding format; facts only; last sentence = one markdown file).

## Process

1. Pull context from the relevant project files / conversation (dates, names, known facts, audience, end use), and write a 1–2 sentence plain-English explainer of what the project is and why it exists for a reader who knows nothing.
2. Identify the ONE question the research answers.
3. Draft 3–6 numbered sub-questions that fully cover it.
4. Add include/avoid constraints + the per-finding output format.
5. Compress to one clean paragraph. Cut filler.

## Template

> [For a reader with zero prior knowledge: in 1–2 plain-English sentences, what the project/product is, why it exists, and the current situation.] Research [TOPIC + key identifying facts] to answer one question: [THE QUESTION] — for [DECISION / END USE]. Find: (1) …; (2) …; (3) …; (4) …. [Constraints: include X, avoid Y.] Prefer primary sources; treat forums/social as weak signal only; if sources conflict, separate fact from inference and flag what needs verification. Don't stop at the first plausible answer: corroborate each key claim with multiple independent primary sources where they exist (and say so explicitly where they don't), continuing until every numbered question is covered to that bar. Before finishing, do a self-critique pass — list gaps, contradictions, and any single-source claims, then run another round of searches to close them, repeating until clean. For each point, give the source link, the specific claim, and a one-line "why it matters". No marketing fluff — verifiable, citable facts only. Output everything into a single detailed markdown file.

## Executing the prompt (this harness)

| Need | Surface |
|---|---|
| arXiv intake | `harness-scan --days N [--x] [--json]` |
| Deep cited digest | OpenCode `/research <prompt>` (`deep-researcher`) or `o delegate --profile reasoning` |
| Paper index (MCP connected) | `firecrawl_research_search_papers` → `firecrawl_research_read_paper` |
| X / web | `xsearch "<query>"` — verify URLs before trusting |

Tier-2 verification (CLAUDE.md): before dispatch, turn the numbered sub-questions into a boolean checklist (+ source link per claim, + gap round present). Grade the report ONLY against it (`Pass/Fail + line ref` per item, hard token cap); never re-do the research. One bounded repair round on Fail, then escalate.
