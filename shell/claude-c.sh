# orchestratormaxxing — `c` / `cs` Claude Code tmux session helpers (SOURCE this file, don't run it).
# Source of truth: <repo>/shell/claude-c.sh — edit here, then `./install.sh` re-syncs it to
# ~/.config/orchestratormaxxing/claude-c.sh and (idempotently) makes ~/.bashrc + ~/.zshrc source it.
# Portable across bash AND zsh: avoids zsh-reserved locals (`path` is tied to $PATH, `status`
# is read-only) and never assumes a 0- vs 1-indexed array base (counter-based selection).
#
# claude shortcut: `c` runs claude with permissions skipped; forwards any flags (e.g. `c -r`, `c --resume`, `c "prompt"`)
# Claude Code launcher with tmux session management
# Creates a detached tmux session named `claude-<project>` so the
# Hermes orchestrator dashboard can detect it and interact with it.
#
# By default each `c` in a folder opens a NEW, independent session — if
# `claude-<name>` is taken it auto-numbers (`claude-<name>-2`, `-3`, …), so a
# folder can run several distinct terminals. Use -a/--attach to reattach.
#
# Usage:
#   c [name]              → new interactive TUI session (auto-numbered if taken)
#   c [name] -a|--attach  → reattach to existing claude-<name> (create if none)
#   c [name] --detach     → create the interactive session and print its name, without attaching
#   c [name] --headless   → headless mode (JSON output, non-interactive, in tmux)
#   c [name] --prompt "..."  → one-shot prompt, no tmux
#   c ls | -l | --list    → list active claude-* tmux sessions (numbered)
#   c ls <number|name>    → attach to a listed session (also: cs, cs <number|name>)

# Resolve a session NAME to an exact tmux TARGET. The leading '=' disables tmux
# prefix matching, so `claude-app` can never resolve to `claude-app-2`. Without it
# `has-session -t claude-app` answers TRUE while only `claude-app-2` exists, and
# the attach that follows (which is exact) then fails with "can't find session".
# Mirrors _g_tmux_target in codex-g.sh. NB: `new-session -s` takes a NAME, not a
# target — never wrap that one.
_c_tmux_target() {
  printf '=%s' "$1"
}

