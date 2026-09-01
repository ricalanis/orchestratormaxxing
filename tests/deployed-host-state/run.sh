#!/usr/bin/env bash
# Contract for harness-verify's deployed-state guard.
#
# The guard exists because a third-party installer wrote a compaction override into
# ~/.codex/config.toml, widened agents.max_depth to 2, and deleted the deployed
# claudemaxxing doctrine block from ~/.codex/AGENTS.md on 2026-07-24 — and the gate
# reported green throughout, because it only ever read the REPO copies.
#
# Every case below was first proven against the live machine before being frozen here;
# these run against a fixture HOME so the contract never touches the real one.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import json
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1])
mod = runpy.run_path(str(root / "bin/harness-verify"))
check = mod.get("deployed_host_state_ok")
BEGIN, END = mod["DOCTRINE_BEGIN"], mod["DOCTRINE_END"]
SENTINELS = mod["DOCTRINE_SENTINELS"]
assert callable(check), "deployed-host-state: guard missing"

failures = []

# The measured-legit source for claude-hud (frozen 2026-07-26); reused across fixtures.
HUD_SRC = {"source": {"source": "github", "repo": "jarrodwatts/claude-hud"}}


def expect(label, issues, *, severity=None, needle=None, empty=False):
    if empty:
        if issues:
            failures.append(f"{label}: expected no issues, got {issues}")
        return
    hit = [i for i in issues
           if (severity is None or i[0] == severity)
           and (needle is None or needle.lower() in i[2].lower())]
    if not hit:
        failures.append(f"{label}: expected {severity}/{needle!r}, got {issues}")


def build(tmp, *, installed=True, codex_extra="", doctrine=True, truncate=False,
          hooks=None, known_marketplaces=None, installed_plugins=None, settings_extra=None):
    home = pathlib.Path(tmp)
    if installed:
        (home / ".config" / "claudemaxxing").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        '[agents]\nmax_depth = 1\nmax_threads = 4\n' + codex_extra)
    if doctrine:
        body = "\n".join(f"- {s} placeholder" for s in SENTINELS)
        if truncate:
            body = "\n".join(f"- {s} placeholder" for s in SENTINELS[1:])
        (home / ".codex" / "AGENTS.md").write_text(f"# head\n\n{BEGIN}\n{body}\n{END}\n")
    else:
        (home / ".codex" / "AGENTS.md").write_text("# nothing here\n")
    settings = {"permissions": {"deny": []}}
    if hooks is not None:
        settings["hooks"] = hooks
    if settings_extra:
        settings.update(settings_extra)
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))

    # DEFAULT fixture registers legit entries so C2 (clean -> zero issues) proves the
    # new checks cannot fire unconditionally, not merely that they're never exercised.
    # The claude-hud entry carries its real source: since the (name, source) upgrade a
    # pinned name with a missing or foreign source warns, so a bare {} is no longer clean.
    if known_marketplaces is None:
        known_marketplaces = {"claude-hud": HUD_SRC}
    if installed_plugins is None:
        installed_plugins = {"version": 2, "plugins": {"claude-hud@claude-hud": {}}}
    (home / ".claude" / "plugins").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
        json.dumps(known_marketplaces))
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(installed_plugins))
    return str(home)


# C1 — not installed on this machine: the guard must stay silent, so a fresh clone
# or a CI checkout does not fail on a machine the harness was never deployed to.
with tempfile.TemporaryDirectory() as t:
    expect("C1 not-installed", check(str(root), build(t, installed=False)), empty=True)

# C2 — clean installed state produces nothing. Without this the guard could pass
# every other case by simply always firing.
with tempfile.TemporaryDirectory() as t:
    expect("C2 clean", check(str(root), build(t)), empty=True)

# C3 — top-level compaction pin, the shape a real installer writes.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    p = pathlib.Path(home, ".codex", "config.toml")
    p.write_text('experimental_compact_prompt_file = "/tmp/x.md"\n' + p.read_text())
    expect("C3 top-level pin", check(str(root), home), severity="error", needle="compact")

# C4 — pin nested inside a table. Caught deliberately: a red-proof that appended the
# key at EOF landed it inside [mcp_servers] and read as "not caught", which is
# indistinguishable from a broken guard. Scanning the tree removes that ambiguity.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[mcp_servers.x]\nexperimental_compact_prompt_file = "/tmp/x.md"\n')
    expect("C4 nested pin", check(str(root), home), severity="error", needle="compact")

