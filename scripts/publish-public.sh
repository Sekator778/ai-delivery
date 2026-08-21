#!/usr/bin/env bash
# scripts/publish-public.sh — gated, idempotent public-mirror publish.
#
# Automates the manual OSS release ritual (STATE/OPEN-SOURCE-USAGE.md).
# Exports a filtered git tree from a private ref, runs two independent
# secret/PII scans, builds one squash commit on top of the public mirror's
# main branch, and pushes only under an explicit --push flag.
#
# WHAT IT NEVER TOUCHES:
#   - The caller's branch, working tree, or index (NFR-002)
#   - .git/config (ADR-005) — the disabled pushurl sentinel stays intact
#   - Any commit already reachable from public-mirror/main (NFR-003)
#   - Untracked or gitignored files — export comes from the git tree only
#
# STRICT PROHIBITIONS (do not relax without a written ADR):
#   - NEVER git push --force / --force-with-lease / --tags / --mirror / --all
#   - NEVER git remote set-url to enable a push URL persistently
#   - NEVER eval blocklist patterns or expand paths unquoted
#   - NEVER add an env var that can widen the exclusion list or skip a scan
#   - NEVER rely on the pre-push hook for safety (NFR-007)
#   - NEVER use GIT_INDEX_FILE outside of the ephemeral-index commands
#
# Exit codes:
#   0  success (published / dry run / nothing to publish / self-check ok)
#   1  usage error
#   2  preflight failure (bad ref, missing gitleaks, missing blocklist,
#      no reachable tag, unauthorized --push)
#   3  gate finding (secrets or PII detected in export or commit message)
#   4  push/verification failure
#
# Usage:
#   scripts/publish-public.sh [OPTIONS]
#
# Options:
#   --ref <ref>            Git ref to publish (default: dev)
#   --summary <text>       Commit message summary (overrides CHANGELOG fallback)
#   --push                 Authorize a real push (requires --push-url or
#                          --allow-temp-pushurl)
#   --push-url <url>       Explicit push URL (bypasses the disabled pushurl)
#   --allow-temp-pushurl   Derive push URL from remote fetch URL (process-scoped,
#                          never written to .git/config)
#   --dry-run              Explicit dry-run (default behavior; --push overrides)
#   --keep-tmp             Keep TMPROOT after run (debugging)
#   --self-check           Offline self-test against a disposable fixture
#   -h, --help             Show this help

set -euo pipefail

# ---------------------------------------------------------------------------
# Repo root.
# PUBLISH_REPO_ROOT is a non-safety override used only by tests; it lets the
# test suite point the script at a throwaway fixture repo (ADR-010).
# ---------------------------------------------------------------------------
if [[ -n "${PUBLISH_REPO_ROOT:-}" ]]; then
  REPO_ROOT="$PUBLISH_REPO_ROOT"
else
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Config block — env-overridable non-safety knobs only (ADR-006).
# ---------------------------------------------------------------------------
PUBLISH_REMOTE="${PUBLISH_REMOTE:-public-mirror}"
PUBLISH_DEFAULT_REF="${PUBLISH_DEFAULT_REF:-dev}"

