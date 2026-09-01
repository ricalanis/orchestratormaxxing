#!/usr/bin/env bash
# Contract for the cheap-delegate layer: the living playbook keeps its
# evidence discipline, both host skills keep their doctrine, and the lane
# primitives resolve. Deterministic — grep/awk only, no LLM, no network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$ROOT/bin:$PATH"
fail() { printf 'cheap-delegate contract: %s\n' "$*" >&2; exit 1; }
PB="$ROOT/knowledge/delegation-playbook.md"
CMD="$ROOT/.claude/commands/cheap-delegate.md"
SKILL="$ROOT/plugins/claudemaxxing/skills/cheap-delegate/SKILL.md"
HOST_DOCTRINE="$ROOT/CLAUDE.md"
INSTALL="$ROOT/install.sh"
NEG_DIR="$ROOT/tests/cheap-delegate/fixtures"

[ -f "$PB" ] || fail "playbook missing: $PB"
[ -f "$CMD" ] || fail "Claude command missing: $CMD"
[ -f "$SKILL" ] || fail "Codex skill missing: $SKILL"

heur="$(awk '/^## Task-class heuristics/{f=1;next} /^## /{f=0} f' "$PB")"
evlog="$(awk '/^## Evidence log/{f=1;next} /^## /{f=0} f' "$PB")"
lanes="$(awk '/^## The lanes/{f=1;next} /^## Task-class heuristics/{f=0} f' "$PB")"

# V1: every active rule carries an [E#] tag, and every tag resolves to a log
# entry. Bullets wrap across lines, so join each bullet into one logical line
# first (checking only first lines false-flagged every wrapped rule).
heur_joined="$(printf '%s\n' "$heur" | awk '
  /^- /{if(buf!="")print buf; buf=$0; next}
  {if(buf!="") buf=buf" "$0}
  END{if(buf!="")print buf}')"
printf '%s\n' "$heur_joined" | grep '^- \*\*' | grep -v '\[E' >/dev/null && fail "V1: active rule without [E#] evidence tag"
for tag in $(printf '%s\n' "$heur" | grep -oE '\[E[0-9]+\]' | sort -u); do
  grep -qF <<<"$evlog" "$tag" || fail "V1: $tag referenced by a rule but absent from the evidence log"
done

# V2: every evidence entry is dated.
printf '%s\n' "$evlog" | grep '^- \*\*\[E' | grep -vE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' >/dev/null \
  && fail "V2: undated evidence entry"

# V3: a superseded STATUS MARKER may only appear in the evidence log — a rule
# still sitting in an active section while marked superseded is a contradiction.
# Prose that merely names the supersede mechanism (the maintenance protocol,
# pointers like "superseded note in the log") must not trip this.
awk '/^## Evidence log/{exit} {print}' "$PB" | grep -iE 'superseded by|\[superseded\]' >/dev/null \
  && fail "V3: superseded status marker outside the evidence log (contradictory active rule?)"

# V4: both host surfaces carry the load-bearing doctrine tokens.
for f in "$CMD" "$SKILL"; do
  for tok in contract repair escalat delegation-playbook; do
    grep -qi "$tok" "$f" || fail "V4: $(basename "$f") missing doctrine token '$tok'"
  done
  # Hosts phrase the never-delegate rule natively: Claude says "hard keeps",
  # Codex says "keep the task in root". Accept either; require sign-off too.
  grep -qiE 'hard[- ]keep|keep (the task )?in (root|session)' "$f" \
    || fail "V4: $(basename "$f") missing a never-delegate (hard-keep) clause"
  grep -qi 'sign-off' "$f" || fail "V4: $(basename "$f") missing final sign-off keep"
done

# V5: public lane primitives resolve; occ remains an internal detail of o.
for bin in oll o provider-ask; do
  command -v "$bin" >/dev/null 2>&1 || fail "V5: lane primitive '$bin' not on PATH"
done
ROUTING="$ROOT/knowledge/provider-routing.md"
[ -f "$ROUTING" ] || fail "V5: routing table missing: $ROUTING"
for p in ollama openai anthropic xai; do
  grep -qF "| \`$p\` |" "$ROUTING" || fail "V5: routing table lacks '$p' row"
done

# V6: routing fixtures — task classes covered by heuristics, lanes named in the table.
FIX="$ROOT/tests/cheap-delegate/fixtures.tsv"
[ -f "$FIX" ] || fail "V6: fixtures.tsv missing"
n=0
while IFS=$'\t' read -r tclass lane; do
  case "$tclass" in ''|'#'*) continue ;; esac
  n=$((n+1))
  rule="$(printf '%s\n' "$heur_joined" | grep -iF "$tclass" | head -1)"
  [ -n "$rule" ] || fail "V6: task class '$tclass' not covered by playbook heuristics"
  printf '%s\n' "$rule" | grep -iF -- "$lane" >/dev/null \
    || fail "V6: task class '$tclass' is not paired with '$lane' in its active rule"
