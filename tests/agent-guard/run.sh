#!/usr/bin/env bash
# Contract for bin/agent-guard — the cross-host pre (dangerous-command) and post
# (write-content security guidance) hook runner + its host adapters.
#
# Authored by Root BEFORE the tool existed (Tier-0). Two upstreams are vendored
# verbatim under deploy/agent-guard/ and this contract is what makes them
# load-bearing here:
#   * davidondrej/skills@76724e4  hooks/dangerous-patterns.txt + the 151-case
#     block/allow corpus (MIT) → C1/C3/C12
#   * anthropics/claude-plugins-official@222b19d  security-guidance patterns.py
#     (Apache-2.0) → C5
# C3 is the proof-of-red: a patterns file with one rule removed must let the
# matching command through, so a guard that "passes" while matching nothing
# cannot pass this file.
#
# Deterministic, offline, hermetic HOME. Nothing here spawns an agent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/agent-guard"
GUARD_SRC="$ROOT/deploy/agent-guard"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0

ok()   { PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$PASS" "$1"; }
fail() { printf 'not ok %d - %s\n' "$((PASS + 1))" "$1" >&2; exit 1; }

[[ -x "$TOOL" ]] || fail 'bin/agent-guard missing or not executable'
[[ -f "$GUARD_SRC/dangerous-patterns.txt" ]] || fail 'vendored dangerous-patterns.txt missing'
[[ -f "$GUARD_SRC/guard-cases.txt" ]] || fail 'vendored guard-cases.txt missing'
[[ -f "$GUARD_SRC/security_patterns.py" ]] || fail 'vendored security_patterns.py missing'

export HOME="$TMP/home"; mkdir -p "$HOME"
export ORCHESTRATORMAXXING_GUARD_DIR="$GUARD_SRC"
unset SECURITY_GUIDANCE_DISABLE ORCHESTRATORMAXXING_AGENT_GUARD WARP_MODEL_PIN_LAYOUT || true

pre_rc() {  # $1 = command string (may contain real newlines); prints exit code of `pre`
  local rc=0
  python3 -c 'import json,sys; print(json.dumps({"tool_input":{"command":sys.argv[1]},"cwd":"/tmp","hook_event_name":"PreToolUse","tool_name":"Bash"}))' "$1" \
    | "$TOOL" pre >"$TMP/pre.out" 2>"$TMP/pre.err" || rc=$?
  printf '%s' "$rc"
}

# ---------------------------------------------------------------------------
# C1: the full vendored corpus — every block case exits 2 with a stderr reason
# and empty stdout; every allow case exits 0 silently. Also the two other
# payload shapes upstream supports (.toolInput.command, .command) and --format json.
python3 - "$TOOL" "$GUARD_SRC/guard-cases.txt" "$TMP" <<'PY' || fail 'C1: corpus'
import json, subprocess, sys
tool, cases, tmp = sys.argv[1:]
bad = []; n = 0
for line in open(cases, encoding="utf-8"):
    if not line.strip() or line.startswith("#"):
        continue
    expected, cmd = line.rstrip("\n").split("\t", 1)
    cmd = cmd.replace("\\n", "\n").replace("\\\\", "\\")
    payload = json.dumps({"tool_input": {"command": cmd}, "cwd": "/tmp"})
    r = subprocess.run([tool, "pre"], input=payload, capture_output=True, text=True, timeout=20)
    n += 1
    if expected == "block":
        if r.returncode != 2 or not r.stderr.strip() or r.stdout.strip():
            bad.append(f"block rc={r.returncode} out={r.stdout[:60]!r} err={r.stderr[:60]!r}: {cmd!r}")
        elif "Matched pattern" not in r.stderr:
            bad.append(f"block reason lacks the matched pattern: {r.stderr[:80]!r}")
    else:
        if r.returncode != 0 or r.stdout.strip() or r.stderr.strip():
            bad.append(f"allow rc={r.returncode} out={r.stdout[:60]!r} err={r.stderr[:60]!r}: {cmd!r}")
assert n >= 150, f"corpus too small: {n}"
# other payload shapes
for shape in ({"toolInput": {"command": "rm -rf /"}}, {"command": "rm -rf /"}):
    r = subprocess.run([tool, "pre"], input=json.dumps(shape), capture_output=True, text=True, timeout=20)
    if r.returncode != 2:
        bad.append(f"shape {list(shape)[0]} not blocked (rc={r.returncode})")
# no command at all → allow
r = subprocess.run([tool, "pre"], input=json.dumps({"tool_input": {"file_path": "x"}}), capture_output=True, text=True, timeout=20)
if r.returncode != 0 or r.stdout.strip():
    bad.append("payload without a command must allow silently")
# --format json: exit 0 always, decision in the body
for cmd, want in (("rm -rf /", "deny"), ("rm -rf node_modules", "allow")):
    r = subprocess.run([tool, "pre", "--format", "json"], input=json.dumps({"tool_input": {"command": cmd}}),
                       capture_output=True, text=True, timeout=20)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        bad.append(f"--format json not JSON for {cmd!r}: {r.stdout[:80]!r}"); continue
    if r.returncode != 0 or d.get("decision") != want:
        bad.append(f"--format json {cmd!r}: rc={r.returncode} decision={d.get('decision')}")
    if want == "deny" and not (d.get("pattern") and d.get("reason")):
        bad.append("--format json deny must carry pattern + reason")
for b in bad[:12]:
    print("  ", b, file=sys.stderr)
sys.exit(1 if bad else 0)
PY
ok "C1: full vendored corpus (block→exit 2 + reason, allow→silent 0), three payload shapes, --format json"

# C2: the patterns compile and status reports them
"$TOOL" status --json > "$TMP/status.json" || fail 'C2: status --json failed'
python3 - "$TMP/status.json" <<'PY' || fail 'C2: status shape'
import json, sys
s = json.load(open(sys.argv[1]))
p = s["patterns"]
assert p["present"] is True and p["count"] >= 30, p
assert p["errors"] == [], p["errors"]
assert s["security_rules"]["count"] == 25, s["security_rules"]
assert "hosts" in s and isinstance(s["hosts"], dict)
PY
ok "C2: status --json — patterns present, zero compile errors, 25 security rules"

# C3: PROOF OF RED — a patterns file missing the `sudo rm` rule must ALLOW `sudo rm file.txt`,
# and the corpus run against that mutant must fail. A guard that never consults the file is caught here.
mkdir -p "$TMP/mut"
grep -v 'sudo\[\[:space:\]\]+(-\[a-zA-Z\]+\[\[:space:\]\]+)\*rm' "$GUARD_SRC/dangerous-patterns.txt" > "$TMP/mut/dangerous-patterns.txt"
[[ "$(grep -c . "$TMP/mut/dangerous-patterns.txt")" -lt "$(grep -c . "$GUARD_SRC/dangerous-patterns.txt")" ]] || fail 'C3: mutant did not remove the sudo-rm rule'
cp "$GUARD_SRC/security_patterns.py" "$GUARD_SRC/guard-cases.txt" "$TMP/mut/"
[[ "$(pre_rc 'sudo rm file.txt')" == "2" ]] || fail 'C3: real patterns must block sudo rm'
[[ "$(ORCHESTRATORMAXXING_GUARD_DIR="$TMP/mut" pre_rc 'sudo rm file.txt')" == "0" ]] || fail 'C3: mutant patterns must ALLOW sudo rm (the file is not being consulted)'
[[ "$(ORCHESTRATORMAXXING_GUARD_DIR="$TMP/mut" pre_rc 'rm -rf /')" == "2" ]] || fail 'C3: mutant still blocks the other rules'
if ORCHESTRATORMAXXING_GUARD_DIR="$TMP/mut" "$TOOL" selftest > "$TMP/mut-selftest.out" 2>&1; then
  fail 'C3: selftest against the mutant patterns must FAIL'
fi
grep -q 'sudo rm file.txt' "$TMP/mut-selftest.out" || fail 'C3: selftest must name the escaped case'
ok "C3: proof of red — a removed rule lets its command through and selftest goes red"

# C4: silence ≠ blindness — no patterns file: pre fails OPEN, but status --gate is red and says why;
# a single invalid regex line is skipped, reported, and never breaks the other rules.
mkdir -p "$TMP/empty"
[[ "$(ORCHESTRATORMAXXING_GUARD_DIR="$TMP/empty" pre_rc 'rm -rf /')" == "0" ]] || fail 'C4: missing patterns must fail open (exit 0)'
if ORCHESTRATORMAXXING_GUARD_DIR="$TMP/empty" "$TOOL" status --gate >/dev/null 2>&1; then fail 'C4: status --gate must be red without a patterns file'; fi
ORCHESTRATORMAXXING_GUARD_DIR="$TMP/empty" "$TOOL" status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["patterns"]["present"] is False, s' || fail 'C4: status must report patterns absent'
mkdir -p "$TMP/badre"; { cat "$GUARD_SRC/dangerous-patterns.txt"; printf '(unclosed[\n'; } > "$TMP/badre/dangerous-patterns.txt"
cp "$GUARD_SRC/security_patterns.py" "$TMP/badre/"
[[ "$(ORCHESTRATORMAXXING_GUARD_DIR="$TMP/badre" pre_rc 'rm -rf /')" == "2" ]] || fail 'C4: one bad regex must not disable the good rules'
ORCHESTRATORMAXXING_GUARD_DIR="$TMP/badre" "$TOOL" status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); assert len(s["patterns"]["errors"])==1, s["patterns"]' || fail 'C4: status must report exactly one compile error'
if ORCHESTRATORMAXXING_GUARD_DIR="$TMP/badre" "$TOOL" status --gate >/dev/null 2>&1; then fail 'C4: a compile error must red the gate'; fi
ok "C4: fail-open at call time, red at the gate — missing file and bad regex are both visible"

