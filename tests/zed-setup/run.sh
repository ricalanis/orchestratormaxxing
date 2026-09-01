#!/usr/bin/env bash
# Contract for bin/zed-setup — the idempotent Zed↔Ollama-Cloud config merger
# (sibling of warp-ollama for the file-editable host). Authored BEFORE the tool
# (Tier-0). The tool's whole job: additively merge
#   language_models.ollama.api_url = "https://ollama.com"
# into ~/.config/zed/settings.json without destroying user state, never storing
# key material in that file, and staying a silent no-op on a Zed-less machine.
# C1/C3 were proven RED against a seeded clobber-and-leak merger before the
# real tool existed (behavioral-gates-prove-failure).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/zed-setup"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'zed-setup contract: %s\n' "$*" >&2; exit 1; }
[[ -x "$TOOL" ]] || fail 'tool missing or not executable'

export HOME="$TMP/home"
mkdir -p "$HOME/.config/zed" "$HOME/.local/share/opencode" "$TMP/emptybin"
# Hermetic PATH: python3 + coreutils, no `zed` binary anywhere.
BASEPATH="/usr/bin:/bin"
FAKE_KEY="sk-zedtest-Ab12Cd34Ef56Gh78"  # gitleaks:allow
printf '{"ollama-cloud": {"key": "%s"}}\n' "$FAKE_KEY" \
  > "$HOME/.local/share/opencode/auth.json"

SETTINGS="$HOME/.config/zed/settings.json"

# The reference fixture mirrors the Mac's live settings.json shape: real user
# state (theme, agent model, favorites, dock) that a clobbering merger destroys.
write_fixture() {
python3 - "$SETTINGS" <<'PY'
import json, sys
cfg = {
    "edit_predictions": {"provider": "ollama"},
    "agent": {
        "default_model": {"provider": "ollama", "model": "kimi-k3",
                          "enable_thinking": True},
        "favorite_models": [
            {"provider": "ollama", "model": "glm-5.2"},
            {"provider": "ollama", "model": "deepseek-v4-flash:0731"},
        ],
        "dock": "right",
    },
    "theme": {"mode": "system", "light": "One Light", "dark": "One Dark"},
    "ui_font_size": 16,
}
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY
}

# --- C1: merge sets api_url AND preserves every user key -------------------
write_fixture
chmod 640 "$SETTINGS"
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C1: merge run failed'
python3 - "$SETTINGS" <<'PY' || fail 'C1: file mode not preserved across atomic rewrite'
import os, stat, sys
assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o640
PY
python3 - "$SETTINGS" <<'PY' || fail 'C1: written file lacks trailing newline'
import sys
assert open(sys.argv[1], 'rb').read().endswith(b'\n')
PY
python3 - "$SETTINGS" "$ROOT/bin/oll" <<'PY' || fail 'C1: api_url missing or user state destroyed'
import json, runpy, sys
cfg = json.load(open(sys.argv[1]))
assert cfg["language_models"]["ollama"]["api_url"] == "https://ollama.com"
# CHANGED 2026-08-24 by explicit instruction: zed-setup now OWNS default_model and
# aims it at bin/oll's DEFAULT_MODEL, so Zed and Warp cannot drift apart. The old
# assertion pinned the literal "kimi-k3" and encoded the previous invariant, that
# the merge never touches the user's model choice. Everything else stays additive.
assert cfg["agent"]["default_model"]["model"] == runpy.run_path(sys.argv[2])["DEFAULT_MODEL"]
assert cfg["agent"]["default_model"]["provider"] == "ollama"
assert cfg["agent"]["favorite_models"][1]["model"] == "deepseek-v4-flash:0731"
assert cfg["theme"]["light"] == "One Light"
assert cfg["ui_font_size"] == 16
assert cfg["edit_predictions"]["provider"] == "ollama"
PY