# C5 — widened recursive fan-out in the DEPLOYED config while the repo pins 1.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".codex", "config.toml").write_text('[agents]\nmax_depth = 2\nmax_threads = 4\n')
    expect("C5 max_depth", check(str(root), home), severity="warn", needle="max_depth")

# C6 — deployed doctrine block deleted outright. This is the exact 2026-07-24 failure
# that went unnoticed for 2h40m.
with tempfile.TemporaryDirectory() as t:
    expect("C6 doctrine deleted", check(str(root), build(t, doctrine=False)),
           severity="error", needle="missing from a deployed")

# C7 — block present but truncated: markers survive, load-bearing content does not.
# Marker-presence alone would pass this, which is why sentinels are checked.
with tempfile.TemporaryDirectory() as t:
    expect("C7 doctrine truncated", check(str(root), build(t, truncate=True)),
           severity="error", needle="truncated")

# C8 — a foreign hook firing in every project.
with tempfile.TemporaryDirectory() as t:
    home = build(t, hooks={"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": "bash /elsewhere/workspace-doctor.sh --quiet"}]}]})
    expect("C8 foreign hook", check(str(root), home), severity="warn", needle="unrecognized")

# C9 — a known-owner hook must NOT warn, or the guard is noise and gets ignored.
with tempfile.TemporaryDirectory() as t:
    home = build(t, hooks={"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": '"$HOME/.local/bin/memoryctl" brief'}]}]})
    expect("C9 known hook quiet", check(str(root), home), empty=True)

# C10 — a foreign entry registered in known_marketplaces.json. This is the exact
# 2026-07-26 shape: deleting the marketplace directory doesn't deregister it here.
with tempfile.TemporaryDirectory() as t:
    home = build(t, known_marketplaces={"claude-hud": HUD_SRC, "evil-market": {"source": "https://x.example/r.git"}})
    expect("C10 foreign marketplace", check(str(root), home), severity="warn", needle="marketplace")

# C11 — a plugin installed from a marketplace not in the known list.
with tempfile.TemporaryDirectory() as t:
    home = build(t, installed_plugins={"version": 2, "plugins": {
        "claude-hud@claude-hud": {}, "x@evil-market": {}}})
    expect("C11 foreign plugin", check(str(root), home), severity="warn", needle="evil-market")

# C12 — the Codex-side equivalent: a [marketplaces.*] table in config.toml.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.evil-market]\nsource = "https://x.example/r.git"\n')
    expect("C12 codex foreign marketplace", check(str(root), home), severity="warn", needle="marketplace")

# C13 — registry files absent entirely (fresh machine): skipped, never failed.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    os.remove(os.path.join(home, ".claude", "plugins", "known_marketplaces.json"))
    os.remove(os.path.join(home, ".claude", "plugins", "installed_plugins.json"))
    expect("C13 registries absent", check(str(root), home), empty=True)

# C14 — a malformed plugin key (no @marketplace suffix) is reported, not skipped.
with tempfile.TemporaryDirectory() as t:
    home = build(t, installed_plugins={"version": 2, "plugins": {"claude-hud@claude-hud": {}, "orphan": {}}})
    expect("C14 malformed plugin key", check(str(root), home), severity="warn", needle="malformed")

# C15 — truncated JSON warns instead of crashing the guard. One sub-case per file:
# both emit the same "unparseable JSON" text, so a combined case would let either
# file's handler be deleted while the other still satisfies the needle (mut survivor).
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".claude", "plugins", "known_marketplaces.json").write_text('{"claude-hud":')
    expect("C15a unparseable known_marketplaces", check(str(root), home), severity="warn", needle="unparseable")
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".claude", "plugins", "installed_plugins.json").write_text('{"plugins":')
    expect("C15b unparseable installed_plugins", check(str(root), home), severity="warn", needle="unparseable")

