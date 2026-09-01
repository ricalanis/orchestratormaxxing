# Configure Ollama as a provider for Warp's agent

**Goal:** make Warp's terminal agent run on Ollama models — ideally the *same heavy
frontier models* this harness already uses (`glm-5.3`, `kimi-k3`, …) — instead of
Warp's hosted models / AI credits.

**TL;DR (the harness path):** point Warp at **Ollama Cloud** (`https://ollama.com/v1`). It's
a public HTTPS, OpenAI-compatible endpoint with the key already in your OpenCode auth store,
so there's nothing to host and no tunnel to run. Get the paste-ready values with:

```bash
warp-ollama            # prints Base URL + API key + recommended models
warp-ollama --mask     # same, key masked (screen-share safe)
```

---

## How Warp does it: Custom inference endpoint

Warp's mechanism is **Settings → search "inference endpoint" → Add endpoint** ("Custom
inference endpoint"). It accepts any **OpenAI-compatible** endpoint that implements
`POST /v1/chat/completions`. You provide three things:

| Field | Value |
|---|---|
| **Base URL** | the URL that exposes `/v1/chat/completions` |
| **API key** | credentials for that endpoint (stored only on your device, in the OS keychain) |
| **Model(s)** | one or more model identifiers to route through this endpoint |

After saving, your models appear in Warp's **model picker**. When you explicitly select an
endpoint-routed model, Warp routes the request through *your* endpoint — billed by that
provider at their pricing, **not** drawn from your Warp AI credits.

### The one hard constraint: endpoint must be a PUBLIC URL

Requests route **through Warp's servers**, so Warp must be able to reach your endpoint over
the public internet. `localhost`, `127.0.0.1`, and other private/LAN addresses are
**rejected**. This is why Ollama *Cloud* is the clean fit and a *local* Ollama needs a tunnel
(see below).

---

## Path A — Ollama Cloud (recommended, no tunnel)

This is the harness default: same heavy models as the worker pool, zero hosting.

1. Run `warp-ollama` (or `warp-ollama --json`) to get the values. They are:
   - **Base URL:** `https://ollama.com/v1`
   - **API key:** your Ollama key (read from `~/.local/share/opencode/auth.json` → `ollama-cloud`).
     If that's empty, wire it first via [`SETUP.md`](../SETUP.md) / `./bootstrap.sh`.
   - **Model(s):** `glm-5.3`, `kimi-k3`, `qwen3.5:397b` (the exact normal
     fanout baseline). Specialty models remain available through `oll` when explicitly
     selected. Any id in your live OpenCode catalog (`oll-sync`) also works.
2. In Warp: **Settings → search "inference endpoint" → Add endpoint.** Paste Base URL +
   API key, add the model id(s), **Save**.
3. Open the **model picker** in Warp and select one of the Ollama models. Done — the agent
   now runs on Ollama Cloud.

> Same key, same models as `oll` / OpenCode / the `ollama-worker` subagent — one Ollama key
> powers the whole stack. Rotate it in one place (`auth.json`) and Warp picks up the change
> the next time you re-paste.

## Path B — local Ollama (needs a public tunnel)

Warp rejects `http://localhost:11434`, so a model running on your own machine must be exposed
at a public HTTPS URL.

1. Make Ollama listen beyond localhost: set `OLLAMA_HOST=0.0.0.0:11434` and restart Ollama
   (systemd: `Environment="OLLAMA_HOST=0.0.0.0:11434"`; shell: `export OLLAMA_HOST=0.0.0.0`).
2. Expose it with a tunnel, e.g. `ngrok http 11434`, and copy the public HTTPS URL.
3. In Warp's custom inference endpoint use:
   - **Base URL:** `https://<your-ngrok-subdomain>.ngrok-free.app/v1`
   - **API key:** anything (local Ollama ignores it) — or whatever your tunnel auth requires.
   - **Model(s):** the local model tags you've pulled (e.g. `llama3.1`, `qwen2.5-coder`).
4. Save → pick the model in Warp's model picker.

> Caveat: traffic flows your-machine → tunnel → Warp's servers → back. Keep the tunnel up
> while you use it, and prefer auth on the tunnel since the URL is public. For always-on use,
> Path A (Cloud) is simpler and more reliable.

---

## Notes & limitations

- **Billing:** endpoint-routed models are billed by Ollama (Cloud) or are free (local) — not
  from Warp AI credits. Warp's own built-in models still use credits as usual.
- **Key storage:** Warp stores the endpoint key in your OS keychain (on-device), never on
  Warp's servers. `warp-ollama` only *reads* the key from the OpenCode auth store and prints
  it — it stores nothing. Use `--mask` when screen-sharing.
- **Enterprise BYO LLM** (`docs.warp.dev/enterprise/.../bring-your-own-llm`) is a *separate*,
  admin-configured path (routing policies, AWS Bedrock, team credentials). For a single
  developer, the **custom inference endpoint** above is the route.
- **OpenAI-compatible only:** the endpoint must implement `POST /v1/chat/completions`. Ollama
  Cloud and local Ollama both do (Ollama ships an OpenAI-compatible API at `/v1`).

## First-class status (2026-08-24)

- Warp is installed on **both machines** (Ubuntu `.deb` 0.2026.08.12 + Mac app); the endpoint
  config is device-local, so run `warp-ollama` once per machine.
- **Rules bridging:** Warp natively reads `AGENTS.md` project rules (WARP.md is legacy and
  wins on conflict — this repo deliberately ships none); the repo's `AGENTS.md → CLAUDE.md`
  symlink is what Warp consumes. Global Rules live only in Warp Drive (cloud) — a one-time
  manual paste if wanted; install.sh cannot write them.
- Warp's official docs still document only local-Ollama+ngrok and never name Ollama Cloud;
  `https://ollama.com/v1` remains **verified-by-live-test**, not vendor-documented.
- Warp is an **interactive surface, never a delegation lane** (Oz is non-BYOK,
  credit-billed): playbook [E21] + `knowledge/zed-warp-first-class-2026-08-24.md`, where the
  Zed sibling (`bin/zed-setup`) is also specified.

## Sources

- [Custom inference endpoint — Warp Docs](https://docs.warp.dev/agent-platform/inference/custom-inference-endpoint/)
- [Bring your own inference to Warp (blog)](https://www.warp.dev/blog/bring-your-own-inference-to-warp)
- [Set up Ollama — Warp Docs](https://docs.warp.dev/guides/external-tools/how-to-set-up-ollama/)
- [Bring Your Own API Key — Warp Docs](https://docs.warp.dev/agent-platform/inference/bring-your-own-api-key/)
- [Issue #9303 — Custom OpenAI-compatible provider endpoints](https://github.com/warpdotdev/warp/issues/9303)
- Ollama OpenAI-compatible API: base `https://ollama.com/v1` (Cloud) / `http://localhost:11434/v1` (local)
