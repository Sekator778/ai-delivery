#!/usr/bin/env bash
# aidstack.sh - control the ai-delivery pipeline stack on macOS/arm64 (atlas).
#
# Replaces the systemd units in ops/systemd/ (no systemd on macOS) for the
# three long-running daemons plus the mem0 (Qdrant) Docker stack. See
# ops/atlas/README.md for what is and isn't covered.
#
# Usage:
#   aidstack.sh up       start mem0 (Qdrant), ensure bot/venv, then start the
#                        dispatcher + watcher daemons and the Telegram bot
#                        (bot only if bot/.env has a real TELEGRAM_BOT_TOKEN)
#   aidstack.sh down     stop the daemons (TERM, then KILL fallback), kill any
#                        orphaned claude children left behind (#18), stop the
#                        mem0 compose stack (volumes kept), release the Docker
#                        engine if nothing else needs it
#   aidstack.sh status   daemon pidfile liveness + container status + qdrant
#                        and TEI health
#   aidstack.sh logs [dispatcher|watcher|bot]   tail -f the daemon log (default: dispatcher)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "$REPO_ROOT/services/stacks/mem0/docker-compose.yml")
PID_DIR="$REPO_ROOT/.pids"
LOG_DIR="$REPO_ROOT/logs"
VENV_DIR="$REPO_ROOT/bot/venv"
VENV_PY="$VENV_DIR/bin/python3"
DAEMONS=(dispatcher watcher bot)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Memory service endpoints.
#
# Resolved exactly the way dispatcher/memory_inject.py resolves them: the
# environment first, then bot/.env (which is what the daemons load), then the
# module's own defaults. Nothing here is a second copy of a port — moving
# Qdrant or TEI to another host is one edit in bot/.env.
# ---------------------------------------------------------------------------