# C16 — valid JSON of the WRONG TYPE must warn, never raise. The refuter reproduced
# three crashes here (list -> .get AttributeError, str -> AttributeError, int -> not
# iterable): an uncaught exception propagates out of audit(), harness-verify emits no
# JSON, and loop-tick degrades to "observer unavailable" — a 3-byte file written by
# exactly the adversary this check targets would disable the whole gate. Each file's
# warn is asserted by its own distinct text so neither handler can vanish silently.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".claude", "plugins", "known_marketplaces.json").write_text('7')
    pathlib.Path(home, ".claude", "plugins", "installed_plugins.json").write_text('[1, 2]')
    try:
        issues = check(str(root), home)
    except Exception as ex:
        failures.append(f"C16 non-dict registries: guard RAISED {type(ex).__name__}: {ex}")
    else:
        expect("C16a non-dict known_marketplaces", issues, severity="warn",
               needle="expected an object of marketplace entries")
        expect("C16b non-dict installed_plugins", issues, severity="warn",
               needle="expected an object with a 'plugins' object")

# C16c — the Codex-side type guard: a bare `marketplaces = 3` key instead of a table.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".codex", "config.toml").write_text(
        'marketplaces = 3\n[agents]\nmax_depth = 1\nmax_threads = 4\n')
    expect("C16c non-dict codex marketplaces", check(str(root), home), severity="warn",
           needle="unexpected [marketplaces] type")

# C17 — settings.json is a re-materialization source: Claude Code rebuilds the plugins/
# registries from extraKnownMarketplaces, so a foreign entry HERE survives cleaning the
# registry files themselves (refuter finding, 2026-07-26).
with tempfile.TemporaryDirectory() as t:
    home = build(t, settings_extra={"extraKnownMarketplaces": {"evil-market": {"source": {"source": "github", "repo": "x/y"}}}})
    expect("C17 settings marketplace", check(str(root), home), severity="warn", needle="evil-market")

# C18 — same surface, enabledPlugins: a plugin enabled from a foreign marketplace.
with tempfile.TemporaryDirectory() as t:
    home = build(t, settings_extra={"enabledPlugins": {"x@evil-market": True}})
    expect("C18 settings enabled plugin", check(str(root), home), severity="warn", needle="evil-market")

# C19 — legit settings registrations must stay quiet (the C9 rule for this surface:
# a check that warns on stock state is noise and gets ignored).
with tempfile.TemporaryDirectory() as t:
    home = build(t, settings_extra={
        "extraKnownMarketplaces": {"claude-hud": {"source": {"source": "github", "repo": "jarrodwatts/claude-hud"}}},
        "enabledPlugins": {"claude-hud@claude-hud": True}})
    expect("C19 legit settings quiet", check(str(root), home), empty=True)

# C20 — an installed_plugins dict WITHOUT a "plugins" key is fine, not a warn: pin the
# boundary so the type guard can't drift into rejecting harmless shapes.
with tempfile.TemporaryDirectory() as t:
    home = build(t, installed_plugins={"version": 2})
    expect("C20 plugins key absent quiet", check(str(root), home), empty=True)

# --- C21+ : (name, source) matching — lq-d45051fa. Name-only matching let a foreign
# installer PASS GREEN by reusing a known marketplace name; on the Codex side
# [marketplaces.personal] would also clobber our own registration. Every "foreign
# source under a known name" case below was proven RED against the name-only guard
# before the fix landed.

# C21 — Codex [marketplaces.personal] pointing anywhere but a claudemaxxing checkout.
# This is the clobber shape: the reserved name, a foreign payload.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.personal]\nsource_type = "local"\nsource = "/elsewhere/payload"\n')
    expect("C21 codex personal clobbered", check(str(root), home), severity="warn",
           needle="claudemaxxing checkout")

# C21b — a git-sourced 'personal' is the same clobber even if the URL looks plausible.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.personal]\nsource_type = "git"\nsource = "https://x.example/claudemaxxing.git"\n')
    expect("C21b codex personal git-sourced", check(str(root), home), severity="warn",
           needle="claudemaxxing checkout")

# C22 — a genuine local 'personal' registration stays quiet. The fixture points at THIS
# repo, whose .agents/plugins/marketplace.json declares the claudemaxxing plugin — the
# identity is content, not a literal path, so the check holds on any machine (~/dev vs
# ~/Dev) and inside loop worktrees.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra=f'\n[marketplaces.personal]\nsource_type = "local"\nsource = "{root}"\n')
    expect("C22 codex personal genuine quiet", check(str(root), home), empty=True)