# --- C2: second run is byte-identical (idempotent) -------------------------
cp "$SETTINGS" "$TMP/after1.json"
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C2: second run failed'
cmp -s "$SETTINGS" "$TMP/after1.json" || fail 'C2: second run rewrote the file'

# --- C3: no key material ever lands in settings.json -----------------------
grep -qF "$FAKE_KEY" "$SETTINGS" && fail 'C3: API key leaked into settings.json'
grep -qiE '"(api_key|apikey|token)"' "$SETTINGS" && fail 'C3: key-shaped field written'

# --- C4: missing settings.json → minimal valid file created ----------------
rm -f "$SETTINGS"
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C4: run without settings.json failed'
python3 - "$SETTINGS" <<'PY' || fail 'C4: minimal file invalid or missing api_url'
import json, sys
cfg = json.load(open(sys.argv[1]))
assert cfg["language_models"]["ollama"]["api_url"] == "https://ollama.com"
PY

# --- C5: Zed absent → exit 0, zero writes, no directory created ------------
ZEDLESS="$TMP/zedless"; mkdir -p "$ZEDLESS"
HOME="$ZEDLESS" PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 \
  || fail 'C5: nonzero exit on a Zed-less machine'
[[ -e "$ZEDLESS/.config/zed" ]] && fail 'C5: created ~/.config/zed on a Zed-less machine'

# --- C6: a file it cannot parse at all → refuse, byte-untouched ------------
# NOTE (2026-08-27, lq-95d9477d): C6 used to assert that ANY commented file was
# refused. That pinned an implementation accident as a requirement — the real
# invariant is "never modify a file it cannot edit exactly", and a targeted
# value splice satisfies it while still converging the Mac's live JSONC config
# (C12-C15). What must still refuse is a genuinely malformed document.
printf '// user comment Zed allows\n{"theme": {"mode": "dark"\n' > "$SETTINGS"
cp "$SETTINGS" "$TMP/jsonc.orig"
set +e
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c6.err"; C6_RC=$?
set -e
[[ "$C6_RC" -eq 2 ]] || fail "C6: parse refusal must exit 2 (got $C6_RC)"
grep -qi 'ERROR' "$TMP/c6.err" || fail 'C6: refusal must explain itself on stderr'
cmp -s "$SETTINGS" "$TMP/jsonc.orig" || fail 'C6: modified a file it could not parse'

# --- C6b: valid JSONC that would need an INSERTED key → refuse, untouched --
# The splice path can only replace an existing scalar. Adding a key to a
# commented file means reformatting it, which destroys the comments — so the
# tool must refuse loudly and name what is missing rather than guess.
printf '// Zed settings\n{\n  "theme": {"mode": "dark"},\n}\n' > "$SETTINGS"
cp "$SETTINGS" "$TMP/c6b.orig"
set +e
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c6b.err"; C6B_RC=$?
set -e
[[ "$C6B_RC" -eq 2 ]] || fail "C6b: insertion refusal must exit 2 (got $C6B_RC)"
cmp -s "$SETTINGS" "$TMP/c6b.orig" || fail 'C6b: modified a file it could not edit exactly'
grep -qF 'language_models.ollama.api_url' "$TMP/c6b.err" \
  || fail 'C6b: refusal must name the key it cannot insert'

# --- C7: --check is read-only and truthful ---------------------------------
write_fixture
cp "$SETTINGS" "$TMP/check.orig"
set +e
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1; C7_RC=$?
set -e
[[ "$C7_RC" -eq 1 ]] || fail "C7: --check must exit 1 while api_url absent (got $C7_RC)"
cmp -s "$SETTINGS" "$TMP/check.orig" || fail 'C7: --check wrote to settings.json'
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1 || fail 'C7: --check exit 1 after merge'

