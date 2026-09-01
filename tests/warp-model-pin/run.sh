#!/usr/bin/env bash
# Contract for bin/warp-model-pin.
#
# The mechanism under test is settings.toml, NOT user_preferences.json. Warp
# re-fetches the prefs catalog from its servers on every start and re-seeds
# default_id there, so a pin written to prefs is clobbered seconds later (measured
# 2026-08-24: prefs rewritten at launch, settings.toml untouched since Aug 13).
# settings.toml is user-owned and survives updates — that is why the tool targets it.
#
# Deterministic — fixture HOME, synthetic sqlite, no network, no real Warp.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/warp-model-pin"
fail() { printf 'warp-model-pin contract: %s\n' "$*" >&2; exit 1; }
[ -x "$TOOL" ] || fail "bin/warp-model-pin missing or not executable"

SCRATCH="$(mktemp -d)"; trap 'rm -rf "$SCRATCH"' EXIT
# Fixtures use the Linux layout on every OS (hermetic); C11 exercises the Darwin layout explicitly.
export WARP_MODEL_PIN_LAYOUT=linux
UUID="5f2c1a7e-0b3d-4c88-9a1e-6d4f2b8c0e11"
STALE="6095c7ee-9a9c-4892-b10f-d6f32cdf3bbc"

make_home() {   # $1 home · $2 seeded base_model · $3 history model_ids
  local home="$1" seed="$2" history="$3"
  mkdir -p "$home/.config/warp-terminal" "$home/.local/state/warp-terminal" "$home/.config/claudemaxxing"
  # Includes the multi-line inline table with a trailing comma that Warp really
  # writes and stdlib tomllib rejects (see C10).
  cat > "$home/.config/warp-terminal/settings.toml" <<TOML
[terminal]
osc52_clipboard_access = "write_only"

[agents.execution_profiles]

[agents.execution_profiles.default]
apply_code_diffs = "agent_decides"
base_model = "$seed"
name = "Default"

[privacy]
custom_secret_regex_list = [
  { name = "IPv4", pattern = 'a\b' },
  {
    name = "IPv6",
    pattern = 'b\b',
  },
]

[account]
is_settings_sync_enabled = true
TOML
  chmod 600 "$home/.config/warp-terminal/settings.toml"
  python3 - "$home" "$history" <<'MKDB'
import os, sqlite3, sys, json
home, history = sys.argv[1], sys.argv[2].split()
llms = {"agent_mode": {"default_id": "auto-genius", "choices": [
    {"id": "auto"}, {"id": "auto-genius"}, {"id": "glm-5.2-fireworks"}]}}
p = os.path.join(home, ".config/warp-terminal/user_preferences.json")
with open(p, "w") as f:
    json.dump({"prefs": {"AvailableLLMs": json.dumps(llms, separators=(",", ":"))}}, f)
db = os.path.join(home, ".local/state/warp-terminal/warp.sqlite")
con = sqlite3.connect(db)
con.execute("create table ai_queries (id integer primary key, start_ts text, conversation_id text, model_id text)")
con.execute("create table agent_conversations (conversation_id text, conversation_data text)")
# history entries are "uuid:modelname" (modelname optional) so a fixture can bind a
# Warp id to a harness model exactly the way Warp's own two tables do.
for i, spec in enumerate(history):
    uuid, _, name = spec.partition("=")
    cid = f"conv{i}"
    con.execute("insert into ai_queries (start_ts, conversation_id, model_id) values (?,?,?)",
                (f"2026-08-{10+i:02d}T00:00:00Z", cid, uuid))
    if name:
        con.execute("insert into agent_conversations (conversation_id, conversation_data) values (?,?)",
                    (cid, json.dumps({"blocks": [{"model_id": name}]})))
con.commit(); con.close()
MKDB
}

base_model_of() { grep -E '^base_model' "$1/.config/warp-terminal/settings.toml" | sed 's/.*"\(.*\)".*/\1/'; }
sumf() { md5sum < "$1/.config/warp-terminal/settings.toml"; }
run() { local h="$1"; shift; env HOME="$h" WARP_MODEL_PIN_FAKE_RUNNING=0 "$TOOL" "$@"; }