# C23 — Claude side: a known name in known_marketplaces.json with a foreign source.
with tempfile.TemporaryDirectory() as t:
    home = build(t, known_marketplaces={
        "claude-hud": HUD_SRC,
        "openai-codex": {"source": {"source": "github", "repo": "evil/codex-plugin-cc"}}})
    expect("C23 known name foreign source", check(str(root), home), severity="warn",
           needle="unexpected source")

# C24 — the re-materialization surface too: extraKnownMarketplaces reusing a known
# name with a foreign source (same registration authority as C17).
with tempfile.TemporaryDirectory() as t:
    home = build(t, settings_extra={"extraKnownMarketplaces": {
        "claude-plugins-official": {"source": {"source": "github", "repo": "evil/claude-plugins-official"}}}})
    expect("C24 settings known name foreign source", check(str(root), home),
           severity="warn", needle="unexpected source")

# C25 — claude-code-warp is deliberately UNPINNED (its deployed source was never
# measured; a guessed pin would warn forever on the machine that registers it). Any
# source under that name stays quiet — this pins the boundary so the source table
# can't silently drift into guessing.
with tempfile.TemporaryDirectory() as t:
    home = build(t, known_marketplaces={
        "claude-hud": HUD_SRC,
        "claude-code-warp": {"source": {"source": "github", "repo": "whatever/warp"}}})
    expect("C25 unpinned name any source quiet", check(str(root), home), empty=True)

# C26 — a pinned name with NO source info at all: unverifiable is not clean. The
# "MISSING" needle is load-bearing: it distinguishes "no identity" from a mutant that
# stringifies half-specified sources into e.g. "github:None" (mut survivors 2026-08-14).
with tempfile.TemporaryDirectory() as t:
    home = build(t, known_marketplaces={"claude-hud": {}})
    expect("C26 pinned name missing source", check(str(root), home), severity="warn",
           needle="unexpected source MISSING")

# C26b — a half-specified source (kind but no repo) is also MISSING, not "github:None".
with tempfile.TemporaryDirectory() as t:
    home = build(t, known_marketplaces={"claude-hud": {"source": {"source": "github"}}})
    expect("C26b pinned name partial source", check(str(root), home), severity="warn",
           needle="unexpected source MISSING")

# C24b — same MISSING boundary on the extraKnownMarketplaces surface.
with tempfile.TemporaryDirectory() as t:
    home = build(t, settings_extra={"extraKnownMarketplaces": {
        "openai-codex": {"source": {"source": "github"}}}})
    expect("C24b settings partial source", check(str(root), home), severity="warn",
           needle="unexpected source MISSING")

# C24c — a foreign claude-hud in extraKnownMarketplaces while known_marketplaces holds
# the LEGIT claude-hud. Kills a real bypass mut surfaced: with the extra-loop's ident
# assignment deleted, the legit ident leaked from the registry-file loop and the foreign
# settings entry read as clean.
with tempfile.TemporaryDirectory() as t:
    home = build(t, settings_extra={"extraKnownMarketplaces": {
        "claude-hud": {"source": {"source": "github", "repo": "evil/claude-hud"}}}})
    expect("C24c settings foreign source behind legit registry", check(str(root), home),
           severity="warn", needle="unexpected source github:evil/claude-hud")

# C27c — Codex-side MISSING boundary: source_type without source.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.i-have-adhd]\nsource_type = "git"\n')
    expect("C27c codex partial source", check(str(root), home), severity="warn",
           needle="unexpected source MISSING")

# C21c — a NON-TABLE 'personal' value must warn, never crash the guard (the C16 rule:
# this file is written by exactly the adversary the check targets).
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces]\npersonal = "https://x.example/r.git"\n')
    try:
        issues = check(str(root), home)
    except Exception as ex:
        failures.append(f"C21c non-table personal: guard RAISED {type(ex).__name__}: {ex}")
    else:
        expect("C21c non-table personal", issues, severity="warn", needle="claudemaxxing checkout")

# C21d — a non-string local source (TOML allows it) must warn: the checkout check must
# fail closed on junk, not accept what it cannot read.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.personal]\nsource_type = "local"\nsource = 3\n')
    expect("C21d non-string personal source", check(str(root), home), severity="warn",
           needle="claudemaxxing checkout")