# ---------------------------------------------------------------------------
# Safety constants — readonly, never env-overridable (ADR-006).
# Widening these requires a code review and a CHANGELOG entry.
# ---------------------------------------------------------------------------
# backlog/ (added 2026-08-20) holds internal work briefs for operator-driven
# Claude Code sessions — candid findings about this deployment, host paths,
# and drift between the docs and reality. Planning artifact, not product.
readonly EXCLUDE_DIRS=(STATE research briefs memory-bank backlog)
# PUBLIC_TOPLEVEL — the fail-closed half of the top-level filter (2026-08-20,
# backlog/T03). EXCLUDE_DIRS is a blacklist: it drops what it knows about and
# publishes everything else, so a new top-level directory (notes/, clients/,
# invoices/) would ship to a public GitHub repo on the next release and neither
# scan would object — gitleaks and the blocklist catch secrets, not internal
# prose. This list inverts that: a top-level component absent from it is
# dropped, and the drop is announced under a NEW TOP-LEVEL PATH heading so
# adding a directory forces a deliberate decision instead of passing unnoticed.
# Same fail-closed stance .claude/ has had since 2026-08-15, applied at the
# root. EXCLUDE_DIRS stays as the second, redundant layer — two independent
# mechanisms, the pattern this script already uses for secrets.
#
# Derived from the actual `--ref dev` export on 2026-08-20, not from guesswork.
# Widening it publishes a directory: review it like a secret constant.
readonly PUBLIC_TOPLEVEL=(
  .claude
  .env.example
  .gitattributes
  .gitignore
  .mcp.json
  AGENTS.md
  ARCHITECTURE.md
  CHANGELOG.md
  CONTRIBUTING.md
  LICENSE
  PHASE-0-EXECUTION.md
  README.md
  USAGE.md
  WORKFLOW.md
  bin
  bot
  dispatcher
  docs
  meta
  ops
  scripts
  services
  skills
  tasks
  tests
  windmill
)
# .claude/ is NOT a blanket exclusion — it is split (2026-08-15). The agent
# persona definitions ARE the product: dispatcher/stage_prompts.py dispatches
# subagents by the names defined in .claude/agents/, so a mirror without them
# ships a pipeline whose stages cannot run (and, since v1.0.1,
# tests/test_reviewer_lenses.py that cannot pass). Excluding the whole directory
# was over-broad. Kept public: agents + commands, scanned clean of host paths and
# private-repo references. Still excluded: templates/ (vendored BMAD + spec-kit,
# upstream licensing — publishing them is a separate owner decision) and
# settings.json (operator harness config, not product).
readonly CLAUDE_KEEP_PREFIXES=(.claude/agents .claude/commands)
# CLAUDE.md documents the private repo's two-remote push workflow (private vs
# public audience, personal GitHub org, local hook install) and links to
# STATE/ paths that don't exist in the export — operator-specific, not a
# public-repo artifact. The public mirror has never carried a top-level
# CLAUDE.md; keep it that way rather than sanitizing content that stops
# making sense once you're already inside the public-only repo.
readonly EXCLUDE_FILES=(bot/projects.json CLAUDE.md)
# tasks/ keep list: anything else under tasks/ is excluded
readonly TASKS_KEEP_PATTERN="tasks/_TEMPLATE"
readonly BLOCKLIST_FILE="$REPO_ROOT/ops/publish-blocklist.local"

# ---------------------------------------------------------------------------
# Logging helpers.
# ---------------------------------------------------------------------------
step() { printf '[publish] >>> %s\n' "$*"; }
ok()   { printf '[publish] OK  %s\n' "$*"; }
skip() { printf '[publish] --  %s\n' "$*"; }
warn() { printf '[publish] WARN %s\n' "$*" >&2; }
die()  { printf '[publish] FATAL: %s\n' "$*" >&2; exit "${2:-1}"; }

# ---------------------------------------------------------------------------
# Temp root + cleanup trap (ADR-009).
# One owner for all temp state; removed on EXIT/INT/TERM/HUP.
# ---------------------------------------------------------------------------
TMPROOT=""
KEEP_TMP=0