# C1: pins base_model; every other byte of settings.toml survives
H="$SCRATCH/c1"; make_home "$H" "$STALE" "auto-genius $UUID=deepseek-v4-flash:0731 auto"
run "$H" >/dev/null 2>&1 || fail "C1: pin failed"
[ "$(base_model_of "$H")" = "$UUID" ] || fail "C1: base_model not pinned"
grep -q "osc52_clipboard_access" "$H/.config/warp-terminal/settings.toml" || fail "C1: lost unrelated settings"
grep -q "is_settings_sync_enabled" "$H/.config/warp-terminal/settings.toml" || fail "C1: lost trailing section"
grep -q "pattern = " "$H/.config/warp-terminal/settings.toml" || fail "C1: mangled the secret regex table"
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }   # GNU, then BSD
[ "$(mode_of "$H/.config/warp-terminal/settings.toml")" = "600" ] || fail "C1: mode not preserved"

# C2: refuse while Warp runs (it rewrites settings from memory on exit)
H="$SCRATCH/c2"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"; before="$(sumf "$H")"
set +e; env HOME="$H" WARP_MODEL_PIN_FAKE_RUNNING=1 "$TOOL" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 3 ] || fail "C2: expected exit 3 while Warp runs, got $rc"
[ "$(sumf "$H")" = "$before" ] || fail "C2: wrote while Warp was running"

# C3: no profile declares base_model -> refuse, never invent one
H="$SCRATCH/c3"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"
grep -v '^base_model' "$H/.config/warp-terminal/settings.toml" > "$H/t"; mv "$H/t" "$H/.config/warp-terminal/settings.toml"
before="$(sumf "$H")"
set +e; run "$H" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] || fail "C3: expected exit 2 with no base_model key, got $rc"
[ "$(sumf "$H")" = "$before" ] || fail "C3: invented a base_model line"

# C4: the harness DEFAULT_MODEL has no Warp id yet -> refuse with instructions.
# Warp mints an id for an endpoint model only when it is selected, so this is the
# normal bootstrap state, not an error — and it must never silently land on a
# credit-billed catalog model instead.
H="$SCRATCH/c4"; make_home "$H" "$STALE" "auto auto-genius=glm-5.2 glm-5.2-fireworks"
set +e; run "$H" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] || fail "C4: expected exit 2 when nothing is discoverable, got $rc"
[ "$(base_model_of "$H")" = "$STALE" ] || fail "C4: pinned a credit-billed fallback"

# C5: resolves the id BOUND TO THE POLICY DEFAULT — not merely the most recent.
# Aligning Warp with Zed is the point; "last used" would silently keep whatever the
# picker happened to be on.
H="$SCRATCH/c5"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731 later-id=glm-5.2 auto"
[ "$(run "$H" --resolve)" = "$UUID" ] || fail "C5: did not resolve the policy default's id"
# a router conversation that merely mentions a harness model must not bind
H="$SCRATCH/c5b"; make_home "$H" "$STALE" "auto-genius=deepseek-v4-flash:0731"
set +e; run "$H" --resolve >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] || fail "C5: bound a router id (auto-genius) as an endpoint model"

# C6: --check reports drift without writing; clean once pinned
H="$SCRATCH/c6"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"; before="$(sumf "$H")"
set +e; run "$H" --check >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 1 ] || fail "C6: --check should exit 1 on drift, got $rc"
[ "$(sumf "$H")" = "$before" ] || fail "C6: --check wrote"
run "$H" >/dev/null 2>&1
set +e; run "$H" --check >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] || fail "C6: --check should exit 0 once pinned, got $rc"

# C7: explicit config override outranks discovery
H="$SCRATCH/c7"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"
printf '# pin\nfrom-conf-id\n' > "$H/.config/claudemaxxing/warp-model.conf"
[ "$(run "$H" --resolve)" = "from-conf-id" ] || fail "C7: config override ignored"

# C8: idempotent
H="$SCRATCH/c8"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"
run "$H" >/dev/null 2>&1; a="$(sumf "$H")"; run "$H" >/dev/null 2>&1
[ "$(sumf "$H")" = "$a" ] || fail "C8: second run rewrote the file"

