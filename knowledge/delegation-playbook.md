# Delegation playbook — the living best-practices doc behind `/cheap-delegate`

**What this is.** The routing brain for smart delegation: which lane runs a task
class at the lowest cost that still clears the acceptance contract. `/cheap-delegate`
reads it before every dispatch and **maintains it after every outcome** — this file
is state, not documentation.

**Maintenance protocol (the skill enforces this):**
1. **Evidence-first.** A routing rule changes only on an observed outcome (a run in
   this harness) or a verified external source — never on vibes. Every rule cites
   its evidence line in the log below.
2. **Supersede, don't append.** A contradicted rule is rewritten in place and the
   old rule moves to the evidence log as a dated "superseded" entry (same doctrine
   as memory governance). No two contradictory active rules.
3. **Date-stamp.** Every evidence entry carries a date; rules older than ~90 days
   without re-confirmation are suspect — re-verify before trusting a stale rule.
4. **Deterministic corpus stays in `win-log`.** `bin/win-log match --class <c>` is
   the KPI-gated record of *winning shapes*; this file holds the prose heuristics.
   When both speak, win-log evidence outranks a playbook rule.
5. **External refresh is gated.** Periodic best-practices intake may come from
   `/research` (OpenCode deep-researcher) or `bin/harness-scan`, but a fetched
   claim becomes a rule only after it survives a local outcome or a deterministic
   check (fetch is mechanical; judging applicability stays gated).

## Canonical routing matrix

Choose the topology before the model; model strength never changes the shape:

| Need | Route | Boundary |
|---|---|---|
| Design one nontrivial task | Claude → Fable · Codex → Sol Ultra · OpenCode → Kimi K3 (`/kimiplan`; `/oplan` is an alias) | Planner is read-only; Root reviews and executes |
| Execute one bounded task | `/cheap-delegate` · `$claudemaxxing:cheap-delegate` | One bounded execution task, never planning or decomposition |
| Execute independent chunks | `/fanout` · `$claudemaxxing:fanout` | Only after planning (or when design is clearly unnecessary) and with at least two disjoint chunks |
| Divide a broad multi-deliverable goal | `/gauntlet` · `$claudemaxxing:gauntlet` | Divides WHAT before each increment enters its host-native planner |

Model selection is orthogonal: DeepSeek V4 Pro handles dialogue, volume and
first passes; GLM 5.3 is the explicit complex-reasoning escalation (bin/oll's
MODEL_EFFORT_POLICY carries its measured `low` — see [R-glm53-adoption]); Kimi K2.7
Code handles bounded code-focused work; Kimi K3 handles planning and
long-horizon/multi-phase chains. K3 is included-first but higher-consumption,
so capability selects it. An HTTP 500 from GLM is `infra`, not a content
failure: retry the same lane once, then change runtime/family without a content
repair or a capability demotion.

For stateful OpenCode work, `bin/oll` is also the executable route authority:
`volume`→`deepseekv4-coder`/V4 Pro, `reasoning`→`glm-coder`/GLM 5.3,
`bounded-code`→`kimi-coder`/K2.7, `long-horizon`→`kimi-k3-coder`/K3,
`general`→`qwen-coder`/Qwen 3.5, `long-context`→MiniMax, and
`planning`→`kimiplan`/K3 read-only. `o` and `o-ubuntu` consume these profiles;
an explicit custom `--agent` is recorded as a caller override.

## The lanes (cheapest adequate first)

| # | Lane | Invocation | Cost profile | Best for |
|---|---|---|---|---|
| 1 | Ollama worker (one-shot) | `oll "<task>"` / `cat f \| oll ...` | Included quota; V4 Pro default | Response-only summarize/classify/draft/review over supplied context; `--reasoning` selects GLM 5.3 |
| 2 | OpenCode worker session | `o delegate <run-id> --profile <profile> --run-dir .results/delegation/<run-id> --json` · continue with `o send` · observe with `o output` · terminate with `o close` | Included quota; stateful and human-attachable | Workspace work; profile resolves the exact agent/model before launch |
| 3 | OpenCode specialist | `o delegate` with explicit `--agent deep-researcher` · `--profile planning` (`oplanner` compatibility alias) | Included quota | Cited research digests; read-only K3 implementation plans |
| 4 | Codex **Luna** | `MODEL=gpt-5.6-luna provider-ask openai "<q>"` (Codex-native: `codex exec -m gpt-5.6-luna -c model_reasoning_effort=low`) | ChatGPT Plus sub, fastest/cheapest tier | The default Codex rung: bounded cross-family second opinion, review, or code on a stated contract |
| 5 | Codex **Terra** | `MODEL=gpt-5.6-terra provider-ask openai "<q>"` (Codex-native: `-m gpt-5.6-terra -c model_reasoning_effort=medium`) | ChatGPT Plus sub, balanced | Balanced everyday coding or reasoning when Luna's answer is thin |
| 6 | Codex **Sol** | `MODEL=gpt-5.6-sol provider-ask openai "<q>"` (Codex-native: `-m gpt-5.6-sol -c model_reasoning_effort=high`) | ChatGPT Plus sub, slowest/most expensive tier | Hard bounded reasoning after the cheaper Codex tiers are inadequate |
| 7 | Claude Sonnet | `provider-ask anthropic "<q>"` | Claude sub (cheap frontier) | Frontier judgment on a bounded question when Ollama output needs a stronger check |
| 8 | Claude Opus | `MODEL=opus provider-ask anthropic "<q>"` | Claude sub (expensive) | Highest-stakes bounded second opinion — rare by design |
| — | **Keep in session** | (no delegation) | Orchestrator tokens | Cross-file architecture, risky edits, final sign-off/merge, anything needing full repo context |

