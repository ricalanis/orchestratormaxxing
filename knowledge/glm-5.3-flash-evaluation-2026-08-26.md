# GLM-5.3-Flash vs GLM-5.2 — measured on Ollama Cloud, 2026-08-26

**Decision: do NOT swap `reasoning` from `glm-5.2` to `glm-5.3-flash` today.**
Add it as a distinct profile, keep throughput-bound paths on the incumbents, and
re-measure serving before any swap. The blocker is *serving speed and its
instability*, not capability — capability shows no regression and the vendor
claims large gains. The swap itself is SELECT (load-bearing doctrine +
`install.sh` surface) and is **not** made here.

> **Confidence.** **Capability: this eval cannot rank these three models.** All
> separation came from one task, and once `reasoning_effort` is set every model
> passes it (§3e) — the pass-rate column measured verbosity against a token cap.
> Speed: measured, but on a hours-old deployment with a 7.8× spread — provisional.
> Quota class: provider-documented, high confidence, and the one axis that does
> separate them (Extra High / High / Medium — §2). Every claim below was put to a
> cross-family refutation critic; §9 records what survived.

## 0. Why this was even a question

`glm-5.2` is the harness's *explicit complex-reasoning escalation* — `bin/oll`
`REASONING_MODEL`, the `reasoning` stateful profile (`glm-coder`), a member of
`oll-council`'s default panel and `gauntlet-judge`'s family-collision list — and
it is also the **live production model of the Hermes orchestrator**:
`orchestrator/dashboard/digestion.py::DIGEST_MODEL` and
`orchestrator/dashboard/whatsapp_classify.py::CLASSIFY_MODEL`, both
env-overridable (`DIGESTION_MODEL`, `WHATSAPP_CLASSIFY_MODEL`).

Z.ai shipped `glm-5.3-flash` on 2026-08-26 and Ollama Cloud is serving it the
same day.

## 1. What the vendor claims

320B total / 18B active MoE, 1,048,576-token context, 128K max output, natively
multimodal (image + video), MIT weights on Hugging Face. Hybrid KDA linear +
NoPE sparse MLA attention with IndexPool → Z.ai reports ~3.0× less attention
compute and a 4.4× smaller KV cache **versus GLM-5.3** (not versus 5.2). It ran
the prior week anonymously as `ox-alpha` **on OpenCode** and OpenRouter, served
on Chinese accelerators.

Z.ai's own comparison table — it beats GLM-5.2 on **all 8** listed
coding/agentic benchmarks:

| Benchmark | 5.3-Flash | 5.2 | Δ |
|---|---|---|---|
| Terminal Bench 2.1 | 84.3 | 81.0 | +3.3 |
| DeepSWE v1.1 | 63.4 | 46.2 | +17.2 |
| NL2Repo | 56.3 | 48.9 | +7.4 |
| Toolathlon Verified | 78.4 | 59.9 | +18.5 |
| AutomationBench v1.0.6 | 48.8 | 26.2 | +22.6 |
| Agents' Last Exam | 26.3 | 20.4 | +5.9 |
| HLE w/ Tools | 55.3 | 54.7 | +0.6 |
| GDPval-AA v2 | 1773 | 1504 | +269 |

Independent: Artificial Analysis scores it **57** on Intelligence Index v4.1.1
at $0.045/task, and measures **48.7 output tok/s with 1.52 s TTFT on Z.ai's own
API** — i.e. *the vendor's own serving is already slow*, which matters below.
Standard API price $0.15/M in, $0.50/M out.

**Vendor claims are corroboration, not acceptance** — the numbers below are ours.

## 2. Ollama Cloud quota class (the axis that actually bills us)

The subscription meters *usage class*, not $/token, so this is the real cost
signal:

| | deepseek-v4-pro:0813 | glm-5.2 | glm-5.3-flash |
|---|---|---|---|
| Ollama usage label | **Extra High Usage** | **High Usage** | **Medium Usage** |
| Params | 1.65T / 49B active | 756B | 320B-A18B |
| Context (library page) | 1M | 976K | 1M |
| Capabilities | tools, thinking (3 modes) | tools, thinking | tools, thinking, **vision** |

