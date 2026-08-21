# WSL2 Operations Notes

> Operator-facing reference: failure modes, edge cases, and recovery procedures
> for running ai-delivery on WSL2 Ubuntu.
>
> For the happy-path install, see [INSTALL.md](INSTALL.md).

## Why WSL2 and not Docker Desktop

Docker Desktop (DD) is avoided for the orchestrator host. The reasons are documented
in `ARCHITECTURE.md` §2:

- **Licensing:** Docker Desktop requires a paid subscription for commercial /
  organizational use above 250 employees or $10M revenue. WSL2 with native Docker
  Engine has no such restriction.
- **RAM overhead:** DD's VM consumes 2–4 GB just for its GUI, tray agent, and
  background services. Native Docker Engine inside WSL2 shares the kernel and
  allocates memory only for running containers.
- **Bind-mount speed:** DD routes bind-mounts through a Windows ↔ Linux translation
  layer. Native Engine accesses the WSL2 ext4 filesystem directly — 10–50× faster
  for containerized builds with local volumes.
- **Telemetry:** DD collects usage data by default (opt-out). Native Docker Engine
  has no telemetry.
- **VPS parity:** The WSL2 + native Engine stack is identical to what you would
  provision on a headless Ubuntu VPS. If the orchestrator ever moves to a dedicated
  server, the same install script and compose files work unchanged.