**Kimi K3 boundaries:** `oll --model kimi-k3` is response-only over supplied
context; `o delegate <run-id> --profile long-horizon --run-dir
.results/delegation/<run-id> --json` is the stateful workspace lane; `o
delegate <run-id> --profile planning --run-dir
.results/delegation/<run-id> --json` is the read-only planning lane. Ollama
consumes included plan capacity first. K3 is included-first but
higher-consumption; route it for capability or 1M context, not as the universal
cheapest lane. Kimi K2.7 Code remains the bounded code-focused worker.

**Escalation ladder.** Start at the cheapest lane whose failure is cheap to detect
(iron rule: the contract, not re-reading, is the check). On contract failure: up to
**2 bounded repair rounds in the same lane**, feeding back a **structured failure
diff** (location + observed + admissible alternatives — arXiv:2607.14167, +44pp),
then escalate — normally one rung, but a **known-hard task class may go
direct-to-strong** (fixed one-rung chains are provably suboptimal there,
arXiv:2605.06350). The escalated lane receives **only the contract + failure
diff, never the cheap lane's attempt** (anchoring costs up to −18pp,
arXiv:2602.19509). A non-converging repair = spec-ceiling signal → keep it in
session; and remember the repair cap is a cost control, not a correctness
control, unless the contract is mutation-tight (arXiv:2608.05917 — `bin/mut`
gates high-value repaired chunks). Science trail:
`knowledge/delegation-science-2026-08-12.md`.

**The Codex rung is three tiers, never one.** Lanes 4–6 are the same backend
(`provider-ask openai` maps to `codex exec -m <model> -s read-only`), so the
`MODEL=` selector *is* the model choice; a bare `provider-ask openai` silently
rides the provider default instead of a chosen tier, which is why the tiers are
named rungs here and on both host surfaces. All three slugs — `gpt-5.6-luna`,
`gpt-5.6-terra`, `gpt-5.6-sol` — returned `ok` with exit `0` through both
`codex exec` and `provider-ask openai` on 2026-08-12; measurements and the
catalog's effort sets are in `knowledge/codex-model-levels-2026-08-12.md`.
Price/latency *ratios* between the tiers are `[unverified]`, so route on the
qualitative order (Luna cheapest → Sol strongest), not on a claimed multiplier.

**Hard keeps (never delegated regardless of lane):** final sign-off, merges,
doctrine/`install.sh` changes, anything whose acceptance contract can't be stated
cheaply (Tier-0 spec gate), credentials-adjacent work.

## Escalation sigil — `F.`

A leading `F.` token (followed by whitespace) in the task text means **the operator
declares this class known-hard**: strip the token, **skip lanes 1–3 entirely** (every
included-quota lane) and dispatch direct-to-strong — Codex **Sol** by default, or the
Claude lane when the question is Claude-shaped per the rule above. Pass `--override` to
`delegate-ledger record` so the ledger never reads a sigil-forced run as "the cheap lane
failed".

This is not new policy: the escalation ladder already allows a known-hard class to go
direct-to-strong (arXiv:2605.06350 — fixed one-rung chains are provably suboptimal
there). The sigil is how the human says "known-hard" inline, instead of the router
guessing it. Borrowed from a friend's harness (his `M.` forces the heavy local model):
when the operator *already knows* the task is hard, a one-token override beats a
classifier. `!`-prefixed alternatives were rejected — `!` collides with Claude Code's
bash-mode affordance and with shell history expansion in pasted commands.

Surfaces: the Claude command and the Codex skill mirror this one line each. **OpenCode
gets no sigil** — it has no cheap-delegate surface at all; there, the agents *are* the
lanes, chosen with Tab.

## Delegation ledger — routing from evidence instead of prose

`bin/delegate-ledger` (`knowledge/delegation-ledger.jsonl`) records **one row per lane
attempt**, pass *and* fail *and* infra — the half `win-log` refuses to hold, because
`win-log` is a corpus of wins by design. Read it with
`delegate-ledger stats --class <c> --json` before dispatch, and write to it with
`delegate-ledger record` at receipt time, once per lane attempted.
The run-dir receipt itself is written by `delegate-ledger receipt --run-dir <dir>
--run-id <id> --class <task-class> --lane <lane> --verdict <pass|fail|infra>` — same
tool, closed field set, `task_class` required, and the SHA-256s measured from the run
dir rather than declared, so a hand-authored receipt is never correct. `record --run-dir` then cross-checks its verdict against that receipt, which
is why the two are separate calls: one call writing both would compare a value to itself.

