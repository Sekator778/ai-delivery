#!/usr/bin/env bash
# install-autostart.sh — make the bot auto-start on WSL boot.
#
# Installs a systemd system service that runs bot/start.sh whenever WSL2
# boots with systemd enabled. Combined with the Windows-side Task Scheduler
# entry (see docs/WSL2-NOTES.md), this gives full reboot survival:
#
#   Windows reboot
#     -> Windows logon
#     -> Task Scheduler: wsl.exe -d Ubuntu --exec true   (launches WSL VM)
#     -> WSL boots with systemd=true
#     -> systemd starts docker.service
#     -> Docker starts containers with restart: unless-stopped
#     -> systemd starts claude-tg-bot.service
#     -> bot.py polls Telegram, ready for messages
#
# Re-running this script is safe — it detects existing install and skips.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/projects/ai-delivery}"
SERVICE_NAME="claude-tg-bot.service"
SERVICE_SRC="$REPO_DIR/scripts/$SERVICE_NAME"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME"

step() { printf "\n\033[1;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m  %s\n" "$*"; }
skip() { printf "  \033[33mskip\033[0m %s\n" "$*"; }
die()  { printf "  \033[31mfail\033[0m %s\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && sudo="" || sudo="sudo"

# ---- step 1: verify prerequisites -----------------------------------------
step "Verifying prerequisites"
[[ -f "$SERVICE_SRC" ]] || die "service file missing: $SERVICE_SRC"
[[ -x "$REPO_DIR/bot/start.sh" ]] || die "bot/start.sh missing or not executable"
pgrep -x systemd >/dev/null || die "systemd not active (apply [boot] systemd=true in /etc/wsl.conf and wsl --shutdown first)"
ok "systemd active, repo + start.sh present"

# ---- step 2: install service file -----------------------------------------
step "Installing systemd unit"
if [[ -f "$SERVICE_DST" ]] && cmp -s "$SERVICE_SRC" "$SERVICE_DST"; then
    skip "$SERVICE_DST already up-to-date"
else
    $sudo install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
    ok "copied $SERVICE_SRC -> $SERVICE_DST"
    $sudo systemctl daemon-reload
    ok "systemd daemon reloaded"
fi

# ---- step 3: enable + start -----------------------------------------------
step "Enabling + starting service"
if $sudo systemctl is-enabled "$SERVICE_NAME" >/dev/null 2>&1; then
    skip "$SERVICE_NAME already enabled"
else
    $sudo systemctl enable "$SERVICE_NAME"
    ok "$SERVICE_NAME enabled for boot"
fi

# Only start the service if no bot.py is already running outside systemd
# (e.g., user launched manually). Avoid double-spawn + port conflict.
if pgrep -fa 'python3.*bot.py' | grep -qv 'pgrep'; then
    skip "bot.py already running outside systemd — leaving as-is. Run \`sudo systemctl restart $SERVICE_NAME\` after stopping the manual one."
else
    if $sudo systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
        skip "service already active"
    else
        $sudo systemctl start "$SERVICE_NAME"
        ok "$SERVICE_NAME started"
    fi
fi

# ---- step 4: summary ------------------------------------------------------
step "Status"
$sudo systemctl status "$SERVICE_NAME" --no-pager | head -8 || true
echo ""
echo "Next steps for full reboot survival:"
echo "  - On Windows: register a Task Scheduler task to run on user logon:"
echo "      Action: wsl.exe -d Ubuntu --exec true"
echo "      Trigger: At log on of the current user"
echo "      Settings: Run only when user is logged on; Do not stop"
echo "    (see docs/WSL2-NOTES.md for step-by-step XML)"
echo "  - Verify after reboot: 'systemctl status $SERVICE_NAME' should be active."
