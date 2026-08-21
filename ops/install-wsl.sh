#!/usr/bin/env bash
# install-wsl.sh — idempotent setup of ai-delivery runtime on WSL2 Ubuntu.
#
# Reproduces every command that was executed by hand during the original M0 install.
# Re-running this script on an already-set-up host should be a no-op (it detects what
# is already there and skips). Use the --reset flag to force re-do of a specific step.
#
# Assumes:
#   - WSL2 Ubuntu 22.04 (jammy) or compatible
#   - Run as the non-root user that owns the runtime (e.g., `sekator`)
#   - sudo available (script will prompt for password if not NOPASSWD)
#   - /etc/wsl.conf already has [boot] systemd=true AND wsl --shutdown has been
#     done from Windows side (run `pidof systemd` to verify before invoking this).
#
# Usage:
#   bash scripts/install-wsl.sh                       # full idempotent install
#   bash scripts/install-wsl.sh --skip-models         # skip ollama model pulls
#   bash scripts/install-wsl.sh --verify-only         # just print state, do nothing

set -euo pipefail

# ---- config ----------------------------------------------------------------
REPO_DIR="${REPO_DIR:-$HOME/projects/ai-delivery}"
RUNTIME_DIR="${RUNTIME_DIR:-$HOME/.claude-tg-bot}"
BOT_DIR="${BOT_DIR:-$HOME/claude-telegram-bot}"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects}"
APT_PACKAGES="ffmpeg jq build-essential ca-certificates gnupg curl python3.10-venv"
NODE_MIN_MAJOR=20
SKIP_MODELS=0
VERIFY_ONLY=0

# ---- arg parsing -----------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --skip-models)  SKIP_MODELS=1 ;;
        --verify-only)  VERIFY_ONLY=1 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0"
            exit 0
            ;;
        *)
            echo "unknown flag: $arg" >&2
            exit 2
            ;;
    esac
done

# ---- helpers ---------------------------------------------------------------
step()   { printf "\n\033[1;36m==>\033[0m %s\n" "$*"; }
ok()     { printf "  \033[32mok\033[0m  %s\n" "$*"; }
skip()   { printf "  \033[33mskip\033[0m %s\n" "$*"; }
warn()   { printf "  \033[33mwarn\033[0m %s\n" "$*" >&2; }
die()    { printf "  \033[31mfail\033[0m %s\n" "$*" >&2; exit 1; }
need()   { command -v "$1" >/dev/null 2>&1; }
need_root() {
    if [[ $EUID -eq 0 ]]; then sudo=""
    elif need sudo;            then sudo="sudo"
    else die "sudo required but not available"
    fi
}

require_systemd() {
    if pgrep -x systemd >/dev/null; then
        ok "systemd active (PID 1 = systemd)"
    else
        die "systemd not active — edit /etc/wsl.conf to add [boot] systemd=true, then run 'wsl.exe --shutdown' from Windows side, then re-open WSL"
    fi
}

# ---- step 1: prerequisites & systemd ---------------------------------------
step "Verifying prerequisites"
[[ -d "$REPO_DIR" ]] || die "REPO_DIR not found: $REPO_DIR (clone the repo first)"
require_systemd
need_root
[[ $VERIFY_ONLY -eq 1 ]] && { ok "verify-only mode — exiting before any mutations"; exit 0; }

# ---- step 2: apt base packages --------------------------------------------
step "Installing apt base packages"
missing_pkgs=""
for pkg in $APT_PACKAGES; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        skip "$pkg already installed"
    else
        missing_pkgs="$missing_pkgs $pkg"
    fi
done
if [[ -n "$missing_pkgs" ]]; then
    $sudo apt-get update -qq
    # shellcheck disable=SC2086
    $sudo apt-get install -y $missing_pkgs
    ok "installed:$missing_pkgs"
else
    ok "all base packages present"
fi

# ---- step 3: Docker Engine (native, not Docker Desktop) -------------------
step "Installing Docker Engine"
if need docker && docker info >/dev/null 2>&1; then
    skip "docker already works without sudo"
else
    if ! need docker; then
        $sudo install -m 0755 -d /etc/apt/keyrings
        $sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        $sudo chmod a+r /etc/apt/keyrings/docker.asc
        codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
        arch=$(dpkg --print-architecture)
        echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
            | $sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
        $sudo apt-get update -qq
        $sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin
        ok "docker-ce installed"
    fi
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
        $sudo usermod -aG docker "$USER"
        warn "added $USER to docker group — log out and back into WSL for it to take effect"
    fi
    $sudo systemctl enable --now docker
    ok "docker daemon enabled + started"
fi

# ---- step 4: uv (for mem0 MCP via uvx) ------------------------------------
step "Installing uv"
if need uv; then
    skip "uv already installed ($(uv --version))"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Re-source the uv shell snippet so this script sees uv on PATH
    [[ -f "$HOME/.local/bin/env" ]] && . "$HOME/.local/bin/env"
    ok "uv installed"
fi

# ---- step 5: Node.js + Claude Code CLI ------------------------------------
step "Installing Claude Code CLI"
if need claude; then
    skip "claude already installed ($(claude --version))"
else
    if ! need node; then
        # Install Node via NodeSource — Ubuntu's apt node is too old for Claude Code
        curl -fsSL "https://deb.nodesource.com/setup_22.x" | $sudo -E bash -
        $sudo apt-get install -y nodejs
        ok "node $(node --version) installed via NodeSource"
    fi
    node_major=$(node --version | sed 's/^v//' | cut -d. -f1)
    if [[ $node_major -lt $NODE_MIN_MAJOR ]]; then
        die "node $node_major too old (need >= $NODE_MIN_MAJOR)"
    fi
    $sudo npm install -g @anthropic-ai/claude-code
    ok "claude CLI installed: $(claude --version)"
