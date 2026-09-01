# claudemaxxing — `g` Codex tmux session helper (SOURCE this file, don't run it).
# Source of truth: <repo>/shell/codex-g.sh; `./install.sh` deploys a copy to
# ~/.config/claudemaxxing/codex-g.sh and sources it from bash/zsh startup files.
#
# `g` mirrors the useful lifecycle of `c` while respecting Codex's CLI:
#   g [name]                         new interactive Codex TUI in tmux
#   g [name] -A|--attach            attach/switch to exact codex-<name>
#   g [name] --detach               create it and print its name without attaching
#   g [name] --headless --prompt P  detached `codex exec --json`, output in /tmp
#   g --prompt P                    one-shot `codex exec --json`, no tmux
#   g ls [number|name]              list or attach to codex-* sessions
#
# Unknown flags are forwarded to Codex. `-a` is deliberately NOT an attach alias:
# Codex owns `-a/--ask-for-approval`. Use `-A` for tmux attach. `gs` is not defined
# because it is Ghostscript on common Linux/macOS installations.

_g_tmux_target() {
  # Leading '=' prevents tmux prefix matching (`codex-app` must not match
  # `codex-app-2`).
  printf '=%s' "$1"
}

_g_slug() {
  local slug
  slug="$(printf '%s' "$1" | tr -cs '[:alnum:]_-' '-' | sed -E 's/-+/-/g; s/^-//; s/-$//')"
  printf '%s\n' "${slug:-session}"
}

_g_has_session() {
  tmux has-session -t "$(_g_tmux_target "$1")" 2>/dev/null
}

