# Harness corpus, Ollama worker path, and Purrly Team Mode audit

_Verified 2026-07-17. Local corpus: `../harness/scaffold-corpus`; practitioner
archive: `~/Downloads/T-1940.zip`._

## Decision

Do not choose a harness by traction and do not turn Purrly's human-team metaphor
into permanent agent bureaucracy. The useful pattern is compositional:

1. keep `oll` as the minimal, stateless primitive for bounded work;
2. keep OpenCode/`occ` for tool-using Ollama work while measuring its reliability and
   native compaction before changing defaults;
3. run Pi as a commit-pinned, Ollama-only challenger, not a replacement;
4. route ticket execution and review from evidence, dependency, and risk instead of
   convening every role for every task.

The corpus paper's central warning matters here: scaffolds occupy spectra and compose
loop primitives; it does not causally establish that a mechanism improves outcomes.
Its authors explicitly call for controlled component experiments with the model held
constant. [Inside the Scaffold (arXiv:2604.03515)](https://arxiv.org/abs/2604.03515)

Tags below separate **[source]** observations from **[inference]** decisions for this
project. The 13 paper rows refer to the pinned commits in the corpus README; local
HEADs were cloned on 2026-07-17 and may have moved. `grok-cli` is a current extra not
covered by the paper.

## What each harness teaches us

| Harness | Observed mechanism and exact local evidence | Transferable lesson | Cost / adoption gate | Verdict |
|---|---|---|---|---|
| OpenCode | **[source]** Sequential tool loop, dynamic tools/plugins/MCP, SQLite typed session parts, role-based models, and prune-then-summarize compaction (`opencode`, paper pin `f54abe58cf`; current `packages/opencode/src/session/{compaction,message-v2}.ts`). | A resumable tool harness should expose structured state and make context a policy surface. | Our deployed 1.17.9 has no explicit compaction policy and `occ` hides most lifecycle data. Advance changes only through identical-task telemetry. | **Adapt; current tool harness.** |
| Gemini CLI | **[source]** Dynamic tool discovery, layered model routing, and a post-summary “Probe” turn intended to detect information loss (paper pin `dd8d4c98b3`). | Compaction deserves verification, not blind trust. | A probe costs another call and an LLM cannot prove its own summary. Gate with deterministic retrieval/instruction canaries. | **Adapt the canary, not the router chain.** |
| Codex CLI | **[source]** Per-turn tool rebuild, OS sandboxing, safety routing, and background extraction/consolidation of memory (paper pin `9dba7337f2`). | Tool availability, safety, and durable memory are separate policies; do not solve them with one giant prompt. | Dynamic tools can churn and extracted memory can be wrong. Keep deterministic permission and governed-memory gates. | **Adopt the separation; already aligned.** |
| OpenHands | **[source]** Immutable event stream with views derived through condensation markers and container isolation (paper pin `922e3a2431`). | A normalized event envelope makes replay, failure attribution, and alternate context views possible. | A full event-sourced platform is heavy. First gate is attributable JSONL for worker calls, not platform migration. | **Adapt event semantics.** |
| Cline | **[source]** Shadow-git checkpoints, runtime MCP changes, and LLM-writable `.clinerules` (paper pin `71e312e92a`). | Checkpoint before risky mutations; runtime extension is useful. | Self-written rules create instruction drift. Any durable belief change needs our existing governance/critic path. | **Adapt checkpoints; reject ungated rules.** |
| Aider | **[source]** Human-driven outer loop, PageRank/tree-sitter repo map, multiple parsed edit formats, recursive summarization (paper pin `861a1e4d15`). | For large repositories, a compact structural map can beat dumping files into context; a human-driven boundary remains valuable. | Indexing and identifier heuristics can mis-rank. Trial only on repos where navigation dominates and measure localization/token cost. | **Adapt repo-map retrieval selectively.** |
| SWE-agent | **[source]** Minimal ACI bundles plus composable first/last-observation processors and bounded retry (paper pin `e72a7e4660`). | Context and tool surfaces should be intentionally small and task-shaped. | Fixed processors can discard decisive middle evidence. Contract retrieval of protected facts before adoption. | **Adopt the minimal-frontier principle.** |
| mini-swe-agent | **[source]** One bash tool and a very small imperative loop; raw history grows until overflow (paper pin `6f1b196616`). | Keep a simple control treatment: complexity must beat this baseline, not merely look sophisticated. | No compaction and little safety make it a poor long-running production harness. | **Keep as experimental control, not host.** |
| AutoCodeRover | **[source]** Search/localize then patch, AST-aware search, optional spectrum-based fault localization, phase-scoped tools (paper pin `585d3e639a`). | Evidence-first localization is a better “Framewright” than generic role-play for bug tasks. | SBFL needs runnable tests and much of the AST path is Python-specific. Activate from task evidence, not globally. | **Adapt phase scoping/localization.** |
| Agentless | **[source]** Fixed JSONL pipeline and file→symbol→line localization with resumable intermediate artifacts (paper pin `5ce5888b9f`). | Deterministic staged artifacts are debuggable and resumable; not every task needs a conversational loop. | Fixed stages cannot react fluidly to unexpected evidence. Use for repeatable pipelines with explicit stage contracts. | **Adopt artifacted stages selectively.** |
| Prometheus | **[source]** Graph-controlled phases, per-node message/tool scopes, dual-model routing, tree-sitter knowledge graph (paper pin `b1c722be02`). | Different phases should see different tools and context. | LangGraph + Neo4j + multiple stores are operational weight without demonstrated need here. | **Adapt phase scoping; reject graph infrastructure by default.** |
| Moatless Tools | **[source]** MCTS, per-node context, value function, semantic index, up to 37 action classes (paper pin `011ead57a5`). | Search breadth is useful only when a trustworthy reward makes alternatives cheaply comparable. | High calls, large tool surface, proxy/reward error. Require an objective scorer and a task-class win before trial. | **Reject as default.** |
| DARS-Agent | **[source]** Depth-first action re-sampling with environment replay and an LLM critic choosing branches (paper pin `eab35168a9`). | Backtracking can escape a failed path; a critic is corroboration, not authority. | Replay is expensive and the critic shares LLM blind spots. Our deterministic contract remains selector. | **Reject tree search; retain bounded repair/critic ideas.** |
| grok-cli | **[source]** Current extra provides JSONL headless events, persisted sessions/usage, context checkpoints, foreground agents plus read-only background delegations with completion notifications (`grok-cli` HEAD `fb97af8`; `src/{headless/output,agent/compaction,agent/delegations}.ts`). | Background work needs completion events and saved output, not polling lore; verification should emit evidence. | Provider is currently unavailable to this project and its default subagent breadth is larger than our minimal frontier. | **Adapt event/notification semantics only.** |
| Corpus meta-analysis | **[source]** Five loop primitives are composed; mechanisms converge around basic tool capabilities/editing/isolation but diverge on context, state, and routing. | There is no singular winning harness. Isolate one architectural variable at a time. | Qualitative, pinned snapshot; system/model confounds remain. Gate every transplant with a same-model component experiment. | **Adopt the experimental frame.** |

## `oll` vs OpenCode/`occ` vs Pi

| Dimension | Direct `oll` | OpenCode / `occ` | Pi challenger |
|---|---|---|---|
| Reliability | **[source]** One HTTP request with a 300s bound; errors surface; no tool loop. | **[source]** Full tool loop, but `occ` exists because OpenCode 1.17.x has intermittent startup hangs; it bounds/reaps retries. | **[source]** Full agent/session runtime; reliability on our Ollama models is unmeasured. |
| Context | **[source]** Stateless per call; the dispatcher controls exactly what is sent. | **[source]** Native pruning, summarization, recent-turn retention, and compaction hooks exist; our config sets no policy. | **[source]** Native structured compaction preserves a recent tail and supports custom hooks. [Pi compaction](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/compaction.md) |
| Tools/extensions | None by design—an advantage for bounded work. | Plugins, MCP, dynamic tools, specialized agents. | Small core with extension APIs for tools, permissions, subagents, and compaction. [Pi coding-agent README](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md) |
| Structured chaining | Caller owns it. | Agent loop and subtask parts; `occ` currently returns human-oriented output. | Sessions/branches/RPC and community subagent extensions; JSON validity in third-party chains remains a trial question. |
| Observability | Local usage JSONL records model/tokens, but no normalized lifecycle envelope. | SQLite holds sessions/tokens/cost; `occ` does not expose a stable normalized run record. | Session JSONL and extension events are promising; attribution must be tested, not inferred. |
| Maintenance | Lowest; one small Python primitive. | Already installed and integrated with MCP/config; medium operational surface. | New Node runtime/config/package surface plus third-party extensions. Pin commits and keep the trial isolated. |
| Ollama economics | Excellent for small bounded prompts; caller must avoid giant stdin. | Native compaction may reduce repeated context, but no measured threshold/cost result yet. | Purrly's trimmer is explicitly designed for request/GPU-time billing: it drops old whole turns, protects dispatch/pinned surfaces, and detects repeated tool calls. It explicitly warns that hard trimming breaks cache economics on token-priced providers. [Purrly trimmer](https://github.com/PurrlyDigital/pi-ollama-context-trimmer) |

**Recommendation [inference]:** “improve before replace.” Preserve `oll`; expose and
measure worker lifecycle first; compare OpenCode native compaction against a pinned Pi
trial with identical models/tasks. Pi wins only on non-inferior contract success plus a
material reliability/context-cost advantage. Never install the Ollama-specific hard
trimmer for Anthropic/OpenAI traffic.

## What survives from Purrly Team Mode

| Practitioner claim | Literature/corpus read | Decision for orchestratormaxxing |
|---|---|---|
| Keep active context well below the advertised maximum. | **Supported direction, unproven thresholds.** Long-context models can miss information in the middle and struggle to follow multiple interdependent threads. Neither paper establishes universal 100k/150k/234k/250k cutoffs. [Lost in the Middle](https://arxiv.org/abs/2307.03172), [Needle Threading](https://arxiv.org/html/2411.05000) | Add model/task-specific canaries; do not encode her observed numbers as doctrine. |
| Reinject PM identity and last five tickets after compaction. | **Mixed.** Reinjecting the current objective, constraints, decisions, and open work is sensible; “personality” is not the load-bearing information and verbatim reinjection has token/cache cost. | Trial a compact charter + current ticket/last relevant decisions, with deterministic recall checks. |
| Define roles by how they fit the process, not personality. | **Supported as a design principle.** Narrow tool/skill frontiers reduce confusion, while our corpus shows phase-scoped roles/tools. [ToolChoiceConfusion](https://arxiv.org/abs/2606.06284) | Adopt job-to-be-done contracts and minimal tools; avoid simulated seniority/persona theater. |
| Framewright gathers evidence before scope. | **Supported for relevant task classes.** Hierarchical localization, AST search, repo maps, and earlier localization are recurring mechanisms. | Make evidence collection a phase, activated by task type; do not create a permanent agent. |
| Skeptical Shears protects scope and decides whether to convene a panel. | **Useful function, wrong authority.** External critique can expose blind spots, while intrinsic self-correction is unreliable. An LLM critic must not have final authority. [Intrinsic self-correction](https://arxiv.org/abs/2310.01798) | Encode an adversarial scope checklist; Root/human/deterministic gates decide. |
| Optional frontend/backend panels. | **Supported only when dynamically routed.** Controlled evidence favors generated, task-decomposed workflows over static teams; generic extra agents often add cost without gains. [Do More Agents Help?](https://arxiv.org/abs/2606.05670) | Activate reviewers from explicit frontend/backend/security/accessibility risk. No standing panel. |
| Up to three deliberation rounds. | **Reasonable cap, not a target.** The archive correctly warns about self-judge bias, over-optimization, and shrinking gains; same-model debate is corroboration, not proof. | Default one round; stop when no new contract-relevant evidence appears; hard cap three. |
| Reviewers change prior work with 50%→25%→10% probability. | **Unsupported literally.** Random change rates are not quality signals and can reward churn. | Replace probability with a convergence rule: edit only for a cited contract violation or new evidence. |
| Every ticket traverses senior, implementation, feature QA, security QA, accessibility QA, senior review. | **Over-process as a default.** Gates have value when risk justifies their verification cost. | Use deterministic risk routing. Security/accessibility/external/irreversible work escalates; low-risk work stays single-path. |
| Sycophancy and conversational attachment are risks. | **Judge bias is supported; attachment claim here is practitioner experience, not evaluated by this coding corpus.** MT-Bench documents position, verbosity, and self-enhancement biases in LLM judges. [MT-Bench](https://arxiv.org/abs/2306.05685) | Keep agents tool-like: factual process roles, explicit disagreement, no relationship/personality optimization. |

The T-1940 archive's strongest usable conclusion is narrower than “four writers in a
trenchcoat”: without a reference output, externalize a rubric, separate generator and
critic when stakes justify it, compare concrete candidates, cap iterations, and retain
the human gate. Intrinsic self-review alone is the rejected configuration.

## Three bets

| Rank | Bet | Expected value / evidence | Reversibility | Verification cost |
|---|---|---|---|---|
| 1 | **Worker-path measurement before harness migration.** Normalize fake/live adapter results, usage, timeout, schema, and lifecycle events; compare `oll`, `occ`, then Pi on identical tasks. | High: directly attacks unknown reliability/context cost and follows the taxonomy's requested causal design. | High: additive experiment; no host change. | Low initially; live long-context cases are opt-in. |
| 2 | **Purrly-lite deterministic ticket routing.** Reject missing ACs; fan out only independent, disjoint chunks; activate specialist review only from explicit domains/risks; one round by default, three maximum. | Medium-high: captures evidence-first scoping and conditional teams without static-team overhead. | High: proposal-only CLI, no execution power. | Low: truth-table contract. |
| 3 | **Pinned Pi + context-compaction canary.** Compare raw, OpenCode-native, and Pi-native/Purrly-trimmed context on retrieval, instruction, and code contracts at increasing sizes. | Potentially high for Ollama request economics, but evidence is currently mechanism-only. | High if read-only/containerized and commit-pinned. | Medium; use a small staged sample before large contexts. |

### Explicit non-bets

- No OpenCode replacement based on Pi traction, Discord reports, or a single operator's
  30k-request outcome.
- No universal 100k/150k/234k compaction threshold.
- No eight-gate default SDLC, permanent role pack, randomized reviewer churn, MCTS,
  Neo4j, or embedding index without a task-class ceiling and measurable win.
- No same-agent self-review as proof, no LLM critic with final authority, and no hard
  context trimming on providers whose economics depend on prompt caching.

## Implemented experiment surface

- `bin/worker-path-bench` + `experiments/worker-path-bench/`: fake-adapter-safe
  normalization and scoring. Live OpenCode/Pi adapters and long-context cases remain
  deliberately unshipped until their exact commands, versions, and spend are reviewed.
- `bin/ticket-route` + `knowledge/ticket-contract.schema.json`: proposal-only
  Purrly-lite routing with deterministic fixtures under `tests/ticket-route/`.

These are bets, not claimed wins. Promotion into default execution requires the gates
above and deterministic `harness-verify` coverage.