Docker Desktop **may** remain installed on the Windows side for other projects
(e.g., Windmill in parked state). Its WSL integration must be disabled — see
[Docker Engine vs Docker Desktop coexistence](#docker-engine-vs-docker-desktop-coexistence).

## Keeping WSL alive 24/7

The WSL2 VM shuts down when no process is actively using it. Since the bot must run
continuously, you need at least one of these approaches:

### Approach 1: Pin a terminal (lazy, works)

Keep one WSL terminal window open at all times. Minimize it, don't close it. As
long as a single shell is alive, the VM stays running. Simplest option; no
configuration needed.

### Approach 2: Windows Task Scheduler

Create a trigger that keeps the VM warm on every user logon:

1. Open Task Scheduler (`taskschd.msc`).
2. Create Basic Task → name: `WSL2 Keepalive`.
3. Trigger: `At logon`.
4. Action: `Start a program` → Program: `wsl.exe`, Arguments: `-d Ubuntu --exec true`.
5. Finish.

The `--exec true` runs `/bin/true` (exits immediately), but the act of launching
WSL keeps the VM alive as long as the Windows session is active.

### Approach 3: Disable Windows sleep

If the Windows host itself suspends, WSL2 freezes regardless of keepalive tricks:

- **Option A (PowerToys):** Install Microsoft PowerToys → Awake → Mode: "Keep awake
  indefinitely".
- **Option B (Power Plan):** Control Panel → Power Options → Change plan settings →
  "Put the computer to sleep: Never".

### Important note on `wsl.exe --shutdown`

Running `wsl.exe --shutdown` from Windows does **not** permanently kill WSL. The
next WSL invocation restarts the VM cold (all services restart via systemd if
enabled). This is the normal recovery procedure, not a destructive operation.

## systemd in WSL — first-time setup

WSL2 does not run systemd by default. Without systemd, Docker Engine and other
services won't auto-start.

### Setup (one-time)

```bash
sudo tee -a /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true
EOF
```

### The failure mode

Editing `/etc/wsl.conf` inside WSL and then just typing `exit` does **not** apply
the change. WSL reads `/etc/wsl.conf` only at VM boot. From the Windows side you
must:

```powershell
wsl.exe --shutdown
```

Then start a new WSL terminal. The VM restarts and systemd boots.

### Verification

```bash
systemctl status docker
```

If you see `Active: active (running)`, systemd is working. If you see
`System has not been booted with systemd`, the flag did not take — repeat the
shutdown step above.

### Enable Docker auto-start (one-time)

```bash
sudo systemctl enable docker
```

Docker will now start automatically on every WSL restart.

## Docker Engine vs Docker Desktop coexistence

Having both native Docker Engine (inside WSL) and Docker Desktop (on Windows) with
WSL integration enabled causes a silent footgun.

### The problem

If Docker Desktop's WSL integration is enabled for your Ubuntu distro, the `docker`
command on your WSL `$PATH` resolves to Docker Desktop's bridge stub located under
`/mnt/c/Program Files/Docker/...`. This is slow, confusing, and may use different
contexts and credentials than the native daemon.

### The fix

1. Docker Desktop → Settings (gear icon) → Resources → WSL Integration.
2. Find your Ubuntu distro in the list.
3. Toggle it **OFF**.
4. Click "Apply & Restart".
5. Close and reopen your WSL terminal.

### Verification

```bash
which docker
```

Must return `/usr/bin/docker`. If it returns anything under `/mnt/c/`, the
integration is still active — repeat the fix and ensure the WSL shell was restarted
(not just `exec bash`).

```bash
docker info | grep "Server Version"
```

Should match the native Engine version, not Docker Desktop's.

## Claude Code OAuth on WSL

The first `claude` invocation from WSL triggers an interactive OAuth flow. Since
WSL has no native browser, you copy the URL manually.

### First login

Run `claude` from WSL:

```bash
claude
```

The CLI prints a URL like `https://auth.anthropic.com/...`. Copy it.

1. Paste the URL into a **Windows** browser (Chrome, Edge, Firefox on the Windows
   side — not a text-mode browser inside WSL).
2. Log in with your Anthropic account.
3. After login, the browser shows an auth code.
4. Copy the auth code back into the WSL terminal and press Enter.

The token is cached on disk (typically under `~/.config/claude-code/`). Subsequent
`claude` invocations reuse it without prompting.

### Token expiry

When the OAuth token expires (roughly every 30 days), the orchestrator will see an
auth error in `bot.py`'s subprocess stderr — the symptom is a sub-Claude call that
fails immediately with an authentication message. Recovery:

```bash
claude
```

Re-run the OAuth flow interactively as above. No other config changes are needed;
the orchestrator picks up the refreshed token automatically.

## File performance — Linux filesystem vs /mnt/c

WSL2 runs two filesystems with dramatically different performance profiles:

| Location | Filesystem | Protocol | Relative speed |
|---|---|---|---|
| `/home/<user>/` | ext4 (WSL VHD) | Native kernel | 1× (baseline) |
| `/mnt/c/...` | Windows NTFS | 9P over Hyper-V socket | 10–50× slower |

The 9P protocol bridge between the Linux kernel and the Windows NTFS driver is
the bottleneck — every `stat()`, `read()`, and `write()` crosses a VM boundary.

### Practical impact

Operations that touch many small files are worst-affected:

- `git clone` of a large repository: minutes vs seconds.
- `npm install` / `pip install` with many dependencies: 30–60× slower.
- `docker build` with a bind-mounted context under `/mnt/c/`: the build context
  transfer alone can dominate build time.
- Any `find`, `grep -r`, or IDE indexing over a mounted NTFS path.

### Rule

**Always clone the repo into `$HOME` in WSL**, never under `/mnt/c/`:

```bash
git clone git@github.com:Sekator778/ai-delivery.git ~/projects/ai-delivery
```

If the repo already lives under `/mnt/c/`, move it:

```bash
cp -a /mnt/c/Users/user/Downloads/Telegram Desktop/almaz/ai-delivery ~/projects/ai-delivery
```

Do not symlink from `/mnt/c/` into `$HOME` — the files still live on NTFS.

## Line endings (LF vs CRLF)

WSL2 uses Unix-native line endings (LF). Windows uses CRLF. When files cross the
boundary, things break.

### Why it matters

- Shell scripts with CRLF line endings fail with cryptic errors
  (`/bin/bash^M: bad interpreter`).
- Python files with CRLF may parse but break hashbangs and shebangs.
- A single-line edit on a CRLF-polluted file shows as the entire file changed in
  `git diff` — impossible to review.

### Repository enforcement

The repo's `.gitattributes` enforces LF normalization:

```
* text=auto eol=lf
*.sh text eol=lf
```

Never set `core.autocrlf=true` in WSL — it overrides `.gitattributes` and
re-introduces CRLF on checkout.

### Cross-editing risk

If you edit files from the **Windows side** (VS Code opened via `\\wsl$\...` in
Windows Explorer, or a Windows-based JetBrains IDE pointing at `\\wsl$\`), the
Windows editor may convert line endings to CRLF on save. VS Code has a status bar
indicator in the lower right — it should read `LF`, not `CRLF`.

To configure VS Code for LF by default:

```json
{
  "files.eol": "\n"
}
```

### Detecting CRLF pollution

```bash
grep -rIl $'\r' . 2>/dev/null
```

If this returns any files, they contain CRLF.

### Recovery

If CRLF pollution is already committed:

```bash
git add --renormalize .
git commit -m "chore: normalize line endings to LF"
```

If it has not been committed yet:

```bash
sed -i 's/\r$//' <affected-file>
```

## Telegram polling vs webhooks (no public IP needed)

The orchestrator uses Telegram's **long-polling** mode (`Application.run_polling()`),
not webhooks. This has important operational implications:

- **No public IP address required.** The bot connects **outbound** to Telegram's
  API servers. No inbound ports need to be opened on your router.
- **No tunnel needed.** You do not need ngrok, cloudflared, localtunnel, or any
  reverse proxy. The bot pulls messages rather than waiting for pushed HTTP
  requests.
- **The only inbound listener** in the entire system is the `:8766` HTTP server
  inside `bot.py`, bound to `127.0.0.1` (loopback only). It is not exposed to the
  LAN or the internet. It exists solely for sub-Claude callbacks from within the
  same WSL instance.
- **Webhook mode** would require a stable public URL, TLS certificate, and inbound
  firewall rules — polling avoids all of this. Webhooks are only needed at massive
  scale (thousands of bots) where polling creates too many open connections.

## Memory layer on CPU — performance expectations

The Ollama + qwen2.5:14b path was retired in May 2026 (commit `bdaa6f7`).
The memory layer now uses **FastEmbed** (ONNX Runtime, no LLM step) which
is dramatically faster on CPU:

### Warm-up

First call after `bot.py` start loads the ONNX model from `~/.cache/fastembed/`
into RAM. Default model is `intfloat/multilingual-e5-large` (~2 GB cached
once on first download from HuggingFace, ~3 s to load into memory on cold
start). Subsequent calls reuse the in-memory model.

### Per-call latency

| Operation | Model | Typical time |
|---|---|---|
| Embedding (`/memo` write, `/recall` query, hook capture/inject) | `multilingual-e5-large` | ~80–150 ms per call |
| Qdrant vector search (top-3, cosine) | n/a | ~10–30 ms |
| Qdrant point insert (one point) | n/a | ~5–15 ms |

Note that there is no LLM fact-extraction step — every input text is
embedded as-is and stored. This loses the ability to deduplicate
near-identical facts automatically, but is ~300× faster than the old
qwen2.5 path.

### If quality is unacceptable

If recall scores are noticeably weaker than 0.7–0.9 on relevant queries:

1. Confirm the embedding model is fully downloaded (`ls -la ~/.cache/fastembed/`).
2. Swap to `intfloat/multilingual-e5-large-instruct` if available, which
   tends to do better on conversational prompts (set `MEMO_EMBED_MODEL`
   and `MEMO_EMBED_DIMS=1024` in `bot/.env`).
3. As a last resort, downgrade to MiniLM for speed
   (`paraphrase-multilingual-MiniLM-L12-v2`, dims=384, ~120 MB cache).
   Quality drops ~10–15 % cosine on cross-lingual queries.

### Memory layer disk usage

- `~/.cache/fastembed/` — ~2 GB for the default model
- `services/stacks/mem0/qdrant-data/` — grows ~1 KB per stored point; even
  months of hook auto-capture stay well under 200 MB
- The retired Ollama stack would have cost ~9.5 GB — that disk is reclaimed.

## Recovery procedures

### Docker daemon dead

Symptom: `docker ps` returns `Cannot connect to the Docker daemon`.

```bash
sudo systemctl status docker
```

If status is `inactive (dead)` or `failed`:

```bash
sudo systemctl restart docker
docker info
```

If restart fails, check logs:

```bash
sudo journalctl -xeu docker
```

Common causes: another process bound to the same socket, or Docker Desktop
integration conflict (see above).

### Bot process zombie / port :8766 stuck

Symptom: `start.sh` fails with `Address already in use` on port 8766.

Find the process:

```bash
pgrep -fa 'python.*bot\.py'
```

Kill it:

```bash
kill <pid>
```

If the process does not respond to `SIGTERM`:

```bash
kill -9 <pid>
```

Verify the port is free:

```bash
ss -tlnp | grep 8766
```

Should return nothing. Then restart:

```bash
./start.sh
```

### Claude Code session corrupted (`--resume` fails)

Symptom: the meta-agent's `claude --resume <id>` call errors with "session not
found" or a corrupt-session message. This can happen after an unclean shutdown or
if the Claude Code session file was truncated.

The meta-agent session ID is stored in `state.json` under the user's key:

```bash
cat ~/.claude-tg-bot/state.json | jq '.<user_id>.meta_session_id'
```

To force a fresh session:

```bash
jq 'del(.<user_id>.meta_session_id)' ~/.claude-tg-bot/state.json > /tmp/state.json.tmp && mv /tmp/state.json.tmp ~/.claude-tg-bot/state.json
```

On the next user message, `bot.py` will detect the missing session ID and start a
new `claude` session, capturing the new ID. No manual `claude` invocation is
needed — the bot handles this automatically.

## Known issues

- **`claude -p ... -c` may silently start a new session** instead of continuing the
  existing one. Use `--resume <session_id>` instead of `-c` for session
  continuation. Tracked as `anthropics/claude-code#43696`.
- **Docker Desktop WSL integration leaves a `docker` stub on `$PATH`** even after
  toggling the integration off in the UI. This sometimes requires a full shell
  restart (`exit` and open a new WSL terminal), not just `exec bash`.
- **WSL2 VM can hang on Windows host suspend/resume.** Symptom: `wsl --status`
  reports running, but no command inside WSL responds (including `ls`). Fix from
  Windows side:

  ```powershell
  wsl.exe --shutdown
  ```

  Then open a new WSL terminal. All services restart via systemd.
- **Ollama first-load delay after VM restart.** The model weights are evicted from
  RAM on WSL shutdown. The first memory operation after a fresh WSL start will
  take ~30 seconds of warm-up. This is not a bug — it is the model loading into
  RAM. Subsequent calls are fast.
- **`/etc/wsl.conf` changes do not apply until full VM restart.** Exiting the WSL
  shell is not enough. You must run `wsl.exe --shutdown` from Windows and then
  start a new WSL terminal. The VM boots fresh and reads `/etc/wsl.conf` at that
  point.
- **`core.autocrlf=true` on Windows-side git** can override `.gitattributes` and
  re-introduce CRLF on checkout within WSL if the repo is accessed via `/mnt/c/`.
  Always configure WSL-side git separately and keep the repo on the ext4
  filesystem.
