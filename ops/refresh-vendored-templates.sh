#!/usr/bin/env bash
# ops/refresh-vendored-templates.sh — read-only drift check for vendored
# upstream content: templates (.claude/templates/bmad-v6/, spec-kit/) and
# agent personas (.claude/agents/, added 2026-08-15).
#
# Fetches every upstream shallow into throwaway tmp clones, walks the SAME
# selective file mapping recorded in each vendored directory's UPSTREAM
# file, and writes a unified diff report. NEVER writes back into the repo
# tree — an operator reviews the printed report path and manually ports
# material changes (see each directory's README "How to refresh" Step 2).
#
# Personas split into sync classes (see .claude/agents/UPSTREAM): only the
# VENDORED ones are diffed. The ADAPTED ones had their bodies rewritten for
# this pipeline, so bulk-applying an upstream diff would undo the adaptation.
#
# Pinning: BMAD_REF / SPECKIT_REF / VOLTAGENT_REF / WSHOBSON_REF env vars
# (default: empty = each repo's default branch HEAD). Set to a tag or commit
# SHA to pin a reproducible comparison, e.g.:
#   BMAD_REF=v6.11.0 SPECKIT_REF=v0.9.0 ops/refresh-vendored-templates.sh
#
# Usage:
#   ops/refresh-vendored-templates.sh [--keep-clones] [-h|--help]
#
# Exit codes:
#   0  report generated successfully (drift or not — see stdout's final
#      line: "UP_TO_DATE: ..." or "DRIFT_FOUND: N file(s) differ ...").
#      This is a report generator, not a gate — it never fails the build
#      just because upstream moved.
#   1  usage error, git not found, or a clone failed.
set -euo pipefail

# ---------------------------------------------------------------------------
# Repo root (script lives in ops/, repo root is its parent).
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BMAD_ROOT=".claude/templates/bmad-v6"
SPECKIT_ROOT=".claude/templates/spec-kit"
AGENTS_ROOT=".claude/agents"

BMAD_REPO="https://github.com/bmad-code-org/BMAD-METHOD"
SPECKIT_REPO="https://github.com/github/spec-kit"
VOLTAGENT_REPO="https://github.com/VoltAgent/awesome-claude-code-subagents"
WSHOBSON_REPO="https://github.com/wshobson/agents"

BMAD_REF="${BMAD_REF:-}"
SPECKIT_REF="${SPECKIT_REF:-}"
VOLTAGENT_REF="${VOLTAGENT_REF:-}"
WSHOBSON_REF="${WSHOBSON_REF:-}"

KEEP_CLONES=0

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --keep-clones) KEEP_CLONES=1 ;;
    -h|--help)
      sed -n '2,29p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo "git not found on PATH" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Temp dirs. Clone dirs are cleaned up on exit unless --keep-clones; the
# report file is NEVER cleaned up — its path is the deliverable.
# ---------------------------------------------------------------------------
BMAD_CLONE="$(mktemp -d /tmp/refresh-vendored-bmad.XXXXXX)"
SPECKIT_CLONE="$(mktemp -d /tmp/refresh-vendored-speckit.XXXXXX)"
VOLTAGENT_CLONE="$(mktemp -d /tmp/refresh-vendored-voltagent.XXXXXX)"
WSHOBSON_CLONE="$(mktemp -d /tmp/refresh-vendored-wshobson.XXXXXX)"
REPORT_DIR="$(mktemp -d /tmp/refresh-vendored-report.XXXXXX)"
REPORT="$REPORT_DIR/report.md"

cleanup() {
  if [ "$KEEP_CLONES" -eq 0 ]; then
    rm -rf "$BMAD_CLONE" "$SPECKIT_CLONE" "$VOLTAGENT_CLONE" "$WSHOBSON_CLONE"
  fi
}
trap cleanup EXIT

