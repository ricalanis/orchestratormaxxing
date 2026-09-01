# Security Policy

## Supported versions

Main only (rolling). Report privately via GitHub "Report a vulnerability" (private vulnerability reporting) — no e-mail address.

## Threat model

- Installer writes only under `$HOME`.
- Hooks never reach the network without `fleet.env`.
- Secrets only from env / OpenCode auth store.
- `harness-verify` refuses to spawn live agents (re-entrancy guard `ORCHESTRATORMAXXING_HARNESS_CHILD`).
- Export gate strips tenant blocks and runs gitleaks.
- The loop is opt-in and never pushes without a clean verifier.

## Reporting

Include: steps to reproduce, expected vs actual behavior, and any relevant logs. Response window is best effort (single maintainer).

## Out of scope

Third-party CLIs (Claude Code, Codex, OpenCode, Zed, Warp) and Ollama Cloud.
