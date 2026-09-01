#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Contract: the harness cannot re-enter itself.
#
# THE BUG THIS EXISTS FOR (incident 2026-08-12): a SessionStart hook runs
# `loop-tick --gate`, which runs `harness-verify`, which runs the cheap-delegate
# contract, which ran `provider-ask --list`, whose anthropic probe is `claude -p`
# — a NEW Claude session, which fires the SessionStart hook again. Unbounded
# recursion: 514 ticks in one day, load average 30 on 12 cores, 60% of ticks
# hitting a double 75s harness-verify timeout, ~151s added to every real session
# start, and a fabricated "observer-health" flaw enqueued on each one.
#
# The invariant, in two independent layers:
#   (1) ENV GUARD — anything the harness spawns is marked, and a marked
#       loop-tick is an immediate silent no-op. Because the marker is inherited
#       by the whole process subtree, it closes the recursion class regardless
#       of WHICH tool does the spawning (a spawner we forget is still covered).
#   (2) CONTRACT HYGIENE — a deterministic verifier never invokes a live agent.
#       Layer 2 alone is not enough (any future contract could reintroduce it);
#       layer 1 alone is not enough (a contract burning 45s on an LLM call is
#       still wrong). Both, or the class comes back.
#
# REAL BOUNDARY (Tier 1c): C1-C3 execute the real bin/loop-tick against a real
# temp git repo whose bin/ holds recording stubs — loop-tick's own tool()
# resolution and subprocess spawning are exercised, not mocked. C4 calls
# harness-verify's real run_behavioral_contract(). C5 runs the real
# cheap-delegate contract with trap shims on PATH.
#
# PROVEN RED before the fix: C1, C3, C4, C5 all fail (C2 passes both sides — it
# is the over-block guard, present so "fix" cannot mean "disable the watcher").
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/tests/lib/precondition.sh"
# Declared HERE, in the main shell: the bounded runs below happen inside
# subshells and $( ), where an exit 77 would end only the subshell and leave
# the parent asserting against output no probe produced.
harness_need_bounded_run
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

MARKER="ORCHESTRATORMAXXING_HARNESS_CHILD"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok  %s  %s\n' "$1" "$2"; }
bad() { fail=$((fail+1)); printf '  FAIL %s  %s\n' "$1" "$2" >&2; }
# A check whose DEPENDENCY is absent did not fail — it could not be measured.
# Reported per-check rather than via the contract-level exit 77 protocol,
# because C1-C7/C9 measure fine on a host with no opencode; skipping the whole
# contract would trade a false red for a much larger blind spot.
skipped=0
skip() { skipped=$((skipped+1)); printf '  SKIP %s  %s\n' "$1" "$2"; }

# bin/occ resolves opencode as OPENCODE_BIN -> PATH -> ~/.opencode/bin, then
# exits 127. C8/C8b drive occ, so on a host where none of those resolve, occ
# never reaches the shim and C8b's else-branch reported "neither decoy nor shim
# ran" as a FAILURE (lq-1f95a006, Mac-only) — an absent dependency misread as a
# broken scrub. Mirroring occ's own resolution order keeps that distinction.
opencode_resolvable() {
  [ -n "${OPENCODE_BIN:-}" ] && [ -x "${OPENCODE_BIN:-}" ] && return 0
  command -v opencode >/dev/null 2>&1 && return 0
  [ -x "$HOME/.opencode/bin/opencode" ] && return 0
  return 1
}

[ -f "$ROOT/bin/loop-tick" ]      || { echo "missing bin/loop-tick" >&2; exit 1; }
[ -f "$ROOT/bin/harness-verify" ] || { echo "missing bin/harness-verify" >&2; exit 1; }

