# Harness-design improvements from X + arXiv (last 7 days) — 2026-06-06

Scope: fresh (≤7d, since 2026-05-30) patterns for **harness design**, mapped onto this
repo's three pillars — **orchestration/verification**, **memory governance**, **tool/context
discipline**. Sources: `xsearch` (xAI/Grok over X+web) + direct arXiv API (`export.arxiv.org`).
arXiv IDs marked ✅ were abstract-verified directly; X claims are leads, not facts.

## TL;DR — what to steal
1. **Cross-model critic is now empirically grounded, and self-review is empirically broken** —
   the asymmetry is driven by *role labels*. → harden the verification policy (Tier 2 critic).
2. **More agents usually *lose* to one good agent**; only *runtime-generated* (dynamic) workflows
   win. → our `/fanout` is the right shape *because* it's task-decomposed, not a fixed team;
   add an explicit "default single-agent, fan out only on truly independent chunks" guard.
3. **Memory contradiction-resolution is a typed algebra** (bitemporal: valid-time vs
   transaction-time, 4 operators, keep the loser as an audit row). → upgrade our supersede rule
   from implicit last-writer-wins to a typed `resolution_rule` + valid-time stamps.
4. **Too many tools/skills *reduce* reliability** (~90% token cut, same success, when filtered to
   the minimal next-step frontier). → counter the "954-skill library" hype; keep worker tool/model
   menus tight.
5. **Harness flaws are a diagnosable category** across 7 layers (ETCLOVG). → adopt the layer
   taxonomy as a failure-attribution checklist for delegated-task post-mortems.

---

## Pillar 1 — Orchestration & verification

### The Self-Correction Illusion (arXiv:2606.05976 ✅, 2026-06-04)
- **Claim:** LLMs correct *others'* errors but not their *own* — and the cause is the chat-template
  **role label**, not reasoning ability. Same byte-identical wrong claim (SHA-256 verified)
  relabeled from the model's own `<thought>` to a `user`/`tool`/`<memory>` role raised explicit
  correction rates **+23–93 percentage points** (10/13 model-domain cells significant at p<0.001).
- **Why it matters for us:** direct empirical backing for our two-tier policy's "different worker
  as critic" rule — *and* a sharper mechanism. **Actionables:**
  - Never rely on a worker self-reviewing its own output. Always use a **different** model.
  - When handing output to a critic, **frame it as another agent's / the user's output** (a
    `user`/`tool`/`memory`-role artifact to be checked), never as "your previous answer."
    The framing alone buys most of the +23–93pp.
  - Same trick for memory contradiction-checks: present the candidate fact as external evidence.

### Do More Agents Help? Protocol-Aligned Evaluation of Agent Workflows (arXiv:2606.05670 ✅, 06-04)
- **Claim:** Under matched tools/benchmarks/logging, **5 of 6 multi-agent systems trailed a single
  agent by 2.56–11.29 pp** and sat at worse cost/accuracy points. The exception: a **Claude-Code-
  style *runtime-generated* workflow hit 66.72% on GAIA, beating fixed MAS by >20 pts.**
- **Why it matters:** validates that *static* agent teams are usually a tax; *dynamic* task
  decomposition is the win. Our `/fanout` decomposes per-task (dynamic) → correct shape.
  **Actionable:** add a guard to the delegate/keep doctrine — **default to single-agent (Opus);
  fan out only when chunks are genuinely independent**, not reflexively. Adding agents ≠ better.

### HarnessFix: Diagnosing & Repairing Harness Flaws (arXiv:2606.06324 ✅, 06-04)
- **Claim:** "Harness flaws" = defects in the support layers around the model. Categorized across
  **ETCLOVG layers: Execution, Tool, Context, Lifecycle, Observability, Verification, Governance.**
  Method: trace → typed IR (HTIR) → step-level failure attribution → flaw-specific patch.
  **+15.2–50.0%** on SWE-Bench / Terminal-Bench / GAIA / AppWorld.
- **Actionable:** when a delegated task fails, attribute the failure to an **ETCLOVG layer** before
  blaming the model. Good skeleton for a `/retro`-style harness audit (see proposed checklist).

### Related leads (not deep-read)
- Retrospective Harness Optimization via self-preference over rollouts (2606.05922 ✅) — improve the
  harness from its own trajectory logs.
- The Self-Correction Illusion's cousin in MARL: Critic-Guided Heterogeneous reasoning (2606.05704).
- X: Anthropic's **native dynamic workflows / "ultracode"** (claude.com blog, 2026-06-02) ships the
  patterns we hand-rolled — classify-and-act, fan-out-and-synthesize, adversarial verification,
  generate-and-filter, tournament judging, loop-until-done, token budgets, worktree isolation,
  save-as-skill. Our angle: orchestrate with the *native* harness but route leaf agents to cheap
  **Ollama workers**. (Also: @SethGammon "Citadel" orchestration layer; @DanKornas CC v2.1.88
  source teardown; awesome-agent-harness list. All X — treat as leads.)

