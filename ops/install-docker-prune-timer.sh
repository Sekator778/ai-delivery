#!/usr/bin/env bash
# Install the safe weekly Docker prune timer (requires sudo):
#   docker-prune.timer   — fires weekly (Persistent=true, naverstyvaet propusk)
#   docker-prune.service — oneshot runner of scripts/docker-prune.sh
# Repo path / deploy user are computed at runtime, so no host-specific value
# is hardcoded in the committed unit files. Mirrors ops/systemd/install.sh
# placeholder shape (<USER>, <REPO_ROOT>).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(whoami)}"
if [[ "$USER_NAME" == "root" ]]; then
    echo "error: cannot determine the non-root deploy user." >&2
    echo "       run as 'sudo $0' (sudo sets SUDO_USER)." >&2
    exit 1
fi
UNITS="docker-prune.service docker-prune.timer"

tmp="$(mktemp -d)"
for u in $UNITS; do
    sed -e "s#<REPO_ROOT>#${ROOT}#g" \
        -e "s#<USER>#${USER_NAME}#g" \
        "$ROOT/ops/systemd/$u" > "$tmp/$u"
done

echo "Installing docker-prune systemd units (sudo) for repo: $ROOT (user: $USER_NAME)"
sudo cp "$tmp"/docker-prune.service "$tmp"/docker-prune.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now docker-prune.timer

echo "--- timer ---"
systemctl list-timers docker-prune.timer --no-pager 2>/dev/null | head -5 || true
echo "Done."
echo "  inspect schedule:           systemctl list-timers docker-prune.timer"
echo "  tail last run:              journalctl -u docker-prune.service -n 80 --no-pager"
echo "  run NOW without waiting:    sudo systemctl start docker-prune.service"
