#!/usr/bin/env python3
"""Tier 3.1 Agent-tool harness — run a pipeline stage via Claude Code Agent tool.

Supports multiple stages via --stage argument. Each stage dispatches the
corresponding subagent_type from .claude/agents/*.md, persists its output
artifact, and writes structured JSON with cost/verdict/elapsed.

All 7 stages wired: discovery, ba, architect, developer, tester, security, reviewer.

Usage:
    python3 dispatcher/stage_runner_agent.py \\
        --stage discovery \\
        --task-dir /path/to/tasks/done/tg-xxx \\
        --target-repo /path/to/repo

    python3 dispatcher/stage_runner_agent.py \\
        --stage ba \\
        --task-dir /path/to/tasks/done/tg-xxx \\
        --target-repo /path/to/repo

    python3 dispatcher/stage_runner_agent.py \\
        --stage architect \\
        --task-dir /path/to/tasks/done/tg-xxx \\
        --target-repo /path/to/repo

    python3 dispatcher/stage_runner_agent.py \\
        --stage security \\
        --task-dir /path/to/tasks/done/tg-xxx \\
        --target-repo /path/to/repo

    python3 dispatcher/stage_runner_agent.py \\
        --stage reviewer \\
        --task-dir /path/to/tasks/done/tg-xxx \\
        --target-repo /path/to/repo

    python3 dispatcher/stage_runner_agent.py \\
        --stage tester \\
        --task-dir /path/to/tasks/done/tg-xxx \\
        --target-repo /path/to/repo

Artifacts written to <task-dir>/:
  - discovery:  00-discovery.md + .json
  - ba:         01-ba-agent.md + .json
  - architect:  02-architecture-agent.md + .json
  - tester:     04-test-agent.md + .json
  - security:   05-security-agent.md + .json
  - reviewer:   06-review-agent.md + .json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# C.2 interactive clarification helpers (sibling module).
import clarify as _clarify  # type: ignore  # noqa: E402

# C.3 INVEST validator (sibling module).
import invest_validator as _invest  # type: ignore  # noqa: E402

# Architecture-doc structural linter (sibling module) — steal-list §2.6/#10.
import architecture_lint as _archlint  # type: ignore  # noqa: E402

# Adaptive complexity triage (sibling module) — effort-sizing / task-complexity
# routing per STATE/ARCH-REVIEW-2026-05-29-adaptive-triage-COMMITTEE-RFC.md.
import triage as _triage  # type: ignore  # noqa: E402
import agent_roster as _agent_roster  # type: ignore  # noqa: E402
from stage_prompts import (  # noqa: E402  (god-module split 2026-06-04)
    STAGE_AGENT_MAP,
    STAGE_ARTIFACT_MAP,
    STAGE_CANONICAL_ARTIFACT,
    SPECS_FOLDER_SEMANTIC,
    STAGE_PROMPTS,
    STAGE_DONE_MARKERS,
    VERDICT_RE,
)
from control_loop import (  # noqa: E402  (god-module split 2026-06-04)
    _parse_cost,
    _read_stage_cost_usd,
    _read_stage_tokens,
    _analyze_critical_count,
    _reviewer_critical_count,
    _critical_is_converging,
    _add_tokens_used,
)
from post_pipeline import _decide_post_pipeline_stage  # noqa: E402  (god-module split 2026-06-04)
from triage_wiring import (  # noqa: E402  (god-module split 2026-06-04)
    _triage_mode,
    _triage_acting,
    _triage_run_claude,
    _write_triage_report,
    _write_lite_brd,
    _persist_triage,
    _load_persisted_triage,
    _REVIEWER_S_HINT,
    _REVIEWER_M_HINT,
)
from git_pr import (  # noqa: E402  (god-module split 2026-06-04)
    _extract_pr_url,
    _extract_branch,
    _current_git_branch,
    _branch_base_ok,
    _recover_pr_from_repo,
    _try_open_draft_pr,
    _verify_and_repair_pr_base,
    _post_nonblocking_review_comment,
    _post_unresolved_findings_comment,
)
from backend_routing import (  # noqa: E402  (god-module split 2026-06-04)
    ESCALATION_AT_ITERATION,
    _stage_timeout_for,
    _resolve_stage_backend,
    _resolve_anthropic_model_for_target,
    # Retained in this namespace (no longer called by run_pipeline — superseded by
    # the per-stage two-model policy) so existing tests that reach
    # stage_runner_agent._apply_tier_model_routing still resolve it.
    _apply_tier_model_routing,  # noqa: F401
    _subagent_env,
    apply_backend_pricing as _apply_backend_pricing,
)
import cost_ledger as _cost_ledger  # noqa: E402  (honest per-stage cost rows)
import memory_inject as _memory_inject  # noqa: E402  (task-scoped recall + write-back)
from budget_gate import park as _park_budget_stop, BUDGET_STOP_REASONS as _BUDGET_STOP_REASONS  # noqa: E402
import limit_stall as _limit_stall  # noqa: E402  (limit-outage detector, #11)
import provider_profiles as _profiles  # noqa: E402  (named key profiles, T15)
import proc_reaper as _proc_reaper  # noqa: E402  (child process-group kill, #18)
from target_policy import (  # noqa: E402  (god-module split 2026-06-04)
    _poc_mode_for_target,
    _branch_safety_ok,
    _base_branch,
)
from telegram_io import (  # noqa: E402  (god-module split 2026-06-04)
    _notify_bot,
    _send_telegram,
)
from notify_policy import (  # noqa: E402  (issue #19 — bot-notification policy)
    should_notify,
    verbose_enabled,
    render_stage_list,
)
from runner_state import (  # noqa: E402  (god-module split 2026-06-04)
    _append_worklog,
    _update_state,
    _append_history,
    _now_iso,
)
# Names the orchestrator no longer references directly but external callers
# (tests / ops) still read via stage_runner_agent.* — re-exported so the
# god-module split keeps the public module surface stable.
import backend_routing as _backend_routing  # noqa: E402
import git_pr as _git_pr  # noqa: E402
BACKEND = _backend_routing.BACKEND
STAGE_TIMEOUT_SEC = _backend_routing.STAGE_TIMEOUT_SEC
STAGE_TIMEOUT_SEC_L = _backend_routing.STAGE_TIMEOUT_SEC_L
SUPPORTED_BACKENDS = _backend_routing.SUPPORTED_BACKENDS
_build_unresolved_findings_comment = _git_pr._build_unresolved_findings_comment

# Pause / await-input destination — matches dispatcher/stage_runner.py:35.
_AWAITING_INPUT_DIR = Path(__file__).resolve().parent.parent / "tasks" / "awaiting-input"
# Terminal failure bucket — where a non-recoverable handoff lands.
_FAILED_DIR = Path(__file__).resolve().parent.parent / "tasks" / "failed"

RC_STAGE_TIMEOUT = 124   # sentinel: "stage timed out" (vs rc=1 crash / rc=3 no-artifact)
RC_RATE_LIMITED = 125    # sentinel: "backend out of quota / session limit" (vs rc=1 crash)
# #11 — "the backend stopped answering WHILE the stage was running" (retry storm
# detected live, child killed early). Distinct from RC_RATE_LIMITED=125, which is
# the post-hoc verdict after the cross-provider fallback was already exhausted:
# 125 fails the task, 126 PARKS it with a resume_at.
RC_LIMIT_STALL = 126


_HANDOFF_RC = 8          # run_pipeline terminated gracefully via _handoff_terminal


# ── Single-runner lock ───────────────────────────────────────────────────
# A task directory must have at most ONE live runner. Spawn decisions are
# distributed across task_dispatcher (at ingest), watcher (crash recovery),
# and manual ops — none can atomically guarantee exclusivity through the
# advisory .runner.pid file (check-then-write is a TOCTOU race; that race
# doubled a run's cost on 2026-05-31, bloating a PR to +1030 lines). This
# flock is the authoritative guard: whoever wins the kernel lock runs,
# everyone else exits cleanly. The lock is released automatically when the
# holding process dies, so a crashed runner leaves no stale lock — unlike the
# pid file, there is nothing for the watcher to misread.
_RUNNER_LOCK_FH = None  # module-global: hold the fd for the process lifetime


def _acquire_runner_lock(task_dir: Path) -> bool:
    """Take the exclusive per-task runner lock.

    Returns True if we hold it (proceed with the pipeline), False if another
    runner already owns it (caller must exit WITHOUT doing any work).
    """
    global _RUNNER_LOCK_FH
    lock_path = task_dir / ".runner.lock"
    # "a+" creates-if-missing and never truncates, so a losing runner cannot
    # clobber the winner's pid line before bouncing off the lock.
    fh = lock_path.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        fh.close()
        return False
    # We hold the lock — safe to rewrite the pid line (observability only; the
    # kernel lock, not this content, is the source of truth).
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        pass
    _RUNNER_LOCK_FH = fh  # never close — releasing the fd would drop the lock
    return True


# ── Ephemeral per-task worktree (#6) ──────────────────────────────────────
# The implementation stages used to run IN the target repo's live checkout: the
# developer subagent ran `git checkout -B <branch> origin/<base>` there, which
# switches the caller's working tree. On the first self-targeted run that tree
# WAS the running ai-delivery deployment — its Mac-stack files vanished from
# disk mid-run and freshly spawned runners started executing June-era code
# (split-brain of a live system caused by its own delivery task).
#
# Now the runner creates a throwaway `git worktree` of the target repo, already
# on the right branch cut fresh from origin/<base>, and hands THAT path to the
# developer / tester / security / reviewer stages. The caller's checkout is
# never touched, and parallel tasks against one target no longer fight over it.
# The worktree is removed once the PR is pushed (or on terminal handoff); the
# BRANCH survives — `git worktree remove` never deletes it.
#
# WORKTREE_ISOLATION_ENABLED=0 restores the legacy in-place behaviour (the
# prompts follow the actual mode — see _build_format_kwargs).
_WORKTREE_ROOT = Path(
    os.environ.get("WORKTREE_ROOT", "").strip() or "/tmp/ai-delivery-wt")
# Stages that must run against the isolated checkout.
_WORKTREE_STAGES = ("developer", "developer-hotfix", "tester", "security", "reviewer")
# ...of which only the developer stages may CREATE it: by the time tester /
# security / reviewer run, the developer has already made one. That keeps a
# single creation point and leaves the read-mostly stages fail-open.
_WORKTREE_CREATE_STAGES = ("developer", "developer-hotfix")
# tester+security run concurrently (_run_two_stages_parallel) — serialize the
# create/reuse decision so two threads cannot race two worktrees into existence.
_WORKTREE_LOCK = threading.Lock()


def _worktree_isolation_enabled() -> bool:
    return os.environ.get("WORKTREE_ISOLATION_ENABLED", "1").strip() != "0"


def _git(repo: Path, *args: str, timeout: int = 120):
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True, timeout=timeout)


def _persisted_worktree(task_dir: Path, state: dict | None = None) -> dict:
    """This task's worktree record ({path, branch, target_repo, base}), read
    FRESH from state.json (the in-memory state a stage carries can predate the
    developer stage that created it). Empty dict when there is none."""
    st: dict = {}
    try:
        st = json.loads((task_dir / "state.json").read_text())
    except Exception:  # noqa: BLE001
        st = state or {}
    wt = st.get("worktree")
    if not isinstance(wt, dict) and isinstance((state or {}).get("worktree"), dict):
        wt = state["worktree"]  # type: ignore[index]
    return dict(wt) if isinstance(wt, dict) else {}


def _worktree_branch(state: dict | None) -> str | None:
    """The ONE branch this task's stages may commit to, or None when no
    worktree was created (legacy in-place mode / single-stage CLI)."""
    wt = (state or {}).get("worktree")
    if isinstance(wt, dict) and wt.get("path"):
        return wt.get("branch") or None
    return None


def _worktree_alive(wt: dict) -> bool:
    """True when the recorded worktree still exists on disk (a linked worktree
    always carries a `.git` FILE pointing back at the main repo)."""
    path = wt.get("path")
    return bool(path) and (Path(path) / ".git").exists()


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}", timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _work_branch_name(task_id: str, target_repo: Path) -> str:
    """The branch the runner creates for this task — the same shapes the
    developer prompt used to ask the subagent for, so _branch_safety_ok and the
    PoC seatbelt are unchanged: PoC → `phase-b4-poc-<UTC ts>`, real →
    `feat/<task-id>`."""
    if _poc_mode_for_target(target_repo):
        return "phase-b4-poc-" + time.strftime("%Y%m%d-%H%M", time.gmtime())
    return f"feat/{task_id}"


def _create_task_worktree(target_repo: Path, task_id: str, base: str,
                          branch: str | None) -> dict | None:
    """`git worktree add` a throwaway checkout of `target_repo` for this task.

    `branch=None` → cut a NEW branch from the freshly fetched origin/<base> (the
    fresh-base property _branch_base_ok enforces; a name collision gets a `-N`
    suffix so parallel tasks can never land on one another's branch). A branch
    name from a prior attempt (resume after crash/handoff) is re-checked-out
    instead, keeping the commits already made.

    Returns the record to persist, or None when the worktree could not be
    created — the caller then FAILS the stage rather than falling back to the
    live checkout, which is the whole point of #6. Never raises."""
    try:
        _WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
        _git(target_repo, "worktree", "prune", timeout=30)  # drop stale metadata
        # Fetch so the new branch is cut from the CURRENT upstream tip, not a
        # stale local ref (the 2026-05-31 contamination). Best-effort: offline
        # falls back to the local base ref below.
        _git(target_repo, "fetch", "origin", base, timeout=180)
        start = f"origin/{base}"
        if _git(target_repo, "rev-parse", "--verify", "--quiet", start,
                timeout=15).returncode != 0:
            start = base
        reuse = bool(branch)
        if not branch:
            stem = _work_branch_name(task_id, target_repo)
            branch, n = stem, 1
            while _branch_exists(target_repo, branch):
                branch, n = f"{stem}-{n}", n + 1
        path = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=str(_WORKTREE_ROOT)))
        add = (["worktree", "add", str(path), branch] if reuse
               else ["worktree", "add", str(path), "-b", branch, start])
        p = _git(target_repo, *add, timeout=300)
        if p.returncode != 0 and reuse:
            # The branch is checked out somewhere else (a leftover worktree the
            # prune could not see, or the main checkout) — take it anyway; this
            # copy is disposable and the branch content is what matters.
            p = _git(target_repo, "worktree", "add", "--force", str(path),
                     branch, timeout=300)
        if p.returncode != 0:
            print(f"error: git worktree add failed in {target_repo}: "
                  f"{(p.stderr or '').strip()[:400]}", file=sys.stderr)
            shutil.rmtree(str(path), ignore_errors=True)
            return None
        return {"path": str(path), "branch": branch,
                "target_repo": str(target_repo), "base": base}
    except Exception as exc:  # noqa: BLE001 — never crash a stage on setup
        print(f"error: worktree setup failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def _ensure_task_worktree(task_dir: Path, target_repo: Path, state: dict,
                          task_id: str) -> Path | None:
    """The task's isolated checkout, creating it on first use and reusing it
    afterwards (including across a watcher respawn — the record lives in
    state.json). None when isolation is disabled or the worktree could not be
    created."""
    if not _worktree_isolation_enabled():
        return None
    with _WORKTREE_LOCK:
        wt = _persisted_worktree(task_dir, state)
        if _worktree_alive(wt):
            state["worktree"] = wt
            return Path(wt["path"])
        base = state.get("base_branch") or _base_branch(target_repo)
        record = _create_task_worktree(target_repo, task_id, base, wt.get("branch"))
        if record is None:
            return None
        try:
            _update_state(task_dir, worktree=record)
        except Exception as exc:  # noqa: BLE001
            print(f"warn: failed to persist worktree record: {exc}", file=sys.stderr)
        state["worktree"] = record
        _append_worklog(
            task_dir,
            f"worktree: {record['path']} on branch {record['branch']} "
            f"(base {base}) — {target_repo} left untouched",
        )
        return Path(record["path"])


def _remove_task_worktree(task_dir: Path, state: dict | None = None) -> None:
    """Drop the task's throwaway checkout once the branch is pushed (or on
    terminal handoff). The BRANCH is deliberately kept — it carries the PR.
    Best-effort: never raises."""
    wt = _persisted_worktree(task_dir, state)
    path, main = wt.get("path"), wt.get("target_repo")
    if not path:
        return
    try:
        if main:
            _git(Path(main), "worktree", "remove", "--force", str(path), timeout=120)
        if Path(path).exists():
            shutil.rmtree(str(path), ignore_errors=True)
        if main:
            _git(Path(main), "worktree", "prune", timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"warn: worktree cleanup failed: {exc}", file=sys.stderr)
    # Keep the record (branch name = the resume handle) but mark it gone.
    record = {**wt, "removed": True}
    try:
        _update_state(task_dir, worktree=record)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(state, dict):
        state["worktree"] = record


def _stage_repo(task_dir: Path, target_repo: Path, stage: str, state: dict,
                task_id: str) -> Path | None:
    """The working directory a stage runs against: the task's isolated worktree
    for the implementation stages, the target checkout itself for the read-only
    upstream ones (BA / architect / … only grep it).

    None means "the isolated checkout was required but unavailable" — the caller
    fails the stage instead of writing into the live checkout (#6)."""
    if stage not in _WORKTREE_STAGES:
        return target_repo
    if stage in _WORKTREE_CREATE_STAGES:
        worktree = _ensure_task_worktree(task_dir, target_repo, state, task_id)
        if worktree is not None:
            return worktree
        return target_repo if not _worktree_isolation_enabled() else None
    # tester / security / reviewer: reuse what the developer stage created.
    wt = _persisted_worktree(task_dir, state)
    if _worktree_alive(wt):
        state["worktree"] = wt
        return Path(wt["path"])
    return target_repo