# --- C7b: --check reads JSONC tolerantly (the Mac's live config shape) -----
# Reading must be JSONC-tolerant — a correct commented config reported as
# broken is a false alarm on every Mac install.sh run. (Writing is handled by
# the targeted splice in C12-C15.)
# The api_url value itself contains "//", so a naive comment-stripper that
# corrupts strings fails this case.
cat > "$SETTINGS" <<'JSONC'
// Zed settings
{
  // provider
  "language_models": {
    "ollama": {
      "api_url": "https://ollama.com",
    },
  },
  // the agent default is owned by zed-setup and sourced from bin/oll
  "agent": {"default_model": {"provider": "ollama", "model": "deepseek-v4-flash:0731"}},
  "theme": {"mode": "dark"},
}
JSONC
cp "$SETTINGS" "$TMP/c7b.orig"
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1 \
  || fail 'C7b: --check must exit 0 on a correctly-configured JSONC file'
cmp -s "$SETTINGS" "$TMP/c7b.orig" || fail 'C7b: --check wrote to the JSONC file'

# --- C7c: JSONC without api_url → --check exits 1 (not a parse crash) ------
printf '// Zed settings\n{\n  "theme": {"mode": "dark"},\n}\n' > "$SETTINGS"
set +e
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1; C7C_RC=$?
set -e
[[ "$C7C_RC" -eq 1 ]] || fail "C7c: --check must exit 1 on unconfigured JSONC (got $C7C_RC)"

# --- C8: --print never writes and shows the paste-ready block --------------
rm -f "$SETTINGS"
OUT="$(PATH="$BASEPATH" "$TOOL" --print 2>/dev/null)" || fail 'C8: --print failed'
grep -qF 'https://ollama.com' <<<"$OUT" || fail 'C8: --print lacks the Ollama Cloud URL'
grep -qF 'language_models' <<<"$OUT" || fail 'C8: --print lacks the provider block'
grep -qF 'settings.json' <<<"$OUT" || fail 'C8: --print does not name its paste target'
grep -qiE 'deepseek|kimi|glm|qwen|minimax|mistral' <<<"$OUT" \
  || fail 'C8: --print suggests no heavy worker model'
[[ -e "$SETTINGS" ]] && fail 'C8: --print created settings.json'

# --- C9: --key-hint prints the key without storing it anywhere -------------
OUT="$(PATH="$BASEPATH" "$TOOL" --key-hint 2>/dev/null)" || fail 'C9: --key-hint failed'
grep -qF "$FAKE_KEY" <<<"$OUT" || fail 'C9: --key-hint did not surface the auth-store key'
grep -qF 'LLM Providers' <<<"$OUT" || fail 'C9: --key-hint lacks the paste instruction'
grep -qF 'OLLAMA_API_KEY' <<<"$OUT" || fail 'C9: --key-hint lacks the env-var alternative'
[[ -e "$SETTINGS" ]] && fail 'C9: --key-hint created settings.json'

# --- C9b: --key-hint fails loudly when the auth store cannot supply a key --
mv "$HOME/.local/share/opencode/auth.json" "$TMP/auth.bak"
PATH="$BASEPATH" "$TOOL" --key-hint >/dev/null 2>&1 \
  && fail 'C9b: exit 0 with the auth store missing'
printf '{}\n' > "$HOME/.local/share/opencode/auth.json"
PATH="$BASEPATH" "$TOOL" --key-hint >/dev/null 2>&1 \
  && fail 'C9b: exit 0 with no ollama-cloud entry'
mv "$TMP/auth.bak" "$HOME/.local/share/opencode/auth.json"

# --- C10: zed on PATH but no config dir yet → dir created, config merged ---
FRESH="$TMP/fresh"; mkdir -p "$FRESH" "$TMP/zbin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/zbin/zed"; chmod +x "$TMP/zbin/zed"
HOME="$FRESH" PATH="$TMP/zbin:$BASEPATH" "$TOOL" >/dev/null 2>&1 \
  || fail 'C10: run failed with zed on PATH and no config dir'
