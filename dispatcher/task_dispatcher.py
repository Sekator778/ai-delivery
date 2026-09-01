#!/usr/bin/env python3
"""task_dispatcher — Phase 5 file-queue daemon.

Polls tasks/inbox/ every POLL_INTERVAL seconds. For each new entry it:
  1. validates spec.json against schema/spec.schema.json,
  2. allocates a task_id if absent,
  3. atomically moves the folder to tasks/active/<task-id>/,
  4. writes the initial task.md and state.json,
  5. appends an "ingested" line to worklog.md,
  6. spawns dispatcher/stage_runner_agent.py as a subprocess to execute the
     full BA→Architect→Dev→Test→Sec→Reviewer pipeline.

Accepts inbox entries in two forms:
  - tasks/inbox/<task-id>/spec.json   (preferred — task_id matches folder)
  - tasks/inbox/<anything>.json       (loose file → wrapped into a new folder)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import jsonschema  # type: ignore
except ImportError:
    print("jsonschema is required: pip install jsonschema", file=sys.stderr)
    raise

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "tasks"
INBOX_DIR = TASKS_DIR / "inbox"
ACTIVE_DIR = TASKS_DIR / "active"
FAILED_DIR = TASKS_DIR / "failed"
TEMPLATE_DIR = TASKS_DIR / "_TEMPLATE"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "spec.schema.json"
POLL_INTERVAL = float(os.environ.get("DISPATCHER_POLL_INTERVAL", "5"))

logging.basicConfig(
    level=os.environ.get("DISPATCHER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("task_dispatcher")
_running = True


def _load_schema() -> dict:
    with SCHEMA_PATH.open() as f:
        return json.load(f)


def _next_task_id() -> str:
    """Allocate the next TASK-<N> based on what's already on disk."""
    existing: list[int] = []
    for column in (INBOX_DIR, ACTIVE_DIR, TASKS_DIR / "awaiting-input",
                   TASKS_DIR / "awaiting-approval", TASKS_DIR / "done", FAILED_DIR):
        if not column.exists():
            continue
        for entry in column.iterdir():
            name = entry.name
            if name.startswith("TASK-"):
                try:
                    existing.append(int(name.split("-", 1)[1]))
                except ValueError:
                    continue
    return f"TASK-{(max(existing) + 1) if existing else 1:04d}"


