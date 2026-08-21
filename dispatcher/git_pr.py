"""Git / GitHub PR I/O — branch + PR-URL parsing, base-branch safety, PR recovery,
draft-PR + review-comment posting.

Extracted from stage_runner_agent.py (god-module split, 2026-06-04). Everything
that shells out to git/gh or parses their output lives here: _extract_pr_url /
_extract_branch (tolerant of DeepSeek's Markdown), _current_git_branch /
_branch_base_ok (the stale-base guard), _recover_pr_from_repo (find the PR when
the developer forgot the trailer), _try_open_draft_pr (the handoff fallback), and
the PR-comment helpers for non-blocking review notes / parked unresolved findings.

Depends on target_policy for branch-safety + base-branch + PoC-mode signals;
otherwise stdlib (git/gh via subprocess).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from target_policy import _base_branch, _branch_safety_ok, _poc_mode_for_target


_PR_URL_RE = re.compile(r"https://github\.com/[\w./-]+/pull/\d+")


_PR_MARKER_RE = re.compile(r"PR_URL:\s*(.+?)(?:\\n|\n|$)")


_BRANCH_RE = re.compile(r"branch\s*\**\s*[:=][\s*`]*([A-Za-z0-9][\w./-]*)", re.IGNORECASE)


def _extract_pr_url(text: str) -> str | None:
    """PR URL by shape first (survives DeepSeek's Markdown like `**PR:** <url>`),
    `PR_URL:` marker as fallback. Ported from stage_runner.py — the agent path
    used a strict DEV_COMPLETE/BRANCH:/PR_URL: regex that missed the Markdown
    form, so pr_url was never persisted and the hotfix loop broke (Bug-8)."""
    m = _PR_URL_RE.search(text or "")
    if m:
        return m.group(0)
    m = _PR_MARKER_RE.search(text or "")
    if m:
        v = m.group(1).strip(" *`").strip()
        if v.startswith(("http://", "https://")):
            return v
    return None


def _extract_branch(text: str) -> str | None:
    """Branch token after a `Branch:` / `**Branch:**` / `BRANCH:` marker,
    tolerating Markdown bold + backticks (DeepSeek formats it that way)."""
    m = _BRANCH_RE.search(text or "")
    return m.group(1) if m else None


def _current_git_branch(target_repo: Path) -> str | None:
    """The branch the target repo is ACTUALLY on (git HEAD truth) — authoritative
    for the safety gate, since a subagent can self-report a branch name that
    differs from where it really committed. None on detached HEAD or any error."""
    try:
        p = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(target_repo), capture_output=True, text=True, timeout=15,
        )
        return (p.stdout or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _branch_base_ok(target_repo: Path, base_branch: str) -> tuple[bool, str]:
    """Verify the developer's branch was cut from a FRESH origin/<base_branch>,
    not a stale local HEAD. The branch-NAME gate (_branch_safety_ok) cannot see
    this: the 2026-05-31 regression cut a CORRECTLY-NAMED `feat/` branch from a
    stale local main and re-included a prior, already-merged task's diff (PR #8
    carrying PR #7's --version diff). We fetch the base, then require
    origin/<base_branch> to be an ANCESTOR of HEAD — true exactly when the branch
    sits on top of the current upstream tip.

    Fail-CLOSED only on a CONFIRMED stale base (origin/base is provably NOT an
    ancestor). Fail-OPEN (returns ok=True with a reason) on any git/infra error
    — no remote, offline fetch, detached HEAD, bad ref — so a transient hiccup
    never stop-the-lines the pipeline; the human merge gate stays the backstop.
    Best-effort: never raises."""
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=str(target_repo), capture_output=True, text=True, timeout=120,
        )
        if fetch.returncode != 0:
            return True, f"fetch failed — skipping base check ({(fetch.stderr or '').strip()[:120]})"
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"origin/{base_branch}", "HEAD"],
            cwd=str(target_repo), capture_output=True, text=True, timeout=30,
        )
        if anc.returncode == 0:
            return True, f"branch is on top of fresh origin/{base_branch}"
        if anc.returncode == 1:
            return False, (f"origin/{base_branch} is not an ancestor of HEAD — "
                           "branch cut from a stale local checkout, not the fresh base")
        # rc >= 2 → git could not decide (bad ref etc.) → fail-open, don't block
        return True, f"base check inconclusive (git rc={anc.returncode}) — skipping"
    except Exception as exc:  # noqa: BLE001 — a base check must never crash the stage
        return True, f"base check errored — skipping ({type(exc).__name__})"


def _recover_pr_from_repo(target_repo: Path) -> tuple[str | None, str | None]:
    """Recover (branch, pr_url) directly from the target repo when the developer
    subagent created the PR but omitted the DEV_COMPLETE/BRANCH/PR_URL trailer
    in its artifact (so _extract_* found nothing). The repo is left checked out
    on the PoC branch the subagent worked on, so we read it from git and ask gh
    for its open PR. Best-effort: returns (None, None) on any failure — never
    raises, so a missing `gh` or detached HEAD just falls through."""
    try:
        branch = _current_git_branch(target_repo)
        if not _branch_safety_ok(branch, _poc_mode_for_target(target_repo)):
            return None, None  # only trust a safe new branch (PoC or feature)
        p = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--json", "url", "-q", ".[0].url"],
            cwd=str(target_repo), capture_output=True, text=True, timeout=30,
        )
        url = (p.stdout or "").strip() if p.returncode == 0 else ""
        if not url.startswith("https://github.com/"):
            url = None
        return branch, url
    except Exception:  # noqa: BLE001 — recovery must never crash the stage
        return None, None


def _try_open_draft_pr(target_repo: Path, branch: str, task_id: str) -> str | None:
    """Best-effort: open a DRAFT PR for a branch that was worked on but never got
    one (e.g. the developer stage timed out after committing but before
    `gh pr create`). Pushes the branch first. Returns the PR url or None — never
    raises. Gated by HANDOFF_DRAFT_PR_ENABLED (default 1; set 0 where the bot
    lacks PR-create rights, then the handoff just writes the findings doc)."""
    if os.environ.get("HANDOFF_DRAFT_PR_ENABLED", "1").strip() != "1":
        return None
    # target_repo is the task's ephemeral worktree when #6 isolation is on;
    # _base_branch maps it back to the real checkout before resolving.
    base = _base_branch(target_repo)
    try:
        subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=str(target_repo), capture_output=True, text=True, timeout=120,
        )
        p = subprocess.run(
            ["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
             "--title", f"[handoff] {task_id} — pipeline stopped early",
             "--body", "Draft PR opened by the pipeline handoff: the run stopped "
                       "before reviewer approval. See UNRESOLVED-FINDINGS.md in "
                       "the task dir for the open findings and what was attempted."],
            cwd=str(target_repo), capture_output=True, text=True, timeout=60,
        )
        m = _PR_URL_RE.search((p.stdout or "") + (p.stderr or ""))
        return m.group(0) if m else None
    except Exception:  # noqa: BLE001 — handoff must never crash on a draft PR
        return None


def _pr_base_ref(target_repo: Path, pr_url: str) -> str | None:
    """`gh pr view <url> --json baseRefName` — the PR's ACTUAL base branch, or
    None on any failure (missing gh, network, bad url). Best-effort: never
    raises."""
    try:
        p = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "baseRefName",
             "-q", ".baseRefName"],
            cwd=str(target_repo), capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    return (p.stdout or "").strip() or None


def _verify_and_repair_pr_base(
    target_repo: Path, pr_url: str, expected_base: str,
) -> tuple[bool, str]:
    """Post-create verification (issue #10): the branch-base check
    (_branch_base_ok) proves the branch was CUT from the right base, but a
    subagent's own `gh pr create` can still drop `--base` and let `gh` fall
    back to the repo's default branch — exactly what happened on
    2026-08-12, PR opened against `dev-fix` instead of the registry-resolved
    `feat/local-longpolling` even though the prompt instructed `--base`.

    Reads the opened PR's actual baseRefName; on a mismatch, attempts ONE
    `gh pr edit --base` repair and re-verifies. Returns (ok, note) — ok is
    True when the base matches (either originally or after repair); note is a
    short human-readable status for the stage's verdict + task history. Never
    raises; fails OPEN (ok=True) when `gh pr view` itself errors (no network,
    no `gh`, bad url) so a transient API hiccup never stop-the-lines the
    pipeline — the human merge gate stays the backstop. A CONFIRMED mismatch
    that the repair could not fix fails CLOSED (ok=False) so it is logged
    loudly and recorded in the task history."""
    if not pr_url or not expected_base:
        return True, "skipped — missing pr_url or expected_base"
    actual = _pr_base_ref(target_repo, pr_url)
    if actual is None:
        return True, "skipped — gh pr view failed, could not read baseRefName"
    if actual == expected_base:
        return True, f"base matches ({actual})"
    try:
        edit = subprocess.run(
            ["gh", "pr", "edit", pr_url, "--base", expected_base],
            cwd=str(target_repo), capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 — repair must never crash the stage
        return False, (f"base mismatch (was {actual!r}, expected "
                       f"{expected_base!r}) — repair errored ({type(exc).__name__})")
    if edit.returncode != 0:
        return False, (f"base mismatch (was {actual!r}, expected "
                       f"{expected_base!r}) — gh pr edit failed: "
                       f"{(edit.stderr or '').strip()[:200]}")
    reverified = _pr_base_ref(target_repo, pr_url)
    if reverified == expected_base:
        return True, f"base mismatch repaired: {actual!r} -> {expected_base!r}"
    return False, (f"base mismatch NOT repaired (was {actual!r}, expected "
                   f"{expected_base!r}, after edit={reverified!r})")


def _post_nonblocking_review_comment(
    task_dir: Path, target_repo: str, pr_url: str, task_id: str,
) -> None:
    """Best-effort: when the nitpick guard approves a 0-critical
    `request_changes`, preserve the reviewer's non-blocking feedback ON the PR
    as a single comment instead of re-running the developer-hotfix loop over it.
    Never raises — a comment failure must not break the pipeline."""
    try:
        rj = task_dir / "06-review-agent.json"
        if not rj.is_file():
            rj = task_dir / "06-review.json"
        v = (json.loads(rj.read_text()).get("verdict") or {}) if rj.is_file() else {}
        warn = int(v.get("warning") or 0)
        sugg = int(v.get("suggestion") or 0)
        body = (
            "🔎 **Automated review — approved with non-blocking notes**\n\n"
            "The pipeline reviewer found **0 critical** issues, so this PR is "
            f"mergeable. It also raised **{warn} warning(s)** and **{sugg} "
            "suggestion(s)** — non-blocking quality notes. These are left for "
            "your discretion rather than auto-fixed, to avoid the scope-creep / "
            "cost runaway that chasing nitpicks causes. Full detail is in the "
            f"pipeline artifact `06-review.md` (task `{task_id}`)."
        )
        subprocess.run(
            ["gh", "pr", "comment", pr_url, "--body", body],
            cwd=str(target_repo), check=False,
            capture_output=True, timeout=30,
        )
    except Exception as exc:  # pragma: no cover - best-effort side channel
        print(f"warn: non-blocking review comment failed: {exc}", file=sys.stderr)


def _build_unresolved_findings_comment(task_dir: Path, task_id: str) -> str:
    """Assemble the PR-comment body for an awaiting-input (parked) stop: the
    reviewer's verdict counts + the human-readable findings section. Pure (no
    network) so it is unit-testable; _post_unresolved_findings_comment posts it."""
    rj = task_dir / "06-review-agent.json"
    if not rj.is_file():
        rj = task_dir / "06-review.json"
    v: dict = {}
    try:
        if rj.is_file():
            v = (json.loads(rj.read_text()).get("verdict") or {})
    except Exception:
        v = {}
    crit = int(v.get("critical") or 0)
    warn = int(v.get("warning") or 0)
    sugg = int(v.get("suggestion") or 0)
    findings = ""
    for name in ("06-review.md", "06-review-agent.md"):
        p = task_dir / name
        try:
            if p.is_file():
                txt = p.read_text()
                start = txt.find("## Critical")
                findings = (txt[start:] if start != -1 else txt).strip()
                break
        except OSError:
            pass
    return (
        "⏸️ **Automated review — parked for operator decision**\n\n"
        f"The pipeline reviewer did not converge: **{crit} Critical**, "
        f"**{warn} warning(s)**, **{sugg} suggestion(s)** remain after the hotfix "
        "iteration cap. The anti-thrash gate stopped escalation rather than "
        "burning more budget, so this PR is **not approved** — it needs your "
        f"decision (task `{task_id}`).\n\n"
        + (findings or "See `06-review.md` in the task dir for the full findings.")
    )


def _post_unresolved_findings_comment(
    task_dir: Path, target_repo: str, pr_url: str, task_id: str,
) -> None:
    """Best-effort: when the pipeline parks at awaiting-input with unresolved
    Critical findings, attach them to the PR so the operator can act from the PR
    itself instead of digging into the task dir (#7, 2026-06-02). Never raises."""
    try:
        body = _build_unresolved_findings_comment(task_dir, task_id)
        subprocess.run(
            ["gh", "pr", "comment", pr_url, "--body", body],
            cwd=str(target_repo), check=False,
            capture_output=True, timeout=30,
        )
    except Exception as exc:  # pragma: no cover - best-effort side channel
        print(f"warn: unresolved-findings comment failed: {exc}", file=sys.stderr)