python3 - "$FRESH/.config/zed/settings.json" <<'PY' || fail 'C10: config dir/file not created for an installed Zed'
import json, sys
cfg = json.load(open(sys.argv[1]))
assert cfg["language_models"]["ollama"]["api_url"] == "https://ollama.com"
PY

# --- C11: the agent default comes from bin/oll's DEFAULT_MODEL, not a literal ---
# Zed and Warp must not drift: both read the same constant, so re-aiming the fleet
# is one edit in bin/oll. A hardcoded tag here is the regression this catches.
C11="$TMP/c11"; mkdir -p "$C11/.config/zed"
printf '{"theme":"One Dark","agent":{"always_allow_tool_actions":true}}\n' > "$C11/.config/zed/settings.json"
HOME="$C11" PATH="$TMP/zbin:$BASEPATH" "$TOOL" >/dev/null 2>&1 \
  || fail 'C11: run failed on a config with an existing agent block'
python3 - "$C11/.config/zed/settings.json" "$ROOT/bin/oll" <<'PY' || fail 'C11: default_model not sourced from policy'
import json, runpy, sys
cfg = json.load(open(sys.argv[1]))
want = runpy.run_path(sys.argv[2])["DEFAULT_MODEL"]
dm = cfg["agent"]["default_model"]
assert dm["provider"] == "ollama", f"provider is {dm.get('provider')!r}, not ollama"
assert dm["model"] == want, f"model is {dm.get('model')!r}, policy says {want!r}"
assert cfg["agent"]["always_allow_tool_actions"] is True, "clobbered a sibling agent key"
assert cfg["theme"] == "One Dark", "clobbered an unrelated key"
PY

# --- C11: the agent default comes from bin/oll's DEFAULT_MODEL, not a literal ---
# Zed and Warp must not drift: both read the same constant, so re-aiming the fleet
# is one edit in bin/oll. A hardcoded tag here is the regression this catches.
C11="$TMP/c11"; mkdir -p "$C11/.config/zed"
printf '{"theme":"One Dark","agent":{"always_allow_tool_actions":true}}\n' > "$C11/.config/zed/settings.json"
HOME="$C11" PATH="$TMP/zbin:$BASEPATH" "$TOOL" >/dev/null 2>&1 \
  || fail 'C11: run failed on a config with an existing agent block'
python3 - "$C11/.config/zed/settings.json" "$ROOT/bin/oll" <<'PY' || fail 'C11: default_model not sourced from policy'
import json, runpy, sys
cfg = json.load(open(sys.argv[1]))
want = runpy.run_path(sys.argv[2])["DEFAULT_MODEL"]
dm = cfg["agent"]["default_model"]
assert dm["provider"] == "ollama", f"provider is {dm.get('provider')!r}, not ollama"
assert dm["model"] == want, f"model is {dm.get('model')!r}, policy says {want!r}"
assert cfg["agent"]["always_allow_tool_actions"] is True, "clobbered a sibling agent key"
assert cfg["theme"] == "One Dark", "clobbered an unrelated key"
PY

# --- C12: a STALE JSONC config is CONVERGED in place -----------------------
# This is the exact flaw lq-95d9477d recorded: the Mac's live settings.json is
# commented, so the strict-json.load write path exited 2 and Zed stayed pinned
# to a retired model while --check kept reporting the drift it could not fix.
# The tool must now edit the stale value in place and leave every comment,
# every trailing comma and every unrelated key byte-identical.
STALE_MODEL="deepseek-v4-pro:0813"
WANT_MODEL="$(python3 -c 'import runpy,sys; print(runpy.run_path(sys.argv[1])["DEFAULT_MODEL"])' "$ROOT/bin/oll")"
[[ "$WANT_MODEL" != "$STALE_MODEL" ]] \
  || fail 'C12: fixture stale model equals the policy default — case proves nothing'

