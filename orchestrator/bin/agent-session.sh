#!/usr/bin/env bash
# agent-session.sh — Claude Code tmux session manager
# Linked to Hermes kanban tasks for the orchestrator workflow.
#
# Usage:
#   agent-session.sh start <task-id> <project-dir> [--interactive] [--prompt "task description"]
#   agent-session.sh status
#   agent-session.sh logs <task-id>
#   agent-session.sh send <task-id> "follow-up prompt"
#   agent-session.sh stop <task-id>
#   agent-session.sh stop-all

set -euo pipefail

OUTPUT_DIR="${HOME}/.hermes/agent-output"
mkdir -p "$OUTPUT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[agent-session]${NC} $*"; }
err() { echo -e "${RED}[agent-session]${NC} $*" >&2; }
ok()  { echo -e "${GREEN}[agent-session]${NC} $*"; }

# --- Commands ---

cmd_start() {
    local task_id="$1"
    local project_dir="$2"
    shift 2

    local interactive=false
    local prompt=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --interactive) interactive=true; shift ;;
            --prompt) prompt="$2"; shift 2 ;;
            *) err "Unknown option: $1"; exit 1 ;;
        esac
    done

    if [ -z "$task_id" ] || [ -z "$project_dir" ]; then
        err "Usage: start <task-id> <project-dir> [--interactive] [--prompt \"desc\"]"
        exit 1
    fi

    if [ ! -d "$project_dir" ]; then
        err "Project directory does not exist: $project_dir"
        exit 1
    fi

    local session_name="task-${task_id}"

    # Check if session already exists
    if tmux has-session -t "$session_name" 2>/dev/null; then
        err "Session already exists: $session_name"
        exit 1
    fi

    # Update kanban status to in_progress
    hermes kanban edit "$task_id" --status in_progress 2>/dev/null || true

    if [ "$interactive" = true ]; then
        # Interactive mode: launch Claude Code in tmux
        tmux new-session -d -s "$session_name" -x 140 -y 40 \
            "cd '$project_dir' && claude --dangerously-skip-permissions"

        # Answer the trust dialog (press Enter for "Yes, I trust this folder")
        sleep 4
        tmux send-keys -t "$session_name" Enter

        # Answer the permissions dialog (Down then Enter for "Yes, I accept")
        sleep 3
        tmux send-keys -t "$session_name" Down
        sleep 0.3
        tmux send-keys -t "$session_name" Enter

        # Send the task prompt if provided
        if [ -n "$prompt" ]; then
            sleep 2
            tmux send-keys -t "$session_name" "$prompt" Enter
        fi

        ok "Started interactive session: $session_name (project: $project_dir)"
    else
        # Print mode: one-shot task
        local output_file="${OUTPUT_DIR}/${task_id}.json"
        local claude_prompt="${prompt:-$(hermes kanban show "$task_id" 2>/dev/null | head -20)}"

        tmux new-session -d -s "$session_name" -x 140 -y 40 \
            "cd '$project_dir' && claude -p \"$claude_prompt\" --max-turns 10 --output-format json --dangerously-skip-permissions > '$output_file' 2>&1; echo 'EXIT_CODE:' \$? >> '$output_file'"

        ok "Started print-mode session: $session_name (output: $output_file)"
    fi

    echo "$session_name"
}

cmd_status() {
    local sessions
    sessions=$(tmux ls 2>/dev/null || echo "")

    if [ -z "$sessions" ]; then
        log "No active agent sessions"
        return 0
    fi

    printf "%-30s %-10s %-10s %-15s\n" "SESSION" "WINDOWS" "ATTACHED" "CREATED"
    printf "%-30s %-10s %-10s %-15s\n" "-------" "-------" "--------" "-------"

    while IFS= read -r line; do
        local name created windows attached
        name=$(echo "$line" | cut -d: -f1)
        created=$(tmux display-message -p -t "$name" '#{session_created}' 2>/dev/null || echo "?")
        windows=$(tmux display-message -p -t "$name" '#{session_windows}' 2>/dev/null || echo "?")
        attached=$(tmux display-message -p -t "$name" '#{session_attached}' 2>/dev/null || echo "?")
        printf "%-30s %-10s %-10s %-15s\n" "$name" "$windows" "$attached" "$(date -d "@$created" '+%H:%M:%S' 2>/dev/null || echo "$created")"
    done <<< "$sessions"
}

cmd_logs() {
    local task_id="$1"
    local session_name="task-${task_id}"

    if ! tmux has-session -t "$session_name" 2>/dev/null; then
        # Check for output file
        local output_file="${OUTPUT_DIR}/${task_id}.json"
        if [ -f "$output_file" ]; then
            log "Session ended. Output file: $output_file"
            cat "$output_file"
            return 0
        fi
        err "No session or output for: $task_id"
        exit 1
    fi

    tmux capture-pane -t "$session_name" -p -S -50
}

cmd_send() {
    local task_id="$1"
    local prompt="$2"
    local session_name="task-${task_id}"

    if ! tmux has-session -t "$session_name" 2>/dev/null; then
        err "No active session: $session_name"
        exit 1
    fi

    tmux send-keys -t "$session_name" "$prompt" Enter
    ok "Sent prompt to $session_name: ${prompt:0:80}..."
}

cmd_stop() {
    local task_id="$1"
    local session_name="task-${task_id}"

    if tmux has-session -t "$session_name" 2>/dev/null; then
        tmux kill-session -t "$session_name"
        ok "Stopped session: $session_name"
    else
        log "No active session: $session_name"
    fi

    # Update kanban status
    hermes kanban complete "$task_id" 2>/dev/null || true
}

cmd_stop_all() {
    local sessions
    sessions=$(tmux ls -F '#{session_name}' 2>/dev/null | grep '^task-' || echo "")

    if [ -z "$sessions" ]; then
        log "No agent sessions to stop"
        return 0
    fi

    while IFS= read -r name; do
        tmux kill-session -t "$name" 2>/dev/null || true
        ok "Stopped: $name"
    done <<< "$sessions"
}

# --- Main ---

case "${1:-}" in
    start)   shift; cmd_start "$@" ;;
    status)  cmd_status ;;
    logs)    shift; cmd_logs "$@" ;;
    send)    shift; cmd_send "$@" ;;
    stop)    shift; cmd_stop "$@" ;;
    stop-all) cmd_stop_all ;;
    *)
        echo "Usage: agent-session.sh {start|status|logs|send|stop|stop-all}"
        echo ""
        echo "Commands:"
        echo "  start <task-id> <project-dir> [--interactive] [--prompt \"desc\"]"
        echo "  status                    — show all agent sessions"
        echo "  logs <task-id>            — show output from a session"
        echo "  send <task-id> \"prompt\"   — send follow-up to interactive session"
        echo "  stop <task-id>            — stop a session and mark task done"
        echo "  stop-all                  — stop all agent sessions"
        exit 1
        ;;
esac