# ── fixture: a real git repo whose bin/ holds recording stubs ────────────────
# loop-tick resolves tools as <git-root>/bin/<name>, so this redirects every
# child it spawns into recorders without touching the real harness.
mk_repo() {
  local repo="$TMP/$1"
  mkdir -p "$repo/bin" "$repo/knowledge"
  git -C "$repo" init -q 2>/dev/null
  git -C "$repo" config user.email t@t 2>/dev/null
  git -C "$repo" config user.name t 2>/dev/null

  # Records every invocation AND whether it inherited the marker.
  # MUST be Python: loop-tick invokes the verifier as `sys.executable <path>`,
  # so a /bin/sh stub dies on a SyntaxError and produces an empty log that a
  # naive C1 would misread as "the guard worked" (a false green — the exact
  # signal-vs-artifact trap this harness has paid for before).
  cat > "$repo/bin/harness-verify" <<PY
#!/usr/bin/env python3
import os, json
with open("$repo/hv.log", "a") as fh:
    fh.write("invoked marker=%s\n" % os.environ.get("$MARKER", "unset"))
print(json.dumps({"errors": 0, "warnings": 0, "inconclusive": 0,
                  "issues": [], "contract_results": []}))
PY
  cat > "$repo/bin/mem-audit" <<SH
#!/bin/sh
printf '{"files":0,"stale":0}\n'
SH
  cat > "$repo/bin/loop-queue" <<SH
#!/bin/sh
case "\$1" in
  status) printf '{"actionable": 0, "open": 0}\n' ;;
  *) : ;;
esac
exit 0
SH
  chmod +x "$repo/bin/"*
  : > "$repo/hv.log"
  printf '%s' "$repo"
}

# ── C1: a MARKED loop-tick must not run the watcher at all ───────────────────
# This is the cut that breaks the cycle. It must happen before any watcher runs.
repo="$(mk_repo c1)"
start=$(date +%s)
( cd "$repo" && env "$MARKER=1" python3 "$ROOT/bin/loop-tick" --gate --quiet >/dev/null 2>&1 )
rc=$?
elapsed=$(( $(date +%s) - start ))
if [ -s "$repo/hv.log" ]; then
  bad C1 "marked loop-tick still ran harness-verify ($(wc -l < "$repo/hv.log") invocation(s)) — recursion NOT cut"
else
  ok C1 "marked loop-tick did not run the watcher"
fi
if [ "$rc" -eq 1 ]; then
  ok C1b "marked --gate reports idle (exit 1), so no round is dispatched"
else
  bad C1b "marked --gate exit=$rc, expected 1 (idle)"
fi
if [ "$elapsed" -le 10 ]; then
  ok C1c "marked loop-tick returned in ${elapsed}s (hook is not blocked)"
else
  bad C1c "marked loop-tick took ${elapsed}s — still blocking the hook"
fi

# The asynchronous SessionStart spelling must obey the same guard. A marked
# child returning quickly is insufficient if it leaves a detached watcher.
repo="$(mk_repo c1kick)"
( cd "$repo" && env "$MARKER=1" python3 "$ROOT/bin/loop-tick" --kick --quiet >/dev/null 2>&1 )
sleep 0.3
if [ ! -s "$repo/hv.log" ] && [ ! -e "$repo/.results/watch-stamp.json" ]; then
  ok C1d "marked --kick spawned no detached watcher"
else
  bad C1d "marked --kick escaped the reentrancy guard"
fi

# ── C2: an UNMARKED loop-tick must still watch (no over-block) ───────────────
# Without this, "disable loop-tick" would pass C1 and silently kill the loop.
repo="$(mk_repo c2)"
( cd "$repo" && env -u "$MARKER" python3 "$ROOT/bin/loop-tick" --gate --quiet >/dev/null 2>&1 )
if [ -s "$repo/hv.log" ]; then
  ok C2 "unmarked loop-tick still runs harness-verify (watcher preserved)"
else
  bad C2 "unmarked loop-tick did NOT run harness-verify — the watcher was disabled, not guarded"
fi

# ── C3: loop-tick must MARK everything it spawns ─────────────────────────────
# This is what actually closes the class: any agent started anywhere beneath a
# loop-tick run inherits the marker, so its hook short-circuits.
if grep -q 'marker=1' "$repo/hv.log" 2>/dev/null; then
  ok C3 "loop-tick exported the marker to its children"
else
  bad C3 "child of loop-tick saw marker=$(sed -n '1s/.*marker=//p' "$repo/hv.log" 2>/dev/null) — subtree unguarded"
fi

# ── C4: harness-verify must MARK every contract it runs ──────────────────────
# Executes the REAL run_behavioral_contract(), not a grep for the variable name.
cat > "$TMP/c4.py" <<PY
import importlib.util, json, os, sys
spec = importlib.util.spec_from_loader("hv", None)
src = open("$ROOT/bin/harness-verify").read()
mod = importlib.util.module_from_spec(spec)
mod.__dict__["__name__"] = "hv_probe"          # never trigger its main()
try:
    exec(compile(src, "$ROOT/bin/harness-verify", "exec"), mod.__dict__)
