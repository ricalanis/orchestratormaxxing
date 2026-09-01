# Provider routing — which AI provider for which task

`provider-ask` exposes three configured routes. Two are currently usable; xAI
remains implemented but unavailable while credits are exhausted. Gemini is
intentionally absent after the the Google AI subscription subscription was suspended on 2026-07-17.

## Active routes

| Provider | Reached via | Default model | Auth source | Best at |
|---|---|---|---|---|
| `ollama` | `bin/oll` | `deepseek-v4-flash:0731` | OpenCode auth store | Dialogue, execution, volume, and first-pass drafts; `oll --reasoning` selects GLM 5.2 |
| `openai` | `codex exec` | `gpt-5.5` | ChatGPT Plus OAuth, no API key | Strong reasoning, code review, careful analysis |
| `anthropic` | `claude -p` | `sonnet` (`MODEL=opus` upgrades) | Claude subscription CLI login | Opus/Sonnet second opinions for non-Claude hosts (OpenCode `/opus` `/sonnet`); never an in-OpenCode Anthropic provider (ToS) |
| `xai` | `vibe-tools ask` / `xsearch` | `grok-4-latest` | claudemaxxing environment file | Web and X retrieval when credits are available |

All routes use `provider-ask <provider> "prompt"`; stdin is appended as context.
`multi-council` and `cross-review` fan out over this primitive and degrade
gracefully when a selected route is unavailable.

## Local web route

| Route | Reached via | Endpoint | Auth | Best at |
|---|---|---|---|---|
| Firecrawl self-hosted | Hermes `web_search` / `web_extract` | `http://localhost:3002` | Local dummy key; server auth disabled | Free, unlimited, local, privacy-first web search and extraction |

The official Firecrawl Compose stack lives at `~/dev/firecrawl` and runs the
API, Playwright, Redis, RabbitMQ, and PostgreSQL locally. Hermes is pinned to
the direct route with `web.backend=firecrawl`, `web.use_gateway=false`, and
`FIRECRAWL_API_URL=http://localhost:3002`; it does not depend on Firecrawl
Cloud or a cloud API key. “Unlimited” means no Firecrawl quota or per-page
billing: throughput is still bounded by this machine, target sites, and their
rate limits. Requests remain local until Firecrawl contacts the requested site
or search engine, and self-hosting lacks the cloud service's proprietary
anti-bot engine.

## Routing convention

- `oll` defaults to `deepseek-v4-flash:0731` for dialogue, execution and volume.
  Pass `oll --reasoning` for the explicit GLM 5.2 complex-reasoning route.
- Normal Ollama fanout uses `deepseek-v4-flash:0731`, `glm-5.3`, `kimi-k3`,
  `kimi-k2.7-code`, and `qwen3.5:397b`. K2.7 remains the bounded code-focused
  worker. K3 is included-first but higher-consumption, so select it for the
  OpenCode planner or a long-horizon/1M-context sequential chain—not as the
  volume default. `minimax-m3` and `mistral-large-3:675b` remain explicit
  multimodal/long-context and diversity specialties.
- Stateful workspace code never runs through `oll`: use `o delegate --profile
  volume|reasoning|bounded-code|long-horizon|general|long-context` (or
  `o-ubuntu delegate` for the Ubuntu checkout). `bin/oll` resolves each profile
  to the canonical OpenCode agent/model; close every session with `o close`.
- Code review, hard reasoning, and correctness: `openai` when a second Codex-family
  process is acceptable; otherwise use an Ollama model from a different family.
- Quick queries, bulk, parallel, and low-stakes work: `ollama`.
- Web, X, and real-time retrieval: `xai`, only when credits are available.
- Diverse second opinions: `multi-council`; agreement is corroboration, never a
  replacement for deterministic verification.
- Cross-checked code review: `cross-review`; the active default is Ollama + OpenAI.

## Tools

```text
provider-ask <provider> "prompt"      # one provider, plain text out
provider-ask --list                   # probe configured routes
oll "prompt"                          # V4 Pro volume/default
oll "prompt" --reasoning              # GLM 5.2 complex reasoning
oll "prompt" --model kimi-k3          # long-horizon / multi-phase chain
multi-council "question"              # one model per selected provider
cross-review [file|--staged] --merge  # review and optionally deduplicate
```

Both fan-out tools accept `--providers a,b,c` to choose a subset.

## Live status (2026-07-17)

| Provider | Status | Note |
|---|---|---|
| `ollama` | **up** | Working through Ollama Cloud |
| `openai` | **up** | Working through ChatGPT Plus and Codex |
| `anthropic` | **up** | Working through the local `claude` CLI (subscription) — verified 2026-08-11 |
| `xai` | **down** | Team credits exhausted; also blocks `xsearch` |

Gemini is not a degraded route waiting for credential repair: it is deliberately
inactive. Do not suggest restoring it or route work to it unless Ricardo explicitly
reactivates the subscription.
