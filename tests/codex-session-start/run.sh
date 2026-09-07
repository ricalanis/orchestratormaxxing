#!/usr/bin/env bash
# Root-authored contract for the bounded Codex SessionStart formatter.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/codex-session-start"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/codex-session-start.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
FAKE="$TMP/bin"; mkdir -p "$FAKE"
LOG="$TMP/calls.log"; : > "$LOG"; export CODEX_START_TEST_LOG="$LOG"
fail(){ printf 'codex-session-start contract: %s\n' "$*" >&2; exit 1; }
pass(){ printf '  ok  %s\n' "$*"; }

[[ -x "$TOOL" ]] || fail 'formatter missing or not executable'

cat > "$FAKE/memoryctl" <<'SH'
#!/usr/bin/env bash
printf 'memoryctl %s\n' "$*" >> "$CODEX_START_TEST_LOG"
[[ "$1" == brief ]] || exit 2
python3 - <<'PY'
print('Shared governed project memory')
print(('MEMORY_DETAIL_áβ ' * 900).strip())
PY
SH
cat > "$FAKE/mem-audit" <<'SH'
#!/usr/bin/env bash
printf 'mem-audit %s\n' "$*" >> "$CODEX_START_TEST_LOG"
printf '⚠ AUDIT_DETAIL_%05000d\n' 1
SH
cat > "$FAKE/loop-tick" <<'SH'
#!/usr/bin/env bash
printf 'loop-tick %s\n' "$*" >> "$CODEX_START_TEST_LOG"
SH
cat > "$FAKE/loop-queue" <<'SH'
#!/usr/bin/env bash
printf 'loop-queue %s\n' "$*" >> "$CODEX_START_TEST_LOG"
case "$1 $2" in
  'status --json') printf '{"open":45,"claimed":2,"resolved":146,"total":193,"actionable":35}\n' ;;
  'list --json') printf '[{"id":"lq-first","status":"open"}]\n' ;;
  *) exit 2 ;;
esac
SH
cat > "$FAKE/session-log" <<'SH'
#!/usr/bin/env bash
printf 'session-log %s\n' "$*" >> "$CODEX_START_TEST_LOG"
[[ "$*" == 'tail --n 1 --json' ]] || exit 2
python3 - <<'PY'
import json
print(json.dumps({
  'changelog': {'path':'/fixture/docs/changelog.md','requested':10,'returned':2,
    'entries':['## NEWEST_CHANGE_MARKER — verified state\n' + 'CHANGE_ñ ' * 700,
               '## older change\nold']},
  'wip': {'path':'/fixture/docs/WIP.md',
    'body':'# WIP\nCURRENT_WIP_MARKER\n' + 'WIP_界 ' * 2400}
}, ensure_ascii=False))
PY
SH
chmod +x "$FAKE"/*

OUT="$TMP/out"
PATH="$FAKE:$PATH" "$TOOL" > "$OUT" || fail 'formatter returned non-zero'
python3 - "$OUT" <<'PY' || fail 'C1 output is not bounded valid UTF-8 with required state'
import pathlib, sys
b = pathlib.Path(sys.argv[1]).read_bytes()
assert len(b) <= 16384, len(b)
s = b.decode('utf-8')
for token in ('Shared governed', '35 actionable', '45 open',
              '$orchestratormaxxing:self-improve', 'NEWEST_CHANGE_MARKER',
              'CURRENT_WIP_MARKER'):
    assert token in s, token
assert any(cmd in s for cmd in ('memoryctl brief', 'loop-queue list', 'session-log tail')), s[-500:]
PY
pass 'C1 output is valid UTF-8, <=16 KiB, and retains every required state signal'

[[ "$(grep -c '^loop-tick --kick --quiet$' "$LOG")" == 1 ]] \
  || fail 'C2 loop watcher did not run exactly once with --kick --quiet'
[[ "$(grep -c '^memoryctl brief' "$LOG")" == 1 ]] || fail 'C2 memory brief call count'
[[ "$(grep -c '^mem-audit *$' "$LOG")" == 1 ]] || fail 'C2 mem-audit call count'
[[ "$(grep -c '^loop-queue status --json$' "$LOG")" == 1 ]] || fail 'C2 queue status call count'
[[ "$(grep -c '^session-log tail --n 1 --json$' "$LOG")" == 1 ]] || fail 'C2 session-log JSON call count'
pass 'C2 every startup dependency runs once and the watcher uses the non-blocking spelling'

: > "$LOG"
PATH="$FAKE:$PATH" SOLPLAN_CHILD=1 "$TOOL" > "$TMP/child"
[[ ! -s "$TMP/child" ]] || fail 'C3 planner child emitted startup context'
[[ ! -s "$LOG" ]] || fail 'C3 planner child invoked a startup dependency'
pass 'C3 SOLPLAN_CHILD is a complete silent no-op'

# Make the dependency genuinely fail. Moving the stub aside is NOT enough:
# session-log is deployed globally to ~/.local/bin, so the real one shadows the
# gap and succeeds, and the formatter correctly renders its healthy sections
# instead of a degraded one. Replace the stub with a failing one so this case
# tests the degraded path it claims to test.
mv "$FAKE/session-log" "$FAKE/session-log.off"
cat > "$FAKE/session-log" <<'SH'
#!/usr/bin/env bash
printf 'session-log %s\n' "$*" >> "$CODEX_START_TEST_LOG"
exit 127
SH
chmod +x "$FAKE/session-log"
PATH="$FAKE:$PATH" "$TOOL" > "$TMP/degraded" || fail 'C4 dependency failure broke startup'
python3 - "$TMP/degraded" <<'PY' || fail 'C4 degraded output invalid'
import pathlib, sys
b = pathlib.Path(sys.argv[1]).read_bytes(); s = b.decode('utf-8')
assert len(b) <= 16384
assert 'session-log' in s and ('unavailable' in s.lower() or 'failed' in s.lower())
PY
pass 'C4 dependency failure is bounded, named, and hook-safe'

python3 - "$ROOT/plugins/orchestratormaxxing/hooks/hooks.json" <<'PY' || fail 'C5 plugin hook shape'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); hooks = json.loads(p.read_text())['hooks']
groups = hooks['SessionStart']
cmds = [h.get('command','') for g in groups for h in g.get('hooks',[])]
assert len(groups) == 2, groups
assert sum('codex-session-start' in c for c in cmds) == 1, cmds
assert sum('warp-agent-recovery' in c for c in cmds) == 1, cmds
for old in ('memoryctl', 'mem-audit', 'loop-queue', 'session-log tail'):
    assert not any(old in c for c in cmds), (old, cmds)
all_cmds = [h.get('command','') for gs in hooks.values() for g in gs for h in g.get('hooks',[])]
assert any('agent-guard" pre' in c for c in all_cmds)
assert any('agent-guard" post' in c for c in all_cmds)
PY
pass 'C5 plugin has one bounded visible hook and preserves lifecycle/security ownership'

grep -Fq 'bin/codex-session-start' "$ROOT/install.sh" || fail 'C6 formatter not copied by installer'
grep -Fq '"$BIN_DST/codex-session-start"' "$ROOT/install.sh" || fail 'C6 formatter not made executable'
pass 'C6 formatter is deployed by the public core installer'

printf 'codex-session-start contract: all cases passed\n'
