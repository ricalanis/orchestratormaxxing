#!/usr/bin/env bash
# Hermetic contract for bin/core-export: a real private git repo, a real local
# bare "public" remote, a fake gitleaks on PATH. No network, no real remotes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="${CORE_EXPORT_TOOL_UNDER_TEST:-$ROOT/bin/core-export}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/core-export.XXXXXX")"
cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT
fail() { printf 'core-export contract: %s\n' "$*" >&2; exit 1; }
pass() { printf '  ok  %s\n' "$*"; }
[[ -x "$TOOL" ]] || fail "bin/core-export missing"

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
FAKE_BIN="$SCRATCH/fakebin"; mkdir -p "$FAKE_BIN"
# fake gitleaks: exit code + findings controlled by env; records that it ran and on what.
cat > "$FAKE_BIN/gitleaks" <<'PY'
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_GITLEAKS_LOG"], "a").write(json.dumps(args) + "\n")
rc = int(os.environ.get("FAKE_GITLEAKS_RC", "0"))
if "--report-path" in args:
    rp = args[args.index("--report-path") + 1]
    tree = args[1]
    findings = [] if rc == 0 else [{"File": os.path.join(tree, "bin", "tool"), "StartLine": 1, "RuleID": "fake-rule"}]
    open(rp, "w").write(json.dumps(findings))
sys.exit(rc)
PY
chmod +x "$FAKE_BIN/gitleaks"
export FAKE_GITLEAKS_LOG="$SCRATCH/gitleaks.log"; : > "$FAKE_GITLEAKS_LOG"
export FAKE_GITLEAKS_RC=0
PATH_WITH="$FAKE_BIN:$PATH"
# a PATH with no gitleaks anywhere (the developer box may carry a real one)
PATH_WITHOUT=""; while IFS= read -r d; do [[ -n "$d" && "$d" != "$FAKE_BIN" && ! -x "$d/gitleaks" ]] && PATH_WITHOUT="${PATH_WITHOUT:+$PATH_WITHOUT:}$d"; done < <(printf '%s' "$PATH" | tr ':' '\n')
unset CORE_EXPORT_GITLEAKS

# ── private repo fixture ─────────────────────────────────────────────────────
SRC="$SCRATCH/private"; mkdir -p "$SRC/bin" "$SRC/docs" "$SRC/bad" "$SRC/deploy" "$SRC/.results" "$SRC/shell/nested/deep"
cd "$SRC" && git init -q -b main .
printf '#!/bin/sh\necho core\n' > bin/tool; chmod +x bin/tool
printf '#!/bin/sh\nssh ricardo@fleet-server\n' > bin/fleet-tool; chmod +x bin/fleet-tool
printf 'SQLite format 3\0junk' > orchestrator.db
# (markers are assembled with printf so this contract file never contains a marker LINE itself)
printf '# doctrine\ncore line 1\n<!-- tenant:%s -->\noperator secret hostname fleet-server and chat 424242\n<!-- tenant:%s -->\ncore line 2\n' begin end > CLAUDE.md
printf 'personal notes about fleet-server\n' > docs/notes.md
printf 'leak: fleet-server\n' > bad/secret.md
ln -s CLAUDE.md AGENTS.md
printf 'TOKEN=never\n' > .env
printf 'deep core\n' > shell/nested/deep/file.txt
mkdir -p "plugins/acmelab/skills/hello" skills
printf -- '---\nname: hello\n---\nUse $acmelab:hello on the ACMELAB fleet (Acmelab docs).\n' > plugins/acmelab/skills/hello/SKILL.md
python3 - <<'PYH'
import hashlib, json, os
base = "plugins/acmelab/skills/hello"
d = hashlib.sha256()
for f in sorted(os.listdir(base)):
    p = os.path.join(base, f)
    d.update(f.encode()); d.update(b"\0"); d.update(hashlib.sha256(open(p,"rb").read()).hexdigest().encode()); d.update(b"\n")
