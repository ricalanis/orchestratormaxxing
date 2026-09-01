# Model usage policy — measured, enforced, 2026-08-26

**What changed:** the routine-fanout default moved from `deepseek-v4-pro:0813`
(**Extra High Usage**) to `deepseek-v4-flash:0731` (**Medium Usage**, 1M ctx),
and a per-role usage-class ceiling is now enforced by `bin/harness-verify`
against the facts the provider publishes.

## Why it was wrong

`knowledge/opencode-first-class-2026-08-11.md` recorded the rule:

> *"deepseek-v4-pro is included but Extra-High usage → reserved for low-volume
> reasoning (the oplanner), **never routine fanout**."*

Eight days later commit `a7e7a7b9` made it exactly that, justified in
README/CLAUDE.md as *"medium usage"*. The label was wrong; nothing mechanical
could contradict it; the pool billed at the top tier for a week. `v4-pro` had
also stopped holding the role the rule reserved for it — `oplanner` now aliases
`kimiplan`/`kimi-k3`.

The generalisable failure: **the harness had no machine-readable facts about its
own models**, so policy ran on prose, and prose drifted.

## The three pieces built

| piece | what it fixes |
|---|---|
| `bin/model-catalog` | reads the provider's published usage class, context and modalities for all 20 live models into `knowledge/model-catalog.json`. Unknown stays UNKNOWN. |
| `bin/oll-sync` (reconcile) | consumes the catalog **and corrects existing entries**. "Preserve curated entries as-is" is why 13 of 20 windows stayed wrong. |
| `ROLE_USAGE_CEILING` + gate | each role declares the most expensive class it may use; `harness-verify` compares against the catalog. |

## The measured pool (2026-08-26)

| usage class | models |
|---|---|
| Low | `gemma4:31b` · `gpt-oss:20b` · `nemotron-3-nano:30b` |
| **Medium** | **`deepseek-v4-flash:0731` (1M)** · `glm-5.3-flash` (1M, +vision) · `qwen3.5:397b` (256K) · `gpt-oss:120b` · `minimax-m2.7` · `mistral-large-3:675b` · `nemotron-3-super` |
| High | `glm-5.2` (976K) · `kimi-k2.7-code` · `minimax-m3` (**512K**, not 1M) · `kimi-k2.6` · `glm-5.1` · `nemotron-3-ultra` |
| Extra High | `deepseek-v4-pro:0813` (1M) · `kimi-k3` (1M) |

13 of 20 were registered in OpenCode with the wrong context window, including
the default worker at **200K against a real 1M**. All corrected; 0 mismatches.

## The ceilings

A ceiling is not a preference. Roles that run on **every** task pay their class
thousands of times; roles selected for capability pay it rarely.

| role | ceiling | model | class |
|---|---|---|---|
| volume | Medium | `deepseek-v4-flash:0731` | Medium ✓ |
| general | Medium | `qwen3.5:397b` | Medium ✓ |
| bounded-code | High | `kimi-k2.7-code` | High ✓ |
| reasoning | High | `glm-5.2` | High ✓ |
| long-context | High | `minimax-m3` | High ✓ |
| long-horizon | Extra High | `kimi-k3` | Extra High ✓ |
| planning | Extra High | `kimi-k3` | Extra High ✓ |

Proven to bite: restoring `v4-pro` as the default reports
`role 'volume' uses deepseek-v4-pro:0813 (Extra High Usage) above its Medium Usage ceiling`.

## How the winner was chosen

`bin/model-bench`, 48 streamed calls, **`reasoning_effort` pinned to `low`** — an
unpinned run measures the token cap, not the model. Split by task shape, because
the volume role does not do code review:

**Volume-shaped work** (digest / reason / extract, 9 calls each):

| model | pass | empty | spread | s/task | max | tok/task |
|---|---|---|---|---|---|---|
| **`deepseek-v4-flash:0731`** | **9/9** | 0 | **1.9x** | **2.8s** | **4.0s** | **286** |
| `glm-5.3-flash` | 8/9 | 0 | 2.3x | 2.9s | 9.7s | 195 |
| `deepseek-v4-pro:0813` | 9/9 | 0 | 1.4x | 3.6s | 5.2s | 641 |
| `qwen3.5:397b` | 8/9 | 1 | 1.2x | 14.2s | 36.6s | 1742 |