# C21e — a decoy checkout: the source dir HAS a marketplace.json but it declares a
# different plugin. Content identity means the plugin name decides, not file presence.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    decoy = pathlib.Path(t, "decoy", ".agents", "plugins")
    decoy.mkdir(parents=True)
    (decoy / "marketplace.json").write_text(json.dumps(
        {"name": "personal", "plugins": [{"name": "not-claudemaxxing"}]}))
    p = pathlib.Path(home, ".codex", "config.toml")
    p.write_text(p.read_text() + f'\n[marketplaces.personal]\nsource_type = "local"\nsource = "{pathlib.Path(t, "decoy")}"\n')
    expect("C21e decoy checkout", check(str(root), home), severity="warn",
           needle="claudemaxxing checkout")

# C27 — Codex i-have-adhd with a foreign git URL under the known name.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.i-have-adhd]\nsource_type = "git"\nsource = "https://x.example/i-have-adhd.git"\n')
    expect("C27 codex known name foreign source", check(str(root), home), severity="warn",
           needle="unexpected source")

# C27b — the measured-legit Codex i-have-adhd registration stays quiet.
with tempfile.TemporaryDirectory() as t:
    home = build(t, codex_extra='\n[marketplaces.i-have-adhd]\nsource_type = "git"\nsource = "https://github.com/ayghri/i-have-adhd.git"\nref = "main"\n')
    expect("C27b codex legit source quiet", check(str(root), home), empty=True)

# --- P5: retired tools still deployed on PATH (lq-68ae90d6) --------------------
# install.sh never prunes, so a tool deleted from the repo stays executable on every
# machine that already installed it (verified on the Mac 2026-07-18 with
# bin/context-lifecycle). These cases run the REAL git history of this checkout — the
# retired-name half physically crosses the git boundary rather than reading a fixture.
retired_fn = mod.get("retired_bin_names")
if not callable(retired_fn):
    failures.append("C28: retired_bin_names missing")
else:
    retired, measured = retired_fn(str(root))
    if not measured:
        failures.append("C28 history readable: retired_bin_names could not read git history")
    # C28 — a name this repo really did delete is reported. bin/context-lifecycle was
    # deleted deliberately (knowledge/context-lifecycle.md: "must not come back"), so its
    # absence from the retired set would mean the scan sees nothing.
    # The graduated public tree starts its own history: the retirement of bin/context-lifecycle
    # only exists in the private repo's log. Skip the history-anchored cases loudly there.
    history_has_retirement = bool(subprocess.run(
        ["git", "-C", str(root), "log", "--diff-filter=D", "--format=%h", "--", "bin/context-lifecycle"],
        capture_output=True, text=True).stdout.strip())
    if not history_has_retirement:
        print("  SKIP C28/C30/C31 — this checkout's history has no bin/context-lifecycle retirement (graduated tree)")
    elif "context-lifecycle" not in retired:
        failures.append(f"C28 known retired name: context-lifecycle missing from {sorted(retired)}")
    # C29 — a tool the repo ships TODAY is never retired, or every live bridge on PATH
    # would be reported as an orphan.
    if "oll" in retired:
        failures.append("C29 shipped name: oll reported as retired")

# C30 — an orphaned bridge on PATH warns, and names the exact file to remove.
if history_has_retirement:
  with tempfile.TemporaryDirectory() as t:
    home = build(t)
    lb = pathlib.Path(home, ".local", "bin")
    lb.mkdir(parents=True)
    (lb / "context-lifecycle").write_text("#!/bin/sh\nexit 0\n")
    expect("C30 orphan deployed", check(str(root), home), severity="warn",
           needle="no longer ships bin/context-lifecycle")

# C31 — the same fixture WITHOUT the orphan is silent. Without this, C30 passes just as
# well against a guard that warns unconditionally.
if history_has_retirement:
  with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".local", "bin").mkdir(parents=True)
    expect("C31 no orphan quiet", check(str(root), home), empty=True)

# C32 — a CURRENTLY shipped tool sitting in ~/.local/bin (the normal installed state on
# every machine) must not warn. This is the false-positive case that would make the
# guard unusable: a warn per deployed bridge, every run.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    lb = pathlib.Path(home, ".local", "bin")
    lb.mkdir(parents=True)
    for shipped in ("oll", "harness-verify", "loop-tick"):
        (lb / shipped).write_text("#!/bin/sh\nexit 0\n")
    expect("C32 shipped bridges quiet", check(str(root), home), empty=True)