# C9: --launch always reaches the terminal, and still pins
H="$SCRATCH/c9"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"
FAKEBIN="$SCRATCH/c9bin"; mkdir -p "$FAKEBIN"
printf '#!/bin/sh\necho "launched:$*" > "$LAUNCH_PROOF"\n' > "$FAKEBIN/warp-terminal"; chmod +x "$FAKEBIN/warp-terminal"
LP="$SCRATCH/c9.proof"; rm -f "$LP"
# The pin now happens AFTER startup (Warp erases a pre-launch write), so the child
# is told Warp is already up and given near-zero waits.
env HOME="$H" PATH="$FAKEBIN:$PATH" LAUNCH_PROOF="$LP" WARP_MODEL_PIN_FAKE_RUNNING=1 \
    WARP_MODEL_PIN_WAIT_UP=1 WARP_MODEL_PIN_SETTLE=0 WARP_MODEL_PIN_WINDOW=6 WARP_MODEL_PIN_INTERVAL=1 \
    "$TOOL" --launch --url >/dev/null 2>&1
[ -f "$LP" ] || fail "C9: --launch did not exec warp-terminal"
grep -q -- "--url" "$LP" || fail "C9: --launch dropped passthrough args"
for _ in 1 2 3 4 5 6 7 8; do [ "$(base_model_of "$H")" = "$UUID" ] && break; sleep 1; done
[ "$(base_model_of "$H")" = "$UUID" ] || fail "C9: --launch did not pin after startup"
H="$SCRATCH/c9b"; make_home "$H" "$STALE" "$UUID=deepseek-v4-flash:0731"
printf 'this is [not valid toml' > "$H/.config/warp-terminal/settings.toml"
LP="$SCRATCH/c9b.proof"; rm -f "$LP"
env HOME="$H" PATH="$FAKEBIN:$PATH" LAUNCH_PROOF="$LP" WARP_MODEL_PIN_FAKE_RUNNING=1 \
    WARP_MODEL_PIN_WAIT_UP=1 WARP_MODEL_PIN_SETTLE=0 WARP_MODEL_PIN_WINDOW=2 WARP_MODEL_PIN_INTERVAL=1 \
    "$TOOL" --launch >/dev/null 2>&1
[ -f "$LP" ] || fail "C9: a refusal blocked the launch"

# C10: Warp emits TOML that stdlib tomllib REJECTS (multi-line inline table with a
# trailing comma). A tool validating with a stricter parser than the producer would
# refuse every real config — this pins that the fixture keeps reproducing that shape,
# so the "no tomllib round-trip" decision stays tested rather than assumed.
python3 - "$SCRATCH/c1/.config/warp-terminal/settings.toml" <<'TOMLCHK' | grep REJECTS >/dev/null \
  || fail "C10: fixture stopped reproducing Warp's tomllib-hostile TOML — guard untested"
import sys, tomllib
try:
    tomllib.loads(open(sys.argv[1]).read()); print("PARSES")
except Exception:
    print("REJECTS")
TOMLCHK


# C11: macOS layout — ~/.warp/settings.toml + the Group-Container warp.sqlite, no prefs file
# (the catalog is then empty and discovery leans on the database join). Same pin outcome.
H="$SCRATCH/c11"; make_home "$H" "$STALE" "auto-genius $UUID=deepseek-v4-flash:0731 auto"
GC="$H/Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Stable"
mkdir -p "$H/.warp" "$GC"
mv "$H/.config/warp-terminal/settings.toml" "$H/.warp/settings.toml"
mv "$H/.local/state/warp-terminal/warp.sqlite" "$GC/warp.sqlite"
rm -f "$H/.config/warp-terminal/user_preferences.json"
env HOME="$H" WARP_MODEL_PIN_LAYOUT=darwin WARP_MODEL_PIN_FAKE_RUNNING=0 "$TOOL" >/dev/null 2>&1 || fail "C11: darwin-layout pin failed"
[ "$(grep -E '^base_model' "$H/.warp/settings.toml" | sed 's/.*"\(.*\)".*/\1/')" = "$UUID" ] || fail "C11: darwin-layout base_model not pinned"
[ ! -e "$H/.config/warp-terminal/settings.toml" ] || fail "C11: darwin layout must not touch the Linux path"
printf 'warp-model-pin contract: PASS (C1-C11)\n'