write_jsonc_stale() {
cat > "$SETTINGS" <<JSONC
// Zed settings — hand-edited, comments must survive
{
  // provider: Ollama Cloud (the URL itself contains "//")
  "language_models": {
    "ollama": {
      "api_url": "https://ollama.com",
    },
  },
  /* the agent default is owned by zed-setup and sourced from bin/oll */
  "agent": {
    "default_model": {"provider": "ollama", "model": "$STALE_MODEL"},
    "favorite_models": [
      {"provider": "ollama", "model": "$STALE_MODEL"},
      {"provider": "ollama", "model": "glm-5.2"},
    ],
    "dock": "right",
  },
  "theme": {"mode": "dark"}, // trailing comment
  "ui_font_size": 16,
}
JSONC
}

write_jsonc_stale
cp "$SETTINGS" "$TMP/c12.orig"
chmod 640 "$SETTINGS"
set +e
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c12.err"; C12_RC=$?
set -e
[[ "$C12_RC" -eq 0 ]] || fail "C12: must converge a stale JSONC config (got $C12_RC; $(cat "$TMP/c12.err"))"
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1 \
  || fail 'C12: --check still reports drift after the converge run'
python3 - "$SETTINGS" <<'PY' || fail 'C12: file mode not preserved across the splice'
import os, stat, sys
assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o640
PY

# --- C13: the splice targets ONLY agent.default_model.model ----------------
# favorite_models carries the SAME stale tag, so a line-oriented or
# string-replace implementation rewrites it too. Only the owned path may move.
python3 - "$SETTINGS" "$TMP/c12.orig" "$WANT_MODEL" "$STALE_MODEL" <<'PY' \
  || fail 'C13: splice hit a path zed-setup does not own, or missed its own'
import json, re, sys
new_text = open(sys.argv[1], encoding="utf-8").read()
old_text = open(sys.argv[2], encoding="utf-8").read()
want, stale = sys.argv[3], sys.argv[4]

def strip(source):
    """Mirror of the tool's comment mask, written independently here so the
    contract does not import the implementation it is grading."""
    out, i, in_str, esc = [], 0, False, False
    while i < len(source):
        c = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if in_str:
            out.append(c)
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            i += 1; continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if c == "/" and nxt in "/*":
            block = nxt == "*"
            j = i + 2
            while j < len(source):
                if block and source[j:j+2] == "*/":
                    j += 2; break
                if not block and source[j] in "\r\n":
                    break
                j += 1
            out.append("".join(
                ch if ch in "\r\n" else " " for ch in source[i:j]))
            i = j; continue
        out.append(c); i += 1
    return "".join(out)

def loads(text):
    clean = strip(text)
    clean = re.sub(r",(\s*[}\]])", r"\1", clean)
    return json.loads(clean)

old, new = loads(old_text), loads(new_text)
assert new["agent"]["default_model"]["model"] == want, \
    f'default_model.model is {new["agent"]["default_model"]["model"]!r}, want {want!r}'
assert [m["model"] for m in new["agent"]["favorite_models"]] == [stale, "glm-5.2"], \
    "favorite_models were rewritten — the splice is not path-targeted"
# everything the tool does not own is byte-for-byte identical
old["agent"]["default_model"]["model"] = want
assert old == new, "the splice changed a value outside agent.default_model.model"
# comments survive byte-for-byte: the comment characters are exactly the
# positions where the mask differs from the source.
def comments(text):
    return "".join(a for a, b in zip(text, strip(text)) if a != b)
assert comments(old_text) == comments(new_text), "comments were altered or dropped"
assert new_text.count("//") == old_text.count("//"), "comment markers lost"
assert "/*" in new_text and "*/" in new_text, "block comment lost"
assert new_text.rstrip().endswith("}"), "document tail mangled"
PY

# --- C14: converging JSONC is idempotent (second run is byte-identical) ----
cp "$SETTINGS" "$TMP/c14.after1"
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C14: second run on JSONC failed'
cmp -s "$SETTINGS" "$TMP/c14.after1" || fail 'C14: second run rewrote a converged JSONC file'

