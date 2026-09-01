# orchestratormaxxing — `o` OpenCode tmux session helper (SOURCE this file, don't run it).
# Source of truth: <repo>/shell/opencode-o.sh; `./install.sh` deploys a copy to
# ~/.config/orchestratormaxxing/opencode-o.sh and sources it from bash/zsh startup files.
#
# `o` mirrors the useful lifecycle of `c`/`g` while respecting OpenCode's CLI:
#   o [name]                         new interactive OpenCode TUI in tmux
#   o [name] -A|--attach            attach/switch to exact opencode-<name> (-a also accepted)
#   o [name] --detach               create it and print its name without attaching
#   o [name] --headless --prompt P  detached one-shot via occ, output in /tmp
#   o --prompt P                    one-shot via occ (hardened `opencode run`), no tmux
#   o [name] --agent NAME           select the OpenCode agent (kimi-coder, glm-coder, …)
#   o ls [--json] [number|name]     list or attach to opencode-* sessions
#   o delegate NAME --profile P|--agent A --run-dir DIR [--json]  execute one bounded turn-1 task
#   o send SESSION --prompt P [--json]                 continue that worker
#   o handoff SESSION [--timeout N] [--json]           durable terminal result
#   o output SESSION [--lines N] [--json]              bounded observation
#   o close SESSION [--json]                           end that worker (tree kill)
#   o reap [--idle-minutes N] [--dry-run] [--json]     close abandoned workers
#
# Unknown flags are forwarded to OpenCode. One-shots go through `occ`, never bare
# `opencode run` — opencode 1.17.x intermittently hangs at startup (lq-6e4c38c5)
# and occ bounds/retries/reaps that. No `os` alias (too generic a name); use `o ls`.

_o_tmux_target() {
  # Leading '=' prevents tmux prefix matching (`opencode-app` must not match
  # `opencode-app-2`).
  printf '=%s' "$1"
}

_o_slug() {
  local slug
  slug="$(printf '%s' "$1" | tr -cs '[:alnum:]_-' '-' | sed -E 's/-+/-/g; s/^-//; s/-$//')"
  printf '%s\n' "${slug:-session}"
}

_o_has_session() {
  tmux has-session -t "$(_o_tmux_target "$1")" 2>/dev/null
}

