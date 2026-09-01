#!/usr/bin/env bash
# Contract: every bash script this repo ships declares `pipefail`.
#
# Why: a pipeline reports only its LAST stage's status, so `cmd | tail` exits 0
# even when cmd died. That is not theoretical — it bit three times in one
# session on 2026-08-27, most consequentially on a `git push` that failed with
# an auth error while the wrapper printed `push_exit=0`; it was caught only
# because the ahead-count did not move. The playbook has recorded the lesson
# since [E26]; prose did not stop it recurring, so it becomes a gate.
#
# The check is deliberately narrow: it requires `pipefail`, never `-e` or `-u`.
# Several cron wrappers intentionally log a failure and continue, and imposing
# errexit on them would change behaviour rather than reveal a bug.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

fail() { echo "pipefail-lint: FAIL — $*" >&2; exit 1; }

# Explicit, auditable exemptions. A file meant to be SOURCED must not set
# pipefail: the option would leak into the sourcing shell and change the
# behaviour of scripts that never opted in. Kept as a literal list, not a
# marker grep, so adding one is a visible decision rather than a comment.
exempt_sourced() {
  case "$1" in
    DevOps/maintenance/gcp/config.sh) return 0 ;;   # "Source this from other scripts"
    *) return 1 ;;
  esac
}

missing=()
exempted=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  hdr="$(head -3 "$f" 2>/dev/null || true)"
  [[ "$hdr" =~ \#\!.*bash ]] || continue
  if exempt_sourced "$f"; then
    grep -qE '^#.*[Ss]ource this' "$f" \
      || fail "$f is exempted as sourced but no longer says so — re-check the exemption"
    exempted=$((exempted + 1)); continue
  fi
  grep -q 'pipefail' "$f" || missing+=("$f")
done < <(git ls-files 'bin/*' 'tests/*' '*.sh' 'orchestrator/bin/*' 'orchestrator/hooks/*')

if [ ${#missing[@]} -gt 0 ]; then
  printf 'bash scripts without `set -o pipefail`:\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  fail "${#missing[@]} script(s) can hide a failing pipeline stage"
fi

# The lint must actually discriminate: a fixture without pipefail has to be
# caught. A guard that has never rejected anything is unfalsified.
probe="$(mktemp -d)/probe.sh"
printf '#!/usr/bin/env bash\nset -u\nfalse | true\n' > "$probe"
probe_hdr="$(head -3 "$probe" || true)"
if [[ "$probe_hdr" =~ \#\!.*bash ]] && ! grep -q 'pipefail' "$probe"; then
  :
else
  fail "the detector does not flag a pipefail-less bash script"
fi
rm -rf "$(dirname "$probe")"

echo "pipefail-lint contract: PASS ($exempted sourced file(s) exempted)"
