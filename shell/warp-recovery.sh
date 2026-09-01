# claudemaxxing — exact Warp terminal recovery (SOURCE this file, don't run it).
#
# Recovery is armed while the user's rc file is sourced, then claimed from the
# first prompt hook. This ordering is load-bearing: replacing the shell from an
# rc file prevents Warp from receiving its Bootstrapped/Precmd frames and leaves
# the restored editor visible but unable to deliver input to a live session.

_warp_agent_recovery_preexec() {
  # A prompt callback is not a user-submitted command, so Warp's normal preexec
  # hook does not run automatically. Without this transition Warp keeps focus in
  # its command editor even though tmux owns a live, read-write PTY.
  local descriptor='warp-recovery attach'
  if [[ -n "${BASH_VERSION:-}" ]]; then
    declare -F warp_preexec >/dev/null 2>&1 || return 1
    warp_preexec "$descriptor" "$descriptor"
  elif [[ -n "${ZSH_VERSION:-}" ]]; then
    (( ${+functions[warp_preexec]} )) || return 1
    warp_preexec "$descriptor" "$descriptor"
  else
    return 1
  fi
}

_warp_agent_recovery_on_prompt() {
  [[ -n "${_WARP_AGENT_RECOVERY_ARMED:-}" ]] || return 0
  [[ -z "${_WARP_AGENT_RECOVERY_CLAIMED:-}" ]] || return 0

  # Do not consume the generation claim until Warp's own execution transition
  # is available. This also makes a partially-installed recovery retryable.
  if [[ -n "${BASH_VERSION:-}" ]]; then
    declare -F warp_preexec >/dev/null 2>&1 || return 0
  elif [[ -n "${ZSH_VERSION:-}" ]]; then
    (( ${+functions[warp_preexec]} )) || return 0
  else
    return 0
  fi

  # Warp appends its Zsh precmd hook after hooks loaded from .zshrc. Emit that
  # ordered frame before replacing this shell. Bash preserves PROMPT_COMMAND as
  # user_prompt_command and runs it after warp_precmd, so no duplicate is needed.
  if [[ -n "${ZSH_VERSION:-}" ]] && (( ${+functions[warp_precmd]} )); then
    warp_precmd
  fi

  local result prefix session mode remainder
  result="$(command warp-agent-recovery claim 2>/dev/null)" || return 0
  prefix="$(printf 'attach-ubuntu\t')"
  case "$result" in
    "$prefix"*)
      remainder="${result#"$prefix"}"
      mode="${remainder%%$'\t'*}"
      session="${remainder#*$'\t'}"
      case "$mode:$session" in
        c:claude-*|g:codex-*) ;;
        *) return 0 ;;
      esac
      case "$session" in
        ''|*[!A-Za-z0-9_.-]*) return 0 ;;
      esac
      _warp_agent_recovery_preexec || return 0
      _WARP_AGENT_RECOVERY_CLAIMED=1
      export _WARP_AGENT_RECOVERY_CLAIMED
      command gpu-agent attach "$mode" "$session"
      return $?
      ;;
  esac

  prefix="$(printf 'attach\t')"
  case "$result" in
    "$prefix"*) session="${result#"$prefix"}" ;;
    *) return 0 ;;
  esac

  case "$session" in
    claude-*|codex-*|opencode-*) ;;
    *) return 0 ;;
  esac
  case "$session" in
    ''|*[!A-Za-z0-9_.-]*) return 0 ;;
  esac

  _warp_agent_recovery_preexec || return 0
  _WARP_AGENT_RECOVERY_CLAIMED=1
  export _WARP_AGENT_RECOVERY_CLAIMED
  exec tmux attach-session -t "=$session"
}

_warp_agent_recovery_arm() {
  case "$-" in
    *i*) ;;
    *) return 0 ;;
  esac

  [[ -z "${TMUX:-}" ]] || return 0
  [[ "${WARP_IS_LOCAL_SHELL_SESSION:-}" == "1" ]] || return 0
  [[ "${WARP_TERMINAL_SESSION_UUID:-}" =~ ^[[:xdigit:]]{32}$ ]] || return 0
  [[ -z "${_WARP_AGENT_RECOVERY_CLAIMED:-}" ]] || return 0
  command -v warp-agent-recovery >/dev/null 2>&1 || return 0

  _WARP_AGENT_RECOVERY_ARMED=1
  if [[ -n "${BASH_VERSION:-}" ]]; then
    if declare -p PROMPT_COMMAND 2>/dev/null | grep -q '^declare -a'; then
      PROMPT_COMMAND+=(_warp_agent_recovery_on_prompt)
    elif [[ -n "${PROMPT_COMMAND:-}" ]]; then
      PROMPT_COMMAND="${PROMPT_COMMAND}"$'\n''_warp_agent_recovery_on_prompt'
    else
      PROMPT_COMMAND='_warp_agent_recovery_on_prompt'
    fi
  elif [[ -n "${ZSH_VERSION:-}" ]]; then
    typeset -ga precmd_functions
    precmd_functions+=(_warp_agent_recovery_on_prompt)
  else
    _WARP_AGENT_RECOVERY_ARMED=
  fi
}

_warp_agent_recovery_arm
unset -f _warp_agent_recovery_arm 2>/dev/null || true
