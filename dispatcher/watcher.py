#!/usr/bin/env python3
"""watcher — crash recovery daemon for the ai-delivery pipeline.

On startup, scans ``tasks/active/`` for orphaned tasks (tasks whose
stage_runner died due to crash/reboot). For each orphan, spawns a new
stage_runner — the runner's built-in artifact-based resume skips already
completed stages.

During steady state, the watcher polls every 15 s to detect newly dead
runners and re-spawn them (up to 3 attempts per task). Every tick it also
considers (but internally throttles, see RECONCILE_INTERVAL_SEC) a
reconciliation sweep of awaiting-approval tasks against GitHub PR state —
catches a PR the owner merged/closed directly on github.com instead of via
the bot's inline keyboard (issue #4). The same tick un-parks limit-stalled
tasks whose ``resume_at`` has passed (issue #11).

The watcher and task_dispatcher are **peers** — they share state only
through the filesystem (``.runner.pid`` files). Neither depends on the
other being up.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import clarify as _clarify
import limit_stall as _limit_stall
import proc_reaper as _proc_reaper
# T24: the "is a runner alive" predicate moved to runner_liveness.py so that
# ops/atlas/aidstack.sh can ask the same question before restarting or stopping
# the stack. A second, shell-shaped definition would drift from this one.
from runner_liveness import RUNNER_SCRIPT_NAMES as _RUNNER_SCRIPT_NAMES
from runner_liveness import pid_is_alive as _pid_is_alive
from runner_liveness import runner_is_dead as _runner_is_dead
from runner_state import _append_history, _update_state
from telegram_io import _notify_bot, _send_telegram

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "tasks"
ACTIVE_DIR = TASKS_DIR / "active"
FAILED_DIR = TASKS_DIR / "failed"
AWAITING_INPUT_DIR = TASKS_DIR / "awaiting-input"
AWAITING_APPROVAL_DIR = TASKS_DIR / "awaiting-approval"
DONE_DIR = TASKS_DIR / "done"
INBOX_DIR = TASKS_DIR / "inbox"

def _load_env_file_into(env: dict) -> None:
    """Overlay KEY=VALUE pairs from the first existing env-file candidate.

    Mirrors the dispatcher's systemd ``EnvironmentFile`` so a runner the
    watcher respawns inherits ``DEEPSEEK_API_KEY`` and the feature flags — even
    when the watcher unit has no ``EnvironmentFile``.
    Without this, a respawned developer/tester/security stage loses its
    DeepSeek key and silently falls back to anthropic (Claude Max), which can
    surface as a spurious ``five_hour`` rate-limit. Existing env keys win,
    matching dotenv/systemd semantics.
    """
    candidates = [
        os.environ.get("STAGE_RUNNER_ENV_FILE", "").strip(),
        str(REPO_ROOT / "bot" / ".env"),
        str(Path.home() / "claude-telegram-bot" / ".env"),
    ]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.is_file():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in env:
                    env[key] = val
        except OSError:
            continue
        break  # first existing file wins, like a single EnvironmentFile


# Environment for respawned runners — os.environ plus the bot env file.
_RUNNER_ENV = os.environ.copy()
_load_env_file_into(_RUNNER_ENV)

# Phase D (2026-06-03): stage_runner_agent.py is the only runner (the legacy
# subprocess runner + the STAGE_RUNNER_MODE switch were removed). The watcher
# respawns exactly the script the dispatcher spawns.
STAGE_RUNNER_SCRIPT = str(REPO_ROOT / "dispatcher" / "stage_runner_agent.py")
POLL_INTERVAL = int(os.environ.get("WATCHER_POLL_INTERVAL", "15"))
MAX_RESPAWN = int(os.environ.get("WATCHER_MAX_RESPAWN", "3"))
MIN_ARTIFACT_SIZE = int(os.environ.get("WATCHER_MIN_ARTIFACT_SIZE", "50"))
GRACE_PERIOD = int(os.environ.get("WATCHER_GRACE_PERIOD", "30"))
# How many times a single task may be auto-requeued for transient network/API
# errors before we leave it parked in awaiting-input/ for operator review.
# Cap of 1 = "one rescue attempt per task lifetime"; subsequent matching
# failures fall through to the normal handoff.
TRANSIENT_RETRY_LIMIT = int(os.environ.get("WATCHER_TRANSIENT_RETRY_LIMIT", "1"))
# Reconcile a task whose state.stage is terminal ('failed' / 'awaiting-input')
# but is STILL physically in active/ — its runner died after setting the
# terminal stage but before the bucket move (e.g. SIGKILL mid-handoff, PR3
# committee 2026-06-02). Move it to the matching bucket so it doesn't orphan in
# active/ forever (and so the bot, which reads awaiting-input/, can find it).
RECONCILE_TERMINAL_IN_ACTIVE = (
    os.environ.get("WATCHER_RECONCILE_TERMINAL_IN_ACTIVE", "1").strip() == "1"
)

# awaiting-approval tasks with a recorded PR never get revisited once the
# stage_runner exits — nothing polls GitHub for an out-of-band merge/close
# (owner clicks "Merge" on github.com instead of the bot's inline keyboard).
# The watcher sweeps for them at most once per RECONCILE_INTERVAL_SEC (issue #4:
# tg-20260607-130837-5f9e sat in active/ at awaiting-approval since 2026-06-07).
RECONCILE_INTERVAL_SEC = int(os.environ.get("RECONCILE_INTERVAL_SEC", "300"))
RECONCILE_GH_TIMEOUT = int(os.environ.get("WATCHER_RECONCILE_GH_TIMEOUT", "30"))

# A runner killed with SIGKILL (or lost to a reboot) cannot run its own kill
# handler, so its claude children survive re-parented to init and keep burning
# the subscription — 3 h 11 m unnoticed on 2026-08-14 (#18). The watcher sweeps
# for that signature (never for anything with a tty or a live parent — see
# proc_reaper) on the same tick, throttled to ORPHAN_SWEEP_INTERVAL_SEC.
ORPHAN_SWEEP_INTERVAL_SEC = int(os.environ.get("ORPHAN_SWEEP_INTERVAL_SEC", "60"))

# Substrings (case-insensitive) that mark a stage failure as a transient
# network/API hiccup worth a single auto-requeue. Keep the list TIGHT —
# over-broad matching would burn quota on real bugs that look network-ish.
# Sourced from operator-observed incidents (claude CLI dropping to a local
# proxy, deepseek/glm upstream blips). Extend only after a real recurrence.
_TRANSIENT_NETWORK_PATTERNS = (
    "API Error: ConnectionRefused",
    "Unable to connect to API",
    "connection refused",
    "ECONNREFUSED",
)


TERMINAL_STAGES = (
    "awaiting-approval",
    "awaiting-input",
    # The agent runner writes "awaiting_clarify" (underscore) when BA pauses
    # for operator answers and then MOVES the task to awaiting-input/. List it
    # here too so that even in the brief window before the move — or if a future
    # path leaves it in active/ — the watcher never respawns a paused task.
    "awaiting_clarify",
    "done",
    "failed",
    "rate_limited",
    # #11 — parked until the backend's limit window resets. Terminal for the
    # respawn logic (a respawn would just re-hit the limit); the resume sweep
    # below is what brings it back.
    _limit_stall.PARKED_STAGE,
)

# Pre-pipeline stages: set by the dispatcher at ingest ("received"), before the
# runner reaches its first pipeline stage. The dispatcher never revisits
# active/, so if a runner dies here (triage runs without updating the stage),
# NEITHER daemon would recover the task — it orphans forever (observed
# 2026-05-31: a double-restart killed a runner at "received" and it sat stuck).
# The watcher adopts these and respawns; the runner restarts cleanly from the
# top, and its flock makes a redundant respawn a no-op.
PRE_PIPELINE_STAGES = ("received",)

# Stage list + artifact names. PIPELINE_STAGES is a SUPERSET — it lists every stage
# regardless of opt-in flags (discovery / pattern-detector / tasks / analyze /
# edge-cases) and is used only for resumability membership, NOT the live order (that
# comes from stage_runner_agent._pipeline_stages_for_agent). The harness writes
# "-agent"-suffixed artifacts.
PIPELINE_STAGES = (
    "discovery", "ba", "pattern-detector", "architect", "tasks", "analyze",
    "edge-cases", "developer", "tester", "security", "reviewer",
    # The hotfix loop is as resumable as any base stage: iteration count and
    # reviewer findings live in state.json, artifacts follow the same naming.
    # Without it a runner dying mid-hotfix orphaned forever (observed
    # 2026-08-14: "stage=developer-hotfix not a recoverable stage, skipping").
    "developer-hotfix",
)
ARTIFACT_NAME: dict[str, str] = {
    "discovery": "00-discovery.md",
    "ba": "01-ba-agent.md",
    "pattern-detector": "01b-patterns-agent.md",
    "architect": "02-architecture-agent.md",
    "tasks": "02b-tasks-agent.md",
    "analyze": "02c-analyze-agent.md",
    "edge-cases": "02d-edgecases-agent.md",
    "developer": "03-dev-agent.md",
    "tester": "04-test-agent.md",
    "security": "05-security-agent.md",
    "reviewer": "06-review-agent.md",
}

logging.basicConfig(
    level=os.environ.get("WATCHER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [watcher] %(message)s",
)
log = logging.getLogger("watcher")

_running = True
_respawn_counts: dict[str, int] = {}
_managed_tasks: set[str] = set()
_last_pr_reconcile_ts = 0.0
_last_orphan_sweep_ts = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_state(task_dir: Path) -> Optional[dict]:
    """Parse state.json, return None on any error."""
    try:
        return json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _is_terminal_stage(stage: str) -> bool:
    """Stages where the pipeline is finished (or paused externally)."""
    # "failed:*" for failed stages like "failed: target_repo not found"
    return stage in TERMINAL_STAGES or stage.startswith("failed:")


def _artifact_is_valid(task_dir: Path, stage: str) -> bool:
    """Check whether the artifact for *stage* exists and has meaningful content."""
    artifact_name = ARTIFACT_NAME.get(stage)
    if not artifact_name:
        return False
    artifact = task_dir / artifact_name
    try:
        return artifact.is_file() and artifact.stat().st_size > MIN_ARTIFACT_SIZE
    except OSError:
        return False


def _append_worklog(task_dir: Path, note: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    try:
        with (task_dir / "worklog.md").open("a") as fh:
            fh.write(f"{ts}  watcher: {note}\n")
    except OSError:
        pass


def _move_to_bucket(task_dir: Path, bucket_dir: Path, reason: str) -> None:
    bucket_dir.mkdir(parents=True, exist_ok=True)
    dst = bucket_dir / task_dir.name
    if dst.exists():
        shutil.rmtree(str(dst))
    _append_worklog(task_dir, f"moving to {bucket_dir.name}: {reason}")
    shutil.move(str(task_dir), str(dst))
    log.warning("moved %s → %s (%s)", task_dir.name, bucket_dir.name, reason)


def _move_to_failed(task_dir: Path, reason: str) -> None:
    _move_to_bucket(task_dir, FAILED_DIR, reason)


def _task_cost_cap(task_dir: Path) -> float:
    """The dollar cost cap for a task, from spec.json (default 20)."""
    try:
        spec = json.loads((task_dir / "spec.json").read_text())
        return float(spec.get("cost_cap_usd") or 20)
    except Exception:
        return 20.0


def _terminal_bucket_for(stage: str) -> Optional[Path]:
    """The bucket a terminal-but-stranded-in-active task belongs in, or None if
    its stage is not one we relocate. Only 'failed' (incl. 'failed: <reason>'),
    'awaiting-input' and the limit park 'waiting-limits' (#11) are reconciled —
    'awaiting-approval'/'done' are left where the existing operator flows expect
    them."""
    if stage in ("awaiting-input", _limit_stall.PARKED_STAGE):
        return AWAITING_INPUT_DIR
    if stage == "failed" or stage.startswith("failed:"):
        return FAILED_DIR
    return None


def _reconcile_terminal_in_active(task_dir: Path, stage: str, task_id: str) -> bool:
    """Relocate a terminal-stage task stranded in active/ to its bucket. Gated by
    WATCHER_RECONCILE_TERMINAL_IN_ACTIVE and the same orphan-safety checks the
    respawn path uses (still in active/, past grace, dead runner) so it never
    races a runner mid-move. Returns True if it moved the task."""
    if not RECONCILE_TERMINAL_IN_ACTIVE:
        return False
    bucket = _terminal_bucket_for(stage)
    if bucket is None:
        return False
    try:
        if task_dir.parent.resolve() != ACTIVE_DIR.resolve():
            return False  # already in a bucket — nothing to do
        age = time.time() - (task_dir / "state.json").stat().st_mtime
    except OSError:
        return False
    if age < GRACE_PERIOD:
        return False  # may be a runner mid-handoff; let it finish
    if not _runner_is_dead(task_dir, task_id):
        return False
    _move_to_bucket(task_dir, bucket,
                    f"terminal stage '{stage}' stranded in active/ (runner gone)")
    return True


# ---------------------------------------------------------------------------
# Transient-network auto-requeue (handoff rescue)
# ---------------------------------------------------------------------------


def _latest_stage_error_text(task_dir: Path) -> str:
    """Return the freshest `<stage>.claude-error.log` body (capped) + worklog
    tail. Empty when nothing readable. We sniff both: the error log carries the
    raw claude stderr (where 'API Error: ConnectionRefused' lives), while the
    worklog tail carries the human-friendly handoff line."""
    blob = ""
    try:
        logs = sorted(
            task_dir.glob("*.claude-error.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if logs:
            blob += logs[0].read_text(errors="replace")[-8000:]
    except OSError:
        pass
    try:
        worklog = task_dir / "worklog.md"
        if worklog.is_file():
            blob += "\n" + worklog.read_text(errors="replace")[-4000:]
    except OSError:
        pass
    return blob


def _looks_like_transient_network_error(blob: str) -> bool:
    """Case-insensitive substring match against the tight allowlist."""
    if not blob:
        return False
    low = blob.lower()
    return any(pat.lower() in low for pat in _TRANSIENT_NETWORK_PATTERNS)


def _requeue_to_inbox(task_dir: Path, task_id: str, state: dict, *,
                      counter: str = "transient_retries",
                      cause: str = "transient network error",
                      limit: int | None = None) -> bool:
    """Move task back to tasks/inbox/ for an automatic retry.

    Bumps state.<counter> BEFORE the move so even a crash mid-move leaves the
    breadcrumb (next pass sees retries=1 and won't loop). Clears the handoff
    stamp so re-ingest starts the task cleanly. The dispatcher's
    _write_state_json carries transient_retries across re-ingest (additive
    change there, see task_dispatcher.py).

    `counter`/`cause`/`limit` let the limit-stall resume sweep (#11) reuse the
    exact same move + collision guard under its own budget."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    state[counter] = int(state.get(counter) or 0) + 1
    state.pop("handoff", None)
    if limit is None:
        limit = TRANSIENT_RETRY_LIMIT
    try:
        (task_dir / "state.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("[%s] auto-requeue: failed to bump %s: %s",
                    task_id, counter, exc)
        return False

    _append_worklog(
        task_dir,
        f"AUTO-REQUEUE: {cause} → inbox/ ({counter}={state[counter]})",
    )
    dst = INBOX_DIR / task_id
    if dst.exists():
        # Pathological: collision with a queued duplicate. Bail rather than
        # overwrite a pending queue entry the dispatcher may be ingesting.
        log.warning("[%s] auto-requeue: inbox/%s already exists; skipping",
                    task_id, task_id)
        return False
    try:
        shutil.move(str(task_dir), str(dst))
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] auto-requeue: move to inbox/ failed: %s", task_id, exc)
        return False
    log.warning(
        "[%s] AUTO-REQUEUE: %s; awaiting-input/ → inbox/ (attempt %d/%d)",
        task_id, cause, state[counter], limit,
    )
    return True


def scan_awaiting_input_for_transient_retry() -> None:
    """Walk tasks/awaiting-input/ and auto-requeue tasks whose handoff was
    caused by a transient network error — once per task. Subsequent transient
    hits (transient_retries >= TRANSIENT_RETRY_LIMIT) sit for the operator,
    matching the existing handoff contract."""
    if not AWAITING_INPUT_DIR.is_dir():
        return
    for entry in sorted(AWAITING_INPUT_DIR.iterdir()):
        if not entry.is_dir():
            continue
        task_id = entry.name
        state = _read_state(entry)
        if state is None:
            continue
        if str(state.get("stage") or "") == _limit_stall.PARKED_STAGE:
            continue  # a limit park owns its own schedule (#11) — don't jump it
        retries = int(state.get("transient_retries") or 0)
        if retries >= TRANSIENT_RETRY_LIMIT:
            continue
        # Grace window keeps us out of races with the bot's notification flow
        # that just picked up this freshly-parked task.
        try:
            age = time.time() - (entry / "state.json").stat().st_mtime
        except OSError:
            continue
        if age < GRACE_PERIOD:
            continue
        blob = _latest_stage_error_text(entry)
        if not _looks_like_transient_network_error(blob):
            continue
        _requeue_to_inbox(entry, task_id, state)


def scan_limit_parked_for_resume() -> None:
    """Un-park limit-stalled tasks (#11): walk tasks/awaiting-input/ and requeue
    every task whose ``resume_at`` has passed. The resume machinery skips the
    stages that already produced an artifact, so the task picks up where the
    limit window cut it off. One log line per action; never raises."""
    if not AWAITING_INPUT_DIR.is_dir():
        return
    for entry in sorted(AWAITING_INPUT_DIR.iterdir()):
        if not entry.is_dir():
            continue
        state = _read_state(entry)
        if state is None:
            continue
        if str(state.get("stage") or "") != _limit_stall.PARKED_STAGE:
            continue
        if not _limit_stall.is_due(state):
            log.debug("[%s] limit-park: still waiting until %s",
                      entry.name, state.get("resume_at"))
            continue
        state["stage"] = "inbox"
        state.pop("resume_at", None)
        try:
            if _requeue_to_inbox(
                    entry, entry.name, state,
                    counter="limit_parks",
                    cause=f"limit window elapsed ({state.get('limit_stall', {}).get('stage', '?')})",
                    limit=_limit_stall.LIMIT_PARK_LIMIT):
                log.info("[%s] limit-park: resume_at passed → inbox/", entry.name)
        except Exception:  # noqa: BLE001 — one bad task must not kill the sweep
            log.exception("[%s] limit-park: unexpected failure", entry.name)


def scan_clarify_deadman() -> None:
    """Resume clarify-paused tasks nobody answered (T10).

    The clarify pause has no timeout: ``awaiting_clarify`` is terminal for this
    watcher (deliberately, fix 2026-06-01), so a task whose questions went
    unnoticed in Telegram waits forever — on 2026-08-17 both live tasks stood
    ~3 hours and the answers they finally got were the BA's own defaults. With
    ``CLARIFY_DEADMAN_HOURS`` set, this sweep writes those defaults into the same
    ``clarifications.md`` the operator would have written, and requeues the task.

    OFF by default (0 hours) — an install that does not opt in never reaches the
    body of the loop. One auto-resume per task, enforced in ``deadman_due`` and
    counted in ``state.clarify_auto_resumes`` (carried across re-ingest by the
    dispatcher), so a SECOND clarify pause on the same task waits for a human
    again instead of answering itself in a circle. Never raises."""
    hours = _clarify.deadman_hours()
    if hours <= 0 or not AWAITING_INPUT_DIR.is_dir():
        return
    for entry in sorted(AWAITING_INPUT_DIR.iterdir()):
        if not entry.is_dir():
            continue
        task_id = entry.name
        state = _read_state(entry)
        if state is None:
            continue
        try:
            if not _clarify.deadman_due(state, entry):
                continue
            questions = _clarify.pending_questions(state, entry)
            if not questions:
                # Parked with no recoverable questions: there is nothing to
                # answer on the task's behalf, and inventing a resume would send
                # BA back in with the same unresolved markers. Leave it.
                log.warning("[%s] clarify dead man: no pending questions found "
                            "— leaving it for the operator", task_id)
                continue
            _clarify.append_answers(
                entry, _clarify.default_answers(questions, hours))
            state.pop("clarify_pending", None)
            state["clarify_deadman"] = {
                "resumed_at": _clarify.now_iso(),
                "waited_hours": hours,
                "questions": len(questions),
            }
            try:
                (entry / "clarifications-pending.json").unlink()
            except OSError:
                pass
            state["stage"] = "inbox"
            if not _requeue_to_inbox(
                    entry, task_id, state,
                    counter=_clarify.AUTO_RESUME_COUNTER,
                    cause=f"clarify unanswered for {hours:g}h — resuming on BA defaults",
                    limit=_clarify.AUTO_RESUME_LIMIT):
                continue
            log.warning("[%s] clarify dead man: %d question(s) unanswered for "
                        "%gh → inbox/ on BA defaults", task_id, len(questions), hours)
            _send_telegram(
                f"[{task_id}] ⏭️ уточнения без ответа {hours:g} ч — продолжаю по "
                f"дефолтам BA ({len(questions)} вопрос(а/ов)). Ответ на тот "
                f"prompt больше не принимается; нужен другой ход — "
                f"/requeue {task_id}.")
            _notify_bot("clarify_deadman_resumed", task_id,
                        waited_hours=hours, questions=len(questions))
        except Exception:  # noqa: BLE001 — one bad task must not kill the sweep
            log.exception("[%s] clarify dead man: unexpected failure", task_id)


# ---------------------------------------------------------------------------
# PR reconciliation (issue #4): awaiting-approval tasks with an out-of-band
# GitHub merge/close never get revisited once the stage_runner exits — the
# stage_runner only sets state.stage=awaiting-approval and leaves the task in
# active/ (there is no bucket move for that stage). If the owner merges (or
# closes) the PR directly on GitHub instead of tapping the bot's inline
# keyboard, nothing ever reconciles the task — it orphans forever.
# ---------------------------------------------------------------------------


def _gh_pr_view(pr_url: str) -> Optional[dict]:
    """`gh pr view <url> --json state,mergedAt`, parsed. Returns None on ANY
    failure — gh not installed, no network, non-zero exit, timeout, bad JSON —
    so a caller must treat None as "unknown, try again next sweep", never as
    CLOSED/MERGED. Never raises."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt"],
            capture_output=True, text=True, timeout=RECONCILE_GH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("pr-reconcile: gh unavailable for %s: %s", pr_url, exc)
        return None
    if proc.returncode != 0:
        log.info("pr-reconcile: gh pr view rc=%d for %s: %s",
                  proc.returncode, pr_url, (proc.stderr or "").strip()[:200])
        return None
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def _pr_reconcile_action(gh_json: Optional[dict]) -> Optional[str]:
    """Pure decision: map a `gh pr view --json state,mergedAt` payload to a
    reconciliation action. No I/O — mockable/testable without gh.

    Returns 'done' (PR merged), 'failed' (PR closed without merging), or None
    to leave the task exactly where it is (still OPEN, or the gh call above
    returned nothing usable)."""
    if not gh_json:
        return None
    state = str(gh_json.get("state") or "").upper()
    if state == "MERGED":
        return "done"
    if state == "CLOSED":
        return "failed"
    return None  # OPEN, or an unrecognized shape — wait for the next sweep


def _reconcile_pr_task(task_dir: Path, task_id: str, pr_url: str) -> bool:
    """Poll gh for *pr_url*'s state and, on MERGED/CLOSED, move the task to its
    terminal bucket with a history note + bot/Telegram notification. Returns
    True if the task was reconciled (moved), False if left alone (still OPEN,
    or gh was unreachable)."""
    action = _pr_reconcile_action(_gh_pr_view(pr_url))
    if action is None:
        return False

    if action == "done":
        bucket_dir, new_stage = DONE_DIR, "done"
        note = f"PR merged: {pr_url} — reconciled awaiting-approval -> done"
        summary = f"[{task_id}] ✅ PR merged → done: {pr_url}"
    else:
        bucket_dir, new_stage = FAILED_DIR, "failed"
        note = f"PR closed without merging: {pr_url} — reconciled awaiting-approval -> failed"
        summary = f"[{task_id}] ❌ PR closed (not merged) → failed: {pr_url}"

    try:
        _update_state(task_dir, stage=new_stage)
        _append_history(task_dir, new_stage, note)
    except Exception as exc:  # noqa: BLE001 — a state-write hiccup must not
        # block the bucket move below (the note is best-effort commentary).
        log.warning("[%s] pr-reconcile: state update failed: %s", task_id, exc)

    _move_to_bucket(task_dir, bucket_dir, note)
    log.info("[%s] pr-reconcile: %s", task_id, note)

    _send_telegram(summary)
    _notify_bot("pr_reconciled", task_id, pr_url=pr_url, verdict=action, next_stage=new_stage)
    return True


def _iter_awaiting_approval_candidates():
    """Yield task dirs to check: active/ (where the current pipeline actually
    leaves awaiting-approval tasks — it never moves them) plus
    awaiting-approval/ (the bucket the bot's inline-keyboard approval flow
    reads from, in case a task ever lands there some other way). Both are
    checked per issue #4's proposal."""
    for base in (ACTIVE_DIR, AWAITING_APPROVAL_DIR):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_dir():
                yield entry


def scan_pr_reconciliation() -> None:
    """Sweep awaiting-approval tasks with a recorded PR and reconcile them
    against GitHub. Throttled to once per RECONCILE_INTERVAL_SEC — safe to
    call on every watcher tick, it self-skips the rest. Never raises: one
    task's failure must not stop the sweep or crash the watcher."""
    global _last_pr_reconcile_ts
    now = time.time()
    if now - _last_pr_reconcile_ts < RECONCILE_INTERVAL_SEC:
        return
    _last_pr_reconcile_ts = now

    for entry in _iter_awaiting_approval_candidates():
        state = _read_state(entry)
        if state is None or state.get("stage") != "awaiting-approval":
            continue
        pr_url = str(state.get("pr_url") or "").strip()
        if not pr_url:
            continue
        try:
            _reconcile_pr_task(entry, entry.name, pr_url)
        except Exception:  # noqa: BLE001 — one bad task must not kill the sweep
            log.exception("[%s] pr-reconcile: unexpected failure", entry.name)


# ---------------------------------------------------------------------------
# Runner lifecycle
# ---------------------------------------------------------------------------


def _spawn_runner(task_dir: Path) -> Optional[int]:
    """Spawn ``stage_runner.py <task_dir>`` and write ``.runner.pid``."""
    task_id = task_dir.name
    try:
        proc = subprocess.Popen(
            [sys.executable, STAGE_RUNNER_SCRIPT, str(task_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_RUNNER_ENV,
        )
    except Exception:
        log.exception("failed to spawn runner for %s", task_id)
        return None

    (task_dir / ".runner.pid").write_text(f"{proc.pid}\n")
    log.info("spawned runner pid=%d for %s", proc.pid, task_id)
    _append_worklog(task_dir, f"spawned runner pid={proc.pid}")
    _managed_tasks.add(task_id)
    return proc.pid


# ---------------------------------------------------------------------------
# Orphan classification
# ---------------------------------------------------------------------------


def _classify_orphan(task_dir: Path, state: dict) -> Optional[str]:
    """Classify an orphaned task and spawn a runner if appropriate.

    Returns a reason string if the task was moved to failed, or None
    if a runner was spawned successfully.
    """
    task_id = task_dir.name
    stage = state.get("stage", "")

    # Grace period: skip tasks modified recently — dispatcher is handling them.
    # Prevents watcher from racing with dispatcher on freshly-ingested tasks.
    state_mtime = (task_dir / "state.json").stat().st_mtime
    age = time.time() - state_mtime
    if age < GRACE_PERIOD:
        log.info("[%s] state.json modified %.0fs ago (< %ds grace), skipping",
                 task_id, age, GRACE_PERIOD)
        return None

    # Check .runner.pid — maybe the runner is still alive
    pid_file = task_dir / ".runner.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _pid_is_alive(pid, task_id):
                log.info("[%s] runner pid=%d is alive, skipping", task_id, pid)
                return None
        except (ValueError, OSError):
            pass

    # Respawn limit
    count = _respawn_counts.get(task_id, 0)
    if count >= MAX_RESPAWN:
        reason = f"reached max respawn count ({MAX_RESPAWN})"
        _move_to_failed(task_dir, reason)
        return reason

    # Over-budget guard (2026-06-07): a task whose cost already meets/exceeds its
    # cap can only re-fail if respawned (it would skip every completed stage and
    # re-hit the cap — the $21-twice loop). Don't respawn; hand it to the operator
    # gate (awaiting-input/ + [Продолжить]/[Удалить]) via the shared budget_gate.
    cost_cap = _task_cost_cap(task_dir)
    cost_usd = float(state.get("cost_usd") or 0.0)
    if cost_cap > 0 and cost_usd >= cost_cap:
        try:
            import budget_gate
            budget_gate.park(task_dir, task_id, stop_reason="cost_cap",
                             cost_usd=cost_usd, cost_cap=cost_cap)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] over-budget park failed: %s", task_id, exc)
            return None
        return f"over budget (${cost_usd:.2f} >= ${cost_cap:.2f}) — parked for operator"

    # If current stage not a pipeline stage, we can't artifact-resume — but we
    # CAN still adopt a pre-pipeline orphan ("received") by respawning from the
    # top. The old code punted "received" to the dispatcher, which never
    # revisits active/, so a runner that died before its first pipeline stage
    # orphaned forever (observed 2026-05-31). Past grace + dead pid means it is
    # a genuine orphan; the runner's flock guard makes a redundant respawn safe.
    if stage not in PIPELINE_STAGES:
        if stage in PRE_PIPELINE_STAGES:
            log.info("[%s] stage=%s, runner dead past grace — adopting orphan "
                     "(runner resumes from top)", task_id, stage)
            _respawn_counts[task_id] = count + 1
            _spawn_runner(task_dir)
            return None
        if _reconcile_terminal_in_active(task_dir, stage, task_id):
            return f"reconciled terminal stage '{stage}' stranded in active/"
        log.info("[%s] stage=%s not a recoverable stage, skipping", task_id, stage)
        return None

    # Check artifact for current stage
    if _artifact_is_valid(task_dir, stage):
        log.info("[%s] artifact for %s exists (valid) — spawning runner (will skip done)", task_id, stage)
    else:
        log.info("[%s] artifact for %s missing/broken — runner will re-run stage", task_id, stage)
        # Delete partial artifact so runner doesn't mistake it for complete
        artifact_name = ARTIFACT_NAME.get(stage)
        if artifact_name:
            partial = task_dir / artifact_name
            if partial.exists():
                partial.unlink()
                log.info("[%s] deleted partial artifact %s", task_id, artifact_name)

    _respawn_counts[task_id] = count + 1
    _spawn_runner(task_dir)
    return None


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


def sweep_orphan_children(force: bool = False) -> int:
    """Kill claude children orphaned by a runner that died without cleaning up.

    Throttled to ORPHAN_SWEEP_INTERVAL_SEC (``force`` runs it now — used by the
    startup scan). One log line per kill. The matcher's safety rules live in
    proc_reaper: ppid==1 AND no tty AND both pipeline flags, so an interactive
    Claude Code session (same --dangerously-skip-permissions flag, but a tty and
    a live parent) can never be selected. Returns the number killed.
    """
    global _last_orphan_sweep_ts
    if not _proc_reaper.SWEEP_ENABLED:
        return 0
    now = time.time()
    if not force and (now - _last_orphan_sweep_ts) < ORPHAN_SWEEP_INTERVAL_SEC:
        return 0
    _last_orphan_sweep_ts = now
    try:
        killed = _proc_reaper.sweep(log=log.warning)
    except Exception:  # noqa: BLE001 — a sweep hiccup must not kill the watcher
        log.exception("orphan child sweep failed")
        return 0
    return len(killed)


def scan_orphans() -> None:
    """Startup sweep: find and recover all orphaned tasks in active/."""
    if not ACTIVE_DIR.is_dir():
        log.info("no active/ directory, nothing to scan")
        return

    for entry in sorted(ACTIVE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        task_id = entry.name
        state = _read_state(entry)
        if state is None:
            _move_to_failed(entry, "unreadable state.json")
            continue

        stage = state.get("stage", "unknown")
        if _is_terminal_stage(stage):
            if _reconcile_terminal_in_active(entry, stage, task_id):
                continue  # relocated to its bucket
            log.info("[%s] stage=%s terminal, skipping", task_id, stage)
            continue

        _classify_orphan(entry, state)

    scan_awaiting_input_for_transient_retry()
    scan_limit_parked_for_resume()
    scan_clarify_deadman()
    scan_pr_reconciliation()
    sweep_orphan_children(force=True)


def monitor_loop() -> None:
    """Periodic liveness check for managed runners."""
    global _running, _managed_tasks, _respawn_counts
    while _running:
        time.sleep(POLL_INTERVAL)

        # Auto-requeue tasks parked on transient network errors (once per task).
        scan_awaiting_input_for_transient_retry()

        # Un-park limit-stalled tasks whose resume_at has passed (#11).
        scan_limit_parked_for_resume()

        # Resume clarify-paused tasks nobody answered in time (T10; off unless
        # CLARIFY_DEADMAN_HOURS is set).
        scan_clarify_deadman()

        # Reconcile awaiting-approval tasks against GitHub PR state (throttled
        # internally to RECONCILE_INTERVAL_SEC — issue #4).
        scan_pr_reconciliation()

        # Kill claude children left behind by a SIGKILLed runner (#18).
        sweep_orphan_children()

        # Scan for new orphans (tasks that arrived after watcher started)
        if ACTIVE_DIR.is_dir():
            for entry in sorted(ACTIVE_DIR.iterdir()):
                if not entry.is_dir():
                    continue
                tid = entry.name
                if tid in _managed_tasks:
                    continue
                state = _read_state(entry)
                if state is None:
                    continue
                stage = state.get("stage", "")
                if _is_terminal_stage(stage):
                    _reconcile_terminal_in_active(entry, stage, tid)
                    continue
                # Pipeline stages resume via artifacts; pre-pipeline orphans
                # ("received") get adopted. Anything else is left to _classify_orphan
                # to skip — but only those two families are worth classifying here.
                if stage not in PIPELINE_STAGES and stage not in PRE_PIPELINE_STAGES:
                    continue
                _classify_orphan(entry, state)

        # Check liveness of managed runners
        dead = set()
        for tid in list(_managed_tasks):
            task_dir = ACTIVE_DIR / tid
            if not task_dir.is_dir():
                dead.add(tid)
                continue

            pid_file = task_dir / ".runner.pid"
            if not pid_file.exists():
                continue  # runner hasn't been spawned yet (edge case)

            try:
                pid = int(pid_file.read_text().strip())
            except (ValueError, OSError):
                dead.add(tid)
                continue

            if not _pid_is_alive(pid, tid):
                dead.add(tid)
                count = _respawn_counts.get(tid, 0)
                if count < MAX_RESPAWN:
                    log.info("[%s] runner pid=%d died — re-spawning", tid, pid)
                    _respawn_counts[tid] = count + 1
                    _spawn_runner(task_dir)
                else:
                    log.warning("[%s] runner died, max respawn reached — moving to failed", tid)
                    _move_to_failed(task_dir, f"runner died after {count} respawns")

        _managed_tasks -= dead


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _on_signal(signum: int, _frame: object) -> None:
    global _running
    name = signal.Signals(signum).name
    log.info("received %s, exiting", name)
    _running = False


def main() -> None:
    log.info("watcher starting — scanning tasks/active/")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    scan_orphans()
    log.info("startup scan complete, entering monitor loop (poll=%ds)", POLL_INTERVAL)
    monitor_loop()
    log.info("watcher stopped")


if __name__ == "__main__":
    main()