json.dump({"schema_version": 1, "skills": [{"name": "hello", "repo": "local://acmelab", "commit": "0"*40,
          "path": "plugins/acmelab/skills/hello", "include": ["SKILL.md"], "license": "MIT",
          "tree_sha256": d.hexdigest()}]}, open("skills/external-stack.json", "w"), indent=2)
PYH
printf '[allowlist]\n' > .gitleaks.toml
printf '.env\n.results/\n' > .gitignore
cat > deploy/graduation.manifest <<'MF'
include bin
include shell
include plugins
include skills
rename acmelab publiclab
include CLAUDE.md
include AGENTS.md
exclude bin/fleet-tool
exclude *.db
strip *.md
block fleet-server
block 424242
branch main
gitleaks-config .gitleaks.toml
MF
git add -A && git commit -qm "fixture" && SRC_SHA="$(git rev-parse HEAD)"

OUT="$SCRATCH/out"

# ── C1: allowlist + strip + symlink + manifest ───────────────────────────────
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --json > "$SCRATCH/c1.json" || fail "C1 export failed"
[[ -x "$OUT/bin/tool" ]] || fail "C1 core tool missing or not executable"
[[ ! -e "$OUT/bin/fleet-tool" ]] || fail "C1 excluded fleet tool exported"
[[ ! -e "$OUT/orchestrator.db" ]] || fail "C1 excluded *.db exported"
[[ ! -e "$OUT/docs" ]] || fail "C1 non-included dir exported"
[[ ! -e "$OUT/.env" ]] || fail "C1 ignored .env exported"
[[ -L "$OUT/AGENTS.md" && "$(readlink "$OUT/AGENTS.md")" == "CLAUDE.md" ]] || fail "C1 symlink not preserved"
grep -q "core line 1" "$OUT/CLAUDE.md" && grep -q "core line 2" "$OUT/CLAUDE.md" || fail "C1 core lines lost"
! grep -q "tenant\|fleet-server\|424242" "$OUT/CLAUDE.md" || fail "C1 tenant block survived strip"
python3 - "$OUT/EXPORT-MANIFEST.json" "$SRC_SHA" <<'PY' || fail "C1 manifest wrong"
import json, sys, hashlib, os
m = json.load(open(sys.argv[1])); assert m["source_commit"] == sys.argv[2], m["source_commit"]
paths = sorted(e["path"] for e in m["files"])
assert paths == ["AGENTS.md", "CLAUDE.md", "bin/tool", "plugins/publiclab/skills/hello/SKILL.md", "shell/nested/deep/file.txt", "skills/external-stack.json"], paths
assert m["stripped"] == {"CLAUDE.md": 1}, m["stripped"]
assert m["gate"]["literal_hits"] == 0 and m["gate"]["gitleaks"] == "clean", m["gate"]
tree = os.path.dirname(sys.argv[1])
for e in m["files"]:
    if e["mode"] == "symlink": continue
    assert hashlib.sha256(open(os.path.join(tree, e["path"]), "rb").read()).hexdigest() == e["sha256"], e
PY
pass "C1 allowlist, exclusions, strip, symlink, manifest"

# ── C2: committed objects only ───────────────────────────────────────────────
printf '#!/bin/sh\necho DIRTY fleet-server\n' > "$SRC/bin/tool"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" >/dev/null 2>&1 || fail "C2 export with dirty tree failed"
grep -q "echo core" "$OUT/bin/tool" && ! grep -q DIRTY "$OUT/bin/tool" || fail "C2 working-tree edit leaked into export"
git -C "$SRC" checkout -q -- bin/tool
pass "C2 exports committed content, never the working tree"