done < "$FIX"
[ "$n" -ge 6 ] || fail "V6: fewer than 6 fixture rows ($n)"

# V7: both hosts expose the stateful o boundary, keep repairs in one session,
# and treat pane output as observation rather than acceptance.
for f in "$CMD" "$SKILL"; do
  grep -qF 'o delegate' "$f" || fail "V7: $(basename "$f") missing o delegate"
  grep -qF 'o send' "$f" || fail "V7: $(basename "$f") missing same-session repair"
  grep -qF 'o output' "$f" || fail "V7: $(basename "$f") missing bounded output"
  grep -qF 'o close' "$f" || fail "V7: $(basename "$f") missing worker close (leaked session)"
  grep -qi 'transport' "$f" || fail "V7: $(basename "$f") missing transport boundary"
  grep -qiE 'observation|observability' "$f" || fail "V7: $(basename "$f") treats pane pixels as acceptance"
  grep -qiE 'contract|acceptance' "$f" || fail "V7: $(basename "$f") drops caller acceptance"
done

# V8: contract-at-birth is physical and byte-identical across the shared brain
# and both host surfaces. A worker can consume this contract but never create
# or certify it. The two fixtures preserve the pre-fix prose-only false green.
extract_gate() {
  awk '/^<!-- durable-delegation-gate:begin -->$/{seen++; on=1; next}
       /^<!-- durable-delegation-gate:end -->$/{on=0; next}
       on{print}
       END{if(seen!=1) exit 1}' "$1"
}
gate_valid() {
  local f="$1" body
  body="$(extract_gate "$f")" || return 1
  [ -n "$body" ] || return 1
  for tok in '.results/delegation/<run-id>/contract.md' 'brief.md' 'output.tmp' \
             'output.md' 'receipt.json' 'SHA-256' 'before dispatch' \
             'delegate-ledger receipt' \
             'worker never authors, alters, grades, or certifies' 'marks both read-only' \
             'o delegate' 'o send' 'o handoff' 'o output' 'o close'; do
    printf '%s\n' "$body" | grep -iF "$tok" >/dev/null || return 1
  done
  printf '%s\n' "$body" | grep -F '`oll` is allowed only for response-only work' >/dev/null || return 1
  printf '%s\n' "$body" | grep -F 'require the public `o` worker runtime' >/dev/null || return 1
  printf '%s\n' "$body" | grep -F '`occ` remains an internal one-shot' >/dev/null || return 1
}

reference="$(extract_gate "$PB")" || fail "V8: playbook missing exactly one durable gate"
for f in "$PB" "$CMD" "$SKILL"; do
  gate_valid "$f" || fail "V8: $(basename "$f") lacks the physical dispatch gate"
  [ "$(extract_gate "$f")" = "$reference" ] \
    || fail "V8: $(basename "$f") durable gate drifted from playbook"
done
for f in "$NEG_DIR/claude-no-durable.md" "$NEG_DIR/codex-no-durable.md"; do
  [ -f "$f" ] || fail "V8: negative fixture missing: $f"
  if gate_valid "$f"; then
    fail "V8: negative fixture incorrectly satisfies durable gate: $(basename "$f")"
  fi
done

# V9: the project instructions shared by Claude and Codex carry the same lane
# boundary even when cheap-delegate was not the entrypoint.
for tok in 'Contract-at-birth is physical' '.results/delegation/<run-id>/contract.md' \
           'Direct `oll` is response-only' 'Route those tasks through the public OpenCode `o` worker runtime' \
           'worker never authors or certifies' 'o close'; do
  grep -qiF "$tok" "$HOST_DOCTRINE" || fail "V9: shared host doctrine missing '$tok'"
done

# V10: fresh global installs teach the boundary to Claude, Codex, and OpenCode.
for tok in 'Physical delegation gate' '.results/delegation/<run-id>/contract.md' \
           'Direct \`oll\` is response-only' 'route through the public \`o\` worker runtime'; do
  grep -qF "$tok" "$INSTALL" || fail "V10: install doctrine missing '$tok'"
done

