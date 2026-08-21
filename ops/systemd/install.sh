#!/usr/bin/env bash
set -euo pipefail
# Install all ai-delivery systemd services.
# Run as root (or with sudo) — but the deploy user is taken from SUDO_USER/USER,
# not from "root". The .service templates contain <USER>, <HOME>, <REPO_ROOT>
# placeholders which are substituted on install.

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVICES_DIR="$REPO_ROOT/ops/systemd"
TARGET="/etc/systemd/system"

DEPLOY_USER="${SUDO_USER:-${USER:-}}"
if [[ -z "$DEPLOY_USER" || "$DEPLOY_USER" == "root" ]]; then
    echo "error: cannot determine the non-root deploy user." >&2
    echo "       run as 'sudo $0' (sudo sets SUDO_USER)," >&2
    echo "       or set DEPLOY_USER=<name> before running." >&2
    exit 1
fi
DEPLOY_USER="${DEPLOY_USER_OVERRIDE:-$DEPLOY_USER}"
DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
if [[ -z "$DEPLOY_HOME" ]]; then
    echo "error: cannot resolve home directory for user '$DEPLOY_USER'." >&2
    exit 1
fi

echo "=== ai-delivery systemd install ==="
echo "Source:      $SERVICES_DIR"
echo "Target:      $TARGET"
echo "Deploy user: $DEPLOY_USER"
echo "Deploy home: $DEPLOY_HOME"
echo "Repo root:   $REPO_ROOT"
echo

for svc in "$SERVICES_DIR"/*.service; do
    name=$(basename "$svc")
    echo "[install] $name"
    sed -e "s|<USER>|$DEPLOY_USER|g" \
        -e "s|<HOME>|$DEPLOY_HOME|g" \
        -e "s|<REPO_ROOT>|$REPO_ROOT|g" \
        "$svc" > "$TARGET/$name"
    chmod 644 "$TARGET/$name"
done

echo
echo "=== reload + enable + start ==="
systemctl daemon-reload

for svc in "$SERVICES_DIR"/*.service; do
    name=$(basename "$svc")
    # Skip oneshots that have no [Install] section — those are driven by a
    # sibling .timer (e.g. docker-prune.service via docker-prune.timer) and
    # are installed by their own dedicated installer.
    if ! grep -q '^\[Install\]' "$svc"; then
        echo "[skip — timer-driven oneshot] $name"
        continue
    fi
    echo "[enable+start] $name"
    systemctl enable --now "$name"
done

echo
echo "=== status ==="
for svc in "$SERVICES_DIR"/*.service; do
    systemctl status "$(basename "$svc")" --no-pager -l 2>/dev/null | head -5 || true
    echo
done

echo
echo "Done. All ai-delivery services installed and running."
