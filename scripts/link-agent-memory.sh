#!/usr/bin/env bash
# link-agent-memory.sh — make Claude Code's auto-memory travel with the repo.
#
# WHY THIS EXISTS
#   Claude Code keeps per-project memory OUTSIDE the repo, at
#       $HOME/.claude/projects/<slug>/memory/
#   where <slug> is the checkout's ABSOLUTE path with every '/' replaced by
#   '-'. That slug differs on every machine, so a memory directory committed
#   to git is never picked up on its own. This script points the per-machine
#   location at the in-repo copy, so memory written by any session lands in
#   git, and memory pulled from git is visible to the next session — on any
#   machine, under any checkout path.
#
#   The repo copy is the source of truth. It is plain markdown, so agents that
#   know nothing about Claude Code's memory layout (a cloud session, a
#   non-Claude runner) can still read it directly. See memory-bank/README.md.
#
# SAFETY
#   Idempotent, and it never deletes memory. An existing real directory is
#   first merged into the repo copy (any file the repo lacks is copied in),
#   then archived next to itself, and only then replaced by the symlink.
#
# Usage:
#   scripts/link-agent-memory.sh [--dry-run] [--force]
#
#   --dry-run   report what would happen; change nothing
#   --force     replace a symlink that points somewhere other than this repo
#
# Exit codes:
#   0  linked, already linked, or dry run
#   1  usage error
#   2  refused (foreign symlink without --force, or unwritable target)

set -euo pipefail

DRY_RUN=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --force)   FORCE=1; shift ;;
    -h|--help)
      sed -n '/^# Usage:/,/^$/{ /^#/!d; s/^# \{0,1\}//; p }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) printf 'link-agent-memory: unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_MEM="$REPO_ROOT/memory-bank/agent-memory"
# Claude Code's slug: absolute path, '/' -> '-'. A leading '-' is expected,
# since the path starts at the filesystem root.
SLUG="${REPO_ROOT//\//-}"
LIVE_PARENT="${HOME}/.claude/projects/${SLUG}"
LIVE_MEM="${LIVE_PARENT}/memory"

log()  { printf '[link-memory] %s\n' "$*"; }
warn() { printf '[link-memory] WARN %s\n' "$*" >&2; }
die()  { printf '[link-memory] FATAL: %s\n' "$*" >&2; exit "${2:-1}"; }

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[link-memory] would: %s\n' "$*"
  else
    "$@"
  fi
}

log "repo:  $REPO_ROOT"
log "slug:  $SLUG"
log "live:  $LIVE_MEM"

# ---------------------------------------------------------------------------
# Case 1 — already linked to this repo. Nothing to do.
# ---------------------------------------------------------------------------
if [[ -L "$LIVE_MEM" ]]; then
  current="$(readlink "$LIVE_MEM")"
  if [[ "$current" == "$REPO_MEM" ]]; then
    log "already linked — nothing to do."
    exit 0
  fi
  if [[ "$FORCE" -eq 0 ]]; then
    warn "existing symlink points elsewhere: $current"
    die "refusing to replace it; re-run with --force if that is intended." 2
  fi
  log "replacing foreign symlink -> $current (--force)"
  run rm "$LIVE_MEM"
fi

# ---------------------------------------------------------------------------
# Case 2 — a real directory is already there. Merge it into the repo copy,
# then archive it. Repo copy wins on same-name conflicts: it is the version
# that other machines have agreed on. Anything the repo lacks is preserved.
# ---------------------------------------------------------------------------
if [[ -d "$LIVE_MEM" && ! -L "$LIVE_MEM" ]]; then
  run mkdir -p "$REPO_MEM"
  merged=0
  kept=0
  shopt -s nullglob dotglob
  for src in "$LIVE_MEM"/*; do
    [[ -f "$src" ]] || continue
    base="$(basename "$src")"
    if [[ -e "$REPO_MEM/$base" ]]; then
      if cmp -s "$src" "$REPO_MEM/$base"; then
        kept=$((kept + 1))
      else
        warn "differs, keeping the repo version: $base"
        kept=$((kept + 1))
      fi
    else
      run cp -p "$src" "$REPO_MEM/$base"
      log "merged into repo: $base"
      merged=$((merged + 1))
    fi
  done
  shopt -u nullglob dotglob
  log "merge: $merged copied in, $kept already present"

  archive="${LIVE_MEM}.pre-link"
  if [[ -e "$archive" ]]; then
    archive="${LIVE_MEM}.pre-link.$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  log "archiving the old directory -> $archive"
  run mv "$LIVE_MEM" "$archive"
fi

# ---------------------------------------------------------------------------
# Case 3 — create the link. The repo copy must exist first; a dangling symlink
# would make Claude Code's memory reads fail rather than come back empty.
# ---------------------------------------------------------------------------
if [[ ! -d "$REPO_MEM" ]]; then
  run mkdir -p "$REPO_MEM"
  log "created an empty repo memory directory: $REPO_MEM"
fi

run mkdir -p "$LIVE_PARENT"
run ln -s "$REPO_MEM" "$LIVE_MEM"
log "linked: $LIVE_MEM -> $REPO_MEM"

if [[ "$DRY_RUN" -eq 0 ]]; then
  # Prove the link resolves to the repo copy before declaring success.
  resolved="$(cd "$LIVE_MEM" 2>/dev/null && pwd -P || true)"
  expected="$(cd "$REPO_MEM" && pwd -P)"
  [[ "$resolved" == "$expected" ]] \
    || die "verification failed: link resolves to '${resolved:-<nothing>}'" 2
  count=$(find "$REPO_MEM" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' ')
  log "verified. $count memory file(s) visible through the link."
fi