**The harness's volume default is the most quota-expensive model in the pool.**
`bin/oll::DEFAULT_MODEL` is `deepseek-v4-pro:0813` — Extra High usage — and it
also emitted the *most* completion tokens per task here (740 median). Most tokens
× most expensive class is the worst combination for a subscription default, and
nothing in this eval showed a capability reason for it (§3d, §3e). That is a
separate open question from the GLM question and is **not** settled here.

A drop from High to Medium is a direct quota win. Relevant history:
`orchestrator/docs/postmortem-2026-07-09-glm-quota-exhaustion.md` — an
autonomous batch of full `glm-5.2` sessions hit an account-level HTTP 429 and
took GLM away from every Hermes worker for a rolling window.

## 3. Measured here — 36 streamed calls, temp 0, 3 reps × 4 task classes

Script + raw records: `experiments/` not committed; run recorded in this note.
Task classes mirror real harness usage: 5-bullet digest (Tier-2), first-pass
code review, bounded logic reasoning, strict-JSON extraction.

### 3a. A shared-infrastructure artifact, not a model property — TTFT

A ~19.7 s time-to-first-token cluster showed up on **all three** models at almost
exactly the same magnitude (glm-5.2 3/12 calls, glm-5.3-flash 6/12,
deepseek-v4-pro 4/12). Because it is model-independent it cannot be attributed to
any one model — the same signal-vs-artifact discipline as
`knowledge/signal-vs-artifact-2026-07-19.md`. **The mechanism is not established**
(account concurrency queue, cold start and shared-infra stalls are all
consistent with the data), so decode rate is reported with TTFT excluded rather
than explained away. Naive `tokens ÷ wall-clock` conflates the two and
understates whichever model happened to stall.

### 3b. Decode rate (TTFT excluded) — and its variance is the finding

All 12 per-call decode rates, tok/s:

| model | full distribution | p50 | spread |
|---|---|---|---|
| deepseek-v4-pro:0813 | 116, 122, 140, 161, 182, 182, 218, 229, 234, 243, 253, 267 | **200.4** | 2.3× |
| glm-5.2 | 146, 146, 147, 177, 180, 182, 185, 196, 202, 212, 213, 230 | **183.5** | 1.6× |
| glm-5.3-flash | 15.7, 15.7, 15.9, 19.0, 21.4, 21.7, 22.2, 39.7, 59.4, 78.7, 105.5, 122.8 | **21.9** | **7.8×** |

The two incumbents are *stable*. GLM-5.3-Flash is not: it spans nearly an order of
magnitude, and the **delegated run in §4 independently hit ~99 tok/s** (523 output
tokens in 5.3 s of wall clock, `oll` process startup included). So:

- **Supported:** on median, GLM-5.3-Flash decoded ~8× slower than GLM-5.2 during
  this window, and its rate is far less predictable than either incumbent's.
- **NOT supported:** that ~8× is the model's steady-state speed. A separate run
  the same hour contradicts it. This is day-one serving behaviour on a
  hours-old deployment; treating the median as a settled property would be
  exactly the artifact-as-signal error above.

Corroborating: Artificial Analysis measures 48.7 tok/s on Z.ai's *own* API — so
the architecture is genuinely slow-decoding, but not as slow as our p50, which
means our p50 is partly infrastructure.

### 3c. Wall time and tokens, unqueued calls only (TTFT < 15 s)

| model | s/task (p50) | completion tokens/task (p50) |
|---|---|---|
| glm-5.2 | **3.7 s** | 448 |
| deepseek-v4-pro:0813 | **6.7 s** | 818 |
| glm-5.3-flash | **26.2 s** | 505 |

Across all 36 calls: 358 billed completion tokens/task median for
GLM-5.3-Flash vs 724 (5.2) and 740 (V4 Pro) — **about half**.

Caveat, and it is a real one: `completion_tokens` **includes hidden reasoning
tokens**, and GLM-5.2's count is inflated by the failure mode in §3d (whole
budget spent thinking, no answer emitted). So this is not a clean
answer-length comparison. It is, however, the **quota-relevant** quantity —
Ollama Cloud bills the tokens, not the useful ones — which is the axis this
harness actually cares about.

### 3d. Deterministic contract pass

| model | passed |
|---|---|
| glm-5.3-flash | **11/12** |
| deepseek-v4-pro:0813 | 10/12 |
| glm-5.2 | 9/12 |

