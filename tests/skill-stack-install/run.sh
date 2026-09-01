#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0

ok() {
  PASS=$((PASS + 1))
  printf 'ok %d - %s\n' "$PASS" "$1"
}

fail() {
  printf 'not ok %d - %s\n' "$((PASS + 1))" "$1" >&2
  exit 1
}

# C1: the production manifest is the exact standardized stack, every external
# source is immutable, and the corrected JCarterJohnson URL is canonical.
python3 - "$ROOT/skills/external-stack.json" <<'PY' || fail "production manifest invariant"
import json, re, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
expected = {
    "anti-slop-design", "humanizer", "creator", "filler", "improver",
    "reviewer", "hallmark", "unslop-ui", "avoid-ai-design",
    "orchestration-practices",
    "cheap-delegate", "fanout", "gauntlet", "i-have-adhd",
    "ideas", "memory", "self-improve", "solplan", "wrap-up",
}
assert {item["name"] for item in data["skills"]} == expected
assert all(re.fullmatch(r"[0-9a-f]{40}", item["commit"]) for item in data["skills"])
workflow = {
    "cheap-delegate", "fanout", "gauntlet", "i-have-adhd",
    "ideas", "memory", "self-improve", "solplan", "wrap-up",
}
for item in data["skills"]:
    if item["name"] not in workflow:
        assert "targets" not in item, item
    else:
        assert item["targets"] == ["OpenCode", "Hermes"], item
# The private fleet stack (never graduated) carries exactly the client-anchored and
# Hermes-bound skills; the two manifests are disjoint.
import os
fleet_p = os.path.join(os.path.dirname(sys.argv[1]), "fleet-stack.json")
if os.path.exists(fleet_p):
    fleet = json.load(open(fleet_p, encoding="utf-8"))
    fleet_names = {item["name"] for item in fleet["skills"]}
    assert fleet_names == {"propuesta", "opportunity-to-project", "fleet-service",
                           "open-design", "plan-to-repo", "product-manager", "graduate"}, fleet_names
    assert not (fleet_names & expected)
    for item in fleet["skills"]:
        if item["name"] == "plan-to-repo":
            assert item["targets"] == ["OpenCode"], item
        elif item["name"] in {"fleet-service", "open-design", "product-manager", "graduate"}:
            assert item["targets"] == ["OpenCode", "Hermes"], item
        else:
            assert "targets" not in item, item
unslop = next(item for item in data["skills"] if item["name"] == "unslop-ui")
assert unslop["repo"] == "https://github.com/JCarterJohnson/vibecoded-design-tells.git"
PY
ok "production manifest pins the general and orchestratormaxxing workflow stacks"

# Build a tiny pinned upstream and a repo-local router fixture. No network is
# allowed in this contract; --source is the deterministic source injection.
FIXTURE_REPO="$TMP/upstream"
mkdir -p "$FIXTURE_REPO/skill" "$TMP/harness/bin" "$TMP/harness/skills/anti-slop-design"
printf '%s\n' '---' 'name: sample' 'description: fixture' '---' '# Sample' > "$FIXTURE_REPO/skill/SKILL.md"
git -C "$FIXTURE_REPO" init -q
git -C "$FIXTURE_REPO" config user.email test@example.invalid
git -C "$FIXTURE_REPO" config user.name test
git -C "$FIXTURE_REPO" add skill/SKILL.md
git -C "$FIXTURE_REPO" commit -qm fixture
FIXTURE_COMMIT="$(git -C "$FIXTURE_REPO" rev-parse HEAD)"
cp "$ROOT/bin/sync-agent-skills" "$TMP/harness/bin/sync-agent-skills"
cp "$ROOT/skills/anti-slop-design/SKILL.md" "$TMP/harness/skills/anti-slop-design/SKILL.md"

python3 - "$TMP/manifest.json" "$FIXTURE_REPO" "$FIXTURE_COMMIT" "$TMP/harness" <<'PY'
import hashlib, json, pathlib, sys
manifest, source, commit, harness = sys.argv[1:]
def digest(root):
    root = pathlib.Path(root)
    out = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        out.update(rel.encode()); out.update(b"\0")
        out.update(hashlib.sha256(path.read_bytes()).hexdigest().encode()); out.update(b"\n")
    return out.hexdigest()
data = {
    "schema_version": 1,
    "skills": [
        {"name": "anti-slop-design", "repo": "local://orchestratormaxxing", "commit": "0" * 40,
         "path": "skills/anti-slop-design", "include": ["SKILL.md"], "license": "MIT",
         "tree_sha256": digest(pathlib.Path(harness) / "skills/anti-slop-design")},
        {"name": "sample", "repo": "fixture://upstream", "commit": commit,
         "path": "skill", "include": ["SKILL.md"], "license": "MIT",
         "tree_sha256": digest(pathlib.Path(source) / "skill")},
        {"name": "open-hermes", "repo": "fixture://upstream", "commit": commit,
         "path": "skill", "include": ["SKILL.md"], "license": "MIT",
         "targets": ["OpenCode", "Hermes"],
         "tree_sha256": digest(pathlib.Path(source) / "skill")},
    ],
}
pathlib.Path(manifest).write_text(json.dumps(data), encoding="utf-8")
PY

run_sync() {
  local home="$1"
  shift
  HOME="$home" CODEX_HOME="$home/custom-codex" \
    "$TMP/harness/bin/sync-agent-skills" \
      --manifest "$TMP/manifest.json" \
      --source "fixture://upstream=$FIXTURE_REPO" "$@"
}

