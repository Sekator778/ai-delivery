#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# Make botctl-* scripts callable by short name from meta-Claude's Bash
# tool (it inherits PATH from the bot.py subprocess, which inherits from
# this shell). Without this, bare `botctl-get-state` falls back to "command
# not found" — meta then has to use full paths in every Bash invocation.
export PATH="$HOME/.claude-tg-bot/bin:$PATH"

if [[ -f .env ]]; then
    set -a
    source .env
    set +a
else
    echo "create bot/.env from bot/.env.example first" >&2
    exit 1
fi

if [[ ! -d venv ]]; then
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

exec python3 bot.py
