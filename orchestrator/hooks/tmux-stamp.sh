#!/bin/bash
set -o pipefail   # a piped stage's failure must not be hidden by its last stage
# tmux-stamp.sh — SessionStart hook: stamp the enclosing tmux session with the
# Claude Code session UUID (`tmux set-option @claude-session-id <uuid>`).
#
# THE persistent UUID→tmux mapping the dashboard reads (sessions.py
# _local_tmux_registry): send-input and live-capture stop depending on the
# fragile cwd heuristic, and idle sessions keep their terminal link.
#
# Silent + safe: no tmux → exit 0; no session_id in the hook payload → exit 0.
# Registered globally by hooks/install-hooks.py.
[ -z "$TMUX" ] && exit 0
uuid=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
[ -n "$uuid" ] && tmux set-option "@claude-session-id" "$uuid" 2>/dev/null
exit 0