# --- C15: a wrong api_url in JSONC is corrected without eating the "//" ----
cat > "$SETTINGS" <<JSONC
// Zed settings
{
  "language_models": {"ollama": {"api_url": "http://localhost:11434"}},
  "agent": {"default_model": {"provider": "ollama", "model": "$WANT_MODEL"}},
}
JSONC
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C15: run failed on a wrong api_url'
grep -qF '"api_url": "https://ollama.com"' "$SETTINGS" \
  || fail 'C15: api_url not corrected in place'
grep -qF '// Zed settings' "$SETTINGS" || fail 'C15: leading comment destroyed'
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1 || fail 'C15: --check red after correcting api_url'

# --- C16: TWO owned values stale at once → both corrected --------------------
# lq-95d9477d mutation review 2026-08-27: every earlier JSONC case produced
# exactly ONE edit, so `sorted(edits, reverse=True)` in splice() was unfalsified
# — front-to-back application (which invalidates every later offset once a
# replacement changes length) passed the whole suite. Two edits of DIFFERENT
# length in one document is the smallest case that discriminates.
cat > "$SETTINGS" <<JSONC
// two owned values are stale here
{
  "language_models": {"ollama": {"api_url": "http://localhost:11434"}},
  "agent": {"default_model": {"provider": "ollama", "model": "$STALE_MODEL"}},
  "ui_font_size": 16,
}
JSONC
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c16.err" \
  || fail "C16: run failed on a doubly-stale JSONC file ($(cat "$TMP/c16.err"))"
grep -qF '"api_url": "https://ollama.com"' "$SETTINGS" || fail 'C16: api_url not corrected'
grep -qF "\"model\": \"$WANT_MODEL\"" "$SETTINGS" || fail 'C16: model not corrected'
grep -qF '// two owned values are stale here' "$SETTINGS" || fail 'C16: comment destroyed'
grep -qF '"ui_font_size": 16' "$SETTINGS" || fail 'C16: unrelated key mangled by a shifted offset'
PATH="$BASEPATH" "$TOOL" --check >/dev/null 2>&1 || fail 'C16: --check red after a two-edit splice'

# --- C17: a converged file is NOT rewritten (either write path) --------------
# install.sh runs this tool on every deploy. "Already correct" must mean "no
# write at all", not "write the same bytes back" — otherwise a converged run
# still churns the user's file (and its mtime) every time.
stamp() { python3 -c 'import os,sys; s=os.stat(sys.argv[1]); print(s.st_ino, s.st_mtime_ns)' "$SETTINGS"; }
assert_untouched() {
  local before after
  before="$(stamp)"
  PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail "$1: converged run failed"
  after="$(stamp)"
  [[ "$before" == "$after" ]] || fail "$1: rewrote an already-converged file"
}
assert_untouched 'C17a (JSONC splice path)'
write_fixture
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C17b: strict-JSON merge run failed'
assert_untouched 'C17b (strict-JSON merge path)'

# --- C18: strict JSON whose top level is not an object → refuse, untouched ---
printf '["not", "an", "object"]\n' > "$SETTINGS"
cp "$SETTINGS" "$TMP/c18.orig"
set +e
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c18.err"; C18_RC=$?
set -e
[[ "$C18_RC" -eq 2 ]] || fail "C18: non-object top level must exit 2 (got $C18_RC)"
cmp -s "$SETTINGS" "$TMP/c18.orig" || fail 'C18: modified a file whose top level is not an object'
grep -qi 'not an object' "$TMP/c18.err" || fail 'C18: refusal must say what is wrong'

