#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/logoscope}"
TASK="${1:-dev}"
SESSION_ID="${SESSION_ID:-codex-$(date +%F-%H%M)-$TASK}"
LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/.runs/$SESSION_ID"

mkdir -p "$LOG_DIR" "$RUN_DIR"

cd "$PROJECT_DIR"

cat > "$RUN_DIR/meta.env" <<META
SESSION_ID=$SESSION_ID
TASK=$TASK
HOSTNAME=$(hostname)
USER=$(whoami)
PROJECT=$PROJECT_DIR
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
START_AT=$(date -Iseconds)
META

if ! tmux has-session -t "$SESSION_ID" 2>/dev/null; then
  tmux new -d -s "$SESSION_ID"
fi

tmux pipe-pane -o -t "$SESSION_ID" "cat >> $LOG_DIR/$SESSION_ID.log"

tmux send-keys -t "$SESSION_ID" "cd $PROJECT_DIR" C-m
tmux send-keys -t "$SESSION_ID" "echo '[SESSION] $SESSION_ID  TASK=$TASK  START=' \$(date -Iseconds)" C-m
tmux send-keys -t "$SESSION_ID" "git status -sb || true" C-m

echo "SESSION_ID=$SESSION_ID"
echo "LOG_FILE=$LOG_DIR/$SESSION_ID.log"
echo "RUN_DIR=$RUN_DIR"
echo
echo "Attach: tmux attach -t $SESSION_ID"
echo "Send cmd: tmux send-keys -t $SESSION_ID '<your command>' C-m"