except SystemExit:
    pass
r = mod.run_behavioral_contract(
    "reentrancy-probe",
    ['sh', '-c', 'printf "%s" "\${$MARKER:-unset}" > "$TMP/c4.out"'],
    timeout_seconds=20, retry_observer=False)
PY
# The marker MUST be unset for this probe: if it leaks in from the ambient
# environment (e.g. this contract is itself running under a marked parent) the
# check passes without harness-verify having done anything — a false green that
# already fooled one run of this contract.
if env -u "$MARKER" python3 "$TMP/c4.py" >/dev/null 2>&1 && [ -f "$TMP/c4.out" ]; then
  seen="$(cat "$TMP/c4.out")"
  if [ "$seen" = "1" ]; then
    ok C4 "harness-verify marked the contract it ran"
  else
    bad C4 "contract run by harness-verify saw marker='$seen' — contracts can still spawn agents"
  fi
else
  bad C4 "could not observe run_behavioral_contract (probe did not produce output)"
fi

# ── C5: no contract may invoke a live agent CLI ──────────────────────────────
# Property-level, implementation-agnostic: whatever the contract does internally,
# starting a real agent session is forbidden. Trap shims record any attempt.
SHIM="$TMP/shim"; mkdir -p "$SHIM"
for agent in claude codex opencode; do
  cat > "$SHIM/$agent" <<SH
#!/bin/sh
printf '%s %s\n' "$agent" "\$*" >> "$TMP/agents.log"
exit 0
SH
done
chmod +x "$SHIM"/*
: > "$TMP/agents.log"
CD_CONTRACT="$ROOT/tests/cheap-delegate/run.sh"
if [ -f "$CD_CONTRACT" ]; then
  # Subshell, not a VAR=x prefix: whether an assignment preceding a FUNCTION
  # call persists after it returns is bash-version and POSIX-mode dependent
  # (measured: it does not on this bash). A subshell scopes it unambiguously.
  ( PATH="$SHIM:$PATH"; harness_timeout 120 bash "$CD_CONTRACT" ) >/dev/null 2>&1
  cd_rc=$?
  # An empty spawn log is only evidence of "no agent" if the probe actually RAN.
  # 124/125/126/127 all mean the bounded run itself did not complete, and this
  # check's pass condition is emptiness — so without this branch every failure
  # of the bound converts into a green on a guard whose whole job is catching
  # recursive agent spawns. That exact conversion happened on the Mac
  # (2026-08-31): timeout(1) was missing, the contract never ran, C5 passed.
  # Cannot measure ≠ passed, same rule as check_spawner below.
  if [ "$cd_rc" -ge 124 ] && [ "$cd_rc" -le 127 ]; then
    skip C5 "the bounded run of cheap-delegate did not complete (rc=$cd_rc) — the spawn log is empty because nothing ran, which is not evidence that nothing spawned"
  elif [ -s "$TMP/agents.log" ]; then
    bad C5 "cheap-delegate contract spawned $(wc -l < "$TMP/agents.log") live agent(s): $(head -1 "$TMP/agents.log")"
  else
    ok C5 "cheap-delegate contract spawned no live agent"
  fi
else
  bad C5 "missing $CD_CONTRACT"
fi

# ── C6/C7/C8: every delegation primitive MARKS the worker it spawns ──────────
# The practice, not just the incident. loop-tick and harness-verify mark their
# subtree, so a contract-spawned agent is covered — but `provider-ask` and `occ`
# are also called directly (by a human, by multi-council/cross-review, by
# cheap-delegate). An unmarked worker pays the full ~63s watch at its own session
# start for nothing: it is a bounded delegate, not a machine that should audit
# the harness. One rule, all three hosts: if a harness tool starts an agent, that
# agent is a harness child.
mk_recorder() {  # $1=agent name -> a PATH shim recording the marker it inherited
  cat > "$SHIM/$1" <<SH
#!/bin/sh
printf '%s marker=%s\n' "$1" "\${$MARKER:-unset}" >> "$TMP/spawned.log"
printf 'OK\n'
exit 0
SH
  chmod +x "$SHIM/$1"
}
for a in claude codex opencode; do mk_recorder "$a"; done

# The shim substitution only measures anything if the shim is what the primitive
# ACTUALLY resolves. By default it is not: `occ` documents `OPENCODE_BIN` as an
# override and consults it BEFORE PATH, so a host that exports one makes this
# check both blind and dangerous — the recorder logs nothing ("check not
# measuring") while the REAL agent CLI is launched from inside a deterministic
# verifier, which is precisely what layer (2) above forbids. Measured on Ubuntu
# 2026-08-18: with OPENCODE_BIN set, C8 printed the queued Mac-only red verbatim
# (lq-d135ee0e) AND ran `<real opencode> run --agent glm-coder probe`.
# Scrub the override CLASS (every exported *_BIN), not the one name we know
# today: a rule still covers the next primitive's override, an enumeration
# reopens the hole the day someone adds CODEX_BIN.
bin_overrides() {  # -> `-u NAME` for every exported *_BIN in this environment
  env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*_BIN\)=.*/-u \1/p'
}