# --- C19: awkward-but-legal JSONC still converges ----------------------------
# Shapes the earlier fixtures never had, each of which silently broke a distinct
# piece of the span scanner in the 2026-08-27 mutation review: a document whose
# FIRST character is '{' (so a scanner that starts at offset 1 is not saved by a
# leading comment), a backslash escape inside a string (escape-skip arithmetic),
# whitespace before ':', a 3-digit number, and an array opened with no
# whitespace after '['.
cat > "$SETTINGS" <<JSONC
{
  "language_models": {"ollama" : {"api_url": "http://localhost:11434"}},
  "agent": {
    "default_model" : {"provider": "ollama", "model": "$STALE_MODEL"},
    "favorite_models": [{"provider": "ollama", "model": "glm-5.2"}]
  },
  // a comment that arrives only after the document has opened
  "terminal": {"font_family": "C:\\\\Fonts\\\\Mono \\"Nerd\\""},
  "ui_font_size": 160
}
JSONC
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c19.err" \
  || fail "C19: run failed on legal-but-awkward JSONC ($(cat "$TMP/c19.err"))"
grep -qF "\"model\": \"$WANT_MODEL\"" "$SETTINGS" || fail 'C19: model not corrected'
grep -qF '"api_url": "https://ollama.com"' "$SETTINGS" || fail 'C19: api_url not corrected'
grep -qF 'C:\\Fonts\\Mono \"Nerd\"' "$SETTINGS" || fail 'C19: escaped string corrupted'
grep -qF '"ui_font_size": 160' "$SETTINGS" || fail 'C19: numeric literal corrupted'
grep -qF '"model": "glm-5.2"' "$SETTINGS" || fail 'C19: array element rewritten'
grep -qF '// a comment that arrives only after' "$SETTINGS" || fail 'C19: late comment destroyed'

# --- C20: the splice self-check actually bites -------------------------------
# verify_splice is the last line of defense before the user's disk. Drive it
# directly with the results a buggy splice would produce: the contract must show
# the guard REFUSES them, not merely that correct input passes through it.
python3 - "$ROOT/bin/zed-setup" <<'PY' || fail 'C20: verify_splice does not refuse a bad splice'
import runpy, sys
zs = runpy.run_path(sys.argv[1])
verify, targets = zs["verify_splice"], zs["owned_targets"]
model = dict(targets())[("agent", "default_model", "model")]
src = ('// keep me\n{\n  "language_models": {"ollama": {"api_url": "https://ollama.com"}},\n'
       '  "agent": {"default_model": {"provider": "ollama", "model": "stale-model"}},\n'
       '  "theme": "One Dark"\n}\n')
good = src.replace('"stale-model"', '"%s"' % model)
verify(src, good)  # the honest splice must pass

def refuses(result, why):
    try:
        verify(src, result)
    except ValueError:
        return
    raise AssertionError("verify_splice accepted " + why)

refuses(good.replace('"One Dark"', '"Solarized"'), "a value outside the owned keys")
refuses(good.replace("// keep me\n", ""), "a dropped comment")
refuses(good.replace("keep me", "keep us"), "rewritten comment text")
refuses(src, "a splice that left the owned value stale")
PY

# --- C21: the strict-JSON writer keeps 2-space indentation -------------------
python3 -c "import json,sys; json.dump({'theme':'One Dark'}, open(sys.argv[1],'w'))" "$SETTINGS"
PATH="$BASEPATH" "$TOOL" >/dev/null 2>&1 || fail 'C21: merge run failed'
grep -q '^  "theme"' "$SETTINGS" || fail 'C21: strict-JSON output is not 2-space indented'