# List / attach helper for `c -l` (and the `cs` alias). Lists active
# `claude-*` tmux sessions with attach status + working dir, or attaches to one
# by list-number or name. Kept standalone so both `c -l` and `cs` reuse it.
_c_list_sessions() {
  local target="${1:-}"

  # Gather active claude-* sessions (stable sort so numbers are consistent).
  local sessions=()
  local _s
  while IFS= read -r _s; do
    [[ -n "$_s" ]] && sessions+=("$_s")
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^claude-' | sort)

  if [[ ${#sessions[@]} -eq 0 ]]; then
    echo "No active claude-* tmux sessions."
    return 1
  fi

  # A target was given → resolve to a session and attach/switch.
  if [[ -n "$target" ]]; then
    local sess=""
    if [[ "$target" =~ ^[0-9]+$ ]]; then
      # Counter-based selection so this behaves identically in bash (0-indexed
      # arrays) and zsh (1-indexed) — no array-base assumption.
      local _i=1
      for _s in "${sessions[@]}"; do
        [[ "$_i" == "$target" ]] && { sess="$_s"; break; }
        _i=$((_i + 1))
      done
      if [[ -z "$sess" ]]; then
        echo "No session #$target (valid: 1-${#sessions[@]}). Run 'cs' to list."
        return 1
      fi
    else
      # Accept both 'foo' and 'claude-foo'.
      local want="$target"
      [[ "$want" != claude-* ]] && want="claude-$want"
      if tmux has-session -t "$(_c_tmux_target "$want")" 2>/dev/null; then
        sess="$want"
      else
        echo "No session named '$target' (tried '$want'). Run 'cs' to list."
        return 1
      fi
    fi
    if [[ -n "${TMUX:-}" ]]; then
      echo "→ Switching to $sess"
      tmux switch-client -t "$(_c_tmux_target "$sess")"
    else
      echo "→ Attaching to $sess"
      _c_register_recovery "$sess"
      _c_bind_recovery "$sess"
      exec tmux attach-session -t "$(_c_tmux_target "$sess")"
    fi
    return 0
  fi

  # No target → print the numbered list.
  # NB: avoid zsh special vars — `path` (tied to $PATH) and `status` (read-only)
  # would break the shared copy of this helper in zsh; use cwd/st instead.
  echo "Active Claude Code sessions:"
  local i=1 att cwd st
  for _s in "${sessions[@]}"; do
    # The ':' is load-bearing: display-message wants a *pane* target, so an exact
    # '=session' target with no ':' resolves to nothing — and tmux still exits 0,
    # so every session would silently read as [detached] with an unknown cwd.
    att="$(tmux display-message -p -t "$(_c_tmux_target "$_s"):" '#{session_attached}' 2>/dev/null)"
    cwd="$(tmux display-message -p -t "$(_c_tmux_target "$_s"):" '#{pane_current_path}' 2>/dev/null)"
    if [[ -z "$att" || "$att" == "0" ]]; then st="detached"; else st="attached"; fi
    printf '  %2d) %-28s [%s]  %s\n' "$i" "$_s" "$st" "${cwd:-?}"
    i=$((i + 1))
  done
  echo
  echo "Attach:  c ls <number|name>   (or: cs <number|name>)"
  return 0
}

_c_register_recovery() {
  local session="$1"
  [[ -z "${TMUX:-}" ]] || return 0
  [[ "${WARP_IS_LOCAL_SHELL_SESSION:-}" == "1" ]] || return 0
  [[ "${WARP_TERMINAL_SESSION_UUID:-}" =~ ^[[:xdigit:]]{32}$ ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0
  command warp-agent-recovery register-launch claude "$session" "$PWD" >/dev/null 2>&1 || true
}

_c_bind_recovery() {
  local session="$1"
  [[ -z "${TMUX:-}" ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0
  command warp-agent-recovery bind-tmux claude "$session" >/dev/null 2>&1 || true
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

c() {
  # ls / list / -l / --list: list & attach to active claude-* sessions
  # (intercept handled before the positional `name` is consumed).
  case "${1:-}" in
    ls|list|-l|--list)
      shift
      _c_list_sessions "$@"
      return $?
      ;;
  esac

  local name="${1:-}"
  shift 2>/dev/null || true
  local mode="interactive"
  local prompt_text=""
  local attach=0
  local detach=0
  local role=""
  local feature=""
  local claude_args=()

  # Warp otherwise replaces OSC titles with its cwd/process-derived title.
  # This export affects the current Warp tab before we attach tmux.
  export WARP_DISABLE_AUTO_TITLE=true

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --headless)      mode="headless"; shift ;;
      --prompt)        mode="prompt"; prompt_text="$2"; shift 2 ;;
      -a|--attach)     attach=1; shift ;;
      -n|--new)        attach=0; shift ;;
      --detach)        detach=1; shift ;;
      -R|--role)       role="$2"; shift 2 ;;   # implementation|verification|docs|planning|review
      -F|--feature)    feature="$2"; shift 2 ;;
      *)               claude_args+=("$1"); shift ;;
    esac
  done
  if [[ "$attach" == "1" && "$detach" == "1" ]]; then
    echo "c: --attach and --detach are mutually exclusive" >&2
    return 2
  fi

  # If no name given, use cwd basename
  if [[ -z "$name" ]]; then
    name="$(basename "$(pwd)")"
  fi

  # Role-based session naming (feature 1): claude-<name>-<role>. The dashboard
  # parses the role straight off the tmux name, so this alone is enough; the
  # best-effort register below adds the feature link when the dashboard is up.
  local base="claude-$name"
  if [[ -n "$role" ]]; then base="claude-$name-$role"; fi
  _c_register_role() {
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
    payload="$(python3 - "$1" "$role" "$feature" <<'PY'
import json, sys
print(json.dumps({"session_key": sys.argv[1], "role": sys.argv[2], "feature": sys.argv[3]}))
PY
)"
    local curl_args=(-fsS -m 2 -X POST "$url/api/session-meta" -H 'Content-Type: application/json')
    [[ -z "$token" ]] || curl_args+=(-H "Authorization: Bearer $token")
    curl "${curl_args[@]}" --data-binary "$payload" >/dev/null 2>&1 || true
    if [[ -n "$feature" ]]; then
      link_payload="$(python3 - "$1" <<'PY'
