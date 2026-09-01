"""Is a stage runner alive for a given task? — the single definition.

Extracted from ``watcher.py`` (T24) so that ``ops/atlas/aidstack.sh`` can ask
the same question before restarting or stopping the stack. It is deliberately
NOT reimplemented in shell: the check is not ``kill -0``. A pid file outlives
its process, pids get reused, and a bash one-liner comparing ``kill -0`` would
call a recycled pid a live runner and refuse a restart forever — or, worse,
diverge from what the watcher believes and let the two disagree about whether
a task is running. Two definitions of "ephemeral" drifting apart is what T20
had to clean up; one definition of "alive" is cheaper than repeating that.

Imports nothing from the dispatcher package, so the shell side pays a bare
interpreter start and no daemon module import.

CLI:
    python3 dispatcher/runner_liveness.py [tasks/active]
        Print the task id of every directory owned by a live runner, one per
        line. Exit 0 whether or not there are any — an empty list is an answer,
        not a failure. Exit 1 only if the directory cannot be read.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# The script names a live stage runner is expected to be executing. Kept here
# rather than in watcher.py because this module is now the authority.
RUNNER_SCRIPT_NAMES = ("stage_runner_agent.py",)

RUNNER_PID_FILE = ".runner.pid"


def pid_is_alive(pid: int, expected_task_id: str) -> bool:
    """Check whether *pid* is a live stage runner for *expected_task_id*.

    The cmdline check is what makes this safe against pid reuse: a recycled pid
    belongs to some unrelated process whose command line mentions neither a
    runner script nor this task id.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    # Portable cmdline read: /proc/{pid}/cmdline is Linux-only (no procfs on
    # macOS). `ps -o command=` reports the full command line on both Linux and
    # macOS, so shell out instead of reading procfs directly.
    try:
        cmdline = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False

    if not cmdline.strip():
        return False

    parts = cmdline.split()
    return (
        any(name in cmdline for name in RUNNER_SCRIPT_NAMES)
        and any(expected_task_id in p for p in parts)
    )


def runner_is_dead(task_dir: Path, task_id: str) -> bool:
    """True when no live runner owns this task (dead / absent .runner.pid)."""
    pid_file = task_dir / RUNNER_PID_FILE
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if pid_is_alive(pid, task_id):
                return False
        except (ValueError, OSError):
            pass
    return True


def live_runner_task_ids(active_dir: Path) -> list[str]:
    """Task ids under *active_dir* that a live runner currently owns.

    A missing active/ directory is an empty list, not an error: a stack that
    has never run a task is a legitimate state for the caller to act on.
    """
    try:
        entries = sorted(p for p in active_dir.iterdir() if p.is_dir())
    except FileNotFoundError:
        return []

    return [p.name for p in entries if not runner_is_dead(p, p.name)]


def main(argv: list[str]) -> int:
    active = Path(argv[1]) if len(argv) > 1 else Path("tasks/active")
    try:
        for task_id in live_runner_task_ids(active):
            print(task_id)
    except OSError as exc:
        print(f"cannot read {active}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