# ── C3: literal gate blocks and leaves the previous export intact ────────────
cp "$SRC/deploy/graduation.manifest" "$SCRATCH/m3"; printf 'include bad\n' >> "$SCRATCH/m3"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --manifest "$SCRATCH/m3" --out "$OUT" 2> "$SCRATCH/c3.err"; rc=$?; set -e
[[ $rc -eq 3 ]] || fail "C3 expected exit 3, got $rc: $(cat "$SCRATCH/c3.err")"
grep -q "bad/secret.md:1" "$SCRATCH/c3.err" || fail "C3 hit not named: $(cat "$SCRATCH/c3.err")"
[[ ! -e "$OUT/bad" && -x "$OUT/bin/tool" ]] || fail "C3 blocked export touched the output tree"
[[ ! -e "$OUT.staging" ]] || fail "C3 staging dir left behind"
pass "C3 blocked literal → exit 3, output untouched"

# ── C4: unbalanced tenant marker → exit 2 ────────────────────────────────────
printf '\n<!-- tenant:%s -->\nno end\n' begin >> "$SRC/CLAUDE.md"; git -C "$SRC" commit -qam "unbalanced"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C4 expected exit 2 for unbalanced marker, got $rc"
git -C "$SRC" reset -q --hard "$SRC_SHA"
pass "C4 unbalanced marker refused"

# ── C5: marker in a non-stripped file is a manifest bug → exit 2 ─────────────
cp "$SRC/deploy/graduation.manifest" "$SCRATCH/m5"; sed -i.bak '/^strip /d' "$SCRATCH/m5"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --manifest "$SCRATCH/m5" --out "$OUT" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C5 expected exit 2 when markers are present but not stripped, got $rc"
pass "C5 unstripped tenant markers refused"

# ── C6: push to a local bare remote; idempotent; mirrors deletions ──────────
PUB="$SCRATCH/public.git"; git init -q --bare -b main "$PUB"
REPO_DIR="$SCRATCH/pubcheckout"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" --json > "$SCRATCH/c6.json" 2>/dev/null || fail "C6 push failed"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["push"]["pushed"] is True, d' "$SCRATCH/c6.json" || fail "C6 summary"
CLONE="$SCRATCH/verify1"; git clone -q "$PUB" "$CLONE"
[[ -x "$CLONE/bin/tool" && -f "$CLONE/shell/nested/deep/file.txt" && -f "$CLONE/EXPORT-MANIFEST.json" && ! -e "$CLONE/bin/fleet-tool" && ! -e "$CLONE/orchestrator.db" ]] || fail "C6 public tree wrong"
[[ "$(git -C "$CLONE" rev-list --count HEAD)" == "1" ]] || fail "C6 expected 1 public commit"
grep -q "fleet-server\|424242" -r "$CLONE" --exclude-dir=.git && fail "C6 tenant literal reached the public repo"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" --json > "$SCRATCH/c6b.json" 2>/dev/null || fail "C6 second push failed"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["push"]["pushed"] is False and d["push"]["reason"]=="no changes", d' "$SCRATCH/c6b.json" || fail "C6 not idempotent"
# delete a core file upstream → mirrored away downstream
git -C "$SRC" rm -q bin/tool && git -C "$SRC" commit -qm "drop tool"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" >/dev/null 2>&1 || fail "C6 third push failed"
CLONE2="$SCRATCH/verify2"; git clone -q "$PUB" "$CLONE2"
[[ ! -e "$CLONE2/bin/tool" && "$(git -C "$CLONE2" rev-list --count HEAD)" == "2" ]] || fail "C6 deletion not mirrored"
git -C "$SRC" reset -q --hard "$SRC_SHA"
pass "C6 push, idempotent re-push, mirrored deletion"

