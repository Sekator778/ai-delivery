# shellcheck shell=bash
# botctl-common.sh — sourced library for botctl-* scripts
# Callers must set -euo pipefail before sourcing.

BOT_DIR="$HOME/.claude-tg-bot"
STATE_FILE="$BOT_DIR/state.json"

load_env() {
  local env_file="$HOME/claude-telegram-bot/.env"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  fi
}

state_get() {
  local path="$1"
  jq -r ".${path} // empty" "$STATE_FILE" 2>/dev/null
}

target_chat_id() {
  local chat_id
  chat_id=$(state_get "last_chat_id")
  if [[ -z "$chat_id" ]]; then
    echo "ERROR: last_chat_id not set in $STATE_FILE" >&2
    exit 1
  fi
  echo "$chat_id"
}

tg_api() {
  local method="$1"
  shift
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/${method}" "$@"
}