spawn_under_shim() {  # $@=command — run it with ONLY the shims resolvable
  : > "$TMP/spawned.log"
  # Probe output is CAPTURED, not discarded: when the spawn never happens, the
  # probe's own words are the only attribution evidence there is (signal-vs-
  # artifact: the next firing must carry its own diagnosis, not force a rerun).
  # $(bin_overrides) is deliberately unquoted: it emits only `-u IDENT` tokens.
  ( PATH="$SHIM:$ROOT/bin:$PATH"
    harness_timeout 90 env -u "$MARKER" $(bin_overrides) "$@" ) >"$TMP/probe.out" 2>&1
}
probe_tail() { tr '\n' ' ' < "$TMP/probe.out" 2>/dev/null | tail -c 120; }

check_spawner() {  # $1=check id, $2=agent, $3...=command
  local id="$1" agent="$2"; shift 2
  local rc line
  spawn_under_shim "$@"; rc=$?
  line="$(grep "^$agent " "$TMP/spawned.log" 2>/dev/null | head -1)"
  # Measurability semantics (lq-1a68024b): an empty spawned.log is NOT a marker
  # verdict — the property is "IF the primitive spawns an agent, THEN it is
  # marked", and a probe that never reached its spawn measured nothing. The old
  # unconditional `bad` here fired once on the Mac (2026-08-29) on a transient
  # non-spawn and loop-tick auto-enqueued it as a phantom flaw — the same
  # unmeasurable-state-as-FAILURE class C8 (opencode_resolvable) and C9
  # (lq-1aab72a9) already had fixed. The split, on the probe's own exit code
  # (the C9 rule again — repo state reds, machine state skips):
  #   - rc 0/1 are the probe's only run-to-completion exits (`--check` ends in
  #     `[ "$s" = up ]`), and for the exact command lines C6/C7/C8 run, every
  #     dispatch and backend path invokes its agent CLI unconditionally
  #     (provider-ask runs set -uo pipefail, NOT -e, so no pre-spawn hiccup can
  #     clean-exit it — verified against a critic's claim 2026-08-30). A CLEAN
  #     exit with an empty log is therefore version-controlled behavior having
  #     changed: a real red, with the probe's own output attached so the firing
  #     self-attributes (e.g. a future pre-spawn guard names itself here and
  #     gets taught to this check, instead of rotting into skip-forever).
  #   - any other rc (124 outer timeout, 126/127, signal deaths) is the probe
  #     dying BEFORE its spawn: cannot measure ≠ failed → named per-check SKIP,
  #     visible and counted, never enqueued as a phantom red.
  # Deliberately NO in-run retry (both cross-family critics, 2026-08-30): a
  # retry can blow the contract's 180s budget (fast crash + 90s hang), and its
  # rc overwrites the first attempt's — a signal-killed retry would launder a
  # clean-exit red into a skip. The watch cadence IS the retry: a transient
  # skip is re-measured within one TTL, at zero verdict risk.
  if [ -z "$line" ]; then
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; then
      bad "$id" "$(basename "$1") exited rc=$rc without spawning $agent — the probe no longer crosses its backend boundary (repo-side path changed → teach this check; probe said: $(probe_tail))"
    else
      skip "$id" "$(basename "$1") died rc=$rc before spawning $agent — cannot measure the marker this pass (transient kill/timeout, not a marker verdict; probe said: $(probe_tail))"
    fi
  elif printf '%s' "$line" | grep 'marker=1' >/dev/null; then
    ok "$id" "$(basename "$1") marked the $agent worker it spawned"
  else
    bad "$id" "$(basename "$1") spawned an UNMARKED $agent ($line) — that worker runs a full watch"
  fi
}
check_spawner C6 claude   "$ROOT/bin/provider-ask" --check anthropic
check_spawner C7 codex    "$ROOT/bin/provider-ask" --check openai
if [ ! -f "$ROOT/bin/occ" ]; then :
elif ! opencode_resolvable; then
  skip C8 "opencode is not installed on this host — occ exits 127 before it can spawn anything"