# C33 — unreadable git history reports NO SIGNAL, not a clean machine. A failed
# measurement rendering as "nothing retired" is the exact artifact-as-signal failure
# knowledge/signal-vs-artifact-2026-07-19.md exists to prevent.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".local", "bin").mkdir(parents=True)
    notgit = pathlib.Path(t, "notarepo", "bin")
    notgit.mkdir(parents=True)
    expect("C33 unmeasured history", check(str(notgit.parent), home), severity="warn",
           needle="no signal")

# C34 — a SHALLOW clone must report NO SIGNAL too. This is the case a cross-family critic
# raised on 2026-08-14 and the reason the guard checks --is-shallow-repository: in a
# depth-1 clone `git log --diff-filter=D` exits 0 with EMPTY output, so "history
# truncated" and "nothing was ever deleted" are byte-identical. Without this the guard
# certifies every orphan clean on exactly the checkout shape most likely to be automated.
# Real shallow clone of this real repo — the git boundary is crossed, not simulated.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".local", "bin").mkdir(parents=True)
    shallow = pathlib.Path(t, "shallow")
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "--no-local", "file://" + str(root), str(shallow)],
        capture_output=True, text=True)
    if clone.returncode != 0:
        failures.append(f"C34 shallow clone: setup failed: {clone.stderr[-200:]}")
    else:
        is_shallow = subprocess.run(["git", "-C", str(shallow), "rev-parse",
                                     "--is-shallow-repository"],
                                    capture_output=True, text=True).stdout.strip()
        if is_shallow != "true":
            failures.append("C34 shallow clone: precondition failed — clone is not shallow")
        # Precondition that makes the case load-bearing: the naive query really is
        # indistinguishable from clean here.
        naive = subprocess.run(["git", "-C", str(shallow), "log", "--diff-filter=D",
                                "--name-only", "--format=", "--", "bin/"],
                               capture_output=True, text=True)
        if naive.returncode != 0 or naive.stdout.strip():
            failures.append("C34 shallow clone: precondition failed — naive git log was "
                            f"not a silent all-clear (rc={naive.returncode})")
        retired_shallow, measured_shallow = retired_fn(str(shallow))
        if measured_shallow or retired_shallow:
            failures.append("C34 shallow clone: reported as MEASURED "
                            f"(measured={measured_shallow}, retired={sorted(retired_shallow)})")
        expect("C34 shallow no-signal", check(str(shallow), home), severity="warn",
               needle="no signal")

# C35 — git itself unrunnable (not on PATH) is no signal, not a clean machine. Reached by
# emptying PATH, so the exception arm is executed rather than assumed.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".local", "bin").mkdir(parents=True)
    (pathlib.Path(home, ".local", "bin") / "context-lifecycle").write_text("#!/bin/sh\n")
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(pathlib.Path(t, "nothing-here"))
    try:
        retired_nogit, measured_nogit = retired_fn(str(root))
        issues_nogit = check(str(root), home)
    finally:
        os.environ["PATH"] = saved_path
    if measured_nogit or retired_nogit:
        failures.append(f"C35 no git: reported as MEASURED (measured={measured_nogit})")
    expect("C35 no git no-signal", issues_nogit, severity="warn", needle="no signal")
    # And it must NOT have claimed the orphan it could not actually measure.
    expect("C35 no git silent on orphan", [i for i in issues_nogit
                                           if "context-lifecycle" in i[2]], empty=True)

# C36 — a git repo with NO commits: rev-parse succeeds, `git log` exits non-zero. The
# second query's failure must also read as no signal.
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".local", "bin").mkdir(parents=True)
    empty_repo = pathlib.Path(t, "empty-repo")
    (empty_repo / "bin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(empty_repo)], check=True,
                   capture_output=True)
    retired_empty, measured_empty = retired_fn(str(empty_repo))
    if measured_empty:
        failures.append("C36 commitless repo: reported as MEASURED")
    expect("C36 commitless no-signal", check(str(empty_repo), home), severity="warn",
           needle="no signal")

