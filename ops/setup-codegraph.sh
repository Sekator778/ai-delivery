#!/usr/bin/env bash
# CodeGraph MCP setup — idempotent.
#
# Usage:
#   ops/setup-codegraph.sh                       # host setup only (npm install + MCP entry)
#   ops/setup-codegraph.sh <target-repo-path>    # host setup + init+index that target
#
# Re-running is safe: skips steps already done.

set -euo pipefail

PKG="@colbymchenry/codegraph"
TARGET="${1:-}"

echo "==> 1/4 MCP entry in ~/.claude.json"
if grep -q '"codegraph"' "$HOME/.claude.json" 2>/dev/null; then
  echo "    already present, skip"
else
  npx -y "$PKG" install --yes
fi

echo "==> 2/4 Global CLI install"
if command -v codegraph >/dev/null 2>&1; then
  echo "    already on PATH at $(command -v codegraph) — skip"
else
  npm install -g "$PKG"
fi

echo "==> 3/4 Verify"
codegraph --version
grep -q '"codegraph"' "$HOME/.claude.json" || {
  echo "ERROR: MCP entry missing from ~/.claude.json after install" >&2
  exit 1
}

if [[ -z "$TARGET" ]]; then
  echo "==> 4/4 No target repo given — host setup done."
  echo "    To index a target later:  $0 /path/to/target-repo"
  exit 0
fi

if [[ ! -d "$TARGET" ]]; then
  echo "ERROR: target dir '$TARGET' does not exist" >&2
  exit 2
fi

echo "==> 4/4 Init+index target: $TARGET"
cd "$TARGET"

if [[ -d .codegraph ]]; then
  echo "    .codegraph/ already exists — running sync (incremental)"
  codegraph sync
else
  codegraph init
  codegraph index
fi

if [[ -f .gitignore ]] && ! grep -qxF '.codegraph/' .gitignore; then
  echo '.codegraph/' >> .gitignore
  echo "    added .codegraph/ to .gitignore"
fi

echo "==> Done. Restart dispatcher/bot to pick up the new MCP server:"
echo "    sudo systemctl restart claude-tg-bot task-dispatcher"