## Pillar 2 — Memory governance

### TOKI: Bitemporal Operator Algebra for Contradiction Resolution (arXiv:2606.06240 ✅, 06-04)
- **Claim:** Treats memory contradiction as **write-time concurrency control**. Types four
  production heuristics as one operator family over a **dual-row (bitemporal) schema**:
  - **valid time** (when the fact is true in the world) vs **transaction time** (when it was
    recorded) — tracked separately.
  - Operators: **last-writer-wins · evidence-weighted merge · await-confirmation · per-rule policy.**
  - Each operator has an *isolation precondition* + *provenance annotation*; the losing fact is
    preserved in an **audit row**, not deleted.
- **Why it matters:** our memory protocol already does "supersede, don't append" + keeps superseded
  files — that's *implicit last-writer-wins*. TOKI says name the operator and split the two clocks.
  **Actionables (upgrade the existing protocol):**
  - Add frontmatter: `valid_from` / `valid_to` (valid time) **distinct from** `created` /
    `last_verified` (transaction time). A fact can be recorded today but valid only until next
    release — decay should key off valid-time, not just age.
  - On supersede, record **`resolution_rule: last-writer-wins | evidence-merge | await-confirm |
    per-rule`** so the choice is auditable. Our critic-gate ≈ the **await-confirmation** operator.
  - Keep the superseded file as the audit row (already done) but link `supersedes`/`superseded_by`
    bidirectionally + stamp the rule.

### Supporting memory papers (06-04 cluster, leads)
- EMBER: budgeted evidence retention (2606.05894) · AdaMEM: test-time adaptive memory (2606.05684)
  · "Memory is Reconstructed, Not Retrieved" graph memory (2606.06036) · "Beyond Semantic
  Organization: Memory as Execution-State Management" (2606.06090) · "When Should Memory Stay
  Silent" memory-use boundaries (2606.06055) · Beyond Similarity: trustworthy memory search
  (2606.06054). Theme: budget + know-when-NOT-to-recall — reinforces our TTL/decay + thin index.

## Pillar 3 — Tool / context discipline

### ToolChoiceConfusion: Causal Minimal Tool Filtering (arXiv:2606.06284 ✅, 06-04)
- **Claim:** "Larger tool menus reduce reliability and efficiency" (wrong-tool calls, premature
  actions, token cost). Semantic relevance ≠ should-be-available-now. CMTF exposes only the
  **minimal next-step tool frontier** via precondition-effect contracts: **100 tools/step → 1,
  ~90% token cut, success matched.**
- **Why it matters:** direct counter-signal to the X "954 ready-to-use skills" hype (@iqtauhid).
  More skills/tools is a *liability*, not an asset. **Actionables:**
  - Keep `ollama-worker` prompts tool-light — hand each worker only the files/tools it needs.
  - Don't bulk-install giant skill libraries; expose the minimal relevant set per task.
  - Mirrors our existing "HEAVY frontier models only, no small-model sprawl" curation instinct.

### Other code-agent benchmarks worth tracking (leads)
- Asuka-Bench: code agents on **underspecified intent** + multi-round refinement (2606.05920) —
  ties to our iron rule (can't spec it → do it in Opus). · SmellBench refactoring (2606.05574) ·
  TensorBench (2606.05570) · "Coding with Enemy": can humans detect agent sabotage (2606.05647) —
  argues for verification you can't eyeball.

---

## Concrete improvements proposed for this repo (prioritized)
1. **Verification (CLAUDE.md, high value, low risk):** add the role-label rule + cite 2606.05976 to
   Tier-2 critic ("different model, framed as external output, never self-review"); add the
   "default single-agent, fan out only on independent chunks" guard citing 2606.05670.
2. **Memory (memory-protocol-upgrade.md + CLAUDE.md + mem-audit):** add bitemporal `valid_from/
   valid_to` + `resolution_rule` frontmatter (TOKI 2606.06240); teach `mem-audit` to flag missing
   valid-time + decay off valid-time.
3. **Tool discipline (ollama-worker.md + CLAUDE.md):** add CMTF principle — minimal tool/skill
   frontier per worker; explicit "don't install giant skill libraries" note (2606.06284).
4. **Harness self-audit (new knowledge/ checklist or bin/ script):** ETCLOVG failure-attribution
   template for post-mortems on delegated tasks (HarnessFix 2606.06324).
5. **Native-workflow bridge (CLAUDE.md note):** position Ollama workers as cheap leaf agents under
   the harness's native dynamic workflows ("ultracode"), not a competitor to them.

## Caveats
- 2606.* IDs were pulled live from the arXiv API and the five starred ones were abstract-verified;
  the rest are titles from the same query (real, not deep-read).
- All X items are leads (growth/influencer noise is high in this niche) — the Anthropic dynamic-
  workflows blog and the arXiv papers are the trustworthy anchors.
</content>
</invoke>