# Shallow-clone a repo at an optional ref (branch, tag, or commit SHA).
# Empty ref = the repo's default branch HEAD (single shallow clone, no
# extra fetch). A non-empty ref does a shallow default-branch clone first,
# then a shallow fetch + checkout of the exact ref — works for SHAs too,
# which `git clone --branch` does not support.
clone_at_ref() {
  local repo="$1" ref="$2" dest="$3"
  if [ -z "$ref" ]; then
    git clone --quiet --depth 1 "$repo" "$dest"
  else
    git clone --quiet --no-checkout --depth 1 "$repo" "$dest"
    git -C "$dest" fetch --quiet --depth 1 origin "$ref"
    git -C "$dest" checkout --quiet FETCH_HEAD
  fi
}

echo "Cloning $BMAD_REPO @ ${BMAD_REF:-<default branch>} (shallow)..." >&2
clone_at_ref "$BMAD_REPO" "$BMAD_REF" "$BMAD_CLONE"

echo "Cloning $SPECKIT_REPO @ ${SPECKIT_REF:-<default branch>} (shallow)..." >&2
clone_at_ref "$SPECKIT_REPO" "$SPECKIT_REF" "$SPECKIT_CLONE"

echo "Cloning $VOLTAGENT_REPO @ ${VOLTAGENT_REF:-<default branch>} (shallow)..." >&2
clone_at_ref "$VOLTAGENT_REPO" "$VOLTAGENT_REF" "$VOLTAGENT_CLONE"

echo "Cloning $WSHOBSON_REPO @ ${WSHOBSON_REF:-<default branch>} (shallow)..." >&2
clone_at_ref "$WSHOBSON_REPO" "$WSHOBSON_REF" "$WSHOBSON_CLONE"

BMAD_SHA="$(git -C "$BMAD_CLONE" rev-parse HEAD)"
SPECKIT_SHA="$(git -C "$SPECKIT_CLONE" rev-parse HEAD)"
VOLTAGENT_SHA="$(git -C "$VOLTAGENT_CLONE" rev-parse HEAD)"
WSHOBSON_SHA="$(git -C "$WSHOBSON_CLONE" rev-parse HEAD)"

# ---------------------------------------------------------------------------
# File mapping: "<local path relative to repo root>|<upstream path relative
# to its clone>". One pair per line. Kept in sync with each vendored
# directory's UPSTREAM file — update both together.
# ---------------------------------------------------------------------------
BMAD_SRC="src/bmm-skills"
BMAD_MAP="
$BMAD_ROOT/mary-analyst/SKILL.md|$BMAD_SRC/agents/bmad-agent-analyst/SKILL.md
$BMAD_ROOT/mary-analyst/customize.toml|$BMAD_SRC/agents/bmad-agent-analyst/customize.toml
$BMAD_ROOT/winston-architect/SKILL.md|$BMAD_SRC/agents/bmad-agent-architect/SKILL.md
$BMAD_ROOT/winston-architect/customize.toml|$BMAD_SRC/agents/bmad-agent-architect/customize.toml
$BMAD_ROOT/code-review/SKILL.md|$BMAD_SRC/ship/bmad-code-review/SKILL.md
$BMAD_ROOT/code-review/customize.toml|$BMAD_SRC/ship/bmad-code-review/customize.toml
$BMAD_ROOT/code-review/steps/step-01-gather-context.md|$BMAD_SRC/ship/bmad-code-review/steps/step-01-gather-context.md
$BMAD_ROOT/code-review/steps/step-02-review.md|$BMAD_SRC/ship/bmad-code-review/steps/step-02-review.md
$BMAD_ROOT/code-review/steps/step-03-triage.md|$BMAD_SRC/ship/bmad-code-review/steps/step-03-triage.md
$BMAD_ROOT/code-review/steps/step-04-present.md|$BMAD_SRC/ship/bmad-code-review/steps/step-04-present.md
$BMAD_ROOT/code-review/references/deletion-check.md|$BMAD_SRC/ship/bmad-code-review/references/deletion-check.md
$BMAD_ROOT/code-review/review-prompts/edge-case-hunter.md|$BMAD_SRC/ship/bmad-code-review/review-prompts/edge-case-hunter.md
$BMAD_ROOT/code-review/review-prompts/verification-gap.md|$BMAD_SRC/ship/bmad-code-review/review-prompts/verification-gap.md
$BMAD_ROOT/edge-case-hunter/references/deletion-check.md|$BMAD_SRC/ship/bmad-code-review/references/deletion-check.md
"
# edge-case-hunter/SKILL.md deliberately excluded from BMAD_MAP: upstream
# restructured its source skill into a deprecated shim (see UPSTREAM), so
# there is no single upstream file it maps 1:1 to anymore — content is
# hand-ported from review-prompts/edge-case-hunter.md above (already
# diffed via the code-review/ mapping) into a reconstructed frontmatter.
# document-project/ deliberately excluded: upstream deprecated the whole
# skill with no equivalent-shape replacement (see document-project/
# DEPRECATED-UPSTREAM.md) — nothing to diff against.