else
  check_spawner C8 opencode "$ROOT/bin/occ" "probe" --timeout 15 --retries 0
fi

# ── C8b: the scrub is real — a hostile override cannot escape the shim ───────
# The negative fixture for C6/C7/C8. Without it, their green only says "this
# machine happens to export no override", and the property rots back silently.
# Proven red against the pre-scrub dispatcher: decoy executed, shim log empty.
if [ -f "$ROOT/bin/occ" ] && ! opencode_resolvable; then
  skip C8b "opencode is not installed on this host — the scrub cannot be observed"
elif [ -f "$ROOT/bin/occ" ]; then
  cat > "$TMP/decoy-opencode" <<SH
#!/bin/sh
printf 'decoy %s\n' "\$*" >> "$TMP/decoy.log"
exit 0
SH
  chmod +x "$TMP/decoy-opencode"
  : > "$TMP/decoy.log"
  OPENCODE_BIN="$TMP/decoy-opencode" \
    spawn_under_shim "$ROOT/bin/occ" "probe" --timeout 15 --retries 0
  if [ -s "$TMP/decoy.log" ]; then
    bad C8b "an exported OPENCODE_BIN escaped the scrub — the verifier ran a binary of the environment's choosing ($(head -1 "$TMP/decoy.log"))"
  elif grep -q '^opencode ' "$TMP/spawned.log" 2>/dev/null; then
    ok C8b "an exported OPENCODE_BIN is scrubbed — the shim, not the override, resolved"
  else
    bad C8b "under OPENCODE_BIN neither decoy nor shim ran — C8 would be blind on such a host"
  fi
fi

# ── C9: ALL THREE HOSTS' session watchers are no-ops under the marker ────────
# Claude (~/.claude/settings.json), Codex (the plugin hooks in this repo) and
# OpenCode (its config + plugins) each get their REAL watcher command line
# extracted and EXECUTED under the marker. Not a grep for the variable name:
# the command runs, and a recording harness-verify shim proves it stayed asleep.
#
# Measurability semantics (lq-1aab72a9): the old tally red-flagged "<2 watchers
# found" with a message that itself said "the check is not measuring" — an
# unmeasurable state reported as a FAILURE, which loop-tick auto-enqueues as a
# phantom flaw on any machine without a registered Claude watcher. The split:
#   - ~/.claude/settings.json is MACHINE state. Absent / unparseable /
#     no-watcher-registered → named per-check SKIP (cannot measure ≠ failed).
#   - plugins/orchestratormaxxing/hooks/hooks.json is REPO state, version-controlled
#     here, and must carry the loop-tick watcher. No codex row extracted → real
#     red. This doubles as the extractor's canary: a broken extraction finds no
#     codex row either, so "extract nothing" can never read as all-skips-green.
#   - Any watcher that IS found and runs under the marker stays a real red.
HVSHIM="$TMP/hvshim"; mkdir -p "$HVSHIM"
cat > "$HVSHIM/harness-verify" <<PY
#!/usr/bin/env python3
import json
open("$TMP/hv-hook.log", "a").write("invoked\n")
print(json.dumps({"errors":0,"warnings":0,"inconclusive":0,"issues":[],"contract_results":[]}))
PY
chmod +x "$HVSHIM/harness-verify"

python3 - "$ROOT" "$TMP/hostcmds.txt" "$TMP/hoststatus.txt" <<'PY'
import json, os, sys, glob
root, out, status_out = sys.argv[1], sys.argv[2], sys.argv[3]
found = []
def walk(node):
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str) and "loop-tick" in cmd:
            found.append(cmd)
        for v in node.values(): walk(v)
    elif isinstance(node, list):
        for v in node: walk(v)