# V11: the Codex rung is three named tiers on every surface. A bare
# `provider-ask openai` silently rides the provider default, so the shared brain
# and both host surfaces must name the exact slugs and their cheap->strong order.
for f in "$PB" "$CMD" "$SKILL"; do
  for slug in gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol; do
    grep -qF "$slug" "$f" || fail "V11: $(basename "$f") missing Codex tier '$slug'"
  done
  # Order is doctrine: cheapest tier appears before the strongest one. Measured as
  # a BYTE offset, not a line number — a surface is free to compress the three
  # tiers onto one row, and cheap-before-strong is the requirement while
  # line-separated slugs were only ever an accident of the old table's formatting.
  luna_at="$(grep -boF 'gpt-5.6-luna' "$f" | head -1 | cut -d: -f1)"
  sol_at="$(grep -boF 'gpt-5.6-sol' "$f" | head -1 | cut -d: -f1)"
  [ "$luna_at" -lt "$sol_at" ] \
    || fail "V11: $(basename "$f") lists Sol before Luna (escalation order inverted)"
done
# The Claude surface reaches the tiers through provider-ask; Codex calls codex exec.
grep -qF 'MODEL=gpt-5.6-luna provider-ask openai' "$CMD" \
  || fail "V11: Claude command lacks the MODEL= tier selector for provider-ask openai"
grep -qF -e '-m gpt-5.6-luna' "$SKILL" \
  || fail "V11: Codex skill lacks the native -m tier selector"

# V12: the escalation sigil is exactly `F.`, defined once in the playbook and
# mirrored on both host surfaces with its three load-bearing semantics (strip the
# token, skip the cheap lanes, mark the row as an override). A surface that lost
# the sigil must fail — that is what the negative fixture proves.
grep -q '^## Escalation sigil' "$PB" \
  || fail "V12: playbook lacks the single-source-of-truth '## Escalation sigil' section"
sigil_sec="$(awk '/^## Escalation sigil/{f=1;next} /^## /{f=0} f' "$PB")"
for token in 'F.' 'direct-to-strong' 'override'; do
  printf '%s' "$sigil_sec" | grep -F "$token" >/dev/null \
    || fail "V12: playbook sigil section missing '$token'"
done
# OpenCode is deliberately excluded; the playbook must say so rather than leave a gap.
printf '%s' "$sigil_sec" | grep -i 'opencode' >/dev/null \
  || fail "V12: playbook sigil section must state why OpenCode has no sigil"
sigil_check() {  # a surface honours the sigil only with all three semantics
  grep -qF 'F.' "$1" && grep -qiF 'direct-to-strong' "$1" && grep -qF -- '--override' "$1"
}
for f in "$CMD" "$SKILL"; do
  sigil_check "$f" || fail "V12: $(basename "$f") does not carry the F. sigil semantics"
done
for f in "$NEG_DIR/claude-no-sigil.md"; do
  [ -f "$f" ] || fail "V12: negative fixture missing: $f"
  if sigil_check "$f"; then
    fail "V12: negative fixture incorrectly satisfies the sigil check: $(basename "$f")"
  fi
done

# V13: the ledger is wired on both surfaces — read the aggregate before dispatch,
# write one row per lane attempt after it — and the playbook states the authority
# order, so an advisory stat can never be quoted as a ranking.
ledger_check() { grep -qF 'delegate-ledger stats' "$1" && grep -qF 'delegate-ledger record' "$1"; }
for f in "$CMD" "$SKILL"; do
  ledger_check "$f" || fail "V13: $(basename "$f") must both read stats and record rows"
  grep -qF 'authority' "$f" \
    || fail "V13: $(basename "$f") must read the ledger's authority field"
done
grep -q '^## Delegation ledger' "$PB" \
  || fail "V13: playbook lacks the '## Delegation ledger' authority-order section"
led_sec="$(awk '/^## Delegation ledger/{f=1;next} /^## /{f=0} f' "$PB")"
for token in 'sufficient' 'advisory' 'preferred_lane'; do
  printf '%s' "$led_sec" | grep -F "$token" >/dev/null \
    || fail "V13: playbook ledger section missing '$token'"
done
[ -x "$ROOT/bin/delegate-ledger" ] || fail "V13: bin/delegate-ledger missing or not executable"
for f in "$NEG_DIR/codex-no-ledger.md"; do
  [ -f "$f" ] || fail "V13: negative fixture missing: $f"
  if ledger_check "$f"; then
    fail "V13: negative fixture incorrectly satisfies the ledger check: $(basename "$f")"
  fi
done

# V14: Kimi K3 keeps distinct response-only, stateful, and planning boundaries;
# included-first must not drift into "free/unmetered" doctrine.
for f in "$PB" "$CMD" "$SKILL"; do
  grep -qF 'oll --model kimi-k3' "$f" \
    || fail "V14: $(basename "$f") missing response-only K3 lane"
  grep -qF -- '--profile long-horizon' "$f" \
    || fail "V14: $(basename "$f") missing stateful K3 coder lane"
  grep -qF -- '--profile planning' "$f" \
    || fail "V14: $(basename "$f") missing read-only K3 planner lane"
  grep -qi 'included' "$f" \
    || fail "V14: $(basename "$f") missing included-first policy"
  grep -qi 'higher-consumption' "$f" \
    || fail "V14: $(basename "$f") missing K3 consumption guardrail"