import json, sys
print(json.dumps({"session_id": sys.argv[1]}))
PY
)"
      curl_args=(-fsS -m 2 -X PATCH "$url/api/tasks/$feature/session" -H 'Content-Type: application/json')
      [[ -z "$token" ]] || curl_args+=(-H "Authorization: Bearer $token")
      curl "${curl_args[@]}" --data-binary "$link_payload" >/dev/null 2>&1 || true
    fi
  }

  # Next free session name: base, else base-2, base-3, …
  _c_next_session() {
    if ! tmux has-session -t "$(_c_tmux_target "$1")" 2>/dev/null; then echo "$1"; return; fi
    local n=2
    while tmux has-session -t "$(_c_tmux_target "${1}-${n}")" 2>/dev/null; do n=$((n+1)); done
    echo "${1}-${n}"
  }

  if [[ "$mode" == "prompt" ]]; then
    claude --dangerously-skip-permissions --output-format json -p "$prompt_text" 2>/dev/null
    return $?
  fi

  if [[ "$mode" == "headless" ]]; then
    local sess
    if [[ "$attach" == "1" ]] && tmux has-session -t "$(_c_tmux_target "$base")" 2>/dev/null; then
      echo "→ Session $base already exists. Use 'tmux attach -t $base' to view."
      return 0
    fi
    sess="$(_c_next_session "$base")"
    local outfile="/tmp/claude-${sess#claude-}-output.json"
    echo "→ Creating headless session: $sess (output → $outfile)"
    tmux new-session -d -s "$sess" \
      "claude --dangerously-skip-permissions --output-format json 2>&1 | tee $outfile"
    _c_register_role "$sess"
    return 0
  fi

  # Interactive (default)
  # A c invocation inside tmux is also the exact crash-recovery entry path.
  # Fresh sessions already receive this setting below; apply it to the direct
  # Linux path too so recovered/nested Ubuntu sessions keep their transcript
  # in tmux history. Keep Darwin's established nested behavior unchanged.
  local linux_normal_screen=0
  [[ "$(uname -s 2>/dev/null)" == "Linux" ]] && linux_normal_screen=1
  if [[ -n "${TMUX:-}" && "$detach" == "0" ]]; then
    if [[ "$linux_normal_screen" == "1" ]]; then
      CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 \
        command claude --dangerously-skip-permissions "${claude_args[@]}"
    else
      command claude --dangerously-skip-permissions "${claude_args[@]}"
    fi
    return
  fi

  local sess
  if [[ "$attach" == "1" ]] && tmux has-session -t "$(_c_tmux_target "$base")" 2>/dev/null; then
    echo "→ Attaching to existing session: $base"
    _c_register_recovery "$base"
    _c_bind_recovery "$base"
    exec tmux attach-session -t "$(_c_tmux_target "$base")"
  fi

  # Default: a fresh, independent session (auto-numbered if the base is taken).
  sess="$(_c_next_session "$base")"
  if [[ "$detach" == "1" ]]; then
    echo "→ Creating new Claude Code session: $sess" >&2
  else
    echo "→ Creating new Claude Code session: $sess"
  fi
  local claude_bin
  claude_bin="$(command -v claude 2>/dev/null)" || {
    echo "c: claude is not installed or not on PATH" >&2
    return 127
  }
  _c_register_recovery "$sess"
  local env_args=(env "PATH=$PATH")
  [[ -z "${WARP_TERMINAL_SESSION_UUID:-}" ]] || env_args+=("WARP_TERMINAL_SESSION_UUID=$WARP_TERMINAL_SESSION_UUID")
  [[ -z "${WARP_IS_LOCAL_SHELL_SESSION:-}" ]] || env_args+=("WARP_IS_LOCAL_SHELL_SESSION=$WARP_IS_LOCAL_SHELL_SESSION")
  [[ -z "${WARP_CLIENT_VERSION:-}" ]] || env_args+=("WARP_CLIENT_VERSION=$WARP_CLIENT_VERSION")
  [[ -z "${WARP_FOCUS_URL:-}" ]] || env_args+=("WARP_FOCUS_URL=$WARP_FOCUS_URL")
  env_args+=("WARP_DISABLE_AUTO_TITLE=$WARP_DISABLE_AUTO_TITLE")
  # Warp currently has selection/OSC52 regressions in fullscreen TUIs. Keep
  # Claude's transcript in tmux scrollback so long selections are not limited
  # to the visible alternate-screen viewport. `/copy` remains available for an
  # exact assistant response.
  env_args+=("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1")
  if ! tmux new-session -d -s "$sess" -c "$PWD" \
    "${env_args[@]}" "$claude_bin" --dangerously-skip-permissions "${claude_args[@]}"; then
    echo "c: failed to create tmux session '$sess'" >&2
    return 1
  fi
  _c_bind_recovery "$sess"
  local pane
  pane="$(tmux display-message -p -t "$(_c_tmux_target "$sess"):" '#{pane_id}' 2>/dev/null)"
  command agent-tab-status idle "$pane" 2>/dev/null || true
  _c_register_role "$sess"
  if [[ "$detach" == "1" ]]; then
    printf '%s\n' "$sess"
    return 0
  fi
  exec tmux attach-session -t "$(_c_tmux_target "$sess")"
}

# cs = "c -l": list active Claude Code sessions (or `cs <number|name>` to attach).
cs() { c -l "$@"; }

# Run the same launcher on Ubuntu, mapping the current directory below local
# ~/Dev to the closest corresponding directory below Ubuntu's ~/dev.
unalias c-ubuntu 2>/dev/null || true
c-ubuntu() {
  command -v gpu-agent >/dev/null 2>&1 || { echo "fleet not configured (gpu-agent missing)" >&2; return 1; }
  command gpu-agent --map-dev-cwd c "$@"
}