# C2: one invocation installs both payloads into all four native roots and
# respects CODEX_HOME instead of hard-coding ~/.codex.
HOME1="$TMP/home-four"
mkdir -p "$HOME1/.hermes" "$HOME1/.claude"
: > "$HOME1/.hermes/kanban.db"
printf 'preserve me\n' > "$HOME1/.claude/CLAUDE.md"
run_sync "$HOME1" > "$TMP/install.out"
for root in "$HOME1/.claude/skills" "$HOME1/custom-codex/skills" \
            "$HOME1/.config/opencode/skills" "$HOME1/.hermes/skills"; do
  test -f "$root/sample/SKILL.md" || fail "four-host install"
  test -f "$root/anti-slop-design/.orchestratormaxxing-source.json" || fail "four-host provenance"
done
test ! -e "$HOME1/.claude/skills/open-hermes" || fail "target filter leaked into Claude"
test ! -e "$HOME1/custom-codex/skills/open-hermes" || fail "target filter leaked into Codex"
test -f "$HOME1/.config/opencode/skills/open-hermes/SKILL.md" || fail "OpenCode target missing"
test -f "$HOME1/.hermes/skills/open-hermes/SKILL.md" || fail "Hermes target missing"
ok "one sync installs atomically to Claude, Codex, OpenCode, and Hermes"

# C3: absent production kanban.db means Hermes is untouched while the other
# three hosts still install. This prevents collisions with unrelated apps.
HOME2="$TMP/home-no-hermes"
run_sync "$HOME2" > "$TMP/no-hermes.out"
test -f "$HOME2/.claude/skills/sample/SKILL.md" || fail "three-host install"
test ! -e "$HOME2/.hermes/skills/sample" || fail "Hermes production gate"
grep -q 'Hermes skipped' "$TMP/no-hermes.out" || fail "Hermes skip report"
ok "Hermes requires its real kanban.db production marker"

# C4: an unmanaged same-name skill aborts during preflight before any sibling
# target is written; the installer never destroys user-owned skill content.
HOME3="$TMP/home-collision"
mkdir -p "$HOME3/custom-codex/skills/sample"
printf 'user owned\n' > "$HOME3/custom-codex/skills/sample/SKILL.md"
if run_sync "$HOME3" > "$TMP/collision.out" 2>&1; then
  fail "unmanaged collision refusal"
fi
grep -q 'refusing to replace unmanaged Codex skill' "$TMP/collision.out" || fail "collision diagnostic"
test ! -e "$HOME3/.claude/skills/sample" || fail "collision preflight atomicity"
grep -q 'user owned' "$HOME3/custom-codex/skills/sample/SKILL.md" || fail "collision preservation"
ok "unmanaged collisions fail closed before the first write"

# C5: a dirty or substituted upstream cannot pass merely by retaining the
# pinned Git HEAD; the selected payload hash must also match.
printf '\ndrift\n' >> "$FIXTURE_REPO/skill/SKILL.md"
HOME4="$TMP/home-drift"
if run_sync "$HOME4" > "$TMP/drift.out" 2>&1; then
  fail "payload hash gate"
fi
grep -q 'payload hash mismatch for sample' "$TMP/drift.out" || fail "hash mismatch diagnostic"
test ! -e "$HOME4/.claude/skills/sample" || fail "hash preflight atomicity"
ok "payload drift is rejected before installation"
git -C "$FIXTURE_REPO" checkout -q -- skill/SKILL.md

# C6: all four hosts receive an idempotent always-on generation rule without
# erasing existing instructions; OpenCode's coding prompt also refreshes
# existing installer-owned agents as a second native surface.
for rules in "$HOME1/.claude/CLAUDE.md" "$HOME1/custom-codex/AGENTS.md" \
             "$HOME1/.config/opencode/AGENTS.md" "$HOME1/.hermes/AGENTS.md"; do
  grep -q 'Automatic anti-slop UI generation' "$rules" || fail "global generation trigger"
  test "$(grep -c 'orchestratormaxxing:anti-slop-design:begin' "$rules")" -eq 1 || fail "idempotent doctrine marker"
done
grep -q 'preserve me' "$HOME1/.claude/CLAUDE.md" || fail "existing doctrine preservation"
run_sync "$HOME1" > "$TMP/reinstall.out"
test "$(grep -c 'orchestratormaxxing:anti-slop-design:begin' "$HOME1/.claude/CLAUDE.md")" -eq 1 || fail "doctrine reinstall idempotency"
grep -q 'PRE_ANTI_SLOP_PROMPT,' "$ROOT/install.sh" || fail "OpenCode prompt migration"
grep -q 'load the anti-slop-design skill automatically' "$ROOT/install.sh" || fail "OpenCode generation trigger"
ok "generation-time routing is wired into every host"

# C7: the deployed command lives in ~/.local/bin, outside the repository.  An
# explicit local:// source must remain authoritative after relocation; deriving
# the router source from the executable path breaks every real install.sh run.
RELOCATED="$TMP/relocated/bin"
mkdir -p "$RELOCATED"
cp "$ROOT/bin/sync-agent-skills" "$RELOCATED/sync-agent-skills"
chmod +x "$RELOCATED/sync-agent-skills"
HOME5="$TMP/home-relocated"
HOME="$HOME5" CODEX_HOME="$HOME5/custom-codex" \
  "$RELOCATED/sync-agent-skills" \
    --manifest "$TMP/manifest.json" \
    --source "local://orchestratormaxxing=$TMP/harness" \
    --source "fixture://upstream=$FIXTURE_REPO" \
    --no-hermes > "$TMP/relocated.out" \
  || fail "relocated command local source override"
test -f "$HOME5/.claude/skills/anti-slop-design/SKILL.md" \
  || fail "relocated command local payload"
ok "relocated command honors the explicit repo-local source"

printf '1..%d\n' "$PASS"