sources = [
    ("claude", os.path.expanduser("~/.claude/settings.json")),
    ("codex",  os.path.join(root, "plugins/orchestratormaxxing/hooks/hooks.json")),
    ("opencode", os.path.expanduser("~/.config/opencode/opencode.json")),
]
labelled = []
status = {}
for host, path in sources:
    if not os.path.isfile(path):
        status[host] = "absent"; continue
    try: data = json.load(open(path))
    except ValueError:
        status[host] = "unparseable"; continue
    before = len(found); walk(data)
    new = found[before:]
    for c in new: labelled.append((host, c))
    status[host] = "found" if new else "no-watcher"
with open(status_out, "w") as fh:
    for host, st in status.items():
        fh.write("%s=%s\n" % (host, st))
# OpenCode plugins are JS, not JSON — scan them textually for a watcher call.
for p in glob.glob(os.path.join(root, "opencode/plugins/*.js")) + \
         glob.glob(os.path.expanduser("~/.config/opencode/plugins/*.js")):
    txt = open(p, encoding="utf-8", errors="replace").read()
    if "loop-tick" in txt: labelled.append(("opencode", "JS-PLUGIN:" + p))
with open(out, "w") as fh:
    for host, cmd in labelled:
        fh.write(json.dumps({"host": host, "cmd": cmd}) + "\n")
PY

hostn=0; hostbad=0; hosts_seen=""
while IFS= read -r row; do
  host="$(printf '%s' "$row" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["host"])')"
  cmd="$(printf '%s' "$row"  | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["cmd"])')"
  case "$cmd" in JS-PLUGIN:*) hostbad=$((hostbad+1));
      bad C9 "$host runs a watcher from a JS plugin ($cmd) — not covered by the tool-level guard"; continue;; esac
  hostn=$((hostn+1)); hosts_seen="$hosts_seen $host"
  : > "$TMP/hv-hook.log"
  start=$(date +%s)
  PATH="$HVSHIM:$ROOT/bin:$PATH" env "$MARKER=1" timeout 60 sh -c "$cmd" >/dev/null 2>&1
  el=$(( $(date +%s) - start ))
  if [ -s "$TMP/hv-hook.log" ]; then
    hostbad=$((hostbad+1)); bad C9 "$host session watcher still ran harness-verify under the marker"
  elif [ "$el" -gt 10 ]; then
    hostbad=$((hostbad+1)); bad C9 "$host session watcher took ${el}s under the marker (still blocking)"
  fi
done < "$TMP/hostcmds.txt"

claude_state="$(sed -n 's/^claude=//p' "$TMP/hoststatus.txt" 2>/dev/null)"
codex_state="$(sed -n 's/^codex=//p' "$TMP/hoststatus.txt" 2>/dev/null)"

# Repo-state canary: the shipped Codex hooks MUST yield a watcher row. This is
# what keeps the skip path honest — a broken extractor reds here, never skips.
c9meta_bad=0
grep -qF '"host": "codex"' "$TMP/hostcmds.txt" 2>/dev/null || {
  c9meta_bad=1
  bad C9 "no codex watcher extracted from the in-repo plugin hooks (state=${codex_state:-unknown}) — the shipped hooks.json must carry a loop-tick watcher; if it does, the extraction is broken"
}

# Machine-state Claude config: a closed enumeration of not-measurable states
# skips by name; anything OUTSIDE it is a broken status probe and stays red.
case "$claude_state" in
  found) : ;;
  absent)      skip C9 "no ~/.claude/settings.json on this host — the Claude watcher cannot be measured here" ;;
  unparseable) skip C9 "~/.claude/settings.json is not valid JSON — the Claude watcher cannot be measured here (host config issue, not a harness regression)" ;;
  no-watcher)  skip C9 "~/.claude/settings.json has no loop-tick watcher registered (install.sh not run on this host?) — nothing to measure" ;;
  *) c9meta_bad=1
     bad C9 "claude watcher state '${claude_state:-<empty>}' is outside the known set — the status probe itself is broken" ;;
esac

if [ "$hostbad" -eq 0 ] && [ "$c9meta_bad" -eq 0 ]; then
  ok C9 "all $hostn measurable host watcher(s) ($hosts_seen ) are silent no-ops under the marker"
fi

printf '\nharness-reentrancy: %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skipped"
[ "$fail" -eq 0 ]