done

# V15: model attribution — a run must say WHICH MODEL ran WHICH TASK, at dispatch
# and in the report. A lane is not a model and an agent is not a model: measured
# 2026-08-30 the ledger held 4 of 31 rows with no model and 3 more with the agent
# `kimi-coder` in the model field, so a fifth of the corpus could not answer "what
# ran". The slug is resolved from the run (`oll --route-profile`, `o delegate
# --json`, the MODEL=/-m selector), never recalled from a table that can drift.
grep -q '^## Model attribution' "$PB" \
  || fail "V15: playbook lacks the '## Model attribution' single-source-of-truth section"
attr_sec="$(awk '/^## Model attribution/{f=1;next} /^## /{f=0} f' "$PB")"
for token in 'oll --route-profile' 'o delegate' 'MODEL=' 'alias' 'per lane attempt' \
             'stderr banner'; do
  printf '%s' "$attr_sec" | grep -iF "$token" >/dev/null \
    || fail "V15: playbook attribution section missing '$token'"
done
# A surface honours attribution only with BOTH halves: the pre-dispatch
# announcement of an exact slug, and a per-attempt line in the report. Match on a
# FLATTENED surface (emphasis/backticks dropped, newlines collapsed): the
# requirement is that the doc says it, so a line wrap or a bolded word must not
# be able to satisfy or defeat the check — a single-line grep made "the **exact
# model\n   slug**" invisible while the rule was plainly there.
flatten_surface() { tr -d '*`' < "$1" | tr '\n' ' ' | tr -s ' '; }
attribution_check() {
  local flat
  flat="$(flatten_surface "$1")"
  printf '%s' "$flat" | grep -i 'model attribution' >/dev/null \
    && printf '%s' "$flat" | grep -iF 'exact model slug' >/dev/null \
    && printf '%s' "$flat" | grep -F 'oll --route-profile' >/dev/null \
    && printf '%s' "$flat" | grep -iF 'per lane attempt' >/dev/null \
    && printf '%s' "$flat" | grep -F '<model slug>' >/dev/null
}
for f in "$CMD" "$SKILL"; do
  attribution_check "$f" \
    || fail "V15: $(basename "$f") does not name the model per task (announce + report)"
done
for f in "$NEG_DIR/claude-no-attribution.md" "$NEG_DIR/codex-no-attribution.md"; do
  [ -f "$f" ] || fail "V15: negative fixture missing: $f"
  if attribution_check "$f"; then
    fail "V15: negative fixture incorrectly satisfies the attribution check: $(basename "$f")"
  fi
done

# V16: OpenCode turn 1 is a physical immediate task, never a bootstrap. Both
# host surfaces and their shared playbook must carry the marker, legacy bridge,
# and repair-only meaning; the old delayed-assignment prose is the negative.
turn1_check() {
  local f="$1" flat
  flat="$(tr -d '*`' < "$f" | tr '\n' ' ' | tr -s ' ')"
  printf '%s' "$flat" | grep -F '<!-- o-delegate-turn-1:begin -->' >/dev/null \
    && printf '%s' "$flat" | grep -F '<!-- o-delegate-turn-1:end -->' >/dev/null \
    && printf '%s' "$flat" | grep -iF 'executes that assignment immediately in turn 1' >/dev/null \
    && printf '%s' "$flat" | grep -iF 'unmarked legacy bounded brief executes as a whole' >/dev/null \
    && printf '%s' "$flat" | grep -iF 'o send is repair-only, never the initial task' >/dev/null
}
for f in "$PB" "$CMD" "$SKILL"; do
  turn1_check "$f" || fail "V16: $(basename "$f") permits delayed initial assignment"
done
for tok in '<!-- o-delegate-turn-1:begin -->' '<!-- o-delegate-turn-1:end -->' \
           'repair-only and never the initial task'; do
  grep -qF "$tok" "$HOST_DOCTRINE" \
    || fail "V16: shared host doctrine missing '$tok'"
done
for tok in 'o-delegate-turn-1:begin' 'o-delegate-turn-1:end' \
           'repair-only and never the initial task'; do
  grep -qF "$tok" "$INSTALL" || fail "V16: install doctrine missing '$tok'"
done
if turn1_check "$NEG_DIR/deferred-turn1.md"; then
  fail 'V16: deferred initial-assignment fixture incorrectly passed'
fi

printf 'cheap-delegate contract: PASS (V1-V16)\n'
