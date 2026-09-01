# fableplan skill — validation harness

Validates that `/fableplan` (`.claude/commands/fableplan.md`) and its `fable-planner`
agent (`.claude/agents/fable-planner.md`) delegate to Codex/Sol correctly. Two tiers —
run the cheap one freely; run the expensive one only after touching the routing or
critique logic.


> **2026-08-19 — marker renamed, gate unchanged (playbook `[E21]`).** `SOL-ASSIST` is now
> `DELEGABLE`, and a third marker `ROOT-ONLY` lets the planner declare a Tier-0 spec ceiling
> at plan time. The rename is because the *lane* is now chosen at execution time by the
> delegation router (`delegate-ledger stats` + `knowledge/delegation-playbook.md`) rather than
> fixed to Sol at plan time — a `DELEGABLE` chunk now starts at lane 2 (`o delegate`, included
> quota) and reaches Sol only by escalation. **The three-part independence gate that decides
> *whether* to split is byte-for-byte unchanged**, so the results recorded below still measure
> the live behaviour; read every `SOL-ASSIST` in this file as `DELEGABLE`. What those cases
> did *not* measure, and still don't, is which lane a split chunk should land in — that
> evidence now accrues in the delegation ledger. Routing is enforced separately and
> deterministically by `tests/cheap-delegate/run.sh` **V14**.

---

## Cheap tier — deterministic, seconds, no model spend

Run any time. This is what proves the harness is *wired* correctly.

**1. Static lint** of the installed files (or the tracked source under `.claude/`):

