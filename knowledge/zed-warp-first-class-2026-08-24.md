# Zed + Warp as first-class interactive surfaces (2026-08-24)

**Adopt/extend/build decision (plugin-first gate), recorded before implementation:**

- **Zed: ADOPT native surfaces** — the Ollama provider (`language_models.ollama.api_url = "https://ollama.com"`, proven live on the Mac with bare model tags and `agent.default_model: kimi-k3`), the documented rules chain (which picks up this repo's `AGENTS.md → CLAUDE.md` symlink), the global always-on `~/.config/zed/AGENTS.md` instructions file, and the ACP registry for optionally hosting Claude Code/Codex inside the panel. **EXTEND** with one idempotent config merger (`bin/zed-setup`) plus install.sh wiring. **BUILD nothing headless.**
- **Warp: ADOPT native surfaces** (mostly already shipped) — AGENTS.md project rules (natively read; WARP.md is legacy and wins on conflict, so we deliberately ship none), the custom inference endpoint at the public `https://ollama.com/v1` via the existing `bin/warp-ollama`, and Warp's auto-detection of Claude/Codex MCP configs. The existing EXTEND layer stays (`warp-agent-recovery`, `warp-agent-event`, `shell/warp-recovery.sh`). **BUILD nothing new.**

## Why neither is a delegation lane (playbook [E21])

- Zed ships **no public headless agent CLI**: `crates/eval_cli` is README-scoped to eval/benchmark harnesses (Harbor/Pier), built from source only; feature request zed#59146 (headless agent CLI) is open with no team commitment.
- Warp's interactive CLI (`warp` binary) documents **no one-shot flag**; its scriptable product (Oz, `POST https://app.warp.dev/api/v1/agent/run`) is **non-BYOK and Warp-credit-billed**, and Warp's docs state custom inference endpoints do not apply to cloud agents.
- Consequence: both are **interactive human surfaces on the Ollama Cloud subscription** — the playbook's `## Interactive surfaces (not lanes)` section pins this; dispatch stays `oll` → `o` → Codex → Sonnet/Opus. Re-litigation requires new upstream evidence, logged first.

## Rules/doctrine bridging map

| Scope | Zed | Warp |
|---|---|---|
| Project (this repo + any repo with AGENTS.md) | rules chain: `.rules` → … → `AGENTS.md` → `CLAUDE.md`, first match wins → our symlink resolves | AGENTS.md read natively (WARP.md legacy) |
| Global | `~/.config/zed/AGENTS.md` (always-on Instructions) — **install.sh POINTER_TARGETS member**, gated on Zed present | Warp Drive only (cloud, no file) — one-time manual paste of the compact block if desired; install.sh cannot write it |

## Per-machine runbook

- **Ubuntu (the Linux server):** Zed installed 2026-08-24 via `curl -f https://zed.dev/install.sh | sh`. `./install.sh` deploys `zed-setup`, merges `api_url`, writes the global doctrine pointer. **One-time human step:** `zed-setup --key-hint` → paste in Agent Panel → LLM Providers → Ollama (or export `OLLAMA_API_KEY` in Zed's launch environment). Warp: `.deb` installed; endpoint via `warp-ollama`.
- **Mac (the laptop):** Zed + Warp already installed; Zed already wired (api_url + kimi-k3 default; **no key stored in settings.json** — auth lives outside the file, so there is nothing secret to migrate). Converges fully on its next `harness-sync pull && ./install.sh`; artifact-deploy of `zed-setup` + pointer file is acceptable interim, per the exact-binary Mac deploy precedent.

## Config invariants (contract-enforced, `tests/zed-setup/run.sh`)

1. Merge is additive — the Mac's live settings shape is the C1 fixture; every user key survives.
2. No key material in settings.json, ever (Zed doctrine: keychain or `OLLAMA_API_KEY`).
3. Unparseable (JSONC) file → refuse (exit 2) byte-untouched; never guess.
4. Zed-less machine → silent no-op, `~/.config/zed` never created (presence-gate protection).
5. Bare model tags are canonical (the `:cloud` suffix seen in Ollama's repo docs is NOT used — the Mac's working config proves bare tags + auto-discovery).

## Deferred / optional (SELECT — Ricardo decides)

- Warp Drive Global Rules paste (cloud-stored; manual one-time).
- Zed ACP adapters (Claude Code / Codex inside the agent panel; per-machine install from Zed's ACP registry, each owns its own auth/billing).
- If Ubuntu Zed's cloud auto-discovery fails after auth, an explicit `available_models` list becomes a follow-up — never a default (risk: shrinking the Mac's working list).

Research: one ultracode workflow round (5 agents: Zed docs, Warp docs, repo template, machine state, playbook fit), 2026-08-24. Implementation: `bin/zed-setup` by Codex (gpt-5.6) against a Root-authored contract proven red first; mutation score 0.88 (82/93, thr 0.85), residual survivors judged equivalent.