Per-task, the spread is narrower than the totals suggest — **every difference
came from one task**:

| task | glm-5.2 | glm-5.3-flash | v4-pro |
|---|---|---|---|
| digest | 3/3 | 3/3 | 3/3 |
| **review** | **0/3** | **2/3** | **1/3** |
| reason | 3/3 | 3/3 | 3/3 |
| extract | 3/3 | 3/3 | 3/3 |

And 3 of the 6 failures were **budget exhaustion against my 4000-token cap**, not
wrong answers — see §3e, which retires this table as a capability signal
altogether.

**What this supports: no capability regression.** What it does **not** support is
"more capable" — n=12 per model, a 2-record gap, no significance. Combined with
the vendor table (§1, not independent) the honest reading is: *nothing here
suggests GLM-5.3-Flash is worse at these task classes, and the vendor claims
substantial gains that we have not independently reproduced.*

Two contract-integrity notes, both required by Tier-1c doctrine:

- **The first review contract was defective and is disclosed as such.** It
  required the literal substring `retry`; GLM-5.3-Flash's *correct* answer wrote
  "retries in a tight loop" and was scored fail. The contract was rewritten to
  anchor on the planted defect (a bare `except: pass` that silently swallows every
  exception) and re-run **against the stored outputs** — no model was re-invoked,
  so this changed scoring only, never measurement. v1→v2 flipped 3 records.
- **GLM-5.2 failed the review task 3/3 by budget, not by content.** At a
  4000-token cap it spent the entire budget on hidden reasoning and emitted
  *zero* assistant text in every rep. A first probe at 700 tokens produced the
  same thing on the digest task (3146 reasoning chars, empty content).

That is not a benchmark curiosity — it is a documented live Hermes failure mode:

> `digestion.py:411` — *"glm-5.2 razona antes de contestar y ese razonamiento
> SALE del mismo presupuesto de tokens. Con 4096 una junta real (130 oraciones)
> se quedaba sin aire y devolvía vacío"* → `DIGEST_MAX_TOKENS=16384`, plus
> `_EMPTY_WORKER_MARKERS` and `_looks_truncated()` written specifically to stop
> the gate from punishing an event for the model's budget overrun.
>
> `digestion.py:80` — *"9 s con un contrato de forma explícito, 53 s cuando el
> modelo se pone a razonar de más."*

**DeepSeek V4 Pro has the same pathology.** The cross-family critic call for
this very note came back `provider_empty` at 2000 tokens — 971 in, 2000 out,
no assistant text — and only produced an answer at 12000.

### 3e. The separation was configuration, not capability — `reasoning_effort` works

Ollama Cloud **honors OpenAI's `reasoning_effort`** on every thinking model in
the pool. `"think": false` is silently ignored; only `reasoning_effort` does
anything. Measured 2026-08-26 on a trivial prompt: `deepseek-v4-pro` went 82 → 6
completion tokens with 294 → 0 reasoning characters at `effort=none`.

Re-running **the identical review task at the identical 4000-token cap**, the
only task that separated the three models:

| model | default | `effort=low` | `effort=none` |
|---|---|---|---|
| glm-5.2 | **0/3, budget exhausted** | **pass**, 936 tok, 8.8 s | **pass**, 187 tok, 4.6 s |
| deepseek-v4-pro:0813 | 1/3 | fail, 4000 tok (still capped) | **pass**, 260 tok, 2.3 s |
| glm-5.3-flash | 2/3 | **pass**, 1191 tok, 15.9 s | **pass**, 582 tok, 6.8 s |

**GLM-5.2 goes 0/3 → pass and 4000 → 187 tokens, a 21× reduction, on the same
prompt and the same cap.** At `effort=none` all three pass. The contract-pass
column in §3d therefore measures *verbosity against a fixed cap*, not capability,
and must not be quoted as a capability ranking — including the "11/12 vs 10/12 vs
9/12" line, which is retired by this section.

Two consequences bigger than the model question:

- `bin/oll` **exposed no reasoning-effort flag**, so every `oll`, `/fanout`,
  `/ideas` and `oll-council` call paid an untunable thinking tax against the
  quota. **Fixed 2026-08-26** (`lq-4b3c9d6a`): `--effort none|minimal|low|medium|high|max`
  plus an `OLL_REASONING_EFFORT` env default, validated *before* the auth store
  is read, and **omitted from the payload entirely when unset** so the default
  wire format is byte-identical. Live: `glm-5.2` on a fixed prompt went 160 → 6
  completion tokens. Contract `tests/oll-reasoning-effort/run.sh` (C0–C9),
  proven red, 25/25 mutants killed. `bin/oll-council` builds its own request and
  does **not** inherit — queued as `lq-e3ffec0b`.
- Hermes carries `DIGEST_MAX_TOKENS=16384`, `_looks_truncated()` and
  `_EMPTY_WORKER_MARKERS` **purely to survive that tax**. A pass-through would
  address the cause instead of the symptom.
- Note `deepseek-v4-pro` was the *only* model that still blew the cap at
  `effort=low` — it is the most reasoning-hungry of the three as well as the most
  quota-expensive.

## 4. One real delegated run (contract-at-birth, `/cheap-delegate` protocol)

- Run dir `.results/delegation/dl-glm53-20260826a/` with read-only Root-authored
  `contract.md` + `brief.md` persisted **before** dispatch.
- Lane `oll --model glm-5.3-flash` (response-only, supplied context).
- Class `tier2-digest`: normalize the vendor benchmark table to strict JSON with
  computed deltas.
- Result: **8/8 Root checks green**, 5.3 s wall, 561 in / 523 out tokens, zero
  repair rounds. Ledger row `dl-2337fcc3`.
- `delegate-ledger stats --class tier2-digest` correctly reports
  `authority: advisory` at n=1 — **one green run is not a ranking**, and the tool
  refuses to emit `preferred_lane` below 5 decided attempts.

## 5. Where this model does and does not belong

**Adopt as a new profile — do not swap in place.**

| Use | Verdict | Why |
|---|---|---|
| A new `multimodal` / vision profile | **Yes** | Nothing in the pool has native vision except MiniMax; 5.3-Flash beats Opus 4.8 on OfficeQA Pro (62.4 vs 48.9) and CharXiv/Chartography. Screenshot-driven UI review and PDF/dashboard reading become possible in-lane. |
| Long-horizon agentic / tool loops | **Trial** | Toolathlon 78.4 vs 59.9 and AutomationBench 48.8 vs 26.2 are the largest gaps in the table, and those are exactly `o delegate` territory. Latency is tolerable when a chunk is long anyway. |
| Replacing `glm-5.2` as `REASONING_MODEL` | **Not yet** | Decode is ~8× slower on median *and* 7.8× more variable (§3b); `/ideas`, `oll-council` and `gauntlet-judge` all fan out and would inherit both. Re-measure before swapping — the speed number, not the capability number, is what is unsettled. |
| Hermes `DIGESTION_MODEL` | **Not by default; A/B it** | *Not* a timeout risk — 26 s/task is nowhere near `DIGEST_TIMEOUT=600`. The real cost is **events per tick**: `TICK_BUDGET_SECONDS=480` divided by ~26 s is ~18 events/tick vs ~130 at GLM-5.2's 3.7 s. That is a throughput reduction, not a failure. Since `DIGESTION_MODEL` is an env var, this is a **zero-diff A/B** — run it on a real backlog and count events/tick and dead-letters, don't decide it from this note. |
| Hermes `WHATSAPP_CLASSIFY_MODEL` | **Candidate** | Batched, latency-insensitive, strictly shape-contracted, and 5.3-Flash's token efficiency + Medium usage class directly attack the 2026-07-09 quota incident. |

## 6. Cost of adopting it (why this is SELECT, not an unattended edit)

`grep -rl "glm-5\.2"` returns **~85 tracked files**. `bin/harness-verify` pins
the pool three ways: `bin/oll`'s `NORMAL_WORKER_MODELS` must equal an exact
tuple, `LEGACY_MODEL_REPLACEMENTS` must match exactly, and every pool model
string must literally appear in each of 8 baseline + 7 operational surfaces
(CLAUDE.md, README.md, fanout command/skill, ollama-worker agent for both hosts,
`provider-routing.md`, the architecture SVG, `install.sh`, `bootstrap.sh`,
`oll-council`, `provider-ask`, `warp-ollama`, `win-log`, SETUP.md). Plus
`tests/model-routing-consistency`, `tests/kimi-k3-routing`, `tests/oll-sync`,
`tests/install-opencode-agents`, `tests/gauntlet-judge`, `tests/zed-setup`.