# C5: post hook — Claude Write/Edit shapes, Codex apply_patch shape, doc-path filter, kill switch
post_json() { printf '%s' "$1" | "$TOOL" post; }
python3 - "$TOOL" <<'PY' || fail 'C5: post hook'
import json, os, subprocess, sys
tool = sys.argv[1]
def run(payload, env=None):
    e = dict(os.environ); e.update(env or {})
    r = subprocess.run([tool, "post"], input=json.dumps(payload), capture_output=True, text=True, timeout=20, env=e)
    assert r.returncode == 0, (r.returncode, r.stderr)
    return r.stdout
# Write with a pickle load → additionalContext names the rule
out = run({"tool_name": "Write", "tool_input": {"file_path": "/tmp/proj/app.py", "content": "import pickle\nobj = pickle.load(f)\n"}})
d = json.loads(out)
h = d["hookSpecificOutput"]; assert h["hookEventName"] == "PostToolUse"
assert "pickle" in h["additionalContext"].lower() and "Security guidance" in h["additionalContext"], h
# Edit with new_string
out = run({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/proj/x.py", "old_string": "a", "new_string": "subprocess.run(cmd, shell=True)"}})
assert "shell=True" in json.loads(out)["hookSpecificOutput"]["additionalContext"] or "subprocess" in json.loads(out)["hookSpecificOutput"]["additionalContext"].lower()
# MultiEdit edits[]
out = run({"tool_name": "MultiEdit", "tool_input": {"file_path": "/tmp/proj/y.js", "edits": [{"old_string": "a", "new_string": "el.innerHTML = userInput"}]}})
assert "innerHTML" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
# safe content → no output at all
assert run({"tool_name": "Write", "tool_input": {"file_path": "/tmp/proj/ok.py", "content": "print('hi')\n"}}).strip() == ""
# eval rule skips prose files (upstream path filter)
assert run({"tool_name": "Write", "tool_input": {"file_path": "/tmp/proj/notes.md", "content": "we call eval( here\n"}}).strip() == ""
# Codex apply_patch: the patch text lives in tool_input.command
patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: src/loader.py\n+import pickle\n+data = pickle.load(fh)\n*** End Patch\nEOF"
out = run({"tool_name": "apply_patch", "tool_input": {"command": patch}})
assert "pickle" in json.loads(out)["hookSpecificOutput"]["additionalContext"].lower(), out
# apply_patch: a docs-only patch with eval( is filtered by path
patch2 = "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: README.md\n@@\n+use eval( carefully\n*** End Patch\nEOF"
assert run({"tool_name": "apply_patch", "tool_input": {"command": patch2}}).strip() == ""
# kill switch
assert run({"tool_name": "Write", "tool_input": {"file_path": "/tmp/proj/app.py", "content": "pickle.load(f)"}}, {"SECURITY_GUIDANCE_DISABLE": "1"}).strip() == ""
# unrelated tool → silent
assert run({"tool_name": "Bash", "tool_input": {"command": "echo pickle.load"}}).strip() == ""
# --format json
r = subprocess.run([tool, "post", "--format", "json"], input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/tmp/a.py", "content": "yaml.load(x)"}}), capture_output=True, text=True, timeout=20)
j = json.loads(r.stdout); assert r.returncode == 0 and j["findings"] and j["context"], j
PY
ok "C5: post — Write/Edit/MultiEdit, Codex apply_patch, doc-path filter, kill switch, --format json"

# C6: OpenCode plugin — real node execution against a fake shell
PLUGIN="$ROOT/opencode/plugins/orchestratormaxxing-guard.js"
[[ -f "$PLUGIN" ]] || fail 'C6: opencode/plugins/orchestratormaxxing-guard.js missing'
JSRUN="$(command -v node 2>/dev/null || command -v bun 2>/dev/null || true)"
for c in /opt/homebrew/bin/node /usr/local/bin/node "$HOME/.local/bin/node" /usr/bin/node; do [[ -z "$JSRUN" && -x "$c" ]] && JSRUN="$c"; done
[[ -n "$JSRUN" ]] || fail 'C6: node/bun unavailable'
"$JSRUN" - "$PLUGIN" <<'JS' || fail 'C6: opencode plugin behaviour'
const pluginPath = process.argv[2]
const mod = await import(pluginPath)
const factory = mod.OrchestratormaxxingGuard
if (typeof factory !== "function") throw new Error("plugin must export OrchestratormaxxingGuard")
function mkShell(mode) {
  const calls = []
  const quieted = []
  const shell = (strings, ...values) => {
    const cmd = strings.reduce((o, p, i) => o + p + (i < values.length ? String(values[i]) : ""), "")
    calls.push(cmd)
    if (mode === "throw") return Promise.reject(new Error("spawn failed"))
    let body = '{"decision":"allow","pattern":null,"reason":""}'
    if (cmd.includes(" pre ") && cmd.includes("rm -rf /")) body = '{"decision":"deny","pattern":"--no-preserve-root","reason":"blocked: rm -rf /"}'
    if (cmd.includes(" post ")) body = cmd.includes("pickle") ? '{"findings":[{"rule":"pickle_deserialization","reminder":"x"}],"context":"⚠️ Security guidance — 1 pattern matched (pickle_deserialization)"}' : '{"findings":[],"context":""}'
    const result = Promise.resolve({ exitCode: 0, text: async () => body, stdout: Buffer.from(body) })
    result.quiet = () => { quieted.push(cmd); return result }
    return result
  }
  return { shell, calls, quieted }
}
async function before(mode, tool, args) {
  const { shell, calls, quieted } = mkShell(mode)
  const p = await factory({ directory: "/tmp/project", $: shell, client: {} })
  let threw = null
  try { await p["tool.execute.before"]({ tool, sessionID: "s", callID: "c" }, { args }) } catch (e) { threw = e }
  return { threw, calls, quieted }
}
async function after(mode, tool, args) {
  const { shell, calls } = mkShell(mode)
  const p = await factory({ directory: "/tmp/project", $: shell, client: {} })
  const output = { title: "t", output: "wrote file", metadata: {} }
  await p["tool.execute.after"]({ tool, sessionID: "s", callID: "c", args }, output)
  return { output, calls }
}
let r = await before("ok", "bash", { command: "rm -rf /" })
if (!r.threw || !String(r.threw.message).includes("blocked")) throw new Error("dangerous bash must throw with the reason")
if (!r.calls.some(c => c.includes("agent-guard") && c.includes("pre") && c.includes("--format json"))) throw new Error("before must call agent-guard pre --format json")
if (r.quieted.length !== r.calls.length) throw new Error("every guard call must be .quiet() so stderr never reaches the TUI: " + r.calls.length + " calls, " + r.quieted.length + " quieted")
if (!r.calls.some(c => c.includes(" < "))) throw new Error("payload must reach the guard through a stdin redirect: " + r.calls[0].slice(0, 80))
if (r.calls.some(c => /^\s*printf/.test(c) || c.includes("printf '%s'"))) throw new Error("payload must not travel as a printf argv (E2BIG drops it at 128 KiB): " + r.calls[0].slice(0, 80))
r = await before("ok", "bash", { command: "ls -la" })
if (r.threw) throw new Error("safe bash must not throw: " + r.threw)
r = await before("ok", "read", { filePath: "/etc/hosts" })
if (r.threw || r.calls.length) throw new Error("non-bash tools must not shell out")
r = await before("throw", "bash", { command: "rm -rf /" })
if (r.threw) throw new Error("a failing guard must fail OPEN (allow)")
r = await before("ok", "bash", { timeout: 5 })
if (r.threw || r.calls.length) throw new Error("bash without a command string must not shell out")
let a = await after("ok", "write", { filePath: "/tmp/p/app.py", content: "pickle.load(f)" })
if (!a.output.output.includes("Security guidance")) throw new Error("write findings must be appended to output: " + a.output.output)
if (!a.calls.some(c => c.includes(" post ") && c.includes("--format json") && c.includes("file_path"))) throw new Error("after must call agent-guard post with the Claude-shaped payload")
a = await after("ok", "edit", { filePath: "/tmp/p/app.py", oldString: "a", newString: "pickle.load(f)" })
if (!a.calls.some(c => c.includes("new_string"))) throw new Error("edit must map newString → new_string")
a = await after("ok", "write", { filePath: "/tmp/p/ok.py", content: "print(1)" })
if (a.output.output !== "wrote file") throw new Error("no findings → output untouched")
a = await after("ok", "read", { filePath: "/tmp/p/ok.py" })
if (a.calls.length) throw new Error("after must ignore non-write tools")
a = await after("throw", "write", { filePath: "/tmp/p/app.py", content: "pickle.load(f)" })
if (a.output.output !== "wrote file") throw new Error("a failing guard must leave output untouched")
JS
ok "C6: OpenCode plugin — bash deny throws, safe/other tools pass, fail-open, write/edit guidance appended"
BUN="$(command -v bun 2>/dev/null || true)"; for c in /opt/homebrew/bin/bun /usr/local/bin/bun "$HOME/.bun/bin/bun" ; do [[ -z "$BUN" && -x "$c" ]] && BUN="$c"; done
if [[ -n "$BUN" ]]; then
  PATH="$ROOT/bin:$PATH" "$BUN" - "$PLUGIN" <<'JS' || fail 'C6b: real Bun transport — a 140 KB dangerous command must still be denied'
import { $ } from "bun"
const mod = await import(process.argv[2])
const p = await mod.OrchestratormaxxingGuard({ directory: "/tmp", $, client: {} })
const command = "echo " + "a".repeat(140000) + "; rm -rf /"
let threw = null
try { await p["tool.execute.before"]({ tool: "bash", sessionID: "s", callID: "c" }, { args: { command } }) } catch (e) { threw = e }
if (!threw || !String(threw.message).includes("blocked")) throw new Error("140 KB command bypassed the guard: " + threw)
JS
  ok "C6b: real Bun transport — 140 KB dangerous command denied (stdin, not argv)"
else
  echo "note - C6b skipped: bun not installed on this host (the node fake-shell case above still guards the transport shape)"
fi

# C7: Hermes plugin — standalone import, block/allow/fail-open contract
HPLUG="$ROOT/deploy/agent-guard/hermes/command-guard"
[[ -f "$HPLUG/plugin.yaml" && -f "$HPLUG/__init__.py" ]] || fail 'C7: hermes plugin files missing'
grep -q '^name: command-guard' "$HPLUG/plugin.yaml" || fail 'C7: plugin.yaml name'
grep -q 'pre_tool_call' "$HPLUG/plugin.yaml" || fail 'C7: plugin.yaml must declare the pre_tool_call hook'
ORCHESTRATORMAXXING_AGENT_GUARD="$TOOL" python3 - "$HPLUG/__init__.py" "$TMP" <<'PY' || fail 'C7: hermes plugin behaviour'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("command_guard_plugin", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
hooks = {}
class Ctx:
    def register_hook(self, name, cb): hooks[name] = cb
m.register(Ctx())
cb = hooks["pre_tool_call"]
r = cb(tool_name="terminal", args={"command": "rm -rf /"})
assert isinstance(r, dict) and r.get("action") == "block" and "Matched pattern" in r.get("message", ""), r
assert cb(tool_name="terminal", args={"command": "ls -la"}) is None
assert cb(tool_name="write_file", args={"path": "x", "content": "rm -rf /"}) is None
assert cb(tool_name="terminal", args={"command": 42}) is None
assert cb(tool_name="terminal", args=None) is None
os.environ["ORCHESTRATORMAXXING_AGENT_GUARD"] = os.path.join(sys.argv[2], "no-such-binary")
assert cb(tool_name="terminal", args={"command": "rm -rf /"}) is None, "a missing guard binary must fail open"
PY
ok "C7: Hermes command-guard plugin — terminal deny → block dict, allow → None, other tools/failures → None"

# C8: wire claude — idempotent merge into a settings.json with existing hooks and unrelated keys
S="$TMP/settings.json"
python3 - "$S" <<'PY'
import json, sys
json.dump({"model": "opus", "hooks": {
  "Stop": [{"hooks": [{"type": "command", "command": "session-log check", "timeout": 15}]}],
  "PreToolUse": [{"matcher": "AskUserQuestion|ExitPlanMode", "hooks": [{"type": "command", "command": "agent-tab-status attention", "timeout": 5}]}],
}}, open(sys.argv[1], "w"), indent=2)
PY
if "$TOOL" wire claude --settings "$S" --check >/dev/null 2>&1; then fail 'C8: --check must be red before wiring'; fi
"$TOOL" wire claude --settings "$S" >/dev/null || fail 'C8: wire claude failed'
cp "$S" "$TMP/settings.first.json"
"$TOOL" wire claude --settings "$S" >/dev/null || fail 'C8: second wire failed'
cmp -s "$S" "$TMP/settings.first.json" || fail 'C8: wire claude is not idempotent'
"$TOOL" wire claude --settings "$S" --check >/dev/null || fail 'C8: --check must be green after wiring'
python3 - "$S" <<'PY' || fail 'C8: merged settings shape'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["model"] == "opus"
h = d["hooks"]
assert any(x["hooks"][0]["command"] == "session-log check" for x in h["Stop"])
pre = [g for g in h["PreToolUse"] if any('agent-guard" pre' in k["command"] for k in g["hooks"])]
assert len(pre) == 1 and pre[0]["matcher"] == "Bash" and pre[0]["hooks"][0]["timeout"] <= 15, pre
assert any(g.get("matcher") == "AskUserQuestion|ExitPlanMode" for g in h["PreToolUse"]), "existing PreToolUse group lost"
post = [g for g in h["PostToolUse"] if any('agent-guard" post' in k["command"] for k in g["hooks"])]
assert len(post) == 1 and post[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit", post
assert "SOLPLAN_CHILD" not in json.dumps(post + pre)
PY
ok "C8: wire claude — one Pre + one Post group, existing hooks and keys preserved, idempotent, --check"

# C9: Warp — targeted denylist insertion into every profile, line-guarded, idempotent, tomllib-verified escaping
W="$TMP/warp.toml"
cat > "$W" <<'TOML'
[terminal]
osc52_clipboard_access = "write_only"

[agents.execution_profiles]

[agents.execution_profiles.default]
apply_code_diffs = "agent_decides"
command_allowlist = []
command_denylist = [
  'bash(\s.*)?',
  'rm(\s.*)?',
]
name = "Default"

[agents.execution_profiles.strict]
command_denylist = []
name = "Strict"

[privacy]
custom_secret_regex_list = [
  { name = "IPv4", pattern = 'a\b' },
  {
    name = "IPv6",
    pattern = 'b\b',
  },
]
TOML
cp "$W" "$TMP/warp.orig.toml"
if "$TOOL" warp --check --settings "$W" >/dev/null 2>&1; then fail 'C9: --check must be red before apply'; fi
"$TOOL" warp --apply --settings "$W" >/dev/null || fail 'C9: apply failed'
"$TOOL" warp --check --settings "$W" >/dev/null || fail 'C9: --check must be green after apply'
cp "$W" "$TMP/warp.first.toml"
"$TOOL" warp --apply --settings "$W" >/dev/null || fail 'C9: second apply failed'
cmp -s "$W" "$TMP/warp.first.toml" || fail 'C9: warp apply is not idempotent'
python3 - "$TMP/warp.orig.toml" "$W" "$GUARD_SRC/dangerous-patterns.txt" <<'PY' || fail 'C9: warp edit guard'
import re, sys
orig, new, pats = [open(p, encoding="utf-8").read() for p in sys.argv[1:]]
patterns = [l for l in pats.splitlines() if l.strip() and not l.startswith("#")]
# 1. every non-inserted line is untouched: drop the inserted (basic-string) lines and compare
inserted = [l for l in new.splitlines(keepends=True) if re.match(r'^\s*"', l)]
assert inserted, "no basic-string lines inserted"
rest = "".join(l for l in new.splitlines(keepends=True) if not re.match(r'^\s*"', l))
rest = rest.replace('\n[agents.warp_agent.other]\nauto_approve_bypasses_command_denylist = false\n', '')
# the previously-inline `command_denylist = []` legitimately became a multi-line array
rest_norm = rest.replace("command_denylist = [\n]", "command_denylist = []")
assert rest_norm == orig, "a non-denylist line changed"
# 2. both profiles carry every pattern (count per profile)
profiles = re.split(r"^\[agents\.execution_profiles\.[^\]]+\]\s*$", new, flags=re.M)[1:]
assert len(profiles) == 2
for body in profiles:
    arr = re.search(r"command_denylist = \[(.*?)^\]", body, flags=re.S | re.M)
    assert arr, "denylist array not found"
    assert arr.group(1).count('\n  "') >= len(patterns), (arr.group(1).count('\n  "'), len(patterns))
# 3. the original literal-string user entries survived
assert "'rm(\\s.*)?'," in new and "'bash(\\s.*)?'," in new
# 4. inline-table trailing comma untouched
assert "pattern = 'b\\b',\n  },\n]" in new
PY
# escaping check on a tomllib-clean fixture (Warp's real files have a trailing-comma inline table tomllib rejects)
W2="$TMP/warp-clean.toml"; printf '[agents.execution_profiles.default]\ncommand_denylist = []\nname = "Default"\n' > "$W2"
"$TOOL" warp --apply --settings "$W2" >/dev/null || fail 'C9: clean apply failed'
python3 - "$W2" "$GUARD_SRC/dangerous-patterns.txt" <<'PY' || fail 'C9: tomllib round-trip of the inserted patterns'
import sys
try:
    import tomllib
except ImportError:
    sys.exit(0)
d = tomllib.loads(open(sys.argv[1], encoding="utf-8").read())
got = d["agents"]["execution_profiles"]["default"]["command_denylist"]
pats = [l.replace("[:space:]", r"\s") for l in open(sys.argv[2], encoding="utf-8").read().splitlines() if l.strip() and not l.startswith("#")]
missing = [p for p in pats if p not in got]
assert not missing, missing[:3]
PY
printf '[agents.execution_profiles.default]\nname = "Default"\n' > "$TMP/warp-nokey.toml"
rc=0; "$TOOL" warp --apply --settings "$TMP/warp-nokey.toml" >/dev/null 2>&1 || rc=$?
[[ "$rc" == "2" ]] || fail "C9: a profile without command_denylist must refuse with exit 2 (got $rc)"
ok "C9: Warp denylist — every profile, guarded insertion, literal user entries kept, idempotent, tomllib-verified, refuses"

# C10: Zed — strict JSON merge and JSONC insertion that preserves comments; user deny entries kept
Z="$TMP/zed.json"
python3 - "$Z" <<'PY'
import json, sys
json.dump({"theme": {"mode": "system"}, "agent": {"default_model": {"provider": "ollama", "model": "kimi-k3"},
           "tool_permissions": {"default": "confirm", "tools": {"terminal": {"always_deny": [{"pattern": "user-kept"}]}}}}}, open(sys.argv[1], "w"), indent=2)
PY
if "$TOOL" zed --check --settings "$Z" >/dev/null 2>&1; then fail 'C10: --check must be red before apply'; fi
"$TOOL" zed --apply --settings "$Z" >/dev/null || fail 'C10: strict apply failed'
"$TOOL" zed --check --settings "$Z" >/dev/null || fail 'C10: --check green after apply'
cp "$Z" "$TMP/zed.first.json"; "$TOOL" zed --apply --settings "$Z" >/dev/null; cmp -s "$Z" "$TMP/zed.first.json" || fail 'C10: not idempotent'
python3 - "$Z" "$GUARD_SRC/dangerous-patterns.txt" <<'PY' || fail 'C10: strict merge shape'
import json, sys
d = json.load(open(sys.argv[1]))
pats = [l.replace("[:space:]", r"\s") for l in open(sys.argv[2], encoding="utf-8").read().splitlines() if l.strip() and not l.startswith("#")]
deny = d["agent"]["tool_permissions"]["tools"]["terminal"]["always_deny"]
assert deny[0] == {"pattern": "user-kept"}, deny[0]
have = {e["pattern"] for e in deny}
assert all(p in have for p in pats)
assert all(e.get("case_sensitive") is True for e in deny if e["pattern"] != "user-kept")
assert d["theme"] == {"mode": "system"} and d["agent"]["default_model"]["model"] == "kimi-k3"
assert d["agent"]["tool_permissions"]["default"] == "confirm"
PY
ZC="$TMP/zed.jsonc"
cat > "$ZC" <<'JSONC'
// Zed settings — the operator's comments must survive
{
  "theme": { "mode": "system" }, // trailing comment
  "agent": {
    /* block comment */
    "default_model": { "provider": "ollama", "model": "kimi-k3", },
  },
}
JSONC
"$TOOL" zed --apply --settings "$ZC" >/dev/null || fail 'C10: JSONC apply must succeed (insertion into an existing agent object)'
"$TOOL" zed --check --settings "$ZC" >/dev/null || fail 'C10: JSONC --check green after apply'
python3 - "$ZC" "$ROOT/bin/zed-setup" <<'PY' || fail 'C10: JSONC result'
import importlib.util, json, sys
import importlib.machinery
spec = importlib.util.spec_from_loader("zs", importlib.machinery.SourceFileLoader("zs", sys.argv[2])); zs = importlib.util.module_from_spec(spec); spec.loader.exec_module(zs)
src = open(sys.argv[1], encoding="utf-8").read()
assert "// Zed settings — the operator's comments must survive" in src and "/* block comment */" in src and "// trailing comment" in src
d = json.loads(zs.jsonc_to_json(src))
assert d["theme"] == {"mode": "system"} and d["agent"]["default_model"]["model"] == "kimi-k3"
assert len(d["agent"]["tool_permissions"]["tools"]["terminal"]["always_deny"]) >= 30
PY
printf '{"agent": [1,2' > "$TMP/zed-broken.json"
rc=0; "$TOOL" zed --apply --settings "$TMP/zed-broken.json" >/dev/null 2>&1 || rc=$?
[[ "$rc" == "2" ]] || fail "C10: unparseable settings must refuse with exit 2 (got $rc)"
rc=0; "$TOOL" zed --apply --settings "$TMP/does-not-exist/settings.json" >/dev/null 2>&1 || rc=$?
[[ "$rc" == "0" ]] || fail 'C10: absent settings (Zed-less machine) must be a silent no-op'
ok "C10: Zed tool_permissions — strict merge keeps user entries, JSONC insertion keeps comments, refuses garbage"

# C11: status --gate — hermetic HOME; present hosts must be wired, absent hosts are 'absent'
mkdir -p "$HOME/.claude" "$HOME/.config/opencode/plugins"
cp "$S" "$HOME/.claude/settings.json"
cp "$PLUGIN" "$HOME/.config/opencode/plugins/orchestratormaxxing-guard.js"
"$TOOL" status --gate --repo "$ROOT" > "$TMP/gate.out" 2>&1 || fail "C11: gate must be green with claude+opencode wired and warp/zed/hermes absent: $(cat "$TMP/gate.out")"
"$TOOL" status --json --repo "$ROOT" | python3 -c '
import json,sys; s=json.load(sys.stdin); h=s["hosts"]
assert h["claude"]["state"]=="wired", h["claude"]
assert h["opencode"]["state"]=="wired", h["opencode"]
assert h["codex"]["state"]=="wired", h["codex"]
for k in ("warp","zed","hermes"): assert h[k]["state"]=="absent", (k, h[k])
' || fail 'C11: status --json host states'
rm "$HOME/.config/opencode/plugins/orchestratormaxxing-guard.js"
if "$TOOL" status --gate --repo "$ROOT" >/dev/null 2>&1; then fail 'C11: gate must go red when a present host loses its adapter'; fi
mkdir -p "$HOME/.config/zed"; printf '{}' > "$HOME/.config/zed/settings.json"
"$TOOL" status --json --repo "$ROOT" | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["hosts"]["zed"]["state"]=="missing", s["hosts"]["zed"]' || fail 'C11: an unwired present host must read missing'
ok "C11: status --gate — wired/missing/absent are three different states and only present hosts can red the gate"

# C12: selftest against the real corpus
"$TOOL" selftest > "$TMP/selftest.out" || fail "C12: selftest failed: $(tail -3 "$TMP/selftest.out")"
grep -Eq 'passed: [0-9]+, failed: 0' "$TMP/selftest.out" || fail 'C12: selftest summary line'
[[ "$(sed -n 's/.*passed: \([0-9]*\).*/\1/p' "$TMP/selftest.out")" -ge 150 ]] || fail 'C12: selftest must run the whole corpus'
ok "C12: selftest runs the vendored corpus green"

# C13: Codex plugin hooks.json carries both guard entries, without the planner-child short-circuit
python3 - "$ROOT/plugins/orchestratormaxxing/hooks/hooks.json" <<'PY' || fail 'C13: codex hooks.json'
import json, sys
h = json.load(open(sys.argv[1]))["hooks"]
pre = [g for g in h["PreToolUse"] if any('agent-guard" pre' in k["command"] for k in g["hooks"])]
assert len(pre) == 1 and pre[0]["matcher"] == "Bash", pre
post = [g for g in h["PostToolUse"] if any('agent-guard" post' in k["command"] for k in g["hooks"])]
assert len(post) == 1 and "apply_patch" in post[0]["matcher"], post
for g in pre + post:
    for k in g["hooks"]:
        assert "SOLPLAN_CHILD" not in k["command"], "the guard must not skip planner children"
        assert k.get("timeout", 99) <= 15
PY
ok "C13: Codex plugin hooks — Bash pre-guard and apply_patch post-guard, no child short-circuit"

# C14: deploy wiring — install.sh and harness-verify reference the new surfaces
for needle in 'agent-guard" wire claude\|agent-guard wire claude' 'agent-guard.* warp' 'agent-guard.* zed' 'deploy/agent-guard' 'command-guard' 'trust-codex'; do
  grep -q -e "$needle" "$ROOT/install.sh" || fail "C14: install.sh lacks: $needle"
done
grep -q 'tests/agent-guard/run.sh' "$ROOT/bin/harness-verify" || fail 'C14: harness-verify has no agent-guard contract row'
grep -q 'agent-guard' "$ROOT/CLAUDE.md" || fail 'C14: CLAUDE.md does not document agent-guard'
ok "C14: install.sh wires the tool, data, adapters and trust step; harness-verify runs this contract"

# C15: trust-codex — writes trusted_hash for orchestratormaxxing hooks only, targeted, idempotent, --check
INV="$TMP/inventory.json"
cat > "$INV" <<'JSON'
{"data":[{"cwd":"/x","hooks":[
 {"key":"orchestratormaxxing@personal:hooks/hooks.json:pre_tool_use:1:0","pluginId":"orchestratormaxxing@personal","currentHash":"sha256:aaaa","trustStatus":"untrusted"},
 {"key":"orchestratormaxxing@personal:hooks/hooks.json:stop:0:0","pluginId":"orchestratormaxxing@personal","currentHash":"sha256:bbbb","trustStatus":"modified"},
 {"key":"orchestratormaxxing@personal:hooks/hooks.json:session_start:0:0","pluginId":"orchestratormaxxing@personal","currentHash":"sha256:cccc","trustStatus":"trusted"},
 {"key":"other@vendor:hooks/hooks.json:stop:0:0","pluginId":"other@vendor","currentHash":"sha256:dddd","trustStatus":"untrusted"}]}]}
JSON
CFG="$TMP/config.toml"
printf 'model = "gpt-5.6-sol"\n\n[hooks.state]\n\n[hooks.state."orchestratormaxxing@personal:hooks/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:old"\n\n[hooks.state."orchestratormaxxing@personal:hooks/hooks.json:session_start:0:0"]\ntrusted_hash = "sha256:cccc"\n\n[features]\nvoice_transcription = true\n' > "$CFG"
if "$TOOL" trust-codex --check --inventory "$INV" --config "$CFG" >/dev/null 2>&1; then fail 'C15: --check must be red with untrusted orchestratormaxxing hooks'; fi
"$TOOL" trust-codex --apply --inventory "$INV" --config "$CFG" >/dev/null || fail 'C15: apply failed'
cp "$CFG" "$TMP/config.first.toml"; "$TOOL" trust-codex --apply --inventory "$INV" --config "$CFG" >/dev/null; cmp -s "$CFG" "$TMP/config.first.toml" || fail 'C15: not idempotent'
python3 - "$CFG" <<'PY' || fail 'C15: config.toml result'
import sys
try:
    import tomllib
except ImportError:
    sys.exit(0)
d = tomllib.loads(open(sys.argv[1], encoding="utf-8").read())
st = d["hooks"]["state"]
assert st["orchestratormaxxing@personal:hooks/hooks.json:pre_tool_use:1:0"]["trusted_hash"] == "sha256:aaaa"
assert st["orchestratormaxxing@personal:hooks/hooks.json:stop:0:0"]["trusted_hash"] == "sha256:bbbb"
assert st["orchestratormaxxing@personal:hooks/hooks.json:session_start:0:0"]["trusted_hash"] == "sha256:cccc"
assert "other@vendor:hooks/hooks.json:stop:0:0" not in st, "never trust another plugin's hooks"
assert d["model"] == "gpt-5.6-sol" and d["features"]["voice_transcription"] is True
PY
grep -q '^voice_transcription = true$' "$CFG" || fail 'C15: unrelated lines must survive byte-for-byte'
ok "C15: trust-codex — orchestratormaxxing hooks trusted from the inventory, other plugins untouched, idempotent"


# ---------------------------------------------------------------------------
# Cases C16–C20 were added after the 2026-09-05 adversarial review; each was
# proven RED against the pre-review tool before its fix landed.

# C16: a non-UTF-8 byte in the patterns file must FAIL OPEN at call time (the
# review found it turned `pre` into exit 2 on EVERY command) and red the gate.
mkdir -p "$TMP/badutf"; { cat "$GUARD_SRC/dangerous-patterns.txt"; printf '# comentario con \xf1\n'; } > "$TMP/badutf/dangerous-patterns.txt"
cp "$GUARD_SRC/security_patterns.py" "$TMP/badutf/"
[[ "$(ORCHESTRATORMAXXING_GUARD_DIR="$TMP/badutf" pre_rc 'ls -la')" == "0" ]] || fail 'C16: a Latin-1 byte in the patterns file must not block ls'
[[ "$(ORCHESTRATORMAXXING_GUARD_DIR="$TMP/badutf" pre_rc 'rm -rf /')" == "2" ]] || fail 'C16: the other rules must still block after a decode error'
if ORCHESTRATORMAXXING_GUARD_DIR="$TMP/badutf" "$TOOL" status --gate >/dev/null 2>&1; then fail 'C16: a decode error must red the gate'; fi
ok "C16: invalid UTF-8 in the patterns file — allow at call time, other rules intact, gate red"

# C17: grep -E semantics — every line is matched on its own. The review showed the
# whole-string match blocked heredocs with an indented `pass` line, and that
# `[^;&|]*` spanning newlines made a 110 KB command take 18 s (a timeout bypass).
python3 - "$TOOL" <<'PY'
import json, subprocess, sys, time
tool = sys.argv[1]
def rc(cmd):
    r = subprocess.run([tool, "pre"], input=json.dumps({"tool_input": {"command": cmd}}), capture_output=True, text=True, timeout=60)
    return r.returncode
assert rc("python3 - <<'PY'\nimport json\nfor x in []:\n    pass\nprint(json.dumps({}))\nPY") == 0, "indented pass inside a heredoc must not match the password-store rule"
assert rc("git push origin feature\nls -f") == 0, "-f on a later line is not a force push"
assert rc("git push origin main\ngh pr create -d") == 0, "-d on a later line is not a remote delete"
assert rc("cd /tmp\npass show example/service") == 2, "a dangerous LINE still blocks in a multi-line command"
assert rc("echo ok\nrm -rf /") == 2
assert rc("rm -rf /\x00") == 2, "a NUL after the target must not defeat the root-rm rule"
big = "git push origin main\n" * 5000
t = time.perf_counter(); assert rc(big) == 0; el = time.perf_counter() - t
assert el < 3.0, f"5000 benign lines took {el:.1f}s (quadratic matching is back)"
t = time.perf_counter(); assert rc(big + "gh auth token") == 2; el = time.perf_counter() - t
assert el < 3.0, f"late dangerous line after 5000 benign ones took {el:.1f}s"
PY
ok "C17: per-line matching — heredoc pass allowed, dangerous line still blocks, NUL stripped, 5000 lines under 3 s"

# C18: post hook reads NotebookEdit's notebook_path (the .ipynb-gated rules fire)
printf '%s' '{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"/tmp/n.ipynb","new_source":"import pickle\nx = pickle.load(f)\n"}}' \
  | "$TOOL" post --format json | python3 -c 'import json,sys; j=json.load(sys.stdin); assert any(f["rule"]=="pickle_deserialization" for f in j["findings"]), j' \
  || fail 'C18: NotebookEdit notebook_path is not scanned as an .ipynb'
ok "C18: NotebookEdit notebook_path reaches the path-gated rules"

# C19: status semantics — no patterns loaded means warp/zed cannot read 'wired';
# and a Hermes plugins.enabled block-sequence at indent 2 (PyYAML default) is read.
mkdir -p "$TMP/h19/.config/zed" "$TMP/h19/.config/warp-terminal" "$TMP/h19/.hermes/plugins/command-guard"
printf '{}' > "$TMP/h19/.config/zed/settings.json"
printf '[agents.execution_profiles.default]\ncommand_denylist = []\n' > "$TMP/h19/.config/warp-terminal/settings.toml"
HOME="$TMP/h19" ORCHESTRATORMAXXING_GUARD_DIR="$TMP/empty" WARP_MODEL_PIN_LAYOUT=linux "$TOOL" status --json --repo "$ROOT" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); h=s["hosts"]; assert h["zed"]["state"]=="missing" and h["warp"]["state"]=="missing", (h["zed"], h["warp"])' \
  || fail 'C19: warp/zed must read missing when no patterns are loaded'
: > "$TMP/h19/.hermes/kanban.db"; cp "$HPLUG/__init__.py" "$TMP/h19/.hermes/plugins/command-guard/"
printf 'plugins:\n  enabled:\n  - command-guard\n  - security-guidance\n  disabled: []\n' > "$TMP/h19/.hermes/config.yaml"
# a stale kanban.db with a DANGLING ~/.local/bin/hermes (the Mac, measured 2026-09-05) is not a Hermes host
mkdir -p "$TMP/h19/.local/bin"; ln -s "$TMP/h19/no-such-venv/bin/hermes" "$TMP/h19/.local/bin/hermes"
HOME="$TMP/h19" PATH="/usr/bin:/bin" "$TOOL" status --json --repo "$ROOT" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["hosts"]["hermes"]["state"]=="absent", s["hosts"]["hermes"]' \
  || fail 'C19: kanban.db without a usable hermes CLI must read absent, never missing'
rm "$TMP/h19/.local/bin/hermes"; printf '#!/bin/sh\nexit 0\n' > "$TMP/h19/.local/bin/hermes"; chmod +x "$TMP/h19/.local/bin/hermes"
HOME="$TMP/h19" PATH="/usr/bin:/bin" "$TOOL" status --json --repo "$ROOT" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["hosts"]["hermes"]["state"]=="wired", s["hosts"]["hermes"]' \
  || fail 'C19: plugins.enabled written as an indent-2 block sequence must count as enabled'
ok "C19: status — no patterns means warp/zed missing; dangling hermes CLI means absent; indent-2 YAML sequences are read"

# C20: install ordering — the guard data must be deployed before any adapter apply
python3 - "$ROOT/install.sh" <<'PY' || fail 'C20: install.sh must deploy the guard data before agent-guard zed/warp --apply'
import re, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
def first(pat):
    for i, l in enumerate(lines, 1):
        if re.search(pat, l): return i
    raise SystemExit(f"missing: {pat}")
data = first(r'^GUARD_DST=')
assert data < first(r'agent-guard" zed --apply'), "zed apply runs before the data copy"
assert data < first(r'agent-guard" warp --apply'), "warp apply runs before the data copy"
assert data < first(r'agent-guard" wire claude'), "wire claude runs before the data copy"
PY
ok "C20: install.sh deploys the guard data before every adapter apply"

python3 "$ROOT/tests/agent-guard/warp-parser.py" || fail 'C21: Warp canonical serialization and owned deduplication'
ok "C21: Warp canonical serialization and owned deduplication"
python3 "$ROOT/tests/agent-guard/warp-policy.py" || fail 'C22: Warp defaults, bypass and desktop/CLI coverage'
ok "C22: Warp defaults, bypass and desktop/CLI coverage"
printf 'agent-guard contract: PASS (%d)\n' "$PASS"
