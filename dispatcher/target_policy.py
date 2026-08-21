"""Target-repo policy & branch-safety gates.

Extracted from stage_runner_agent.py (god-module split, 2026-06-04). Two
concerns:

  * Target classification — _is_mergeable_target is the SAFETY seatbelt (a repo
    produces real PRs only if explicitly allowlisted in MERGEABLE_REPO_PATHS;
    fail-safe to PoC otherwise); _is_sandbox_target is a COST-only signal;
    _poc_mode_for_target / _normalized_repo_paths back them. Since #6 the stages
    run inside an EPHEMERAL worktree, so the classifiers first map the path they
    are handed back to the real checkout via _main_repo — a throwaway worktree
    path is in no allowlist and would otherwise flip a mergeable target to PoC
    (and mis-price the model).
  * Branch safety — _branch_safety_ok enforces that the developer committed to
    the exact branch we asked for (never a default branch under any name / case
    / sha); _norm_branch / _base_branch / _SHA_RE support it.

Mostly pure (env + git-ref string checks). Two deliberate exceptions, both
read-only and best-effort: _main_repo reads a worktree's `.git` pointer file,
and _base_branch may shell out to `git symbolic-ref` for the origin default
branch.
"""
from __future__ import annotations

import os as _os
import re
import subprocess
from pathlib import Path

import project_registry as _registry  # sibling module (dispatcher/)


def _main_repo(target_repo: Path) -> Path:
    """Map a linked git worktree back to its main checkout; any other path is
    returned unchanged.

    A linked worktree's `.git` is a FILE holding `gitdir: <main>/.git/worktrees/
    <name>`. Since #6 the pipeline hands the stages a throwaway worktree path,
    but policy (PoC seatbelt, sandbox model, per-target base branch) must key on
    the REAL target repo. Best-effort: never raises."""
    try:
        dot = Path(target_repo) / ".git"
        if dot.is_file():
            text = dot.read_text(errors="replace").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                # <main>/.git/worktrees/<name>  ->  <main>
                if (gitdir.parent.name == "worktrees"
                        and gitdir.parent.parent.name == ".git"):
                    return gitdir.parent.parent.parent.resolve()
    except (OSError, RuntimeError):
        pass
    return Path(target_repo)


def _normalized_repo_paths(env_name: str) -> set[str]:
    """Parse a comma-separated env list of repo paths into a set of canonical
    (expanduser+resolve) absolute paths, so trailing-slash / symlink / ~ entries
    still match a resolved target_repo. Unresolvable entries are kept verbatim."""
    raw = _os.environ.get(env_name, "").strip()
    out: set[str] = set()
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.add(str(Path(p).expanduser().resolve()))
        except Exception:  # noqa: BLE001
            out.add(p)
    return out


def _is_sandbox_target(target_repo: Path) -> bool:
    """COST-only signal (which Anthropic model): True for throwaway/validation
    repos so they run on the cheaper model. Misdetecting here only affects cost,
    never safety — the PoC seatbelt is governed separately by _is_mergeable_target.
    A target is sandbox if its name contains 'sandbox' OR it is in SANDBOX_REPO_PATHS
    (additive union, path-normalized)."""
    repo = _main_repo(target_repo)
    if "sandbox" in repo.name.lower():
        return True
    return str(repo) in _normalized_repo_paths("SANDBOX_REPO_PATHS")


def _is_mergeable_target(target_repo: Path) -> bool:
    """SAFETY signal (real PR vs PoC seatbelt). A target may produce real,
    mergeable PRs ONLY if it is explicitly allowlisted in MERGEABLE_REPO_PATHS
    (comma-separated, path-normalized). Default empty ⇒ NO repo is mergeable ⇒
    everything stays PoC. You opt a real repo IN deliberately; you can never
    accidentally opt OUT of the seatbelt."""
    return str(_main_repo(target_repo)) in _normalized_repo_paths("MERGEABLE_REPO_PATHS")