# ── C7: gitleaks fail-closed on push ─────────────────────────────────────────
BEFORE="$(git -C "$PUB" rev-parse main)"
set +e; PATH="$PATH_WITHOUT" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 4 ]] || fail "C7 expected exit 4 without gitleaks on push, got $rc"
set +e; FAKE_GITLEAKS_RC=1 PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" 2> "$SCRATCH/c7.err"; rc=$?; set -e
[[ $rc -eq 3 ]] || fail "C7 expected exit 3 on gitleaks findings, got $rc"
grep -q "GITLEAKS bin/tool:1: fake-rule" "$SCRATCH/c7.err" || fail "C7 finding not reported: $(cat "$SCRATCH/c7.err")"
[[ "$(git -C "$PUB" rev-parse main)" == "$BEFORE" ]] || fail "C7 remote changed despite a red gate"
# gitleaks ran on the STRIPPED tree, not the source repo
python3 - "$FAKE_GITLEAKS_LOG" "$SRC" <<'PY' || fail "C7 gitleaks scanned the wrong tree"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
assert rows, "gitleaks never ran"
for r in rows:
    assert r[0] == "dir" and not r[1].startswith(sys.argv[2] + "/bin") and sys.argv[2] != r[1], r
    import os
    assert "--config" in r and os.path.realpath(r[r.index("--config") + 1]) == os.path.realpath(sys.argv[2] + "/.gitleaks.toml"), r
PY
pass "C7 gitleaks required for push; findings block; scans the export tree"

# ── C8: --check writes nothing; manifest without block refuses to push ───────
rm -rf "$OUT"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --check >/dev/null 2>&1 || fail "C8 check failed"
[[ ! -e "$OUT" ]] || fail "C8 --check wrote an output tree"
cp "$SRC/deploy/graduation.manifest" "$SCRATCH/m8"; sed -i.bak '/^block /d' "$SCRATCH/m8"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --manifest "$SCRATCH/m8" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C8 expected exit 2 pushing with an ungated manifest, got $rc"
pass "C8 --check is read-only; ungated push refused"

# ── C9: the manifest (it names the blocklist) can never be exported ─────────
cp "$SRC/deploy/graduation.manifest" "$SCRATCH/m9"; printf 'include deploy/graduation.manifest\n' >> "$SCRATCH/m9"
cp "$SCRATCH/m9" "$SRC/deploy/graduation.manifest"; git -C "$SRC" commit -qam "manifest includes itself"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" 2> "$SCRATCH/c9.err"; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C9 expected exit 2 when the manifest selects itself, got $rc"
grep -q "refusing to export the manifest itself" "$SCRATCH/c9.err" || fail "C9 wrong reason: $(cat "$SCRATCH/c9.err")"
git -C "$SRC" reset -q --hard "$SRC_SHA"
# swept in by a directory include → dropped silently, export succeeds without it
cp "$SRC/deploy/graduation.manifest" "$SCRATCH/m9b"; printf 'include deploy\n' >> "$SCRATCH/m9b"
cp "$SCRATCH/m9b" "$SRC/deploy/graduation.manifest"; git -C "$SRC" commit -qam "manifest dir include"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" >/dev/null 2>&1 || fail "C9 directory include covering the manifest must still export"
[[ ! -e "$OUT/deploy/graduation.manifest" ]] || fail "C9 manifest shipped via directory include"
git -C "$SRC" reset -q --hard "$SRC_SHA"
pass "C9 manifest never ships: explicit include refused, directory include dropped"

# ── C10: usage errors are exit 1/2, never a silent partial export ────────────
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --tree-ish no-such-ref --out "$OUT" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 1 ]] || fail "C10 expected exit 1 for an unknown tree-ish, got $rc"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C10 expected exit 2 for --push without a remote, got $rc"
pass "C10 unknown tree-ish → 1, push without remote → 2"

# ── C11: default public checkout is the sibling project ../orchestratormaxxing ─
rm -rf "$SCRATCH/orchestratormaxxing"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" >/dev/null 2>&1 || fail "C11 default-dir push failed"
[[ -d "$SCRATCH/orchestratormaxxing/.git" && -x "$SCRATCH/orchestratormaxxing/bin/tool" ]] || fail "C11 sibling checkout not created at <repo>/../orchestratormaxxing"
[[ "$(git -C "$SCRATCH/orchestratormaxxing" remote get-url origin)" == "$PUB" ]] || fail "C11 sibling remote wrong"
pass "C11 sibling ../orchestratormaxxing checkout by default"

