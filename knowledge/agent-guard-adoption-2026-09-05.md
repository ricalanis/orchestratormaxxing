# agent-guard: pre/post security hooks across every cmaxxing host (2026-09-05)

**Adopt/extend/build decision (plugin-first gate), recorded before implementation:**

- **ADOPT** the command denylist and its 151-case block/allow corpus from `davidondrej/skills`
  @ `76724e4c517f786d631f8e6b8d75078c800357d7` (MIT): `hooks/dangerous-patterns.txt` (36 POSIX-ERE
  rules) and the cases extracted from `hooks/test-guard.sh` into `guard-cases.txt`.
- **ADOPT** the 25 write-content security rules from `anthropics/claude-plugins-official`
  @ `222b19d2d1c635c440cc490364f5a88169ee31a7` (Apache-2.0): `plugins/security-guidance/hooks/patterns.py`,
  vendored unmodified as `security_patterns.py`.
- **ADOPT** Hermes's bundled `security-guidance` plugin as the Hermes post hook. It is the same
  Anthropic rule set already ported to Hermes's `write_file`/`patch`/`skill_manage` tools; we only enable it.
- **BUILD** one stdlib Python runner, `bin/agent-guard` (the `provider-ask` shape: the only place that
  knows each host's hook protocol), plus thin native adapters per host. No network, no agent spawn.
- **REJECT** the official Claude `security-guidance` plugin as the integration: it covers one host, and
  its Stop-hook LLM review needs an API key and re-enters an agent (the 2026-08-12 re-entrancy incident
  is exactly the failure class a hook that starts an agent produces).

Everything vendored lives under `deploy/agent-guard/` with `PROVENANCE.json` (repo, commit, path,
license, exact modification per file) and both `LICENSE.*` files. `install.sh` copies the data to
`~/.config/orchestratormaxxing/agent-guard/` (copy, not symlink); `$ORCHESTRATORMAXXING_GUARD_DIR` overrides the
lookup; an in-repo run before install resolves the repo copy.

## What was scouted

1. `davidondrej/skills` (MIT). The `global-agent-guardrails` skill + `hooks/` directory: a Claude-only
   PreToolUse Bash denylist with a shell test harness of 151 cases. Taken: the patterns and the corpus,
   verbatim. Not taken: its Claude-only hook script (replaced by the cross-host runner) and the skills
   listed under "Rejected skills" below.
2. `anthropics/claude-plugins-official` (Apache-2.0). The `security-guidance` plugin: a PostToolUse
   pattern scan over Write/Edit/MultiEdit content (25 rules, path filters, 256 KiB cap) and an optional
   Stop-hook LLM review. Taken: `patterns.py` and the `_scan_content` semantics. Rejected: the Stop-hook
   review (API key + agent re-entry).
3. Hermes bundled plugins (`~/.hermes/hermes-agent/plugins/security-guidance/`). Already the Anthropic
   rule set with `SECURITY_GUIDANCE_DISABLE=1` / `SECURITY_GUIDANCE_BLOCK=1`. Adopted by enabling it.
   Hermes had no bundled command guard, so `command-guard` (a `pre_tool_call` plugin) is the one
   Hermes adapter we ship.
4. Native surfaces with no hook system: Warp (`command_denylist` per execution profile) and Zed
   (`agent.tool_permissions.tools.terminal.always_deny`). Both get the same patterns as configuration.

## Per-host wiring

| Host | Event | Mechanism | Blocks or confirms | Post scan |
|---|---|---|---|---|
| Claude Code | `PreToolUse` matcher `Bash`; `PostToolUse` matcher `Edit\|Write\|MultiEdit\|NotebookEdit` | `~/.claude/settings.json` hooks merged by `agent-guard wire claude` (idempotent, other hooks preserved); fire under `--dangerously-skip-permissions` | Blocks (exit 2 + stderr reason) | Yes |
| Codex | `PreToolUse` `Bash`; `PostToolUse` `apply_patch\|Write\|Edit` | `plugins/orchestratormaxxing/hooks/hooks.json` entries, no `SOLPLAN_CHILD` short-circuit; trust via `agent-guard trust-codex --apply` | Blocks | Yes (apply_patch `+` lines per file section) |
| OpenCode | `tool.execute.before` on `bash`; `tool.execute.after` on `write`/`edit` | `opencode/plugins/orchestratormaxxing-guard.js` (installed to `~/.config/opencode/plugins/`); deny = thrown Error, guidance appended to tool output | Blocks | Yes |
| Hermes | `pre_tool_call` on `terminal` | `~/.hermes/plugins/command-guard/` (`{"action":"block"}` on deny) + bundled `security-guidance`; both under `plugins.enabled` in `~/.hermes/config.yaml` | Blocks | Yes (bundled plugin, warn by default) |
| Warp | agent execution profile | `command_denylist` in every `[agents.execution_profiles.<name>]` of `settings.toml`, written by `agent-guard warp --apply` | Confirms (asks before running; never refuses) | No |
| Zed | agent terminal tool | `agent.tool_permissions.tools.terminal.always_deny` `{"pattern","case_sensitive":true}` entries, written by `agent-guard zed --apply` | Blocks (Zed native) | No |
| Hermes orchestrator | inherited | `claude --dangerously-skip-permissions`, `claude -p`, `codex exec` sessions pick up the Claude global hooks and the Codex plugin hooks; no code of its own | Blocks | Yes (inherited) |

## Invariants

- **Fail-open at call time, red at the gate.** `pre` never breaks a session: a missing patterns file, a
  payload without a command, or a dead guard binary behind an adapter all allow. `status --gate` is where
  blindness shows: it reds a missing patterns file, any regex compile error (a bad line is skipped and
  reported, never allowed to disable the other rules), and any present host that is unwired. `wired`,
  `missing`, `absent` are three states; only a present host can red the gate (a Zed-less Mac stays green).
- **Exit codes are the contract.** `pre`/`check`: 0 allow, 2 block. `warp`/`zed`: 0 ok, 1 drift under
  `--check`, 2 refuse with the file untouched. `trust-codex --check`: 1 iff any orchestratormaxxing hook is not
  `trusted`. `selftest`: 1 on any escaped case. Bad input anywhere: one stderr line and exit 2, no traceback.
- **Seatbelt, not sandbox.** Regex over the raw command string. Obfuscation (base64, `$()` indirection,
  a script written to disk and then executed) slips past by design; the real containment is the
  permission mode and the loopback rules elsewhere in the harness.
- **Never re-enters an agent.** Nothing in the tool, the adapters, or the contract spawns `claude`,
  `codex exec`, `opencode run`, or `hermes chat`. The one exception is `trust-codex` without
  `--inventory`, which spawns `codex app-server --stdio` (marked `ORCHESTRATORMAXXING_HARNESS_CHILD=1`) to read
  the hook inventory; it never runs inside a contract and install.sh only prints the instruction.
- **Proof of red.** `tests/agent-guard/run.sh` C3 removes the `sudo rm` rule from a copy of the patterns
  file and requires `sudo rm file.txt` to pass, `rm -rf /` to still block, and `selftest` to fail naming the
  escaped case. A guard that passes while consulting nothing cannot pass this file.

## Gotchas

- **Codex trust hash.** Codex records a `trusted_hash` per hook entry in `~/.codex/config.toml`
  (`[hooks.state."<key>"]`). Any edit to `plugins/orchestratormaxxing/hooks/hooks.json` makes the entries read
  `untrusted`/`modified` and Codex silently does not run them. Re-trust with `agent-guard trust-codex --apply`
  (reads the app-server `hooks/list` inventory; `--check` reports drift). Only `orchestratormaxxing@personal`
  hooks are ever trusted by the tool; other plugins' hooks are never touched.
- **Warp restores account settings at startup and honours live writes.** Same measurement as
  `warp-model-pin`: `settings.toml` written before launch is overwritten from the account seconds later;
  a write to a running Warp is honoured live and never reverted. `agent-guard warp` follows the same
  two layouts (`WARP_MODEL_PIN_LAYOUT=linux|darwin`) and the same targeted line-insertion guard, and it
  deliberately does not validate with `tomllib` (Warp writes trailing-comma inline tables tomllib rejects).
  Patterns go in as TOML basic strings because the rules contain `'`.
- **Warp denylist means confirm, not block.** A matching command is shown to the human for approval;
  it is not refused. Say so when someone reports "Warp let it through".
- **Zed regex is native-only and `case_sensitive: true`.** No `[:space:]` classes; the tool converts
  to `\s`. JSONC settings are spliced offset-preservingly (comments kept) or refused with exit 2; the
  tool never rewrites a file it cannot parse safely. Absent `~/.config/zed/settings.json` is a silent
  no-op so install.sh's Zed presence gate is never flipped.
- **Hermes presence needs a usable CLI, not just `kanban.db`.** The Mac carries a stale `~/.hermes/kanban.db` (Aug 21) and a dangling `~/.local/bin/hermes` symlink with no venv behind it — production Hermes runs on Ubuntu only. `status` reads that as `absent` (never `missing`), the same rule install.sh uses before deploying the plugin.
- **Hermes plugins are opt-in.** A plugin directory under `~/.hermes/plugins/` does nothing until it is
  listed under `plugins.enabled` in `~/.hermes/config.yaml` (the live config had `enabled: []`).
  install.sh runs `hermes plugins enable command-guard --no-allow-tool-override` and the same for
  `security-guidance`, tolerating "already enabled"; Hermes must be restarted to load them.
- **False-positive class.** A dangerous string inside `echo`, a commit message, a doc example, or a test
  fixture matches like the real thing. The fix is to put the text in a file (or a heredoc the guard does
  not see as a command), never to loosen a rule. Tuning a rule means: edit
  `deploy/agent-guard/dangerous-patterns.txt`, add a corpus case to `guard-cases.txt`, run
  `tests/agent-guard/run.sh`, then `./install.sh`.
- **Obfuscation slips past regex.** `echo cm0gLXJmIC8= | base64 -d | sh`, `$(printf ...)`, aliases,
  and scripts written first then executed are not caught. This is expected; the guard is for accidents.

## Review findings folded in (two adversarial refuters, 2026-09-05)

The 15-case contract was green and two independent refuters were still right about five things. Each got a
contract case (C16–C20) that was proven red against the pre-review tool before the fix:

- **Whole-string matching was quadratic and not grep-faithful.** `[^;&|]*` crossing newlines made 5000 benign
  `git push` lines take 18 s on the allow path — past Claude's 10 s and Hermes's 5 s hook timeouts, which fail
  open, so padding + a late dangerous line was a measured bypass — and the same form blocked any heredoc with
  an indented `pass` line. Fix: match per line like `grep -E` (no `MULTILINE`), strip NUL first (a NUL after
  the target defeated the root-rm rule), window lines over 32 KiB to their first and last 32 KiB (a 105 KB single line of repeated `git push` fell from 6.7 s to ~1.5 s, under Hermes's 5 s hook budget; the unscanned middle of such a line is an accepted seatbelt gap). Five per-line
  cases were appended to the corpus under a marked cmaxxing section.
- **A non-UTF-8 byte in the patterns file turned the guard fail-closed** (exit 2 on every Bash call). Fix: the
  file is decoded with replacement and the error reported by `status`; `pre`/`check` are wrapped so no internal
  error can ever produce exit 2 without a match.
- **The OpenCode transport dropped commands over 128 KiB.** `printf '%s' ${payload}` execs `/usr/bin/printf`,
  Linux `MAX_ARG_STRLEN` fails it with E2BIG, and the guard read an empty stdin and answered "allow". Fix: the
  payload is a Bun-shell stdin redirect (`< ${Buffer}`); Bun's parser refuses a `2>/dev/null` next to an object
  redirect, so stderr is swallowed by `.quiet()`; an 8 s race guards the TUI against a hung guard (fail-open).
  C6b runs the real Bun shell with a 140 KB command when `bun` is installed.
- **install.sh applied Zed before the guard data existed** (a first install refused and printed a hand-merge
  note). Fix: the Zed apply lives in the guard section after the copy; Hermes is resolved on PATH or
  `~/.local/bin/hermes` (the Mac over SSH has only the latter) with a distinct message when `kanban.db` exists
  but no CLI does.
- **`status` read Warp/Zed as `wired` with no patterns loaded**, and the stdlib YAML reader missed
  `plugins.enabled` block sequences at indent 2. Both fixed; NotebookEdit's `notebook_path` now reaches the
  `.ipynb`-gated rules.

Accepted as documentation only: `agent-guard warp --apply` writes while Warp runs on purpose (the measured
rule from `warp-model-pin`: a write to a running Warp is honoured live, a pre-launch write is restored from the
account at startup); whether the inserted denylist survives a Warp quit/relaunch is **unmeasured** — re-run
`agent-guard warp --check` after a restart. A JSONC `always_deny` array holding an inline `//` comment is
refused (exit 2, file untouched); remove the comment and re-run. Rejected: "deleting the install block stays
green" — C14 runs inside `harness-verify` and greps for every wiring token.

## Mac propagation (fleet machine, in order)

1. `harness-sync pull` (clean fast-forward only; refuses on local edits).
2. `./install.sh` (copies `bin/agent-guard`, the data directory, the OpenCode plugin, the Hermes plugin;
   wires Claude; applies Warp and Zed where their settings exist; prints the Codex trust instruction).
3. `agent-guard status --gate` (must be green; a red names the host or the file).
4. `agent-guard selftest` (`passed: 151, failed: 0` expected).
5. `agent-guard trust-codex --apply` then `agent-guard trust-codex --check` (spawns `codex app-server`
   once; run by a human, never by a contract).
6. Restart Hermes so `command-guard` and `security-guidance` load.
7. Safe E2E probe on Claude and Codex: ask the agent to run `git push --force` from a non-git directory.
   Expected: the block reason names the matched pattern and the agent explains rather than retries.

## Skills adopted (governed manifest `skills/external-stack.json`)

- `decisions` (pinned upstream `skills/thinking-and-docs/decisions`, MIT, verbatim): decision records.
- `next-decision` (pinned upstream `skills/thinking-and-docs/next-decision`, MIT, verbatim).
- `research-prompt` (internal, adapted): upstream body with the execution section rerouted to this
  harness's research surfaces (`harness-scan`, the OpenCode `deep-researcher` agent / `/research`,
  Firecrawl `firecrawl_research_*`, `xsearch`) and the Tier-2 checklist verification rule.
- `agent-guard` (internal runbook adapted from upstream `global-agent-guardrails`): state check,
  add/tune a pattern, the per-host wiring table above, the gotchas, the safe E2E probe.

## Skills rejected (one reason each)

- `launch-subagent`: CLAUDE.md delegate/keep table + `/fanout` already govern this.
- `handoff`: `/wrap-up` + `docs/WIP.md` are the handoff primitive.
- `total-review`: `cross-review --merge` covers multi-reviewer review.
- `deep-research`, `risky-changes`, `signal-from-expert`: bound to DeepAPI, which this harness does not use.
- `agent-self-scheduling`: `loop-tick` + the cron doctrine forbid a clock-driven self-scheduler.
- `goal-loop`: overlaps the loop-engineering doctrine (event-driven acting, ratchet, SELECT stops).
- `effective-agent-skills`: `docs/skill-style-guide.md` is the house style.

## Verification

- `bash tests/agent-guard/run.sh` (C1-C15) and `bin/harness-verify` with the `agent-guard` row green.
- `bash tests/skill-stack-install/run.sh`, `bash tests/skill-manifest-hash/run.sh` with the four new
  manifest entries.
- Regression: `tests/opencode-event-plugin/run.sh`, `tests/harness-reentrancy/run.sh`,
  `tests/codex-stop-hook/run.sh`.
- Root-side after install: `agent-guard status --gate`, `trust-codex --apply` + `--check`, the E2E probe.

Related: `knowledge/plugin-first-integration-gate-2026-07-19.md` (the gate this decision followed),
`knowledge/zed-warp-first-class-2026-08-24.md` (why Warp and Zed are surfaces, not lanes),
`knowledge/harness-reentrancy` incident in CLAUDE.md (why no hook may start an agent).
