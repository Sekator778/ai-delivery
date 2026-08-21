# Installation — WSL2 Ubuntu

Canonical install procedure for v1 on a fresh WSL2 Ubuntu host.
Run every command as written, top to bottom. You should end with a working
dev environment and a bot that responds to Telegram messages.

## Prerequisites

- **Windows 10 or 11** with WSL2 already enabled. If WSL2 is not installed, run
  `wsl --install -d Ubuntu` from a PowerShell/CMD terminal (admin), reboot, and
  create a Linux username/password when prompted.
- **Claude Max** subscription (the `claude` CLI OAuths into it in Step 5).
- **At least 8 GB RAM** recommended. The heaviest single component is the
  FastEmbed embedder cache (`intfloat/multilingual-e5-large`, ~2 GB ONNX
  loaded lazily in `bot.py`). Add ~2 GB if you bring up the optional
  voice stack (Whisper STT + Silero TTS).
- **Telegram bot token** from [@BotFather](https://t.me/BotFather). Have it ready
  for Step 9.

## Step 1 — Enable systemd in WSL2

First, check if systemd is already active:

```bash
pidof systemd || echo "not active"
```

If the output is `not active`, create/edit `/etc/wsl.conf`:

```bash
sudo tee /etc/wsl.conf <<'EOF'
[boot]
systemd=true
EOF
```

Now switch to the Windows side. From a **PowerShell** or **CMD** terminal:

```powershell
wsl.exe --shutdown
```

This closes all WSL VMs. Re-open your WSL2 terminal, then verify:

```bash
systemctl --version && pidof systemd
```

Both commands should succeed without errors.

## Step 2 — Update apt and install base packages

```bash
sudo apt update
sudo apt install -y ffmpeg jq build-essential ca-certificates gnupg curl git
```

| Package | Why |
|---|---|
| `ffmpeg` | Voice pipeline: OGG (Telegram) ↔ WAV (Whisper) |
| `jq` | botctl scripts parse JSON state |
| `build-essential` | Compiles whisper.cpp in M2 |
| `ca-certificates` + `gnupg` | Docker repo signing keys |
| `curl` + `git` | Everything |

## Step 3 — Install Docker Engine (native, not Docker Desktop)

Add Docker's official APT repository and install the engine:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
sudo systemctl enable --now docker
```

**Important:** exit and re-open the shell for the `docker` group to apply.
Verify the daemon is reachable without `sudo`:

```bash
docker info
```

If Docker Desktop is installed on the Windows side, disable its WSL integration
in Docker Desktop settings to avoid PATH conflicts. Docker Desktop itself can
stay running if Windmill is still parked on Windows.

## Step 4 — Install Node.js + npm (if missing)

Check whether Node is already present:

```bash
node --version
```

If the command is not found, install via NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

Verify:

```bash
node --version && npm --version
```

## Step 4b — GitHub CLI (`gh`) — required for PR operations

The Reviewer stage opens the PR and the `[Да]` approval merges it, both via `gh`.
Without an authenticated `gh`, the pipeline reaches the Reviewer and then fails at
the PR step. Install and authenticate:

```bash
# install (Debian/Ubuntu)
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
sudo apt update && sudo apt install -y gh

gh auth login          # WSL/dev: browser web-flow. Headless server: paste a PAT.
gh auth status         # must show your account + repo scope
```

(On a headless production host `gh auth login` falls back to the device/PAT web
flow — see *Production-only gotchas* below.)

## Step 5 — Install Claude Code CLI and login to Max

```bash
sudo npm install -g @anthropic-ai/claude-code
claude --version
```

Now authenticate to your Max subscription:

```bash
claude
```

A URL is printed to the terminal. Copy it, open it in your Windows browser, log in
to your Claude Max account, and paste the resulting auth code back into the WSL
terminal. The `claude` CLI stores the OAuth token in `~/.claude-auth/`.

## Step 6 — Install uv (for MCP servers)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv --version
uvx --version
```

## Step 7 — Clone this repository

Clone into the WSL filesystem — not under `/mnt/c/...` — for IO performance:

```bash
cd $HOME
mkdir -p projects
git clone <repo-url> projects/ai-delivery
cd projects/ai-delivery
git config core.autocrlf input
```

Replace `<repo-url>` with the actual repository URL. `core.autocrlf input` is a
safety net; `.gitattributes` already enforces LF line endings.

## Step 8 — Run install.sh (symlink repo into runtime paths)

`install.sh` does not exist yet — it arrives in a later milestone. For now,
create the symlinks manually (update for monorepo: stacks/ now lives at services/stacks/):

```bash
mkdir -p $HOME/.claude-tg-bot
ln -sfn $PWD/bin             $HOME/.claude-tg-bot/bin
ln -sfn $PWD/meta            $HOME/.claude-tg-bot/meta
ln -sfn $PWD/services/stacks $HOME/.claude-tg-bot/stacks
ln -sfn $PWD/bot             $HOME/claude-telegram-bot
```

> `-sfn` (not `-sf`): with `-n` set, `ln` does not follow an existing
> symlink target — it replaces the symlink itself. Without `-n`, running
> `ln -sf X dest` where `dest` is already a symlink to a directory will
> create `dest/X` inside the target. The `-n` flag prevents that footgun.

This mirrors the layout described in `ARCHITECTURE.md` §3.8. Edits in the working
tree apply immediately because runtime paths are symlinks, not copies.

## Step 9 — Configure .env

```bash
cp bot/.env.example bot/.env
chmod 600 bot/.env
```

Open `bot/.env` in an editor and fill in the three required values:

| Variable | Source |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather when you created the bot |
| `OWNER_TELEGRAM_ID` | Your numeric Telegram user ID (get it from @userinfobot) |
| `OWNER_NAME` | Your display name (used in log lines, not security-critical) |

Do not commit `bot/.env` — it is already in `.gitignore`.

## Step 9b — Register your target repos (`bot/projects.json`)

```bash
cp bot/projects.example.json bot/projects.json
```

Edit it so each alias points at an absolute path on this host. Two entry forms
are accepted:

```json
{
  "_default": "sandbox",
  "projects": {
    "sandbox": "/home/you/projects/ai-delivery-sandbox",
    "ai-delivery": {"path": "/home/you/projects/ai-delivery", "base": "dev"}
  }
}
```

- plain string — path only; the base branch is resolved automatically;
- `{"path": ..., "base": ...}` — `base` pins the branch the pipeline cuts its
  work branch from and targets with the PR.

Base-branch resolution order: per-target `base` → `PIPELINE_BASE_BRANCH` (env,
deployment-wide) → the target repo's own `origin/HEAD` → `main`. Set `base`
whenever a repo's default branch is not where development happens — an
ai-delivery target whose GitHub default is still `master` while work lives on
`dev` is exactly that case.

`bot/projects.json` is gitignored (paths are host-specific); the bot caches it
at startup, so restart the bot after editing. `/projects` in Telegram lists the
registered aliases and any pinned base.

Nothing else needs preparing per target: each task runs in its own ephemeral
`git worktree`, created under `/tmp/ai-delivery-wt/` (override with
`WORKTREE_ROOT`) and removed once the PR is pushed, so the pipeline never
switches branches inside the checkout you work in. Set
`WORKTREE_ISOLATION_ENABLED=0` only to fall back to the legacy in-place
behaviour.

## Step 10 — Smoke test

```bash
chmod +x bin/botctl-* bot/start.sh
python3 -m venv bot/venv
source bot/venv/bin/activate
pip install -r bot/requirements.txt
python3 bot/bot.py
```

Leave the process running. From Telegram, send "hello" to your bot. The bot
logs should show the message reaching `handle_text`.

The meta-agent reply path requires `meta/CLAUDE.md` and the botctl scripts to
be in place. On first run you may see "no bot reply yet" until the meta-agent
is wired at the M1 sync point. The text echo and routing layer work immediately.

## Step 11 — STT configuration (voice-to-text transcript files)

The `/stt` command transcribes voice notes and audio files via the Whisper
server and writes the result to disk. Four environment variables control this
behaviour; set them in `bot/.env` (and mirror the timeout pair in the compose
environment — see below).

| Variable | Default | Step governed | Purpose |
|---|---|---|---|
| `STT_OUTPUT_DIR` | `~/Downloads/transcripts` | bot (file write) | Directory where `.txt` transcript files are created. Tilde-expanded; auto-created on first use. Must be writable by the bot process. |
| `STT_URL_MAX_MB` | `100` | bot (URL download) | Maximum size in MB for audio fetched from a bare URL. Downloads exceeding this limit are aborted mid-stream. |
| `WHISPER_TIMEOUT_SEC` | `600` | whisper-server: whisper-cli | Seconds allowed for the whisper-cli transcription subprocess. Set identically in `bot/.env` **and** `services/stacks/voice/docker-compose.yml` (the compose file reads `${WHISPER_TIMEOUT_SEC:-600}`). The bot derives its HTTP client timeout as `FFMPEG_TIMEOUT_SEC + WHISPER_TIMEOUT_SEC + 30`; a mismatch only narrows/widens the safety margin. |
| `FFMPEG_TIMEOUT_SEC` | `60` | whisper-server: ffmpeg | Seconds allowed for the ffmpeg audio-conversion subprocess. Governed **independently** of `WHISPER_TIMEOUT_SEC` — never derive one from the other. Set identically in `bot/.env` and the compose file. |

Example `bot/.env` block:

```env
STT_OUTPUT_DIR=~/Downloads/transcripts
STT_URL_MAX_MB=100
WHISPER_TIMEOUT_SEC=600
FFMPEG_TIMEOUT_SEC=60
```

After editing `bot/.env`, rebuild and restart the whisper-server container so
the timeout vars take effect inside it:

```bash
cd services/stacks/voice
docker compose up -d --build
```

Then restart the bot process. The new env vars have safe built-in defaults, so
the stack is immediately functional without any `.env` edits.

## Troubleshooting

- **`claude` command not found after install** — restart the shell to refresh
  `PATH`. npm global bins are added by `~/.bashrc` on login.
- **`docker: permission denied`** — did you exit and re-enter the shell after
  `usermod -aG docker`? Run `groups` to verify `docker` appears in your groups.
- **WSL2 sleeps and the bot stops** — Windows may suspend the WSL VM when idle.
  See `docs/WSL2-NOTES.md` for keep-alive options (PowerToys Awake, power plan
  "never sleep", Task Scheduler keep-alive).

---

# Installation — production Linux server (no WSL)

Use this section instead of the WSL2 walk-through above when deploying to a
real Linux host (VPS, baremetal, EC2/Hetzner/whatever). The differences from
the WSL2 path are concentrated in five areas: no `wsl.conf`/systemd-bootstrap,
no `--exec sleep infinity` keep-alive Task Scheduler hack, real DNS / TLS for
external endpoints, proper secrets management, and a non-root container
user — call them out before flipping the cutover.

## Prerequisites (server)

- **Linux distro with systemd**: Ubuntu 22.04+/24.04 LTS, Debian 12+, RHEL/Rocky 9+
  all fine. The systemd units in `ops/systemd/` are vanilla.
- **Non-root user** with sudo + docker group membership (e.g. `aidelivery`).
  *Never* run the stack as root in production — `bot/.env` + secrets are
  600-perm'd to the user.
- **Outbound HTTPS** to: `api.anthropic.com`, `api.deepseek.com`,
  `api.z.ai`, `api.telegram.org`, `github.com`, `*.docker.io`,
  `*.langchain.com` (if LangSmith), `pypi.org`, `registry.npmjs.org`.
- **Inbound TCP 80/443** *only if* you expose Windmill UI externally — the
  default deploy keeps everything on `127.0.0.1` and tunnels via SSH.
- **Persistent disk** ≥ 30 GB for Docker volumes + research artifacts.

Skip everything WSL-specific: no `wsl.conf`, no Windows Task Scheduler entry,
no `/mnt/c` paths anywhere.

## Step S1 — System packages

Same as WSL2 Step 2 — `ffmpeg jq build-essential ca-certificates gnupg curl git`
plus `unzip` (some tarballs in `services/stacks/` need it).

## Step S2 — Docker Engine (native)

Identical to WSL2 Step 3, but skip the Docker Desktop tug-of-war: there's no
competing engine, so just install `docker-ce` from the official repo and add
the deploy user to the `docker` group.

```bash
sudo usermod -aG docker $USER
newgrp docker   # avoids logout/login
```

## Step S3 — Clone the repo to the deploy user's home

```bash
sudo -u aidelivery -i
cd ~
git clone https://github.com/Sekator778/ai-delivery.git projects/ai-delivery
cd projects/ai-delivery
```

The systemd units, START scripts, and bot all assume `~/projects/ai-delivery`
exactly as in WSL — no path edits needed if you mirror the layout.

## Step S4 — Secrets management

In production you **do not** put live keys into `bot/.env` and `chmod 600` it.
Use one of these patterns (pick one, document it in your fork):

### Option A — systemd LoadCredentialEncrypted (recommended)

```ini
# /etc/systemd/system/claude-tg-bot.service.d/secrets.conf
[Service]
LoadCredentialEncrypted=telegram_token:/etc/credstore.encrypted/telegram_token
LoadCredentialEncrypted=anthropic_key:/etc/credstore.encrypted/anthropic_key
LoadCredentialEncrypted=deepseek_key:/etc/credstore.encrypted/deepseek_key
LoadCredentialEncrypted=glm_key:/etc/credstore.encrypted/glm_key
LoadCredentialEncrypted=langsmith_key:/etc/credstore.encrypted/langsmith_key
```

Then in `bot/start.sh` (the systemd unit's ExecStart wrapper), source the
credentials directory:

```bash
export TELEGRAM_BOT_TOKEN=$(cat "$CREDENTIALS_DIRECTORY/telegram_token")
export ANTHROPIC_API_KEY=$(cat "$CREDENTIALS_DIRECTORY/anthropic_key")
# ...
```

Encrypt the secrets with `systemd-creds encrypt`:

```bash
sudo systemd-creds encrypt --name=telegram_token - \
  /etc/credstore.encrypted/telegram_token <<<'123456:ABC...'
```

### Option B — HashiCorp Vault / sops / age

If you already have a secrets pipeline, mount the decrypted env file into
`/run/secrets/ai-delivery.env` (tmpfs, world-unreadable) and override:

```ini
# /etc/systemd/system/claude-tg-bot.service.d/secrets.conf
[Service]
EnvironmentFile=/run/secrets/ai-delivery.env
```

### Option C — last-resort plain file (NOT recommended for prod)

`bot/.env` with `chmod 600 bot/.env` + `chown aidelivery:aidelivery bot/.env`.
Acceptable for personal-VPS hobby deploys. **Do not check it in to git.**

## Step S5 — systemd units

The unit templates in `ops/systemd/*.service` contain `<USER>`, `<HOME>`, and
`<REPO_ROOT>` placeholders. Use the installer script — it resolves them from
the invoking sudo user and the repo checkout location at copy time:

```bash
sudo ops/systemd/install.sh
```

This substitutes placeholders, copies the units into `/etc/systemd/system/`,
runs `daemon-reload`, and enables + starts each one. Verify:

```bash
systemctl status claude-tg-bot task-dispatcher watcher --no-pager
```

If you need to override the deploy user (e.g. running as root, not via sudo),
set `DEPLOY_USER_OVERRIDE=<user>` before invoking the script.

## Step S5b — LiteLLM proxy (non-anthropic backends)

Routes DeepSeek and GLM traffic through a local proxy that handles
rate-limit cooldown and provider fallback. Max OAuth (anthropic backend)
bypasses the proxy entirely.

```bash
cd ~/projects/ai-delivery/ops/litellm
cp .env.example .env
# Edit .env: set DEEPSEEK_API_KEY, GLM_API_KEY,
# LITELLM_MASTER_KEY=sk-litellm-$(openssl rand -hex 32)
docker compose up -d
sleep 5
curl -fsS http://localhost:4000/health/liveliness
```

The proxy listens on `127.0.0.1:4000` (loopback only). When stage_runner
adopts it in Phase 2 (separate change), DeepSeek/GLM stages will set
`ANTHROPIC_BASE_URL=http://localhost:4000/v1` and authenticate with
`LITELLM_MASTER_KEY`. Anthropic stages remain direct.

Full rationale and trouble guide: `ops/litellm/README.md`.

## Step S6 — Windmill behind real TLS (optional)

On WSL the Caddy config points at `localhost`. In production, replace
`services/stacks/.../Caddyfile` with a real hostname + Let's Encrypt:

```caddyfile
windmill.yourdomain.example {
    reverse_proxy windmill-server:8000
}
```

…and open inbound 80/443 on your firewall. If you don't need external
Windmill access (most owner-only deploys don't), leave Caddy bound to
`127.0.0.1:80` and SSH-tunnel: `ssh -L 8080:127.0.0.1:80 server`.

## Step S7 — Validate

Run the post-deploy validator that catches the "container up but bot
unreachable" failure mode:

```bash
./ops/check-recovery.sh
```

All four lines should be green; the windmill stack should report ≥7
healthy containers. If anything red, the script's source comments
explain what's expected and where to look.

## Production-only gotchas

- **No `/mnt/c` paths** — the WSL2 install paths assume Windows Downloads.
  Production clone goes straight from GitHub; nothing else needs adjusting.
- **`ANTHROPIC_AUTH_TOKEN` vs Claude Max OAuth** — the meta-agent in
  `bot.py` resumes Claude Max OAuth via the local keychain on WSL. On a
  headless server that fails — switch the meta-agent to API-key auth by
  setting `ANTHROPIC_API_KEY` in the env and removing any keychain hint.
- **No GUI** — `gh auth login` falls back to the web flow with a pasted
  one-time code; do that once during S3, then `gh` stays authed via a
  `~/.config/gh/hosts.yml` token.
- **systemd unit `KillMode=mixed`** is correct on production too — the
  bot spawns long-lived meta-Claude subprocesses; without `KillMode=mixed`
  a `systemctl stop` orphans them.