**Doctrine says an unattended round stops at SELECT for doctrine/`install.sh`.**
This note is the PROPOSED diff's justification; the diff needs human sign-off.

## 7. Defect found on the way in (independent of the adoption decision)

`bin/oll-sync::ctx_for()` maps anything starting with `glm-` to **200K context /
32K output**. `glm-5.3-flash` is **1M / 128K**. Running `oll-sync` today
registers the model in OpenCode with a context window 5× too small and an output
cap 4× too small — silently truncating long-context work on the one pool member
whose headline feature is 1M context. `PRETTY` also lacks a display name. This is
a real bug today, whether or not the model is adopted.

## 8. Open questions

1. Is the ~22 tok/s decode a day-one Ollama Cloud provisioning artifact or the
   steady state? Re-run this benchmark in 7–14 days before any swap.
2. Does Ollama Cloud expose the `low/high/max` reasoning-effort control Z.ai
   documents? If effort is pinnable, the thinking tax becomes tunable and the
   whole latency picture changes.
3. Vision through `oll` — the OpenAI-compatible image content-part path is
   untested here; `capabilities/contabilidad/scripts/oll-vision.py` is the
   existing precedent to check against.

## 9. Cross-family critic — what survived

Per Tier-2 doctrine the six load-bearing claims were handed to
`deepseek-v4-pro:0813` (different family from the subject) framed as *another
agent's* work with a refute-or-say-cannot-refute mandate. It refuted **6/6** —
a maximally adversarial read. Root adjudicated each; four changed this note.

| Claim | Critic | Root's adjudication |
|---|---|---|
| 1. The 19.7 s TTFT cluster is a *queueing* artifact | REFUTED — co-occurrence ≠ queueing; cold start equally plausible | **Upheld.** Claim narrowed to "shared-infrastructure, mechanism unestablished" (§3a). The model-independence *is* supported; the mechanism was not. |
| 2. ~8× slower decode than GLM-5.2 | REFUTED — the §4 delegated run implies ~99 tok/s, contradicting the 21.9 p50 | **Upheld, and the strongest hit.** §3b rewritten to publish the full 15.7–122.8 distribution and state explicitly that the median is not a steady-state property. |
| 3. About half the output tokens | REFUTED — confounded by GLM-5.2's hidden-reasoning failure mode | **Partly upheld.** Caveat added; the number is retained because billed completion tokens are the quota-relevant quantity regardless of what they were spent on. |
| 4. At least as capable | REFUTED — 11/12 vs 9/12 at n=12 is not significant | **Upheld.** Downgraded to "no capability regression observed; not powered to show superiority" (§3d). |
| 5. Must NOT go in a 480 s/600 s pipeline | REFUTED — 26 s is far below both budgets | **Upheld — the original argument was simply wrong.** The constraint is events-per-tick throughput, not timeout. §5 rewritten and the recommendation changed to "A/B it via the existing env var". |
| 6. Add-and-wait is the right move | REFUTED — the 7–14 day window is arbitrary | **Rejected as stated, label accepted.** The window is a judgment call, now labelled as one; the underlying reason (a hours-old deployment is not a steady state) stands on §3b's variance. |

**Two agreeing LLMs would still be corroboration, not proof** — and here they did
not agree, which is the more useful outcome. The deterministic contracts and the
delegate-ledger receipt remain the only acceptance gates.

## 10. The lane this eval did NOT measure — and the bigger thing it exposed

The 36-call benchmark went over **raw HTTP**. That is neither production lane:
`oll` adds a system prompt and defaults, and open-model work was consolidated
onto **OpenCode** (`knowledge/opencode-first-class-2026-08-11.md`), where a run
also carries tools, an agent prompt, and a tool loop. Treat §3 as a *provider*
measurement, not a lane measurement.

Checking the consolidated lane produced three findings, in ascending order of
importance.

### 10a. OpenCode has no reasoning-effort control either

