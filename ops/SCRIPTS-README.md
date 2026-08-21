# scripts/

Reproducible automation for setting up and operating ai-delivery
on any WSL2 Ubuntu host (or, with minor tweaks, plain Ubuntu / Mac mini /
Linux VPS).

**Rule:** anything we do manually during install or operations gets captured
here as an idempotent script. Manual commands must not be the only record.

## Scripts

| Script | Purpose |
|---|---|
| `publish-public.sh` | Gated, idempotent public-mirror publish. Exports a filtered git tree (private paths excluded), runs two independent secret/PII scans (gitleaks + project blocklist), builds one squash commit on the public mirror's `main`, and pushes only under an explicit `--push` flag. See [ops/PUBLISH-PUBLIC.md](PUBLISH-PUBLIC.md) for setup and usage. |
| `install-wsl.sh` | One-shot idempotent install of the runtime: apt base packages, Docker Engine (native, not Docker Desktop), uv, Claude Code CLI, shell aliases, runtime symlinks, executable bits, .env validation, ollama model pulls. Re-running is safe — each step detects whether the work is already done. |
| `claude-aliases.sh` | Shell functions `claude-deepseek` and `claude-anthropic` for routing the `claude` CLI to different model backends. Sourced from `~/.zshrc` and `~/.bashrc` by the installer. Mirror of the Windows PowerShell profile functions. |
| `docker-prune.sh` | Safe weekly Docker cleanup: dangling/untagged images (`until=48h`, skips in-use + in-progress-build layers) + reclaimable BuildKit cache only. Never prunes tagged images, volumes, networks or containers. Installed as a systemd timer via `ops/install-docker-prune-timer.sh` (weekly, `Persistent=true`). |

## Bootstrap on a fresh WSL2 Ubuntu host

```bash
# 1. Clone the repo into your WSL home (NOT under /mnt/c — perf reasons)
cd "$HOME"
git clone https://github.com/<owner>/ai-delivery.git
cd ai-delivery

# 2. Ensure WSL2 systemd is enabled (one-time, persists)
echo -e '[boot]\nsystemd=true' | sudo tee /etc/wsl.conf
# From Windows side: wsl.exe --shutdown
# Re-open WSL terminal, then verify:
pgrep -x systemd >/dev/null && echo "systemd ok"

# 3. Run the installer
bash scripts/install-wsl.sh
# OR skip the long ollama model pull and do it later:
bash scripts/install-wsl.sh --skip-models

# 4. Fill in bot/.env (TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, OWNER_NAME,
#    DEFAULT_PROJECT_DIR) — see bot/.env.example for the template.

# 5. Log in to Claude Max (interactive, OAuth flow opens browser via WSL
#    fallback, you copy-paste an auth code back):
claude-anthropic
# Inside the session: type /exit when login succeeds.

# 6. Start the mem0 stack (Qdrant + Ollama):
docker compose -f stacks/mem0/docker-compose.yml up -d
bash stacks/mem0/init-ollama.sh   # ~9 GB download for qwen2.5:14b + bge-m3

# 7. Start the bot:
bash ~/claude-telegram-bot/start.sh
```

## Verification

```bash
bash scripts/install-wsl.sh --verify-only
```

Prints what is currently installed without mutating state. Useful for
quick sanity check after a reboot or before sharing the host with a teammate.

## Migration to another host

The full bootstrap above is the same on any new host (laptop, VPS, Mac
under Linux, fresh WSL2 instance). The only things specific to the
current host are:

- `bot/.env` (gitignored — must be re-created with new Telegram token /
  IDs / project paths)
- `~/.claude/.credentials.json` (gitignored — re-run `claude-anthropic`
  to re-OAuth)
- `~/.claude-aliases.sh` (auto-copied from `scripts/claude-aliases.sh`
  by the installer; edit the runtime copy if you need to rotate the
  DeepSeek key)

Everything else is in git.