# Read KEY=value from bot/.env without sourcing it (that file carries tokens
# and is loaded per-daemon on purpose). Last assignment wins, like a source.
dotenv_value() {
  local key="$1" file="$REPO_ROOT/bot/.env"
  [ -f "$file" ] || return 1
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$file" \
    | tail -1 \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

memory_url() {
  local key="$1" fallback="$2" value
  value="$(printenv "$key" 2>/dev/null || true)"
  [ -n "$value" ] || value="$(dotenv_value "$key" || true)"
  printf '%s' "${value:-$fallback}"
}

QDRANT_URL="$(memory_url MEMORY_QDRANT_URL http://127.0.0.1:6333)"
TEI_URL="$(memory_url MEMORY_TEI_URL http://127.0.0.1:8087)"
# launchd label of the external TEI agent (owned by another project on this
# host) — lets the TEI warnings print the exact start command. Optional.
TEI_LAUNCHD_LABEL="$(memory_url TEI_LAUNCHD_LABEL "")"

# The exact command to bring the external TEI server up, for warn messages.
# $(id -u) stays literal on purpose: the hint is meant to be copy-pasted.
tei_start_hint() {
  if [ -n "$TEI_LAUNCHD_LABEL" ]; then
    printf 'launchctl kickstart -k gui/$(id -u)/%s' "$TEI_LAUNCHD_LABEL"
  else
    printf 'its launchd agent (set TEI_LAUNCHD_LABEL in bot/.env for the exact command)'
  fi
}

# ---------------------------------------------------------------------------
# Docker engine (OrbStack)
# ---------------------------------------------------------------------------

# OrbStack keeps a login helper (`OrbStack --internal-cli-background`) running at
# all times, so `open -a OrbStack` finds the app already up and returns without
# booting the VM. orbctl is the only thing that actually starts and stops it.
orbctl_bin() {
  if command -v orbctl >/dev/null 2>&1; then
    echo orbctl
  elif [ -x "$HOME/.orbstack/bin/orbctl" ]; then
    echo "$HOME/.orbstack/bin/orbctl"
  else
    return 1
  fi
}

start_docker_engine() {
  local orb
  docker info >/dev/null 2>&1 && return 0
  if ! orb="$(orbctl_bin)"; then
    warn "the Docker engine is down and orbctl was not found - start OrbStack by hand"
    return 1
  fi
  log "starting the OrbStack engine"
  "$orb" start >/dev/null 2>&1 || true
  for _ in $(seq 30); do
    docker info >/dev/null 2>&1 && { log "the Docker engine is up"; return 0; }
    sleep 1
  done
  warn "the Docker engine did not come up in 30s - check '$orb status'"
  return 1
}

# The engine is shared with other stacks on this machine (other projects'
# containers), so it may only be stopped once this stack was the last thing
# left on it.
stop_docker_engine() {
  local orb others
  others="$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')" || others=0
  if [ "$others" != 0 ]; then
    warn "$others other container(s) are still running - leaving the Docker engine up"
    return 0
  fi
  if ! orb="$(orbctl_bin)"; then
    warn "orbctl was not found - leaving the Docker engine up"
    return 0
  fi
  log "stopping the OrbStack engine"
  "$orb" stop
}

wait_qdrant() {
  local tries="${1:-60}"
  log "waiting for qdrant ..."
  for _ in $(seq "$tries"); do
    if curl -fsS -o /dev/null -m 2 "$QDRANT_URL/readyz" 2>/dev/null \
       || curl -fsS -o /dev/null -m 2 "$QDRANT_URL/healthz" 2>/dev/null \
       || curl -fsS -o /dev/null -m 2 "$QDRANT_URL/collections" 2>/dev/null; then
      log "qdrant is up on $QDRANT_URL"
      return 0
    fi
    sleep 1
  done
  warn "qdrant did not answer on $QDRANT_URL within ${tries}s - see: aidstack.sh logs"
  return 1
}

# TEI — the embedding half of the memory path, and NOT part of this stack.
#
# dispatcher/memory_inject.py needs two services: Qdrant (this compose file)
# and a text-embeddings-inference server on MEMORY_TEI_URL. The second one is
# a native Metal binary owned by another project on this host and run by its
# own launchd agent (label in TEI_LAUNCHD_LABEL), with RunAtLoad=false and
# KeepAlive=false — so it does not come back after a reboot, and nothing in
# this repo starts it.
# Containerising a second copy would waste the bge-m3 cache and fight that
# agent for the port; see ops/atlas/README.md.
#
# Why this probe exists: memory_inject's contract is that every public function
# degrades to a no-op so a stage never fails because memory infra is down.
# Correct — and it means a dead TEI is completely silent. `aidup` reports a
# healthy stack, every stage runs, recall injects nothing, write-back stores
# nothing, and no log line says so. This is the one place that can say it.
#
# Probe /info, not /health: TEI serves no /health endpoint, so probing it
# returns nothing and reads as "down" on a perfectly healthy server. GET /info
# returns the model card ({"model_id":"BAAI/bge-m3",...}).
wait_tei() {
  local tries="${1:-5}"
  log "checking the TEI embedding server ..."
  for _ in $(seq "$tries"); do
    if curl -fsS -o /dev/null -m 2 "$TEI_URL/info" 2>/dev/null; then
      log "TEI is up on $TEI_URL"
      return 0
    fi
    sleep 1
  done
  warn "TEI did not answer on $TEI_URL/info within ${tries}s."
  warn "  The stack is up and every stage will run normally - memory just goes"
  warn "  quiet: recall injects nothing and write-back stores nothing, and no"
  warn "  log line will tell you. This warning is the only signal."
  warn "  TEI is external to this stack (launchd agent, another project)."
  warn "  Start it:  $(tei_start_hint)"
  return 1
}

# ---------------------------------------------------------------------------
# Python / venv
# ---------------------------------------------------------------------------

# Pick the newest python3 >= 3.10 available. The system python3 on this
# machine is 3.9 (too old for the fastembed/aiohttp pins in
# bot/requirements.txt); Homebrew only installs versioned binaries
# (python3.12, not an unversioned python3) unless the plain `python3`
# formula was also installed, so probe both.
pick_python() {
  local candidates=(
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    /opt/homebrew/bin/python3.10
    /opt/homebrew/bin/python3
    python3
  )
  local c resolved ver major minor
  for c in "${candidates[@]}"; do
    resolved="$(command -v "$c" 2>/dev/null)" || continue
    ver="$("$resolved" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
    major="${ver%%.*}"; minor="${ver#*.}"
    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }; then
      echo "$resolved"
      return 0
    fi
  done
  return 1
}