**Authority order.** Deterministic evidence outranks prose: `win-log` and a
`delegate-ledger stats` result whose `authority` is `sufficient` both beat a rule
written here. A stat whose `authority` is `advisory` **never** overrides a rule — and
it structurally cannot be quoted as a ranking, because an advisory stat omits the
`preferred_lane` key entirely rather than caveating it. When `win-log` and the ledger
disagree, the most recent evidence governs and Root re-verifies before trusting either.

**Why the ledger is not derived from receipts.** `.results/` is gitignored (0 tracked
files), so receipts are machine-local and ephemeral, while a routing table needs
cross-machine, long-horizon evidence. And the receipts drifted: measured 2026-08-17,
41 distinct keys across 10 files, 8 spellings for 4 lanes, and `task_class` present in
1 of 10. So the ledger is a primary write with a closed field set, cross-checked
against the receipt when `--run-dir` is passed; `delegate-ledger import` backfills the
old receipts best-effort and **skips loudly** rather than guessing a class.

<!-- durable-delegation-gate:begin -->
**Physical dispatch gate (all lanes).** Before dispatch, Root creates
`.results/delegation/<run-id>/contract.md` and sibling `brief.md`, marks both read-only,
and checks both are regular and non-empty. The worker never authors, alters, grades, or certifies
the contract. OpenCode starts with `o delegate`; repairs use `o send`; `o handoff`
writes `output.tmp`, published as `output.md` only after exit 0; `o output` is diagnostic;
`o close` ends every path. After acceptance, write `receipt.json` with
`delegate-ledger receipt` (closed fields, measured SHA-256s).
Every newly authored OpenCode `brief.md` contains exactly one non-empty
`<!-- o-delegate-turn-1:begin -->` … `<!-- o-delegate-turn-1:end -->` block with
the complete bounded assignment. `o delegate` executes that assignment immediately
in turn 1; an unmarked legacy bounded brief executes as a whole. Never use turn 1
as a read-only bootstrap for a later assignment: `o send` is repair-only, never the
initial task.
Keep prompts, credentials, and sensitive context out of receipts and `/tmp`.
`oll` is allowed only for response-only work over supplied context; Root captures
stdout. Stateful workspace work and repairs require the public `o` worker runtime;
`occ` remains an internal one-shot transport behind `o`.
<!-- durable-delegation-gate:end -->

An unclosed OpenCode worker is a leak, not a pause: its tmux session holds a live
OpenCode process plus the MCP servers it spawned, and nothing else reclaims them.
Root closes the session as part of writing the receipt; `o delegate` sweeps first,
so a forgotten close costs one idle worker rather than an unbounded pile of them
(`o reap [--idle-minutes N]`, tag-scoped to delegation-born sessions only).

## Model attribution — name the model, measured not recalled

Every dispatch announces, and every report attributes, **which model ran which
task**. A lane is not a model: `o delegate --profile reasoning` is a lane,
`glm-coder` is an agent, and `glm-5.3` is the model. Without the slug a run is
unreadable after the fact — measured 2026-08-30, 4 of 31 ledger rows carried no
`model` at all and 3 more recorded the *agent* `kimi-coder` in that field, so the
ledger could not answer "what actually ran" for a fifth of its own corpus.

**Resolve the slug; never recall it from a table.** Every lane already emits its
own model, so the announced and reported slug is read from the run — prose drifts,
the tool does not (same rule that keeps `bin/oll` the route authority above):

| Lane | Where the slug is measured |
|---|---|
| `oll` one-shot | the stderr banner `[<model> \| in N / out N tok]` printed on every call |
| `o delegate` | the `model` field of its `--json` result (beside `agent`, `profile`, `selection_source`); resolvable **before** dispatch with `oll --route-profile <profile>` |
| Codex Luna/Terra/Sol | the `MODEL=`/`-m` slug you passed — a bare `provider-ask openai` rides the provider default (`gpt-5.5`), which is no tier at all |
| Claude Sonnet/Opus | the `MODEL=` selector; `sonnet`/`opus` are **aliases**, so report them as aliases rather than inventing a version slug |

**Report shape.** The closing report opens with one line **per lane attempt** —
the same rows `delegate-ledger record` writes, so the prose and the ledger cannot
disagree:

`<subtask or step> — <lane> · <model slug> · <verdict> (attempt N[, R repairs])`

Repairs and escalation rungs each name their own model: a chunk that ran V4 Pro,
was repaired on V4 Pro, then escalated to Sol is three attributed lines, not one.
A single-lane single-attempt run is one line — the shape does not scale down to
zero. Surfaces: the Claude command and the Codex skill each mirror this rule at
dispatch and at report; `tests/cheap-delegate/run.sh` V15 enforces it.

## Codex lanes

This section is the active Codex-host routing overlay. It supersedes the generic
“Codex / GPT” row above for self-delegation from Codex; the shared lanes and
maintenance protocol remain authoritative, and `win-log` evidence still
outranks this prose.