V4 Flash beats the incumbent on speed, tail latency and tokens at half the cost
class, with identical correctness. It is not the cheapest in tokens —
`glm-5.3-flash` is — but it is steadier and two days older on the provider,
which for the highest-frequency role is worth more than 90 tokens a call.

**Review** (the hard reasoning task, 3 reps): `glm-5.3-flash` **3/3** ·
`deepseek-v4-flash` 1/3 · `deepseek-v4-pro` 1/3. GLM-5.3-Flash is therefore the
strong candidate for the *reasoning* role — a separate decision, deliberately
not taken here while the model is this new (its decode spread over all tasks was
7.2x against 1.9x for V4 Flash).

## What was deliberately NOT changed

- `deepseek-v4-pro:0813` is **not retired**. It remains reachable with an
  explicit `--model` for the low-volume reasoning the 2026-08-11 rule assigned
  it. What changed is that it is no longer what every chunk gets by default.
- The `reasoning` role stays on `glm-5.2`. Re-measure `glm-5.3-flash` in 7–14
  days before moving it.
- `bin/oll-council` still has its own transport and does not inherit
  `--effort` (`lq-e3ffec0b`).

## Related

- `knowledge/glm-5.3-flash-evaluation-2026-08-26.md` — the evaluation that
  surfaced all of this, including why its own first conclusion was wrong.
- `knowledge/delegation-playbook.md` `[E28]`, `[E29]`.
- Queue: `lq-17b20b52`, `lq-faefaa8c`, `lq-4b3c9d6a` resolved.

## Hermes — measured on its own gate (2026-08-27)

The bench's `extract` task could not separate the candidates (all 4 passed 3/3),
so Hermes was tested on the property `digestion.py` actually validates: a `quote`
must be a **verbatim substring of `sentences[].text`**, and `summary.action_items`
is Fireflies' *AI-generated* paraphrase that must never be cited
(`digestion.py:21-24`). The fixture is a Spanish transcript whose action_items
restate two decisions in words appearing nowhere in the speech; scoring uses
digestion's own `_norm` (whitespace-collapse + lowercase).

| model | verbatim | median s | median tok | class |
|---|---|---|---|---|
| **`glm-5.3-flash`** | **3/3** | **7.1s** | **182** | Medium |
| `deepseek-v4-flash:0731` | 3/3 | 6.9s | 1422 | Medium |
| `glm-5.2` (incumbent) | 3/3 | 28.5s | 252 | High |

**The trap did not discriminate — no model cited the AI summary.** So citation
fidelity is not the differentiator; cost and latency are. Against the incumbent
`glm-5.2`, `glm-5.3-flash` is **4× faster, uses 28% fewer tokens, and drops a
usage tier** (High → Medium) at identical correctness.

Two infra 429s hit `glm-5.3-flash` mid-run and were **retried, not scored** — a
transport failure must never demote a lane (playbook rule). Both retries passed
on the first attempt.

### Recommended configuration

- `DIGESTION_MODEL=glm-5.3-flash` — the citation-critical path, where its token
  frugality (182 vs 252) and 4× speed both beat the incumbent.
- `WHATSAPP_CLASSIFY_MODEL` — leave on the volume default. It is ordinary batched
  volume work with no special evidence, so it should follow policy, not a
  special case.
- `OLL_REASONING_EFFORT=low` on the gateway — **the largest single win, and it is
  model-independent.** Both call sites shell out to `oll` via `subprocess.run`
  with no env scrubbing (`digestion.py:677`, `whatsapp_classify.py:202`), so this
  propagates with **zero code change**. It directly addresses the pathology
  `DIGEST_MAX_TOKENS=16384`, `_looks_truncated()` and `_EMPTY_WORKER_MARKERS`
  were all written to survive.

### Confidence and caveats

- 3 reps on **one synthetic fixture**, not on live Hermes data. It measures the
  gate faithfully; it does not measure a 130-sentence real meeting.
- `glm-5.3-flash` is **two days old** on this provider and showed one 37.7 s
  outlier against a 7 s median — the same tail variance seen everywhere else.
  Digestion is retry-safe and the model is one env var, so this is a cheap A/B,
  not a commitment.