cleanup() {
  local exit_code=$?
  if [[ "$KEEP_TMP" -eq 1 && -n "${TMPROOT:-}" ]]; then
    warn "TMPROOT kept at: $TMPROOT"
  elif [[ -n "${TMPROOT:-}" && "$TMPROOT" == /tmp/* ]]; then
    rm -rf "$TMPROOT"
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM HUP

# ---------------------------------------------------------------------------
# Argument parser.
# ---------------------------------------------------------------------------
REF=""
SUMMARY=""
DO_PUSH=0
PUSH_URL=""
ALLOW_TEMP_PUSHURL=0
SELF_CHECK=0

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ref)
        [[ $# -ge 2 ]] || die "--ref requires a value" 1
        REF="$2"; shift 2 ;;
      --summary)
        [[ $# -ge 2 ]] || die "--summary requires a value" 1
        SUMMARY="$2"; shift 2 ;;
      --push)              DO_PUSH=1; shift ;;
      --push-url)
        [[ $# -ge 2 ]] || die "--push-url requires a value" 1
        PUSH_URL="$2"; shift 2 ;;
      --allow-temp-pushurl) ALLOW_TEMP_PUSHURL=1; shift ;;
      --dry-run)           DO_PUSH=0; shift ;;
      --keep-tmp)          KEEP_TMP=1; shift ;;
      --self-check)        SELF_CHECK=1; shift ;;
      -h|--help)
        sed -n '/^# Usage:/,/^[^#]/{ /^#/!d; s/^# \{0,1\}//; p }' \
          "${BASH_SOURCE[0]}"
        exit 0 ;;
      *) die "Unknown option: $1" 1 ;;
    esac
  done
  REF="${REF:-$PUBLISH_DEFAULT_REF}"
}

# ---------------------------------------------------------------------------
# self_check — offline fixture smoke test (FR-024).
# ---------------------------------------------------------------------------
self_check() {
  step "self-check: building disposable fixture"
  TMPROOT="$(mktemp -d)"

  local src="$TMPROOT/src" mirror="$TMPROOT/mirror.git" seed="$TMPROOT/seed"
  local remote_name="sc-mirror"

  # Source repo
  git init -b dev "$src" -q
  git -C "$src" config user.email "selfcheck@publish"
  git -C "$src" config user.name "self-check"
  mkdir -p "$src/ops" "$src/tasks/_TEMPLATE" "$src/tasks/active" "$src/STATE"
  printf '# Public readme\n' > "$src/README.md"
  printf '## [Unreleased]\n\n- **Self-check.** Smoke test.\n\n## v0.1.0\n\n- Init\n' \
    > "$src/CHANGELOG.md"
  printf 'private\n' > "$src/STATE/secret.md"
  printf '' > "$src/tasks/active/.gitkeep"
  printf 'Template\n' > "$src/tasks/_TEMPLATE/task.md"
  printf '# empty\n' > "$src/ops/publish-blocklist.local"
  git -C "$src" add -A
  git -C "$src" commit -q -m "init"
  git -C "$src" tag v0.1.0

  # Seed the bare mirror
  git init --bare -b main "$mirror" -q
  git init -b main "$seed" -q
  git -C "$seed" config user.email "seed@publish"
  git -C "$seed" config user.name "seed"
  printf '# Mirror seed\n' > "$seed/README.md"
  git -C "$seed" add -A
  git -C "$seed" commit -q -m "initial mirror"
  git -C "$seed" remote add origin "$mirror"
  git -C "$seed" push origin main -q

  git -C "$src" remote add "$remote_name" "$mirror"

  # Run a dry-run against the fixture
  local sc_exit=0
  PUBLISH_REPO_ROOT="$src" PUBLISH_REMOTE="$remote_name" \
    bash "${BASH_SOURCE[0]}" --ref dev 2>&1 || sc_exit=$?

  if [[ "$sc_exit" -eq 0 ]]; then
    ok "self-check: passed"
  else
    die "self-check: dry-run exited with code $sc_exit" 2
  fi
}

# ---------------------------------------------------------------------------
# preflight — all fail-closed checks before any temp state (exit 2).
# ---------------------------------------------------------------------------
VERSION=""

preflight() {
  step "preflight: checking prerequisites"

  # Ref validity (FR-003)
  if ! git rev-parse --verify "${REF}^{commit}" >/dev/null 2>&1; then
    die "Invalid ref '${REF}' — does not resolve to a commit." 2
  fi

  # gitleaks version >= 8 (FR-007, E13)
  local gl_bin
  gl_bin="$(command -v gitleaks 2>/dev/null || true)"
  if [[ -z "$gl_bin" ]]; then
    die "gitleaks not found on PATH — install gitleaks >= 8 to continue." 2
  fi
  local gl_ver
  gl_ver=$(gitleaks version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
  gl_ver="${gl_ver:-0}"
  if [[ "$gl_ver" -lt 8 ]]; then
    die "gitleaks >= 8 required (found version ${gl_ver:-unknown})" 2
  fi

  # Blocklist present (FR-006)
  if [[ ! -f "$BLOCKLIST_FILE" ]]; then
    die "ops/publish-blocklist.local not found.
  Create it from the template and fill in your project-specific patterns:
    cp ops/publish-blocklist.local.example ops/publish-blocklist.local" 2
  fi

  # Reachable version tag (FR-014)
  if ! VERSION=$(git describe --tags --abbrev=0 "${REF}" 2>/dev/null); then
    die "No tag reachable from ref '${REF}' — a version tag is required." 2
  fi

  # Push authorization (AC-10, ADR-005)
  if [[ "$DO_PUSH" -eq 1 ]]; then
    if [[ -z "$PUSH_URL" && "$ALLOW_TEMP_PUSHURL" -eq 0 ]]; then
      die "--push requires either --push-url <url> or --allow-temp-pushurl." 2
    fi
    if [[ -n "$PUSH_URL" && "$ALLOW_TEMP_PUSHURL" -eq 1 ]]; then
      die "--push-url and --allow-temp-pushurl are mutually exclusive." 2
    fi
  fi

  ok "preflight: ref=$REF version=$VERSION gitleaks=$gl_ver blocklist=present"
}

# ---------------------------------------------------------------------------
# fetch_public — fresh public tip before building the export (FR-015).
# ---------------------------------------------------------------------------
PUBLIC_TIP=""

fetch_public() {
  step "fetch: $PUBLISH_REMOTE main"
  if ! git fetch "$PUBLISH_REMOTE" main 2>&1; then
    die "git fetch $PUBLISH_REMOTE main failed — network access required (DEP-04)." 2
  fi
  PUBLIC_TIP="$(git rev-parse FETCH_HEAD)"
  ok "fetch: public tip = ${PUBLIC_TIP:0:12}"
}

# ---------------------------------------------------------------------------
# build_export — ephemeral index + git archive; never touches caller's index
# (ADR-001).
# GIT_INDEX_FILE is set per-command; it never bleeds into other git calls.
# ---------------------------------------------------------------------------
TREE=""
EXPORT=""
# Top-level components dropped by the PUBLIC_TOPLEVEL allowlist, deduped.
# Reported loudly by report_new_toplevel(); empty on a clean run.
NEW_TOPLEVEL=()
NEW_TOPLEVEL_PATHS=()
# Plain scalar counters: under `set -u`, ${#arr[@]} on an empty array is an
# unbound-variable error in bash 3.2 (the /bin/bash on macOS).
NEW_TOPLEVEL_COUNT=0
NEW_TOPLEVEL_PATHS_COUNT=0

build_export() {
  step "export: building filtered tree from $REF"

  local idx="$TMPROOT/index"
  EXPORT="$TMPROOT/export"
  mkdir -p "$EXPORT"

  # Read the ref's tree into the ephemeral index
  GIT_INDEX_FILE="$idx" git read-tree "$REF"

  # Collect all tracked paths in the ephemeral index
  local all_paths=()
  while IFS= read -r -d '' path; do
    all_paths+=("$path")
  done < <(GIT_INDEX_FILE="$idx" git ls-files -z)

  # Build the remove list
  local remove_paths=()
  for path in "${all_paths[@]}"; do
    local top="${path%%/*}"
    local basename="${path##*/}"

    # 1. Excluded directories (in full, no carve-outs)
    local exclude_dir=0
    for d in "${EXCLUDE_DIRS[@]}"; do
      if [[ "$top" == "$d" || "$path" == "$d" ]]; then
        exclude_dir=1
        break
      fi
    done
    if [[ "$exclude_dir" -eq 1 ]]; then
      remove_paths+=("$path")
      continue
    fi

    # 2. Excluded single files
    local exclude_file=0
    for f in "${EXCLUDE_FILES[@]}"; do
      if [[ "$path" == "$f" ]]; then
        exclude_file=1
        break
      fi
    done
    if [[ "$exclude_file" -eq 1 ]]; then
      remove_paths+=("$path")
      continue
    fi

    # 3. *.env* files (FR-002) — exclude unless the filename ends in .example
    if [[ "$basename" == *.env* && "$basename" != *.example ]]; then
      remove_paths+=("$path")
      continue
    fi

    # 4. Top-level allowlist (fail-closed). A path whose top-level component
    # is not in PUBLIC_TOPLEVEL is dropped and recorded, so a directory added
    # to the private tree stays private until someone lists it deliberately.
    # Runs after the exclusion rules above so a known-private path (STATE/,
    # CLAUDE.md, bot/.env) is reported as an exclusion, not as a new decision.
    local toplevel_ok=0
    local t
    for t in "${PUBLIC_TOPLEVEL[@]}"; do
      if [[ "$top" == "$t" ]]; then
        toplevel_ok=1
        break
      fi
    done
    if [[ "$toplevel_ok" -eq 0 ]]; then
      remove_paths+=("$path")
      NEW_TOPLEVEL_PATHS+=("$path")
      NEW_TOPLEVEL_PATHS_COUNT=$(( NEW_TOPLEVEL_PATHS_COUNT + 1 ))
      local already_seen=0
      for t in ${NEW_TOPLEVEL[@]+"${NEW_TOPLEVEL[@]}"}; do
        if [[ "$t" == "$top" ]]; then
          already_seen=1
          break
        fi
      done
      if [[ "$already_seen" -eq 0 ]]; then
        NEW_TOPLEVEL+=("$top")
        NEW_TOPLEVEL_COUNT=$(( NEW_TOPLEVEL_COUNT + 1 ))
      fi
      continue
    fi

    # 5. .claude/ — exclude everything except the keep prefixes. Fail-closed:
    # a path under .claude that matches no prefix is dropped, so a new
    # subdirectory added later is private by default until it is listed here.
    if [[ "$top" == ".claude" ]]; then
      local claude_keep=0
      for p in "${CLAUDE_KEEP_PREFIXES[@]}"; do
        if [[ "$path" == "$p"/* ]]; then
          claude_keep=1
          break
        fi
      done
      if [[ "$claude_keep" -eq 0 ]]; then
        remove_paths+=("$path")
        continue
      fi
    fi

    # 6. tasks/ — exclude everything except the keep list
    if [[ "$top" == "tasks" ]]; then
      local keep=0
      # tasks/README.md
      [[ "$path" == "tasks/README.md" ]] && keep=1
      # tasks/_TEMPLATE/** (anything under _TEMPLATE)
      [[ "$path" == tasks/_TEMPLATE/* || "$path" == "tasks/_TEMPLATE" ]] && keep=1
      # tasks/*/.gitkeep (each column's marker)
      [[ "$path" == tasks/*/.gitkeep ]] && keep=1
      if [[ "$keep" -eq 0 ]]; then
        remove_paths+=("$path")
        continue
      fi
    fi
  done

  # Remove excluded paths from the ephemeral index (NUL-delimited)
  if [[ "${#remove_paths[@]}" -gt 0 ]]; then
    printf '%s\0' "${remove_paths[@]}" \
      | GIT_INDEX_FILE="$idx" git update-index -z --force-remove --stdin
  fi

  # Write the filtered tree object
  TREE=$(GIT_INDEX_FILE="$idx" git write-tree)
  ok "export: tree=$TREE"

  # Materialize for scanning (gitleaks needs real files on disk)
  git archive "$TREE" | tar -x -C "$EXPORT"

  local file_count
  file_count=$(find "$EXPORT" -type f | wc -l | tr -d ' ')
  ok "export: materialized $file_count files"

  report_new_toplevel
}

# ---------------------------------------------------------------------------
# report_new_toplevel — announce every path dropped by the PUBLIC_TOPLEVEL
# allowlist. Silence here means the private tree grew nothing new; anything
# printed is a directory somebody added without deciding whether it is public.
# Never fatal: the safe outcome (not publishing) has already happened, and
# failing the run would only tempt an operator to widen the list to get past it.
# ---------------------------------------------------------------------------
report_new_toplevel() {
  if [[ "$NEW_TOPLEVEL_PATHS_COUNT" -eq 0 ]]; then
    ok "export: no unlisted top-level paths (allowlist clean)"
    return 0
  fi

  printf '\n[publish] NEW TOP-LEVEL PATH — not published\n' >&2
  printf '[publish]   %s top-level component(s) absent from PUBLIC_TOPLEVEL:\n' \
    "$NEW_TOPLEVEL_COUNT" >&2
  local t
  for t in ${NEW_TOPLEVEL[@]+"${NEW_TOPLEVEL[@]}"}; do
    printf '[publish]     %s\n' "$t" >&2
  done
  printf '[publish]   %s file(s) dropped from the export:\n' \
    "$NEW_TOPLEVEL_PATHS_COUNT" >&2
  local path
  for path in ${NEW_TOPLEVEL_PATHS[@]+"${NEW_TOPLEVEL_PATHS[@]}"}; do
    printf '[publish]     - %s\n' "$path" >&2
  done
  printf '[publish]   Decide per component: add it to PUBLIC_TOPLEVEL in\n' >&2
  printf '[publish]   scripts/publish-public.sh to publish it, or to EXCLUDE_DIRS\n' >&2
  printf '[publish]   to record that it stays private. Until then it is private.\n\n' >&2
}

# ---------------------------------------------------------------------------
# run_gate — two-layer scan + symlink check (ADR-004, ADR-007, E8, E10).
# ---------------------------------------------------------------------------
PATTERNS_FILE=""

run_gate() {
  step "gate: reporting excluded paths"
  printf '[publish]   top-level allowed: %s\n' "${PUBLIC_TOPLEVEL[*]}"
  printf '[publish]   excluded dirs:  %s\n' "${EXCLUDE_DIRS[*]}"
  printf '[publish]   excluded files: %s\n' "${EXCLUDE_FILES[*]}"
  printf '[publish]   .claude/ kept:  %s (rest of .claude excluded)\n' \
    "${CLAUDE_KEEP_PREFIXES[*]}"
  printf '[publish]   tasks/ (kept: _TEMPLATE/**, README.md, column .gitkeep files)\n'

  # Abort if export contains gate-config files (ADR-004, E10)
  if [[ -f "$EXPORT/.gitleaksignore" || -f "$EXPORT/.gitleaks.toml" ]]; then
    die "Export contains .gitleaksignore or .gitleaks.toml — gate bypass not permitted (ADR-004)." 3
  fi

  # Symlink check (E8): abort on absolute targets or targets escaping export root
  local symlink_abort=0
  while IFS= read -r -d '' link; do
    local target
    target=$(readlink "$link")
    if [[ "$target" == /* ]]; then
      warn "Symlink '$link' has absolute target '$target' — blocked."
      symlink_abort=1
    else
      local link_dir resolved
      link_dir="$(dirname "$link")"
      resolved="$(cd "$link_dir" && readlink -f "$target" 2>/dev/null || true)"
      if [[ -n "$resolved" && "$resolved" != "$EXPORT"* ]]; then
        warn "Symlink '$link' target escapes export root — blocked."
        symlink_abort=1
      fi
    fi
  done < <(find "$EXPORT" -type l -print0)
  if [[ "$symlink_abort" -eq 1 ]]; then
    die "Export contains symlinks with external targets." 3
  fi

  # Layer 1: gitleaks dir (ADR-004)
  step "gate: layer 1 (gitleaks)"
  local gl_empty="$TMPROOT/empty"
  local gl_report="$TMPROOT/gitleaks.json"
  touch "$gl_empty"
  local gl_exit=0
  gitleaks dir "$EXPORT" \
    --no-banner \
    --redact \
    --exit-code 1 \
    --ignore-gitleaks-allow \
    --gitleaks-ignore-path "$gl_empty" \
    --report-format json \
    --report-path "$gl_report" 2>/dev/null || gl_exit=$?

  local gl_findings=0
  if [[ -f "$gl_report" ]]; then
    gl_findings=$(python3 -c \
      "import json,sys; d=json.load(open('$gl_report')); print(len(d) if isinstance(d,list) else 0)" \
      2>/dev/null || echo 0)
  fi
  printf '[publish] gate layer 1 (gitleaks): %d finding(s)\n' "$gl_findings"
  if [[ "$gl_exit" -ne 0 || "$gl_findings" -gt 0 ]]; then
    die "Layer 1 (gitleaks) found $gl_findings finding(s) — publish blocked." 3
  fi
  ok "gate: layer 1 clean"

  # Layer 2: blocklist per-pattern grep (ADR-007)
  step "gate: layer 2 (blocklist)"
  PATTERNS_FILE="$TMPROOT/patterns.sanitized"
  grep -v '^\s*#' "$BLOCKLIST_FILE" \
    | grep -v '^\s*$' \
    | sed 's/[[:space:]]*$//' \
    > "$PATTERNS_FILE" || true

  local effective_count
  effective_count=$(wc -l < "$PATTERNS_FILE" | tr -d ' ')
  printf '[publish] gate layer 2 (blocklist): %d effective pattern(s)\n' "$effective_count"

  if [[ "$effective_count" -eq 0 ]]; then
    warn "Blocklist has 0 effective patterns — layer 2 is a no-op for this run."
  fi

  local bl_abort=0
  local bl_findings=0
  while IFS= read -r pat; do
    [[ -z "$pat" ]] && continue
    local hits
    hits=$(grep -rIlE -- "$pat" "$EXPORT" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r hit_file; do
        printf '[publish] BLOCKED: pattern [%s] matched in %s\n' "$pat" "$hit_file" >&2
        bl_findings=$((bl_findings + 1))
        bl_abort=1
      done <<< "$hits"
    fi
  done < "$PATTERNS_FILE"

  printf '[publish] gate layer 2 (blocklist): %d hit(s)\n' "$bl_findings"
  if [[ "$bl_abort" -eq 1 ]]; then
    die "Layer 2 (blocklist) found $bl_findings hit(s) — publish blocked." 3
  fi
  ok "gate: layer 2 clean"
}

# ---------------------------------------------------------------------------
# compose_message — build and scan commit message (FR-013, ADR-008).
# Scanned text is byte-identical to committed text (normalization first).
# ---------------------------------------------------------------------------
MSG=""

compose_message() {
  step "message: composing commit message"

  local summary="$SUMMARY"

  # CHANGELOG fallback: first bullet line under ## [Unreleased]
  if [[ -z "$summary" ]]; then
    local changelog="$REPO_ROOT/CHANGELOG.md"
    if [[ -f "$changelog" ]]; then
      summary=$(awk '
        /^## \[Unreleased\]/ { in_section=1; next }
        in_section && /^## / { exit }
        in_section && /^- / { print; exit }
      ' "$changelog" | sed 's/^- //')
    fi
  fi

  # Final fallback
  if [[ -z "$summary" ]]; then
    local short_sha
    short_sha=$(git rev-parse --short "$REF")
    summary="Forwarded from ${REF}@${short_sha}"
  fi

  # Normalize: strip ** and ` markers, collapse whitespace, truncate to 100 chars
  summary=$(printf '%s' "$summary" \
    | sed 's/\*\*//g; s/`//g' \
    | tr -s ' \t' ' ' \
    | sed 's/^ //; s/ $//' \
    | cut -c1-100)

  MSG="Release ${VERSION}: ${summary}"
  ok "message: $MSG"

  # Scan the message with both layers (ADR-008)
  step "gate: message scan — layer 1 (gitleaks stdin)"
  local msg_gl_exit=0
  printf '%s' "$MSG" \
    | gitleaks stdin --no-banner --redact --exit-code 1 --ignore-gitleaks-allow \
    2>/dev/null || msg_gl_exit=$?
  if [[ "$msg_gl_exit" -ne 0 ]]; then
    die "Message scan (layer 1): gitleaks found a secret in the commit message." 3
  fi

  step "gate: message scan — layer 2 (blocklist)"
  if [[ -f "${PATTERNS_FILE:-}" ]]; then
    local msg_bl_abort=0
    while IFS= read -r pat; do
      [[ -z "$pat" ]] && continue
      if printf '%s' "$MSG" | grep -qE -- "$pat" 2>/dev/null; then
        warn "Commit message blocked by blocklist pattern: [$pat]"
        msg_bl_abort=1
      fi
    done < "$PATTERNS_FILE"
    if [[ "$msg_bl_abort" -eq 1 ]]; then
      die "Message scan (layer 2): blocklist pattern matched commit message." 3
    fi
  fi

  printf '[publish] gate message scan: clean\n'
}

# ---------------------------------------------------------------------------
# check_idempotent — tree-SHA equality (ADR-003).
# Called after scans (so AC-12 verdicts are printed) and before commit-tree.
# ---------------------------------------------------------------------------
check_idempotent() {
  local public_tree
  public_tree=$(git rev-parse "${PUBLIC_TIP}^{tree}")
  if [[ "$TREE" == "$public_tree" ]]; then
    skip "Nothing to publish — export matches public mirror's current tree."
    printf '[publish] push: not required (nothing to publish)\n'
    exit 0
  fi
}

# ---------------------------------------------------------------------------
# build_commit — unreferenced commit-tree object (ADR-002).
# ---------------------------------------------------------------------------
COMMIT=""

build_commit() {
  step "commit: building unreferenced commit object"
  COMMIT=$(git commit-tree "$TREE" -p "$PUBLIC_TIP" -m "$MSG")
  ok "commit: ${COMMIT:0:12} (parent=${PUBLIC_TIP:0:12})"
}

# ---------------------------------------------------------------------------
# report_dry_run — diffstat + exact push command (FR-017).
# ---------------------------------------------------------------------------
report_dry_run() {
  step "dry-run: diff against public mirror"

  printf '\n[publish] DIFF STAT (%s → export):\n' "${PUBLIC_TIP:0:12}"
  git diff --stat "${PUBLIC_TIP}^{tree}" "$TREE" | sed 's/^/[publish]   /'

  printf '\n[publish] ADDITIONS (new in export):\n'
  git diff --name-only --diff-filter=A "${PUBLIC_TIP}^{tree}" "$TREE" \
    | sed 's/^/[publish]   + /'

  printf '\n[publish] DELETIONS (removed from mirror):\n'
  git diff --name-only --diff-filter=D "${PUBLIC_TIP}^{tree}" "$TREE" \
    | sed 's/^/[publish]   - /'

  # Build the push URL for display
  local display_url
  if [[ -n "$PUSH_URL" ]]; then
    display_url="$PUSH_URL"
  elif [[ "$ALLOW_TEMP_PUSHURL" -eq 1 ]]; then
    display_url="$(git remote get-url "$PUBLISH_REMOTE")"
  else
    display_url="<push-url>"
  fi

  printf '\n[publish] Push command (requires --push --push-url <url>):\n'
  printf '[publish]   git push %s "%s:refs/heads/main"\n\n' \
    "$display_url" "${COMMIT}"
}

# ---------------------------------------------------------------------------
# do_push — authorized push, never writes .git/config (ADR-005).
# ---------------------------------------------------------------------------
do_push() {
  step "push: pushing ${COMMIT:0:12} to $PUBLISH_REMOTE main"

  if [[ -n "$PUSH_URL" ]]; then
    # Explicit URL path (ASSUME-07: primary mechanism)
    git push "$PUSH_URL" "${COMMIT}:refs/heads/main"
  else
    # --allow-temp-pushurl: process-scoped override only (ADR-005)
    local fetch_url
    fetch_url=$(git remote get-url "$PUBLISH_REMOTE")
    git -c "remote.${PUBLISH_REMOTE}.pushurl=${fetch_url}" \
      push "$PUBLISH_REMOTE" "${COMMIT}:refs/heads/main"
  fi

  # Verification: re-fetch and assert tip == COMMIT (ADR-005 consequence)
  step "push: verifying $PUBLISH_REMOTE/main"
  git fetch "$PUBLISH_REMOTE" main 2>/dev/null
  local new_tip
  new_tip=$(git rev-parse FETCH_HEAD)
  if [[ "$new_tip" != "$COMMIT" ]]; then
    die "Push verification failed: $PUBLISH_REMOTE/main is ${new_tip:0:12}, expected ${COMMIT:0:12}." 4
  fi
  ok "push: verified $PUBLISH_REMOTE/main = ${COMMIT:0:12}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"

  if [[ "$SELF_CHECK" -eq 1 ]]; then
    self_check
    exit 0
  fi

  TMPROOT="$(mktemp -d)"

  preflight
  fetch_public
  build_export
  run_gate
  compose_message
  check_idempotent
  build_commit
  report_dry_run

  if [[ "$DO_PUSH" -eq 1 ]]; then
    do_push
    printf '[publish] push: completed (%s)\n' "${COMMIT:0:12}"
  else
    printf '[publish] push: skipped (dry-run mode — use --push to publish)\n'
  fi

  ok "done."
}

main "$@"
