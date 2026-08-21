"""Runner-state side effects — worklog, history, state.json mutations.

Extracted from stage_runner_agent.py (god-module split, 2026-06-04). These are
the task-folder side effects run_pipeline produces so the rest of the system
(bot, watcher, /status) can observe progress: worklog.md lines, history[] in
state.json, and the top-level state.* fields. Pure persistence: imports only
stdlib, nothing from sibling dispatcher modules. (The single-runner flock stays
in the orchestrator — its process-lifetime fd is part of run_pipeline's lifecycle.)
"""
from __future__ import annotations

import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _vanished(task_dir: Path, op: str) -> bool:
    """True when the task dir is gone — DROP the write (#18).

    A park moves the dir to ``awaiting-input/`` while a stage may still be
    running; the late writer must not resurrect the old location. These calls
    are progress breadcrumbs, so losing one is strictly better than a
    split-brain directory (or an exception from a background thread).
    """
    if task_dir.is_dir():
        return False
    print(f"warn: task dir {task_dir} vanished (moved/parked) — dropping {op}",
          file=sys.stderr)
    return True


def _append_worklog(task_dir: Path, note: str) -> None:
    if _vanished(task_dir, "worklog line"):
        return
    line = f"- {_now_iso()} — {note}\n"
    worklog = task_dir / "worklog.md"
    try:
        if not worklog.exists():
            worklog.write_text(f"# Worklog\n\n{line}")
        else:
            with worklog.open("a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(line)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:  # moved between the check and the write
        if not _vanished(task_dir, "worklog line"):
            raise


def _update_state(task_dir: Path, **kwargs: object) -> None:
    if _vanished(task_dir, "state update"):
        return
    state_path = task_dir / "state.json"
    try:
        with state_path.open("r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            state = json.loads(f.read())
            state.update(kwargs)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state, indent=2) + "\n")
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        if not _vanished(task_dir, "state update"):
            raise


def _append_history(task_dir: Path, stage: str, note: str) -> None:
    if _vanished(task_dir, "history entry"):
        return
    state_path = task_dir / "state.json"
    try:
        with state_path.open("r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            state = json.loads(f.read())
            state.setdefault("history", []).append(
                {"at": _now_iso(), "stage": stage, "note": note}
            )
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state, indent=2) + "\n")
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        if not _vanished(task_dir, "history entry"):
            raise
