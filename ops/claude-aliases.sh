# shellcheck shell=bash
# Claude Code backend aliases for WSL Ubuntu (zsh/bash compatible).
# Mirror of the Windows PowerShell profile functions
# (Use-ClaudeDeepSeek / claude-anthropic).
#
# Source this file from ~/.zshrc and/or ~/.bashrc:
#   [ -f "$HOME/.claude-aliases.sh" ] && . "$HOME/.claude-aliases.sh"
#
# To rotate the DeepSeek key: edit the canonical copy at ~/.claude-aliases.sh
# (NOT this file inside the repo). The install script copies repo -> $HOME,
# but the runtime copy is read at every shell startup.

claude-deepseek() {
    : "${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY not set — put it in ~/.claude-aliases.sh on the host (not this repo copy)}"
    printf '\033[36m[DeepSeek V4 Pro]\033[0m\n' >&2
    ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
    ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
    ANTHROPIC_MODEL="deepseek-v4-pro" \
    ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro" \
    ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash" \
    CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash" \
        claude "$@"
}

claude-anthropic() {
    # Force-unset any DeepSeek overrides that might leak from .env or prior
    # invocations in this shell, then run plain claude (uses Max OAuth login).
    unset ANTHROPIC_BASE_URL \
          ANTHROPIC_AUTH_TOKEN \
          ANTHROPIC_MODEL \
          ANTHROPIC_DEFAULT_SONNET_MODEL \
          ANTHROPIC_DEFAULT_HAIKU_MODEL \
          CLAUDE_CODE_SUBAGENT_MODEL
    printf '\033[35m[Claude Max]\033[0m\n' >&2
    claude "$@"
}
