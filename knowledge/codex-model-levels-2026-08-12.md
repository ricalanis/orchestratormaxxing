# Codex model levels — 2026-08-12

## Result

- **[probed]** This host runs `codex-cli 0.147.0`.
- **[docs]** The account-refreshed `codex debug models` catalog lists the exact
  slugs `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol` with visibility
  `list` and `supported_in_api: true`.
- **[probed]** All three model IDs reached the OpenAI provider through this
  host's ChatGPT subscription and returned exactly `ok` with exit `0` from the
  orchestrator shell.

| Level | Exact `-m` ID | Local catalog description | Catalog efforts | Live result |
|---|---|---|---|---|
| Luna | **[docs]** `gpt-5.6-luna` | **[docs]** “Fast and affordable agentic coding model.” | **[docs]** `low`, `medium` (default), `high`, `xhigh`, `max` | **[probed]** `low` returned exactly `ok`, exit `0` |
| Terra | **[docs]** `gpt-5.6-terra` | **[docs]** “Balanced agentic coding model for everyday work.” | **[docs]** `low`, `medium` (default), `high`, `xhigh`, `max`, `ultra` | **[probed]** `medium` returned exactly `ok`, exit `0` |
| Sol | **[docs]** `gpt-5.6-sol` | **[docs]** “Latest frontier agentic coding model.” | **[docs]** `low` (default), `medium`, `high`, `xhigh`, `max`, `ultra` | **[probed]** `high` returned exactly `ok`, exit `0` |

## Tier ordering

- **[model-knowledge]** For August 2026 routing, use Luna as the fastest and
  cheapest Codex level, Terra as the balanced middle level, and Sol as the
  highest-capability, highest-cost/latency level.
- **[docs]** The local catalog descriptions independently support the qualitative
  roles “fast and affordable” for Luna, “balanced” for Terra, and “latest
  frontier” for Sol; they do not publish numeric price or latency ratios.
- **[unverified]** Exact subscription usage multipliers, token prices, and
  latency ratios among Luna, Terra, and Sol were not exposed by the inspected
  CLI surfaces and were not measured.

## CLI and reasoning controls

- **[docs]** `codex exec --help` exposes `-m, --model <MODEL>`, generic
  `-c, --config <key=value>`, `-s, --sandbox <SANDBOX_MODE>`, `--ephemeral`,
  `--ignore-user-config`, `--ignore-rules`, and `--skip-git-repo-check`.
- **[probed]** With `--ignore-user-config --strict-config`, the override
  `-c model_reasoning_effort=low` passed strict configuration parsing, while an
  invented configuration key was rejected as unknown.
- **[docs]** `codex debug models` is the model-listing surface on 0.147.0; its
  `supported_reasoning_levels` entries are the source of the effort sets in the
  table.
- **[probed]** `codex exec` rejected `-a never` at argument parsing even though
  the top-level `codex --help` exposes `-a`; bounded delegation commands must not
  pass `-a` to the `exec` subcommand on this version.
- **[probed]** `low` on Luna, `medium` on Terra, and `high` on Sol were exercised
  end-to-end; the CLI startup transcript reported the requested model and effort
  before each successful `ok` response.
- **[unverified]** The other catalog-listed model/effort combinations were not
  individually live-probed.

## Probe record

- **[probed]** The final probe shape for each row was:

  ```text
  codex exec --ignore-user-config --ignore-rules --ephemeral \
    --skip-git-repo-check -s read-only -m <id> \
    -c model_reasoning_effort=<effort> 'Reply with exactly: ok'
  ```

- **[probed]** The first sandboxed round made Luna, Terra, and Sol exit `1` with
  `failed to initialize in-process app-server client: Read-only file system
  (os error 30)` before model selection; a later orchestrator-shell round using
  the same model IDs and lane efforts returned `ok` with exit `0` for all three.
- **[probed]** The later successes establish that the earlier exits were a
  sandbox/state-path artifact rather than model rejection.
- **[probed]** No web search was run because the refreshed local account catalog,
  local help, and bounded probes supplied the primary evidence without a remote
  source becoming a build dependency.

## `provider-ask openai` compatibility

- **[docs]** `bin/provider-ask` maps its `openai` backend to
  `codex exec -m "$model" -s read-only --skip-git-repo-check --ephemeral`.
- **[docs]** Its supported selectors are `provider-ask openai --model <id> ...`
  and `MODEL=<id> provider-ask openai ...`; `MODEL="${MODEL:-}"` now preserves
  the inherited environment selector.
- **[docs]** Source inspection shows that `provider-ask openai --model
  gpt-5.6-{luna,terra,sol}` passes the selected ID to `codex exec -m`.
- **[probed]** `MODEL=gpt-5.6-luna`, `MODEL=gpt-5.6-terra`, and
  `MODEL=gpt-5.6-sol` each returned exactly `ok` with exit `0` through
  `provider-ask openai` on 2026-08-12.