# ── C12: --worktree sees uncommitted edits (check-only) and can never push ───
printf '#!/bin/sh\necho WORKTREE-EDIT\n' > "$SRC/bin/tool"; printf 'untracked core\n' > "$SRC/bin/newtool"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --worktree --out "$OUT" --json > "$SCRATCH/c12.json" 2>/dev/null || fail "C12 worktree export failed"
grep -q WORKTREE-EDIT "$OUT/bin/tool" || fail "C12 working-tree edit not exported"
[[ -f "$OUT/bin/newtool" ]] || fail "C12 untracked file under an included dir not exported"
[[ ! -e "$OUT/.env" ]] || fail "C12 gitignored .env leaked through --worktree"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["source_commit"].startswith("worktree:"), d' "$SCRATCH/c12.json" || fail "C12 manifest must mark a worktree export"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --worktree --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C12 --worktree --push must be refused (exit 2), got $rc"
git -C "$SRC" checkout -q -- bin/tool; rm -f "$SRC/bin/newtool"
# and the plain (HEAD) export still ignores the working tree
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" >/dev/null 2>&1 || fail "C12 HEAD export failed"
grep -q "echo core" "$OUT/bin/tool" || fail "C12 HEAD export polluted"
pass "C12 --worktree exports live edits, honours .gitignore, never pushes"

# ── C13: --pr publishes a rolling tool-owned branch with ONE open PR (fake gh) ────
cat > "$FAKE_BIN/gh" <<'PY'
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_GH_LOG"], "a").write(json.dumps(args) + "\n")
if args[:2] == ["pr", "list"]:
    n = os.environ.get("FAKE_GH_OPEN_PR")
    print(json.dumps([{"number": int(n), "url": f"https://example.invalid/pull/{n}"}] if n else []))
elif args[:2] == ["pr", "create"]:
    print("https://example.invalid/pull/99")
elif args[:2] == ["pr", "edit"]:
    pass
elif args[:2] == ["pr", "view"]:
    print(json.dumps({"number": int(args[2]), "title": "public fix", "url": "https://example.invalid/pull/" + args[2],
                      "headRefName": "contrib/fix", "files": json.loads(os.environ.get("FAKE_GH_FILES", "[]"))}))
elif args[:2] == ["pr", "diff"]:
    sys.stdout.write(open(os.environ["FAKE_GH_DIFF"]).read())
else:
    sys.exit(97)
