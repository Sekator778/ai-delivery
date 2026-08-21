#!/usr/bin/env bash
#
# sandbox-validate.sh — launch the task-dispatcher in the AGENT runner with the
# new pipeline stages/gates enabled, for a controlled sandbox validation run.
#
# This sets feature flags for the chosen ramp phase, loads keys from bot/.env,
# and starts the dispatcher as a FOREGROUND daemon. Drop a spec.json into
# tasks/inbox/<id>/ in another shell; the daemon ingests it and runs the whole
# pipeline. Ctrl-C ONLY after the task reaches a terminal stage (done/failed/
# awaiting-*), otherwise SIGINT terminates the live runner.
#
# Do NOT use `task_dispatcher.py --once` for a live run: it terminates spawned
# runners on exit. This script intentionally runs the daemon without --once.
#
# Usage:
#   scripts/sandbox-validate.sh v1   # stages ON, analyze gate report-only, mirror ON
#   scripts/sandbox-validate.sh v2   # same + ANALYZE_GATE_BLOCKING=1 (blocking gate)
#
# See STATE/SANDBOX-VALIDATION-2026-05-29.md for the full runbook + checklist.

set -euo pipefail

PHASE="${1:-v1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Load secrets/keys (DEEPSEEK_API_KEY, ANTHROPIC_*, etc.) if present.
if [[ -f bot/.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source bot/.env
  set +a
fi

# Force the agent runner regardless of what bot/.env set (our export wins in
# this shell, and the dispatcher spawns the runner with this environment).

# V1: additive, low-risk — every new stage on, analyze gate report-only.
export PATTERN_DETECTION_ENABLED=1
export TASKS_STAGE_ENABLED=1
export ANALYZE_STAGE_ENABLED=1
export EDGE_CASES_STAGE_ENABLED=1
export SPECS_FOLDER_MIRROR_ENABLED=1

case "$PHASE" in
  v1)
    unset ANALYZE_GATE_BLOCKING || true
    ;;
  v2)
    export ANALYZE_GATE_BLOCKING=1
    ;;
  *)
    echo "unknown phase '$PHASE' (use v1 or v2)" >&2
    exit 2
    ;;
esac

echo "=== sandbox-validate ($PHASE) ==="
echo "repo:            $REPO_ROOT"
for v in PATTERN_DETECTION_ENABLED TASKS_STAGE_ENABLED ANALYZE_STAGE_ENABLED \
         EDGE_CASES_STAGE_ENABLED SPECS_FOLDER_MIRROR_ENABLED ANALYZE_GATE_BLOCKING; do
  echo "$v=${!v:-<unset>}"
done
echo
echo "Now drop a spec.json into tasks/inbox/<id>/ (see the runbook §3)."
echo "Ctrl-C ONLY after the task reaches a terminal stage."
echo

exec python dispatcher/task_dispatcher.py