fi

# ---- step 6: claude-aliases.sh ---------------------------------------------
step "Installing claude-aliases.sh (deepseek/anthropic backend toggles)"
src="$REPO_DIR/scripts/claude-aliases.sh"
dst="$HOME/.claude-aliases.sh"
if [[ -f "$src" ]]; then
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        skip "$dst already up-to-date"
    else
        install -m 0600 "$src" "$dst"
        ok "copied $src -> $dst"
    fi
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
        [[ -f "$rc" ]] || continue
        if grep -qF claude-aliases.sh "$rc"; then
            skip "already sourced in $(basename "$rc")"
        else
            printf '\n# ai-delivery: load backend aliases\n[ -f "$HOME/.claude-aliases.sh" ] && . "$HOME/.claude-aliases.sh"\n' >> "$rc"
            ok "added source line to $(basename "$rc")"
        fi
    done
else
    warn "$src not in repo yet — claude-deepseek/anthropic functions unavailable"
fi

# ---- step 7: runtime symlinks ---------------------------------------------
step "Wiring runtime symlinks"
mkdir -p "$RUNTIME_DIR" "$PROJECTS_DIR"
link_one() {
    local target="$1" link="$2"
    if [[ -L "$link" ]] && [[ "$(readlink -f "$link")" == "$target" ]]; then
        skip "$link -> $target"
    else
        ln -sfn "$target" "$link"
        ok "$link -> $target"
    fi
}
link_one "$REPO_DIR/bin"     "$RUNTIME_DIR/bin"
link_one "$REPO_DIR/meta"    "$RUNTIME_DIR/meta"
link_one "$REPO_DIR/stacks"  "$RUNTIME_DIR/stacks"
link_one "$REPO_DIR/bot"     "$BOT_DIR"

# ---- step 8: chmod +x on scripts ------------------------------------------
step "Ensuring scripts are executable"
chmod +x "$REPO_DIR"/bin/botctl-* 2>/dev/null && ok "bin/botctl-* +x" || warn "no bin/botctl-* files"
chmod +x "$REPO_DIR/bot/start.sh" 2>/dev/null && ok "bot/start.sh +x" || warn "bot/start.sh missing"
chmod +x "$REPO_DIR/stacks/mem0/init-ollama.sh" 2>/dev/null && ok "stacks/mem0/init-ollama.sh +x" || true
chmod +x "$REPO_DIR/scripts"/*.sh 2>/dev/null && ok "scripts/*.sh +x" || true

# ---- step 9: bot/.env stub -------------------------------------------------
step "Verifying bot/.env"
env_path="$REPO_DIR/bot/.env"
if [[ -f "$env_path" ]]; then
    needed_keys="TELEGRAM_BOT_TOKEN OWNER_TELEGRAM_ID OWNER_NAME DEFAULT_PROJECT_DIR LOG_LEVEL"
    missing=""
    for k in $needed_keys; do
        grep -qE "^${k}=" "$env_path" || missing="$missing $k"
    done
    [[ -z "$missing" ]] && ok "all required keys present" || warn "missing keys in $env_path:$missing"
    chmod 600 "$env_path"
else
    warn "$env_path missing — copy from $REPO_DIR/bot/.env.example and fill values"
fi

# ---- step 10: optional ollama model pull (mem0 stack) ---------------------
if [[ $SKIP_MODELS -eq 0 ]]; then
    step "Pulling Ollama models for mem0 memory layer (~9.5 GB, may take 20-40 min)"
    if docker compose -f "$REPO_DIR/stacks/mem0/docker-compose.yml" ps --services 2>/dev/null | grep -qx ollama; then
        if docker compose -f "$REPO_DIR/stacks/mem0/docker-compose.yml" ps --status=running ollama | grep -q ollama; then
            bash "$REPO_DIR/stacks/mem0/init-ollama.sh"
            ok "ollama models ready"
        else
            warn "ollama container not running — start with: docker compose -f stacks/mem0/docker-compose.yml up -d"
        fi
    else
        warn "mem0 stack not up — skipping model pull. Run: docker compose -f stacks/mem0/docker-compose.yml up -d"
    fi
else
    skip "ollama model pull (--skip-models)"
fi

# ---- summary ---------------------------------------------------------------
step "Summary"
printf "  systemd          : %s\n" "$(pgrep -x systemd >/dev/null && echo active || echo INACTIVE)"
printf "  docker           : %s\n" "$(need docker && (docker info >/dev/null 2>&1 && echo 'works without sudo' || echo 'installed but no group access yet') || echo MISSING)"
printf "  claude CLI       : %s\n" "$(need claude && claude --version || echo MISSING)"
printf "  uv               : %s\n" "$(need uv && uv --version || echo MISSING)"
printf "  ffmpeg           : %s\n" "$(need ffmpeg && ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3 || echo MISSING)"
printf "  jq               : %s\n" "$(need jq && jq --version || echo MISSING)"
printf "  symlinks         : "
ls -la "$RUNTIME_DIR" 2>/dev/null | grep -c '^l' || echo 0
echo ""
echo "Next steps if any 'warn' appeared:"
echo "  - edit $env_path to add missing keys"
echo "  - log out + back into WSL if docker group was just added"
echo "  - start the mem0 stack: docker compose -f $REPO_DIR/stacks/mem0/docker-compose.yml up -d"
echo "  - start the bot: $BOT_DIR/start.sh"