# --- C22: main() actually CALLS the splice self-check -------------------------
# C20 proves verify_splice refuses bad input when handed it directly; that is a
# statement about the FUNCTION, not about the writer. Mutation (2026-08-27)
# showed the whole `verify_splice(source, result)` call could be DELETED with
# every case still green -- the same "checks stop at the function boundary" hole
# found in bin/oll the day before. So drive main() itself with a deliberately
# mis-targeted edit (the exact scanner bug this guard exists for: rewriting a
# favorite_models entry the tool does not own) and require that main refuses it,
# reports why, and leaves the file byte-identical.
# A COMMENTED fixture: the strict-JSON branch never reaches the splice at all.
cat > "$SETTINGS" <<'JSONC'
// Zed settings
{
  "language_models": {"ollama": {"api_url": "https://ollama.com"}},
  "agent": {
    "default_model": {"provider": "ollama", "model": "stale-model"},
    "favorite_models": [{"provider": "ollama", "model": "glm-5.2"}]
  }
}
JSONC
python3 - "$ROOT/bin/zed-setup" "$SETTINGS" <<'PY' || fail 'C22: main() does not enforce the splice self-check'
import importlib.util, io, sys
from importlib.machinery import SourceFileLoader

tool, settings = sys.argv[1], sys.argv[2]
loader = SourceFileLoader("zed_setup_under_test", tool)
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

before = open(settings, encoding="utf-8").read()
needle = "glm-5.2"
if needle not in before:
    raise AssertionError("fixture lost its non-owned favorite; case proves nothing")
start = before.index(needle)
# A span aimed at a value zed-setup does NOT own -- what a scanner bug produces.
mod.plan_edits = lambda source: ([(start, start + len(needle), "hijacked")], [])

sys.argv = ["zed-setup"]
captured, real = io.StringIO(), sys.stderr
sys.stderr = captured
try:
    rc = mod.main()
finally:
    sys.stderr = real

after = open(settings, encoding="utf-8").read()
if after != before:
    raise AssertionError("main() WROTE a mis-targeted splice to the user's file")
if rc != 2:
    raise AssertionError("main() returned %r, not 2, for a refused splice" % (rc,))
if "refusing to write" not in captured.getvalue():
    raise AssertionError("no refusal reason on stderr: %r" % captured.getvalue())
PY

# --- C23: a malformed document REFUSES (exit 2); it never crashes -------------
# The scanner indexes self.text[self.pos] and recurses per nesting level, so a
# truncated or pathological file raises IndexError/RecursionError -- neither is
# a ValueError. Catching only ValueError turned "input we cannot parse" into a
# traceback (exit 1) on the user's real config. Each case asserts BOTH the exit
# code and a meaningful reason, because an off-by-one that degrades a named
# diagnostic into a raw Python index error still exits 2.
c23() {  # <fixture-content> <expected-stderr-fragment> <label>
  printf '%s' "$1" > "$SETTINGS"
  local snapshot; snapshot="$(cat "$SETTINGS")"
  set +e
  PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c23.err"
  local rc=$?
  set -e
  [[ $rc -eq 2 ]] || fail "C23 ($3): expected exit 2, got $rc ($(cat "$TMP/c23.err"))"
  grep -qF "$2" "$TMP/c23.err" \
    || fail "C23 ($3): expected reason '$2', got: $(cat "$TMP/c23.err")"
  [[ "$(cat "$SETTINGS")" == "$snapshot" ]] || fail "C23 ($3): modified an unparseable file"
}
c23 '{"agent": {"default_model": {"model": "truncated' \
    'unterminated string' 'unterminated string'
c23 '{"a": 1' 'unexpected end of document' 'truncated after a literal'
# Nesting past Python's recursion limit: RecursionError, not ValueError. Without
# the REFUSABLE tuple this exits 1 with a traceback instead of refusing.
python3 -c "import sys; open(sys.argv[1],'w').write('['*5000)" "$SETTINGS"
set +e
PATH="$BASEPATH" "$TOOL" >/dev/null 2>"$TMP/c23d.err"
C23D_RC=$?
set -e
[[ $C23D_RC -eq 2 ]] \
  || fail "C23 (deep nesting): expected exit 2, got $C23D_RC ($(head -c 200 "$TMP/c23d.err"))"
! grep -q 'Traceback' "$TMP/c23d.err" || fail 'C23 (deep nesting): leaked a traceback'

echo 'zed-setup contract: PASS'