- Latency is not the binding constraint either way (`DIGEST_TIMEOUT=600`), but
  events-per-tick is: at 480 s per tick, 7 s/event ≈ 68 events vs the incumbent's
  ≈ 17.

## Effort levels are NOT a monotonic cost dial (2026-08-27)

Swept `reasoning_effort` across the same Hermes verbatim-citation gate, 2 reps
per cell, `max_tokens=4000`. Cell = passes/decided + min completion tokens.

| model | none | low | medium | high | max |
|---|---|---|---|---|---|
| `glm-5.3-flash` | **0/2** · 495t | 2/2 · **179t** | 2/2 · 432t | 2/2 · 208t | 2/2 · 412t |
| `glm-5.2` | 2/2 · **146t** | 2/2 · 252t | 2/2 · 256t | 2/2 · 291t | 2/2 · 1082t |
| `deepseek-v4-flash:0731` | 2/2 · **215t** | 2/2 · **1570t** | 2/2 · 966t | 2/2 · 747t | 2/2 · 616t |

Three findings, two of which correct earlier entries in this file.

**1. Correctness is flat from `low` upward.** Every model passed 2/2 at low,
medium, high and max. Effort buys nothing on this task above `low`; it only
costs. The single break is `glm-5.3-flash` at **`none`**, which failed 0/2 —
and the reason matters: at `none` it does not stop reasoning, it stops
*separating* reasoning from the answer, emitting its deliberation as `content`
("The user has sent 'say ok' — a very simple, direct instruction…"). So `none`
is not "thinking off" for this model; it is "thinking unsegregated", which
destroys any response with a shape contract.

**2. Cost is non-monotonic and MODEL-SPECIFIC.** `deepseek-v4-flash:0731` is
*inverted*: `low` is its **most expensive** setting (1570–2360 t) and `max` is
cheaper (616–860 t), with `none` cheapest at 215 t. `glm-5.2` climbs toward max
(146 → 1082 t). `glm-5.3-flash` is cheapest at `low`. **There is no single level
that is cheap for every model.**

> **This corrects the advice in "Recommended configuration" above.** A global
> `OLL_REASONING_EFFORT=low` on the gateway would put the volume default
> (`deepseek-v4-flash`) on its *worst* setting — ~7× the tokens of `none` — so
> the blanket recommendation was wrong. Effort belongs **next to the model in the
> role policy**, not in one global env var. Queued as `lq-956ab278`; **landed
> 2026-08-30** as `MODEL_EFFORT_POLICY` in `bin/oll` (per-model defaults for the
> three swept models, precedence `--effort` > env > policy > unset, `--effort
> default` forces the provider default, council resolves per member). Live
> confirmation the same day: a bare `oll` call ran deepseek at policy `none`
> for 2 completion tokens vs 32 at the provider default.

**3. The provider's accepted set is `none|low|medium|high|max`.** `minimal` is
rejected with HTTP 400 (*"invalid reasoning value: 'minimal' (must be "high",
"medium", "low", "max", or "none")"*). `bin/oll`'s `REASONING_EFFORTS` had
included it — taken from OpenAI's documented vocabulary rather than measured —
so `--effort minimal` passed validation and died mid-flight, the exact failure
the closed set exists to prevent. Corrected, and the contract now asserts both
the exact set and that `minimal` is refused.

### Revised Hermes configuration

`glm-5.3-flash` at `low` remains right for **digestion** (179–199 t, ~7 s, 3/3
verbatim). But the gateway must not set a single global effort while its two call
sites can run different models: `whatsapp_classify` on the volume default would
inherit `low`, v4-flash's worst level. Two coherent options:

- **Zero code:** pin both `DIGESTION_MODEL` and `WHATSAPP_CLASSIFY_MODEL` to
  `glm-5.3-flash`, then `OLL_REASONING_EFFORT=low` is correct for both. (No
  classify-shaped evidence for that model yet — it is an inference from the
  sweep, not a measurement.)
- **Two-line change (preferred):** append `--effort` to the argv lists at
  `digestion.py:677` and `whatsapp_classify.py:202` so each site pins its own,
  independent of any global.