PY
chmod +x "$FAKE_BIN/gh"
export FAKE_GH_LOG="$SCRATCH/gh.log"; : > "$FAKE_GH_LOG"; unset FAKE_GH_OPEN_PR
PUBURL="https://github.com/example/orchestratormaxxing.git"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --push --remote "$PUB" --repo-dir "$REPO_DIR" >/dev/null 2>&1 || fail "C13 baseline push failed"
printf '#!/bin/sh\necho core v2\n' > "$SRC/bin/tool"; git -C "$SRC" commit -qam "v2"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --remote "$PUB" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C13 --pr with a non-GitHub remote must exit 2, got $rc"
# the local bare remote stands in for GitHub through a scoped url rewrite; the gh shim only sees owner/repo
export GIT_CONFIG_GLOBAL="$SCRATCH/gitconfig"; : > "$GIT_CONFIG_GLOBAL"
git config --global url."$PUB".insteadOf "$PUBURL"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --remote "$PUBURL" --repo-dir "$REPO_DIR" --json > "$SCRATCH/c13.json" 2> "$SCRATCH/c13.err" || fail "C13 --pr failed: $(cat "$SCRATCH/c13.err")"
python3 - "$SCRATCH/c13.json" "$FAKE_GH_LOG" "$PUB" <<'PY' || fail "C13 PR publication wrong"
import json, subprocess, sys
d = json.load(open(sys.argv[1])); p = d["push"]
assert p["mode"] == "pr" and p["pushed"] and p["action"] == "created" and p["branch"] == "graduate/next", p
calls = [json.loads(l) for l in open(sys.argv[2])]
assert calls[0][:2] == ["pr", "list"] and calls[1][:2] == ["pr", "create"], calls
create = calls[1]
assert create[create.index("--repo") + 1] == "example/orchestratormaxxing"
assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == "graduate/next"
body = create[create.index("--body") + 1]
assert "source commit" in body and "absorb-pr" in body
heads = subprocess.run(["git", "-C", sys.argv[3], "for-each-ref", "--format=%(refname:short)"], capture_output=True, text=True).stdout.split()
assert "graduate/next" in heads and "main" in heads, heads
ahead = subprocess.run(["git", "-C", sys.argv[3], "rev-list", "--count", "main..graduate/next"], capture_output=True, text=True).stdout.strip()
assert ahead == "1", ahead
blob = subprocess.run(["git", "-C", sys.argv[3], "show", "graduate/next:bin/tool"], capture_output=True, text=True).stdout
assert "core v2" in blob
mainblob = subprocess.run(["git", "-C", sys.argv[3], "show", "main:bin/tool"], capture_output=True, text=True).stdout
assert "core v2" not in mainblob, "main must not move on --pr"
PY
printf '#!/bin/sh\necho core v3\n' > "$SRC/bin/tool"; git -C "$SRC" commit -qam "v3"
: > "$FAKE_GH_LOG"
FAKE_GH_OPEN_PR=99 PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --remote "$PUBURL" --repo-dir "$REPO_DIR" --json > "$SCRATCH/c13b.json" 2>/dev/null || fail "C13 second --pr failed"
python3 - "$SCRATCH/c13b.json" "$FAKE_GH_LOG" "$PUB" <<'PY' || fail "C13 PR refresh wrong"
import json, subprocess, sys
d = json.load(open(sys.argv[1])); p = d["push"]
assert p["action"] == "updated" and p["pr"] == 99, p
calls = [json.loads(l) for l in open(sys.argv[2])]
assert [c[:2] for c in calls] == [["pr", "list"], ["pr", "edit"]], calls
ahead = subprocess.run(["git", "-C", sys.argv[3], "rev-list", "--count", "main..graduate/next"], capture_output=True, text=True).stdout.strip()
assert ahead == "1", f"rolling branch must be rebuilt from main, got {ahead} ahead"
PY
: > "$FAKE_GH_LOG"
FAKE_GH_OPEN_PR=99 PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --remote "$PUBURL" --repo-dir "$REPO_DIR" --json > "$SCRATCH/c13c.json" 2>/dev/null || fail "C13 idempotent --pr failed"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["push"]["pushed"] is False and "no changes" in d["push"]["reason"], d' "$SCRATCH/c13c.json" || fail "C13 no-change run must not publish"
[[ ! -s "$FAKE_GH_LOG" ]] || fail "C13 no-change run must not call gh"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --push --remote "$PUBURL" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C13 --push with --pr must exit 2, got $rc"
printf 'x\n' >> "$SRC/bin/tool"
set +e; PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --worktree --out "$OUT" --pr --remote "$PUBURL" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "C13 --worktree --pr must exit 2, got $rc"
git -C "$SRC" checkout -q -- bin/tool
printf '#!/bin/sh\necho core v4\n' > "$SRC/bin/tool"; git -C "$SRC" commit -qam "v4"
# no usable gh (the override points at nothing; a non-executable override never falls back to PATH)
set +e; CORE_EXPORT_GH="$SCRATCH/no-such-gh" PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --remote "$PUBURL" --repo-dir "$REPO_DIR" 2>/dev/null; rc=$?; set -e
[[ $rc -eq 4 ]] || fail "C13 --pr without gh must exit 4, got $rc"
pass "C13 --pr: rolling branch from main, one PR created then refreshed, idempotent, refusals"