SPECKIT_MAP="
$SPECKIT_ROOT/constitution.md|templates/commands/constitution.md
$SPECKIT_ROOT/specify.md|templates/commands/specify.md
$SPECKIT_ROOT/clarify.md|templates/commands/clarify.md
$SPECKIT_ROOT/plan.md|templates/commands/plan.md
$SPECKIT_ROOT/tasks.md|templates/commands/tasks.md
$SPECKIT_ROOT/checklist.md|templates/commands/checklist.md
$SPECKIT_ROOT/analyze.md|templates/commands/analyze.md
$SPECKIT_ROOT/implement.md|templates/commands/implement.md
$SPECKIT_ROOT/LICENSE|LICENSE
"

# Personas (.claude/agents/) — see that directory's UPSTREAM file, which
# records the same mapping in human-readable form plus the per-file patch
# notes. Only the VENDORED class is listed here.
#
# The ADAPTED class is deliberately excluded: architect.md, code-reviewer.md
# and the three review lenses had their bodies rewritten for this pipeline, so
# a diff against upstream is noise, not signal — bulk-applying it would undo
# the adaptation. The lenses' BMAD sources are already diffed through the
# code-review/ mapping above. pattern-detector.md and README.md are ours.
VOLTAGENT_MAP="
$AGENTS_ROOT/backend-developer.md|categories/01-core-development/backend-developer.md
$AGENTS_ROOT/test-automator.md|categories/04-quality-security/test-automator.md
$AGENTS_ROOT/security-auditor.md|categories/04-quality-security/security-auditor.md
"
# business-analyst.md and context-manager.md were initially mapped here and the
# first drift run corrected that (2026-08-15): business-analyst is 56 lines
# against upstream's ~290 (275 upstream-only lines), context-manager 163 with
# 131 lines that exist only on our side. Both were rewritten, not vendored —
# they belong to the ADAPTED class and are listed under "Not diffed" below.

WSHOBSON_MAP="
$AGENTS_ROOT/team-lead.md|plugins/agent-teams/agents/team-lead.md
$AGENTS_ROOT/team-reviewer.md|plugins/agent-teams/agents/team-reviewer.md
$AGENTS_ROOT/team-debugger.md|plugins/agent-teams/agents/team-debugger.md
$AGENTS_ROOT/team-implementer.md|plugins/agent-teams/agents/team-implementer.md
"

# ---------------------------------------------------------------------------
# Diff one repo's mapping. Args: label, clone dir, ref, sha, mapping string.
# Appends to $REPORT; echoes the count of differing files and the count of
# mapping anomalies (missing on either side) via globals.
# ---------------------------------------------------------------------------
DIFF_COUNT=0
ANOMALY_COUNT=0