def _poc_mode_for_target(target_repo: Path) -> bool:
    """PoC mode (the test seatbelt: phase-b4-poc-* branch + [PoC, DO NOT MERGE]
    title) is ON by default for EVERY target. It turns OFF only for repos
    explicitly allowlisted as mergeable (MERGEABLE_REPO_PATHS). FAIL-SAFE: an
    unknown / misnamed / misconfigured target stays PoC and can never produce an
    accidentally-mergeable PR."""
    return not _is_mergeable_target(target_repo)


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def _norm_branch(branch: str | None) -> str:
    """Normalize a self-reported branch for safety comparison: strip whitespace,
    drop a leading remote prefix (origin/), casefold. Closes the case/prefix
    bypass (e.g. 'Main', 'origin/main')."""
    if not branch:
        return ""
    b = branch.strip()
    if b.lower().startswith("origin/"):
        b = b[len("origin/"):]
    return b.casefold()


def _branch_safety_ok(branch: str | None, poc_mode: bool,
                      expected: str | None = None) -> bool:
    """The non-negotiable safety property: the developer committed to the NEW
    branch we generated, never to a default branch. The check is tied to the
    EXACT branch shapes the dispatcher asks for, so anything else (a default
    branch under any name/case, a bare SHA, origin/-prefixed) is rejected:
      - PoC mode  → must start with `phase-b4-poc-`
      - real mode → must start with `feat/`  (the feat/<task-id> we generate;
        'main'/'Main'/'origin/main'/'trunk'/<sha> all fail this).

    `expected` tightens the shape check to an identity check: since #6 the
    runner itself creates the branch inside an ephemeral worktree, so it knows
    the ONE branch the developer may commit to. A correctly-shaped but different
    branch (the subagent ran `git checkout -b` anyway) is then a violation too.
    Left None (single-stage CLI / legacy in-place mode) the prefix rules stand."""
    nb = _norm_branch(branch)
    if not nb or _SHA_RE.match(nb):
        return False
    if expected:
        return nb == _norm_branch(expected)
    if poc_mode:
        return nb.startswith("phase-b4-poc-")
    return nb.startswith("feat/")


def _origin_default_branch(target_repo: Path) -> str | None:
    """The remote's own default branch (`origin/HEAD` → 'master'/'dev'/…), or
    None when there is no origin / no HEAD ref / git is unavailable. Local ref
    read only — no network. Best-effort: never raises."""
    try:
        p = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=str(target_repo), capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001 — a base probe must never crash a stage
        return None
    ref = (p.stdout or "").strip()
    if p.returncode != 0 or not ref.startswith("origin/"):
        return None
    return ref[len("origin/"):] or None


def _base_branch(target_repo: Path | None = None) -> str:
    """The upstream base branch the Developer cuts from AND the PR targets — the
    single source of truth shared by the prompt (_build_format_kwargs) and the
    post-run base check (_branch_base_ok), so the two can never drift.

    Resolution order (#6 — a global default of 'main' cut the first
    self-targeted run from a stale 'master' while development lived on 'dev'):
      1. the target's `base` in the project registry (`bot/projects.json`:
         `"alias": {"path": "...", "base": "dev"}`) — per-target, explicit;
      2. PIPELINE_BASE_BRANCH — the deployment-wide override;
      3. the target repo's own origin default branch (`origin/HEAD`);
      4. 'main'.
    `target_repo` may be an ephemeral worktree — it is mapped back to the real
    checkout first, so both the registry lookup and origin/HEAD see the target.
    Called with no target (legacy call sites) only steps 2 and 4 apply."""
    repo = _main_repo(target_repo) if target_repo is not None else None
    if repo is not None:
        try:
            per_target = _registry.base_for_path(repo)
        except Exception:  # noqa: BLE001 — a broken registry must not stop a run
            per_target = None
        if per_target:
            return per_target
    env = _os.environ.get("PIPELINE_BASE_BRANCH", "").strip()
    if env:
        return env
    if repo is not None:
        origin = _origin_default_branch(repo)
        if origin:
            return origin
    return "main"