| Order | Lane | Invocation | Route when |
|---|---|---|---|
| 1 | Ollama one-shot | `oll "<brief + contract>" --model glm-5.3` | Included-quota response-only text, transforms, triage, or review over supplied context (its measured effort `low` is applied by MODEL_EFFORT_POLICY) |
| 2 | OpenCode worker | `o delegate <run-id> --profile volume\|reasoning\|bounded-code\|long-horizon\|general\|long-context --run-dir .results/delegation/<run-id> --json` | Workspace work; repair via `o send`, retrieve the typed turn with `o handoff`, then `o close`; `o output` is diagnostics |
| 3 | Codex Luna | `codex exec --ephemeral --skip-git-repo-check -s read-only -m gpt-5.6-luna -c model_reasoning_effort=low '<brief + contract>'` | Fastest/cheapest bounded Codex task (live-probed 2026-08-12 from the orchestrator shell) |
| 4 | Codex Terra | `codex exec --ephemeral --skip-git-repo-check -s read-only -m gpt-5.6-terra -c model_reasoning_effort=medium '<brief + contract>'` | Balanced everyday code or reasoning (live-probed 2026-08-12 from the orchestrator shell) |
| 5 | Codex Sol | `codex exec --ephemeral --skip-git-repo-check -s read-only -m gpt-5.6-sol -c model_reasoning_effort=high '<brief + contract>'` | Hard bounded reasoning after cheaper lanes are inadequate (live-probed 2026-08-12 from the orchestrator shell) |
| 6 | Claude | `provider-ask anthropic "<brief + contract>"` | Cross-family opinion or refutation |
| — | Keep in root | no dispatch | Final sign-off, merges, doctrine/`install.sh`, credentials, cross-file architecture, or an acceptance contract that is not cheap to state |

Codex routing keeps the shared Tier-0 gate and escalation rule: author the
contract plus self-contained context first, verify only by that contract, allow
at most two same-lane repairs with a structured failure diff, then move one rung.
Use `provider-ask openai --model <id>` or `MODEL=<id> provider-ask openai` —
both work as of 2026-08-12 (the env path was silently clobbered by a bare
`MODEL=""` init until the Codex cross-review caught it; superseded note in the
evidence log).

### Codex lane evidence (newest first)

- **[C14] 2026-08-19** — Cross-host `kimi-coder` transport smoke: immutable
  contracts on Ubuntu and Mac required the exact 15-byte final text. Both real
  OpenCode sessions returned `completed_retrievable` with turn 1 plus durable
  session/message IDs, and both worker trees were closed after Root's byte check.
  The Mac run also reproduced a 1.18.5 startup composer with no `Ask anything`;
  startup now accepts the rendered version footer, while `pending=1` prevents
  that persistent footer from making a busy turn look ready. Cost class:
  included subscription; receipts: `dl-0f3b0c97`, `dl-13202a98`.

- **[C13] 2026-08-19** — Cheap-delegate, task class
  `delegation-runtime-reliability`: OpenCode `kimi-coder` produced the requested
  five-case audit file in 2m20s, but 8 source citations violated Root's exact
  one-line literal gate. The output contract **FAILED**; the same-session repair
  was refused as `not_ready` even though the pane showed the worker idle, directly
  reproducing the transport defect under investigation. Root accepted no worker
  verdict, recorded the failed lane, and implemented the coupled runtime/plugin/
  doctrine change directly. Cost class: included subscription; playbook change:
  durable results now use event-bound `o handoff`, while `o output` is diagnostic.

- **[C12] 2026-08-20** — OpenCode `glm-coder`, task class
  `bounded-planner-leaf`: implemented only `opencode/{agents,commands}/kimiplan.md`
  plus the `bin/task-plan` mapping under immutable Root-authored contract files.
  Root's checks passed first round: 7 pytest cases, the real tmux/cwd/task-link
  boundary, and `bin/mut` 4/4 killed with zero survivors. Cost class: included
  Ollama Max capacity; 0 repairs; worker session closed through `o close`.
  Quirk: GLM was the bootstrap writer because installed `kimi-coder` still
  pointed at K2.7 until this deployment.

- **[C11] 2026-08-16** — Cheap-delegate, task class `code-review` for the
  Cogload binary and Hermes Orchestrator surfaces on Ubuntu/macOS: OpenCode
  `glm-coder` ran the bounded repo contracts and surfaced source-grounded leads,
  but its final assistant response was prose plus `{}` instead of the required
  raw JSON artifact. The output contract **FAILED**; one same-session repair was
  refused as `not_ready`, so Root accepted no worker verdict and completed the
  live causal diagnosis directly. Cost class: included subscription; measured
  worker time 298 seconds. Quirk: a worker-authored JSON file outside the frozen
  run directory does not rescue a malformed assistant handoff.

- **[C10] 2026-08-16** — OpenCode `glm-coder`, task class
  `code-surface-inventory` for the Ubuntu 26.04 post-upgrade audit: Root froze a
  30-row JSONL contract before dispatch, and the worker grounded every relied-on
  runtime surface to a repo-relative file plus a byte-for-byte literal. Root's
  deterministic validator passed 30/30 rows, all nine required categories,
  read-only commands, unique names, and zero missing source literals; the shared
  tracked diff/status hashes stayed byte-identical. No repair or escalation was
  needed. Cost class: included subscription; measured worker time 66 seconds.





