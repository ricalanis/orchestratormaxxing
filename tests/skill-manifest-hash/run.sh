#!/usr/bin/env bash
# Contract: every local:// entry in the governed skill manifests carries the tree_sha256 of the
# payload that is actually in the tree. sync-agent-skills refuses drift at INSTALL time; this makes
# the same drift red at VERIFY time, before it ships to a machine that cannot repair it.
# Pure Python digest (same algorithm as bin/sync-agent-skills.tree_digest); no network, no cache.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST_DIR="${SKILL_MANIFEST_DIR:-$ROOT/skills}"
fail() { printf 'skill-manifest-hash contract: %s\n' "$*" >&2; exit 1; }
python3 - "$ROOT" "$MANIFEST_DIR" <<'PY' || fail "manifest hash drift (re-hash the entry after editing its payload)"
import hashlib, json, os, sys
root, mdir = sys.argv[1], sys.argv[2]

def tree_digest(base, includes):
    files = set()
    for inc in includes:
        p = os.path.join(base, inc)
        if os.path.isfile(p):
            files.add(p)
        elif os.path.isdir(p):
            for dp, _, fs in os.walk(p):
                for f in fs:
                    files.add(os.path.join(dp, f))
        else:
            raise SystemExit(f"missing include {inc} under {base}")
    d = hashlib.sha256()
    for f in sorted(files, key=lambda q: os.path.relpath(q, base)):
        rel = os.path.relpath(f, base)
        d.update(rel.encode()); d.update(b"\0")
        d.update(hashlib.sha256(open(f, "rb").read()).hexdigest().encode()); d.update(b"\n")
    return d.hexdigest()

checked, bad = 0, []
for name in ("external-stack.json", "fleet-stack.json"):
    mp = os.path.join(mdir, name)
    if not os.path.isfile(mp):
        continue
    for item in json.load(open(mp))["skills"]:
        if not str(item.get("repo", "")).startswith("local://"):
            continue
        base = os.path.join(root, item["path"])
        if not os.path.isdir(base):
            bad.append(f"{name}: {item['name']} payload dir missing: {item['path']}"); continue
        got = tree_digest(base, item.get("include", ["."]))
        checked += 1
        if got != item["tree_sha256"]:
            bad.append(f"{name}: {item['name']} expected {item['tree_sha256'][:12]} got {got[:12]}")
if checked == 0:
    bad.append("no local:// entries found — contract would be vacuous")
for b in bad:
    print("  drift:", b, file=sys.stderr)
sys.exit(1 if bad else 0)
PY
echo "skill-manifest-hash contract: all local:// payload hashes match"