def _spec_files() -> list[Path]:
    """Find every spec.json in inbox/, either inside a folder or as a loose .json."""
    out: list[Path] = []
    if not INBOX_DIR.exists():
        return out
    for entry in sorted(INBOX_DIR.iterdir()):
        if entry.is_dir():
            spec = entry / "spec.json"
            if spec.exists():
                out.append(spec)
        elif entry.suffix == ".json" and entry.name != "README.md":
            out.append(entry)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ingest(spec_path: Path, schema: dict) -> None:
    """Validate, allocate id if needed, move into active/, write initial files."""
    raw = spec_path.read_text()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        _quarantine(spec_path, f"invalid JSON: {e}")
        return
    try:
        jsonschema.validate(instance=spec, schema=schema)
    except jsonschema.ValidationError as e:
        _quarantine(spec_path, f"schema validation failed: {e.message}")
        return

    task_id = spec.get("task_id") or _next_task_id()
    spec["task_id"] = task_id
    spec.setdefault("created_at", _now_iso())

    src_dir = spec_path.parent if spec_path.parent != INBOX_DIR else None
    dst_dir = ACTIVE_DIR / task_id
    if dst_dir.exists():
        _quarantine(spec_path, f"collision: active/{task_id} already exists")
        return

    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    if src_dir is not None:
        shutil.move(str(src_dir), str(dst_dir))
        (dst_dir / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    else:
        dst_dir.mkdir()
        (dst_dir / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
        spec_path.unlink()

    _write_task_md(dst_dir, spec)
    _write_state_json(dst_dir, spec)
    _append_worklog(dst_dir, "ingested by task_dispatcher")
    log.info("ingested %s (trigger=%s user=%s)", task_id, spec["trigger"], spec["user"])

    # Fire-and-forget: launch the pipeline in a subprocess
    _spawn_stage_runner(dst_dir)


def _write_task_md(task_dir: Path, spec: dict) -> None:
    body = (
        f"# {spec['task_id']} — {spec.get('prompt', '').splitlines()[0][:60]}\n\n"
        f"- **Source:** {spec['trigger']}\n"
        f"- **Submitted by:** {spec['user']}\n"
        f"- **Submitted at:** {spec.get('created_at')}\n"
        f"- **Target repo:** {spec['target_repo']}\n"
    )
    if spec.get("schedule"):
        body += f"- **Schedule:** `{spec['schedule']}`\n"
    body += (
        "\n## Original request (verbatim)\n\n"
        f"> {spec['prompt']}\n"
    )
    (task_dir / "task.md").write_text(body)


def _write_state_json(task_dir: Path, spec: dict) -> None:
    routing = spec.get("model_routing") or {}
    # On RE-ingest (rl_switch / clarify / the budget-continue button bounce a task
    # back through inbox/), an existing state.json holds resume-critical fields the
    # fresh template would otherwise drop — notably branch/pr_url, which the
    # developer-hotfix branch-lock safety guard REQUIRES (without them a PR'd task
    # dies with safety_violation; 2026-06-07 dogfood). Carry those forward.
    prior: dict = {}
    existing = task_dir / "state.json"
    if existing.exists():
        try:
            prior = json.loads(existing.read_text())
        except (OSError, json.JSONDecodeError):
            prior = {}
    state = {
        "id": spec["task_id"],
        "title": spec.get("prompt", "")[:80],
        "source": spec["trigger"],
        "submitted_by": spec["user"],
        "submitted_at": spec.get("created_at"),
        "stage": "received",
        "iteration": 0,
        "cost_usd": 0.0,
        "cost_cap_usd": spec.get("cost_cap_usd", 20),
        "iteration_cap": spec.get("iteration_cap", 3),
        "model_routing": routing,
        "telegram_thread": spec.get("telegram_thread"),
        "schedule": spec.get("schedule"),
        "history": [
            {"at": _now_iso(), "stage": "received", "note": "ingested by task_dispatcher"}
        ],
    }
    # clarify_auto_resumes persists for the same reason (T10): the clarify dead
    # man may resume a task on BA defaults exactly ONCE, and the re-ingest it
    # triggers runs through here — a reset counter would let every later clarify
    # pause answer itself again instead of waiting for the operator.
    # transient_retries persists across re-ingest so the watcher's
    # auto-requeue is capped at TRANSIENT_RETRY_LIMIT across the task lifetime
    # — without this carry-forward, the dispatcher would reset retries to 0
    # on every re-ingest and the rescue could loop on a flapping API.
    # stage_sessions persists so a re-ingested task can --resume the per-stage
    # Claude conversation that was interrupted by the failure (otherwise
    # ingest would drop the id and the stage would restart from scratch).
    # limit_parks does the same for the limit-outage park budget (#11), and
    # worktree keeps the resumed run on the SAME branch/checkout instead of
    # cutting a `feat/<id>-1` twin next to the branch that already carries the PR.
    # cost_usd / iteration persist across re-ingest (#14): a mid-flight task
    # moved back to tasks/inbox/ — manual requeue OR the limit-park auto-
    # requeue sweep (watcher.scan_limit_parked_for_resume) — used to have its
    # spent budget silently zeroed here, even though state.json carried the
    # real accumulated spend. Since run_pipeline's cost-cap check reads
    # state.cost_usd as its starting cumulative_cost (stage_runner_agent.py,
    # "Carry prior cost forward on resume"), the reset let an operator bypass
    # cost_cap entirely by repeatedly requeuing: each re-ingest restarted the
    # cap check from $0 while the true spend kept climbing (observed:
    # cost_usd=0.00 on resume vs. $11.32 already spent, cumulative real cost
    # $13.45 with a $20 cap never tripped). The watcher-respawn path never
    # goes through here and never had this bug — it reads state.cost_usd
    # directly off disk. triage/base_branch are included too: the sticky-
    # triage verdict (see stage_runner_agent._load_persisted_triage) and the
    # once-per-run base branch are both re-derived early in run_pipeline
    # regardless, but preserving them here closes the same reset-to-default
    # window for any code that reads state.json between ingest and the next
    # pipeline start.
    for k in ("branch", "pr_url", "review_trend", "review_trend_iter",
              "transient_retries", "stage_sessions", "limit_parks", "worktree",
              "cost_usd", "iteration", "triage", "base_branch",
              "clarify_auto_resumes"):
        if prior.get(k) is not None:
            state[k] = prior[k]
    (task_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n")


def _append_worklog(task_dir: Path, note: str) -> None:
    line = f"- {_now_iso()} — {note}\n"
    worklog = task_dir / "worklog.md"
    if not worklog.exists():
        worklog.write_text(f"# Worklog\n\n{line}")
    else:
        with worklog.open("a") as f:
            f.write(line)


def _quarantine(spec_path: Path, reason: str) -> None:
    log.error("quarantine %s: %s", spec_path, reason)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # If spec.json lives inside a per-task folder (the preferred layout),
    # move the whole folder; otherwise just the loose .json file.
    if spec_path.parent != INBOX_DIR:
        target = spec_path.parent
        dst = FAILED_DIR / f"intake-{stamp}-{target.name}"
    else:
        target = spec_path
        dst = FAILED_DIR / f"intake-{stamp}-{spec_path.name}"
    shutil.move(str(target), str(dst))
    (dst.parent / f"{dst.name}.reason.txt").write_text(reason + "\n")


# Tracks running stage-runner subprocesses so we can reap them.
_stage_runners: dict[str, subprocess.Popen] = {}

# Phase D (2026-06-03) — the Agent-tool harness (stage_runner_agent.py) is the ONLY
# runner: all triage / convergence-gate / handoff / clarify logic lives there, and it
# has been the validated production path since committee Q3 (2026-06-02). The legacy
# subprocess runner (stage_runner.py) and the STAGE_RUNNER_MODE switch were removed.
_STAGE_RUNNER_SCRIPT = str(Path(__file__).resolve().parent / "stage_runner_agent.py")

_MAX_CONCURRENT_STAGES = int(os.environ.get("DISPATCHER_MAX_STAGES", "3"))


def _spawn_stage_runner(task_dir: Path) -> None:
    """Launch stage_runner_agent.py in a subprocess for the given task directory.

    Non-blocking — the subprocess runs independently while the dispatcher
    continues polling for new inbox entries.
    """
    # Reap finished processes to avoid zombie accumulation
    finished = [tid for tid, p in _stage_runners.items() if p.poll() is not None]
    for tid in finished:
        p = _stage_runners.pop(tid)
        log.info("stage-runner for %s exited rc=%d", tid, p.returncode)

    # Respect concurrency limit
    if len(_stage_runners) >= _MAX_CONCURRENT_STAGES:
        log.warning(
            "stage-runner concurrency limit reached (%d/%d) — deferring %s",
            len(_stage_runners), _MAX_CONCURRENT_STAGES, task_dir.name,
        )
        return

    task_id = task_dir.name
    try:
        proc = subprocess.Popen(
            [sys.executable, _STAGE_RUNNER_SCRIPT, str(task_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach from dispatcher's signal handling
        )
        _stage_runners[task_id] = proc
        (task_dir / ".runner.pid").write_text(f"{proc.pid}\n")
        log.info("spawned stage-runner pid=%d for %s", proc.pid, task_id)
    except Exception:
        log.exception("failed to spawn stage-runner for %s", task_id)


def _reap_stage_runners() -> None:
    """Collect finished stage-runners, log their exit codes."""
    finished = [tid for tid, p in _stage_runners.items() if p.poll() is not None]
    for tid in finished:
        p = _stage_runners.pop(tid)
        log.info("stage-runner for %s exited rc=%d", tid, p.returncode)


def _handle_signal(signum, frame):  # noqa: ARG001
    global _running
    log.info("received signal %s, draining", signum)
    _running = False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single sweep and exit")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    schema = _load_schema()
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("task_dispatcher up (poll=%ss, inbox=%s, max_stages=%d)",
             POLL_INTERVAL, INBOX_DIR, _MAX_CONCURRENT_STAGES)

    while _running:
        try:
            specs = _spec_files()
            for spec_path in specs:
                _ingest(spec_path, schema)
            _reap_stage_runners()
        except Exception:
            log.exception("dispatcher sweep failed")
        if args.once:
            break
        time.sleep(POLL_INTERVAL)

    # Graceful shutdown: terminate active stage runners
    if _stage_runners:
        log.info("shutting down %d active stage-runner(s)...", len(_stage_runners))
        for tid, proc in _stage_runners.items():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                log.warning("stage-runner %s did not terminate gracefully, killing", tid)
                try:
                    proc.kill()
                except Exception:
                    pass
        _stage_runners.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