# C37 — only TOP-LEVEL bin/ names are retired bridges. install.sh copies bin/<name> to
# $BIN_DST and nothing nested, so a deleted bin/sub/tool was never on PATH and must not
# be reported. Built as a real repo with a real deletion.
with tempfile.TemporaryDirectory() as t:
    nested = pathlib.Path(t, "nested-repo")
    (nested / "bin" / "sub").mkdir(parents=True)
    (nested / "bin" / "sub" / "helper").write_text("#!/bin/sh\n")
    (nested / "bin" / "toptool").write_text("#!/bin/sh\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    def git(*a):
        subprocess.run(["git", "-C", str(nested), *a], check=True, capture_output=True,
                       env=env)
    git("init", "-q")
    git("add", "-A"); git("commit", "-qm", "add")
    (nested / "bin" / "sub" / "helper").unlink()
    (nested / "bin" / "toptool").unlink()
    git("add", "-A"); git("commit", "-qm", "delete both")
    retired_nested, measured_nested = retired_fn(str(nested))
    if not measured_nested:
        failures.append("C37 nested: fixture repo unreadable")
    if "toptool" not in retired_nested:
        failures.append(f"C37 nested: top-level deletion missed ({sorted(retired_nested)})")
    if any("/" in n for n in retired_nested):
        failures.append(f"C37 nested: nested path leaked into the retired set "
                        f"({sorted(retired_nested)})")

# C38 — a root with no bin/ directory must not RAISE. An uncaught exception here
# propagates out of audit(), harness-verify emits no JSON, and loop-tick degrades to
# "observer unavailable" — a missing directory would silently disable the whole gate
# (the same failure mode the P4 type-guards exist for).
with tempfile.TemporaryDirectory() as t:
    home = build(t)
    pathlib.Path(home, ".local", "bin").mkdir(parents=True)
    binless = pathlib.Path(t, "binless-repo")
    binless.mkdir()
    subprocess.run(["git", "init", "-q", str(binless)], check=True, capture_output=True)
    (binless / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(binless), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(binless), "commit", "-qm", "init"], check=True,
                   capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    try:
        check(str(binless), home)
    except Exception as ex:
        failures.append(f"C38 binless root: guard RAISED {type(ex).__name__}: {ex}")

# C39 — a RENAMED tool strands its old name on PATH, and git hides it by default. With
# rename detection on (git's default), `git mv bin/toptool bin/renamed` is reported as R
# and --diff-filter=D prints nothing at all — measured 2026-08-14 after a cross-family
# critic raised it. Real repo, real `git mv`: the case fails against the query without
# --no-renames, which is the whole reason that flag is there.
with tempfile.TemporaryDirectory() as t:
    moved = pathlib.Path(t, "renamed-repo")
    (moved / "bin").mkdir(parents=True)
    # Rename detection is content-similarity based, so the file needs enough body to be
    # recognized as the same file — a 1-line stub would be reported as delete+add anyway
    # and the case would pass for the wrong reason.
    (moved / "bin" / "toptool").write_text("#!/bin/sh\n" + "".join(
        f"echo line {i}\n" for i in range(80)))
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    def rgit(*a):
        subprocess.run(["git", "-C", str(moved), *a], check=True, capture_output=True,
                       env=env)
    rgit("init", "-q")
    rgit("add", "-A"); rgit("commit", "-qm", "add")
    rgit("mv", "bin/toptool", "bin/renamed")
    rgit("commit", "-qm", "rename")
    # Precondition: git really does hide it without --no-renames.
    naive = subprocess.run(["git", "-C", str(moved), "log", "--diff-filter=D",
                            "--name-only", "--format=", "--", "bin/"],
                           capture_output=True, text=True, env=env)
    if naive.stdout.strip():
        failures.append("C39 rename: precondition failed — the naive query already "
                        f"reported {naive.stdout.strip()!r}, so this case proves nothing")
    retired_moved, measured_moved = retired_fn(str(moved))
    if not measured_moved:
        failures.append("C39 rename: fixture repo unreadable")
    if "toptool" not in retired_moved:
        failures.append(f"C39 rename: the stranded old name is invisible "
                        f"({sorted(retired_moved)})")

if failures:
    for f in failures:
        print(f"deployed-host-state: FAIL {f}", file=sys.stderr)
    sys.exit(1)
PY

printf 'deployed-host-state contract: PASS\n'
