#!/usr/bin/env bash
# scripts/docker-prune.sh — safe weekly Docker cleanup.
#
# Run by docker-prune.timer (weekly, Persistent=true) → docker-prune.service.
# Can also be run by hand at any time; it is idempotent and only ever removes
# layers/cache Docker itself classifies as unused.
#
# WHAT IT CLEANS (safe):
#   1. Dangling/untagged images that are NOT used by any container,
#      AND are at least 48 h old. The until=48h filter protects layers of an
#      in-progress build (e.g. a long maven build still warming up).
#   2. Reclaimable BuildKit cache (layers no longer referenced by any image).
#
# WHAT IT NEVER TOUCHES:
#   - tagged images        (may be pulled-but-idle, or used by stopped containers)
#   - in-use images        (Docker refuses anyway, but be explicit)
#   - volumes              (staging sessions + postgres data live in named volumes)
#   - networks
#   - containers (running OR stopped)
#
# STRICT PROHIBITIONS (do not relax without a written ADR):
#   - NEVER `docker image prune -a`           — would also delete tagged-but-unused images
#   - NEVER any prune command with `--volumes` — would wipe staging sessions / DB data
#   - NEVER `docker system prune`             — opaque blanket cleanup, touches networks/containers
#   - NEVER `docker rmi <tag>`                — we do not manage tagged images by hand
#
# Background: 2026-06-07 the host had accumulated 35 dangling images (~120 GB)
# from repeated rebuilds of windmill/worker/smoke/staging. A manual prune of
# dangling + cache reclaimed ~122 GB without touching anything live. This
# script automates that exact narrow scope.

set -euo pipefail

log() { printf '[docker-prune] %s\n' "$*"; }
hr()  { printf -- '--------------------------------------------------------\n'; }

log "host=$(hostname) user=$(id -un) date=$(date -Iseconds)"
hr
log "BEFORE — docker system df:"
docker system df
hr

log "step 1/2: docker image prune -f --filter \"until=48h\"  (dangling-only, >=48h old)"
image_out="$(docker image prune -f --filter "until=48h")"
printf '%s\n' "$image_out"
image_reclaimed="$(printf '%s\n' "$image_out" | awk -F': *' '/Total reclaimed space/{print $2}')"

hr
log "step 2/2: docker builder prune -f  (reclaimable build cache)"
cache_out="$(docker builder prune -f)"
printf '%s\n' "$cache_out"
cache_reclaimed="$(printf '%s\n' "$cache_out" | awk -F': *' '/Total:?[[:space:]]*$|Total reclaimed space/{print $2}' | tail -1)"

hr
log "AFTER — docker system df:"
docker system df
hr
log "reclaimed (dangling images): ${image_reclaimed:-0B}"
log "reclaimed (build cache):     ${cache_reclaimed:-0B}"
log "done."
