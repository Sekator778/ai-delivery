#!/usr/bin/env bash
# Install this project's git hooks into .git/hooks.
#
# Per-clone bootstrap: hooks live in .git/, which is not tracked and never
# travels with a clone, so nothing in the repo can assert that a hook is
# installed anywhere. Run this once after `git clone` — and know that a
# colleague's clone, CI, or a fresh remote session has not.
#
# Idempotent by design: re-running is a no-op when the installed copy already
# matches. A hook this script did not install (no ai-delivery-managed-hook
# marker) is never overwritten silently — pass --force to replace it, and the
# previous copy is kept alongside as <name>.pre-ai-delivery.
#
# The hooks are a convenience layer, not a safety guarantee. The real guards
# are the disabled pushurl on public-mirror and the two-layer gate inside
# scripts/publish-public.sh, which refuses to depend on any hook (NFR-007).
set -euo pipefail

readonly MARKER="ai-delivery-managed-hook"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        -h|--help)
            sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
            echo
            echo "Usage: scripts/install-git-hooks.sh [--force]"
            exit 0
            ;;
        *)
            echo "error: unknown argument '$arg' (try --help)." >&2
            exit 1
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# .git is a directory in a normal clone and a file in a worktree/submodule.
if [[ ! -e .git ]]; then
    echo "error: not in a git repo." >&2
    exit 1
fi

HOOKS_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS_DIR"

refused=0
for src in scripts/git-hooks/*; do
    [[ -f "$src" ]] || continue
    name="$(basename "$src")"
    dest="$HOOKS_DIR/$name"

    if [[ -e "$dest" ]]; then
        if cmp -s "$src" "$dest"; then
            # Chmod anyway: a copy restored from backup can lose the bit.
            chmod +x "$dest"
            echo "[hooks] $dest already up to date"
            continue
        fi
        if ! grep -q "$MARKER" "$dest" && [[ "$FORCE" -ne 1 ]]; then
            echo "[hooks] REFUSED $dest — exists and was not installed by this script." >&2
            echo "[hooks]   Inspect it, then re-run with --force to replace it" >&2
            echo "[hooks]   (the current copy is kept as $dest.pre-ai-delivery)." >&2
            refused=1
            continue
        fi
        if ! grep -q "$MARKER" "$dest"; then
            cp "$dest" "$dest.pre-ai-delivery"
            echo "[hooks] backed up $dest -> $dest.pre-ai-delivery"
        fi
    fi

    cp "$src" "$dest"
    chmod +x "$dest"
    echo "[hooks] installed $dest"
done

if [[ "$refused" -eq 1 ]]; then
    exit 1
fi