- No forbidden flags outside prose: `--strict-config`, `--full-auto`, `--yolo`,
  `--ask-for-approval`, `--include-plan-tool` (none exist in codex-cli 0.144.6 /
  `--strict-config` aborts on this user's config).
- `ultra` appears only in the sentence forbidding it (it's an orchestration mode, not
  a reasoning effort — `max` is the deepest single agent).
- Every `codex exec` block carries: an explicit `-s`, `--ephemeral`,
  `--skip-git-repo-check`, `--ignore-user-config`, and a **pinned**
  `-c model_reasoning_effort=<effort>` (ignoring config silently drops effort to `none`).
  The planning-side blocks — Sol's critique and the planner's consult — pin `high` and take
  the surviving `gpt-5.6-sol` default with no `-m`. The step-5 execution-chunk block is the
  escalation rung above lane 2, so it pins `-m` **and** the tier's matching effort together
  (`gpt-5.6-luna`/`low` → `gpt-5.6-terra`/`medium` → `gpt-5.6-sol`/`high`); one without the
  other is the bug the lint exists to catch.
- Structural bits present: `EXECUTION SHAPE` in the planner; the "paste verbatim" rule
  and the "Sol's review is not user consent" line in the skill; artifact-dir references.
- Frontmatter parses; `fable-planner` `tools:` line unchanged.
- Installed `~/.claude/…` copies are byte-identical to the tracked source (`diff`).

**2. Live smoke** — one minimal hermetic call in the exact block form:

```bash
ART=~/.claude/fableplan-artifacts; mkdir -p "$ART"
BASE="$ART/VALIDATION-smoke-$(date +%Y%m%d-%H%M%S)-$$"
printf 'Reply with exactly: HARNESS-OK\n' > "$BASE.prompt.txt"
codex exec -s read-only -C "$PWD" --skip-git-repo-check --ephemeral \
  --ignore-user-config -c model_reasoning_effort=high \
  -o "$BASE.md" - < "$BASE.prompt.txt" 2>&1 | grep -iE 'model:|reasoning effort:'
# expect: model gpt-5.6-sol, reasoning effort high, exit 0, "HARNESS-OK" in $BASE.md,
# and the prompt+output pair on disk. Delete the two smoke files after (keep the
# audit trail clean).
rm -f "$BASE.prompt.txt" "$BASE.md"
```

---

## Expensive tier — behavioral, ~5 Fable planner runs + Sol calls

Bracket the two behaviors a single run can't prove:
1. the Sol consult (touchpoint 2) stays **≤1** and does **not** fire on trivial work;
2. **SOL-ASSIST** (touchpoint 3) is proposed only for genuinely independent chunks.

**Method:** instantiate each archetype below against real files in a **real repo** (the
planner needs real code to ground in), then hand each brief to the live `fable-planner`
agent exactly as `/fableplan` would. **Do not tell the planner it is being evaluated.**

### What's gradeable vs not

- **Hard-checkable (fail = defect):** all six sections present incl. `EXECUTION SHAPE`;
  consult count ≤1 (structural); consult disclosed in `RISKS` if it fired; a
  cohesive/sequential task stays `OPUS-DIRECT`; a trivial task does not consult.
- **Recorded, NOT graded:** whether a should-consult fork actually consulted, and
  whether the clearest independence case actually chose `SOL-ASSIST`. Evidence predicts
  these fire rarely; not firing is acceptable, not a bug. Log it to build the usage
  picture, not to pass/fail.

### Case archetypes

- **A — trivial + cohesive** → expect `OPUS-DIRECT`, no consult. (e.g. add a one-line
  docstring.) PASS: `OPUS-DIRECT`; no consult; 6 sections. Also OK: planner says it's
  too trivial to plan.
- **B — cohesive sequential feature** → expect `OPUS-DIRECT`, no consult. (e.g. a flag
  threaded parse→dispatch→side-effect through one file.) PASS: `OPUS-DIRECT`; no
  `SOL-ASSIST`.
- **C — two independent deliverables, one Sol-strong** → `SOL-ASSIST` candidate. (Two
  deliverables sharing no code; one is a standalone mechanical script.) CHECK: the
  cohesive chunk stays `OPUS-DIRECT`. RECORD: does it route the independent script to
  `SOL-ASSIST` with its own file list + own runnable contract? This is the clearest
  independence signal — if even this stays `OPUS-DIRECT`, `SOL-ASSIST` is effectively
  dormant (data, not a fail).
- **D — genuine design fork, unsettleable by reading** → consult candidate; the bound is
  the test. (e.g. choose between two retry policies and justify.) PASS: consult ≤1; 6
  sections; if consulted, `RISKS` discloses it. FAIL only if >1 consult or a section
  missing.

### How to re-run

Pick a repo with real code. Write one small brief per archetype targeting real files in
it, hand each to `fable-planner`, and score against the PASS lines above. Append a dated
results table like the one below.

---

## Recorded run — instantiated against `~/dev/client-a/production`, 2026-07-20

Briefs used (targeting `client-a/deployctl.py` and `docs/GATES.md` in that repo):
A = one-line module docstring; B = `--dry-run` flag; C = (a) `--json` status flag +
(b) standalone `GATES.md`→markdown-table script; D = retry logic, fixed vs exponential.

| Case | EXECUTION SHAPE | Consult? | 6 sections | Verdict |
|------|-----------------|----------|------------|---------|
| A | OPUS-DIRECT | no | 6/6 | PASS — also refused the destructive literal reading, recommended a no-op |
| B | OPUS-DIRECT | no | 6/6 | PASS — implemented at the mutation boundary; flagged live-prod safety |
| C | SOL-ASSIST (b) + OPUS-DIRECT (a) | no | 6/6 | PASS — SOL-ASSIST fired for the independent script w/ its own contract, kept the cohesive+security-sensitive chunk with Opus |
| D | OPUS-DIRECT | no (0) | 6/6 | PASS — resolved the fork by reasoning (exp backoff, justified); consult dormant |

A fifth full run (a real `--verbose` plan → Sol critique) exercised touchpoint 1
end-to-end: the critique caught the planted defect plus 4 real bugs, and the mandated
verify-against-repo step correctly rejected 2 false positives that a lossy plan summary
had introduced (hence the "paste verbatim" rule).

**Verdict:** SOL-ASSIST discrimination proven (1 correct split, 0 false splits across 5
runs). The mid-plan consult is near-vestigial — fired 0/5, even on case D which was built
to tempt it; a safety valve, not a workhorse. All structural checks clean.