diff_mapping() {
  local label="$1" clone_dir="$2" ref="$3" sha="$4" mapping="$5"
  {
    echo "## $label ($ref @ $sha)"
    echo
  } >> "$REPORT"

  while IFS='|' read -r local_path upstream_rel; do
    [ -z "$local_path" ] && continue
    local upstream_path="$clone_dir/$upstream_rel"

    if [ ! -f "$local_path" ]; then
      echo "ANOMALY: local file missing: $local_path" >> "$REPORT"
      ANOMALY_COUNT=$((ANOMALY_COUNT + 1))
      continue
    fi
    if [ ! -f "$upstream_path" ]; then
      echo "ANOMALY: upstream path no longer exists: $upstream_rel (local: $local_path — see the directory's UPSTREAM/DEPRECATED notes)" >> "$REPORT"
      ANOMALY_COUNT=$((ANOMALY_COUNT + 1))
      continue
    fi

    if diff -q "$local_path" "$upstream_path" >/dev/null 2>&1; then
      : # identical, no report noise
    else
      DIFF_COUNT=$((DIFF_COUNT + 1))
      {
        echo "### DIFFERS: $local_path"
        echo '```diff'
        diff -u "$local_path" "$upstream_path" || true
        echo '```'
        echo
      } >> "$REPORT"
    fi
  done <<< "$mapping"

  echo >> "$REPORT"
}

{
  echo "# Vendored templates drift report"
  echo
  echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
} > "$REPORT"

diff_mapping "BMAD-METHOD" "$BMAD_CLONE" "${BMAD_REF:-default branch}" "$BMAD_SHA" "$BMAD_MAP"
diff_mapping "spec-kit" "$SPECKIT_CLONE" "${SPECKIT_REF:-default branch}" "$SPECKIT_SHA" "$SPECKIT_MAP"
diff_mapping "VoltAgent personas" "$VOLTAGENT_CLONE" "${VOLTAGENT_REF:-default branch}" "$VOLTAGENT_SHA" "$VOLTAGENT_MAP"
diff_mapping "wshobson personas" "$WSHOBSON_CLONE" "${WSHOBSON_REF:-default branch}" "$WSHOBSON_SHA" "$WSHOBSON_MAP"

{
  echo "## Not diffed (by design — see each directory's UPSTREAM/DEPRECATED notes)"
  echo
  echo "- $BMAD_ROOT/edge-case-hunter/SKILL.md — hand-ported, no 1:1 upstream path"
  echo "- $BMAD_ROOT/document-project/ — upstream skill deprecated, no equivalent-shape replacement"
  echo "- $BMAD_ROOT/README.md, $SPECKIT_ROOT/README.md — our own docs, not vendored content"
  echo "- $AGENTS_ROOT/architect.md, $AGENTS_ROOT/code-reviewer.md,"
  echo "  $AGENTS_ROOT/business-analyst.md, $AGENTS_ROOT/context-manager.md — ADAPTED"
  echo "  class: bodies rewritten for this pipeline, a full diff is noise"
  echo "  (see $AGENTS_ROOT/UPSTREAM)"
  echo "- $AGENTS_ROOT/blind-hunter.md, $AGENTS_ROOT/edge-case-hunter.md,"
  echo "  $AGENTS_ROOT/verification-gap.md — adapted from BMAD; their sources are"
  echo "  already diffed through the code-review/ mapping above"
  echo "- $AGENTS_ROOT/pattern-detector.md, $AGENTS_ROOT/README.md — ours, no upstream"
} >> "$REPORT"

echo >&2
echo "Report: $REPORT" >&2

if [ "$DIFF_COUNT" -eq 0 ] && [ "$ANOMALY_COUNT" -eq 0 ]; then
  echo "UP_TO_DATE: $REPORT"
else
  echo "DRIFT_FOUND: $DIFF_COUNT file(s) differ, $ANOMALY_COUNT anomaly(ies) — see $REPORT"
fi

exit 0