Measured 2026-08-26 against the deployed `~/.config/opencode/opencode.json`:
`reasoning_effort` appears **0 times**, `effort` **0 times**, `providerOptions`
**0 times**. The `ollama-cloud` provider's `options` block contains only
`baseURL`; not one of the ten agents (`glm-coder`, `v4-coder`, `kimi-coder`,
`kimi-k3-coder`, `qwen-coder`, `minimax-coder`, the five reviewers) sets
anything but `mode`, `model` and `temperature`. So the thinking tax is unbounded
on the consolidated lane too — the `bin/oll` fix was not redundant, it was
merely *partial*. OpenCode's schema does allow a free-form per-model `options`
object (`ProviderConfig.models.<id>.options` is `{"type": "object"}`), which is
the plausible passthrough hook, but **that is untested here** — do not wire it
without an empirical check that the tokens actually drop.

Note which lane Hermes is on: `digestion.py:677` and `whatsapp_classify.py:202`
both shell out to `_oll_bin()`, i.e. **`oll`, not OpenCode**. So Hermes gets the
`--effort` control immediately, and the consolidation never reached it.

### 10b. The OpenCode model registry under-declares the pool

`bin/oll-sync::ctx_for()` guesses context by family prefix, and the deployed
config has also drifted from that guess:

| model | deployed in OpenCode | `ctx_for` says | actual (Ollama library) |
|---|---|---|---|
| `deepseek-v4-pro:0813` | 200K | 200K | **1M** |
| `glm-5.2` | 200K | 200K | **976K** |
| `qwen3.5:397b` | 200K | 200K | **256K** |
| `minimax-m3` | 200K | 1M | drifted — sync never re-run |
| `kimi-k3` | 1M | 1M | 1M ✓ |

The default worker is registered at **one fifth** of its real context. Queued as
`lq-17b20b52`, which is materially worse than the glm-5.3-flash framing it was
filed under.

### 10c. The volume default has been on the most expensive usage class since 2026-08-19

This is the finding that actually answers "should we be on DeepSeek V4 Pro?".

- Commit **`a7e7a7b9`** (2026-08-19) set `bin/oll::DEFAULT_MODEL =
  "deepseek-v4-pro:0813"` and justified it in README/CLAUDE.md with
  *"110 raw tok/s, about 96 visible tok/s, **medium usage**"*.
- Verified live 2026-08-26: `ollama.com/library/deepseek-v4-pro` reports
  **"Extra High Usage"** on all three tags. The label in the commit is wrong.
- `knowledge/opencode-first-class-2026-08-11.md` had already recorded the
  correct class **eight days earlier**, together with the rule it implies:
  *"deepseek-v4-pro is included but Extra-High usage → reserved for low-volume
  reasoning (the oplanner), **never routine fanout**."*
- That rule was never superseded by a dated decision. It was overridden by a
  commit resting on a wrong fact.
- The same note named the intended cheap alternate: **`deepseek-v4-flash:0731`**
  — verified live as **"Medium Usage", 1M context**. Pro and Flash differ by one
  word and by two usage tiers.
- And `v4-pro` no longer holds the role the policy reserved it for: `oplanner`
  now aliases `kimiplan`/`kimi-k3`.

So every `/fanout`, every bare `oll` call, and the OpenCode `volume` profile
(`v4-coder`) have been burning Extra-High quota by default for a week. Queued as
`lq-faefaa8c` [G]. **The candidate replacements are both Medium usage with 1M
context: `deepseek-v4-flash:0731` (text, already in the registry at the correct
1M) and `glm-5.3-flash` (adds vision).** That decision is SELECT and is not made
here — but it, not the GLM-5.2 escalation, is where the quota actually goes.

**Method note.** The three measurements above cost less than one benchmark call
each and moved the decision further than 36 streamed completions did. The
benchmark answered "which model is faster"; the config and the git history
answered "what is this harness actually running, and did anyone decide that".

## Sources

- https://z.ai/blog/glm-5.3-flash (vendor, 2026-08-26)
- https://huggingface.co/zai-org/GLM-5.3-Flash
- https://ollama.com/library/glm-5.3-flash · https://ollama.com/library/glm-5.2
- https://artificialanalysis.ai/models/glm-5-3-flash
- https://www.marktechpost.com/2026/08/26/z-ai-releases-glm-5-3-flash-a-320b-a18b-natively-multimodal-moe-with-a-1m-token-context/