_o_list_sessions() {
  local target="${1:-}"
  local sessions=()
  local _s
  while IFS= read -r _s; do
    [[ -n "$_s" ]] && sessions+=("$_s")
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^opencode-' | sort)

  if [[ ${#sessions[@]} -eq 0 ]]; then
    echo "No active opencode-* tmux sessions."
    return 1
  fi

  if [[ -n "$target" ]]; then
    local sess=""
    if [[ "$target" =~ ^[0-9]+$ ]]; then
      # Counter-based selection: identical in bash (0-indexed) and zsh (1-indexed).
      local _i=1
      for _s in "${sessions[@]}"; do
        [[ "$_i" == "$target" ]] && { sess="$_s"; break; }
        _i=$((_i + 1))
      done
      if [[ -z "$sess" ]]; then
        echo "No session #$target (valid: 1-${#sessions[@]}). Run 'o ls'."
        return 1
      fi
    else
      local want="$target"
      [[ "$want" != opencode-* ]] && want="opencode-$want"
      if _o_has_session "$want"; then
        sess="$want"
      else
        echo "No session named '$target' (tried '$want'). Run 'o ls'."
        return 1
      fi
    fi

    if [[ -n "${TMUX:-}" ]]; then
      echo "→ Switching to $sess"
      tmux switch-client -t "$(_o_tmux_target "$sess")"
    else
      echo "→ Attaching to $sess"
      _o_register_recovery "$sess"
      _o_bind_recovery "$sess"
      tmux attach-session -t "$(_o_tmux_target "$sess")"
    fi
    return $?
  fi

  echo "Active OpenCode sessions:"
  local i=1 att cwd st
  for _s in "${sessions[@]}"; do
    # The ':' is load-bearing: display-message wants a *pane* target, so a bare
    # '=session' resolves to nothing — and tmux still exits 0, so every session
    # would silently read as [detached] with an unknown cwd.
    att="$(tmux display-message -p -t "$(_o_tmux_target "$_s"):" '#{session_attached}' 2>/dev/null)"
    cwd="$(tmux display-message -p -t "$(_o_tmux_target "$_s"):" '#{pane_current_path}' 2>/dev/null)"
    if [[ -z "$att" || "$att" == "0" ]]; then st="detached"; else st="attached"; fi
    printf '  %2d) %-28s [%s]  %s\n' "$i" "$_s" "$st" "${cwd:-?}"
    i=$((i + 1))
  done
  echo
  echo "Attach: o ls <number|name>"
}

_o_list_sessions_json() {
  # Machine-readable sibling of _o_list_sessions: emits a JSON array of
  # {name, attached, cwd} for every active opencode-* tmux session.
  # Empty / no tmux → "[]" with exit 0 (machine consumers shouldn't treat
  # "nothing running" as an error). Output is ONLY the JSON — no header/footer.
  local sessions=()
  local _s
  while IFS= read -r _s; do
    [[ -n "$_s" ]] && sessions+=("$_s")
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^opencode-' | sort)

  if [[ ${#sessions[@]} -eq 0 ]]; then
    printf '[]'
    return 0
  fi

  local att cwd esc first=1
  printf '['
  for _s in "${sessions[@]}"; do
    # The ':' is load-bearing: display-message wants a *pane* target, so a
    # bare '=session' resolves to nothing (see _o_list_sessions for the trap).
    att="$(tmux display-message -p -t "$(_o_tmux_target "$_s"):" '#{session_attached}' 2>/dev/null)"
    cwd="$(tmux display-message -p -t "$(_o_tmux_target "$_s"):" '#{pane_current_path}' 2>/dev/null)"
    [[ -z "$att" || "$att" == "0" ]] && att="false" || att="true"
    [[ -z "$cwd" ]] && cwd="/"
    # Escape backslash and double-quote in cwd for safe JSON string emission.
    esc="${cwd//\\/\\\\}"
    esc="${esc//\"/\\\"}"
    [[ "$first" == "1" ]] && first=0 || printf ','
    printf '{"name":"%s","attached":%s,"cwd":"%s"}' "$_s" "$att" "$esc"
  done
  printf ']'
}

_o_next_session() {
  if ! _o_has_session "$1"; then printf '%s\n' "$1"; return; fi
  local n=2
  while _o_has_session "${1}-${n}"; do n=$((n + 1)); done
  printf '%s-%s\n' "$1" "$n"
}

# Fleet identity reader — identical copy in claude-c.sh / codex-g.sh / opencode-o.sh
# so each installed file stays self-contained. Prints the value of one
# ORCHESTRATORMAXXING_* key from the fleet.env file ($ORCHESTRATORMAXXING_FLEET_ENV, else
# ~/.config/orchestratormaxxing/fleet.env) WITHOUT sourcing it: only `KEY=VALUE` lines
# whose KEY matches ORCHESTRATORMAXXING_[A-Z_]+ count, an optional `export ` prefix and
# surrounding double quotes are stripped, nothing is expanded, comments/unknown
# keys are ignored, last assignment wins. Missing/unreadable file or absent key
# prints nothing (== not configured). Always returns 0. Portable bash/zsh.
_orchestratormaxxing_fleet_env() {
  local key="$1" file="${ORCHESTRATORMAXXING_FLEET_ENV:-$HOME/.config/orchestratormaxxing/fleet.env}"
  local line value="" re='^ORCHESTRATORMAXXING_[A-Z_]+='
  [[ -f "$file" && -r "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#export }"
    [[ "$line" =~ $re ]] || continue
    [[ "${line%%=*}" == "$key" ]] || continue
    value="${line#*=}"
    if [[ "$value" == \"*\" && ${#value} -ge 2 ]]; then
      value="${value#\"}"; value="${value%\"}"
    fi
  done < "$file"
  printf '%s' "$value"
}

_o_register_role() {
  local session="$1" role="$2" feature="$3"
  [[ -z "$role" && -z "$feature" ]] && return 0
  # Dashboard base URL: legacy ORCH_DASHBOARD_URL > ORCHESTRATORMAXXING_DASHBOARD_URL (env,
  # else fleet.env) > not configured → no network at all.
  local url="${ORCH_DASHBOARD_URL:-${ORCHESTRATORMAXXING_DASHBOARD_URL:-$(_orchestratormaxxing_fleet_env ORCHESTRATORMAXXING_DASHBOARD_URL)}}"
  [[ -n "$url" ]] || return 0
  local token="" payload link_payload
  if [[ -n "${HERMES_DASHBOARD_TOKEN+x}" ]]; then
    token="$HERMES_DASHBOARD_TOKEN"
  elif [[ -f "$HOME/.config/orchestratormaxxing/dashboard-token" ]]; then
    token="$(<"$HOME/.config/orchestratormaxxing/dashboard-token")"
  fi
  payload="$(python3 - "$session" "$role" "$feature" <<'PY'
import json, sys
print(json.dumps({"session_key": sys.argv[1], "role": sys.argv[2], "feature": sys.argv[3]}))
PY
)"
  local curl_args=(-fsS -m 2 -X POST "$url/api/session-meta" -H 'Content-Type: application/json')
  [[ -z "$token" ]] || curl_args+=(-H "Authorization: Bearer $token")
  curl "${curl_args[@]}" --data-binary "$payload" >/dev/null 2>&1 || true
  if [[ -n "$feature" ]]; then
    link_payload="$(python3 - "$session" <<'PY'
import json, sys
print(json.dumps({"session_id": sys.argv[1]}))
PY
)"
    curl_args=(-fsS -m 2 -X PATCH "$url/api/tasks/$feature/session" -H 'Content-Type: application/json')
    [[ -z "$token" ]] || curl_args+=(-H "Authorization: Bearer $token")
    curl "${curl_args[@]}" --data-binary "$link_payload" >/dev/null 2>&1 || true
  fi
}

_o_register_recovery() {
  local session="$1"
  [[ -z "${TMUX:-}" ]] || return 0
  [[ "${WARP_IS_LOCAL_SHELL_SESSION:-}" == "1" ]] || return 0
  [[ "${WARP_TERMINAL_SESSION_UUID:-}" =~ ^[[:xdigit:]]{32}$ ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0
  command warp-agent-recovery register-launch opencode "$session" "$PWD" >/dev/null 2>&1 || true
}

_o_bind_recovery() {
  local session="$1"
  [[ -z "${TMUX:-}" ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0
  command warp-agent-recovery bind-tmux opencode "$session" >/dev/null 2>&1 || true
}

_o_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

_o_json_status() {
  local st="$1" sess="${2:-}" detail="${3:-}"
  python3 -c 'import json,sys; print(json.dumps({"status":sys.argv[1],"session":sys.argv[2],"detail":sys.argv[3]}, separators=(",",":")))' \
    "$st" "$sess" "$detail"
}

_o_route_fields() {
  # stdout: profile<TAB>agent<TAB>model. The installed `oll` bridge is the
  # single executable authority; o/o-ubuntu therefore cannot drift apart.
  local kind="$1" value="$2" row
  command -v oll >/dev/null 2>&1 || return 127
  case "$kind" in
    profile) row="$(command oll --route-profile "$value")" || return $? ;;
    agent) row="$(command oll --route-agent "$value" 2>/dev/null)" || return $? ;;
    *) return 2 ;;
  esac
  [[ "$row" == *$'\t'*$'\t'* ]] || return 4
  printf '%s\n' "$row"
}

_o_installed_agent_model() {
  local agent="$1" cfg="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
  [[ -r "$cfg" ]] || return 1
  python3 - "$cfg" "$agent" <<'PY'
import json, sys
try:
    model = json.load(open(sys.argv[1], encoding="utf-8")).get("agent", {}).get(sys.argv[2], {}).get("model", "")
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
if not isinstance(model, str) or not model:
    raise SystemExit(1)
print(model)
PY
}

_o_model_policy_ok() {
  local model="$1" bare="$1"
  [[ "$bare" != ollama-cloud/* ]] || bare="${bare#ollama-cloud/}"
  command oll --check-model "$bare" >/dev/null
}

_o_validate_turn1_brief() {
  # stdout is either the accepted mode (legacy|marked) or a bounded refusal
  # detail. New briefs use a physical marker; unmarked bounded briefs remain
  # compatible, but an explicit bootstrap/wait-for-the-task brief is not a task.
  python3 - "$1" <<'PY'
import re
import sys

path = sys.argv[1]
begin = "<!-- o-delegate-turn-1:begin -->"
end = "<!-- o-delegate-turn-1:end -->"
try:
    text = open(path, encoding="utf-8").read()
except (OSError, UnicodeError):
    print("brief.md must be readable UTF-8")
    raise SystemExit(1)

begins = [match.start() for match in re.finditer(re.escape(begin), text)]
ends = [match.start() for match in re.finditer(re.escape(end), text)]
if begins or ends:
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        print("brief.md must contain exactly one ordered turn-1 marker block")
        raise SystemExit(1)
    task = text[begins[0] + len(begin):ends[0]].strip()
    if not task:
        print("turn-1 marker block must contain a bounded assignment")
        raise SystemExit(1)
    print("marked")
    raise SystemExit(0)

# Compatibility applies only to briefs that already are the immediate task.
# These paired signals cover the measured English/Spanish bootstrap pattern;
# they intentionally do not pretend to infer general boundedness from prose.
flat = re.sub(r"\s+", " ", text.casefold())
turn = re.search(r"\b(first|primer(?:o)?)\s+turn(?:o)?\b", flat)
wait = re.search(r"\b(wait|await|espera(?:r|ndo)?|espere|espero)\b", flat)
later = re.search(
    r"\b(next|siguiente)\s+(message|mensaje|turn|turno|task|tarea)\b"
    r"|\b(task|tarea)\b.{0,100}\b(next|siguiente)\s+(message|mensaje|turn|turno)\b",
    flat,
)
if (turn and wait and later) or (wait and later and re.search(r"\b(task|tarea)\b", flat)):
    print("brief.md defers the initial assignment; o delegate executes turn 1 immediately")
    raise SystemExit(1)

print("legacy")
PY
}

_o_exact_worker_session() {
  [[ "$1" =~ ^opencode-[[:alnum:]_-]+$ ]]
}

_o_resolve_pane() {
  local sess="$1"
  _o_has_session "$sess" || return 3
  local pane
  pane="$(tmux display-message -p -t "$(_o_tmux_target "$sess"):" '#{pane_id}' 2>/dev/null)" || return 4
  [[ "$pane" == %* ]] || return 4
  printf '%s\n' "$pane"
}

_o_worker_option() {
  local sess="$1" key="$2"
  tmux show-options -v -t "$(_o_tmux_target "$sess"):" "$key" 2>/dev/null || true
}

_o_set_worker_option() {
  local sess="$1" key="$2" value="$3"
  tmux set-option -t "$(_o_tmux_target "$sess"):" "$key" "$value" >/dev/null 2>&1
}

_o_owned_worker_session() {
  local sess="$1" tagged run_abs turn
  _o_exact_worker_session "$sess" && _o_has_session "$sess" || return 1
  tagged="$(_o_worker_option "$sess" @orchestratormaxxing_delegated)"
  run_abs="$(_o_worker_option "$sess" @orchestratormaxxing_run_dir)"
  turn="$(_o_worker_option "$sess" @orchestratormaxxing_turn)"
  [[ "$tagged" == 1 && "$run_abs" == /* && "$turn" =~ ^[1-9][0-9]*$ ]]
}

_o_wait_ready() {
  local sess="$1" max_seconds="${O_READY_TIMEOUT_SECONDS:-15}"
  [[ "$max_seconds" =~ ^[0-9]+$ ]] && [[ "$max_seconds" -ge 1 ]] || max_seconds=15
  local attempts=$((max_seconds * 5)) pane snap i=0 pending bound transport
  pane="$(_o_resolve_pane "$sess")" || return $?
  transport="$(_o_worker_option "$sess" @orchestratormaxxing_transport)"
  if [[ "$transport" == "run" ]]; then
    # TUI-less worker: ready = no in-flight turn. The pane is a runner, not a composer.
    while [[ "$i" -lt "$attempts" ]]; do
      pending="$(_o_worker_option "$sess" @orchestratormaxxing_pending)"
      [[ "$pending" != "1" ]] && return 0
      sleep 0.2; i=$((i + 1))
    done
    return 4
  fi
  while [[ "$i" -lt "$attempts" ]]; do
    # A plugin-bound idle event is the durable readiness signal after turn 1.
    # It survives alternate-screen redraws and localized/missing placeholders.
    pending="$(_o_worker_option "$sess" @orchestratormaxxing_pending)"
    bound="$(_o_worker_option "$sess" @opencode_session_id)"
    if [[ "$pending" == "0" && "$bound" == ses_* ]]; then
      return 0
    fi
    # Once a delegated turn exists, only its terminal plugin event may clear
    # readiness. A version footer remains visible while OpenCode is busy.
    if [[ "$pending" == "1" ]]; then
      sleep 0.2
      i=$((i + 1))
      continue
    fi
    snap="$(tmux capture-pane -p -t "$pane" -S -40 2>/dev/null)" || return 4
    # Initial OpenCode versions differ: some render the composer placeholder,
    # while 1.18.x can render only the ready UI plus its semantic-version
    # footer. This fallback is startup-only because delegated turns set
    # pending=1 before submission and clear it only through bind-event.
    if printf '%s\n' "$snap" | grep -qF 'Ask anything'; then
      return 0
    fi
    if printf '%s\n' "$snap" | grep -Eq '(^|[[:space:]])[0-9]+\.[0-9]+\.[0-9]+[[:space:]]*$'; then
      return 0
    fi
    sleep 0.2
    i=$((i + 1))
  done
  return 4
}

# Submit one TUI-less turn into a delegated worker pane: write the prompt to the
# run dir (audit trail), then respawn the pane onto a fresh bounded run-turn.
_o_submit_run_turn() {
  local sess="$1" agent="$2" oc_session="$3" prompt_text="$4"
  local run_abs turn pane pf
  run_abs="$(_o_worker_option "$sess" @orchestratormaxxing_run_dir)"
  turn="$(_o_worker_option "$sess" @orchestratormaxxing_turn)"
  [[ -d "$run_abs" && "$turn" =~ ^[0-9]+$ ]] || return 1
  pf="$run_abs/turn-$turn.prompt"
  printf '%s' "$prompt_text" > "$pf" || return 1
  chmod 600 "$pf" 2>/dev/null || true
  pane="$(_o_resolve_pane "$sess")" || return 1
  local cmd
  cmd="o run-turn --agent $(printf '%q' "$agent") --prompt-file $(printf '%q' "$pf")"
  [[ -n "$oc_session" ]] && cmd+=" --session $(printf '%q' "$oc_session")"
  tmux respawn-pane -k -t "$pane" \
    "ORCHESTRATORMAXXING_HARNESS_CHILD=1 ORCHESTRATORMAXXING_O_DELEGATED=1 ORCHESTRATORMAXXING_O_SHELL=$(printf '%q' "${ORCHESTRATORMAXXING_O_SHELL:-$HOME/.config/orchestratormaxxing/opencode-o.sh}") O_TURN_TIMEOUT_SECONDS=$(printf '%q' "${O_TURN_TIMEOUT_SECONDS:-600}") $cmd" 2>/dev/null
}

_o_send_worker() {
  local sess="${1:-}" prompt_text="" as_json=0
  [[ -n "$sess" ]] || { echo 'o send: SESSION is required' >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prompt) [[ $# -ge 2 ]] || { echo 'o send: --prompt requires text' >&2; return 2; }; prompt_text="$2"; shift 2 ;;
      --json) as_json=1; shift ;;
      *) echo "o send: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  _o_exact_worker_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status invalid_session "$sess" 'exact opencode-* session required'; return 2; }
  [[ -n "$prompt_text" ]] || { [[ "$as_json" == 1 ]] && _o_json_status invalid_prompt "$sess" 'non-empty prompt required'; return 2; }
  _o_owned_worker_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status not_owned "$sess" 'delegation ownership binding required'; return 4; }
  local pane rc old_turn new_turn
  pane="$(_o_resolve_pane "$sess")" || {
    rc=$?
    [[ "$as_json" == 1 ]] && { [[ "$rc" == 3 ]] && _o_json_status missing "$sess" 'session absent' || _o_json_status unreadable "$sess" 'pane resolution failed'; }
    return "$rc"
  }
  _o_wait_ready "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status not_ready "$sess" 'worker is busy or unreadable'; return 4; }
  old_turn="$(_o_worker_option "$sess" @orchestratormaxxing_turn)"
  [[ "$old_turn" =~ ^[0-9]+$ ]] || old_turn=0
  new_turn=$((old_turn + 1))
  _o_set_worker_option "$sess" @orchestratormaxxing_turn "$new_turn" || return 4
  _o_set_worker_option "$sess" @orchestratormaxxing_pending 1 || return 4
  local transport agent oc_session submitted=0
  transport="$(_o_worker_option "$sess" @orchestratormaxxing_transport)"
  if [[ "$transport" == "run" ]]; then
    agent="$(_o_worker_option "$sess" @orchestratormaxxing_agent)"
    oc_session="$(_o_worker_option "$sess" @opencode_session_id)"
    _o_submit_run_turn "$sess" "$agent" "$oc_session" "$prompt_text" && submitted=1
  else
    command tmux-send "$sess" "$prompt_text" >/dev/null && submitted=1
  fi
  if [[ "$submitted" != 1 ]]; then
    _o_set_worker_option "$sess" @orchestratormaxxing_turn "$old_turn" || true
    _o_set_worker_option "$sess" @orchestratormaxxing_pending 0 || true
    [[ "$as_json" == 1 ]] && _o_json_status unconfirmed "$sess" 'submission not confirmed'
    return 1
  fi
  if [[ "$as_json" == 1 ]]; then
    _o_json_status sent "$sess" 'submission confirmed'
  else
    printf '%s\n' "$sess"
  fi
}

# Machine-only turn runner for delegated workers (runs INSIDE the worker pane).
# The OpenTUI cannot be born reliably in a clientless pane (measured 2026-09-01:
# blank even with control-mode, script-PTY and properly sized real clients —
# lq-1e91f49c), so delegation is TUI-less: each turn is one bounded
# `opencode run --format json` in the pane, and its terminal event feeds the
# same durable `o bind-event` path the TUI plugin used. occ-style hardening:
# process-group kill on timeout so MCP children never orphan.
_o_run_turn() {
  local agent="" session="" prompt_file="" timeout="${O_TURN_TIMEOUT_SECONDS:-600}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent) agent="$2"; shift 2 ;;
      --session) session="$2"; shift 2 ;;
      --prompt-file) prompt_file="$2"; shift 2 ;;
      --timeout) timeout="$2"; shift 2 ;;
      *) echo "o run-turn: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  [[ -n "$agent" && -n "$prompt_file" && -r "$prompt_file" ]] || { echo 'o run-turn: --agent and a readable --prompt-file are required' >&2; return 2; }
  [[ "$timeout" =~ ^[0-9]+$ ]] && [[ "$timeout" -ge 1 ]] || timeout=600
  local oc; oc="$(command -v opencode 2>/dev/null)" || { echo 'o run-turn: opencode not on PATH' >&2; return 127; }
  local stream rc waited=0
  stream="$(mktemp "${TMPDIR:-/tmp}/o-run-turn.XXXXXX")" || return 1
  local prompt_text; prompt_text="$(cat "$prompt_file")"
  local run_args=(run --format json --agent "$agent")
  [[ -n "$session" ]] && run_args+=(-s "$session")
  echo "o run-turn: agent=$agent session=${session:-new} timeout=${timeout}s"
  set -m
  "$oc" "${run_args[@]}" "$prompt_text" >"$stream" 2>&1 </dev/null &
  local pid=$!
  set +m
  # Deterministic close path: o close reads this pidfile and kills the group,
  # so an in-flight model run can never outlive its worker (a trap alone loses
  # the race when the pane group takes SIGKILL before bash can forward TERM).
  printf '%s\n' "$pid" > "${prompt_file%.prompt}.pid" 2>/dev/null || true
  # The pane dying (o close, respawn, kill-session) must take the in-flight
  # model run and its MCP children with it — otherwise every close of a busy
  # worker leaks an opencode process group (caught live 2026-09-01: a closed
  # headless proof left `opencode run` running for 40 minutes).
  trap 'kill -TERM -- "-$pid" 2>/dev/null; sleep 1; kill -KILL -- "-$pid" 2>/dev/null; exit 143' TERM HUP INT
  while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt "$timeout" ]]; do
    sleep 1; waited=$((waited + 1))
  done
  # bin/o runs this under set -e: a nonzero wait must be captured, never fatal,
  # or a failing turn dies before its error event can bind (caught live: only
  # the PROVIDER_ERROR turn hung).
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null; sleep 2; kill -KILL -- "-$pid" 2>/dev/null
    wait "$pid" 2>/dev/null || true; rc=124
  else
    rc=0; wait "$pid" || rc=$?
  fi
  # Fold the stream into the bind-event payload. Any malformed/hung/failed turn
  # still produces a TYPED terminal event — silence is the one forbidden outcome.
  local payload
  payload="$(python3 - "$stream" "$rc" <<'PY'
import json, sys
path, rc = sys.argv[1], int(sys.argv[2])
sid = ""; mid = ""; finish = ""; texts = {}; err = ""
try:
    for raw in open(path, encoding="utf-8", errors="replace"):
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        t = ev.get("type"); part = ev.get("part") or {}
        if isinstance(ev.get("sessionID"), str):
            sid = ev["sessionID"] or sid
        m = part.get("messageID")
        if t == "text" and isinstance(part.get("text"), str) and isinstance(m, str):
            texts.setdefault(m, []).append(part["text"])
        elif t == "step_finish" and isinstance(m, str):
            mid = m; finish = str(part.get("reason") or "")
        elif t == "error":
            err = str((ev.get("error") or {}).get("name") or "error")[:64]
except OSError:
    err = err or "unreadable-stream"
if rc == 124:
    err = err or "timeout"
elif rc != 0:
    err = err or f"exit-{rc}"
text = "".join(texts.get(mid, [])) if mid else ""
if not sid:
    sid = "ses_unbound0"      # keeps the payload well-formed; error_code marks the failure
    err = err or "no-session"
if not mid and not err:
    err = "no-terminal-event"
print(json.dumps({"session_id": sid, "message_id": mid, "finish": finish or ("stop" if not err else "error"),
                  "text": text, "error_code": err}, separators=(",", ":")))
PY
)"
  rm -f -- "$stream"
  if printf '%s' "$payload" | o bind-event --json >/dev/null 2>&1 || printf '%s' "$payload" | command o bind-event --json >/dev/null 2>&1; then
    local turn; turn="$(printf '%s' "$payload" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("IDLE" if not d["error_code"] else "ERROR:"+d["error_code"])')"
    echo "TURN-DONE-$turn"
  else
    echo "BIND-FAILED"
  fi
  return 0
}

# Machine-only event bridge. The OpenCode plugin calls this from the exact
# worker process tree on session.idle/session.error. TMUX_PANE binds the event
# to one delegation even when several workers share a directory and model.
_o_bind_worker_event() {
  local as_json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) as_json=1; shift ;;
      *) echo "o bind-event: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  local pane="${TMUX_PANE:-}" sess tagged run_abs turn project_root allowed
  local bind_result rc ids oc_session message_id
  [[ -n "${TMUX:-}" && "$pane" == %* ]] || {
    [[ "$as_json" == 1 ]] && _o_json_status invalid_event '' 'TMUX_PANE worker context required'
    return 2
  }
  # Resolve the exact pane ID without tmux's prefix-matching target parser.
  # `=%pane` is not portable across the Linux and macOS tmux versions here.
  sess="$(tmux list-panes -a -F '#{pane_id} #{session_name}' 2>/dev/null \
    | awk -v want="$pane" '$1 == want { print $2; exit }')"
  _o_exact_worker_session "$sess" || {
    [[ "$as_json" == 1 ]] && _o_json_status invalid_event "$sess" 'delegated opencode session required'
    return 2
  }
  tagged="$(_o_worker_option "$sess" @orchestratormaxxing_delegated)"
  [[ "$tagged" == "1" ]] || {
    [[ "$as_json" == 1 ]] && _o_json_status invalid_event "$sess" 'untagged session'
    return 2
  }
  run_abs="$(_o_worker_option "$sess" @orchestratormaxxing_run_dir)"
  turn="$(_o_worker_option "$sess" @orchestratormaxxing_turn)"
  [[ "$turn" =~ ^[1-9][0-9]*$ ]] || {
    [[ "$as_json" == 1 ]] && _o_json_status invalid_event "$sess" 'missing turn binding'
    return 2
  }
  project_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)"
  allowed="$(python3 -c 'import os,sys
run=os.path.realpath(sys.argv[2])
base=os.path.realpath(os.path.join(sys.argv[1], ".results", "delegation"))
try:
    ok=os.path.commonpath([run,base]) == base and run != base
except ValueError:
    ok=False
if not ok or not os.path.isdir(run) or os.path.islink(sys.argv[2]):
    raise SystemExit(1)
print(run)' "$project_root" "$run_abs" 2>/dev/null)" || {
    [[ "$as_json" == 1 ]] && _o_json_status invalid_event "$sess" 'invalid run directory binding'
    return 2
  }
  run_abs="$allowed"
  bind_result="$(python3 -c 'import json,os,re,sys,tempfile
run,worker,turn=sys.argv[1],sys.argv[2],int(sys.argv[3])
try:
    raw=sys.stdin.buffer.read(1024*1024+1)
    if len(raw)>1024*1024:
        raise ValueError("event-too-large")
    row=json.loads(raw)
    if not isinstance(row,dict) or set(row)!={"session_id","message_id","finish","text","error_code"}:
        raise ValueError("invalid-shape")
    sid=row["session_id"]; mid=row["message_id"]; finish=row["finish"]
    text=row["text"]; error=row["error_code"]
    if not isinstance(sid,str) or not re.fullmatch(r"ses_[A-Za-z0-9]+",sid):
        raise ValueError("invalid-session-id")
    if not isinstance(mid,str) or (mid and not re.fullmatch(r"msg_[A-Za-z0-9_]+",mid)):
        raise ValueError("invalid-message-id")
    if not isinstance(finish,str) or len(finish)>64:
        raise ValueError("invalid-finish")
    if not isinstance(text,str):
        raise ValueError("invalid-text")
    if not isinstance(error,str) or len(error)>64:
        raise ValueError("invalid-error-code")
    if not mid and not error:
        raise ValueError("missing-message-id")
    binding={"schema_version":1,"worker_session":worker,"turn":turn,
             "opencode_session_id":sid,"message_id":mid,
             "finish":finish,"error_code":error}
    handoff=dict(binding); handoff["text"]=text
    for name,data in (("binding.json",binding),("handoff.json",handoff)):
        fd,tmp=tempfile.mkstemp(prefix="."+name+".",dir=run)
        try:
            with os.fdopen(fd,"w") as f:
                json.dump(data,f,separators=(",",":"),ensure_ascii=False)
                f.write("\n"); f.flush(); os.fsync(f.fileno())
            os.chmod(tmp,0o600)
            os.replace(tmp,os.path.join(run,name))
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    print(json.dumps({"status":"bound","session":worker,"turn":turn,
                      "opencode_session":sid,"message_id":mid},separators=(",",":")))
except Exception as exc:
    print(json.dumps({"status":"invalid_event","session":worker,
                      "detail":str(exc)[:80]},separators=(",",":")))
    raise SystemExit(2)' "$run_abs" "$sess" "$turn")"
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    [[ "$as_json" == 1 ]] && printf '%s\n' "$bind_result"
    return "$rc"
  fi
  ids="$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
print(o["opencode_session_id"]); print(o["message_id"])' "$run_abs/binding.json")" || return 4
  oc_session="${ids%%$'\n'*}"
  message_id="${ids#*$'\n'}"
  _o_set_worker_option "$sess" @opencode_session_id "$oc_session" || return 4
  _o_set_worker_option "$sess" @opencode_message_id "$message_id" || return 4
  _o_set_worker_option "$sess" @orchestratormaxxing_pending 0 || return 4
  if [[ "$as_json" == 1 ]]; then
    printf '%s\n' "$bind_result"
  else
    printf '%s\n' "$sess"
  fi
}

_o_handoff_worker() {
  local sess="${1:-}" timeout="${O_HANDOFF_TIMEOUT_SECONDS:-600}" as_json=0
  [[ -n "$sess" ]] || { echo 'o handoff: SESSION is required' >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --timeout) [[ $# -ge 2 ]] || { echo 'o handoff: --timeout requires seconds' >&2; return 2; }; timeout="$2"; shift 2 ;;
      --json) as_json=1; shift ;;
      *) echo "o handoff: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  _o_exact_worker_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status invalid_session "$sess" 'exact opencode-* session required'; return 2; }
  [[ "$timeout" =~ ^[0-9]+$ ]] || { [[ "$as_json" == 1 ]] && _o_json_status invalid_limit "$sess" 'timeout must be a non-negative integer'; return 2; }
  _o_has_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status missing "$sess" 'session absent'; return 3; }
  _o_owned_worker_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status not_owned "$sess" 'delegation ownership binding required'; return 4; }
  local run_abs turn waited=0 ticks file pending
  run_abs="$(_o_worker_option "$sess" @orchestratormaxxing_run_dir)"
  turn="$(_o_worker_option "$sess" @orchestratormaxxing_turn)"
  [[ -n "$run_abs" && "$turn" =~ ^[1-9][0-9]*$ ]] || {
    [[ "$as_json" == 1 ]] && _o_json_status unbound "$sess" 'worker has no durable turn binding'
    return 4
  }
  file="$run_abs/handoff.json"
  ticks=$((timeout * 5))
  while :; do
    if [[ -f "$file" ]] && python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
raise SystemExit(0 if o.get("worker_session")==sys.argv[2] and o.get("turn")==int(sys.argv[3]) else 1)' \
        "$file" "$sess" "$turn" 2>/dev/null; then
      break
    fi
    [[ "$waited" -lt "$ticks" ]] || {
      pending="$(_o_worker_option "$sess" @orchestratormaxxing_pending)"
      [[ "$as_json" == 1 ]] && _o_json_status readiness_failure "$sess" "turn $turn pending=${pending:-unknown}"
      return 4
    }
    sleep 0.2
    waited=$((waited + 1))
  done
  python3 -c 'import json,sys
path,worker,turn,as_json=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]=="1"
try:
    o=json.load(open(path))
    required={"schema_version","worker_session","turn","opencode_session_id",
              "message_id","finish","error_code","text"}
    if not isinstance(o,dict) or set(o)!=required or o["schema_version"]!=1:
        raise ValueError("invalid-shape")
    if o["worker_session"]!=worker or o["turn"]!=turn:
        raise ValueError("stale-turn")
    text=o["text"]; finish=o["finish"]; error=o["error_code"]
    if not isinstance(text,str) or not isinstance(finish,str) or not isinstance(error,str):
        raise ValueError("invalid-fields")
    n=len(text.encode("utf-8"))
    status="completed_retrievable"; rc=0
    if error:
        status="provider_error"; rc=69
    elif finish!="stop":
        status="incomplete_tool_call"; rc=65
    elif not text.strip():
        status="provider_empty"; rc=65
    elif n>65536:
        status="oversize"; rc=65
    row={"status":status,"session":worker,"turn":turn,
         "opencode_session":o["opencode_session_id"],
         "message_id":o["message_id"],"finish":finish,
         "bytes":n,"text":text if rc==0 else ""}
    if as_json:
        print(json.dumps(row,separators=(",",":"),ensure_ascii=False))
    elif rc==0:
        print(text)
    else:
        print("o handoff: "+status,file=sys.stderr)
    raise SystemExit(rc)
except SystemExit:
    raise
except Exception as exc:
    row={"status":"malformed_handoff","session":worker,"turn":turn,
         "detail":str(exc)[:80]}
    if as_json: print(json.dumps(row,separators=(",",":")))
    else: print("o handoff: malformed_handoff",file=sys.stderr)
    raise SystemExit(65)' "$file" "$sess" "$turn" "$as_json"
}

_o_output_worker() {
  local sess="${1:-}" lines=80 as_json=0
  [[ -n "$sess" ]] || { echo 'o output: SESSION is required' >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lines) [[ $# -ge 2 ]] || { echo 'o output: --lines requires a value' >&2; return 2; }; lines="$2"; shift 2 ;;
      --json) as_json=1; shift ;;
      *) echo "o output: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  _o_exact_worker_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status invalid_session "$sess" 'exact opencode-* session required'; return 2; }
  [[ "$lines" =~ ^[0-9]+$ ]] && [[ "$lines" -ge 1 ]] && [[ "$lines" -le 200 ]] \
    || { [[ "$as_json" == 1 ]] && _o_json_status invalid_limit "$sess" 'lines must be 1..200'; return 2; }
  local pane rc snap
  pane="$(_o_resolve_pane "$sess")" || {
    rc=$?
    [[ "$as_json" == 1 ]] && { [[ "$rc" == 3 ]] && _o_json_status missing "$sess" 'session absent' || _o_json_status unreadable "$sess" 'pane resolution failed'; }
    return "$rc"
  }
  snap="$(tmux capture-pane -p -t "$pane" -S "-$lines" 2>/dev/null)" || {
    [[ "$as_json" == 1 ]] && _o_json_status unreadable "$sess" 'pane capture failed'
    return 4
  }
  if [[ "$as_json" == 1 ]]; then
    printf '%s' "$snap" | python3 -c 'import json,sys
raw=sys.stdin.buffer.read()[-65536:]
text=raw.decode("utf-8","replace")
status="captured" if text.strip() else "empty"
print(json.dumps({"status":status,"session":sys.argv[1],"lines":int(sys.argv[2]),"byte_limit":65536,"text":text},separators=(",",":")))' "$sess" "$lines"
  else
    if printf '%s' "$snap" | grep -q '[^[:space:]]'; then
      printf '%s' "$snap" | tail -c 65536
      printf '\n'
    else
      printf 'o output: empty capture for %s\n' "$sess" >&2
    fi
  fi
}

# A delegated worker is a resource, not a document: it holds an OpenCode
# process plus its MCP children for as long as its tmux session lives. The
# lifecycle has to be closeable, or every delegation leaks one of each.
_o_close_worker() {
  local sess="${1:-}" as_json=0
  [[ -n "$sess" ]] || { echo 'o close: SESSION is required' >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) as_json=1; shift ;;
      *) echo "o close: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  _o_exact_worker_session "$sess" || { [[ "$as_json" == 1 ]] && _o_json_status invalid_session "$sess" 'exact opencode-* session required'; return 2; }
  if ! _o_has_session "$sess"; then
    [[ "$as_json" == 1 ]] && _o_json_status missing "$sess" 'session absent'
    return 3
  fi
  _o_owned_worker_session "$sess" || {
    [[ "$as_json" == 1 ]] && _o_json_status not_owned "$sess" 'delegation ownership binding required'
    return 4
  }
  local pane pid confirm_pane confirm_pid pgid waited=0
  # Resolve first so an unreadable session is typed as such rather than being
  # reported closed on the strength of a kill nobody could verify.
  pane="$(_o_resolve_pane "$sess")" || {
    [[ "$as_json" == 1 ]] && _o_json_status unreadable "$sess" 'pane resolution failed'
    return 4
  }
  pid="$(tmux display-message -p -t "$(_o_tmux_target "$sess"):" '#{pane_pid}' 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    # Revalidate identity immediately before signalling. A respawned pane or
    # reused name must never turn a stale observation into authority over a
    # different process tree.
    _o_owned_worker_session "$sess" || {
      [[ "$as_json" == 1 ]] && _o_json_status ownership_changed "$sess" 'ownership changed before signal'
      return 4
    }
    confirm_pane="$(_o_resolve_pane "$sess" 2>/dev/null || true)"
    confirm_pid="$(tmux display-message -p -t "$(_o_tmux_target "$sess"):" '#{pane_pid}' 2>/dev/null || true)"
    if [[ "$confirm_pane" != "$pane" || "$confirm_pid" != "$pid" ]]; then
      [[ "$as_json" == 1 ]] && _o_json_status ownership_changed "$sess" 'pane identity changed before signal'
      return 4
    fi
    # OpenCode spawns its MCP servers as children in its own process group, and
    # tmux only signals the pane leader — so a bare kill-session reparents live
    # node MCP servers to init (the same orphan class occ reaps). Signal the
    # group, and only when the pane process really leads it: killing -PID when
    # PID is not a group leader would signal an unrelated group.
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$pgid" == "$pid" ]]; then
      kill -TERM -- "-$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 25 ]]; do
      sleep 0.2
      waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      if [[ "$pgid" == "$pid" ]]; then
        kill -KILL -- "-$pid" 2>/dev/null || true
      else
        kill -KILL "$pid" 2>/dev/null || true
      fi
    fi
  fi
  # Kill the newest in-flight turn's own process group (run-turn spawns the
  # model run with set -m, so the pane-group kill above cannot reach it).
  local run_abs_close turnpid
  run_abs_close="$(_o_worker_option "$sess" @orchestratormaxxing_run_dir)"
  if [[ -d "$run_abs_close" ]]; then
    turnpid="$(ls -1t "$run_abs_close"/turn-*.pid 2>/dev/null | head -1)"
    if [[ -n "$turnpid" ]] && [[ "$(cat "$turnpid" 2>/dev/null)" =~ ^[0-9]+$ ]]; then
      turnpid="$(cat "$turnpid")"
      if kill -0 "$turnpid" 2>/dev/null; then
        kill -TERM -- "-$turnpid" 2>/dev/null || kill -TERM "$turnpid" 2>/dev/null
        sleep 0.3
        kill -KILL -- "-$turnpid" 2>/dev/null || kill -KILL "$turnpid" 2>/dev/null
      fi
    fi
  fi
  tmux kill-session -t "$(_o_tmux_target "$sess")" 2>/dev/null || true
  if _o_has_session "$sess"; then
    [[ "$as_json" == 1 ]] && _o_json_status unclosed "$sess" 'session survived kill'
    return 4
  fi
  if [[ "$as_json" == 1 ]]; then
    _o_json_status closed "$sess" 'worker tree terminated'
  else
    printf '%s\n' "$sess"
  fi
}

# Janitor for workers Root never closed. It only ever touches sessions tagged
# as delegation-born at birth, so a human's interactive `o` TUI is untouchable;
# and only when detached, idle past the threshold, and sitting at a ready
# composer — a worker still producing output keeps its window activity fresh.
_o_reap_workers() {
  local idle_minutes="${O_REAP_IDLE_MINUTES:-60}" dry=0 as_json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --idle-minutes) [[ $# -ge 2 ]] || { echo 'o reap: --idle-minutes requires a value' >&2; return 2; }; idle_minutes="$2"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      --json) as_json=1; shift ;;
      *) echo "o reap: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  [[ "$idle_minutes" =~ ^[0-9]+$ ]] && [[ "$idle_minutes" -ge 1 ]] \
    || { [[ "$as_json" == 1 ]] && _o_json_status invalid_limit '' 'idle-minutes must be a positive integer'; return 2; }
  # Seam so the contract can drive the real selection path in seconds instead
  # of sleeping out a minute-scale threshold. Not a user-facing knob.
  local threshold=$((idle_minutes * 60))
  if [[ "${O_REAP_IDLE_SECONDS:-}" =~ ^[0-9]+$ ]]; then threshold="$O_REAP_IDLE_SECONDS"; fi
  local now candidates=() _s att activity pending run_abs turn handoff
  now="$(date +%s)"
  while IFS= read -r _s; do
    [[ -n "$_s" ]] || continue
    # The ':' is load-bearing here too: tmux 3.6 rejects a bare '=session'
    # target for set-option/show-options ("no such session").
    _o_owned_worker_session "$_s" || continue
    att="$(tmux display-message -p -t "$(_o_tmux_target "$_s"):" '#{session_attached}' 2>/dev/null || true)"
    [[ -z "$att" || "$att" == "0" ]] || continue
    # session_activity does not track pane output on every tmux build; the
    # window's activity clock does, so a busy worker cannot read as idle.
    activity="$(tmux display-message -p -t "$(_o_tmux_target "$_s"):" '#{window_activity}' 2>/dev/null || true)"
    [[ "$activity" =~ ^[0-9]+$ ]] || continue
    [[ $(( now - activity )) -ge "$threshold" ]] || continue
    pending="$(_o_worker_option "$_s" @orchestratormaxxing_pending)"
    [[ "$pending" == "0" ]] || continue
    run_abs="$(_o_worker_option "$_s" @orchestratormaxxing_run_dir)"
    turn="$(_o_worker_option "$_s" @orchestratormaxxing_turn)"
    handoff="$run_abs/handoff.json"
    python3 -c 'import json,sys
try:
    row=json.load(open(sys.argv[1],encoding="utf-8"))
    ok=(row.get("worker_session")==sys.argv[2]
        and row.get("turn")==int(sys.argv[3])
        and isinstance(row.get("finish"),str)
        and isinstance(row.get("error_code"),str))
except (OSError,ValueError,TypeError,AttributeError):
    ok=False
raise SystemExit(0 if ok else 1)' "$handoff" "$_s" "$turn" 2>/dev/null || continue
    candidates+=("$_s")
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^opencode-' | sort)

  local closed=()
  if [[ "$dry" == "0" ]]; then
    for _s in ${candidates[@]+"${candidates[@]}"}; do
      _o_close_worker "$_s" >/dev/null 2>&1 && closed+=("$_s")
    done
  else
    closed=(${candidates[@]+"${candidates[@]}"})
  fi

  if [[ "$as_json" == 1 ]]; then
    python3 -c 'import json,sys
print(json.dumps({"status":"reaped","dry_run":sys.argv[1]=="1","idle_minutes":int(sys.argv[2]),"sessions":sys.argv[3:]},separators=(",",":")))' \
      "$dry" "$idle_minutes" ${closed[@]+"${closed[@]}"}
  else
    for _s in ${closed[@]+"${closed[@]}"}; do printf '%s\n' "$_s"; done
  fi
}

_o_delegate_worker() {
  local name="${1:-}" agent="" profile="" model="" selection_source="" run_dir="" as_json=0
  [[ -n "$name" ]] || { echo 'o delegate: NAME is required' >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent) [[ $# -ge 2 ]] || { echo 'o delegate: --agent requires a value' >&2; return 2; }; agent="$2"; shift 2 ;;
      --profile) [[ $# -ge 2 ]] || { echo 'o delegate: --profile requires a value' >&2; return 2; }; profile="$2"; shift 2 ;;
      --run-dir) [[ $# -ge 2 ]] || { echo 'o delegate: --run-dir requires a value' >&2; return 2; }; run_dir="$2"; shift 2 ;;
      --json) as_json=1; shift ;;
      *) echo "o delegate: unknown argument '$1'" >&2; return 2 ;;
    esac
  done
  [[ -n "$run_dir" ]] || { [[ "$as_json" == 1 ]] && _o_json_status invalid_contract '' '--run-dir required'; return 2; }
  if [[ -n "$agent" && -n "$profile" ]] || [[ -z "$agent" && -z "$profile" ]]; then
    [[ "$as_json" == 1 ]] && _o_json_status invalid_route '' 'choose exactly one of --profile or --agent'
    return 2
  fi
  local route_fields="" canonical_profile="" canonical_agent="" canonical_model=""
  if [[ -n "$profile" ]]; then
    route_fields="$(_o_route_fields profile "$profile" 2>/dev/null)" || {
      [[ "$as_json" == 1 ]] && _o_json_status invalid_route '' "unknown profile: $profile"
      return 2
    }
    IFS=$'\t' read -r canonical_profile canonical_agent canonical_model <<< "$route_fields"
    profile="$canonical_profile"; agent="$canonical_agent"; model="$canonical_model"
    selection_source="profile"
  elif route_fields="$(_o_route_fields agent "$agent" 2>/dev/null)"; then
    IFS=$'\t' read -r canonical_profile canonical_agent canonical_model <<< "$route_fields"
    profile="$canonical_profile"; agent="$canonical_agent"; model="$canonical_model"
    selection_source="caller_agent"
  else
    # A custom installed agent is a deliberate caller override. Inspect its
    # configured model when possible; neutral custom names remain supported.
    selection_source="caller_override"
    model="$(_o_installed_agent_model "$agent" 2>/dev/null || true)"
    [[ -n "$model" ]] || model="caller-configured"
    local agent_model_guess="${agent%-coder}"
    if ! _o_model_policy_ok "$model" 2>/dev/null || ! _o_model_policy_ok "$agent_model_guess" 2>/dev/null; then
      [[ "$as_json" == 1 ]] && _o_json_status legacy_model '' "legacy model is forbidden for agent: $agent"
      return 2
    fi
  fi
  local project_root run_abs contract brief contract_sha brief_sha sess prompt_text cwd_physical turn1_mode
  project_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)"
  cwd_physical="$(pwd -P)"
  run_abs="$(python3 -c 'import os,sys
run=os.path.realpath(sys.argv[2])
allowed=os.path.realpath(os.path.join(sys.argv[1], ".results", "delegation"))
current=run
while True:
    try:
        if os.path.samefile(current, allowed):
            print(run)
            raise SystemExit(0)
    except OSError:
        pass
    parent=os.path.dirname(current)
    if parent == current:
        raise SystemExit(1)
    current=parent' "$project_root" "$run_dir")" || {
    [[ "$as_json" == 1 ]] && _o_json_status invalid_contract '' 'run-dir must be project .results/delegation child'
    return 2
  }
  contract="$run_abs/contract.md"; brief="$run_abs/brief.md"
  if [[ ! -f "$contract" || ! -s "$contract" || -L "$contract" || -w "$contract" || \
        ! -f "$brief" || ! -s "$brief" || -L "$brief" || -w "$brief" ]]; then
    [[ "$as_json" == 1 ]] && _o_json_status invalid_contract '' 'contract.md and brief.md must be non-empty regular read-only files'
    return 2
  fi
  if ! turn1_mode="$(_o_validate_turn1_brief "$brief")"; then
    [[ "$as_json" == 1 ]] && _o_json_status invalid_turn1_task '' "$turn1_mode"
    return 2
  fi
  contract_sha="$(_o_sha256 "$contract")" || return 2
  brief_sha="$(_o_sha256 "$brief")" || return 2
  # Dispatch is the natural sweep point (occ reaps orphans the same way): a new
  # delegation cleans up the workers earlier runs abandoned, so a forgotten
  # `o close` costs one idle window, not an unbounded pile of them.
  _o_reap_workers --json >/dev/null 2>&1 || true
  # Delegation is unattended by construction: OpenCode's --auto resolves any
  # permission that would otherwise pause on an approval dialog. Explicit deny
  # rules still block; canonical coding agents are reconciled to allow-all by
  # install.sh. Keep this flag here, not in generic `o`, so a human's ordinary
  # interactive OpenCode session retains its normal permission mode.
  # Mark the OpenCode process tree at birth. The global event plugin runs for
  # ordinary human sessions too; only delegation-born workers may bridge idle
  # and error events into `o bind-event`.
  sess="$(_o_next_session "opencode-$name")"
  echo "→ Creating new OpenCode session: $sess" >&2
  # A delegated worker pane hosts bounded `o run-turn` processes, one per turn
  # (TUI-less: see _o_run_turn). remain-on-exit keeps the pane addressable
  # between turns so respawn-pane can submit the next one.
  if ! tmux new-session -d -x 220 -y 50 -s "$sess" -c "$cwd_physical" 'tail -f /dev/null'       || ! tmux set-option -t "$(_o_tmux_target "$sess"):" remain-on-exit on 2>/dev/null; then
    echo "o delegate: failed to create worker session" >&2
    return 1
  fi
  # Bind ownership before readiness or submission. A human opencode-* TUI has
  # the same name shape, so prefix matching alone is never deletion authority.
  if ! _o_set_worker_option "$sess" @orchestratormaxxing_delegated 1 \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_run_dir "$run_abs" \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_turn 1 \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_pending 0 \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_turn1_mode "$turn1_mode" \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_transport run \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_agent "$agent" \
      || ! _o_set_worker_option "$sess" @orchestratormaxxing_model "$model"; then
    tmux kill-session -t "$(_o_tmux_target "$sess")" 2>/dev/null || true
    [[ "$as_json" == 1 ]] && _o_json_status ownership_failure "$sess" 'worker ownership binding failed; exact new session closed'
    return 4
  fi
  if [[ -n "$profile" ]] && ! _o_set_worker_option "$sess" @orchestratormaxxing_profile "$profile"; then
    tmux kill-session -t "$(_o_tmux_target "$sess")" 2>/dev/null || true
    [[ "$as_json" == 1 ]] && _o_json_status ownership_failure "$sess" 'worker profile binding failed; exact new session closed'
    return 4
  fi
  _o_set_worker_option "$sess" @orchestratormaxxing_pending 1 || return 4
  if [[ "$turn1_mode" == "marked" ]]; then
    prompt_text="You are a delegated OpenCode worker in this project. Read the Root-authored brief at $brief and acceptance contract at $contract. Execute the complete bounded assignment inside the o-delegate-turn-1 marker block now."
  else
    prompt_text="You are a delegated OpenCode worker in this project. Read the Root-authored brief at $brief and acceptance contract at $contract. Treat the whole brief as the complete bounded turn-1 assignment and execute it now."
  fi
  prompt_text+=" Never modify, grade, or certify either file. Work only within their scope. Give a bounded final handoff in this same session; Root alone runs acceptance, publishes output.md, and writes receipt.json."
  if ! _o_submit_run_turn "$sess" "$agent" "" "$prompt_text"; then
    _o_set_worker_option "$sess" @orchestratormaxxing_pending 0 || true
    [[ "$as_json" == 1 ]] && _o_json_status unconfirmed "$sess" "worker preserved; attach with o ls $sess"
    return 1
  fi
  if [[ "$as_json" == 1 ]]; then
    python3 -c 'import json,sys; print(json.dumps({"status":"sent","session":sys.argv[1],"attach":"o ls "+sys.argv[1],"handoff":"o handoff "+sys.argv[1],"close":"o close "+sys.argv[1],"contract_sha256":sys.argv[2],"brief_sha256":sys.argv[3],"profile":sys.argv[4],"agent":sys.argv[5],"model":sys.argv[6],"selection_source":sys.argv[7],"cwd":sys.argv[8],"turn1_mode":sys.argv[9]},separators=(",",":")))' \
      "$sess" "$contract_sha" "$brief_sha" "$profile" "$agent" "$model" "$selection_source" "$cwd_physical" "$turn1_mode"
  else
    printf '%s\n' "$sess"
  fi
}

_o_machine_main() {
  local verb="${1:-}"
  case "$verb" in
    delegate) shift; _o_delegate_worker "$@" ;;
    run-turn) shift; _o_run_turn "$@" ;;
    send) shift; _o_send_worker "$@" ;;
    bind-event) shift; _o_bind_worker_event "$@" ;;
    handoff) shift; _o_handoff_worker "$@" ;;
    output) shift; _o_output_worker "$@" ;;
    close) shift; _o_close_worker "$@" ;;
    reap) shift; _o_reap_workers "$@" ;;
    *) echo 'usage: o {delegate|send|handoff|output|close|reap} ...' >&2; return 2 ;;
  esac
}

# zsh expands an existing alias while parsing `o()`, which turns the function
# declaration into a syntax error. The harness intentionally owns this command.
unalias o 2>/dev/null || true

o() {
  case "${1:-}" in
    ls|list|-l|--list)
      shift
      if [[ "${1:-}" == "--json" ]]; then
        shift
        [[ -n "${1:-}" ]] && { echo "o: --json does not accept a target" >&2; return 2; }
        _o_list_sessions_json
        return $?
      fi
      _o_list_sessions "$@"
      return $?
      ;;
    delegate|send|bind-event|handoff|output|close|reap)
      local machine_verb="$1"
      shift
      _o_machine_main "$machine_verb" "$@"
      return $?
      ;;
  esac

  local name=""
  if [[ -n "${1:-}" && "${1:-}" != -* ]]; then
    name="$1"
    shift
  fi

  local headless=0 attach=0 detach=0 prompt_text="" role="" feature="" agent=""
  # Warp otherwise replaces OSC titles with its cwd/process-derived title.
  export WARP_DISABLE_AUTO_TITLE=true
  local opencode_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --headless) headless=1; shift ;;
      --prompt)
        [[ $# -ge 2 ]] || { echo "o: --prompt requires text" >&2; return 2; }
        prompt_text="$2"; shift 2 ;;
      -a|-A|--attach) attach=1; shift ;;
      -N|--new) attach=0; shift ;;
      --detach) detach=1; shift ;;
      --agent)
        [[ $# -ge 2 ]] || { echo "o: --agent requires a value" >&2; return 2; }
        agent="$2"; shift 2 ;;
      -R|--role)
        [[ $# -ge 2 ]] || { echo "o: --role requires a value" >&2; return 2; }
        role="$2"; shift 2 ;;
      -F|--feature)
        [[ $# -ge 2 ]] || { echo "o: --feature requires a value" >&2; return 2; }
        feature="$2"; shift 2 ;;
      --)
        shift
        while [[ $# -gt 0 ]]; do opencode_args+=("$1"); shift; done
        ;;
      *) opencode_args+=("$1"); shift ;;
    esac
  done
  if [[ "$attach" == "1" && "$detach" == "1" ]]; then
    echo "o: --attach and --detach are mutually exclusive" >&2
    return 2
  fi

  [[ -n "$name" ]] || name="$(basename "$(pwd)")"
  name="$(_o_slug "$name")"
  local base="opencode-$name"
  [[ -n "$role" ]] && base="opencode-$name-$role"

  local env_args=(env "PATH=$PATH")
  [[ -z "${WARP_TERMINAL_SESSION_UUID:-}" ]] || env_args+=("WARP_TERMINAL_SESSION_UUID=$WARP_TERMINAL_SESSION_UUID")
  [[ -z "${WARP_IS_LOCAL_SHELL_SESSION:-}" ]] || env_args+=("WARP_IS_LOCAL_SHELL_SESSION=$WARP_IS_LOCAL_SHELL_SESSION")
  [[ -z "${WARP_CLIENT_VERSION:-}" ]] || env_args+=("WARP_CLIENT_VERSION=$WARP_CLIENT_VERSION")
  [[ -z "${WARP_FOCUS_URL:-}" ]] || env_args+=("WARP_FOCUS_URL=$WARP_FOCUS_URL")
  env_args+=("WARP_DISABLE_AUTO_TITLE=$WARP_DISABLE_AUTO_TITLE")
  env_args+=("ORCHESTRATORMAXXING_HARNESS_CHILD=1")
  [[ "${ORCHESTRATORMAXXING_O_DELEGATED:-}" == "1" ]] \
    && env_args+=("ORCHESTRATORMAXXING_O_DELEGATED=1")
  local opencode_bin
  opencode_bin="$(command -v opencode 2>/dev/null)" || {
    echo "o: opencode is not installed or not on PATH" >&2
    return 127
  }

  # One-shots ride occ (bounded timeout/retry/orphan-reap over `opencode run`);
  # fall back to bare `opencode run` only if occ is missing. Scalar flag, not an
  # array index — bash arrays are 0-indexed, zsh's are 1-indexed. The optional
  # --agent pair lives in an array: `${agent:+--agent "$agent"}` word-splits
  # differently in bash vs zsh.
  local use_occ=0
  command -v occ >/dev/null 2>&1 && use_occ=1
  local agent_args=()
  [[ -z "$agent" ]] || agent_args=(--agent "$agent")

  if [[ -n "$prompt_text" && "$headless" == "0" ]]; then
    [[ "$use_occ" == "1" ]] || { echo 'o: occ is required for hardened one-shot mode' >&2; return 127; }
    command occ "$prompt_text" ${agent_args[@]+"${agent_args[@]}"}
    return $?
  fi

  if [[ "$headless" == "1" ]]; then
    if [[ -z "$prompt_text" ]]; then
      echo "o: --headless requires --prompt <text>" >&2
      return 2
    fi
    local sess outfile
    if [[ "$attach" == "1" ]] && _o_has_session "$base"; then
      echo "→ Session $base already exists. Use 'o ls $base' to view."
      return 0
    fi
    sess="$(_o_next_session "$base")"
    outfile="/tmp/${sess}-output.txt"
    echo "→ Creating headless OpenCode session: $sess (output → $outfile)"
    [[ "$use_occ" == "1" ]] || { echo 'o: occ is required for hardened headless mode' >&2; return 127; }
    tmux new-session -d -s "$sess" -c "$PWD" \
      sh -c 'out="$1"; shift; exec "$@" >"$out" 2>&1' sh "$outfile" \
      "${env_args[@]}" occ "$prompt_text" ${agent_args[@]+"${agent_args[@]}"}
    _o_register_role "$sess" "$role" "$feature"
    return 0
  fi

  # Interactive TUI args: --agent picks the primary agent; `o name --prompt` is
  # a one-shot (handled above), so no prompt flag reaches the TUI from here —
  # callers wanting a seeded interactive TUI forward opencode's own --prompt
  # through the pass-through args.
  local tui_args=()
  (( ${#agent_args[@]} )) && tui_args+=("${agent_args[@]}")
  (( ${#opencode_args[@]} )) && tui_args+=("${opencode_args[@]}")

  if [[ -n "${TMUX:-}" && "$detach" == "0" ]]; then
    command "$opencode_bin" ${tui_args[@]+"${tui_args[@]}"}
    return $?
  fi

  local sess
  if [[ "$attach" == "1" ]] && _o_has_session "$base"; then
    echo "→ Attaching to existing session: $base"
    _o_register_recovery "$base"
    _o_bind_recovery "$base"
    tmux attach-session -t "$(_o_tmux_target "$base")"
    return $?
  fi

  sess="$(_o_next_session "$base")"
  if [[ "$detach" == "1" ]]; then
    echo "→ Creating new OpenCode session: $sess" >&2
  else
    echo "→ Creating new OpenCode session: $sess"
  fi
  _o_register_recovery "$sess"
  local create_rc=0
  if [[ "$detach" == "1" ]]; then
    tmux new-session -d -s "$sess" -c "$PWD" \
      "${env_args[@]}" "$opencode_bin" ${tui_args[@]+"${tui_args[@]}"} || create_rc=$?
  else
    # OpenTUI probes the real terminal during process birth. If OpenCode starts
    # in the detached gap before `tmux attach-session`, those replies are lost
    # and the TUI remains permanently blank. Keep the pane alive as a tiny gate
    # and exec OpenCode only after tmux reports an attached human client. This
    # also covers o-ubuntu: its SSH PTY becomes that client on remote attach.
    tmux new-session -d -s "$sess" -c "$PWD" \
      sh -c 'session="$1"
shift
i=0
while [ "$i" -lt 600 ]; do
  attached="$(tmux display-message -p -t "=$session:" "#{session_attached}" 2>/dev/null || true)"
  case "$attached" in ""|0) ;; *) exec "$@" ;; esac
  sleep 0.05
  i=$((i + 1))
done
printf "o: timed out waiting for an attached tmux client\n" >&2
exit 70' sh "$sess" "${env_args[@]}" "$opencode_bin" ${tui_args[@]+"${tui_args[@]}"} || create_rc=$?
  fi
  if [[ "$create_rc" -ne 0 ]]; then
    echo "o: failed to create tmux session '$sess'" >&2
    return 1
  fi
  _o_bind_recovery "$sess"
  local pane
  pane="$(tmux display-message -p -t "$(_o_tmux_target "$sess"):" '#{pane_id}' 2>/dev/null)"
  if [[ "$detach" == "1" ]]; then
    command agent-tab-status idle "$pane" 2>/dev/null || true
  fi
  _o_register_role "$sess" "$role" "$feature"
  if [[ "$detach" == "1" ]]; then
    printf '%s\n' "$sess"
    return 0
  fi
  tmux attach-session -t "$(_o_tmux_target "$sess")"
}

# Run the same launcher on Ubuntu, mapping the current directory below local
# ~/Dev to the closest corresponding directory below Ubuntu's ~/dev.
unalias o-ubuntu 2>/dev/null || true
o-ubuntu() {
  command -v gpu-agent >/dev/null 2>&1 || { echo "fleet not configured (gpu-agent missing)" >&2; return 1; }
  command gpu-agent --map-dev-cwd o "$@"
}
