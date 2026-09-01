# ai-delivery on macOS/arm64 (atlas)

Adaptation notes for running this repo on a Mac instead of the WSL2/Ubuntu
host `ops/INSTALL.md` and `ops/systemd/` assume.

## What differs on the Mac

- **`ops/atlas/aidstack.sh` instead of systemd.** macOS has no systemd/journald.
  The three `ops/systemd/*.service` units (`claude-tg-bot`, `task-dispatcher`,
  `watcher`) become one supervisor script with pidfiles in `.pids/` and
  logfiles in `logs/`.
  No launchd plists — a script matches the project's existing
  `.runner.pid`-per-task convention more closely and is faster to iterate on.
- **mem0 (Qdrant) is the only Docker stack `up` brings up by default.**
  Voice, Windmill, and LiteLLM are deferred (see below) — bring them up by
  hand with their own `docker compose -f <stack>/docker-compose.yml up -d`
  once needed; `aidstack.sh` does not manage them.
- **The TEI embedding server is external to this stack and nothing here
  starts it.** `dispatcher/memory_inject.py` needs two services: Qdrant on
  `MEMORY_QDRANT_URL` (the mem0 compose stack above) and a
  text-embeddings-inference server on `MEMORY_TEI_URL` (default
  `http://127.0.0.1:8087`). The second is a native Metal binary owned by
  **another project on this host**, run by its own launchd agent
  (`text-embeddings-router --model-id BAAI/bge-m3 --port 8087`). It is
  deliberately not containerised here: a second copy would duplicate the
  bge-m3 cache and fight that agent for the port. Set the agent's label in
  `TEI_LAUNCHD_LABEL` in `bot/.env` — `aidstack.sh` reads it and prints the
  exact start command in its TEI warnings.

  The agent has `RunAtLoad=false` and `KeepAlive=false`, so **it does not come
  back after a reboot**. That matters because `memory_inject`'s contract is
  that every public function degrades to a no-op rather than failing a stage —
  correct for the pipeline, and the reason a dead TEI is otherwise completely
  silent: stages run, recall injects nothing, write-back stores nothing, and
  no log line says so. `aidstack.sh up` and `status` now probe it and warn;
  the probe hits `/info` (TEI serves no `/health`, so probing that reads as
  "down" on a healthy server). A TEI-down `up` still brings the stack up.

  Start it by hand:

  ```bash
  launchctl kickstart -k "gui/$(id -u)/$TEI_LAUNCHD_LABEL"
  curl -s 127.0.0.1:8087/info      # {"model_id":"BAAI/bge-m3",...}
  ```

  Moved it elsewhere? Set `MEMORY_TEI_URL` in `bot/.env` — `aidstack.sh` reads
  the same variable the daemons do, so there is one place to change.
- **Docker engine is OrbStack**, started/stopped via `orbctl`. The engine is
  shared with other stacks on this machine (e.g. another project's Qdrant
  container on its own port) — `aidstack.sh down` only stops the engine when
  no other containers are left running on it.
- **`bot/venv` is built with a Homebrew python3, not the system one.** The
  system `python3` on this Mac is 3.9.6 — too old for the `fastembed`/`aiohttp`
  pins in `bot/requirements.txt` (repo map says assume 3.10+). `aidstack.sh up`
  probes `/opt/homebrew/bin/python3.{13,12,11,10}`, then an unversioned
  `/opt/homebrew/bin/python3`, then ambient `python3`, and picks the newest
  match. On this machine that resolved to `/opt/homebrew/bin/python3.12`
  (installed via `brew install python@3.12`, network permitting — Homebrew
  only ships versioned binaries per formula, no `python3` symlink unless the
  plain `python3` formula is also installed).