_g_list_sessions() {
  local target="${1:-}"
  local sessions=()
  local _s
  while IFS= read -r _s; do
    [[ -n "$_s" ]] && sessions+=("$_s")
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^codex-' | sort)

  if [[ ${#sessions[@]} -eq 0 ]]; then
    echo "No active codex-* tmux sessions."
    return 1
  fi

  if [[ -n "$target" ]]; then
    local sess=""
    if [[ "$target" =~ ^[0-9]+$ ]]; then
      local _i=1
      for _s in "${sessions[@]}"; do
        [[ "$_i" == "$target" ]] && { sess="$_s"; break; }
        _i=$((_i + 1))
      done
      if [[ -z "$sess" ]]; then
        echo "No session #$target (valid: 1-${#sessions[@]}). Run 'g ls'."
        return 1
      fi
    else
      local want="$target"
      [[ "$want" != codex-* ]] && want="codex-$want"
      if _g_has_session "$want"; then
        sess="$want"
      else
        echo "No session named '$target' (tried '$want'). Run 'g ls'."
        return 1
      fi
    fi

    if [[ -n "${TMUX:-}" ]]; then
      echo "→ Switching to $sess"
      tmux switch-client -t "$(_g_tmux_target "$sess")"
    else
      echo "→ Attaching to $sess"
      _g_register_recovery "$sess"
      _g_bind_recovery "$sess"
      tmux attach-session -t "$(_g_tmux_target "$sess")"
    fi
    return $?
  fi

  echo "Active Codex sessions:"
  local i=1 att cwd st
  for _s in "${sessions[@]}"; do
    # The ':' is load-bearing: display-message wants a *pane* target, so a bare
    # '=session' resolves to nothing — and tmux still exits 0, so every session
    # silently read as [detached] with an unknown cwd. Keep the '=' (exact match,
    # no prefix matching) and add ':' for the session's current pane.
    att="$(tmux display-message -p -t "$(_g_tmux_target "$_s"):" '#{session_attached}' 2>/dev/null)"
    cwd="$(tmux display-message -p -t "$(_g_tmux_target "$_s"):" '#{pane_current_path}' 2>/dev/null)"
    if [[ -z "$att" || "$att" == "0" ]]; then st="detached"; else st="attached"; fi
    printf '  %2d) %-28s [%s]  %s\n' "$i" "$_s" "$st" "${cwd:-?}"
    i=$((i + 1))
  done
  echo
  echo "Attach: g ls <number|name>"
}

_g_next_session() {
  if ! _g_has_session "$1"; then printf '%s\n' "$1"; return; fi
  local n=2
  while _g_has_session "${1}-${n}"; do n=$((n + 1)); done
  printf '%s-%s\n' "$1" "$n"
}

# Fleet identity reader — identical copy in claude-c.sh / codex-g.sh / opencode-o.sh
# so each installed file stays self-contained. Prints the value of one
# CLAUDEMAXXING_* key from the fleet.env file ($CLAUDEMAXXING_FLEET_ENV, else
# ~/.config/claudemaxxing/fleet.env) WITHOUT sourcing it: only `KEY=VALUE` lines
# whose KEY matches CLAUDEMAXXING_[A-Z_]+ count, an optional `export ` prefix and
# surrounding double quotes are stripped, nothing is expanded, comments/unknown
# keys are ignored, last assignment wins. Missing/unreadable file or absent key
# prints nothing (== not configured). Always returns 0. Portable bash/zsh.
_claudemaxxing_fleet_env() {
  local key="$1" file="${CLAUDEMAXXING_FLEET_ENV:-$HOME/.config/claudemaxxing/fleet.env}"
  local line value="" re='^CLAUDEMAXXING_[A-Z_]+='
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

_g_register_role() {
  local session="$1" role="$2" feature="$3"
  [[ -z "$role" && -z "$feature" ]] && return 0
  # Dashboard base URL: legacy ORCH_DASHBOARD_URL > CLAUDEMAXXING_DASHBOARD_URL (env,
  # else fleet.env) > not configured → no network at all.
  local url="${ORCH_DASHBOARD_URL:-${CLAUDEMAXXING_DASHBOARD_URL:-$(_claudemaxxing_fleet_env CLAUDEMAXXING_DASHBOARD_URL)}}"
  [[ -n "$url" ]] || return 0
  local token="" payload link_payload
  if [[ -n "${HERMES_DASHBOARD_TOKEN+x}" ]]; then
    token="$HERMES_DASHBOARD_TOKEN"
  elif [[ -f "$HOME/.config/claudemaxxing/dashboard-token" ]]; then
    token="$(<"$HOME/.config/claudemaxxing/dashboard-token")"
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

_g_register_recovery() {
  local session="$1"
  [[ -z "${TMUX:-}" ]] || return 0
  [[ "${WARP_IS_LOCAL_SHELL_SESSION:-}" == "1" ]] || return 0
  [[ "${WARP_TERMINAL_SESSION_UUID:-}" =~ ^[[:xdigit:]]{32}$ ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0
  command warp-agent-recovery register-launch codex "$session" "$PWD" >/dev/null 2>&1 || true
}

_g_bind_recovery() {
  local session="$1"
  [[ -z "${TMUX:-}" ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0
  command warp-agent-recovery bind-tmux codex "$session" >/dev/null 2>&1 || true
}

# zsh expands an existing alias while parsing `g()`, which turns the function
# declaration into a syntax error. The harness intentionally owns this command.
unalias g 2>/dev/null || true

g() {
  case "${1:-}" in
    ls|list|-l|--list)
      shift
      _g_list_sessions "$@"
      return $?
      ;;
  esac

  local name=""
  if [[ -n "${1:-}" && "${1:-}" != -* ]]; then
    name="$1"
    shift
  fi

  local headless=0 attach=0 detach=0 prompt_text="" role="" feature=""
  # Warp otherwise replaces OSC titles with its cwd/process-derived title.
  export WARP_DISABLE_AUTO_TITLE=true
  local codex_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --headless) headless=1; shift ;;
      --prompt)
        [[ $# -ge 2 ]] || { echo "g: --prompt requires text" >&2; return 2; }
        prompt_text="$2"; shift 2 ;;
      -A|--attach) attach=1; shift ;;
      -N|--new) attach=0; shift ;;
      --detach) detach=1; shift ;;
      -R|--role)
        [[ $# -ge 2 ]] || { echo "g: --role requires a value" >&2; return 2; }
        role="$2"; shift 2 ;;
      -F|--feature)
        [[ $# -ge 2 ]] || { echo "g: --feature requires a value" >&2; return 2; }
        feature="$2"; shift 2 ;;
      --)
        shift
        while [[ $# -gt 0 ]]; do codex_args+=("$1"); shift; done
        ;;
      *) codex_args+=("$1"); shift ;;
    esac
  done
  if [[ "$attach" == "1" && "$detach" == "1" ]]; then
    echo "g: --attach and --detach are mutually exclusive" >&2
    return 2
  fi

  [[ -n "$name" ]] || name="$(basename "$(pwd)")"
  name="$(_g_slug "$name")"
  local base="codex-$name"
  [[ -n "$role" ]] && base="codex-$name-$role"

  local env_args=(env "PATH=$PATH")
  [[ -z "${CODEX_HOME:-}" ]] || env_args+=("CODEX_HOME=$CODEX_HOME")
  [[ -z "${WARP_TERMINAL_SESSION_UUID:-}" ]] || env_args+=("WARP_TERMINAL_SESSION_UUID=$WARP_TERMINAL_SESSION_UUID")
  [[ -z "${WARP_IS_LOCAL_SHELL_SESSION:-}" ]] || env_args+=("WARP_IS_LOCAL_SHELL_SESSION=$WARP_IS_LOCAL_SHELL_SESSION")
  [[ -z "${WARP_CLIENT_VERSION:-}" ]] || env_args+=("WARP_CLIENT_VERSION=$WARP_CLIENT_VERSION")
  [[ -z "${WARP_FOCUS_URL:-}" ]] || env_args+=("WARP_FOCUS_URL=$WARP_FOCUS_URL")
  env_args+=("WARP_DISABLE_AUTO_TITLE=$WARP_DISABLE_AUTO_TITLE")
  local codex_bin
  codex_bin="$(command -v codex 2>/dev/null)" || {
    echo "g: codex is not installed or not on PATH" >&2
    return 127
  }

  if [[ -n "$prompt_text" && "$headless" == "0" ]]; then
    command "$codex_bin" exec --dangerously-bypass-approvals-and-sandbox \
      --dangerously-bypass-hook-trust --json \
      "${codex_args[@]}" "$prompt_text"
    return $?
  fi

  if [[ "$headless" == "1" ]]; then
    if [[ -z "$prompt_text" ]]; then
      echo "g: --headless requires --prompt <text>" >&2
      return 2
    fi
    local sess outfile
    if [[ "$attach" == "1" ]] && _g_has_session "$base"; then
      echo "→ Session $base already exists. Use 'g ls $base' to view."
      return 0
    fi
    sess="$(_g_next_session "$base")"
    outfile="/tmp/${sess}-output.jsonl"
    echo "→ Creating headless Codex session: $sess (output → $outfile)"
    tmux new-session -d -s "$sess" -c "$PWD" \
      sh -c 'out="$1"; shift; exec "$@" >"$out" 2>&1' sh "$outfile" \
      "${env_args[@]}" "$codex_bin" exec --dangerously-bypass-approvals-and-sandbox \
      --dangerously-bypass-hook-trust --json "${codex_args[@]}" "$prompt_text"
    _g_register_role "$sess" "$role" "$feature"
    return 0
  fi

  # Ubuntu's Warp/tmux selection can copy only the visible viewport from an
  # alternate-screen TUI. Keep interactive Codex output in tmux history on
  # Linux, including the direct-in-tmux crash-recovery path. Mac-local Codex
  # retains its established full-screen TUI; c-ubuntu/g-ubuntu execute here on
  # the Linux host and therefore receive the copy-safe mode remotely too.
  local codex_tui_args=()
  local has_no_alt_screen=0 _g_arg
  for _g_arg in "${codex_args[@]}"; do
    [[ "$_g_arg" == "--no-alt-screen" ]] && has_no_alt_screen=1
  done
  if [[ "$(uname -s 2>/dev/null)" == "Linux" && "$has_no_alt_screen" == "0" ]]; then
    codex_tui_args+=(--no-alt-screen)
  fi

  if [[ -n "${TMUX:-}" && "$detach" == "0" ]]; then
    command "$codex_bin" --dangerously-bypass-approvals-and-sandbox \
      --dangerously-bypass-hook-trust "${codex_tui_args[@]}" "${codex_args[@]}"
    return $?
  fi

  local sess
  if [[ "$attach" == "1" ]] && _g_has_session "$base"; then
    echo "→ Attaching to existing session: $base"
    _g_register_recovery "$base"
    _g_bind_recovery "$base"
    tmux attach-session -t "$(_g_tmux_target "$base")"
    return $?
  fi

  sess="$(_g_next_session "$base")"
  if [[ "$detach" == "1" ]]; then
    echo "→ Creating new Codex session: $sess" >&2
  else
    echo "→ Creating new Codex session: $sess"
  fi
  _g_register_recovery "$sess"
  if ! tmux new-session -d -s "$sess" -c "$PWD" \
    "${env_args[@]}" "$codex_bin" --dangerously-bypass-approvals-and-sandbox \
    --dangerously-bypass-hook-trust "${codex_tui_args[@]}" "${codex_args[@]}"; then
    echo "g: failed to create tmux session '$sess'" >&2
    return 1
  fi
  _g_bind_recovery "$sess"
  local pane
  pane="$(tmux display-message -p -t "$(_g_tmux_target "$sess"):" '#{pane_id}' 2>/dev/null)"
  command agent-tab-status idle "$pane" 2>/dev/null || true
  _g_register_role "$sess" "$role" "$feature"
  if [[ "$detach" == "1" ]]; then
    printf '%s\n' "$sess"
    return 0
  fi
  tmux attach-session -t "$(_g_tmux_target "$sess")"
}

# Run the same launcher on Ubuntu, mapping the current directory below local
# ~/Dev to the closest corresponding directory below Ubuntu's ~/dev.
unalias g-ubuntu 2>/dev/null || true
g-ubuntu() {
  command -v gpu-agent >/dev/null 2>&1 || { echo "fleet not configured (gpu-agent missing)" >&2; return 1; }
  command gpu-agent --map-dev-cwd g "$@"
}