- **[C5] 2026-08-14** — OpenCode `glm-coder`, task class `structured-analysis`
  transport repair: local OpenCode 1.17.9 proved that default formatted output
  mixes progress/tool presentation with assistant content, while official
  `--pure --format json` exposes typed events. `occ --final-output` now emits
  exactly one terminal assistant text and rejects schema drift with exit 65;
  the caller contract still rejects malformed assistant JSONL unchanged.
  Black-box contract 6/6 and a live `LIVE_FINAL_OUTPUT` probe passed. Cost class:
  included subscription; no `win-log` record because this repaired routing
  infrastructure rather than delivering a delegated task artifact.

- **[C4] 2026-08-14** — Cheap-delegate, task class `analysis` / bounded
  article-to-repo gap matrix: Ollama `glm-5.2` exhausted 6,000 output tokens
  without a response, then a source-sliced 10,000-token repair returned `0/0`
  usage and empty output. Escalation to OpenCode `glm-coder` first misresolved
  relative skill paths outside the repo; an absolute-path repair read all six
  sources and produced the eight requested rows, but added prose and a trailing
  `{}`, so the strict JSONL contract **FAILED**. Root accepted no worker verdict,
  recorded no `win-log` win, and kept synthesis/sign-off in-session. Cost class:
  included subscriptions; quirk: useful content does not rescue a failed output
  contract, and repeated empty output triggers the no-progress brake rather than
  more same-lane retries.

- **[C3] 2026-08-14** — Ollama `glm-5.2`, task class
  `document-synthesis` / functional-vs-non-functional classification: a
  source-sliced one-shot returned exactly 49 JSONL objects (`R-01…R-24`,
  `C-01…C-25`), excluded `R-25/R-26`, preserved `C-23/C-25`, and marked
  `R-21` as received. Root's deterministic ledger gate confirmed 31 F / 18 NF,
  one occurrence per ID and no invented actors/timestamps. No repair or
  escalation was needed; final wording and cross-artifact sign-off stayed in
  root. Cost class: included subscription.


- **[C1] 2026-08-12** — `codex-cli 0.147.0` and its refreshed local model
  catalog list `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`; live
  orchestrator-shell probes returned `ok` with exit `0` for Luna/low,
  Terra/medium, and Sol/high. The same three tiers also returned `ok` through
  `MODEL=<id> provider-ask openai`; the earlier env clobber was fixed by
  preserving inherited `MODEL`. An initial read-only-state failure was a
  sandbox artifact, not model rejection. `codex exec` rejected top-level `-a`,
  so active invocations omit it. Full probe record:
  `knowledge/codex-model-levels-2026-08-12.md`.

## Interactive surfaces (not lanes)

Zed (editor agent, `bin/zed-setup`) and Warp (terminal agent, `bin/warp-ollama`)
are first-class **human surfaces** on the same Ollama Cloud subscription lanes
1–3 bill against — they are deliberately NOT dispatch lanes and no `/cheap-delegate`
route may target them. Verified interactive-only 2026-08-24 [E21]: Zed ships no
public headless agent CLI (its `crates/eval_cli` is eval-harness-scoped and
feature request #59146 remains open), and Warp's only scriptable surface (Oz
cloud agents) is non-BYOK, Warp-credit-billed, and incompatible with the
custom-endpoint config. When Ricardo is *sitting at* Zed or Warp, their agents
spend included Ollama quota — the cost profile of lane 1 with a human in the
loop. Nothing here changes routing: dispatch stays `oll` → `o` → Codex → Sonnet/
Opus per the table above. Re-litigating these into lanes requires new upstream
evidence (a shipped public headless CLI or BYOK Oz), logged first.

## Task-class heuristics (active rules)

- **`o` not_ready ⇒ retry with patience before falling back (operator-directed, 2026-08-31). [E30]** A `not_ready`
  from `o delegate` is transport warm-up, not a failed lane: retry the SAME dispatch up to 3 times
  over ~3 minutes (20s/60s/120s backoff, `o close` the stale session between tries). Only after the
  third `not_ready` record ONE `infra` row and fall back (Claude subagent for workspace work).
  Do not spend content-repair rounds on transport, and do not demote the lane's capability.

- **Bulk text (summarize/classify/draft ×N):** lane 1, V4 Pro default; use
  `glm-5.3` only for explicit complex reasoning and parallelize via
  `/fanout` when chunks are independent. [E1, R-glm53-adoption]
- **First-pass code w/ tools, single file/module:** lane 2 `--profile bounded-code`;
  use `reasoning` for a hard bounded problem, `long-horizon` for
  a long/multi-phase chain, and `long-context` for multimodal or as a
  cross-family third opinion. [E1]