# ── C14: --absorb-pr brings a public PR back into the private tree (check-only, then --apply) ─
cat > "$SCRATCH/pr.diff" <<'DIFF'
diff --git a/bin/tool b/bin/tool
--- a/bin/tool
+++ b/bin/tool
@@ -1,2 +1,3 @@
 #!/bin/sh
 echo core v4
+echo contributed
DIFF
export FAKE_GH_DIFF="$SCRATCH/pr.diff"
FAKE_GH_FILES='[{"path":"bin/tool"},{"path":"EXPORT-MANIFEST.json"}]' PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --absorb-pr 5 --remote "$PUBURL" --json > "$SCRATCH/c14.json" 2>/dev/null || fail "C14 absorb check failed"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))["absorb"]; assert d["applies_cleanly"] and not d["applied"] and d["ignored"]==["EXPORT-MANIFEST.json"], d' "$SCRATCH/c14.json" || fail "C14 check-only report wrong"
git -C "$SRC" diff --quiet HEAD || fail "C14 check-only run modified the private tree"
FAKE_GH_FILES='[{"path":"bin/tool"}]' PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --absorb-pr 5 --apply --remote "$PUBURL" --json > "$SCRATCH/c14b.json" 2>/dev/null || fail "C14 absorb --apply failed"
grep -q "echo contributed" "$SRC/bin/tool" || fail "C14 patch not applied to the private tree"
# --3way applies through the index: the change is STAGED, never committed
git -C "$SRC" diff --quiet HEAD -- bin/tool && fail "C14 --apply must leave the change uncommitted for contracts + commit"
[[ "$(git -C "$SRC" log -1 --format=%s)" == "v4" ]] || fail "C14 absorb must never commit"
git -C "$SRC" checkout -q HEAD -- bin/tool
set +e; FAKE_GH_FILES='[{"path":"bin/tool"},{"path":"bin/fleet-tool"}]' PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --absorb-pr 6 --apply --remote "$PUBURL" 2> "$SCRATCH/c14.err"; rc=$?; set -e
[[ $rc -eq 3 ]] || fail "C14 PR outside the selection must exit 3, got $rc"
grep -q "OUTSIDE bin/fleet-tool" "$SCRATCH/c14.err" || fail "C14 outside path not named"
git -C "$SRC" diff --quiet HEAD || fail "C14 refused absorb touched the tree"
pass "C14 --absorb-pr: check-only, --apply uncommitted, outside-selection refused"
unset GIT_CONFIG_GLOBAL


# ── C15: rename-at-graduation — contents, paths, skill-manifest rehash, inverse absorb ─
rm -rf "$OUT"
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --json > "$SCRATCH/c15.json" 2>/dev/null || fail "C15 export with rename failed"
[[ -f "$OUT/plugins/publiclab/skills/hello/SKILL.md" ]] || fail "C15 path not renamed"
[[ ! -e "$OUT/plugins/acmelab" ]] || fail "C15 private-named path survived"
grep -q '\$publiclab:hello' "$OUT/plugins/publiclab/skills/hello/SKILL.md" || fail "C15 lower token not renamed"
grep -q 'PUBLICLAB fleet' "$OUT/plugins/publiclab/skills/hello/SKILL.md" || fail "C15 UPPER token not renamed"
grep -q 'Publiclab docs' "$OUT/plugins/publiclab/skills/hello/SKILL.md" || fail "C15 Capitalized token not renamed"
! grep -riq acmelab "$OUT" --exclude=EXPORT-MANIFEST.json && true || fail "C15 private identity leaked into the public tree"
python3 - "$OUT" <<'PYH' || fail "C15 skill manifest hash not recomputed over the renamed payload"
import hashlib, json, os, sys
out = sys.argv[1]
doc = json.load(open(os.path.join(out, "skills/external-stack.json")))
it = doc["skills"][0]
assert it["path"] == "plugins/publiclab/skills/hello", it["path"]
base = os.path.join(out, it["path"])
d = hashlib.sha256()
for f in sorted(os.listdir(base)):
    p = os.path.join(base, f)
    d.update(f.encode()); d.update(b"\0"); d.update(hashlib.sha256(open(p,"rb").read()).hexdigest().encode()); d.update(b"\n")