- **Two code fixes for Linux-only assumptions** (repo map §6.1/§6.2):
  - `dispatcher/watcher.py:_pid_is_alive` read `/proc/{pid}/cmdline`, which
    doesn't exist on macOS (no procfs). Now shells out to
    `ps -p <pid> -o command=`, which reports the same information on both
    Linux and macOS. Same liveness semantics (`os.kill(pid, 0)` pre-check +
    cmdline substring match against the expected stage_runner script name and
    task id), no new dependency.
  - `bin/botctl-list-projects` used GNU-only `find -printf '%h\n'`. BSD find
    (macOS default) doesn't support `-printf`; rewritten as
    `find ... -name .git -maxdepth 2 -type d -exec dirname {} \;`, which works
    identically on both.

### Deferred stacks and why

| Stack | Why deferred |
|---|---|
| `services/stacks/voice/` (Whisper STT + Silero TTS) | RUNNING on atlas since 2026-08-13: the predicted arm64 break (`torch==2.4.1+cpu` index has no aarch64 wheels) hit on first build and was fixed with a per-arch install (TARGETARCH conditional in silero-server/Dockerfile). Bring up: `docker compose -f services/stacks/voice/docker-compose.yml up -d` (whisper model via download-models.sh once, ~1.5GB). |
| `windmill/` (cron scheduling, `/schedule`) | Highest image count (7) and most arm64-unknowns of any stack — `windmill:main`, `windmill-extra`, and `caddy-l4` all need `docker manifest inspect --platform linux/arm64` verification before trusting them (repo map §4). Only `/schedule` degrades without it; the core pipeline doesn't touch Windmill. |
| `ops/litellm/` (DeepSeek/GLM proxy) | Purely opt-in — only used when `LITELLM_PROXY_URL` is set in `bot/.env`, which it isn't by default. DeepSeek/GLM calls go direct to the provider APIs without it. |

Bring these up individually once needed, following the recommendation in the
repo map (§4): mem0 → voice → Windmill, ascending arm64 risk.

## Starting and stopping

```bash
ops/atlas/aidstack.sh up       # mem0 (Qdrant) + bot/venv + dispatcher + watcher (+ bot if configured)
ops/atlas/aidstack.sh restart  # THE DEPLOY: down, then up — see below
ops/atlas/aidstack.sh down     # stop daemons, stop mem0 (volumes kept), release the Docker engine if idle
ops/atlas/aidstack.sh status   # daemon pidfile liveness + container status + qdrant and TEI health
ops/atlas/aidstack.sh logs [dispatcher|watcher|bot]   # tail -f (default: dispatcher)
```

The bot only starts when `bot/.env` has a real `TELEGRAM_BOT_TOKEN` (not the
`123456:ABC...` placeholder) — otherwise `up` warns and skips it. Dispatcher
and watcher start unconditionally; they don't need the Telegram token.

## Starting the stand is the deploy

There is no scheduler, no agent and no background process. `aidup` takes the
newest commit of the branch the checkout is on, then starts. If there is
nothing new, or anything at all is in the way, it starts what is already
checked out.

```bash
aidup                     # update if there is one, then start
ops/atlas/aidstack.sh pull   # just the update step, no start
AIDUP_PULL=0 aidup        # start without touching git
```

### `up` does not deploy to a stand that is already running

`up` starts what is not running and deliberately never touches a live daemon.
On a **cold** stand that is the deploy: nothing is running, so everything starts
on the code just pulled. On a **live** stand it is not, and the difference is
easy to miss:

| | code it executes after `aidup` |
|---|---|
| dispatcher / watcher, already running | **old** — the modules they imported at start |
| a stage spawned from now on | **new** — a fresh process reading the new files |
| persona and prompt files | **new** — read from disk per stage |

So the orchestrator and the stages it orchestrates end up on different versions
of this repository. If a commit changes the contract between them — a
`state.json` field, the handoff format, the stage argv — a task that straddles
the pull breaks in a way that will not reproduce.

`up` now warns when it pulled something while daemons were already up. To
actually deploy:

```bash
ops/atlas/aidstack.sh restart --wait    # finish the current task, then restart
ops/atlas/aidstack.sh restart           # restart now; refuses if a task is running
ops/atlas/aidstack.sh restart --force   # restart over a running task, deliberately
```

### `restart` and `down` refuse while a task is running

Both stop the daemons, and `down` also sweeps orphaned `claude` children — so
either will kill a stage in flight. A stage killed halfway is a paid Claude
call thrown away; the watcher resuming from `state.json` does not refund it.
One fully green task cost $14.56 (`8f7619e`).

They refuse when any task under `tasks/active/` has a live runner, naming the
tasks and both ways forward. `--wait` polls until they finish
(`AIDSTACK_WAIT_TIMEOUT`, default 3600s) and **refuses on timeout** rather than
falling through to a kill — timing out into `--force` would be exactly the
silent kill the guard exists to prevent.

Liveness is answered by `dispatcher/runner_liveness.py`, the same module the
watcher uses — it is not a `kill -0` in shell. A pidfile outlives its process
and pids get reused, so the check also matches the process command line against
the runner script and the task id. A shell approximation would call a recycled
pid a live runner and block deploys forever, and could disagree with what the
watcher believes about the same task.

**The update never blocks the start.** Every obstacle is a warning, not a
failure — dirty tree, unpushed local commits, diverged history, detached HEAD,
no network. A stand that refuses to come up because git had an opinion is worse
than a stand running last week's code, and you are standing right there reading
the output.

| Situation | What happens |
|---|---|
| remote branch moved ahead | fast-forward, then start |
| nothing new | start |
| uncommitted changes | keep them, start as is |
| local branch ahead of origin | start as is (you have unpushed work) |
| local and origin diverged | start as is, no guessing |
| detached HEAD | start as is |
| fetch failed (offline) | start as is |

It follows whatever branch is checked out, so no branch name is configured in
two places — on `dev` it takes `origin/dev`, on a release branch it takes that.

## Where logs and pids live

- `.pids/{dispatcher,watcher,bot}.pid` — one pidfile per daemon, at the repo
  root.
- `logs/{dispatcher,watcher,bot}.log` — one log per daemon, at the repo root.
  One-generation rotation: each `up` run that (re)starts a daemon moves the
  previous log to `<name>.log.prev` first.
- Both directories are gitignored (`.pids/`, `logs/` in `.gitignore`) —
  runtime state, not history.

## What still awaits data from the previous Windows machine (WSL2)

Per repo map §5, none of this is regenerable — pull it from
`~/projects/ai-delivery` on the previous Windows machine's WSL2 host:

- **`STATE/`** (`CURRENT.md`, `ROADMAP.md`, `DECISIONS.md`) — missing from
  this clone entirely (stripped from the public-mirror checkout this
  adaptation started from). No other way to recover "what was in flight."
- **`tasks/{active,awaiting-input,awaiting-approval,inbox}/*`** — in-flight
  task state (`state.json`, `worklog.md`, per-stage artifacts). Gitignored
  by design, only exists on disk.
- **`bot/.env` secrets** — `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID`,
  `DEEPSEEK_API_KEY`/`GLM_API_KEY` (if used), `CC_LANGSMITH_API_KEY` (if
  used). This adaptation created `bot/.env` from `bot/.env.example` with a
  `# TODO: real values from the previous machine's WSL ~/projects/ai-delivery/bot/.env`
  marker at the top — every value is still the placeholder from the example
  file.
- **`services/stacks/mem0/qdrant-data/`** — cross-task semantic memory
  (`/memo`/`/recall` facts, hook-captured history). Optional — `/memo`
  auto-creates a fresh collection if this isn't copied over.
- **`memory-bank/`** in each target repo (not in `ai-delivery` itself) — BA/
  Architect stages read this per target project.
- **`bot/projects.json`** — created by this adaptation with the target-repo
  aliases pointing at their absolute paths on this Mac; not a pull from the
  previous machine, just host-specific and gitignored like the original.