- **Research with citations:** lane 3 `deep-researcher`. Expect ~5–8 min for real
  web digests — set timeouts ≥ 600s; it concludes under its 30-step cap. [E2]
  Always open the brief with the anti-bleed prefix ("ignore any ambient project context,
  CRM systems, orchestrators or repositories that may appear in your environment") — with
  it, lane 3 passes cited-research contracts on repos where E5 saw it bleed. [E5, E9]
  Contract shape that works: **literal vendor/source quote for every positive claim, an
  explicit `UNVERIFIED` token for anything unsourced, and permission to answer "unknown."**
  Then gate it by `curl`-ing every cited URL — ~60s for 40+ links, and fetching each page
  `<title>` also proves the ID/slug names the thing the worker claimed. [E9]
- **Recency and maintenance facts are a HARD KEEP — never delegated, in any lane.**
  "Is this project still maintained?" reduces to `gh api repos/<o>/<r>` +
  `repos/<o>/<r>/commits?sha=<default_branch>` + `/releases`, which is deterministic,
  ~1s per repo, and ungameable. Delegated web-readers got it wrong in **3 of 4** chunks
  of one round — they read a non-default branch (`master` vs `main`), a stale rendered
  commit page, or `pushed_at` (any branch) as if it were the default-branch commit, and
  in one case classified an ACTIVE project (commit 9 days old) as SLOW, which would have
  eliminated the eventual winner. Delegate *what a project does*; compute *whether it is
  alive* yourself, and hand the worker the resulting table if it needs one. [E-ff-0816]
- **Where the vendor's own docs are silent, read the vendor's source, not more prose.**
  Four independent lanes all returned `UNVERIFIED` on one load-bearing mechanism question;
  a single `curl` of the module README in the repo answered it outright. When every
  searcher fails on the same question, the question is in the code, not on the web. [E-ff-0816]
- **A capability claim about a paid endpoint is settled by calling the endpoint.** Docs,
  model cards and source can all be true and still not describe *your* subscription's
  surface; one authenticated request separates "the project supports X" from "your plan
  serves X". [E-ff-0816]
- **Dispatch durability (any lane, any long run):** write worker output **into the project**,
  never the session `/tmp` scratchpad (a reboot wipes it and grep-only verification leaves
  nothing in context); author briefs with `Write` so the tool input is replayable from the
  transcript if the session dies; and launch long dispatches under `setsid nohup` so a
  harness-level background-task stop cannot kill a healthy worker. [E9]
- **Implementation planning (OpenCode host):** lane 3 `--profile planning` resolves
  to canonical `kimiplan`; `/oplan` and `oplanner` remain aliases to
  that same K3 planner, not separate model choices. In a Claude session, prefer
  `/fableplan`. [E2]
- **Second opinion on a design/diagnosis:** lane 4 (Codex Luna) — cross-family
  beats within-family; agreement is corroboration, never proof. Use lane 7/8
  (Claude Sonnet/Opus) when the question is Claude-shaped (long nuanced context,
  writing quality). Always name the lane's model, not only its number: the
  2026-08-12 Codex-tier split ([C1]) renumbered Sonnet 5→7 and Opus 6→8, and two
  rules here kept pointing at the old slots — which by then held Codex Terra/Sol,
  silently routing Claude-shaped questions cross-family. [E1]
- **Cheap frontier check of a worker's output:** lane 7 (Claude Sonnet) on the
  *contract checklist*, not the raw output (Tier-2 boolean scan). [E1]
- **Visual / craft output (design, rendering, layout, art direction):** every cheap lane is
  text-only, so **direction accept/reject** stays in-session. Delegate the literature sweep (lane 3
  `deep-researcher`), code variants (lane 2), and — this is the [E8] refinement — the
  **source-level design audit** (lane 1): cheap lanes read CSS/HTML tells reliably when the
  contract forces literal citations that you then verify with `grep -F -e`. Pair it with a
  cross-family bounded second opinion (lane 4, Codex Luna) on the *direction* question; independent
  convergence is strong corroboration. Build a headless screenshot + deterministic grader first —
  it converts taste into a contract and is worth more than any lane choice. [E4, E8]
- **Dispatch mechanics (OpenCode):** `o delegate` is the public stateful boundary;
  repairs use `o send` on the returned exact session, `o handoff --json` retrieves
  the typed current-turn result, and `o output --json` is bounded diagnostic
  observation, not the handoff or acceptance gate. Internally, any outer one-shot
  supervisor timeout must exceed occ's
  full retry budget — ≥ 2×(`--timeout`) + 60s — or a healthy-but-slow dispatch
  gets killed mid-retry and reads as a worker failure (artifact, not signal).
  When a contract grades structured assistant output, use `--final-output`: it
  isolates OpenCode plugins, structurally extracts one terminal text event, and
  never repairs assistant content. Exit 65 is transport/schema failure, not a
  worker verdict. [E3, E12, E14, E19]
- **A delegated chunk's contract must not be satisfiable by NAMING.** Assertions
  of the form `"fail_streak" in src` pass when a worker writes the vocabulary
  without the behaviour — observed live: a fully green suite over a chunk whose
  primary requirement was never implemented. Every delegated behavioural
  requirement needs at least one assertion that *executes* the code path
  (call the function, inspect the returned value). Grep assertions are fine as a
  cheap supplement, never as the proof. [E7]
- **A silent worker is an artifact, not a verdict.** Zero output + zero edits =
  infrastructure (occ's lq-6e4c38c5 startup hang), so do NOT demote the lane or
  count it as a capability failure. Skip the sibling rung that shares the same
  runtime and go to the next distinct one, or pull a small well-specified chunk
  in-session — re-dispatching after a hang usually costs more than writing it. [E7]
- **Shared-tree hygiene is part of the dispatch, not the reviewer's job.** Every
  worker prompt on this repo carries an explicit ban on `git stash`/`checkout`/
  `restore`/`reset`/`clean`, and the caller records the peer's `git diff` hash
  before and after so a clobber is provable rather than assumed. [E7]
- **Plan→implement chaining:** a `kimiplan` plan's acceptance contract can be
  handed verbatim to a lane-2 profile as the dispatch contract — the plan run pays
  for itself as the Tier-0 spec gate. [E3]

## Evidence log (newest first)

### [E30] 2026-08-31 — `o delegate` not_ready is transport warm-up, not a lane failure (operator-directed)
- Context: several cheap-delegate runs on 2026-08-30/31 were logged as OpenCode lane failures when the
  typed result was `not_ready` — the tmux/OpenTUI worker had not finished booting (the fix that day
  delayed OpenTUI birth until tmux has an attached client; see docs/changelog 2026-08-31 14:23 and
  2026-08-30 21:33). Re-dispatching the SAME brief after a short wait succeeded without repair.
- Rule derived: treat `not_ready` as retryable (up to 3 attempts with backoff) before falling back to
  the next lane; only a typed model/contract failure demotes the lane in the ledger.
- Verdict: operator-directed (recorded here so the active rule carries an evidence tag; the
  ledger rows that motivated it are the 2026-08-30/31 `o` infra rows, `delegate-ledger stats --class ops`).


### [E3] 2026-08-11/12 — integration rounds (E1–E3, inline form)

- **[E3] 2026-08-12** — First real `/cheap-delegate` round (`o ls --json`):
  lane 3 `oplanner` authored the plan+contract, lane 2 `occ --agent glm-coder`
  implemented it, orchestrator ran contract C1–C5 → all green, `harness-verify`
  0 errors, zero subscription tokens (win-8b5ceda5). One false failure: the
  outer bash timeout (330s) was smaller than occ's retry budget (2×300s) and
  killed a healthy dispatch — codified as the dispatch-mechanics rule above.
- **[E2] 2026-08-12** — Live `o` runs: `oplanner` (deepseek-v4-pro) produced a
  correct, grounded plan (17 read-only tool calls, file:line anchors, 5-check
  acceptance contract) in one shot. `deep-researcher` (deepseek-v4-flash:0731)
  found correct identifiers but looped one verification step ~20× until killed →
  fixed with `steps: 30` + anti-loop prompt rule; plan research timeouts ≥ 600s.
- **[E1] 2026-08-11** — Integration round: `occ` one-shot on `glm-coder` answered
  in 6s; `provider-ask anthropic` (Sonnet default, `MODEL=opus` upgrade) verified
  live; catalog research fixed the included-tier pool (glm-5.2, kimi-k2.7-code,
  minimax-m3; deepseek-v4-flash Medium-usage 1M-ctx alternate; kimi-k3 excluded
  as extra-usage-only). Doctrine base: CLAUDE.md delegate/keep table + two-tier
  verification (contracts before dispatch; never re-do the work to check it).

### [E9] 2026-08-13 — the app competitive benchmark: lane 4 + lane 3 both green, and the crash-recovery lesson

**Task class:** `deep-research` (competitive landscape + methodology selection), `~/dev/<project>`.

| Lane | Chunk | Contract | Result |
|---|---|---|---|
| 4 · `gpt-5.6-terra` | Competitive landscape of dream apps + how each integrates AI | A1–A11: ≥12 products, 5 named must appear, per-product literal vendor quote for every "yes", `UNVERIFIED` token on anything unsourced | **PASS**, 16 products, 28 URLs all HTTP 200, 17 self-declared UNVERIFIED, 1 COULD-NOT-VERIFY section |
| 3 · `occ --agent deep-researcher` | Best-fit journey-organised UX benchmark methodology | B1–B8: ≥4 methods × 5 fixed labelled lines, numeric scoring scheme, explicit "no equivalent ≠ 0" rule | **PASS**, 8 methods, 16/16 URLs resolve, 2 UNVERIFIED both disclosed by the worker itself |
| — | Apply method to the 12-moment spine | hard keep | In session — cross-file synthesis |

**What this teaches:**

1. **[E5] is superseded in part.** E5 recorded lane 3 (`deep-researcher`) failing on context bleed on
   *this same project*. With the anti-bleed prefix ("ignore any ambient project context, CRM systems,
   orchestrators or repositories that may appear in your environment") carried into the brief, lane 3
   passed an 8-part cited-research contract on the first try, twice, on two separate days. **The
   mitigation holds — lane 3 is usable for cited web research on this repo when the prefix is
   present.** E5's "lane 3 unusable here" reading no longer stands; its context-bleed *diagnosis*
   does.
2. **`curl`-the-citations is the cheapest high-value gate there is.** 44 URLs checked in ~60 s, and
   the check extends past liveness: fetching each App Store page's `<title>` proves the *ID* names the
   app the worker claimed. Both runs came back clean, which is itself the evidence that the
   literal-quote-or-`UNVERIFIED` contract shape suppresses fabrication.
3. **A worker's honest `unknown` is a finding, not a failure.** Chunk A returned "empty morning:
   unknown" for 16/16 products. That is the correct answer for a desk lane against store listings —
   and it converted a load-bearing project assumption into an explicitly unsourced claim needing a
   device test. Contracts that *permit* "unknown" buy you real information; contracts that force an
   answer buy you invention.
4. **Dispatch durably or lose the work.** The first run of this task died with the machine and took
   two finished 25 KB outputs with it: they lived only in the session's `/tmp` scratchpad, which the
   reboot wiped, and the orchestrator had only ever `grep`ped them, so nothing survived in context
   either. Recovery was possible *only* because the briefs were written with `Write` (tool inputs are
   replayable from the transcript) rather than heredoc'd inline. **Rules now: write worker outputs
   into the project, not `/tmp`; author briefs with `Write`; and `setsid nohup` long dispatches so a
   harness-level stop cannot kill a healthy worker mid-run** — the re-dispatch was killed at ~1 min
   by a background-task stop before this was applied.

### [E8] 2026-08-13 — Adversarial audit of a commercial proposal + its prototype: lane 1 ×2 + lane 4, all green first try

**Task class:** `adversarial-review` (document critique + anti-slop design audit), a university proposal.

| Lane | Chunk | Contract | Result |
|---|---|---|---|
| 1 · `oll glm-5.2` | Commercial/clinical adversarial critique of a 17 KB Spanish proposal | 7 assertions (exact count, closed-set defect classes, paste-ready replacement per finding, ≥2 on pricing, ≥2 on liability, no invented norms) | **PASS 7/7**, ~19.6 KB brief, one shot |
| 1 · `oll glm-5.2` | Anti-AI-slop audit of a 53 KB self-contained HTML prototype | 6 assertions incl. **B2: every cited CSS selector/token must exist in the file** | **PASS 6/6**, 24/24 citations verified by `grep -F` |
| 4 · `gpt-5.6-terra` | Bounded 4-question second opinion on design direction | prose, judged in-session | Converged with lane 1 independently |

**What this teaches (three transferable moves):**

1. **"Every citation must exist in the source" is the cheapest real contract for a
   review task.** It converts a subjective audit into a `grep -F` check and kills the
   dominant failure mode of delegated review — fabricated evidence. 24/24 verified in
   one command. Generalize: any delegated critique of a file should require literal
   quotes, then verify them mechanically. This is the review-task analogue of [E7]'s
   "contract must not be satisfiable by NAMING."
2. **A closed-set label + a mandatory paste-ready rewrite line kills waffle.** Forcing
   `REEMPLAZO:` with concrete text (and banning "considerar revisar") produced findings
   that were directly usable; no round of "make this actionable" was needed.
3. **Rule [E4] held and sharpened.** Cheap lanes read *code* for design tells extremely
   well (the worker independently flagged the ivory+serif+teal triad, an orphan
   `--elev-raised` token, and a uniform-elevation system). What stayed in-session was
   only the *accept/reject* on direction. Refined statement of [E4] below.

**Quirks:** `grep -qF "$s"` parses a leading `--token` as a flag — use `grep -qF -e "$s"`
or the verification reports false MISSes and you will wrongly fail a green worker.
Cost class: two included-quota calls + one ChatGPT-sub call.

### [E9] 2026-08-13 — Orchestra practice loop: Luna core + Terra CLI, contract-first

**Task class:** governed orchestration policy/runtime, split only at a stable API boundary.

| Lane | Chunk | Result |
|---|---|---|
| 4 · `gpt-5.6-luna` low | canonical catalog/evaluator under root-authored negative fixtures | 17/17; one critic-driven repair; mutation 0.99, sole survivor equivalent `None` assignment |
| 4 · `gpt-5.6-terra` medium | bounded relocatable CLI against eight prewritten cases | 8/8; mutation 1.00, 81/81 killed |
| 1 · Qwen 3.5 397B | final refutation | infrastructure-empty after exhausting output budget; no capability demotion |
| 1 · Mistral Large 3 | bounded current-source critic | confirmed legacy compatibility + shortcut removal; surfaced completion-gate observability, tightened in root |

**Rule earned:** delegate a coupled policy system only where a deterministic
boundary already exists. Here the pure catalog API and CLI were cheap; schema,
acceptance semantics, migration, and cross-host rollout stayed in root. A critic
must receive current source, not a unified diff: Mistral initially treated a
deleted `completed_count >= 3` line as live code. Diff reviewers need explicit
polarity or current-source excerpts.