assert it["tree_sha256"] == d.hexdigest(), (it["tree_sha256"], d.hexdigest())
PYH
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["renames"]==["acmelab->publiclab"] and d["rename"]["paths_renamed"]>=1 and d["rehashed_manifests"], d' "$SCRATCH/c15.json" || fail "C15 summary wrong"
# inverse absorb: a public PR touching the renamed path + tokens lands on the private names
cat > "$SCRATCH/pr15.diff" <<'DIFF'
diff --git a/plugins/publiclab/skills/hello/SKILL.md b/plugins/publiclab/skills/hello/SKILL.md
--- a/plugins/publiclab/skills/hello/SKILL.md
+++ b/plugins/publiclab/skills/hello/SKILL.md
@@ -1,4 +1,5 @@
 ---
 name: hello
 ---
 Use $publiclab:hello on the PUBLICLAB fleet (Publiclab docs).
+Contributed line about $publiclab:hello.
DIFF
export FAKE_GH_DIFF="$SCRATCH/pr15.diff"
FAKE_GH_FILES='[{"path":"plugins/publiclab/skills/hello/SKILL.md"}]' PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --absorb-pr 7 --apply --remote "$PUBURL" --json > "$SCRATCH/c15b.json" 2>/dev/null || fail "C15 inverse absorb failed"
grep -q 'Contributed line about \$acmelab:hello' "$SRC/plugins/acmelab/skills/hello/SKILL.md" || fail "C15 absorbed content not inverse-renamed"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))["absorb"]; assert d["files"]==["plugins/acmelab/skills/hello/SKILL.md"], d' "$SCRATCH/c15b.json" || fail "C15 absorbed file list not inverse-renamed"
git -C "$SRC" checkout -q HEAD -- plugins 2>/dev/null || true
pass "C15 rename-at-graduation: contents+paths renamed, manifests rehashed, absorb inverse-mapped"

# ── C16: PR body carries sanitized, publicly-named source subjects; blocked ones dropped ─
git -C "$SRC" commit -q --allow-empty -m "feat: acmelab learns a trick"
git -C "$SRC" commit -q --allow-empty -m "fix: secret client fleet-server tuning"
: > "$FAKE_GH_LOG"; unset FAKE_GH_OPEN_PR
export GIT_CONFIG_GLOBAL="$SCRATCH/gitconfig"   # re-arm the scoped URL rewrite (C14 unset it)
PATH="$PATH_WITH" "$TOOL" --repo "$SRC" --out "$OUT" --pr --remote "$PUBURL" --repo-dir "$REPO_DIR" --json > "$SCRATCH/c16.json" 2> "$SCRATCH/c16.err" || fail "C16 provenance --pr failed: $(tail -3 "$SCRATCH/c16.err" | tr '\n' ' ')"
python3 - "$FAKE_GH_LOG" <<'PY' || fail "C16 provenance body wrong"
import json, sys
calls = [json.loads(l) for l in open(sys.argv[1])]
create = next(c for c in calls if c[:2] == ["pr", "create"])
body = create[create.index("--body") + 1]
assert "feat: publiclab learns a trick" in body, body[:400]
assert "fleet-server" not in body and "acmelab" not in body, body[:400]
PY
pass "C16 provenance: renamed subjects in the PR body, blocked subjects dropped"
unset GIT_CONFIG_GLOBAL
echo "core-export contract: all cases passed"