ensure_venv() {
  if [ -x "$VENV_PY" ]; then
    return 0
  fi
  local py
  if ! py="$(pick_python)"; then
    warn "no python3 >= 3.10 found (system python3 is $(python3 --version 2>&1))"
    warn "checked /opt/homebrew/bin/python3.{10,11,12,13}, /opt/homebrew/bin/python3, ambient python3"
    warn "checked 'brew list python@3.12 python@3.11' - none installed"
    warn "fix: brew install python@3.12 (network permitting), then re-run 'up'"
    return 1
  fi
  log "creating bot/venv with $py ($("$py" --version 2>&1))"
  "$py" -m venv "$VENV_DIR"
  log "installing bot/requirements.txt into bot/venv"
  if ! "$VENV_PY" -m pip install -q -r "$REPO_ROOT/bot/requirements.txt"; then
    warn "pip install failed (network blocked?) - venv exists but dependencies are incomplete"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Daemon lifecycle - pidfile in .pids/, one-generation log rotation in logs/.
# ---------------------------------------------------------------------------

pidfile_alive() {
  local pidfile="$1" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

rotate_log() {
  local logfile="$1"
  [ -f "$logfile" ] && mv -f "$logfile" "$logfile.prev"
  return 0
}

# Starts "$@" detached, in $REPO_ROOT, with stdout/stderr into logs/$name.log
# and its pid recorded in .pids/$name.pid. Detach discipline:
# `exec` re-points the subshell's own descriptors before the daemon is forked,
# so neither it nor anything it spawns is left holding this script's stdout.
start_daemon() {
  local name="$1" workdir="$2"; shift 2
  local pidfile="$PID_DIR/$name.pid" logfile="$LOG_DIR/$name.log"
  if pidfile_alive "$pidfile"; then
    log "$name already running (pid=$(cat "$pidfile"))"
    return 0
  fi
  rm -f "$pidfile"
  mkdir -p "$PID_DIR" "$LOG_DIR"
  rotate_log "$logfile"
  log "starting $name"
  (
    cd "$workdir" || exit 1
    exec </dev/null >"$logfile" 2>&1
    nohup "$@" &
    echo $! >"$pidfile"
  )
}

stop_daemon() {
  local name="$1" pidfile="$PID_DIR/$name.pid" pid
  if [ ! -f "$pidfile" ]; then
    log "$name is not running (no pidfile)"
    return 0
  fi
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    log "$name is not running (stale pidfile)"
    rm -f "$pidfile"
    return 0
  fi
  log "stopping $name (pid=$pid)"
  kill -TERM "$pid" 2>/dev/null || true
  # Both daemons poll in a time.sleep(POLL_INTERVAL) loop and only check the
  # SIGTERM-set exit flag once that sleep returns; Python 3 (PEP 475) resumes
  # a sleep() interrupted by a signal for its REMAINING duration instead of
  # returning immediately. The wait budget must clear the watcher's own
  # WATCHER_POLL_INTERVAL (15s default) or every stop degrades into a KILL.
  for _ in $(seq 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "$name (pid=$pid) did not exit in 20s - sending KILL"
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

daemon_status() {
  local name="$1" pidfile="$PID_DIR/$name.pid" pid
  if [ -f "$pidfile" ] && pid="$(cat "$pidfile" 2>/dev/null)" && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "$name: RUNNING (pid=$pid)"
  else
    log "$name: STOPPED"
  fi
}

# ---------------------------------------------------------------------------
# bot.py - only started when bot/.env holds a real Telegram token.
# ---------------------------------------------------------------------------

bot_configured() {
  local env_file="$REPO_ROOT/bot/.env" token
  [ -f "$env_file" ] || return 1
  token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$env_file" | tail -1 | cut -d= -f2-)"
  token="${token%\"}"; token="${token#\"}"
  [ -n "$token" ] && [ "$token" != "123456:ABC..." ]
}

start_bot() {
  if ! bot_configured; then
    warn "bot skipped: no TELEGRAM_BOT_TOKEN yet (pull bot/.env from the previous Windows machine)"
    return 0
  fi
  local pidfile="$PID_DIR/bot.pid" logfile="$LOG_DIR/bot.log"
  if pidfile_alive "$pidfile"; then
    log "bot already running (pid=$(cat "$pidfile"))"
    return 0
  fi
  rm -f "$pidfile"
  mkdir -p "$PID_DIR" "$LOG_DIR"
  rotate_log "$logfile"
  log "starting bot"
  (
    cd "$REPO_ROOT/bot" || exit 1
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    exec </dev/null >"$logfile" 2>&1
    nohup env PATH="$HOME/.claude-tg-bot/bin:$PATH" "$VENV_PY" bot.py &
    echo $! >"$pidfile"
  )
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_up() {
  start_docker_engine || return 1

  log "starting containers"
  "${COMPOSE[@]}" up -d
  wait_qdrant

  # Never fatal: TEI is genuinely optional and owned by another project, and
  # the pipeline runs without it. The stack comes up either way.
  wait_tei || true

  if ! ensure_venv; then
    warn "bot/venv is not ready - dispatcher/watcher/bot cannot start (see above)"
    return 1
  fi

  # Both daemons load bot/.env first — the systemd units did this via
  # EnvironmentFile, and the pipeline's per-target policy lives there
  # (MERGEABLE_REPO_PATHS: without it EVERY target runs in PoC seatbelt mode —
  # phase-b4-poc-* branches + [PoC, DO NOT MERGE] titles). Explicit vars after
  # the source override the file.
  # TRIAGE_MODE=s-only: act on S-tier tasks (short pipeline) — ai-delivery-private#2.
  # STAGE_TIMEOUT_SEC=1800: the 900s default killed a productive M-tier TDD
  # developer stage mid-commit (rc=124).
  start_daemon dispatcher "$REPO_ROOT" \
    bash -c 'set -a; [ -f bot/.env ] && . bot/.env; set +a; exec env PYTHONUNBUFFERED=1 DISPATCHER_POLL_INTERVAL=5 BOT_HTTP_URL=http://127.0.0.1:8766 TRIAGE_MODE=s-only STAGE_TIMEOUT_SEC=1800 "$1" dispatcher/task_dispatcher.py' _ "$VENV_PY"

  start_daemon watcher "$REPO_ROOT" \
    bash -c 'set -a; [ -f bot/.env ] && . bot/.env; set +a; exec env PYTHONUNBUFFERED=1 WATCHER_POLL_INTERVAL=15 "$1" dispatcher/watcher.py' _ "$VENV_PY"

  start_bot

  cmd_status
}

# A claude child whose runner was killed survives re-parented to init (ppid 1)
# and keeps burning the subscription — 3h11m unnoticed on 2026-08-14 (#18).
# Bringing the stack down must not leave one behind. The matcher (ppid==1, no
# tty, both pipeline flags) lives in dispatcher/proc_reaper.py: it can never
# select an interactive Claude Code session, which has a tty and a live parent.
sweep_orphan_children() {
  local py="$VENV_PY"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  if [ -z "$py" ]; then
    warn "no python3 - skipping the orphaned-claude sweep"
    return 0
  fi
  log "sweeping orphaned claude children"
  "$py" "$REPO_ROOT/dispatcher/proc_reaper.py" || warn "orphan sweep failed"
}

cmd_down() {
  local name
  for name in "${DAEMONS[@]}"; do
    stop_daemon "$name"
  done

  sweep_orphan_children

  if docker info >/dev/null 2>&1; then
    log "stopping containers"
    "${COMPOSE[@]}" down
    stop_docker_engine
  else
    warn "the Docker engine is already down, so there are no containers to stop"
  fi
}

cmd_status() {
  log "daemons"
  local name
  for name in "${DAEMONS[@]}"; do
    daemon_status "$name"
  done

  log "containers"
  if docker info >/dev/null 2>&1; then
    "${COMPOSE[@]}" ps --format 'table {{.Name}}\t{{.Service}}\t{{.Status}}'
  else
    warn "the Docker engine is not running"
  fi

  if curl -fsS -o /dev/null -m 2 "$QDRANT_URL/collections" 2>/dev/null; then
    log "qdrant: healthy on $QDRANT_URL"
  else
    warn "qdrant: not reachable on $QDRANT_URL"
  fi

  if curl -fsS -o /dev/null -m 2 "$TEI_URL/info" 2>/dev/null; then
    log "TEI: healthy on $TEI_URL (external to this stack)"
  else
    warn "TEI: not reachable on $TEI_URL - memory recall and write-back no-op"
    warn "  Start it:  $(tei_start_hint)"
  fi
}

cmd_logs() {
  local name="${1:-dispatcher}" logfile
  logfile="$LOG_DIR/$name.log"
  if [ ! -f "$logfile" ]; then
    warn "no log file at $logfile"
    return 1
  fi
  tail -f "$logfile"
}

case "${1:-status}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  logs)   shift; cmd_logs ${1+"$@"} ;;
  # Print the header comment block, whatever length it happens to be.
  *)      awk 'NR>1 && /^#/ { print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