def _build_format_kwargs(stage: str, task_dir: Path, target_repo: Path,
                         state: dict) -> dict:
    """Common placeholder values for any stage's orchestrator prompt."""
    artifact_name = STAGE_ARTIFACT_MAP[stage]
    kwargs = {
        "task_id":     task_dir.name,
        "target_repo": str(target_repo),
        "task_dir":    str(task_dir),
        "diff_path":   str(task_dir / "pr-diff-for-agent.md"),
        # This repo's root. Stages run with cwd inside the TARGET project
        # (2026-08-15), so every citation of a vendored template under
        # .claude/templates/ has to be absolute or it resolves against the
        # wrong tree — silently, since the stage would just find no file and
        # carry on with the inlined copy of the pattern.
        "pipeline_root": str(_agent_roster.REPO_ROOT),
    }
    if stage == "reviewer":
        pr_url = state.get("pr_url") or ""
        pr_number = pr_url.rsplit("/", 1)[-1] if pr_url else ""
        kwargs["pr_number"] = pr_number
        kwargs["review_artifact"] = str(task_dir / artifact_name)
        # Raw, untriaged output of the three review lenses (#21) — audit trail of
        # what each lens said BEFORE the orchestrator's severity triage. Named
        # without the NN- prefix on purpose: it is reviewer-stage scratch like
        # pr-diff-for-agent.md, not a pipeline artifact any other stage reads
        # (the weave test treats NN-*.md names as stage outputs).
        kwargs["lenses_artifact"] = str(task_dir / "review-lenses.md")
        kwargs["triage_hint"] = _reviewer_triage_hint(state)
    elif stage == "architect":
        kwargs["arch_artifact"] = str(task_dir / artifact_name)
    elif stage == "discovery":
        kwargs["discovery_artifact"] = str(task_dir / artifact_name)
    elif stage == "ba":
        kwargs["ba_artifact"] = str(task_dir / artifact_name)
    elif stage == "pattern-detector":
        kwargs["patterns_artifact"] = str(task_dir / artifact_name)
    elif stage == "tasks":
        kwargs["tasks_artifact"] = str(task_dir / artifact_name)
    elif stage == "analyze":
        kwargs["analyze_artifact"] = str(task_dir / artifact_name)
    elif stage == "edge-cases":
        kwargs["edgecases_artifact"] = str(task_dir / artifact_name)
    elif stage == "security":
        pr_url = state.get("pr_url") or ""
        pr_number = pr_url.rsplit("/", 1)[-1] if pr_url else ""
        kwargs["pr_number"] = pr_number
        kwargs["security_artifact"] = str(task_dir / artifact_name)
    elif stage == "tester":
        pr_url = state.get("pr_url") or ""
        pr_number = pr_url.rsplit("/", 1)[-1] if pr_url else ""
        kwargs["pr_number"] = pr_number
        kwargs["tester_artifact"] = str(task_dir / artifact_name)
    elif stage == "developer":
        kwargs["dev_artifact"] = str(task_dir / artifact_name)
        # Base branch the feature branch is cut from AND the PR targets — the same
        # value the post-run base check enforces (see _base_branch / _branch_base_ok).
        # run_pipeline resolves it once per run and persists it, so a mid-run
        # origin/HEAD change cannot drift the prompt from the check.
        base = state.get("base_branch") or _base_branch(target_repo)
        kwargs["base_branch"] = base
        # Per-target PoC mode: sandbox → phase-b4-poc-* branch + [PoC, DO NOT
        # MERGE] title (test seatbelt); real repo → feat/<task-id> + clean,
        # mergeable PR. See _poc_mode_for_target / _branch_safety_ok.
        tid = task_dir.name
        if _poc_mode_for_target(target_repo):
            legacy_branch_rule = (
                "* The subagent MUST create a NEW branch named with the prefix "
                "`phase-b4-poc-` + current UTC timestamp (e.g. "
                "`phase-b4-poc-20260526-2050`) — a throwaway PoC branch."
            )
            kwargs["pr_title_rule"] = (
                "* The PR title MUST begin with the literal phrase "
                "`[PoC, DO NOT MERGE]`."
            )
            legacy_branch_gen = (
                "   - timestamp = `date -u +%Y%m%d-%H%M`\n"
                "   - branch_name = `phase-b4-poc-<timestamp>`"
            )
            kwargs["subagent_pr_title_rule"] = (
                "PR title MUST start with `[PoC, DO NOT MERGE]` followed by a "
                "Conventional Commits subject (e.g. `[PoC, DO NOT MERGE] "
                "feat(api): add ping endpoint`)."
            )
            kwargs["merge_framing"] = (
                "This is a throwaway PoC — it must NEVER be merged; the PR stays "
                "open for inspection and is closed, not merged."
            )
            kwargs["branch_verify_rule"] = (
                "verify it starts with the prefix `phase-b4-poc-`"
            )
        else:
            legacy_branch_rule = (
                f"* The subagent MUST create a NEW feature branch named "
                f"`feat/{tid}` (never commit to main/master) — a real, "
                "mergeable PR."
            )
            kwargs["pr_title_rule"] = (
                "* The PR title is a clean Conventional Commits subject (e.g. "
                "`feat(api): add ping endpoint`) — NO `[PoC]` prefix."
            )
            legacy_branch_gen = f"   - branch_name = `feat/{tid}`"
            kwargs["subagent_pr_title_rule"] = (
                "PR title is a clean Conventional Commits subject (e.g. "
                "`feat(api): add ping endpoint`) — NO `[PoC]` prefix; this is a "
                "real, mergeable PR."
            )
            kwargs["merge_framing"] = (
                f"Open a normal, mergeable PR against `{base}` for human review "
                "and merge."
            )
            kwargs["branch_verify_rule"] = (
                "verify it is a feature branch, NOT main/master/develop/HEAD"
            )
        # #6 — with an ephemeral worktree the branch ALREADY exists and is
        # checked out, so the rules flip from "create a NEW branch" to "you are
        # on branch X, commit here, never switch". The wording follows the
        # ACTUAL mode (state.worktree is written only when one was created), so
        # WORKTREE_ISOLATION_ENABLED=0 / the single-stage CLI keep the legacy
        # fetch + `checkout -B` contract and never see a false promise.
        wt = state.get("worktree") if isinstance(state.get("worktree"), dict) else {}
        wt_branch = (wt or {}).get("branch") if (wt or {}).get("path") else None
        if wt_branch:
            kwargs["branch_name"] = wt_branch
            kwargs["branch_rule"] = (
                f"* The work happens in an ISOLATED, disposable git worktree of "
                f"the target repo that the orchestrator already created and "
                f"checked out on branch `{wt_branch}` (cut fresh from "
                f"origin/{base}). The subagent commits THERE and MUST NOT create, "
                f"switch or rename branches."
            )
            kwargs["branch_gen"] = (
                f"   - branch_name = `{wt_branch}` — ALREADY created and checked "
                f"out for you; do NOT invent, create or rename it."
            )
            kwargs["branch_setup_rule"] = (
                f"- Branch — you are ALREADY on `{wt_branch}`. {target_repo} is an\n"
                f"  isolated, disposable git worktree of the target repo, cut fresh from\n"
                f"  origin/{base}; the caller's own checkout is a DIFFERENT directory and\n"
                f"  must stay untouched. So do NOT create or switch branches:\n"
                f"    cd {target_repo}\n"
                f"    git rev-parse --abbrev-ref HEAD   # must print {wt_branch}\n"
                f"  NEVER run `git checkout`, `git switch`, `git checkout -b`, `git\n"
                f"  worktree` or `git rebase` here — leaving `{wt_branch}` is a SAFETY\n"
                f"  VIOLATION that fails the stage."
            )
            kwargs["branch_verify_rule"] += f" AND equals `{wt_branch}`"
        else:
            kwargs["branch_name"] = "<BRANCH_NAME_FROM_ORCHESTRATOR>"
            kwargs["branch_rule"] = legacy_branch_rule
            kwargs["branch_gen"] = legacy_branch_gen
            kwargs["branch_setup_rule"] = (
                "- Branch — cut it FRESH from the upstream base, NEVER from the current local\n"
                "  HEAD. A stale local checkout would re-include a PREVIOUS task's already-merged\n"
                "  diff into this PR (the exact 2026-05-31 regression). Run, in order:\n"
                f"    cd {target_repo}\n"
                "    git fetch origin\n"
                f"    git checkout -B <BRANCH_NAME_FROM_ORCHESTRATOR> origin/{base}\n"
                "  (`-B` is idempotent — it (re)creates the branch from the just-fetched\n"
                f"  origin/{base}, so a leftover branch from a prior run is reset, not\n"
                "  inherited.)"
            )
    elif stage == "developer-hotfix":
        # iteration + cap + previous_branch + previous_pr_url come from the
        # pipeline driver (run_pipeline) so the hotfix prompt can lock the
        # subagent to the same branch and PR as the initial Developer
        # iteration.
        kwargs["dev_artifact"] = str(task_dir / artifact_name)
        kwargs["iteration"] = int(state.get("iteration") or 1)
        kwargs["iteration_cap"] = int(state.get("iteration_cap") or 3)
        previous_branch = str(state.get("branch") or "")
        kwargs["previous_branch"] = previous_branch
        kwargs["previous_pr_url"] = str(state.get("pr_url") or "")
        # #6 — in the isolated worktree the branch is already checked out, so
        # the hotfix must NOT run `git checkout` (there is nothing to switch to,
        # and switching is what wrecked the caller's tree in the first place).
        wt = state.get("worktree") if isinstance(state.get("worktree"), dict) else {}
        if (wt or {}).get("path"):
            kwargs["branch_setup_rule"] = (
                f"   - cd {target_repo} — an isolated, disposable git worktree of the\n"
                f"     target repo, ALREADY checked out on `{previous_branch}`.\n"
                "   - Do NOT `git checkout` / `git switch` / `git worktree`: there is\n"
                "     nothing to switch to and leaving the branch fails the stage.\n"
                f"   - `git fetch origin {previous_branch}` (in case the remote moved)\n"
                "   - Verify HEAD is on the right branch: `git rev-parse --abbrev-ref HEAD`"
            )
        else:
            kwargs["branch_setup_rule"] = (
                f"   - cd {target_repo}\n"
                f"   - `git fetch origin {previous_branch}` (in case the remote moved)\n"
                f"   - `git checkout {previous_branch}`\n"
                "   - Verify HEAD is on the right branch: `git rev-parse --abbrev-ref HEAD`"
            )
    return kwargs


def _dump_stage_error(task_dir: Path, stage: str, proc, reason: str) -> None:
    """Persist a failed stage's claude stdout/stderr tail to a log file so the
    failure is diagnosable later — the dispatcher does not log the runner's
    captured output (Bug-9: 'failed but no error visible'). Best-effort."""
    try:
        out = getattr(proc, "stdout", "") or ""
        err = getattr(proc, "stderr", "") or ""
        (task_dir / f"{stage}.claude-error.log").write_text(
            f"stage={stage}  reason={reason}\n\n"
            f"=== STDERR (tail) ===\n{err[-6000:]}\n\n"
            f"=== STDOUT (tail) ===\n{out[-6000:]}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _reap_children_before_move(task_id: str, why: str) -> None:
    """Kill every claude child this runner still owns, BEFORE moving the task
    dir out of active/ (#18).

    A moved-away directory that a live child still writes into comes back as a
    split-brain ``active/<task>/`` with an artifact and no state.json
    (2026-08-14). In the sequential stages the stage's own child is already dead
    by the time we get here — this is the belt for the concurrent tester+security
    pair, where one stage's disposition moves the dir while the other's child
    still runs.
    """
    killed = _proc_reaper.kill_tracked()
    if killed:
        print(f"[proc-reaper] {task_id}: killed {killed} live claude child "
              f"group(s) before {why}", file=sys.stderr)


def _task_dir_now(task_dir: Path) -> Path:
    """Where this task's directory lives RIGHT NOW (#18).

    A park (limit stall / budget stop) moves the dir out of ``active/`` while a
    stage is still running. Writing an artifact through the stale path
    re-CREATES the moved-away directory — the split-brain
    ``active/<task>/03-dev-agent.md`` with no state.json (2026-08-14). Re-resolve
    by task id across the buckets before any post-stage write; the original path
    is returned unchanged when it still exists (the normal case) or when the task
    cannot be found at all (caller's own error handling then applies).
    """
    if task_dir.is_dir():
        return task_dir
    tasks_root = task_dir.parent.parent
    for bucket in ("awaiting-input", "awaiting-approval", "failed", "done",
                   "active", "inbox"):
        candidate = tasks_root / bucket / task_dir.name
        if candidate.is_dir():
            print(f"warn: task dir moved to {bucket}/ mid-stage — writing there "
                  f"instead of re-creating {task_dir}", file=sys.stderr)
            return candidate
    return task_dir


# ── Live stream watch (#11) ─────────────────────────────────────────────────
# The stage used to run under subprocess.run(capture_output=True): the whole
# stream-json output only became visible AFTER the child exited, so a limit
# outage — which the claude CLI answers by silently retrying the inner Agent
# call — was indistinguishable from a slow stage until STAGE_TIMEOUT_SEC fired
# (2026-08-12: 30 min of retries, rc=124, $4.42, no artifact). We now stream the
# child's stdout/stderr through LimitStallWatch and kill it as soon as the storm
# is recognisable. LIMIT_STALL_DETECT=0 restores the old buffered behaviour.
_STALL_POLL_SEC = 5.0
_STALL_KILL_GRACE_SEC = 10.0


def _limit_stall_detect_enabled() -> bool:
    return os.environ.get("LIMIT_STALL_DETECT", "1").strip() != "0"


def _terminate_child(proc: "subprocess.Popen", grace: float = _STALL_KILL_GRACE_SEC) -> None:
    """TERM the claude child's whole process GROUP; KILL it after *grace*.

    #18: the group, not the pid. The claude CLI spawns its own subagent/MCP
    subtree, and a bare pid TERM left that subtree running — a 3 h 11 m silent
    token burn after the operator killed the runners (2026-08-14).
    """
    if not _proc_reaper.terminate_child(proc, grace=grace):
        print(f"warn: claude child pid={proc.pid} survived TERM+KILL — its "
              f"process group may still be running", file=sys.stderr)


def _run_claude_stage_buffered(argv: list, *, env: dict, timeout: int,
                               cwd: "str | None" = None):
    """LIMIT_STALL_DETECT=0 path: capture the whole stream, hand it over at exit.

    Was ``subprocess.run(capture_output=True, timeout=...)``, whose timeout path
    kills the CHILD PID only and leaves the CLI's subtree orphaned (#18). Same
    contract: a CompletedProcess, or TimeoutExpired carrying the partial output.
    """
    proc = _proc_reaper.spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, cwd=cwd)
    child_pgid = _proc_reaper.pgid_of(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_child(proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    except BaseException:
        _terminate_child(proc)
        raise
    finally:
        _proc_reaper.untrack(proc)
        _proc_reaper.kill_group_leftovers(child_pgid)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _run_claude_stage(argv: list, *, env: dict, timeout: int, stage: str,
                      cwd: "str | None" = None):
    """Run the claude orchestrator while WATCHING its stream-json output live.

    Returns ``(completed_process, stall_reason, stall_hint)`` — stall_reason is
    None for a normal run, else 'api-error' / 'stall' with the child already
    terminated. Raises ``subprocess.TimeoutExpired`` (carrying the captured
    output) on the wall clock, exactly like the ``subprocess.run`` it replaces,
    so the existing RC_STAGE_TIMEOUT path is untouched."""
    if not _limit_stall_detect_enabled():
        return (_run_claude_stage_buffered(argv, env=env, timeout=timeout, cwd=cwd),
                None, "")

    watch = _limit_stall.LimitStallWatch()
    lock = threading.Lock()
    out_lines: list = []
    err_lines: list = []

    try:
        # #18 — start_new_session: the child leads its OWN process group, so
        # every exit path below can kill the whole claude subtree as a unit,
        # and the runner's own group is never in the blast radius.
        proc = _proc_reaper.spawn(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True,
                                  bufsize=1, env=env, cwd=cwd)
    except OSError:
        # The child could not be spawned at all (claude not on PATH, bad env).
        # Re-issue through the buffered path so the failure surfaces exactly as
        # it did before the live reader existed — same exception, same rc.
        # subprocess.run, not the group-aware helper above: there is no child to
        # own here, and this is the shape the timeout contract is tested against.
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=cwd), None, ""

    def _pump(pipe, sink) -> None:
        try:
            for line in pipe:
                with lock:
                    sink.append(line)
                    watch.feed(line)
        except (OSError, ValueError):
            pass

    pumps = [threading.Thread(target=_pump, args=(p, s), daemon=True)
             for p, s in ((proc.stdout, out_lines), (proc.stderr, err_lines))]
    for t in pumps:
        t.start()

    child_pgid = _proc_reaper.pgid_of(proc)
    deadline = time.time() + timeout
    stall_reason = None
    timed_out = False
    try:
        while True:
            try:
                proc.wait(timeout=_STALL_POLL_SEC)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            with lock:
                stall_reason = watch.verdict(now)
            if stall_reason:
                print(f"[limit-stall] stage={stage}: {stall_reason} "
                      f"(retries={watch.retries}, silent="
                      f"{now - watch.last_progress:.0f}s) — terminating child",
                      file=sys.stderr)
                _terminate_child(proc)
                break
            if now >= deadline:
                timed_out = True
                _terminate_child(proc)
                break
    except BaseException:
        # #18 — any way out of this loop that is NOT one of the two kills above
        # (crash, KeyboardInterrupt, a raising watcher) used to abandon the
        # child. Nothing may leave this function with the group still alive.
        _terminate_child(proc)
        raise
    finally:
        _proc_reaper.untrack(proc)

    # #18 — the CLI itself is gone, but it can leave a detached MCP server or
    # subagent in its group; the runner's pid handle is already reaped, so
    # nothing else would ever collect them.
    if _proc_reaper.kill_group_leftovers(child_pgid):
        print(f"[proc-reaper] stage={stage}: killed leftover process(es) in the "
              f"claude child's group (pgid={child_pgid})", file=sys.stderr)

    for t in pumps:
        t.join(timeout=_STALL_KILL_GRACE_SEC)
    with lock:
        out, err, hint = "".join(out_lines), "".join(err_lines), watch.error_hint

    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    completed = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if stall_reason and not hint:
        hint = err[-2000:] or out[-2000:]
    return completed, stall_reason, hint


def _report_limit_stall(task_dir: Path, stage: str, proc, reason: str,
                        hint: str, elapsed: float) -> int:
    """Persist a killed-early limit stall and return the RC_LIMIT_STALL sentinel.

    The hint (the api-error line, or the output tail when the storm was silent)
    goes into the usual `<stage>.claude-error.log` — that is where the park step
    later looks for a 'resets at' / retry-after to turn into resume_at."""
    print(f"error: stage {stage} limit-stalled ({reason}) after {elapsed:.0f}s "
          f"— child terminated, parking instead of burning the timeout",
          file=sys.stderr)
    _dump_stage_error(task_dir, stage, proc,
                      f"limit stall ({reason}) after {elapsed:.0f}s\n{hint}")
    return RC_LIMIT_STALL


def _limit_stall_text(task_dir: Path, stage: str) -> str:
    """The stalled stage's error log — the text `resume_at` is parsed from."""
    try:
        return (task_dir / f"{stage}.claude-error.log").read_text(
            errors="replace")[-8000:]
    except OSError:
        return ""


def _execute_single_stage(
    task_dir: Path, target_repo: Path, stage: str, state: dict,
    backend_override: str | None = None,
) -> int:
    """Exception-safe entry point for one stage. Any unhandled error becomes
    rc=1 so the caller marks the task `failed` (terminal) — instead of the
    runner crashing mid-stage, leaving state on a normal stage, and letting
    the watcher respawn-loop (the pattern-detector KeyError failure mode).

    backend_override forces a specific backend, bypassing spec-based routing —
    used by the auto-fallback to actually re-run the stage on anthropic."""
    try:
        return _execute_single_stage_inner(
            task_dir, target_repo, stage, state, backend_override,
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"error: stage {stage} crashed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1


def _restore_dev_lock_from_artifact(task_dir: Path) -> bool:
    """Re-derive the branch/PR lock from the Developer artifact on resume.

    The lock (``state.branch`` / ``state.pr_url``) is written when the Developer
    stage completes (see _execute_single_stage_inner). A resumed task SKIPS that
    stage when its artifact is already present, so in a fresh runner process the
    lock is absent even though the PR exists. The hotfix gate is fail-closed and
    rejects a hotfix it cannot verify — which failed a completed, fully green
    task (stt-local-path-source, 2026-08-15: the reviewer found one critical,
    the hotfix fixed it on the right branch and the right PR, and the runner
    still failed the task with rc=5 after $14.56 spent).

    Same class as #14 (cost_usd / iteration / triage / base_branch lost across
    re-ingest) — that fix simply did not cover these two fields.

    Reads only the artifact, never git: the lock records what the Developer
    stage DID, and re-deriving it from whatever branch the worktree happens to
    be on now would defeat the very drift check the gate exists to perform.
    Returns True when it wrote something.
    """
    state_path = task_dir / "state.json"
    if not state_path.exists():
        return False
    try:
        current = json.loads(state_path.read_text())
    except Exception as exc:
        print(f"warn: resume lock restore — unreadable state.json: {exc}",
              file=sys.stderr)
        return False
    if current.get("branch") and current.get("pr_url"):
        return False  # lock already intact — nothing to restore

    artifact = task_dir / STAGE_ARTIFACT_MAP["developer"]
    if not artifact.exists():
        return False
    try:
        text = artifact.read_text(errors="replace")
    except OSError as exc:
        print(f"warn: resume lock restore — unreadable {artifact.name}: {exc}",
              file=sys.stderr)
        return False

    branch = current.get("branch") or _extract_branch(text)
    pr_url = current.get("pr_url") or _extract_pr_url(text)
    if not branch or not pr_url:
        # Leave the lock absent rather than half-set: the gate must stay
        # fail-closed when the artifact genuinely does not name both.
        print("warn: resume lock restore — developer artifact names "
              f"branch={branch!r} pr_url={pr_url!r}; leaving lock unset",
              file=sys.stderr)
        return False

    current["branch"] = branch
    current["pr_url"] = pr_url
    try:
        state_path.write_text(json.dumps(current, indent=2) + "\n")
    except OSError as exc:
        print(f"warn: resume lock restore — write failed: {exc}", file=sys.stderr)
        return False
    print(f"[agent-pipeline] resume: restored branch/PR lock from "
          f"{artifact.name} (branch={branch}, pr={pr_url})")
    return True


def _execute_single_stage_inner(
    task_dir: Path, target_repo: Path, stage: str, state: dict,
    backend_override: str | None = None,
) -> int:
    """Run ONE pipeline stage via the Agent-tool orchestrator subprocess.

    Returns 0 on success (artifact written + verdict block produced); a
    non-zero error code matching the original main()'s exit conventions
    otherwise.

    Used by both single-stage CLI (--stage X) and multi-stage run_pipeline.
    """
    if stage not in STAGE_PROMPTS:
        print(
            f"error: stage '{stage}' not yet wired for Agent-tool path "
            f"(only {sorted(STAGE_PROMPTS.keys())} supported)",
            file=sys.stderr,
        )
        return 4

    # ── Build prompt ──
    fmt_kwargs = _build_format_kwargs(stage, task_dir, target_repo, state)
    prompt = STAGE_PROMPTS[stage].format(**fmt_kwargs)

    artifact_path = task_dir / STAGE_ARTIFACT_MAP[stage]
    json_path = task_dir / STAGE_ARTIFACT_MAP[stage].replace(".md", ".json")

    # ── Phase C Step 2d — backend rotation per stage ──
    # routing comes from spec.json.model_routing; iteration from state.json
    # (1 on initial pass, incremented per hotfix loop in run_pipeline).
    spec_path = task_dir / "spec.json"
    spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}
    routing = spec.get("model_routing") or {}
    iteration = int(state.get("iteration") or 1)

    # ── Task-scoped memory recall (roadmap #0) ──
    # Replaces the literal `(none)` slot the ba/architect/developer prompts
    # carry. Runner-side by design: the old UserPromptSubmit hook is dead and
    # stage sessions no longer start in this repo. Degrades to the unchanged
    # prompt on any infra failure — a stage never blocks on memory.
    _prompt_with_memory = _memory_inject.fill_slot(
        prompt, stage=stage,
        query=f"{stage}: {spec.get('prompt') or ''}",
        target_repo=str(target_repo))
    if _prompt_with_memory is not prompt:
        _append_worklog(task_dir, f"memory: recall injected into {stage} prompt")
    prompt = _prompt_with_memory
    # Triage tier (durable triage.json → sticky) scales this stage's backend and
    # wall-clock: L runs build/verify stages on anthropic upfront + a longer window.
    tier = (_load_persisted_triage(task_dir) or {}).get("tier") or state.get("tier")
    stage_timeout = _stage_timeout_for(tier)
    if backend_override:
        backend, escalated_from = backend_override, None
    else:
        backend, escalated_from = _resolve_stage_backend(stage, iteration, routing, tier)
    # Named key profile for this provider (T15). Only meaningful when
    # bot/providers.json exists; for_stage() drops the profile when the stage was
    # escalated or L-guarded onto another backend, because a deepseek profile
    # means nothing to anthropic.
    profile = _profiles.for_stage(routing, stage, backend,
                                  spec.get("provider_profile"))
    env = _subagent_env(backend, stage, profile=profile)
    if escalated_from:
        print(f"[agent-pipeline] stage={stage} backend escalated: "
              f"{escalated_from} -> {backend} (iteration {iteration} >= "
              f"{ESCALATION_AT_ITERATION})", file=sys.stderr)

    # ── Per-stage Claude session continuity ────────────────────────────────
    # First attempt for THIS stage: pre-assign a session id with --session-id
    # <uuid> and persist it to state.json BEFORE spawning so a kill mid-run
    # leaves the breadcrumb for the next attempt.
    # Re-run (id already in state.stage_sessions[<stage>]): use --resume <uuid>
    # with a short "continue from where you left off" prompt — the model
    # restores its in-stage conversation instead of starting over.
    # Fallback: if the stored session is gone ("No conversation found ..." —
    # 30-day cleanup or corruption), we retry ONCE with a fresh --session-id
    # and the full original prompt. On any other failure the id is kept so
    # the next watcher respawn can --resume.
    # On clean success (rc=0 at the end of this function) the id is cleared
    # so the next invocation of the same stage (e.g. developer-hotfix iter 2)
    # starts a brand-new session.
    prior_sessions = dict(state.get("stage_sessions") or {})
    prior_id = prior_sessions.get(stage)
    if prior_id:
        session_args = ["--resume", prior_id]
        effective_prompt = (
            f"You were running pipeline stage `{stage}` for this task inside "
            f"this same Claude session. You did not finish — the stage "
            f"artifact `{artifact_path.name}` was not written. Resume where "
            f"you left off and finish the remaining work, then write the "
            f"artifact at the path the original instructions named."
        )
        print(f"[stage-session] {stage}: --resume {prior_id} "
              f"(in-stage continuation)", file=sys.stderr)
    else:
        new_id = str(uuid.uuid4())
        prior_sessions[stage] = new_id
        try:
            _update_state(task_dir, stage_sessions=prior_sessions)
        except Exception as exc:  # noqa: BLE001
            print(f"warn: failed to persist stage_sessions[{stage}]: {exc}",
                  file=sys.stderr)
        session_args = ["--session-id", new_id]
        effective_prompt = prompt
        print(f"[stage-session] {stage}: --session-id {new_id} (fresh)",
              file=sys.stderr)

    base_args = ["claude", "--dangerously-skip-permissions"]
    tail_args = ["--output-format", "stream-json", "--verbose"]

    # ── Working directory + persona injection (2026-08-15) ──
    # The stage runs FROM the target project, so Claude Code loads the TARGET's
    # CLAUDE.md / AGENTS.md at startup — its language, build and test commands,
    # its house conventions. Until now no cwd was passed at all, so the child
    # inherited the daemon's directory (this repo) and every stage of every task
    # booted with ai-delivery's own instructions while developing something
    # else entirely.
    #
    # Personas come in through --agents instead of being resolved from
    # .claude/agents/ of the working directory, which is what makes moving the
    # cwd possible; --add-dir keeps this repo reachable for the task artifacts
    # under tasks/active/<id>/ and the vendored templates the prompts cite.
    work_dir = _agent_roster.stage_cwd(
        target_repo, (_persisted_worktree(task_dir, state) or {}).get("path"))
    roster_args = _agent_roster.agents_arg() + _agent_roster.add_dir_args(work_dir)
    print(f"[stage-cwd] {stage}: "
          f"{_agent_roster.describe(work_dir, len(_agent_roster.build_agents_payload()))}",
          file=sys.stderr)

    # ── Run orchestrator ──
    t0 = time.time()
    try:
        proc, stall_reason, stall_hint = _run_claude_stage(
            base_args + session_args + roster_args + tail_args
            + ["-p", effective_prompt],
            env=env, timeout=stage_timeout, stage=stage, cwd=str(work_dir),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - t0
        # A wall-clock timeout is NOT a crash. Returning RC_STAGE_TIMEOUT (not 1)
        # lets the caller (a) skip the anthropic auto-fallback — a second full
        # timeout window was ~half the M-validation incident's $17.31 — and
        # (b) route the partial work through the graceful handoff instead of
        # a silent FAILED.
        print(f"error: stage {stage} timed out after {elapsed:.0f}s "
              f"(timeout={stage_timeout}s, tier={tier or '-'})", file=sys.stderr)
        _dump_stage_error(task_dir, stage, exc,
                          f"stage timed out after {elapsed:.0f}s")
        return RC_STAGE_TIMEOUT

    if stall_reason:
        return _report_limit_stall(task_dir, stage, proc, stall_reason,
                                   stall_hint, time.time() - t0)

    # ── Fallback: stored session vanished ⇒ one retry with a fresh id ──────
    if prior_id is not None and proc.returncode != 0:
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if "No conversation found" in combined:
            print(f"[stage-session] {stage}: stored session {prior_id} is gone "
                  f"— retrying with a fresh session id", file=sys.stderr)
            new_id = str(uuid.uuid4())
            prior_sessions[stage] = new_id
            try:
                _update_state(task_dir, stage_sessions=prior_sessions)
            except Exception:
                pass
            try:
                proc, stall_reason, stall_hint = _run_claude_stage(
                    base_args + ["--session-id", new_id] + tail_args
                    + ["-p", prompt],
                    env=env, timeout=stage_timeout, stage=stage,
                )
            except subprocess.TimeoutExpired as exc:
                elapsed = time.time() - t0
                print(f"error: stage {stage} timed out after {elapsed:.0f}s "
                      f"(timeout={stage_timeout}s, tier={tier or '-'}, "
                      f"fresh retry after resume miss)", file=sys.stderr)
                _dump_stage_error(task_dir, stage, exc,
                                  f"stage timed out after {elapsed:.0f}s")
                return RC_STAGE_TIMEOUT
            if stall_reason:
                return _report_limit_stall(task_dir, stage, proc, stall_reason,
                                           stall_hint, time.time() - t0)

    elapsed = time.time() - t0

    # #18 — the child is gone; before touching ANY path under the task folder,
    # re-resolve it: a parallel park may have moved it to awaiting-input/ while
    # this stage ran, and writing through the stale path re-creates it.
    task_dir = _task_dir_now(task_dir)
    artifact_path = task_dir / STAGE_ARTIFACT_MAP[stage]
    json_path = task_dir / STAGE_ARTIFACT_MAP[stage].replace(".md", ".json")

    # Honest provider pricing: the CLI prices every endpoint at Anthropic
    # rates (~22× for DeepSeek), which fed cost_cap_usd — see
    # backend_routing.apply_backend_pricing. Applied at the ONE point where
    # cost enters the artifact, so _read_stage_cost_usd and everything
    # downstream get the corrected figure with no further changes.
    cost_info = _apply_backend_pricing(backend, _parse_cost(proc.stdout))

    if proc.returncode != 0:
        print(f"orchestrator exited rc={proc.returncode}", file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        _dump_stage_error(task_dir, stage, proc, f"orchestrator exited rc={proc.returncode}")
        return 1

    # ── Check completion marker ──
    done_marker = STAGE_DONE_MARKERS.get(stage)
    if done_marker and done_marker not in proc.stdout:
        print(f"orchestrator did not print completion marker ({done_marker})",
              file=sys.stderr)

    # ── Verify artifact ──
    if not artifact_path.exists():
        print(f"error: artifact {artifact_path} not written", file=sys.stderr)
        _dump_stage_error(task_dir, stage, proc, f"artifact {artifact_path.name} not written")
        return 3

    artifact_text = artifact_path.read_text()

    # ── Stage-dependent verdict parsing ──
    if stage == "reviewer":
        m = VERDICT_RE.search(artifact_text)
        if m:
            verdict_block = {
                "verdict": m.group(1).lower(),
                "critical": int(m.group(2)),
                "warning": int(m.group(3)),
                "suggestion": int(m.group(4)),
            }
        else:
            verdict_block = {"verdict": "unparseable", "critical": None,
                             "warning": None, "suggestion": None}
    elif stage == "architect":
        completed = "ARCHITECT_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "discovery":
        completed = "DISCOVERY_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "ba":
        completed = "BA_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "pattern-detector":
        completed = "PATTERNS_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "tasks":
        completed = "TASKS_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "analyze":
        completed = "ANALYZE_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "edge-cases":
        completed = "EDGECASES_COMPLETE" in proc.stdout
        verdict_block = {"verdict": "complete" if completed else "incomplete"}
    elif stage == "security":
        sec_re = re.compile(
            r"SECURITY_COMPLETE:\s*(.+?)\s*\n"
            r"CRITICAL:\s*(\d+)\s*\n"
            r"WARNING:\s*(\d+)\s*\n"
            r"SUGGESTION:\s*(\d+)",
            re.IGNORECASE | re.MULTILINE,
        )
        m = sec_re.search(artifact_text)
        if m:
            verdict_block = {
                "verdict": "complete",
                "summary": m.group(1).strip(),
                "critical": int(m.group(2)),
                "warning": int(m.group(3)),
                "suggestion": int(m.group(4)),
            }
        else:
            verdict_block = {"verdict": "unparseable"}
    elif stage in ("developer", "developer-hotfix"):
        # Robust parse: anchor PR on URL shape + branch on a flexible marker.
        # DeepSeek writes `**PR:** <url>` / `**Branch:** `x`` (Markdown), which
        # the old strict DEV_COMPLETE/BRANCH:/PR_URL: regex missed — leaving
        # pr_url unpersisted and breaking the hotfix loop (Bug-8).
        pr_url = _extract_pr_url(artifact_text)
        branch = _extract_branch(artifact_text)
        # GIT-TRUTH overrides self-report for the safety gate: a subagent can
        # write `Branch: feat/x` while actually committing to main; git HEAD
        # cannot lie. Prefer the branch the repo is really on.
        git_branch = _current_git_branch(target_repo)
        if git_branch:
            branch = git_branch
        # Fallback: the subagent created the PR but may have omitted the trailer
        # block. Recover branch + PR URL from the repo's actual git/gh state so
        # state.pr_url is persisted (needed by the hotfix loop AND the
        # [Да]/[Нет] merge button + the Telegram PR link).
        if not pr_url or not branch:
            rec_branch, rec_pr = _recover_pr_from_repo(target_repo)
            branch = branch or rec_branch
            pr_url = pr_url or rec_pr
        tm = re.search(r"TESTS:\s*(\d+)\s*/\s*(\d+)", artifact_text, re.IGNORECASE)
        tests = (
            {"tests_passed": int(tm.group(1)), "tests_total": int(tm.group(2))}
            if tm else {}
        )
        if not pr_url or not branch:
            verdict_block = {"verdict": "unparseable", "branch": branch, "pr_url": pr_url}
        elif stage == "developer-hotfix":
            # Hotfix must stay on the existing branch and PR — drift is the
            # safety violation here, not the phase-b4-poc- prefix. FAIL-CLOSED:
            # if the lock target was never persisted (resumed/partial task), we
            # cannot verify the hotfix stayed put → treat as a violation, never
            # accept a hotfix on whatever branch git happens to be on.
            expected_branch = state.get("branch") or ""
            expected_pr = state.get("pr_url") or ""
            if not expected_branch or not expected_pr:
                verdict_block = {
                    "verdict": "safety_violation", "branch": branch,
                    "reason": "hotfix ran without a persisted branch/PR lock "
                              "(cannot verify it stayed on the original PR)",
                }
            elif branch != expected_branch:
                verdict_block = {
                    "verdict": "safety_violation", "branch": branch,
                    "reason": f"hotfix drifted from {expected_branch!r}",
                }
            elif expected_pr and pr_url != expected_pr:
                verdict_block = {
                    "verdict": "safety_violation", "pr_url": pr_url,
                    "reason": f"hotfix opened new PR (expected {expected_pr!r})",
                }
            else:
                verdict_block = {
                    "verdict": "complete", "branch": branch, "pr_url": pr_url, **tests,
                }
        elif not _branch_safety_ok(branch, _poc_mode_for_target(target_repo),
                                   _worktree_branch(state)):
            expected = _worktree_branch(state)
            verdict_block = {
                "verdict": "safety_violation", "branch": branch,
                "reason": (f"developer left the worktree branch {expected!r}"
                           if expected else
                           "branch missing phase-b4-poc- prefix (PoC mode)"
                           if _poc_mode_for_target(target_repo)
                           else "developer committed to a default branch (real mode)"),
            }
        else:
            # Branch NAME is safe — now enforce the branch BASE for REAL/mergeable
            # targets: the name gate cannot detect a correctly-named branch cut
            # from a stale local HEAD (the 2026-05-31 contamination). Skip for PoC
            # (throwaway PRs; sandbox base may differ). See _branch_base_ok.
            base_ok, base_reason = (True, "poc — base check skipped")
            if not _poc_mode_for_target(target_repo):
                # Same value the prompt was rendered with (state.base_branch),
                # so prompt and check can never drift. target_repo here is the
                # task's worktree — that is exactly where HEAD must be checked.
                base_ok, base_reason = _branch_base_ok(
                    target_repo, state.get("base_branch") or _base_branch(target_repo))
            if not base_ok:
                verdict_block = {
                    "verdict": "safety_violation", "branch": branch,
                    "reason": f"stale base: {base_reason}",
                }
            else:
                verdict_block = {
                    "verdict": "complete", "branch": branch, "pr_url": pr_url, **tests,
                }
                # Post-create verification (issue #10): _branch_base_ok above
                # proves the BRANCH was cut from the right base, but the
                # subagent's own `gh pr create` can still drop `--base` and
                # let gh silently target the repo's default branch — exactly
                # what happened 2026-08-12 (PR opened against `dev-fix`
                # instead of the registry-resolved `feat/local-longpolling`)
                # even though the prompt told it to pass `--base`. Verify the
                # opened PR's actual baseRefName and self-repair with one
                # `gh pr edit --base` retry before trusting the PR. Skipped
                # for PoC targets — same gating as the base check above,
                # those PRs are throwaways that are closed, never merged.
                if pr_url and not _poc_mode_for_target(target_repo):
                    expected_base = state.get("base_branch") or _base_branch(target_repo)
                    pr_base_ok, pr_base_note = _verify_and_repair_pr_base(
                        target_repo, pr_url, expected_base)
                    verdict_block["pr_base_check"] = pr_base_note
                    if not pr_base_ok:
                        print(f"error: PR base mismatch for {pr_url} could not "
                              f"be repaired — {pr_base_note}", file=sys.stderr)
                        _append_history(
                            task_dir, stage,
                            f"WARNING: {pr_url} base != {expected_base!r} and "
                            f"the automatic repair failed — {pr_base_note}",
                        )
    elif stage == "tester":
        test_re = re.compile(
            r"TEST_COMPLETE:\s*(.+?)\s*\n"
            r"TESTS_ADDED:\s*(\d+)\s*\n"
            r"ACS_COVERED:\s*(\d+)",
            re.IGNORECASE | re.MULTILINE,
        )
        m = test_re.search(artifact_text)
        if m:
            verdict_block = {
                "verdict": "complete",
                "summary": m.group(1).strip(),
                "tests_added": int(m.group(2)),
                "acs_covered": int(m.group(3)),
            }
        else:
            verdict_block = {"verdict": "unparseable"}
    else:
        verdict_block = {"verdict": "unknown"}

    # ── Write structured result ──
    structured = {
        "task_id": task_dir.name,
        "elapsed_sec": elapsed,
        "stage": f"{stage}-agent-poc",
        "backend": backend,
        "profile": profile,
        "escalated_from": escalated_from,
        "verdict": verdict_block,
        "cost": cost_info,
        "artifact_path": str(artifact_path),
    }
    try:
        json_path.write_text(json.dumps(structured, indent=2) + "\n")
    except FileNotFoundError:
        # The task dir vanished for good (deleted, not moved — _task_dir_now
        # already followed a move). Log it; never mkdir it back into existence.
        print(f"warn: task dir {task_dir} is gone — dropping {json_path.name}",
              file=sys.stderr)
    print(json.dumps(structured, indent=2))

    # Durable cost row (survives tasks/done cleanup). Written at the same
    # point as the artifact so the ledger and the artifact can never carry
    # different figures. record() is best-effort by contract — a ledger
    # failure must not fail a green stage.
    if cost_info:
        _cost_ledger.record(
            task_id=task_dir.name,
            stage=stage,
            backend=backend,
            profile=profile,
            cost_usd=float(cost_info.get("total_cost_usd") or 0.0),
            source=cost_info.get("cost_source") or "cli",
            input_tokens=cost_info.get("input_tokens"),
            output_tokens=cost_info.get("output_tokens"),
            cache_read_tokens=cost_info.get("cache_read_tokens"),
            cache_creation_tokens=cost_info.get("cache_creation_tokens"),
            elapsed_sec=elapsed,
            session_id=cost_info.get("session_id"),
        )

    # ── Persist branch + pr_url to state.json after Developer stage so the
    # hotfix iteration knows what to checkout / push to. Best-effort —
    # state.json may not exist in pure single-stage smoke runs.
    if stage in ("developer", "developer-hotfix") and verdict_block.get("verdict") == "complete":
        state_path = task_dir / "state.json"
        if state_path.exists():
            try:
                current = json.loads(state_path.read_text())
                current["branch"] = verdict_block.get("branch") or current.get("branch")
                current["pr_url"] = verdict_block.get("pr_url") or current.get("pr_url")
                state_path.write_text(json.dumps(current, indent=2) + "\n")
            except Exception as exc:
                print(f"warn: failed to persist branch/pr to state.json: {exc}",
                      file=sys.stderr)

    # Enforce the branch/PR safety gate in the RUNNER, not just the prompt: a
    # safety_violation (developer committed to a default branch, or hotfix
    # drifted off the locked branch/PR) HALTS the stage (rc=5) instead of
    # soft-continuing to tester/security/reviewer as if it passed.
    if (stage in ("developer", "developer-hotfix")
            and verdict_block.get("verdict") == "safety_violation"):
        reason = verdict_block.get("reason", "branch/PR safety violation")
        print(f"error: {stage} SAFETY VIOLATION — {reason}", file=sys.stderr)
        _dump_stage_error(task_dir, stage, proc, f"safety_violation: {reason}")
        return 5

    # Stage finished cleanly — clear its stored session id so the next
    # invocation of this stage (e.g. a new developer-hotfix iteration) opens a
    # brand-new Claude conversation instead of --resume-ing the one that just
    # produced this artifact.
    try:
        ss = dict(state.get("stage_sessions") or {})
        if stage in ss:
            ss.pop(stage, None)
            _update_state(task_dir, stage_sessions=ss)
    except Exception:
        pass
    return 0


# Sequential stages run by run_pipeline. NOTE: Phase C Step 1 is the SKELETON
# only — no REQUEST_CHANGES loop, no parallel Tester+Security, no model
# routing, no cost cap enforcement, no Telegram side-effects. Phase C Step 2
# will port these from stage_runner.py:run_pipeline.
_PIPELINE_STAGES_BASE = [
    "ba", "architect", "developer", "tester", "security", "reviewer",
]


# ── C.2 — interactive clarification pause ────────────────────────────────
# After BA completes, if CLARIFY_INTERACTIVE_ENABLED=1 and the BA artifact
# still has [NEEDS CLARIFICATION:] markers, persist a question payload, move
# the task to awaiting-input/, notify the bot, and return rc=7 to stop the
# pipeline. The bot collects answers, writes clarifications.md, moves the
# task back to inbox/, and BA re-ingests with the answers next pass.
_CLARIFY_PAUSE_RC = 7


# ── Notification self-containment ───────────────────────────────────────────
# A remote operator reads notifications in Telegram and CANNOT open a server-local
# file path. Notifications that only cited a filename ("see 01-ba-invest.md" /
# "see UNRESOLVED-FINDINGS.md") were dead ends (operator: "файла там нет").
# _report_digest inlines the report's CONTENT (capped) into the message instead.
# botctl-send-text chunks at 4000 chars and converts/escapes Markdown with a
# plain-text retry on failure, so a trimmed body is safe.
def _report_digest(text: str, cap: int = 1600) -> str:
    """Trim a report body for inlining into a Telegram notification. Caps length
    (the useful summary is at the TOP of both INVEST and findings reports) and
    flags truncation. Empty input → empty string (caller adds its own header)."""
    body = (text or "").strip()
    if len(body) > cap:
        body = body[:cap].rstrip() + "\n…(truncated — full report in the task folder)"
    return body


def _maybe_run_invest_validation(task_dir: Path, task_id: str) -> int:
    """C.3 INVEST validation of the BA artifact. Off unless
    INVEST_VALIDATION_ENABLED=1.

    Writes `01-ba-invest.md` next to the BA artifact with INVEST violations
    (Testable / Small / vague-verb / missing-AC) detected by static patterns.

    Returns 0 when disabled, when the artifact is absent, or when the spec
    passes. Returns 1 (BLOCK the pipeline) when violations are found and
    blocking is active — blocking is the default once validation is enabled
    (compass / ROADMAP C.3: "auto-reject + feedback to BA"). Set
    INVEST_BLOCKING=0 to fall back to the legacy warn-only behavior.
    """
    if os.environ.get("INVEST_VALIDATION_ENABLED", "").strip() != "1":
        return 0
    artifact = task_dir / "01-ba-agent.md"
    if not artifact.exists():
        artifact = task_dir / "01-ba.md"
    if not artifact.exists():
        return 0
    report = _invest.validate_artifact(artifact)
    report_path = task_dir / "01-ba-invest.md"
    blocking = os.environ.get("INVEST_BLOCKING", "1").strip() != "0"
    report_text = _invest.format_report(report, blocking=blocking)
    report_path.write_text(report_text, encoding="utf-8")
    if report.ok:
        _append_worklog(
            task_dir,
            f"INVEST: ok — FR={report.fr_count} AC={report.ac_count}; "
            f"report at {report_path.name}",
        )
        return 0

    mode = "BLOCK" if blocking else "warn-only"
    _append_worklog(
        task_dir,
        f"INVEST ({mode}): {len(report.violations)} violation(s) — "
        f"FR={report.fr_count} AC={report.ac_count}; see {report_path.name}",
    )
    # Diagnostic detail, not its own notification: a BLOCK outcome is a
    # terminal failure and gets exactly one message via _handoff_terminal;
    # a warn-only outcome doesn't stop the pipeline, so it isn't one of the
    # default-notify categories either (issue #19). Full detail stays in
    # 01-ba-invest.md + worklog.md; NOTIFY_VERBOSE=1 also mirrors it here.
    if should_notify("invest_report", verbose=verbose_enabled()):
        _send_telegram(
            f"[{task_id}] INVEST {mode}: {len(report.violations)} violation(s) "
            f"in BA artifact (FR={report.fr_count}, AC={report.ac_count}):\n\n"
            + _report_digest(report_text)
        )
    return 1 if blocking else 0


def _maybe_run_architecture_lint(task_dir: Path, task_id: str) -> None:
    """Steal-list §2.6/#10 (adapt) — deterministic structural pre-check of the
    Architect artifact (ADR field completeness, C4 Mermaid presence, leftover
    placeholders), run right after the Architect stage completes and BEFORE
    the 'analyze' stage's LLM pass, so that pass spends judgment on the
    semantic half rather than re-discovering mechanical gaps. Off unless
    ARCHITECTURE_LINT_ENABLED=1.

    Unlike _maybe_run_invest_validation, this is ALWAYS report-only — it never
    blocks the pipeline (weaker-confidence adapt, not a BMAD adopt-fragment;
    see stage_prompts.py's architect ADR section and architecture_lint.py's
    module docstring for the schema this deliberately does NOT adopt).
    """
    if os.environ.get("ARCHITECTURE_LINT_ENABLED", "").strip() != "1":
        return
    artifact = task_dir / "02-architecture-agent.md"
    if not artifact.exists():
        artifact = task_dir / "02-architecture.md"
    if not artifact.exists():
        return
    report = _archlint.validate_artifact(artifact)
    report_path = task_dir / "architecture-lint.md"
    report_path.write_text(_archlint.format_report(report), encoding="utf-8")
    if report.ok:
        _append_worklog(
            task_dir,
            f"ARCHITECTURE LINT: ok — ADRs={report.adr_count}; "
            f"report at {report_path.name}",
        )
    else:
        _append_worklog(
            task_dir,
            f"ARCHITECTURE LINT (advisory): {len(report.violations)} finding(s) "
            f"— ADRs={report.adr_count}; see {report_path.name}",
        )


def _maybe_pause_for_clarifications(
    task_dir: Path, task_id: str, cumulative_cost: float,
) -> int:
    """Returns _CLARIFY_PAUSE_RC if paused, 0 otherwise (no markers / flag off).

    Caller must propagate the rc all the way out of run_pipeline so the
    subprocess exits with that code (task_dispatcher just logs the rc).
    """
    if os.environ.get("CLARIFY_INTERACTIVE_ENABLED", "").strip() != "1":
        return 0
    artifact = task_dir / "01-ba-agent.md"
    if not artifact.exists():
        artifact = task_dir / "01-ba.md"
    if not artifact.exists():
        return 0
    questions = _clarify.extract_pending_markers(artifact.read_text())
    if not questions:
        return 0

    _clarify.write_pending_payload(task_dir, questions)
    _update_state(
        task_dir,
        stage=_clarify.PAUSED_STAGE,
        clarify_pending={"count": len(questions), "questions": questions},
        cost_usd=cumulative_cost,
        # An EXPLICIT pause stamp for the dead-man sweep (T10). The state.json
        # mtime cannot serve: re-ingest rewrites the file with carried-forward
        # fields, which would reset the clock and keep the task waiting forever.
        clarify_paused_at=_now_iso(),
    )
    _append_history(
        task_dir, _clarify.PAUSED_STAGE,
        f"paused for {len(questions)} clarification question(s)",
    )
    _append_worklog(
        task_dir,
        f"PAUSED: BA emitted {len(questions)} unresolved [NEEDS CLARIFICATION] "
        "marker(s); awaiting operator answers via Telegram reply.",
    )

    _reap_children_before_move(task_id, "pausing for clarifications")
    _AWAITING_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = _AWAITING_INPUT_DIR / task_id
    if dst.exists():
        shutil.rmtree(str(dst))
    shutil.move(str(task_dir), str(dst))

    _notify_bot(
        "clarify_needed",
        task_id,
        questions=questions,
        count=len(questions),
    )
    return _CLARIFY_PAUSE_RC


_HANDOFF_REASONS = {
    RC_STAGE_TIMEOUT: "stage timed out (non-convergent / slow stage)",
    RC_LIMIT_STALL: "backend limits exhausted mid-stage (retry storm) — parked "
                    "until the window resets, then auto-requeued",
    RC_RATE_LIMITED: "backend hit the Claude session/rate limit and the "
                     "cross-provider fallback was exhausted — re-queue after "
                     "the window resets",
    1: "stage failed (crash; anthropic fallback also failed)",
    2: "pre-stage gate failed (BA / INVEST / quality)",
    3: "stage artifact not written",
    4: "stage not wired",
}


def _write_unresolved_findings(
    task_dir: Path, *, reason: str, rc: int, cost: float,
    pr_url: str, trend: list,
) -> None:
    """English handoff doc: the open reviewer findings + what the pipeline
    attempted, so the operator can finish by hand or re-queue. Best-effort."""
    rj = task_dir / "06-review-agent.json"
    if not rj.is_file():
        rj = task_dir / "06-review.json"
    verdict: dict = {}
    try:
        if rj.is_file():
            verdict = (json.loads(rj.read_text()).get("verdict") or {})
    except Exception:
        verdict = {}
    err_tail = ""
    try:
        logs = sorted(task_dir.glob("*.claude-error.log"),
                      key=lambda p: p.stat().st_mtime)
        if logs:
            err_tail = logs[-1].read_text(errors="replace")[-3000:]
    except Exception:
        err_tail = ""
    lines = [
        "# Unresolved findings — pipeline handoff",
        "",
        f"- Task: `{task_dir.name}`",
        f"- Stopped because: {reason} (rc={rc})",
        f"- Cost so far: ${cost:.4f}",
        f"- Critical-count trend (per reviewer pass): {trend or []}",
        f"- PR: {pr_url or '(none recovered)'}",
        "",
        "## Last reviewer verdict",
        f"- verdict: {verdict.get('verdict', 'n/a')}",
        f"- critical: {verdict.get('critical', 'n/a')}",
        f"- warning: {verdict.get('warning', 'n/a')}",
        f"- suggestion: {verdict.get('suggestion', 'n/a')}",
        "",
        "## What to do",
        "The pipeline stopped before the reviewer approved. The branch/PR above "
        "holds the work completed so far. Read the open Critical findings in "
        "`06-review.md` (this task dir), then either finish them by hand on the "
        "branch or re-queue the task with extra guidance.",
        "",
    ]
    if err_tail:
        lines += ["## Stage error (tail)", "", "```", err_tail, "```", ""]
    try:
        (task_dir / "UNRESOLVED-FINDINGS.md").write_text(
            "\n".join(lines), encoding="utf-8")
    except OSError:
        pass


def _handoff_terminal(
    task_dir: Path, target_repo: Path, task_id: str, *,
    rc: int, cumulative_cost: float,
) -> int:
    """Graceful terminal handoff for a failed / timed-out / non-convergent
    pipeline (PR2, committee 2026-06-02). Best-effort throughout — never raises;
    a handoff hiccup must not mask the original rc.

      1. Recover the PR the developer already pushed (state.pr_url else
         _recover_pr_from_repo); optionally open a DRAFT PR for a worked branch
         that never got one. This reviewable artifact is the work we refuse to
         throw away — the incident pushed PR #10 then `return rc` discarded it.
      2. Write UNRESOLVED-FINDINGS.md (verdict counts, critical trend, reason).
      3. Pick the bucket: a recovered PR / worked branch -> awaiting-input/
         (operator-actionable); nothing pushed (pre-stage error) -> failed/.
      4. Set state.stage to the terminal label and shutil.move into the bucket
         (mirrors _maybe_pause_for_clarifications), then notify.

    Returns _HANDOFF_RC; the caller propagates it out of run_pipeline."""
    reason = _HANDOFF_REASONS.get(rc, f"stage failed (rc={rc})")

    # ── 1. recover / open PR ──
    # From the task's WORKTREE when there is one (#6): that is where the work
    # branch is checked out — the live target checkout sits on its own branch
    # and would make the recovery read a foreign HEAD.
    wt = _persisted_worktree(task_dir)
    work_repo = Path(wt["path"]) if _worktree_alive(wt) else target_repo
    pr_url, branch, trend = "", None, []
    try:
        st = json.loads((task_dir / "state.json").read_text())
        pr_url = (st.get("pr_url") or "").strip()
        trend = st.get("review_trend") or []
    except Exception:
        pass
    if not pr_url:
        try:
            branch, recovered = _recover_pr_from_repo(work_repo)
            if recovered:
                pr_url = recovered
            elif branch:
                pr_url = _try_open_draft_pr(work_repo, branch, task_id) or ""
        except Exception:
            branch = None
    if pr_url:
        try:
            _update_state(task_dir, pr_url=pr_url)
        except Exception:
            pass

    # Terminal stop: drop the throwaway checkout now that everything pushable is
    # pushed (the BRANCH stays — a re-queue re-attaches a worktree to it). Must
    # run BEFORE the bucket move below, which relocates state.json.
    _remove_task_worktree(task_dir)

    # ── 2. findings doc ──
    _write_unresolved_findings(
        task_dir, reason=reason, rc=rc, cost=cumulative_cost,
        pr_url=pr_url, trend=trend,
    )
    # Capture the findings BEFORE the bucket move so the notification can inline
    # them — the remote operator can't open the server-local file (the cited path
    # was a dead end; observed 2026-06-05).
    try:
        findings_text = (task_dir / "UNRESOLVED-FINDINGS.md").read_text(errors="replace")
    except OSError:
        findings_text = ""

    # ── 3. bucket choice ──
    has_work = bool(pr_url) or bool(branch)
    if has_work:
        bucket_dir, terminal_stage = _AWAITING_INPUT_DIR, "awaiting-input"
    else:
        bucket_dir, terminal_stage = _FAILED_DIR, "failed"

    _append_worklog(
        task_dir,
        f"HANDOFF: {reason} — bucket={bucket_dir.name}, pr={pr_url or 'none'}, "
        f"trend={trend}",
    )
    _append_history(task_dir, "handoff", f"{reason} -> {terminal_stage}")

    # ── 4. set terminal stage + move bucket ──
    try:
        _update_state(task_dir, stage=terminal_stage,
                      handoff={"reason": reason, "rc": rc, "pr_url": pr_url})
    except Exception:
        pass
    try:
        _reap_children_before_move(task_id, f"handing off to {bucket_dir.name}/")
        bucket_dir.mkdir(parents=True, exist_ok=True)
        dst = bucket_dir / task_id
        if dst.exists():
            shutil.rmtree(str(dst))
        shutil.move(str(task_dir), str(dst))
    except Exception as exc:  # noqa: BLE001
        print(f"warn: handoff bucket move failed: {exc}", file=sys.stderr)

    # ── 5. notify ──
    try:
        digest = _report_digest(findings_text)
        if pr_url:
            _send_telegram(
                f"[{task_id}] ⏸️ handed off — {reason}.\n"
                f"Partial work preserved: {pr_url}\n\n{digest}")
        else:
            _send_telegram(
                f"[{task_id}] ⛔ {reason} — no recoverable PR (moved to "
                f"{terminal_stage}/).\n\n{digest}")
        _notify_bot("pipeline_handoff", task_id, reason=reason, rc=rc,
                    pr_url=pr_url, next_stage=terminal_stage,
                    cost_usd=cumulative_cost)
    except Exception:
        pass
    return _HANDOFF_RC


def _terminate_pipeline(
    task_dir: Path, target_repo: Path, task_id: str,
    rc: int, cumulative_cost: float,
) -> int:
    """Disposition for a non-zero stage rc inside run_pipeline. Replaces the bare
    `return rc` that threw away pushed work and stranded the task in active/.
    Two deliberate pass-throughs (committee 2026-06-02):
      - _CLARIFY_PAUSE_RC: the clarify pause ALREADY self-moved the task to
        awaiting-input/; re-moving a vanished dir would crash.
      - rc == 5 (cost/token cap): the cap stop ALREADY self-moved the task to
        awaiting-input/ via budget_gate.park (T08) — the operator gate of
        2026-06-07, which the mid-pipeline checks used to bypass. The committee's
        2026-06-02 constraint still holds as written: the graceful handoff must
        not reclassify a budget abort, so the pass-through stays.
      - RC_LIMIT_STALL (#11): the limit park ALREADY self-moved the task to
        awaiting-input/ with a resume_at; a handoff would rewrite it as a
        terminal failure and drop the auto-requeue.
    Everything else routes through the graceful handoff."""
    if rc in (_CLARIFY_PAUSE_RC, 5, RC_LIMIT_STALL):
        return rc
    return _handoff_terminal(
        task_dir, target_repo, task_id, rc=rc, cumulative_cost=cumulative_cost)


def _check_ba_quality_gate(task_dir: Path) -> list[str]:
    """WS-3b — programmatic hard gate on the Spec-Kit Specification Quality
    Checklist the BA writes to checklists/requirements.md. Per compass §1 this
    checklist is a HARD GATE before Architect, not a self-report. Returns a
    list of problems (empty list = pass). Checks:
      - the checklist file exists,
      - every `- [ ]` box is ticked (no unchecked items remain),
      - no `[NEEDS CLARIFICATION` marker survives in the BRD.
    """
    problems: list[str] = []
    checklist = task_dir / "checklists" / "requirements.md"
    if not checklist.is_file():
        return ["checklists/requirements.md not produced by BA"]
    text = checklist.read_text(encoding="utf-8", errors="replace")
    # `\[\s*\]` catches `[]`, `[ ]`, `[  ]` — any never-ticked box.
    unchecked = len(re.findall(r"^\s*[-*]\s*\[\s*\]", text, re.MULTILINE))
    if unchecked:
        problems.append(f"{unchecked} unchecked quality-checklist item(s)")
    brd = task_dir / "01-ba.md"
    if brd.is_file():
        brd_text = brd.read_text(encoding="utf-8", errors="replace")
        # Require the colon form `[NEEDS CLARIFICATION: ...]` (matches the BA
        # convention + clarify.py's matcher) so a prose mention such as
        # "No [NEEDS CLARIFICATION] markers remain" does NOT false-block a
        # clean BRD.
        if re.search(r"\[NEEDS\s+CLARIFICATION\s*:", brd_text, re.IGNORECASE):
            problems.append("unresolved [NEEDS CLARIFICATION:] marker(s) in BRD")
    return problems


# ── Adaptive complexity triage (committee RFC) ───────────────────────────
# A feed-forward layer that sizes the pipeline to the task instead of running
# the maximum pipeline on every task (the $16.95-for-a-10-line-function
# pathology). Staged rollout is a single dial:
#   TRIAGE_MODE = off | shadow | s-only | full
#     off     — no-op; byte-identical to pre-triage behaviour (explicit opt-out)
#     shadow  — Stage 1: classify + log + write state.triage, run FULL pipeline (DEFAULT)
#     s-only  — Stage 2: act (size the pipeline + caps) on S-tier only (recommended deployment)
#     full    — Stage 3: act on S and M tiers (L is the full pipeline anyway)
# Global kill switch TRIAGE_DISABLED=1 forces `off` regardless of TRIAGE_MODE
# (the RFC §Q8 backward-compatibility guarantee / rollback path).


def _maybe_run_triage(task_dir: Path, target_repo: Path, state: dict,
                      spec: dict, task_id: str):
    """RFC Stages 0–3 entry point. Classifies the task, writes `state.triage`
    + `00a-triage.md` + a durable `triage.json`, and (when acting on an S-tier
    task that skips BA) writes a lite BRD. Returns the Triage object (fresh run),
    the persisted verdict dict (sticky reuse), or None when triage is off/errored
    — the caller treats a non-None return only as a 'triage produced a verdict'
    flag and reads the actual tier/caps from state.triage. NEVER raises — any
    failure leaves state.triage absent so _pipeline_stages_for_agent falls back
    to the full pipeline (fail-safe)."""
    mode = _triage_mode()
    if mode == "off":
        return None

    # Sticky triage (committee follow-up 2026-06-02): reuse a verdict already
    # computed for this task instead of re-classifying. The dispatcher writes a
    # fresh state.json on every (re-)ingest, so a clarify round-trip would
    # otherwise re-run triage — and that is non-deterministic: the best-effort
    # LLM verdict can flake on the second pass, leaving deterministic-only
    # conf 0.50 which trips the <0.70 fail-safe and silently downgrades M→L
    # (the exact regression that derailed the 06-02 M-validation). The durable
    # triage.json survives the state reset. Disable with TRIAGE_STICKY=0.
    if os.environ.get("TRIAGE_STICKY", "1").strip() != "0":
        persisted = _load_persisted_triage(task_dir)
        if persisted is not None:
            reused = dict(persisted)
            reused["reasons"] = (persisted.get("reasons") or []) + [
                "reused across re-ingest (sticky triage — not re-classified)"]
            _update_state(task_dir, triage=reused)
            _append_worklog(
                task_dir,
                f"triage: reused persisted verdict tier={persisted.get('tier')} "
                f"(conf={persisted.get('confidence')}) — sticky across re-ingest, "
                f"not re-classifying")
            _append_history(task_dir, "triage",
                            f"reused tier={persisted.get('tier')} (sticky)")
            # Diagnostic, not a lifecycle event: this fires on EVERY re-ingest
            # of the same task (limit-park resume, clarify round-trip, watcher
            # respawn) — issue #19 folds it under NOTIFY_VERBOSE=1 so a resume
            # notifies once (the "auto_resumed" message in run_pipeline), not
            # via this plus "pipeline started" plus "stage started".
            if should_notify("triage_reused", verbose=verbose_enabled()):
                _send_telegram(
                    f"[{task_id}] TRIAGE: reused tier {persisted.get('tier')} "
                    f"(sticky across re-ingest — verdict preserved)")
            return reused  # non-None: caller only checks `is not None`

    try:
        prompt = str(spec.get("prompt") or "")
        use_llm = os.environ.get("TRIAGE_LLM_ENABLED", "1").strip() != "0"
        runner = _triage_run_claude if use_llm else None
        tri = _triage.decide(prompt, target_repo, spec, run_claude=runner)
    except Exception as exc:  # noqa: BLE001 — triage must never crash the pipeline
        print(f"warn: triage crashed ({type(exc).__name__}: {exc}) — full pipeline",
              file=sys.stderr)
        _append_worklog(task_dir, f"triage: error ({exc}); falling back to full pipeline")
        return None

    verdict = tri.to_state(mode)
    _update_state(task_dir, triage=verdict)
    _persist_triage(task_dir, verdict)
    _write_triage_report(task_dir, tri, mode)

    acting = _triage_acting(mode, tri.tier)
    if acting:
        routed = _triage.stages_for_tier(tri.tier, _pipeline_stages_for_agent())
        if "ba" not in routed:
            _write_lite_brd(task_dir, spec)

    _append_worklog(
        task_dir,
        f"triage [{mode}]: tier={tri.tier} ({tri.source}, conf={tri.confidence:.2f}) "
        f"dims={tri.dimensions} caps={tri.caps} "
        f"{'ACTING' if acting else 'observing (full pipeline)'}; "
        f"{'; '.join(tri.reasons)}",
    )
    _append_history(task_dir, "triage",
                    f"tier={tri.tier} mode={mode} acting={acting}")
    budget = (f" — budget ≈{tri.caps['token_cap'] // 1000}k tok / "
              f"{tri.caps['iteration_cap']} iter") if acting else ""
    # Diagnostic detail (issue #19): the task-start default-notify slot is
    # "pipeline started" (with the actual planned Stages: list) in
    # run_pipeline; this sizing detail is logged and mirrored under
    # NOTIFY_VERBOSE=1 rather than adding a second always-on message.
    if should_notify("triage_classified", verbose=verbose_enabled()):
        _send_telegram(
            f"[{task_id}] TRIAGE [{mode}]: tier {tri.tier} "
            f"(size={tri.dimensions['size']}, risk={tri.dimensions['risk']}, "
            f"conf={tri.confidence:.2f}) — "
            + ("acting — sized pipeline" if acting else "observing — full pipeline")
            + budget
        )
    return tri


def _maybe_upgrade_tier(task_dir: Path, state: dict, iteration_cap: int,
                        trigger: str):
    """RFC §Q5 upgrade ladder — underestimation recovery. When an acted-on S/M
    task struggles (e.g. hits its iteration cap without approval), bump it one
    tier (S→M→L), RAISING the token + iteration caps (never lowering). The new
    token_cap is written into state.triage.caps, which the per-stage governor
    reads. Returns (new_iteration_cap, upgraded). No-op unless triage is acting
    and the current tier can still step up."""
    mode = _triage_mode()
    triage = state.get("triage") or {}
    tier = triage.get("tier")
    if not _triage_acting(mode, tier):
        return iteration_cap, False
    nxt = _triage.next_tier(tier) if tier else None
    if not nxt:
        return iteration_cap, False
    new_caps = _triage.caps_for_tier(nxt)
    old_tok = int((triage.get("caps") or {}).get("token_cap") or 0)
    new_caps["token_cap"] = max(old_tok, int(new_caps["token_cap"]))
    new_iter = max(iteration_cap, int(new_caps["iteration_cap"]))
    new_caps["iteration_cap"] = new_iter
    triage = {
        **triage, "tier": nxt, "caps": new_caps,
        "reasons": (triage.get("reasons") or []) + [f"upgraded {tier}->{nxt}: {trigger}"],
    }
    _update_state(task_dir, triage=triage)
    state["triage"] = triage
    _append_worklog(
        task_dir,
        f"triage upgrade {tier}->{nxt} ({trigger}); caps raised to "
        f"token={new_caps['token_cap']} iter={new_iter}",
    )
    _append_history(task_dir, "triage", f"upgrade {tier}->{nxt} ({trigger})")
    if should_notify("triage_upgraded", verbose=verbose_enabled()):
        _send_telegram(
            f"[{task_dir.name}] TRIAGE upgrade {tier}→{nxt} — {trigger}; "
            f"caps raised to ≈{new_caps['token_cap'] // 1000}k tok / {new_iter} iters"
        )
    return new_iter, True


def _reviewer_triage_hint(state: dict) -> str:
    """Inject a tier-appropriate 'what NOT to flag' clause into the reviewer
    prompt when triage is ACTING. S = aggressive 'trivial, what NOT to flag';
    M = reserve Critical for genuine merge-blockers (test-quality / DRY / polish
    → Warning/Suggestion) — added 2026-06-02 after an M run looped [2,2] because
    the reviewer elevated test-DRY to Critical and never converged. Empty for L,
    shadow, or triage-off — so those review exactly as before."""
    tier = (state.get("triage") or {}).get("tier")
    if not _triage_acting(_triage_mode(), tier):
        return ""
    if tier == "S":
        return _REVIEWER_S_HINT
    if tier == "M":
        return _REVIEWER_M_HINT
    return ""


def _cap_stop(task_dir: Path, task_id: str, stage: str, msg: str, *,
              stop_reason: str, cost_usd: float, cost_cap: float,
              park: bool = True) -> int:
    """The ONE branch every mid-pipeline cap stop goes through (T08).

    A cap stop is a designed operator gate, not a crash: caps are tuned
    empirically and WILL fire. The post-pipeline path has parked them in
    ``awaiting-input/`` with [Продолжить]/[Удалить] since 2026-06-07, but the
    mid-pipeline checks (token cap, per-stage cost cap, the pair check after the
    parallel stages) kept writing ``stage="failed"`` + a FAILED line, so both L
    tasks that hit their cap on developer (2026-08-17) landed in ``failed/`` and
    needed hand surgery on state.json to revive. The 2026-06-02 committee note
    that made rc=5 a hard ``failed/`` is preserved where it applies — the
    graceful handoff still must not reclassify a budget abort — but the bucket
    now comes from ``budget_gate.park`` instead, per the 2026-06-07 operator gate
    (and the original Q2 decision of 2026-05-24).

    Breadcrumbs are written BEFORE the park: ``park`` moves the task dir out of
    ``active/``, so anything appended afterwards would land in a vanished path.
    The task's worktree is deliberately LEFT alive — unlike a post-pipeline park
    (branch pushed, PR open), a mid-pipeline stop can sit on uncommitted stage
    work, and ``_ensure_task_worktree`` reuses a live checkout on resume.

    ``park=False`` records the stop but leaves the move to the caller — used by
    the two stages of the parallel pair, which must not move the task dir out
    from under each other (see ``_run_two_stages_parallel``).

    Returns 5, still one of ``_terminate_pipeline``'s short-circuits — now for
    the same reason as ``_CLARIFY_PAUSE_RC``: the task has ALREADY self-moved.
    The park's ``budget_stop`` notification REPLACES the FAILED message, so a cap
    stop still emits exactly one terminal message (issue #19).
    """
    print(f"[agent-pipeline] {msg}; "
          f"{'parking for operator decision' if park else 'deferring park to the pair check'}",
          file=sys.stderr)
    _append_worklog(task_dir, f"BUDGET STOP: {msg}")
    _append_history(task_dir, stage, msg)
    if park:
        _park_budget_stop(task_dir, task_id, stop_reason=stop_reason,
                          cost_usd=cost_usd, cost_cap=cost_cap)
    return 5


def _token_cap_exceeded(task_dir: Path, state: dict, stage: str,
                        task_id: str, *, cost_usd: float = 0.0,
                        cost_cap: float = 0.0, park: bool = True) -> bool:
    """Accumulate this stage's tokens into state.tokens_used and, when triage is
    acting with a token_cap, abort the run if the running total exceeds it. The
    cap is read from state.triage.caps (written by triage + the upgrade ladder).
    Returns True when the run was aborted — the task is parked for the operator
    (``_cap_stop``), not failed. Tokens are tracked even when triage is
    off/shadow (useful baseline data), but only ENFORCED when acting."""
    tokens_used = _add_tokens_used(task_dir, _read_stage_tokens(task_dir, stage))
    state["tokens_used"] = tokens_used
    sp = task_dir / "state.json"
    triage = state.get("triage") or {}
    if sp.exists():
        try:
            triage = (json.loads(sp.read_text()).get("triage") or {})
        except Exception:
            pass
    if not _triage_acting(_triage_mode(), triage.get("tier")):
        return False
    token_cap = (triage.get("caps") or {}).get("token_cap")
    if not token_cap or tokens_used <= int(token_cap):
        return False
    msg = f"token cap hit: used={tokens_used} > cap={int(token_cap)} tok"
    _cap_stop(task_dir, task_id, stage, msg, stop_reason="token_cap",
              cost_usd=cost_usd, cost_cap=cost_cap, park=park)
    return True


def _pipeline_stages_for_agent(state: dict | None = None) -> list[str]:
    """Compose the pipeline stage list, applying feature flags:
      - PATTERN_DETECTION_ENABLED  inserts "pattern-detector" between "ba" and
        "architect". DEFAULT ON (constraint-inheritance feeds the Architect,
        compass §5); set PATTERN_DETECTION_ENABLED=0 to opt out.
      - TASKS_STAGE_ENABLED=1      inserts "tasks" between "architect" and
        "developer" (Spec-Kit /tasks -> 02b-tasks.md the Developer executes).
        OPT-IN (new stage, default off until validated end-to-end).
      - ANALYZE_STAGE_ENABLED=1    inserts "analyze" after "tasks" (Spec-Kit
        /analyze cross-artifact consistency gate -> 02c-analyze.md). Requires
        the tasks stage (it analyzes spec<->arch<->tasks). OPT-IN, default off.
      - EDGE_CASES_STAGE_ENABLED=1 inserts "edge-cases" right before "developer"
        (BMAD Edge Case Hunter -> 02d-edgecases.md the Developer guards against).
        OPT-IN, default off.
      - DISCOVERY_ENABLED=1        prepends "discovery" before "ba" (opt-in).
    """
    stages = list(_PIPELINE_STAGES_BASE)
    if os.environ.get("PATTERN_DETECTION_ENABLED", "1").strip() != "0":
        ba_idx = stages.index("ba")
        stages.insert(ba_idx + 1, "pattern-detector")
    if os.environ.get("TASKS_STAGE_ENABLED", "").strip() == "1":
        arch_idx = stages.index("architect")
        stages.insert(arch_idx + 1, "tasks")
    if os.environ.get("ANALYZE_STAGE_ENABLED", "").strip() == "1" and "tasks" in stages:
        tasks_idx = stages.index("tasks")
        stages.insert(tasks_idx + 1, "analyze")
    if os.environ.get("EDGE_CASES_STAGE_ENABLED", "").strip() == "1":
        dev_idx = stages.index("developer")
        stages.insert(dev_idx, "edge-cases")
    if os.environ.get("DISCOVERY_ENABLED", "").strip() == "1":
        stages = ["discovery"] + stages

    # ── Adaptive triage routing (committee RFC) ──
    # The flag-composed list above is the FULL menu. When triage is acting on
    # this task, narrow it to the tier subset (review/test/security/developer
    # never drop — only redundant upstream reasoning stages do). With no state,
    # triage off/shadow, or no/invalid classification, return the full menu —
    # byte-identical to pre-triage behaviour (backward-compat + fail-safe).
    full = stages
    mode = _triage_mode()
    if state is None or mode in ("off", "shadow"):
        return full
    tier = (state.get("triage") or {}).get("tier")
    if tier not in ("S", "M", "L") or not _triage_acting(mode, tier):
        return full
    return _triage.stages_for_tier(tier, full)


def _canonicalize_stage_artifact(task_dir: Path, stage: str) -> None:
    """Mirror a stage's "-agent" artifact (+ its .json sibling) to the
    canonical unsuffixed name so downstream guards and prompts find it.
    No-op when the names already match (e.g. discovery) or the source is
    absent."""
    src_name = STAGE_ARTIFACT_MAP.get(stage)
    dst_name = STAGE_CANONICAL_ARTIFACT.get(stage)
    if not src_name or not dst_name or src_name == dst_name:
        return
    for src_n, dst_n in (
        (src_name, dst_name),
        (src_name[:-3] + ".json", dst_name[:-3] + ".json"),
    ):
        src = task_dir / src_n
        dst = task_dir / dst_n
        if src.exists():
            try:
                shutil.copyfile(src, dst)
            except OSError as exc:
                print(f"warn: could not mirror {src_n} -> {dst_n}: {exc}",
                      file=sys.stderr)


def _mirror_to_specs_folder(task_dir: Path, stage: str) -> None:
    """WS-4b alias-staging (Фаза 1): additively mirror a stage's canonical
    artifact (+ its .json sibling) into `task_dir/specs/<spec-kit-name>` when
    SPECS_FOLDER_MIRROR_ENABLED=1.

    Purely additive — the flat canonical names stay primary and authoritative;
    this only makes the Spec-Kit folder layout appear on disk for forward
    tooling. Default OFF. No-op for stages without a documented Spec-Kit
    filename (only ba/architect/tasks → spec/plan/tasks.md). Idempotent.
    See STATE/WS-4b-IMPACT-2026-05-29.md."""
    if os.environ.get("SPECS_FOLDER_MIRROR_ENABLED", "").strip() != "1":
        return
    dst_name = SPECS_FOLDER_SEMANTIC.get(stage)
    src_name = STAGE_CANONICAL_ARTIFACT.get(stage)
    if not dst_name or not src_name:
        return
    specs_dir = task_dir / "specs"
    for src_n, dst_n in (
        (src_name, dst_name),
        (src_name[:-3] + ".json", dst_name[:-3] + ".json"),
    ):
        src = task_dir / src_n
        if not src.exists():
            continue
        try:
            specs_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, specs_dir / dst_n)
        except OSError as exc:
            print(f"warn: could not mirror {src_n} -> specs/{dst_n}: {exc}",
                  file=sys.stderr)


# ── Rate-limit detection + cross-provider fallback ──────────────────────────
# A stage that fails because its backend hit a session / rate limit (HTTP 429,
# Anthropic's "five_hour" window) is NOT a code crash: the SAME backend keeps
# refusing until the window resets, so the legacy cheap→anthropic retry is a
# pure no-op when the stage was ALREADY on anthropic — the failure mode that
# crashed three parallel anthropic dev tasks at once (2026-06-05). Detect it and
# retry on a DIFFERENT provider whose quota is independent (DeepSeek / GLM via
# the LiteLLM-compatible proxy). Opt out with RATE_LIMIT_CROSS_PROVIDER_FALLBACK=0.
_RATE_LIMIT_SIGNATURES = (
    '"api_error_status":429',
    '"api_error_status": 429',
    '"ratelimittype":"five_hour"',
    '"error":"rate_limit"',
    '"error": "rate_limit"',
    "you've hit your session limit",
    "hit your usage limit",
)


def _stage_hit_rate_limit(task_dir: Path, stage: str) -> bool:
    """True when the stage's claude-error log carries a 429 / session-limit
    signature — a quota stall, not a crash. Best-effort (missing log → False)."""
    log = task_dir / f"{stage}.claude-error.log"
    try:
        blob = log.read_text(errors="replace").lower()
    except OSError:
        return False
    return any(sig in blob for sig in _RATE_LIMIT_SIGNATURES)


def _backend_key_available(backend: str) -> bool:
    """Whether `backend` can actually run (its API key is configured). anthropic
    is the claude-CLI default and is always considered available."""
    if backend == "anthropic":
        return True
    if backend == "deepseek":
        return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
    if backend == "glm":
        return bool(os.getenv("GLM_API_KEY", "").strip())
    return False


def _rate_limit_fallback_chain(used_backend: str) -> "list[str]":
    """Ordered alternate backends with INDEPENDENT quota to try when
    `used_backend` is rate-limited. Skips the rate-limited backend itself and any
    whose key is unset; preserves SUPPORTED_BACKENDS order."""
    return [b for b in SUPPORTED_BACKENDS
            if b != used_backend and _backend_key_available(b)]


def _park_limit_stall(task_dir: Path, task_id: str, stage: str,
                      cumulative_cost: float, elapsed: float) -> None:
    """Disposition for RC_LIMIT_STALL (#11): park with a resume_at instead of
    failing. The reset time is parsed from the stage error log when the stream
    advertised one ('resets at <t>' / retry-after / *-reset header), else the
    flat LIMIT_BACKOFF_MIN backoff applies.

    The task's worktree is deliberately KEPT (unlike _handoff_terminal): this is
    a pause, not a terminal stop, and task_dispatcher carries the worktree record
    across re-ingest so the resumed run re-attaches to the same branch."""
    # #18 — NOTHING may still be writing into task_dir when park() moves it. The
    # stalled stage's own child is already dead (_run_claude_stage kills and
    # reaps it before returning RC_LIMIT_STALL); this covers the parallel sibling.
    _reap_children_before_move(task_id, "parking the task")
    hint = _limit_stall_text(task_dir, stage)
    resume_at = _limit_stall.resume_at_for(hint)
    reason = f"limits exhausted during stage {stage} after {elapsed:.0f}s"
    try:
        _append_history(task_dir, stage, f"limit stall — {reason}")
    except Exception:  # noqa: BLE001
        pass
    _limit_stall.park(task_dir, task_id, stage=stage, reason=reason,
                      resume_at=resume_at, hint=hint, cost_usd=cumulative_cost)


def _run_pipeline_stage_with_breadcrumbs(
    task_dir: Path,
    target_repo: Path,
    stage: str,
    state: dict,
    task_id: str,
    cumulative_cost: float,
    cost_cap: float,
    state_path: Path,
    *,
    defer_cap_park: bool = False,
) -> tuple[int, float, dict]:
    """Run one stage with full breadcrumb wiring. Returns (rc, new_cumulative_cost, refreshed_state).

    On rc != 0 or cost-cap hit, caller is responsible for returning rc from
    run_pipeline. This helper does NOT itself call sys.exit / return early —
    it just runs the stage and reports back.

    ``defer_cap_park`` keeps a cap stop from parking the task here (breadcrumbs
    only, still rc=5): the two stages of the parallel pair run concurrently, so
    whichever hit the cap first would move the task dir out from under the other
    one and send a second terminal message. Their shared park happens once, in
    ``_run_two_stages_parallel``, after both futures have joined.
    """
    # BRD is required for stages after BA; skip the precheck for BA itself
    # since BA is the producer of 01-ba.md.
    if stage != "ba" and stage != "discovery" and not (task_dir / "01-ba.md").exists():
        msg = f"01-ba.md missing before stage {stage}"
        print(f"error: {task_dir}/{msg}", file=sys.stderr)
        _append_worklog(task_dir, f"FAILED: {msg}")
        _append_history(task_dir, stage, f"aborted — {msg}")
        _update_state(task_dir, stage="failed")
        # No _send_telegram here: rc=2 always reaches _terminate_pipeline ->
        # _handoff_terminal below, which sends the ONE terminal-failure
        # message (issue #19) — a second one here would just duplicate it.
        return 2, cumulative_cost, state

    # #6 — the implementation stages run in the task's own ephemeral worktree,
    # never in the caller's live checkout. Single funnel: every stage (including
    # the parallel tester+security pair) is dispatched from here.
    repo = _stage_repo(task_dir, target_repo, stage, state, task_id)
    if repo is None:
        msg = (f"could not create an isolated worktree of {target_repo} for "
               f"stage {stage} — refusing to run in the live checkout")
        print(f"error: {msg}", file=sys.stderr)
        _append_worklog(task_dir, f"FAILED: {msg}")
        _append_history(task_dir, stage, f"aborted — {msg}")
        _update_state(task_dir, stage="failed")
        # Same rationale as the BA-missing guard above: rc=2 -> handoff sends
        # the one terminal message.
        return 2, cumulative_cost, state

    print(f"[agent-pipeline] stage={stage} starting")
    _append_history(task_dir, stage, f"stage {stage} started (agent-path)")
    _update_state(task_dir, stage=stage)
    if should_notify("stage_started", verbose=verbose_enabled()):
        _send_telegram(f"[{task_id}] {stage.upper()} started (agent-path)")
    t0 = time.time()
    rc = _execute_single_stage(task_dir, repo, stage, state)
    elapsed = time.time() - t0

    stage_cost = _read_stage_cost_usd(task_dir, stage)
    cumulative_cost += stage_cost
    _update_state(task_dir, cost_usd=cumulative_cost)

    # #11 — limits exhausted mid-stage: PARK, don't fail and don't retry. The
    # same window blocks every anthropic backend, so a fallback pass would only
    # burn another stage. The watcher requeues the task once resume_at passes and
    # the artifact-based resume skips the stages that already completed.
    if rc == RC_LIMIT_STALL:
        _park_limit_stall(task_dir, task_id, stage, cumulative_cost, elapsed)
        return rc, cumulative_cost, state

    # Auto-fallback when a stage fails (NOT a wall-clock timeout, which a retry
    # would only burn a second full window on — RC_STAGE_TIMEOUT is excluded).
    # Two distinct failure modes get two distinct recoveries:
    #   • backend out of quota (429 / "five_hour" session limit): the SAME
    #     backend keeps refusing until the window resets, so retrying it — or,
    #     when it was already anthropic, "retrying on anthropic" — is a no-op.
    #     Retry on a DIFFERENT provider with independent quota (DeepSeek/GLM).
    #     This is what would have saved the three parallel anthropic tasks that
    #     all hit the 5-hour limit at once (2026-06-05).
    #   • a one-off hiccup on a CHEAP backend: the legacy behaviour — ONE retry
    #     on anthropic.
    rate_limited = False
    if rc != 0 and rc != RC_STAGE_TIMEOUT:
        used_backend, _ = _resolve_stage_backend(
            stage, int(state.get("iteration") or 1),
            state.get("model_routing") or {},
            (_load_persisted_triage(task_dir) or {}).get("tier") or state.get("tier"),
        )
        rate_limited = _stage_hit_rate_limit(task_dir, stage)
        cross_provider = (
            os.getenv("RATE_LIMIT_CROSS_PROVIDER_FALLBACK", "1").strip() == "1"
        )

        if rate_limited and cross_provider:
            for alt in _rate_limit_fallback_chain(used_backend):
                print(f"[agent-pipeline] stage={stage} rc={rc}: {used_backend} "
                      f"rate-limited — retrying on {alt} (independent quota)",
                      file=sys.stderr)
                _append_history(
                    task_dir, stage,
                    f"rate-limited on {used_backend} — retrying on {alt}")
                if should_notify("stage_retry", verbose=verbose_enabled()):
                    _send_telegram(
                        f"[{task_id}] {stage.upper()} rate-limited on {used_backend} "
                        f"— auto-retrying on {alt}")
                rc = _execute_single_stage(
                    task_dir, repo, stage, state, backend_override=alt)
                elapsed = time.time() - t0
                cumulative_cost += _read_stage_cost_usd(task_dir, stage)
                _update_state(task_dir, cost_usd=cumulative_cost)
                if rc == 0:
                    _append_history(task_dir, stage, f"recovered on {alt} fallback")
                    if should_notify("stage_recovered", verbose=verbose_enabled()):
                        _send_telegram(f"[{task_id}] {stage.upper()} recovered on {alt}")
                    rate_limited = False
                    break
                # still failing — did THIS provider also rate-limit?
                rate_limited = _stage_hit_rate_limit(task_dir, stage)

        elif used_backend != "anthropic":
            print(f"[agent-pipeline] stage={stage} rc={rc} on {used_backend} — "
                  f"retrying on anthropic", file=sys.stderr)
            _append_history(
                task_dir, stage,
                f"failed rc={rc} on {used_backend} — retrying on anthropic",
            )
            if should_notify("stage_retry", verbose=verbose_enabled()):
                _send_telegram(
                    f"[{task_id}] {stage.upper()} failed on {used_backend} (rc={rc}) "
                    f"— auto-retrying on anthropic"
                )
            rc = _execute_single_stage(
                task_dir, repo, stage, state, backend_override="anthropic",
            )
            elapsed = time.time() - t0
            cumulative_cost += _read_stage_cost_usd(task_dir, stage)
            _update_state(task_dir, cost_usd=cumulative_cost)
            if rc == 0:
                _append_history(task_dir, stage, "recovered on anthropic fallback")
                if should_notify("stage_recovered", verbose=verbose_enabled()):
                    _send_telegram(f"[{task_id}] {stage.upper()} recovered on anthropic")

    if rc != 0:
        # A quota stall that outlived the cross-provider fallback is reported
        # honestly (re-queue after the window resets), not as a code crash.
        if rate_limited:
            rc = RC_RATE_LIMITED
        msg = _HANDOFF_REASONS.get(rc, f"stage {stage} failed rc={rc}")
        print(f"[agent-pipeline] stage {stage}: {msg}; aborting", file=sys.stderr)
        _append_worklog(task_dir, f"FAILED: stage {stage} — {msg} (rc={rc}) "
                                  f"elapsed={elapsed:.0f}s")
        _append_history(task_dir, stage, f"failed rc={rc} elapsed={elapsed:.0f}s")
        _update_state(task_dir, stage="failed")
        # No _send_telegram here: every rc reaching this branch (crash,
        # timeout, exhausted rate-limit fallback) is NOT in
        # _terminate_pipeline's short-circuit list, so it always reaches
        # _handoff_terminal next, which sends the one terminal-failure
        # message (issue #19).
        return rc, cumulative_cost, state

    # Mirror "-agent" artifact to the canonical name so the next stage's
    # guard + prompt (which read 01-ba.md / 02-architecture.md / 03-dev.md)
    # find it in a pure agent-path run.
    _canonicalize_stage_artifact(task_dir, stage)
    _mirror_to_specs_folder(task_dir, stage)

    _append_history(
        task_dir, stage,
        f"completed rc={rc} elapsed={elapsed:.0f}s cost=${stage_cost:.4f} (agent-path)",
    )
    if should_notify("stage_done", verbose=verbose_enabled()):
        _send_telegram(
            f"[{task_id}] {stage.upper()} done ({elapsed:.0f}s, "
            f"+${stage_cost:.4f}, total ${cumulative_cost:.4f})"
        )

    # Token-cap guard — the subscription-relevant budget governor. Accumulate
    # this stage's tokens into state.tokens_used; when triage is acting with a
    # token_cap, abort if exceeded. This is the cap that actually binds on a
    # flat-plan host (the dollar cap below is a loose, notional safety net).
    if _token_cap_exceeded(task_dir, state, stage, task_id,
                           cost_usd=cumulative_cost, cost_cap=cost_cap,
                           park=not defer_cap_park):
        return 5, cumulative_cost, state

    # Cost cap guard — Phase C Step 2a safety (notional $ under a subscription).
    if cumulative_cost > cost_cap:
        msg = (f"cost cap hit: cumulative=${cumulative_cost:.4f} "
               f"> cap=${cost_cap:.2f}")
        return (_cap_stop(task_dir, task_id, stage, msg, stop_reason="cost_cap",
                          cost_usd=cumulative_cost, cost_cap=cost_cap,
                          park=not defer_cap_park),
                cumulative_cost, state)

    # C.3 — INVEST validation of the BA artifact when enabled. Blocks the
    # pipeline on violations unless INVEST_BLOCKING=0 (compass / ROADMAP C.3).
    if stage == "ba":
        invest_rc = _maybe_run_invest_validation(task_dir, task_id)
        if invest_rc:
            msg = "BA artifact failed INVEST validation (see 01-ba-invest.md)"
            _append_worklog(task_dir, f"FAILED: {msg}")
            _append_history(task_dir, stage, f"blocked — {msg}")
            _update_state(task_dir, stage="failed")
            # No _send_telegram: rc=2 -> _handoff_terminal sends the one
            # terminal message (the violation detail already went out, if
            # NOTIFY_VERBOSE=1, from _maybe_run_invest_validation above).
            return 2, cumulative_cost, state

    # Steal-list §2.6/#10 — deterministic structural lint of the architecture
    # doc when enabled. ALWAYS report-only (never blocks); runs right after
    # Architect so the artifact is already lint-annotated before the
    # 'analyze' stage's LLM pass reads it.
    if stage == "architect":
        _maybe_run_architecture_lint(task_dir, task_id)

    # C.2 — pause for operator clarifications after BA when [NEEDS CLARIFICATION]
    # markers remain in the artifact and CLARIFY_INTERACTIVE_ENABLED=1.
    if stage == "ba":
        pause_rc = _maybe_pause_for_clarifications(
            task_dir, task_id, cumulative_cost,
        )
        if pause_rc:
            # No _send_telegram here: _maybe_pause_for_clarifications already
            # sent the ONE clarify notification (the actual questions, via
            # _notify_bot("clarify_needed", ...) -> bot.py's rich prompt) —
            # this used to duplicate it with a content-free "PAUSED" line.
            return pause_rc, cumulative_cost, state

    # WS-3b — BA quality gate: the Spec-Kit Specification Quality Checklist is
    # a HARD GATE before Architect (compass §1), not a self-report. Default on;
    # set BA_QUALITY_GATE_ENABLED=0 to opt out. NOTE: with both
    # CLARIFY_INTERACTIVE_ENABLED and INVEST_VALIDATION_ENABLED off (defaults),
    # this gate is the sole enforcer of "no unresolved [NEEDS CLARIFICATION:]".
    if stage == "ba" and os.environ.get(
        "BA_QUALITY_GATE_ENABLED", "1"
    ).strip() != "0":
        gate_problems = _check_ba_quality_gate(task_dir)
        if gate_problems:
            msg = "BA quality gate failed: " + "; ".join(gate_problems)
            _append_worklog(task_dir, f"FAILED: {msg}")
            _append_history(task_dir, stage, f"blocked — {msg}")
            _update_state(task_dir, stage="failed")
            # No _send_telegram: rc=2 -> _handoff_terminal (issue #19).
            return 2, cumulative_cost, state

    # WS-5 — analyze gate: block before the Developer when the cross-artifact
    # consistency pass found CRITICAL findings (Spec-Kit /analyze: "resolve
    # CRITICAL before implement"). REPORT-ONLY by default — the stage always
    # writes 02c-analyze.md; set ANALYZE_GATE_BLOCKING=1 to make CRITICAL
    # findings hard-fail the task once the stage is validated end-to-end
    # (new gate ships report-only first, like the stages themselves).
    if stage == "analyze" and os.environ.get(
        "ANALYZE_GATE_BLOCKING", ""
    ).strip() == "1":
        crit = _analyze_critical_count(task_dir)
        if crit:
            msg = (f"analyze gate: {crit} CRITICAL cross-artifact finding(s) "
                   f"(see 02c-analyze.md)")
            _append_worklog(task_dir, f"FAILED: {msg}")
            _append_history(task_dir, stage, f"blocked — {msg}")
            _update_state(task_dir, stage="failed")
            # No _send_telegram: rc=2 -> _handoff_terminal (issue #19).
            return 2, cumulative_cost, state

    # Refresh state for next stage so it sees any updates the previous
    # stage's artifact may have touched (e.g. Developer setting pr_url).
    if state_path.exists():
        state = json.loads(state_path.read_text())

    return 0, cumulative_cost, state


def _run_two_stages_parallel(
    task_dir: Path,
    target_repo: Path,
    stage_a: str,
    stage_b: str,
    state: dict,
    task_id: str,
    cumulative_cost_in: float,
    cost_cap: float,
    state_path: Path,
) -> tuple[int, float, dict, int, float, dict]:
    """Run stage_a and stage_b concurrently via ThreadPoolExecutor.

    Each stage receives cumulative_cost_in (the snapshot before either ran)
    so both independently add their own cost on top. After both finish, the
    true combined cumulative is computed and written back to state.json so
    the next stage sees the correct total.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(
            _run_pipeline_stage_with_breadcrumbs,
            task_dir, target_repo, stage_a, state, task_id,
            cumulative_cost_in, cost_cap, state_path, defer_cap_park=True,
        )
        future_b = executor.submit(
            _run_pipeline_stage_with_breadcrumbs,
            task_dir, target_repo, stage_b, state, task_id,
            cumulative_cost_in, cost_cap, state_path, defer_cap_park=True,
        )
        rc_a, cost_after_a, _state_a = future_a.result()
        rc_b, cost_after_b, _state_b = future_b.result()

    combined_cumulative = (
        cumulative_cost_in
        + (cost_after_a - cumulative_cost_in)
        + (cost_after_b - cumulative_cost_in)
    )
    _update_state(task_dir, cost_usd=combined_cumulative)

    # The pair's single cap stop: either child deferred one to us (rc=5), or the
    # combined total tips the dollar cap now that both stages are accounted for.
    # One park, one budget_stop notification — the old code could park/announce
    # twice for the same stop (a child's FAILED line plus this one).
    over_cost = combined_cumulative > cost_cap
    if over_cost or 5 in (rc_a, rc_b):
        msg = ((f"cost cap hit: cumulative=${combined_cumulative:.4f} "
                f"> cap=${cost_cap:.2f}") if over_cost else
               f"token cap hit during {stage_a}+{stage_b} (see worklog)")
        rc = _cap_stop(task_dir, task_id, f"{stage_a}+{stage_b}", msg,
                       stop_reason="cost_cap" if over_cost else "token_cap",
                       cost_usd=combined_cumulative, cost_cap=cost_cap)
        return rc, combined_cumulative, state, rc, combined_cumulative, state

    if state_path.exists():
        state = json.loads(state_path.read_text())

    return rc_a, combined_cumulative, state, rc_b, combined_cumulative, state


def _stage_artifact_ready(task_dir: Path, stage: str) -> bool:
    """True if a stage already produced a valid (non-trivial) artifact under
    either its -agent or canonical name. Lets a respawned runner SKIP completed
    stages instead of re-running discovery+ba on every crash recovery —
    mirrors the watcher's _artifact_is_valid heuristic (size > 50 bytes)."""
    for name in (STAGE_ARTIFACT_MAP.get(stage), STAGE_CANONICAL_ARTIFACT.get(stage)):
        if not name:
            continue
        p = task_dir / name
        try:
            if p.is_file() and p.stat().st_size > 50:
                return True
        except OSError:
            pass
    return False


def _ba_needs_clarify_rerun(task_dir: Path, stage: str) -> bool:
    """True when `stage` is BA, the operator has answered clarifications
    (clarifications.md present), but the BA artifact STILL carries unresolved
    [NEEDS CLARIFICATION] markers — so BA must re-run to fold the answers in.

    Without this, the resume-artifact check (_stage_artifact_ready) would SKIP
    BA on a clarify re-ingest (01-ba-agent.md already exists from the first pass)
    and the operator's answers would be silently ignored — the BRD would reach
    the developer with its markers unresolved (observed 2026-06-02). Symmetric
    with _maybe_pause_for_clarifications: once BA re-runs and resolves the
    markers, this returns False and future resumes skip BA normally (so no
    re-run loop)."""
    if stage != "ba":
        return False
    if not (task_dir / "clarifications.md").exists():
        return False
    for name in ("01-ba-agent.md", "01-ba.md"):
        p = task_dir / name
        try:
            if p.is_file() and _clarify.extract_pending_markers(p.read_text()):
                return True
        except OSError:
            pass
    return False


def _task_is_resuming(task_dir: Path) -> bool:
    """True when this run_pipeline invocation is continuing prior work — a
    triage verdict was already persisted, or some stage already produced an
    artifact — rather than the task's first-ever pass.

    MUST be evaluated before _maybe_run_triage / any stage executes (they are
    what CREATE those artifacts) — otherwise a brand-new task's own first
    triage classification or first stage would immediately read back as "a
    resume". Drives the once-per-task notify gate (issue #19): a limit-park
    requeue, a clarify round-trip, or a watcher respawn must not repeat the
    "pipeline started" announcement — they get the "auto_resumed" one instead.
    """
    if _load_persisted_triage(task_dir) is not None:
        return True
    return any(_stage_artifact_ready(task_dir, stage) for stage in STAGE_ARTIFACT_MAP)


def _resume_reason(task_dir: Path, state: dict | None = None) -> str:
    """WHY this run_pipeline invocation is resuming prior work.

    Inferred from task state the runner already has (FR-001, notify-resume-reason):
    a clarifications.md answer block means a clarify round-trip; a limit-park
    marker (``limit_stall`` / ``limit_parks`` in state.json) means a limit resume;
    anything else is a watcher respawn after a runner crash. Ordered so the most
    specific signal wins when a task carries more than one.
    """
    st = state or {}
    if (task_dir / "clarifications.md").exists():
        return "clarify answers"
    if st.get("limit_stall") or st.get("limit_parks"):
        return "limit park"
    return "watcher respawn"


def _resumed_notification(task_id: str, stages: list, task_dir: Path,
                          state: dict | None = None) -> str:
    """The auto_resumed Telegram body — names WHY the task resumed and WHERE it
    continues from, so two different resumes (limit park vs clarify round-trip)
    no longer read as a duplicate send (FR-001). Uses the same done/continuing
    computation the resume skip loop below uses, so the two cannot drift."""
    reason = _resume_reason(task_dir, state)
    done = sum(
        1 for s in stages
        if _stage_artifact_ready(task_dir, s) and not _ba_needs_clarify_rerun(task_dir, s)
    )
    continuing = next(
        (s for s in stages
         if not (_stage_artifact_ready(task_dir, s)
                 and not _ba_needs_clarify_rerun(task_dir, s))),
        None,
    )
    if continuing is None:
        return (f"[{task_id}] resumed after {reason} — "
                f"{done}/{len(stages)} stage(s) already done")
    return (f"[{task_id}] resumed after {reason} — continuing from "
            f"{continuing} ({done}/{len(stages)} done)")


def run_pipeline(task_dir: Path) -> int:
    """Multi-stage pipeline driver — Phase C Step 2c (hotfix loop).

    Runs each stage sequentially via _execute_single_stage. After the initial
    linear pass through [discovery?, ba, architect, developer, tester,
    security, reviewer], calls _decide_post_pipeline_stage to check the
    reviewer verdict. If the verdict is REQUEST_CHANGES with room below
    iteration_cap, enters a hotfix loop re-running [developer-hotfix,
    tester, security, reviewer]. The loop repeats until the verdict reaches
    approve, iteration_cap is exhausted, or the cost cap is hit.

    Each hotfix iteration increments `iteration` in state.json so
    _decide_post_pipeline_stage can enforce the cap via
    auto_loop.evaluate_stop. The developer-hotfix prompt locks the subagent
    to the same branch and PR as the initial Developer iteration.

    Activated by setting STAGE_RUNNER_MODE=agent in the dispatcher
    environment so task_dispatcher.py spawns this script instead of
    stage_runner.py. See dispatcher/task_dispatcher.py:_STAGE_RUNNER_MODE.
    """
    task_dir = task_dir.expanduser().resolve()
    if not (task_dir / "spec.json").exists():
        print(f"error: {task_dir}/spec.json missing", file=sys.stderr)
        return 2

    # Single-runner guard (FAIL-SAFE): if another runner already holds this
    # task's lock, exit cleanly before doing any (billable) work. Closes the
    # dispatcher/watcher/manual double-spawn race at the runner level, so it
    # holds regardless of which path tried to start us. See _acquire_runner_lock.
    if not _acquire_runner_lock(task_dir):
        print(f"[agent-pipeline] {task_dir.name}: another runner holds the lock "
              f"— exiting (no double-run)", file=sys.stderr)
        return 0

    spec = json.loads((task_dir / "spec.json").read_text())
    target_repo_str = spec.get("target_repo", "")
    if not target_repo_str:
        print(f"error: spec.target_repo missing in {task_dir}/spec.json",
              file=sys.stderr)
        return 2
    target_repo = Path(target_repo_str).expanduser().resolve()
    if not target_repo.is_dir():
        print(f"error: target_repo not a directory: {target_repo}",
              file=sys.stderr)
        return 2

    # Per-target Anthropic model: sandbox → cheaper (Sonnet for ALL stages), real
    # → the two-model per-stage policy (Opus only for ba/architect, see
    # backend_routing._anthropic_model_for_stage). The per-target step only pins
    # sandbox; real targets leave PIPELINE_ANTHROPIC_MODEL unset so the per-stage
    # policy decides. (The old tier→model routing — S/M→Sonnet, L→Opus, process-
    # wide — is superseded by the per-stage split and no longer called.)
    _resolve_anthropic_model_for_target(target_repo)

    cost_cap = float(spec.get("cost_cap_usd") or 20)
    iteration_cap = int(spec.get("iteration_cap") or 3)
    task_id = task_dir.name

    state_path = task_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    # Snapshot BEFORE triage/any stage runs — see _task_is_resuming docstring
    # for why the ordering matters. Drives the once-per-task "pipeline
    # started" vs. "auto_resumed" choice below (issue #19).
    is_resume = _task_is_resuming(task_dir)
    verbose = verbose_enabled()

    # ── Adaptive triage (feed-forward sizing) — runs FIRST, before stage
    # composition (RFC §Q1). Off by default; writes state.triage when enabled.
    triage_obj = _maybe_run_triage(task_dir, target_repo, state, spec, task_id)
    if state_path.exists():
        state = json.loads(state_path.read_text())  # reload — triage wrote it

    stages = _pipeline_stages_for_agent(state)

    # Tier caps override spec defaults ONLY when triage is acting AND the spec
    # used the defaults — an explicit non-default spec value always wins (§Q4).
    # The TOKEN cap (the real budget on a subscription) lives in
    # state.triage.caps and is enforced per-stage by the breadcrumb helpers; the
    # dollar cost_cap stays at the spec default as a loose system-wide safety
    # net (it is notional under a flat plan, so it should not be the governor).
    if triage_obj is not None and _triage_acting(_triage_mode(),
                                                 (state.get("triage") or {}).get("tier")):
        caps = (state.get("triage") or {}).get("caps") or {}
        # `is None` (not `in (None, 3)`): the normal ingestion flow writes the
        # default cap only to state.json, never spec.json — so None alone means
        # "operator did not specify", and an explicit spec value (even 3) wins.
        if spec.get("iteration_cap") is None and caps.get("iteration_cap"):
            iteration_cap = int(caps["iteration_cap"])
        # Keep the in-memory spec in sync so _decide_post_pipeline_stage (which
        # reads spec.iteration_cap) enforces the tier-aware iteration cap.
        spec = {**spec, "iteration_cap": iteration_cap}

    # Base branch — resolved ONCE per run (per-target registry entry ->
    # PIPELINE_BASE_BRANCH -> the target's own origin/HEAD -> 'main') and
    # persisted, so the developer prompt, the worktree cut and the post-run
    # stale-base check all use the same value even if origin/HEAD moves. #6:
    # the old global 'main' default cut a self-targeted run from a stale
    # 'master' while development lived on 'dev'.
    base_branch = _base_branch(target_repo)
    state["base_branch"] = base_branch

    tok_cap = ((state.get("triage") or {}).get("caps") or {}).get("token_cap")
    print(f"[agent-pipeline] stages={stages} task={task_id} "
          f"repo={target_repo.name} base={base_branch} cost_cap=${cost_cap:.2f} "
          f"token_cap={tok_cap or '-'} iteration_cap={iteration_cap}")
    _append_worklog(task_dir, f"agent-pipeline started for {task_id} "
                              f"(stages={','.join(stages)}, base={base_branch}, "
                              f"cap=${cost_cap:.2f}, "
                              f"iteration_cap={iteration_cap})")
    iteration = 1
    _update_state(task_dir, stage=stages[0], iteration=iteration,
                  iteration_cap=iteration_cap, base_branch=base_branch)
    # ONE message for task start (issue #19): "pipeline started" fires only
    # on the task's first-ever pass, rendered from `stages` — the exact same
    # list the "[agent-pipeline] stages=..." log line above just printed, so
    # the two can never drift apart. A later resume (limit-park requeue,
    # clarify round-trip, watcher respawn) gets "auto_resumed" instead, never
    # a second "pipeline started".
    if should_notify("pipeline_started", verbose=verbose, is_resume=is_resume):
        _send_telegram(
            f"[{task_id}] agent-pipeline started\n"
            f"Target: {target_repo.name}\n"
            f"Stages: {render_stage_list(stages)}"
        )
    elif should_notify("auto_resumed", verbose=verbose, is_resume=is_resume):
        _send_telegram(_resumed_notification(task_id, stages, task_dir, state))

    # Carry prior cost forward on resume so the cost cap survives a respawn.
    cumulative_cost = float(state.get("cost_usd") or 0.0)

    # ── Initial linear pass ──
    i = 0
    while i < len(stages):
        s1 = stages[i]
        s2 = stages[i + 1] if i + 1 < len(stages) else None

        # Resume: skip a stage that already produced a valid artifact (watcher
        # respawn / reboot recovery) instead of re-running it. EXCEPTION: BA must
        # re-run after a clarify round-trip — when the operator has answered but
        # the BA artifact still carries unresolved markers, skipping it would
        # silently drop the answers (the BRD would reach developer unresolved).
        if _stage_artifact_ready(task_dir, s1) and not _ba_needs_clarify_rerun(task_dir, s1):
            print(f"[agent-pipeline] resume: skipping completed stage {s1}")
            _append_history(task_dir, s1, "skipped on resume (artifact present)")
            # Skipping developer also skips the write of the branch/PR lock the
            # hotfix gate needs — re-derive it from the artifact instead, or a
            # later hotfix on a resumed task fails fail-closed with rc=5 even
            # though it stayed on the right branch and PR.
            if s1 == "developer" and _restore_dev_lock_from_artifact(task_dir):
                state = json.loads(state_path.read_text())  # reload the lock
            i += 1
            continue

        if s1 == "tester" and s2 == "security":
            rc1, cumulative_cost, state, rc2, _, _ = _run_two_stages_parallel(
                task_dir, target_repo, "tester", "security", state, task_id,
                cumulative_cost, cost_cap, state_path,
            )
            if rc1 != 0:
                return _terminate_pipeline(task_dir, target_repo, task_id, rc1, cumulative_cost)
            if rc2 != 0:
                return _terminate_pipeline(task_dir, target_repo, task_id, rc2, cumulative_cost)
            i += 2
            continue

        rc, cumulative_cost, state = _run_pipeline_stage_with_breadcrumbs(
            task_dir, target_repo, s1, state, task_id,
            cumulative_cost, cost_cap, state_path,
        )
        if rc != 0:
            return _terminate_pipeline(task_dir, target_repo, task_id, rc, cumulative_cost)
        i += 1

    msg = (f"all {len(stages)} stages completed for {task_id} "
           f"(total cost ${cumulative_cost:.4f})")
    print(f"[agent-pipeline] {msg}")
    _append_worklog(task_dir, msg)

    # ── Phase C Step 2b: parse reviewer verdict + evaluate stop ──
    next_stage, stop_reason = _decide_post_pipeline_stage(
        task_dir, cumulative_cost, cost_cap, spec,
    )

    # ── Phase C Step 2c: hotfix iteration loop ──
    # When the reviewer requests changes and we still have room below the
    # cap, re-run [developer-hotfix, tester, security, reviewer]. Each pass
    # increments iteration so _decide_post_pipeline_stage's auto_loop check
    # naturally breaks the loop when the cap is reached.
    #
    # RFC §Q5 upgrade ladder: when triage UNDERESTIMATED (an acted-on S/M task
    # hits its iteration cap without approval), bump the tier one step before
    # giving up — this RAISES the caps and grants more hotfix room, then
    # dovetails with the existing auto-escalation (Dev/Test/Sec → Opus at
    # iteration ≥ 2). Underestimation is the costly error; the ladder makes it
    # cheap to recover from. No-op when triage isn't acting.
    while True:
        if next_stage == "request-changes-pending":
            iteration += 1
            _update_state(task_dir, iteration=iteration, iteration_cap=iteration_cap)
            _append_worklog(task_dir, f"hotfix iteration {iteration}/{iteration_cap} starting")
            if should_notify("hotfix_iteration", verbose=verbose):
                _send_telegram(
                    f"[{task_id}] hotfix iteration {iteration}/{iteration_cap} — "
                    f"addressing reviewer findings"
                )
            _append_history(task_dir, "hotfix", f"iteration {iteration} starting")

            hotfix_stages = ["developer-hotfix", "tester", "security", "reviewer"]
            j = 0
            while j < len(hotfix_stages):
                s1 = hotfix_stages[j]
                s2 = hotfix_stages[j + 1] if j + 1 < len(hotfix_stages) else None

                if s1 == "tester" and s2 == "security":
                    rc1, cumulative_cost, state, rc2, _, _ = _run_two_stages_parallel(
                        task_dir, target_repo, "tester", "security", state, task_id,
                        cumulative_cost, cost_cap, state_path,
                    )
                    if rc1 != 0:
                        return _terminate_pipeline(task_dir, target_repo, task_id, rc1, cumulative_cost)
                    if rc2 != 0:
                        return _terminate_pipeline(task_dir, target_repo, task_id, rc2, cumulative_cost)
                    j += 2
                    continue

                rc, cumulative_cost, state = _run_pipeline_stage_with_breadcrumbs(
                    task_dir, target_repo, s1, state, task_id,
                    cumulative_cost, cost_cap, state_path,
                )
                if rc != 0:
                    return _terminate_pipeline(task_dir, target_repo, task_id, rc, cumulative_cost)
                j += 1

            next_stage, stop_reason = _decide_post_pipeline_stage(
                task_dir, cumulative_cost, cost_cap, spec,
            )
            continue

        # Not pending — try a tier upgrade to rescue an acted-on underestimate
        # before accepting an iteration-cap stop. BUT only when the reviewer
        # found a genuine merge-blocker: a 0-critical (pure nitpick)
        # request_changes must NOT escalate an S task — escalating would re-open
        # the exact trivial-task hotfix loop the S cap prevents (the $16.95
        # pathology). On a 0-critical stop the cap holds and the operator decides.
        if stop_reason == "iteration_cap":
            crit = _reviewer_critical_count(task_dir)
            # Convergence gate (anti-thrash, committee 2026-06-02): only grant
            # the upgrade ladder more rope when criticals are genuinely shrinking
            # ([5,3,1]). A flat/rising trend ([2,2] — the incident) is perpetual-
            # criticals: the M→L bump would just buy another non-converging,
            # timeout-prone iteration, so deny it and let the cap stop the loop
            # (PR2's handoff turns that stop into a draft-PR + findings handoff).
            # Fail-open on a missing trend so the first upgrade is never blocked.
            try:
                _trend = json.loads(state_path.read_text()).get("review_trend") or []
            except Exception:
                _trend = []
            _streak_k = _triage._env_int("STAGNANT_STREAK_CAP", 2)
            if crit > 0 and _critical_is_converging(_trend, _streak_k):
                iteration_cap, upgraded = _maybe_upgrade_tier(
                    task_dir, state, iteration_cap,
                    f"{crit} critical finding(s) remain at iteration cap",
                )
                if upgraded:
                    spec = {**spec, "iteration_cap": iteration_cap}
                    next_stage, stop_reason = _decide_post_pipeline_stage(
                        task_dir, cumulative_cost, cost_cap, spec,
                    )
                    continue
            elif crit > 0:
                # crit>0 but the trend is flat/rising — perpetual-criticals.
                # Deny the ladder (the upgrade would only buy another
                # non-converging, timeout-prone iteration) and let the cap stop.
                _append_worklog(
                    task_dir,
                    f"triage: iteration cap hit with {crit} critical but trend "
                    f"{_trend} not converging — NOT upgrading (perpetual-criticals); "
                    f"cap holds, operator decides",
                )
                _append_history(
                    task_dir, "triage",
                    f"no upgrade — perpetual-criticals (trend {_trend})",
                )
            elif _triage_acting(_triage_mode(), (state.get("triage") or {}).get("tier")):
                _append_worklog(
                    task_dir,
                    "triage: iteration cap hit with 0 critical findings — NOT "
                    "upgrading (nitpick-only request_changes); cap holds, operator decides",
                )
                _append_history(task_dir, "triage", "no upgrade — 0-critical nitpick at cap")
        break

    # ── Final state update (runs once after all passes) ──
    _update_state(task_dir, stage=next_stage)
    # The ephemeral worktree has served its purpose — the branch is pushed and
    # the PR is open, so drop the throwaway checkout (#6). The BRANCH survives
    # `git worktree remove`, so a re-queue / [Продолжить] just re-attaches a
    # fresh worktree to it. Done BEFORE the budget park below, which moves the
    # task dir (and with it state.json) out from under us.
    _remove_task_worktree(task_dir, state)
    _append_worklog(
        task_dir,
        f"agent-pipeline complete — stage={next_stage} "
        f"(stop_reason={stop_reason}, total ${cumulative_cost:.4f})",
    )
    # Typed memory write-back (roadmap #0, WRITE-BACK half). Best-effort by
    # module contract — the pipeline outcome is already decided above and a
    # memory failure must not change it.
    try:
        _spec_wb = json.loads((task_dir / "spec.json").read_text())
    except Exception:
        _spec_wb = {}
    if _memory_inject.write_back(
            task_id=task_id, target_repo=str(_spec_wb.get("target_repo") or ""),
            spec_prompt=_spec_wb.get("prompt") or "",
            state=state, stop_reason=str(stop_reason)):
        _append_worklog(task_dir, "memory: task_lesson written to memory store")
    # Surface a clickable PR link (+ findings) so the operator can act straight
    # from Telegram / the PR — whether the run APPROVED (awaiting-approval) or
    # PARKED for a decision with unresolved criticals (awaiting-input, #7).
    # ONE message per outcome (issue #19) — the generic "pipeline complete"
    # line above is worklog-only; the branches below are each other's
    # alternative (next_stage is always exactly one of the two).
    if next_stage in ("awaiting-approval", "awaiting-input"):
        pr_url = ""
        try:
            pr_url = json.loads(state_path.read_text()).get("pr_url") or ""
        except Exception:
            pr_url = (state or {}).get("pr_url") or ""

        if next_stage == "awaiting-approval":
            lines = [f"[{task_id}] ✅ PR ready: {pr_url}"] if pr_url else [
                f"[{task_id}] ✅ approved (stop_reason={stop_reason}; no PR recovered)"
            ]
            # Nitpick guard fired: reviewer had only non-blocking findings (0
            # critical). Preserve that feedback ON the PR as a comment instead of
            # looping the developer over it. Best-effort; never breaks the run.
            # Folded into the SAME message as the PR link above (was a second
            # "approved" send for the identical event).
            if stop_reason == "approve_no_critical":
                if pr_url:
                    _post_nonblocking_review_comment(
                        task_dir, target_repo, pr_url, task_id,
                    )
                lines.append(
                    "0 critical; reviewer had only non-blocking notes — no "
                    "hotfix loop. Ready to merge."
                )
            _send_telegram("\n".join(lines))
        else:  # awaiting-input — parked for operator decision (#7, 2026-06-02)
            if pr_url:
                _post_unresolved_findings_comment(
                    task_dir, target_repo, pr_url, task_id,
                )
            # Operator gate: move to awaiting-input/ + send [Продолжить]/[Удалить]
            # buttons. MUST run after the PR-comment above (it reads task_dir).
            # This IS the notification for this outcome (issue #19): every
            # awaiting-input stop_reason post_pipeline.py can return is
            # budget-stop-shaped (_BUDGET_STOP_REASONS covers all of them), so
            # a preceding plain "parked for review" message only duplicated it.
            if stop_reason in _BUDGET_STOP_REASONS:
                _park_budget_stop(
                    task_dir, task_id, stop_reason=stop_reason,
                    cost_usd=cumulative_cost, cost_cap=cost_cap,
                )
            else:
                # Defensive fallback — no currently-reachable stop_reason
                # hits this, but a stop_reason added to post_pipeline.py
                # later without a matching budget-stop entry must still
                # notify SOMETHING rather than go silent.
                crit = _reviewer_critical_count(task_dir)
                _send_telegram(
                    f"[{task_id}] ⏸️ stopped for your review "
                    f"(stop_reason={stop_reason}, {crit} critical) — no PR "
                    f"recovered; see the task dir."
                )
    _notify_bot(
        "pipeline_complete", task_id,
        cost_usd=cumulative_cost,
        next_stage=next_stage,
        stop_reason=stop_reason,
    )
    return 0


def main() -> int:
    """Dispatch entry point.

    Two invocation shapes:
      (A) Positional task_dir → multi-stage pipeline. Used by
          task_dispatcher.py when STAGE_RUNNER_MODE=agent.
      (B) --task-dir + --target-repo + --stage → single-stage harness.
          Used for B.1 smoke tests and manual per-stage runs.
    """
    # #18 — a killed runner must never leave a live claude child behind: TERM/INT
    # kill the children's process groups first, atexit is the belt. (SIGKILL
    # cannot be caught — that residue is what the watcher's orphan sweep is for.)
    _proc_reaper.install()

    if len(sys.argv) >= 2 and not sys.argv[1].startswith("-"):
        return run_pipeline(Path(sys.argv[1]))

    ap = argparse.ArgumentParser(
        description="Tier 3.1 Agent-tool harness — run a pipeline stage via claude Agent tool"
    )
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--target-repo", required=True, type=Path)
    ap.add_argument(
        "--stage", required=True, choices=list(STAGE_AGENT_MAP.keys()),
        help="Pipeline stage to run via Agent tool",
    )
    args = ap.parse_args()

    task_dir = args.task_dir.expanduser().resolve()
    target_repo = args.target_repo.expanduser().resolve()
    stage = args.stage

    if not (task_dir / "spec.json").exists():
        print(f"error: {task_dir}/spec.json missing", file=sys.stderr)
        return 2
    if not (task_dir / "01-ba.md").exists() and stage not in ("ba", "discovery"):
        print(f"error: {task_dir}/01-ba.md missing — BRD required for stage {stage}",
              file=sys.stderr)
        return 2

    state = json.loads((task_dir / "state.json").read_text())
    return _execute_single_stage(task_dir, target_repo, stage, state)


if __name__ == "__main__":
    raise SystemExit(main())
